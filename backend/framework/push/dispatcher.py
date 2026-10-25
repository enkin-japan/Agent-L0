"""通知分发器：账本 → APNs 的映射层（CHARTER 铁律 2 里预留的"通知读者"）。

两条读者路径，无旁路：
  ① 全局 feed 订阅 → 离散横幅（结果 / 定时任务开始 / 鉴权要求 / 无人值守自动拒绝通报）
     + 定时任务的 Live Activity push-to-start；
  ② 会话 journal 订阅（仅在 app 上报活动 token 后附着）→ Live Activity 阶段更新
     （节流 + 滚动 stale-date 心跳 + turn_ended 收尾）。

审批语义按 origin 分叉：交互轮的 awaiting_approval 是**可互动**的（category=APPROVAL，
带允许/拒绝按钮、time-sensitive、TTL 过期不投）；定时轮的 awaiting_approval 紧跟
自动拒绝（runner 无人值守语义），推的是**通报**（"已按策略自动拒绝"）——把它推成
互动审批卡是撒谎。远程限时审批是刻意搁置的完整版（见 DEFERRED.md）。

内容分级（per-device prefs.content_level，隐私缓解的落点）：
  full        标题=会话名，正文含 prompt 头 / 工具名；
  category    标题=会话名，正文只说类别（默认）；
  placeholder 统一"Agent 有新事件"，零内容出门。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from framework.utils.instance_id import instance_id

# 灵动岛/锁屏上的常用工具短标签（岛上空间极小；未收录的用原名）
_TOOL_LABELS = {
    "web_research": "联网调查", "math_research": "数学推演", "excel_research": "表格工程",
    "render_chart": "绘图", "web_search": "搜索", "file_extract": "读文件",
    "write_file": "写文件", "run_bash": "执行命令", "rag_search": "查记忆",
    "deck_export_pdf": "导出 PDF", "update_plan": "订计划", "complete_step": "步骤完成",
}

_STEP_STATE = {"pending": "pending", "doing": "doing", "done": "done", "failed": "failed"}


def _tool_label(name: str) -> str:
    return _TOOL_LABELS.get(name, name)


class _ActivityState:
    """一个活跃轮次的 Live Activity 簿记：多设备 token + 内容态 + 节流/心跳任务。"""

    def __init__(self, session_id: str, turn_id: str):
        self.session_id = session_id
        self.turn_id = turn_id
        self.tokens: Dict[str, str] = {}          # device_id -> activity push token
        self.content: Dict[str, Any] = {
            "stage": "运行中", "status": "running", "done": 0, "total": 0,
            "steps": [], "detail": "",
        }
        self.plan: List[Dict[str, Any]] = []       # 最近一次 plan_updated 的完整计划
        self.last_pushed: str = ""                 # 上次成功推送的内容签名（变更检测）
        self.last_push_ts: float = 0.0
        self.reader_task: Optional[asyncio.Task] = None
        self.flush_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.ended = False


class NotificationDispatcher:
    def __init__(self, feed, registry, apns, session_manager, cfg: Dict[str, Any],
                 sessions_dir: Path):
        self.feed = feed
        self.registry = registry
        self.apns = apns                     # None = iOS 推送禁用（缺 .p8），完全静默
        self.session_manager = session_manager
        self.sessions_dir = Path(sessions_dir)
        cfg = cfg or {}
        self.bundle_id = str(cfg.get("bundle_id", "dev.enkin.macagent"))
        # 多 Mac 的 iPhone 据此认来源（见 framework/utils/instance_id.py）；None = 不带
        self.instance_id = instance_id()
        self.approval_ttl = int(cfg.get("approval_ttl_seconds", 900))
        la = cfg.get("live_activity") or {}
        self.la_enabled = bool(la.get("enabled", True))
        self.la_heartbeat = float(la.get("heartbeat_seconds", 60))
        self.la_stale = int(la.get("stale_seconds", 150))
        self.la_min_interval = float(la.get("min_push_interval", 2.0))

        # turn_id -> {origin, prompt, session_id}（feed 是瘦身流，turn_started 不带
        # origin——从 turn_queued 记下，容量护栏防泄漏）
        self._turns: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._activities: Dict[str, _ActivityState] = {}   # session_id -> state
        self._task: Optional[asyncio.Task] = None
        self._queue: Optional[asyncio.Queue] = None

    # ------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._task is None:
            # 订阅必须在 start() 内同步完成：create_task 的首次运行在若干个循环步之后，
            # 期间 publish 的事件对晚订阅者不可见（实测竞态，测试守着）
            self._queue = self.feed.subscribe()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for st in list(self._activities.values()):
            self._teardown_activity(st)
        self._activities.clear()
        if self.apns is not None:
            await self.apns.aclose()

    @property
    def enabled(self) -> bool:
        return self.apns is not None

    # ------------------------------------------------ feed 读者（横幅 + push-to-start）
    async def _run(self) -> None:
        q = self._queue
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                try:
                    await self._route(item)
                except Exception as e:
                    print(f"[Push] feed 事件处理异常（忽略继续）: {e}")
        finally:
            self.feed.unsubscribe(q)

    def _remember_turn(self, turn_id: str, meta: Dict[str, Any]) -> None:
        self._turns[turn_id] = meta
        while len(self._turns) > 128:
            self._turns.popitem(last=False)

    async def _route(self, item: Dict[str, Any]) -> None:
        etype = item.get("type")
        sid = item.get("session_id") or ""
        turn_id = item.get("turn_id") or ""
        data = item.get("data") or {}

        if etype == "turn_queued":
            self._remember_turn(turn_id, {
                "origin": data.get("origin", "chat"),
                "prompt": str(data.get("prompt", "")),
                "session_id": sid,
            })
            return

        meta = self._turns.get(turn_id) or {}
        origin = meta.get("origin", "chat")

        if etype == "turn_started":
            if origin == "schedule":
                await self._alert("schedule_start", sid, turn_id, meta)
                await self._push_to_start(sid, turn_id, meta)
            return

        if etype == "awaiting_approval":
            tools = [t.get("name", "?") for t in (data.get("pending_tools") or [])]
            if origin == "schedule":
                # runner 即将自动拒绝（无人值守语义）——推通报，不推互动审批卡
                await self._alert("approval_autodenied", sid, turn_id, {**meta, "tools": tools})
            else:
                await self._alert("approval", sid, turn_id, {**meta, "tools": tools})
            return

        if etype == "turn_ended":
            status = str(data.get("status", ""))
            # cancelled 是用户自己按的，不用告诉他自己
            if status and status != "cancelled":
                await self._alert("result", sid, turn_id, {**meta, "status": status})
            self._turns.pop(turn_id, None)
            return

    # ------------------------------------------------ 横幅推送
    async def _alert(self, kind: str, sid: str, turn_id: str, ctx: Dict[str, Any]) -> None:
        if self.apns is None:
            return
        pref_key = {"result": "results", "approval": "approval",
                    "approval_autodenied": "approval", "schedule_start": "schedule_start"}[kind]
        devices = self.registry.with_pref(pref_key)
        if not devices:
            return
        title_hint = self._session_title(sid, ctx.get("prompt", ""))
        now = int(time.time())
        for d in devices:
            level = ((d.get("prefs") or {}).get("content_level")) or "category"
            title, body = self._content(kind, level, title_hint, ctx)
            aps: Dict[str, Any] = {
                "alert": {"title": title, "body": body},
                "sound": "default",
                "thread-id": sid,
            }
            expiration = now + 1800
            if kind == "approval":
                aps["category"] = "APPROVAL"
                aps["interruption-level"] = "time-sensitive"
                expiration = now + self.approval_ttl
            ma = {"v": 1, "kind": kind, "session_id": sid, "turn_id": turn_id}
            if self.instance_id:
                ma["src"] = self.instance_id
            payload = {"aps": aps, "ma": ma}
            res = await self.apns.send(
                d["token"], payload, topic=self.bundle_id, push_type="alert",
                priority=10, expiration=expiration,
                collapse_id=f"{turn_id}:{kind}",
                environment=d.get("env", "auto"))
            self._post_send(d, res)

    def _post_send(self, device: Dict[str, Any], res: Dict[str, Any]) -> None:
        """发送后账务：auto 环境粘滞学习；410 设备失效清理。"""
        if res.get("ok") and device.get("env") == "auto":
            self.registry.set_env(device["device_id"], res.get("environment", "prod"))
        elif res.get("status") == 410:
            self.registry.remove(device["device_id"])
            print(f"[Push] 设备 token 已失效，移除 {device['device_id']}")
        elif not res.get("ok"):
            print(f"[Push] 推送失败 device={device['device_id']} "
                  f"status={res.get('status')} reason={res.get('reason')}")

    def _content(self, kind: str, level: str, title_hint: str,
                 ctx: Dict[str, Any]) -> tuple:
        """按内容分级组装 (title, body)。placeholder 档零内容出门。"""
        if level == "placeholder":
            return "Agent", "有新事件，点开查看。"
        title = f"「{title_hint}」" if title_hint else "Agent"
        if kind == "approval":
            if level == "full" and ctx.get("tools"):
                return title, "等待授权：" + "、".join(ctx["tools"][:4])
            return title, "有敏感操作等待你的授权"
        if kind == "approval_autodenied":
            if level == "full" and ctx.get("tools"):
                return title, ("定时任务遇到需授权操作，已按无人值守策略自动拒绝："
                               + "、".join(ctx["tools"][:4]))
            return title, "定时任务遇到需授权操作，已自动拒绝"
        if kind == "schedule_start":
            if level == "full" and ctx.get("prompt"):
                return title, "定时任务开始运行：" + self._strip_preamble(ctx["prompt"])[:80]
            return title, "定时任务开始运行"
        # result
        status = ctx.get("status", "")
        mark = {"success": "任务完成 ✓", "warning": "任务完成（有警告）",
                "fail": "任务失败 ✗"}.get(status, f"任务结束（{status}）")
        if level == "full" and ctx.get("prompt"):
            return title, f"{mark} — {self._strip_preamble(ctx['prompt'])[:80]}"
        return title, mark

    @staticmethod
    def _strip_preamble(prompt: str) -> str:
        """定时任务的落盘 prompt 带无人值守 preamble，通知正文剥掉只留任务本体。"""
        marker = "\n\nTask: "
        idx = prompt.find(marker)
        return prompt[idx + len(marker):] if idx >= 0 else prompt

    def _session_title(self, sid: str, fallback_prompt: str = "") -> str:
        """会话名：meta.json 的用户改名优先 → prompt 头派生 → sid。轻量直读，绝不扫描。"""
        try:
            meta_file = self.sessions_dir / sid / "meta.json"
            if meta_file.exists():
                t = str((json.loads(meta_file.read_text(encoding="utf-8")) or {})
                        .get("title") or "").strip()
                if t:
                    return t[:24]
        except Exception:
            pass
        p = self._strip_preamble(fallback_prompt).strip().replace("\n", " ")
        return p[:24] if p else sid[:24]

    # ================================================ Live Activity
    def register_activity(self, session_id: str, turn_id: str,
                          device_id: str, token: str) -> Dict[str, Any]:
        """app 上报一个活动的推送 token（每设备每轮一条）。轮次已不在时拒绝，
        app 据此本地收尾孤儿活动。须在事件循环线程调用（routes 直调）。"""
        if not (self.la_enabled and self.apns is not None):
            return {"active": False, "reason": "push_disabled"}
        session = self.session_manager._all_sessions.get(session_id)
        if session is None or session.is_cold:
            return {"active": False, "reason": "session_not_hot"}
        journal = session.journal
        if journal.turn_state == "none" or journal.current_turn_id != turn_id:
            return {"active": False, "reason": "turn_over"}

        st = self._activities.get(session_id)
        if st is not None and st.turn_id != turn_id:
            self._teardown_activity(st)   # 上一轮的活动读者残留：收掉再建
            st = None
        if st is None:
            st = _ActivityState(session_id, turn_id)
            self._activities[session_id] = st
            st.reader_task = asyncio.create_task(self._activity_reader(st, journal))
            st.heartbeat_task = asyncio.create_task(self._activity_heartbeat(st))
        st.tokens[device_id] = token
        # 新 token 立即补推当前内容（app 刚起活动，第一帧别等下个事件）
        asyncio.create_task(self._activity_push(st, priority=5, force=True))
        return {"active": True}

    def _teardown_activity(self, st: _ActivityState) -> None:
        st.ended = True
        for t in (st.reader_task, st.flush_task, st.heartbeat_task):
            if t is not None:
                t.cancel()
        self._activities.pop(st.session_id, None)

    # ---------- 读者：会话 journal → 内容态 ----------
    async def _activity_reader(self, st: _ActivityState, journal) -> None:
        q = journal.subscribe()
        try:
            while not st.ended:
                env = await q.get()
                if env is None:
                    break   # 会话被删除
                if env.get("turn_id") not in (st.turn_id, None):
                    continue
                etype = env.get("type")
                data = env.get("data") or {}
                if etype == "turn_ended":
                    await self._activity_end(st, str(data.get("status", "success")))
                    break
                if self._la_reduce(st, etype, data):
                    await self._activity_push_throttled(
                        st, priority=10 if etype in ("awaiting_approval", "suspended") else 5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[Push] Live Activity 读者异常（活动停更，靠 stale-date 兜底）: {e}")
        finally:
            journal.unsubscribe(q)

    def _la_reduce(self, st: _ActivityState, etype: str, data: Dict[str, Any]) -> bool:
        """账本事件 → 内容态归约。返回内容是否变化（变了才值得推）。"""
        c = st.content
        before = json.dumps(c, ensure_ascii=False, sort_keys=True)
        if etype == "plan_updated":
            task = data.get("task") or {}
            st.plan = [s for s in (task.get("plan") or []) if isinstance(s, dict)]
            self._apply_plan(st)
        elif etype == "tool_started":
            c["stage"] = _tool_label(str(data.get("name", "")))[:40]
            c["status"] = "running"
        elif etype == "tool_call_pending":
            c["detail"] = ("准备调用 " + _tool_label(str(data.get("name", ""))))[:80]
        elif etype == "tool_result":
            c["detail"] = ""
        elif etype == "queue_moved":
            pos = data.get("position")
            c["stage"] = "排队中" if not pos else f"排队中 · 前面还有 {pos}"[:40]
        elif etype == "awaiting_approval":
            names = "、".join(_tool_label(t.get("name", "?"))
                              for t in (data.get("pending_tools") or [])[:3])
            c["status"] = "awaiting_approval"
            c["stage"] = (f"等待授权：{names}" if names else "等待授权")[:40]
        elif etype in ("resumed", "turn_started"):
            if c["status"] in ("awaiting_approval", "suspended"):
                c["status"] = "running"
                self._apply_plan(st)   # 回到计划视角的当前步
        elif etype == "suspended":
            c["status"] = "suspended"
            c["stage"] = "已挂起（可恢复）"
        else:
            return False
        return json.dumps(c, ensure_ascii=False, sort_keys=True) != before

    def _apply_plan(self, st: _ActivityState) -> None:
        """计划 → steps 窗口（≤6 步，围绕当前 doing 步取窗）+ 进度计数 + 阶段名。"""
        c = st.content
        plan = st.plan
        if not plan:
            return
        c["total"] = len(plan)
        c["done"] = sum(1 for s in plan if s.get("status") == "done")
        doing_idx = next((i for i, s in enumerate(plan) if s.get("status") == "doing"), None)
        if doing_idx is not None:
            c["stage"] = str(plan[doing_idx].get("title", ""))[:40] or c["stage"]
        lo = 0 if len(plan) <= 6 else max(0, min((doing_idx or 0) - 2, len(plan) - 6))
        c["steps"] = [
            {"t": str(s.get("title", ""))[:24],
             "s": _STEP_STATE.get(str(s.get("status", "pending")), "pending")}
            for s in plan[lo:lo + 6]
        ]

    # ---------- 推送：节流 + 心跳 + 收尾 ----------
    async def _activity_push_throttled(self, st: _ActivityState, priority: int) -> None:
        now = time.time()
        if now - st.last_push_ts >= self.la_min_interval or priority == 10:
            await self._activity_push(st, priority=priority)
        elif st.flush_task is None or st.flush_task.done():
            delay = self.la_min_interval - (now - st.last_push_ts)

            async def _trailing():
                await asyncio.sleep(max(delay, 0.1))
                await self._activity_push(st, priority=5)

            st.flush_task = asyncio.create_task(_trailing())

    async def _activity_push(self, st: _ActivityState, *, priority: int,
                             event: str = "update", extra_aps: Optional[Dict] = None,
                             force: bool = False) -> None:
        if self.apns is None or st.ended and event != "end":
            return
        sig = event + json.dumps(st.content, ensure_ascii=False, sort_keys=True)
        if not force and event == "update" and sig == st.last_pushed:
            return
        now = int(time.time())
        aps: Dict[str, Any] = {
            "timestamp": now, "event": event,
            "content-state": dict(st.content),
            "stale-date": now + self.la_stale,
        }
        if extra_aps:
            aps.update(extra_aps)
        payload = {"aps": aps}
        topic = f"{self.bundle_id}.push-type.liveactivity"
        for device_id, token in list(st.tokens.items()):
            dev = self.registry.devices.get(device_id) or {}
            res = await self.apns.send(token, payload, topic=topic,
                                       push_type="liveactivity", priority=priority,
                                       expiration=0, environment=dev.get("env", "auto"))
            if res.get("status") == 410:
                st.tokens.pop(device_id, None)
        st.last_pushed = sig
        st.last_push_ts = time.time()

    async def _activity_heartbeat(self, st: _ActivityState) -> None:
        """滚动 stale-date：Mac 端进程死掉时，岛上最多 stale_seconds 后自动标记过期，
        绝不永远"运行中"。低优先级、只在静默窗口补发。"""
        try:
            while not st.ended:
                await asyncio.sleep(self.la_heartbeat)
                if time.time() - st.last_push_ts >= self.la_heartbeat - 1:
                    await self._activity_push(st, priority=5, force=True)
        except asyncio.CancelledError:
            pass

    async def _activity_end(self, st: _ActivityState, status: str) -> None:
        c = st.content
        c["status"] = {"success": "ok"}.get(status, status)
        c["stage"] = {"success": "已完成 ✓", "fail": "执行失败 ✗", "warning": "完成（有警告）",
                      "cancelled": "已取消"}.get(status, f"已结束（{status}）")
        if c["total"]:
            c["done"] = c["total"] if status == "success" else c["done"]
        now = int(time.time())
        await self._activity_push(st, priority=10, event="end",
                                  extra_aps={"dismissal-date": now + 1800}, force=True)
        self._teardown_activity(st)

    # ---------- push-to-start（iOS 17.2+；无 token 的设备静默跳过） ----------
    async def _push_to_start(self, sid: str, turn_id: str, meta: Dict[str, Any]) -> None:
        if not (self.la_enabled and self.apns is not None):
            return
        devices = self.registry.push_to_start_capable()
        if not devices:
            return
        title = self._session_title(sid, meta.get("prompt", ""))
        now = int(time.time())
        attributes = {"sessionId": sid, "turnId": turn_id, "title": title}
        if self.instance_id:
            attributes["src"] = self.instance_id
        payload = {"aps": {
            "timestamp": now, "event": "start",
            "attributes-type": "AgentActivityAttributes",
            "attributes": attributes,
            "content-state": {"stage": "定时任务启动", "status": "running",
                              "done": 0, "total": 0, "steps": [], "detail": ""},
            "stale-date": now + self.la_stale,
        }}
        topic = f"{self.bundle_id}.push-type.liveactivity"
        for d in devices:
            res = await self.apns.send(d["push_to_start_token"], payload, topic=topic,
                                       push_type="liveactivity", priority=10,
                                       expiration=now + 300,
                                       environment=d.get("env", "auto"))
            if not res.get("ok"):
                print(f"[Push] push-to-start 失败 device={d['device_id']} "
                      f"reason={res.get('reason')}")
