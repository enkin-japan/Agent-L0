"""Retrieval failures must never become fabricated streamed/canonical answers."""
import asyncio
import json

import pytest

from framework.core.orchestrator import Orchestrator
from framework.core.retrieval_evidence import (DELIVERY_BLOCKED, DELIVERY_TOOLS, MAX_TARGETS,
                                               REPAIR_NOTE, RetrievalEvidence, is_delivery,
                                               observation)
from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_fabrication_guardrails import GEN, begin_turn, make_orch

FAKE = "模拟结果：青瓷1250，墨色840，总计2110。"
SEARCH = dict(query="青瓷验收", category="sales", optimized_query="青瓷")
OTHER = {**SEARCH, "category": "other"}
CONTEXT = dict(source_path="test/sales", seq=1)
CONTEXT_HIT = ("[Context around chunk 1 of 'test' (2 chunks total)]\n"
               "--- Chunk 1 (the hit) ---\nCeladon=12, Ink=8, Paper=5，总和25。")
LATER = "delivery blocked until the later retrieval in this batch"


def empty(category="sales"):
    return f"No results for '青瓷验收' in category '{category}': below cutoff."


def hit(args=SEARCH):
    # Same trusted tool formatter used in production, not a second text grammar.
    from framework.mcp.tools.rag_search import format_results
    text, items = format_results(args["query"], args["category"], [
        {"document": "Celadon=12, Ink=8, Paper=5，总和25。", "rerank_score": .9,
         "metadata": {"title": "验收", "source_path": "test/sales", "seq": 1}}])
    return text, {"hint": "citations", "payload": {
        "query": args["query"], "category": args["category"], "items": items}}


def api_tool(name, method, **kw):
    # Mimics an api_tool_factory instance: the delivery predicate keys on `_method`.
    tool = FakeTool(name, **kw)
    tool._method = method
    return tool


class ScriptRegistry(FakeRegistry):
    def __init__(self, results, *, approval=False, extra=()):
        super().__init__([FakeTool("rag_search", approval=approval),
                          FakeTool("rag_get_context"), FakeTool("read_thing"),
                          FakeTool("render_chart", read_only=False, concurrency_safe=True),
                          # A mutating dynamic API tool (POST) needing approval; like
                          # production POST tools it is not concurrency-safe.
                          api_tool("write_thing", "POST", approval=True, read_only=False,
                                   concurrency_safe=False),
                          *extra])
        self.results = list(results)

    def execute_tool(self, name, arguments):
        self.executed.append({"name": name, "args": dict(arguments)})
        if name not in {"rag_search", "rag_get_context"}:
            return "ok", None
        result = self.results.pop(0)
        return result if isinstance(result, tuple) else (result, None)


def execute(make_session, script, results, *, registry=None, **cfg):
    engine = FakeEngine(script)
    registry = registry or ScriptRegistry(results)
    orch = make_orch(engine, registry, **cfg)
    session = make_session()
    session.add_user_message("根据知识库的青瓷验收资料列出数值，不要模拟。")
    begin_turn(session)
    result = asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))
    return result, session, engine, registry


def drive(make_session, script, registry):
    """Run a turn, approving every pending batch, and return the final result."""
    result, session, engine, registry = execute(make_session, script, None, registry=registry)
    for _ in range(len(script)):
        if result["status"] != "pending_approval":
            break
        result = asyncio.run(make_orch(engine, registry).resume_after_approval(
            session, approved=True, journal=session.journal, **GEN))
    return result, session, registry


def executed(registry):
    return [c["name"] for c in registry.executed]


def assert_no_fabrication(result, session):
    assert result["status"] == "warning"
    assert "知识库检索" in result["response"]
    assert FAKE not in str(result)
    assert "1250" not in str(session.history)
    assert "1250" not in json.dumps(list(session.journal.ring), ensure_ascii=False)


