"""D44 通用实参预检金测试（registry.validate_call 声明核对 + catalog 模式附完整声明；CPU / FakeEngine）。

catalog 模式的前缀里非常驻工具只剩「名字 + 80 字摘要」，模型要先 get_tool_schema 才知道参数——b2 实测
80 run 只取了 3 次、却出了 16 次缺参错误，而当时生产没有任何通用核对：缺参只在 execute(**args) 抛
TypeError 时才冒出来，回执是异常串 + 整段 traceback、没有 schema。这里钉住：缺必填（含 null）/ 未知键
在执行前就被打回、回执措辞固定（harness 的 analyze_orch 按其中的判别串分类）、catalog 模式附该工具的
完整声明而 full 模式只附一句提示、工具自带 validate_arguments 的先后关系、执行异常路径不再把
traceback 喂给模型。
"""
import asyncio
import json
import logging

import pytest

from framework.core import events
from framework.core.journal import new_turn_id
from framework.core.orchestrator import Orchestrator
from framework.mcp.base import BaseTool
from framework.mcp.registry import MCPRegistry

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call

GEN = dict(temperature=0.2, min_p=0.05, enable_thinking=False,
           thinking_budget=2048, max_tokens=4096)

PARAMS = {"type": "object",
          "properties": {"path": {"type": "string", "description": "目标路径"},
                         "page": {"type": "string"}},
          "required": ["path"]}


class _Probe(BaseTool):
    name = "probe"
    description = "Read a probe file."
    parameters = PARAMS

    def __init__(self, parameters=None, checker=None, raises=None):
        if parameters is not None:
            self.parameters = parameters
        self._checker = checker
        self._raises = raises
        self.checked = []
        self.executed = []

    def validate_arguments(self, arguments):
        self.checked.append(dict(arguments))
        return self._checker(arguments) if self._checker else None

    def execute(self, **kw):
        self.executed.append(kw)
        if self._raises is not None:
            raise self._raises
        return "probe-ok"


def _reg(tmp_path, tool):
    reg = MCPRegistry(tools_dir=tmp_path / "tools-empty")
    reg.register(tool)
    return reg


# ====================================================================== registry 层
def test_missing_required_text_exact_and_not_executed(tmp_path):
    t = _Probe()
    reg = _reg(tmp_path, t)
    err = reg.validate_call("probe", {"page": "1"})
    assert err == ("System Error: 'probe' was not executed: missing required argument(s): 'path'."
                   "\nCheck the tool's declaration and call it again.")
    assert "Traceback" not in err
    assert t.executed == [] and t.checked == []           # 核对不过：不执行、也不调工具自带预检
    assert reg.validate_call("probe", {"path": "a"}) is None


def test_null_value_counts_as_missing(tmp_path):
    reg = _reg(tmp_path, _Probe())
    err = reg.validate_call("probe", {"path": None})
    assert err is not None and "missing required argument(s): 'path'" in err
    assert reg.argument_problems("probe", {"path": None}) == (["path"], [])


def test_unknown_key_reported_with_accepted_list(tmp_path):
    reg = _reg(tmp_path, _Probe())
    err = reg.validate_call("probe", {"path": "a", "bogus": 1})
    assert err.startswith("System Error: 'probe' was not executed: unknown argument(s): 'bogus' "
                          "(accepted: path, page).")
    assert "missing required" not in err
    both = reg.validate_call("probe", {"bogus": 1})
    assert ("missing required argument(s): 'path'; unknown argument(s): 'bogus' (accepted: path, page)."
            in both)


@pytest.mark.parametrize("extra", [True, {"type": "string"}])
def test_additional_properties_disables_unknown_check(tmp_path, extra):
    reg = _reg(tmp_path, _Probe(parameters={**PARAMS, "additionalProperties": extra}))
    assert reg.validate_call("probe", {"path": "a", "bogus": 1}) is None
    assert reg.argument_problems("probe", {"bogus": 1}) == (["path"], [])   # 必填照查


def test_additional_properties_false_keeps_unknown_check(tmp_path):
    reg = _reg(tmp_path, _Probe(parameters={**PARAMS, "additionalProperties": False}))
    assert "unknown argument(s): 'bogus'" in reg.validate_call("probe", {"path": "a", "bogus": 1})


