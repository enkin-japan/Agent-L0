"""拐点分析脚本金测试（benchmarks/context_knee/analyze.py）：去重、拐点规则、AUTO 块改写、题级 bootstrap。

守的是什么：方案文档里的拐点数与区间直接抄 analyze 的输出。去重让晚到的 error 行顶掉真实 run、
顶档整档缺测被当成「测到了」、bootstrap 再抽 rep 把区间推离点估计——都会画出看着合理的错数。
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "benchmarks") not in sys.path:
    sys.path.append(str(REPO / "benchmarks"))

from context_knee import analyze as A   # noqa: E402


def _row(level, item, rep, ok, stamp="s1", **kw):
    return {"level": level, "item_id": item, "rep": rep, "stamp": stamp, "model": "m/x",
            "score": {"pass": ok}, "gens": [], **kw}


def _write(d: Path, rows):
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcripts.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return d


# ---- 去重 ----

def test_dedupe_real_row_beats_later_error(tmp_path):
    g = A.load_group(_write(tmp_path / "g", [
        _row(0, "a", 0, True, "s1"),
        {"level": 0, "item_id": "a", "rep": 0, "stamp": "s2", "error": "timeout"},
        {"level": 0, "item_id": "b", "rep": 0, "stamp": "s1", "error": "timeout"},
        _row(0, "b", 0, False, "s0")]))
    kept = {(r["item_id"], r["rep"]): r for r in g["records"]}
    assert kept[("a", 0)]["stamp"] == "s1" and kept[("b", 0)]["stamp"] == "s0"
    assert g["missing"] == []


def test_dedupe_same_key_two_stamps_latest_and_oldest(tmp_path, monkeypatch):
    rows = [_row(0, "a", 0, False, "20261031-04"), _row(0, "a", 0, True, "20261031-05"),
            _row(0, "a", 0, False, "20261031-05")]           # 同 stamp：文件序靠后者
    d = _write(tmp_path / "g", rows)
    g = A.load_group(d)
    assert g["records"][0]["score"]["pass"] is False and g["records"][0]["stamp"] == "20261031-05"
    assert len(g["dropped"]) == 2
    monkeypatch.setattr(A, "KEEP", "oldest")
    g2 = A.load_group(d)
    assert g2["records"][0]["stamp"] == "20261031-04"


# ---- 拐点规则 ----

def _agg(spec):
    """spec = {level: [每题 rep 列表]} → knee 需要的最小 agg。"""
    out = {}
    for lv, items in spec.items():
        it = {f"q{i}": v for i, v in enumerate(items)}
        two = [v for v in it.values() if len(v) == 2]
        out[lv] = {"_items": it,
                   "pass_hat_2": (sum(all(v) for v in two) / len(two)) if two else None,
                   "success_rate": sum(sum(v) for v in it.values()) / sum(len(v) for v in it.values())}
    return out


TT, TF, T = [True, True], [True, False], [True]


def test_knee_detected():
    agg = _agg({0: [TT] * 10, 16384: [TT] * 10, 32768: [TF] * 5 + [TT] * 5, 49152: [TF] * 6 + [TT] * 4})
    k = A.knee(agg, "pass_hat_2", [0, 4096], 0.20, 0.10)
    assert k["knee"] == 32768 and k["base_level"] == 0


def test_knee_not_seen_and_recovery_blocks():
    agg = _agg({0: [TT] * 10, 16384: [TT] * 9 + [TF], 49152: [TT] * 10})
    assert A.knee(agg, "pass_hat_2", [0], 0.20, 0.10)["verdict"] == "到 49152 未见拐点"
    dip = _agg({0: [TT] * 10, 16384: [TF] * 5 + [TT] * 5, 32768: [TT] * 10})   # 掉下去又回来
    assert A.knee(dip, "pass_hat_2", [0], 0.20, 0.10)["knee"] is None


def test_knee_top_level_with_empty_metric_is_not_top():
    """顶档 Pass^2 全空（只有单 rep）：不得把它算作「测到了」的顶档。"""
    agg = _agg({0: [TT] * 10, 32768: [TT] * 10, 57344: [T] * 10})
    assert agg[57344]["pass_hat_2"] is None
    k = A.knee(agg, "pass_hat_2", [0], 0.20, 0.10)
    assert k["verdict"] == "到 32768 未见拐点" and k["top"] == 32768


def test_knee_uses_common_item_set():
    """基线 10 题全过；高档只测到 8 题且全过——共同题集上差为 0，不判拐点。"""
    agg = _agg({0: [TT] * 10, 16384: [TT] * 8})
    agg[16384]["_items"] = {f"q{i}": TT for i in range(8)}
    assert A.knee(agg, "pass_hat_2", [0], 0.20, 0.10)["knee"] is None
    assert A.common_diff(agg[0]["_items"], agg[16384]["_items"], "pass_hat_2") == (0.0, 8)


def test_knee_floor_baseline_short_circuits_before_drop_rule():
    """基线本身 ≤ drop：「≤ 基线 − drop」规则永远不会命中，不得静默读成「未见拐点」。"""
    agg = _agg({0: [TT, TT] + [TF] * 8, 16384: [TT] * 10})     # 基线 pass_hat_2 = 2/10 = 0.20
    k = A.knee(agg, "pass_hat_2", [0], 0.20, 0.10)
    assert k["floor"] is True and k["knee"] is None and "基线在地板" in k["verdict"]
    assert k["base_level"] == 0 and k["base"] == pytest.approx(0.20)


# ---- write_md ----

def test_write_md_only_rewrites_between_markers(tmp_path):
    f = tmp_path / "A.md"
    f.write_text(f"# 手写\n结论 X\n{A.BEGIN}\nold\n{A.END}\n尾注 Y\n")
    A.write_md(f, "new")
    t = f.read_text()
    assert t == f"# 手写\n结论 X\n{A.BEGIN}\nnew\n{A.END}\n尾注 Y\n"
    f.write_text(f"head\n{A.END}\nx\n{A.BEGIN}\n")
    with pytest.raises(SystemExit, match="顺序"):
        A.write_md(f, "new")


# ---- 题级 bootstrap ----

def test_pass_hat_bootstrap_item_level_centered():
    """a 侧 10 题 [T,T]；b 侧 5 题 [T,F] + 5 题 [T,T] → 点差 −50。题级重抽不碰 rep 对：
    中位应贴着 −50、区间覆盖 −50（旧的两段重抽会把 [T,F] 抽成 [T,T]，中位被推向 0）。"""
    a = {f"q{i}": TT for i in range(10)}
    b = {f"q{i}": (TF if i < 5 else TT) for i in range(10)}
    p = A.paired(a, b, "pass_hat_2")
    assert p["n_items"] == 10 and p["diff"] == pytest.approx(-0.5)
    assert p["lo"] <= -0.5 <= p["hi"]
    assert abs(p["median"] - (-0.5)) <= 0.05
    assert p["down"] == 5 and p["up"] == 0


def test_success_rate_bootstrap_item_level_only():
    """success_rate 区间也只按题重抽（题内 rep 原样；与 analyze_p2.paired_item 同口径，2026-11-07）：
    两侧逐字相同 → 点差与区间全零宽（原两段重抽——先按题、再档内按 rep——给出非零宽，P2 批 L0 [−12,+12]）；
    一题 0→1、一题不变 → 点差 0.5、题级重抽区间恰为 [0, 1]，中位落在 {0, 0.5, 1}。"""
    same = {"i1": ([TF], [TF]), "i2": ([[False, False]], [[False, False]])}
    r = A.paired_units(same, "success_rate")
    assert (r["n_items"], r["diff"], r["lo"], r["hi"], r["median"]) == (2, 0.0, 0.0, 0.0, 0.0)
    assert (r["up"], r["down"]) == (0, 0)
    one = {"i1": ([[False, False]], [TT]), "i2": ([TF], [TF])}
    r = A.paired_units(one, "success_rate")
    assert r["diff"] == 0.5 and r["lo"] == 0.0 and r["hi"] == 1.0 and r["median"] in (0.0, 0.5, 1.0)
    assert (r["up"], r["down"]) == (1, 0)
    # 多档：题内各档 rep 不被重抽 → 同一题两侧各档 rep 完全相同时仍零宽
    multi = {"i1": ([TF, [False, True]], [TF, [False, True]]), "i2": ([TT, TF], [TT, TF])}
    r = A.paired_units(multi, "success_rate")
    assert (r["diff"], r["lo"], r["hi"]) == (0.0, 0.0, 0.0)


# ---- W1 峰值列 ----

def test_aggregate_peak_prompt_median_and_csv_tail_column(tmp_path):
    """峰值中位数按 run 级 peak_prompt_tokens；W1 前的旧行缺键不进分母；CSV 新列在末尾。"""
    g = A.load_group(_write(tmp_path / "g", [
        _row(0, "a", 0, True, peak_prompt_tokens=3000),
        _row(0, "a", 1, True, peak_prompt_tokens=5000),
        _row(0, "b", 0, False)]))
    agg = A.aggregate(g, A.th_global, set())
    assert agg[0]["peak_prompt_tokens_median"] == 4000
    assert A.CSV_COLS[-1] == "peak_prompt_tokens_median"
    g2 = A.load_group(_write(tmp_path / "g2", [_row(0, "a", 0, True)]))
    assert A.aggregate(g2, A.th_global, set())[0]["peak_prompt_tokens_median"] is None


# ---- D43 (a) 回忆来源归因 ----

_H_STUB, _H_READ, _H_CLUE = "aaaaaaaaaaaaaaa1", "bbbbbbbbbbbbbbb2", "ccccccccccccccc3"
_CARDS = [{"slot": "front", "kind": "config_path", "value": "/opt/halyard/conf/halyard-a1b2c3.toml"},
          {"slot": "mid", "kind": "ticket", "value": "OPS-73519"},
          {"slot": "back", "kind": "threshold", "value": "4630"}]


def _knee_run(tmp_path, *, final, spill, calls=(), feed=(), journal=None, stub_clue=None,
              name="L57344-x-r0-abc123"):
    """一个最小编排器 run：组目录 g/、会话 g/sessions/<name>/spill/*.txt；session_dir 记的是
    别处的原路径（只取末段名）。"""
    g = tmp_path / "g"
    sd = g / "sessions" / name
    (sd / "spill").mkdir(parents=True)
    for stem, text in spill.items():
        (sd / "spill" / f"{stem}.txt").write_text(text, encoding="utf-8")
    if journal is not None:
        (sd / "journal.jsonl").write_text("\n".join(json.dumps(e) for e in journal) + "\n")
    row = _row(57344, "x", 0, False, mode="orchestrator", session_dir=f"/content/elsewhere/{name}",
               fill={"cards": _CARDS},
               turns=[{"tool_calls": list(calls), "feed": list(feed), "text": ""},
                      {"tool_calls": [], "text": final}])
    if stub_clue:
        row["stub_clue"] = stub_clue
    _write(g, [row])
    return g


def _filler_obs(card_text, pad=260):
    return json.dumps({"path": "/srv/legacy-2025q3/a.log",
                       "content": "p" * pad + "\n" + card_text}, ensure_ascii=False)


def test_recall_source_three_cards_stub_readback_original(tmp_path):
    """★ 三张卡三种来源：ticket 在桩首部 200 字里（stub）；config_path 在首部之外、被回读到
    （readback）；threshold 的观察没被压（spill 里没有它 → original）。"""
    spill = {_H_STUB: json.dumps({"path": "/srv/legacy-2025q3/b.md",
                                  "content": "- escalation: incident tracked as ticket OPS-73519\n" + "q" * 300}),
             _H_READ: _filler_obs("halyard: loaded config from /opt/halyard/conf/halyard-a1b2c3.toml")}
    call = {"name": "file_extract", "arguments": {"path": f"spill:{_H_READ}"}}
    g = _knee_run(tmp_path, spill=spill, calls=[call], feed=[{"content": spill[_H_READ]}],
                  final="路径 /opt/halyard/conf/halyard-a1b2c3.toml，工单 OPS-73519，阈值 4630")
    grp = A.load_group(g)
    r = grp["records"][0]
    assert r["recall_source"] == {"config_path": "readback", "ticket": "stub", "threshold": "original"}
    assert r["readback_calls"] == 1
    a = A.aggregate(grp, A.th_global, set())[57344]
    assert (a["recall_src_stub"], a["recall_src_readback"], a["recall_src_original"],
            a["recall_src_unknown"]) == (1, 1, 1, 0)
    assert a["readback_calls"] == 1 and a["readback_runs"] == 1
    assert A.recall_src_cell(a) == "1/1/1"


def test_recall_source_only_counts_cards_in_the_final_reply(tmp_path):
    g = _knee_run(tmp_path, spill={}, final="只记得 OPS-73519")
    assert A.load_group(g)["records"][0]["recall_source"] == {"ticket": "original"}


def test_recall_source_readback_seen_only_in_the_journal(tmp_path):
    """feed 会被事后原地压成桩（b2 实测）；journal 里的回读结果也算。"""
    spill = {_H_READ: _filler_obs("queue_depth_alert_threshold = 4630")}
    call = {"name": "file_extract", "arguments": {"path": f"spill:{_H_READ}"}}
    journal = [{"type": "tool_started", "data": {"call_id": "c1", "name": "file_extract",
                                                 "args": {"path": f"spill:{_H_READ}"}}},
               {"type": "tool_result", "data": {"call_id": "c1", "name": "file_extract",
                                                "response": spill[_H_READ], "status": "ok"}}]
    g = _knee_run(tmp_path, spill=spill, calls=[call], feed=[{"content": "{…[Observation compacted]"}],
                  journal=journal, final="阈值 4630")
    assert A.load_group(g)["records"][0]["recall_source"] == {"threshold": "readback"}


def test_recall_source_pairs_readback_by_feed_name_not_position(tmp_path):
    """编排器 feed 的序是「预检打回在前、执行结果在后」：调用序 [file_extract 回读, run_math 缺参]、feed 序
    [run_math 打回, file_extract 结果]。按位置配会把回读配到打回回执上（F10）。"""
    spill = {_H_READ: _filler_obs("queue_depth_alert_threshold = 4630")}
    calls = [{"name": "file_extract", "arguments": {"path": f"spill:{_H_READ}"}},
             {"name": "run_math", "arguments": {"expression": "1"}}]
    feed = [{"name": "run_math",
             "content": "System Error: 'run_math' was not executed: missing required argument(s): 'code'."},
            {"name": "file_extract", "content": spill[_H_READ]}]
    g = _knee_run(tmp_path, spill=spill, calls=calls, feed=feed, final="阈值 4630")
    r = A.load_group(g)["records"][0]
    assert r["recall_source"] == {"threshold": "readback"} and r["readback_calls"] == 1


def test_recall_source_clue_counts_as_stub_only_for_rows_with_stub_clue(tmp_path):
    """★ 值在首部之外、只能经线索看见：D43 (a) 之后的行（stub_clue）记 stub；之前的批桩里没有
    线索，同样的数据只能记 unknown——不然 b2 这种旧批会被追认成「桩上看得见」。"""
    spill = {_H_CLUE: _filler_obs("queue_depth_alert_threshold = 4630")}
    new = _knee_run(tmp_path / "new", spill=spill, final="阈值 4630", stub_clue="R2")
    old = _knee_run(tmp_path / "old", spill=spill, final="阈值 4630")
    assert A.load_group(new)["records"][0]["recall_source"] == {"threshold": "stub"}
    assert A.load_group(old)["records"][0]["recall_source"] == {"threshold": "unknown"}


def test_recall_source_without_archived_session_is_unknown_for_orchestrator_rows(tmp_path):
    g = _write(tmp_path / "g", [_row(57344, "x", 0, False, mode="orchestrator",
                                     session_dir="/content/gone/s1", fill={"cards": _CARDS},
                                     turns=[{"tool_calls": [], "text": "OPS-73519"}])])
    assert A.load_group(g)["records"][0]["recall_source"] == {"ticket": "unknown"}


def test_render_shows_recall_src_columns(tmp_path):
    g = _knee_run(tmp_path, spill={}, final="OPS-73519 4630")
    grp = A.load_group(g)
    aggs = {grp["name"]: A.aggregate(grp, A.th_global, set())}
    args = A.argparse.Namespace(baseline_levels=[0, 4096], metric="pass_hat_2", drop=0.2,
                                recover=0.1, high_from=16384, max_model_len=65536,
                                gen_reserve=12288, prod_prefix=9424, pair=[])
    out = A.render([grp], aggs, args)
    assert "recall_src 桩/回读/原文" in out and "| 0/0/2 | 0(0) |" in out


# ---- Pass^2 泛化到 rep ≥ 2（2026-11-07：L0 噪声基线 6 rep）----

def test_pass_hat_2_unbiased_for_more_reps_and_identical_for_two(tmp_path):
    from math import comb
    # n = 2：逐项等于泛化前的 all / any
    for v in ([True, True], [True, False], [False, True], [False, False]):
        assert A.pass_hat_2_of(v) == float(all(v)) and A.pass_at_2_of(v) == float(any(v))
    six = [True, True, True, False, False, True]               # c = 4, n = 6
    assert A.pass_hat_2_of(six) == comb(4, 2) / comb(6, 2)
    assert A.pass_at_2_of(six) == 1 - comb(2, 2) / comb(6, 2)
    rows = ([_row(0, "a", r, ok) for r, ok in enumerate(six)]
            + [_row(0, "b", r, ok) for r, ok in enumerate([True, True])]
            + [_row(0, "c", 0, True)]                           # 只剩 1 rep：排出 Pass^2 分母
            + [_row(4096, "a", r, True) for r in range(2)] + [_row(4096, "b", r, r == 0) for r in range(2)])
    g = A.load_group(_write(tmp_path / "g", rows))
    agg = A.aggregate(g, A.th_global, set())
    a0 = agg[0]
    assert a0["n_items2"] == 2 and a0["pass2_k"] == comb(4, 2) / comb(6, 2) + 1
    assert a0["pass_hat_2"] == (comb(4, 2) / comb(6, 2) + 1) / 2
    assert a0["pass_at_2"] == (1 - comb(2, 2) / comb(6, 2) + 1) / 2
    a1 = agg[4096]
    assert (a1["pass2_k"], type(a1["pass2_k"])) == (1, int)    # 全是 2 rep：仍是整数计数
    # 配对与共同题集：6 rep 的题不再被排出
    d, n = A.common_diff(a1["_items"], a0["_items"], "pass_hat_2")
    assert n == 2 and d == ((comb(4, 2) / comb(6, 2) - 1) + (1 - 0)) / 2
    p = A.paired(a1["_items"], a0["_items"], "pass_hat_2")
    assert p["n_items"] == 2 and p["diff"] == d
