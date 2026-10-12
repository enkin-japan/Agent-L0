"""会话事件账本(PROTOCOL.md §2/§3 的实现)。

原则:**先记账,后分发**。编排循环/TurnRunner 把每个事件无条件 append 到本账本;
SSE 附着流、/turn 状态查询、旧端点适配层全部是账本的读者。不存在"某条路径忘了
发事件"的结构可能。

两层存储:
- 内存环(ring):活跃轮的**全部**事件(含 token/thinking 增量),新轮 turn_queued
  时清空上一轮——重附着可完整重放进行中的轮次;
- journal.jsonl:仅里程碑事件持久化(生命周期/工具/计划),首行 journal_header;
  tool_started.args / tool_result.response 的持久化副本截断至 2000 字符;
  turn_ended 持久化瘦身为 {status, turn_index}(全量已在 history.jsonl)。

线程纪律:emit() 必须在事件循环线程调用(工具线程的进度钩子经 call_soon_threadsafe
桥接,与旧 emit 契约一致);磁盘追加为小量同步写(对齐 events.py 的既有纪律),
永不抛异常、永不阻断主流程。
"""
import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .jsonl import iter_records, repair_tail

# 持久化进 journal.jsonl 的里程碑事件类型(增量与瞬时进度不落盘,见模块头)
MILESTONE_TYPES = {
    "turn_queued", "turn_started", "awaiting_approval", "suspended", "resumed",
    "turn_ended", "tool_started", "tool_result",
    "plan_updated", "step_completed", "step_failed",
}
# 持久化副本的字段截断上限(SSE 实时帧不受此限)
_PERSIST_TRUNCATE = 2000

# 终态执行事实仅作有界投影，不新增账本或模型上下文。
_RECEIPT_ITEMS = 64
_RECEIPT_DETAIL = 2000
_RECEIPT_IDENTIFIER = 128
_RECEIPT_STATUS = 32

# ── 重启后的 seq 水位余量(OSS 扫描 §6.3,2026-08-17)────────────────────────────
# 增量事件(token_delta/thinking_delta)同样消耗 seq,但**不在 MILESTONE_TYPES 里、
# 不落盘**——一次长回答轻松吃掉上千个 seq。进程重启后若直接从末条里程碑续号,seq 会
# **就地回退**到崩溃前已经用过的号段;而客户端游标是内存态、跨重连刻意保留
# (PROTOCOL §2.4「任何时刻、任意次」),于是重启后的新事件全部撞上服务端直播循环的
# `seq <= 游标` 判定被当成重放丢弃——**会话对用户表现为永久静音**。触发者是最常规的
# 动作:devctl restart / SidecarManager 监护重启 / 崩溃自愈(app 内嵌 sidecar 就是设计,
# 重启是日常)。
# 修法:重启续号跳过一整段余量,取 1e6(Python 整数无上限,JSON/JS Number 到 2^53 也远够用:
# 按每次重启 +1e6 计约 9e9 次重启才触顶)。
# **真正的不变量**(别记成「1e6 > 单轮增量数」):自**最后一条成功落盘的里程碑**以来
# emit 的事件总数必须 < 1e6。正常态下里程碑很密(tool_started/tool_result 都是),
# 空档被单段生成的 token 数封顶(~1e4 量级),余量有百倍冗余;但 `_persist` 失败会被吞掉
# (磁盘满/只读),那时 last_persisted 冻住而 self.seq 继续爬,爬过 1e6 后下次重启仍会回退。
# 故落盘失败必须响亮可见(见 _persist 的一次性告警),而不是每条事件打一行 print 淹掉。
# 彻底闭合要在 __init__ 落一条持久高水位线,那会引入新的账本记录类型,超出止血范围。
# 另一半——进程死时那一轮没有 `turn_ended`,客户端停在 generating——已由启动恢复补发
# 收敛(2026-08-22 B2:SessionManager.emit_recovery_events,PROTOCOL §3);全局 feed 的
# feed_seq 进程内归零是同源缺陷,无持久层走客户端按 BOOT_ID 重锚(§2.7)。
SEQ_RESTART_RESERVE = 1_000_000

