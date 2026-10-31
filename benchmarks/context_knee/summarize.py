"""拐点汇总：按 level 聚合 transcripts.jsonl，输出 markdown 表；多个产物目录（两模型）并排。

  backend/.venv/bin/python benchmarks/context_knee/summarize.py <out_dir> [<out_dir> ...]

只读 transcripts.jsonl（中断的 run 也能汇总；summary.json 不是必需）。
Pass^2 / Pass@2 按题算（每题 n 个 rep、c 个通过）：pass^2 = C(c,2)/C(n,2)、
pass@2 = 1 − C(n−c,2)/C(n,2)，再对题取均值；n<2 的题不计入（表中 n_tasks2 列出计入数）。
n=2 时即「两 rep 都过」/「至少一 rep 过」。任何率之前先去重：同 (level, 题, rep) 取最新的非 error 行
（全是 error 才留 error）；--resume 补跑的 rep 带新 stamp，照样与旧批同题合并。
采样标签 card(sent …) = 本端已发送的键值，远端是否生效未经回执核验。
prefill 秒(未命中) = 每 run 第一次生成的 prompt_tokens / prompt_tps。远端首次生成上报的
reused_prefix（vLLM num_cached_tokens）实测不可信（探针：报 48000/49204 命中，prompt_tps 却只有
1583 ≈ 31 s 实算），故不按它过滤，原值照列；对照列「后续 prompt_tps 中位」= 每 run 第 2 次起各次
生成 prompt_tps 的中位再跨 run 取中位——真命中缓存时会比首生成高一到两个数量级。
连贯率 = final_text_quality（按记录 lang 现算）判 coherent 的 run / 有非空收尾答复的 run（阈值 --coherent-dup/-foreign/-mixed）。
recall 率分母剔除 aborted run（回合耗尽没给收尾答复，不是记忆问题；aborted 数单列）。
{"error": …} 记录（单 run 异常）只计入 errors 列，不进任何率。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from math import comb
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parents[1] / "backend", _HERE.parent):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))
from context_knee.quality import COHERENT_THRESHOLDS, final_text_quality, is_coherent  # noqa: E402


def pass_hat_k(n: int, c: int, k: int = 2) -> Optional[float]:
    return None if n < k else comb(c, k) / comb(n, k)


def pass_at_k(n: int, c: int, k: int = 2) -> Optional[float]:
    return None if n < k else 1 - comb(n - c, k) / comb(n, k)


def _mean(xs: Iterable[Optional[float]]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _median(xs: Iterable[Optional[float]]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _first_gen(rec: Dict[str, Any]) -> Dict[str, Any]:
    gens = rec.get("gens") or []
    return gens[0] if gens else {}


def _prefill_s(g: Dict[str, Any]) -> Optional[float]:
    pt, tps = g.get("prompt_tokens"), g.get("prompt_tps")
    return pt / tps if pt and tps else None


def _quality(rec: Dict[str, Any]) -> Dict[str, Any]:
    """末轮正文 + 记录 lang 现算（旧记录、run 时未带 lang 的记录同一口径）。"""
    turns = rec.get("turns") or []
    if turns:      # 按记录的题面 lang 现算：判据依赖 lang，run 时记的值未必带 lang
        return final_text_quality(turns[-1].get("text") or "", rec.get("lang"))
    return rec.get("final_text_quality") or {}


def _later_prompt_tps(rec: Dict[str, Any]) -> Optional[float]:
    return _median(g.get("prompt_tps") for g in (rec.get("gens") or [])[1:])


def dedupe(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同 (level, item_id, rep) 只留一条：有非 error 行就取其中最新的（stamp 大者，同 stamp 取
    文件序靠后者），全是 error 才留最新的 error 行。无 rep 字段的行原样保留。"""
    best: Dict[tuple, tuple] = {}
    loose: List[Dict[str, Any]] = []
    for i, r in enumerate(records):
        if "rep" not in r or "item_id" not in r:
            loose.append(r)
            continue
        key = (int(r["level"]), r["item_id"], int(r["rep"]))
        rank = ("error" not in r, str(r.get("stamp", "")), i)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, r)
    return loose + [v[1] for v in sorted(best.values(), key=lambda v: v[0][2])]


