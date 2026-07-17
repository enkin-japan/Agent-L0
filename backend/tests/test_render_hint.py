"""工具渲染格金测试（EXECUTION_LEDGER R3）。

守三条边界：

1. **`render` 不进模型上下文**（D.12）。`tool_responses[]` 条目是「模型上下文 +
   渲染源」两用面，靠 `prompt_builder._MODEL_VISIBLE_KEYS` 白名单隔开。漏了 =
   整份引文列表白烧 token、打掉 KV 前缀。
2. **`render` 必须活过落盘**。它不带 `_` 前缀，刷新页面后卡片还得在。
3. **渲染是锦上添花，坏了不许把主链路带下水**：工具已经跑成功，画不出来只能降级
   灰框，绝不能让这次调用看起来像失败。
"""
import asyncio

import pytest

from framework.core.journal import new_turn_id
from framework.mcp.base import BaseTool, ToolOutput
from framework.mcp.registry import MCPRegistry

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_orchestrator_lifecycles import make_orch


class _Cited(BaseTool):
    name = "t_cite"
    description = "d"
    render_hint = "citations"
    parameters = {"properties": {}, "required": []}

    def execute(self) -> ToolOutput:
        return ToolOutput("给模型读的长文本", {"items": [{"source": "a.md"}]})


class _Silent(BaseTool):
    """声明了 hint，但本次没有载荷（如"零命中"）→ 灰框。"""
    name = "t_silent"
    description = "d"
    render_hint = "citations"
    parameters = {"properties": {}, "required": []}

    def execute(self) -> str:
        return "No results were found."


class _Boom(BaseTool):
    name = "t_boom"
    description = "d"
    render_hint = "citations"
    parameters = {"properties": {}, "required": []}

    def execute(self) -> str:
        raise RuntimeError("工具炸了")


class _Plain(BaseTool):
    name = "t_plain"
    description = "d"
    parameters = {"properties": {}, "required": []}

    def execute(self) -> str:
        return "ok"


def _registry(*tools):
    reg = MCPRegistry.__new__(MCPRegistry)
    reg._tools = {t.name: t for t in tools}
    return reg


# ---------- 契约默认值：不声明 = 现状 ----------

def test_not_declaring_a_hint_is_the_status_quo():
    """不声明 render_hint = 现状灰框。这是零破坏增量迁移的全部依据——
    存量工具一个都不用改，execute 照旧返回 str。"""
    assert BaseTool.render_hint is None
    text, render = _registry(_Plain()).execute_tool("t_plain", {})
    assert text == "ok" and render is None


def test_declaring_a_hint_without_a_payload_is_not_an_error():
    """声明了 hint 却没给载荷（零命中等）→ 灰框，不报错。"""
    text, render = _registry(_Silent()).execute_tool("t_silent", {})
    assert render is None and "No results" in text


def test_render_shape_is_hint_plus_payload():
    text, render = _registry(_Cited()).execute_tool("t_cite", {})
    assert text == "给模型读的长文本"
    assert render == {"hint": "citations", "payload": {"items": [{"source": "a.md"}]}}


# ---------- fail-open ----------

def test_tool_crash_yields_no_render():
    text, render = _registry(_Boom()).execute_tool("t_boom", {})
    assert text.startswith("System Error") and render is None


def test_unknown_tool_yields_no_render():
    text, render = _registry(_Cited()).execute_tool("nope", {})
    assert text.startswith("System Error") and render is None


# ---------- 与模型上下文的隔离（D.12） ----------

def test_render_never_reaches_the_model():
    """两用面的隔离：render 挂在观察条目上，但模型看不到它。"""
    from framework.dialects.gemma4.prompt_builder import PromptBuilder

    class _Tok:
        special_tokens_map = {}
        def encode(self, s, add_special_tokens=False):
            return [1]

    class _Proc:
        tokenizer = _Tok()

    obs = {"name": "rag_search", "response": "ok",
           "render": {"hint": "citations", "payload": {"items": [{"source": "秘密.md"}]}}}
    out = PromptBuilder(_Proc(), suppress_ghost_thinking=False,
                        env={})._build_tool_responses([obs])
    assert '"render"' not in out and "秘密" not in out


def test_render_survives_persistence():
    """`render` 必须**不带** `_` 前缀：它要随 append_log 落盘，重载会话后卡片还得在。
    `_` 前缀会被 _strip_internal_meta 剥掉 → 刷新即退回灰框，且退得很安静。"""
    from framework.memory.session import _strip_internal_meta
    turn = {"response": [{"role": "tool", "tool_responses": [
        {"name": "rag_search", "response": "ok",
         "render": {"hint": "citations", "payload": {}}, "_safe": True}]}]}
    kept = _strip_internal_meta(turn)["response"][0]["tool_responses"][0]
    assert "render" in kept, "render 必须活过落盘，否则刷新页面卡片就没了"
    assert "_safe" not in kept


