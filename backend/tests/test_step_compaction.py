"""W2 轮内步骤边界压缩金测试（D37；FakeEngine / CPU，不起服务、不载模型）。

A 配置解析与起压判定（core/step_compaction.py）；B 压缩器范围 / 保护窗 / stub_handle
（memory/compactor.py）；C 编排层步骤边界与 hard 钩子（块级前缀缓存 / 不可裁缓存后端）；D 落盘与暂存轮
保全文；E spill 补写；F 引擎全量重渲染计数与发出的 prompt。
"""
import asyncio
import json
import os
import re
import stat
from types import SimpleNamespace

import pytest

from framework.core import context_guard as cg
from framework.core import events
from framework.core import step_compaction as sc
from framework.core.journal import new_turn_id
from framework.core.orchestrator import Orchestrator
from framework.core.scheduler import ClassPolicy, TaskScheduler
from framework.core.spill import resolve_spill_ref, truncate_observation
from framework.core.turn_runner import TurnRunner
from framework.memory.compactor import (compact_tool_observations,
                                        compact_tool_observations_detailed)

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call

GEN = dict(temperature=0.2, min_p=0.05, enable_thinking=False,
           thinking_budget=2048, max_tokens=4096)


def _events(etype=None):
    p = events.get_path()
    recs = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    return [e for e in recs if etype is None or e["type"] == etype]


def _step_events():
    return [e["detail"] for e in _events(sc.EVT_STEP_COMPACT)]


# ====================================================================== A 配置 / 判定
def test_missing_section_is_defaults():
    want = sc.StepCompactionConfig(True, "soft", "hard", 2, 200, True)
    assert sc.resolve_step_compaction({}) == want
    assert sc.resolve_step_compaction(None) == want
    assert sc.resolve_step_compaction({"step_compaction": None}) == want


def test_partial_section_merges_defaults():
    c = sc.resolve_step_compaction({"step_compaction": {"keep_recent": 0, "trigger_level": "hard"}})
    assert (c.keep_recent, c.trigger_level, c.cold_refill_trigger_level, c.digest_chars) == (0, "hard", "hard", 200)


def test_unknown_key_refused_with_both_lists():
    with pytest.raises(ValueError) as e:
        sc.resolve_step_compaction({"step_compaction": {"keep_recnt": 1}})
    assert "keep_recnt" in str(e.value) and "keep_recent" in str(e.value)


def test_non_mapping_section_refused():
    with pytest.raises(ValueError, match="step_compaction"):
        sc.resolve_step_compaction({"step_compaction": ["soft"]})


@pytest.mark.parametrize("key,bad", [
    ("enabled", "yes"), ("enabled", 1), ("enabled", None),
    ("spill_full", "true"), ("spill_full", 0),
    ("trigger_level", "ok"), ("trigger_level", "ceiling"), ("trigger_level", None), ("trigger_level", 1),
    ("cold_refill_trigger_level", "ok"), ("cold_refill_trigger_level", "Hard"),
    ("keep_recent", -1), ("keep_recent", True), ("keep_recent", 1.5), ("keep_recent", "2"), ("keep_recent", None),
    ("digest_chars", 0), ("digest_chars", -5), ("digest_chars", True), ("digest_chars", 2.0), ("digest_chars", "200"),
])
def test_bad_value_refused_names_key(key, bad):
    with pytest.raises(ValueError, match=key):
        sc.resolve_step_compaction({"step_compaction": {key: bad}})


def test_keep_recent_zero_allowed():
    assert sc.resolve_step_compaction({"step_compaction": {"keep_recent": 0}}).keep_recent == 0


def test_repo_config_section_resolves():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "config.yaml").read_text())
    c = sc.resolve_step_compaction(cfg["agent"])
    assert c.enabled is True and c.trigger_level == "soft" and c.cold_refill_trigger_level == "hard"


DEF = sc.resolve_step_compaction({})


@pytest.mark.parametrize("level,block,cold", [
    (None, False, False), ("ok", False, False), ("soft", True, False),
    ("hard", True, True), ("ceiling", True, True),
])
def test_should_compact_matrix_defaults(level, block, cold):
    assert sc.should_compact(DEF, True, level) is block
    assert sc.should_compact(DEF, False, level) is cold


