"""W1 上下文上限护栏金测试（FakeEngine / CPU，不起服务、不载模型）。

A 配置解析与纯函数；B 编排层四档处置（经 Orchestrator.run_turn）；C 引擎量法与生成
共用渲染（防分叉）；D 三后端暴露上限；E inspector 字段。
"""
import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from framework.core import context_guard as cg
from framework.core.journal import new_turn_id
from framework.core.orchestrator import Orchestrator
from framework.core.scheduler import ClassPolicy, TaskScheduler
from framework.core.spill import resolve_spill_ref
from framework.core.turn_runner import TurnRunner
from framework.core import events

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call

GEN = dict(temperature=0.2, min_p=0.05, enable_thinking=False,
           thinking_budget=2048, max_tokens=4096)
CFG = cg.resolve_context_guard({})
LIM = cg.derive_limits(CFG, 65536)            # ceiling 53248


def _events(etype=None):
    p = events.get_path()
    recs = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    return [e for e in recs if etype is None or e["type"] == etype]


def _ctx_events():
    return [e for e in _events() if e["type"].startswith("context_")]


# ====================================================================== A 配置
def test_missing_section_is_defaults():
    assert cg.resolve_context_guard({}) == cg.ContextGuardConfig(True, 24576, 39322, 12288)
    assert cg.resolve_context_guard({"context_guard": None}) == cg.resolve_context_guard(None)


def test_partial_section_merges_defaults():
    c = cg.resolve_context_guard({"context_guard": {"soft_tokens": 1000}})
    assert (c.soft, c.hard, c.reserve, c.enabled) == (1000, 39322, 12288, True)


def test_unknown_key_refused_with_both_lists():
    with pytest.raises(ValueError) as e:
        cg.resolve_context_guard({"context_guard": {"hard_token": 1}})
    assert "hard_token" in str(e.value) and "hard_tokens" in str(e.value)


@pytest.mark.parametrize("soft,hard", [(39322, 39322), (40000, 39322)])
def test_soft_not_below_hard_refused(soft, hard):
    with pytest.raises(ValueError, match="soft_tokens < hard_tokens"):
        cg.resolve_context_guard({"context_guard": {"soft_tokens": soft, "hard_tokens": hard}})


@pytest.mark.parametrize("key", ["soft_tokens", "hard_tokens", "generation_reserve"])
@pytest.mark.parametrize("bad", [True, 1.5, "100", 0, -1, None])
def test_non_positive_int_refused(key, bad):
    with pytest.raises(ValueError, match=key):
        cg.resolve_context_guard({"context_guard": {key: bad}})


def test_enabled_must_be_bool():
    with pytest.raises(ValueError, match="enabled"):
        cg.resolve_context_guard({"context_guard": {"enabled": "yes"}})


def test_derive_limits_ceiling_and_refusal_names_all_four_numbers():
    assert cg.derive_limits(CFG, 65536) == cg.ContextLimits(limit=65536, ceiling=53248)
    with pytest.raises(ValueError) as e:
        cg.derive_limits(CFG, 32768)
    msg = str(e.value)
    for n in ("32768", "12288", "20480", "39322", "agent.context_guard.hard_tokens",
              "inference.mlx.context_limit", "--max-model-len"):
        assert n in msg, n
    with pytest.raises(ValueError):              # hard == ceiling 也拒（须严格小于）
        cg.derive_limits(CFG, 39322 + 12288)


def test_classify_four_levels_with_equal_boundaries():
    assert cg.classify(0, CFG, LIM) == "ok"
    assert cg.classify(24575, CFG, LIM) == "ok"
    assert cg.classify(24576, CFG, LIM) == "soft"
    assert cg.classify(39321, CFG, LIM) == "soft"
    assert cg.classify(39322, CFG, LIM) == "hard"
    assert cg.classify(53247, CFG, LIM) == "hard"
    assert cg.classify(53248, CFG, LIM) == "ceiling"


def test_fallback_estimate():
    assert cg.fallback_estimate("a" * 400) == 100
    assert cg.fallback_estimate("汉" * 100) == 100
    assert cg.fallback_estimate("a" * 400 + "汉" * 100) == 200
    assert cg.fallback_estimate("") == 0


def test_repo_config_resolves_and_derives():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "config.yaml").read_text())
    c = cg.resolve_context_guard(cfg["agent"])
    lim = cg.derive_limits(c, cfg["inference"]["mlx"]["context_limit"])
    assert lim.ceiling == cfg["inference"]["mlx"]["context_limit"] - c.reserve


# ====================================================================== B 编排层
def _orch(engine, tools=(), guard=None, bind=True, **over):
    cfg = {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
           "observation_max_chars": 4000, "tool_timeout_seconds": 5,
           "max_parse_retries": 3, "thinking_mode": "manual", "goal_verify": False}
    if guard is not None:
        cfg["context_guard"] = guard
    cfg.update(over)
    orch = Orchestrator(engine, FakeRegistry(list(tools) or [FakeTool("read_thing")]), cfg)
    if bind:
        orch.bind_context_limit()
    return orch


def _begin(session, prompt="p"):
    session.journal.emit("turn_queued", {"prompt": prompt, "attachments": [], "gen": dict(GEN),
                                         "origin": "chat"}, turn_id=new_turn_id())
    session.journal.emit("turn_started", {})


def _run(orch, session, **gen):
    session.add_user_message("做点事")
    _begin(session)
    return asyncio.run(orch.run_turn(session, journal=session.journal, **{**GEN, **gen}))


BIG = "X" * 1000
BIG_TOOL = FakeTool("read_thing", result=BIG)
HARD_NOTE_HEAD = "上下文已用 "


def test_bind_refuses_incompatible_limit_and_skips_unknown():
    eng = FakeEngine([])
    eng.context_limit = 32768
    with pytest.raises(ValueError, match="ceiling"):
        _orch(eng)
    eng.context_limit = None
    orch = _orch(eng)
    assert orch.context_limits is None


