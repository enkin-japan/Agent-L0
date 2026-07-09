import asyncio
import json
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse

from .schemas import (
    ChatResponse,
    ApprovalRequest,
    CancelResponse,
    DeleteSessionResponse,
    DeleteTurnResponse,
    HistoryResponse,
    BranchRequest,
    BranchResponse,
    MemoryStatusResponse,
    PinRequest,
    PinResponse,
    SessionListResponse,
    ScheduleActionResponse,
    ScheduleListResponse,
    ScheduleRunsResponse,
    ScheduleToggleRequest,
    SystemPromptResponse,
    TaskListResponse,
    TaskStatusResponse,
    ToolsResponse,
    UiStatePutRequest,
    UiStateResponse,
)
from ..core.task_state import TaskState
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


def _restore_task(session, snapshot):
    """按轮初快照回滚会话的任务状态（引擎故障/用户取消时与 history 回滚同步）"""
    session.task_state = TaskState.from_snapshot(snapshot) if snapshot else None


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


# ---------- UI 状态服务端化（客户端迁移：换设备打开即恢复工作区/会话结构） ----------
# 落盘在 sessions 目录的同级文件（不放 sessions_dir 内部，避免会话目录扫描把它当会话）。
# state 是不透明 JSON blob（shape 归前端管），last-write-wins，个人场景不做版本向量。
_UI_STATE_LOCK = asyncio.Lock()
_UI_STATE_MAX_BYTES = 1024 * 1024  # 1MB 上限：会话元数据量级远小于此，超限=前端出错或滥用


def _ui_state_path(request: Request):
    return request.app.state.session_manager.sessions_dir.parent / "ui_state.json"


@router.get("/api/v1/ui-state", response_model=UiStateResponse)
async def get_ui_state(request: Request):
    """读取前端 UI 状态（工作区/会话元数据）。从未写入过则 state 为 null。"""
    path = _ui_state_path(request)

    def _read():
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    try:
        doc = await asyncio.to_thread(_read)
    except Exception as e:
        # 文件损坏按空状态处理（前端会回退 localStorage 并在下次写入时覆盖修复）
        print(f"[Routes] ui_state.json 读取失败（按空处理）: {e}")
        doc = None
    if not doc:
        return {"status": True, "state": None, "updated_at": None}
    return {"status": True, "state": doc.get("state"), "updated_at": doc.get("updated_at")}


@router.put("/api/v1/ui-state", response_model=UiStateResponse)
async def put_ui_state(request: Request, body: UiStatePutRequest):
    """整体覆盖写入前端 UI 状态（last-write-wins）。原子写：tmp + rename。"""
    payload = {"state": body.state, "updated_at": time.time()}
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw.encode("utf-8")) > _UI_STATE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="ui state too large (limit 1MB)")
    path = _ui_state_path(request)

    def _write():
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(raw, encoding="utf-8")
        tmp.replace(path)

    async with _UI_STATE_LOCK:
        await asyncio.to_thread(_write)
    return {"status": True, "state": body.state, "updated_at": payload["updated_at"]}


# ---------- 会话资产出货（render_chart / deck 等工具产物 → 前端展示） ----------
# 安全边界：只从 sessions/{id}/assets/ 出货；session_id 与文件名双重白名单正则 +
# resolve 包含校验，双保险防路径穿越（_ASSET_NAME_RE 定义在文件顶部，与路由级
# 路径 ID 校验共用）。经既有 Bearer 鉴权（前端 <img> 走同源 /api 相对路径，
# Authorization 由 Vite/Nginx 代理注入）。文件名含毫秒时间戳恒不复用，可安全长缓存。


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
            "total_turns": len(session.history),
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


