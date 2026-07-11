"""编排层生命周期金测试(Phase 0 回归网)。

覆盖 Orchestrator.run_turn / resume_after_approval 的全部关键收尾路径与护栏行为。
这些断言捕获的是**必须保全的现状行为**——Phase 1 收敛 TurnRunner、改 emit 为账本
句柄时,本文件必须持续全绿(事件形态的断言届时按 PROTOCOL.md 扩展)。
"""
import asyncio

import pytest

from framework.core.orchestrator import Orchestrator

from .fake_engine import (EmitCollector, FakeEngine, FakeRegistry, FakeTool,
                          bad_call, call)

GEN = dict(temperature=0.2, min_p=0.05, enable_thinking=False,
           thinking_budget=2048, max_tokens=4096)


def make_orch(engine, registry, **cfg_over):
    cfg = {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
           "observation_max_chars": 4000, "tool_timeout_seconds": 5,
           "max_parse_retries": 3, "thinking_mode": "manual"}
    cfg.update(cfg_over)
    return Orchestrator(engine, registry, cfg)


def run(coro):
    return asyncio.run(coro)


# ---------- 基本收尾 ----------

def test_plain_text_success(make_session):
    engine = FakeEngine([{"text": "你好!", "thoughts": ["简单问候"]}])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("hi")
    emit = EmitCollector()
    streamed = []

    result = run(orch.run_turn(session, emit=emit, on_text_delta=streamed.append, **GEN))

    assert result["status"] == "success"
    assert result["response"] == "你好!"
    assert "".join(streamed) == "你好!"                      # 真流式回吐
    assert session.history[-1]["role"] == "assistant"
    assert session.history[-1]["content"] == "你好!"
    assert session.activity["phase"] == "done"
    assert emit.of("thinking") == [{"text": "简单问候"}]      # 思考成块下发(现状契约)


def test_tool_then_answer(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/tmp/x")]},
        {"text": "答案是 42。"},
    ])
    registry = FakeRegistry([FakeTool("read_thing", result="42")])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("x 是多少?")
    emit = EmitCollector()

    result = run(orch.run_turn(session, emit=emit, **GEN))

    assert result["status"] == "success"
    assert registry.executed == [{"name": "read_thing", "args": {"path": "/tmp/x"}}]
    # 历史结构:system(会话头)→ user → assistant(tool_calls) → tool → assistant(text)
    roles = [m["role"] for m in session.history]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    # 事件序:tool_started 先于 tool_result,且携带配对 call_id
    started, finished = emit.of("tool_started"), emit.of("tool_result")
    assert len(started) == len(finished) == 1
    assert started[0]["call_id"] == finished[0]["call_id"]
    assert finished[0]["status"] == "ok" and finished[0]["response"] == "42"
    assert emit.types().index("tool_started") < emit.types().index("tool_result")


# ---------- 审批闸门 ----------

