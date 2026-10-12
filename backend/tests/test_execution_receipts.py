"""R16: existing journal facts survive model-context rollback; CPU/FakeEngine only."""
import json
from pathlib import Path

import pytest

from framework.api.schemas import ChatResponse
from framework.core.journal import TurnJournal, new_turn_id
from framework.core.turn_runner import public_turn

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_resume_budget import PausingEngine
from .test_turn_runner import history_lines, make_runner, run, submit


class WritingRegistry(FakeRegistry):
    def __init__(self, path, *, approval=False):
        super().__init__([FakeTool("write_probe", read_only=False, concurrency_safe=False,
                                  approval=approval)])
        self.path = path

    def execute_tool(self, name, arguments):
        result = super().execute_tool(name, arguments)
        self.path.write_text("effect remains", encoding="utf-8")
        return (f"Tool returned after writing {self.path.name}", result[1])


def _terminal_receipts(session, result=None):
    disk = history_lines(session)[-1]
    ended = [e for e in session.journal.ring if e["type"] == "turn_ended"][-1]["data"]
    assert disk["execution_receipts"] == ended["execution_receipts"]
    if result is not None:
        assert result["execution_receipts"] == disk["execution_receipts"]
    return disk["execution_receipts"]


@pytest.mark.parametrize("terminal", ["cancelled", "fail"])
def test_written_effect_survives_context_rollback_and_receipts_land(make_session, tmp_path, terminal):
    class TerminalEngine(FakeEngine):
        async def generate_once(self, session, **kwargs):
            if self.calls and terminal == "fail":
                raise RuntimeError("synthetic failure after write")
            return await super().generate_once(session, **kwargs)

    engine = TerminalEngine([{"tool_calls": [call("write_probe")]}, {"cancelled": True}])
    target = tmp_path / "effect.txt"
    registry = WritingRegistry(target)
    runner = make_runner(engine, registry)
    session = make_session()
    before = list(session.history)
    _, result = run(submit(runner, session, session.session_id))
    assert result["status"] == terminal
    assert target.read_text() == "effect remains"
    assert session.history == before
    assert len(registry.executed) == 1
    receipts = _terminal_receipts(session, result)
    assert [item["type"] for item in receipts["items"]] == ["tool_started", "tool_result"]
    assert receipts["items"][0]["name"] == "write_probe"
    assert "effect.txt" in receipts["items"][1]["detail"]
    assert receipts["items"][1]["status"] == "ok"  # only the existing return status
    assert receipts["truncated"] is False
    assert not session.inflight_path.exists()
    assert runner.scheduler.snapshot()["tasks"] == []
    # HTTP response model must preserve the optional canonical field.
    assert ChatResponse.model_validate(public_turn(result)).model_dump(exclude_none=True)["execution_receipts"] == receipts


@pytest.mark.parametrize("terminal", ["cancelled", "fail"])
def test_approval_then_repeated_suspend_keeps_earlier_facts(make_session, tmp_path, terminal):
    class TerminalEngine(PausingEngine):
        async def generate_once(self, session, **kwargs):
            if len(self.calls) == 3 and terminal == "fail":
                raise RuntimeError("synthetic continuation failure")
            return await super().generate_once(session, **kwargs)

    engine = TerminalEngine([
        {"tool_calls": [call("write_probe", i=1)]},
        {"tool_calls": [call("write_probe", i=2)]},
        {"tool_calls": [call("write_probe", i=3)]},
        {"cancelled": True}], pauses=(2, 3))
    registry = WritingRegistry(tmp_path / "effect.txt", approval=True)
    runner = make_runner(engine, registry)
    session = make_session()
    before = list(session.history)

    async def scenario():
        await submit(runner, session, session.session_id)
        # Each approved write starts execution; after second/third generation,
        # the pending approval is resolved before the safe suspension boundary.
        for _ in range(3):
            await runner.accept_approval(session, session.session_id, True)
            result = await runner.run_approval(session, session.session_id, True)
            if result["status"] == "suspended":
                await runner.accept_resume(session, session.session_id)
                result = await runner.run_resume(session, session.session_id)
        return result

    result = run(scenario())
    assert result["status"] == terminal
    assert session.history == before
    assert registry.path.exists() and len(registry.executed) == 3
    receipts = _terminal_receipts(session, result)
    assert len(receipts["items"]) == 6
    assert len({item["call_id"] for item in receipts["items"]}) == 3


def test_fresh_turn_does_not_mix_previous_execution(make_session, tmp_path):
    engine = FakeEngine([{"tool_calls": [call("write_probe")]}, {"cancelled": True},
                         {"text": "new answer"}])
    registry = WritingRegistry(tmp_path / "effect.txt")
    runner = make_runner(engine, registry)
    session = make_session()
    run(submit(runner, session, session.session_id))
    _, fresh = run(submit(runner, session, session.session_id, "new question"))
    assert fresh["status"] == "success"
    assert _terminal_receipts(session, fresh) == {"items": [], "truncated": False}
    assert len(registry.executed) == 1