def test_ok_level_measures_same_prompt_args_as_generation(make_session):
    eng = FakeEngine([{"tool_calls": [call("read_thing", path="a")]}, {"text": "完成"}])
    orch = _orch(eng)
    assert orch.context_limits == cg.ContextLimits(65536, 53248)
    r = _run(orch, make_session())
    assert r["status"] == "success"
    assert _ctx_events() == []
    assert len(eng.measure_calls) == len(eng.calls) == 2
    for m, g in zip(eng.measure_calls, eng.calls):     # 防分叉：量的与发的同一份入参
        assert m["tools"] == g["tools"] and m["tools"]  # 完整 schema 内容，不只名字
        assert m["tail_notes"] == g["tail_notes"]
        assert m["task_block"] == g["task_block"]
        assert m["enable_thinking"] == g["kwargs"]["enable_thinking"]


def test_soft_event_once_per_turn(make_session):
    eng = FakeEngine([{"tool_calls": [call("read_thing", path="a")]},
                      {"tool_calls": [call("read_thing", path="b")]}, {"text": "完成"}])
    eng.measure_script = [30000]
    r = _run(_orch(eng), make_session())
    assert r["status"] == "success" and len(eng.calls) == 3
    soft = _events(cg.EVT_SOFT)
    assert len(soft) == 1 and _events(cg.EVT_HARD) == []
    d = soft[0]["detail"]
    assert (d["prompt_tokens"], d["level"], d["limit"], d["ceiling"], d["method"], d["iteration"]) \
        == (30000, "soft", 65536, 53248, "tokenizer", 1)


def test_hard_event_once_and_tail_note_reaches_generation(make_session):
    eng = FakeEngine([{"tool_calls": [call("read_thing", path="a")]}, {"text": "完成"}])
    eng.measure_script = [40000, 40100, 40200]
    r = _run(_orch(eng), make_session())
    assert r["status"] == "success"
    assert len(_events(cg.EVT_HARD)) == 1
    note = [n for n in eng.calls[0]["tail_notes"] if n.startswith(HARD_NOTE_HEAD)]
    assert note == ["上下文已用 40,000/65,536 token，接近上限。请收敛：少回灌大段观察，"
                    "尽快给出结论或完成当前步骤。"]
    # 尾注追加后重量：最后一次测量的入参与真正发出的那次逐项一致
    assert eng.measure_calls[1]["tail_notes"] == eng.calls[0]["tail_notes"]
    # 一次性尾注：第二次生成不再带
    assert not [n for n in eng.calls[1]["tail_notes"] if n.startswith(HARD_NOTE_HEAD)]


def _three_reads_then(final):
    return [{"tool_calls": [call("read_thing", path=p)]} for p in ("a", "b", "c")] + final


def test_ceiling_once_folds_observations_keeps_persisted_text(make_session):
    eng = FakeEngine(_three_reads_then([{"text": "完成"}]))
    eng.measure_script = [100, 100, 100, 60000, 30000]
    orch = _orch(eng, tools=[BIG_TOOL])
    session = make_session()
    start = len(session.history)
    r = _run(orch, session)
    assert r["status"] == "success" and len(eng.calls) == 4    # 压缩后照常生成
    fold = _events(cg.EVT_FOLD)
    assert len(fold) == 1
    d = fold[0]["detail"]
    assert (d["before"], d["after"], d["ceiling"]) == (60000, 30000, 53248) and d["compacted"] >= 1
    tool_msgs = [m for m in session.history if m.get("role") == "tool"]
    first = tool_msgs[0]["tool_responses"][0]["response"]
    assert "Observation compacted" in first and len(first) < len(BIG)
    # 保护窗口（最近 2 条）原文不动
    assert tool_msgs[-1]["tool_responses"][0]["response"] == BIG
    # 落盘视图拿全文
    persisted = [m for m in session.persistable_messages(start) if m.get("role") == "tool"]
    assert all(m["tool_responses"][0]["response"] == BIG for m in persisted)
    assert any(a["kind"] == "note" and "已折叠旧观察" in a["label"]
               for a in session.activity.get("steps", []))


def test_ceiling_after_compaction_aborts_without_generation(make_session):
    eng = FakeEngine([{"text": "不该走到这里"}])
    eng.measure_script = [60000, 60000]
    session = make_session()
    r = _run(_orch(eng), session)
    assert r["status"] == "warning"
    assert eng.calls == [] and len(eng.script) == 1          # 后端没收到请求
    last = session.history[-1]
    assert last["role"] == "assistant" and "上下文已达模型上限" in last["content"]
    assert "60,000 / 上限 65,536" in last["content"] and "生成预留 12,288" in last["content"]
    assert "上下文已达模型上限" in r["response"]
    aborts = _events("turn_abort")
    assert len(aborts) == 1 and aborts[0]["detail"]["reason"] == "context_guard"
    assert aborts[0]["detail"]["ceiling"] == 53248 and aborts[0]["detail"]["limit"] == 65536
    assert (aborts[0]["detail"]["compacted"], aborts[0]["detail"]["spilled"]) == (0, 0)
    assert session.activity["phase"] == "done"
    assert _events(cg.EVT_FOLD) == []


def test_second_ceiling_in_turn_aborts_without_another_compaction(make_session):
    eng = FakeEngine([{"tool_calls": [call("read_thing", path="a")]}, {"text": "不该走到这里"}])
    eng.measure_script = [60000, 30000, 60000]
    session = make_session()
    r = _run(_orch(eng), session)
    assert r["status"] == "warning" and len(eng.calls) == 1
    assert len(_events(cg.EVT_FOLD)) == 1
    assert len(eng.measure_calls) == 3                        # 第二次 ceiling 不再压缩、不再重量
    assert _events("turn_abort")[0]["detail"]["reason"] == "context_guard"
    assert "上下文已达模型上限" in session.history[-1]["content"]


# ---- W1b：ceiling 应急压缩的桩带回读句柄（不看 step_compaction.spill_full） ----
_CEIL_HANDLE_RE = re.compile(r'\n全文：file_extract\(path="(spill:[0-9a-f]+(?:-[0-9]+)?)"\)$')


def _spill_files(session):
    d = session.session_dir / "spill"
    return sorted(d.iterdir()) if d.exists() else []


def _tool_obs(session):
    return [(tr["name"], tr["response"]) for m in session.history if m.get("role") == "tool"
            for tr in m["tool_responses"]]