def test_should_compact_respects_trigger_and_enabled():
    hard_remote = sc.resolve_step_compaction({"step_compaction": {"trigger_level": "hard"}})
    assert not sc.should_compact(hard_remote, True, "soft")
    assert sc.should_compact(hard_remote, True, "hard")
    soft_local = sc.resolve_step_compaction({"step_compaction": {"cold_refill_trigger_level": "soft"}})
    assert sc.should_compact(soft_local, False, "soft")
    off = sc.resolve_step_compaction({"step_compaction": {"enabled": False}})
    assert not any(sc.should_compact(off, r, l) for r in (True, False) for l in cg.LEVELS)
    assert sc.trigger_rank(DEF, True) == cg.level_rank("soft")
    assert sc.trigger_rank(DEF, False) == cg.level_rank("hard")


def test_backend_cache_semantics_flags():
    from framework.backends.cuda.backend import CudaVLLMBackend
    from framework.backends.remote.backend import RemoteBackend
    from .fake_engine import FakeBackend
    assert RemoteBackend.block_prefix_cache is True
    assert CudaVLLMBackend.block_prefix_cache is True      # vLLM 自动前缀缓存
    assert FakeBackend().block_prefix_cache is False        # 缺省 = 不可裁（MLX 语义）


# ====================================================================== B 压缩器
def _tool(*resps):
    return {"role": "tool", "tool_responses": [{"name": "r", "response": r} for r in resps]}


def _resp(hist):
    return [tr["response"] for m in hist if m.get("role") == "tool" for tr in m["tool_responses"]]


A, B, C, D = "a" * 500, "b" * 500, "c" * 500, "d" * 500


def _four():
    # 下标：0 user, 1..4 各一条 tool 消息
    return [{"role": "user", "content": "u"}, _tool(A), _tool(B), _tool(C), _tool(D)]


def test_range_leaves_outside_untouched():
    h = _four()
    n = compact_tool_observations(h, keep_recent=0, digest_chars=50, budget_chars=1, start=2, end=4)
    out = _resp(h)
    assert n == 2
    assert out[0] == A and out[3] == D                       # 范围外原样
    assert "Observation compacted" in out[1] and "Observation compacted" in out[2]


def test_keep_recent_counts_whole_history_not_slice():
    h = _four()
    # 范围 [0,4) 含观察 A/B/C；保护窗按全 history 倒数 2 条 = C、D → C 虽在范围内也不动
    n = compact_tool_observations(h, keep_recent=2, digest_chars=50, budget_chars=1, start=0, end=4)
    out = _resp(h)
    assert n == 2
    assert "Observation compacted" in out[0] and "Observation compacted" in out[1]
    assert out[2] == C and out[3] == D


def test_supersede_older_read_outside_range_not_touched():
    def obs(r):
        return {"name": "rd", "response": r, "_safe": True, "_target": "f"}
    h = [{"role": "tool", "tool_responses": [obs(A)]}, {"role": "tool", "tool_responses": [obs(B)]},
         {"role": "tool", "tool_responses": [obs(C)]}]
    n = compact_tool_observations(h, keep_recent=0, digest_chars=50, start=1, end=None)
    out = _resp(h)
    assert out[0] == A                                        # 被取代但在范围外
    assert "superseded" in out[1] and out[2] == C and n == 1


def _rich_history():
    def o(name, r, safe=True, target=None):
        tr = {"name": name, "response": r}
        if safe:
            tr["_safe"] = True
        if target:
            tr["_target"] = target
        return tr
    return [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
            {"role": "tool", "tool_responses": [o("rd", "x" * 900, target="f"), o("ls", "y" * 300)]},
            {"role": "assistant", "content": ""},
            {"role": "tool", "tool_responses": [o("wr", "ok", safe=False), o("rd", "z" * 1200, target="f")]},
            {"role": "tool", "tool_responses": [o("rd", "w" * 700, target="f"), o("big", "q" * 5000)]},
            {"role": "tool", "tool_responses": [o("rd", "v" * 100, target="f")]}]


@pytest.mark.parametrize("kw", [dict(keep_recent=1, budget_chars=2000), dict(keep_recent=2),
                                dict(keep_recent=0, budget_chars=1), dict(keep_recent=6, budget_chars=20000)])
