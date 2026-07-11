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

# 持久化进 journal.jsonl 的里程碑事件类型(增量与瞬时进度不落盘,见模块头)
MILESTONE_TYPES = {
    "turn_queued", "turn_started", "awaiting_approval", "suspended", "resumed",
    "turn_ended", "tool_started", "tool_result",
    "plan_updated", "step_completed", "step_failed",
}
# 持久化副本的字段截断上限(SSE 实时帧不受此限)
_PERSIST_TRUNCATE = 2000

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
        self.seq = self._load_last_seq()
        self.ring: List[Dict[str, Any]] = []          # 活跃轮全部事件(信封)
        self._listeners: set = set()                   # asyncio.Queue 集合
        self._closed = False

        # ---- 轮次状态跟踪(GET /turn 的数据源,由 emit 单点驱动) ----
        self.current_turn_id: Optional[str] = None
        self.turn_state: str = TURN_NONE
        self.state_since: float = 0.0
        self.turn_prompt: str = ""
        self.partial_text: str = ""
        self.partial_thinking: str = ""
        self.pending_tools: Optional[List[Dict[str, Any]]] = None
        self.task_snapshot: Optional[Dict[str, Any]] = None
        self.turn_first_seq: int = 0

    # ---------- 落盘 ----------
    def _load_last_seq(self) -> int:
        try:
            if not self.path.exists():
                return 0
            last = 0
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(rec.get("seq"), int):
                        last = max(last, rec["seq"])
            return last
        except Exception as e:
            print(f"[Journal] 读取账本失败(seq 从 0 起): {e}")
            return 0

    def _persist(self, env: Dict[str, Any]) -> None:
        """里程碑事件追加落盘(带持久化瘦身);失败只打印,绝不影响主流程。"""
        try:
            etype, data = env["type"], env["data"]
            if etype == "turn_ended":
                data = {"status": data.get("status"), "turn_index": data.get("turn_index")}
            elif etype == "tool_started":
                data = _truncated_copy(data, ["args"])
            elif etype == "tool_result":
                data = _truncated_copy(data, ["response"])
            rec = {**env, "data": data}
            is_new = not self.path.exists()
            with open(self.path, "a", encoding="utf-8") as f:
                if is_new:
                    f.write(json.dumps({"kind": "journal_header", "v": 1}) + "\n")
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            print(f"[Journal] 里程碑落盘失败(事件仍在内存环): {e}")

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
        if etype == "turn_queued":
            # 新轮开始:清上一轮的环与累计(上一轮已以 turn_ended 收尾)
            self.ring.clear()
            self.current_turn_id = env["turn_id"]
            self.turn_state = TURN_QUEUED
            self.state_since = now
            self.turn_prompt = str(data.get("prompt", ""))
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
    def replay(self, after: int = 0) -> List[Dict[str, Any]]:
        """按 seq 归并重放:磁盘里程碑(环起点之前) + 内存环(活跃轮全量)。
        磁盘读为小文件同步读;大账本场景由端点侧 to_thread 包裹本方法。"""
        ring_min = self.ring[0]["seq"] if self.ring else None
        out: List[Dict[str, Any]] = []
        try:
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        s = rec.get("seq")
                        if not isinstance(s, int) or s <= after:
                            continue
                        if ring_min is not None and s >= ring_min:
                            break  # 环覆盖区间:以环(含增量)为准
                        out.append(rec)
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
