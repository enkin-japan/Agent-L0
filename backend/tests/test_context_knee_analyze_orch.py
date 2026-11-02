"""第二批（b2）分析扩展金测试：analyze_orch 的 W-K2 桩 / 推回链 / W1b 行级计数与 fold_forensics 取证。

守的是什么：ANALYSIS.md 里「stub_unknown 恒 0」「超上限实发 0」「失败对与成功对摘要器输入相同」这些结论
直接抄这几个函数的输出。绊线用行字段而不独立重数、超限按 ≥ 而非 > 计、时间戳不剥就比输入——
都会给出看着合理的错数。CPU、合成数据，不读真实产物。
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "benchmarks") not in sys.path:
    sys.path.append(str(REPO / "benchmarks"))

from context_knee import analyze as A              # noqa: E402
from context_knee import analyze_orch as O         # noqa: E402
from summary_trace import fold_forensics as FF     # noqa: E402


def _orch_row(**kw):
    r = {"level": 57344, "item_id": "lc-hard-001", "rep": 0, "context_limit": 65536,
         "guard_thresholds": {"soft": 24576, "hard": 39322, "reserve": 12288},
         "stub_served": {"run_shell_command": 2, "list_dir": 1}, "stub_unknown": 0, "stub_failed": 1,
         "turns": [{"tool_calls": [{"name": "run_shell_command"}, {"name": "x_tool"}, {"name": "write_file"}],
                    "feed": [{"content": "ok"}, {"content": '{"error": "Unknown tool: x_tool"}'},
                             {"content": "System Error: delivery blocked because ..."}]}],
         "turn_metrics": {"retrieval_incomplete": True},
         "aborted_reason": "回合上限 12 耗尽仍在发调用",
         "gens": [{"prompt_tokens": 53248}, {"prompt_tokens": 53249}],
         "guard_events_lines": [0, 3]}
    r.update(kw)
    return r


def test_stub_row_recounts_unknown_independently_of_field():
    st = O.stub_row(_orch_row())
    assert st["served_n"] == 3 and st["unknown"] == 0 and st["failed"] == 1
    assert st["calls"] == 3 and st["feed_unknown"] == 1      # 行字段说 0，feed 重数出 1：两者要能对不上
    assert O.stub_row({"turns": []})["has"] is False           # 旧行缺键 ≠ 0


def test_pushback_row_counts_delivery_blocked_and_abort_kinds():
    pb = O.pushback_row(_orch_row())
    assert pb == {"delivery_blocked": 1, "retrieval_incomplete": 1, "iter_exhausted": 1, "context_guard": 0}
    pb2 = O.pushback_row(_orch_row(aborted_reason="context_guard"))
    assert pb2["context_guard"] == 1
    assert pb2["iter_exhausted"] == 0          # context_guard 中止不算迭代耗尽


def test_w1b_row_fold_spill_and_strict_over_ceiling():
    ev = [{"type": "context_guard_fold", "session": "s", "detail": {"compacted": 5, "spilled": 4, "iteration": 1}},
          {"type": "turn_abort", "session": "s",
           "detail": {"reason": "context_guard", "compacted": 2, "spilled": 2, "prompt_tokens": 60000}},
          {"type": "context_hard", "session": "s", "detail": {}}]
    w = O.w1b_row(_orch_row(), ev)
    assert (w["folds"], w["compacted"], w["spilled"], w["fold_spilled_runs"]) == (1, 5, 4, 1)
    assert (w["aborts"], w["abort_compacted"], w["abort_spilled"], w["abort_pt"]) == (1, 2, 2, [60000])
    assert w["hard"] == 1
    assert w["over_ceiling"] == 2          # ceiling = 65536 − 12288 = 53248：等于也算超，53248、53249 都算
    assert w["over_limit"] == 0 and w["has_flags_key"] is False


def test_b2_csv_extra_counts_ceiling_equal_and_spill_readbacks():
    row = _orch_row(gens=[{"prompt_tokens": 53248}], spill_readbacks=3)   # 53248 == ceiling
    G = {"arm": {"records": [row]}}
    evs = {"arm": []}           # 非 None 即可触发 w1b_row 计算；guard_events_lines 切出的空段不影响 over_ceiling
    extra = O.b2_csv_extra(G, evs, ["arm"])
    cell = extra[("arm", 57344)]
    assert cell["sent_over_ceiling_gens"] == 1     # 等于 ceiling 的一次也计 1
    assert cell["spill_readbacks"] == 3


def test_b2_layout_detection(tmp_path):
    (tmp_path / "orch-turn-catalog").mkdir()
    assert O.b2_layout(tmp_path)
    (tmp_path / "direct-mock").mkdir()
    assert not O.b2_layout(tmp_path)       # 11-02 上午批布局（有 direct-mock）走老路径


def _sess(root: Path, name: str, created: float, turn=None, memory=None):
    d = root / "sessions" / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"session_id": name, "created_at": created}))
    if turn is not None:
        (d / "history.jsonl").write_text(json.dumps(turn) + "\n")
    if memory is not None:
        (d / "memory.json").write_text(json.dumps(memory))


def test_sec_q_system_error_missing_required_argument_counted_separately(tmp_path):
    """Q 节按 `missing required argument` 判别串把 System Error 回执分成「缺必填参数」/「其他」两类；
    改掉判别串（如拼错、换成别的子串）必须让此用例变红——这是唯一守住它的用例。"""
    def _min_row(item, rep, extra=None):
        return {"level": 0, "item_id": item, "rep": rep, "stamp": "s1", "model": "m/x",
                "score": {"pass": True}, "gens": [], **(extra or {})}

    a_name, b_name = "orch-turn-w1w2", "orch-turn-catalog"
    rows_a = [_min_row("x", 0, {"turns": [{"tool_calls": [{"name": "t1"}, {"name": "t2"}],
                                            "feed": [{"content": "System Error: missing required argument 'q'"},
                                                     {"content": "System Error: delivery blocked because ..."}]}]}),
              _min_row("x", 1)]
    rows_b = [_min_row("x", 0), _min_row("x", 1)]
    for name, rows in ((a_name, rows_a), (b_name, rows_b)):
        d = tmp_path / name
        d.mkdir()
        (d / "transcripts.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    G = {name: A.load_group(tmp_path / name) for name in (a_name, b_name)}
    aggs = {name: A.aggregate(G[name], A.th_global, set()) for name in (a_name, b_name)}
    out = O.sec_q(G, aggs, {}, a_name=a_name, b_name=b_name)
    line = next(ln for ln in out if ln.startswith(f"- {a_name} `System Error`"))
    assert "合计 2" in line
    assert "/缺必填参数 1" in line
    assert "/其他 1" in line


def test_fold_forensics_error_vs_ok_pair(tmp_path):
    turn_a = {"timestamp": 1793619600.0, "status": "warning", "prompt": "任务 A", "response": [
        {"role": "assistant", "content": None, "tool_calls": [{"name": "list_files"}]}]}
    turn_b = dict(turn_a, timestamp=1793619900.0)          # 同输入、不同时间戳
    leg, new = tmp_path / "trace-legacy-640", tmp_path / "trace-new-640"
    for d in (leg, new):
        d.mkdir()
    # legacy：error 对。t1 建于 100.0，error wall 30.0 → 抛错于 130.0；post 建于 128.5 → 摘要调用 1.5 s
    _sess(leg, "lc-hard-003-r0-aaaaaa-t1", 100.0)
    _sess(leg, "lc-hard-003-r0-aaaaaa-pre", 110.0)
    _sess(leg, "lc-hard-003-r0-aaaaaa-post", 128.5, turn=turn_a)
    (leg / "transcripts.jsonl").write_text(json.dumps(
        {"item": "lc-hard-003", "rep": 0, "branch": None, "error": "RuntimeError: POST 折叠失败（摘要为空或无可折叠轮次）",
         "wall_s": 30.0}) + "\n")
    # new：ok 对，摘要落盘于 post 建会话后 3.0 s
    _sess(new, "lc-hard-003-r0-bbbbbb-t1", 200.0)
    _sess(new, "lc-hard-003-r0-bbbbbb-pre", 210.0)
    _sess(new, "lc-hard-003-r0-bbbbbb-post", 220.0, turn=turn_b,
          memory={"summary": "x", "summarized_turns": 1, "updated_at": 223.0})
    (new / "transcripts.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"item": "lc-hard-003", "rep": 0, "branch": "pre", "turn1_status": "warning"},
        {"item": "lc-hard-003", "rep": 0, "branch": "post", "summary_chars": 10, "summary_tokens_est": 100}]) + "\n")
    e = FF.pair_rows(leg)[0]
    assert e["status"] == "error" and e["summarize_s"] == 1.5
    assert e["hist_lines"] == 1 and e["foldable_blocks"] == 1 and e["memory_json"] is False
    o = FF.pair_rows(new)[0]
    assert o["status"] == "ok" and o["summarize_s"] == 3.0 and o["summarized_turns"] == 1
    assert e["input_text"] != o["input_text"]                         # 时间戳不同
    assert FF.strip_ts(e["input_text"]) == FF.strip_ts(o["input_text"])
    out = FF.render([leg, new])
    assert "逐字相同的 1 个" in out and "memory.json 未落盘的 1 个" in out