def test_default_range_byte_identical_to_whole_history(kw):
    a, b = _rich_history(), _rich_history()
    na = compact_tool_observations(a, digest_chars=200, **kw)
    nb = compact_tool_observations(b, digest_chars=200, start=0, end=None, stub_handle=None, **kw)
    assert na == nb and a == b
    c = _rich_history()
    d = compact_tool_observations_detailed(c, digest_chars=200, **kw)
    assert d["count"] == na and c == a


def test_detailed_reports_freed_chars():
    h = _four()
    before = sum(len(r) for r in _resp(h))
    d = compact_tool_observations_detailed(h, keep_recent=1, digest_chars=50, budget_chars=1)
    assert d["count"] == 3 and d["freed_chars"] == before - sum(len(r) for r in _resp(h)) > 0


def test_stub_handle_line_appended_or_absent():
    h = _four()
    seen = []

    def cb(tr):
        seen.append(tr["response"])           # 改写前调用：拿到的是原文
        return 'file_extract(path="spill:abc")' if tr["response"] == A else None
    compact_tool_observations(h, keep_recent=2, digest_chars=50, budget_chars=1, stub_handle=cb)
    out = _resp(h)
    assert sorted(seen) == sorted([A, B])
    assert out[0].endswith('\n全文：file_extract(path="spill:abc")')
    assert "全文：" not in out[1] and "Observation compacted" in out[1]


def test_stub_handle_not_called_for_truncated_unspilled_observation():
    seam = truncate_observation("l\n" * 3000, 400, None)     # 截断了、没落盘：内存里只剩头尾
    h = [_tool(seam), _tool(D)]
    called = []
    n = compact_tool_observations(h, keep_recent=1, digest_chars=50, budget_chars=1,
                                  stub_handle=lambda tr: called.append(1) or "X")
    assert n == 1 and called == [] and "全文：" not in _resp(h)[0]


def test_stub_handle_not_called_in_supersede_phase():
    def obs(r):
        return {"name": "rd", "response": r, "_safe": True, "_target": "f"}
    h = [{"role": "tool", "tool_responses": [obs(A)]}, {"role": "tool", "tool_responses": [obs(B)]}]
    called = []
    n = compact_tool_observations(h, keep_recent=0, digest_chars=50,
                                  stub_handle=lambda tr: called.append(1) or "X")
    assert n == 1 and called == []
    assert "superseded" in _resp(h)[0] and "全文：" not in _resp(h)[0]


def test_exempt_names_not_compacted_default_none_compacts():
    def hist():
        return [{"role": "tool", "tool_responses": [{"name": "system", "response": A},
                                                    {"name": "get_tool_schema", "response": B},
                                                    {"name": "cloud_consult", "response": C},
                                                    {"name": "r", "response": D}]},
                _tool("x"), _tool("y")]
    h = hist()
    n = compact_tool_observations(h, keep_recent=2, digest_chars=50, budget_chars=1,
                                  exempt_names={"system", "get_tool_schema", "cloud_consult"})
    assert n == 1 and _resp(h)[:3] == [A, B, C] and "Observation compacted" in _resp(h)[3]
    h2 = hist()
    assert compact_tool_observations(h2, keep_recent=2, digest_chars=50, budget_chars=1) == 4


def test_stub_handle_not_called_for_already_spilled_observation():
    seam = truncate_observation("l\n" * 3000, 400, ("/s/spill/0abc.txt", 6000))
    h = [_tool(seam), _tool(D)]
    called = []
    compact_tool_observations(h, keep_recent=1, digest_chars=50, budget_chars=1,
                              stub_handle=lambda tr: called.append(1) or "X")
    assert called == []
    assert "read it with file_extract(path=\"/s/spill/0abc.txt\"" in _resp(h)[0]


# ====================================================================== C 编排层
class SnapEngine(FakeEngine):
    """每次生成前快照 model-facing 观察与步骤锚（断言「下一次生成看到的是桩」）。"""

    def __init__(self, script):
        super().__init__(script)
        self.snaps = []

    async def generate_once(self, session, **kw):
        self.snaps.append({
            "obs": {tr.get("name"): tr.get("response") for m in session.history
                    if m.get("role") == "tool" for tr in m.get("tool_responses") or []},
            "all": [(tr.get("name"), tr.get("response")) for m in session.history
                    if m.get("role") == "tool" for tr in m.get("tool_responses") or []],
            "anchor": getattr(session, "turn_step_start_idx", None),
            "turn_start": getattr(session, "turn_history_start_idx", None),
            "n_history": len(session.history),
        })
        return await super().generate_once(session, **kw)