def test_no_properties_declared_skips_unknown_check(tmp_path):
    reg = _reg(tmp_path, _Probe(parameters={"required": []}))
    assert reg.validate_call("probe", {"anything": 1}) is None
    reg2 = _reg(tmp_path, _Probe(parameters={}))
    assert reg2.validate_call("probe", {"anything": 1}) is None
    # 空 properties 与没声明同口径：api_tool_factory 把省略的 params 规整成 properties={}，而它的
    # execute(**kwargs) 照单转发任意实参——空 dict 若当「什么都不接受」，用户配的无参 API 工具全被打回
    reg3 = _reg(tmp_path, _Probe(parameters={"properties": {}, "required": []}))
    assert reg3.validate_call("probe", {"anything": 1}) is None


def test_schema_feedback_appended_only_when_passed(tmp_path):
    reg = _reg(tmp_path, _Probe())
    seen = []

    def fb(name):
        seen.append(name)
        return "declaration:probe{...}"
    err = reg.validate_call("probe", {}, schema_feedback=fb)
    assert err == ("System Error: 'probe' was not executed: missing required argument(s): 'path'."
                   "\nFull declaration of 'probe':\ndeclaration:probe{...}")
    assert seen == ["probe"]
    assert "Check the tool's declaration" not in err
    assert reg.validate_call("probe", {"path": "a"}, schema_feedback=fb) is None
    assert seen == ["probe"]                               # 核对通过不渲染
    plain = reg.validate_call("probe", {})
    assert "Full declaration" not in plain and plain.endswith("Check the tool's declaration and call it again.")


def test_schema_feedback_failure_falls_back_to_hint(tmp_path):
    reg = _reg(tmp_path, _Probe())

    def boom(name):
        raise RuntimeError("render broke")
    err = reg.validate_call("probe", {}, schema_feedback=boom)
    assert err.endswith("Check the tool's declaration and call it again.")


def test_opt_in_checker_runs_after_passing_schema_check(tmp_path):
    t = _Probe(checker=lambda a: "System Error: bad path" if a.get("path") == "x" else None)
    reg = _reg(tmp_path, t)
    assert reg.validate_call("probe", {"path": "x"}) == "System Error: bad path"
    assert reg.validate_call("probe", {"path": "ok"}) is None
    assert t.checked == [{"path": "x"}, {"path": "ok"}]
    assert reg.validate_call("probe", {}).startswith("System Error: 'probe' was not executed")
    assert t.checked == [{"path": "x"}, {"path": "ok"}]   # 核对失败时不调


def test_execute_exception_has_no_traceback_in_model_text(tmp_path, caplog):
    reg = _reg(tmp_path, _Probe(raises=ValueError("kaboom " + "z" * 900)))
    with caplog.at_level(logging.WARNING, logger="framework.mcp.registry"):
        text, render = reg.execute_tool("probe", {"path": "a"})
    assert render is None
    assert text.startswith("System Error: Exception occurred while executing 'probe': ValueError: kaboom ")
    assert "Traceback" not in text and 'File "' not in text and "\n" not in text
    assert len(text) == len("System Error: Exception occurred while executing 'probe': ") + 500
    assert any(r.exc_info for r in caplog.records)         # 全栈留在日志


# ====================================================================== 编排层
def _orch(registry, **over):
    cfg = {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
           "observation_max_chars": 4000, "tool_timeout_seconds": 5,
           "max_parse_retries": 3, "thinking_mode": "manual", "goal_verify": False}
    cfg.update(over)
    return Orchestrator(FakeEngine(_SCRIPT()), registry, cfg)


def _SCRIPT():
    return [{"tool_calls": [call("probe", page="2")]},          # 缺必填 path：预检打回
            {"tool_calls": [call("probe", path="/tmp/a")]},      # 修正：照常执行
            {"text": "读到了。"}]


def _run(orch, session):
    session.add_user_message("读一下")
    session.journal.emit("turn_queued", {"prompt": "读一下", "attachments": [], "gen": dict(GEN),
                                         "origin": "chat"}, turn_id=new_turn_id())
    session.journal.emit("turn_started", {})
    return asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))


def _tool_errors():
    p = events.get_path()
    recs = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return [e["detail"] for e in recs if e["type"] == "tool_error"]


def _fed(session):
    return [tr["response"] for m in session.history if m.get("role") == "tool"
            for tr in m.get("tool_responses") or [] if tr.get("name") == "probe"]


