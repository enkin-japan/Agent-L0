"""成对续跑评估的汇总表（只读 transcripts.jsonl，CPU、标准库）。

  backend/.venv/bin/python benchmarks/summary_trace/summarize_trace.py <out> [<out> ...]

按 (model, summary_max_tokens, template, task_state, drop_task, tool_digest_chars, turn1_iterations)
分组（后两键 D45 加；老行缺键归 "None" 组，老报表数值不变）；配对键另加源目录与
(题, rep)——合并多批（640 vs 900、legacy vs new、digest 200 vs 0 分目录）时各批各自配对。同一配对键同一支出现
两次（同一目录传了两遍、transcripts 被拼接过）直接报错退出，绝不静默覆盖。
- Δ(repeated+blocked) = POST − PRE，先在 (题, rep) 上配对求差，再按题平均；95% CI = 题级
  bootstrap（有放回重抽题，B 次，取各次「题均差的均值」的 2.5 / 97.5 分位）。n 很小时 CI 很宽，
  如实给出，不做显著性判词。
- pass 率 PRE / POST、续跑首次生成 prompt token 中位 PRE / POST、POST 摘要字符中位。
- D47：turn 1 remember 调用总数、turn 1 记录带便签（[Scratchpad]）的 pair 数——两支共用同一个 turn 1，
  按对计一次（取 PRE 行）。某组 PRE 行里一行都没带这个键（老批次）时两项聚合为 None，报表显示 "—"；
  组内有行带键时按现逻辑算（该组里缺键的行视 0 / False）。
error 行与缺一支的 (题, rep) 不进配对，计入 n_unpaired。
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_BOOT = 1000


def load_rows(out_dirs: Iterable[Path]) -> List[Dict[str, Any]]:
    """读各目录的 transcripts.jsonl；每行补 `_source`（目录的 resolve 路径），配对键用它。"""
    rows: List[Dict[str, Any]] = []
    for d in out_dirs:
        p = Path(d) / "transcripts.jsonl"
        if not p.exists():
            raise SystemExit(f"❌ 缺 {p}")
        src = str(Path(d).resolve())
        with p.open(encoding="utf-8") as fh:
            for ln in fh:
                if ln.strip():
                    rows.append({**json.loads(ln), "_source": src})
    return rows


GROUP_KEYS = ("model", "summary_max_tokens", "template", "task_state", "drop_task",
              "tool_digest_chars", "turn1_iterations")


def group_key(r: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(str(r.get(k)) for k in GROUP_KEYS)


def cost(row: Dict[str, Any]) -> int:
    """续跑重复功 = repeated + blocked。"""
    return int(row.get("repeated") or 0) + int(row.get("blocked") or 0)


def pairs(rows: List[Dict[str, Any]]) -> Tuple[Dict[Tuple[str, ...], Dict[Tuple[str, str, int], Dict[str, Dict[str, Any]]]], int]:
    """{group_key: {(源目录, item, rep): {"pre": row, "post": row}}}，以及不成对的条数。
    同一配对键同一支第二次出现 = SystemExit（不覆盖）。"""
    groups: Dict[Tuple[str, ...], Dict[Tuple[str, str, int], Dict[str, Dict[str, Any]]]] = {}
    for r in rows:
        if "error" in r or r.get("branch") not in ("pre", "post"):
            continue
        g = groups.setdefault(group_key(r), {})
        pk = (str(r.get("_source")), str(r.get("item")), int(r.get("rep") or 0))
        slot = g.setdefault(pk, {})
        if r["branch"] in slot:
            raise SystemExit(f"❌ 配对键碰撞：{dict(zip(GROUP_KEYS, group_key(r)))} {pk} "
                             f"branch={r['branch']} 出现两次（同一目录重复传入或 transcripts 被拼接）")
        slot[r["branch"]] = r
    unpaired = sum(1 for r in rows if "error" in r)
    for g in groups.values():
        for k in [k for k, v in g.items() if not ("pre" in v and "post" in v)]:
            unpaired += 1
            del g[k]
    return groups, unpaired


def item_mean_diffs(group: Dict[Tuple[str, str, int], Dict[str, Dict[str, Any]]]) -> Dict[str, float]:
    """题 → 该题各 (目录, rep) 的 POST−PRE 重复功差的均值（同组多目录 = 更多 rep）。"""
    per: Dict[str, List[int]] = {}
    for (_src, item, _rep), v in group.items():
        per.setdefault(item, []).append(cost(v["post"]) - cost(v["pre"]))
    return {k: sum(v) / len(v) for k, v in per.items()}


def bootstrap_ci(values: List[float], *, n_boot: int = DEFAULT_BOOT, seed: int = 0
                 ) -> Tuple[Optional[float], Optional[float]]:
    """题级 bootstrap 95% CI（均值）。<2 个题 = (None, None)：一个点给不出区间。"""
    if len(values) < 2:
        return None, None
    rnd = random.Random(seed)
    means = sorted(sum(rnd.choice(values) for _ in values) / len(values) for _ in range(n_boot))
    lo = means[int(0.025 * (n_boot - 1))]
    hi = means[int(0.975 * (n_boot - 1))]
    return lo, hi


def _median(xs: List[Any]) -> Optional[float]:
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return statistics.median(xs) if xs else None


def _rate(rows: List[Dict[str, Any]]) -> Optional[float]:
    return (sum(1 for r in rows if r.get("score")) / len(rows)) if rows else None


def aggregate(rows: List[Dict[str, Any]], *, n_boot: int = DEFAULT_BOOT, seed: int = 0
              ) -> Dict[str, Any]:
    groups, unpaired = pairs(rows)
    out: Dict[str, Any] = {"n_unpaired": unpaired, "groups": []}
    for key, g in sorted(groups.items()):
        diffs = item_mean_diffs(g)
        vals = list(diffs.values())
        lo, hi = bootstrap_ci(vals, n_boot=n_boot, seed=seed)
        pre = [v["pre"] for v in g.values()]
        post = [v["post"] for v in g.values()]
        out["groups"].append({
            **dict(zip(GROUP_KEYS, key)), "n_pairs": len(g), "n_items": len(diffs),
            "delta_cost_mean": (sum(vals) / len(vals)) if vals else None,
            "delta_cost_ci95": [lo, hi],
            "cost_pre_mean": (sum(cost(r) for r in pre) / len(pre)) if pre else None,
            "cost_post_mean": (sum(cost(r) for r in post) / len(post)) if post else None,
            "pass_pre": _rate(pre), "pass_post": _rate(post),
            "first_pt_median_pre": _median([r.get("first_prompt_tokens") for r in pre]),
            "first_pt_median_post": _median([r.get("first_prompt_tokens") for r in post]),
            "summary_chars_median": _median([r.get("summary_chars") for r in post]),
            # 老批次（D47 之前跑的）PRE 行没有这两个键：缺键时聚合给 None，报表显示 "—"，
            # 不要跟真实的 0 混在一起。
            "turn1_remember_calls_total": (
                sum(int(r.get("turn1_remember_calls") or 0) for r in pre)
                if any("turn1_remember_calls" in r for r in pre) else None),
            "turn1_scratchpad_pairs": (
                sum(1 for r in pre if r.get("turn1_task_state_has_scratchpad"))
                if any("turn1_task_state_has_scratchpad" in r for r in pre) else None),
        })
    return out


def _f(x: Any, nd: int = 2) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def report(out_dirs: Iterable[Path], *, seed: int = 0, n_boot: int = DEFAULT_BOOT) -> str:
    agg = aggregate(load_rows(out_dirs), n_boot=n_boot, seed=seed)
    head = ("| model | max_tokens | template | task_state | drop_task | tool_digest | turn1_iter | 对数 | 题数 | "
            "重复功 PRE | 重复功 POST | Δ(POST−PRE) | 95% CI | "
            "pass PRE | pass POST | 首 prompt 中位 PRE | 首 prompt 中位 POST | 摘要字符中位 | "
            "turn1 remember | 有便签对数 |")
    lines = [head, "|" + "---|" * 20]
    for g in agg["groups"]:
        lo, hi = g["delta_cost_ci95"]
        ci = "—" if lo is None else f"[{lo:.2f}, {hi:.2f}]"
        lines.append(
            f"| {g['model']} | {g['summary_max_tokens']} | {g['template']} | {g['task_state']} | "
            f"{g['drop_task']} | {g['tool_digest_chars']} | {g['turn1_iterations']} | "
            f"{g['n_pairs']} | {g['n_items']} | "
            f"{_f(g['cost_pre_mean'])} | {_f(g['cost_post_mean'])} | {_f(g['delta_cost_mean'])} | {ci} | "
            f"{_f(g['pass_pre'])} | {_f(g['pass_post'])} | {_f(g['first_pt_median_pre'], 0)} | "
            f"{_f(g['first_pt_median_post'], 0)} | {_f(g['summary_chars_median'], 0)} | "
            f"{_f(g['turn1_remember_calls_total'], 0)} | {_f(g['turn1_scratchpad_pairs'], 0)} |")
    lines.append(f"（重复功 = repeated + blocked；不成对 / error：{agg['n_unpaired']}；"
                 f"CI = 题级 bootstrap {n_boot} 次，<2 题不给）")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("用法：summarize_trace.py <out> [<out> ...]")
    print(report([Path(a) for a in argv]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