BIG = {k: k.upper() * 1000 for k in ("ra", "rb", "rc", "rd", "re", "rf")}
TOOLS = [FakeTool(k, result=v) for k, v in BIG.items()]


def _orch(engine, tools=TOOLS, step=None, guard=None, bpc=False, **over):
    cfg = {"react_max_steps": 4, "max_total_iterations": 14, "replan_max": 3,
           "observation_max_chars": 4000, "tool_timeout_seconds": 5,
           "max_parse_retries": 3, "thinking_mode": "manual", "goal_verify": False}
    if step is not None:
        cfg["step_compaction"] = step
    if guard is not None:
        cfg["context_guard"] = guard
    cfg.update(over)
    engine.backend.block_prefix_cache = bpc
    orch = Orchestrator(engine, FakeRegistry(list(tools)), cfg)
    orch.bind_context_limit()
    return orch


def _begin(session, prompt="p"):
    session.journal.emit("turn_queued", {"prompt": prompt, "attachments": [], "gen": dict(GEN),
                                         "origin": "chat"}, turn_id=new_turn_id())
    session.journal.emit("turn_started", {})


def _run(orch, session):
    session.add_user_message("做点事")
    _begin(session)
    return asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))


def _runner_turn(orch, session):
    runner = TurnRunner(orch, TaskScheduler(policy=ClassPolicy()))

    async def go():
        ctx = await runner.accept_turn(session, session.session_id, prompt="做点事", images=[],
                                       audios=[], ingested_text="", attachments=[], gen=dict(GEN))
        return await runner.run_accepted(session, session.session_id, ctx)
    return asyncio.run(go())


def _log_lines(session):
    return [json.loads(l) for l in session.history_log.read_text(encoding="utf-8").splitlines() if l]


PLAN = {"tool_calls": [call("update_plan", op="replace", steps=["一", "二"])]}


def _remote_script(final=None):
    return [PLAN,
            {"tool_calls": [call("ra"), call("rb")]},
            {"tool_calls": [call("complete_step", result="r1")]},     # 边界 1：档位 ok
            {"tool_calls": [call("rc"), call("rd")]},
            {"tool_calls": [call("complete_step", result="r2"), call("re")]},   # 边界 2：soft
            final or {"text": "完成"}]


REMOTE_MEASURE = [100, 100, 100, 30000, 30000, 30000]


def _stubbed(resp):
    return "Observation compacted" in resp


def test_block_cache_soft_compacts_completed_segment_at_step_boundary(make_session):
    eng = SnapEngine(_remote_script())
    eng.measure_script = list(REMOTE_MEASURE)
    session = make_session()
    r = _run(_orch(eng, bpc=True), session)
    assert r["status"] == "success" and len(eng.calls) == 6
    s = eng.snaps
    # 边界 1（ok）：不压，锚前移到边界 1 那批 tool 消息的下标（assistant 之后一条）
    assert s[3]["anchor"] == s[2]["n_history"] + 1 and s[2]["anchor"] == s[2]["turn_start"]
    assert all(not _stubbed(s[3]["obs"][k]) for k in ("ra", "rb"))
    # 边界 2（soft）：[轮起点, 本批) 的观察折成桩，本批（complete_step + re）不碰；下一次生成看到的是桩
    last = s[5]["obs"]
    for k in ("ra", "rb", "rc", "rd"):
        assert _stubbed(last[k]) and BIG[k] not in last[k], k
    assert last["re"] == BIG["re"]
    ev = _step_events()
    assert len(ev) == 1
    # 恰 4 条读观察：边界 1 的结构提醒（name=system）豁免
    assert (ev[0]["trigger"], ev[0]["level"], ev[0]["count"]) == ("step", "soft", 4)
    assert ev[0]["end"] == s[5]["n_history"] - 1
    assert [r_ for n_, r_ in s[5]["all"] if n_ == "system"] and all(
        not _stubbed(r_) for n_, r_ in s[5]["all"] if n_ == "system")
    assert ev[0]["freed_chars"] > 0 and ev[0]["step_id"] is not None
    assert r["metrics"]["step_compactions"] == 1
    assert r["metrics"]["compaction_freed_chars"] == ev[0]["freed_chars"]
    assert session.turn_step_start_idx == s[5]["n_history"] - 1


