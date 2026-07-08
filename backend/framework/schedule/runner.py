"""定时任务触发循环与无人值守执行器。

执行模型：每个 schedule 一个专属常驻会话（session_id = "schedule_{id}"），每次运行
是该会话里的一个普通 turn——结果天然可见（前端打开该会话即完整记录、可继续追问），
跨次运行有上下文连续性，增长由既有 summarize/驱逐机制接管。

调度语义：
- 准入 kind="scheduled"、priority=-1 —— 交互任务永远优先；
- 被交互任务抢占时走 suspend（agentic 多步运行不像 summarize 那样可安全放弃重跑），
  执行器循环 scheduler.resume 等 worker 空闲后续跑；
- 无人值守审批：run_turn 返回 pending_approval 时立即以 approved=False 决议
  （模型收到 DENIED 后收尾）。副产品安全性质：schedule_* 工具本身需审批，
  故定时任务无法在无人值守时创建/修改别的定时任务，阻断注入内容自我增殖。
"""
import asyncio
import time
import uuid
from typing import Any, Dict, Optional

from .store import ScheduleStore

# 无人值守运行的固定生成参数（对齐 chat 端点默认值）
_GEN = {"temperature": 0.2, "min_p": 0.05, "enable_thinking": False,
        "thinking_budget": 2048, "max_tokens": 4096}
_MAX_AUTO_DENIES = 3   # 单次运行内自动拒绝审批的上限：模型反复撞审批墙即熔断收尾
_SUMMARY_CHARS = 200   # 运行历史里保留的答复摘要长度


def session_id_for(schedule_id: str) -> str:
    return f"schedule_{schedule_id}"


def _extract_summary(messages) -> str:
    """从本轮新增消息里取最后一条助手正文的首部，作运行历史摘要。"""
    for msg in reversed(messages or []):
        if msg.get("role") == "assistant" and msg.get("content"):
            text = str(msg["content"]).strip()
            return text[:_SUMMARY_CHARS]
    return ""


