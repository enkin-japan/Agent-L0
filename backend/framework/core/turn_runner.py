"""TurnRunner —— 轮次生命周期的唯一属主(REARCHITECTURE.md §2.2)。

收敛自 routes.py 曾经的三份近拷贝(_chat_turn / _resume_approval_turn / resume_task):
前置清理(放弃暂停态旧轮)→ 追加用户消息 → inflight 预写 → 账本 turn_queued →
调度门闸 → 编排闭环 → 终态分流(成功/取消/故障回滚/审批暂存/挂起暂存)→
落盘或暂存 → 摘要触发 → 账本生命周期事件。routes 退化为薄 HTTP 适配器。

不变量清单(改动本文件前先读,金测试 test_turn_runner.py 逐条守着):
- session.lock 覆盖从清理到落盘的完整临界区;受理(accept)与执行(run)分两次
  持锁,期间由账本 turn_state 闸门挡住并发受理(queued/running → 409 语义);
- scheduler.acquire/release/suspend 严格配对:suspended 走 suspend(句柄保留),
  其余一律 release;
- 取消/故障整轮回滚到 _rollback_idx(含用户消息),任务态同步回滚 _task_snapshot;
- 待审批/挂起的轮次**不落盘**,暂存 session.pending_turn / suspended_turn,
  决议/恢复后合并;落盘时剥掉全部 `_` 内部账目键;
- inflight 预写在受理时写入,append_log 自动撤销(崩溃恢复的 write-ahead 闭环);
- 生命周期事件(turn_queued/started/awaiting/suspended/resumed/ended)由本层发射,
  过程事件由编排层发射——账本必传,先记账后分发。
"""
import asyncio
from typing import Any, Dict, List, Optional

from .journal import new_turn_id
from .task_state import TaskState


class TurnConflict(Exception):
    """受理冲突:会话已有排队/运行中的轮次,或决议/恢复的目标态不存在。"""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def public_turn(response_data: Dict[str, Any]) -> Dict[str, Any]:
    """剥掉内部账目键(`_` 前缀)得到可下发的轮次数据。

    **递归**剥离(B2 搭车⑦):内部键不止长在顶层——嵌套 dict/list 里的 `_` 前缀键
    (如工具响应内的 _safe/_target/_compacted)原先原样下发给客户端。落盘侧的
    `_strip_internal_meta`(session.py)同一意图但只外科手术式清 tool_responses
    (要保内存原 dict 不动);本函数是下发投影,整棵新建,可以全剥。
    代价如实记:工具**参数**里若有正当的 `_` 前缀键(如 ES 的 _source),投影里
    也会被剥——只影响展示,不影响执行与落盘。
    """
    def _strip(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: _strip(x) for k, x in v.items() if not str(k).startswith("_")}
        if isinstance(v, list):
            return [_strip(x) for x in v]
        return v
    return _strip(response_data)


def _restore_task(session, snapshot):
    """按轮初快照回滚会话的任务状态(引擎故障/用户取消时与 history 回滚同步)。"""
    session.task_state = TaskState.from_snapshot(snapshot) if snapshot else None


