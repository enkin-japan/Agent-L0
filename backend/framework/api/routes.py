import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .schemas import (
    ChatResponse,
    ApprovalRequest,
    CancelResponse,
    DeleteSessionResponse,
    DeleteTurnResponse,
    HistoryResponse,
    BranchRequest,
    BranchResponse,
    LiveActivityTokenRequest,
    MemoryStatusResponse,
    PinRequest,
    PinResponse,
    PushPrefsRequest,
    PushRegisterRequest,
    SessionListResponse,
    ScheduleActionResponse,
    ScheduleListResponse,
    ScheduleRunsResponse,
    ScheduleToggleRequest,
    SystemPromptResponse,
    TaskListResponse,
    TaskStatusResponse,
    ToolsResponse,
)
from ..core.journal import TurnJournal
from ..core.turn_runner import TurnConflict, public_turn
from ..memory.compactor import maybe_summarize_context

# 路径 ID 白名单（会话/定时任务/资产文件名共用）：首字符字母数字，其余限 [A-Za-z0-9._-]。
# 若不校验，`DELETE /api/v1/chat/..` 这类构造请求会把 sessions/.. 当作会话目录整棵 rmtree。
_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


async def _validate_path_ids(request: Request) -> None:
    """路由级依赖：所有含 session_id / schedule_id 路径参数的端点统一做字符白名单校验，
    杜绝 `..`、分隔符等路径穿越载荷进入文件系统操作（rmtree/open/mkdir）。"""
    for key in ("session_id", "schedule_id"):
        val = request.path_params.get(key)
        if val is not None and (not _ASSET_NAME_RE.match(val) or ".." in val):
            raise HTTPException(status_code=400, detail=f"非法的 {key}：仅允许字母/数字开头，且只含字母/数字/._-")


# 构建模块化路由（统一挂路径 ID 校验依赖）
router = APIRouter(dependencies=[Depends(_validate_path_ids)])


async def _maybe_summarize_active(session, orchestrator, scheduler, session_id):
    """
    轮次落盘后的活跃期摘要压缩（方案A 高阈值路径）：上下文越过高阈值则折叠旧轮入滚动摘要。
    摘要是一次生成，走调度器低优先级（priority=-1），给活跃任务让路。须在会话锁内、轮次边界调用。
    """
    if session.last_prompt_tokens < orchestrator.summary_high_threshold:
        return
    await scheduler.acquire(session_id, kind="summarize", priority=-1)
    try:
        n = await maybe_summarize_context(
            session, orchestrator.engine,
            threshold_tokens=orchestrator.summary_high_threshold,
            keep_tail_turns=orchestrator.summary_keep_tail_turns,
            last_prompt_tokens=session.last_prompt_tokens,
            sediment=orchestrator.sediment_to_rag,
        )
        if n:
            print(f"[Routes] 摘要压缩：折叠 {n} 轮入滚动摘要（活跃高阈值）")
    finally:
        # 复位抢占取消标志（ClassPolicy 抢占后台摘要时置位），防止污染该会话下一轮
        session.cancel_requested = False
        await scheduler.release(session_id)


@router.get("/api/v1/tools", response_model=ToolsResponse)
async def list_tools(request: Request):
    """
    已注册工具清单接口：供前端上下文面板展示模型当前可用的工具及其审批要求。
    """
    registry = request.app.state.mcp_registry
    tools = registry.list_tools()
    return {"status": True, "count": len(tools), "tools": tools}


@router.post("/api/v1/tools/rescan")
async def rescan_tools(request: Request):
    """
    工具注册表热重扫（D 阶段工程票）：重探启动时被舍弃的工具（依赖后装场景），
    新通过者补注册并重建剧本表/工具声明。只增不删；探活有真 IO，走线程。
    """
    from framework.agents import runtime as _runtime
    result = await asyncio.to_thread(_runtime.rescan_tools)
    return {"status": True, **result}


@router.get("/api/v1/tasks", response_model=TaskListResponse)
async def list_tasks(request: Request):
    """
    全局任务视图（跨会话）：单 worker 调度器下谁在跑、谁排队、谁挂起。
    供前端「任务切换器」及后续自动调度观测使用（只读，不触发任何调度动作）。
    """
    scheduler = request.app.state.scheduler
    return {"status": True, **scheduler.snapshot()}


