"""build 85：熔断原因落进会话（A1）与预算将尽时强制作答（A2）金测试。

**现场**（诊断 diag/findings.md Q4）：一轮里模型连调 12 次 web_research，子调研生成累计
84 次撞上 max_sub_generations=80 熔断。熔断说明只放在 run_turn 返回值的 `response` 里，
turn_runner 落盘 / 下发只取 `session.persistable_messages(...)`——用户界面上什么都没看到，
也没有任何回答。

- A1：两处熔断出口（子调研生成 / 总迭代）的说明文字作为助手消息写进会话，经 turn_runner
  落盘与 turn_ended 下发都看得见。
- A2：子调研生成余量不足以再调研一次（`max_sub_generations - sub < forced_answer_sub_reserve`）
  或主循环只剩最后一次迭代时，做**一次**不给工具的强制作答生成（一次性尾注、不落盘）；
  成功即终点（success + metrics.forced_answer + turn_forced_answer 事件），失败落 A1 熔断出口。

全部走真实 run_turn（FakeEngine 回放生成、假调研工具经 usage sink 的真实交接通道记子生成）。
"""
import json

import pytest

from framework.core import events
from framework.core.orchestrator import (ESCALATED_MAX_TOKENS, FORCED_ANSWER_FAILURE_NOTE,
                                         FORCED_ANSWER_NOTE)
from framework.core.scheduler import TaskScheduler
from framework.core.turn_runner import TurnRunner, public_turn
from framework.agents import runtime as agent_runtime
from framework.agents.sandbox_runtime import note_usage

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_orchestrator_lifecycles import GEN, begin_turn, make_orch, run
from .test_turn_runner import history_lines, submit

# 子调研生成熔断说明的两种开头：强制作答失败时尚未真到上限（77 < 80）→「接近」；真到上限 →「已达」
SUB_NEAR_PHRASE = "子调研生成接近预算上限（80 次）"
SUB_HIT_PHRASE = "子调研生成已达预算上限"
ANSWER = "根据已收集的材料：A 公司 2025 年营收 12 亿（未能确认季度拆分）。"


class ResearchRegistry(FakeRegistry):
    """假调研工具：每次执行经 ContextVar 取本轮用量槽，记 per_call 次子生成
    （与 web_research 子沙箱 6 步 + 1 次强制摘要同量级），走的是生产交接路径。
    fail_from：第 n 次（1 起）及之后的调研返回 `System Error: …`（搜索不可用，置 unresolved_failure）。"""

    def __init__(self, per_call=7, fail_from=None):
        super().__init__([FakeTool("fake_research"), FakeTool("probe"),
                          FakeTool("save_note", read_only=False, concurrency_safe=False)])
        self.per_call = per_call
        self.fail_from = fail_from
        self.research_calls = 0

    def execute_tool(self, name, arguments):
        if name == "fake_research":
            note_usage(agent_runtime.get_usage_sink(), generations=self.per_call,
                       generation_tokens=10 * self.per_call)
            self.research_calls += 1
            if self.fail_from is not None and self.research_calls >= self.fail_from:
                self.executed.append({"name": name, "args": dict(arguments)})
                return "System Error: search backend unavailable (SearXNG 503)", None
        return super().execute_tool(name, arguments)


def _research(i):
    return {"tool_calls": [call("fake_research", q=f"query {i}")]}


def _probe(i):
    return {"tool_calls": [call("probe", i=i)]}


def _events(etype):
    p = events.get_path()
    recs = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    return [e for e in recs if e["type"] == etype]


def _start(make_session, prompt="帮我查一下 A 公司的营收"):
    session = make_session()
    session.add_user_message(prompt)
    begin_turn(session)
    return session, len(session.history) - 1


def _last_assistant(messages):
    return [m for m in messages if m.get("role") == "assistant"][-1]


def _blob(messages):
    return json.dumps(messages, ensure_ascii=False, default=str)