def test_approval_gate_and_approve_resume(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("danger_op", target="prod")]},
        {"text": "已执行完毕。"},  # 决议批准后的续跑生成
    ])
    registry = FakeRegistry([FakeTool("danger_op", result="done",
                                      approval=True, read_only=False,
                                      concurrency_safe=False)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("执行危险操作")

    result = run(orch.run_turn(session, **GEN))

    assert result["status"] == "pending_approval"
    assert result["pending_tools"] == [{"name": "danger_op", "arguments": {"target": "prod"}}]
    assert registry.executed == []                       # 挂起期间未执行
    assert session.pending_tool_calls                    # 暂存待决议
    assert session.carried_iterations == 1               # 熔断分母跨续跑累计

    resumed = run(orch.resume_after_approval(session, approved=True, **GEN))

    assert resumed["status"] == "success"
    assert resumed["response"] == "已执行完毕。"
    assert registry.executed == [{"name": "danger_op", "args": {"target": "prod"}}]
    assert session.pending_tool_calls is None


def test_approval_deny_feeds_model(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("danger_op", target="prod")]},
        {"text": "好的,已取消该操作。"},
    ])
    registry = FakeRegistry([FakeTool("danger_op", approval=True, read_only=False,
                                      concurrency_safe=False)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("执行危险操作")
    run(orch.run_turn(session, **GEN))

    resumed = run(orch.resume_after_approval(session, approved=False, **GEN))

    assert resumed["status"] == "success"
    assert registry.executed == []                       # 拒绝 = 绝不执行
    tool_msgs = [m for m in session.history if m["role"] == "tool"]
    assert any("DENIED" in str(m) for m in tool_msgs)    # 拒绝结果反馈给模型


# ---------- 取消 / 挂起 ----------

def test_cancel_before_generation(make_session):
    engine = FakeEngine([])                              # 不该发生任何生成
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("hi")
    session.cancel_requested = True

    result = run(orch.run_turn(session, **GEN))

    assert result["status"] == "cancelled"
    assert engine.calls == []


def test_cancel_during_generation(make_session):
    engine = FakeEngine([{"cancelled": True}])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("hi")

    result = run(orch.run_turn(session, **GEN))

    assert result["status"] == "cancelled"


def test_suspend_at_iteration_boundary(make_session):
    engine = FakeEngine([{"tool_calls": [call("read_thing")]}])
    registry = FakeRegistry([FakeTool("read_thing")])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("做点事")

    async def scenario():
        # 第一迭代(工具执行)后置位挂起 → 循环在下一个迭代边界让出
        async def flag_after_first():
            while not registry.executed:
                await asyncio.sleep(0)
            session.suspend_requested = True
        task = asyncio.create_task(flag_after_first())
        r = await orch.run_turn(session, **GEN)
        await task
        return r

    result = run(scenario())

    assert result["status"] == "suspended"
    assert session.suspend_requested is False            # 标志被消费
    assert session.activity["phase"] == "suspended"
    assert len(engine.calls) == 1                        # 未再多转一圈生成


# ---------- 护栏:重复调用 / 解析断环 / 截断恢复 / 验证门控 ----------

def test_duplicate_call_blocked(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/a")]},
        {"tool_calls": [call("read_thing", path="/a")]},   # 同名同参复读
        {"text": "汇总完成。"},
    ])
    registry = FakeRegistry([FakeTool("read_thing", result="data")])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("看两眼")

    result = run(orch.run_turn(session, **GEN))

    assert result["status"] == "success"
    assert len(registry.executed) == 1                   # 第二次被拦,未真执行
    dup_feedback = [tr for m in session.history if m["role"] == "tool"
                    for tr in m["tool_responses"] if "Duplicate call blocked" in tr["response"]]
    assert len(dup_feedback) == 1


def test_write_barrier_allows_reread(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/a")]},
        {"tool_calls": [call("write_thing", path="/a")]},  # 写调用 → 解禁 r: 指纹
        {"tool_calls": [call("read_thing", path="/a")]},   # 读-改-再读:应放行
        {"text": "验证通过。"},
    ])
    registry = FakeRegistry([
        FakeTool("read_thing", result="v1"),
        FakeTool("write_thing", result="written", read_only=False, concurrency_safe=False),
    ])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("改后复查")

    result = run(orch.run_turn(session, **GEN))

    assert result["status"] == "success"
    assert [e["name"] for e in registry.executed] == ["read_thing", "write_thing", "read_thing"]


def test_parse_error_loop_aborts(make_session):
    engine = FakeEngine([{"tool_calls": [bad_call()]} for _ in range(3)])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("hi")

    result = run(orch.run_turn(session, **GEN))

    assert result["status"] == "warning"
    assert "解析" in result["response"]                   # 优雅终止而非烧光预算
    assert len(engine.calls) == 3                        # max_parse_retries 断环