# ---------- 定时任务（schedule）直接操作端点 ----------
# 创建/编辑不设直接端点——走对话（模型是任务的协商界面，schedule_* 工具经审批卡片确认）。
# 这里只提供不经过模型的用户控制权：启停、立即运行、中止本次运行、删除、历史查询——
# 模型忙或犯傻时，用户对任务的控制不被模型劫持。


def _schedule_or_404(store, schedule_id: str):
    task = store.get(schedule_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"定时任务 {schedule_id} 不存在。")
    return task


@router.get("/api/v1/schedules", response_model=ScheduleListResponse)
async def list_schedules(request: Request):
    """定时任务清单：定义 + 是否正在执行 + 最近一次运行摘要。前端侧栏低频轮询。"""
    store = request.app.state.schedule_store
    runner = request.app.state.schedule_runner
    schedules = []
    for task in await asyncio.to_thread(store.list_tasks):
        runs = await asyncio.to_thread(store.read_runs, task["id"], 1)
        schedules.append({
            **task,
            "running": task["id"] in runner.running,
            "last_run": runs[0] if runs else None,
        })
    return {"status": True, "count": len(schedules), "schedules": schedules}


@router.post("/api/v1/schedules/{schedule_id}/toggle", response_model=ScheduleActionResponse)
async def toggle_schedule(schedule_id: str, request: Request, body: ScheduleToggleRequest):
    """启停定时任务。重新启用时 next_run 从当前时刻重算（不追补停用期间的触发）。"""
    store = request.app.state.schedule_store
    _schedule_or_404(store, schedule_id)
    task = await asyncio.to_thread(lambda: store.update(schedule_id, enabled=body.enabled))
    return {"status": True, "schedule_id": schedule_id, "schedule": task,
            "response": "已启用" if body.enabled else "已停用（进行中的运行不受影响）"}


@router.post("/api/v1/schedules/{schedule_id}/run", response_model=ScheduleActionResponse)
async def run_schedule_now(schedule_id: str, request: Request):
    """立即运行一次（测试/手动触发）。不影响 next_run 的既定节奏。"""
    store = request.app.state.schedule_store
    runner = request.app.state.schedule_runner
    task = _schedule_or_404(store, schedule_id)
    if not runner.launch(task, scheduled_for=time.time()):
        return {"status": False, "schedule_id": schedule_id, "schedule": task,
                "response": "该任务正在运行中，不能重复启动。"}
    return {"status": True, "schedule_id": schedule_id, "schedule": task,
            "response": "已开始运行，结果将出现在该任务的专属会话中。"}


@router.post("/api/v1/schedules/{schedule_id}/cancel", response_model=ScheduleActionResponse)
async def cancel_schedule_run(schedule_id: str, request: Request):
    """中止本次运行（区别于「停用任务」）：复用会话取消原语，最近检查点停止并整轮回滚。"""
    from ..schedule.runner import session_id_for
    store = request.app.state.schedule_store
    runner = request.app.state.schedule_runner
    task = _schedule_or_404(store, schedule_id)
    if schedule_id not in runner.running:
        return {"status": False, "schedule_id": schedule_id, "schedule": task,
                "response": "该任务当前没有进行中的运行。"}
    session = request.app.state.session_manager._all_sessions.get(session_id_for(schedule_id))
    if session is None or session.is_cold:
        return {"status": False, "schedule_id": schedule_id, "schedule": task,
                "response": "运行会话尚未就绪，稍后重试。"}
    session.cancel_requested = True
    return {"status": True, "schedule_id": schedule_id, "schedule": task,
            "response": "中止信号已发出，将在最近的检查点停止（正在执行的工具需等其返回）。"}


@router.delete("/api/v1/schedules/{schedule_id}", response_model=ScheduleActionResponse)
async def delete_schedule(schedule_id: str, request: Request):
    """删除定时任务（定义 + 运行历史；专属会话保留，属聊天记录）。运行中则先发中止信号。"""
    from ..schedule.runner import session_id_for
    store = request.app.state.schedule_store
    runner = request.app.state.schedule_runner
    _schedule_or_404(store, schedule_id)
    if schedule_id in runner.running:
        session = request.app.state.session_manager._all_sessions.get(session_id_for(schedule_id))
        if session is not None and not session.is_cold:
            session.cancel_requested = True
    await asyncio.to_thread(store.delete, schedule_id)
    return {"status": True, "schedule_id": schedule_id, "schedule": None, "response": "已删除。"}