# 强制作答失败的各种形态：仍吐工具调用 / 解析错误项 / 只有思考 / 空输出 / 复读打断
FORCED_FAILURES = {
    "tool_call": {"tool_calls": [call("fake_research", q="one more")]},
    # 有正文又带工具调用：正文不得被当成答复放行（变异 A11 的靶子——只有调用没有正文的两例
    # 会先被「正文为空」那一条挡下，杀不死「不看 tool_calls」的变异）
    "text_and_tool_call": {"text": "先说个大概，我再查一次。",
                           "tool_calls": [call("fake_research", q="one more")]},
    "parse_error": {"tool_calls": [{"type": "error", "raw_input": "<call>fake_research{",
                                    "error_msg": "unterminated"}]},
    "thinking_only": {"thoughts": ["我还想再查一次"]},
    "empty": {"text": "   "},
    "repetition": {"text": "答答答答", "repetition_guard": {"channel": "content",
                                                          "reason": "ngram"}},
}


# ==========================================================================
# A2-T1：子调研生成余量不足 → 恰好一次强制作答，成功即终点
# ==========================================================================

def test_sub_budget_reserve_forces_one_toolless_answer(make_session):
    """11 次调研 × 7 = 77，余量 3 < 7 → 第 12 次生成是强制作答：不给工具、带尾注；
    之后不再执行任何工具；回答落进会话、success、metrics/事件留痕；尾注不落盘。"""
    engine = FakeEngine([_research(i) for i in range(11)] + [{"text": ANSWER}])
    registry = ResearchRegistry(per_call=7)
    orch = make_orch(engine, registry, max_total_iterations=40, max_sub_generations=80)
    session, start = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 12                       # 强制作答之后没有第二次生成（含收尾审计）
    assert len(registry.executed) == 11                  # 强制作答之后没有再执行任何工具
    for c in engine.calls[:11]:                          # 前 11 次照常带工具、不带强制尾注
        assert c["n_tools"] > 0 and FORCED_ANSWER_NOTE not in c["tail_notes"]
    forced = engine.calls[11]
    assert forced["n_tools"] == 0 and forced["tools"] == []
    assert forced["grammar_tools"] == []                 # 约束语法也关掉
    assert forced["tail_notes"] == [FORCED_ANSWER_NOTE]        # 无失败：不追加失败句
    # F6：只此一次作答机会，上限升到 ESCALATED（缺省 generation_reserve 12288 不再往下钳）
    assert forced["kwargs"]["max_tokens"] == ESCALATED_MAX_TOKENS
    assert all(c["kwargs"]["max_tokens"] == GEN["max_tokens"] for c in engine.calls[:11])

    assert result["status"] == "success"                  # 无失败、无动作声明 → 不降级
    assert result["response"] == ANSWER
    assert result["metrics"]["forced_answer"] == "sub_generations"
    assert result["metrics"]["sub_generations"] == 77
    persisted = session.persistable_messages(start)
    assert _last_assistant(persisted)["content"] == ANSWER
    assert "tool budget for this turn is used up" not in _blob(persisted)
    assert "tool budget for this turn is used up" not in _blob(session.history)

    fe = _events("turn_forced_answer")
    assert len(fe) == 1
    assert fe[0]["detail"]["reason"] == "sub_generations"
    assert fe[0]["detail"]["sub_generations"] == 77
    assert fe[0]["detail"]["iterations"] == 12
    assert _events("turn_abort") == []


# ==========================================================================
# A2-T2：主循环只剩最后一次迭代 → 强制作答
# ==========================================================================

def test_last_iteration_forces_toolless_answer(make_session):
    engine = FakeEngine([_probe(i) for i in range(4)] + [{"text": ANSWER}])
    registry = ResearchRegistry()
    orch = make_orch(engine, registry, max_total_iterations=5)
    session, start = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 5
    assert all(c["n_tools"] > 0 for c in engine.calls[:4])
    assert engine.calls[4]["n_tools"] == 0
    assert engine.calls[4]["tail_notes"] == [FORCED_ANSWER_NOTE]
    assert len(registry.executed) == 4
    assert result["status"] == "success"
    assert result["metrics"]["forced_answer"] == "iterations"
    assert "sub_generations" not in result["metrics"]
    assert _last_assistant(session.persistable_messages(start))["content"] == ANSWER
    fe = _events("turn_forced_answer")
    assert len(fe) == 1 and fe[0]["detail"]["reason"] == "iterations"
    assert fe[0]["detail"]["iterations"] == 5