class ScheduleRunner:
    """触发扫描 + 无人值守执行（进程内单例，lifespan 装配）。"""

    def __init__(self, store: ScheduleStore, session_manager, orchestrator, scheduler,
                 cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self.store = store
        self.session_manager = session_manager
        self.orchestrator = orchestrator
        self.scheduler = scheduler
        self.tick_seconds = int(cfg.get("tick_seconds", 30))
        self.running: set = set()  # 正在执行（含排队/挂起等待）的 schedule_id，防同任务双发

    # ---------- 触发循环（lifespan 内 asyncio.create_task） ----------
    async def tick_loop(self):
        while True:
            try:
                await asyncio.sleep(self.tick_seconds)
                now = time.time()
                for task in self.store.list_tasks():
                    if not task.get("enabled") or task["id"] in self.running:
                        continue
                    next_run = task.get("next_run")
                    if next_run is None or next_run > now:
                        continue
                    # 触发即推进 next_run 并落盘（从现在起算：睡眠错过的多次只补跑这一次）
                    self.store.advance_after_fire(task["id"])
                    self.launch(task, scheduled_for=next_run)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Schedule] 触发循环异常（已忽略，继续）: {e}")

    def launch(self, task: Dict[str, Any], scheduled_for: float) -> bool:
        """派发一次运行（tick 与 /run 端点共用）。已在运行则拒绝。"""
        if task["id"] in self.running:
            return False
        self.running.add(task["id"])
        asyncio.create_task(self._execute(task, scheduled_for))
        return True

    # ---------- 无人值守执行器（仿 routes._chat_turn 骨架，无 SSE） ----------
    async def _execute(self, task: Dict[str, Any], scheduled_for: float):
        sid = session_id_for(task["id"])
        run: Dict[str, Any] = {
            "run_id": uuid.uuid4().hex[:12],
            "scheduled_for": scheduled_for,
            "started_at": time.time(),
            "finished_at": None,
            "status": "fail",
            "summary": "",
            "session_id": sid,
        }
        try:
            await self._execute_inner(task, run)
        except Exception as e:
            import traceback
            run["summary"] = f"runner error: {e}"
            print(f"[Schedule] 运行 {task['id']} 执行器异常: {e}\n{traceback.format_exc()}")
        finally:
            run["finished_at"] = time.time()
            self.running.discard(task["id"])
            self.store.append_run(task["id"], run)
            print(f"[Schedule] {task['id']} \"{task.get('name', '')}\" 运行结束：{run['status']}")

    async def _execute_inner(self, task: Dict[str, Any], run: Dict[str, Any]):
        session = self.session_manager.get_or_create(session_id_for(task["id"]))
        sid = session.session_id
        preamble = (
            f"[Scheduled run — unattended] This turn was triggered automatically by the "
            f"schedule \"{task['name']}\" at {time.strftime('%Y-%m-%d %H:%M')}. No user is "
            "watching right now: any tool that requires approval will be DENIED automatically, "
            "so prefer approval-free tools. Do NOT ask the user for confirmation or "
            "clarification — nobody can answer. Deliver your best result from the evidence you "
            "have and state its limitations honestly. Complete the task below and write your "
            "findings as the final answer — the user will read this conversation later.\n\nTask: "
        )

        async with session.lock:
            # 残留清理：专属会话也可能被用户手动聊过并留下待审批/挂起态（语义同 _chat_turn：
            # 新输入到来即视为放弃旧限态，补取消/中断记录后落盘收尾）
            if session.pending_tool_calls:
                cancelled = [
                    {"name": c.get("name", "unknown"),
                     "response": "Operation cancelled: a new scheduled run started without approval."}
                    for c in session.pending_tool_calls if c.get("type") != "error"
                ] or [{"name": "system", "response": "Pending tool call cancelled."}]
                session.add_tool_message(cancelled)
                session.pending_tool_calls = None
                if session.pending_turn is not None:
                    session.pending_turn.pop("_rollback_idx", None)
                    session.pending_turn.pop("_task_snapshot", None)
                    session.pending_turn["status"] = "warning"
                    session.pending_turn.setdefault("response", []).append(
                        {"role": "tool", "tool_responses": cancelled})
                    await asyncio.to_thread(session.append_log, session.pending_turn)
                    session.pending_turn = None
            if session.suspended_turn is not None:
                susp = session.suspended_turn
                for k in ("_rollback_idx", "_task_snapshot", "_history_start_idx"):
                    susp.pop(k, None)
                susp["status"] = "warning"
                susp.setdefault("response", []).append(
                    {"role": "system",
                     "content": "The suspended task was interrupted by a new scheduled run and abandoned."})
                await asyncio.to_thread(session.append_log, susp)
                session.suspended_turn = None
                session.suspend_requested = False
                session.task_state = None
                self.scheduler.discard(sid)

            session.cancel_requested = False
            session.suspend_requested = False
            rollback_idx = len(session.history)
            task_snapshot = session.task_state.snapshot() if session.task_state else None
            # preamble 一并进入落盘 prompt（见 _turn_data）：否则冷恢复重放的用户消息比
            # 当时的内存版少了 preamble，上下文不一致且 KV 前缀失配多付一次重预填充
            full_prompt = preamble + task["prompt"]
            session.add_user_message(full_prompt)
            history_start_idx = len(session.history)
            # 轮次预写标记：进程死亡时启动恢复补 fail 轮（对齐 routes._chat_turn）
            await asyncio.to_thread(session.write_inflight, full_prompt, dict(_GEN))

            final_result = None
            denies = 0
            await self.scheduler.acquire(sid, kind="scheduled", priority=-1)
            try:
                final_result = await self.orchestrator.run_turn(session=session, **_GEN)
                # 无人值守分支闭环：挂起（被交互抢占）→ 等 worker 续跑；审批 → 自动拒绝续跑
                while True:
                    status = final_result.get("status")
                    if status == "suspended":
                        await self.scheduler.suspend(sid)
                        if await self.scheduler.resume(sid, priority=-1) is None:
                            await self.scheduler.acquire(sid, kind="scheduled", priority=-1)
                        # 轮中重入：本轮未落盘观察不参与压缩（防压缩桩落盘）
                        final_result = await self.orchestrator.run_turn(
                            session=session, compact_history=False, **_GEN)
                        continue
                    if status == "pending_approval":
                        denies += 1
                        run["status"] = "denied_approval"  # 记档：本次运行撞过审批墙
                        from ..core import events
                        events.emit("approval_autodeny", session=sid,
                                    tools=[t.get("name") for t in
                                           (final_result.get("pending_tools") or [])])
                        if denies > _MAX_AUTO_DENIES:
                            session.add_tool_message([{
                                "name": "system",
                                "response": "Unattended run aborted: too many approval-required "
                                            "attempts. Stop and summarize what was done so far."}])
                            final_result = {"status": "warning", "metrics": {}}
                            break
                        final_result = await self.orchestrator.resume_after_approval(
                            session, approved=False, **_GEN)
                        continue
                    break

                if final_result.get("status") == "cancelled":
                    # 中止：与交互轮同语义——整轮回滚出内存，落盘取消存档
                    session.history = session.history[:rollback_idx]
                    from ..core.task_state import TaskState
                    session.task_state = TaskState.from_snapshot(task_snapshot) if task_snapshot else None
                    run["status"] = "cancelled"
                    response_data = self._turn_data(full_prompt, "cancelled", [
                        {"role": "system", "content": "Scheduled run cancelled by the user."}])
                else:
                    new_messages = session.history[history_start_idx:]
                    status = final_result.get("status", "success")
                    if run["status"] != "denied_approval":
                        run["status"] = status
                    run["summary"] = _extract_summary(new_messages)
                    response_data = self._turn_data(full_prompt, status, new_messages,
                                                    final_result.get("metrics"))
            except Exception as e:
                import traceback
                session.history = session.history[:rollback_idx]
                from ..core.task_state import TaskState
                session.task_state = TaskState.from_snapshot(task_snapshot) if task_snapshot else None
                run["status"] = "fail"
                run["summary"] = f"engine error: {e}"
                response_data = self._turn_data(full_prompt, "fail", [
                    {"role": "system", "content": f"Scheduled run failed: {e}\n{traceback.format_exc()}"}])
            finally:
                session.cancel_requested = False
                await self.scheduler.release(sid)

            await asyncio.to_thread(session.append_log, response_data)
            await asyncio.to_thread(session.save_task)

    @staticmethod
    def _turn_data(prompt: str, status: str, messages, metrics=None) -> Dict[str, Any]:
        """组装与 _chat_turn 同构的轮次数据（history.jsonl 落盘 / 前端历史渲染兼容）。
        prompt 为含 preamble 的完整用户消息，与内存 history 一致（冷恢复重放不失配）。"""
        return {
            "status": status,
            "prompt": prompt,
            "image": [], "audio": [],
            "ingested_text": "", "attachments": [],
            "enable_thinking": _GEN["enable_thinking"],
            "thinking_budget": _GEN["thinking_budget"],
            "max_tokens": _GEN["max_tokens"],
            "temperature": _GEN["temperature"],
            "min_p": _GEN["min_p"],
            "response": list(messages),
            "scheduled_run": True,  # 前端可据此标记「定时任务轮」
            **(metrics or {}),
        }