def test_batch_observations_unseen_at_boundary_wait_for_next_boundary(make_session):
    """Major 1：complete_step 与 2 条物理调用同批——边界后首次生成里两条都是全文；下一边界才压。"""
    eng = SnapEngine([PLAN,
                      {"tool_calls": [call("ra")]},
                      {"tool_calls": [call("complete_step", result="r1"), call("rb"), call("rc")]},
                      {"tool_calls": [call("complete_step", result="r2")]},
                      {"text": "完成"}])
    eng.measure_script = [30000]
    session = make_session()
    r = _run(_orch(eng, bpc=True), session)
    assert r["status"] == "success"
    after1 = eng.snaps[3]["obs"]
    assert _stubbed(after1["ra"])
    assert after1["rb"] == BIG["rb"] and after1["rc"] == BIG["rc"]
    after2 = eng.snaps[4]["obs"]
    assert _stubbed(after2["rb"]) and _stubbed(after2["rc"])
    assert [e["trigger"] for e in _step_events()] == ["step", "step"]


def test_cold_refill_soft_does_not_compact_at_step_boundary(make_session):
    eng = SnapEngine(_remote_script())
    eng.measure_script = list(REMOTE_MEASURE)
    session = make_session()
    r = _run(_orch(eng, bpc=False), session)
    assert r["status"] == "success"
    assert _step_events() == []
    assert all(v == BIG[k] for k, v in eng.snaps[5]["obs"].items() if k in BIG)
    assert "step_compactions" not in r["metrics"] and "compaction_freed_chars" not in r["metrics"]
    # 锚照样前移（无论压没压）
    assert session.turn_step_start_idx == eng.snaps[5]["n_history"] - 1


def _cold_script():
    return [PLAN,
            {"tool_calls": [call("ra"), call("rb")]},
            {"tool_calls": [call("complete_step", result="r1"), call("rc")]},  # 边界 1：soft，不可裁缓存不压
            {"tool_calls": [call("rd"), call("re"), call("rf")]},             # 进行中步骤
            {"tool_calls": [call("complete_step", result="r2")]},             # 本次生成前首次 hard
            {"text": "完成"}]


def _hard_view_checks(eng):
    hard_view = eng.snaps[4]["obs"]          # hard 钩子之后、第 5 次生成看到的
    for k in ("ra", "rb"):                   # 已完成步骤段（锚之前）
        assert _stubbed(hard_view[k]), k
    # 锚之后（边界 1 批的 rc + 进行中步骤 rd/re/rf）：rc、rd 不在保护窗（最近 2 条 = re、rf）也不动
    for k in ("rc", "rd", "re", "rf"):
        assert hard_view[k] == BIG[k], k


def test_cold_refill_hard_hook_compacts_completed_steps_only(make_session):
    eng = SnapEngine(_cold_script())
    eng.measure_script = [100, 30000, 30000, 30000, 40000, 40050, 40100, 40100]
    session = make_session()
    r = _run(_orch(eng, bpc=False), session)
    assert r["status"] == "success"
    _hard_view_checks(eng)
    ev = _step_events()
    assert [e["trigger"] for e in ev] == ["hard", "step"]
    assert ev[0]["count"] == 2 and ev[0]["end"] == eng.snaps[3]["anchor"]
    # hard 之后的步骤边界（档位 hard ≥ cold_refill 起压档）再压：rc/rd/re 折桩，rf 在保护窗
    final_view = eng.snaps[5]["obs"]
    assert all(_stubbed(final_view[k]) for k in ("rc", "rd", "re")) and final_view["rf"] == BIG["rf"]
    assert r["metrics"]["step_compactions"] == 2
    # 压后重量仍 hard：尾注照发、tokens 用压后值；尾注后再量一次，即发出去的那份
    note = [n for n in eng.calls[4]["tail_notes"] if n.startswith("上下文已用 ")]
    assert note and note[0].startswith("上下文已用 40,050/65,536 token")
    assert len(eng.measure_calls) == 8
    assert eng.measure_calls[6]["tail_notes"] == eng.calls[4]["tail_notes"]
    assert len(_events(cg.EVT_HARD)) == 1