def test_failure_unrelated_success_cannot_release_fabricated_answer(make_session):
    result, session, engine, _ = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("read_thing", path="elsewhere")]},
        {"text": FAKE, "thoughts": [FAKE]}, {"text": FAKE, "thoughts": [FAKE]},
    ], ["System Error: read timeout"])
    assert_no_fabrication(result, session)
    assert REPAIR_NOTE in engine.calls[-1]["tail_notes"]
    session.append_log({"prompt": "test", "response": session.history[2:]})
    persisted = (session.session_dir / "history.jsonl").read_text()
    assert "1250" not in persisted
    assert "_retrieval" not in persisted


@pytest.mark.parametrize("empty", [
    "No results for '青瓷验收' in category 'sales': below cutoff.",
    "No results for '青瓷验收' in category 'sales': target store is empty.",
])
def test_empty_result_is_a_completed_retrieval_and_tool_status_is_unchanged(make_session, empty):
    answer = "知识库中没有找到青瓷验收资料。"
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]}, {"text": answer}
    ], [empty])
    assert result["status"] == "success" and result["response"] == answer
    tokens = [e["data"]["text"] for e in session.journal.ring if e["type"] == "token_delta"]
    assert tokens == [answer]  # normal streaming, not provisional
    ev = [e for e in session.journal.ring if e["type"] == "tool_result"]
    assert ev[0]["data"]["status"] == "ok"  # frozen public semantics
    assert "_retrieval" not in str(ev)


def test_same_target_retry_with_new_keywords_restores_normal_stream(make_session):
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"text": FAKE},
        {"tool_calls": [call("rag_search", **{**SEARCH, "optimized_query": "Celadon"})]},
        {"text": "来源资料的总和为25。"},
    ], ["System Error: timeout", hit()])
    assert result["status"] == "success"
    assert result["response"] == "来源资料的总和为25。"
    assert "1250" not in str(session.history)
    tokens = [e["data"]["text"] for e in session.journal.ring if e["type"] == "token_delta"]
    assert tokens == [result["response"]]


@pytest.mark.parametrize("change", [{"category": "other"}, {"query": "其它问题"}])
def test_other_target_success_releases_original_failure(make_session, change):
    other = {**SEARCH, **change}
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("rag_search", **other)]},
        {"text": "来源资料的总和为25。"},
    ], ["System Error: timeout", hit(other)])
    assert result["status"] == "success"
    assert result["response"] == "来源资料的总和为25。"


def test_parallel_failures_are_all_released_by_one_later_completed_retrieval(make_session):
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH), call("rag_search", **OTHER)]},
        {"tool_calls": [call("rag_search", **{**SEARCH, "optimized_query": "retry"})]},
        {"text": "来源资料的总和为25。"},
    ], ["System Error: timeout", "System Error: other timeout", hit()])
    assert result["status"] == "success"


def test_failure_after_completed_retrieval_in_same_batch_stays_pending(make_session):
    # Release is ordered: only a LATER completed retrieval resolves a failure.
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("rag_search", **OTHER), call("rag_search", **SEARCH)]},
        {"text": FAKE}, {"text": FAKE},
    ], [hit(OTHER), "System Error: timeout"])
    assert_no_fabrication(result, session)


def test_approval_pause_keeps_missing_evidence_after_unrelated_success(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("read_thing", path="test")]},
        {"text": FAKE}, {"text": FAKE}])
    registry = ScriptRegistry(["System Error: timeout"])
    registry._tools["read_thing"].approval = True
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("检索资料后另建文件")
    begin_turn(session)
    first = asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))
    assert first["status"] == "pending_approval"
    assert session.carried_turn_stats["retrieval_evidence"]["pending"]
    # Simulate mechanical replacement of observations: checkpoint is authoritative.
    for message in session.history:
        for response in message.get("tool_responses", []):
            response["response"] = "[compacted]"
            response.pop("_retrieval", None)
    result = asyncio.run(orch.resume_after_approval(
        session, approved=True, journal=session.journal, **GEN))
    assert_no_fabrication(result, session)


def test_approved_retrieval_failure_is_guarded_on_first_resume_generation(make_session):
    engine = FakeEngine([{"tool_calls": [call("rag_search", **SEARCH)]},
                         {"text": FAKE}, {"text": FAKE}])
    orch = make_orch(engine, ScriptRegistry(["System Error: timeout"], approval=True))
    session = make_session()
    session.add_user_message("检索资料")
    begin_turn(session)
    assert asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))["status"] == "pending_approval"
    result = asyncio.run(orch.resume_after_approval(
        session, approved=True, journal=session.journal, **GEN))
    assert_no_fabrication(result, session)