def test_forced_answer_skips_unfinished_plan_pushback(make_session):
    """强制作答就是终点：计划还有未完成步骤也不推回「继续执行剩余步骤」（否则又回工具循环）。"""
    engine = FakeEngine([
        {"tool_calls": [call("update_plan", op="replace", steps=["查A", "查B", "汇总"])]},
        _probe(1),
        {"text": ANSWER},
    ])
    orch = make_orch(engine, ResearchRegistry(), max_total_iterations=3)
    session, start = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 3
    assert engine.calls[2]["n_tools"] == 0
    assert result["status"] == "success"
    assert result["metrics"]["forced_answer"] == "iterations"
    assert _events("premature_finish_nudge") == []
    assert _last_assistant(session.persistable_messages(start))["content"] == ANSWER


# ==========================================================================
# A2 边界：余量恰好等于 reserve 不触发；按配置值判；reserve ≤ 0 关闭
# ==========================================================================

@pytest.mark.parametrize("reserve, third_forced", [(None, False), (7, False), (8, True), (0, False)])
def test_reserve_boundary_and_config(make_session, reserve, third_forced):
    """上限 21、每次调研 7：两次后 sub=14、余量 7。缺省/显式 7 → 余量 == reserve 不触发，
    第 3 次照常带工具；配成 8 → 余量 7 < 8，第 3 次就是强制作答；0 → 关闭。"""
    cfg = {"max_total_iterations": 40, "max_sub_generations": 21}
    if reserve is not None:
        cfg["forced_answer_sub_reserve"] = reserve
    script = [_research(1), _research(2)] + ([{"text": ANSWER}] if third_forced
                                              else [{"text": "结论"}])
    engine = FakeEngine(script)
    orch = make_orch(engine, ResearchRegistry(per_call=7), goal_verify=False, **cfg)
    session, _ = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 3
    assert result["status"] == "success"
    if third_forced:
        assert engine.calls[2]["n_tools"] == 0
        assert engine.calls[2]["tail_notes"] == [FORCED_ANSWER_NOTE]
        assert result["metrics"]["forced_answer"] == "sub_generations"
    else:
        assert engine.calls[2]["n_tools"] > 0
        assert FORCED_ANSWER_NOTE not in engine.calls[2]["tail_notes"]
        assert "forced_answer" not in result["metrics"]
        assert _events("turn_forced_answer") == []


# ==========================================================================
# A1 + A2 失败：强制作答失败 → 熔断出口，说明文字落进会话（只强制一次）
# ==========================================================================

@pytest.mark.parametrize("kind", sorted(FORCED_FAILURES))
def test_forced_failure_falls_to_sub_budget_exit_with_message(make_session, kind):
    engine = FakeEngine([_research(i) for i in range(11)] + [FORCED_FAILURES[kind]])
    registry = ResearchRegistry(per_call=7)
    orch = make_orch(engine, registry, max_total_iterations=40, max_sub_generations=80)
    session, start = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 12                       # 不做第二次强制生成，脚本也不会耗尽
    assert len(registry.executed) == 11                  # 强制作答吐的工具调用不执行
    assert result["status"] == "warning"
    assert SUB_NEAR_PHRASE in result["response"]
    assert SUB_HIT_PHRASE not in result["response"]         # 77 < 80：不谎称已达
    assert "forced_answer" not in result["metrics"]
    persisted = session.persistable_messages(start)
    last = _last_assistant(persisted)
    assert SUB_NEAR_PHRASE in last["content"]
    assert not last.get("tool_calls")                    # 失败产出整体丢弃
    assert "我还想再查一次" not in _blob(persisted)
    assert [e["detail"]["reason"] for e in _events("turn_abort")] == ["max_sub_generations"]
    assert _events("turn_forced_answer") == []