@router.post("/api/v1/chat/{session_id}/task/resume", response_model=ChatResponse)
async def resume_task(session_id: str, request: Request):
    """
    恢复一个已挂起的任务：重新排队申请 worker，再次进入编排闭环从挂起处续跑
    （run_turn 可重入，从 task_state + 已提交 history 接着走）。响应仅含本次续跑新增的消息。
    续跑若再次挂起/触发审批，则相应转入挂起/审批暂存；正常完结则整轮合并落盘。
    """
    orchestrator = request.app.state.orchestrator
    session_manager = request.app.state.session_manager
    scheduler = request.app.state.scheduler
    session = session_manager.get_or_create(session_id)

    async with session.lock:
        turn = session.suspended_turn
        if turn is None:
            return {
                "status": "fail",
                "prompt": "", "image": [], "audio": [],
                "enable_thinking": False, "max_tokens": 0, "temperature": 0.0, "min_p": 0.0,
                "response": [{"role": "system", "content": "当前会话没有挂起的任务（可能已恢复、已完结或会话被回收）。"}],
            }

        session.cancel_requested = False
        session.suspend_requested = False
        history_start_idx = len(session.history)
        final_result = None
        # 恢复：把挂起句柄重新排队并等待准入（无挂起句柄则退化为一次新准入，兜底进程重启等情形）
        if await scheduler.resume(session_id) is None:
            await scheduler.acquire(session_id, kind="resume")
        try:
            final_result = await orchestrator.run_turn(
                session=session,
                compact_history=False,  # 轮中重入：本轮未落盘观察不参与压缩（防压缩桩落盘）
                temperature=turn.get("temperature", 0.2),
                min_p=turn.get("min_p", 0.05),
                enable_thinking=turn.get("enable_thinking", False),
                thinking_budget=turn.get("thinking_budget", 2048),
                max_tokens=turn.get("max_tokens", 4096),
            )

            if final_result.get("status") == "cancelled":
                # 续跑期间被取消：整轮回滚到最初起点（含最早的用户消息），落盘取消存档
                rollback_idx = turn.get("_rollback_idx")
                if isinstance(rollback_idx, int) and 0 <= rollback_idx <= len(session.history):
                    session.history = session.history[:rollback_idx]
                _restore_task(session, turn.get("_task_snapshot"))
                note = {"role": "system", "content": "本轮已被用户取消，未产生任何记忆变更。"}
                turn["status"] = "cancelled"
                turn["response"].append(note)
                turn["pending_tools"] = None
                response_data = {**turn, "response": [note]}
            else:
                new_messages = session.history[history_start_idx:]
                turn["response"].extend(new_messages)  # 累计到完整轮次（落盘用）
                turn["status"] = final_result.get("status", "success")
                turn["pending_tools"] = final_result.get("pending_tools")
                turn.update(final_result.get("metrics", {}))
                response_data = {**turn, "response": new_messages}  # 响应仅带本次增量

        except Exception as e:
            import traceback
            error_details = traceback.format_exc()

            # 续跑故障：整轮回滚出内存记忆与任务状态，落盘为 fail
            rollback_idx = turn.get("_rollback_idx")
            if isinstance(rollback_idx, int) and 0 <= rollback_idx <= len(session.history):
                session.history = session.history[:rollback_idx]
            _restore_task(session, turn.get("_task_snapshot"))
            error_message = {"role": "system", "content": f"任务恢复续跑故障: {str(e)}\n{error_details}"}
            turn["status"] = "fail"
            turn["response"].append(error_message)
            turn["pending_tools"] = None
            response_data = {**turn, "response": [error_message]}
        finally:
            if final_result is not None and final_result.get("status") == "suspended":
                await scheduler.suspend(session_id)
            else:
                await scheduler.release(session_id)

        # 续跑后状态分流：又挂起→继续暂存；又触发审批→转审批暂存；否则整轮合并落盘
        status = response_data.get("status")
        if status == "suspended":
            session.suspended_turn = turn  # turn 已累计本次增量，继续挂起
        elif status == "pending_approval":
            session.suspended_turn = None
            session.pending_turn = turn    # 携原账目（_rollback_idx/_task_snapshot）转审批
        else:
            session.suspended_turn = None
            turn.pop("_rollback_idx", None)
            turn.pop("_task_snapshot", None)
            turn.pop("_history_start_idx", None)
            await asyncio.to_thread(session.append_log, turn)
            await asyncio.to_thread(session.save_task)
            await _maybe_summarize_active(session, orchestrator, scheduler, session_id)

    return response_data


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
    # 进行中轮次（inflight 预写标记）：作为独立字段返回，绝不并入 turns（分支下标依赖 turns
    # 位置）。使刷新时正在生成/待审批/挂起的轮次可见（用户消息 + 生成中），而非空白会话。
    inflight = session_manager.read_inflight(session_id)
    return {
        "status": True,
        "session_id": session_id,
        "count": len(turns),
        "turns": turns,
        "inflight": inflight,
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


def _public_turn(response_data: Dict[str, Any]) -> Dict[str, Any]:
    """剥掉内部账目键（_rollback_idx / _task_snapshot / _history_start_idx）得到可下发前端的轮次数据。"""
    return {k: v for k, v in response_data.items() if not k.startswith("_")}


async def _chat_turn(
    session, session_id: str, orchestrator, scheduler, *,
    prompt: str, images, audios, ingested_text: str, attachments,
    gen: Dict[str, Any], on_text_delta=None, emit=None,
) -> Dict[str, Any]:
    """
    对话轮的加锁处理闭环（JSON 端点与 SSE 端点共享）：轮前清理（待审批/挂起残留）→ 追加用户
    消息 → 单 worker 调度门闸 → run_turn 编排 → 结果分流（成功/取消/故障）→ 落盘。
    on_text_delta 非 None 时经引擎把可见正文逐段回吐（真流式）；终态仍以返回的 response_data
    为准（流为预览、终态为真）。逻辑与原 chat_endpoint 一致，仅抽取为可复用闭环 + 透传流式回调。
    """
    temperature = gen["temperature"]
    min_p = gen["min_p"]
    enable_thinking = gen["enable_thinking"]
    thinking_budget = gen["thinking_budget"]
    max_tokens = gen["max_tokens"]

    # 同一会话的并发请求必须整轮串行：history 追加、KV Cache 读写、
    # 日志落盘都不是并发安全的（不同会话之间仍可并发受理）
    async with session.lock:
        # 若上一轮敏感操作仍在等待审批而用户直接发来新消息：视为放弃，
        # 挂起的工具调用不执行，该轮补上取消结果后以 warning 落盘收尾
        if session.pending_tool_calls:
            cancelled = [
                {"name": c.get("name", "unknown"),
                 "response": "Operation cancelled: the user sent a new message without approving."}
                for c in session.pending_tool_calls if c.get("type") != "error"
            ] or [{"name": "system", "response": "Pending tool call cancelled."}]
            session.add_tool_message(cancelled)
            session.pending_tool_calls = None
            if session.pending_turn is not None:
                session.pending_turn.pop("_rollback_idx", None)
                session.pending_turn.pop("_task_snapshot", None)
                session.pending_turn["status"] = "warning"
                session.pending_turn.setdefault("response", []).append(
                    {"role": "tool", "tool_responses": cancelled}
                )
                await asyncio.to_thread(session.append_log, session.pending_turn)
                session.pending_turn = None

        # 若有挂起的任务而用户直接发来新消息：视为改变方向、放弃该挂起任务
        # （正规恢复途径是 /task/resume）。已产出的局部工作留在 history 可见，
        # 该轮以 warning 落盘收尾，任务状态清空后按新消息重新开始。
        if session.suspended_turn is not None:
            susp = session.suspended_turn
            susp.pop("_rollback_idx", None)
            susp.pop("_task_snapshot", None)
            susp.pop("_history_start_idx", None)
            susp["status"] = "warning"
            susp.setdefault("response", []).append(
                {"role": "system", "content": "The suspended task was interrupted by a new user message and abandoned (not continued)."}
            )
            await asyncio.to_thread(session.append_log, susp)
            session.suspended_turn = None
            session.suspend_requested = False
            session.task_state = None
            if scheduler is not None:
                scheduler.discard(session_id)  # 清掉残留的挂起句柄

        # 将新解析的资源加入本会话的生命周期
        session.images.extend(images)
        session.audios.extend(audios)

        # 本轮起点快照：引擎故障/用户取消时，history 与任务状态同步整轮回滚
        session.cancel_requested = False
        rollback_idx = len(session.history)
        task_snapshot = session.task_state.snapshot() if session.task_state else None
        session.add_user_message(prompt, images, audios, extra_text=ingested_text)
        history_start_idx = len(session.history)
        # 轮次预写标记（write-ahead）：进程若在本轮完结前死亡，启动恢复会据此补 fail 轮，
        # 用户输入不蒸发。append_log 时自动撤销。
        await asyncio.to_thread(session.write_inflight, prompt, gen)

        # 任务级调度门闸：向单 worker 调度器申请准入（其余任务排队），
        # run_turn 全程独占 worker，结束后释放并调度下一个。底层生成锁仍在其内层保证原子性。
        final_result = None
        await scheduler.acquire(session_id, kind="chat")
        try:
            # 4. 领域层：交付给编排循环执行完整闭环（计划推进/工具调用/失败路由）
            #    on_text_delta 经 gen_kwargs 透传至 engine.generate_once，逐段回吐可见正文
            final_result = await orchestrator.run_turn(
                session=session,
                emit=emit,
                temperature=temperature,
                min_p=min_p,
                enable_thinking=enable_thinking,
                thinking_budget=thinking_budget,
                max_tokens=max_tokens,
                on_text_delta=on_text_delta,
            )

            if final_result.get("status") == "cancelled":
                # 用户取消：与引擎故障同语义——整轮（含用户消息与中途产物）回滚出内存，
                # KV 缓存多算的部分由下次推理前缀匹配自动裁剪；落盘保留取消存档
                session.history = session.history[:rollback_idx]
                _restore_task(session, task_snapshot)
                response_data = {
                    "status": "cancelled",
                    "prompt": prompt,
                    "image": images,
                    "audio": audios,
                    "ingested_text": ingested_text,
                    "attachments": attachments,
                    "enable_thinking": enable_thinking,
                    "thinking_budget": thinking_budget,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "min_p": min_p,
                    "response": [{"role": "system", "content": "本轮已被用户取消，未产生任何记忆变更。"}],
                    **final_result.get("metrics", {})
                }
            else:
                # 截取本轮发生的新动作、思考与对话记录
                new_messages = session.history[history_start_idx:]
                response_data = {
                    "status": final_result.get("status", "success"),
                    "prompt": prompt,
                    "image": images,
                    "audio": audios,
                    "ingested_text": ingested_text,
                    "attachments": attachments,
                    "enable_thinking": enable_thinking,
                    "thinking_budget": thinking_budget,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "min_p": min_p,
                    "response": new_messages,
                    "pending_tools": final_result.get("pending_tools"),
                    **final_result.get("metrics", {})
                }

        except Exception as e:
            import traceback
            error_details = traceback.format_exc()

            # 引擎故障：连同本轮用户消息一起回滚内存记忆与任务状态，防止污染
            session.history = session.history[:rollback_idx]
            _restore_task(session, task_snapshot)
            response_data = {
                "status": "fail",
                "prompt": prompt,
                "image": images,
                "audio": audios,
                "ingested_text": ingested_text,
                "attachments": attachments,
                "enable_thinking": enable_thinking,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "min_p": min_p,
                "response": [{"role": "system", "content": f"核心引擎故障: {str(e)}\n{error_details}"}]
            }
        finally:
            # 挂起让出 vs 正常结束分流：挂起时转 worker 给下一个任务（KV 留内存待恢复），
            # 其余情况（成功/取消/故障/待审批）正常释放 worker 并调度下一个。落盘属 I/O，不占 worker。
            if final_result is not None and final_result.get("status") == "suspended":
                await scheduler.suspend(session_id)
            else:
                await scheduler.release(session_id)

        # 5. 持久化层：落盘历史记录（锁内执行，保证 history.jsonl 轮次顺序与完整性）。
        # 未完结的轮次（待审批 / 已挂起）尚不落盘：暂存到会话上，待决议/恢复后合并落盘。
        if response_data.get("status") == "pending_approval":
            response_data["_rollback_idx"] = rollback_idx      # 决议执行故障时整轮回滚
            response_data["_task_snapshot"] = task_snapshot    # 任务状态同步回滚
            session.pending_turn = response_data
        elif response_data.get("status") == "suspended":
            response_data["_rollback_idx"] = rollback_idx      # 恢复续跑故障时整轮回滚
            response_data["_task_snapshot"] = task_snapshot
            response_data["_history_start_idx"] = history_start_idx  # 恢复时接续截取增量消息
            session.suspended_turn = response_data
        else:
            await asyncio.to_thread(session.append_log, response_data)
            # 跨轮任务快照落盘：保证冷启动后任务不丢/不残留
            await asyncio.to_thread(session.save_task)
            await _maybe_summarize_active(session, orchestrator, scheduler, session_id)

    return response_data


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
    核心对话接口（非流式，回退路径）：一次性返回整轮结果。真流式见 /stream。
    """
    orchestrator, scheduler, session, io = await _prepare_turn(request, session_id, system, files)
    gen = {"temperature": temperature, "min_p": min_p, "enable_thinking": enable_thinking,
           "thinking_budget": thinking_budget, "max_tokens": max_tokens}
    return await _chat_turn(
        session, session_id, orchestrator, scheduler,
        prompt=prompt, images=io["images"], audios=io["audios"],
        ingested_text=io["ingested_text"], attachments=io["attachments"], gen=gen,
    )


@router.post("/api/v1/chat/{session_id}/stream")
async def chat_stream(
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
    真流式对话接口（SSE）：响应体是事件流。事件——
      turn_start / token(可见正文逐段) / turn_end(权威 response_data，含终态与完整消息)。
    契约：token 为临时预览，turn_end 为真（前端据 turn_end 对账；终态若 cancelled/fail 则撤回预览）。
    编排在后台任务内跑完整闭环并落盘；客户端中途断开也不丢工作（轮次照常完成并存档）。
    """
    orchestrator, scheduler, session, io = await _prepare_turn(request, session_id, system, files)
    gen = {"temperature": temperature, "min_p": min_p, "enable_thinking": enable_thinking,
           "thinking_budget": thinking_budget, "max_tokens": max_tokens}
    turn_id = uuid.uuid4().hex

    async def event_gen():
        queue: asyncio.Queue = asyncio.Queue()

        def emit(event: str, data: Dict[str, Any]):
            # 引擎/编排线程内同步调用（本事件循环）：入队不阻塞，随后引擎的 sleep(0) 让出，
            # 本 SSE 生成器得以出队下发。所有事件统一带 turn_id。
            queue.put_nowait((event, {"turn_id": turn_id, **data}))

        def emit_token(text: str):
            emit("token", {"delta": text})

        async def worker():
            try:
                rd = await _chat_turn(
                    session, session_id, orchestrator, scheduler,
                    prompt=prompt, images=io["images"], audios=io["audios"],
                    ingested_text=io["ingested_text"], attachments=io["attachments"],
                    gen=gen, on_text_delta=emit_token, emit=emit,
                )
                queue.put_nowait(("turn_end", {"turn_id": turn_id, **_public_turn(rd)}))
            except Exception as e:
                import traceback
                queue.put_nowait(("turn_end", {
                    "turn_id": turn_id, "status": "fail",
                    "response": [{"role": "system",
                                  "content": f"流式处理故障: {str(e)}\n{traceback.format_exc()}"}],
                }))
            finally:
                queue.put_nowait(None)  # 哨兵：通知生成器收流

        task = asyncio.create_task(worker())
        yield _sse("turn_start", {"turn_id": turn_id, "session_id": session_id})
        try:
            while True:
                # 出队等待带心跳超时：静默生成阶段（无 token/事件）也周期性发注释帧保活。
                # 超时不丢事件——queue.get() 被取消时未出队的项仍留在队列，下轮取回。
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SECS)
                except asyncio.TimeoutError:
                    yield _SSE_HEARTBEAT
                    continue
                if item is None:
                    break
                yield _sse(item[0], item[1])
        finally:
            # 客户端断开时生成器被关闭：不取消后台任务，让轮次跑完并落盘（工作不丢）。
            if not task.done():
                task.add_done_callback(lambda t: t.exception())

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