def test_normal_creative_and_successful_retrieval_streaming_unchanged(make_session):
    result, session, _, _ = execute(make_session, [{"text": "示例：青瓷1250。"}], [])
    assert result["status"] == "success"
    assert any(e["type"] == "token_delta" for e in session.journal.ring)
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]}, {"text": "总和25。"}
    ], [hit()])
    assert result["status"] == "success"
    assert any(e["type"] == "token_delta" for e in session.journal.ring)


def test_iteration_budget_exit_keeps_real_reason_and_names_missing_retrieval(make_session):
    # T6: the budget exit keeps its own reason; the retrieval note is appended.
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]}
    ], ["System Error: timeout"], max_total_iterations=1)
    assert_no_fabrication(result, session)
    assert "未能收敛" in result["response"]
    assert "有效证据" in result["response"]
    assert result["metrics"]["retrieval_incomplete"] is True


def test_context_success_requires_nonempty_chunk_and_matching_pointer():
    args = {"source_path": "test/doc", "seq": 1}
    failed = observation("rag_get_context", args, "System Error: timeout", None, True)
    found = observation("rag_get_context", args,
                        "[Context around chunk 1 of 'test' (2 chunks total)]\n"
                        "--- Chunk 1 (the hit) ---\n真实文本", None, False)
    state = RetrievalEvidence()
    state.observe([call("rag_get_context", **args)], [{"_retrieval": failed}])
    assert state.unresolved
    # A fact that does not belong to the observed call is ignored.
    state.observe([call("rag_get_context", **{**args, "seq": 2})], [{"_retrieval": found}])
    assert state.unresolved
    empty_chunk = observation("rag_get_context", args,
                              "[Context around chunk 1 of 'test' (2 chunks total)]\n"
                              "--- Chunk 1 (the hit) ---\n", None, False)
    assert empty_chunk["kind"] == "empty"  # not evidence, but a completed lookup
    state.observe([call("rag_get_context", **args)], [{"_retrieval": found}])
    assert not state.unresolved


def test_evidence_checkpoint_is_bounded_without_eviction():
    state = RetrievalEvidence()
    for i in range(MAX_TARGETS + 10):
        c = call("rag_search", **{**SEARCH, "query": str(i)})
        state.observe([c], [{"_retrieval": observation(
            c["name"], c["arguments"], "System Error: timeout", None, True)}])
    assert len(state.checkpoint()["pending"]) == MAX_TARGETS
    assert state.overflow and state.unresolved
    assert all(len(key) == 64 for key in state.pending)
    restored = RetrievalEvidence(state.checkpoint())
    assert restored.overflow and restored.unresolved
    c = call("rag_search", **OTHER)
    restored.observe([c], [{"_retrieval": observation(
        c["name"], c["arguments"], empty("other"), None, False)}])
    assert not restored.unresolved and not restored.checkpoint()["pending"]


def test_citations_on_another_tool_do_not_count_as_retrieval_evidence():
    text, render = hit()
    assert observation("read_thing", SEARCH, text, render, False) is None
    assert observation("rag_search", SEARCH, "System Error: timeout", render, True)["kind"] == "failed"


def test_turn_runner_journal_and_disk_never_publish_discarded_draft(make_session):
    from .test_turn_runner import make_runner, submit
    engine = FakeEngine([
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"text": FAKE, "thoughts": [FAKE]}, {"text": FAKE, "thoughts": [FAKE]}])
    runner = make_runner(engine, ScriptRegistry(["System Error: timeout"]))
    session = make_session()
    _, result = asyncio.run(submit(runner, session, session.session_id, "从知识库核对数值"))
    assert result["status"] == "warning"
    assert "1250" not in session.history_log.read_text()
    assert "1250" not in str(session.journal.replay(0))
    assert "_retrieval" not in str(session.journal.replay(0))
    assert session.journal.replay(0)[-1]["type"] == "turn_ended"
    assert not session.inflight_path.exists()
    assert runner.scheduler.snapshot()["tasks"] == []


