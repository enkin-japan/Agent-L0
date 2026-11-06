"""W-2 工具披露纯函数金测试（D29–D32，framework/core/tool_disclosure.py）。

守的是：目录行文本的取法、词法路由的确定性与边界、披露装配的两种形态、模式解析的
优先级与 fail-closed，以及真 config.yaml 的分档事实。编排层接线见 test_tool_routing.py。
"""
import pytest

from framework.core import tool_disclosure as td
from framework.core.orchestrator import Orchestrator
from framework.mcp.base import BaseTool

from .fake_engine import FakeEngine, FakeRegistry, FakeTool


def _s(name, desc="", params=None):
    return {"name": name, "description": desc,
            "parameters": params or {"type": "object", "properties": {}, "required": []}}


# ---------- D30：目录行 ----------

def test_summary_explicit_wins_over_description():
    s = _s("deck_init", "Start a presentation deck. Call once.")
    assert td.tool_summary(s, {"deck_init": "  做 PPT：先建演示稿  "}) == "做 PPT：先建演示稿"
    # 空白 summary 视同未声明，回落首句
    assert td.tool_summary(s, {"deck_init": "   "}) == "Start a presentation deck."


def test_summary_falls_back_to_first_sentence():
    assert td.tool_summary(_s("a", "Run a shell command. More text here.")) == "Run a shell command."
    assert td.tool_summary(_s("a", "列出文件。然后别的")) == "列出文件。"
    assert td.tool_summary(_s("a", "Is it ready? yes")) == "Is it ready?"
    assert td.tool_summary(_s("a", "First line\nsecond line")) == "First line"
    # ASCII 点后非空白不算句末：文件名/版本号不把首句截成半个词
    assert td.tool_summary(_s("a", "Edit config.yaml or v1.2 files. Then go.")) == \
        "Edit config.yaml or v1.2 files."
    assert td.tool_summary(_s("a", "  no terminator at all  ")) == "no terminator at all"


def test_summary_hard_cut_at_80_chars():
    long = "x" * 200 + ". tail"
    assert td.tool_summary(_s("a", long)) == "x" * 80
    assert td.tool_summary(_s("a", "d"), {"a": "字" * 120}) == "字" * 80
    assert td.SUMMARY_MAX_CHARS == 80


def test_catalog_entry_has_exactly_name_and_description():
    e = td.catalog_entry(_s("a", "Do it. Details.", {"type": "object", "properties": {"x": {}}}))
    assert e == {"name": "a", "description": "Do it."}


def test_summary_never_enters_get_schema_and_registry_serves_it(tmp_path):
    """summary 不进 schema 字典（full 模式字节不变的前提）；registry 另给。"""
    from framework.mcp.registry import MCPRegistry

    class _T(BaseTool):
        name = "t_tool"
        summary = "  该取它的时候  "
        description = "A tool."

        def execute(self, **kw):
            return "ok"

    class _U(BaseTool):
        name = "u_tool"
        description = "Another tool."

        def execute(self, **kw):
            return "ok"

    assert "summary" not in _T().get_schema()
    reg = MCPRegistry(tmp_path / "empty_tools")
    reg.register(_T())
    reg.register(_U())
    assert reg.get_summaries() == {"t_tool": "该取它的时候"}


def test_all_explicit_summaries_are_one_short_line():
    """D30 写作规范：一行、≤40 字（生产工具类属性）。"""
    from ._tool_fleet import audit
    summaries = audit.collect_summaries(include_rag_admin=True)
    assert len(summaries) >= 47
    bad = {n: s for n, s in summaries.items() if "\n" in s or len(s) > 40}
    assert not bad, f"summary 须一行且 ≤40 字: {bad}"


# ---------- D32：分词与打分 ----------