# ---------- 编排层接线 ----------

def test_orchestrator_attaches_render_to_the_observation(make_session):
    tool = FakeTool("t_r", result="ok", render={"hint": "citations", "payload": {"items": []}})
    orch = make_orch(FakeEngine([]), FakeRegistry([tool]))
    session = make_session()
    session.journal.emit("turn_queued", {"prompt": "p"}, turn_id=new_turn_id())
    obs = asyncio.run(orch._invoke_tool(session, call("t_r"), "c1", session.journal))
    assert obs["render"] == {"hint": "citations", "payload": {"items": []}}


def test_payload_does_not_ride_the_truncated_text_path(make_session):
    """载荷与观察文本是两条路：文本吃截断（护模型上下文），载荷不吃。

    这条由构造保证——载荷是工具在结构尚在时另行产出的（ToolOutput），从不经过
    截断那一步。若哪天有人改成"从 result_text 里再解析载荷"，本用例会红。
    """
    long_text = "X" * 500
    tool = FakeTool("t_long", result=long_text,
                    render={"hint": "citations", "payload": {"n": len(long_text)}})
    orch = make_orch(FakeEngine([]), FakeRegistry([tool]), observation_max_chars=100)
    session = make_session()
    session.journal.emit("turn_queued", {"prompt": "p"}, turn_id=new_turn_id())
    obs = asyncio.run(orch._invoke_tool(session, call("t_long"), "c2", session.journal))
    assert obs["render"]["payload"]["n"] == 500              # 载荷完好
    assert "observation truncated" in obs["response"]        # 文本照旧截断（头+尾保留式）


def test_failed_tool_gets_no_render(make_session):
    """工具报错时不挂渲染格：那时的 result_text 是错误文本，没有载荷可言。"""
    tool = FakeTool("t_err", raises=RuntimeError("boom"),
                    render={"hint": "citations", "payload": {"x": 1}})
    orch = make_orch(FakeEngine([]), FakeRegistry([tool]))
    session = make_session()
    session.journal.emit("turn_queued", {"prompt": "p"}, turn_id=new_turn_id())
    obs = asyncio.run(orch._invoke_tool(session, call("t_err"), "c3", session.journal))
    assert obs["response"].startswith("System Error")
    assert "render" not in obs, "报错的调用不该带渲染格"


# ---------- 试点：rag_search ----------

def test_rag_pilot_projects_citations():
    from framework.mcp.tools.rag_search import RAGSearchTool
    t = RAGSearchTool.__new__(RAGSearchTool)      # 绕开 __init__ 的分类探活（要连 RAG 服务）
    assert t.render_hint == "citations"

    docs = [{"rerank_score": 0.9123, "document": "正文" * 200,
             "metadata": {"title": "手册.md", "source_path": "/k/手册.md"}},
            {"rerank_score": 0.5, "document": "短文", "metadata": {}}]

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"results": docs}

    import framework.mcp.tools.rag_search as mod
    orig = mod.requests.post
    mod.requests.post = lambda *a, **k: _Resp()
    try:
        out = t.execute(query="q", category="default", optimized_query="q")
    finally:
        mod.requests.post = orig

    assert isinstance(out, ToolOutput)
    # 文本照旧是给模型读的格式化全文（含全文正文，不受卡片摘要影响）
    assert "--- Result 1 (Match Score: 0.91" in out.text and "正文" * 200 in out.text
    items = out.payload["items"]
    assert [i["source"] for i in items] == ["手册.md", "Unknown"]
    assert [i["path"] for i in items] == ["/k/手册.md", "Unknown"]
    assert items[0]["score"] == 0.9123          # 卡片给全精度，文本给两位小数
    assert len(items[0]["snippet"]) == 280      # 卡片只摆摘要
    assert out.payload["query"] == "q"


def test_rag_pilot_no_results_renders_nothing():
    """零命中 → 返回纯 str → 无载荷 → 灰框（卡片区不该出现一张空卡）。"""
    from framework.mcp.tools.rag_search import RAGSearchTool
    t = RAGSearchTool.__new__(RAGSearchTool)

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"results": []}

    import framework.mcp.tools.rag_search as mod
    orig = mod.requests.post
    mod.requests.post = lambda *a, **k: _Resp()
    try:
        out = t.execute(query="q", category="default", optimized_query="q")
    finally:
        mod.requests.post = orig
    assert isinstance(out, str) and "No results" in out