def test_suspend_resume_preserves_evidence_gate(make_session):
    session = make_session()

    class SuspendingRegistry(ScriptRegistry):
        def execute_tool(self, name, arguments):
            result = super().execute_tool(name, arguments)
            session.suspend_requested = True
            return result

    engine = FakeEngine([{"tool_calls": [call("rag_search", **SEARCH)]},
                         {"text": FAKE}, {"text": FAKE}])
    orch = make_orch(engine, SuspendingRegistry(["System Error: timeout"]))
    session.add_user_message("检索资料")
    begin_turn(session)
    assert asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))["status"] == "suspended"
    result = asyncio.run(orch.run_turn(session, journal=session.journal,
                                      keep_recent_calls=True, compact_history=False, **GEN))
    assert_no_fabrication(result, session)


def test_new_user_turn_does_not_inherit_failed_retrieval_gate(make_session):
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]}, {"text": FAKE}, {"text": FAKE}
    ], ["System Error: timeout"])
    assert result["status"] == "warning"
    engine = FakeEngine([{"text": "这是一个明确的虚构示例：1250。"}])
    session.add_user_message("接下来不查询资料，请编写虚构示例。")
    begin_turn(session)
    result = asyncio.run(make_orch(engine, ScriptRegistry([])).run_turn(
        session, journal=session.journal, **GEN))
    assert result["status"] == "success"
    assert "1250" in result["response"]


@pytest.mark.parametrize("name", ["render_chart", "write_thing"])
def test_failed_retrieval_blocks_delivery_without_execution_or_approval(make_session, name):
    result, session, _, registry = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call(name, values=[1250, 840])]},
        {"text": "完成了。"}, {"text": "完成了。"},
    ], ["System Error: timeout"])
    assert result["status"] == "warning"
    assert [c["name"] for c in registry.executed] == ["rag_search"]
    assert "delivery blocked" in str(session.history)
    assert not session.pending_tool_calls
    assert not any(e["type"] == "tool_started" and e["data"]["name"] == name
                   for e in session.journal.ring)


@pytest.mark.parametrize("approval", [False, True])
@pytest.mark.parametrize("retry_ok", [True, False])
def test_pending_failure_same_batch_retry_is_flushed_before_chart(make_session, retry_ok, approval):
    retry = call("rag_search", **{**SEARCH, "optimized_query": "Celadon"})
    result, session, registry = drive(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [retry, call("render_chart", values=[12, 8, 5])]},
        {"text": "总和25。"}, {"text": "完成了。"}, {"text": "完成了。"},
    ], ScriptRegistry(["System Error: timeout", hit() if retry_ok else "System Error: again"],
                      approval=approval))
    if retry_ok:
        assert result["status"] == "success"
        assert executed(registry) == ["rag_search", "rag_search", "render_chart"]
    else:
        assert result["status"] == "warning"
        assert executed(registry) == ["rag_search", "rag_search"]
        assert DELIVERY_BLOCKED in str(session.history)


def test_same_batch_successful_retrieval_allows_chart(make_session):
    result, _, _, registry = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH), call("render_chart", values=[12, 8, 5])]},
        {"text": "总和25。"},
    ], [hit()])
    assert result["status"] == "success"
    assert [c["name"] for c in registry.executed] == ["rag_search", "render_chart"]


def test_fresh_batch_failed_retrieval_is_serialized_before_chart(make_session):
    # Nothing pending at batch start: the chart still waits for the queued
    # retrieval, which fails, so the chart is blocked and never executed.
    result, session, _, registry = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH), call("render_chart", values=[7])]},
        {"text": FAKE}, {"text": FAKE},
    ], ["System Error: timeout"])
    assert_no_fabrication(result, session)
    assert executed(registry) == ["rag_search"]
    chart = [r for m in session.history for r in m.get("tool_responses", [])
             if r.get("name") == "render_chart"]
    assert [r["response"] for r in chart] == [DELIVERY_BLOCKED]
    assert not any(e["type"] == "tool_started" and e["data"]["name"] == "render_chart"
                   for e in session.journal.ring)