def test_hard_hook_drop_below_hard_skips_tail_note(make_session):
    eng = SnapEngine(_cold_script())
    eng.measure_script = [100, 30000, 30000, 30000, 40000, 30000, 30000]
    r = _run(_orch(eng, bpc=False), make_session())
    assert r["status"] == "success"
    _hard_view_checks(eng)
    assert [e["trigger"] for e in _step_events()][0] == "hard"
    assert len(_events(cg.EVT_HARD)) == 1                   # 事件照旧已发
    assert not [n for n in eng.calls[4]["tail_notes"] if n.startswith("上下文已用 ")]
    # 第 5 次生成前只量两次（压前 40000、压后 30000）：最后量到的就是发出的那份
    assert eng.measure_calls[5]["tail_notes"] == eng.calls[4]["tail_notes"]
    assert len(eng.measure_calls) == 7


def test_hard_hook_noop_without_completed_step(make_session):
    eng = SnapEngine([{"tool_calls": [call("ra")]}, {"text": "完成"}])
    eng.measure_script = [100, 40000, 40100]
    r = _run(_orch(eng, bpc=False), make_session())
    assert r["status"] == "success" and len(_events(cg.EVT_HARD)) == 1
    assert _step_events() == []
    assert eng.snaps[1]["obs"]["ra"] == BIG["ra"]
    # 没压就不重量：尾注照 W1 原样（tokens 为首次测量值）
    assert any(n.startswith("上下文已用 40,000/") for n in eng.calls[1]["tail_notes"])


def test_complete_step_without_active_step_is_not_a_boundary(make_session):
    # 无计划的 complete_step 没有落地（did_complete 判据看的是「有当前步可完成」）
    eng = SnapEngine([{"tool_calls": [call("ra"), call("rb")]},
                      {"tool_calls": [call("complete_step", result="x"), call("rc")]},
                      {"text": "完成"}])
    eng.measure_script = [30000]
    session = make_session()
    _run(_orch(eng, bpc=True), session)
    assert _step_events() == []
    assert session.turn_step_start_idx == session.turn_history_start_idx


class _FailRag(FakeRegistry):
    def execute_tool(self, name, arguments):
        self.executed.append({"name": name, "args": dict(arguments or {})})
        if name == "rag_search":
            return "System Error: timeout", None
        return super().execute_tool(name, arguments)


def test_evidence_gate_blocked_complete_step_does_not_trigger(make_session):
    search = dict(query="青瓷验收", category="sales", optimized_query="青瓷")
    eng = SnapEngine([PLAN,
                      {"tool_calls": [call("ra"), call("rag_search", **search)]},
                      {"tool_calls": [call("complete_step", result="编的")]},
                      {"text": "完成了。"}, {"text": "完成了。"}])
    eng.measure_script = [30000]
    orch = _orch(eng, bpc=True)
    orch.registry = _FailRag(TOOLS + [FakeTool("rag_search")])
    session = make_session()
    _run(orch, session)
    assert "step completion blocked" in str(session.history)
    assert _step_events() == []
    assert session.turn_step_start_idx == session.turn_history_start_idx
    assert all(not _stubbed(sn["obs"].get("ra", "")) for sn in eng.snaps)


def test_disabled_never_compacts_but_w1_ceiling_still_folds(make_session):
    eng = SnapEngine(_remote_script())
    eng.measure_script = [100, 100, 100, 40000, 40100, 40100, 40100]
    _run(_orch(eng, bpc=True, step={"enabled": False}), make_session())
    assert _step_events() == []
    assert all(v == BIG[k] for k, v in eng.snaps[-1]["obs"].items() if k in BIG)
    # W1 ceiling 应急压缩照常
    eng2 = SnapEngine([{"tool_calls": [call("ra")]}, {"tool_calls": [call("rb")]},
                       {"tool_calls": [call("rc")]}, {"text": "完成"}])
    eng2.measure_script = [100, 100, 100, 60000, 30000]
    events.get_path().unlink(missing_ok=True)
    r = _run(_orch(eng2, bpc=True, step={"enabled": False}), make_session("s2"))
    assert r["status"] == "success" and len(_events(cg.EVT_FOLD)) == 1
    assert _stubbed(eng2.snaps[3]["obs"]["ra"]) and _step_events() == []


