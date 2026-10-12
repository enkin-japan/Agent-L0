"""R12: 同一轮跨手动挂起/定时抢占的预算与指纹不能重置。CPU FakeEngine only."""
import asyncio
from types import SimpleNamespace

import pytest

from framework.agents import runtime
from framework.agents.sandbox_runtime import note_usage
from framework.core.scheduler import ClassPolicy, TaskScheduler
from framework.core.task_state import TaskState
from framework.core.turn_runner import TurnRunner
from framework.schedule.runner import ScheduleRunner

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_orchestrator_lifecycles import GEN, begin_turn, make_orch, run
from .test_turn_runner import history_lines, submit


class PausingEngine(FakeEngine):
    """生成结束前请求挂起；已提交工具后的迭代边界安全让出。"""
    def __init__(self, script, pauses=(1,)):
        super().__init__(script)
        self.pauses = set(pauses)

    async def generate_once(self, session, **kwargs):
        result = await super().generate_once(session, **kwargs)
        if len(self.calls) in self.pauses:
            session.suspend_requested = True
        return result


class UsageRegistry(FakeRegistry):
    def execute_tool(self, name, arguments):
        note_usage(runtime.get_usage_sink(), generations=2, generation_tokens=17)
        return super().execute_tool(name, arguments)


def _metrics(n):
    return {"generation_tokens": n, "token_breakdown": {"text": n},
            "spec_drafted": n + 1, "spec_accepted": n}


def test_manual_resume_preserves_two_iteration_fuse(make_session):
    engine = PausingEngine([{"tool_calls": [call("probe", i=1)]},
                            {"tool_calls": [call("probe", i=2)]},
                            {"text": "third generation must not happen"}])
    runner = TurnRunner(make_orch(engine, FakeRegistry([FakeTool("probe")]),
                                  max_total_iterations=2), TaskScheduler())
    session = make_session()

    async def scenario():
        _, first = await submit(runner, session, session.session_id)
        assert first["status"] == "suspended"
        await runner.accept_resume(session, session.session_id)
        return await runner.run_resume(session, session.session_id)

    result = run(scenario())
    assert len(engine.calls) == 2
    assert result["status"] == "warning"
    assert result["generations"] == 2
    assert history_lines(session)[0]["generations"] == 2


@pytest.mark.parametrize("budget, preemptions", [(2, 1), (3, 2)])
def test_schedule_preemption_preserves_iteration_fuse(make_session, budget, preemptions):
    """真实ClassPolicy一次/多次交互抢占，主预算与子用量保持整轮口径。"""
    session = make_session("schedule_test")

    async def scenario():
        started = [asyncio.Event() for _ in range(preemptions)]
        gates = [asyncio.Event() for _ in range(preemptions)]

        class GatedEngine(FakeEngine):
            async def generate_once(self, session, **kwargs):
                n = len(self.calls)
                if n < preemptions:
                    started[n].set()
                    await gates[n].wait()
                return await super().generate_once(session, **kwargs)

        engine = GatedEngine([{"tool_calls": [call("probe", i=i)]}
                              for i in range(budget)] + [{"text": "must not happen"}])
        scheduler = TaskScheduler(policy=ClassPolicy(), on_suspend_request=lambda sid:
                                  setattr(session, "suspend_requested", True))
        runner = ScheduleRunner(None, SimpleNamespace(get_or_create=lambda sid: session),
                                make_orch(engine, UsageRegistry([FakeTool("probe")]),
                                          max_total_iterations=budget), scheduler)
        record = {"status": "fail"}
        scheduled = asyncio.create_task(runner._execute_inner(
            {"id": "test", "name": "test", "prompt": "查询"}, record))
        for start, gate in zip(started, gates):
            await start.wait()
            interactive = asyncio.create_task(scheduler.acquire("interactive"))
            await asyncio.sleep(0)
            assert session.suspend_requested
            gate.set()
            await interactive
            await scheduler.release("interactive")
        await scheduled
        assert scheduler.snapshot()["tasks"] == []
        return engine, record

    engine, record = run(asyncio.wait_for(scenario(), timeout=5))
    assert len(engine.calls) == budget
    assert record["status"] == "warning"
    landed = history_lines(session)[0]
    assert landed["generations"] == budget
    assert landed["generation_tokens_turn"] == 20 * budget
    assert landed["sub_generations"] == 2 * budget
    assert landed["sub_generation_tokens"] == 17 * budget
    assert [e["type"] for e in session.journal.replay(0)].count("suspended") == preemptions


def test_repeated_suspension_carries_stats_once_and_resets_new_turn(make_session):
    engine = PausingEngine([
        {"tool_calls": [call("probe", i=1)], "metrics": _metrics(7)},
        {"tool_calls": [call("probe", i=2)], "metrics": _metrics(11)},
        {"text": "答案", "metrics": _metrics(13)},
        {"text": "新轮", "metrics": _metrics(5)}], pauses=(1, 2))
    orch = make_orch(engine, UsageRegistry([FakeTool("probe")]))
    session = make_session()
    session.add_user_message("查询")
    begin_turn(session)

    async def scenario():
        first = await orch.run_turn(session, journal=session.journal, **GEN)
        second = await orch.run_turn(session, journal=session.journal,
                                     keep_recent_calls=True, compact_history=False, **GEN)
        last = await orch.run_turn(session, journal=session.journal,
                                   keep_recent_calls=True, compact_history=False, **GEN)
        return first, second, last

    first, second, last = run(scenario())
    assert first["status"] == second["status"] == "suspended"
    assert first["metrics"]["generations"] == 1
    assert second["metrics"]["generations"] == 2
    m = last["metrics"]
    assert m["generations"] == 3
    assert m["generation_tokens_turn"] == 31
    assert m["token_breakdown"] == {"text": 31}
    assert m["spec_drafted"] == 34 and m["spec_accepted"] == 31
    assert m["sub_generations"] == 4 and m["sub_generation_tokens"] == 34
    session.add_user_message("新轮")
    begin_turn(session)
    fresh = run(orch.run_turn(session, journal=session.journal, **GEN))["metrics"]
    assert fresh["generations"] == 1 and fresh["generation_tokens_turn"] == 5
    assert "sub_generations" not in fresh