def test_fresh_batch_successful_retrieval_runs_before_chart(make_session):
    result, _, _, registry = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH), call("render_chart", values=[12, 8, 5])]},
        {"text": "总和25。"},
    ], [hit()])
    assert result["status"] == "success"
    assert executed(registry) == ["rag_search", "render_chart"]


def test_no_pending_batch_runs_without_barrier(make_session):
    # T5 (no pending): an approved delivery ahead of a retrieval runs as before C4.
    result, session, registry = drive(make_session, [
        {"tool_calls": [call("write_thing", values=[12]), call("rag_search", **SEARCH)]},
        {"text": "总和25。"},
    ], ScriptRegistry([hit()]))
    assert result["status"] == "success"
    assert sorted(executed(registry)) == ["rag_search", "write_thing"]
    assert LATER not in str(session.history)


def test_pending_failure_blocks_delivery_ahead_of_later_retrieval(make_session):
    # T5 (pending): a delivery preceded by a still-failing retrieval and followed
    # by another retrieval in the same approved batch is held, never executed.
    result, session, registry = drive(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("rag_search", **{**SEARCH, "optimized_query": "r1"}),
                        call("write_thing", values=[1250]),
                        call("rag_search", **{**SEARCH, "optimized_query": "r2"})]},
        {"text": "总和25。"},
    ], ScriptRegistry(["System Error: timeout", "System Error: again", hit()]))
    assert "write_thing" not in executed(registry)
    assert LATER in str(session.history)
    assert result["status"] == "success"  # the later retrieval released the gate


def test_pending_failure_blocks_leading_delivery_before_approval(make_session):
    # T5 (pending, [delivery, retrieval]): no retrieval precedes the delivery, so
    # the pre-approval block answers it without an approval prompt.
    result, session, _, registry = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("write_thing", values=[1250]), call("rag_search", **OTHER)]},
        {"text": "总和25。"},
    ], ["System Error: timeout", hit(OTHER)])
    assert executed(registry) == ["rag_search", "rag_search"]
    assert DELIVERY_BLOCKED in str(session.history)
    assert not session.pending_tool_calls


def test_unfinished_replanned_task_with_final_retrieval_failures_cannot_finish_success(make_session):
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("update_plan", op="replace", steps=["检索", "交付"])]},
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("read_thing", path="categories")]},
        {"tool_calls": [call("rag_search", **{**SEARCH, "optimized_query": "retry1"})]},
        {"tool_calls": [call("rag_search", **{**SEARCH, "optimized_query": "retry2"})]},
        {"tool_calls": [call("update_plan", op="replace", steps=["整理", "交付"])]},
        {"tool_calls": [call("update_plan", op="add", title="另一个未完成步骤")]},
        {"text": FAKE}, {"text": FAKE},
    ], ["System Error: timeout"] * 3)
    assert_no_fabrication(result, session)
    types = [e["type"] for e in session.journal.ring]
    assert "step_failed" in types
    assert "step_completed" not in types
    assert session.task_state and not session.task_state.all_done()


def test_failed_retrieval_cannot_publish_fabricated_complete_step_result(make_session):
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("update_plan", op="replace", steps=["检索数值", "交付"])]},
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("complete_step", result=FAKE)]},
        {"text": "完成了。"}, {"text": "完成了。"},
    ], ["System Error: timeout"])
    assert result["status"] == "warning"
    assert "step completion blocked" in str(session.history)
    assert not any(e["type"] == "step_completed" for e in session.journal.ring)
    assert "1250" not in str(session.journal.ring)
    assert "1250" not in str(session.task_state.snapshot())