def aggregate(records: List[Dict[str, Any]],
              coherent_thresholds: Optional[Dict[str, float]] = None) -> Dict[str, Dict[str, Any]]:
    """按 level 聚合（键为 str(level)，JSON 友好）。先 dedupe，再算任何率。"""
    records = dedupe(records)
    by_level: Dict[int, List[Dict[str, Any]]] = {}
    errors: Dict[int, int] = {}
    for r in records:
        if "error" in r:
            errors[int(r["level"])] = errors.get(int(r["level"]), 0) + 1
            by_level.setdefault(int(r["level"]), [])
            continue
        by_level.setdefault(int(r["level"]), []).append(r)
    out: Dict[str, Dict[str, Any]] = {}
    for level in sorted(by_level):
        rs = by_level[level]
        if not rs:
            out[str(level)] = {"n": 0, "errors": errors.get(level, 0)}
            continue
        scores = [r["score"] for r in rs]
        # Pass^2/@2：按题分组（dedupe 后同 (题, rep) 已唯一；resume 补跑的 rep 带新 stamp，不按 stamp 拆）
        latest: Dict[tuple, bool] = {}
        for r in rs:
            latest[(r["item_id"], r.get("rep"))] = bool(r["score"].get("pass"))
        per_task: Dict[str, List[bool]] = {}
        for (item_id, _rep), ok in latest.items():
            per_task.setdefault(item_id, []).append(ok)
        hat = [pass_hat_k(len(v), sum(v)) for v in per_task.values()]
        at = [pass_at_k(len(v), sum(v)) for v in per_task.values()]
        rec_scores = [s for s in scores if s.get("recall_ok") is not None and not s.get("aborted")]
        slots: Dict[str, Optional[float]] = {}
        for slot in ("front", "mid", "back"):
            vals = [bool((s.get("recall_by_slot") or {}).get(slot)) for s in rec_scores
                    if slot in (s.get("recall_by_slot") or {})]
            slots[slot] = sum(vals) / len(vals) if vals else None
        firsts = [_first_gen(r) for r in rs]
        cohs = [c for c in (is_coherent(_quality(r), coherent_thresholds) for r in rs)
                if c is not None]
        out[str(level)] = {
            "n": len(rs),
            "errors": errors.get(level, 0),
            "success_rate": sum(1 for s in scores if s.get("pass")) / len(rs),
            "pass_hat_2": _mean(hat),
            "pass_at_2": _mean(at),
            "n_tasks2": sum(1 for h in hat if h is not None),
            "recall_rate": (sum(1 for s in rec_scores if s["recall_ok"]) / len(rec_scores)
                            if rec_scores else None),
            "recall_n": len(rec_scores),
            "recall_front": slots["front"], "recall_mid": slots["mid"], "recall_back": slots["back"],
            "redundant_calls_mean": _mean(s.get("redundant_calls") for s in scores),
            "env_errors_mean": _mean(s.get("env_errors") for s in scores),
            "repetition_hits": sum(s.get("repetition_hits") or 0 for s in scores),
            "aborted": sum(1 for s in scores if s.get("aborted")),
            "prompt_tokens_median": _median(g.get("prompt_tokens") for g in firsts),
            "fill_error_median": _median((r.get("fill") or {}).get("fill_error") for r in rs),
            "reused_prefix_median": _median(g.get("reused_prefix") for g in firsts),
            "prefill_s_median": _median(_prefill_s(g) for g in firsts),
            "first_prompt_tps_median": _median(g.get("prompt_tps") for g in firsts),
            "later_prompt_tps_median": _median(_later_prompt_tps(r) for r in rs),
            "coherent_rate": (sum(1 for c in cohs if c) / len(cohs)) if cohs else None,
            "coherent_n": len(cohs),
            "wall_s_mean": _mean(r.get("wall_s") for r in rs),
        }
    return out