# ====================================================================== D 落盘 / 暂存轮
def test_step_compaction_never_reaches_history_log(make_session):
    eng = SnapEngine(_remote_script())
    eng.measure_script = list(REMOTE_MEASURE)
    session = make_session()
    rd = _runner_turn(_orch(eng, bpc=True), session)
    assert rd["status"] == "success" and len(_step_events()) == 1
    assert _stubbed([tr["response"] for m in session.history if m.get("role") == "tool"
                     for tr in m["tool_responses"] if tr.get("name") == "ra"][0])
    for resp in (_log_lines(session)[0]["response"], rd["response"]):
        obs = {tr["name"]: tr["response"] for m in resp if m.get("role") == "tool"
               for tr in m["tool_responses"]}
        for k in ("ra", "rb", "rc", "rd", "re"):
            assert obs[k] == BIG[k], k


def test_step_compaction_keeps_staged_turn_full_text_and_anchor_survives_resume(make_session):
    danger = FakeTool("danger", approval=True, read_only=False)
    eng = SnapEngine(_remote_script(final={"tool_calls": [call("danger", x=1)]})
                     + [{"text": "完成"}])
    eng.measure_script = list(REMOTE_MEASURE) + [30000]
    orch = _orch(eng, tools=TOOLS + [danger], bpc=True)
    session = make_session()
    rd = _runner_turn(orch, session)
    assert rd["status"] == "pending_approval" and len(_step_events()) == 1
    staged = session.pending_turn["response"]
    obs = {tr["name"]: tr["response"] for m in staged if m.get("role") == "tool"
           for tr in m["tool_responses"]}
    assert all(obs[k] == BIG[k] for k in ("ra", "rb", "rc", "rd", "re"))
    anchor = session.turn_step_start_idx
    turn_start = session.turn_history_start_idx
    assert anchor > turn_start
    # 审批续跑段：锚与轮统计沿用，不被重置成续跑时的 history 末尾
    resumed = asyncio.run(orch.resume_after_approval(session, approved=True,
                                                     journal=session.journal, **GEN))
    assert resumed["status"] == "success"
    assert eng.snaps[-1]["anchor"] == anchor and eng.snaps[-1]["turn_start"] == turn_start
    assert resumed["metrics"]["step_compactions"] == 1


# ====================================================================== E spill 补写
def _spill_files(session):
    d = session.session_dir / "spill"
    return sorted(d.iterdir()) if d.exists() else []


def test_spill_full_written_and_handle_resolves(make_session):
    eng = SnapEngine(_remote_script())
    eng.measure_script = list(REMOTE_MEASURE)
    session = make_session()
    _run(_orch(eng, bpc=True), session)
    files = _spill_files(session)
    texts = [f.read_text(encoding="utf-8") for f in files]
    # ra..rd 各一份全文（结构提醒 name=system 豁免，不压不写）
    assert sorted(texts) == sorted(BIG[k] for k in ("ra", "rb", "rc", "rd"))
    assert BIG["re"] not in texts                            # 保护窗内的不压、不写
    for f in files:
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    stub = eng.snaps[5]["obs"]["ra"]
    m = re.search(r'\n全文：file_extract\(path="(spill:[0-9a-f]+)"\)$', stub)
    assert m, stub
    p = resolve_spill_ref(session, m.group(1))
    assert p is not None and p.read_text(encoding="utf-8") == BIG["ra"]


def test_spill_full_false_writes_nothing(make_session):
    eng = SnapEngine(_remote_script())
    eng.measure_script = list(REMOTE_MEASURE)
    session = make_session()
    _run(_orch(eng, bpc=True, step={"spill_full": False}), session)
    assert _spill_files(session) == []
    stub = eng.snaps[5]["obs"]["ra"]
    assert _stubbed(stub) and "全文：" not in stub and "file_extract" not in stub


def test_spill_callback_skips_small_observation(make_session):
    orch = _orch(FakeEngine([]))
    session = make_session()
    cb = orch._step_stub_handle(session)
    assert cb({"name": "x", "response": "s" * 200}) is None
    assert _spill_files(session) == []
    h = cb({"name": "x", "response": "s" * 201})
    assert h and h.startswith('file_extract(path="spill:') and len(_spill_files(session)) == 1


def test_spill_callback_failure_returns_none(make_session, monkeypatch, capsys):
    import framework.core.orchestrator as om
    monkeypatch.setattr(om, "spill_observation", lambda *a, **k: None)
    orch = _orch(FakeEngine([]))
    assert orch._step_stub_handle(make_session())({"name": "x", "response": "s" * 500}) is None
    assert "补写 spill 失败" in capsys.readouterr().out


