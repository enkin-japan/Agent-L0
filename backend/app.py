from pathlib import Path
import asyncio
import os
import time
import yaml
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

# 注：silero_vad 刻意**不在顶层 import**——它会连带 import torch(385M)。
# 它只服务原生音频直喂（modalities 含 "audio"），故改为按需惰性载入，见 lifespan。

# 导入解耦后的架构组件
from framework.mcp.registry import MCPRegistry
from framework.mcp.tools.skill_invoke import SkillInvokeTool
from framework.core.skill_registry import SkillRegistry
from framework.core.engine import AgentEngine
from framework.core.orchestrator import Orchestrator
from framework.core.scheduler import TaskScheduler, ClassPolicy
from framework.core.turn_runner import TurnRunner
from framework.memory.session import SessionManager
from framework.ingestion import Ingestion
from framework.utils.file_handler import FileHandler
from framework.utils.path_resolver import resolve_model_path, resolve_tokenizer_only_path
from framework.memory.compactor import maybe_summarize_context
from framework.memory import dream as notes_dream
from framework.maintenance import drift_patrol
from framework.api.routes import router, _maybe_summarize_active
from framework.api.auth import verify_api_key


async def _idle_maintenance_loop(session_manager, scheduler, orchestrator, interval: int = 120):
    """
    空闲维护回合（方案A 低阈值路径）：定期扫描空闲但未冻结的会话，对越过低阈值者
    在其被 freeze 释放 KV 前先做一次摘要压缩——此时 KV 反正要释放，压缩近乎免费，
    还顺带缩小冷恢复要重放的量。摘要走调度器低优先级，绝不打扰活跃任务。
    """
    while True:
        try:
            await asyncio.sleep(interval)
            now = time.time()
            idle_before = orchestrator.summary_idle_seconds
            for sid, session in list(session_manager._all_sessions.items()):
                if session.is_cold or scheduler.is_active(sid) or session.lock.locked():
                    continue
                if now - session.last_access_time < idle_before:
                    continue
                if session.last_prompt_tokens < orchestrator.summary_low_threshold:
                    continue
                async with session.lock:
                    if session.is_cold or scheduler.is_active(sid):
                        continue  # 拿锁后复检
                    await scheduler.acquire(sid, kind="summarize", priority=-1)
                    try:
                        n = await maybe_summarize_context(
                            session, orchestrator.engine,
                            threshold_tokens=orchestrator.summary_low_threshold,
                            keep_tail_turns=orchestrator.summary_keep_tail_turns,
                            last_prompt_tokens=session.last_prompt_tokens,
                            sediment=orchestrator.sediment_to_rag,
                        )
                        if n:
                            print(f"[Maintenance] 空闲摘要压缩 {sid}：折叠 {n} 轮（低阈值）")
                    finally:
                        # 复位抢占取消标志（ClassPolicy 抢占后台摘要时置位），防止污染下一轮
                        session.cancel_requested = False
                        await scheduler.release(sid)

            # dream 式笔记整理（评估采纳）：全系统安静时才做（任何会话在忙/在调度即让路——
            # 整理梦占一次生成，不能顶到用户轮次前面）。闸门（时限+变动数）在 dream 内部。
            # T1 压缩后空闲预热：上下文被改写（context_dirty）的热会话，趁闲把
            # [system]+[摘要]+[尾部] 预填回缓存——把 2-4s 重预填挪出用户等待路径。
            # 纪律与空闲摘要一致：锁+调度器低优先级，拿锁后复检
            for sid, session in list(session_manager._all_sessions.items()):
                if session.is_cold or not getattr(session, "context_dirty", False):
                    continue
                if scheduler.is_active(sid) or session.lock.locked():
                    continue
                async with session.lock:
                    if session.is_cold or scheduler.is_active(sid) \
                            or not session.context_dirty:
                        continue
                    await scheduler.acquire(sid, kind="prewarm", priority=-1)
                    try:
                        await orchestrator.prewarm_session_cache(session)
                    except Exception as e:
                        print(f"[Maintenance] 空闲预热失败（下一轮冷路兜底）: {e}")
                    finally:
                        session.cancel_requested = False
                        await scheduler.release(sid)

            busy = any(
                s.lock.locked() or scheduler.is_active(sid_)
                for sid_, s in list(session_manager._all_sessions.items()))
            if not busy:
                # T3 基座前缀缓存：幂等重建（工具集 rescan 变化后 key 变才真跑，
                # 平时是纯哈希比较的 no-op）
                try:
                    if await orchestrator.prewarm_base_cache():
                        print("[Maintenance] 基座前缀缓存已重建（工具集/提示词变化）")
                except Exception as e:
                    print(f"[Maintenance] 基座前缀预填失败（冷路兜底）: {e}")
                applied = await notes_dream.maybe_dream(
                    orchestrator.engine, orchestrator.notes_dream_cfg)
                if applied:
                    print(f"[Maintenance] dream 笔记整理：应用 {applied} 项操作")
                # 依赖时效巡检（只报不修）：每回合最多探一项，节流/告警全在模块内。
                # 不占生成位（纯网络 IO），但同样只在全系统安静时做——canary 打外网
                # 不该与用户轮次抢带宽
                await drift_patrol.maybe_patrol(
                    orchestrator.registry, orchestrator.drift_patrol_cfg,
                    DATA_DIR / "maintenance")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Maintenance] 空闲维护回合异常（已忽略，继续）: {e}")