def test_no_observed_tools_is_empty_evidence_not_a_side_effect_claim(make_session):
    runner = make_runner(FakeEngine([{"cancelled": True}]), FakeRegistry([]))
    session = make_session()
    _, result = run(submit(runner, session, session.session_id))
    assert _terminal_receipts(session, result) == {"items": [], "truncated": False}
    assert "no_side_effects" not in result["execution_receipts"]


def _journal(tmp_path):
    directory = tmp_path / "journal"
    directory.mkdir()
    journal = TurnJournal(directory)
    journal.emit("turn_queued", {"prompt": "test"}, turn_id=new_turn_id())
    return journal


def test_projection_filters_same_turn_without_disk_replay(tmp_path, monkeypatch):
    journal = _journal(tmp_path)
    journal.emit("tool_started", {"name": "foreign", "call_id": "other"}, turn_id="t_other")
    own = journal.emit("tool_started", {"name": "own", "call_id": "own"})
    monkeypatch.setattr(journal, "replay", lambda *a, **k: pytest.fail("must not replay disk"))
    receipts = journal.execution_receipts()
    assert [item["seq"] for item in receipts["items"]] == [own["seq"]]
    assert receipts["items"][0]["type"] == "tool_started"
    assert receipts["items"][0]["ts"] == own["ts"]
    assert "status" not in receipts["items"][0]  # started has no result


def test_projection_bounds_count_and_detail_and_marks_both(tmp_path):
    journal = _journal(tmp_path)
    for n in range(65):
        journal.emit("tool_result", {"name": "probe", "call_id": str(n),
                                    "response": "x" * 2001, "status": "ok"})
    receipts = journal.execution_receipts()
    assert len(receipts["items"]) == 64
    assert receipts["items"][0]["call_id"] == "1"
    assert all(len(item["detail"]) == 2000 and item["truncated"] is True
               for item in receipts["items"])
    assert receipts["truncated"] is True


def test_projection_preserves_exact_limits_and_existing_truncation(tmp_path):
    journal = _journal(tmp_path)
    for n in range(64):
        journal.emit("tool_result", {"name": "probe", "call_id": str(n),
                                    "response": "x" * 2000, "status": "timeout"})
    receipts = journal.execution_receipts()
    assert len(receipts["items"]) == 64 and receipts["truncated"] is False
    assert all("truncated" not in item and item["status"] == "timeout" for item in receipts["items"])
    journal.ring[-1]["data"]["truncated"] = True
    assert journal.execution_receipts()["truncated"] is True


def test_restart_missing_ring_returns_unknown_not_fabricated(tmp_path):
    journal = _journal(tmp_path)
    journal.emit("tool_started", {"name": "probe", "call_id": "one"})
    restarted = TurnJournal(journal.path.parent)
    assert restarted.execution_receipts() is None


def test_started_does_not_copy_tool_arguments(tmp_path):
    journal = _journal(tmp_path)
    journal.emit("tool_started", {"name": "probe", "call_id": "one",
                                  "args": {"large": "ARG-CANARY" * 10000}})
    receipts = journal.execution_receipts()
    receipt = receipts["items"][0]
    assert "ARG-CANARY" not in json.dumps(receipt)
    assert "args" not in receipt
    assert receipt["truncated"] is receipts["truncated"] is True
    assert receipt["detail"] == ""


def test_identifier_and_status_bounds_are_honest(tmp_path):
    journal = _journal(tmp_path)
    journal.emit("tool_result", {"name": "N" * 129, "call_id": "C" * 129,
                                "response": "returned", "status": "S" * 33})
    receipts = journal.execution_receipts()
    item = receipts["items"][0]
    assert len(item["name"]) == len(item["call_id"]) == 128
    assert len(item["status"]) == 32
    assert item["truncated"] is receipts["truncated"] is True


def test_missing_turn_start_does_not_claim_complete_evidence(tmp_path):
    journal = _journal(tmp_path)
    journal.emit("tool_result", {"name": "probe", "call_id": "one", "response": "returned"})
    journal.ring.pop(0)
    assert journal.execution_receipts() is None


def test_protocol_fixture_receipts_match_same_turn_projection(tmp_path):
    root = Path(__file__).resolve().parents[2]
    fixture = root / "protocol/fixtures/015-cancelled-execution-receipts.events.jsonl"
    events = [json.loads(line) for line in fixture.read_text().splitlines()]
    journal = _journal(tmp_path)
    journal.ring = events[:-1]
    journal.current_turn_id = events[-1]["turn_id"]
    assert journal.execution_receipts() == events[-1]["data"]["execution_receipts"]
