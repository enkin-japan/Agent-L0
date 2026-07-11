"""TurnRunner 生命周期金测试(Phase 1 验收核心)。

用真实 TaskScheduler + 真实 ChatSession(临时目录) + FakeEngine 驱动完整闭环,
断言三件事:返回值形状(旧契约兼容)、磁盘状态(history.jsonl / inflight / journal)、
账本事件序(PROTOCOL.md 生命周期)。routes 的新旧端点都是 TurnRunner 的薄适配器,
这里绿 = 两套契约的共同地基绿。
"""
import asyncio
import json

import pytest

from framework.core.orchestrator import Orchestrator
from framework.core.scheduler import ClassPolicy, TaskScheduler
from framework.core.turn_runner import TurnConflict, TurnRunner

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call

GEN = dict(temperature=0.2, min_p=0.05, enable_thinking=False,
           thinking_budget=2048, max_tokens=4096)


def make_runner(engine, registry):
    orch = Orchestrator(engine, registry, {
        "react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
        "observation_max_chars": 4000, "tool_timeout_seconds": 5,
        "max_parse_retries": 3, "thinking_mode": "manual"})
    return TurnRunner(orch, TaskScheduler(policy=ClassPolicy()))


async def submit(runner, session, sid, prompt="做点事"):
    ctx = await runner.accept_turn(
        session, sid, prompt=prompt, images=[], audios=[],
        ingested_text="", attachments=[], gen=dict(GEN))
    rd = await runner.run_accepted(session, sid, ctx)
    return ctx, rd


def journal_types(session):
    """账本事件类型序(重放口径:里程碑 + 活跃轮全量)。"""
    return [e["type"] for e in session.journal.replay(0)]


def history_lines(session):
    if not session.history_log.exists():
        return []
    return [json.loads(l) for l in
            session.history_log.read_text(encoding="utf-8").strip().split("\n") if l]


def run(coro):
    return asyncio.run(coro)


# ---------- 成功轮:落盘 + 账本 + 兼容形状 ----------

def test_success_turn_lands_and_journals(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/a")]},
        {"text": "答案。", "thoughts": ["想一下"]},
    ])
    runner = make_runner(engine, FakeRegistry([FakeTool("read_thing", result="42")]))
    session = make_session()

    ctx, rd = run(submit(runner, session, "t-session", prompt="查一下"))

    assert rd["status"] == "success"
    assert rd["prompt"] == "查一下"
    assert [m["role"] for m in rd["response"]] == ["assistant", "tool", "assistant"]
    # 磁盘:落盘一轮,inflight 撤销
    lines = history_lines(session)
    assert len(lines) == 1 and lines[0]["status"] == "success"
    assert not session.inflight_path.exists()
    # 账本:完整生命周期 + 过程事件,turn_ended 带 turn_index
    types = journal_types(session)
    assert types[0:2] == ["turn_queued", "turn_started"]
    for t in ("iteration_started", "tool_started", "tool_result",
              "thinking_delta", "token_delta"):
        assert t in types
    assert types[-1] == "turn_ended"
    ended = session.journal.replay(0)[-1]
    assert ended["data"]["turn_index"] == 0
    assert ended["turn_id"] == ctx["turn_id"]
    assert session.journal.turn_state == "none"
    # 调度器无泄漏
    assert runner.scheduler.snapshot()["tasks"] == []


def test_conflict_while_active(make_session):
    engine = FakeEngine([{"text": "ok"}])
    runner = make_runner(engine, FakeRegistry([]))
    session = make_session()

    async def scenario():
        ctx = await runner.accept_turn(session, "t-session", prompt="第一条",
                                       images=[], audios=[], ingested_text="",
                                       attachments=[], gen=dict(GEN))
        with pytest.raises(TurnConflict) as ei:
            await runner.accept_turn(session, "t-session", prompt="插队",
                                     images=[], audios=[], ingested_text="",
                                     attachments=[], gen=dict(GEN))
        assert ei.value.reason == "turn_active"
        await runner.run_accepted(session, "t-session", ctx)

    run(scenario())