async def _session_cleanup_loop(session_manager, cfg: dict):
    """确定性磁盘回收（session_cleanup）：周期性回收过期会话（pin/调度活跃/进行中一律跳过）。
    先 sleep 再扫——启动不被拖慢，且 recover_inflight_turns 已在装配阶段先行完成。
    单目录异常在 delete_session 内隔离，整轮异常在此隔离，绝不掀翻循环。"""
    interval = int(cfg.get("scan_interval_seconds", 3600))
    retention_days = float(cfg.get("retention_days", 30))
    empty_ttl_hours = float(cfg.get("empty_ttl_hours", 24))
    while True:
        try:
            await asyncio.sleep(interval)
            reaped = await session_manager.prune_expired_sessions(retention_days, empty_ttl_hours)
            if reaped:
                shown = ", ".join(reaped[:8]) + (" …" if len(reaped) > 8 else "")
                print(f"🧹 [Cleanup] 回收 {len(reaped)} 个过期会话: {shown}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"❌ [Cleanup] 清扫回合异常（已隔离，循环继续）: {e}")

# ==========================================
# 1. 路径与配置初始化
# ==========================================
BASE_DIR = Path(__file__).parent

# 「代码在哪」与「数据写哪」分家（R5-4/D.16）：BASE_DIR 从此只锚**只读内容**
# （代码树、剧本、自托管打印包）；一切运行时写入挂 DATA_DIR。dev 不设
# AGENT_DATA_DIR 时两者相同（backend/），行为与分家前逐字节一致；app 模式注入
# ~/Library/Application Support/dev.enkin.macagent/——bundle 是签名的，写进去即破坏签名。
from framework.utils.config_loader import load_config, data_dir
DATA_DIR = data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)   # 全新数据根（app 首启）时各消费方的父目录

SESSIONS_DIR = DATA_DIR / "sessions"
SCHEDULES_DIR = DATA_DIR / "schedules"  # 定时任务实体（与 sessions 平级，防会话扫描误认）
NOTES_DIR = DATA_DIR / "notes"          # 长期记忆笔记（R4）：用户个人事实，绝不进代码树
TOOLS_DIR = BASE_DIR / "framework" / "mcp" / "tools"
SKILLS_DIR = BASE_DIR.parent / "skills"      # 剧本是内容不是代码，与 backend/ 平级

# 配置在仓库根（backend 与 rag 共用一份），寻址统一走 config_loader
config = load_config()

# 模型来源：.model-selection 指针（./devctl.sh model 写入）→ 注册表条目；无选择时用
# config 默认。解析逻辑与 scripts/fetch_model.py 共用同一函数，保证下载与加载指向一致。
from framework.utils.model_selection import resolve_selected_model, model_options
MODEL, MODEL_REVISION, _model_source = resolve_selected_model()
MODEL_OPTIONS = model_options(MODEL)  # 注册表条目里的模型级行为开关（表外模型为 {}）
AUDIO_MAX_LENGTH = config["inference"]["audio_max_length"]
AGENT_HOST = config["inference"]["host"]
AGENT_PORT = config["inference"]["port"]