def test_tokenize_ascii_lower_and_cjk_bigrams():
    assert td.tokenize("演示稿") == ["演示", "示稿"]
    assert td.tokenize("做") == ["做"]                        # 单字串退化为该字
    assert td.tokenize("Export PDF") == ["export", "pdf"]
    toks = td.tokenize("做PPT导出")
    assert "ppt" in toks and "做" in toks and "导出" in toks
    assert td.tokenize("deck_set_slide") == ["deck", "set", "slide", "deck_set_slide"]
    assert td.tokenize("知识库，合同") == ["知识", "识库", "合同"]   # 标点断开 CJK 串


def _corpus():
    return [
        _s("deck_init", "Start a presentation deck."),
        _s("deck_export_pdf", "Export the deck as a PDF file."),
        _s("calendar_add", "Create a calendar event."),
        _s("memory_write", "Save a durable note."),
        _s("update_plan", "Create or revise the plan."),
        _s("zeta_tool", "Nothing relevant."),
    ]


def test_route_tools_deterministic_and_excludes_core_and_meta():
    sm = {"deck_init": "做 PPT：先建演示稿", "deck_export_pdf": "演示稿导出 PDF"}
    kw = dict(core=["memory_write"], top_k=5, exclude=["update_plan"], summaries=sm)
    a = td.route_tools("做个PPT，导出PDF，记个笔记 note plan", _corpus(), **kw)
    b = td.route_tools("做个PPT，导出PDF，记个笔记 note plan", _corpus(), **kw)
    assert a == b
    assert a[0] == "deck_export_pdf"                # 命中 pdf + 导出 两项，分最高
    assert set(a) == {"deck_export_pdf", "deck_init"}
    assert "memory_write" not in a and "update_plan" not in a   # core/元工具不入候选
    assert "zeta_tool" not in a and "calendar_add" not in a     # 得分 0 不入，不补


def test_route_tools_ties_break_by_name():
    docs = [_s("b_tool", "alpha"), _s("a_tool", "alpha"), _s("c_tool", "beta"),
            _s("d_tool", "gamma")]                           # alpha df 2/4，不越 N/2
    assert td.route_tools("alpha", docs, top_k=5) == ["a_tool", "b_tool"]
    assert td.route_tools("alpha", list(reversed(docs)), top_k=1) == ["a_tool"]


def test_route_tools_top_k_bounds():
    assert td.route_tools("pdf deck", _corpus(), top_k=0) == []
    # top_k 远超候选数：只回得分 > 0 的，不补
    got = td.route_tools("deck", _corpus(), top_k=100)
    assert got == ["deck_init", "deck_export_pdf"]     # 同 tf，短文档 BM25 长度归一后分更高
    assert td.route_tools("", _corpus(), top_k=3) == []


def test_query_filter_stopwords_and_high_df():
    """停用词与 df > N/2 的词不参与打分（语言无关的高频剔除）。"""
    docs = [_s("a_tool", "please common alpha"), _s("b_tool", "common beta"),
            _s("c_tool", "common gamma"), _s("d_tool", "delta")]
    assert td.route_tools("please", docs, top_k=3) == []         # 停用词
    assert td.route_tools("common", docs, top_k=3) == []         # 3/4 > N/2
    assert td.route_tools("common alpha", docs, top_k=3) == ["a_tool"]
    cjk = [_s("x", "工具"), _s("y", "工具"), _s("z", "工具"), _s("a", "alpha")]
    assert td.route_tools("工具 alpha", cjk, top_k=3) == ["a"]   # CJK 高频 bigram 同样剔除


def test_high_df_filter_disabled_on_tiny_corpus():
    """N < 4 时不做 df > N/2 剔除：N=1 的唯一命中、N=2 的「两篇都有」都照常计分。"""
    assert td.route_tools("alpha", [_s("a_tool", "alpha")], top_k=3) == ["a_tool"]
    two = [_s("a_tool", "alpha beta"), _s("b_tool", "alpha")]
    assert td.route_tools("alpha", two, top_k=3) == ["b_tool", "a_tool"]   # 短文档分更高
    assert td.HIGH_DF_MIN_DOCS == 4