def test_catalog_mode_precheck_feeds_full_declaration_then_retry_executes(make_session):
    reg = FakeRegistry([FakeTool("probe", result="probe-ok", parameters=PARAMS)])
    orch = _orch(reg, tool_schema_mode="catalog")
    assert orch.tool_schema_mode == "catalog"
    session = make_session()
    r = _run(orch, session)
    assert r["status"] == "success"
    fed = _fed(session)
    assert fed[0].startswith("System Error: 'probe' was not executed: missing required argument(s): 'path'.")
    assert "\nFull declaration of 'probe':\n" in fed[0]
    # 与 get_tool_schema 同一渲染（FakeEngine 无方言渲染器 → JSON 回退，整段可解析、含声明的参数）
    decl = json.loads(fed[0].split("\nFull declaration of 'probe':\n", 1)[1])
    assert decl[0]["name"] == "probe" and decl[0]["parameters"]["required"] == ["path"]
    assert fed[1] == "probe-ok"
    assert reg.executed == [{"name": "probe", "args": {"path": "/tmp/a"}}]   # 被打回的那次没执行
    te = _tool_errors()
    assert len(te) == 1
    assert te[0]["kind"] == "precheck" and te[0]["schema_fed"] is True
    assert te[0]["missing"] == "path" and te[0]["unknown"] == ""


def test_full_mode_precheck_gives_hint_not_declaration(make_session):
    reg = FakeRegistry([FakeTool("probe", result="probe-ok", parameters=PARAMS)])
    orch = _orch(reg)
    assert orch.tool_schema_mode == "full"
    session = make_session()
    assert _run(orch, session)["status"] == "success"
    fed = _fed(session)
    assert "Full declaration" not in fed[0]
    assert fed[0].endswith("\nCheck the tool's declaration and call it again.")
    te = _tool_errors()
    assert len(te) == 1 and te[0]["schema_fed"] is False and te[0]["missing"] == "path"
    assert reg.executed == [{"name": "probe", "args": {"path": "/tmp/a"}}]


def test_precheck_event_lists_unknown_and_truncates_to_eight(make_session):
    reg = FakeRegistry([FakeTool("probe", result="probe-ok", parameters=PARAMS)])
    orch = Orchestrator(FakeEngine([
        {"tool_calls": [call("probe", path="a", **{f"k{i}": i for i in range(10)})]},
        {"text": "放弃。"}, {"text": "参数不对，没读成。"}]), reg, {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
                                   "observation_max_chars": 4000, "tool_timeout_seconds": 5,
                                   "max_parse_retries": 3, "thinking_mode": "manual",
                                   "goal_verify": False})
    session = make_session()
    _run(orch, session)
    te = _tool_errors()
    assert len(te) == 1 and te[0]["missing"] == ""
    assert te[0]["unknown"] == ",".join(f"k{i}" for i in range(8))
    assert "unknown argument(s): " + ", ".join(repr(f"k{i}") for i in range(10)) in _fed(session)[0]
    assert reg.executed == []


def test_required_key_not_in_properties_is_never_unknown(tmp_path):
    """接受集 = properties ∪ required：声明只把键写进 required 的，传了它不能报未知（F6）。"""
    reg = _reg(tmp_path, _Probe(parameters={"properties": {"path": {"type": "string"}},
                                            "required": ["path", "mode"]}))
    assert reg.argument_problems("probe", {"path": "a", "mode": "r"}) == ([], [])
    err = reg.validate_call("probe", {"path": "a", "x": 1})
    assert "missing required argument(s): 'mode'" in err and "(accepted: path, mode)" in err


# ---------------------------------------------------------------------- 打回 = 失败的调用（F1 / F8）
SAVE = {"properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"]}


def _types():
    p = events.get_path()
    return [json.loads(x)["type"] for x in p.read_text().splitlines()] if p.exists() else []


def test_rejected_call_counts_as_failure_for_swallow_guard(make_session):
    """缺参打回之后模型直接宣称完成：与执行期失败同一待遇——failure_swallow_nudge 推回一次，轮次不算
    success（review_probe/probe_swallow.py 的口径：有无 schema 两条路行为一致）。"""
    reg = FakeRegistry([FakeTool("save_note", result="ok", read_only=False, concurrency_safe=False,
                                 parameters=SAVE)])
    eng = FakeEngine([{"tool_calls": [call("save_note", path="a.txt")]}, {"text": "已保存。"}]
                     + [{"text": "笔记没有保存：缺 content 参数。"}] * 4)
    session = make_session()
    orch = Orchestrator(eng, reg, {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
                                   "observation_max_chars": 4000, "tool_timeout_seconds": 5,
                                   "max_parse_retries": 3, "thinking_mode": "manual",
                                   "goal_verify": False})
    r = _run(orch, session)
    assert _types().count("failure_swallow_nudge") == 1
    assert r["status"] != "success"
    assert reg.executed == []


