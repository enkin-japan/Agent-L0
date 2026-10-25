"""Readiness does not consume due schedules; shutdown archives cooperative cancels."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from framework.core.orchestrator import Orchestrator
from framework.core.scheduler import TaskScheduler
from framework.memory.session import SessionManager
from framework.schedule.runner import ScheduleRunner
from framework.schedule.store import ScheduleStore

from .fake_engine import FakeEngine, FakeRegistry


TASK = {"id": "test", "name": "test", "prompt": "scheduled task", "enabled": True, "next_run": 0}


def test_due_task_waits_for_readiness_without_advancing_or_duplicate_launch():
    async def scenario():
        control = SimpleNamespace(ready=False, advanced=0, executions=0)
        def advance(sid):
            control.advanced += 1
            TASK["next_run"] = 10**20
        store = SimpleNamespace(list_tasks=lambda: [dict(TASK, next_run=0) if not control.advanced else dict(TASK)],
                                advance_after_fire=advance)
        runner = ScheduleRunner(store, None, None, None, can_execute=lambda: control.ready)
        runner.tick_seconds = 0.005
        gate = asyncio.Event()
        async def execute(task, at):
            control.executions += 1
            await gate.wait()
            runner.running.discard(task["id"])
        runner._execute = execute
        tick = asyncio.create_task(runner.tick_loop())
        await asyncio.sleep(0.02)
        assert not control.advanced and not control.executions
        assert not runner.launch(TASK, 0)
        control.ready = True
        while not control.executions:
            await asyncio.sleep(0.001)
        assert control.advanced == control.executions == 1
        assert not runner.launch(TASK, 0)
        await asyncio.sleep(0.02)
        assert control.advanced == control.executions == 1
        gate.set()
        await asyncio.gather(*list(runner._tasks))
        tick.cancel()
        await tick
        await runner.stop()
        assert not runner._tasks
    asyncio.run(scenario())


def test_stop_settles_real_scheduled_turn_and_releases_scheduler(tmp_path):
    class CancellableEngine(FakeEngine):
        async def generate_once(self, session, **kwargs):
            entered.set()
            while not session.cancel_requested:
                await asyncio.sleep(0)
            return await super().generate_once(session, **kwargs)
    async def scenario():
        nonlocal entered
        entered = asyncio.Event()
        manager = SessionManager(tmp_path / "sessions")
        scheduler = TaskScheduler()
        engine = CancellableEngine([{"cancelled": True}])
        orch = Orchestrator(engine, FakeRegistry([]), {"cloud": {}, "sediment_to_rag": False})
        store = ScheduleStore(tmp_path / "schedules")
        runner = ScheduleRunner(store, manager, orch, scheduler)
        assert runner.launch(TASK, 0)
        await entered.wait()
        await runner.stop()
        history = manager.get_history("schedule_test")
        assert len(history) == 1 and history[0]["status"] == "cancelled"
        session = manager._all_sessions["schedule_test"]
        assert not session.inflight_path.exists()
        assert session.journal.ring[-1]["type"] == "turn_ended"
        assert scheduler.snapshot()["tasks"] == []
        assert not runner.running and not runner._tasks
        assert not runner.launch(TASK, 0)
    entered = None
    asyncio.run(scenario())


def test_idle_maintenance_waits_for_compute_availability():
    from app import _idle_maintenance_loop
    async def scenario():
        control = SimpleNamespace(ready=False, reads=0)
        entered = asyncio.Event()
        class Manager:
            @property
            def _all_sessions(self):
                control.reads += 1
                entered.set()
                raise RuntimeError("synthetic stop before any maintenance work")
        worker = asyncio.create_task(_idle_maintenance_loop(
            Manager(), None, SimpleNamespace(summary_idle_seconds=0), interval=0.005,
            can_execute=lambda: control.ready))
        await asyncio.sleep(0.02)
        assert control.reads == 0
        control.ready = True
        await asyncio.wait_for(entered.wait(), 1)
        worker.cancel()
        await worker
    asyncio.run(scenario())


@pytest.mark.parametrize("paused_kind", ["pending", "suspended"])
def test_stop_during_previous_turn_archive_does_not_accept_new_run(tmp_path, paused_kind):
    async def scenario():
        manager = SessionManager(tmp_path / "sessions")
        session = manager.get_or_create("schedule_test")
        scheduler = TaskScheduler()
        engine = FakeEngine([{"text": "unexpected new run"}])
        orch = Orchestrator(engine, FakeRegistry([]), {"cloud": {}, "sediment_to_rag": False})
        runs = []
        runner = ScheduleRunner(SimpleNamespace(append_run=lambda sid, run: runs.append(dict(run))),
                                manager, orch, scheduler)
        old_turn = {"status": "awaiting_approval", "prompt": "old", "response": []}
        if paused_kind == "pending":
            session.pending_tool_calls = [{"name": "tool", "arguments": {}}]
            session.pending_turn = old_turn
        else:
            session.suspended_turn = old_turn
        entered, release = threading.Event(), threading.Event()
        append_log = session.append_log

        def held_append(record):
            if not entered.is_set():
                entered.set()
                assert release.wait(5), "test failed to release archive"
            return append_log(record)

        session.append_log = held_append
        assert runner.launch(TASK, 0)
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            stopping = asyncio.create_task(runner.stop())
            await asyncio.sleep(0)
            assert runner._stopping and session.cancel_requested
        finally:
            release.set()
        await asyncio.wait_for(stopping, 5)
        assert not engine.calls, "shutdown must not accept a new generation after archive"
        assert not any(message.get("role") == "user" for message in session.history)
        assert not session.inflight_path.exists()
        history = manager.get_history("schedule_test")
        assert len(history) == 1 and history[0]["prompt"] == "old"
        assert history[0]["status"] == "warning"
        assert runs[0]["status"] == "cancelled"
        assert scheduler.snapshot()["tasks"] == []
        assert not runner.running and not runner._tasks

    asyncio.run(scenario())
