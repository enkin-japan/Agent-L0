"""Existing optional approval guard: stale displayed turn cannot authorize replacement."""
import asyncio
from types import SimpleNamespace

import pytest

from framework.api.schemas import ApprovalRequest
from framework.core.turn_runner import TurnConflict
from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_turn_runner import make_runner, submit


@pytest.mark.parametrize("approve", [True, False])
def test_stale_card_cannot_decide_replacement(make_session, approve):
    registry = FakeRegistry([FakeTool("danger_op", approval=True, read_only=False,
                                      concurrency_safe=False)])
    runner = make_runner(FakeEngine([
        {"tool_calls": [call("danger_op", target="A")]},
        {"tool_calls": [call("danger_op", target="B")]},
        {"text": "done"},
    ]), registry)
    session = make_session()

    async def scenario():
        a, _ = await submit(runner, session, "fixture-session", prompt="A")
        b, _ = await submit(runner, session, "fixture-session", prompt="B")
        before = session.journal.turn_snapshot()
        with pytest.raises(TurnConflict, match="approval_target_mismatch"):
            await runner.accept_approval(session, "fixture-session", approve,
                                         expected_turn_id=a["turn_id"])
        assert session.journal.turn_snapshot() == before
        assert registry.executed == []
        await runner.accept_approval(session, "fixture-session", approve,
                                     expected_turn_id=b["turn_id"])
        await runner.run_approval(session, "fixture-session", approve)

    asyncio.run(scenario())
    assert registry.executed == ([{"name": "danger_op", "args": {"target": "B"}}] if approve else [])


def test_legacy_approval_request_remains_optional():
    assert ApprovalRequest(approve=True).expected_turn_id is None


async def _two_batches(session):
    registry = FakeRegistry([FakeTool("danger_op", approval=True, read_only=False,
                                      concurrency_safe=False)])
    runner = make_runner(FakeEngine([
        {"tool_calls": [call("danger_op", target="A")]},
        {"tool_calls": [call("danger_op", target="B")]},
        {"text": "done"},
    ]), registry)
    ctx, _ = await submit(runner, session, "fixture-session")
    first = next(e for e in session.journal.replay(0) if e["type"] == "awaiting_approval")
    # Another client legitimately approves A; our client never receives these SSE
    # updates and keeps A's card while the same turn reaches B's approval gate.
    await runner.accept_approval(session, "fixture-session", True,
                                 expected_turn_id=ctx["turn_id"])
    result = await runner.run_approval(session, "fixture-session", True)
    assert result["status"] == "pending_approval"
    second = [e for e in session.journal.replay(0) if e["type"] == "awaiting_approval"][-1]
    assert first["turn_id"] == second["turn_id"] and first["seq"] < second["seq"]
    assert registry.executed == [{"name": "danger_op", "args": {"target": "A"}}]
    return runner, registry, first, second


@pytest.mark.parametrize("approve", [True, False])
def test_same_turn_batch_binding_uses_latest_approval_not_last_event(make_session, approve):
    session = make_session()

    async def scenario():
        runner, registry, first, second = await _two_batches(session)
        session.journal.emit("tool_progress", {"label": "synthetic later event"})
        before = session.journal.turn_snapshot()
        with pytest.raises(TurnConflict, match="approval_target_mismatch"):
            await runner.accept_approval(session, "fixture-session", approve,
                                         expected_turn_id=first["turn_id"],
                                         expected_approval_seq=first["seq"])
        assert session.journal.turn_snapshot() == before
        assert len(registry.executed) == 1
        await runner.accept_approval(session, "fixture-session", approve,
                                     expected_turn_id=second["turn_id"],
                                     expected_approval_seq=second["seq"])
        await runner.run_approval(session, "fixture-session", approve)
        assert registry.executed == ([{"name": "danger_op", "args": {"target": "A"}},
                                      {"name": "danger_op", "args": {"target": "B"}}] if approve else
                                     [{"name": "danger_op", "args": {"target": "A"}}])

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["stale", "current", "legacy", "turn-only", "seq-only", "nullseq", "unknown", "badseq"])
def test_http_approval_batch_gate_and_compatibility(make_session, mode):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from framework.api.routes import router
    session = make_session()

    async def scenario():
        runner, registry, first, second = await _two_batches(session)
        app = FastAPI()  # No lifespan, services, real credentials or socket.
        app.include_router(router)
        app.state.turn_runner = runner
        app.state.session_manager = SimpleNamespace(get_or_create=lambda _: session)
        body = {"approve": True, "expected_turn_id": second["turn_id"]}
        if mode not in {"legacy", "turn-only"}:
            body["expected_approval_seq"] = first["seq"] if mode == "stale" else second["seq"]
        if mode == "legacy":
            body.pop("expected_turn_id")
        if mode == "seq-only":
            body.pop("expected_turn_id")
        if mode == "nullseq":
            body["expected_approval_seq"] = None
        if mode == "unknown":
            body["future_field"] = "ignored-compatible-field"
        if mode == "badseq":
            body["expected_approval_seq"] = "not-a-sequence"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.invalid") as client:
            response = await client.post("/api/v1/chat/fixture-session/turns/approval", json=body)
        if mode in {"stale", "badseq"}:
            assert response.status_code == (409 if mode == "stale" else 422)
            if mode == "stale":
                assert response.json()["reason"] == "approval_target_mismatch"
            assert len(registry.executed) == 1
            assert session.journal.turn_state == "awaiting_approval"
        else:
            assert response.status_code == 202
            task = runner._bg.get("fixture-session")
            if task:
                await task
            assert registry.executed[-1]["args"] == {"target": "B"}

    asyncio.run(scenario())


def test_approval_batch_is_rechecked_after_waiting_for_session_lock(make_session):
    session = make_session()

    async def scenario():
        runner, registry, _, second = await _two_batches(session)
        await session.lock.acquire()
        decision = asyncio.create_task(runner.accept_approval(
            session, "fixture-session", True, expected_turn_id=second["turn_id"],
            expected_approval_seq=second["seq"]))
        try:
            await asyncio.sleep(0)  # Decision observed awaiting state and waits for the lock.
            assert not decision.done()
            session.journal.emit("resumed", {"trigger": "approve"})
            session.journal.emit("turn_started", {})
            session.pending_tool_calls = [call("danger_op", target="C")]
            session.journal.emit("awaiting_approval", {"pending_tools": [{"name": "danger_op", "arguments": {"target": "C"}}]})
        finally:
            session.lock.release()
        with pytest.raises(TurnConflict, match="approval_target_mismatch"):
            await asyncio.wait_for(decision, timeout=1)
        assert registry.executed == [{"name": "danger_op", "args": {"target": "A"}}]

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, -1, True, 3.5, "3"])
def test_approval_sequence_requires_positive_integer(value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ApprovalRequest(approve=True, expected_approval_seq=value)


def test_journal_approval_identity_tracks_only_approval_lifecycle(tmp_path):
    from framework.core.journal import TurnJournal
    journal = TurnJournal(tmp_path)
    assert journal.approval_seq is None
    for transition in ("resumed", "turn_started", "suspended", "turn_ended", "turn_queued"):
        approval = journal.emit("awaiting_approval", {"pending_tools": [{"name": "danger_op"}]})
        assert journal.approval_seq == approval["seq"]
        journal.emit("token_delta", {"text": "progress"})
        assert journal.approval_seq == approval["seq"]
        journal.emit(transition, {})
        assert journal.approval_seq is None