def test_ceiling_compaction_stubs_carry_spill_handles(make_session):
    # 每条 ~600 字：> observation_compact_chars(200)、< observation_max_chars(4000)，入口不落盘
    texts = {p: "\n".join(f"{p}-fact-{i:03d} " + p * 30 for i in range(15)) for p in "abcd"}
    tools = [FakeTool(f"read_{p}", result=t) for p, t in texts.items()]
    eng = FakeEngine([{"tool_calls": [call(f"read_{p}", path=p)]} for p in "abcd"]
                     + [{"text": "完成"}])
    eng.measure_script = [100, 100, 100, 100, 60000, 30000]
    orch = _orch(eng, tools=tools, react_max_steps=8, step_compaction={"spill_full": False})
    session = make_session()
    r = _run(orch, session)
    assert r["status"] == "success" and len(eng.calls) == 5
    obs = _tool_obs(session)
    folded = [(n, o) for n, o in obs if "Observation compacted" in o]
    assert [n for n, _ in folded] == ["read_a", "read_b"]        # 保护窗最近 2 条不压
    for name, stub in folded:
        m = _CEIL_HANDLE_RE.search(stub)
        assert m, stub
        f = resolve_spill_ref(session, m.group(1))
        assert f is not None and f.read_text(encoding="utf-8") == texts[name[-1]]
    assert [o for n, o in obs[2:]] == [texts["c"], texts["d"]]
    assert len(_spill_files(session)) == 2
    fold = _events(cg.EVT_FOLD)
    assert len(fold) == 1 and fold[0]["detail"]["compacted"] == 2
    assert fold[0]["detail"]["spilled"] == 2


def _four_reads_ceiling(eng_measure, tools, make_session, session_id="t-session"):
    eng = FakeEngine([{"tool_calls": [call(f"read_{p}", path=p)]} for p in "abcd"]
                     + [{"text": "完成"}])
    eng.measure_script = list(eng_measure)
    session = make_session(session_id)
    r = _run(_orch(eng, tools=tools, react_max_steps=8), session)
    return r, session


def test_ceiling_compaction_reuses_spill_file_for_same_text(make_session):
    # 两个会话对象共用同一会话目录（≈ 重建/恢复后旧轮全文回到内存再被 ceiling 压一次）
    texts = {p: "\n".join(f"{p}-fact-{i:03d} " + p * 30 for i in range(15)) for p in "abcd"}
    tools = [FakeTool(f"read_{p}", result=t) for p, t in texts.items()]
    handles = []
    for _ in range(2):
        r, session = _four_reads_ceiling([100, 100, 100, 100, 60000, 30000], tools, make_session)
        assert r["status"] == "success"
        stub = next(o for n, o in _tool_obs(session) if n == "read_a")
        handles.append(_CEIL_HANDLE_RE.search(stub).group(1))
    assert handles[0] == handles[1]
    assert len(_spill_files(session)) == 2                     # a、b 各一份，第二次不重写
    f = resolve_spill_ref(session, handles[0])
    assert f is not None and f.read_text(encoding="utf-8") == texts["a"]
    assert [e["detail"]["spilled"] for e in _events(cg.EVT_FOLD)] == [2, 2]


def test_spill_callback_same_hash_different_content_gets_fresh_file(make_session):
    import hashlib
    orch, session = _orch(FakeEngine([])), make_session()
    text = "t" * 500
    h = hashlib.sha256(text.encode()).hexdigest()[:16]
    d = session.session_dir / "spill"
    d.mkdir(parents=True)
    (d / f"{h}.txt").write_text("别的内容", encoding="utf-8")   # 同名异内容：不得复用
    cb = orch._spill_stub_handle(session, digest_chars=200, label="t")
    assert cb({"name": "x", "response": text}) == f'file_extract(path="spill:{h}-1")'
    assert (d / f"{h}-1.txt").read_text(encoding="utf-8") == text
    assert (d / f"{h}.txt").read_text(encoding="utf-8") == "别的内容"
    assert cb({"name": "x", "response": text}) == f'file_extract(path="spill:{h}-1")'
    assert len(_spill_files(session)) == 2 and cb.spilled == 2


def test_ceiling_abort_after_compaction_reports_counts(make_session):
    tools = [FakeTool(f"read_{p}", result=BIG) for p in "abcd"]
    r, session = _four_reads_ceiling([100, 100, 100, 100, 60000, 60000], tools, make_session)
    assert r["status"] == "warning"
    aborts = _events("turn_abort")
    assert len(aborts) == 1 and aborts[0]["detail"]["reason"] == "context_guard"
    assert (aborts[0]["detail"]["compacted"], aborts[0]["detail"]["spilled"]) == (2, 2)
    assert len(_spill_files(session)) == 1                     # 两条同文 BIG：同一份文件
    assert _events(cg.EVT_FOLD) == []


def test_ceiling_compaction_short_observation_has_no_handle(make_session):
    short = "s" * 150                                   # ≤ digest：桩里已是全文，不写盘不给句柄
    eng = FakeEngine(_three_reads_then([{"text": "完成"}]))
    eng.measure_script = [100, 100, 100, 60000, 30000]
    orch = _orch(eng, tools=[FakeTool("read_thing", result=short)])
    session = make_session()
    r = _run(orch, session)
    assert r["status"] == "success"
    assert [o for _, o in _tool_obs(session)] == [short] * 3
    assert _spill_files(session) == []
    d = _events(cg.EVT_FOLD)[0]["detail"]
    assert d["compacted"] == 0 and d["spilled"] == 0
    # ceiling 用的回调本身：正文恰为 digest 长不给句柄，多一字才写
    cb = orch._spill_stub_handle(session, digest_chars=orch.observation_compact_chars, label="t")
    assert cb({"name": "x", "response": "s" * 200}) is None and cb.spilled == 0
    assert _spill_files(session) == []
    h = cb({"name": "x", "response": "s" * 201})
    assert h and h.startswith('file_extract(path="spill:') and cb.spilled == 1


