"""收尾目标校验金测试（DEFERRED §2 完整版：任务级独立审计，防虚报完成）。

不变量：计划≥3步全完成后的最终文本触发一次审计生成；FAIL 推回自纠（tail_note，
预算 1）；PASS 放行；小计划(<3步)与关开关不触发；审计输入携带原始 goal。
"""
from .fake_engine import FakeEngine, FakeRegistry, call
from .test_orchestrator_lifecycles import GEN, begin_turn, make_orch, run

PLAN3 = {"tool_calls": [call("update_plan", op="replace", steps=["查A", "查B", "汇总"])]}
DONE3 = {"tool_calls": [call("complete_step", result="A=1"),
                        call("complete_step", result="B=2"),
                        call("complete_step", result="汇总=3")]}


def test_goal_verify_fail_pushback_then_fixed(make_session):
    engine = FakeEngine([
        PLAN3, DONE3,
        {"text": "全都做完了。"},                       # 虚报形态 → 触发审计
        {"text": "VERDICT: FAIL\nno concrete values in the answer"},   # 审计生成
        {"text": "A=1，B=2，汇总=3。"},                  # 推回后自纠（预算已尽，直接放行）
    ])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("查 A 和 B 并汇总数值")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert result["response"] == "A=1，B=2，汇总=3。"
    assert len(engine.calls) == 5                       # 主循环 4 次 + 审计 1 次
    # 审计输入是一次性会话替身：携带原始 goal 与被审答复，不带主历史
    audit = engine.calls[3]
    assert "Goal:" in audit["last_user"] and "查 A 和 B" in audit["last_user"]
    assert audit["task_block"] is None                  # 审计生成不渲染任务块
    # 推回经可变尾部注入：自纠那次生成能看到审计理由
    assert any("completion audit" in n for n in engine.calls[4]["tail_notes"])
    assert any("no concrete values" in n for n in engine.calls[4]["tail_notes"])
    assert session.task_state is None                   # 最终收尾照常弃任务


def test_goal_verify_pass_accepts(make_session):
    engine = FakeEngine([
        PLAN3, DONE3,
        {"text": "A=1，B=2，汇总=3。"},
        {"text": "VERDICT: PASS\ncomplete"},            # 审计通过 → 原答复放行
    ])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("查 A 和 B 并汇总数值")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert result["response"] == "A=1，B=2，汇总=3。"
    assert len(engine.calls) == 4                       # 审计恰好 1 次，无推回


def test_goal_verify_skips_small_plan(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("update_plan", op="replace", steps=["查", "答"])]},
        {"tool_calls": [call("complete_step", result="x=1"),
                        call("complete_step", result="答完")]},
        {"text": "x=1。"},
    ])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("小任务")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert len(engine.calls) == 3                       # <3 步：不触发审计


def test_goal_verify_config_off(make_session):
    engine = FakeEngine([PLAN3, DONE3, {"text": "全都做完了。"}])
    orch = make_orch(engine, FakeRegistry([]), goal_verify=False)
    session = make_session()
    session.add_user_message("查 A 和 B 并汇总数值")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert len(engine.calls) == 3                       # 开关关闭：零审计成本
