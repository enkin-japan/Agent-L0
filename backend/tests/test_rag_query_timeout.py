"""Bounded RAG query budgets, with virtual latency and real registry/orchestrator.

No actual network, credentials, sleeps, models or services are used. Requests
and their elapsed duration are synthetic; existing orchestration code receives
its actual tool-specific deadline through MCPRegistry.
"""
import ast
import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from framework.mcp.base import BaseTool, ToolOutput
from framework.mcp.registry import MCPRegistry

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = "synthetic-private-auth-query-body"


def harness(config=None):
    calls = []
    response = SimpleNamespace(json=lambda: {"results": [{"document": "Celadon=12, Ink=8, Paper=5",
        "rerank_score": .9, "metadata": {"title": "synthetic", "source_path": "synthetic/sample"}}]},
        raise_for_status=lambda: None)
    def get(*args, **kwargs):
        calls.append(("get", args, kwargs))
        return SimpleNamespace(json=lambda: [], raise_for_status=lambda: None)
    def post(*args, **kwargs):
        calls.append(("post", args, kwargs))
        return response
    fake_requests = SimpleNamespace(get=get, post=post, Timeout=requests.Timeout,
        ConnectionError=requests.ConnectionError, HTTPError=requests.HTTPError)
    ns = dict(math=math, json=json, requests=fake_requests, BaseTool=BaseTool, ToolOutput=ToolOutput,
              load_section=lambda section: config or {}, rag_auth_headers=lambda: {"Authorization": "Bearer " + PRIVATE})
    source = ROOT / "backend/framework/mcp/tools/rag_search.py"
    tree = ast.parse(source.read_text())
    nodes = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), ns)
    tool = ns["RAGSearchTool"]()
    return SimpleNamespace(ns=ns, calls=calls, requests=fake_requests, tool=tool, response=response)


def arguments():
    return dict(query=PRIVATE, category="synthetic", optimized_query="synthetic")


def test_default_query_limits_and_registry_deadline_cover_both_http_phases():
    h = harness()
    result = h.tool.execute(**arguments())
    assert isinstance(result, ToolOutput)
    post = [call for call in h.calls if call[0] == "post"]
    assert post[0][2]["timeout"] == (3, 120)
    registry = MCPRegistry.__new__(MCPRegistry)
    registry._tools = {h.tool.name: h.tool}
    assert registry.tool_timeout("rag_search") == 128
    assert sum(post[0][2]["timeout"]) < registry.tool_timeout("rag_search") <= 128


@pytest.mark.parametrize("budget", [1, 1.25, 20, 20.5, 119.9, 120])
def test_configured_read_budget_is_bounded_and_outer_keeps_margin(budget):
    h = harness({"query_timeout_seconds": budget})
    h.tool.execute(**arguments())
    timeout = next(call[2]["timeout"] for call in h.calls if call[0] == "post")
    assert timeout == (3, budget)
    assert h.tool.timeout_seconds == math.ceil(3 + budget + 5)
    assert 1 <= timeout[1] <= 120 and sum(timeout) < h.tool.timeout_seconds <= 128


@pytest.mark.parametrize("bad,expected", [
    (None, 120), (True, 120), (False, 120), ("abc", 120), (PRIVATE, 120), ("500", 120),
    ({}, 120), ([], 120), (float("nan"), 120), (float("inf"), 120), (float("-inf"), 120),
    (0, 1), (-1, 1), (.5, 1), (120.1, 120), (121, 120), (500, 120), (10 ** 1000, 120),
])
def test_bad_config_warns_and_falls_back_instead_of_dropping_both_rag_tools(bad, expected, capsys):
    # 便利参数不拒启：import 期抛会让 MCPRegistry 连带丢掉 rag_search 与
    # rag_get_context，模型侧表现为检索能力凭空消失——比一个坏超时值严重得多。
    h = harness({"query_timeout_seconds": bad})
    assert h.ns["RAG_QUERY_READ_TIMEOUT_SECONDS"] == expected
    assert h.ns["RAGSearchTool"]().name == "rag_search"
    assert h.ns["RagGetContextTool"]().name == "rag_get_context"
    h.tool.execute(**arguments())
    timeout = next(call[2]["timeout"] for call in h.calls if call[0] == "post")
    assert timeout == (3, expected)
    assert h.tool.timeout_seconds == math.ceil(3 + expected + 5)
    warning = capsys.readouterr().out
    assert warning.count("\n") == 1 and "rag.query_timeout_seconds" in warning
    assert PRIVATE not in warning   # 警告只说类型/钳后值，不回显原值