def test_ceiling_compaction_without_session_dir_unchanged(make_session):
    eng = FakeEngine(_three_reads_then([{"text": "完成"}]))
    eng.measure_script = [100, 100, 100, 60000, 30000]
    orch = _orch(eng, tools=[BIG_TOOL])
    session = make_session()
    session.session_dir = None
    assert orch._spill_stub_handle(session, digest_chars=200, label="t") is None
    r = _run(orch, session)
    assert r["status"] == "success" and len(eng.calls) == 4
    first = _tool_obs(session)[0][1]
    assert first == BIG[:200] + " …[Observation compacted; originally 1000 chars]"
    d = _events(cg.EVT_FOLD)[0]["detail"]
    assert d["compacted"] == 1 and d["spilled"] == 0


def test_ceiling_compaction_spill_failure_falls_back_to_plain_stub(make_session, monkeypatch, capsys):
    import framework.core.orchestrator as om

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(om, "spill_observation", boom)
    eng = FakeEngine([{"tool_calls": [call("read_thing", path=p)]} for p in "abcd"]
                     + [{"text": "完成"}])
    eng.measure_script = [100, 100, 100, 100, 60000, 30000]
    session = make_session()
    r = _run(_orch(eng, tools=[BIG_TOOL], react_max_steps=8), session)
    assert r["status"] == "success"
    folded = [o for _, o in _tool_obs(session) if "Observation compacted" in o]
    assert len(folded) == 2 and not any("全文：" in o for o in folded)
    d = _events(cg.EVT_FOLD)[0]["detail"]
    assert d["compacted"] == 2 and d["spilled"] == 0
    assert capsys.readouterr().out.count("补写 spill 失败") == 1          # 告警一次


def test_max_tokens_clamped_to_reserve_once(make_session):
    eng = FakeEngine([{"tool_calls": [call("read_thing", path="a")]}, {"text": "完成"}])
    _run(_orch(eng), make_session(), max_tokens=20000)
    assert [c["kwargs"]["max_tokens"] for c in eng.calls] == [12288, 12288]
    clamp = _events(cg.EVT_CLAMP)
    assert len(clamp) == 1 and clamp[0]["detail"] == {"requested": 20000, "clamped_to": 12288}


def test_max_tokens_within_reserve_untouched(make_session):
    eng = FakeEngine([{"text": "完成"}])
    _run(_orch(eng), make_session(), max_tokens=4096)
    assert eng.calls[0]["kwargs"]["max_tokens"] == 4096
    assert _events(cg.EVT_CLAMP) == []


def test_peak_and_level_stamped_into_turn_stats(make_session):
    eng = FakeEngine([{"tool_calls": [call("read_thing", path="a")]},
                      {"tool_calls": [call("read_thing", path="b")],
                       "metrics": {"prompt_tokens": 31000, "generation_tokens": 20}},
                      {"text": "完成"}])
    eng.measure_script = [10000, 30000, 20000]
    session = make_session()
    r = _run(_orch(eng, guard={"soft_tokens": 20000, "hard_tokens": 25000}), session)
    assert r["status"] == "success"
    assert session.turn_peak_prompt_tokens == 31000
    assert r["metrics"]["peak_prompt_tokens"] == 31000
    assert r["metrics"]["context_guard_level"] == "hard"


def test_peak_resets_on_new_turn(make_session):
    eng = FakeEngine([{"text": "一", "metrics": {"prompt_tokens": 5000}},
                      {"text": "二", "metrics": {"prompt_tokens": 700}}])
    orch, session = _orch(eng), make_session()
    _run(orch, session)
    assert session.turn_peak_prompt_tokens == 5000
    r = _run(orch, session)
    assert session.turn_peak_prompt_tokens == 700 and r["metrics"]["peak_prompt_tokens"] == 700


def test_disabled_guard_skips_measure_but_still_clamps(make_session):
    eng = FakeEngine([{"text": "完成"}])
    eng.measure_script = [60000]
    r = _run(_orch(eng, guard={"enabled": False}), make_session(), max_tokens=20000)
    assert r["status"] == "success"
    assert eng.measure_calls == []
    assert eng.calls[0]["kwargs"]["max_tokens"] == 12288
    assert [e["type"] for e in _ctx_events()] == [cg.EVT_CLAMP]


def test_unbound_limit_warns_once_and_generates(make_session, capsys):
    eng = FakeEngine([{"tool_calls": [call("read_thing", path="a")]}, {"text": "完成"}])
    eng.context_limit = None
    eng.measure_script = [60000]
    r = _run(_orch(eng), make_session())
    assert r["status"] == "success" and len(eng.calls) == 2
    assert eng.measure_calls == []
    assert len(_events(cg.EVT_UNBOUND)) == 1
    assert "上下文上限未绑定" in capsys.readouterr().out


def _runner_turn(engine, session, tools):
    orch = _orch(engine, tools=tools)
    runner = TurnRunner(orch, TaskScheduler(policy=ClassPolicy()))

    async def go():
        ctx = await runner.accept_turn(session, session.session_id, prompt="做点事", images=[],
                                       audios=[], ingested_text="", attachments=[], gen=dict(GEN))
        return await runner.run_accepted(session, session.session_id, ctx)
    return asyncio.run(go())


def _log_lines(session):
    return [json.loads(l) for l in session.history_log.read_text(encoding="utf-8").splitlines() if l]


def test_emergency_compaction_never_reaches_history_log(make_session):
    eng = FakeEngine(_three_reads_then([{"text": "完成"}]))
    eng.measure_script = [100, 100, 100, 60000, 30000]
    session = make_session()
    rd = _runner_turn(eng, session, [BIG_TOOL])
    assert rd["status"] == "success" and len(_events(cg.EVT_FOLD)) == 1
    # 内存 model-facing 已压缩……
    assert "Observation compacted" in [m for m in session.history
                                       if m.get("role") == "tool"][0]["tool_responses"][0]["response"]
    # ……落盘日志与响应保留全文
    for resp in (_log_lines(session)[0]["response"], rd["response"]):
        obs = [tr["response"] for m in resp if m.get("role") == "tool" for tr in m["tool_responses"]]
        assert obs == [BIG, BIG, BIG]