@router.get("/api/v1/schedules/{schedule_id}/runs", response_model=ScheduleRunsResponse)
async def get_schedule_runs(schedule_id: str, request: Request, limit: int = 20):
    """运行历史（新在前）。每条含指向专属会话的 session_id，前端点击可跳转查看全文。"""
    store = request.app.state.schedule_store
    _schedule_or_404(store, schedule_id)
    runs = await asyncio.to_thread(store.read_runs, schedule_id, limit)
    return {"status": True, "schedule_id": schedule_id, "count": len(runs), "runs": runs}


# ---------- 会话资产出货（render_chart / deck 等工具产物 → 前端展示） ----------
# 安全边界：只从 sessions/{id}/assets/ 出货；session_id 与文件名双重白名单正则 +
# resolve 包含校验，双保险防路径穿越（_ASSET_NAME_RE 定义在文件顶部，与路由级
# 路径 ID 校验共用）。经既有 Bearer 鉴权（前端 <img> 走同源 /api 相对路径，
# Authorization 由 Vite/Nginx 代理注入）。文件名含毫秒时间戳恒不复用，可安全长缓存。


# ---------- 推送设备注册面（iOS APNs；macOS 本地通知不经此）----------


@router.post("/api/v1/push/register")
async def push_register(request: Request, body: PushRegisterRequest):
    """设备注册/更新（upsert，幂等）：device token / push-to-start token / 偏好。
    token 轮换直接重打本端点；apns 未配置时注册照收（配置好即生效）。"""
    registry = request.app.state.push_registry
    d = registry.upsert(
        body.device_id, platform=body.platform, token=body.token,
        push_to_start_token=body.push_to_start_token,
        prefs=body.prefs, app_build=body.app_build)
    return {"status": True, "device_id": d["device_id"],
            "push_enabled": request.app.state.push_dispatcher.enabled,
            "prefs": d.get("prefs")}


@router.delete("/api/v1/push/devices/{device_id}")
async def push_unregister(device_id: str, request: Request):
    """设备注销（用户关掉总开关/卸载前清理）。不存在也返回成功（幂等）。"""
    request.app.state.push_registry.remove(device_id)
    return {"status": True}


@router.patch("/api/v1/push/devices/{device_id}/prefs")
async def push_update_prefs(device_id: str, request: Request, body: PushPrefsRequest):
    """通知偏好部分更新（设置页开关的同步端点）。设备未注册时 404。"""
    prefs = {k: v for k, v in body.model_dump().items() if v is not None}
    d = request.app.state.push_registry.update_prefs(device_id, prefs)
    if d is None:
        raise HTTPException(status_code=404, detail="设备未注册，请先调 /push/register。")
    return {"status": True, "prefs": d.get("prefs")}


@router.post("/api/v1/push/live-activity")
async def push_live_activity_token(request: Request, body: LiveActivityTokenRequest):
    """Live Activity 活动 token 上报：后端据此附着该会话账本做阶段推送。
    轮次已结束/会话不热时 active=False——app 据此本地收尾孤儿活动。"""
    dispatcher = request.app.state.push_dispatcher
    res = dispatcher.register_activity(body.session_id, body.turn_id,
                                       body.device_id, body.token)
    return {"status": True, **res}


@router.get("/api/v1/files/{session_id}/assets/{filename}")
async def get_session_asset(session_id: str, filename: str, request: Request):
    """出货单个会话资产文件（图表 PNG、导出 PDF 等）。"""
    if not _ASSET_NAME_RE.match(session_id) or not _ASSET_NAME_RE.match(filename) \
            or ".." in session_id or ".." in filename:
        raise HTTPException(status_code=400, detail="非法的会话 ID 或文件名。")
    sessions_dir = request.app.state.session_manager.sessions_dir.resolve()
    path = (sessions_dir / session_id / "assets" / filename).resolve()
    if not path.is_relative_to(sessions_dir) or not path.is_file():
        raise HTTPException(status_code=404, detail="资产不存在。")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@router.get("/api/v1/files/{session_id}/deck")