def test_identical_resend_of_rejected_call_is_blocked_as_duplicate(make_session):
    reg = FakeRegistry([FakeTool("save_note", result="ok", read_only=False, concurrency_safe=False,
                                 parameters=SAVE)])
    eng = FakeEngine([{"tool_calls": [call("save_note", path="a.txt")]},
                      {"tool_calls": [call("save_note", path="a.txt")]},
                      ] + [{"text": "笔记没有保存：缺 content 参数。"}] * 4)
    orch = Orchestrator(eng, reg, {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
                                   "observation_max_chars": 4000, "tool_timeout_seconds": 5,
                                   "max_parse_retries": 3, "thinking_mode": "manual",
                                   "goal_verify": False})
    session = make_session()
    _run(orch, session)
    t = _types()
    assert t.count("duplicate_call") == 1 and t.count("tool_error") == 1
    fed = [tr for m in session.history if m.get("role") == "tool"
           for tr in m.get("tool_responses") or [] if tr.get("name") == "save_note"]
    assert fed[1]["response"].startswith("Duplicate call blocked")
    assert fed[0]["_failed"] is True and fed[1]["_failed"] is True
    assert reg.executed == []


def test_rejected_call_is_not_a_write_barrier_for_supersede(make_session):
    """打回的调用什么也没执行（`_safe`）：它两侧同一目标的两次只读，较旧的照常被取代（F8）。"""
    from framework.memory.compactor import compact_tool_observations
    reg = FakeRegistry([FakeTool("read_thing", result="A" * 600),
                        FakeTool("save_note", result="ok", read_only=False, concurrency_safe=False,
                                 parameters=SAVE)])
    eng = FakeEngine([{"tool_calls": [call("read_thing", path="a")]},
                      {"tool_calls": [call("save_note", path="a.txt")]},
                      {"tool_calls": [call("read_thing", path="a", page="2")]},
                      {"text": "读完了，笔记没存成。"}])
    orch = Orchestrator(eng, reg, {"react_max_steps": 6, "max_total_iterations": 12, "replan_max": 3,
                                   "observation_max_chars": 4000, "tool_timeout_seconds": 5,
                                   "max_parse_retries": 3, "thinking_mode": "manual",
                                   "goal_verify": False})
    session = make_session()
    _run(orch, session)
    rejected = [tr for m in session.history if m.get("role") == "tool"
                for tr in m.get("tool_responses") or [] if tr.get("name") == "save_note"]
    assert rejected and rejected[0]["_safe"] is True
    assert compact_tool_observations(session.history, keep_recent=0, digest_chars=50) == 1
    reads = [tr["response"] for m in session.history if m.get("role") == "tool"
             for tr in m.get("tool_responses") or [] if tr.get("name") == "read_thing"]
    assert reads[0].startswith("[Observation superseded") and reads[1] == "A" * 600


def test_catalog_mode_tool_already_full_in_prefix_gets_hint_not_declaration(make_session):
    """core 工具的完整声明本就在前缀里：打回只给 full 模式尾句，不再重复喂一遍声明（F14）。"""
    reg = FakeRegistry([FakeTool("probe", result="probe-ok", parameters=PARAMS),
                        FakeTool("other", result="x", parameters=PARAMS)])
    orch = _orch(reg, tool_schema_mode="catalog", tool_routing={"top_k": 0, "core": ["probe"]})
    decl = {d["name"]: d for d in orch._tools_decl_for(make_session("probe-s"))}
    assert "parameters" in decl["probe"] and "parameters" not in decl["other"]
    session = make_session()
    assert _run(orch, session)["status"] == "success"
    fed = _fed(session)
    assert "Full declaration" not in fed[0]
    assert fed[0].endswith("\nCheck the tool's declaration and call it again.")
    assert _tool_errors()[0]["schema_fed"] is False


def test_precheck_render_fallback_event_names_its_source(make_session):
    class _Boom:
        def render_tool_declaration(self, tool):
            raise RuntimeError("render broke")
    reg = FakeRegistry([FakeTool("probe", result="probe-ok", parameters=PARAMS)])
    orch = _orch(reg, tool_schema_mode="catalog")
    orch.engine.dialect = _Boom()
    session = make_session()
    _run(orch, session)
    p = events.get_path()
    fb = [json.loads(x)["detail"] for x in p.read_text().splitlines()
          if json.loads(x)["type"] == "tool_schema_render_fallback"]
    assert [d["source"] for d in fb] == ["precheck"]
    assert "\nFull declaration of 'probe':\n" in _fed(session)[0]   # JSON 回退照样附声明