async def _resume_approval_turn(
    session, session_id: str, orchestrator, scheduler, *,
    approve: bool,
    emit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    on_text_delta: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    审批决议后的续跑闭环（阻塞端点与 SSE 端点共享的单一实现）：持锁续跑 → 结果分流/回滚 →
    落盘或暂存（再审批/挂起）。返回 response_data（response 仅含本次决议后新增的消息）。
    emit / on_text_delta 非 None 时经引擎把编排事件与可见正文逐段回吐（真流式）；均为 None
    时行为与改动前的阻塞端点完全一致。两端共用本函数，避免收尾逻辑双路径漂移。
    """
    async with session.lock:
        turn = session.pending_turn
        if not session.pending_tool_calls or turn is None:
            return {
                "status": "fail",
                "prompt": "", "image": [], "audio": [],
                "enable_thinking": False, "max_tokens": 0, "temperature": 0.0, "min_p": 0.0,
                "response": [{"role": "system", "content": "当前会话没有等待审批的操作（可能已过期或已被处理）。"}],
            }

        session.cancel_requested = False
        history_start_idx = len(session.history)
        final_result = None
        # 审批决议后的续跑同样是一次生成，走单 worker 调度门闸（与对话轮同等排队）
        await scheduler.acquire(session_id, kind="approval")
        try:
            # 复用挂起轮次的生成参数，恢复编排闭环（多步任务从挂起的步骤继续）
            final_result = await orchestrator.resume_after_approval(
                session,
                approved=approve,
                temperature=turn.get("temperature", 0.2),
                min_p=turn.get("min_p", 0.05),
                enable_thinking=turn.get("enable_thinking", False),
                thinking_budget=turn.get("thinking_budget", 2048),
                max_tokens=turn.get("max_tokens", 4096),
                emit=emit,
                on_text_delta=on_text_delta,
            )

            if final_result.get("status") == "cancelled":
                # 决议后续跑期间被取消：整轮回滚（含最初的用户消息），落盘取消存档
                rollback_idx = turn.get("_rollback_idx")
                if isinstance(rollback_idx, int) and 0 <= rollback_idx <= len(session.history):
                    session.history = session.history[:rollback_idx]
                _restore_task(session, turn.get("_task_snapshot"))
                note = {"role": "system", "content": "本轮已被用户取消，未产生任何记忆变更。"}
                turn["status"] = "cancelled"
                turn["response"].append(note)
                turn["pending_tools"] = None
                response_data = {**turn, "response": [note]}
            else:
                new_messages = session.history[history_start_idx:]
                # 决议后的消息合并进挂起轮次（落盘用），响应体只带增量消息
                turn["response"].extend(new_messages)
                turn["status"] = final_result.get("status", "success")
                turn["pending_tools"] = final_result.get("pending_tools")
                turn.update(final_result.get("metrics", {}))
                response_data = {**turn, "response": new_messages}

        except Exception as e:
            import traceback
            error_details = traceback.format_exc()

            # 决议执行故障：整轮（含最初的用户消息）回滚出内存记忆与任务状态，落盘为 fail
            rollback_idx = turn.get("_rollback_idx")
            if isinstance(rollback_idx, int) and 0 <= rollback_idx <= len(session.history):
                session.history = session.history[:rollback_idx]
            _restore_task(session, turn.get("_task_snapshot"))
            error_message = {"role": "system", "content": f"审批决议执行故障: {str(e)}\n{error_details}"}
            turn["status"] = "fail"
            turn["response"].append(error_message)
            turn["pending_tools"] = None
            response_data = {**turn, "response": [error_message]}
        finally:
            # 续跑期间被挂起（用户点了挂起按钮）：与 _chat_turn 同语义——转挂起态让出
            # worker（句柄保留可恢复），其余情况正常释放。此前无脑 release 会把挂起轮
            # 当完结轮落盘、丢失可恢复句柄，前端"恢复"按钮点了报"没有挂起的任务"。
            if final_result is not None and final_result.get("status") == "suspended":
                await scheduler.suspend(session_id)
            else:
                await scheduler.release(session_id)

        # 状态分流：又出现敏感调用→继续审批暂存；被挂起→转挂起暂存（待 /task/resume）；
        # 否则整轮合并落盘
        if response_data.get("status") == "pending_approval":
            session.pending_turn = turn
        elif response_data.get("status") == "suspended":
            session.pending_turn = None
            session.suspended_turn = turn  # 携原账目（_rollback_idx/_task_snapshot）转挂起
        else:
            session.pending_turn = None
            turn.pop("_rollback_idx", None)
            turn.pop("_task_snapshot", None)
            turn.pop("_history_start_idx", None)  # 挂起→审批转移路径可能带过来，防内部键落盘
            await asyncio.to_thread(session.append_log, turn)
            await asyncio.to_thread(session.save_task)
            await _maybe_summarize_active(session, orchestrator, scheduler, session_id)

    return response_data


@router.post("/api/v1/chat/{session_id}/approval", response_model=ChatResponse)
async def approve_pending_tools(session_id: str, request: Request, body: ApprovalRequest):
    """
    敏感工具审批决议接口（非流式，回退路径）：一次性返回决议后新增的消息。真流式见 /approval/stream。
    批准则执行工具并继续任务，拒绝则由模型调整后续策略。
    """
    orchestrator = request.app.state.orchestrator
    session_manager = request.app.state.session_manager
    scheduler = request.app.state.scheduler
    session = session_manager.get_or_create(session_id)
    return await _resume_approval_turn(
        session, session_id, orchestrator, scheduler, approve=body.approve,
    )


@router.post("/api/v1/chat/{session_id}/approval/stream")
async def approve_pending_tools_stream(session_id: str, request: Request, body: ApprovalRequest):
    """
    审批决议真流式接口（SSE）：与 /stream 同一套事件契约（turn_start/token/thinking/
    tool_started/tool_result/task_progress/turn_end）。续跑在后台 worker 内跑完整闭环并落盘，
    客户端中途断开也不丢工作（决议照常执行、轮次照常完结存档）。带心跳保活，续跑再慢也不掉线。
    """
    orchestrator = request.app.state.orchestrator
    session_manager = request.app.state.session_manager
    scheduler = request.app.state.scheduler
    session = session_manager.get_or_create(session_id)
    turn_id = uuid.uuid4().hex

    async def event_gen():
        queue: asyncio.Queue = asyncio.Queue()

        def emit(event: str, data: Dict[str, Any]):
            queue.put_nowait((event, {"turn_id": turn_id, **data}))

        def emit_token(text: str):
            emit("token", {"delta": text})

        async def worker():
            try:
                rd = await _resume_approval_turn(
                    session, session_id, orchestrator, scheduler, approve=body.approve,
                    emit=emit, on_text_delta=emit_token,
                )
                queue.put_nowait(("turn_end", {"turn_id": turn_id, **_public_turn(rd)}))
            except Exception as e:
                import traceback
                queue.put_nowait(("turn_end", {
                    "turn_id": turn_id, "status": "fail",
                    "response": [{"role": "system",
                                  "content": f"审批流式处理故障: {str(e)}\n{traceback.format_exc()}"}],
                }))
            finally:
                queue.put_nowait(None)  # 哨兵：通知生成器收流

        task = asyncio.create_task(worker())
        yield _sse("turn_start", {"turn_id": turn_id, "session_id": session_id})
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SECS)
                except asyncio.TimeoutError:
                    yield _SSE_HEARTBEAT
                    continue
                if item is None:
                    break
                yield _sse(item[0], item[1])
        finally:
            # 客户端断开时生成器被关闭：不取消后台任务，让决议续跑跑完并落盘（工作不丢）。
            if not task.done():
                task.add_done_callback(lambda t: t.exception())

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
