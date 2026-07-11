"""
任务级调度器（单 worker 模型）。

硬件前提：Apple Silicon 统一内存「高带宽、弱算力」，LLM decode 阶段受内存带宽约束，
并行生成只会互相抢带宽、各自多占一份 KV 显存 —— 串行才是吞吐最优。因此整个进程
同一时刻只让「一个任务」占用推理 worker，其余任务排队或挂起，像单核 CPU 的协作式多任务。

与底层生成锁的分工：
- AgentEngine._inference_lock 是「生成级」correctness 锁（MLX detokenizer/criteria 是
  共享可变单例，单次生成必须原子），它只裹一次 generate_once；
- 本调度器是「任务级」策略层：一个任务一旦被准入即独占 worker，跨其多步生成持续持有，
  直到任务完成或被挂起，避免多任务在生成粒度上无序 ping-pong。二者叠加，各司其职。

可扩展点（为后续「自动调度」预留，无需改动骨架）：
- SchedulingPolicy —— 决定「worker 空出来跑谁」(pick_next) 与「新任务是否抢占在跑的」
  (should_preempt)。默认 FifoPolicy：先到先跑、不自动抢占（挂起仅由外部接口手动触发）。
  将来实现优先级/时限/依赖调度，只需替换该策略对象。
- 句柄状态表对外只读快照（snapshot / active_session_ids），自动调度器据此决策。

挂起态（suspend）语义（区别于 cancel）：
- cancel = 丢弃 + 整轮回滚（已有能力，见 routes/engine 的 cancel_requested）；
- suspend = 半路冻结、**保留** KV(留内存) + task_state + 局部产出，让出 worker，可恢复。
  运行中的任务在 token 边界检查 handle.suspend_requested，命中即干净让出。
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# 任务在调度器视角下的生命周期状态
QUEUED = "queued"        # 已登记、等待被准入
RUNNING = "running"      # 正占用 worker 推理
SUSPENDED = "suspended"  # 半路让出、KV 留内存、可恢复
IDLE = "idle"            # 未登记（无活跃任务）


@dataclass
class TaskHandle:
    """一个可调度任务的登记句柄（一个会话同一时刻至多一个活跃句柄）。"""
    session_id: str
    status: str = QUEUED
    kind: str = "chat"                 # chat | approval | resume —— 供策略区分任务来源
    priority: int = 0                  # 预留：自动调度按优先级选择（默认 0）
    enqueued_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    # 抢占请求标志：外部接口或调度策略置位，编排循环在 token 边界检查后挂起让出
    suspend_requested: bool = False
    # 自动抢占已发出标志：防止每次 dispatch 重复触发 cancel/suspend 回调（幂等但吵）
    preempt_requested: bool = False
    # 准入信号：被调度选中并置为 running 后触发，acquire/resume 的等待方随之被唤醒
    _admit: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def public(self) -> dict:
        """对外只读快照（供 /tasks 接口与自动调度策略读取，不含内部信号量）。"""
        now = time.time()
        return {
            "session_id": self.session_id,
            "status": self.status,
            "kind": self.kind,
            "priority": self.priority,
            "waiting_seconds": round(now - self.enqueued_at, 1) if self.status == QUEUED else 0.0,
            "running_seconds": round(now - self.started_at, 1) if (self.status == RUNNING and self.started_at) else 0.0,
        }


class SchedulingPolicy:
    """
    调度策略基类（自动调度的扩展点）。默认 FIFO、不抢占。
    子类可覆写 pick_next / should_preempt 实现优先级、时限、依赖等自动调度逻辑，
    调度器骨架、状态表、准入机制均不需改动。
    """

    def pick_next(self, waiters: List[TaskHandle]) -> Optional[TaskHandle]:
        """
        worker 空闲时从排队者中选出下一个跑谁：优先级高者先（默认 0），同优先级按 FIFO。
        低优先级任务（如后台摘要压缩 priority=-1）只在没有正常任务排队时才被选中，
        从而"给活跃任务让路"。
        """
        if not waiters:
            return None
        return min(waiters, key=lambda h: (-h.priority, h.enqueued_at))

    def should_preempt(self, running: Optional[TaskHandle],
                       waiters: List[TaskHandle]) -> Optional[TaskHandle]:
        """
        有任务排队而 worker 正忙时，判断是否抢占在跑的任务。
        返回「应当抢占上位的排队者」则触发挂起在跑任务；返回 None 表示不抢占。
        默认不自动抢占（v1 仅支持外部手动挂起）。
        """
        return None


# 默认策略即 FIFO 语义
FifoPolicy = SchedulingPolicy


# 后台任务类别：可被交互任务抢占（原语=cancel，见 TaskScheduler._dispatch_locked）。
# 摘要是单次生成、可安全放弃：取消后水位线不推进，下次触发自动重试，无半途状态。
BACKGROUND_KINDS = {"summarize"}

# 可挂起的后台类别：同样给交互任务让位，但原语=suspend（agentic 多步运行有半途状态，
# 放弃重跑代价大；挂起后由其执行器经 resume 等 worker 空闲续跑，见 schedule/runner.py）。
SUSPENDABLE_BACKGROUND = {"scheduled"}


class ClassPolicy(SchedulingPolicy):
    """
    确定性两级调度（无启发式，行为完全可预测）：
    - 静态类别优先级：交互任务（chat/approval/resume，priority=0）> 后台任务（summarize，
      priority=-1）——由 pick_next 的优先级排序天然实现；
    - 抢占：交互任务排队而 worker 被后台摘要占用时，取消该摘要让位（绝不抢占用户自己的任务）；
    - 老化防饿死：排队超过 aging_seconds 的任务有效优先级抬一档，老化后的摘要与新交互同级、
      按 FIFO（更早入队）胜出，交互流持续不断时摘要仍能得到执行机会。
    """

    def __init__(self, aging_seconds: float = 300.0, preempt_background: bool = True):
        self.aging_seconds = float(aging_seconds)
        self.preempt_background = bool(preempt_background)

    def _effective_priority(self, h: TaskHandle, now: float) -> int:
        aged = self.aging_seconds > 0 and (now - h.enqueued_at) > self.aging_seconds
        return h.priority + (1 if aged else 0)

    def pick_next(self, waiters: List[TaskHandle]) -> Optional[TaskHandle]:
        if not waiters:
            return None
        now = time.time()
        return min(waiters, key=lambda h: (-self._effective_priority(h, now), h.enqueued_at))

    def should_preempt(self, running: Optional[TaskHandle],
                       waiters: List[TaskHandle]) -> Optional[TaskHandle]:
        if not self.preempt_background or running is None:
            return None
        if running.kind not in BACKGROUND_KINDS | SUSPENDABLE_BACKGROUND:
            return None  # 只抢占后台任务，用户自己的任务永不被自动打断
        cands = [h for h in waiters if h.priority > running.priority]
        if not cands:
            return None
        return min(cands, key=lambda h: h.enqueued_at)


class TaskScheduler:
    """全局单 worker 任务调度器。所有对状态表的读写都在短临界区内经 self._lock 保护。"""

    def __init__(self, policy: Optional[SchedulingPolicy] = None,
                 on_suspend_request: Optional[Callable[[str], None]] = None,
                 on_cancel_request: Optional[Callable[[str], None]] = None,
                 on_queue_positions: Optional[Callable[[Dict[str, int]], None]] = None):
        self.policy = policy or FifoPolicy()
        # 排队位次广播(Phase 3):每次调度后,位次有变化的 QUEUED 会话经此回调上报
        # {session_id: position}(0=下一个被准入)。app 层接到后写入各会话账本
        # (queue_moved 事件),前端把"黑盒排队"变成可见位次。
        self._on_queue_positions = on_queue_positions
        self._last_positions: Dict[str, int] = {}
        # 挂起请求桥接：手动接口与自动抢占策略统一经此把挂起意图传播到会话
        # （置位 session.suspend_requested），编排循环在迭代边界读取单一来源即可。
        self._on_suspend_request = on_suspend_request
        # 取消请求桥接：后台任务（BACKGROUND_KINDS）被自动抢占时走 cancel 原语
        # （置位 session.cancel_requested，摘要生成逐 token 段响应；执行侧 finally 复位）。
        self._on_cancel_request = on_cancel_request
        self._handles: Dict[str, TaskHandle] = {}   # session_id -> 活跃句柄
        self._running: Optional[str] = None          # 当前占用 worker 的 session_id
        self._lock = asyncio.Lock()

    def set_policy(self, policy: SchedulingPolicy) -> None:
        """热替换调度策略（后续启用自动调度时用）。"""
        self.policy = policy

    # ---------- 准入 / 释放（任务级 worker 门闸） ----------
    async def acquire(self, session_id: str, *, kind: str = "chat", priority: int = 0) -> TaskHandle:
        """
        登记为排队任务并等待被准入。被调度选中后返回其句柄（status=running）。
        调用方在 run_turn 全程持有该准入，结束时须 release（或挂起时经 suspend 让出）。
        """
        async with self._lock:
            handle = TaskHandle(session_id=session_id, kind=kind, priority=priority, status=QUEUED)
            self._handles[session_id] = handle
            self._dispatch_locked()
        try:
            await handle._admit.wait()
        except asyncio.CancelledError:
            self._cleanup_cancelled_waiter(session_id, handle)
            raise
        return handle

    async def release(self, session_id: str) -> None:
        """任务正常结束：移除句柄、腾出 worker 并调度下一个。挂起态句柄不受影响（保留可恢复）。"""
        async with self._lock:
            handle = self._handles.get(session_id)
            if handle is not None and handle.status == RUNNING:
                self._handles.pop(session_id, None)
            if self._running == session_id:
                self._running = None
            self._dispatch_locked()

    # ---------- 挂起 / 恢复（KV 留内存，可轮换） ----------
    def request_suspend(self, session_id: str) -> bool:
        """
        请求挂起在跑的任务（外部接口 / 自动策略调用，不阻塞、不需持锁语义）：
        置位句柄的 suspend_requested，编排循环在 token 边界检查后干净让出。
        返回是否成功置位（仅对 running 任务有效）。
        """
        handle = self._handles.get(session_id)
        if handle is not None and handle.status == RUNNING:
            handle.suspend_requested = True
            if self._on_suspend_request is not None:
                self._on_suspend_request(session_id)  # 传播到 session.suspend_requested
            return True
        return False

    async def suspend(self, session_id: str) -> None:
        """
        运行中任务已在 token 边界响应挂起：转入 suspended、让出 worker、调度下一个。
        句柄保留在表中（KV 与 task_state 留在内存），供后续 resume。
        """
        async with self._lock:
            handle = self._handles.get(session_id)
            if handle is not None:
                handle.status = SUSPENDED
                handle.suspend_requested = False
                handle.preempt_requested = False
                handle.started_at = None
            if self._running == session_id:
                self._running = None
            self._dispatch_locked()

    async def resume(self, session_id: str, *, priority: int = 0) -> Optional[TaskHandle]:
        """
        恢复一个挂起任务：重新排队并等待被准入。无挂起句柄则返回 None。
        """
        async with self._lock:
            handle = self._handles.get(session_id)
            if handle is None or handle.status != SUSPENDED:
                return None
            handle.status = QUEUED
            handle.priority = priority
            handle.enqueued_at = time.time()
            handle.suspend_requested = False
            handle.preempt_requested = False
            handle._admit = asyncio.Event()
            self._dispatch_locked()
        try:
            await handle._admit.wait()
        except asyncio.CancelledError:
            self._cleanup_cancelled_waiter(session_id, handle)
            raise
        return handle

    def _cleanup_cancelled_waiter(self, session_id: str, handle: TaskHandle) -> None:
        """
        排队等待被任务取消（如客户端断连令 FastAPI 取消 handler）时的善后：
        不清理的话句柄以 QUEUED 残留，之后被调度准入成「无人在跑的幽灵任务」，
        _running 被它永久占住 → 全局单 worker 死锁。
        同步执行、无 await（asyncio 单线程内不可能被打断），故无需再取 self._lock——
        取消路径上再去 await 锁反而可能被二次取消打断、留下更糟的半清理状态。
        """
        if self._handles.get(session_id) is not handle:
            return  # 句柄已被他人替换/清理（如 discard），不越权动
        self._handles.pop(session_id, None)
        # 极端竞态：清理前一刻恰好被准入（_admit 已 set、_running 已指向它）→ 按 release 语义腾出
        if self._running == session_id:
            self._running = None
        self._dispatch_locked()

    def discard(self, session_id: str) -> None:
        """
        丢弃某会话的调度句柄（会话删除 / 任务作废时清理，防止状态表泄漏）。
        若正占用 worker 也一并腾出。不触发调度（由调用侧的正常路径善后）。
        """
        handle = self._handles.pop(session_id, None)
        if handle is not None and self._running == session_id:
            self._running = None

    # ---------- 内部调度核心（持锁调用） ----------
    def _dispatch_locked(self) -> None:
        """在持有 self._lock 时被调用：worker 空则准入下一个；worker 忙则给策略一次抢占机会。"""
        if self._running is not None:
            waiters = [h for h in self._handles.values() if h.status == QUEUED]
            if waiters:
                running = self._handles.get(self._running)
                claimant = self.policy.should_preempt(running, waiters)
                if claimant is not None and running is not None and not running.preempt_requested:
                    running.preempt_requested = True  # 只触发一次，后续 dispatch 不再重复回调
                    if running.kind in BACKGROUND_KINDS:
                        # 后台任务用 cancel 原语让位：摘要无挂起检查点、半份摘要无恢复价值，
                        # 取消后水位线不推进、下次触发自动重试
                        if self._on_cancel_request is not None:
                            self._on_cancel_request(running.session_id)
                    else:
                        running.suspend_requested = True  # 由编排循环在迭代边界响应
                        if self._on_suspend_request is not None:
                            self._on_suspend_request(running.session_id)
            self._broadcast_positions()  # worker 忙:新排队者也要知道自己的位次
            return

        waiters = [h for h in self._handles.values() if h.status == QUEUED]
        nxt = self.policy.pick_next(waiters)
        if nxt is None:
            self._broadcast_positions()
            return
        nxt.status = RUNNING
        nxt.started_at = time.time()
        self._running = nxt.session_id
        nxt._admit.set()
        self._broadcast_positions()

    def _broadcast_positions(self) -> None:
        """计算 QUEUED 句柄的当前位次并上报变化(与默认策略同序:优先级降序、先来先到)。"""
        if self._on_queue_positions is None:
            return
        waiters = sorted(
            (h for h in self._handles.values() if h.status == QUEUED),
            key=lambda h: (-h.priority, h.enqueued_at),
        )
        now = {h.session_id: i for i, h in enumerate(waiters)}
        changed = {sid: pos for sid, pos in now.items()
                   if self._last_positions.get(sid) != pos}
        self._last_positions = now
        if changed:
            try:
                self._on_queue_positions(changed)
            except Exception as e:
                print(f"[Scheduler] 位次广播失败(忽略): {e}")

    # ---------- 只读观测（/tasks 接口、驱逐器守卫、自动调度决策） ----------
    def status_of(self, session_id: str) -> str:
        handle = self._handles.get(session_id)
        return handle.status if handle is not None else IDLE

    def is_active(self, session_id: str) -> bool:
        """该会话是否处于调度活跃态（排队/运行/挂起）——驱逐器据此避免冻结掉其内存 KV。"""
        return session_id in self._handles

    def active_session_ids(self) -> set:
        return set(self._handles.keys())

    def snapshot(self) -> dict:
        """全局任务视图（供 /tasks 接口与前端任务切换器）。"""
        handles = [h.public() for h in self._handles.values()]
        return {
            "running": self._running,
            "queued": sum(1 for h in self._handles.values() if h.status == QUEUED),
            "suspended": sum(1 for h in self._handles.values() if h.status == SUSPENDED),
            "tasks": handles,
        }