def test_pure_sub_budget_exit_lands_message(make_session):
    """强制作答关闭（reserve=0）时的熔断本体：说明同样落进会话（A1 不依赖 A2）。"""
    engine = FakeEngine([_research(1), _research(2), {"text": "不该被生成"}])
    orch = make_orch(engine, ResearchRegistry(per_call=7), max_sub_generations=14,
                     forced_answer_sub_reserve=0)
    session, start = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 2
    assert result["status"] == "warning"
    assert "子调研生成已达预算上限（14 次）" in result["response"]
    last = _last_assistant(session.persistable_messages(start))
    assert last["content"] == result["response"]


@pytest.mark.parametrize("kind", ["tool_call", "thinking_only"])
def test_forced_failure_falls_to_iteration_exit_with_message(make_session, kind):
    engine = FakeEngine([_probe(1), _probe(2), FORCED_FAILURES[kind]])
    registry = ResearchRegistry()
    orch = make_orch(engine, registry, max_total_iterations=3)
    session, start = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 3
    assert engine.calls[2]["n_tools"] == 0
    assert len(registry.executed) == 2
    assert result["status"] == "warning"
    assert "3 次主循环迭代内未能收敛" in result["response"]
    last = _last_assistant(session.persistable_messages(start))
    assert "3 次主循环迭代内未能收敛" in last["content"]
    assert [e["detail"]["reason"] for e in _events("turn_abort")] == ["max_iterations"]


# ==========================================================================
# A1 端到端：经 TurnRunner 落盘（history.jsonl）与 turn_ended 下发都看得见
# ==========================================================================

def _runner(engine, registry, **cfg):
    return TurnRunner(make_orch(engine, registry, **cfg), TaskScheduler())


def _turn_ended_response(session):
    ended = [e for e in session.journal.replay(0) if e["type"] == "turn_ended"]
    assert len(ended) == 1
    return ended[0]["data"]["response"]


def _client_visible_assistant_texts(messages):
    return [m.get("content") or "" for m in messages if m.get("role") == "assistant"]


def test_sub_budget_message_reaches_disk_and_client(make_session):
    engine = FakeEngine([_research(i) for i in range(11)] + [FORCED_FAILURES["tool_call"]])
    runner = _runner(engine, ResearchRegistry(per_call=7), max_total_iterations=40,
                     max_sub_generations=80)
    session = make_session()

    _, rd = run(submit(runner, session, session.session_id, prompt="查营收"))

    assert rd["status"] == "warning"
    assert any(SUB_NEAR_PHRASE in t for t in _client_visible_assistant_texts(rd["response"]))
    landed = history_lines(session)[0]
    assert any(SUB_NEAR_PHRASE in t for t in _client_visible_assistant_texts(landed["response"]))
    sent = _turn_ended_response(session)
    assert any(SUB_NEAR_PHRASE in t for t in _client_visible_assistant_texts(sent))
    assert sent == public_turn(rd)["response"]


def test_iteration_message_reaches_disk_and_client(make_session):
    engine = FakeEngine([_probe(1), _probe(2), FORCED_FAILURES["tool_call"]])
    runner = _runner(engine, ResearchRegistry(), max_total_iterations=3)
    session = make_session()

    _, rd = run(submit(runner, session, session.session_id, prompt="做点事"))

    phrase = "3 次主循环迭代内未能收敛"
    assert rd["status"] == "warning"
    assert any(phrase in t for t in _client_visible_assistant_texts(rd["response"]))
    landed = history_lines(session)[0]
    assert any(phrase in t for t in _client_visible_assistant_texts(landed["response"]))
    assert any(phrase in t for t in _client_visible_assistant_texts(_turn_ended_response(session)))