def test_sub_generation_fuse_survives_suspension(make_session):
    engine = PausingEngine([{"tool_calls": [call("dig", i=1)]},
                            {"tool_calls": [call("dig", i=2)]},
                            {"text": "不应生成"}])
    orch = make_orch(engine, UsageRegistry([FakeTool("dig")]), max_sub_generations=4)
    session = make_session()
    session.add_user_message("查询")
    begin_turn(session)
    first = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert first["status"] == "suspended"
    last = run(orch.run_turn(session, journal=session.journal,
                             keep_recent_calls=True, compact_history=False, **GEN))
    assert last["status"] == "warning" and len(engine.calls) == 2
    assert last["metrics"]["sub_generations"] == 4


@pytest.mark.parametrize("with_task", [False, True])
def test_manual_resume_keeps_duplicate_fingerprints(make_session, with_task):
    engine = PausingEngine([{"tool_calls": [call("probe", i=1)]},
                            {"tool_calls": [call("probe", i=1)]}, {"text": "答案"},
                            {"tool_calls": [call("probe", i=1)]}, {"text": "新轮"}])
    registry = FakeRegistry([FakeTool("probe")])
    runner = TurnRunner(make_orch(engine, registry), TaskScheduler())
    session = make_session()
    if with_task:
        session.task_state = TaskState(goal="查询")

    async def scenario():
        await submit(runner, session, session.session_id)
        await runner.accept_resume(session, session.session_id)
        await runner.run_resume(session, session.session_id)
        assert len(registry.executed) == 1
        await submit(runner, session, session.session_id, "新轮")

    run(scenario())
    assert len(registry.executed) == 2


def test_denied_call_fingerprint_survives_manual_suspend(make_session):
    engine = PausingEngine([{"tool_calls": [call("danger", i=1)]},
                            {"tool_calls": [call("probe")]},
                            {"tool_calls": [call("danger", i=1)]}, {"text": "答案"}],
                           pauses=(2,))
    registry = FakeRegistry([FakeTool("danger", approval=True), FakeTool("probe")])
    runner = TurnRunner(make_orch(engine, registry), TaskScheduler())
    session = make_session()

    async def scenario():
        await submit(runner, session, session.session_id)
        await runner.accept_approval(session, session.session_id, False)
        denied = await runner.run_approval(session, session.session_id, False)
        assert denied["status"] == "suspended"
        await runner.accept_resume(session, session.session_id)
        return await runner.run_resume(session, session.session_id)

    result = run(scenario())
    assert result["status"] == "success"
    assert [c["name"] for c in registry.executed] == ["probe"]
    assert any("Denied-call blocked" in str(m) for m in session.history)
    engine.script.append({"tool_calls": [call("danger", i=1)]})
    _, fresh = run(submit(runner, session, session.session_id, "新轮"))
    assert fresh["status"] == "pending_approval"  # 新轮不能继承旧轮拒批


def test_approval_preserves_no_task_fingerprints(make_session):
    """无计划批准调用与审批之前的指纹均保留；新轮次清零。"""
    engine = FakeEngine([{"tool_calls": [call("probe", i=1)]},
                         {"tool_calls": [call("danger", i=1)]},
                         {"tool_calls": [call("probe", i=1), call("danger", i=1)]},
                         {"text": "答案"},
                         {"tool_calls": [call("danger", i=1)]}, {"text": "新轮"}])
    registry = FakeRegistry([FakeTool("danger", approval=True), FakeTool("probe")])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("查询")
    begin_turn(session)

    async def scenario():
        first = await orch.run_turn(session, journal=session.journal, **GEN)
        assert first["status"] == "pending_approval"
        second = await orch.resume_after_approval(session, True, journal=session.journal, **GEN)
        assert second["status"] == "pending_approval"
        last = await orch.resume_after_approval(session, True, journal=session.journal, **GEN)
        assert last["status"] == "success"
        assert [c["name"] for c in registry.executed] == ["probe", "danger"]
        assert last["metrics"]["generations"] == 4
        session.add_user_message("新轮")
        begin_turn(session)
        await orch.run_turn(session, journal=session.journal, **GEN)
        await orch.resume_after_approval(session, True, journal=session.journal, **GEN)

    run(scenario())
    assert [c["name"] for c in registry.executed] == ["probe", "danger", "danger"]


def test_schedule_suspension_preserves_no_task_fingerprints(make_session):
    engine = PausingEngine([{"tool_calls": [call("probe", i=1)]},
                            {"tool_calls": [call("probe", i=1)]}, {"text": "答案"}])
    registry = FakeRegistry([FakeTool("probe")])
    session = make_session("schedule_test")
    runner = ScheduleRunner(None, SimpleNamespace(get_or_create=lambda sid: session),
                            make_orch(engine, registry), TaskScheduler())
    run(runner._execute_inner({"id": "test", "name": "test", "prompt": "查询"},
                              {"status": "fail"}))
    assert len(registry.executed) == 1
    assert history_lines(session)[0]["generations"] == 3
