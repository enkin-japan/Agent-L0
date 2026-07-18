"""客户端反查通道（client query bridge）。

有些事实只有「用户手里的设备」知道（当前位置是第一例）：后端主机拿不到或拿到即错
——算力端在桌上，人可能在外面拿着 iPhone。本模块把「工具在工作线程里要一个值」
桥接为「会话事件流上发一个请求事件，由附着中的客户端回 POST 应答」：

    工具线程 request(session, etype, data, timeout)
      → call_soon_threadsafe 跳回事件循环线程 journal.emit(etype)（SSE 推到客户端）
      → threading.Event 阻塞等待
    客户端 POST /api/v1/chat/{sid}/location {request_id, ...}
      → resolve(session, request_id, payload) 置结果并唤醒工具线程

设计边界：
- 请求事件**不是里程碑**（不落盘）：只进内存环——活跃轮内客户端断线重连时经环重放
  仍能补收到请求；新轮 turn_queued 清环，过期请求不会穿越轮次。
- 无客户端附着 / 拒绝 / 超时 = 值不可用，工具明确降级——通道 fail-open，
  绝不卡死编排（threading.Event.wait 自带超时）。
- 沙箱一次性会话替身无 client_queries / journal 属性（getattr 拿 None）→ 通道对
  子生成天然关闭，与 include_env 隐私门同向：个人事实不进沙箱。
- 多客户端同时附着同一会话时首个应答生效（resolve 弹出请求），后到者收 409。
"""
import threading
import uuid
from typing import Any, Dict, Optional

# 主事件循环（app 启动时登记）。金测试 / 无服务环境为 None → 通道整体不可用（fail-open）。
_loop = None


def set_loop(loop) -> None:
    global _loop
    _loop = loop


def request(session, etype: str, data: Optional[Dict[str, Any]] = None,
            timeout: float = 15.0) -> Optional[Dict[str, Any]]:
    """（工具线程调用）向会话的附着客户端要一个值。

    返回应答 payload 的拷贝；None = 不可用（无通道 / 无客户端应答 / 超时）。
    """
    queries = getattr(session, "client_queries", None)
    journal = getattr(session, "journal", None)
    if _loop is None or queries is None or journal is None:
        return None
    request_id = "q_" + uuid.uuid4().hex[:12]
    done = threading.Event()
    slot: Dict[str, Any] = {}
    queries[request_id] = (done, slot)
    try:
        _loop.call_soon_threadsafe(
            journal.emit, etype, {**(data or {}), "request_id": request_id})
        if not done.wait(timeout):
            return None
        return dict(slot)
    finally:
        queries.pop(request_id, None)


def resolve(session, request_id: str, payload: Dict[str, Any]) -> bool:
    """（事件循环线程调用）客户端应答落位。返回是否有等待中的对应请求。"""
    queries = getattr(session, "client_queries", None)
    if not queries:
        return False
    entry = queries.pop(request_id, None)
    if entry is None:
        return False
    done, slot = entry
    slot.update(payload)
    done.set()
    return True
