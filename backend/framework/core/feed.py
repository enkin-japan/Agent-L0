"""全局任务 feed(PROTOCOL.md §2.7):跨会话聚合生命周期事件。

任务中心与后台通知的推送源——替代 /tasks 差分轮询("消失即完结"的猜测式诊断)。
仅内存:环形缓冲兜短时断线的补发,冷启动状态由 /tasks 快照对账(feed 是增量流,
不是持久账本;会话级持久重放走各自的 journal)。

写入方:各会话 TurnJournal 在 emit 生命周期事件时镜像到此(单一写入路径,
不存在旁路);turn_ended 镜像为瘦身版(全量在会话账本/history,feed 只报状态)。
"""
import asyncio
from collections import deque
from typing import Any, Dict

# 进入全局 feed 的事件类型(会话内过程事件不聚合——feed 是任务生命周期视图)
FEED_TYPES = {
    "turn_queued", "turn_started", "awaiting_approval",
    "suspended", "resumed", "turn_ended",
}


class GlobalFeed:
    def __init__(self, capacity: int = 512):
        self.seq = 0                       # feed 自己的单调游标(独立于会话 seq)
        self.ring: deque = deque(maxlen=capacity)
        self._listeners: set = set()

    def publish(self, session_id: str, env: Dict[str, Any]) -> None:
        """镜像一条会话账本信封(必须在事件循环线程调用,与 journal.emit 同线程)。"""
        data = env.get("data") or {}
        if env.get("type") == "turn_ended":
            # 瘦身:终态只报 status/turn_index(+prompt 头,通知的最低语境),全量在会话账本
            data = {"status": data.get("status"), "turn_index": data.get("turn_index"),
                    "prompt": str(data.get("prompt", ""))[:120]}
        elif env.get("type") == "turn_queued":
            data = {"prompt": str(data.get("prompt", ""))[:120],
                    "origin": data.get("origin", "chat")}
        self.seq += 1
        item = {
            "feed_seq": self.seq,
            "session_id": session_id,
            "seq": env.get("seq"),
            "turn_id": env.get("turn_id"),
            "ts": env.get("ts"),
            "type": env.get("type"),
            "data": data,
        }
        self.ring.append(item)
        for q in list(self._listeners):
            try:
                q.put_nowait(item)
            except Exception:
                pass

    def replay(self, after: int = 0):
        return [e for e in self.ring if e["feed_seq"] > after]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._listeners.discard(q)