# ── 进程代际（B2 代际围栏）─────────────────────────────────────────────────────
# 每次进程启动一个新值,经 stream_attached 首帧与 /turn 下发。**必须是模块级**而非
# TurnJournal 实例属性:/turn 对冷会话会另 new 一个只读实例,实例级值会让同一会话的
# 两个端点报出不同代际。
# 语义(PROTOCOL §2.4/§2.7):代际 = seq 编号命名空间的纪元。会话流的 seq 因
# SEQ_RESTART_RESERVE 跨代际单调,旧游标仍然有效——会话流客户端**不需要**因代际变化
# 重锚;全局 feed 无持久层、feed_seq 每代归零,代际变化即游标作废,客户端必须清零重锚。
BOOT_ID = uuid.uuid4().hex[:16]

# 轮次状态(PROTOCOL.md §1)
TURN_NONE = "none"
TURN_QUEUED = "queued"
TURN_RUNNING = "running"
TURN_AWAITING = "awaiting_approval"
TURN_SUSPENDED = "suspended"


def new_turn_id() -> str:
    return "t_" + uuid.uuid4().hex[:12]


def _truncated_copy(data: Dict[str, Any], keys: List[str]) -> Dict[str, Any]:
    """持久化副本:把指定键的字符串化长度截到上限并打 truncated 标记。"""
    out = dict(data)
    for k in keys:
        v = out.get(k)
        s = v if isinstance(v, str) else (json.dumps(v, ensure_ascii=False, default=str)
                                          if v is not None else None)
        if s is not None and len(s) > _PERSIST_TRUNCATE:
            out[k] = s[:_PERSIST_TRUNCATE]
            out["truncated"] = True
    return out