def test_conflict_rejects_instantly_while_generating(make_session):
    """回归(实机发现):运行中的轮次持 session.lock 数分钟,受理冲突必须锁外即拒,
    否则后续提交排队等锁、409 永远打不出来,断连的请求还会在锁释放后复活执行。"""
    gate = asyncio.Event()
    engine = FakeEngine([{"text": "慢答案", "gate": gate}])
    runner = make_runner(engine, FakeRegistry([]))
    session = make_session()

    async def scenario():
        ctx = await runner.accept_turn(session, "t-session", prompt="慢任务",
                                       images=[], audios=[], ingested_text="",
                                       attachments=[], gen=dict(GEN))
        bg = asyncio.create_task(runner.run_accepted(session, "t-session", ctx))
        while session.journal.turn_state != "running":   # 等到生成真正开始(持锁中)
            await asyncio.sleep(0)
        # 生成中提交/决议/恢复:必须在有限时间内 409,而不是卡在锁上
        with pytest.raises(TurnConflict):
            await asyncio.wait_for(
                runner.accept_turn(session, "t-session", prompt="插队",
                                   images=[], audios=[], ingested_text="",
                                   attachments=[], gen=dict(GEN)),
                timeout=1.0)
        with pytest.raises(TurnConflict):
            await asyncio.wait_for(runner.accept_approval(session, "t-session", True), timeout=1.0)
        with pytest.raises(TurnConflict):
            await asyncio.wait_for(runner.accept_resume(session, "t-session"), timeout=1.0)
        gate.set()
        rd = await bg
        assert rd["status"] == "success"

    run(scenario())
    assert [l["status"] for l in history_lines(session)] == ["success"]  # 只落一轮


# ---------- 审批:暂存不落盘 → 决议续跑合并落盘 ----------

def test_approval_pending_then_approve(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("danger_op", target="prod")]},
        {"text": "执行完毕。"},
    ])
    registry = FakeRegistry([FakeTool("danger_op", result="done", approval=True,
                                      read_only=False, concurrency_safe=False)])
    runner = make_runner(engine, registry)
    session = make_session()

    async def scenario():
        _, rd = await submit(runner, session, "t-session", prompt="危险操作")
        assert rd["status"] == "pending_approval"
        assert history_lines(session) == []              # 暂存不落盘
        assert session.inflight_path.exists()            # 预写标记保留(崩溃防丢)
        snap = session.journal.turn_snapshot()
        assert snap["state"] == "awaiting_approval"
        assert snap["pending_tools"] == [{"name": "danger_op", "arguments": {"target": "prod"}}]

        await runner.accept_approval(session, "t-session", True)
        rd2 = await runner.run_approval(session, "t-session", True)
        assert rd2["status"] == "success"
        assert [m["role"] for m in rd2["response"]] == ["tool", "assistant"]  # 仅增量
        return rd2

    run(scenario())
    lines = history_lines(session)
    assert len(lines) == 1 and lines[0]["status"] == "success"
    assert not session.inflight_path.exists()
    # 落盘轮次含完整消息流(含审批前的 assistant tool_calls)
    assert [m["role"] for m in lines[0]["response"]] == ["assistant", "tool", "assistant"]
    assert registry.executed == [{"name": "danger_op", "args": {"target": "prod"}}]
    # 账本:awaiting → resumed(approve) → turn_started → 工具事件 → turn_ended
    types = journal_types(session)
    i_await = types.index("awaiting_approval")
    assert "resumed" in types[i_await:]
    assert types[-1] == "turn_ended"
    # 批准的敏感调用事件不丢(修复验收)
    assert "tool_started" in types[i_await:] and "tool_result" in types[i_await:]


def test_approval_deny_lands_without_execution(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("danger_op", target="prod")]},
        {"text": "已按拒绝调整。"},
    ])
    registry = FakeRegistry([FakeTool("danger_op", approval=True,
                                      read_only=False, concurrency_safe=False)])
    runner = make_runner(engine, registry)
    session = make_session()

    async def scenario():
        await submit(runner, session, "t-session")
        await runner.accept_approval(session, "t-session", False)
        return await runner.run_approval(session, "t-session", False)

    rd = run(scenario())
    assert rd["status"] == "success"
    assert registry.executed == []
    ended = [e for e in session.journal.replay(0) if e["type"] == "resumed"]
    assert ended[0]["data"]["trigger"] == "deny"


def test_approval_conflict_when_none_pending(make_session):
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    session = make_session()
    with pytest.raises(TurnConflict) as ei:
        run(runner.accept_approval(session, "t-session", True))
    assert ei.value.reason == "no_pending_approval"


# ---------- 挂起 → 恢复 ----------