async def get_session_deck(session_id: str, request: Request):
    """出货会话 Deck Spec（deck.json 原文）：前端 SlideRenderer 的数据源（单一真相的投影）。"""
    if not _ASSET_NAME_RE.match(session_id) or ".." in session_id:
        raise HTTPException(status_code=400, detail="非法的会话 ID。")
    sessions_dir = request.app.state.session_manager.sessions_dir.resolve()
    path = (sessions_dir / session_id / "deck" / "deck.json").resolve()
    if not path.is_relative_to(sessions_dir) or not path.is_file():
        raise HTTPException(status_code=404, detail="该会话没有 deck。")
    try:
        deck = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(status_code=500, detail="deck.json 损坏。")
    return {"status": True, "deck": deck}


@router.get("/api/v1/chat/{session_id}/system", response_model=SystemPromptResponse)
async def get_system_prompt(session_id: str, request: Request):
    """
    会话系统提示词查询接口（只读，不触发会话创建）。
    """
    session_manager = request.app.state.session_manager
    return {
        "status": True,
        "session_id": session_id,
        **session_manager.get_system_prompt(session_id),
    }


@router.get("/api/v1/chat/{session_id}/task", response_model=TaskStatusResponse)
async def get_task_status(session_id: str, request: Request):
    """
    多步任务进度快照（只读，不触发会话创建/恢复）。
    前端在等待对话响应期间轮询本接口，把「黑盒等待」变成可见的计划推进。
    """
    session_manager = request.app.state.session_manager
    session = session_manager._all_sessions.get(session_id)
    if session is not None and not session.is_cold:
        task = session.task_state
        return {
            "status": True,
            "session_id": session_id,
            "active": task is not None and bool(task.plan),
            "generating": session.lock.locked(),
            "task": task.snapshot() if task is not None else None,
            "activity": session.activity_snapshot(),  # 实时步骤流（当前动作 + 已完成步骤）
        }

    # 冷会话/未加载：直接读磁盘快照（不拉起会话）
    task_file = session_manager.sessions_dir / session_id / "task.json"
    if task_file.exists():
        try:
            import json
            with open(task_file, "r", encoding="utf-8") as f:
                snap = json.load(f)
            return {"status": True, "session_id": session_id,
                    "active": bool(snap.get("plan")), "generating": False, "task": snap}
        except Exception:
            pass
    return {"status": True, "session_id": session_id,
            "active": False, "generating": False, "task": None}