def test_truncation_escalates_then_recovers(make_session):
    engine = FakeEngine([
        {"text": "前半截", "truncated": True},
        {"text": "完整的答案。"},
    ])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("长答复")

    result = run(orch.run_turn(session, **GEN))

    assert result["status"] == "success"
    assert result["response"] == "完整的答案。"
    # 升档重试:第二次生成的 max_tokens 被抬到 ESCALATED,半截产出被丢弃不入历史
    assert engine.calls[1]["kwargs"]["max_tokens"] > GEN["max_tokens"]
    assistant_texts = [m.get("content") for m in session.history if m["role"] == "assistant"]
    assert assistant_texts == ["完整的答案。"]


def test_verify_nudge_on_unverified_write(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("write_file", path="/a", content="x")]},
        {"text": "已修好。"},        # 写后未复查即收尾 → 推回一次
        {"text": "已修好并验证。"},  # 推回预算用尽 → 放行
    ])
    registry = FakeRegistry([FakeTool("write_file", result="ok",
                                      read_only=False, concurrency_safe=False)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("修一下")

    result = run(orch.run_turn(session, **GEN))

    assert result["status"] == "success"
    assert result["response"] == "已修好并验证。"
    # 推回经可变尾部注入(不进历史):第三次生成应看到验证提醒 tail_note
    assert any("verify" in n.lower() for n in engine.calls[2]["tail_notes"])


# ---------- 计划元工具与提前收尾门控 ----------

def test_plan_flow_and_premature_finish_gate(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("update_plan", op="replace", steps=["查资料", "写结论"])]},
        {"text": "先说到这。"},                                    # 计划未完 → 推回
        {"tool_calls": [call("complete_step", result="资料已查"),
                        call("complete_step", result="结论已写")]},
        {"text": "全部完成:结论如下。"},
    ])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("做个两步任务")
    emit = EmitCollector()

    result = run(orch.run_turn(session, emit=emit, **GEN))

    assert result["status"] == "success"
    assert result["response"] == "全部完成:结论如下。"
    assert session.task_state is None                    # 完成即弃(blackboard=消息流)
    # 提前收尾推回走 tail_note,第三次生成能看到
    assert any("unfinished steps" in n for n in engine.calls[2]["tail_notes"])
    # task_progress 事件里出现过含两步的计划快照
    snapshots = [d["task"] for d in emit.of("task_progress") if d.get("task")]
    assert any(len(s.get("plan", [])) == 2 for s in snapshots)


def test_total_iteration_fuse(make_session):
    engine = FakeEngine([{"tool_calls": [call("read_thing", i=i)]} for i in range(12)])
    registry = FakeRegistry([FakeTool("read_thing")])
    orch = make_orch(engine, registry, max_total_iterations=12)
    session = make_session()
    session.add_user_message("转圈")

    result = run(orch.run_turn(session, **GEN))

    assert result["status"] == "warning"
    assert "12" in result["response"]                    # 总熔断兜底,优雅收尾


# ---------- 并发只读批 ----------

def test_concurrent_readonly_batch_order_preserved(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/a"), call("read_thing", path="/b"),
                        call("write_thing", path="/c")]},
        {"text": "齐活。"},
    ])
    registry = FakeRegistry([
        FakeTool("read_thing", result="r"),
        FakeTool("write_thing", result="w", read_only=False, concurrency_safe=False),
    ])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("批量")

    result = run(orch.run_turn(session, **GEN))

    assert result["status"] == "success"
    # 写调用前必须先冲刷只读批:两个 read 都在 write 之前执行
    names = [e["name"] for e in registry.executed]
    assert names.index("write_thing") == 2
    # 响应与调用一一对应、按声明序回填
    tool_msg = next(m for m in session.history if m["role"] == "tool")
    assert [tr["name"] for tr in tool_msg["tool_responses"]] \
        == ["read_thing", "read_thing", "write_thing"]