@pytest.mark.parametrize("good", [1, 1.5, 119, 120])
def test_valid_config_is_used_verbatim_without_warning(good, capsys):
    h = harness({"query_timeout_seconds": good})
    assert h.ns["RAG_QUERY_READ_TIMEOUT_SECONDS"] == good
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("text,expected", [("60", 60), (" 120 ", 120), ("1.5", 1.5)])
def test_numeric_strings_are_read_as_numbers(text, expected, capsys):
    # Quoted in YAML or written back by a settings writer: still the user's number.
    h = harness({"query_timeout_seconds": text})
    assert h.ns["RAG_QUERY_READ_TIMEOUT_SECONDS"] == expected
    assert capsys.readouterr().out == ""


def test_read_timeout_reports_the_exhausted_budget_without_body_headers_or_traceback(capsys):
    h = harness()
    def timeout(*args, **kwargs):
        raise requests.ReadTimeout("Authorization: Bearer " + PRIVATE, request=SimpleNamespace(headers={"Authorization": PRIVATE}))
    h.requests.post = timeout
    result = h.tool.execute(**arguments())
    assert result.startswith("System Error:") and "timed out" in result
    assert "connect limit 3s" in result and "read limit 120s" in result
    assert "did not return evidence" in result
    assert PRIVATE not in result and "Traceback" not in result and "Authorization" not in result
    captured = capsys.readouterr()
    assert PRIVATE not in captured.out + captured.err


def test_connect_timeout_keeps_the_actionable_unreachable_message(capsys):
    # ConnectTimeout 同时是 ConnectionError 与 Timeout 的子类：连不上要给
    # 「去看服务起没起」，不能被读超时的预算文案吃掉。
    h = harness()
    def timeout(*args, **kwargs):
        raise requests.ConnectTimeout("Authorization: Bearer " + PRIVATE, request=SimpleNamespace(headers={"Authorization": PRIVATE}))
    h.requests.post = timeout
    result = h.tool.execute(**arguments())
    assert result.startswith("System Error:") and "Unable to connect to the RAG server" in result
    assert "timed out" not in result and "read limit" not in result
    assert PRIVATE not in result and "Traceback" not in result and "Authorization" not in result
    captured = capsys.readouterr()
    assert PRIVATE not in captured.out + captured.err


def test_local_context_keeps_fifteen_second_timeout():
    h = harness()
    h.requests.get = lambda *a, **kw: (
        h.calls.append(("context", a, kw)) or
        SimpleNamespace(json=lambda: {"chunks": []}, raise_for_status=lambda: None))
    assert h.ns["RagGetContextTool"]().execute("synthetic/sample", 1) == "No chunks found for that path."
    assert h.calls[-1][2]["timeout"] == 15


def test_twenty_second_query_survives_real_orchestrator_with_global_fifteen_second_budget(make_session, monkeypatch):
    from framework.core.orchestrator import Orchestrator
    from framework.core.journal import new_turn_id
    from .fake_engine import FakeEngine, call

    h = harness()
    virtual = SimpleNamespace(elapsed=0.)
    def delayed_post(*args, **kwargs):
        connect, read = kwargs["timeout"]
        assert connect == 3
        duration = 20.
        if duration > read:
            raise requests.ReadTimeout(PRIVATE)
        virtual.elapsed += duration
        return h.response
    h.requests.post = delayed_post
    registry = MCPRegistry.__new__(MCPRegistry)
    registry._tools = {h.tool.name: h.tool}
    registry._schemas = [h.tool.get_schema()]
    orch = Orchestrator(FakeEngine([]), registry, {"tool_timeout_seconds": 15,
        "observation_max_chars": 4000})
    observed = []
    async def virtual_wait_for(awaitable, timeout):
        # Execute the real to_thread work, then apply its recorded virtual time
        # against the exact deadline passed by the actual orchestration method.
        before = virtual.elapsed
        result = await awaitable
        observed.append(timeout)
        if virtual.elapsed - before > timeout:
            raise asyncio.TimeoutError
        return result
    monkeypatch.setattr(asyncio, "wait_for", virtual_wait_for)
    session = make_session()
    session.journal.emit("turn_queued", {"prompt": "synthetic", "attachments": [],
                                         "gen": {}, "origin": "chat"}, turn_id=new_turn_id())
    session.journal.emit("turn_started", {})
    result = asyncio.run(orch._invoke_tool(session, call("rag_search", **arguments()), "synthetic-call", session.journal))
    assert orch.tool_timeout_seconds == 15
    assert virtual.elapsed == 20 and observed == [128]
    assert not result["_failed"] and "Celadon=12" in result["response"]
    assert result["render"]["hint"] == "citations"