def load_records(out_dir: Path) -> List[Dict[str, Any]]:
    path = Path(out_dir) / "transcripts.jsonl"
    if not path.is_file():
        raise SystemExit(f"❌ 缺 {path}")
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _sent(r: Dict[str, Any]) -> Dict[str, Any]:
    so = r.get("sampling_overrides") or {}
    return so.get("sent", so.get("applied")) or {}


def _label(records: List[Dict[str, Any]], out_dir: Path) -> str:
    models = sorted({str(r.get("model", "?")).split("/")[-1]
                     + (" (fake)" if r.get("backend") == "fake" and r.get("model") != "dry-run/fake" else "")
                     for r in records})
    return "+".join(models) if models else out_dir.name


def _fmt(v: Any, kind: str = "f2") -> str:
    if v is None:
        return "—"
    if kind == "pct":
        return f"{v * 100:.0f}%"
    if kind == "int":
        return f"{v:.0f}"
    if kind == "f1":
        return f"{v:.1f}"
    return f"{v:.2f}"


COLUMNS = [
    ("model", None, None), ("level", None, None), ("n", "n", "int"), ("errors", "errors", "int"),
    ("成功率", "success_rate", "pct"), ("Pass^2", "pass_hat_2", "pct"),
    ("Pass@2", "pass_at_2", "pct"), ("题数(≥2rep)", "n_tasks2", "int"),
    ("recall", "recall_rate", "pct"), ("recall分母", "recall_n", "int"), ("前", "recall_front", "pct"),
    ("中", "recall_mid", "pct"), ("后", "recall_back", "pct"),
    ("冗余调用均", "redundant_calls_mean", "f2"), ("env错均", "env_errors_mean", "f2"),
    ("复读命中", "repetition_hits", "int"), ("aborted", "aborted", "int"),
    ("首生成prompt中位", "prompt_tokens_median", "int"), ("填充误差中位", "fill_error_median", "int"),
    ("复用前缀中位", "reused_prefix_median", "int"), ("prefill秒(未命中)", "prefill_s_median", "f1"),
    ("首生成prompt_tps中位", "first_prompt_tps_median", "int"),
    ("后续prompt_tps中位", "later_prompt_tps_median", "int"),
    ("连贯率", "coherent_rate", "pct"), ("连贯分母", "coherent_n", "int"),
    ("wall秒均", "wall_s_mean", "f1"),
]


def render_table(groups: List[tuple]) -> str:
    """groups = [(label, aggregate_dict)]；行按 level 升序、同 level 各模型相邻（并排比较）。"""
    head = "| " + " | ".join(c[0] for c in COLUMNS) + " |"
    sep = "|" + "|".join("---" for _ in COLUMNS) + "|"
    levels = sorted({int(lv) for _, agg in groups for lv in agg})
    rows = [head, sep]
    for lv in levels:
        for label, agg in groups:
            a = agg.get(str(lv))
            if a is None:
                continue
            cells = [label, str(lv)] + [_fmt(a.get(key), kind) for _, key, kind in COLUMNS[2:]]
            rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="拐点汇总（markdown 表）")
    ap.add_argument("out_dirs", nargs="+")
    ap.add_argument("--coherent-dup", type=float, default=COHERENT_THRESHOLDS["dup_ngram_ratio"])
    ap.add_argument("--coherent-foreign", type=float,
                    default=COHERENT_THRESHOLDS["foreign_script_ratio"])
    ap.add_argument("--coherent-mixed", type=float, default=COHERENT_THRESHOLDS["mixed_token_ratio"])
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    th = {"dup_ngram_ratio": args.coherent_dup, "foreign_script_ratio": args.coherent_foreign,
          "mixed_token_ratio": args.coherent_mixed}
    groups, notes = [], []
    for d in args.out_dirs:
        recs = load_records(Path(d))
        label = _label(recs, Path(d))
        groups.append((label, aggregate(recs, th)))
        sents = sorted({json.dumps(_sent(r), sort_keys=True) for r in recs if "sampling_overrides" in r})
        if sents:
            notes.append(f"- {label} 采样：" + " / ".join(
                "current（未覆盖）" if s == "{}" else f"card(sent {s})（已发送、未回执核验）" for s in sents))
    print(render_table(groups))
    if notes:
        print("\n" + "\n".join(notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