# 鉴权与跨域（P0）：密钥优先取环境变量 AGENT_API_KEY，回落 config.yaml 的 server.api_key。
# 仓库内 config 为占位空串；为空即"未启用"（启动告警后放行）。允许的浏览器直连源走白名单。
_SERVER_CFG = config.get("server") or {}
API_KEY = os.environ.get("AGENT_API_KEY") or _SERVER_CFG.get("api_key") or ""
ALLOWED_ORIGINS = _SERVER_CFG.get("allowed_origins") or ["http://localhost:5173"]
# 设备级票据（§7 预留 1）：留空 = 单密钥期，行为与预留前全同。填了之后每台设备一张票，
# 吊销一台不牵连其余（原生 app / iOS 端自持凭据时启用）。
AUTH_DEVICES = (config.get("auth") or {}).get("devices") or {}


def is_loopback_bind(host: str) -> bool:
    """绑定地址是否只对本机可达。空串/0.0.0.0/:: = 绑全部网卡，最不回环的一种。"""
    return str(host).strip() in ("127.0.0.1", "localhost", "::1", "127.0.0.0/8")


MIN_KEY_LEN = 16  # 非回环绑定的密钥最短长度：空或过短（含 "test" 弱值）一律拒启


def enforce_bind_guardrail(host: str, key: str, *, service: str, key_env: str) -> None:
    """部署铁则（§8 / §7 预留 2）：非回环绑定 + 密钥缺失或过弱（长度 < 16）= 拒启。

    此前这条只是文档里的散文，而"绑到 Tailscale 地址"与"设了密钥"是两个独立动作——
    先改 host 后忘了 key，服务就对整个 tailnet 裸奔且**只打印一行告警**（谁看启动日志？）。
    edge/gpu profile 让非回环绑定成为常规操作，这条必须由代码强制。
    密钥过短/过弱（如占位 "test"）同样拒绝——弱钥等于没钥。
    回环绑定时空密钥仍放行（dev 现状不变）。
    """
    if is_loopback_bind(host) or len(key or "") >= MIN_KEY_LEN:
        return
    raise SystemExit(
        f"❌ [{service}] 拒绝启动：绑定到非回环地址 {host!r} 但 {key_env} 缺失或过弱"
        f"（长度 {len(key or '')} < {MIN_KEY_LEN}）——服务会对整个可达网络裸奔。\n"
        f"   密钥过短/过弱同样拒绝。请 `./devctl.sh keygen` 生成强随机值（两侧同值，"
        f"app 形态填进设置页），或把绑定地址改回 127.0.0.1。\n"
        f"   （部署铁则 §8：非回环绑定只允许出现在受信内网 + 必须带 Bearer。）"
    )