def test_emergency_compaction_keeps_staged_turn_full_text(make_session):
    danger = FakeTool("danger", approval=True, read_only=False)
    eng = FakeEngine(_three_reads_then([{"tool_calls": [call("danger", x=1)]}]))
    eng.measure_script = [100, 100, 100, 60000, 30000]
    session = make_session()
    rd = _runner_turn(eng, session, [BIG_TOOL, danger])
    assert rd["status"] == "pending_approval" and len(_events(cg.EVT_FOLD)) == 1
    staged = session.pending_turn["response"]
    obs = [tr["response"] for m in staged if m.get("role") == "tool" for tr in m["tool_responses"]]
    assert obs == [BIG, BIG, BIG]


# ====================================================================== C 引擎量法
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


def _engine(backend, dialect):
    from framework.core.engine import AgentEngine
    return AgentEngine(backend, kv_config={}, model_options={"dialect": dialect})


def _session(images=()):
    return SimpleNamespace(history=[{"role": "system", "content": "你是助手 system prompt"},
                                    {"role": "user", "content": "帮我 读 一下 文件 please"},
                                    {"role": "assistant", "content": "",
                                     "tool_calls": [call("read_thing", path="a")]},
                                    {"role": "tool", "tool_responses": [
                                        {"name": "read_thing", "response": "line one\nline two"}]}],
                           images=list(images), audios=[], brain_memory_state=None,
                           cancel_requested=False, inject_env_context=True,
                           prompt_stream=None, session_id="cg-s",
                           journal=SimpleNamespace(current_turn_id="turn-1"))


TOOLS = [{"name": "read_thing", "description": "read a thing",
          "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                         "required": ["path"]}}]


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
@pytest.mark.parametrize("thinking", [False, True])
def test_measure_equals_tokens_of_prompt_sent_to_backend(dialect, thinking):
    be = _CaptureBackend()
    eng = _engine(be, dialect)
    args = dict(tools=TOOLS, task_block="[Task] step 1 of 2",
                tail_notes=["尾注 一 条"], enable_thinking=thinking)
    m = eng.measure_prompt(_session(), **args)
    asyncio.run(eng.generate_once(_session(), **args))
    assert len(be.prompts) == 1
    assert m.method == "tokenizer" and m.images == 0
    assert m.tokens == len(be.prompts[0].split())
    assert m.chars == len(be.prompts[0])


def test_measure_without_tokenizer_uses_fallback_estimate():
    be = _CaptureBackend()
    eng = _engine(be, "gemma4")
    s = _session()
    inc, extra = eng._prompt_env(s)
    text = eng._render_full_prompt(s, TOOLS, enable_thinking=False, task_block=None,
                                   tail_notes=None, include_env=inc, env_extra=extra)
    be.processor = None
    m = eng.measure_prompt(s, tools=TOOLS)
    assert m.method == "chars" and m.tokens == cg.fallback_estimate(text)


def test_measure_adds_image_estimate():
    eng = _engine(_CaptureBackend(), "gemma4")
    base = eng.measure_prompt(_session(), tools=TOOLS)
    with_img = eng.measure_prompt(_session(images=["/tmp/x.png"]), tools=TOOLS)
    assert with_img.images == 1
    assert with_img.tokens - base.tokens == cg.IMAGE_TOKENS_EST


def test_measure_does_not_touch_session_state():
    eng = _engine(_CaptureBackend(), "gemma4")
    s = _session()
    sentinel = SimpleNamespace(stream=None)     # 无效流：判定退全量，但对象身份可辨
    s.prompt_stream = sentinel
    before = json.dumps(s.history, ensure_ascii=False, sort_keys=True)
    eng.measure_prompt(s, tools=TOOLS)
    assert s.prompt_stream is sentinel
    assert json.dumps(s.history, ensure_ascii=False, sort_keys=True) == before


@pytest.mark.parametrize("value,expected", [(40960, 40960), (None, None), (0, None),
                                            (-1, None), (True, None), ("65536", None)])
def test_engine_context_limit_property(value, expected):
    be = _CaptureBackend()
    eng = _engine(be, "gemma4")
    if value is not None:
        be.context_limit = value
    assert eng.context_limit == expected


# ====================================================================== D 后端
class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture
def remote_load(monkeypatch):
    import framework.backends.remote.backend as mod

    class _Session:
        payload = {}
        gets = 0

        def __init__(self):
            self.headers = {}

        def get(self, url, timeout=None):
            assert url.endswith("/health")
            _Session.gets += 1
            return _Resp(_Session.payload)

    monkeypatch.setattr(mod.requests, "Session", _Session)
    monkeypatch.setattr("transformers.AutoTokenizer.from_pretrained",
                        staticmethod(lambda p: object()))
    monkeypatch.setattr("framework.utils.model_selection.resolve_endpoints",
                        lambda cfg=None: [{"name": "main", "repo": "org/m", "revision": None,
                                           "host": "127.0.0.1", "port": 1}])

    def load(payload, **cfg):
        _Session.payload = payload
        _Session.gets = 0
        be = asyncio.run(mod.RemoteBackend.load(
            "/nonexistent-tokenizer-dir", remote={"host": "127.0.0.1", "port": 1}, **cfg))
        return be, _Session.gets
    return load


def test_remote_adopts_health_max_model_len(remote_load):
    be, gets = remote_load({"status": "ok", "inference_options": {"max_model_len": 40960}},
                           cuda={"max_model_len": 32768})
    assert be.context_limit == 40960 and gets == 1           # 复用同一次 /health
    assert be.context_limit_source == "health"
    assert _events("context_limit_fallback") == []


@pytest.mark.parametrize("payload", [{"status": "ok"}, {"inference_options": None},
                                     {"inference_options": {"max_model_len": 0}},
                                     {"inference_options": {"max_model_len": True}},
                                     "not-a-dict"])
def test_remote_falls_back_to_executor_cfg_with_event(remote_load, payload, capsys):
    be, _ = remote_load(payload, cuda={"max_model_len": 32000})
    assert be.context_limit == 32000
    assert be.context_limit_source == "config"
    ev = _events("context_limit_fallback")
    assert len(ev) == 1 and ev[0]["detail"] == {"source": "config", "value": 32000}
    assert "回落" in capsys.readouterr().out


def test_remote_falls_back_to_default_without_cfg(remote_load):
    be, _ = remote_load({"status": "ok"})
    assert be.context_limit == 65536
    assert be.context_limit_source == "default"
    assert _events("context_limit_fallback")[0]["detail"] == {"source": "default", "value": 65536}


def test_mlx_backend_reads_cfg_key():
    from framework.backends.mlx.backend import MLXBackend
    assert MLXBackend(None, None, {"mlx": {"context_limit": 40000}}).context_limit == 40000
    assert MLXBackend(None, None, {}).context_limit == 65536


def test_cuda_context_limit_and_receipt():
    from framework.backends.cuda.backend import CudaVLLMBackend
    from framework.backends.cuda.options import engine_options, public_options
    assert CudaVLLMBackend({"cuda": {"max_model_len": 40960}}).context_limit == 40960
    assert CudaVLLMBackend({}).context_limit == 32768
    receipt = public_options(engine_options({}, {}), max_model_len=40960)
    assert receipt["max_model_len"] == 40960
    assert "max_model_len" not in public_options(engine_options({}, {}))


# ====================================================================== E inspector
def _memory(orch, session):
    from framework.api.routes import get_memory_status
    sm = SimpleNamespace(_all_sessions={session.session_id: session})
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_manager=sm,
                                                                   orchestrator=orch)))
    return asyncio.run(get_memory_status(session.session_id, req))