def _real_routing():
    from framework.utils.config_loader import load_section

    from ._tool_fleet import META_NAMES, audit, collect_schemas
    top_k, core = td.resolve_tool_routing(load_section("agent") or {})
    schemas, summaries = collect_schemas(), audit.collect_summaries()
    return lambda q: td.route_tools(q, schemas, core=core, top_k=top_k,
                                    exclude=META_NAMES, summaries=summaries)


def test_real_tools_english_chitchat_routes_nothing():
    route = _real_routing()
    assert route("Please write a short poem for me") == []
    assert route("Please write a short poem for me and then do it again") == []


def test_real_tools_naming_core_tool_does_not_drag_in_others():
    """直呼常驻工具名（run_shell_command 在 core）：名字切片 run/shell/command 不参与打分。"""
    route = _real_routing()
    got = route("run_shell_command 跑一下 ls")
    assert not [n for n in got if n.startswith("schedule_")], got
    assert "run_applescript" not in got


def test_lexical_scores_bm25_prefers_rarer_terms():
    sc = td.lexical_scores("deck calendar", _corpus())
    assert sc["calendar_add"] > sc["deck_init"] > 0      # calendar 只一篇有 → idf 更高
    assert sc["zeta_tool"] == 0


# ---------- 装配 ----------

def test_disclose_full_returns_original_objects():
    schemas = _corpus()
    out = td.disclose(schemas, mode="full", meta_names=["update_plan"],
                      core=["memory_write"], routed=["deck_init"])
    assert out == schemas and all(a is b for a, b in zip(out, schemas))
    assert out is not schemas                         # 新列表，调用方改它不污染注册表


def test_disclose_catalog_shapes_and_order():
    schemas = _corpus()
    out = td.disclose(schemas, mode="catalog", meta_names=["update_plan"],
                      core=["memory_write"], routed=["deck_export_pdf"],
                      summaries={"calendar_add": "在日历新建日程"})
    names = [s["name"] for s in out]
    assert names == ["update_plan", "memory_write", "deck_export_pdf",
                     "deck_init", "calendar_add", "zeta_tool"]
    by = {s["name"]: s for s in out}
    for full in ("update_plan", "memory_write", "deck_export_pdf"):
        assert by[full] is next(s for s in schemas if s["name"] == full)
    for line in ("deck_init", "calendar_add", "zeta_tool"):
        assert set(by[line]) == {"name", "description"}
    assert by["calendar_add"]["description"] == "在日历新建日程"
    with pytest.raises(ValueError):
        td.disclose(schemas, mode="Catalog")


# ---------- D29 / D31：配置解析 ----------

def test_resolve_mode_priority():
    assert td.resolve_tool_schema_mode(None, None) == "full"
    # `tool_schema_mode:` 写了没给值（YAML null）= 未设，落到下一层
    assert td.resolve_tool_schema_mode({"tool_schema_mode": None},
                                       {"tool_schema_mode": "catalog"}) == "catalog"
    assert td.resolve_tool_schema_mode({}, {"tool_schema_mode": None}) == "full"
    assert td.resolve_tool_schema_mode({}, {"tool_schema_mode": "catalog"}) == "catalog"
    assert td.resolve_tool_schema_mode({"tool_schema_mode": "catalog"},
                                       {"tool_schema_mode": "full"}) == "catalog"
    assert td.resolve_tool_schema_mode({"tool_schema_mode": "full"},
                                       {"tool_schema_mode": "catalog"}) == "full"


@pytest.mark.parametrize("entry, agent", [
    ({"tool_schema_mode": "slim"}, {}),
    ({"tool_schema_mode": ""}, {}),
    ({"tool_schema_mode": True}, {}),
    ({}, {"tool_schema_mode": "Catalog"}),
    ({"tool_schema_mode": "catalog"}, {"tool_schema_mode": "bogus"}),
])
def test_resolve_mode_rejects_invalid(entry, agent):
    with pytest.raises(ValueError):
        td.resolve_tool_schema_mode(entry, agent)