def test_forced_answer_reaches_disk_and_client(make_session):
    engine = FakeEngine([_research(i) for i in range(11)] + [{"text": ANSWER}])
    runner = _runner(engine, ResearchRegistry(per_call=7), max_total_iterations=40,
                     max_sub_generations=80)
    session = make_session()

    _, rd = run(submit(runner, session, session.session_id, prompt="查营收"))

    assert rd["status"] == "success"
    assert rd["forced_answer"] == "sub_generations"
    landed = history_lines(session)[0]
    assert _client_visible_assistant_texts(landed["response"])[-1] == ANSWER
    assert "tool budget for this turn is used up" not in _blob(landed)


# ==========================================================================
# 修补（第一道审查）F1：强制作答不绕过 D2 谎报围堵
# ==========================================================================

def test_forced_answer_after_failed_tool_appends_failure_note_and_warns(make_session):
    """最后一次调研返回 System Error（搜索不可用）后触发 T1：尾注追加失败句；回答照常落会话、
    forced_answer/事件照常，但状态降级 warning。"""
    engine = FakeEngine([_research(i) for i in range(11)]
                        + [{"text": "搜索服务暂不可用，以下是已收集的部分材料。"}])
    registry = ResearchRegistry(per_call=7, fail_from=11)
    orch = make_orch(engine, registry, max_total_iterations=40, max_sub_generations=80)
    session, start = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 12
    assert engine.calls[11]["tail_notes"] == [FORCED_ANSWER_NOTE, FORCED_ANSWER_FAILURE_NOTE]
    assert result["status"] == "warning"
    assert result["metrics"]["forced_answer"] == "sub_generations"
    assert len(_events("turn_forced_answer")) == 1
    persisted = session.persistable_messages(start)
    assert _last_assistant(persisted)["content"] == "搜索服务暂不可用，以下是已收集的部分材料。"
    assert "most recent tool call FAILED" not in _blob(persisted)


def test_forced_answer_after_recovered_failure_stays_success(make_session):
    """阴性对照：失败之后又有成功调用（unresolved_failure 已清）→ 不追加失败句、success。"""
    engine = FakeEngine([_research(i) for i in range(10)] + [_probe(1), {"text": ANSWER}])
    registry = ResearchRegistry(per_call=7, fail_from=10)
    orch = make_orch(engine, registry, max_total_iterations=12, max_sub_generations=80)
    session, _ = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 12
    assert engine.calls[11]["tail_notes"] == [FORCED_ANSWER_NOTE]
    assert result["status"] == "success"


def test_forced_answer_claiming_unexecuted_action_warns(make_session):
    """强制作答声称做了动作（已保存 / 已创建），本轮却没有任何成功的变更类调用 → warning。"""
    engine = FakeEngine([_probe(1), _probe(2), {"text": "结果已保存到 report.md，并已创建备份。"}])
    orch = make_orch(engine, ResearchRegistry(), max_total_iterations=3)
    session, _ = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert engine.calls[2]["n_tools"] == 0
    assert result["status"] == "warning"
    assert result["metrics"]["forced_answer"] == "iterations"


def test_forced_answer_claim_backed_by_real_write_stays_success(make_session):
    """阴性对照：本轮有成功的变更类调用 → 同一句动作声明不降级。"""
    engine = FakeEngine([{"tool_calls": [call("save_note", path="report.md")]}, _probe(2),
                         {"text": "结果已保存到 report.md，并已创建备份。"}])
    orch = make_orch(engine, ResearchRegistry(), max_total_iterations=3)
    session, _ = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert engine.calls[2]["n_tools"] == 0
    assert result["status"] == "success"


# ==========================================================================
# F2：检索证据未决时不强制作答（证据门优先）
# ==========================================================================

from .test_retrieval_evidence_guard import SEARCH, ScriptRegistry  # noqa: E402