# ====================================================================== F 引擎：全量重渲染计数与发出的 prompt
class _WsTok:
    special_tokens_map = {}

    def encode(self, s, add_special_tokens=False):
        return s.split()


class _CaptureBackend:
    def __init__(self):
        self.processor = SimpleNamespace(tokenizer=_WsTok())
        self.prompts = []

    def stream_generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        yield SimpleNamespace(text="好的", delta=None, prompt_tokens=len(prompt.split()),
                              generation_tokens=1, prompt_tps=1.0, generation_tps=1.0,
                              peak_memory=0.0, extra={})


ENG_TOOLS = [{"name": "read_thing", "description": "read a thing",
              "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                             "required": ["path"]}}]


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_compaction_forces_full_render_with_stub_and_counts_it(dialect):
    from framework.core.engine import AgentEngine
    be = _CaptureBackend()
    eng = AgentEngine(be, kv_config={}, model_options={"dialect": dialect})
    full = "FULLTEXT " * 60
    s = SimpleNamespace(history=[{"role": "system", "content": "你是助手"},
                                 {"role": "user", "content": "读 文件"}],
                        images=[], audios=[], brain_memory_state=None, cancel_requested=False,
                        inject_env_context=True, prompt_stream=None, session_id="w2-s",
                        journal=SimpleNamespace(current_turn_id="turn-1"), turn_full_renders=0)
    a = dict(tools=ENG_TOOLS, task_block="[Task] step 1", enable_thinking=False)
    asyncio.run(eng.generate_once(s, **a))
    assert s.turn_full_renders == 1                          # 首次必全量
    s.history.append({"role": "assistant", "content": "好的"})
    s.history.append({"role": "tool", "tool_responses": [{"name": "read_thing", "response": full}]})
    r2 = asyncio.run(eng.generate_once(s, **a))
    assert r2["metrics"].get("stream_delta") is True and s.turn_full_renders == 1   # 增量路不计
    s.history.append({"role": "assistant", "content": "好的"})
    s.history.append({"role": "tool", "tool_responses": [{"name": "read_thing", "response": "tail obs"}]})
    # 编排层压缩已入流的那条观察（换副本，与 _compact_turn_range 同）
    cp = dict(s.history[3])
    cp["tool_responses"] = [dict(tr) for tr in cp["tool_responses"]]
    s.history[3] = cp
    assert compact_tool_observations(s.history, keep_recent=1, digest_chars=20, budget_chars=1) == 1
    r3 = asyncio.run(eng.generate_once(s, **a))
    assert r3["metrics"].get("stream_delta") is not True and s.turn_full_renders == 2
    sent = be.prompts[-1]
    assert "Observation compacted" in sent and full.strip() not in sent and "tail obs" in sent


def test_full_render_not_counted_without_attribute():
    from framework.core.engine import AgentEngine
    be = _CaptureBackend()
    eng = AgentEngine(be, kv_config={}, model_options={"dialect": "gemma4"})
    s = SimpleNamespace(history=[{"role": "system", "content": "x"}, {"role": "user", "content": "y"}],
                        images=[], audios=[], brain_memory_state=None, cancel_requested=False,
                        inject_env_context=False, prompt_stream=None, session_id="sb",
                        journal=SimpleNamespace(current_turn_id="t"))
    asyncio.run(eng.generate_once(s, tools=ENG_TOOLS, enable_thinking=False))
    assert not hasattr(s, "turn_full_renders")


def test_turn_stats_full_renders_stamped_from_session(make_session):
    class CountingEngine(FakeEngine):
        async def generate_once(self, session, **kw):
            session.turn_full_renders += 1          # 模拟 engine 全量路计数
            return await super().generate_once(session, **kw)
    eng = CountingEngine([{"tool_calls": [call("ra")]}, {"text": "完成"}])
    r = _run(_orch(eng), make_session())
    assert r["metrics"]["full_renders"] == 2
    eng2 = FakeEngine([{"text": "完成"}])
    r2 = _run(_orch(eng2), make_session("s2"))
    assert "full_renders" not in r2["metrics"] and "step_compactions" not in r2["metrics"]