@pytest.mark.parametrize("reverse", [False, True])
def test_same_batch_retrieval_cannot_supply_complete_step_evidence_yet(make_session, reverse):
    # Meta calls run before the physical batch: while a failure is pending, the
    # same-batch retry cannot back a completion yet.
    calls = [call("rag_search", **{**SEARCH, "optimized_query": "retry"}),
             call("complete_step", result=FAKE)]
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("update_plan", op="replace", steps=["检索数值"])]},
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": calls[::-1] if reverse else calls},
        {"tool_calls": [call("complete_step", result="真实资料总和25")]},
        {"text": "总和25。"},
    ], ["System Error: timeout", hit()])
    assert result["status"] == "success"
    assert "step completion blocked" in str(session.history)
    completed = [e for e in session.journal.ring if e["type"] == "step_completed"]
    assert len(completed) == 1 and completed[0]["data"]["result"] == "真实资料总和25"
    assert "1250" not in str(session.journal.ring)


def test_pending_failure_chart_cannot_run_ahead_of_later_retrieval(make_session):
    result, session, _, registry = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("rag_search", **{**SEARCH, "optimized_query": "r1"}),
                        call("render_chart", values=[1250]),
                        call("rag_search", **{**SEARCH, "optimized_query": "r2"})]},
        {"text": "完成了。"}, {"text": "完成了。"},
    ], ["System Error: timeout"] * 3)
    assert result["status"] == "warning"
    assert executed(registry) == ["rag_search"] * 3
    assert LATER in str(session.history)
    assert not any(e["type"] == "tool_started" and e["data"]["name"] == "render_chart"
                   for e in session.journal.ring)


def test_blocked_delivery_can_retry_after_later_retrieval_succeeds(make_session):
    chart = call("render_chart", values=[12, 8, 5])
    result, session, _, registry = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [chart, call("rag_search", **{**SEARCH, "optimized_query": "Celadon"})]},
        {"tool_calls": [chart]}, {"text": "总和25。"},
    ], ["System Error: timeout", hit()])
    assert result["status"] == "success"
    assert DELIVERY_BLOCKED in str(session.history)
    assert executed(registry) == ["rag_search", "rag_search", "render_chart"]


def test_failed_retrieval_cannot_remember_fabricated_values_as_facts(make_session):
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("update_plan", op="replace", steps=["检索数值", "交付"])]},
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("remember", note=FAKE)]},
        {"text": "完成了。"}, {"text": "完成了。"},
    ], ["System Error: timeout"])
    assert result["status"] == "warning"
    assert "fact recording blocked" in str(session.history)
    assert session.task_state.notes == []
    assert "1250" not in str(session.journal.ring)


def test_remember_waits_for_same_batch_retrieval_then_records_verified_fact(make_session):
    result, session, _, _ = execute(make_session, [
        {"tool_calls": [call("update_plan", op="replace", steps=["检索数值"])]},
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("remember", note=FAKE),
                        call("rag_search", **{**SEARCH, "optimized_query": "retry"})]},
        {"tool_calls": [call("remember", note="真实资料总和25")]},
        {"tool_calls": [call("complete_step", result="真实资料总和25")]},
        {"text": "总和25。"},
    ], ["System Error: timeout", hit()])
    assert result["status"] == "success"
    assert "fact recording blocked" in str(session.history)
    assert "1250" not in str(session.journal.ring)
    plans = [e["data"]["task"] for e in session.journal.ring
             if e["type"] == "plan_updated" and e["data"]["task"]]
    assert any(p["notes"] == ["真实资料总和25"] for p in plans)


# ---------- R1: delivery scope ----------

@pytest.mark.parametrize("approval", [False, True])
def test_shell_is_judged_by_its_own_read_only_logic(make_session, approval):
    # T1: approval == not read-only command (shell_exec's own judgment).
    shell = FakeTool("run_shell_command", read_only=False, approval=approval)
    result, session, _, registry = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("run_shell_command", command="ls")]},
        {"text": FAKE}, {"text": FAKE},
    ], None, registry=ScriptRegistry(["System Error: timeout"], extra=[shell]))
    assert_no_fabrication(result, session)
    assert not session.pending_tool_calls
    if approval:
        assert executed(registry) == ["rag_search"]
        assert DELIVERY_BLOCKED in str(session.history)
    else:
        assert executed(registry) == ["rag_search", "run_shell_command"]
        assert DELIVERY_BLOCKED not in str(session.history)