# ==========================================
# 2. 生命周期与依赖装配 (IoC Container)
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 推理承载（B1）先于路径解析定夺：mode=remote 时本进程只当编排端，权重在设备 A 的
    # inference-server 里——这里若走全量 resolve_model_path，edge 机会照样拉满 4.9G，
    # devctl 的 --tokenizer-only 预取也拦不住（那只让分词器先到位，全量解析看的是
    # .download_complete 标记，缺了就重下）。故按 mode 选解析器，别按 profile 旗标。
    _inference_cfg = config.get("inference") or {}
    _mode = str(_inference_cfg.get("mode", "inprocess"))
    _remote = _mode == "remote"
    print(f"🚀 [System] {'resolving tokenizer for' if _remote else 'loading model:'} {MODEL}"
          + ("（来自 .model-selection 启动选择）" if _model_source == "selection" else "")
          + ("（mode=remote：只读分词器，不拉权重）" if _remote else ""))
    model_path = (resolve_tokenizer_only_path(MODEL, revision=MODEL_REVISION) if _remote
                  else resolve_model_path(MODEL, revision=MODEL_REVISION))

    # VAD 只服务**原生音频直喂**（把音频切成 ≤29s 块喂多模态模型），而它由
    # inference.modalities 含 "audio" 才启用（config.yaml 默认 ["text","image"]
    # → 音频一律走 ASR 转写，不经 VAD）。此前这里无条件加载：默认配置下 torch(385M)
    # + silero 被拉进内存却从不参与音频路——edge 打包时这是纯粹的 385M 死重（R5-2）。
    # 惰性 import 同样必要：`from silero_vad import ...` 本身就会 import torch。
    _native_audio = "audio" in ((config.get("inference") or {}).get("modalities") or ["text", "image"])
    vad_model = None
    if _native_audio:
        print("🎵 [System] loading model: Silero VAD（原生音频直喂已启用）")
        from silero_vad import load_silero_vad
        vad_model = load_silero_vad()
    else:
        print("🎵 [System] 跳过 Silero VAD（modalities 不含 audio → 音频走 ASR 转写）")

    print("⚙️  [System] Initializing architecture components...")
    # 0. 长期记忆类目就绪（Stage 3）：在扫描 MCP 工具前确保 agent_memory 类目存在，
    #    好让 rag_search 在启动探测类目时就把它列为可召回类目（RAG 离线则静默跳过）。
    if (config.get("agent") or {}).get("sediment_to_rag", True):
        from framework.memory.rag_sink import ensure_memory_category
        await asyncio.to_thread(ensure_memory_category)

    # 0.0b 长期记忆笔记本（R4）：与上面同一个理由必须先于 MCPRegistry——rag_search 在
    #      __init__ 里探测类目来构建自己的 schema，agent_notes 晚了它就不认识，笔记正文
    #      再也搜不到。笔记本句柄同样先于工具扫描登记（memory_write 的探活要看它）；
    #      与 skill_invoke 不同，这里没有互为前置的环，一段装配即可。
    from framework.agents import runtime as research_runtime
    from framework.memory.notes import NoteStore
    from framework.memory.rag_sink import ensure_notes_category
    research_runtime.set_note_store(NoteStore(NOTES_DIR))
    await asyncio.to_thread(ensure_notes_category)

    # 0.1 定时任务仓库：须先于 MCPRegistry 注入进程全局句柄（schedule_* 工具经
    #     framework.schedule.get_store() 取用，对齐 agents/runtime.py 惯例）。
    from framework.schedule import ScheduleStore, ScheduleRunner, set_store
    schedules_cfg = config.get("schedules") or {}
    schedule_store = ScheduleStore(
        SCHEDULES_DIR,
        run_history_keep=int(schedules_cfg.get("run_history_keep", 50)),
        min_interval_minutes=int(schedules_cfg.get("min_interval_minutes", 5)),
    )
    set_store(schedule_store)

    # 1. 插件层：扫描并注册所有 MCP 工具
    mcp_registry = MCPRegistry(tools_dir=TOOLS_DIR)

    # 1b. 剧本层（R2a）：skill = 主轮次的提示词剧本，工具沿用主循环 → 审批闸门照常生效。
    #     两段装配是因为两表互为前置：剧本的 requires 探活要看工具表（故必在 MCPRegistry
    #     之后），而 skill_invoke 的探活要看剧本表（故上一步的目录扫描必然跳过它，此处补注册）。
    skill_registry = SkillRegistry(SKILLS_DIR, tool_names=mcp_registry.tool_names(),
                                   user_dir=data_dir() / "skills")
    research_runtime.set_skill_registry(skill_registry)
    mcp_registry.load_tool(SkillInvokeTool())
    
    # 2. 领域层：推理引擎（单次生成）+ 编排循环（计划推进/失败路由/终止保护）。
    #    经 AgentEngine.create 在专用推理线程内加载模型并装配——推理宇宙(权重/惰性
    #    常量/KV/生成)同属一个线程,事件循环全程自由(prefill 不再饿死 API)。
    #    推理承载（B1）：mode=inprocess（现状默认）在本进程加载所选后端；
    #    mode=remote 改装 RemoteBackend（backends/remote/）连 inference-server——
    #    执行层/会话数据留在本机，本进程不加载权重（只在本地读分词器）。
    _backend_name = "remote" if _remote else str(_inference_cfg.get("backend") or "")
    if _remote:
        _r = _inference_cfg.get("remote") or {}
        print(f"🔌 [System] 推理模式 remote：连接 inference-server "
              f"{_r.get('host', '127.0.0.1')}:{_r.get('port', 14161)}（本进程不加载权重）")
    engine = await AgentEngine.create(
        model_path, kv_config=_inference_cfg,
        # 注册表条目整条透传（R0）：方言名与全部模型级开关（suppress_ghost_thinking 等）
        # 由方言自行解读，引擎不解读——换模型家族不动 app.py 与 engine 的签名。
        model_options=MODEL_OPTIONS,
        env=(config.get("agent") or {}).get("environment") or {},
        backend=_backend_name,
        max_concurrency=int((config.get("agent") or {}).get("max_inference_concurrency", 1)),
    )
    model, processor = engine.model, engine.processor
    orchestrator = Orchestrator(engine, mcp_registry, config.get("agent") or {})

    # 2.1 联网调查沙箱运行时：把引擎与主事件循环登记给 web_research（受限沙箱工具经
    #     run_coroutine_threadsafe 投回本循环复用主模型生成，见 framework/agents）。
    research_runtime.set_runtime(engine, asyncio.get_running_loop())
    #     客户端反查通道（get_location 等）：工具线程经此 loop 往会话事件流发请求事件
    from framework.core import client_query
    client_query.set_loop(asyncio.get_running_loop())

    # 2.2 工具注册表热重扫（D 阶段工程票）：依赖后装（LibreOffice 案）不再要求重启
    #     执行层。编排与启动时的两段装配同构：重探工具 → 重建剧本表（requires 要看
    #     新工具面）→ 二次重探（skill_invoke 的探活要看新剧本表）→ 刷新工具声明与
    #     摘要阈值锚点。只增不删（删除与进行中轮次竞态；服务死亡由执行期报错兜底）。
    def _rebuild_skills() -> "SkillRegistry":
        """重建剧本表（含用户目录）并登记到 runtime，返回新表。热重扫与剧本
        保存/删除端点共用——重扫式注册表，改动即时生效。"""
        new_skills = SkillRegistry(SKILLS_DIR, tool_names=mcp_registry.tool_names(),
                                   user_dir=data_dir() / "skills")
        research_runtime.set_skill_registry(new_skills)
        return new_skills

    def _rescan_tools() -> dict:
        newly = mcp_registry.rescan_unavailable()
        new_skills = _rebuild_skills()
        newly += mcp_registry.rescan_unavailable()
        if newly:
            orchestrator.refresh_tools_declaration()
            print(f"🔄 [System] 热重扫补注册工具: {newly}（声明与阈值已刷新）")
        return {"newly_available_tools": newly,
                "skills_available": new_skills.names()}
    research_runtime.set_rescan_hook(_rescan_tools)
    # 剧本保存/删除端点（POST/DELETE /api/v1/skills）经此即时重建剧本表（照 app.state.feed 形状）
    app.state.rebuild_skills = _rebuild_skills


    # 3. 记忆层：初始化弹性冷热会话管理 (默认闲置 30 分钟冻结)。
    #    全局任务 feed(Phase 3):各会话账本的生命周期事件镜像于此,
    #    GET /api/v1/events 推送给任务中心/通知(替代 /tasks 差分轮询)。
    from framework.core.feed import GlobalFeed
    feed = GlobalFeed()
    session_manager = SessionManager(sessions_dir=SESSIONS_DIR, idle_timeout=1800)
    session_manager.feed = feed
    # 推理后端注入：会话缓存句柄（new/clone/release）的生命周期属主（P0 协议面）
    session_manager.backend = engine.backend
    app.state.feed = feed

    # 3.0 中断轮次恢复（write-ahead 闭环）：上次进程死亡时进行中/待审批/挂起的轮次，
    #     按残留的 inflight.json 补 fail 轮存档——用户输入不蒸发、会话窗口可见死因。
    recovered = await asyncio.to_thread(session_manager.recover_inflight_turns)
    if recovered:
        print(f"🩹 [System] 恢复 {recovered} 个被进程中断的轮次存档（输入已保留）")

    # 3.1 调度层：全局单 worker 任务调度器（默认 FIFO、不抢占；策略可后续热替换为自动调度）。
    #     注入会话管理器：驱逐器据此避免冻结掉排队/运行/挂起中任务的内存 KV。
    #     on_suspend_request 把手动/自动的挂起意图传播到会话，编排循环在迭代边界据此让出。
    def _flag_session_suspend(sid: str):
        s = session_manager._all_sessions.get(sid)
        if s is not None and not s.is_cold:
            s.suspend_requested = True

    # on_cancel_request：交互任务到达时抢占运行中的后台摘要（cancel 原语，摘要可安全放弃；
    # 执行侧 finally 复位 cancel_requested，见 routes._maybe_summarize_active 与上方空闲维护回合）
    def _flag_session_cancel(sid: str):
        s = session_manager._all_sessions.get(sid)
        if s is not None and not s.is_cold:
            s.cancel_requested = True

    # 排队位次广播(Phase 3):位次变化写入对应会话账本(queue_moved 事件)。
    # 排队中的会话必然是热会话(受理即建),取不到就静默跳过。
    def _publish_queue_positions(positions: dict):
        for sid, pos in positions.items():
            s = session_manager._all_sessions.get(sid)
            if s is not None and not s.is_cold:
                s.journal.emit("queue_moved", {"position": pos})

    sched_cfg = (config.get("agent") or {}).get("scheduler") or {}
    scheduler = TaskScheduler(
        policy=ClassPolicy(
            aging_seconds=float(sched_cfg.get("aging_seconds", 300)),
            preempt_background=bool(sched_cfg.get("preempt_background", True)),
        ),
        on_suspend_request=_flag_session_suspend,
        on_cancel_request=_flag_session_cancel,
        on_queue_positions=_publish_queue_positions,
    )
    session_manager.scheduler = scheduler
    
    # 3.2 轮次执行层：TurnRunner —— 轮次生命周期的唯一属主（受理/执行/终态分流/落盘/
    #     账本生命周期事件）。routes 的对话端点全部是它的薄适配器（新旧契约共用一套闭环）。
    async def _summarize_hook(session, sid):
        await _maybe_summarize_active(session, orchestrator, scheduler, sid)

    turn_runner = TurnRunner(orchestrator, scheduler, summarize_after_turn=_summarize_hook)

    # 4. 基础设施层：文件处理器（图像落盘 + 原生音频 VAD 切片）
    file_handler = FileHandler(vad_model=vad_model, audio_max_length=AUDIO_MAX_LENGTH)

    # 4.1 摄入解析层：按模态分路（图像原生 / 音频等转写 / 代码大文件懒登记）。
    #     能力声明取自 inference.modalities（默认 [text,image]，音频转写）；复用 file_handler
    #     的图像落盘与原生音频 VAD 切片（能力含 audio 时）。
    ingestion = Ingestion(
        inference_cfg=config.get("inference") or {},
        ingestion_cfg=config.get("ingestion") or {},
        file_handler=file_handler,
    )

    # 5. 依赖注入：挂载到 FastAPI 全局上下文，供 API 路由层提取
    app.state.model = model
    app.state.processor = processor
    app.state.vad_model = vad_model
    app.state.engine = engine
    app.state.orchestrator = orchestrator
    app.state.mcp_registry = mcp_registry
    app.state.session_manager = session_manager
    app.state.scheduler = scheduler
    app.state.turn_runner = turn_runner
    app.state.file_handler = file_handler
    app.state.ingestion = ingestion

    # 6. 维护层：启动空闲摘要压缩的后台回合（方案A 低阈值路径）
    maintenance_task = asyncio.create_task(
        _idle_maintenance_loop(session_manager, scheduler, orchestrator)
    )
    app.state.maintenance_task = maintenance_task

    # 6b. T3 基座前缀缓存：启动即后台预填（~16s，M5 实测 9424 token）。用户若抢先
    # 开聊，其首轮在推理锁上排队——但排的正是同一段前缀的预填工作，总时长不变；
    # 预填先完成则此后每个新会话省下整段冷预填。失败只损失优化，冷路兜底。
    async def _startup_prewarm():
        try:
            await orchestrator.prewarm_base_cache()
        except Exception as e:
            print(f"[App] 基座前缀预填失败（冷路兜底）: {e}")
    app.state.base_prewarm_task = asyncio.create_task(_startup_prewarm())

    # 6.1 清理层：确定性磁盘回收的后台清扫（默认自动过期、pin 才长留；见 config.session_cleanup）。
    #     依赖 session_manager.scheduler（已于上方注入），故置于其后；recover_inflight_turns 已先行。
    cleanup_cfg = config.get("session_cleanup") or {}
    cleanup_task = None
    if cleanup_cfg.get("enabled", True):
        cleanup_task = asyncio.create_task(_session_cleanup_loop(session_manager, cleanup_cfg))
        app.state.cleanup_task = cleanup_task
        print(f"🧹 [System] 会话自动清理已启用（保留 {cleanup_cfg.get('retention_days', 30)} 天 / "
              f"空壳 {cleanup_cfg.get('empty_ttl_hours', 24)} 小时 / "
              f"周期 {cleanup_cfg.get('scan_interval_seconds', 3600)}s）")

    # 7. 定时任务层：触发扫描循环（due 任务经 kind="scheduled"/priority=-1 无人值守执行，
    #    交互任务到达时被 suspend 抢占让位，见 framework/schedule/runner.py）
    schedule_runner = ScheduleRunner(
        schedule_store, session_manager, orchestrator, scheduler, schedules_cfg
    )
    schedule_tick_task = asyncio.create_task(schedule_runner.tick_loop())
    app.state.schedule_store = schedule_store
    app.state.schedule_runner = schedule_runner
    app.state.schedule_tick_task = schedule_tick_task

    # 7.5 推送层（CHARTER 铁律 2 预留的"通知读者"）：dispatcher 订阅全局 feed 把生命周期
    #     事件映射为 APNs 推送（iOS）；Live Activity 按 app 上报的活动 token 附着会话账本。
    #     缺 .p8 密钥时 APNs 客户端不建，dispatcher 保持注册表可用但一个字节不发（macOS
    #     本地通知不依赖本层）。纯出站 HTTPS，回环绑定的隐私边界不变。
    from framework.push import APNsClient, APNsDisabled, PushRegistry, NotificationDispatcher
    push_cfg = config.get("push") or {}
    push_registry = PushRegistry(DATA_DIR / "push")
    apns_client = None
    if push_cfg.get("enabled", True):
        # 密钥相对路径 = 相对仓库根（config.yaml 所在处）：`.keys/AuthKey_X.p8` 在
        # 开发机/远程机都指向各自仓库副本里的同一位置，不受进程启动目录影响。
        # bundle 形态（R5-9）仓库根 = 只读 Resources/，私钥永不打进 app 包——
        # 相对路径落空时兜底到数据根（Application Support，用户级权限），放那即启用。
        key_path = str(push_cfg.get("apns_key_path") or "")
        if key_path and not Path(key_path).expanduser().is_absolute():
            primary = BASE_DIR.parent / key_path
            key_path = str(primary if primary.exists() else DATA_DIR / key_path)
        try:
            apns_client = APNsClient(
                key_path,
                str(push_cfg.get("apns_key_id") or ""),
                str(push_cfg.get("apns_team_id") or ""),
            )
            print("📮 [System] APNs 推送已启用（iOS 横幅 + Live Activity）")
        except APNsDisabled as e:
            print(f"📮 [System] iOS 推送未启用（{e}）——设备注册照常，macOS 本地通知不受影响")
    push_dispatcher = NotificationDispatcher(
        feed, push_registry, apns_client, session_manager, push_cfg, SESSIONS_DIR)
    push_dispatcher.start()
    app.state.push_registry = push_registry
    app.state.push_dispatcher = push_dispatcher

    if AUTH_DEVICES:
        print(f"🔐 [System] API 鉴权已启用（设备级票据 ×{len(AUTH_DEVICES)}"
              + ("；共享密钥同时有效" if API_KEY else "") + "）")
    elif API_KEY:
        print("🔐 [System] API 鉴权已启用（Bearer 共享密钥）")
    else:
        print("⚠️  [System] API 鉴权未启用：未设置 AGENT_API_KEY / server.api_key，接口对可达网络裸奔。"
              "\n    生产务必设置 AGENT_API_KEY 环境变量。")

    print(f"✅ [System] Agent has started and is listening on {AGENT_HOST}:{AGENT_PORT}")
    yield

    # 停机：先停后台维护回合、清扫回合、定时任务触发循环与推送分发器
    maintenance_task.cancel()
    if cleanup_task is not None:
        cleanup_task.cancel()
    schedule_tick_task.cancel()
    try:
        await push_dispatcher.stop()
    except Exception:
        pass

    # 优雅停机：安全清理物理与内存资源
    print("\n🛑 [System] An exit signal has been received, resources are being cleaned up....")
    try:
        # 清理所有活跃的会话缓存
        if hasattr(app.state, 'session_manager'):
            for session in list(app.state.session_manager._all_sessions.values()):
                session.freeze()
                
        # 卸载模型组件
        del app.state.model
        del app.state.processor
        del app.state.vad_model
        del app.state.engine
        del app.state.orchestrator
        del app.state.session_manager
        del app.state.file_handler

        # 释放推理后端持有的全局资源（MLX 下即强制释放苹果芯片的显存/内存）
        engine.backend.shutdown()
        print("✅ [System] Resources have been safely released. Goodbye!")
    except Exception as e:
        print(f"❌ [System] Resource release error, force termination: {e}")

