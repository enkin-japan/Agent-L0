from pathlib import Path
import asyncio
import os
import time
import yaml
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
import mlx.core as mx

# 导入底层模型库
from silero_vad import load_silero_vad

# 导入解耦后的架构组件
from framework.mcp.registry import MCPRegistry
from framework.core.engine import AgentEngine
from framework.core.orchestrator import Orchestrator
from framework.core.scheduler import TaskScheduler, ClassPolicy
from framework.core.turn_runner import TurnRunner
from framework.memory.session import SessionManager
from framework.ingestion import Ingestion
from framework.utils.file_handler import FileHandler
from framework.utils.path_resolver import resolve_model_path
from framework.memory.compactor import maybe_summarize_context
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
SESSIONS_DIR = BASE_DIR / "sessions"
SCHEDULES_DIR = BASE_DIR / "schedules"   # 定时任务实体（与 sessions 平级，防会话扫描误认）
TOOLS_DIR = BASE_DIR / "framework" / "mcp" / "tools"

# 配置在仓库根（backend 与 rag 共用一份），寻址统一走 config_loader
from framework.utils.config_loader import load_config
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

# ==========================================
# 2. 生命周期与依赖装配 (IoC Container)
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"🚀 [System] loading model: {MODEL}"
          + ("（来自 .model-selection 启动选择）" if _model_source == "selection" else ""))
    model_path = resolve_model_path(MODEL, revision=MODEL_REVISION)

    print("🎵 [System] loading model: Silero VAD")
    vad_model = load_silero_vad()

    print("⚙️  [System] Initializing architecture components...")
    # 0. 长期记忆类目就绪（Stage 3）：在扫描 MCP 工具前确保 agent_memory 类目存在，
    #    好让 rag_search 在启动探测类目时就把它列为可召回类目（RAG 离线则静默跳过）。
    if (config.get("agent") or {}).get("sediment_to_rag", True):
        from framework.memory.rag_sink import ensure_memory_category
        await asyncio.to_thread(ensure_memory_category)

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
    
    # 2. 领域层：推理引擎（单次生成）+ 编排循环（计划推进/失败路由/终止保护）。
    #    经 AgentEngine.create 在专用推理线程内加载模型并装配——推理宇宙(权重/惰性
    #    常量/KV/生成)同属一个线程,事件循环全程自由(prefill 不再饿死 API)。
    engine = await AgentEngine.create(
        model_path, kv_config=config.get("inference") or {},
        suppress_ghost_thinking=bool(MODEL_OPTIONS.get("suppress_ghost_thinking", False)),
        env=(config.get("agent") or {}).get("environment") or {},
    )
    model, processor = engine.model, engine.processor
    orchestrator = Orchestrator(engine, mcp_registry, config.get("agent") or {})

    # 2.1 联网调查沙箱运行时：把引擎与主事件循环登记给 web_research（受限沙箱工具经
    #     run_coroutine_threadsafe 投回本循环复用主模型生成，见 framework/agents）。
    from framework.agents import runtime as research_runtime
    research_runtime.set_runtime(engine, asyncio.get_running_loop())


    # 3. 记忆层：初始化弹性冷热会话管理 (默认闲置 30 分钟冻结)。
    #    全局任务 feed(Phase 3):各会话账本的生命周期事件镜像于此,
    #    GET /api/v1/events 推送给任务中心/通知(替代 /tasks 差分轮询)。
    from framework.core.feed import GlobalFeed
    feed = GlobalFeed()
    session_manager = SessionManager(sessions_dir=SESSIONS_DIR, idle_timeout=1800)
    session_manager.feed = feed
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
    push_registry = PushRegistry(BASE_DIR / "push")
    apns_client = None
    if push_cfg.get("enabled", True):
        # 密钥相对路径 = 相对仓库根（config.yaml 所在处）：`.keys/AuthKey_X.p8` 在
        # 开发机/远程机都指向各自仓库副本里的同一位置，不受进程启动目录影响。
        key_path = str(push_cfg.get("apns_key_path") or "")
        if key_path and not Path(key_path).expanduser().is_absolute():
            key_path = str(BASE_DIR.parent / key_path)
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

    if API_KEY:
        print("🔐 [System] API 鉴权已启用（Bearer 共享密钥）")
    else:
        print("⚠️  [System] API 鉴权未启用：未设置 AGENT_API_KEY / server.api_key，接口对可达网络裸奔。"
              "\n    生产务必设置 AGENT_API_KEY 环境变量。")

    print(f"✅ [System] AI Agent has started and is listening on {AGENT_HOST}:{AGENT_PORT}")
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
        
        # 强制释放苹果芯片的显存/内存
        mx.clear_cache()
        print("✅ [System] Resources have been safely released. Goodbye!")
    except Exception as e:
        print(f"❌ [System] Resource release error, force termination: {e}")

# ==========================================
# 3. FastAPI 表现层服务配置
# ==========================================
app = FastAPI(
    title="SplitStack Agent API",
    description="Based on MLX Framework and Domain Driven Design",
    version="2.0.0",
    lifespan=lifespan
)

# 鉴权密钥挂到 app.state，供 verify_api_key 依赖按请求读取（避免每次触碰环境变量）
app.state.api_key = API_KEY

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
    # 建议不要在此处使用 reload=True，MLX 模型反复重载会导致严重内存泄漏
    uvicorn.run(app, host=AGENT_HOST, port=AGENT_PORT)