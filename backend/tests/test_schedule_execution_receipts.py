"""R16: scheduled archive paths preserve the same existing journal projection."""
from types import SimpleNamespace

import pytest

from framework.core.scheduler import TaskScheduler
from framework.core.turn_runner import TurnRunner, public_turn
from framework.schedule.runner import ScheduleRunner

from .fake_engine import FakeEngine, FakeTool, call
from .test_execution_receipts import WritingRegistry
from .test_orchestrator_lifecycles import make_orch, run
from .test_resume_budget import PausingEngine
from .test_turn_runner import history_lines, submit


def _schedule_runner(session, engine, registry, scheduler=None):
    return ScheduleRunner(None, SimpleNamespace(get_or_create=lambda sid: session),
                          make_orch(engine, registry), scheduler or TaskScheduler())


TASK = {"id": "test", "name": "test", "prompt": "write and report"}


@pytest.mark.parametrize("terminal", ["cancelled", "fail", "success"])
def test_scheduled_write_then_terminal_preserves_facts(make_session, tmp_path, terminal):
    class TerminalEngine(FakeEngine):
        async def generate_once(self, session, **kwargs):
            if self.calls and terminal == "fail":
                raise RuntimeError("synthetic scheduled failure after write")
            return await super().generate_once(session, **kwargs)

    engine = TerminalEngine([{"tool_calls": [call("write_probe")]},
                             {"cancelled": True} if terminal == "cancelled" else {"text": "answer"}])
    registry = WritingRegistry(tmp_path / "effect.txt")
    session = make_session("schedule_test")
    before = list(session.history)
    runner = _schedule_runner(session, engine, registry)
    record = {"status": "fail"}
    run(runner._execute_inner(TASK, record))
    assert record["status"] == terminal
    assert registry.path.read_text() == "effect remains"
    assert len(registry.executed) == 1
    assert (session.history == before) is (terminal != "success")
    disk = history_lines(session)[0]
    terminal_event = session.journal.ring[-1]
    assert terminal_event["type"] == "turn_ended"
    assert disk["execution_receipts"] == terminal_event["data"]["execution_receipts"]
    items = disk["execution_receipts"]["items"]
    assert [item["type"] for item in items] == ["tool_started", "tool_result"]
    assert "effect.txt" in items[1]["detail"]
    assert items[1]["status"] == "ok"  # original return status only
    assert not session.inflight_path.exists()
    assert runner.scheduler.snapshot()["tasks"] == []


def test_scheduled_approval_autodeny_never_executes_or_fabricates_facts(make_session, tmp_path):
    engine = FakeEngine([{"tool_calls": [call("write_probe")]}, {"text": "acknowledged denial"}])
    registry = WritingRegistry(tmp_path / "must-not-exist.txt", approval=True)
    session = make_session("schedule_test")
    runner = _schedule_runner(session, engine, registry)
    record = {"status": "fail"}
    run(runner._execute_inner(TASK, record))
    assert record["status"] == "denied_approval"
    assert not registry.path.exists() and registry.executed == []
    disk = history_lines(session)[0]
    assert disk["status"] == "success"
    assert disk["execution_receipts"] == {"items": [], "truncated": False}
    assert session.journal.ring[-1]["data"]["execution_receipts"] == disk["execution_receipts"]
    types = [event["type"] for event in session.journal.ring]
    assert "awaiting_approval" in types and "resumed" in types
    assert "tool_started" not in types and runner.scheduler.snapshot()["tasks"] == []


@pytest.mark.parametrize("paused", ["suspended", "pending_approval"])
def test_new_scheduled_run_preserves_abandoned_paused_facts(make_session, tmp_path, monkeypatch, paused):
    engine = PausingEngine([{"tool_calls": [call("write_probe")]},
                            {"tool_calls": [call("danger")]}, {"text": "new answer"}],
                           pauses=(1,) if paused == "suspended" else ())
    registry = WritingRegistry(tmp_path / "effect.txt")
    registry._tools["danger"] = FakeTool("danger", approval=True)
    session = make_session("schedule_test")
    scheduler = TaskScheduler()
    turn_runner = TurnRunner(make_orch(engine, registry), scheduler)
    # Capture both terminal payloads before turn_queued clears the old turn's ring.
    terminals = []
    original_emit = session.journal.emit

    def observe_emit(etype, data=None, turn_id=None):
        env = original_emit(etype, data, turn_id)
        if etype == "turn_ended":
            terminals.append(env["data"])
        return env

    monkeypatch.setattr(session.journal, "emit", observe_emit)

    async def scenario():
        _, first = await submit(turn_runner, session, session.session_id)
        assert first["status"] == paused
        # The new scheduled run abandons the earlier paused turn, without executing it.
        runner = _schedule_runner(session, FakeEngine([{"text": "new answer"}]), registry, scheduler)
        await runner._execute_inner(TASK, {"status": "fail"})

    run(scenario())
    old, fresh = history_lines(session)
    assert old["status"] == "warning" and registry.path.exists()
    assert [item["type"] for item in old["execution_receipts"]["items"]] == [
        "tool_started", "tool_result"]
    assert terminals[0]["execution_receipts"] == old["execution_receipts"]
    assert terminals[0] == {**public_turn(old), "turn_index": 0}
    assert fresh["execution_receipts"] == {"items": [], "truncated": False}
    assert terminals[1] == {**public_turn(fresh), "turn_index": 1}
    assert len(registry.executed) == 1 and scheduler.snapshot()["tasks"] == []