def test_non_delivery_mutation_runs_while_chart_is_blocked(make_session):
    # T2: a non-read-only tool outside the delivery set is not held back.
    result, session, _, registry = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("move_thing", src="a", dst="b")]},
        {"tool_calls": [call("render_chart", values=[7])]},
        {"text": FAKE}, {"text": FAKE},
    ], None, registry=ScriptRegistry(["System Error: timeout"],
                                     extra=[FakeTool("move_thing", read_only=False)]))
    assert_no_fabrication(result, session)
    assert executed(registry) == ["rag_search", "move_thing"]
    assert DELIVERY_BLOCKED in str(session.history)


# ---------- R2 / R3: completed retrievals release ----------

@pytest.mark.parametrize("failed_first", [True, False])
def test_empty_retrieval_completes_and_chart_executes(make_session, failed_first):
    # T3: an empty result in another category releases a failure; an empty
    # result alone never opens the gate (normal stream, chart executes).
    script = [{"tool_calls": [call("rag_search", **SEARCH)]}] if failed_first else []
    script += [{"tool_calls": [call("rag_search", **OTHER)]},
               {"tool_calls": [call("render_chart", values=[0])]},
               {"text": "知识库中没有找到相关资料。"}, {"text": "完成了。"}]
    results = (["System Error: timeout"] if failed_first else []) + [empty("other")]
    result, session, _, registry = execute(make_session, script, results)
    assert "render_chart" in executed(registry)
    assert DELIVERY_BLOCKED not in str(session.history)
    assert result["status"] != "warning"
    assert any(e["type"] == "token_delta" for e in session.journal.ring)


def test_context_evidence_for_another_target_releases_delivery(make_session):
    # T4: rag_search fails, rag_get_context succeeds for a different target.
    result, session, registry = drive(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("rag_get_context", **CONTEXT)]},
        {"tool_calls": [call("write_thing", values=[12, 8, 5])]},
        {"text": "总和25。"},
    ], ScriptRegistry(["System Error: timeout", CONTEXT_HIT]))
    assert executed(registry) == ["rag_search", "rag_get_context", "write_thing"]
    assert DELIVERY_BLOCKED not in str(session.history)
    assert result["status"] == "success"


# ---------- T7: is_delivery unit cases ----------

def test_is_delivery_scope():
    registry = FakeRegistry([
        FakeTool("rag_search", read_only=False), FakeTool("rag_get_context", read_only=False),
        FakeTool("cloud_consult", read_only=False, approval=True),
        FakeTool("move_thing", read_only=False),
        FakeTool("shell_ro", read_only=False),
        api_tool("crm_post", "POST", read_only=False),
        api_tool("crm_get", "GET", read_only=True),
        *[FakeTool(n, read_only=True) for n in sorted(DELIVERY_TOOLS)],
    ])
    for name in sorted(DELIVERY_TOOLS):
        assert is_delivery(registry, call(name)), name
    for name in ("rag_search", "rag_get_context", "cloud_consult", "move_thing",
                 "crm_get", "read_thing"):
        assert not is_delivery(registry, call(name)), name
    assert is_delivery(registry, call("crm_post"))
    assert "excel_research" in DELIVERY_TOOLS
    assert is_delivery(registry, call("excel_research", query="x"))
    shell = FakeRegistry([FakeTool("run_shell_command", read_only=False, approval=False)])
    assert not is_delivery(shell, call("run_shell_command", command="ls"))
    shell._tools["run_shell_command"].approval = True
    assert is_delivery(shell, call("run_shell_command", command="rm x"))


# ---------- round 2 ----------

NOT_FOUND = "未找到该 source_path 的任何切片（路径可能不精确，先用检索确认 Path）。"


def test_context_not_found_message_is_a_completed_lookup(make_session):
    # rag_get_context returns the RAG server's own msg when chunks == [].
    result, session, _, registry = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("rag_get_context", **CONTEXT)]},
        {"tool_calls": [call("render_chart", values=[12, 8, 5])]},
        {"text": "总和25。"},
    ], [hit(), NOT_FOUND])
    assert executed(registry) == ["rag_search", "rag_get_context", "render_chart"]
    assert DELIVERY_BLOCKED not in str(session.history)
    assert result["status"] != "warning"