def test_resolve_routing():
    assert td.resolve_tool_routing({}) == (0, ())
    assert td.resolve_tool_routing({"tool_routing": {"top_k": 3, "core": ["a", "b", "a"]}}) == \
        (3, ("a", "b"))
    assert td.resolve_tool_routing({"tool_routing": {"top_k": 0}}) == (0, ())


@pytest.mark.parametrize("routing", [
    {"top_k": -1}, {"top_k": "6"}, {"top_k": True}, {"top_k": 1.5},
    {"top_k": 2, "core": "edit_file"}, {"top_k": 2, "core": [""]}, {"top_k": 2, "extra": 1},
    ["top_k", 2],
])
def test_resolve_routing_rejects_invalid(routing):
    with pytest.raises(ValueError):
        td.resolve_tool_routing({"tool_routing": routing})


def _cfg(**over):
    cfg = {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
           "observation_max_chars": 4000, "tool_timeout_seconds": 5,
           "max_parse_retries": 3, "thinking_mode": "manual"}
    cfg.update(over)
    return cfg


def test_orchestrator_refuses_invalid_top_k_and_mode():
    reg = FakeRegistry([FakeTool("a")])
    with pytest.raises(ValueError):
        Orchestrator(FakeEngine([]), reg, _cfg(tool_routing={"top_k": -2}))
    with pytest.raises(ValueError):
        Orchestrator(FakeEngine([]), reg, _cfg(), model_options={"tool_schema_mode": "slim"})
    with pytest.raises(ValueError):
        Orchestrator(FakeEngine([]), reg, _cfg(tool_schema_mode="compact"))


def test_orchestrator_mode_follows_model_entry():
    reg = FakeRegistry([FakeTool("a")])
    o = Orchestrator(FakeEngine([]), reg, _cfg(tool_schema_mode="full"),
                     model_options={"tool_schema_mode": "catalog"})
    assert o.tool_schema_mode == "catalog"
    assert Orchestrator(FakeEngine([]), reg, _cfg()).tool_schema_mode == "full"


def test_unknown_core_names_warn_once_and_are_skipped(capsys):
    reg = FakeRegistry([FakeTool("edit_file"), FakeTool("other")])
    o = Orchestrator(FakeEngine([]), reg,
                     _cfg(tool_schema_mode="catalog",
                          tool_routing={"top_k": 2, "core": ["edit_file", "rag_admin_x"]}))
    assert o._core_names == ("edit_file",)
    by = {t["name"]: t for t in o._tools_decl}
    assert "parameters" in by["edit_file"]            # 已注册的 core 照常完整
    assert "parameters" not in by["other"]
    o.refresh_tools_declaration()                     # 热重扫：同一名字不重复告警
    out = capsys.readouterr().out
    assert out.count("rag_admin_x") == 1


# ---------- 真 config.yaml（D29 分档事实 + core 名字有效） ----------

def test_real_config_tiers_and_core_names():
    from framework.utils.config_loader import load_section

    from ._tool_fleet import collect_schemas
    inference = load_section("inference")
    agent = load_section("agent") or {}
    tiers = {}
    for entry in inference.get("model_registry") or []:
        tiers.setdefault(entry.get("dialect"), []).append(
            td.resolve_tool_schema_mode(entry, agent))
    assert tiers.get("gemma4") == ["catalog", "catalog"]
    assert tiers.get("qwen35") == ["catalog", "catalog"]   # D44 (iii) 2026-11-05 用户拍板
    top_k, core = td.resolve_tool_routing(agent)
    assert top_k >= 0 and "run_shell_command" in core   # 不钉件数：core 增减属配置决策
    known = {s["name"] for s in collect_schemas()}
    for name in core:
        assert name in known, f"core 中 {name!r} 不在确定性工具集"