class TurnJournal:
    """单会话事件账本:seq 分配、内存环、里程碑落盘、订阅分发、轮次状态跟踪。
    feed 非 None 时,生命周期事件同步镜像进全局任务 feed(PROTOCOL.md §2.7)。"""

    def __init__(self, session_dir: Path, session_id: Optional[str] = None, feed=None):
        self.session_id = session_id or session_dir.name
        self.feed = feed
        self.path = session_dir / "journal.jsonl"
        last_persisted = self._load_last_seq()
        # 判据是「账本在不在」,**不是** last_persisted 的真假:后者为 0 有三种含义
        # (无账本 / 空账本 / 读失败),把「读失败」当成冷启动会从 0 起号——正是账本
        # 损坏这个最该防的场景下把永久静音原样重现,比原缺陷更狠。
        # 无账本 = 冷启动的新会话,保持 0 让 seq 从 1 起(可读,且不存在回退风险)。
        self.seq = 0 if last_persisted is None else last_persisted + SEQ_RESTART_RESERVE
        self.ring: List[Dict[str, Any]] = []          # 活跃轮全部事件(信封)
        self._listeners: set = set()                   # asyncio.Queue 集合
        self._closed = False
        self._persist_failed = False                   # 落盘失败告警去重(见 _persist)

        # ---- 轮次状态跟踪(GET /turn 的数据源,由 emit 单点驱动) ----
        self.current_turn_id: Optional[str] = None
        self.turn_origin: str = "chat"      # 本轮来源（turn_queued.origin：chat / schedule …）；⑤′-3 据此识别无人值守轮
        self.turn_state: str = TURN_NONE
        self.state_since: float = 0.0
        self.turn_prompt: str = ""
        self.partial_text: str = ""
        self.partial_thinking: str = ""
        self.pending_tools: Optional[List[Dict[str, Any]]] = None
        self.approval_seq: Optional[int] = None  # 当前 awaiting_approval 的信封序号；只由 emit 驱动
        self.task_snapshot: Optional[Dict[str, Any]] = None
        self.turn_first_seq: int = 0
        self.turn_client_id: Optional[str] = None   # 本轮提交幂等键(B3,来自 turn_queued)

    # ---------- 落盘 ----------
    def _load_last_seq(self) -> Optional[int]:
        """账本里已落盘的最大 seq(取全部记录的 max,不是「末行」——坏行会让末行不是最大)。

        **None 专指「没有账本文件」**(冷启动),与「读失败」「空账本」严格区分:后两者
        返回 0 且照样吃水位余量,否则一次读盘失败就会把 seq 打回 0(见 __init__)。
        """
        if not self.path.exists():
            return None
        try:
            last = 0
            for rec in iter_records(self.path):   # 坏行纪律唯一口径（core/jsonl）
                if isinstance(rec.obj.get("seq"), int):
                    last = max(last, rec.obj["seq"])
            return last
        except Exception as e:
            # 返回 0 而非 None:账本文件确实在,只是读不动——仍按「续跑」加余量起号
            print(f"[Journal] 读取账本失败(仍按续跑加水位余量起号): {e}")
            return 0

    def _persist(self, env: Dict[str, Any]) -> None:
        """里程碑事件追加落盘(带持久化瘦身);失败只打印,绝不影响主流程。"""
        try:
            etype, data = env["type"], env["data"]
            if etype == "turn_ended":
                data = {"status": data.get("status"),
                        **({"turn_index": data["turn_index"]} if "turn_index" in data else {}),
                        **({"discarded": True} if data.get("discarded") is True else {})}
            elif etype == "tool_started":
                data = _truncated_copy(data, ["args"])
            elif etype == "tool_result":
                data = _truncated_copy(data, ["response"])
            rec = {**env, "data": data}
            # 写前修尾（B1）：崩溃残留的半截记录若不处理，本次追加会与它粘成一行垃圾。
            # is_new 必须在修尾**之后**按大小判：首次写 header 写到一半就崩，文件「在」
            # 但会被修尾截成 0 字节——按存在性判会跳过 header,首行契约永久缺失（首审 M9）
            repair_tail(self.path)
            is_new = not self.path.exists() or self.path.stat().st_size == 0
            with open(self.path, "a", encoding="utf-8") as f:
                if is_new:
                    f.write(json.dumps({"kind": "journal_header", "v": 1}) + "\n")
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            if self._persist_failed:
                # 恢复也要说一声：水位余量的不变量重新成立了，这件事值得在日志里留痕
                self._persist_failed = False
                print("[Journal] 里程碑落盘已恢复（seq 水位余量的不变量重新成立）")
        except Exception as e:
            # 落盘失败会悄悄侵蚀水位余量的不变量(last_persisted 冻住、self.seq 继续爬,
            # 爬过 SEQ_RESTART_RESERVE 后下次重启照样回退),故首次失败必须响亮——
            # 之后只在恢复时再说一次,避免磁盘满时每条事件刷一行把日志淹掉。
            if not self._persist_failed:
                self._persist_failed = True
                print(f"[Journal] ⚠️ 里程碑落盘失败(事件仍在内存环;持续失败会使重启后的 "
                      f"seq 水位余量失效,见 SEQ_RESTART_RESERVE): {e}")

    # ---------- 写入(唯一入口) ----------
    def emit(self, etype: str, data: Optional[Dict[str, Any]] = None,
             turn_id: Optional[str] = None) -> Dict[str, Any]:
        """追加一个事件:分配 seq → 状态跟踪 → 内存环 → 里程碑落盘 → 分发订阅者。
        必须在事件循环线程调用。返回完整信封。"""
        data = data or {}
        self.seq += 1
        env = {
            "seq": self.seq,
            "turn_id": turn_id or self.current_turn_id,
            "ts": round(time.time(), 3),
            "type": etype,
            "data": data,
        }
        self._track(etype, data, env)
        self.ring.append(env)
        if etype in MILESTONE_TYPES:
            self._persist(env)
        for q in list(self._listeners):
            try:
                q.put_nowait(env)
            except Exception:
                pass
        if self.feed is not None:
            try:
                from .feed import FEED_TYPES
                if etype in FEED_TYPES:
                    self.feed.publish(self.session_id, env)
            except Exception as e:
                print(f"[Journal] feed 镜像失败(忽略): {e}")
        return env

    def _track(self, etype: str, data: Dict[str, Any], env: Dict[str, Any]) -> None:
        """由事件流单点驱动的轮次状态机(GET /turn 数据源)。"""
        now = env["ts"]
        if etype in {"turn_queued", "turn_started", "resumed", "suspended", "turn_ended"}:
            self.approval_seq = None
        if etype == "turn_queued":
            # 新轮开始:清上一轮的环与累计(上一轮已以 turn_ended 收尾)
            self.ring.clear()
            self.current_turn_id = env["turn_id"]
            self.turn_state = TURN_QUEUED
            self.state_since = now
            self.turn_prompt = str(data.get("prompt", ""))
            self.turn_origin = str(data.get("origin") or "chat")
            self.turn_client_id = data.get("client_turn_id") or None
            self.partial_text = ""
            self.partial_thinking = ""
            self.pending_tools = None
            self.task_snapshot = None
            self.turn_first_seq = env["seq"]
        elif etype == "turn_started":
            self.turn_state = TURN_RUNNING
            self.state_since = now
        elif etype == "awaiting_approval":
            self.turn_state = TURN_AWAITING
            self.state_since = now
            self.pending_tools = data.get("pending_tools") or []
            self.approval_seq = env["seq"]
        elif etype == "suspended":
            self.turn_state = TURN_SUSPENDED
            self.state_since = now
        elif etype == "resumed":
            self.turn_state = TURN_QUEUED  # 重新排队,随后有新的 turn_started
            self.state_since = now
            self.pending_tools = None
        elif etype == "turn_ended":
            self.turn_state = TURN_NONE
            self.state_since = now
            self.pending_tools = None
        elif etype == "token_delta":
            self.partial_text += str(data.get("text", ""))
        elif etype == "thinking_delta":
            self.partial_thinking += str(data.get("text", ""))
        elif etype == "plan_updated":
            self.task_snapshot = data.get("task")

    # ---------- 读取 ----------
    def execution_receipts(self) -> Optional[Dict[str, Any]]:
        """同轮最近64条工具开始/返回事实；缺轮首内存证据时未知，不猜副作用。

        不重放磁盘、不复制args。started仅证明开始，result的status保留工具返回
        原值，不等于外部操作成功。省略非空参数和任何截断均显式标记。
        """
        tid = self.current_turn_id
        if tid is None or not any(e.get("type") == "turn_queued" and e.get("turn_id") == tid
                                  for e in self.ring):
            return None
        items: List[Dict[str, Any]] = []
        truncated = False
        for env in reversed(self.ring):
            if env.get("turn_id") != tid or env.get("type") not in {"tool_started", "tool_result"}:
                continue
            if len(items) >= _RECEIPT_ITEMS:
                truncated = True
                break
            data = env.get("data") or {}
            cut = data.get("truncated") is True

            def bounded(value: Any, limit: int) -> str:
                nonlocal cut
                if not isinstance(value, str):
                    cut |= value is not None
                    return ""
                cut |= len(value) > limit
                return value[:limit]

            item = {"seq": env["seq"], "ts": env["ts"], "type": env["type"],
                    "name": bounded(data.get("name"), _RECEIPT_IDENTIFIER),
                    "call_id": bounded(data.get("call_id"), _RECEIPT_IDENTIFIER),
                    "detail": ""}
            if env["type"] == "tool_started":
                cut |= bool(data.get("args"))  # 参数有意省略，不能伪装完整凭据
            else:
                item["detail"] = bounded(data.get("response"), _RECEIPT_DETAIL)
                if data.get("status") is not None:
                    item["status"] = bounded(data["status"], _RECEIPT_STATUS)
            if cut:
                item["truncated"] = True
                truncated = True
            items.append(item)
        items.reverse()
        return {"items": items, "truncated": truncated}

    def replay(self, after: int = 0) -> List[Dict[str, Any]]:
        """按 seq 归并重放:磁盘里程碑(环起点之前) + 内存环(活跃轮全量)。
        磁盘读为小文件同步读;大账本场景由端点侧 to_thread 包裹本方法。"""
        ring_min = self.ring[0]["seq"] if self.ring else None
        out: List[Dict[str, Any]] = []
        try:
            for rec in iter_records(self.path):   # 坏行纪律唯一口径（core/jsonl）
                s = rec.obj.get("seq")
                if not isinstance(s, int) or s <= after:
                    continue
                if ring_min is not None and s >= ring_min:
                    break  # 环覆盖区间:以环(含增量)为准
                out.append(rec.obj)
        except Exception as e:
            print(f"[Journal] 账本重放读盘失败(退化为仅内存环): {e}")
        out.extend(e for e in self.ring if e["seq"] > after)
        return out

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        if self._closed:
            q.put_nowait(None)
        else:
            self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._listeners.discard(q)

    def close(self) -> None:
        """会话删除时终结账本:通知全部订阅者收流。"""
        self._closed = True
        for q in list(self._listeners):
            try:
                q.put_nowait(None)
            except Exception:
                pass
        self._listeners.clear()

    # ---------- 状态快照(GET /turn) ----------
    def turn_snapshot(self) -> Optional[Dict[str, Any]]:
        """当前(未 ended)轮次的可恢复状态;无活跃轮返回 None。"""
        if self.turn_state == TURN_NONE or self.current_turn_id is None:
            return None
        return {
            "turn_id": self.current_turn_id,
            "state": self.turn_state,
            "since": self.state_since,
            "prompt": self.turn_prompt,
            "partial_text": self.partial_text,
            "partial_thinking": self.partial_thinking,
            "pending_tools": self.pending_tools or [],
            "task": self.task_snapshot,
            "first_seq": self.turn_first_seq,
            "last_seq": self.seq,
        }
