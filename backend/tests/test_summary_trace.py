"""TRACE 式成对续跑评估驾驭器（benchmarks/summary_trace，W4）金测试：FakeEngine / CPU，不起服务、不载模型。

守的是什么：两支续跑真的分叉自同一执行状态、POST 真的折叠成了摘要块、repeated 真的数到了
turn 1 做过的调用、--template / --task-state 两个对照开关真的接到了摘要器输入上——开关接错了，
报表照样画得出来，只是 A/B 两列在量同一件事。
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "benchmarks") not in sys.path:
    sys.path.append(str(REPO / "benchmarks"))

from summary_trace import run_trace as T          # noqa: E402
from summary_trace import summarize_trace as S    # noqa: E402
import tests.fake_engine as fe                    # noqa: E402
from framework.core.engine import AgentEngine     # noqa: E402
from framework.memory import session as session_mod  # noqa: E402
from framework.utils.config_loader import data_dir   # noqa: E402

ITEM = "lc-hard-001"


@pytest.fixture
def summarize_calls(monkeypatch):
    calls = []

    async def rec(self, content, prior_summary="", **kwargs):
        calls.append({"content": content, "prior": prior_summary, **kwargs})
        return "[fake summary]"
    monkeypatch.setattr(fe.FakeEngine, "summarize", rec)
    return calls


def _run(tmp_path, *extra):
    out = tmp_path / "trace"
    rc = T.main(["--dry-run", "--ids", ITEM, "--reps", "1", "--out", str(out), *extra])
    assert rc == 0
    rows = [json.loads(l) for l in (out / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()]
    return out, rows


def _t1_record(row):
    t1 = Path(row["turn1_session_dir"])
    return json.loads((t1 / "history.jsonl").read_text(encoding="utf-8").splitlines()[0])


def _stat(p):
    return (p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None


def test_two_branches_post_folded_and_repeated_counted(tmp_path, summarize_calls):
    root_events = Path(data_dir()) / "guardrail_events.jsonl"
    before = _stat(root_events)
    out, rows = _run(tmp_path)
    assert [r.get("error") for r in rows] == [None, None]
    by = {r["branch"]: r for r in rows}
    assert set(by) == {"pre", "post"}
    # POST：history 首块（system 之后）是摘要块，头为 W4 新头
    head = by["post"]["history_head"]
    assert head.startswith("[Memory summary of earlier conversation")
    assert "do not redo items listed under [Done]" in head and head.endswith("[fake summary]")
    assert by["pre"]["history_head"] is None
    # 干跑续跑脚本重做了 turn 1 的那次只读调用 → repeated 必为正（两支同脚本）
    assert by["pre"]["repeated"] > 0 and by["post"]["repeated"] > 0
    assert by["post"]["summary_chars"] == len("[fake summary]")
    # 默认模板 new、task_state on：摘要器收到 W4 指令、输入里有轮末任务状态块
    assert len(summarize_calls) == 1
    c = summarize_calls[0]
    assert c["instruction"] == AgentEngine.SUMMARY_INSTRUCTION
    assert c["mode"] == "merge" and c["max_tokens"] == 900        # 缺省 = config.yaml 值
    assert "[Task state at end of turn]" in c["content"]
    assert _t1_record(rows[0]).get("task_state")
    # 两支会话目录独立（不共享 memory.json）；只有 POST 有摘要旁挂
    pre_dir, post_dir = Path(by["pre"]["session_dir"]), Path(by["post"]["session_dir"])
    assert pre_dir != post_dir
    assert (post_dir / "memory.json").exists() and not (pre_dir / "memory.json").exists()
    # 产物不写数据根：事件流落产物目录（turn 1 熔断必有 turn_abort）、数据根事件文件不动
    ev = out / "guardrail_events.jsonl"
    assert ev.exists() and ev.stat().st_size > 0
    assert any(json.loads(l)["type"] == "turn_abort" for l in ev.read_text(encoding="utf-8").splitlines())
    assert _stat(root_events) == before
    assert by["pre"]["cont_live_task"] is True and by["post"]["cont_live_task"] is True
    # 报表能出
    rep = S.report([out])
    assert "| new | on | off | 1 | 1 |" in rep


def test_template_legacy_passes_legacy_text(tmp_path, summarize_calls):
    header_now = session_mod.SUMMARY_BLOCK_HEADER
    _out, rows = _run(tmp_path, "--template", "legacy", "--summary-max-tokens", "640")
    assert all(r.get("error") is None for r in rows)
    assert summarize_calls[0]["instruction"] == AgentEngine.SUMMARY_INSTRUCTION_LEGACY
    assert summarize_calls[0]["max_tokens"] == 640
    assert {r["template"] for r in rows} == {"legacy"}
    # legacy 臂摘要块头 = 旧头（A/B 只差指令 + 头），run 结束后模块常量还原
    post = next(r for r in rows if r["branch"] == "post")
    assert post["history_head"] == session_mod.SUMMARY_BLOCK_HEADER_LEGACY + "\n[fake summary]"
    assert session_mod.SUMMARY_BLOCK_HEADER == header_now


def test_task_state_off_record_has_no_task_state(tmp_path, summarize_calls):
    _out, rows = _run(tmp_path, "--task-state", "off")
    assert all(r.get("error") is None for r in rows)
    assert "task_state" not in _t1_record(rows[0])
    assert rows[0]["turn1_record_has_task_state"] is False
    assert "[Task state at end of turn]" not in summarize_calls[0]["content"]


def test_drop_task_clears_task_before_continuation(tmp_path, summarize_calls):
    _out, rows = _run(tmp_path, "--drop-task", "on")
    assert all(r.get("error") is None for r in rows)
    assert {r["cont_live_task"] for r in rows} == {False}
    assert {r["drop_task"] for r in rows} == {"on"}


def test_catalog_prefix_forces_catalog_with_mock_tools_full(tmp_path, summarize_calls, monkeypatch):
    """--prefix catalog：编排器按目录模式披露（orch_model_options），题目 mock 工具恒完整
    （keep_mock_tools_full），生产工具只剩目录行——与 run_knee 编排器 catalog 档同一装配。"""
    seen = []
    orig = fe.FakeEngine.generate_once

    async def spy(self, session, tools=None, **kw):
        if tools:
            seen.append(list(tools))
        return await orig(self, session, tools=tools, **kw)
    monkeypatch.setattr(fe.FakeEngine, "generate_once", spy)
    _out, rows = _run(tmp_path, "--prefix", "catalog")
    assert [r.get("error") for r in rows] == [None, None]
    decl = {d["name"]: d for d in seen[0]}
    assert "get_tool_schema" in decl
    for name in ("list_files", "read_file", "write_file", "append_file", "delete_file"):
        assert "parameters" in decl[name], name
    assert "parameters" not in decl["web_read"] and "parameters" not in decl["rag_search"]


def test_existing_transcripts_refused(tmp_path, summarize_calls):
    out, _rows = _run(tmp_path)
    with pytest.raises(SystemExit):
        T.main(["--dry-run", "--ids", ITEM, "--reps", "1", "--out", str(out)])


def test_count_repeated_canonical_args_and_excludes_dup_blocked():
    c1 = {T.call_key({"name": "read_file", "arguments": {"b": 1, "a": 2}})}
    hist = [{"role": "user", "content": "go"},
            {"role": "assistant", "tool_calls": [
                {"type": "function", "name": "read_file", "arguments": {"a": 2, "b": 1}},
                {"type": "function", "name": "read_file", "arguments": {"a": 3}},
                {"type": "function", "name": "remember", "arguments": {"note": "n"}},
                {"type": "error", "raw_input": "x"}]},
            {"role": "tool", "tool_responses": [
                {"name": "read_file", "response": "{\"content\": \"1\"}"},
                {"name": "read_file", "response": "{\"content\": \"2\"}"},
                {"name": "remember", "response": "Saved"}]},
            {"role": "assistant", "tool_calls": [
                {"type": "function", "name": "read_file", "arguments": {"a": 2, "b": 1}}]},
            {"role": "tool", "tool_responses": [
                {"name": "read_file", "response": "Duplicate call blocked: you already executed"}]}]
    assert T.count_repeated(hist, 1, c1) == 1          # 第二次被拦的那次只算 blocked
    assert T.count_repeated(hist, 3, c1) == 0


def test_aggregate_pairs_and_bootstrap():
    def row(item, branch, rep_, blk, ok, pt):
        return {"item": item, "rep": 0, "branch": branch, "template": "new", "task_state": "on",
                "repeated": rep_, "blocked": blk, "score": ok, "first_prompt_tokens": pt}
    rows = [row("a", "pre", 2, 1, True, 100), row("a", "post", 1, 0, True, 50),
            row("b", "pre", 0, 0, False, 200), row("b", "post", 1, 1, False, 80),
            {"item": "c", "rep": 0, "branch": None, "template": "new", "task_state": "on",
             "error": "boom"}]
    agg = S.aggregate(rows, n_boot=200, seed=1)
    g = agg["groups"][0]
    assert g["n_pairs"] == 2 and agg["n_unpaired"] == 1
    assert g["delta_cost_mean"] == pytest.approx(((1 - 3) + (2 - 0)) / 2)
    lo, hi = g["delta_cost_ci95"]
    assert lo <= g["delta_cost_mean"] <= hi and lo >= -2 and hi <= 2
    assert g["pass_pre"] == 0.5 and g["first_pt_median_post"] == 65
    assert S.bootstrap_ci([1.0]) == (None, None)


def _write(d, rows):
    d.mkdir(parents=True)
    (d / "transcripts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _pair(**kw):
    base = {"item": "a", "rep": 0, "template": "new", "task_state": "on", "drop_task": "off",
            "model": "m", "summary_max_tokens": 900, "repeated": 0, "blocked": 0, "score": True}
    return [{**base, **kw, "branch": "pre"}, {**base, **kw, "branch": "post"}]


def test_merge_dirs_keeps_batches_apart_and_collision_errors(tmp_path):
    """一审 Major 2：多批合并不静默覆盖——不同 max_tokens 分组、同配置两目录各自成对；
    同一目录传两遍 / transcripts 里同键同支重复 → 报错退出。"""
    _write(tmp_path / "a", _pair())
    _write(tmp_path / "b", _pair(summary_max_tokens=640))
    _write(tmp_path / "c", _pair())
    agg = S.aggregate(S.load_rows([tmp_path / "a", tmp_path / "b", tmp_path / "c"]))
    by = {(g["summary_max_tokens"]): g for g in agg["groups"]}
    assert set(by) == {"900", "640"}
    assert by["900"]["n_pairs"] == 2 and by["640"]["n_pairs"] == 1
    with pytest.raises(SystemExit):
        S.aggregate(S.load_rows([tmp_path / "a", tmp_path / "a"]))
    _write(tmp_path / "d", _pair() + _pair(repeated=3))
    with pytest.raises(SystemExit):
        S.aggregate(S.load_rows([tmp_path / "d"]))
