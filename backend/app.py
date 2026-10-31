from pathlib import Path
import asyncio
import hashlib
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
from framework.memory.retention import session_cleanup_policy
from framework.ingestion import Ingestion
from framework.utils.file_handler import FileHandler
from framework.utils.path_resolver import resolve_model_path, resolve_tokenizer_only_path
from framework.memory.compactor import maybe_summarize_context, sediment_pending_turns
from framework.memory import dream as notes_dream
from framework.maintenance import drift_patrol
from framework.api.routes import router, _maybe_summarize_active, require_compute_ready
from framework.api.auth import verify_api_key
# 两张附加路由表（挂载见文件末尾，鉴权/就绪依赖各不相同）：离线文件只读面与 iPhone 扫码配对。
from framework.api.offline_files import router as offline_files_router
from framework.api.pairing import router as pairing_router

# 收摊时「协作结算活跃轮次」的总预算（秒）。合作取消是尽力而为：卡住的轮次
# 不能拖住 GPU/后端释放与 compute_state="stopped"，超时即取消剩余继续收摊。
SHUTDOWN_JOIN_SECONDS = 10       # 活跃轮次与定时任务各自一份，互不挤占
ENGINE_RELEASE_SECONDS = 10      # 推理线程退出的上限：到点不再等，照常释放后端
_LINGERING = set()               # 超时未退出的推理线程 join：留引用，免得被回收时报 pending


async def _idle_maintenance_loop(session_manager, scheduler, orchestrator, interval: int = 120,
                                 can_execute=None):
    """
    空闲维护回合（方案A 低阈值路径）：定期扫描空闲但未冻结的会话，对越过低阈值者
    在其被 freeze 释放 KV 前先做一次摘要压缩——此时 KV 反正要释放，压缩近乎免费，
    还顺带缩小冷恢复要重放的量。摘要走调度器低优先级，绝不打扰活跃任务。
    """
    while True:
        try:
            await asyncio.sleep(interval)
            if can_execute is not None and not can_execute():
                continue
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

            # 零 LLM 沉淀（波 F5）：把还没进 RAG 的已落盘轮次原文补进长期记忆。
            # **刻意不带 token 阈值**——上面那遍摘要压缩要越过低阈值才动，而短会话
            # （≤keep_tail_turns 轮）永远越不过，长期记忆产出因此恒为零，那正是本层要修的病。
            # 不占生成位、不改 model-facing 上下文，只有一次 RAG 写入；但要写 memory.json，
            # 故仍进会话锁（沿用上面那遍的纪律：拿锁后复检）。
            # 沉淀总开关关掉时连遍历都不做——否则每条空闲会话都要上一次锁，只为调一个
            # 第一行就返回 0 的函数（首审 #9）
            sediment_targets = (list(session_manager._all_sessions.items())
                                if orchestrator.sediment_to_rag else [])
            for sid, session in sediment_targets:
                if session.is_cold or scheduler.is_active(sid) or session.lock.locked():
                    continue
                if now - session.last_access_time < idle_before:
                    continue
                # 无事可做就**别碰锁**：稳态下（水位线正好追平落盘轮数）这一遍是纯读盘的
                # 空转。判据必须是**恰好相等**，不能写成 `>=`——水位线**高于**行数正是
                # 「分支只带前缀 / 删末轮截断」留下的越界态，那种会话恰恰需要进去修复；
                # 用 `>=` 会把它们永久挡在门外，修复分支从此在生产里跑不到，而守它的
                # 那条金测试也就成了「守一条产品进不去的路」（首审 #2，是我自审时加的）。
                if session.sedimented_turns == session.disk_turn_count():
                    continue
                async with session.lock:
                    # 拿锁后复检——**含空闲判定**：上面那个 lock.locked() 是 await 之前
                    # 看的，等锁期间用户完全可能已经发了新消息。持锁期间有一次网络写
                    # （最坏 ~15s），不复检就等于在用户打字时把会话锁住（首审 #4）。
                    if session.is_cold or scheduler.is_active(sid):
                        continue
                    if time.time() - session.last_access_time < idle_before:
                        continue
                    try:
                        n = await sediment_pending_turns(session)
                        if n:
                            print(f"[Maintenance] 零 LLM 沉淀 {sid}：{n} 轮原文入长期记忆")
                    except Exception as e:
                        print(f"[Maintenance] 零 LLM 沉淀失败（下一回合重试）: {e}")

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


async def _session_cleanup_loop(session_manager, cfg: dict, sediment_on_expire: bool = False):
    """确定性磁盘回收（session_cleanup）：周期性回收过期会话（pin/调度活跃/进行中一律跳过）。
    先 sleep 再扫——启动不被拖慢，且 recover_inflight_turns 已在装配阶段先行完成。
    单目录异常在 delete_session 内隔离，整轮异常在此隔离，绝不掀翻循环。"""
    cfg = session_cleanup_policy(cfg)
    interval = cfg["scan_interval_seconds"]
    retention_days = cfg["retention_days"]
    empty_ttl_hours = cfg["empty_ttl_hours"]
    while True:
        try:
            await asyncio.sleep(interval)
            reaped = await session_manager.prune_expired_sessions(
                retention_days, empty_ttl_hours, sediment_on_expire)
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
from framework.utils import tailnet_dns
DATA_DIR = data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)   # 全新数据根（app 首启）时各消费方的父目录