def test_c4_block_does_not_count_as_upgrade_failure(make_session, monkeypatch):
    # The only physical response in batch 3 is a C4 block (the deferred-to
    # retrieval was re-denied and removed); it must not count as a failed batch.
    import framework.cloud.upgrade as upgrade
    seen = []
    monkeypatch.setattr(upgrade, "decide_on_failures",
                        lambda n, *a, **k: seen.append(n))
    write = FakeTool("write_file", read_only=False, concurrency_safe=False)
    registry = ScriptRegistry(["System Error: timeout"], extra=[write])
    registry._tools["rag_get_context"].approval = True
    script = [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("rag_get_context", **CONTEXT)]},
        {"tool_calls": [call("rag_get_context", **CONTEXT), call("write_file", path="a", content="1250")]},
        {"text": FAKE}, {"text": FAKE},
    ]
    result, session, engine, registry = execute(make_session, script, None, registry=registry)
    assert result["status"] == "pending_approval" and seen == [1]
    result = asyncio.run(make_orch(engine, registry).resume_after_approval(
        session, approved=False, journal=session.journal, **GEN))
    assert "Denied-call blocked" in str(session.history)
    assert "write_file" not in executed(registry)
    assert DELIVERY_BLOCKED in str(session.history)
    # Resumed segment restarts the counter; the blocked-only batch leaves it at 0.
    assert seen[1:] and all(n == 0 for n in seen[1:])
    assert result["status"] == "warning"


VERIFY_NOTE = "never re-checked the result"


def test_resumed_c4_blocked_file_write_is_not_an_unverified_write(make_session):
    # A failure is pending from before the approval pause. On resume the approved
    # write_file is held by the later-retrieval barrier; that later retrieval then
    # releases the gate, so the next generation is a normal answer that reaches
    # the verify gate. A write that never ran must not trigger the verify nudge.
    write = FakeTool("write_file", read_only=False, approval=True, concurrency_safe=False)
    registry = ScriptRegistry(["System Error: timeout", "System Error: again", hit()],
                              extra=[write])
    result, session, engine, registry = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("rag_search", **{**SEARCH, "optimized_query": "r1"}),
                        call("write_file", path="a", content="x"),
                        call("rag_search", **{**SEARCH, "optimized_query": "r2"})]},
        {"text": "总和25。"}, {"text": "总和25。"},
    ], None, registry=registry)
    assert result["status"] == "pending_approval"
    assert session.carried_turn_stats["retrieval_evidence"]["pending"]
    result = asyncio.run(make_orch(engine, registry).resume_after_approval(
        session, approved=True, journal=session.journal, **GEN))
    assert "write_file" not in executed(registry)
    assert LATER in str(session.history)
    assert result["status"] == "success"
    notes = [n for c in engine.calls[2:] for n in (c.get("tail_notes") or [])]
    assert not any(VERIFY_NOTE in n for n in notes)


def test_serial_retrieval_failure_is_accounted(make_session):
    # rag_search not concurrency-safe -> the serial execution path observes it.
    registry = ScriptRegistry(["System Error: timeout"])
    registry._tools["rag_search"].concurrency_safe = False
    result, session, _, registry = execute(make_session, [
        {"tool_calls": [call("rag_search", **SEARCH)]},
        {"tool_calls": [call("render_chart", values=[7])]},
        {"text": FAKE}, {"text": FAKE},
    ], None, registry=registry)
    assert_no_fabrication(result, session)
    assert executed(registry) == ["rag_search"]
    assert DELIVERY_BLOCKED in str(session.history)


@pytest.mark.parametrize("checkpoint", [
    {"pending": ["x"], "nudges": "x"}, {"pending": "x", "nudges": None, "overflow": "yes"},
    {"pending": None, "nudges": [1]}, {"nudges": True}, "garbage", None,
])
def test_malformed_checkpoint_is_treated_as_empty(checkpoint):
    state = RetrievalEvidence(checkpoint)
    assert not state.unresolved
    assert state.checkpoint() == {"pending": {}, "overflow": False, "nudges": 0}