def test_suspend_then_resume(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("read_thing")]},
        {"text": "续跑完成。"},
    ])
    registry = FakeRegistry([FakeTool("read_thing")])
    runner = make_runner(engine, registry)
    session = make_session()

    async def scenario():
        ctx = await runner.accept_turn(session, "t-session", prompt="长任务",
                                       images=[], audios=[], ingested_text="",
                                       attachments=[], gen=dict(GEN))

        async def flag():
            while not registry.executed:
                await asyncio.sleep(0)
            session.suspend_requested = True
        flag_task = asyncio.create_task(flag())
        rd = await runner.run_accepted(session, "t-session", ctx)
        await flag_task
        assert rd["status"] == "suspended"
        assert history_lines(session) == []                       # 暂存不落盘
        assert session.journal.turn_state == "suspended"
        assert runner.scheduler.status_of("t-session") == "suspended"  # 句柄保留可恢复

        await runner.accept_resume(session, "t-session")
        rd2 = await runner.run_resume(session, "t-session")
        assert rd2["status"] == "success"
        assert rd2["response"][-1]["content"] == "续跑完成。"

    run(scenario())
    lines = history_lines(session)
    assert len(lines) == 1 and lines[0]["status"] == "success"
    assert session.journal.turn_state == "none"
    assert runner.scheduler.snapshot()["tasks"] == []


def test_resume_conflict_when_none_suspended(make_session):
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    session = make_session()
    with pytest.raises(TurnConflict) as ei:
        run(runner.accept_resume(session, "t-session"))
    assert ei.value.reason == "no_suspended_turn"


# ---------- 暂停态被新消息放弃 ----------

def test_new_message_abandons_pending_approval(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("danger_op")]},   # 第一轮:挂审批
        {"text": "新一轮答复。"},               # 第二轮
    ])
    registry = FakeRegistry([FakeTool("danger_op", approval=True,
                                      read_only=False, concurrency_safe=False)])
    runner = make_runner(engine, registry)
    session = make_session()

    async def scenario():
        await submit(runner, session, "t-session", prompt="危险操作")
        # 直接发新消息 = 放弃审批:旧轮 warning 落盘,新轮照常受理执行
        _, rd2 = await submit(runner, session, "t-session", prompt="换个方向")
        assert rd2["status"] == "success"

    run(scenario())
    lines = history_lines(session)
    assert [l["status"] for l in lines] == ["warning", "success"]
    assert registry.executed == []                      # 被放弃的敏感调用永不执行
    assert session.pending_tool_calls is None
    # 账本:旧轮 turn_ended(warning) 在新轮 turn_queued 之前
    events = [(e["type"], e["data"].get("status")) for e in session.journal.replay(0)
              if e["type"] in ("turn_ended", "turn_queued")]
    # 重放视角:旧轮里程碑已在磁盘,新轮在环——序为 queued(旧)…ended(旧, warning)…queued(新)…ended(新)
    statuses = [s for t, s in events if t == "turn_ended"]
    assert statuses == ["warning", "success"]


# ---------- 取消与故障回滚 ----------

def test_cancel_rolls_back_and_archives(make_session):
    engine = FakeEngine([{"cancelled": True}])
    runner = make_runner(engine, FakeRegistry([]))
    session = make_session()
    mem_before = len(session.history)

    _, rd = run(submit(runner, session, "t-session", prompt="马上取消"))

    assert rd["status"] == "cancelled"
    assert len(session.history) == mem_before            # 用户消息整轮回滚出内存
    lines = history_lines(session)
    assert len(lines) == 1 and lines[0]["status"] == "cancelled"  # 存档保留
    assert not session.inflight_path.exists()
    assert journal_types(session)[-1] == "turn_ended"


def test_engine_crash_lands_fail_turn(make_session):
    class BoomEngine:
        async def generate_once(self, *a, **k):
            raise RuntimeError("boom")

    runner = make_runner(BoomEngine(), FakeRegistry([]))
    session = make_session()
    mem_before = len(session.history)

    _, rd = run(submit(runner, session, "t-session", prompt="会炸"))

    assert rd["status"] == "fail"
    assert "boom" in rd["response"][0]["content"]
    assert len(session.history) == mem_before            # 内存记忆无污染
    lines = history_lines(session)
    assert len(lines) == 1 and lines[0]["status"] == "fail"
    assert not session.inflight_path.exists()
    assert runner.scheduler.snapshot()["tasks"] == []    # 门闸正常释放