class TurnRunner:
    def __init__(self, orchestrator, scheduler, summarize_after_turn=None):
        self.orchestrator = orchestrator
        self.scheduler = scheduler
        # 轮次落盘后的摘要压缩钩子(routes._maybe_summarize_active 注入;测试可传 None)
        self._summarize = summarize_after_turn
        self._bg: Dict[str, asyncio.Task] = {}   # sid -> 背景执行任务(调试观测用)

    # ---------- 背景执行 ----------
    def spawn(self, sid: str, coro) -> asyncio.Task:
        """把一次轮次执行放入背景(202 受理即返的路径)。异常一律消费并打印,
        故障语义已在执行体内部转为 fail 轮落盘,这里只兜底真正的 bug。"""
        task = asyncio.create_task(coro)
        self._bg[sid] = task

        def _done(t: asyncio.Task):
            if self._bg.get(sid) is t:
                self._bg.pop(sid, None)
            try:
                exc = t.exception()
                if exc is not None:
                    print(f"[TurnRunner] 背景轮次异常({sid}): {exc!r}")
            except asyncio.CancelledError:
                pass

        task.add_done_callback(_done)
        return task

    # ---------- 新轮次:受理 ----------
    async def accept_turn(self, session, sid: str, *, prompt: str,
                          images: List[str], audios: List[str],
                          ingested_text: str, attachments: List[Any],
                          gen: Dict[str, Any], origin: str = "chat",
                          voice_transcripts: Optional[List[Dict[str, Any]]] = None,
                          client_turn_id: Optional[str] = None
                          ) -> Dict[str, Any]:
        """
        受理一个新轮次(短临界区):放弃暂停态旧轮 → 提交用户消息 → inflight 预写 →
        账本 turn_queued。返回执行上下文 ctx(交给 run_accepted);受理冲突抛 TurnConflict。

        voice_transcripts(方案 B,来自 ingestion.ingest):非 native_audio 的 audio 分支
        转写清单。纯音频消息 prompt 为空时,把转写升格为本轮用户文本(见下),既有调用方
        不传即维持现状。
        """
        journal = session.journal
        # 锁外快路径:运行中的轮次持 session.lock 长达数分钟,受理冲突必须即时拒绝
        # 而不是排队等锁(否则 409 永远打不出来,请求全部串成隐形队列)。
        if journal.turn_state in ("queued", "running"):
            raise TurnConflict("turn_active")
        async with session.lock:
            # 锁内复检:两个受理竞争同一空闲会话时,后进者在此被拦住
            if journal.turn_state in ("queued", "running"):
                raise TurnConflict("turn_active")

            await self._abandon_paused_turn(session, sid)

            # 语音升格（方案 B）：纯音频消息客户端发空 prompt，把 ASR 转写升格为本轮
            # 用户文本——否则任务 goal / 会话搜索 / 斜杠分发全拿不到语音内容。落在斜杠
            # 分发**之前**：语音说「/剧本名 …」也能触发确定性分发。升格后把 ingested_text
            # 里对应音频的完整转写块降到仅 provenance 标记行（转写正文不在用户文本与摄入
            # 块里各存一份，省 8K 上下文预算）；用 router 交回的精确 block 串做定点替换,
            # 不解析 ingested_text 结构。空/全空白转写（静音）不升格,走现状。
            if not prompt.strip() and voice_transcripts:
                texts = [t for t in (str(vt.get("transcript") or "").strip()
                                     for vt in voice_transcripts) if t]
                if texts:
                    prompt = "\n".join(texts)
                    for vt in voice_transcripts:
                        block, marker = vt.get("block"), vt.get("marker_line")
                        if block and marker and block in ingested_text:
                            ingested_text = ingested_text.replace(block, marker, 1)

            # 斜杠点名剧本 → 确定性注入（D1/D.17：点名是机械可判的，不靠模型悟）。
            # 点名的是被舍弃剧本时先热重扫再分发：依赖可能装好了没生效（LibreOffice
            # 时序案）——重扫是稀有慢路径（几秒探活），只在这一分支走 to_thread。
            from ..agents import runtime as _runtime
            from .skill_registry import dispatch_slash_skill, slash_name
            skills = _runtime.get_skill_registry()
            named = slash_name(prompt)
            if skills is not None and named and skills.unavailable_reason(named):
                await asyncio.to_thread(_runtime.rescan_tools)
                skills = _runtime.get_skill_registry()  # 重扫换了新表
            prompt, ingested_text = dispatch_slash_skill(prompt, ingested_text, skills)

            # 将新解析的资源加入本会话的生命周期
            session.images.extend(images)
            session.audios.extend(audios)

            # 本轮起点快照:引擎故障/用户取消时,history 与任务状态同步整轮回滚
            session.cancel_requested = False
            rollback_idx = len(session.history)
            task_snapshot = session.task_state.snapshot() if session.task_state else None
            session.add_user_message(prompt, images, audios, extra_text=ingested_text)
            history_start_idx = len(session.history)
            # 轮次预写标记(write-ahead):进程死亡时启动恢复补 fail 轮,用户输入不蒸发。
            # turn_id 先于标记诞生并写入(B2):崩溃补发的 turn_ended 才有正主
            tid = new_turn_id()
            await asyncio.to_thread(session.write_inflight, prompt, gen, tid)

            env = journal.emit("turn_queued", {
                "prompt": prompt,
                "attachments": [
                    {"name": getattr(a, "name", None) or (a.get("name") if isinstance(a, dict) else str(a)),
                     "kind": (a.get("type") if isinstance(a, dict) else None) or "file"}
                    for a in (attachments or [])
                ],
                "gen": dict(gen), "origin": origin,
                # 提交幂等键(B3):随 turn_queued 落 journal,是重复提交判重的唯一持久真相
                # (inflight.json 在 append_log 后即撤,不是已完结轮次的 durable 记录)
                "client_turn_id": client_turn_id or None,
            }, turn_id=tid)

        return {
            "turn_id": env["turn_id"], "seq": env["seq"],
            "prompt": prompt, "images": images, "audios": audios,
            "ingested_text": ingested_text, "attachments": attachments,
            "gen": gen, "origin": origin,
            "_rollback_idx": rollback_idx, "_task_snapshot": task_snapshot,
            "_history_start_idx": history_start_idx,
        }

    # 判重回查的轮次配额:按 turn_queued **轮**计,不按里程碑计——一个工具密集轮
    # 上百条里程碑,按里程碑计窗口会把自己的 turn_queued 挤出去(首审 H2)
    _IDEM_TURN_WINDOW = 64

    def lookup_client_turn(self, session, client_turn_id: str) -> Optional[Dict[str, Any]]:
        """提交幂等键判重(B3)。命中返回 {turn_id, seq, duplicate: True},未命中 None。

        **必须在 `_prepare_turn` 之前调用**:摄入(落盘媒体/ASR/解析文档)发生在受理之前,
        判重写在受理内部的话,重复提交照样付一遍摄入成本并留一份孤儿媒体(OSS 扫描的
        审查修正点)。两级判定:
        1. 活跃轮(queued/running/awaiting/suspended)的键由 journal 状态机跟踪——原本
           这类重复会 409,带同键说明是同一次提交的重试,应回 202 引导客户端附着;
        2. 已完结轮:唯一持久真相是 journal 的 turn_queued 里程碑,从文件尾反向扫
           (core/jsonl.iter_records_reversed,B1 依赖),命中即停。窗口=近 64 **轮**
           (幂等键防分钟级手动重发,不做无限期查重;越界旧键当新提交,只增不害)。
        **fail/cancelled 的命中不算命中**(首审 H4):那一轮没有产出,B2 的恢复文案
        明说「可直接重发」——吞掉这次重发等于把收敛承诺变成谎话。反向扫先见
        turn_ended 后见 turn_queued,按 turn_id 记状态即可判。无 turn_ended 记录
        (补发失败等罕见形态)同样放行重执行:宁重复,不吞话。
        seq 返回该轮 turn_queued 的 seq(客户端拿它当 after 游标,与首次 202 同语义)。
        """
        if not client_turn_id:
            return None
        journal = session.journal
        from .journal import TURN_NONE
        if journal.turn_state != TURN_NONE and journal.turn_client_id == client_turn_id:
            return {"turn_id": journal.current_turn_id,
                    "seq": journal.turn_first_seq, "duplicate": True}
        from .jsonl import iter_records_reversed
        ended_status: Dict[Any, Any] = {}
        turns_seen = 0
        try:
            for rec in iter_records_reversed(journal.path):
                rtype = rec.get("type")
                if rtype == "turn_ended":
                    tid = rec.get("turn_id")
                    if tid is not None and tid not in ended_status:
                        ended_status[tid] = (rec.get("data") or {}).get("status")
                elif rtype == "turn_queued":
                    turns_seen += 1
                    if (rec.get("data") or {}).get("client_turn_id") == client_turn_id:
                        status = ended_status.get(rec.get("turn_id"))
                        if status in ("fail", "cancelled", None):
                            return None
                        return {"turn_id": rec.get("turn_id"),
                                "seq": rec.get("seq"), "duplicate": True}
                    if turns_seen >= self._IDEM_TURN_WINDOW:
                        return None
        except OSError:
            return None            # 账本读不动:当无记录,走正常受理(fail-open,不挡提交)
        return None

    async def _abandon_paused_turn(self, session, sid: str) -> None:
        """暂停态旧轮的放弃收尾(须持 session.lock 调用):
        待审批 → 工具作废 + warning 落盘;挂起 → warning 落盘 + 调度句柄回收。"""
        journal = session.journal
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
                    {"role": "tool", "tool_responses": cancelled})
                await asyncio.to_thread(session.append_log, session.pending_turn)
                if journal.turn_state != "none":
                    journal.emit("turn_ended", {
                        **public_turn(session.pending_turn),
                        "turn_index": session.disk_turn_count() - 1})
                session.pending_turn = None

        if session.suspended_turn is not None:
            susp = session.suspended_turn
            for k in ("_rollback_idx", "_task_snapshot", "_history_start_idx"):
                susp.pop(k, None)
            susp["status"] = "warning"
            susp.setdefault("response", []).append(
                {"role": "system",
                 "content": "The suspended task was interrupted by a new user message and abandoned (not continued)."})
            await asyncio.to_thread(session.append_log, susp)
            if journal.turn_state != "none":
                journal.emit("turn_ended", {
                    **public_turn(susp), "turn_index": session.disk_turn_count() - 1})
            session.suspended_turn = None
            session.suspend_requested = False
            session.task_state = None
            self.scheduler.discard(sid)

    # ---------- 新轮次:执行 ----------
    async def run_accepted(self, session, sid: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """执行一个已受理的轮次(完整闭环,持锁全程)。返回完整轮次数据(response 为本轮全量)。"""
        gen = ctx["gen"]
        journal = session.journal
        async with session.lock:
            final_result = None
            await self.scheduler.acquire(sid, kind="chat")
            journal.emit("turn_started", {})
            try:
                final_result = await self.orchestrator.run_turn(
                    session=session, journal=journal, **gen,
                )
                if final_result.get("status") == "cancelled":
                    # 用户取消:整轮(含用户消息与中途产物)回滚出内存,落盘保留取消存档
                    session.history = session.history[:ctx["_rollback_idx"]]
                    _restore_task(session, ctx["_task_snapshot"])
                    response_data = self._turn_record(ctx, "cancelled", [
                        {"role": "system", "content": "本轮已被用户取消,未产生任何记忆变更。"},
                    ], final_result.get("metrics"))
                else:
                    new_messages = session.history[ctx["_history_start_idx"]:]
                    response_data = self._turn_record(
                        ctx, final_result.get("status", "success"), new_messages,
                        final_result.get("metrics"))
                    response_data["pending_tools"] = final_result.get("pending_tools")
            except Exception as e:
                import traceback
                error_details = traceback.format_exc()
                # 引擎故障:连同本轮用户消息一起回滚内存记忆与任务状态,防止污染
                session.history = session.history[:ctx["_rollback_idx"]]
                _restore_task(session, ctx["_task_snapshot"])
                response_data = self._turn_record(ctx, "fail", [
                    {"role": "system", "content": f"核心引擎故障: {str(e)}\n{error_details}"},
                ])
            finally:
                if final_result is not None and final_result.get("status") == "suspended":
                    await self.scheduler.suspend(sid)
                else:
                    await self.scheduler.release(sid)

            await self._settle(session, sid, response_data,
                               rollback_idx=ctx["_rollback_idx"],
                               task_snapshot=ctx["_task_snapshot"],
                               history_start_idx=ctx["_history_start_idx"])
        return response_data

    def _turn_record(self, ctx: Dict[str, Any], status: str,
                     messages: List[Dict[str, Any]], metrics: Optional[Dict] = None
                     ) -> Dict[str, Any]:
        gen = ctx["gen"]
        return {
            "status": status,
            "prompt": ctx["prompt"],
            "image": ctx["images"], "audio": ctx["audios"],
            "ingested_text": ctx["ingested_text"], "attachments": ctx["attachments"],
            "enable_thinking": gen.get("enable_thinking", False),
            "thinking_budget": gen.get("thinking_budget", 2048),
            "max_tokens": gen.get("max_tokens", 4096),
            "temperature": gen.get("temperature", 0.2),
            "min_p": gen.get("min_p", 0.05),
            "response": list(messages),
            **(metrics or {}),
        }

    async def _settle(self, session, sid: str, response_data: Dict[str, Any], *,
                      rollback_idx: int, task_snapshot, history_start_idx: int) -> None:
        """终态分流(须持 session.lock):暂存(审批/挂起)或落盘,并发射生命周期事件。"""
        journal = session.journal
        status = response_data.get("status")
        if status == "pending_approval":
            response_data["_rollback_idx"] = rollback_idx      # 决议执行故障时整轮回滚
            response_data["_task_snapshot"] = task_snapshot    # 任务状态同步回滚
            session.pending_turn = response_data
            journal.emit("awaiting_approval",
                         {"pending_tools": response_data.get("pending_tools") or []})
        elif status == "suspended":
            response_data["_rollback_idx"] = rollback_idx      # 恢复续跑故障时整轮回滚
            response_data["_task_snapshot"] = task_snapshot
            response_data["_history_start_idx"] = history_start_idx  # 恢复时接续截取增量
            session.suspended_turn = response_data
            journal.emit("suspended", {})
        else:
            for k in ("_rollback_idx", "_task_snapshot", "_history_start_idx"):
                response_data.pop(k, None)
            await asyncio.to_thread(session.append_log, response_data)
            await asyncio.to_thread(session.save_task)
            journal.emit("turn_ended", {
                **public_turn(response_data),
                "turn_index": session.disk_turn_count() - 1})
            if self._summarize is not None:
                await self._summarize(session, sid)

    # ---------- 审批决议 ----------
    async def accept_approval(self, session, sid: str, approve: bool, *,
                              expected_turn_id: str = None) -> str:
        """受理审批决议(短临界区):校验目标态 → 账本 resumed。返回 turn_id。
        expected_turn_id:远程决议(推送通知按钮)的防错批锚——迟到点击时当前等待
        审批的可能已是另一个轮次,不匹配即拒,绝不把决议错递给后来者。"""
        journal = session.journal
        if journal.turn_state != "awaiting_approval":  # 锁外快路径(运行中不排队等锁)
            raise TurnConflict("no_pending_approval")
        async with session.lock:
            if journal.turn_state != "awaiting_approval" \
                    or not session.pending_tool_calls or session.pending_turn is None:
                raise TurnConflict("no_pending_approval")
            if expected_turn_id and journal.current_turn_id != expected_turn_id:
                raise TurnConflict("approval_target_mismatch")
            journal.emit("resumed", {"trigger": "approve" if approve else "deny"})
            return journal.current_turn_id

    async def run_approval(self, session, sid: str, approve: bool) -> Dict[str, Any]:
        """执行审批决议后的续跑闭环。返回轮次数据(response 仅含本次决议后新增的消息)。"""
        journal = session.journal
        async with session.lock:
            turn = session.pending_turn
            if turn is None or not session.pending_tool_calls:
                # 受理与执行之间被抢先处理(会话删除等边界):按无事可做收场
                return self._stale_target("当前会话没有等待审批的操作(可能已过期或已被处理)。")

            session.cancel_requested = False
            history_start_idx = len(session.history)
            final_result = None
            await self.scheduler.acquire(sid, kind="approval")
            journal.emit("turn_started", {})
            try:
                final_result = await self.orchestrator.resume_after_approval(
                    session, approved=approve, journal=journal,
                    temperature=turn.get("temperature", 0.2),
                    min_p=turn.get("min_p", 0.05),
                    enable_thinking=turn.get("enable_thinking", False),
                    thinking_budget=turn.get("thinking_budget", 2048),
                    max_tokens=turn.get("max_tokens", 4096),
                )
                response_data = self._merge_continuation(session, turn, final_result,
                                                         history_start_idx)
            except Exception as e:
                response_data = self._continuation_failure(session, turn, e, "审批决议执行故障")
            finally:
                if final_result is not None and final_result.get("status") == "suspended":
                    await self.scheduler.suspend(sid)
                else:
                    await self.scheduler.release(sid)

            # 状态分流:又现敏感调用→继续审批暂存;被挂起→转挂起暂存;否则整轮合并落盘
            status = response_data.get("status")
            if status == "pending_approval":
                session.pending_turn = turn
                journal.emit("awaiting_approval",
                             {"pending_tools": turn.get("pending_tools") or []})
            elif status == "suspended":
                session.pending_turn = None
                session.suspended_turn = turn  # 携原账目(_rollback_idx/_task_snapshot)转挂起
                journal.emit("suspended", {})
            else:
                session.pending_turn = None
                await self._land_continuation(session, sid, turn)
        return response_data

    # ---------- 挂起恢复 ----------
    async def accept_resume(self, session, sid: str) -> str:
        """受理恢复请求(短临界区):校验目标态 → 账本 resumed。返回 turn_id。"""
        journal = session.journal
        if journal.turn_state != "suspended":          # 锁外快路径(运行中不排队等锁)
            raise TurnConflict("no_suspended_turn")
        async with session.lock:
            if journal.turn_state != "suspended" or session.suspended_turn is None:
                raise TurnConflict("no_suspended_turn")
            journal.emit("resumed", {"trigger": "resume"})
            return journal.current_turn_id

    async def run_resume(self, session, sid: str) -> Dict[str, Any]:
        """执行挂起恢复的续跑闭环。返回轮次数据(response 仅含本次续跑新增的消息)。"""
        journal = session.journal
        async with session.lock:
            turn = session.suspended_turn
            if turn is None:
                return self._stale_target("当前会话没有挂起的任务(可能已恢复、已完结或会话被回收)。")

            session.cancel_requested = False
            session.suspend_requested = False
            history_start_idx = len(session.history)
            final_result = None
            # 恢复:挂起句柄重新排队等待准入(无句柄则退化为一次新准入,兜底进程重启等情形)
            if await self.scheduler.resume(sid) is None:
                await self.scheduler.acquire(sid, kind="resume")
            journal.emit("turn_started", {})
            try:
                final_result = await self.orchestrator.run_turn(
                    session=session, journal=journal,
                    compact_history=False,  # 轮中重入:本轮未落盘观察不参与压缩(防压缩桩落盘)
                    temperature=turn.get("temperature", 0.2),
                    min_p=turn.get("min_p", 0.05),
                    enable_thinking=turn.get("enable_thinking", False),
                    thinking_budget=turn.get("thinking_budget", 2048),
                    max_tokens=turn.get("max_tokens", 4096),
                )
                response_data = self._merge_continuation(session, turn, final_result,
                                                         history_start_idx)
            except Exception as e:
                response_data = self._continuation_failure(session, turn, e, "任务恢复续跑故障")
            finally:
                if final_result is not None and final_result.get("status") == "suspended":
                    await self.scheduler.suspend(sid)
                else:
                    await self.scheduler.release(sid)

            # 续跑后状态分流:又挂起→继续暂存;又触发审批→转审批暂存;否则整轮合并落盘
            status = response_data.get("status")
            if status == "suspended":
                session.suspended_turn = turn  # turn 已累计本次增量,继续挂起
                journal.emit("suspended", {})
            elif status == "pending_approval":
                session.suspended_turn = None
                session.pending_turn = turn    # 携原账目转审批
                journal.emit("awaiting_approval",
                             {"pending_tools": turn.get("pending_tools") or []})
            else:
                session.suspended_turn = None
                await self._land_continuation(session, sid, turn)
        return response_data

    # ---------- 续跑共用收尾 ----------
    def _merge_continuation(self, session, turn: Dict[str, Any],
                            final_result: Dict[str, Any], history_start_idx: int
                            ) -> Dict[str, Any]:
        """把续跑产物合并进暂存轮次:取消→整轮回滚;否则增量并入(响应只带增量)。"""
        if final_result.get("status") == "cancelled":
            rollback_idx = turn.get("_rollback_idx")
            if isinstance(rollback_idx, int) and 0 <= rollback_idx <= len(session.history):
                session.history = session.history[:rollback_idx]
            _restore_task(session, turn.get("_task_snapshot"))
            note = {"role": "system", "content": "本轮已被用户取消,未产生任何记忆变更。"}
            turn["status"] = "cancelled"
            turn["response"].append(note)
            turn["pending_tools"] = None
            return {**turn, "response": [note]}
        new_messages = session.history[history_start_idx:]
        turn["response"].extend(new_messages)      # 累计到完整轮次(落盘用)
        turn["status"] = final_result.get("status", "success")
        turn["pending_tools"] = final_result.get("pending_tools")
        turn.update(final_result.get("metrics", {}))
        return {**turn, "response": new_messages}  # 响应仅带本次增量

    def _continuation_failure(self, session, turn: Dict[str, Any], exc: Exception,
                              label: str) -> Dict[str, Any]:
        """续跑故障:整轮(含最初的用户消息)回滚出内存记忆与任务状态,落盘为 fail。"""
        import traceback
        error_details = traceback.format_exc()
        rollback_idx = turn.get("_rollback_idx")
        if isinstance(rollback_idx, int) and 0 <= rollback_idx <= len(session.history):
            session.history = session.history[:rollback_idx]
        _restore_task(session, turn.get("_task_snapshot"))
        error_message = {"role": "system", "content": f"{label}: {exc}\n{error_details}"}
        turn["status"] = "fail"
        turn["response"].append(error_message)
        turn["pending_tools"] = None
        return {**turn, "response": [error_message]}

    async def _land_continuation(self, session, sid: str, turn: Dict[str, Any]) -> None:
        """续跑终态落盘(须持 session.lock):剥内部键 → append_log → 账本 turn_ended → 摘要。"""
        journal = session.journal
        for k in ("_rollback_idx", "_task_snapshot", "_history_start_idx"):
            turn.pop(k, None)
        await asyncio.to_thread(session.append_log, turn)
        await asyncio.to_thread(session.save_task)
        journal.emit("turn_ended", {
            **public_turn(turn), "turn_index": session.disk_turn_count() - 1})
        if self._summarize is not None:
            await self._summarize(session, sid)

    @staticmethod
    def _stale_target(msg: str) -> Dict[str, Any]:
        """受理后目标态消失(会话删除等边界)的兜底响应(与旧端点的 fail 形状一致)。"""
        return {
            "status": "fail",
            "prompt": "", "image": [], "audio": [],
            "enable_thinking": False, "max_tokens": 0, "temperature": 0.0, "min_p": 0.0,
            "response": [{"role": "system", "content": msg}],
        }