class _RagResearchRegistry(ScriptRegistry):
    """rag_search 走检索证据通道；fake_research 额外记子生成（T1 场景用）。"""

    def __init__(self, results, per_call):
        super().__init__(results, extra=(FakeTool("fake_research"),))
        self.per_call = per_call

    def execute_tool(self, name, arguments):
        if name == "fake_research":
            note_usage(agent_runtime.get_usage_sink(), generations=self.per_call,
                       generation_tokens=1)
        return super().execute_tool(name, arguments)


INVENTED = "模拟结果：青瓷1250，墨色840。"


def test_unresolved_retrieval_blocks_iteration_trigger(make_session):
    """rag_search 失败后正好到最后一次迭代（T2 时刻）：不强制作答（那次仍带工具、无强制尾注），
    未经检索的答复不以 success 落盘，落既有的总迭代熔断 + 检索附注。"""
    engine = FakeEngine([{"tool_calls": [call("rag_search", **SEARCH)]}, _probe(1),
                         {"text": INVENTED}])
    registry = _RagResearchRegistry(["System Error: timeout"], per_call=0)
    registry._tools["probe"] = FakeTool("probe")
    orch = make_orch(engine, registry, max_total_iterations=3)
    session, start = _start(make_session, "根据知识库列出青瓷验收数值")

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 3
    assert engine.calls[2]["n_tools"] > 0
    assert FORCED_ANSWER_NOTE not in engine.calls[2]["tail_notes"]
    assert result["status"] == "warning" and "forced_answer" not in result["metrics"]
    assert result["metrics"]["retrieval_incomplete"] is True
    assert INVENTED not in _blob(session.persistable_messages(start))
    assert _events("turn_forced_answer") == []


def test_unresolved_retrieval_blocks_sub_budget_trigger(make_session):
    """同批调研烧到 77（T1 时刻）+ rag_search 失败：不强制作答，证据门的修复路照旧。"""
    engine = FakeEngine([
        {"tool_calls": [call("fake_research", q="x"), call("rag_search", **SEARCH)]},
        {"text": INVENTED}, {"text": INVENTED},
    ])
    registry = _RagResearchRegistry(["System Error: timeout"], per_call=77)
    orch = make_orch(engine, registry, max_total_iterations=40, max_sub_generations=80)
    session, start = _start(make_session, "根据知识库列出青瓷验收数值")

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 3
    for c in engine.calls[1:]:
        assert c["n_tools"] > 0 and FORCED_ANSWER_NOTE not in c["tail_notes"]
    assert result["status"] == "warning" and "forced_answer" not in result["metrics"]
    assert INVENTED not in _blob(session.persistable_messages(start))
    assert _events("turn_forced_answer") == []


# ==========================================================================
# F3：一步跳过上限（单次调研子生成 > reserve）仍先强制作答，而不是直接熔断
# ==========================================================================

def test_jump_past_cap_still_gets_forced_answer_first(make_session):
    engine = FakeEngine([_research(i) for i in range(5)] + [{"text": ANSWER}])
    orch = make_orch(engine, ResearchRegistry(per_call=16), max_total_iterations=40,
                     max_sub_generations=80)
    session, _ = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 6                         # 64 → 80：第 6 次是强制作答
    assert engine.calls[4]["n_tools"] > 0                 # 64 时余量 16 ≥ 7，照常调研
    assert engine.calls[5]["n_tools"] == 0
    assert result["status"] == "success"
    assert result["metrics"]["forced_answer"] == "sub_generations"
    assert result["metrics"]["sub_generations"] == 80
    assert _events("turn_abort") == []


# ==========================================================================
# F5：runaway_call / parse_loop 出口的说明同样落进会话
# ==========================================================================

def test_runaway_call_exit_lands_message(make_session):
    runaway = {"text": "", "truncated": True,
               "metrics": {"prompt_tokens": 100, "generation_tokens": 4096,
                           "generation_tps": 50.0, "constrained_steps": 3000}}
    engine = FakeEngine([dict(runaway), dict(runaway), dict(runaway)])
    orch = make_orch(engine, ResearchRegistry(), max_parse_retries=3)
    session, start = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "warning" and "参数失控" in result["response"]
    assert _last_assistant(session.persistable_messages(start))["content"] == result["response"]
    assert [e["detail"]["reason"] for e in _events("turn_abort")] == ["runaway_call"]