SESSIONS_DIR = DATA_DIR / "sessions"
SCHEDULES_DIR = DATA_DIR / "schedules"  # 定时任务实体（与 sessions 平级，防会话扫描误认）
NOTES_DIR = DATA_DIR / "notes"          # 长期记忆笔记（R4）：用户个人事实，绝不进代码树
TOOLS_DIR = BASE_DIR / "framework" / "mcp" / "tools"
SKILLS_DIR = BASE_DIR.parent / "skills"      # 剧本是内容不是代码，与 backend/ 平级

# 配置在仓库根（backend 与 rag 共用一份），寻址统一走 config_loader
config = load_config()
# 算力端固定 MagicDNS 名在换 VM 期间不存在，macOS 系统缓存的「无此主机」会远长于空窗；
# 这两个配置的算力端名系统解析失败时改问 Tailscale 自身解析器（详见模块注释，配合下方 loop="asyncio"）
tailnet_dns.install([((config.get("inference") or {}).get("remote") or {}).get("host"),
                     ((config.get("rag") or {}).get("compute") or {}).get("host")])

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


def preflight_bind_addr(host: str, *, service: str, cfg_key: str) -> None:
    """启动前本机绑定地址预检：host 若非本机任何网卡地址（EADDRNOTAVAIL）立即拒启。

    实机事故（2026-07-19）：算力端在 mini 上启动而 config 的 host 还填着另一台设备
    （Air）的 IP → uvicorn 报晦涩的 `[Errno 49] can't assign requested address`。
    这里先用一个临时 socket 试绑（端口 0）把「地址不属本机」提前成一条明确指引。
    回环/通配/空值不必检；域名解析失败（gaierror）等其余错误静默放行，留给真实 bind 报错。
    """
    import errno
    import socket
    h = str(host or "").strip()
    if h in ("", "127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
        return
    fam = socket.AF_INET6 if ":" in h else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.bind((h, 0))
    except OSError as e:
        if getattr(e, "errno", None) == errno.EADDRNOTAVAIL:
            raise SystemExit(
                f"❌ [{service}] 拒绝启动：绑定地址 '{h}' 不是本机任何网卡的地址（EADDRNOTAVAIL）。\n"
                f"   {cfg_key} 应填**本机**（对外提供服务的这台设备自己）的 IP。\n"
                f"   查看本机地址：`tailscale ip -4` 或 `ifconfig | grep 'inet '`；\n"
                f"   若该地址属于另一台设备，请到那台设备上启动本服务。"
            )
        # 其余错误（含 socket.gaierror 域名解析失败）静默放行，留给真实 bind 报错
    finally:
        s.close()


def require_remote_inference_host(cfg) -> None:
    """配置完整性：inference.mode=remote 但算力端地址为空 = 配置缺失，拒启。

    config.yaml 的 inference.remote.host 缺省为空（通用模板不钉任何机器 IP），机器专属
    地址落各机 .env.local（INFER_REMOTE_HOST）或 app 设置注入——两者经 load_config 的
    env 覆盖已在 cfg 里生效，故此处只看 cfg、不自己读 env。remote 模式下地址仍空 =
    没人告诉本进程去哪拨号，与其让 RemoteBackend 拨空地址晦涩失败不如启动即拒。
    分段缺失（inference/remote 键不存在或为 None）按空处理，不 AttributeError。
    """
    inf = cfg.get("inference") or {}
    if str(inf.get("mode", "inprocess")) != "remote":
        return
    host = str((inf.get("remote") or {}).get("host") or "").strip()
    if host:
        return
    raise SystemExit(
        "❌ [backend] 拒绝启动：inference.mode=remote 但算力端地址为空。\n"
        "   请填写算力端地址——app 形态：设置 → 执行层 → 算力端地址；dev 形态：.env.local 写\n"
        "   INFER_REMOTE_HOST=<算力端 tailnet IP>（或 config 的 inference.remote.host）；\n"
        "   或将 inference.mode 改为 inprocess（单机部署，本进程加载模型）。"
    )

# ==========================================
# 2. 生命周期与依赖装配 (IoC Container)
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    from contextlib import suppress
    from framework.api.compute_lifecycle import (
        ComputeLifecycle, blocking_call, finish_task, probe_remote_compute)
    from framework.core.feed import GlobalFeed
    from framework.schedule import ScheduleStore, ScheduleRunner, set_store
    from framework.agents import runtime as research_runtime

    global MODEL, MODEL_REVISION, MODEL_OPTIONS, _model_source
    enforce_bind_guardrail(AGENT_HOST, API_KEY, service="backend", key_env="AGENT_API_KEY")
    inference_cfg = config.get("inference") or {}
    _remote_transport = (str(inference_cfg.get("mode", "inprocess")) == "remote"
                         or inference_cfg.get("backend") == "remote")
    _discover_default = _model_source in ("default", "adopted")
    app.state.data_ready = False
    app.state.compute_ready = False
    app.state.compute_state = "starting"
    app.state.compute_installed = False
    # Contract H（PROTOCOL §2.10）：compute_state 是整个装配的态；下面三项拆开说它在等谁。
    app.state.engine_ready = False
    app.state.compute_node_state = "probing" if _remote_transport else "local"
    # 非 remote 形态本进程从不探 RAG（单机契约由 lib_edge 启动门把关）：如实报 skipped。
    app.state.rag_state = "probing" if _remote_transport else "skipped"
    # RAG 可用性单记一处：它不参与 compute_ready 判定（装配后掉线只影响检索类工具）。
    app.state.rag_ready = False
    feed = GlobalFeed()
    session_manager = SessionManager(sessions_dir=SESSIONS_DIR, idle_timeout=1800)
    session_manager.feed = feed
    app.state.feed = feed
    app.state.session_manager = session_manager
    schedules_cfg = config.get("schedules") or {}
    schedule_store = ScheduleStore(
        SCHEDULES_DIR,
        run_history_keep=int(schedules_cfg.get("run_history_keep", 50)),
        min_interval_minutes=int(schedules_cfg.get("min_interval_minutes", 5)))
    set_store(schedule_store)
    app.state.schedule_store = schedule_store
    recovered = await blocking_call(session_manager.recover_inflight_turns)
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
    from framework.api.devices import store_for
    push_dispatcher = NotificationDispatcher(
        feed, push_registry, apns_client, session_manager, push_cfg, SESSIONS_DIR,
        device_active=lambda device_id: store_for(app).active(device_id))
    push_dispatcher.start()
    app.state.push_registry = push_registry
    app.state.push_dispatcher = push_dispatcher

    # 崩溃轮补发 turn_ended（B2 代际围栏的另一半；清单来自 3.0 的恢复扫描）：
    # 只补 history 不发账本事件的话,纯事件流消费者永远停在 generating——重附着的
    # 客户端因水位余量游标仍有效,补发事件经重放送达即收敛。**必须在循环线程调用**
    # （磁盘扫描在 to_thread,emit 不许跟过去）,且必须在 dispatcher 订阅(上方 start)
    # 之后——feed 只推给在场订阅者,不回放环(首审 H1:早发一步,iOS 推送就真丢)。
    session_manager.emit_recovery_events(recovered)


    app.state.data_ready = True
    identity = None
    last_rescan = 0.0

    async def _probe_dependencies():
        """只有推理活体/鉴权决定 compute_ready；RAG 只是装配期前置，装完即与执行解耦。"""
        nonlocal identity, last_rescan
        from framework.api.compute_lifecycle import (
            LocalDependencyUnavailable, compute_node_failure_state, describe_failure,
            probe_rag_data, rag_failure_is_transient, rag_failure_state, rag_probe_host)
        remote = inference_cfg.get("remote") or {}
        installed = getattr(app.state, "compute_installed", False)
        # 装配期每次探活结果先落 app.state 再抛（/health 的 compute_node_state / rag_state）。
        # 装配后 compute_node_state 归生命周期监视器发布：与 compute_state 同一道连续失败
        # 门槛才翻，单个丢包周期不让 UI 闪、两个字段稳态下不打架。
        try:
            identity = await probe_remote_compute(
                f"http://{remote.get('host', '127.0.0.1')}:{int(remote.get('port', 14161))}",
                {"repo": MODEL, "revision": MODEL_REVISION},
                os.environ.get("INFER_API_KEY", ""), identity)
        except Exception as exc:
            if not installed:
                app.state.compute_node_state = compute_node_failure_state(exc)
            raise
        if not installed:
            app.state.compute_node_state = "ready"
        rag_cfg = config.get("rag") or {}
        try:
            await probe_rag_data(rag_cfg, os.environ.get("RAG_API_KEY", ""))
        except Exception as exc:
            was_ready = getattr(app.state, "rag_ready", None)
            app.state.rag_ready = False
            app.state.rag_state = rag_failure_state(exc)
            # 装配前 RAG 仍是前置（工具探活一次性，启动契约不变）；装配完成后 RAG 掉线
            # 只让 rag_search 失败（本次改动之前的行为），绝不把执行能力一起拉下来。
            # 装配前：算力端已过、只差**本机**数据面且只是还没起来（连接级失败/5xx）→
            # 标成本地依赖，生命周期按固定短间隔重试。钥匙错（401/403）是永久故障、
            # 远端 RAG 主机不归本机编队——两者都走普通退避，快速重试帮不了谁。
            if not installed:
                if is_loopback_bind(rag_probe_host(rag_cfg)) and rag_failure_is_transient(exc):
                    raise LocalDependencyUnavailable("RAG 数据面未就绪") from exc
                raise
            if was_ready is not False:
                print(f"[Compute] RAG 数据面不可用（{describe_failure(exc) or type(exc).__name__}）；"
                      f"检索类工具将失败，执行能力不受影响")
        else:
            if getattr(app.state, "rag_ready", None) is False:
                print("[Compute] RAG 数据面已恢复")
            app.state.rag_ready = True
            app.state.rag_state = "ready"
        if installed and time.monotonic() - last_rescan >= 30:
            last_rescan = time.monotonic()
            try:
                await blocking_call(app.state.rescan_tools)
            except Exception as exc:
                print(f"[Compute] 可选工具重扫失败（{type(exc).__name__}），下次重试")

    async def _close_engine(engine, label="主引擎"):
        """每段收摊各自隔离：一个失败的专科端点不能挡住主引擎（及其 GPU/后端）释放。"""
        try:
            executor = getattr(engine, "_inference_executor", None)
            if executor is not None:
                # cancel_futures 只丢弃排队的活：正在生成的那个线程要等它自己返回。设上限，
                # 到点就不再等（释放后端会断开远端连接，线程随之结束），收摊照常走完。
                joiner = asyncio.ensure_future(asyncio.to_thread(
                    executor.shutdown, wait=True, cancel_futures=True))
                done, _ = await asyncio.wait({joiner}, timeout=ENGINE_RELEASE_SECONDS)
                if done:
                    joiner.result()
                else:
                    _LINGERING.add(joiner)
                    joiner.add_done_callback(_LINGERING.discard)
                    print(f"[App] {label} 推理线程 {ENGINE_RELEASE_SECONDS}s 内未退出：不再等待，继续释放后端")
        except Exception as exc:
            print(f"[App] {label} 推理线程收摊失败（{type(exc).__name__}），继续释放后端")
        try:
            await blocking_call(engine.backend.shutdown)
        except Exception as exc:
            print(f"[App] {label} 后端释放失败（{type(exc).__name__}）")

    async def _cleanup_compute(resources):
        tasks = [resources.get(name) for name in (
            "maintenance_task", "base_prewarm_task", "cleanup_task", "schedule_tick_task")]
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await finish_task(task)
        async def _join_bounded(label, coro, pending):
            """有界结算：取消检查点不配合（或轮次真的卡死）时也必须继续释放引擎。

            asyncio.wait 不会代为取消被等待者——超时后由本函数显式取消它与
            它名下的 worker，再带着错误继续走完收摊，而不是永远挂在这里。
            """
            # 每段各自计时：卡住的轮次不能把定时任务归档所需的时间吃掉
            worker = asyncio.ensure_future(coro)
            done, _ = await asyncio.wait({worker}, timeout=SHUTDOWN_JOIN_SECONDS)
            if not done:
                print(f"[App] {label} 在 {SHUTDOWN_JOIN_SECONDS}s 内未结算：取消剩余，继续释放引擎")
                for item in pending:
                    item.cancel()
                worker.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await worker

        runner = resources.get("turn_runner")
        # Cooperatively settle active turns through their existing cancellation path.
        for session in list(session_manager._all_sessions.values()):
            session.cancel_requested = True
        if runner is not None and runner._bg:
            turns = list(runner._bg.values())
            await _join_bounded("活跃轮次", asyncio.gather(*turns, return_exceptions=True), turns)
        scheduled = resources.get("schedule_runner")
        if scheduled is not None:
            await _join_bounded("定时任务", scheduled.stop(), list(scheduled._tasks))
        for session in list(session_manager._all_sessions.values()):
            try:
                session.freeze()
            except Exception as exc:
                print(f"[App] 会话冻结失败（{type(exc).__name__}），继续释放引擎")
        try:
            pool = resources.get("engine_pool")
            if pool is not None:
                try:
                    extras = [(name, engine) for name, engine in list(pool._engines.items())
                              if name != "main"]
                except Exception as exc:
                    print(f"[App] 端点引擎池清点失败（{type(exc).__name__}），仍释放主引擎")
                    extras = []
                for name, extra in extras:
                    await _close_engine(extra, f"端点 {name}")
                with suppress(Exception):
                    pool._engines.clear()
            if resources.get("engine") is not None:
                await _close_engine(resources["engine"])
        finally:
            # 状态复位无论如何都要做：否则 app.state 留着已释放的引擎句柄。
            session_manager.backend = None
            session_manager.scheduler = None
            research_runtime.set_runtime(None, None)
            research_runtime.set_engine_pool(None)
            research_runtime.set_rescan_hook(None)
            research_runtime.set_skill_registry(None)
            research_runtime.set_note_store(None)
            for name in resources:
                if hasattr(app.state, name):
                    delattr(app.state, name)

    async def _assemble_compute():
        global MODEL, MODEL_REVISION, MODEL_OPTIONS, _model_source
        resources = {}
        try:
            try:
                require_remote_inference_host(config)
            except SystemExit:
                # 装配前就定格的失败（地址缺失）也要给 compute_node_state 一个终态：
                # 否则 /health 永远 probing，app 的快轮询与「启动中」标签都退不出来。
                app.state.compute_node_state = "unreachable"
                raise
            # 推理承载（B1）先于路径解析定夺：mode=remote 时本进程只当编排端，权重在设备 A 的
            # inference-server 里——这里若走全量 resolve_model_path，edge 机会照样拉满 4.9G，
            # devctl 的 --tokenizer-only 预取也拦不住（那只让分词器先到位，全量解析看的是
            # .download_complete 标记，缺了就重下）。故按 mode 选解析器，别按 profile 旗标。
            _inference_cfg = config.get("inference") or {}
            _mode = str(_inference_cfg.get("mode", "inprocess"))
            _remote = _mode == "remote"
            # 模型发现采纳（分离部署铁则，build 72 实机复盘）：remote 形态先问算力端 /health
            # 拿真身就地采纳——app bundle 没有 .model-selection、config 默认键（D27 起为本机
            # Qwen3.8-27B-4bit，条目级 drafter 随之默认开）只管 inprocess 回落，不发现就必然
            # 与算力端实跑模型（Colab Qwen INT4）错配、被身份护栏正确拒启。
            # 只在本地无显式选型（source=default）时采纳（修正评审 S6）：操作员写下的
            # .model-selection 是决定不是建议（评测臂强钉某档），显式选型走「不采纳 + 装配
            # 护栏响亮比对」。采纳落在 model_selection 模块态（adopt_model）而非仅 app 全局：
            # resolve_endpoints/路由在调用期重推主模型，只改这里的 MODEL 会让路由面继续拿
            # 未采纳缺省的能力集（修正评审 C1）。旧版服务未报身份/外来占端口 → 回落缺省，
            # 护栏 fail-closed 兜底；不可达由单个后台启动属主有界退避重试。
            if _remote and _discover_default:
                from framework.utils.model_selection import adopt_model, discover_remote_model
                require_remote_inference_host(config)   # 空 host 先拦成明确指引（uvicorn app:app 路径也覆盖）
                _rm = _inference_cfg.get("remote") or {}
                try:
                    _port = int(_rm.get("port", 14161))
                except (TypeError, ValueError):
                    app.state.compute_node_state = "unreachable"   # 端口非法同属定格失败，不得停在 probing
                    raise SystemExit(f"❌ [backend] inference.remote.port 不是数值：{_rm.get('port')!r}")
                _base = f"http://{str(_rm.get('host') or '').strip()}:{_port}"
                try:
                    _found = await blocking_call(lambda: discover_remote_model(_base))
                except Exception:
                    # 发现即对算力端 /health 的一次探问：不可达要如实反映，否则算力端
                    # 宕机期间每次装配都卡在这里、compute_node_state 永远停在 probing。
                    app.state.compute_node_state = "unreachable"
                    raise
                if _found:
                    adopt_model(_found[0], _found[1])
                    MODEL, MODEL_REVISION, _model_source = resolve_selected_model()
                    MODEL_OPTIONS = model_options(MODEL)
                    print(f"🔎 [System] 已按算力端发现采纳模型：{MODEL}"
                          f"（注册表钉扎 {str(MODEL_REVISION)[:12] if MODEL_REVISION else '无'}——"
                          f"模型状态靠发现不靠抄；首启可能现场拉取分词器 ~31MB）")
                else:
                    print(f"⚠️ [System] 算力端未报模型身份（旧版服务/外来程序占端口）：回落本地"
                          f"缺省 {MODEL}，装配期身份护栏将 fail-closed 兜底")
            elif _remote:
                print(f"📌 [System] 本地 .model-selection 显式选型 {MODEL}：跳过发现采纳"
                      f"（操作员决定优先），装配期身份护栏将与算力端实跑模型响亮比对")
            print(f"🚀 [System] {'resolving tokenizer for' if _remote else 'loading model:'} {MODEL}"
                  + ("（来自 .model-selection 启动选择）" if _model_source == "selection" else "")
                  + ("（mode=remote：只读分词器，不拉权重）" if _remote else ""))
            if _remote_transport:
                await _probe_dependencies()
            model_path = await blocking_call(lambda: (
                resolve_tokenizer_only_path(MODEL, revision=MODEL_REVISION) if _remote
                else resolve_model_path(MODEL, revision=MODEL_REVISION)))

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
                vad_model = await blocking_call(load_silero_vad)
            else:
                print("🎵 [System] 跳过 Silero VAD（modalities 不含 audio → 音频走 ASR 转写）")

            print("⚙️  [System] Initializing architecture components...")
            # 0. 长期记忆类目就绪（Stage 3）：在扫描 MCP 工具前确保 agent_memory 类目存在，
            #    好让 rag_search 在启动探测类目时就把它列为可召回类目（RAG 离线则静默跳过）。
            if (config.get("agent") or {}).get("sediment_to_rag", True):
                from framework.memory.rag_sink import ensure_memory_category
                await blocking_call(ensure_memory_category)

            # 0.0b 长期记忆笔记本（R4）：与上面同一个理由必须先于 MCPRegistry——rag_search 在
            #      __init__ 里探测类目来构建自己的 schema，agent_notes 晚了它就不认识，笔记正文
            #      再也搜不到。笔记本句柄同样先于工具扫描登记（memory_write 的探活要看它）；
            #      与 skill_invoke 不同，这里没有互为前置的环，一段装配即可。
            from framework.agents import runtime as research_runtime
            from framework.memory.notes import NoteStore
            from framework.memory.rag_sink import ensure_notes_category
            research_runtime.set_note_store(await blocking_call(NoteStore, NOTES_DIR))
            await blocking_call(ensure_notes_category)

            # 1. 插件层：扫描并注册所有 MCP 工具
            mcp_registry = await blocking_call(MCPRegistry, tools_dir=TOOLS_DIR)

            # 1b. 剧本层（R2a）：skill = 主轮次的提示词剧本，工具沿用主循环 → 审批闸门照常生效。
            #     两段装配是因为两表互为前置：剧本的 requires 探活要看工具表（故必在 MCPRegistry
            #     之后），而 skill_invoke 的探活要看剧本表（故上一步的目录扫描必然跳过它，此处补注册）。
            skill_registry = await blocking_call(
                SkillRegistry, SKILLS_DIR, tool_names=mcp_registry.tool_names(),
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
                # 多端点声明校验（接口准备③）：inference.endpoints 配置错误在装配期响亮拒启
                # （resolve_endpoints 抛 ValueError 说清哪条错），不留到专科调用时才炸
                from framework.utils.model_selection import resolve_endpoints
                _eps = resolve_endpoints()
                if len(_eps) > 1:
                    print("🔌 [System] 追加推理端点：" + ", ".join(
                        f"{e['name']}={e['repo'].split('/')[-1]}:{e['port']}" for e in _eps[1:]))
            if _backend_name == "remote":
                # 模型身份护栏（分离部署缺陷二）：把本侧选型注入装配 cfg，RemoteBackend 拿
                # inference-server /health 报的真身来比——两侧 .model-selection 各管各的，
                # 档位错开时在装配期响亮拒启，不是分词器驱动错权重静默出垃圾。键在
                # _backend_name 而非 mode：inference.backend=remote（mode 仍 inprocess）
                # 同样装 RemoteBackend，按 mode 注入会让那条路的护栏静默失效（首评 S1）。
                _inference_cfg = {**_inference_cfg,
                                  "expected_model": {"repo": MODEL, "revision": MODEL_REVISION}}
            engine = await AgentEngine.create(
                model_path, kv_config=_inference_cfg,
                # 注册表条目整条透传（R0）：方言名与全部模型级开关（suppress_ghost_thinking 等）
                # 由方言自行解读，引擎不解读——换模型家族不动 app.py 与 engine 的签名。
                model_options=MODEL_OPTIONS,
                env=(config.get("agent") or {}).get("environment") or {},
                backend=_backend_name,
                max_concurrency=int((config.get("agent") or {}).get("max_inference_concurrency", 1)),
            )
            resources["engine"] = engine
            model, processor = engine.model, engine.processor
            orchestrator = Orchestrator(engine, mcp_registry, config.get("agent") or {})

            # 2.1 联网调查沙箱运行时：把引擎与主事件循环登记给 web_research（受限沙箱工具经
            #     run_coroutine_threadsafe 投回本循环复用主模型生成，见 framework/agents）。
            #     任务路由执行面（混合架构第一波）：端点引擎池——无状态单发生成经
            #     core/model_router 按实测画像选端点、由池懒装配专科引擎（仅 remote 模式
            #     真的会建；inprocess 恒只有 main）。主循环轮次不经此层（铁律 6 方向）。
            from framework.core.engine_pool import EnginePool
            engine_pool = EnginePool(engine)
            resources["engine_pool"] = engine_pool
            #     客户端反查通道（get_location 等）：工具线程经此 loop 往会话事件流发请求事件

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
            # 剧本保存/删除端点（POST/DELETE /api/v1/skills）经此即时重建剧本表（照 app.state.feed 形状）


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
            # 任务级 worker 数与生成并发同源（B1 单旋钮）：remote 后端才放开多会话并行准入，
            # 进程内后端共享单例不可并发 → 恒 1（与引擎的钳制口径一致）。
            _n_task_workers = (
                max(1, int((config.get("agent") or {}).get("max_inference_concurrency", 1)))
                if _backend_name == "remote" else 1
            )
            scheduler = TaskScheduler(
                policy=ClassPolicy(
                    aging_seconds=float(sched_cfg.get("aging_seconds", 300)),
                    preempt_background=bool(sched_cfg.get("preempt_background", True)),
                ),
                on_suspend_request=_flag_session_suspend,
                on_cancel_request=_flag_session_cancel,
                on_queue_positions=_publish_queue_positions,
                max_workers=_n_task_workers,
            )

            # 3.2 轮次执行层：TurnRunner —— 轮次生命周期的唯一属主（受理/执行/终态分流/落盘/
            #     账本生命周期事件）。routes 的对话端点全部是它的薄适配器（新旧契约共用一套闭环）。
            async def _summarize_hook(session, sid):
                if app.state.compute_ready:
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

            resources.update(
                model=model, processor=processor, vad_model=vad_model,
                orchestrator=orchestrator, mcp_registry=mcp_registry,
                scheduler=scheduler, turn_runner=turn_runner,
                file_handler=file_handler, ingestion=ingestion,
                rebuild_skills=_rebuild_skills, rescan_tools=_rescan_tools)
            if _remote_transport:
                await _probe_dependencies()  # Auth/identity may have changed during tokenizer/tool IO.
            return resources
        except BaseException as exc:
            try:
                await finish_task(asyncio.create_task(_cleanup_compute(resources)))
            except Exception as cleanup_exc:
                # 原始装配错误才是诊断：收摊噪声只记日志，绝不盖掉它（否则重试日志
                # 永远在讲收摊的那个异常，没人知道装配为什么失败）。
                print(f"[App] 装配失败后的收摊出错（{type(cleanup_exc).__name__}）；"
                      f"保留原始装配错误")
            # finish_task 刻意吞掉取消、坚持把收摊 join 完：落在这一下的 stop() 取消已被消费。
            # 这里把它还原成 CancelledError（先于下面的 SystemExit 转换），否则装配错误被 _run
            # 当普通失败接住进入重试，3.13 不会重投取消，重试循环永不结束、stop() 永远等。
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            if isinstance(exc, SystemExit):
                raise RuntimeError(str(exc)) from exc  # Bad compute config cannot exit the data service.
            raise

    def _publish_compute(resources):
        engine, orchestrator = resources["engine"], resources["orchestrator"]
        scheduler = resources["scheduler"]
        session_manager.backend = engine.backend
        session_manager.scheduler = scheduler
        research_runtime.set_runtime(engine, asyncio.get_running_loop())
        research_runtime.set_engine_pool(resources["engine_pool"])
        research_runtime.set_rescan_hook(resources["rescan_tools"])
        from framework.core import client_query
        client_query.set_loop(asyncio.get_running_loop())
        resources["maintenance_task"] = asyncio.create_task(
            _idle_maintenance_loop(session_manager, scheduler, orchestrator,
                                   can_execute=lambda: app.state.compute_ready))
        async def _startup_prewarm():
            try:
                await orchestrator.prewarm_base_cache()
            except Exception as exc:
                print(f"[App] 基座前缀预填失败（冷路兜底）: {type(exc).__name__}")
        resources["base_prewarm_task"] = asyncio.create_task(_startup_prewarm())
        cleanup_cfg = session_cleanup_policy(config.get("session_cleanup") or {})
        app.state.session_cleanup_policy = cleanup_cfg
        if cleanup_cfg["enabled"]:
            resources["cleanup_task"] = asyncio.create_task(_session_cleanup_loop(
                session_manager, cleanup_cfg, orchestrator.sediment_to_rag))
        schedule_runner = ScheduleRunner(
            schedule_store, session_manager, orchestrator, scheduler, schedules_cfg,
            can_execute=lambda: app.state.compute_ready)
        resources["schedule_runner"] = schedule_runner
        resources["schedule_tick_task"] = asyncio.create_task(schedule_runner.tick_loop())
        for name, value in resources.items():
            setattr(app.state, name, value)

    lifecycle = ComputeLifecycle(
        app, _assemble_compute, _publish_compute, _cleanup_compute,
        _probe_dependencies if _remote_transport else None)
    app.state.compute_lifecycle = lifecycle
    try:
        if _remote_transport:
            lifecycle.start()
        else:
            # In-process model loading retains its original startup/readiness contract.
            await lifecycle._install()
        if AUTH_DEVICES:
            print(f"🔐 [System] API 鉴权已启用（设备级票据 ×{len(AUTH_DEVICES)}"
                  + ("；共享密钥同时有效" if API_KEY else "") + "）")
        elif API_KEY:
            print("🔐 [System] API 鉴权已启用（Bearer 共享密钥）")
        else:
            print("⚠️ [System] API 鉴权未启用：未设置 AGENT_API_KEY / server.api_key。")
        print(f"✅ [System] 本地数据服务已就绪：{AGENT_HOST}:{AGENT_PORT}")
        yield
    finally:
        # data_ready 先落（收摊期间 /health 立刻说实话，不等引擎释放完）
        app.state.data_ready = False
        try:
            await lifecycle.stop()
        finally:
            await push_dispatcher.stop()
            for session in list(session_manager._all_sessions.values()):
                session.journal.close()
            set_store(None)

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
# status 保留进程探活语义；data_ready/compute_ready 明确区分本地读取与执行能力。
#
# 实例指纹（#85 完整版 1，2026-08-10 401 事故根治）：探针「14159 上有 200」不足以认领
# 一个后端——端口可能被别的程序占着，也可能是钥匙已漂移的旧后端（app 恒发钥匙串值 →
# 构造性 401）。故 /health 除 status 外再报三件可核对的身份信息：
#   service = 我是谁（非本值 = 端口被外来程序占用）
#   layout  = 我按哪套布局起的（repo=devctl/dev 桥接 · bundle=app 全栈包 · unknown=直启）
#   key_fp  = 我认哪把钥匙的 **sha256 前 8 位**（空 = 未启用鉴权的 dev 对端）
# 红线：明文密钥绝不出现在响应/日志里，只有指纹；指纹在进程启动时算一次（下面的常量），
# /health 保持零 IO、零哈希，仍是最便宜的一个 GET（devctl/反代/lib_edge 都在轮询它）。
SERVICE_NAME = "macagent-backend"


def key_fingerprint(key: str) -> str:
    """密钥指纹：sha256 十六进制前 8 位。空钥 = 空串（表示"未启用鉴权"，不是某把钥匙）。

    8 位 = 32 bit：够两端核对"是不是同一把"，不够反推原值（且原值是 32 字节随机 token）。
    """
    if not key:
        return ""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


_HEALTH_KEY_FP = key_fingerprint(API_KEY)
_HEALTH_LAYOUT = os.environ.get("AGENT_LAYOUT") or "unknown"


def _health_node_default() -> str:
    """lifespan 尚未落值时的缺省：与 lifespan 的 _remote_transport 同一判据（零 IO）。"""
    inf = config.get("inference") or {}
    remote = str(inf.get("mode", "inprocess")) == "remote" or inf.get("backend") == "remote"
    return "probing" if remote else "local"


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "layout": _HEALTH_LAYOUT,
        "key_fp": _HEALTH_KEY_FP,
        "data_ready": getattr(app.state, "data_ready", False),
        "compute_ready": getattr(app.state, "compute_ready", False),
        "compute_state": getattr(app.state, "compute_state", "starting"),
        # Contract H：engine_ready=引擎已装上（算力后来掉线看 compute_state）；
        # compute_node_state=远端算力端探活（装配期取最近一次，就绪后按监视器连续失败门槛）；
        # rag_state=RAG 数据面最近一次探活（非 remote 形态不探 = skipped）。
        "engine_ready": getattr(app.state, "engine_ready", False),
        "compute_node_state": getattr(app.state, "compute_node_state", _health_node_default()),
        "rag_state": getattr(app.state, "rag_state",
                             "probing" if _health_node_default() == "probing" else "skipped"),
    }

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
app.include_router(router, dependencies=[Depends(verify_api_key), Depends(require_compute_ready)])
app.include_router(offline_files_router,
                   dependencies=[Depends(verify_api_key), Depends(require_compute_ready)])
# iPhone 扫码配对：不挂计算端就绪闸（配对与算力无关）；start 自带 Bearer，claim 凭一次性码
app.include_router(pairing_router)

# ==========================================
# 4. 启动器
# ==========================================
if __name__ == "__main__":
    import uvicorn
    # 算力配置验证归计算装配；缺算力地址不阻止本地历史数据服务启动。
    # 绑定护栏先于加载模型：非回环 + 空密钥立即拒启，不浪费几十秒权重加载才发现
    enforce_bind_guardrail(AGENT_HOST, API_KEY, service="backend", key_env="AGENT_API_KEY")
    # 密钥护栏之后再验地址（先密钥后地址，护栏输出顺序稳定）：绑定地址不属本机立即拒启
    preflight_bind_addr(AGENT_HOST, service="backend", cfg_key="config 的 inference.host")
    # 建议不要在此处使用 reload=True，MLX 模型反复重载会导致严重内存泄漏
    # timeout_graceful_shutdown：默认 None = 无限等连接排空——挂着 SSE 长连时
    # SIGTERM 形同虚设（实机 2026-07-17：孤儿 backend 无视 TERM 20s 被 -9），
    # 收摊方一旦在升级 -9 前被打断（quit 看门狗）就留孤儿。5s 足够冻结落盘。
    # loop="asyncio"：uvloop 在 libuv 内解析域名，会绕过 tailnet_dns 的回退（算力探活走 httpx 异步）
    uvicorn.run(app, host=AGENT_HOST, port=AGENT_PORT, timeout_graceful_shutdown=5, loop="asyncio")
