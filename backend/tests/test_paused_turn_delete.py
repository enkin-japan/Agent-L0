"""R11: 删除未落盘轮必须闭合账本、撤销 write-ahead，且不触碰上一条历史。"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from framework.api.routes import delete_last_turn
from framework.core.feed import GlobalFeed
from framework.core.journal import TurnJournal
from framework.core.turn_runner import TurnConflict
from framework.memory.session import SessionManager

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_turn_runner import GEN, make_runner, submit


def setup(tmp_path, *, suspended=False, with_history=False):
    steps = ([{"text": "保留的答案"}] if with_history else []) + [
        {"tool_calls": [call("operation")]}, {"text": "新答案"}]
    registry = FakeRegistry([FakeTool("operation", approval=not suspended,
                                      read_only=False, concurrency_safe=False)])
    runner = make_runner(FakeEngine(steps), registry)
    manager = SessionManager(tmp_path / "sessions")
    manager.feed = GlobalFeed()
    manager.scheduler = runner.scheduler
    session = manager.get_or_create("r11", "测试")
    return runner, manager, session, registry


async def pause(runner, session, registry, suspended):
    if not suspended:
        return await submit(runner, session, "r11", prompt="撤销的输入")
    ctx = await runner.accept_turn(session, "r11", prompt="撤销的输入", images=[],
                                   audios=[], ingested_text="", attachments=[], gen=dict(GEN))
    async def flag():
        while not registry.executed:
            await asyncio.sleep(0)
        session.suspend_requested = True
    flag_task = asyncio.create_task(flag())
    result = await runner.run_accepted(session, "r11", ctx)
    await flag_task
    assert result["status"] == "suspended"
    return ctx, result


@pytest.mark.parametrize("suspended", [False, True])
@pytest.mark.parametrize("with_history", [False, True])
def test_delete_paused_turn_closes_without_archiving(tmp_path, suspended, with_history):
    runner, manager, session, registry = setup(tmp_path, suspended=suspended, with_history=with_history)
    async def scenario():
        if with_history:
            await submit(runner, session, "r11", prompt="保留的输入")
        before = session.history_log.read_bytes() if session.history_log.exists() else b""
        ctx, _ = await pause(runner, session, registry, suspended)
        listener = session.journal.subscribe()
        cursor = session.journal.seq
        result = await runner.delete_last_turn(manager, "r11")
        assert result["status"] is True and result["remaining_turns"] == int(with_history)
        terminal = listener.get_nowait()
        session.journal.unsubscribe(listener)
        assert terminal["type"] == "turn_ended" and terminal["turn_id"] == ctx["turn_id"]
        assert terminal["data"]["status"] == "cancelled"
        assert terminal["data"]["discarded"] is True
        assert "turn_index" not in terminal["data"]
        assert session.journal.turn_snapshot() is None
        assert session.pending_tool_calls is session.pending_turn is session.suspended_turn is None
        assert not session.inflight_path.exists() and not session.task_file.exists()
        assert not session.suspend_requested and runner.scheduler.snapshot()["tasks"] == []
        after = session.history_log.read_bytes() if session.history_log.exists() else b""
        assert after == before
        assert all(m.get("content") != "撤销的输入" for m in session.history)
        restarted = SessionManager(manager.sessions_dir)
        assert restarted.recover_inflight_turns() == []
        replay = TurnJournal(session.session_dir).replay(cursor)
        ended = [e for e in replay if e["type"] == "turn_ended"]
        assert len(ended) == 1 and ended[0]["data"]["discarded"] is True
        assert ended[0]["data"].get("turn_index") is None
        assert manager.feed.replay(0)[-1]["type"] == "turn_ended"
        await submit(runner, session, "r11", prompt="新输入")
        latest = session.journal.replay(0)[-1]
        assert latest["data"]["turn_index"] == int(with_history)
    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["queued", "running"])
def test_delete_active_turn_rejects_without_waiting_for_lock(tmp_path, state):
    runner, manager, session, _ = setup(tmp_path)
    async def scenario():
        await runner.accept_turn(session, "r11", prompt="处理中", images=[], audios=[],
                                 ingested_text="", attachments=[], gen=dict(GEN))
        if state == "running":
            session.journal.emit("turn_started", {})
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            session_manager=manager, turn_runner=runner)))
        async with session.lock:
            response = await asyncio.wait_for(delete_last_turn("r11", request), timeout=0.2)
        assert response.status_code == 409
        assert session.journal.turn_state == state and session.inflight_path.exists()
    asyncio.run(scenario())


def test_delete_waiting_for_lock_does_not_target_a_replacement_turn(tmp_path):
    runner, manager, session, registry = setup(tmp_path)
    async def scenario():
        await pause(runner, session, registry, False)
        async with session.lock:
            task = asyncio.create_task(runner.delete_last_turn(manager, "r11"))
            await asyncio.sleep(0)
            session.journal.emit("turn_ended", {"status": "cancelled", "discarded": True})
            session.journal.emit("turn_queued", {"prompt": "另一轮"}, turn_id="t_replacement")
            session.journal.emit("turn_ended", {"status": "success", "turn_index": 0})
        with pytest.raises(TurnConflict):
            await task
    asyncio.run(scenario())


def test_delete_waiting_for_lock_does_not_target_a_new_approval_batch(tmp_path):
    runner, manager, session, registry = setup(tmp_path)
    async def scenario():
        await pause(runner, session, registry, False)
        async with session.lock:
            task = asyncio.create_task(runner.delete_last_turn(manager, "r11"))
            await asyncio.sleep(0)
            session.journal.emit("resumed", {"trigger": "approve"})
            session.journal.emit("awaiting_approval", {"pending_tools": [{"name": "different"}]})
        with pytest.raises(TurnConflict):
            await task
        assert session.inflight_path.exists()
        assert session.journal.turn_state == "awaiting_approval"
    asyncio.run(scenario())


def test_unlink_failure_cannot_report_a_successful_discard(tmp_path, monkeypatch):
    runner, manager, session, registry = setup(tmp_path)
    async def scenario():
        await pause(runner, session, registry, False)
        cursor = session.journal.seq
        original = Path.unlink
        def fail_marker(path, *args, **kwargs):
            if path == session.inflight_path:
                raise PermissionError("synthetic marker unlink failure")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", fail_marker)
        with pytest.raises(PermissionError):
            await runner.delete_last_turn(manager, "r11")
        assert session.journal.seq == cursor and session.inflight_path.exists()
        assert session.pending_turn is not None
    asyncio.run(scenario())


def test_regular_history_delete_keeps_existing_storage_semantics(tmp_path):
    runner, manager, session, _ = setup(tmp_path)
    session.append_log({"status": "success", "prompt": "历史输入", "response": []})
    session.rebuild_from_log()
    result = asyncio.run(runner.delete_last_turn(manager, "r11"))
    assert result["status"] is True and result["remaining_turns"] == 0
    assert manager.get_history("r11") == []
    assert session.journal.replay(0) == []