def test_parse_loop_exit_lands_message(make_session):
    from .fake_engine import bad_call
    engine = FakeEngine([{"tool_calls": [bad_call()]}, {"tool_calls": [bad_call()]},
                         {"tool_calls": [bad_call()]}])
    orch = make_orch(engine, ResearchRegistry(), max_parse_retries=3)
    session, start = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "warning" and "多次未能解析" in result["response"]
    persisted = session.persistable_messages(start)
    assert _last_assistant(persisted)["content"] == result["response"]
    assert sum(1 for m in persisted if "多次未能解析" in str(m.get("content") or "")) == 1
    assert [e["detail"]["reason"] for e in _events("turn_abort")] == ["parse_loop"]


# ==========================================================================
# F6：max_tokens 钳在 generation_reserve 之内
# ==========================================================================

def test_forced_answer_max_tokens_clamped_to_reserve(make_session):
    engine = FakeEngine([_probe(1), {"text": ANSWER}])
    orch = make_orch(engine, ResearchRegistry(), max_total_iterations=2,
                     context_guard={"generation_reserve": 8000})
    session, _ = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["metrics"]["forced_answer"] == "iterations"
    assert engine.calls[0]["kwargs"]["max_tokens"] == GEN["max_tokens"]
    assert engine.calls[1]["kwargs"]["max_tokens"] == 8000


# ==========================================================================
# F7：取消检查点先于强制作答；强制那次撞 ceiling 走既有 ceiling 出口
# ==========================================================================

class _CancelAfter(FakeEngine):
    def __init__(self, script, after):
        super().__init__(script)
        self.after = after

    async def generate_once(self, session, **kwargs):
        out = await super().generate_once(session, **kwargs)
        if len(self.calls) == self.after:
            session.cancel_requested = True
        return out


def test_cancel_takes_priority_over_forced_answer(make_session):
    engine = _CancelAfter([_probe(1), _probe(2), {"text": "不该生成"}], after=2)
    orch = make_orch(engine, ResearchRegistry(), max_total_iterations=3)
    session, _ = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "cancelled"
    assert len(engine.calls) == 2                         # 强制那次生成没有发生
    assert _events("turn_forced_answer") == []


def test_forced_answer_hitting_ceiling_uses_ceiling_exit(make_session):
    engine = FakeEngine([_probe(1), {"text": "不该生成"}])
    engine.measure_script = [100, 60000]                  # 强制那次量到 ≥ ceiling（65536 − 12288），压缩后仍超
    orch = make_orch(engine, ResearchRegistry(), max_total_iterations=2)
    orch.bind_context_limit()
    session, start = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert len(engine.calls) == 1
    assert engine.measure_calls[-1]["tool_names"] == []   # 量的正是强制作答的 prompt
    assert engine.measure_calls[-1]["tail_notes"][0] == FORCED_ANSWER_NOTE
    assert result["status"] == "warning" and "上下文已达模型上限" in result["response"]
    blob = _blob(session.persistable_messages(start))
    assert "未能收敛" not in blob and "预算上限" not in blob
    assert [e["detail"]["reason"] for e in _events("turn_abort")] == ["context_guard"]


# ==========================================================================
# F8：评估 harness 的总开关（forced_answer: false = build 84 的直接熔断）
# ==========================================================================

def test_forced_answer_switch_off_restores_plain_fuse(make_session):
    engine = FakeEngine([_probe(1), _probe(2), _probe(3)])
    registry = ResearchRegistry()
    orch = make_orch(engine, registry, max_total_iterations=3, forced_answer=False)
    session, _ = _start(make_session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert all(c["n_tools"] > 0 for c in engine.calls)
    assert len(registry.executed) == 3                    # 末次工具调用照常执行
    assert result["status"] == "warning" and "未能收敛" in result["response"]