@router.get("/api/v1/chat/{session_id}/memory", response_model=MemoryStatusResponse)
async def get_memory_status(session_id: str, request: Request):
    """
    会话记忆/上下文压缩状态快照（只读，不触发会话创建/恢复）。
    前端「记忆」面板据此可视化有界记忆：当前上下文规模 vs 折叠阈值、已折叠进
    滚动摘要的水位线与摘要体量、以及被折叠原文是否沉淀进 RAG（可召回）。
    """
    session_manager = request.app.state.session_manager
    orchestrator = request.app.state.orchestrator
    base = {
        "status": True,
        "session_id": session_id,
        "high_threshold": orchestrator.summary_high_threshold,
        "low_threshold": orchestrator.summary_low_threshold,
        "keep_tail_turns": orchestrator.summary_keep_tail_turns,
        "sediment_to_rag": orchestrator.sediment_to_rag,
    }

    session = session_manager._all_sessions.get(session_id)
    if session is not None and not session.is_cold:
        summary = session.rolling_summary or ""
        return {
            **base,
            "loaded": True,
            "last_prompt_tokens": session.last_prompt_tokens,
            "total_turns": session.disk_turn_count(),  # 统一口径:落盘轮数(原为消息条数,冷热不一致)
            "summarized_turns": session.summarized_turns,
            "has_summary": bool(summary.strip()),
            "summary_chars": len(summary),
        }

    # 冷会话/未加载：读盘快照，不拉起会话（memory.json + 历史行数）
    summary = ""
    summarized_turns = 0
    mem_file = session_manager.sessions_dir / session_id / "memory.json"
    if mem_file.exists():
        try:
            with open(mem_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            summary = str(data.get("summary", ""))
            summarized_turns = int(data.get("summarized_turns", 0))
        except Exception:
            pass
    try:
        total_turns = len(session_manager.get_history(session_id))
    except Exception:
        total_turns = 0
    return {
        **base,
        "loaded": False,
        "last_prompt_tokens": 0,   # 冷会话无 model-facing 上下文（KV 已释放）
        "total_turns": total_turns,
        "summarized_turns": summarized_turns,
        "has_summary": bool(summary.strip()),
        "summary_chars": len(summary),
    }


@router.post("/api/v1/chat/{session_id}/task/cancel", response_model=CancelResponse)
async def cancel_task(session_id: str, request: Request):
    """
    取消当前轮生成/任务：置位取消标志，引擎在 token 段粒度、编排循环在迭代边界响应。
    取消的轮次整体回滚出内存记忆（与引擎故障同语义），以 cancelled 状态落盘存档。
    """
    session_manager = request.app.state.session_manager
    session = session_manager._all_sessions.get(session_id)
    if session is None or session.is_cold or not session.lock.locked():
        return {"status": False, "session_id": session_id,
                "response": "当前没有进行中的生成/任务，无需取消。"}
    session.cancel_requested = True
    return {"status": True, "session_id": session_id,
            "response": "取消信号已发出，将在最近的检查点停止。"}


@router.post("/api/v1/chat/{session_id}/task/suspend", response_model=CancelResponse)
async def suspend_task(session_id: str, request: Request):
    """
    挂起当前运行中的任务（区别于取消）：置位挂起标志，编排循环在当前步骤完成后的
    安全点让出 worker，KV 与 task_state 依设计常驻内存，供 /task/resume 恢复续跑。
    仅对「正在占用 worker」的任务有效（排队中/已挂起/无任务均返回 False）。
    """
    scheduler = request.app.state.scheduler
    if scheduler.request_suspend(session_id):
        return {"status": True, "session_id": session_id,
                "response": "挂起信号已发出，将在当前步骤完成后的安全点让出。"}
    return {"status": False, "session_id": session_id,
            "response": "该任务当前不在运行中，无法挂起（可能已在排队/挂起，或无进行中的任务）。"}


@router.get("/api/v1/sessions", response_model=SessionListResponse)
async def list_sessions(request: Request):
    """
    会话索引：扫描 sessions 目录返回后端真相元数据（session_id/标题/轮数/时间/pin/活跃/血缘）。
    供前端反向映射与对账——据此展示可筛选删除的会话，并剔除 localStorage 里的幽灵条目。
    只读、不拉起会话；磁盘扫描走线程池避免阻塞事件循环。
    """
    session_manager = request.app.state.session_manager
    items = await asyncio.to_thread(session_manager.list_sessions_meta)
    return {"status": True, "count": len(items), "sessions": items}


@router.patch("/api/v1/chat/{session_id}/pin", response_model=PinResponse)
async def set_session_pin(session_id: str, request: Request, body: PinRequest):
    """
    置/撤会话 pin（落盘 meta.json）。pin 的会话不被后台自动清扫回收——手动"保留"语义。
    """
    session_manager = request.app.state.session_manager
    ok = await asyncio.to_thread(session_manager.set_pinned, session_id, body.pinned)
    if not ok:
        return {"status": False, "session_id": session_id, "pinned": body.pinned,
                "response": f"❌ 未发现会话 {session_id} 或写入失败"}
    verb = "已置顶保留" if body.pinned else "已取消保留"
    return {"status": True, "session_id": session_id, "pinned": body.pinned,
            "response": f"✅ 会话 {session_id} {verb}"}


@router.patch("/api/v1/chat/{session_id}/meta")
async def set_session_meta(session_id: str, request: Request, body: dict):
    """会话元数据(服务端一等,Phase 4b):title 改名(空串=清除回退派生)/pinned。
    双端同源——web 与原生 app 的改名从此一致可见。"""
    session_manager = request.app.state.session_manager
    title = body.get("title")
    pinned = body.get("pinned")
    if title is None and pinned is None:
        return JSONResponse(status_code=400, content={"status": False, "response": "空请求:至少给 title 或 pinned。"})
    ok = await asyncio.to_thread(
        session_manager.set_meta, session_id,
        title=title if title is None else str(title),
        pinned=None if pinned is None else bool(pinned))
    return {"status": ok, "session_id": session_id,
            "response": "已保存" if ok else f"未发现会话 {session_id} 或写入失败"}


@router.delete("/api/v1/chat/{session_id}", response_model=DeleteSessionResponse)
async def delete_session(session_id: str, request: Request):
    """
    销毁会话接口
    """
    session_manager = request.app.state.session_manager
    return await session_manager.delete_session(session_id)


@router.delete("/api/v1/chat/{session_id}/turns/last", response_model=DeleteTurnResponse)
async def delete_last_turn(session_id: str, request: Request):
    """
    删除会话最后一轮问答：截断落盘日志、重建内存记忆（KV Cache 经前缀匹配自动对齐）、
    清理该轮媒体文件。供前端「更新/删除最后一次发言」使用（更新 = 删除后重发新内容）。
    """
    session_manager = request.app.state.session_manager
    return await session_manager.delete_last_turn(session_id)


@router.get("/api/v1/chat/{session_id}/history", response_model=HistoryResponse)
async def get_history(session_id: str, request: Request):
    """
    读取会话历史节点列表接口：供前端展示对话窗口并选取分支节点（下标即节点位置）。
    """
    session_manager = request.app.state.session_manager
    turns = session_manager.get_history(session_id)
    # 进行中轮次不再随历史下发(Phase 4 退役):客户端统一经 GET /turn + /events
    # 恢复活跃轮;inflight.json 仅存崩溃防丢(write-ahead)一个职责。
    return {
        "status": True,
        "session_id": session_id,
        "count": len(turns),
        "turns": turns,
    }


@router.post("/api/v1/chat/{session_id}/branch", response_model=BranchResponse)
async def branch_session(session_id: str, request: Request, body: BranchRequest):
    """
    会话分支接口：从当前会话的任意历史节点复制出一个新的会话窗口。
    分支复用父会话的共同历史文本与 KV Cache，供前端“克隆对话窗口”使用。
    """
    session_manager = request.app.state.session_manager
    return await session_manager.branch_session(
        parent_session_id=session_id,
        node_index=body.node_index,
        new_session_id=body.new_session_id,
    )


def _sse(event: str, data: Dict[str, Any]) -> str:
    """序列化一条 SSE 事件帧（event + JSON data）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


# SSE 心跳间隔（秒）：静默生成阶段（预填充 / 思考 token / 约束采样）没有任何事件下发，
# 一条注释帧（": ping"）每隔这么久保活一次，顶住 cloudflared ~100s 无响应硬超时与代理空闲断连。
# 注释帧不含 event:/data: 字段，前端 parseSSE 会静默跳过（不产出 frame），零副作用。
_SSE_HEARTBEAT_SECS = 15
_SSE_HEARTBEAT = ": ping\n\n"


# ---------- 旧事件契约翻译层(兼容至 Phase 4;映射表见 PROTOCOL.md §4) ----------

_CONFLICT_MSG = {
    "turn_active": "该会话已有正在进行的轮次，请等它完结后再发送。",
    "no_pending_approval": "当前会话没有等待审批的操作（可能已过期或已被处理）。",
    "no_suspended_turn": "当前会话没有挂起的任务（可能已恢复、已完结或会话被回收）。",
    "approval_target_mismatch": "这条审批请求已过期：当前等待审批的是另一个轮次，请打开会话确认后再决议。",
}


async def _prepare_turn(request: Request, session_id: str, system: str, files):
    """两个对话端点的共用前置：取依赖、恢复/建会话、摄入解析（在推理 worker 之外完成）。"""
    orchestrator = request.app.state.orchestrator
    session_manager = request.app.state.session_manager
    scheduler = request.app.state.scheduler
    ingestion = request.app.state.ingestion

    session = session_manager.get_or_create(session_id, system)
    ingested = await ingestion.ingest(files, session.image_dir, session.audio_dir, session.attach_dir)
    return orchestrator, scheduler, session, {
        "images": ingested.get("images", []),
        "audios": ingested.get("audios", []),
        "ingested_text": ingested.get("ingested_text", ""),
        "attachments": ingested.get("attachments", []),
    }


@router.post("/api/v1/chat/{session_id}", response_model=ChatResponse)
async def chat_endpoint(
    session_id: str,
    request: Request,
    prompt: str = Form(...),
    system: str = Form(""),
    enable_thinking: bool = Form(False),
    thinking_budget: int = Form(2048),
    max_tokens: int = Form(4096),
    temperature: float = Form(0.2),
    min_p: float = Form(0.05),
    files: Optional[List[UploadFile]] = File(None)
):
    """
    非流式便捷入口（App Intents / 脚本一次性问答）：阻塞到整轮完成一次性返回。
    内部走 TurnRunner 同一闭环;交互客户端一律用 /turns(202) + /events(附着流)。
    """
    _, _, session, io = await _prepare_turn(request, session_id, system, files)
    runner = request.app.state.turn_runner
    gen = {"temperature": temperature, "min_p": min_p, "enable_thinking": enable_thinking,
           "thinking_budget": thinking_budget, "max_tokens": max_tokens}
    try:
        ctx = await runner.accept_turn(
            session, session_id, prompt=prompt, images=io["images"], audios=io["audios"],
            ingested_text=io["ingested_text"], attachments=io["attachments"], gen=gen)
    except TurnConflict as e:
        return {
            "status": "fail",
            "prompt": prompt, "image": [], "audio": [],
            "enable_thinking": enable_thinking, "max_tokens": max_tokens,
            "temperature": temperature, "min_p": min_p,
            "response": [{"role": "system", "content": _CONFLICT_MSG.get(e.reason, e.reason)}],
        }
    return public_turn(await runner.run_accepted(session, session_id, ctx))


# ---------- 事件账本端点(PROTOCOL.md §2.4~§2.6,协议 v1 正式面) ----------


@router.post("/api/v1/chat/{session_id}/turns")
async def submit_turn(
    session_id: str,
    request: Request,
    prompt: str = Form(...),
    system: str = Form(""),
    enable_thinking: bool = Form(False),
    thinking_budget: int = Form(2048),
    max_tokens: int = Form(4096),
    temperature: float = Form(0.2),
    min_p: float = Form(0.05),
    files: Optional[List[UploadFile]] = File(None)
):
    """提交一个新轮次：摄入完成即受理返回（202 {turn_id, seq}），执行在背景进行,
    过程与终态经 GET /events 附着流观察。会话已有排队/运行中的轮次时 409。"""
    _, _, session, io = await _prepare_turn(request, session_id, system, files)
    runner = request.app.state.turn_runner
    gen = {"temperature": temperature, "min_p": min_p, "enable_thinking": enable_thinking,
           "thinking_budget": thinking_budget, "max_tokens": max_tokens}
    try:
        ctx = await runner.accept_turn(
            session, session_id, prompt=prompt, images=io["images"], audios=io["audios"],
            ingested_text=io["ingested_text"], attachments=io["attachments"], gen=gen)
    except TurnConflict as e:
        return JSONResponse(status_code=409, content={"status": False, "reason": e.reason})
    runner.spawn(session_id, runner.run_accepted(session, session_id, ctx))
    return JSONResponse(status_code=202, content={
        "status": True, "turn_id": ctx["turn_id"], "seq": ctx["seq"]})


@router.post("/api/v1/chat/{session_id}/turns/approval")
async def submit_approval(session_id: str, request: Request, body: ApprovalRequest):
    """审批决议（202 受理即返）：过程与终态经 GET /events 观察。无等待审批的轮次时 409。
    expected_turn_id（可选，推送按钮的远程决议带上）：目标轮已换时 409 防错批。"""
    runner = request.app.state.turn_runner
    session = request.app.state.session_manager.get_or_create(session_id)
    try:
        turn_id = await runner.accept_approval(session, session_id, body.approve,
                                               expected_turn_id=body.expected_turn_id)
    except TurnConflict as e:
        return JSONResponse(status_code=409, content={"status": False, "reason": e.reason})
    runner.spawn(session_id, runner.run_approval(session, session_id, body.approve))
    return JSONResponse(status_code=202, content={"status": True, "turn_id": turn_id})


@router.post("/api/v1/chat/{session_id}/turns/resume")
async def submit_resume(session_id: str, request: Request):
    """恢复挂起轮次（202 受理即返）：过程与终态经 GET /events 观察。无挂起轮次时 409。"""
    runner = request.app.state.turn_runner
    session = request.app.state.session_manager.get_or_create(session_id)
    try:
        turn_id = await runner.accept_resume(session, session_id)
    except TurnConflict as e:
        return JSONResponse(status_code=409, content={"status": False, "reason": e.reason})
    runner.spawn(session_id, runner.run_resume(session, session_id))
    return JSONResponse(status_code=202, content={"status": True, "turn_id": turn_id})


@router.get("/api/v1/chat/{session_id}/turn")
async def get_turn_state(session_id: str, request: Request):
    """当前轮次的可恢复状态（PROTOCOL.md §2.5）：客户端刷新/重启后据此即时渲染,
    再带 after 游标附着 /events 续播。无活跃轮次时 turn 为 null。"""
    session_manager = request.app.state.session_manager
    session = session_manager._all_sessions.get(session_id)
    if session is not None:
        journal = session.journal
    else:
        sdir = session_manager.sessions_dir / session_id
        if not sdir.exists():
            return {"status": True, "turn": None, "last_seq": 0}
        journal = TurnJournal(sdir)  # 只读实例：冷会话无活跃轮,仅取账本游标

    snap = journal.turn_snapshot()
    if snap is not None and snap["state"] == "queued":
        # 排队位次 = 比本会话更早入队的排队任务数（0 = 下一个被准入）
        tasks = request.app.state.scheduler.snapshot().get("tasks", [])
        mine = next((t for t in tasks
                     if t["session_id"] == session_id and t["status"] == "queued"), None)
        snap["queue_position"] = (
            sum(1 for t in tasks
                if t["status"] == "queued" and t["waiting_seconds"] > mine["waiting_seconds"])
            if mine else 0
        )
    return {"status": True, "turn": snap, "last_seq": journal.seq}


@router.get("/api/v1/events")
async def attach_global_feed(request: Request, after: int = 0):
    """
    全局任务 feed（SSE，PROTOCOL.md §2.7）：跨会话聚合生命周期事件
    （turn_queued/started/awaiting_approval/suspended/resumed/turn_ended,信封含
    session_id 与独立 feed_seq 游标）。任务中心与后台通知的推送源,替代 /tasks
    差分轮询;环形缓冲兜短时断线,深度断档由 /tasks 快照对账。
    """
    feed = request.app.state.feed

    async def event_gen():
        queue = feed.subscribe()
        try:
            yield _sse("stream_attached", {
                "feed_seq": 0, "type": "stream_attached",
                "data": {"v": 1, "last_feed_seq": feed.seq},
            })
            last = after
            for item in feed.replay(after):
                last = max(last, item["feed_seq"])
                yield _sse(item["type"], item)
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SECS)
                except asyncio.TimeoutError:
                    yield _SSE_HEARTBEAT
                    continue
                if item is None:
                    break
                if item["feed_seq"] <= last:
                    continue
                last = item["feed_seq"]
                yield _sse(item["type"], item)
        finally:
            feed.unsubscribe(queue)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/api/v1/chat/{session_id}/events")
async def attach_events(session_id: str, request: Request, after: int = 0):
    """
    事件附着流（SSE，PROTOCOL.md §2.4）：首帧 stream_attached，随后重放 seq > after 的
    可重放事件（journal 里程碑 + 活跃轮内存环全量），再转实时续播。断开任意重连,
    带上已消费的最大 seq 即可。长连贯穿多个轮次（不因轮次完结闭流）。
    """
    session_manager = request.app.state.session_manager
    session = session_manager._all_sessions.get(session_id)
    if session is None:
        if not (session_manager.sessions_dir / session_id).exists():
            raise HTTPException(status_code=404, detail="会话不存在。")
        session = session_manager.get_or_create(session_id)
    journal = session.journal

    async def event_gen():
        queue = journal.subscribe()  # 先订阅后重放：重放期间的新事件按 seq 去重续上
        try:
            yield _sse("stream_attached", {
                "seq": 0, "turn_id": None, "ts": round(time.time(), 3),
                "type": "stream_attached", "data": {"v": 1, "last_seq": journal.seq},
            })
            last = after
            for env in await asyncio.to_thread(journal.replay, after):
                last = max(last, env["seq"])
                yield _sse(env["type"], env)
            while True:
                try:
                    env = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SECS)
                except asyncio.TimeoutError:
                    yield _SSE_HEARTBEAT
                    continue
                if env is None:
                    break  # 会话被删除,账本终结
                if env["seq"] <= last:
                    continue
                last = env["seq"]
                yield _sse(env["type"], env)
        finally:
            journal.unsubscribe(queue)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