def test_inspector_exposes_limits_and_peak(make_session):
    eng = FakeEngine([{"text": "完成", "metrics": {"prompt_tokens": 1234}}])
    orch, session = _orch(eng), make_session()
    _run(orch, session)
    out = _memory(orch, session)
    assert (out["context_limit"], out["context_ceiling"], out["context_soft"], out["context_hard"]) \
        == (65536, 53248, 24576, 39322)
    assert out["turn_peak_prompt_tokens"] == 1234
    from framework.api.schemas import MemoryStatusResponse
    MemoryStatusResponse(**out)


def test_inspector_nulls_when_unbound(make_session):
    eng = FakeEngine([])
    eng.context_limit = None
    out = _memory(_orch(eng), make_session())
    assert (out["context_limit"], out["context_ceiling"], out["context_soft"], out["context_hard"]) \
        == (None, None, None, None)


def test_app_lifespan_binds_limit_after_engine_and_orchestrator_assembly():
    """源锚：lifespan 必须在 AgentEngine.create 与 _make_orchestrator 之后、TurnRunner 装配
    （首轮可达）之前调 bind_context_limit——漏调 = 生产上护栏永远 unbound。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    i_create = src.index("engine = await AgentEngine.create(")
    i_make = src.index("orchestrator = _make_orchestrator(engine, mcp_registry)")
    i_bind = src.index("orchestrator.bind_context_limit()")
    i_publish = src.index("turn_runner = TurnRunner(orchestrator")
    assert i_create < i_make < i_bind < i_publish


# ====================================================================== 一审补测
# ---- M1 / m7(d)：增量路（prompt_stream 生效）量到的 == 实发文本的 token 数 ----
def _fresh_session():
    s = _session()
    s.history = s.history[:2]          # [system, user]
    return s


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_measure_follows_incremental_path_after_stream_commit(dialect):
    be = _CaptureBackend()
    eng = _engine(be, dialect)
    s = _fresh_session()
    a1 = dict(tools=TOOLS, task_block="[Task] step 1 of 3: read", enable_thinking=False)
    m1 = eng.measure_prompt(s, **a1)
    asyncio.run(eng.generate_once(s, **a1))
    assert m1.tokens == len(be.prompts[0].split())
    assert s.prompt_stream is not None                     # 无尾注 → 流已提交
    # 编排层：追加本次生成的 assistant 占位与工具观察（第二次迭代的形状）
    s.history.append({"role": "assistant", "content": "好的"})
    s.history.append({"role": "tool", "tool_responses": [
        {"name": "read_thing", "response": "alpha beta gamma"}]})
    a2 = dict(tools=TOOLS, task_block="[Task] step 2 of 3: write", enable_thinking=False)
    m2 = eng.measure_prompt(s, **a2)
    r2 = asyncio.run(eng.generate_once(s, **a2))
    assert r2["metrics"].get("stream_delta") is True       # 实发确实走了增量路
    sent = be.prompts[1]
    assert sent.startswith(be.prompts[0])
    assert m2.tokens == len(sent.split()) and m2.chars == len(sent)
    # 全量渲染只含一份任务块快照：量法若退回全量就会与实发不等
    inc, extra = eng._prompt_env(s)
    full = eng._render_full_prompt(s, TOOLS, enable_thinking=False, task_block=a2["task_block"],
                                   tail_notes=None, include_env=inc, env_extra=extra)
    assert len(full.split()) != m2.tokens


# ---- m1：分词器 encode 抛异常 → 字符估算 + 每进程一次事件，不杀轮 ----
class _BoomTok:
    special_tokens_map = {}

    def encode(self, s, add_special_tokens=False):
        raise RuntimeError("Already borrowed")


def test_measure_tokenizer_failure_falls_back_once(monkeypatch, capsys):
    import framework.core.engine as engine_mod
    monkeypatch.setattr(engine_mod, "_measure_fallback_warned", False)
    be = _CaptureBackend()
    eng = _engine(be, "gemma4")
    s = _session()
    inc, extra = eng._prompt_env(s)
    text = eng._render_full_prompt(s, TOOLS, enable_thinking=False, task_block=None,
                                   tail_notes=None, include_env=inc, env_extra=extra)
    be.processor = SimpleNamespace(tokenizer=_BoomTok())
    m = eng.measure_prompt(s, tools=TOOLS)
    eng.measure_prompt(s, tools=TOOLS)
    assert m.method == "chars" and m.tokens == cg.fallback_estimate(text)
    ev = _events("context_guard_measure_fallback")
    assert len(ev) == 1 and "Already borrowed" in ev[0]["detail"]["error"]
    assert capsys.readouterr().out.count("量法分词器失败") == 1


# ---- m7(a)：三遍判定循环的两条复合路径（range(3)→range(1) 必红） ----
def test_ceiling_then_compaction_lands_in_hard_and_still_gets_note(make_session):
    eng = FakeEngine([{"text": "完成"}])
    eng.measure_script = [60000, 45000, 45100]
    r = _run(_orch(eng), make_session())
    assert r["status"] == "success" and len(eng.calls) == 1
    assert len(_events(cg.EVT_FOLD)) == 1 and len(_events(cg.EVT_HARD)) == 1
    assert any(n.startswith(HARD_NOTE_HEAD) for n in eng.calls[0]["tail_notes"])
    assert len(eng.measure_calls) == 3


def test_hard_note_pushing_over_ceiling_triggers_compaction_not_abort(make_session):
    eng = FakeEngine([{"text": "完成"}])
    eng.measure_script = [45000, 53300, 30000]
    r = _run(_orch(eng), make_session())
    assert r["status"] == "success" and len(eng.calls) == 1
    assert len(_events(cg.EVT_HARD)) == 1
    fold = _events(cg.EVT_FOLD)
    assert len(fold) == 1 and (fold[0]["detail"]["before"], fold[0]["detail"]["after"]) == (53300, 30000)
    assert _events("turn_abort") == []


# ---- n2：终止事件与其它护栏事件同口径 ----
def test_abort_event_carries_method_and_iteration(make_session):
    eng = FakeEngine([{"text": "x"}])
    eng.measure_script = [60000]
    _run(_orch(eng), make_session())
    d = _events("turn_abort")[0]["detail"]
    assert (d["method"], d["iteration"], d["prompt_tokens"], d["level"]) == ("tokenizer", 1, 60000, "ceiling")


# ---- m6：max_tokens 缺键（引擎缺省）同样受钳 ----
def test_missing_max_tokens_clamped_to_small_reserve(make_session):
    from framework.core.engine import DEFAULT_MAX_TOKENS
    eng = FakeEngine([{"text": "完成"}])
    session = make_session()
    session.add_user_message("做点事")
    _begin(session)
    gen = {k: v for k, v in GEN.items() if k != "max_tokens"}
    orch = _orch(eng, guard={"soft_tokens": 500, "hard_tokens": 1000, "generation_reserve": 2048})
    asyncio.run(orch.run_turn(session, journal=session.journal, **gen))
    assert eng.calls[0]["kwargs"]["max_tokens"] == 2048
    assert _events(cg.EVT_CLAMP)[0]["detail"] == {"requested": DEFAULT_MAX_TOKENS, "clamped_to": 2048}


def test_missing_max_tokens_within_reserve_written_back_as_engine_default(make_session):
    from framework.core.engine import DEFAULT_MAX_TOKENS
    eng = FakeEngine([{"text": "完成"}])
    session = make_session()
    session.add_user_message("做点事")
    _begin(session)
    gen = {k: v for k, v in GEN.items() if k != "max_tokens"}
    asyncio.run(_orch(eng).run_turn(session, journal=session.journal, **gen))
    assert eng.calls[0]["kwargs"]["max_tokens"] == DEFAULT_MAX_TOKENS
    assert _events(cg.EVT_CLAMP) == []


# ---- m2：旗标按轮（跨审批续跑段）生效 ----
DANGER_BIG = FakeTool("danger", result=BIG, approval=True, read_only=False)


def _approve(runner, session):
    async def go():
        await runner.accept_approval(session, session.session_id, True)
        return await runner.run_approval(session, session.session_id, True)
    return asyncio.run(go())


def _runner(engine, tools):
    return TurnRunner(_orch(engine, tools=tools), TaskScheduler(policy=ClassPolicy()))


def _submit(runner, session):
    async def go():
        ctx = await runner.accept_turn(session, session.session_id, prompt="做点事", images=[],
                                       audios=[], ingested_text="", attachments=[], gen=dict(GEN))
        return await runner.run_accepted(session, session.session_id, ctx)
    return asyncio.run(go())


def test_hard_note_and_event_not_repeated_in_approval_resume_segment(make_session):
    eng = FakeEngine([{"tool_calls": [call("danger", x=1)]}, {"text": "完成"}])
    eng.measure_script = [40000, 40100, 40200]
    session = make_session()
    runner = _runner(eng, [DANGER_BIG])
    assert _submit(runner, session)["status"] == "pending_approval"
    assert _approve(runner, session)["status"] == "success"
    assert len(eng.calls) == 2
    assert len(_events(cg.EVT_HARD)) == 1
    assert not [n for n in eng.calls[1]["tail_notes"] if n.startswith(HARD_NOTE_HEAD)]


def test_second_ceiling_in_resume_segment_aborts_without_recompacting(make_session):
    eng = FakeEngine([{"tool_calls": [call("danger", x=1)]}, {"text": "不该走到这里"}])
    eng.measure_script = [60000, 30000, 60000]
    session = make_session()
    runner = _runner(eng, [DANGER_BIG])
    assert _submit(runner, session)["status"] == "pending_approval"
    rd = _approve(runner, session)
    assert rd["status"] == "warning" and len(eng.calls) == 1
    assert len(_events(cg.EVT_FOLD)) == 1 and len(eng.measure_calls) == 3


def test_flags_reset_on_new_user_turn(make_session):
    eng = FakeEngine([{"text": "一"}, {"text": "二"}])
    eng.measure_script = [30000]
    orch, session = _orch(eng), make_session()
    _run(orch, session)
    _run(orch, session)
    assert len(_events(cg.EVT_SOFT)) == 2


# ---- m7(b)：审批续跑落盘（turn_runner._merge_continuation）与定时任务落盘走 persistable_messages ----
def test_compaction_in_approval_resume_segment_never_reaches_history_log(make_session):
    # 段一：读 a → danger 待批；段二：danger 执行（BIG）→ 读 b → 读 c → ceiling 压缩 → 收尾。
    # 被压缩的 danger 观察属于段二（:783 切片区间），读 a 属于段一（暂存轮原件）。
    eng = FakeEngine([{"tool_calls": [call("read_thing", path="a")]},
                      {"tool_calls": [call("danger", x=1)]},
                      {"tool_calls": [call("read_thing", path="b")]},
                      {"tool_calls": [call("read_thing", path="c")]},
                      {"text": "完成"}])
    eng.measure_script = [100, 100, 100, 100, 60000, 30000]
    session = make_session()
    runner = _runner(eng, [BIG_TOOL, DANGER_BIG])
    assert _submit(runner, session)["status"] == "pending_approval"
    rd = _approve(runner, session)
    assert rd["status"] == "success" and len(_events(cg.EVT_FOLD)) == 1
    mem = [tr["response"] for m in session.history if m.get("role") == "tool"
           for tr in m["tool_responses"]]
    assert sum("Observation compacted" in r for r in mem) >= 2      # 段一与段二的旧观察都已成桩
    log = _log_lines(session)
    assert len(log) == 1
    obs = [tr["response"] for m in log[0]["response"] if m.get("role") == "tool"
           for tr in m["tool_responses"]]
    assert obs == [BIG] * 4


def test_compaction_in_scheduled_run_never_reaches_history_log(make_session):
    from framework.schedule.runner import ScheduleRunner
    eng = FakeEngine(_three_reads_then([{"text": "完成"}]))
    eng.measure_script = [100, 100, 100, 60000, 30000]
    session = make_session("schedule_test")
    runner = ScheduleRunner(None, SimpleNamespace(get_or_create=lambda sid: session),
                            _orch(eng, tools=[BIG_TOOL]), TaskScheduler())
    record = {"status": "fail"}
    asyncio.run(runner._execute_inner({"id": "t", "name": "t", "prompt": "读三个文件"}, record))
    assert record["status"] == "success" and len(_events(cg.EVT_FOLD)) == 1
    obs = [tr["response"] for m in _log_lines(session)[0]["response"] if m.get("role") == "tool"
           for tr in m["tool_responses"]]
    assert obs == [BIG, BIG, BIG]


# ---- n1：应急压缩只复制/登记本轮段 ----
def test_emergency_compaction_registers_only_current_turn(make_session):
    eng = FakeEngine([{"tool_calls": [call("read_thing", path="old")]}, {"text": "一"},
                      *_three_reads_then([{"text": "二"}])])
    eng.measure_script = [100, 100, 100, 100, 100, 60000, 30000]
    orch, session = _orch(eng, tools=[BIG_TOOL]), make_session()
    _run(orch, session)
    old_tool = next(m for m in session.history if m.get("role") == "tool")
    _run(orch, session)
    originals = [orig for _, orig in session.guard_persist_originals.values()]
    assert originals and all(o is not old_tool for o in originals)
    assert len(originals) == 3


# ---- m3：冻结 / 重建清空还原表 ----
def test_freeze_and_rebuild_release_guard_originals(make_session):
    session = make_session()
    session.guard_persist_originals = {1: ({}, {})}
    session.rebuild_from_log()
    assert session.guard_persist_originals == {}
    session.guard_persist_originals = {1: ({}, {})}
    session._backend = None
    session.freeze()
    assert session.guard_persist_originals == {}


# ---- m4：资产链接补挂读落盘视图 ----
def test_asset_links_recovered_from_original_after_compaction(make_session):
    link = "![chart](/api/v1/files/s/chart.png)"
    tool = FakeTool("read_thing", result="Y" * 600 + "\n" + link + "\n" + "Z" * 400)
    eng = FakeEngine(_three_reads_then([{"text": "图表已生成"}]))
    eng.measure_script = [100, 100, 100, 60000, 30000]
    r = _run(_orch(eng, tools=[tool]), make_session())
    assert r["status"] == "success" and len(_events(cg.EVT_FOLD)) == 1
    assert "/api/v1/files/s/chart.png" in r["response"]


# ---- M2：算力端重开换 max_model_len → 重绑 / 不相容即探活失败 ----
class _RemoteEng:
    def __init__(self, backend):
        self.backend = backend

    @property
    def context_limit(self):
        return self.backend.context_limit


def _remote_orch(limit=65536):
    from framework.backends.remote.backend import RemoteBackend
    be = RemoteBackend(processor=None, base_url="http://127.0.0.1:1")
    be.context_limit = limit
    orch = Orchestrator(_RemoteEng(be), FakeRegistry([FakeTool("read_thing")]), {})
    orch.bind_context_limit()
    return orch, be


def _health(n):
    return {"status": "ok", "inference_options": {"max_model_len": n}}


def test_refresh_context_limit_rebinds_on_change():
    from framework.api.compute_lifecycle import refresh_context_limit
    orch, be = _remote_orch()
    assert refresh_context_limit(orch, _health(65536), {}) is None
    assert refresh_context_limit(orch, _health(98304), {}) == 98304
    assert be.context_limit == 98304
    assert orch.context_limits == cg.ContextLimits(98304, 98304 - 12288)
    ev = _events("context_limit_rebound")
    assert len(ev) == 1 and ev[0]["detail"] == {"previous": 65536, "value": 98304, "source": "health"}


def test_refresh_context_limit_incompatible_raises_and_stays_failed():
    from framework.api.compute_lifecycle import refresh_context_limit
    orch, be = _remote_orch()
    for _ in range(2):                          # 回滚保证下一次探活仍判、仍失败
        with pytest.raises(ValueError, match="ceiling"):
            refresh_context_limit(orch, _health(32768), {})
        assert be.context_limit == 65536
        assert orch.context_limits == cg.ContextLimits(65536, 53248)


def test_refresh_context_limit_ignores_inprocess_backends():
    from framework.api.compute_lifecycle import refresh_context_limit
    orch = _orch(FakeEngine([]))
    assert refresh_context_limit(orch, _health(32768), {}) is None
    assert orch.context_limits == cg.ContextLimits(65536, 53248)


def test_app_probe_dependencies_refreshes_context_limit():
    """源锚：探活在身份复核之后对同一份 /health 调 refresh_context_limit，装配期末尾那次显式传编排器。"""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_probe_dependencies")
    body = ast.get_source_segment(src, fn)
    assert body.index("probe_remote_compute(") < body.index("refresh_context_limit(_orch, identity")
    assert "await _probe_dependencies(orchestrator)" in src