# ==========================================
# 3. FastAPI 表现层服务配置
# ==========================================
app = FastAPI(
    title="Agent API",
    description="Based on MLX Framework and Domain Driven Design",
    version="2.0.0",
    lifespan=lifespan
)

# 鉴权密钥挂到 app.state，供 verify_api_key 依赖按请求读取（避免每次触碰环境变量）
app.state.api_key = API_KEY
app.state.auth_devices = AUTH_DEVICES   # §7 预留 1：空 dict = 单密钥期（现状）

# CORS 中间件：收敛为白名单。走 header Bearer（非 cookie）不需要 credentials；
# 且前端经 Vite/Nginx 代理为同源，CORS 仅在浏览器直连 14159 时才触发，此处为纵深防御。
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 免鉴权健康探活：挂在 app 上（不经 router 的鉴权依赖），供 devctl/反代/负载均衡就绪探测。
# uvicorn 在 lifespan 启动完成（模型加载完）后才开始受理请求，故 200 即代表已就绪。
@app.get("/health")
async def health():
    return {"status": "ok"}

# 自托管 deck 打印页(Phase 4b 解除后端→前端倒挂):`./devctl.sh bundle-print` 把
# 前端构建产物放进 backend/static/webapp,这里原样出货——前端服务不在时,
# deck_export_pdf 打印本机 14159 的这份(渲染器仍是同一份 React 代码的构建物)。
# 页面 shell 免鉴权(只是渲染器外壳);数据请求由 pdf_export 经 Playwright 注入 Bearer。
_PRINT_BUNDLE = BASE_DIR / "static" / "webapp"
if (_PRINT_BUNDLE / "index.html").exists():
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    app.mount("/assets", StaticFiles(directory=_PRINT_BUNDLE / "assets"), name="print-assets")

    @app.get("/print/deck")
    async def print_deck_page():
        return FileResponse(_PRINT_BUNDLE / "index.html")

    print("🖨️  [System] deck 打印页自托管已启用（backend/static/webapp）")

# 挂载解耦后的路由：全量 /api/v1/* 套用 Bearer 鉴权依赖（未配置密钥时依赖内部放行）
app.include_router(router, dependencies=[Depends(verify_api_key)])

# ==========================================
# 4. 启动器
# ==========================================
if __name__ == "__main__":
    import uvicorn
    # 绑定护栏先于加载模型：非回环 + 空密钥立即拒启，不浪费几十秒权重加载才发现
    enforce_bind_guardrail(AGENT_HOST, API_KEY, service="backend", key_env="AGENT_API_KEY")
    # 建议不要在此处使用 reload=True，MLX 模型反复重载会导致严重内存泄漏
    # timeout_graceful_shutdown：默认 None = 无限等连接排空——挂着 SSE 长连时
    # SIGTERM 形同虚设（实机 2026-07-17：孤儿 backend 无视 TERM 20s 被 -9），
    # 收摊方一旦在升级 -9 前被打断（quit 看门狗）就留孤儿。5s 足够冻结落盘。
    uvicorn.run(app, host=AGENT_HOST, port=AGENT_PORT, timeout_graceful_shutdown=5)