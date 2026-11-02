"""11-02 Colab 八臂复测（W-K 编排器模式 + 直调前缀档）：analyze.py 的 A–F 节之上加 G–N 节。

  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/context_knee/analyze_orch.py \
      --data benchmarks/data/context-knee-20261102 --ref benchmarks/data/context-knee-20261031/qwen-current \
      [--out-md <data>/ANALYSIS.md] [--out-csv <data>/summary_by_level.csv]

只读：各臂 `transcripts.jsonl`、编排器臂的 `guardrail_events.jsonl`（按行 `guard_events_lines` 切片）、
`run_colab.log`（臂起止时刻）。不读 `sessions/`（`session_dir` 只当字符串与事件的 session 名比对）。
CPU、标准库 + quality.py（经 analyze.py）；bootstrap 种子固定 → 输出确定。

A–F：原样调 analyze.render（9 组 = 参照 + 8 臂；--pair 见 main() 里 `[f"{REF}:direct-mock"] + PAIRS_TAIL`，
REF 按 `--ref` 目录名现算），CSV 列与 analyze.py 相同（analyze.write_csv）。
G–N 的口径：
- 「实发峰值」= 本 run gens 里引擎回报 prompt_tokens 的最大值（真正送到后端的 prompt）；
  「峰值字段」= 行的 `peak_prompt_tokens`（编排器模式 = max(turn_metrics.peak_prompt_tokens, 实发)，
  护栏在 ceiling 应急压缩之前量到的值也算进去，所以它可以高于任何一次实发）。两者分列。
- W3 预期节省（推断量）：对 all 口径的参照组，每 run 取实发峰值那次生成 m，last_k(K) 会丢掉
  第 0..m−1−K 次生成的思考 → 预期节省 ≈ Σ turns[j].metrics.token_breakdown.thinking（j ≤ m−1−K）；
  不含渲染标记开销，填充里的思考不计（level 是首次 prompt 目标，填充差额由 plan_run 补齐）。
- 比值表（§2 目标）：成功率 / Pass^2 比 = 管理臂 ÷ 无管理臂（同档，各自题集）；Σwall 含 error 行的 wall_s。
- ceiling = context_limit − guard_thresholds.reserve（行内字段，现算，不用常数）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from argparse import Namespace
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parents[1] / "backend", _HERE.parent):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))
from context_knee import analyze as A  # noqa: E402
from context_knee.quality import COHERENT_THRESHOLDS  # noqa: E402

REF = "qwen-current"      # 占位默认；main() 按 --ref 的目录名覆盖这个全局（见 REF 推导）
DIRECT = ["direct-mock", "direct-prod", "direct-catalog", "direct-routed"]
TURN = ["orch-turn-none", "orch-turn-w1", "orch-turn-w1w2"]
HIST = "orch-history-ref"
ARMS = DIRECT + TURN + [HIST]
PAIRS_TAIL = ["direct-mock:direct-prod", "direct-prod:direct-catalog",
              "direct-prod:direct-routed", "orch-turn-none:orch-turn-w1", "orch-turn-none:orch-turn-w1w2",
              "orch-turn-w1:orch-turn-w1w2"]
STUB_MARK = "Unknown tool"
GUARD_TYPES_SHOWN = None   # None = 出现过的全部类型


# ---------------------------------------------------------------- 小件
def med(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def n0(v, d=0) -> str:
    return "—" if v is None else (f"{v:,.{d}f}" if isinstance(v, (int, float)) else str(v))


def pc(v, d=0) -> str:
    return A.pct(v, d)


def ratio(a, b) -> Optional[float]:
    return None if a is None or b in (None, 0) else a / b


def sent_peak(r) -> Optional[int]:
    xs = [g.get("prompt_tokens") for g in (r.get("gens") or []) if g.get("prompt_tokens") is not None]
    return max(xs) if xs else None


def first_pt(r) -> Optional[int]:
    return (A.first_gen(r) or {}).get("prompt_tokens")


def ceiling_of(r) -> Optional[int]:
    gt, lim = r.get("guard_thresholds") or {}, r.get("context_limit")
    return lim - gt["reserve"] if lim and gt.get("reserve") else None


def by_level(rs) -> Dict[int, List[Dict[str, Any]]]:
    out: Dict[int, List[Dict[str, Any]]] = {}
    for r in rs:
        out.setdefault(int(r["level"]), []).append(r)
    return dict(sorted(out.items()))


def dedup_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (level, item_id, rep) 去重，取舍规则与 `A.load_group` 同一套（`A._better`：非 error 行恒优先，
    同类按 stamp 取 latest/oldest）。Σwall 要把 `--resume` 补跑留下的重复行去重掉，但仍要带上 error 行
    的 wall_s——`A.load_group` 的 `records` 丢了 error 行（只在 `missing` 里留错误串，没有 wall_s），
    所以这里直接对 `raw_rows()` 去重，不能借 `records`。"""
    best: Dict[tuple, tuple] = {}
    for i, r in enumerate(rows):
        key = (int(r["level"]), r["item_id"], int(r["rep"]))
        cand = (str(r.get("stamp", "")), i, r)
        best[key] = cand if (key not in best or A._better(cand, best[key])) else best[key]
    return [v[2] for v in best.values()]


