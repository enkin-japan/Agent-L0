"""11-02 Colab 八臂复测（W-K 编排器模式 + 直调前缀档）：analyze.py 的 A–F 节之上加 G–N 节。

  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/context_knee/analyze_orch.py \
      --data benchmarks/data/context-knee-20261102 --ref benchmarks/data/context-knee-20261031/qwen-current \
      [--out-md <data>/ANALYSIS.md] [--out-csv <data>/summary_by_level.csv]

只读：各臂 `transcripts.jsonl`、编排器臂的 `guardrail_events.jsonl`（按行 `guard_events_lines` 切片）、
`run_colab.log`（臂起止时刻）。本模块不读 `sessions/`（`session_dir` 只当字符串与事件的 session 名比对）；
例外是经 `A.load_group` 算的 recall_source（D43 (a)）：读 `<臂>/sessions/<session_dir 末段>/` 的 spill/ 与 journal.jsonl。
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
from context_knee.orch_tools import norm_guard_thresholds  # noqa: E402
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
EXTRA_ORCH: List[str] = []   # 第二批（b2）：turn 三臂之外并入编排器表的臂（orch-turn-catalog）；11-02 批为空 → 输出不变
GUARD_TYPES_SHOWN = None   # None = 出现过的全部类型
RAG_NOTE = "rag_search 在 harness 里是桩、恒回 Unknown tool"   # 第二批（W-K2 可用桩）main_b2 改写


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
        m = re.match(r"(\S+ \S+) ARM (\S+)(?: \[[^\]]*\])? (start|harness rc=\d+)", ln)
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
    L = ["### I. P2 出口：turn 放置三臂（无管理 → +W1 → +W1+W2；prefix prod）"
         + (f"；首表与事件表并入 {'、'.join(EXTRA_ORCH)}（比值表只比 prod 三臂）" if EXTRA_ORCH else "") + "\n"]
    k = A.knee(aggs[TURN[0]], args.metric, args.baseline_levels, args.drop, args.recover)
    kb = A.knee(aggs[TURN[0]], "success_rate", args.baseline_levels, args.drop, args.recover)
    L.append(f"{TURN[0]} 拐点（analyze.py 默认规则 metric={args.metric} drop={args.drop}）：**{k['verdict']}**，基线 {k.get('base_level')} 档 {pc(k.get('base'))}；"
             f"成功率口径：{kb['verdict']}，基线 {pc(kb.get('base'))}。"
             f"基线 Pass^2 {pc(k.get('base'))} 时「降 {int(args.drop * 100)} 点」意味着降到 {pc(max(0.0, (k.get('base') or 0) - args.drop))}——规则在地板附近几乎不可触发。\n")
    L.append("| 组 | level | n(缺测) | 成功率 | Pass^2 | 峰值字段中位 | 实发峰值中位 / 最大 | 首prompt中位 | 生成次数中位 | full_renders 总/中位/÷生成 | update_plan/complete_step/remember | aborted 原因 | warning 状态 | Σwall s | wall中位 | system_errors | dup_blocked |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in TURN + EXTRA_ORCH:
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
    types = sorted({e["type"] for n in TURN + EXTRA_ORCH for e in (evs.get(n) or [])})
    L.append("护栏 / 编排层事件（本臂 run 的 guard_events_lines 切片计数）：\n")
    L.append("| 组 | level | " + " | ".join(types) + " |")
    L.append("|---|---|" + "---|" * len(types))
    for name in TURN + EXTRA_ORCH:
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
                 f"{it} 因只剩 1 个有效 rep 被排出 Pass^2 分母（不是「记 0」），现 Pass^2 = {A.fmt_k(a['pass2_k'])}/{a['n_items2']} = {pc(a['pass_hat_2'])}。")
    L.append("")
    return L


# ---------------------------------------------------------------- J：W1 天花板路径
def sec_j(G, aggs, raws, evs, levels=(49152, 57344)) -> List[str]:
    L = ["### J. W1 天花板路径（ceiling = context_limit − reserve，行内字段现算）\n"]
    L.append("| 组 | level | runs | soft/hard/ceiling | ceiling_fold（第 1 次生成前 / 之后） | fold before 区间 | fold after 区间 | fold 压缩条数中位 | context_hard（第 1 次 / 之后） | context_guard 终止 | 峰值字段 > 实发峰值 的 run | 实发 ≥ ceiling 的生成 | 实发最大 | recall |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in TURN + EXTRA_ORCH:
        recs = by_level(G[name]["records"])
        ev = evs.get(name)
        for lv in levels:
            rs = recs.get(lv, [])
            if not rs:
                continue
            ce = sorted({ceiling_of(r) for r in rs} - {None})
            # D48：fold_mode 键 2026-11-09 才加入，归一（缺键按 full 补齐）后再判唯一性/展示——
            # 不然同臂一半行有 fold_mode、一半（更早）缺键会被误判成「不唯一」（与 run_knee.
            # harness_mode_of 的续跑口径比对共用同一归一函数，见 orch_tools.norm_guard_thresholds）
            gt = sorted({json.dumps(norm_guard_thresholds(r.get("guard_thresholds")), sort_keys=True)
                        for r in rs})
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
            thr = (("；".join(f"{v['soft']}/{v['hard']}（fold {v['fold_mode']}）" for v in gt_vals)
                    + f"/{','.join(map(str, ce))}")
                   if len(gt_vals) > 1 else
                   (f"{gts['soft']}/{gts['hard']}/{','.join(map(str, ce))}（fold {gts['fold_mode']}）"
                    if gts else f"—（guard off；按 {cmax} 计）"))
            rec = aggs[name][lv]["recall_rate"]
            L.append(f"| {name} | {lv} | {len(rs)} | {thr} | {f1} / {len(folds) - f1} | "
                     f"{n0(min([f['before'] for f in folds] or [None]) if folds else None)}–{n0(max([f['before'] for f in folds]) if folds else None)} | "
                     f"{n0(min([f['after'] for f in folds]) if folds else None)}–{n0(max([f['after'] for f in folds]) if folds else None)} | "
                     f"{n0(med(f.get('compacted') for f in folds))} | {h1} / {len(hards) - h1} | "
                     f"{sum(1 for r in rs if r.get('aborted_reason') == 'context_guard')} | {pre} | {over} | "
                     f"{n0(max([sent_peak(r) or 0 for r in rs]))} | {pc(rec)} |")
    L.append("")
    L.append(f"fold 之后模型做了什么（按 run 计；rag_search / file_extract 是生产工具名：{RAG_NOTE}；"
             "file_extract 只回读 `spill:` 句柄；System Error = 编排层拦截回执）：\n")
    L.append("| 组 | level | runs | 调 rag_search 的 run | 收到 System Error 的 run（其中 aborted） | System Error 回执合计 | 调 file_extract 的 run | spill_readbacks 合计 | aborted 合计 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for name in TURN + EXTRA_ORCH:
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
    for name in TURN + EXTRA_ORCH + ([HIST] if HIST in G else []):
        gs = [g for r in G[name]["records"] for g in r.get("gens") or []]
        dd = [g["prompt_tokens"] - g["measure_tokens"] for g in gs
              if g.get("prompt_tokens") is not None and g.get("measure_tokens") is not None]
        src = Counter(str(g.get("measure_source")) for g in gs)
        L.append(f"- {name}：gens {len(gs)} 次，有量值 {len(dd)} 次，引擎 prompt_tokens − 量值 ≠ 0 的 {sum(1 for d in dd if d)} 次"
                 f"（最大 |差| {max([abs(d) for d in dd] or [0])}）；measure_source {dict(sorted(src.items()))}")
    for name in TURN + EXTRA_ORCH:
        errs = [r for r in raws[name] if "error" in r]
        for r in errs:
            L.append(f"- {name} error 行：{r['level']} {r['item_id']} r{r['rep']}，wall {r.get('wall_s')} s，出错前已成功生成 {len(r.get('gens') or [])} 次、"
                     f"实发最大 {n0(sent_peak(r))}（不在上表：error 行不进有效记录）：`{str(r['error'])[:230]}`")
    L.append("")
    return L


# ---------------------------------------------------------------- K：W2 有效性
def sec_k(G, evs) -> List[str]:
    L = ["### K. W2 有效性（orch-turn-w1 对 orch-turn-w1w2" + "".join(f" / {x}" for x in EXTRA_ORCH) + "）\n"]
    L.append("| 组 | level | complete_step 次数 | 有 ≥1 次 complete_step 的 run | update_plan 次数 | step_compaction 事件（按档位） | freed_chars 合计 | turn_metrics.step_compactions 合计 | W2免压回读 | spill_readbacks 合计 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for name in TURN[1:] + EXTRA_ORCH:
        for lv, rs in by_level(G[name]["records"]).items():
            sc = [(e.get("detail") or {}) for r in rs for e in seg(r, evs.get(name)) if e["type"] == "step_compaction"]
            byl = Counter(str(d.get("level")) for d in sc)
            L.append(f"| {name} | {lv} | {sum((r.get('meta_calls') or {}).get('complete_step', 0) for r in rs)} | "
                     f"{sum(1 for r in rs if (r.get('meta_calls') or {}).get('complete_step'))}/{len(rs)} | "
                     f"{sum((r.get('meta_calls') or {}).get('update_plan', 0) for r in rs)} | "
                     f"{len(sc)}（{'、'.join(f'{k} {v}' for k, v in sorted(byl.items())) or '—'}） | "
                     f"{sum(d.get('freed_chars') or 0 for d in sc):,} | "
                     f"{sum(int((r.get('turn_metrics') or {}).get('step_compactions') or 0) for r in rs)} | "
                     f"{n0(_sum_or_none(w2_readback_exempt(r, evs.get(name)) for r in rs))} | "
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
def sec_n(G, raws, evs, data: Path, logs=("run_colab.log",)) -> List[str]:
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
    for name in TURN[1:] + EXTRA_ORCH:
        rs = [r for r in G[name]["records"] if any(e["type"] == "context_guard_fold" and (e.get("detail") or {}).get("iteration") == 1
                                                     for e in seg(r, evs.get(name)))]
        if rs:
            L.append(f"- {name}：第 1 次生成前 fold 的 {len(rs)} 个 run，fill.prompt_tokens_est 中位 {n0(med(r['fill'].get('prompt_tokens_est') for r in rs))}，"
                     f"fill.prompt_tokens_actual 中位 {n0(med(r['fill'].get('prompt_tokens_actual') for r in rs))}（= 压缩后首生成，不是填充实际量）")
    for lg in logs:
        el = arm_elapsed(data / lg)
        if el:
            L.append(f"- {lg} 臂起止：" + "；".join(f"{k} {v}" for k, v in el.items()))
    L.append("")
    return L


# ---------------------------------------------------------------- 第二批（b2）：O–T 节
# 只在 --data 是第二批布局（有 orch-turn-catalog、无 direct-mock）时渲染；11-02 批不走这里。
GEMMA = ["gemma-direct-mock", "gemma-direct-catalog"]
B2_ORCH = ["orch-turn-none", "orch-turn-w1", "orch-turn-w1w2", "orch-turn-catalog"]
TRACE_DIRS = ["trace-new-900", "trace-legacy-640", "trace-new-640"]
PREV_TAG = "@am"            # --prev 批（11-02 上午）的组名后缀，避免与本批同名臂撞名
DELIVERY_MARK = "delivery blocked"
B2_EXTRA_COLS = ["stub_served_total", "stub_unknown_total", "feed_unknown_tool", "stub_failed_total",
                 "tool_calls_total", "system_errors_total", "delivery_blocked", "retrieval_incomplete_runs",
                 "iter_exhausted_runs", "context_guard_aborts", "ceiling_folds", "ceiling_compacted",
                 "ceiling_spilled", "sent_over_ceiling_gens", "spill_files", "spill_readbacks",
                 "missing_arg_recovered", "schema_fed", "w2_readback_exempt"]


def orch_arms(data: Path, known: Optional[List[str]] = None) -> List[str]:
    """编排器臂列表：已知顺序（缺省 B2_ORCH）在前，--data 下其余 `orch-turn-*` 臂目录（有 transcripts.jsonl）
    按名追加（如 orch-turn-full；orch-history-ref 等非 turn 放置的目录不算）。已知臂是否齐由调用方判（缺臂报错）。"""
    known = list(B2_ORCH if known is None else known)
    extra = sorted(p.name for p in data.glob("orch-turn-*")
                   if p.is_dir() and p.name not in known and (p / "transcripts.jsonl").is_file())
    return known + extra


def b2_layout(data: Path) -> bool:
    return (data / "orch-turn-catalog").is_dir() and not (data / "direct-mock").is_dir()


def stub_row(r) -> Dict[str, Any]:
    """行级 W-K2 桩计数（stub_served {名: 次}、stub_unknown、stub_failed；旧行缺键 = None）
    + 独立复数：feed 里 `Unknown tool` 回执数（与 stub_unknown 同源规则，不同实现）。"""
    tot, unk = stub_calls(r)
    ss = r.get("stub_served")
    return {"has": isinstance(ss, dict), "served": dict(ss or {}), "served_n": sum((ss or {}).values()),
            "unknown": r.get("stub_unknown"), "failed": r.get("stub_failed"),
            "calls": tot, "feed_unknown": unk}


def pushback_row(r) -> Dict[str, int]:
    """检索推回链：feed 里 `delivery blocked` 回执数、turn_metrics.retrieval_incomplete；回合上限耗尽；context_guard 中止。"""
    db = sum(str(f.get("content", "")).count(DELIVERY_MARK) for t in r.get("turns") or [] for f in t.get("feed") or [])
    ar = r.get("aborted_reason") or ""
    return {"delivery_blocked": db,
            "retrieval_incomplete": 1 if (r.get("turn_metrics") or {}).get("retrieval_incomplete") else 0,
            "iter_exhausted": 1 if "回合上限" in ar else 0,
            "context_guard": 1 if ar == "context_guard" else 0}


def spill_files(r) -> Optional[int]:
    """会话目录 sessions/<session>/spill/ 下的文件数（session_dir 是 Colab 上的绝对路径，按末段名在本地 --data 里找）。"""
    d = r.get("_local_session")
    if d is None:
        return None
    sp = Path(d) / "spill"
    return sum(1 for x in sp.iterdir() if x.is_file()) if sp.is_dir() else 0


def w1b_row(r, ev) -> Dict[str, Any]:
    folds = [(e.get("detail") or {}) for e in seg(r, ev) if e["type"] == "context_guard_fold"]
    aborts = [(e.get("detail") or {}) for e in seg(r, ev)
              if e["type"] == "turn_abort" and (e.get("detail") or {}).get("reason") == "context_guard"]
    ce = ceiling_of(r)
    lim = r.get("context_limit")
    pts = [g.get("prompt_tokens") for g in r.get("gens") or [] if g.get("prompt_tokens") is not None]
    flags = r.get("turn_guard_flags")
    return {"folds": len(folds), "fold_spilled_runs": 1 if any((f.get("spilled") or 0) > 0 for f in folds) else 0,
            "compacted": sum(f.get("compacted") or 0 for f in folds), "spilled": sum(f.get("spilled") or 0 for f in folds),
            "aborts": len(aborts), "abort_compacted": sum(a.get("compacted") or 0 for a in aborts),
            "abort_spilled": sum(a.get("spilled") or 0 for a in aborts),
            "abort_pt": [a.get("prompt_tokens") for a in aborts],
            "over_ceiling": sum(1 for x in pts if ce and x >= ce), "over_limit": sum(1 for x in pts if lim and x > lim),
            "hard": sum(1 for e in seg(r, ev) if e["type"] == "context_hard"),
            "flags": flags if isinstance(flags, dict) else None, "has_flags_key": "turn_guard_flags" in r,
            "sent_max": max(pts) if pts else None}


def _attach_sessions(G, data: Path, names) -> None:
    for n in names:
        for r in G[n]["records"]:
            sd = data / n / "sessions" / sess_name(r)
            r["_local_session"] = sd if sd.is_dir() else None


def sec_o(G, aggs, names) -> List[str]:
    L = ["### O. W-K2 可用桩与推回链（编排器臂 × level；`@am` = 11-02 上午批同名臂，无桩字段记 —）\n",
         "口径：桩调用 = 行字段 `stub_served` 各名求和；`stub_unknown` = 行字段（绊线，应恒 0）；feed Unknown = 本脚本按 turns[*].feed "
         "逐位重数回执含 `Unknown tool` 的调用（独立复核绊线）；delivery blocked = feed 回执含该串的次数；检索未完成 = "
         "`turn_metrics.retrieval_incomplete` 为真的 run；迭代耗尽 = aborted_reason 含「回合上限」；超限中止 = aborted_reason == context_guard。\n",
         "| 组 | level | n | 成功率 | 工具调用 | 桩调用 | stub_unknown | feed Unknown | stub_failed | system_errors | delivery blocked | 检索未完成 run | 迭代耗尽 run | 超限中止 run |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name in names:
        for lv, rs in by_level(G[name]["records"]).items():
            st = [stub_row(r) for r in rs]
            pb = [pushback_row(r) for r in rs]
            has = all(x["has"] for x in st)
            L.append(f"| {name} | {lv} | {len(rs)} | {pc(aggs[name][lv]['success_rate'])} | {sum(x['calls'] for x in st)} | "
                     f"{sum(x['served_n'] for x in st) if has else '—'} | {sum(x['unknown'] or 0 for x in st) if has else '—'} | "
                     f"{sum(x['feed_unknown'] for x in st)} | {sum(x['failed'] or 0 for x in st) if has else '—'} | "
                     f"{sum(r.get('system_errors') or 0 for r in rs)} | {sum(x['delivery_blocked'] for x in pb)} | "
                     f"{sum(x['retrieval_incomplete'] for x in pb)} | {sum(x['iter_exhausted'] for x in pb)} | {sum(x['context_guard'] for x in pb)} |")
    L.append("")
    L.append("桩按工具名（全档合计，降序；括号 = 占该臂工具调用的比例）：\n")
    for name in names:
        rs = G[name]["records"]
        st = [stub_row(r) for r in rs]
        if not all(x["has"] for x in st):
            continue
        c = Counter()
        for x in st:
            c.update(x["served"])
        tot = sum(x["calls"] for x in st)
        L.append(f"- {name}：桩 {sum(c.values())}/{tot} 调用（{pc(ratio(sum(c.values()), tot))}）："
                 + "、".join(f"{k} {v}" for k, v in c.most_common()) +
                 f"；stub_unknown 合计 {sum(x['unknown'] or 0 for x in st)}（非 0 行 {sum(1 for x in st if x['unknown'])}）；"
                 f"stub_failed 合计 {sum(x['failed'] or 0 for x in st)}（非 0 行 {sum(1 for x in st if x['failed'])}）")
    L.append("")
    base = names[0]
    fails = [r for r in G[base]["records"] if int(r["level"]) == 0 and not (r.get("score") or {}).get("pass")]
    L.append(f"{base} L0 失败 run 明细（{len(fails)}/{sum(1 for r in G[base]['records'] if int(r['level']) == 0)}；why = 评分器原文，"
             "步数 = score.steps，元工具 = meta_calls 合计，桩 = stub_served 合计）：\n")
    for r in sorted(fails, key=lambda x: (x["item_id"], int(x["rep"]))):
        sc = r.get("score") or {}
        L.append(f"- {r['item_id']} r{r['rep']}：{'；'.join(str(w) for w in sc.get('why') or []) or '—'}（步数 {sc.get('steps')}，"
                 f"max_total_iterations {r.get('max_total_iterations')}，元工具 {sum((r.get('meta_calls') or {}).values())}，"
                 f"桩 {stub_row(r)['served_n']}，aborted_reason「{r.get('aborted_reason') or ''}」）")
    L.append("")
    return L


def sec_p(G, aggs, evs, names, prev_names) -> List[str]:
    L = ["### P. W1b：ceiling 压缩带回读句柄（事件 = guard_events_lines 切片；spill 文件 = 本地 sessions/<session>/spill/ 计数）\n",
         "spill 文件含 W2 步骤边界压缩（spill_full）落的盘，不只 ceiling；事件里的 spilled 才是 ceiling 压缩给出句柄的条数。\n",
         "| 组 | level | n | ceiling_fold 事件 | Σcompacted | Σspilled | fold 带 spill 的 run | context_hard 事件 | 超限中止（turn_abort context_guard）| 中止时量值 | spill 文件 | spill_readbacks（run 数） | 实发 ≥ ceiling 的生成 | 实发 > context_limit 的生成 | 实发最大 | recall (分母) |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    flag_rows = flag_key = 0
    for name in names:
        ev = evs.get(name)
        for lv, rs in by_level(G[name]["records"]).items():
            w = [w1b_row(r, ev) for r in rs]
            flag_key += sum(1 for x in w if x["has_flags_key"])
            flag_rows += len(w)
            sf = [spill_files(r) for r in rs]
            rb = [r.get("spill_readbacks") or 0 for r in rs]
            apt = [x for y in w for x in y["abort_pt"]]
            a = aggs[name][lv]
            L.append(f"| {name} | {lv} | {len(rs)} | {sum(x['folds'] for x in w)} | {sum(x['compacted'] for x in w)} | "
                     f"{sum(x['spilled'] for x in w)} | {sum(x['fold_spilled_runs'] for x in w)} | {sum(x['hard'] for x in w)} | "
                     f"{sum(x['aborts'] for x in w)}（compacted {sum(x['abort_compacted'] for x in w)} / spilled {sum(x['abort_spilled'] for x in w)}） | "
                     f"{','.join(map(str, apt)) or '—'} | {'—' if any(x is None for x in sf) else sum(sf)} | "
                     f"{sum(rb)}（{sum(1 for x in rb if x)}） | {sum(x['over_ceiling'] for x in w)} | {sum(x['over_limit'] for x in w)} | "
                     f"{n0(max([x['sent_max'] or 0 for x in w]))} | {pc(a['recall_rate'])} ({a['recall_n']}) |")
    L.append("")
    L.append(f"- 行级 `turn_guard_flags` 键：{flag_key}/{flag_rows} 行带（不带时 ceiling_compacted / ceiling_spilled 只能从事件读，即上表）。")
    for name in prev_names:
        if name in aggs and 57344 in aggs[name]:
            a = aggs[name][57344]
            L.append(f"- 对照 {name} L57344：recall {pc(a['recall_rate'])} ({a['recall_n']})，成功率 {pc(a['success_rate'])}")
    L.append("")
    return L


def _tool_shape(rs) -> Tuple[Counter, List[int]]:
    c = Counter()
    per = []
    for r in rs:
        n = 0
        for t in r.get("turns") or []:
            for x in t.get("tool_calls") or []:
                c[str(x.get("name"))] += 1
                n += 1
        per.append(n)
    return c, per


# ---------------------------------------------------------------- D44 / D43b 行级计数
#: 参数类 System Error 的判别串（D44 预检回执 `… missing required argument(s): …` / `… unknown
#: argument(s): …`；harness 桩直调模式的 `execute() missing required argument(s)` 同样命中第一条）
_ARG_CLASSES = (("missing required argument", "缺必填参数"), ("unknown argument(s)", "未知参数"))


def syserr_class(text: str) -> str:
    for pat, label in _ARG_CLASSES:
        if pat in text:
            return label
    return "其他"


def feed_pairs(r) -> List[Tuple[str, str]]:
    """本 run 全部 feed 回执按序展开成 (工具名, 回执文本)。工具名优先取 feed 自己的 name（编排器模式
    traj_from_history 写的，元工具在前的回执序也不会错位）；旧行没有就按位取同 turn 的 tool_calls。"""
    out: List[Tuple[str, str]] = []
    for t in r.get("turns") or []:
        calls = t.get("tool_calls") or []
        for i, f in enumerate(t.get("feed") or []):
            nm = f.get("name") if isinstance(f, dict) else None
            if nm is None and i < len(calls):
                nm = calls[i].get("name")
            out.append((str(nm), str((f or {}).get("content", "")) if isinstance(f, dict) else ""))
    return out


#: 编排层 `_FAILED_RESP_RE`（backend/framework/core/orchestrator.py）的副本——分析器只用标准库、不 import
#: 编排层；两边逐字一致由 test_context_knee_analyze_orch 钉住
_FAILED_RESP_RE = re.compile(r"^System Error:|^exit_code: (?!0\b)\d+\b(?! \(benign\b)")
#: 没真执行的拦截回执（不算重发成功）
_NOT_RUN_PREFIXES = ("Duplicate call blocked", "Denied-call blocked")


def feed_failed(text: str) -> bool:
    """harness 口径的「这条回执是失败」：编排层失败正则（System Error / 非良性非零退出码）、桩 / env 的
    `{"error": …}` JSON 回执、去重 / 拒批拦截回执。"""
    if _FAILED_RESP_RE.search(text) or text.startswith(_NOT_RUN_PREFIXES):
        return True
    if text.lstrip().startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return False
        return isinstance(obj, dict) and "error" in obj
    return False


def missing_arg_recovered(r) -> int:
    """D44：本 run 里参数类（缺必填 / 未知参数）System Error 回执中，被**之后**同一工具的一次成功回执
    （`feed_failed` 判否）收回的条数。按序配对、一次成功只收回一条更早的打回（先进先出）。"""
    pending: Dict[str, int] = {}
    n = 0
    for tool, text in feed_pairs(r):
        if text.startswith("System Error") and syserr_class(text) != "其他":
            pending[tool] = pending.get(tool, 0) + 1
        elif not feed_failed(text) and pending.get(tool):
            pending[tool] -= 1
            n += 1
    return n


def schema_fed(r, ev) -> Optional[int]:
    """D44：本 run 预检回执附了完整声明的次数 = 本 run 切片里 tool_error 事件 detail.schema_fed 为真的条数。
    只认事件流：臂没有 guardrail_events.jsonl（ev 为 None）→ None（与 w2_readback_exempt 同一契约）。"""
    if ev is None:
        return None
    return sum(1 for e in seg(r, ev)
               if e["type"] == "tool_error" and (e.get("detail") or {}).get("schema_fed") is True)


def w2_readback_exempt(r, ev) -> Optional[int]:
    """D43b：本 run 切片里 step_readback_exempt 事件的 count 之和（W2 边界上**首次**免压的 spill 回读条数；
    同一条回读跨边界只计一次，全免的边界也有事件）。旧批没有这个事件 = 0；无事件流 → None。"""
    if ev is None:
        return None
    return sum(int((e.get("detail") or {}).get("count") or 0)
               for e in seg(r, ev) if e["type"] == "step_readback_exempt")


def _sum_or_none(xs) -> Optional[int]:
    xs = list(xs)
    return None if any(x is None for x in xs) else sum(xs)


def sec_q(G, aggs, evs, a_name="orch-turn-w1w2", b_name="orch-turn-catalog") -> List[str]:
    L = [f"### Q. catalog 对照臂：{b_name}（catalog 前缀）对 {a_name}（prod 前缀）——两臂管理开关相同（guard on + step_compaction on），只差前缀档\n",
         "| 组 | level | 成功率 | Pass^2 | prefix_base | 首prompt中位 | 实发峰值中位 | 生成次数中位 | 每 run 工具调用 中位/均 | 桩调用 | stub_failed | tool_error 事件 | update_plan/complete_step | 迭代耗尽 | wall中位 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name in (a_name, b_name):
        for lv, rs in by_level(G[name]["records"]).items():
            a = aggs[name][lv]
            _c, per = _tool_shape(rs)
            st = [stub_row(r) for r in rs]
            mc = Counter()
            for r in rs:
                mc.update(r.get("meta_calls") or {})
            te = sum(1 for r in rs for e in seg(r, evs.get(name)) if e["type"] == "tool_error")
            L.append(f"| {name} | {lv} | {pc(a['success_rate'])} | {pc(a['pass_hat_2'])} | "
                     f"{','.join(map(str, sorted({r.get('prefix_base_tokens') for r in rs})))} | {n0(a['prompt_tokens_median'])} | "
                     f"{n0(med(sent_peak(r) for r in rs))} | {n0(a['n_gens_median'])} | {n0(med(per))}/{A.num(sum(per) / len(per) if per else None, 1)} | "
                     f"{sum(x['served_n'] for x in st)} | {sum(x['failed'] or 0 for x in st)} | {te} | "
                     f"{mc.get('update_plan', 0)}/{mc.get('complete_step', 0)} | {sum(pushback_row(r)['iter_exhausted'] for r in rs)} | {A.num(a['wall_median'])} |")
    L.append("")
    common = sorted(set(aggs[a_name]) & set(aggs[b_name]))
    ps = A.pooled(aggs[a_name], aggs[b_name], common, common, "success_rate")
    pp = A.pooled(aggs[a_name], aggs[b_name], common, common, "pass_hat_2")
    L.append(f"- {b_name} − {a_name}（{len(common)} 档合并按题聚类）：成功率 {A.fmt_pair(ps)}（升/降 {ps['up']}/{ps['down']} p={A.num(ps['p'], 2)}），"
             f"Pass^2 {A.fmt_pair(pp)}（升/降 {pp['up']}/{pp['down']} p={A.num(pp['p'], 2)}）")
    for name in (a_name, b_name):
        c, per = _tool_shape(G[name]["records"])
        tot = sum(c.values())
        L.append(f"- {name} 工具调用形态（全档 {tot} 次，前 10）：" + "、".join(f"{k} {v}（{pc(v / tot)}）" for k, v in c.most_common(10)))
    for name in (a_name, b_name):
        se = Counter()
        rec_n = 0
        for r in G[name]["records"]:
            for tool, s_ in feed_pairs(r):
                if s_.startswith("System Error"):
                    se[(tool, syserr_class(s_))] += 1
            rec_n += missing_arg_recovered(r)
        fed_n = _sum_or_none(schema_fed(r, evs.get(name)) for r in G[name]["records"])
        L.append(f"- {name} `System Error` 回执（工具, 类）合计 {sum(se.values())}"
                 f"（其中随后同工具成功 {rec_n}；schema_fed {'—' if fed_n is None else fed_n}）：" + "、".join(f"{k[0]}/{k[1]} {v}" for k, v in se.most_common()))
    L.append("")
    return L


def sec_r(G, aggs, names, pairs) -> List[str]:
    L = ["### R. Gemma 4 31B 直调两臂与参照（各组模型 / 采样 / 回放口径见 A 节；成功率单位 run，Pass^2 单位 题）\n",
         "| 组 | 模型 | 采样 | 回放 | level | n | 成功率 | Pass^2 | recall | aborted | env错均 | 复读命中 | 连贯 | 首prompt中位 | 实发峰值中位 | 轮内增长中位 | prefill秒中位 | wall中位 | 未知工具调用/总调用 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name in names:
        if name not in G:
            continue
        g = G[name]
        recs = by_level(g["records"])
        rp = sorted({f"{r.get('thinking_replay_mode') or 'all(缺键)'}" + (f"/{r.get('thinking_replay_k')}" if r.get('thinking_replay_k') else "")
                     for r in g["records"]})
        for lv, a in aggs[name].items():
            rs = recs.get(lv, [])
            tot = sum(stub_calls(r)[0] for r in rs)
            unk = sum(stub_calls(r)[1] for r in rs)
            L.append(f"| {name} | {A.short_model(g)} | {A.sampling_label(g)} | {','.join(rp)} | {lv} | {a['n']} | {pc(a['success_rate'])} | "
                     f"{pc(a['pass_hat_2'])} | {pc(a['recall_rate'])} | {a['aborted']} | {A.num(a['env_errors_mean'], 2)} | "
                     f"{a['repetition_hits']} | {pc(a['coherent_rate'])} | {n0(a['prompt_tokens_median'])} | {n0(med(sent_peak(r) for r in rs))} | "
                     f"{n0(med((sent_peak(r) or 0) - (first_pt(r) or 0) for r in rs))} | {A.num(a['prefill_s_median'], 2)} | {A.num(a['wall_median'])} | "
                     f"{unk}/{tot} ({pc(ratio(unk, tot))}) |")
    L.append("")
    L.append("合并对照（b − a，共同档、按题聚类 bootstrap；正 = b 更好）：\n")
    for a_name, b_name in pairs:
        if a_name not in aggs or b_name not in aggs:
            continue
        common = sorted(set(aggs[a_name]) & set(aggs[b_name]))
        ps = A.pooled(aggs[a_name], aggs[b_name], common, common, "success_rate")
        pp = A.pooled(aggs[a_name], aggs[b_name], common, common, "pass_hat_2")
        L.append(f"- {b_name} − {a_name}（共同档 {common}）：成功率 {A.fmt_pair(ps)}（升/降 {ps['up']}/{ps['down']} p={A.num(ps['p'], 2)}），"
                 f"Pass^2 {A.fmt_pair(pp)}（升/降 {pp['up']}/{pp['down']} p={A.num(pp['p'], 2)}），{ps['n_items']} 题；"
                 f"回忆 {pc(A._pool_recall(aggs[a_name], common))} → {pc(A._pool_recall(aggs[b_name], common))}")
    L.append("")
    return L


def _trace_rows(d: Path) -> List[Dict[str, Any]]:
    return [json.loads(x) for x in (d / "transcripts.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def sec_t(data: Path) -> List[str]:
    from summary_trace import fold_forensics as FF
    from summary_trace import summarize_trace as ST
    dirs = [data / n for n in TRACE_DIRS if (data / n / "transcripts.jsonl").is_file()]
    if not dirs:
        return []
    L = ["### T. 成对续跑（summary_trace，D4：模板 new / legacy × 摘要上限 900 / 640；`*.partial-*` 半截目录不计）\n"]
    L.append("| 目录 | template / task_state / max_tokens / drop_task | 对 ok / error | 支 | pass | cont_status 分布 | repeated 均 | blocked 均（dup_blocked / system_errors） | cont_redundant_calls 均 | env_errors 均 | cont_gens 中位 | 首 prompt 中位 | 摘要字符中位 | 摘要 token 中位 | pair_wall_s 中位 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    ok_keys: Dict[str, set] = {}
    for d in dirs:
        rows = _trace_rows(d)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").is_file() else {}
        err = [r for r in rows if "error" in r]
        good = [r for r in rows if "error" not in r]
        ok_keys[d.name] = {(r["item"], int(r["rep"])) for r in good}
        cfg = f"{meta.get('template')} / {meta.get('task_state')} / {meta.get('summary_max_tokens')} / {meta.get('drop_task')}"
        for br in ("pre", "post"):
            rs = [r for r in good if r.get("branch") == br]
            if not rs:
                continue
            cs = Counter(str(r.get("cont_status")) for r in rs)

            def mean(k):
                xs = [r.get(k) for r in rs if isinstance(r.get(k), (int, float))]
                return sum(xs) / len(xs) if xs else None
            L.append(f"| {d.name} | {cfg} | {len(ok_keys[d.name])} / {len(err)} | {br} | {sum(1 for r in rs if r.get('score'))}/{len(rs)} | "
                     + "、".join(f"{k} {v}" for k, v in sorted(cs.items())) + f" | {A.num(mean('repeated'), 2)} | "
                     f"{A.num(mean('blocked'), 2)}（{sum(r.get('dup_blocked') or 0 for r in rs)} / {sum(r.get('system_errors') or 0 for r in rs)}） | "
                     f"{A.num(mean('cont_redundant_calls'), 2)} | {A.num(mean('env_errors'), 2)} | {n0(med(r.get('cont_gens') for r in rs))} | "
                     f"{n0(med(r.get('first_prompt_tokens') for r in rs))} | {n0(med(r.get('summary_chars') for r in rs)) if br == 'post' else '—'} | "
                     f"{n0(med(r.get('summary_tokens_est') for r in rs)) if br == 'post' else '—'} | {A.num(med(r.get('pair_wall_s') for r in rs))} |")
    L.append("")
    if "trace-legacy-640" in ok_keys:
        keep = ok_keys["trace-legacy-640"]
        L.append(f"限 trace-legacy-640 成功的 {len(keep)} 对 (题, rep) 的同口径子集（new 两步同题同 rep；题与 rep 同 seed 但 turn 1 是否逐字相同未核）：\n")
        L.append("| 目录 | 对 | pass PRE / POST | repeated 均 PRE / POST | blocked 均 PRE / POST | cont_status POST |")
        L.append("|---|---|---|---|---|---|")
        for d in dirs:
            rows = [r for r in _trace_rows(d) if "error" not in r and (r["item"], int(r["rep"])) in keep]
            pre = [r for r in rows if r["branch"] == "pre"]
            post = [r for r in rows if r["branch"] == "post"]

            def mean2(rs, k):
                return A.num(sum(r.get(k) or 0 for r in rs) / len(rs), 2) if rs else "—"
            L.append(f"| {d.name} | {len(post)} | {sum(1 for r in pre if r.get('score'))} / {sum(1 for r in post if r.get('score'))} | "
                     f"{mean2(pre, 'repeated')} / {mean2(post, 'repeated')} | {mean2(pre, 'blocked')} / {mean2(post, 'blocked')} | "
                     + "、".join(f"{k} {v}" for k, v in sorted(Counter(str(r.get('cont_status')) for r in post).items())) + " |")
        L.append("")
    L.append("按题（成功对数 / 3 rep；error = POST 折叠失败）：\n")
    items = sorted({r["item"] for d in dirs for r in _trace_rows(d)})
    L.append("| 目录 | " + " | ".join(i[3:] for i in items) + " |")
    L.append("|---|" + "---|" * len(items))
    for d in dirs:
        rows = _trace_rows(d)
        cells = []
        for it in items:
            ok = len({int(r["rep"]) for r in rows if r["item"] == it and "error" not in r})
            er = sum(1 for r in rows if r["item"] == it and "error" in r)
            cells.append(f"{ok}" + (f"（err {er}）" if er else ""))
        L.append(f"| {d.name} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("summarize_trace.report（原样，seed 0；重复功 = repeated + blocked）：\n")
    L.append(ST.report(dirs))
    L.append("")
    L.append("POST 折叠失败取证（`benchmarks/summary_trace/fold_forensics.py`；摘要器原始输出不落盘，下表是间接证据）：\n")
    L.append(FF.render(dirs))
    return L


def write_csv_b2(path: Path, groups, aggs, extra: Dict[Tuple[str, int], Dict[str, Any]]) -> None:
    import csv
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=A.CSV_COLS + B2_EXTRA_COLS)
        w.writeheader()
        for g in groups:
            for lv, a in aggs[g["name"]].items():
                row = {k: a.get(k) for k in A.CSV_COLS if k in a}
                row.update({"group": g["name"], "model": A.short_model(g), "sampling": A.sampling_label(g),
                            "mtp_by_dirname": g["mtp"], "level": lv,
                            "fail_primary": json.dumps(a["fail_primary"], ensure_ascii=False, sort_keys=True),
                            "incoherent": json.dumps(a["incoherent"], ensure_ascii=False, sort_keys=True)})
                row.update(extra.get((g["name"], lv), {}))
                if isinstance(row.get("pass2_k"), float):
                    row["pass2_k"] = A.fmt_k(row["pass2_k"])
                for k, v in row.items():
                    if isinstance(v, float):
                        row[k] = round(v, 4)
                w.writerow(row)


def b2_csv_extra(G, evs, names) -> Dict[Tuple[str, int], Dict[str, Any]]:
    out: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for name in names:
        ev = evs.get(name)
        for lv, rs in by_level(G[name]["records"]).items():
            st = [stub_row(r) for r in rs]
            pb = [pushback_row(r) for r in rs]
            w = [w1b_row(r, ev) for r in rs] if ev is not None else []
            has = bool(st) and all(x["has"] for x in st)
            sf = [spill_files(r) for r in rs]
            out[(name, lv)] = {
                "stub_served_total": sum(x["served_n"] for x in st) if has else None,
                "stub_unknown_total": sum(x["unknown"] or 0 for x in st) if has else None,
                "feed_unknown_tool": sum(x["feed_unknown"] for x in st),
                "stub_failed_total": sum(x["failed"] or 0 for x in st) if has else None,
                "tool_calls_total": sum(x["calls"] for x in st),
                "system_errors_total": sum(r.get("system_errors") or 0 for r in rs),
                "delivery_blocked": sum(x["delivery_blocked"] for x in pb),
                "retrieval_incomplete_runs": sum(x["retrieval_incomplete"] for x in pb),
                "iter_exhausted_runs": sum(x["iter_exhausted"] for x in pb),
                "context_guard_aborts": sum(x["context_guard"] for x in pb),
                "ceiling_folds": sum(x["folds"] for x in w) if w else None,
                "ceiling_compacted": sum(x["compacted"] for x in w) if w else None,
                "ceiling_spilled": sum(x["spilled"] for x in w) if w else None,
                "sent_over_ceiling_gens": sum(x["over_ceiling"] for x in w) if w else None,
                "spill_files": None if (not sf or any(x is None for x in sf)) else sum(sf),
                "spill_readbacks": sum(r.get("spill_readbacks") or 0 for r in rs) if ev is not None else None,
                "missing_arg_recovered": sum(missing_arg_recovered(r) for r in rs),
                "schema_fed": _sum_or_none(schema_fed(r, ev) for r in rs),
                "w2_readback_exempt": _sum_or_none(w2_readback_exempt(r, ev) for r in rs),
            }
    return out


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="11-02 Colab 八臂复测分析（analyze.py A–F + G–N）")
    ap.add_argument("--data", required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--out-md", default=None)
    ap.add_argument("--out-csv", default=None)
    ap.add_argument("--thinking-k", type=int, default=2)
    ap.add_argument("--prev", default=None,
                    help="第二批布局专用：上一批（11-02 上午）目录，其编排器三臂与直调 mock/catalog 以 @am 后缀并入对照")
    ap.add_argument("--extra-ref", action="append", default=[],
                    help="第二批布局专用：附加参照组目录（可重复，如 10-31 的 gemma-current / gemma31b-w4a16-card）")
    a = ap.parse_args(argv)
    data, ref = Path(a.data), Path(a.ref)
    if b2_layout(data):
        return main_b2(a, data, ref)
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


def main_b2(a, data: Path, ref: Path) -> int:
    """第二批布局：编排器四臂（none / w1 / w1w2 / catalog；目录下其余 orch-* 臂按名追加）+ Gemma 直调两臂 + trace 三步；
    A–F 原样 analyze.render（自动对照关掉、只用下面 pair 指定的），然后 I / J / K / M / N（与 11-02 批同一套，
    编排器表并入 catalog 臂）与 O–T（本批新增）。"""
    global REF, ARMS, EXTRA_ORCH
    ref_key = os.path.basename(os.path.normpath(a.ref))
    orch = orch_arms(data)                 # B2_ORCH 四臂 + 本目录其余 orch-* 臂（按名）
    arms = [n for n in orch + GEMMA if (data / n / "transcripts.jsonl").is_file()]
    missing = [n for n in B2_ORCH + GEMMA if n not in arms]
    if missing:
        raise SystemExit(f"❌ 第二批布局缺臂：{missing}")
    prev = Path(a.prev) if a.prev else None
    prev_arm_names = TURN + ["direct-mock", "direct-catalog"]
    prev_arms = [n for n in prev_arm_names if prev and (prev / n / "transcripts.jsonl").is_file()]
    if a.prev and not prev_arms:
        print(f"❌ --prev {a.prev} 下没找到任何预期臂目录（{prev_arm_names}）；路径不存在或没有 transcripts.jsonl",
              file=sys.stderr)
        raise SystemExit(2)
    extra = [Path(x) for x in a.extra_ref]
    names_taken = set(arms) | {n + PREV_TAG for n in prev_arms}
    for k in [ref_key] + [x.name for x in extra]:
        if k in names_taken:
            print(f"❌ 参照目录名「{k}」与臂名/已检查过的参照目录名冲突", file=sys.stderr)
            raise SystemExit(2)
        names_taken.add(k)
    global RAG_NOTE
    saved = (REF, ARMS, EXTRA_ORCH, A.comparisons, RAG_NOTE)
    # 编排器表并入的臂 = catalog + orch_arms 追加的其余 orch-turn-* 臂
    REF, ARMS, EXTRA_ORCH = ref_key, arms, ["orch-turn-catalog"] + orch[len(B2_ORCH):]
    RAG_NOTE = "rag_search 在本批是 W-K2 可用桩（回生产空结果形态）"
    try:
        args = Namespace(metric="pass_hat_2", drop=0.20, recover=0.10, baseline_levels=[0, 4096], high_from=16384,
                         max_model_len=65536, gen_reserve=12288, prod_prefix=None, pair=[])
        A.KEEP = "latest"
        A.th_global = dict(COHERENT_THRESHOLDS)
        A.wall_global = {x for x in A.DEFAULT_WALL_INVALID.split(",") if x}
        A.comparisons = lambda groups: []      # 本批组多、跨模型自动配对会爆表：只用 pair 指定的
        dirs = [ref] + [data / n for n in arms]
        groups = [A.load_group(d) for d in dirs]
        for d in extra:
            groups.append(A.load_group(d))
        for n in prev_arms:
            g = A.load_group(prev / n)
            g["name"] = n + PREV_TAG
            groups.append(g)
        G = {g["name"]: g for g in groups}
        raws = {d.name: raw_rows(d) for d in dirs}
        raws.update({n + PREV_TAG: raw_rows(prev / n) for n in prev_arms})
        evs = {d.name: load_events(d) for d in dirs}
        evs.update({n + PREV_TAG: load_events(prev / n) for n in prev_arms})
        _attach_sessions(G, data, orch)
        pb = sorted({r.get("prefix_base_tokens") for r in G["orch-turn-none"]["records"]} - {None})
        args.prod_prefix = max(pb) if pb else None
        warn = None if len(pb) == 1 else f"orch-turn-none 的 prefix_base_tokens 不唯一：{pb or '全缺'}，取较大值"
        P = PREV_TAG
        args.pair = [f"orch-turn-none{P}:orch-turn-none", f"orch-turn-w1{P}:orch-turn-w1", f"orch-turn-w1w2{P}:orch-turn-w1w2",
                     "orch-turn-none:orch-turn-w1", "orch-turn-none:orch-turn-w1w2", "orch-turn-w1:orch-turn-w1w2",
                     "orch-turn-w1w2:orch-turn-catalog", f"direct-mock{P}:gemma-direct-mock",
                     f"direct-catalog{P}:gemma-direct-catalog", "gemma-direct-mock:gemma-direct-catalog"]
        args.pair += [f"{x.name}:gemma-direct-mock" for x in extra]
        aggs = {g["name"]: A.aggregate(g, A.th_global, A.wall_global) for g in groups}
        parts = [A.render(groups, aggs, args), ""]
        parts += sec_i(G, aggs, raws, evs, args)
        parts += sec_j(G, aggs, raws, evs)
        parts += sec_k(G, evs)
        parts += sec_m(G, aggs, raws)
        parts += sec_n(G, raws, evs, data, logs=("run_colab.lease1.log", "run_colab.log"))
        orch_all = orch + [n + P for n in TURN if n + P in G]
        parts += sec_o(G, aggs, orch_all)
        parts += sec_p(G, aggs, evs, orch, [n + P for n in TURN if n + P in G])
        parts += sec_q(G, aggs, evs)
        gem_names = GEMMA + [f"direct-mock{P}", f"direct-catalog{P}"] + [x.name for x in extra] + [REF]
        gem_pairs = [(x.name, "gemma-direct-mock") for x in extra] + [
            (f"direct-mock{P}", "gemma-direct-mock"), (f"direct-catalog{P}", "gemma-direct-catalog"),
            ("gemma-direct-mock", "gemma-direct-catalog")]
        parts += sec_r(G, aggs, gem_names, gem_pairs)
        parts += sec_t(data)
        auto = "\n".join(parts).rstrip() + "\n"
        header_marker = "_自动生成：`benchmarks/context_knee/analyze.py`；"
        if header_marker not in auto:
            raise SystemExit(f"❌ analyze.render 输出里找不到预期的『{header_marker}』")
        auto = auto.replace(header_marker,
                            "_自动生成：`benchmarks/context_knee/analyze_orch.py` 第二批布局（A–F 节 = analyze.render 原样，"
                            "自动对照关闭、E 节只列指定对；F 节生产前缀取 orch-turn-none 行内 prefix_base_tokens "
                            f"{args.prod_prefix}" + (f"；⚠️ {warn}" if warn else "") + f"；`{P}` = --prev 批同名臂）；", 1)
        engine_note = "（engine.py:140 注释的 M5 实测）"
        if engine_note in auto:
            auto = auto.replace(engine_note, "（取 orch-turn-none 行内字段 prefix_base_tokens，见上）", 1)
        rerun = ("_复跑：`PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/context_knee/analyze_orch.py "
                 f"--data {a.data} --ref {a.ref}" + (f" --prev {a.prev}" if a.prev else "")
                 + "".join(f" --extra-ref {x}" for x in a.extra_ref)
                 + (f" --out-md {a.out_md}" if a.out_md else "") + (f" --out-csv {a.out_csv}" if a.out_csv else "") + "`_\n\n")
        auto = rerun + auto
        if a.out_csv:
            write_csv_b2(Path(a.out_csv), groups, aggs, b2_csv_extra(G, evs, orch_all))
        if a.out_md:
            A.write_md(Path(a.out_md), auto)
        else:
            print(auto)
        return 0
    finally:
        REF, ARMS, EXTRA_ORCH, A.comparisons, RAG_NOTE = saved


if __name__ == "__main__":
    sys.exit(main())
