"""客户端反查通道（client_query）+ get_location 工具金测试。

通道契约：工具线程发请求事件（经主循环 journal.emit）→ 阻塞等待 → 客户端 POST
应答落位唤醒；无 loop / 无属性（沙箱）/ 超时 / 拒绝均 fail-open 明确降级。
"""
import asyncio
import threading

import pytest

from framework.core import client_query
from framework.mcp.tools.user_location import GetLocationTool


class _FakeJournal:
    def __init__(self):
        self.events = []

    def emit(self, etype, data=None, turn_id=None):
        self.events.append((etype, data or {}))


class _FakeSession:
    def __init__(self):
        self.journal = _FakeJournal()
        self.client_queries = {}


@pytest.fixture
def bg_loop():
    """后台线程跑一个真实事件循环，模拟主循环；用毕关停。"""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    client_query.set_loop(loop)
    yield loop
    client_query.set_loop(None)
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()


def test_no_loop_returns_none():
    client_query.set_loop(None)
    assert client_query.request(_FakeSession(), "location_request", timeout=0.1) is None


def test_sandbox_session_without_channel_returns_none(bg_loop):
    class _Bare:  # 沙箱替身：无 client_queries / journal 属性
        pass
    assert client_query.request(_Bare(), "location_request", timeout=0.1) is None


def test_timeout_returns_none_and_cleans_up(bg_loop):
    session = _FakeSession()
    assert client_query.request(session, "location_request", timeout=0.2) is None
    assert session.client_queries == {}          # 挂起表不残留
    assert session.journal.events, "超时前请求事件应已发出"
    assert session.journal.events[0][0] == "location_request"
    assert "request_id" in session.journal.events[0][1]


def test_roundtrip_resolve_wakes_requester(bg_loop):
    session = _FakeSession()
    result = {}

    def _client():
        # 模拟附着客户端：等请求事件出现，取 request_id 回 POST（resolve）
        for _ in range(200):
            if session.journal.events:
                break
            threading.Event().wait(0.01)
        rid = session.journal.events[0][1]["request_id"]
        assert client_query.resolve(session, rid,
                                    {"status": "ok", "latitude": 35.6581,
                                     "longitude": 139.7017, "place": "東京都渋谷区"})

    t = threading.Thread(target=_client)
    t.start()
    result = client_query.request(session, "location_request", timeout=5)
    t.join(timeout=5)
    assert result and result["status"] == "ok" and result["place"] == "東京都渋谷区"
    assert session.client_queries == {}


def test_resolve_unknown_request_id_rejected(bg_loop):
    assert client_query.resolve(_FakeSession(), "q_nope", {"status": "ok"}) is False


def _run_tool_with_session(session):
    from framework.agents import runtime
    runtime.set_current_session(session)
    try:
        return GetLocationTool().execute()
    finally:
        runtime.set_current_session(None)


def test_tool_unavailable_without_channel():
    client_query.set_loop(None)
    out = _run_tool_with_session(_FakeSession())
    assert "定位不可用" in out and "时区" in out    # 明确降级并给退路


def test_tool_denied_reply(bg_loop, monkeypatch):
    monkeypatch.setattr(client_query, "request",
                        lambda *a, **k: {"status": "denied", "error": "用户拒绝定位授权"})
    out = _run_tool_with_session(_FakeSession())
    assert "定位不可用" in out and "用户拒绝定位授权" in out


def test_tool_formats_ok_reply(bg_loop, monkeypatch):
    monkeypatch.setattr(client_query, "request",
                        lambda *a, **k: {"status": "ok", "latitude": 35.65812,
                                         "longitude": 139.70172, "place": "東京都渋谷区",
                                         "accuracy_m": 65.0})
    out = _run_tool_with_session(_FakeSession())
    assert "東京都渋谷区" in out
    assert "35.658" in out and "139.702" in out   # 三位小数截断
    assert "±65m" in out


def test_tool_ok_without_coords_degrades(bg_loop, monkeypatch):
    monkeypatch.setattr(client_query, "request", lambda *a, **k: {"status": "ok"})
    out = _run_tool_with_session(_FakeSession())
    assert "定位不可用" in out
