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