def raw_rows(d: Path) -> List[Dict[str, Any]]:
    return [json.loads(x) for x in (d / "transcripts.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def load_events(d: Path) -> Optional[List[Dict[str, Any]]]:
    p = d / "guardrail_events.jsonl"
    if not p.is_file():
        return None
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def seg(r, ev) -> List[Dict[str, Any]]:
    gl = r.get("guard_events_lines")
    return ev[gl[0]:gl[1]] if ev is not None and gl else []


def sess_name(r) -> str:
    return str(r.get("session_dir") or "").rstrip("/").split("/")[-1]


def stub_calls(r) -> Tuple[int, int]:
    """(总工具调用, 回执含 Unknown tool 的调用)：按 turns[*].tool_calls 与 feed 逐位对齐。"""
    tot = unk = 0
    for t in r.get("turns") or []:
        feed = t.get("feed") or []
        for i, _c in enumerate(t.get("tool_calls") or []):
            tot += 1
            if i < len(feed) and STUB_MARK in str(feed[i].get("content", "")):
                unk += 1
    return tot, unk


def w3_expected_saving(r, k: int) -> Optional[int]:
    gens, turns = r.get("gens") or [], r.get("turns") or []
    pts = [g.get("prompt_tokens") or 0 for g in gens]
    if not pts:
        return None
    m = max(range(len(pts)), key=lambda i: pts[i])
    th = [((t.get("metrics") or {}).get("token_breakdown") or {}).get("thinking") for t in turns]
    if any(x is None for x in th[:max(0, m - k)]):
        return None
    return sum(th[:max(0, m - k)])


def arm_elapsed(log: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not log.is_file():
        return out
    start: Dict[str, str] = {}
    from datetime import datetime
    for ln in log.read_text(encoding="utf-8").splitlines():
        m = re.match(r"(\S+ \S+) ARM (\S+) (start|harness rc=\d+)", ln)
        if not m:
            continue
        ts, arm, what = m.groups()
        if what == "start":
            start[arm] = ts
        elif arm in start:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") - datetime.strptime(start[arm], "%Y-%m-%d %H:%M:%S")
            out[arm] = f"{start[arm][11:16]}→{ts[11:16]}（{int(dt.total_seconds() // 60)} min，{what}）"
    return out


# ---------------------------------------------------------------- G：P1 出口 a + W3
def sec_g(G, aggs, k_last: int) -> List[str]:
    L = ["### G. P1 出口 a / W3：direct-mock（11-02，W3 last_k，MTP 关）对 qwen-current（10-31，W3=all，MTP 关）\n"]
    ref, dm = aggs[REF], aggs["direct-mock"]
    common = sorted(set(ref) & set(dm))
    L.append(f"共同档 {common}。思考回放口径（行内 thinking_replay_mode/k）：{REF} = "
             f"{sorted({str(r.get('thinking_replay_mode') or 'all(缺键)') for r in G[REF]['records']})}，direct-mock = "
             f"{sorted({str(r.get('thinking_replay_mode')) + '/' + str(r.get('thinking_replay_k')) for r in G['direct-mock']['records']})}。"
             f"前缀档：{REF} = {sorted({str(r.get('prefix_mode') or 'mock(缺键)') for r in G[REF]['records']})}，"
             f"direct-mock = {sorted({str(r.get('prefix_mode')) for r in G['direct-mock']['records']})}。\n")
    L.append("| 组 | level | 成功率 | Pass^2 | recall | env错均 | 复读命中 | aborted | 连贯 | 首prompt中位 | 实发峰值中位 | 轮内增长(峰−首)中位 | W3预期节省中位(推断) | 生成次数中位 | prefill秒中位 | wall中位 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in (REF, "direct-mock"):
        recs = by_level(G[name]["records"])
        for lv in common:
            a, rs = aggs[name][lv], recs.get(lv, [])
            grow = med((sent_peak(r) or 0) - (first_pt(r) or 0) for r in rs)
            sav = med(w3_expected_saving(r, k_last) for r in rs) if name == REF else None
            L.append(f"| {name} | {lv} | {pc(a['success_rate'])} | {pc(a['pass_hat_2'])} | {pc(a['recall_rate'])} | "
                     f"{A.num(a['env_errors_mean'], 2)} | {a['repetition_hits']} | {a['aborted']} | {pc(a['coherent_rate'])} | "
                     f"{n0(a['prompt_tokens_median'])} | {n0(med(sent_peak(r) for r in rs))} | {n0(grow)} | "
                     f"{n0(sav) if name == REF else '—'} | {n0(a['n_gens_median'])} | {A.num(a['prefill_s_median'])} | "
                     f"{A.num(a['wall_median'])}{'*' if a['wall_invalid'] else ''} |")
    L.append("")
    ps = A.pooled(ref, dm, common, common, "success_rate")
    pp = A.pooled(ref, dm, common, common, "pass_hat_2")
    L.append(f"合并（{len(common)} 档、按题聚类，direct-mock − {REF}）：成功率 {A.fmt_pair(ps)}（升/降 {ps['up']}/{ps['down']} p={A.num(ps['p'], 2)}），"
             f"Pass^2 {A.fmt_pair(pp)}（升/降 {pp['up']}/{pp['down']} p={A.num(pp['p'], 2)}），{ps['n_items']} 题。")
    for lv in common:
        s = A.paired(ref[lv]["_items"], dm[lv]["_items"], "success_rate")
        p = A.paired(ref[lv]["_items"], dm[lv]["_items"], "pass_hat_2")
        L.append(f"- {lv}：成功率 {A.fmt_pair(s)}，Pass^2 {A.fmt_pair(p)}")
    ra, rd = by_level(G[REF]["records"]), by_level(G["direct-mock"]["records"])
    gr = med((sent_peak(r) or 0) - (first_pt(r) or 0) for lv in common for r in ra.get(lv, []))
    gd = med((sent_peak(r) or 0) - (first_pt(r) or 0) for lv in common for r in rd.get(lv, []))
    sv = med(w3_expected_saving(r, k_last) for lv in common for r in ra.get(lv, []))
    L.append(f"\n全档合并的轮内增长中位：{REF} {n0(gr)} → direct-mock {n0(gd)}（差 {n0(None if gr is None or gd is None else gd - gr)}）；"
             f"按 {REF} 思考 token 估的 last_k(K={k_last}) 预期节省中位 {n0(sv)}（推断量，见文件头口径）。\n")
    return L


# ---------------------------------------------------------------- H：W-2 直调四档
def sec_h(G, aggs) -> List[str]:
    L = ["### H. W-2 直调四档（共同档；prefix_base_tokens = 行内字段；未知工具 = 回执含 `Unknown tool` 的调用，即声明了但 harness 不执行的生产工具）\n"]
    common = sorted(set.intersection(*[set(aggs[n]) for n in DIRECT]))
    L.append(f"共同档 {common}。\n")
    L.append("| 组 | level | prefix_base | 首prompt中位 | 成功率 | Pass^2 | 实发峰值中位 | prefill秒中位 | wall中位 | aborted | 缺测 | env错均 | 未知工具调用/总调用 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in DIRECT:
        recs = by_level(G[name]["records"])
        for lv in sorted(aggs[name]):
            a, rs = aggs[name][lv], recs.get(lv, [])
            tot = sum(stub_calls(r)[0] for r in rs)
            unk = sum(stub_calls(r)[1] for r in rs)
            pb = sorted({r.get("prefix_base_tokens") for r in rs})
            L.append(f"| {name} | {lv}{'' if lv in common else '（非共同）'} | {','.join(map(str, pb))} | {n0(a['prompt_tokens_median'])} | "
                     f"{pc(a['success_rate'])} | {pc(a['pass_hat_2'])} | {n0(med(sent_peak(r) for r in rs))} | "
                     f"{A.num(a['prefill_s_median'], 2)} | {A.num(a['wall_median'])} | {a['aborted']} | {a['missing']} | "
                     f"{A.num(a['env_errors_mean'], 2)} | {unk}/{tot} ({pc(ratio(unk, tot))}) |")
    L.append("")
    prod = aggs["direct-prod"]
    p0 = prod.get(0, {}).get("prompt_tokens_median")
    for name in ("direct-mock", "direct-catalog", "direct-routed"):
        x = aggs[name]
        ps = A.pooled(prod, x, common, common, "success_rate")
        pp = A.pooled(prod, x, common, common, "pass_hat_2")
        x0 = x.get(0, {}).get("prompt_tokens_median")
        L.append(f"- {name} − direct-prod（{len(common)} 档合并按题聚类）：成功率 {A.fmt_pair(ps)}（升/降 {ps['up']}/{ps['down']} p={A.num(ps['p'], 2)}），"
                 f"Pass^2 {A.fmt_pair(pp)}；L0 首 prompt 中位 {n0(x0)} vs {n0(p0)}（差 {n0(None if x0 is None or p0 is None else x0 - p0)}，"
                 f"{pc(ratio(None if x0 is None or p0 is None else p0 - x0, p0))} 省）；"
                 f"L0 prefill 秒中位 {A.num(x.get(0, {}).get('prefill_s_median'), 2)} vs {A.num(prod.get(0, {}).get('prefill_s_median'), 2)}")
    L.append("")
    return L


# ---------------------------------------------------------------- I：P2 turn 三臂
def _abort_kinds(rs) -> str:
    c = Counter()
    for r in rs:
        ar = r.get("aborted_reason") or ""
        c["回合上限" if "回合上限" in ar else (ar or "无")] += 1
    return "、".join(f"{k} {v}" for k, v in sorted(c.items()) if k != "无") or "0"


def sec_i(G, aggs, raws, evs, args) -> List[str]:
    L = ["### I. P2 出口：turn 放置三臂（无管理 → +W1 → +W1+W2；prefix prod）\n"]
    k = A.knee(aggs[TURN[0]], args.metric, args.baseline_levels, args.drop, args.recover)
    kb = A.knee(aggs[TURN[0]], "success_rate", args.baseline_levels, args.drop, args.recover)
    L.append(f"{TURN[0]} 拐点（analyze.py 默认规则 metric={args.metric} drop={args.drop}）：**{k['verdict']}**，基线 {k.get('base_level')} 档 {pc(k.get('base'))}；"
             f"成功率口径：{kb['verdict']}，基线 {pc(kb.get('base'))}。"
             f"基线 Pass^2 {pc(k.get('base'))} 时「降 {int(args.drop * 100)} 点」意味着降到 {pc(max(0.0, (k.get('base') or 0) - args.drop))}——规则在地板附近几乎不可触发。\n")
    L.append("| 组 | level | n(缺测) | 成功率 | Pass^2 | 峰值字段中位 | 实发峰值中位 / 最大 | 首prompt中位 | 生成次数中位 | full_renders 总/中位/÷生成 | update_plan/complete_step/remember | aborted 原因 | warning 状态 | Σwall s | wall中位 | system_errors | dup_blocked |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in TURN:
        recs = by_level(G[name]["records"])
        rawlv = by_level(dedup_rows(raws[name]))
        for lv, a in aggs[name].items():
            rs = recs.get(lv, [])
            fr = [r.get("full_renders") for r in rs]
            ng = sum(len(r.get("gens") or []) for r in rs)
            mc = Counter()
            for r in rs:
                mc.update(r.get("meta_calls") or {})
            sp = [sent_peak(r) for r in rs]
            L.append(f"| {name} | {lv} | {a['n']}({a['missing']}) | {pc(a['success_rate'])} | {pc(a['pass_hat_2'])} | "
                     f"{n0(a['peak_prompt_tokens_median'])} | {n0(med(sp))} / {n0(max([x for x in sp if x] or [None]) if any(sp) else None)} | "
                     f"{n0(a['prompt_tokens_median'])} | {n0(a['n_gens_median'])} | "
                     f"{sum(x or 0 for x in fr)}/{n0(med(fr))}/{A.num(ratio(sum(x or 0 for x in fr), ng), 2)} | "
                     f"{mc.get('update_plan', 0)}/{mc.get('complete_step', 0)}/{mc.get('remember', 0)} | {_abort_kinds(rs)} | "
                     f"{sum(1 for r in rs if r.get('turn_status') == 'warning')} | {sum(r.get('wall_s') or 0 for r in rawlv.get(lv, [])):,.0f} | "
                     f"{A.num(a['wall_median'])} | {sum(r.get('system_errors') or 0 for r in rs)} | {sum(r.get('dup_blocked') or 0 for r in rs)} |")
    L.append("")
    types = sorted({e["type"] for n in TURN for e in (evs.get(n) or [])})
    L.append("护栏 / 编排层事件（本臂 run 的 guard_events_lines 切片计数）：\n")
    L.append("| 组 | level | " + " | ".join(types) + " |")
    L.append("|---|---|" + "---|" * len(types))
    for name in TURN:
        for lv, rs in by_level(G[name]["records"]).items():
            c = Counter(e["type"] for r in rs for e in seg(r, evs.get(name)))
            L.append(f"| {name} | {lv} | " + " | ".join(str(c.get(t, 0)) for t in types) + " |")
    L.append("")
    base = aggs[TURN[0]]
    braw = by_level(dedup_rows(raws[TURN[0]]))
    L.append("§2 目标比值（管理臂 ÷ 无管理臂，同档；峰值用实发峰值中位；Σwall 含 error 行）：\n")
    L.append("| 组 | level | 成功率比（≥0.9） | Pass^2 比（≥0.9） | Pass^2 差 | 实发峰值中位 ÷ 无管理 | 实发峰值中位 ÷ level | 实发峰值 ≤ 0.7×level 的 run | Σwall 增量（≤25%） |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for name in TURN[1:]:
        recs = by_level(G[name]["records"])
        rawlv = by_level(dedup_rows(raws[name]))
        brec = by_level(G[TURN[0]]["records"])
        for lv, a in aggs[name].items():
            b = base.get(lv)
            if not b:
                continue
            sp = med(sent_peak(r) for r in recs.get(lv, []))
            spb = med(sent_peak(r) for r in brec.get(lv, []))
            wa = sum(r.get("wall_s") or 0 for r in rawlv.get(lv, []))
            wb = sum(r.get("wall_s") or 0 for r in braw.get(lv, []))
            under = sum(1 for r in recs.get(lv, []) if lv and (sent_peak(r) or 0) <= 0.7 * lv)
            L.append(f"| {name} | {lv} | {A.num(ratio(a['success_rate'], b['success_rate']), 2)} | "
                     f"{A.num(ratio(a['pass_hat_2'], b['pass_hat_2']), 2)} | {A.sgn(None if a['pass_hat_2'] is None or b['pass_hat_2'] is None else a['pass_hat_2'] - b['pass_hat_2'])} | "
                     f"{A.num(ratio(sp, spb), 2)} | {A.num(ratio(sp, lv) if lv else None, 2)} | "
                     f"{(str(under) + '/' + str(a['n'])) if lv else '—'} | {A.sgn(None if not wb else wa / wb - 1)}% |")
    L.append("")
    hi = [lv for lv in base if lv >= args.high_from]
    for name in TURN[1:]:
        ps = A.pooled(base, aggs[name], hi, hi, "success_rate")
        pp = A.pooled(base, aggs[name], hi, hi, "pass_hat_2")
        L.append(f"- {name} − {TURN[0]}，高段 {hi} 合并按题聚类：成功率 {A.fmt_pair(ps)}（升/降 {ps['up']}/{ps['down']} p={A.num(ps['p'], 2)}），"
                 f"Pass^2 {A.fmt_pair(pp)}（升/降 {pp['up']}/{pp['down']} p={A.num(pp['p'], 2)}）")
    # 无管理臂 error 行敏感性：按失败计（同档多条 error 行要一起计入分母，不能每行各自 +1）
    miss = [m for m in G[TURN[0]]["missing"]]
    err_at_level = Counter(lv for lv, _it, _rep, _e in miss)
    for lv, it, rep, e in miss:
        a = base[lv]
        denom = a["n"] + err_at_level[lv]
        L.append(f"- 敏感性：{TURN[0]} {lv} 档 error 行（{it} r{rep}：{e[:60]}…）按失败计入分母时成功率 "
                 f"{a['pass_k']}/{denom} = {pc(a['pass_k'] / denom)}（去重口径 {pc(a['success_rate'])}；该档 error 行共 {err_at_level[lv]} 条）；"
                 f"{it} 因只剩 1 个有效 rep 被排出 Pass^2 分母（不是「记 0」），现 Pass^2 = {a['pass2_k']}/{a['n_items2']} = {pc(a['pass_hat_2'])}。")
    L.append("")
    return L


# ---------------------------------------------------------------- J：W1 天花板路径
def sec_j(G, aggs, raws, evs, levels=(49152, 57344)) -> List[str]:
    L = ["### J. W1 天花板路径（ceiling = context_limit − reserve，行内字段现算）\n"]
    L.append("| 组 | level | runs | soft/hard/ceiling | ceiling_fold（第 1 次生成前 / 之后） | fold before 区间 | fold after 区间 | fold 压缩条数中位 | context_hard（第 1 次 / 之后） | context_guard 终止 | 峰值字段 > 实发峰值 的 run | 实发 ≥ ceiling 的生成 | 实发最大 | recall |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in TURN:
        recs = by_level(G[name]["records"])
        ev = evs.get(name)
        for lv in levels:
            rs = recs.get(lv, [])
            if not rs:
                continue
            ce = sorted({ceiling_of(r) for r in rs} - {None})
            gt = sorted({json.dumps(r.get("guard_thresholds"), sort_keys=True) for r in rs})
            folds = [(e.get("detail") or {}) for r in rs for e in seg(r, ev) if e["type"] == "context_guard_fold"]
            hards = [(e.get("detail") or {}) for r in rs for e in seg(r, ev) if e["type"] == "context_hard"]
            f1 = sum(1 for f in folds if f.get("iteration") == 1)
            h1 = sum(1 for h in hards if h.get("iteration") == 1)
            pre = sum(1 for r in rs if (r.get("peak_prompt_tokens") or 0) > (sent_peak(r) or 0))
            cmax = ce[-1] if ce else 65536 - 12288
            over = sum(1 for r in rs for g in r.get("gens") or [] if (g.get("prompt_tokens") or 0) >= cmax)
            gt_vals = [json.loads(x) for x in gt if x != "null"]
            if len(gt_vals) > 1:
                print(f"⚠️ {name} L{lv}：guard_thresholds 不唯一（{len(gt_vals)} 种）：{gt_vals}", file=sys.stderr)
            gts = gt_vals[0] if gt_vals else None
            thr = (("；".join(f"{v['soft']}/{v['hard']}" for v in gt_vals) + f"/{','.join(map(str, ce))}")
                   if len(gt_vals) > 1 else
                   (f"{gts['soft']}/{gts['hard']}/{','.join(map(str, ce))}" if gts else f"—（guard off；按 {cmax} 计）"))
            rec = aggs[name][lv]["recall_rate"]
            L.append(f"| {name} | {lv} | {len(rs)} | {thr} | {f1} / {len(folds) - f1} | "
                     f"{n0(min([f['before'] for f in folds] or [None]) if folds else None)}–{n0(max([f['before'] for f in folds]) if folds else None)} | "
                     f"{n0(min([f['after'] for f in folds]) if folds else None)}–{n0(max([f['after'] for f in folds]) if folds else None)} | "
                     f"{n0(med(f.get('compacted') for f in folds))} | {h1} / {len(hards) - h1} | "
                     f"{sum(1 for r in rs if r.get('aborted_reason') == 'context_guard')} | {pre} | {over} | "
                     f"{n0(max([sent_peak(r) or 0 for r in rs]))} | {pc(rec)} |")
    L.append("")
    L.append("fold 之后模型做了什么（按 run 计；rag_search / file_extract 是生产工具名：rag_search 在 harness 里是桩、恒回 Unknown tool；"
             "file_extract 只回读 `spill:` 句柄；System Error = 编排层拦截回执）：\n")
    L.append("| 组 | level | runs | 调 rag_search 的 run | 收到 System Error 的 run（其中 aborted） | System Error 回执合计 | 调 file_extract 的 run | spill_readbacks 合计 | aborted 合计 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for name in TURN:
        recs = by_level(G[name]["records"])
        for lv in levels:
            rs = recs.get(lv, [])
            if not rs:
                continue

            def calls(r, nm):
                return any(c.get("name") == nm for t in r.get("turns") or [] for c in t.get("tool_calls") or [])
            se = [r for r in rs if (r.get("system_errors") or 0) > 0]
            L.append(f"| {name} | {lv} | {len(rs)} | {sum(1 for r in rs if calls(r, 'rag_search'))} | "
                     f"{len(se)}（{sum(1 for r in se if (r.get('score') or {}).get('aborted'))}） | {sum(r.get('system_errors') or 0 for r in rs)} | "
                     f"{sum(1 for r in rs if calls(r, 'file_extract'))} | {sum(r.get('spill_readbacks') or 0 for r in rs)} | "
                     f"{sum(1 for r in rs if (r.get('score') or {}).get('aborted'))} |")
    L.append("")
    # 量测 vs 引擎计数
    for name in TURN + [HIST]:
        gs = [g for r in G[name]["records"] for g in r.get("gens") or []]
        dd = [g["prompt_tokens"] - g["measure_tokens"] for g in gs
              if g.get("prompt_tokens") is not None and g.get("measure_tokens") is not None]
        src = Counter(str(g.get("measure_source")) for g in gs)
        L.append(f"- {name}：gens {len(gs)} 次，有量值 {len(dd)} 次，引擎 prompt_tokens − 量值 ≠ 0 的 {sum(1 for d in dd if d)} 次"
                 f"（最大 |差| {max([abs(d) for d in dd] or [0])}）；measure_source {dict(sorted(src.items()))}")
    for name in TURN:
        errs = [r for r in raws[name] if "error" in r]
        for r in errs:
            L.append(f"- {name} error 行：{r['level']} {r['item_id']} r{r['rep']}，wall {r.get('wall_s')} s，出错前已成功生成 {len(r.get('gens') or [])} 次、"
                     f"实发最大 {n0(sent_peak(r))}（不在上表：error 行不进有效记录）：`{str(r['error'])[:230]}`")
    L.append("")
    return L


# ---------------------------------------------------------------- K：W2 有效性
def sec_k(G, evs) -> List[str]:
    L = ["### K. W2 有效性（orch-turn-w1 对 orch-turn-w1w2）\n"]
    L.append("| 组 | level | complete_step 次数 | 有 ≥1 次 complete_step 的 run | update_plan 次数 | step_compaction 事件（按档位） | freed_chars 合计 | turn_metrics.step_compactions 合计 | spill_readbacks 合计 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for name in TURN[1:]:
        for lv, rs in by_level(G[name]["records"]).items():
            sc = [(e.get("detail") or {}) for r in rs for e in seg(r, evs.get(name)) if e["type"] == "step_compaction"]
            byl = Counter(str(d.get("level")) for d in sc)
            L.append(f"| {name} | {lv} | {sum((r.get('meta_calls') or {}).get('complete_step', 0) for r in rs)} | "
                     f"{sum(1 for r in rs if (r.get('meta_calls') or {}).get('complete_step'))}/{len(rs)} | "
                     f"{sum((r.get('meta_calls') or {}).get('update_plan', 0) for r in rs)} | "
                     f"{len(sc)}（{'、'.join(f'{k} {v}' for k, v in sorted(byl.items())) or '—'}） | "
                     f"{sum(d.get('freed_chars') or 0 for d in sc):,} | "
                     f"{sum(int((r.get('turn_metrics') or {}).get('step_compactions') or 0) for r in rs)} | "
                     f"{sum(r.get('spill_readbacks') or 0 for r in rs)} |")
    L.append("")
    return L


# ---------------------------------------------------------------- L：history 参照
def sec_l(G, aggs, evs) -> List[str]:
    L = [f"### L. {HIST}：现状参照（history 放置 + 轮首压缩 + W1 + W2；不与 turn 放置跨比）\n"]
    L.append("| level | 成功率 | Pass^2 | 首prompt中位（÷level） | 伪轮次中位 | 实发峰值中位 | recall (分母) | aborted | context_soft/hard | step_compaction | upgrade_triggered | full_renders中位 | wall中位 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for lv, rs in by_level(G[HIST]["records"]).items():
        a = aggs[HIST][lv]
        c = Counter(e["type"] for r in rs for e in seg(r, evs.get(HIST)))
        L.append(f"| {lv} | {pc(a['success_rate'])} | {pc(a['pass_hat_2'])} | {n0(a['prompt_tokens_median'])}（{A.num(ratio(a['prompt_tokens_median'], lv) if lv else None, 2)}） | "
                 f"{n0(med((r.get('fill') or {}).get('n_pseudo_turns') for r in rs))} | {n0(med(sent_peak(r) for r in rs))} | "
                 f"{pc(a['recall_rate'])} ({a['recall_n']}) | {a['aborted']} | {c.get('context_soft', 0)}/{c.get('context_hard', 0)} | "
                 f"{c.get('step_compaction', 0)} | {c.get('upgrade_triggered', 0)} | {n0(a['full_renders_median'])} | {A.num(a['wall_median'])} |")
    L.append("")
    return L


# ---------------------------------------------------------------- M：数据质量
def sec_m(G, aggs, raws) -> List[str]:
    L = ["### M. 数据质量（去重后；error 行不进率；连贯率按 quality.py 现算）\n"]
    L.append("| 组 | 原始行 | 有效 | error 行（原因） | repetition_events 非空 run | 复读命中合计 | 连贯 run/有正文 run | 非 ja 题含假名/谚文的 run | L0 aborted [Wilson] | 全档 aborted |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for name in [REF] + ARMS:
        g, ag = G[name], aggs[name]
        recs = g["records"]
        errs = Counter(str(r["error"]).split(":")[0] for r in raws[name] if "error" in r)
        coh = [A.coherent(r, A.th_global) for r in recs]
        kana = sum(1 for r in recs if r.get("lang") != "ja" and A._q(r).get("kana_hangul_count", 0) > 0)
        a0 = ag.get(0)
        lo, hi = A.wilson(a0["aborted"], a0["n"]) if a0 else (None, None)
        L.append(f"| {name} | {g['raw_n']} | {len(recs)} | {sum(errs.values())}（{'、'.join(f'{k}×{v}' for k, v in sorted(errs.items())) or '—'}） | "
                 f"{sum(1 for r in recs if r.get('repetition_events'))} | {sum(a['repetition_hits'] for a in ag.values())} | "
                 f"{sum(1 for c in coh if c)}/{sum(1 for c in coh if c is not None)} | {kana} | "
                 f"{(str(a0['aborted']) + '/' + str(a0['n']) + ' ' + A.ci(lo, hi)) if a0 else '—'} | {sum(a['aborted'] for a in ag.values())} |")
    L.append("")
    return L


# ---------------------------------------------------------------- N：口径核对
def sec_n(G, raws, evs, data: Path) -> List[str]:
    L = ["### N. 口径与记账核对（自动检查；非零 = 值得看）\n"]
    L.append("| 组 | 峰值字段≠实发峰值 | full_renders 非 int | full_renders≠turn_metrics | full_renders>生成次数 | Σfull_renders÷Σ生成 | 事件计数≠切片 | 切片 session 不符 | 事件行未被任何 run 覆盖 | fill.prompt_tokens_actual≠首生成 | prefix_base 取值 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for name in [REF] + ARMS:
        recs = G[name]["records"]
        ev = evs.get(name)
        pk = sum(1 for r in recs if r.get("peak_prompt_tokens") is not None and r.get("peak_prompt_tokens") != sent_peak(r))
        nonint = sum(1 for r in recs if not isinstance(r.get("full_renders"), int) or isinstance(r.get("full_renders"), bool))
        tm = sum(1 for r in recs if isinstance(r.get("turn_metrics"), dict)
                 and int(r["turn_metrics"].get("full_renders") or 0) != r.get("full_renders"))
        fr_gt = sum(1 for r in recs if isinstance(r.get("full_renders"), int) and r["full_renders"] > len(r.get("gens") or []))
        sfr = sum(r.get("full_renders") or 0 for r in recs if isinstance(r.get("full_renders"), int))
        sg = sum(len(r.get("gens") or []) for r in recs if isinstance(r.get("full_renders"), int))
        cnt_bad = sess_bad = 0
        covered = set()
        if ev is not None:
            for r in raws[name]:
                gl = r.get("guard_events_lines")
                if gl:
                    covered.update(range(gl[0], gl[1]))
            for r in recs:
                s = seg(r, ev)
                if dict(Counter(e["type"] for e in s)) != {k: v for k, v in (r.get("guard_events") or {}).items() if v}:
                    cnt_bad += 1
                if any(e.get("session") != sess_name(r) for e in s):
                    sess_bad += 1
        unc = (len(ev) - len(covered)) if ev is not None else None
        fa = sum(1 for r in recs if (r.get("fill") or {}).get("prompt_tokens_actual") not in (None, first_pt(r)))
        pb = sorted({r.get("prefix_base_tokens") for r in recs}, key=lambda x: (x is None, x))
        L.append(f"| {name} | {pk} | {nonint} | {tm} | {fr_gt} | {A.num(ratio(sfr, sg), 2)} | "
                 f"{'—' if ev is None else cnt_bad} | {'—' if ev is None else sess_bad} | {'—' if unc is None else unc} | {fa} | {pb} |")
    L.append("")
    # fold 后 fill.prompt_tokens_actual 被首生成（压缩后）回填
    for name in TURN[1:]:
        rs = [r for r in G[name]["records"] if any(e["type"] == "context_guard_fold" and (e.get("detail") or {}).get("iteration") == 1
                                                     for e in seg(r, evs.get(name)))]
        if rs:
            L.append(f"- {name}：第 1 次生成前 fold 的 {len(rs)} 个 run，fill.prompt_tokens_est 中位 {n0(med(r['fill'].get('prompt_tokens_est') for r in rs))}，"
                     f"fill.prompt_tokens_actual 中位 {n0(med(r['fill'].get('prompt_tokens_actual') for r in rs))}（= 压缩后首生成，不是填充实际量）")
    el = arm_elapsed(data / "run_colab.log")
    if el:
        L.append("- run_colab.log 臂起止：" + "；".join(f"{k} {v}" for k, v in el.items()))
    L.append("")
    return L


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="11-02 Colab 八臂复测分析（analyze.py A–F + G–N）")
    ap.add_argument("--data", required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--out-md", default=None)
    ap.add_argument("--out-csv", default=None)
    ap.add_argument("--thinking-k", type=int, default=2)
    a = ap.parse_args(argv)
    data, ref = Path(a.data), Path(a.ref)
    ref_key = os.path.basename(os.path.normpath(a.ref))
    if ref_key in ARMS:
        raise SystemExit(f"❌ --ref 目录名「{ref_key}」与 --data 下的臂名冲突（{ARMS}），"
                          f"换个 --ref 目录（或给它改名）")
    global REF
    REF = ref_key
    dirs = [ref] + [data / n for n in ARMS]
    args = Namespace(metric="pass_hat_2", drop=0.20, recover=0.10, baseline_levels=[0, 4096], high_from=16384,
                     max_model_len=65536, gen_reserve=12288, prod_prefix=None,
                     pair=[f"{REF}:direct-mock"] + PAIRS_TAIL)
    A.KEEP = "latest"
    A.th_global = dict(COHERENT_THRESHOLDS)
    A.wall_global = {x for x in A.DEFAULT_WALL_INVALID.split(",") if x}
    groups = [A.load_group(d) for d in dirs]
    G = {g["name"]: g for g in groups}
    raws = {d.name: raw_rows(d) for d in dirs}
    evs = {d.name: load_events(d) for d in dirs}
    prod_pb = sorted({r.get("prefix_base_tokens") for r in G["direct-prod"]["records"]} - {None})
    prod_prefix_warn = None
    if len(prod_pb) == 1:
        args.prod_prefix = prod_pb[0]
    else:
        args.prod_prefix = max(prod_pb) if prod_pb else None
        prod_prefix_warn = (f"direct-prod 的 prefix_base_tokens 不唯一：{prod_pb or '全缺'}，"
                            f"取较大值 {args.prod_prefix} 代入 F 节")
        print(f"⚠️ {prod_prefix_warn}", file=sys.stderr)
    aggs = {g["name"]: A.aggregate(g, A.th_global, A.wall_global) for g in groups}
    parts = [A.render(groups, aggs, args), ""]
    parts += sec_g(G, aggs, a.thinking_k)
    parts += sec_h(G, aggs)
    parts += sec_i(G, aggs, raws, evs, args)
    parts += sec_j(G, aggs, raws, evs)
    parts += sec_k(G, evs)
    parts += sec_l(G, aggs, evs)
    parts += sec_m(G, aggs, raws)
    parts += sec_n(G, raws, evs, data)
    auto = "\n".join(parts).rstrip() + "\n"
    header_marker = "_自动生成：`benchmarks/context_knee/analyze.py`；"
    if header_marker not in auto:
        raise SystemExit(f"❌ analyze.render 输出里找不到预期的『{header_marker}』——"
                          f"analyze.py 的文案变了，下面这次替换会静默失效")
    auto = auto.replace(header_marker,
                        "_自动生成：`benchmarks/context_knee/analyze_orch.py`（A–F 节 = analyze.render 原样；"
                        f"F 节生产前缀取 direct-prod 行内 prefix_base_tokens {args.prod_prefix}"
                        + (f"；⚠️ {prod_prefix_warn}" if prod_prefix_warn else "") + "）；", 1)
    engine_note = "（engine.py:140 注释的 M5 实测）"
    if engine_note in auto:
        auto = auto.replace(engine_note, "（取 direct-prod 行内字段 prefix_base_tokens，见上）", 1)
    rerun = ("_复跑：`PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/context_knee/analyze_orch.py "
             f"--data {a.data} --ref {a.ref}" + (f" --out-md {a.out_md}" if a.out_md else "")
             + (f" --out-csv {a.out_csv}" if a.out_csv else "") + "`_\n\n")
    auto = rerun + auto
    if a.out_csv:
        A.write_csv(Path(a.out_csv), groups, aggs)
    if a.out_md:
        A.write_md(Path(a.out_md), auto)
    else:
        print(auto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
