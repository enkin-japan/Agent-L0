"""P0 有效上下文拐点：从 transcripts.jsonl 复算 + 拐点判定 + 按题分解 + 采样/模型对照。

  backend/.venv/bin/python benchmarks/context_knee/analyze.py <dir> [<dir> ...] \
      [--out-md ANALYSIS.md] [--out-csv summary_by_level.csv] [--drop 0.20] [--recover 0.10]

与 summarize.py 的区别：
- 先按 (level, item_id, rep) 去重，再算任何率。去重规则：非 error 行优先；其中取 (stamp, 文件序)
  最大者（--keep oldest 取最小者，仅作敏感性）。被丢弃的重复行及其 pass 分歧单列。
- {"error": …} 行（去重后仍为 error）= 缺测，不进任何分母，按 (level, item, rep) 列出。
- 成功率 Wilson 95%（单位 = run）；Pass^2 Wilson 95%（单位 = 题，只计 rep 数 = 2 的题）。
  同题两 rep 相关，run 级区间偏窄——这是已知的乐观偏差，表中如实标注。
- 相对基线的差：限定共同题集（两档都有的题；Pass^2 另要求两档都 2 rep），表里标各档题数。
  区间：Pass^2 只做题级 bootstrap（按题有放回重抽，题内 rep 对保持完整，不再重抽 rep）；成功率两段
  bootstrap（先按题、再档内按 rep）。种子固定。符号检验（题级升降）给双侧精确 p。拐点判定同样用共同题集上的差。
- 采样标签 card(sent …) = harness 已发送给后端的键值；远端是否生效没有回执核验。sampling 不唯一的组标 mixed，
  不参与自动对照。
- 连贯率：quality.final_text_quality(末轮正文, 记录 lang) 现算，is_coherent 判；空答复不进分母。
- prefill 秒 = 首次生成 prompt_tokens / prompt_tps（不按 reused_prefix 过滤，见 README）。
- wall：一组已知因并发不可用的 (目录, level)（--wall-invalid），再加自动检测：同一 level 出现多个 stamp
  的重复行 → 视为两客户端并发写入，该档 wall 标不可用。不可用的 wall 照算中位但标 *。
- MTP 状态 transcripts 里没有记录，只能从目录名推断（名字含 "mtp" → MTP 开，否则关）。

拐点规则（全部可调）：
  metric（默认 pass_hat_2）；基线 = baseline-levels（默认 0,4096）里 metric 点估计最高者；
  候选档 = 大于 0 且不是基线档本身的档，按升序；
  拐点 = 首个满足「metric ≤ 基线 − drop」且「其后所有档 metric < 基线 − recover」的档；无则「到顶档未见拐点」。
  另输出敏感性表（基线取法 × drop × metric）。

ANALYSIS.md 只改 `<!-- BEGIN AUTO -->` 与 `<!-- END AUTO -->` 之间的部分（标记外的手写结论保留）；
文件不存在或无标记时写一个只含自动部分的新文件。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parents[1] / "backend", _HERE.parent):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))
from context_knee.quality import COHERENT_THRESHOLDS, final_text_quality, is_coherent  # noqa: E402

BEGIN, END = "<!-- BEGIN AUTO -->", "<!-- END AUTO -->"
DEFAULT_WALL_INVALID = "qwen-card:57344,qwen-current:0"
BOOT_N, BOOT_SEED = 10000, 20261031
th_global: Dict[str, float] = dict(COHERENT_THRESHOLDS)
wall_global: set = set()
FAIL_ORDER = ("复读", "aborted", "超步数", "无收尾答复", "终态断言", "其他")


# ---------------------------------------------------------------- 统计小件
def wilson(k: int, n: int, z: float = 1.959964) -> Tuple[Optional[float], Optional[float]]:
    if n <= 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - h), min(1.0, c + h)


def sign_test_p(up: int, down: int) -> Optional[float]:
    n = up + down
    if n == 0:
        return None
    k = min(up, down)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n * 2
    return min(1.0, p)


def median(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def mean(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ---------------------------------------------------------------- 读取与去重
KEEP = "latest"   # --keep：latest = stamp 最大者（默认，委托口径）；oldest = stamp 最小者（敏感性用）


def _better(cand, old) -> bool:
    """cand 是否应顶替 old。非 error 行恒优先；同类时 latest 取 (stamp, 文件序) 大者，oldest 取小者。"""
    ok_c, ok_o = "error" not in cand[2], "error" not in old[2]
    if ok_c != ok_o:
        return ok_c
    kc, ko = (cand[0], cand[1]), (old[0], old[1])
    return kc > ko if KEEP == "latest" else kc < ko


def load_group(d: Path) -> Dict[str, Any]:
    path = d / "transcripts.jsonl"
    if not path.is_file():
        raise SystemExit(f"缺 {path}")
    raw = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    best: Dict[tuple, Tuple[str, int, Dict[str, Any]]] = {}
    dropped: List[Dict[str, Any]] = []
    stamps_by_level: Dict[int, set] = {}
    for i, r in enumerate(raw):
        key = (int(r["level"]), r["item_id"], int(r["rep"]))
        cand = (str(r.get("stamp", "")), i, r)
        if key in best:
            old = best[key]
            keep, lose = (cand, old) if _better(cand, old) else (old, cand)
            best[key] = keep
            dropped.append({"key": key, "kept_stamp": keep[0], "dropped_stamp": lose[0],
                            "kept_pass": _pass(keep[2]), "dropped_pass": _pass(lose[2])})
            if "error" not in keep[2] and "error" not in lose[2] and keep[0] != lose[0]:
                # 两条都是真 run、批次不同 → 两客户端并发写入（error 行的补跑重复不算）
                stamps_by_level.setdefault(key[0], set()).update({keep[0], lose[0]})
        else:
            best[key] = cand
    recs = [v[2] for v in best.values()]
    ok = [r for r in recs if "error" not in r]
    missing = sorted(((int(r["level"]), r["item_id"], int(r["rep"]), str(r["error"])[:80])
                      for r in recs if "error" in r))
    models = sorted({str(r.get("model", "?")) for r in raw})
    samp = sorted({json.dumps(_sent(r), sort_keys=True) for r in raw if "error" not in r} or
                  {json.dumps(_sent(r), sort_keys=True) for r in raw})
    concurrent_levels = sorted(lv for lv, s in stamps_by_level.items() if len(s) > 1)
    return {"name": d.name, "dir": str(d), "raw_n": len(raw), "records": ok, "missing": missing,
            "dropped": dropped, "models": models, "sampling": samp,
            "mtp": "mtp" in d.name.lower(), "concurrent_levels": concurrent_levels}


def _sent(r: Dict[str, Any]) -> Dict[str, Any]:
    so = r.get("sampling_overrides") or {}
    return so.get("sent", so.get("applied")) or {}       # applied = 改名前的旧键


def _pass(r: Dict[str, Any]) -> Optional[bool]:
    return None if "error" in r else bool((r.get("score") or {}).get("pass"))


def short_model(g: Dict[str, Any]) -> str:
    return "+".join(m.split("/")[-1] for m in g["models"])


def sampling_label(g: Dict[str, Any]) -> str:
    s = [json.loads(x) for x in g["sampling"]]
    if len(s) != 1:
        return "mixed"
    s = s[0]
    if not s:
        return "current"
    return "card(sent: " + ",".join(f"{k}={v}" for k, v in s.items()) + ")"


# ---------------------------------------------------------------- 每 run 派生量
def _q(r: Dict[str, Any]) -> Dict[str, Any]:
    turns = r.get("turns") or []
    return (final_text_quality(turns[-1].get("text") or "", r.get("lang")) if turns
            else (r.get("final_text_quality") or {}))


def coherent(r: Dict[str, Any], th: Dict[str, float]) -> Optional[bool]:
    return is_coherent(_q(r), th)


def incoherent_kind(r: Dict[str, Any], th: Dict[str, float]) -> Optional[str]:
    """不连贯的归因（只描述触发了哪条判据，不判语义）。"""
    q = _q(r)
    if is_coherent(q, th) is not False:
        return None
    if q.get("dup_ngram_ratio", 0) >= th["dup_ngram_ratio"]:
        return "16-gram重复"
    if q.get("kana_hangul_count", 0) > 0:
        return "假名/谚文"
    if q.get("foreign_script_ratio", 0) >= th["foreign_script_ratio"]:
        return "en题汉字作答" if r.get("lang") == "en" else "其他外文"
    return "en题混写"


def fail_labels(s: Dict[str, Any]) -> List[str]:
    why = " ".join(str(w) for w in (s.get("why") or []))
    labs = []
    if (s.get("repetition_hits") or 0) > 0:
        labs.append("复读")
    if s.get("aborted"):
        labs.append("aborted")
    if s.get("steps_ok") is False or "步数超限" in why:
        labs.append("超步数")
    if s.get("final_text_ok") is False or "无收尾答复" in why:
        labs.append("无收尾答复")
    if s.get("state_ok") is False:
        labs.append("终态断言")
    return labs or ["其他"]


def first_gen(r):
    g = r.get("gens") or []
    return g[0] if g else {}


def prefill_s(g):
    pt, tps = g.get("prompt_tokens"), g.get("prompt_tps")
    return pt / tps if pt and tps else None


# ---------------------------------------------------------------- 按档聚合
def per_item(rs: List[Dict[str, Any]]) -> Dict[str, List[bool]]:
    out: Dict[str, List[bool]] = {}
    for r in sorted(rs, key=lambda x: (x["item_id"], int(x["rep"]))):
        out.setdefault(r["item_id"], []).append(bool(r["score"].get("pass")))
    return out


def aggregate(g: Dict[str, Any], th: Dict[str, float], wall_invalid: set) -> Dict[int, Dict[str, Any]]:
    by_lv: Dict[int, List[Dict[str, Any]]] = {}
    for r in g["records"]:
        by_lv.setdefault(int(r["level"]), []).append(r)
    miss_lv: Dict[int, int] = {}
    for m in g["missing"]:
        miss_lv[m[0]] = miss_lv.get(m[0], 0) + 1
    out = {}
    for lv in sorted(set(by_lv) | set(miss_lv)):
        rs = by_lv.get(lv, [])
        sc = [r["score"] for r in rs]
        n = len(rs)
        k = sum(1 for s in sc if s.get("pass"))
        items = per_item(rs)
        two = {i: v for i, v in items.items() if len(v) == 2}
        both = sum(1 for v in two.values() if all(v))
        anyp = sum(1 for v in two.values() if any(v))
        rec = [s for s in sc if s.get("recall_ok") is not None and not s.get("aborted")]
        slots = {}
        for slot in ("front", "mid", "back"):
            vals = [bool((s.get("recall_by_slot") or {}).get(slot)) for s in rec
                    if slot in (s.get("recall_by_slot") or {})]
            slots[slot] = (sum(vals) / len(vals)) if vals else None
        cohs = [c for c in (coherent(r, th) for r in rs) if c is not None]
        inc: Dict[str, int] = {}
        for r in rs:
            kd = incoherent_kind(r, th)
            if kd:
                key = f"{kd}:{r['item_id']}"
                inc[key] = inc.get(key, 0) + 1
        firsts = [first_gen(r) for r in rs]
        lo, hi = wilson(k, n)
        plo, phi = wilson(both, len(two))
        fails: Dict[str, int] = {}
        prim: Dict[str, int] = {}
        for s in sc:
            if s.get("pass"):
                continue
            labs = fail_labels(s)
            for lab in labs:
                fails[lab] = fails.get(lab, 0) + 1
            p = next(x for x in FAIL_ORDER if x in labs)
            prim[p] = prim.get(p, 0) + 1
        w_bad = (f"{g['name']}:{lv}" in wall_invalid) or (lv in g["concurrent_levels"])
        out[lv] = {
            "n": n, "missing": miss_lv.get(lv, 0), "pass_k": k,
            "success_rate": k / n if n else None, "success_lo": lo, "success_hi": hi,
            "n_items2": len(two), "pass2_k": both,
            "pass_hat_2": both / len(two) if two else None, "pass_hat_2_lo": plo, "pass_hat_2_hi": phi,
            "pass_at_2": anyp / len(two) if two else None,
            "recall_rate": (sum(1 for s in rec if s["recall_ok"]) / len(rec)) if rec else None,
            "recall_n": len(rec),
            "recall_partial": sum(1 for s in rec if 0 < (s.get("recall_hits") or 0) < 3),
            "recall_zero_nofinal": sum(1 for s in rec if (s.get("recall_hits") or 0) == 0
                                       and s.get("final_text_ok") is False),
            "recall_front": slots["front"], "recall_mid": slots["mid"],
            "recall_back": slots["back"],
            "redundant_mean": mean(s.get("redundant_calls") for s in sc),
            "env_errors_mean": mean(s.get("env_errors") for s in sc),
            "repetition_hits": sum(s.get("repetition_hits") or 0 for s in sc),
            "repetition_runs": sum(1 for s in sc if (s.get("repetition_hits") or 0) > 0),
            "aborted": sum(1 for s in sc if s.get("aborted")),
            "coherent_rate": (sum(cohs) / len(cohs)) if cohs else None, "coherent_n": len(cohs),
            "prompt_tokens_median": median(f.get("prompt_tokens") for f in firsts),
            "prefill_s_median": median(prefill_s(f) for f in firsts),
            "first_prompt_tps_median": median(f.get("prompt_tps") for f in firsts),
            "gen_tps_median": median(f.get("generation_tps") for f in firsts),
            "n_gens_median": median(len(r.get("gens") or []) for r in rs),
            "wall_median": median(r.get("wall_s") for r in rs), "wall_invalid": w_bad,
            "incoherent": inc,
            "legacy_mention": sum(1 for r in rs if "legacy-2025q3" in
                                  (((r.get("turns") or [{}])[-1].get("text")) or "")),
            "zero_step": sum(1 for s in sc if s.get("steps") == 0),
            "recall_zero_withfinal": sum(1 for s in rec if (s.get("recall_hits") or 0) == 0
                                         and s.get("final_text_ok") is not False),
            "fail_labels": fails, "fail_primary": prim, "_items": items,
        }
    return out


# ---------------------------------------------------------------- 配对比较
def paired_units(units: Dict[str, Tuple[List[List[bool]], List[List[bool]]]], metric: str):
    """b − a，按题聚类配对。units[题] = (a 侧各档 rep 列表, b 侧各档 rep 列表)。
    每题每侧的值 = 该侧各档 f(reps) 的均值；f = 通过率（success_rate）或「全部 rep 通过」（pass_hat_2，
    只用 rep 数 = 2 的档）。点估计 = 各题 (b − a) 的均值。区间：pass_hat_2 只做题级
    bootstrap（按题有放回重抽，题内各档 rep 对原样保留——「两 rep 都过」是题内 rep 对的性质，再抽 rep
    会把 [T,F] 抽成 [T,T]，系统性抬高、区间偏离点估计）；success_rate 两段（先按题、再档内按 rep）。
    符号检验按题的升/降。"""
    def keep(lists):
        return [v for v in lists if v and (metric != "pass_hat_2" or len(v) == 2)]
    u = {i: (keep(a), keep(b)) for i, (a, b) in units.items()}
    u = {i: ab for i, ab in u.items() if ab[0] and ab[1]}
    if not u:
        return None

    def f(reps):
        return float(all(reps)) if metric == "pass_hat_2" else sum(reps) / len(reps)

    def side(lists):
        return sum(f(v) for v in lists) / len(lists)
    ids = sorted(u)
    per = {i: side(u[i][1]) - side(u[i][0]) for i in ids}
    diff = sum(per.values()) / len(ids)
    rng = random.Random(BOOT_SEED)

    def rs(lists):
        if metric == "pass_hat_2":
            return lists
        return [[rng.choice(v) for _ in v] for v in lists]
    boots = []
    for _ in range(BOOT_N):
        tot = 0.0
        for _k in ids:
            a, b = u[rng.choice(ids)]
            tot += side(rs(b)) - side(rs(a))
        boots.append(tot / len(ids))
    boots.sort()
    up = sum(1 for i in ids if per[i] > 1e-12)
    down = sum(1 for i in ids if per[i] < -1e-12)
    return {"n_items": len(ids), "diff": diff, "median": boots[BOOT_N // 2],
            "lo": boots[int(0.025 * BOOT_N)],
            "hi": boots[int(0.975 * BOOT_N) - 1], "up": up, "down": down, "p": sign_test_p(up, down)}


def paired(a_items: Dict[str, List[bool]], b_items: Dict[str, List[bool]], metric: str):
    """单档对单档：b − a。"""
    return paired_units({i: ([a_items[i]], [b_items[i]]) for i in a_items if i in b_items}, metric)


def pooled(aggs_a, aggs_b, levels_a, levels_b, metric):
    """多档合并、按题聚类：a 取 levels_a 各档、b 取 levels_b 各档。"""
    items = sorted({i for lv in levels_a for i in aggs_a[lv]["_items"]}
                   | {i for lv in levels_b for i in aggs_b[lv]["_items"]})
    units = {i: ([aggs_a[lv]["_items"][i] for lv in levels_a if i in aggs_a[lv]["_items"]],
                 [aggs_b[lv]["_items"][i] for lv in levels_b if i in aggs_b[lv]["_items"]]) for i in items}
    return paired_units(units, metric)


def overlap(a, b, key):
    lo1, hi1, lo2, hi2 = a.get(key + "_lo"), a.get(key + "_hi"), b.get(key + "_lo"), b.get(key + "_hi")
    if None in (lo1, hi1, lo2, hi2):
        return None
    return not (hi1 < lo2 or hi2 < lo1)


# ---------------------------------------------------------------- 拐点
def common_diff(a_items: Dict[str, List[bool]], b_items: Dict[str, List[bool]],
                metric: str) -> Tuple[Optional[float], int]:
    """共同题集上的点差 b − a（不做 bootstrap，拐点判定用）。返回 (差, 共同题数)。"""
    ids = [i for i in a_items if i in b_items and a_items[i] and b_items[i]
           and (metric != "pass_hat_2" or (len(a_items[i]) == 2 and len(b_items[i]) == 2))]
    if not ids:
        return None, 0

    def f(v):
        return float(all(v)) if metric == "pass_hat_2" else sum(v) / len(v)
    return sum(f(b_items[i]) - f(a_items[i]) for i in ids) / len(ids), len(ids)


def knee(agg: Dict[int, Dict[str, Any]], metric: str, base_levels: List[int], drop: float,
         recover: float, base_mode: str = "max") -> Dict[str, Any]:
    """基线 = 基线候选档里 metric（各档自身题集）最高者；各档与基线的比较一律用共同题集上的差。
    top = metric 非 None 的最高档（顶档整档缺测时不算「测到了」）。"""
    eps = 1e-9
    measured = [lv for lv in sorted(agg) if agg[lv].get(metric) is not None]
    if not measured:
        return {"verdict": "无可用档（metric 全空）", "knee": None}
    top = max(measured)
    cands = [lv for lv in base_levels if lv in measured]
    if not cands:
        return {"verdict": "无基线（基线档缺测或单 rep）", "knee": None, "top": top}
    if base_mode == "max":
        base_lv = max(cands, key=lambda lv: (agg[lv][metric], -lv))
    else:
        base_lv = int(base_mode) if int(base_mode) in cands else None
        if base_lv is None:
            return {"verdict": f"无基线档 {base_mode}", "knee": None, "top": top}
    base = agg[base_lv][metric]
    diffs = {}
    for lv in measured:
        if lv > 0 and lv != base_lv:
            d, _n = common_diff(agg[base_lv]["_items"], agg[lv]["_items"], metric)
            if d is not None:
                diffs[lv] = d
    levels = sorted(diffs)
    for i, lv in enumerate(levels):
        if diffs[lv] <= -drop + eps:
            if all(diffs[x] < -recover - eps for x in levels[i + 1:]):
                return {"verdict": f"拐点 {lv}", "knee": lv, "base_level": base_lv, "base": base,
                        "top": top}
    return {"verdict": f"到 {top} 未见拐点", "knee": None, "base_level": base_lv, "base": base,
            "top": top}


# ---------------------------------------------------------------- 按题分类
def classify_items(agg, base_levels, high_from, th_hi=0.75, th_lo=0.25):
    items = sorted({i for a in agg.values() for i in a["_items"]})
    rows = []
    for it in items:
        cells, lowp, highp, allp = {}, [], [], []
        for lv in sorted(agg):
            v = agg[lv]["_items"].get(it)
            cells[lv] = f"{sum(v)}/{len(v)}" if v else "—"
            if v:
                allp += v
                (lowp if lv in base_levels else highp if lv >= high_from else []).extend(v)
        r_all = sum(allp) / len(allp) if allp else None
        r_low = sum(lowp) / len(lowp) if lowp else None
        r_high = sum(highp) / len(highp) if highp else None
        if r_all is not None and r_all >= 0.9:
            cls = "稳定通过"
        elif r_all is not None and r_all <= 0.1:
            cls = "稳定失败"
        elif r_low is not None and r_high is not None and r_low >= th_hi and r_high <= th_lo:
            cls = "随长度失败"
        elif r_low is not None and r_high is not None and r_low <= th_lo and r_high >= th_hi:
            cls = "随长度变好(反向)"
        else:
            cls = "本来就不稳"
        rows.append({"item": it, "cells": cells, "low": r_low, "high": r_high, "all": r_all, "cls": cls})
    return rows


# ---------------------------------------------------------------- 渲染
def pct(v, d=0):
    return "—" if v is None else f"{v * 100:.{d}f}%"


def ci(lo, hi):
    return "—" if lo is None else f"[{lo * 100:.0f},{hi * 100:.0f}]"


def num(v, d=1):
    return "—" if v is None else f"{v:.{d}f}"


def sgn(v):
    return "—" if v is None else f"{v * 100:+.0f}"


CSV_COLS = ["group", "model", "sampling", "mtp_by_dirname", "level", "n", "missing", "pass_k",
            "success_rate", "success_lo", "success_hi", "n_items2", "pass2_k", "pass_hat_2",
            "pass_hat_2_lo", "pass_hat_2_hi", "pass_at_2", "recall_rate", "recall_n", "recall_front",
            "recall_mid", "recall_back", "recall_partial", "recall_zero_nofinal", "recall_zero_withfinal", "legacy_mention", "zero_step", "redundant_mean", "env_errors_mean", "repetition_hits",
            "repetition_runs", "aborted", "coherent_rate", "coherent_n", "prompt_tokens_median",
            "prefill_s_median", "first_prompt_tps_median", "gen_tps_median", "n_gens_median",
            "wall_median", "wall_invalid", "fail_primary", "incoherent"]


def render(groups, aggs, args) -> str:
    global KEEP
    L: List[str] = []
    base_levels = args.baseline_levels
    L.append(f"_自动生成：`benchmarks/context_knee/analyze.py`；规则 metric={args.metric} drop={args.drop} "
             f"recover={args.recover} 基线候选={base_levels}；重复行保留 {KEEP}（非 error 优先）；bootstrap {BOOT_N} 次 seed {BOOT_SEED}：Pass^2 只按题重抽（题内 rep 对保持完整），"
             f"成功率先按题再档内按 rep 重抽；相对基线一律限定共同题集。采样 card(sent …) = 已发送、未回执核验。_\n")
    L.append("### A. 输入与去重\n")
    L.append("| 组 | 模型 | 采样 | MTP(按目录名) | 原始行 | 去重后有效 | 缺测(error) | 丢弃重复行 | 重复行 pass 分歧 | wall 不可用档 | 全组复读命中(run) | 全组 aborted |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for g in groups:
        dis = sum(1 for d in g["dropped"] if d["kept_pass"] != d["dropped_pass"])
        wbad = sorted(lv for lv, a in aggs[g["name"]].items() if a["wall_invalid"])
        L.append(f"| {g['name']} | {short_model(g)} | {sampling_label(g)} | {'开' if g['mtp'] else '关'} | "
                 f"{g['raw_n']} | {len(g['records'])} | {len(g['missing'])} | {len(g['dropped'])} | {dis} | "
                 f"{','.join(map(str, wbad)) or '—'} | "
                 f"{sum(a['repetition_hits'] for a in aggs[g['name']].values())}({sum(a['repetition_runs'] for a in aggs[g['name']].values())}) | "
                 f"{sum(a['aborted'] for a in aggs[g['name']].values())} |")
    L.append("")
    for g in groups:
        if g["missing"]:
            L.append(f"- {g['name']} 缺测：" + "；".join(f"{lv} {it} r{rep}（{e[:40]}）" for lv, it, rep, e in g["missing"]))
        dd = [d for d in g["dropped"] if d["kept_pass"] != d["dropped_pass"]]
        if dd:
            saved, KEEP = KEEP, ("oldest" if KEEP == "latest" else "latest")
            try:
                alt = aggregate(load_group(Path(g["dir"])), th_global, wall_global)
            finally:
                KEEP = saved
            lvs = sorted({d["key"][0] for d in dd})
            ka = knee(alt, args.metric, base_levels, args.drop, args.recover)
            L.append(f"- {g['name']} 去重敏感性：反向保留（{'oldest' if KEEP == 'latest' else 'latest'}）时 "
                     + "；".join(f"{lv} 档 Pass^2 {pct(aggs[g['name']][lv]['pass_hat_2'])}→{pct(alt[lv]['pass_hat_2'])}、"
                                f"成功率 {pct(aggs[g['name']][lv]['success_rate'])}→{pct(alt[lv]['success_rate'])}"
                                for lv in lvs if lv in alt and lv in aggs[g['name']])
                     + f"；拐点判定变为「{ka['verdict']}」")
            L.append(f"- {g['name']} 重复行分歧（按 --keep 保留）：" + "；".join(
                f"{d['key'][0]} {d['key'][1]} r{d['key'][2]} 留 {d['kept_stamp']}={d['kept_pass']} / 弃 {d['dropped_stamp']}={d['dropped_pass']}"
                for d in dd))
    L.append("")

    L.append("### B. 每档汇总（成功率单位 run，Pass^2 单位 题；区间 Wilson 95%）\n")
    L.append("| 组 | level | n | 缺测 | 成功率 [CI] | Pass^2 [CI] (题数) | Pass@2 | recall (分母) | 前/中/后 | recall漏：部分/零命中且无收尾 | 冗余均 | env错均 | 复读命中(run) | aborted | 连贯 (分母) | 首prompt中位 | prefill秒中位 | wall中位 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for g in groups:
        for lv, a in aggs[g["name"]].items():
            L.append(
                f"| {g['name']} | {lv} | {a['n']} | {a['missing']} | {pct(a['success_rate'])} {ci(a['success_lo'], a['success_hi'])} | "
                f"{pct(a['pass_hat_2'])} {ci(a['pass_hat_2_lo'], a['pass_hat_2_hi'])} ({a['n_items2']}) | {pct(a['pass_at_2'])} | "
                f"{pct(a['recall_rate'])} ({a['recall_n']}) | {pct(a['recall_front'])}/{pct(a['recall_mid'])}/{pct(a['recall_back'])} | {a['recall_partial']}/{a['recall_zero_nofinal']} | "
                f"{num(a['redundant_mean'], 2)} | {num(a['env_errors_mean'], 2)} | {a['repetition_hits']}({a['repetition_runs']}) | {a['aborted']} | "
                f"{pct(a['coherent_rate'])} ({a['coherent_n']}) | {num(a['prompt_tokens_median'], 0)} | {num(a['prefill_s_median'])} | "
                f"{num(a['wall_median'])}{'*' if a['wall_invalid'] else ''} |")
    L.append("\n`*` = wall 因并发不可用（已知清单或同档多 stamp 重复行自动检测）。\n")
    L.append("不连贯 run 的归因（判据触发项:题 ×次；不判语义）：\n")
    for g in groups:
        parts = [f"{lv}: " + "、".join(f"{k}×{v}" for k, v in sorted(a["incoherent"].items()))
                 for lv, a in aggs[g["name"]].items() if a["incoherent"]]
        L.append(f"- {g['name']}：" + ("；".join(parts) if parts else "无"))
    L.append("")
    L.append("串台与异常收尾（每档：末轮正文提及填充路径 `legacy-2025q3` 的 run / 零步即终的 run / 回忆零命中但有收尾答复的 run）：\n")
    for g in groups:
        L.append(f"- {g['name']}：" + "；".join(
            f"{lv}: {a['legacy_mention']}/{a['zero_step']}/{a['recall_zero_withfinal']}" for lv, a in aggs[g["name"]].items()))
    L.append("")

    L.append("### C. 拐点判定与相对基线的差\n")
    for g in groups:
        agg = aggs[g["name"]]
        k = knee(agg, args.metric, base_levels, args.drop, args.recover)
        if is_probe(g):
            k = {"verdict": "探针（题数 < 10 或单 rep），不判拐点", "knee": None}
        L.append(f"**{g['name']}**：{k['verdict']}"
                 + (f"（基线 {k['base_level']} 档 {pct(k['base'])}）" if k.get("base_level") is not None else ""))
        if k.get("base_level") is None:
            L.append("")
            continue
        b = agg[k["base_level"]]
        L.append("")
        L.append(f"基线档题数：Pass^2 {b['n_items2']} 题 / 成功率 {len(b['_items'])} 题；差一律在共同题集上算。\n")
        L.append("| level | Pass^2 (本档题数) | 共同题数 | 配对差 [题级 bootstrap 95%] | 题级 升/降 | 符号检验 p | Wilson 区间与基线重叠 | 成功率 (本档题数) | 成功率共同题数 | 成功率配对差 [两段 95%] |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for lv, a in agg.items():
            if lv == k["base_level"]:
                continue
            pp = paired(b["_items"], a["_items"], "pass_hat_2")
            ps = paired(b["_items"], a["_items"], "success_rate")
            ov = overlap(a, b, "pass_hat_2")
            L.append(f"| {lv} | {pct(a['pass_hat_2'])} ({a['n_items2']}) | {pp['n_items'] if pp else '—'} | "
                     f"{fmt_pair(pp)} | "
                     f"{(str(pp['up']) + '/' + str(pp['down'])) if pp else '—'} | {num(pp['p'], 2) if pp else '—'} | "
                     f"{'—' if ov is None else ('重叠' if ov else '不重叠')} | {pct(a['success_rate'])} ({len(a['_items'])}) | "
                     f"{ps['n_items'] if ps else '—'} | {fmt_pair(ps)} |")
        lows = [lv for lv in base_levels if lv in agg]
        highs = [lv for lv in agg if lv >= args.high_from]
        bp = pooled(agg, agg, lows, highs, "pass_hat_2") if lows and highs else None
        bs = pooled(agg, agg, lows, highs, "success_rate") if lows and highs else None
        if bp and bs:
            L.append(f"\n分段合并（按题聚类，低段 {lows} → 高段 ≥{args.high_from}）：Pass^2 {fmt_pair(bp)}，"
                     f"题级升/降 {bp['up']}/{bp['down']} p={num(bp['p'], 2)}；成功率 {fmt_pair(bs)}，"
                     f"升/降 {bs['up']}/{bs['down']} p={num(bs['p'], 2)}（{bs['n_items']} 题）")
        L.append("")
    L.append("敏感性（拐点档；`—` = 未见拐点；基线列 max = 0/4096 取高者）：\n")
    variants = [(m, bm, dr) for m in ("pass_hat_2", "success_rate") for bm in ("max", "0", "4096")
                for dr in (0.15, 0.20, 0.25)]
    L.append("| 组 | " + " | ".join(f"{'P^2' if m == 'pass_hat_2' else '成功'}/{bm}/{int(dr * 100)}" for m, bm, dr in variants) + " |")
    L.append("|---|" + "---|" * len(variants))
    for g in groups:
        cells = []
        for m, bm, dr in variants:
            if is_probe(g):
                cells.append("探针")
                continue
            kk = knee(aggs[g["name"]], m, base_levels, dr, args.recover, bm)
            cells.append(str(kk["knee"]) if kk["knee"] is not None else ("n/a" if kk.get("base_level") is None else "—"))
        L.append(f"| {g['name']} | " + " | ".join(cells) + " |")
    L.append("")

    L.append(f"### D. 按题分解（每格 = 通过 rep / rep 数；低段 = {base_levels}，高段 = ≥{args.high_from}）\n")
    for g in groups:
        agg = aggs[g["name"]]
        rows = classify_items(agg, base_levels, args.high_from)
        lvs = sorted(agg)
        L.append(f"**{g['name']}**\n")
        L.append("| 题 | " + " | ".join(map(str, lvs)) + " | 低段 | 高段 | 全部 | 归类 |")
        L.append("|---|" + "---|" * (len(lvs) + 4))
        for r in rows:
            L.append(f"| {r['item']} | " + " | ".join(r["cells"][lv] for lv in lvs)
                     + f" | {pct(r['low'])} | {pct(r['high'])} | {pct(r['all'])} | {r['cls']} |")
        L.append("")
    L.append("失败原因（主因按 复读命中 > aborted > 超步数 > 无收尾答复 > 终态断言 > 其他 取首个；括号内为多标签计数）：\n")
    L.append("| 组 | level | 失败 run | " + " | ".join(FAIL_ORDER) + " |")
    L.append("|---|---|---|" + "---|" * len(FAIL_ORDER))
    for g in groups:
        for lv, a in aggs[g["name"]].items():
            nf = a["n"] - a["pass_k"]
            L.append(f"| {g['name']} | {lv} | {nf} | " + " | ".join(
                f"{a['fail_primary'].get(x, 0)}({a['fail_labels'].get(x, 0)})" for x in FAIL_ORDER) + " |")
        A = aggs[g["name"]].values()
        L.append(f"| {g['name']} | 合计 | {sum(a['n'] - a['pass_k'] for a in A)} | " + " | ".join(
            f"{sum(a['fail_primary'].get(x, 0) for a in A)}({sum(a['fail_labels'].get(x, 0) for a in A)})"
            for x in FAIL_ORDER) + " |")
    L.append("")

    L.append("### E. 对照（b − a，共同档位、共同题集；Pass^2 题级 bootstrap、成功率两段 bootstrap 95%；正 = b 更好）\n")
    L.append("> 采样对照：card(sent …) 只说明 harness 发送了这些键；**采样键是否在远端生效未经回执核验**。\n")
    for g in groups:
        if sampling_label(g) == "mixed":
            L.append(f"> ⚠️ {g['name']} 的采样不唯一（mixed），已跳过它参与的全部自动对照。\n")
    L.append("| 对照 | level | 成功率差 [配对95%] | Pass^2 差 [配对95%] | 题级 升/降 p | recall a→b | 复读命中 a→b | 连贯 a→b | aborted a→b | env错均 a→b |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    per_item_lines: List[str] = []
    for (an, bn, lab) in comparisons(groups):
        A, B = aggs[an], aggs[bn]
        common = sorted(set(A) & set(B))
        for lv in common:
            a, b = A[lv], B[lv]
            ps = paired(a["_items"], b["_items"], "success_rate")
            pp = paired(a["_items"], b["_items"], "pass_hat_2")
            L.append(f"| {lab} | {lv} | {fmt_pair(ps)} | {fmt_pair(pp)} | "
                     f"{(str(ps['up']) + '/' + str(ps['down']) + ' p=' + num(ps['p'], 2)) if ps else '—'} | "
                     f"{pct(a['recall_rate'])}→{pct(b['recall_rate'])} | "
                     f"{a['repetition_hits']}→{b['repetition_hits']} | {pct(a['coherent_rate'])}→{pct(b['coherent_rate'])} | {a['aborted']}→{b['aborted']} | "
                     f"{num(a['env_errors_mean'], 2)}→{num(b['env_errors_mean'], 2)} |")
        per_item_lines.append(f"- {lab}：" + "，".join(
            f"{it[3:] if it.startswith('lc-') else it} {sum(sum(A[x]['_items'].get(it, [])) for x in common)}"
            f"→{sum(sum(B[x]['_items'].get(it, [])) for x in common)}"
            for it in sorted({i for x in common for i in A[x]['_items']} | {i for x in common for i in B[x]['_items']})))
        ps = pooled(A, B, common, common, "success_rate")
        pp = pooled(A, B, common, common, "pass_hat_2")
        if ps:
            L.append(f"| {lab} | 合并(按题聚类) | {fmt_pair(ps)} | {fmt_pair(pp)} | "
                     f"{ps['up']}/{ps['down']} p={num(ps['p'], 3)} | {pct(_pool_recall(A, common))}→{pct(_pool_recall(B, common))} | "
                     f"{sum(A[x]['repetition_hits'] for x in common)}→{sum(B[x]['repetition_hits'] for x in common)} | | "
                     f"{sum(A[x]['aborted'] for x in common)}→{sum(B[x]['aborted'] for x in common)} | |")
    L.append("")
    L.append("按题通过 run 数（共同档合并，a→b；每题满分 = 共同档数 × 2）：\n")
    L.extend(per_item_lines)
    L.append("")
    L.append("### F. D1/D2 派生（soft = 拐点 × 0.5，hard = 拐点 × 0.8；单位 = 首次生成 prompt 总 token）\n")
    ceiling = args.max_model_len - args.gen_reserve
    L.append(f"ceiling = max_model_len {args.max_model_len} − 生成预留 {args.gen_reserve} = {ceiling}（远端）。"
             f"生产 system+tools 前缀 {args.prod_prefix} tok（engine.py:140 注释的 M5 实测）；harness 基础 prompt 见 B 表 level 0 的首 prompt 中位。\n")
    L.append("| 组 | 拐点（默认规则） | 敏感性范围（P^2 与成功率、各基线、drop 15–25） | soft | hard | hard 与生产前缀之差 | 敏感性上沿 soft/hard | 备注 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for g in groups:
        if is_probe(g):
            continue
        agg = aggs[g["name"]]
        if not agg:
            continue
        k = knee(agg, args.metric, base_levels, args.drop, args.recover)
        ks = sorted({kk["knee"] for m in ("pass_hat_2", "success_rate") for bm in ("max", "0", "4096")
                     for dr in (0.15, 0.20, 0.25)
                     for kk in [knee(agg, m, base_levels, dr, args.recover, bm)] if kk["knee"] is not None}
                    | ({k["knee"]} if k["knee"] is not None else set()))
        top = k.get("top") or max(agg)
        if k["knee"] is not None:
            L.append(f"| {g['name']} | {k['knee']} | {ks[0]}–{ks[-1]} | {k['knee'] * 0.5:.0f} | {k['knee'] * 0.8:.0f} | "
                     f"{k['knee'] * 0.8 - args.prod_prefix:+.0f} | {ks[-1] * 0.5:.0f}/{ks[-1] * 0.8:.0f} | |")
        else:
            rng_s = f"{ks[0]}–{ks[-1]}" if ks else "全部变体未见拐点"
            L.append(f"| {g['name']} | 未见（≤{top}） | {rng_s} | ≥{top * 0.5:.0f} | ≥{top * 0.8:.0f} | "
                     f"{top * 0.8 - args.prod_prefix:+.0f} | — | 下限：来自测量顶档，不是拐点；hard 对 ceiling {top * 0.8 - ceiling:+.0f} |")
    L.append("")
    return "\n".join(L)


def _pool_recall(agg, levels):
    n = sum(agg[x]["recall_n"] for x in levels)
    k = sum(round((agg[x]["recall_rate"] or 0) * agg[x]["recall_n"]) for x in levels)
    return k / n if n else None


def fmt_pair(p):
    return "—" if not p else f"{sgn(p['diff'])} [{sgn(p['lo'])},{sgn(p['hi'])}]"


def is_probe(g) -> bool:
    items = {r["item_id"] for r in g["records"]}
    reps = {int(r["rep"]) for r in g["records"]}
    return len(items) < 10 or len(reps) < 2


def comparisons(groups) -> List[Tuple[str, str, str]]:
    """自动配对：同模型同 MTP 的 current→card；同模型同采样的 MTP关→开；同采样同 MTP 的 Qwen→Gemma。
    只配每档 ≥10 题的组（探针目录跳过）。"""
    full = []
    for g in groups:
        if not g["records"] or is_probe(g):
            continue
        if sampling_label(g) == "mixed":
            print(f"⚠️ {g['name']} 采样不唯一（mixed），跳过自动对照", file=sys.stderr)
            continue
        full.append(g)
    out = []

    def samp_kind(g):
        return "current" if sampling_label(g) == "current" else "card"
    for a in full:
        for b in full:
            if a is b:
                continue
            same_model = short_model(a) == short_model(b)
            if same_model and a["mtp"] == b["mtp"] and samp_kind(a) == "current" and samp_kind(b) == "card":
                out.append((a["name"], b["name"], f"{a['name']}→{b['name']}（采样）"))
            if same_model and samp_kind(a) == samp_kind(b) and not a["mtp"] and b["mtp"]:
                out.append((a["name"], b["name"], f"{a['name']}→{b['name']}（MTP）"))
            if (not same_model and samp_kind(a) == samp_kind(b) and a["mtp"] == b["mtp"] == False
                    and "qwen" in short_model(a).lower() and "gemma" in short_model(b).lower()):
                out.append((a["name"], b["name"], f"{a['name']}→{b['name']}（模型）"))
    return out


def write_csv(path: Path, groups, aggs):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for g in groups:
            for lv, a in aggs[g["name"]].items():
                row = {k: a.get(k) for k in CSV_COLS if k in a}
                row.update({"group": g["name"], "model": short_model(g), "sampling": sampling_label(g),
                            "mtp_by_dirname": g["mtp"], "level": lv,
                            "fail_primary": json.dumps(a["fail_primary"], ensure_ascii=False, sort_keys=True),
                            "incoherent": json.dumps(a["incoherent"], ensure_ascii=False, sort_keys=True)})
                for k, v in row.items():
                    if isinstance(v, float):
                        row[k] = round(v, 4)
                w.writerow(row)


def write_md(path: Path, auto: str):
    block = f"{BEGIN}\n{auto}\n{END}"
    if path.is_file():
        old = path.read_text(encoding="utf-8")
        if BEGIN in old and END in old:
            if old.count(BEGIN) != 1 or old.count(END) != 1 or old.index(END) < old.index(BEGIN):
                raise SystemExit(f"❌ {path} 的 BEGIN/END AUTO 标记重复或顺序颠倒，拒绝改写")
            pre, rest = old.split(BEGIN, 1)
            post = rest.split(END, 1)[1]
            path.write_text(pre + block + post, encoding="utf-8")
            return
    path.write_text("# 自动部分\n\n" + block + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P0 拐点复算与分析")
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--out-md", default=None)
    ap.add_argument("--out-csv", default=None)
    ap.add_argument("--metric", default="pass_hat_2", choices=["pass_hat_2", "success_rate"])
    ap.add_argument("--drop", type=float, default=0.20)
    ap.add_argument("--recover", type=float, default=0.10)
    ap.add_argument("--baseline-levels", default="0,4096")
    ap.add_argument("--high-from", type=int, default=16384)
    ap.add_argument("--wall-invalid", default=DEFAULT_WALL_INVALID)
    ap.add_argument("--coherent-dup", type=float, default=COHERENT_THRESHOLDS["dup_ngram_ratio"])
    ap.add_argument("--coherent-foreign", type=float, default=COHERENT_THRESHOLDS["foreign_script_ratio"])
    ap.add_argument("--coherent-mixed", type=float, default=COHERENT_THRESHOLDS["mixed_token_ratio"])
    ap.add_argument("--max-model-len", type=int, default=65536)
    ap.add_argument("--gen-reserve", type=int, default=12288)
    ap.add_argument("--prod-prefix", type=int, default=9424)
    ap.add_argument("--keep", default="latest", choices=["latest", "oldest"],
                    help="同 (level,item,rep) 重复行保留哪条（oldest 仅作敏感性检查）")
    args = ap.parse_args(argv)
    global KEEP
    KEEP = args.keep
    args.baseline_levels = [int(x) for x in args.baseline_levels.split(",") if x]
    th = {"dup_ngram_ratio": args.coherent_dup, "foreign_script_ratio": args.coherent_foreign,
          "mixed_token_ratio": args.coherent_mixed}
    wall_invalid = {x.strip() for x in args.wall_invalid.split(",") if x.strip()}
    groups = [load_group(Path(d)) for d in args.dirs]
    global th_global, wall_global
    th_global, wall_global = th, wall_invalid
    aggs = {g["name"]: aggregate(g, th, wall_invalid) for g in groups}
    auto = render(groups, aggs, args)
    if args.out_csv:
        write_csv(Path(args.out_csv), groups, aggs)
    if args.out_md:
        write_md(Path(args.out_md), auto)
    else:
        print(auto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
