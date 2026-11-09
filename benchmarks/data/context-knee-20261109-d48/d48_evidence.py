"""D48 复测批（context-knee-20261109-d48）证据：输入去重 / 冒烟漂移 / L0 口径锚 / §2 三条 / 零分布门槛 / fold 机制 / 成本 / 口径。

  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/data/context-knee-20261109-d48/d48_evidence.py \
      [--data benchmarks/data/context-knee-20261109-d48] [--w12 benchmarks/data/context-knee-20261108-w12eval] \
      [--p2b benchmarks/data/context-knee-20261108-p2b] [--out PATH]（--data/--w12/--p2b 缺省即上列路径；
      --out 缺省 None 即打印到 stdout，复现本批存档 `d48_evidence.md` 需显式传
      --out benchmarks/data/context-knee-20261109-d48/d48_evidence.md）

臂：orch-turn-none（无管理）对 orch-turn-w1w2-rev（guard on + step on + hard 51,200 + fold_mode=minimal，prefix prod）；
档位 L0（2 rep）/ 49152 / 57344（6 rep）；KNEE_SEED 1107。对照：w12eval 批 none / w1w2（生产缺省 hard 39,322、fold full），
p2b 批 none + full L0（委托门槛 23 / 27 的来源，断言复现）。
只读：三批 orch-turn-*/transcripts.jsonl、guardrail_events.jsonl、summary.json。CPU、标准库 + backend/framework/core/context_guard
（ABORT_REASON 常量）+ benchmarks/context_knee 的 analyze / analyze_orch / analyze_p2 / run_knee.harness_mode_of；
bootstrap 种子 A.BOOT_SEED → 输出确定。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT / "backend", ROOT / "benchmarks"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))
from context_knee import analyze as A  # noqa: E402
from context_knee import analyze_orch as O  # noqa: E402
from context_knee import analyze_p2 as P2  # noqa: E402
from context_knee import run_knee as RK  # noqa: E402
from context_knee.quality import COHERENT_THRESHOLDS  # noqa: E402
from framework.core import context_guard as CG  # noqa: E402

NONE, REV, W12, FULL = "orch-turn-none", "orch-turn-w1w2-rev", "orch-turn-w1w2", "orch-turn-full"
ARMS = (NONE, REV)
FILL = (49152, 57344)
LEVELS = (0,) + FILL
CEIL, HARD, SOFT, MML = 53248, 51200, 24576, 65536      # ceiling = 65,536 − reserve 12,288；hard = ceiling − 2,048（D48）
SEED = 1107
DROP = 2000          # e3：相邻生成 prompt 下降超过此值记为一次落刀
P2B_THR = {("success", 3): 23, ("pass_hat_2", 3): 27, ("success", 2): 30}   # 委托 / w12eval §2 给出的 p2b none+full L0 门槛：断言复现
W12_57K_FOLD_MED = 27158                                                       # w12eval §1.10：w1w2 57K pre-fold 首生成中位：断言复现
# backend/framework/core/orchestrator.py 全部 events.emit("turn_abort", ..., reason=...) 取值（grep -n 'reason=' 核对，2026-11-09）：
# context_guard.ABORT_REASON（ceiling）、max_sub_generations、repetition_guard、runaway_call、parse_loop、max_iterations。
ABORT_REASONS_KNOWN = {CG.ABORT_REASON, "max_sub_generations", "repetition_guard", "runaway_call", "parse_loop", "max_iterations"}
GUARD_EV = ("context_soft", "context_hard", "context_guard_fold", "step_compaction", "step_readback_exempt", "turn_abort")
CFG_KEYS = ("model", "revision", "dialect", "backend", "tasks", "seed", "token_counter", "sampling_overrides", "entry_sampling",
            "thinking_replay_mode", "thinking_replay_k", "thinking_replay_source", "context_limit", "prefix_mode", "prefix_decl",
            "prefix_base_tokens", "prefix_n_tools", "mode", "fill_placement", "guard", "step_compaction", "turn_compaction",
            "goal_verify", "fill_shape", "guard_thresholds", "step_compaction_cfg", "agent_cfg_fingerprint",
            "orchestrator_cfg_source", "tool_schema_mode_effective", "stub_impl", "stub_clue", "mock_merge_rule",
            "verify_gate_rule", "sampling_seed_sent", "max_total_iterations", "context_guard_bound")


def rows(p: Path):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def pct(x):
    return "—" if x is None else f"{100 * x:.1f}%"


def pts(x):
    if x is None:
        return "—"
    s = f"{100 * x:+.1f}"
    return "0.0" if s in ("-0.0", "+0.0") else s


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def rng_s(xs):
    xs = [x for x in xs if x is not None]
    return f"{med(xs):,.0f}（{min(xs):,}–{max(xs):,}）" if xs else "—"


def dist(c: Counter) -> str:
    return "{" + ", ".join(f"{k}:{v}" for k, v in sorted(c.items(), key=lambda kv: (kv[0] is None, kv[0] if kv[0] is not None else 0))) + "}"


def lvl(r) -> int:
    return int(r["level"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "benchmarks/data/context-knee-20261109-d48"))
    ap.add_argument("--w12", default=str(ROOT / "benchmarks/data/context-knee-20261108-w12eval"))
    ap.add_argument("--p2b", default=str(ROOT / "benchmarks/data/context-knee-20261108-p2b"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    N, W, PB = Path(a.data), Path(a.w12), Path(a.p2b)
    L = [f"# D48 复测批证据（`{Path(__file__).name}` 生成；data = `{N.name}`，对照 w12eval = `{W.name}`、p2b = `{PB.name}`）\n",
         f"常量：ceiling {CEIL:,}（= max_model_len {MML:,} − reserve 12,288）、rev 臂 hard {HARD:,}、soft {SOFT:,}；"
         f"bootstrap {A.BOOT_N} 次、种子 {A.BOOT_SEED}，只按题重抽（P2.paired_item 同法）。\n"]

    # ============================================================ 载入
    raw = {arm: rows(N / arm / "transcripts.jsonl") for arm in ARMS}
    G = {arm: A.load_group(N / arm) for arm in ARMS}
    evs = {arm: O.load_events(N / arm) for arm in ARMS}
    Gw = {arm: A.load_group(W / arm) for arm in (NONE, W12)}
    evw = {arm: O.load_events(W / arm) for arm in (NONE, W12)}
    th, wi = dict(COHERENT_THRESHOLDS), {x for x in A.DEFAULT_WALL_INVALID.split(",") if x}
    aggs = {arm: A.aggregate(G[arm], th, wi) for arm in ARMS}
    R = {arm: {(lvl(r), r["item_id"], int(r["rep"])): r for r in G[arm]["records"]} for arm in ARMS}

    # ============================================================ a. 输入与去重
    L.append("## a. 输入与去重\n")
    L.append(f"- DONE：`{(N / 'DONE').read_text(encoding='utf-8').strip()}`；arms_status：`{' / '.join((N / 'arms_status.txt').read_text(encoding='utf-8').split(chr(10))).strip(' /')}`")
    L.append("| 臂 | 原始行 | A.load_group 有效 | missing(error) | 丢弃重复 | 顶层 error 键行 | 含 `\"error\"` 字样行 | 按档 run 数 | 按档 rep 集合 | sessions 目录 | seed≠1107×1000+rep 或未发送 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for arm in ARMS:
        lines = (N / arm / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()
        cnt = Counter(lvl(r) for r in G[arm]["records"])
        reps = {lv: sorted({int(r["rep"]) for r in G[arm]["records"] if lvl(r) == lv}) for lv in LEVELS}
        nsess = sum(1 for d in (N / arm / "sessions").iterdir() if d.is_dir())
        bad = sum(1 for r in raw[arm] if r.get("sampling_seed") != SEED * 1000 + int(r["rep"]) or not r.get("sampling_seed_sent"))
        L.append(f"| {arm} | {len(raw[arm])} | {len(G[arm]['records'])} | {len(G[arm]['missing'])} | {len(G[arm].get('dropped') or [])} | "
                 f"{sum(1 for r in raw[arm] if 'error' in r)} | {sum(1 for x in lines if '\"error\"' in x)} | {dict(sorted(cnt.items()))} | "
                 f"{ {lv: f'{v[0]}–{v[-1]}' if v else '—' for lv, v in reps.items()} } | {nsess} | {bad} |")
    keys = sorted(set(R[NONE]) & set(R[REV]))
    same_fp = sum(1 for k in keys if P2.fill_fp(R[NONE][k]) == P2.fill_fp(R[REV][k]))
    same_task = sum(1 for k in keys if R[NONE][k].get("task_fp") == R[REV][k].get("task_fp"))
    L.append(f"\n- none vs rev 同 (level,item,rep) 配对 {len(keys)}：fill.fingerprint 相同 {same_fp}、task_fp 相同 {same_task}")
    for arm in ARMS:
        bad = tot = 0
        for lv in FILL:
            for it in sorted({k[1] for k in R[arm] if k[0] == lv}):
                rs = [R[arm][k] for k in sorted(R[arm]) if k[0] == lv and k[1] == it]
                tot += 1
                bad += len({P2.fill_fp(r) for r in rs}) != len(rs)
        L.append(f"- {arm} 填充档 (level,item) {tot} 组：rep 间 fill.fingerprint 不全互异的组 {bad}")
    # 含 "error" 字样的行：type=error 的工具调用（不是 run 级错误）
    for arm in ARMS:
        for ln, line in enumerate((N / arm / "transcripts.jsonl").read_text(encoding="utf-8").splitlines(), 1):
            if '"error"' not in line:
                continue
            r = json.loads(line)
            calls = [f"turns[{ti}].tool_calls[{ci}]" for ti, t in enumerate(r.get("turns") or [])
                     for ci, c in enumerate(t.get("tool_calls") or []) if c.get("type") == "error"]
            L.append(f"  - {arm} 第 {ln} 行 L{r['level']} {r['item_id']} r{r['rep']}：顶层 error 键 {'error' in r}；type=error 工具调用 {calls or '无'}；"
                     f"score.pass {r['score']['pass']}；system_errors {r.get('system_errors')}")
    L.append("")

    # 冒烟漂移
    sw = rows(next(W.glob("smoke-orch-*/transcripts.jsonl")))[0]
    sd = rows(next(N.glob("smoke-orch-*/transcripts.jsonl")))[0]
    skip = {"turns", "gens", "turn_response", "stamp", "wall_s", "session_dir", "score", "final_text_quality", "turn_metrics",
            "peak_prompt_tokens", "peak_prompt_measured", "env_state", "guard_events", "guard_events_lines", "meta_calls",
            "full_renders", "audit_gens", "stub_served", "tool_effects", "dup_blocked", "turn_status", "aborted_reason",
            "repetition_events", "spill_readbacks", "system_errors", "audit_generations", "stub_unknown", "stub_failed"}
    cfgdiff = [k for k in sorted(set(sw) | set(sd)) if k not in skip and sw.get(k) != sd.get(k)]
    fd = P2.first_divergent_turn(sw, sd)
    L.append(f"### a2. 冒烟漂移（w12eval `{next(W.glob('smoke-orch-*')).name}` vs 本批 `{next(N.glob('smoke-orch-*')).name}`）\n")
    L.append(f"- 输入 L{sw['level']} {sw['item_id']} r{sw['rep']}，seed {sw['sampling_seed']} / {sd['sampling_seed']}；非输出字段差：{cfgdiff or '无'}")
    L.append(f"- output_sig 相同 {P2.output_sig(sw) == P2.output_sig(sd)}；first_divergent_turn = {fd}；轮数 {len(sw['turns'])} / {len(sd['turns'])}；"
             f"pass {sw['score']['pass']} / {sd['score']['pass']}")
    L.append(f"- gens prompt_tokens：{[g['prompt_tokens'] for g in sw['gens']]} / {[g['prompt_tokens'] for g in sd['gens']]}")
    if fd is not None and fd < min(len(sw["turns"]), len(sd["turns"])):
        for f in ("thoughts", "text", "tool_calls"):
            x, y = json.dumps(sw["turns"][fd][f], ensure_ascii=False), json.dumps(sd["turns"][fd][f], ensure_ascii=False)
            if x != y:
                i = next((j for j, (c, d) in enumerate(zip(x, y)) if c != d), min(len(x), len(y)))
                L.append(f"- turn {fd}.{f}：长度 {len(x)} / {len(y)}，首个不同字符位置 {i}")
    k0 = (sd["level"], sd["item_id"], sd["rep"])
    for arm in ARMS:
        r = R[arm].get((int(k0[0]), k0[1], int(k0[2])))
        if r is not None:
            L.append(f"- 本批 {arm} 同键行 vs 本批冒烟（同 VM）：output_sig 相同 {P2.output_sig(r) == P2.output_sig(sd)}；"
                     f"first_divergent_turn = {P2.first_divergent_turn(r, sd)}")
    L.append("")

    # ============================================================ b. L0 口径锚
    L.append("## b. L0 口径锚\n")
    a_w = A.aggregate(Gw[NONE], th, wi)
    sub01 = P2.pt_items(a_w[0]["_items"], (0, 1))
    two = [x for x in sub01.values() if len(x) == 2]
    L.append("| 组 | L0 rep | 成功率 k/n | Pass^2 |")
    L.append("|---|---|---|---|")
    for lab, ag, gg in (("本批 none", aggs[NONE], G[NONE]), ("本批 rev", aggs[REV], G[REV]), ("w12eval none（6 rep）", a_w, Gw[NONE])):
        x = ag[0]
        L.append(f"| {lab} | {len({int(r['rep']) for r in gg['records'] if lvl(r) == 0})} | "
                 f"{x['pass_k']}/{x['n']} = {pct(x['success_rate'])} | {A.fmt_k(x['pass2_k'])}/{x['n_items2']} = {pct(x['pass_hat_2'])} |")
    L.append(f"| w12eval none 只取 rep 0–1 | 2 | {sum(sum(v) for v in sub01.values())}/{sum(len(v) for v in sub01.values())} | "
             f"{A.fmt_k(A._ksum([A.pass_hat_2_of(v) for v in two]))}/{len(two)} |")
    L.append("")
    # none vs rev L0 逐字
    k0s = [k for k in keys if k[0] == 0]
    same0 = sum(1 for k in k0s if P2.output_sig(R[NONE][k]) == P2.output_sig(R[REV][k]))
    fdt0 = Counter(P2.first_divergent_turn(R[NONE][k], R[REV][k]) for k in k0s)
    pin0 = sum(1 for k in k0s if [g["prompt_tokens"] for g in R[NONE][k]["gens"]] == [g["prompt_tokens"] for g in R[REV][k]["gens"]])
    L.append(f"- 本批 none vs rev L0 同 (item,rep)：输出逐字相同 {same0}/{len(k0s)}；全部 gens prompt_tokens 序列相同 {pin0}/{len(k0s)}；"
             f"first_divergent_turn 分布 {dist(fdt0)}（None = 全程相同）")
    for arm in ARMS:
        c = Counter(e["type"] for r in G[arm]["records"] if lvl(r) == 0 for e in O.seg(r, evs[arm]))
        L.append(f"- {arm} L0 护栏事件：" + "、".join(f"{t} {c.get(t, 0)}" for t in GUARD_EV) + f"；其余类型 {dict(sorted((k, v) for k, v in c.items() if k not in GUARD_EV))}")
    # 同 VM 共同随机数：49K 两臂首生成输入相同（rev 首生成只记 soft 事件）
    for lv in FILL:
        kk = [k for k in keys if k[0] == lv]
        c = Counter()
        fdt = Counter()
        for k in kk:
            x, y = R[NONE][k], R[REV][k]
            same_in = x["gens"][0]["prompt_tokens"] == y["gens"][0]["prompt_tokens"] and P2.fill_fp(x) == P2.fill_fp(y)
            t0 = all(x["turns"][0].get(f) == y["turns"][0].get(f) for f in ("thoughts", "text", "tool_calls")) if x["turns"] and y["turns"] else False
            c[(same_in, t0)] += 1
            fdt[P2.first_divergent_turn(x, y)] += 1
        L.append(f"- L{lv} none vs rev（同 VM）：(首生成输入相同, 首轮输出逐字相同) → {dict(sorted(c.items()))}；first_divergent_turn {dist(fdt)}")
    L.append("")

    # ============================================================ c/d. §2 三条 + 零分布
    L.append(f"## c. §2 三条（none → rev，b − a；按题配对、只按题重抽 bootstrap 95%，种子 {A.BOOT_SEED}）\n")
    kn = A.knee(aggs[NONE], "pass_hat_2", [0, 4096], 0.20, 0.10)
    L.append(f"- 本批 none 拐点（Pass^2 规则）：{kn['verdict']}（L0 只 2 rep）；w12eval none 6 rep 定的拐点是 32768。两种口径下 49K / 57K 都在拐点以上。")
    # 零分布
    nulls = {}
    gp = {arm: A.load_group(PB / arm) for arm in (NONE, FULL)}
    for metric in ("success", "pass_hat_2"):
        for m in (2, 3):
            pool = []
            for arm in ARMS:
                pool += P2.split_null(P2.item_matrix(P2.keyed(G[arm]["records"], 0)), m, metric)
            nulls[("本批 L0 none+rev（各 2 rep）", metric, m)] = sorted(pool)
            both = []
            for lv in FILL:
                pool = []
                for arm in ARMS:
                    pool += P2.split_null(P2.item_matrix(P2.keyed(G[arm]["records"], lv)), m, metric)
                nulls[(f"本批 L{lv} 臂内 none+rev", metric, m)] = sorted(pool)
                both += pool
            nulls[("本批 49K+57K 臂内合并", metric, m)] = sorted(both)
            nulls[("w12eval none L0（6 rep）", metric, m)] = P2.split_null(P2.item_matrix(P2.keyed(Gw[NONE]["records"], 0)), m, metric)
            pool = []
            for arm in (NONE, FULL):
                pool += P2.split_null(P2.item_matrix(P2.keyed(gp[arm]["records"], 0)), m, metric)
            nulls[("p2b none+full L0（委托门槛来源）", metric, m)] = sorted(pool)
    srcs = ["本批 L0 none+rev（各 2 rep）", "本批 L49152 臂内 none+rev", "本批 L57344 臂内 none+rev", "本批 49K+57K 臂内合并",
            "w12eval none L0（6 rep）", "p2b none+full L0（委托门槛来源）"]
    thr = {}
    L.append("\n### d. 零分布门槛（V2c 同法：同一臂内取两组不相交的 m 个 rep 当两条假臂，枚举全部无序拆分得 |Δ|；"
             "门槛 t = P(|Δ|≥t) ≤ 5% 的最小可取值；— = 超出支撑，括号 = 最大值:尾概率）\n")
    L.append("| 零分布 | 拆分数 m=3 | 成功率 m=3 | Pass^2 m=3 | 拆分数 m=2 | 成功率 m=2 | Pass^2 m=2 |")
    L.append("|---|---|---|---|---|---|---|")
    for src in srcs:
        cells = []
        for metric, m in (("success", 3), ("pass_hat_2", 3), ("success", 2), ("pass_hat_2", 2)):
            xs = nulls[(src, metric, m)]
            t, p = P2.null_threshold(xs)
            thr[(src, metric, m)] = t
            if src.startswith("p2b") and (metric, m) in P2B_THR:
                if t is None or round(100 * t) != P2B_THR[(metric, m)]:
                    raise SystemExit(f"p2b 门槛复现失败：{(src, metric, m, t)} ≠ {P2B_THR[(metric, m)]}")
            if not xs:
                cells.append("—（0 拆分）")
            else:
                cells.append(f"≥{100 * t:.0f}（{100 * p:.1f}%）" if t is not None else f"—（{100 * xs[-1]:.0f}:{100 * P2.tail_p(xs, xs[-1]):.1f}%）")
        L.append(f"| {src} | {len(nulls[(src, 'success', 3)])} | {cells[0]} | {cells[1]} | {len(nulls[(src, 'success', 2)])} | {cells[2]} | {cells[3]} |")
    L.append("\n- 本批 L0 各臂只 2 rep：m=2 需 4 个 rep、m=3 需 6 个，本批 L0 拆不出零分布（拆分数 0）。"
             "本批可用的同法零分布只有填充档臂内 6 rep（每臂每档 10 种 m=3 拆分，两臂合并 20 种 / 两档合并 40 种）。"
             "p2b none+full L0 行复现委托 / w12eval 所用 23 / 27（脚本断言）。\n")

    al = {lv: P2.aligned(G[NONE]["records"], G[REV]["records"], lv, (NONE, REV)) for lv in FILL}

    def ratio_ci(sel, metric):
        items = sorted(set.intersection(*[set(al[lv]["items_a"]) & set(al[lv]["items_b"]) for lv in sel]))

        def per(i, side):
            vs = [al[lv]["items_" + side][i] for lv in sel]
            if metric == "success":
                return sum(sum(v) for v in vs), sum(len(v) for v in vs)
            return sum(A.pass_hat_2_of(v) for v in vs), len(vs)
        ua, ub = {i: per(i, "a") for i in items}, {i: per(i, "b") for i in items}

        def r(ids):
            na, da = sum(ua[i][0] for i in ids), sum(ua[i][1] for i in ids)
            nb, db = sum(ub[i][0] for i in ids), sum(ub[i][1] for i in ids)
            return None if na == 0 else (nb / db) / (na / da)
        rng = random.Random(A.BOOT_SEED)
        bs = sorted(x for x in (r([rng.choice(items) for _ in items]) for _b in range(A.BOOT_N)) if x is not None)
        return r(items), bs[int(0.025 * len(bs))], bs[int(0.975 * len(bs)) - 1], A.BOOT_N - len(bs)

    L.append("### c1. 第一条：成功率 / Pass^2 ≥ 无管理的 90%（§2 原文两项都是 90%）\n")
    L.append("| level | 成功率 none → rev | 差 [CI]（题升/降） | 比 [CI] | 尾概率 本档臂内 m=3 / 两档合并 m=3 / p2b m=3 | "
             "Pass^2 none → rev | 差 [CI]（题升/降） | 比 [CI] | 尾概率 本档臂内 m=3 / 两档合并 m=3 / p2b m=3 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for lab, sel in [(str(lv), [lv]) for lv in FILL] + [("49K+57K 合并", list(FILL))]:
        if len(sel) == 1:
            lv = sel[0]
            ia, ib = al[lv]["items_a"], al[lv]["items_b"]
            units = {i: ([ia[i]], [ib[i]]) for i in ia if i in ib}
            ps, ph = P2.paired_item(units, "success_rate"), P2.paired_item(units, "pass_hat_2")
            ra = (P2.items_rate(ia), P2.items_rate(ib))
            rb = (P2.items_ph2(ia), P2.items_ph2(ib))
        else:
            ps, ph, _ = P2.pooled_aligned(al, sel)
            ra = tuple(sum(sum(sum(v) for v in al[lv]["items_" + s].values()) for lv in sel)
                       / sum(sum(len(v) for v in al[lv]["items_" + s].values()) for lv in sel) for s in "ab")
            rb = tuple(statistics.mean(P2.items_ph2(al[lv]["items_" + s]) for lv in sel) for s in "ab")
        cells = []
        for p, (x, y), metric in ((ps, ra, "success"), (ph, rb, "pass_hat_2")):
            rr, lo, hi, nn = ratio_ci(sel, metric)
            if len(sel) == 1:
                tp = " / ".join(f"{100 * P2.tail_p(nulls[(src, metric, 3)], p['diff']):.0f}%" for src in
                                (f"本批 L{sel[0]} 臂内 none+rev", "本批 49K+57K 臂内合并", "p2b none+full L0（委托门槛来源）"))
            else:
                tp = "（合并无零分布）"
            cells += [f"{pct(x)} → {pct(y)}", f"{pts(p['diff'])} [{pts(p['lo'])},{pts(p['hi'])}]（{p['up']}/{p['down']}）",
                      f"{rr:.2f} [{lo:.2f},{hi:.2f}]" + (f"（{nn} 次分母 0 丢弃）" if nn else ""), tp]
        L.append(f"| {lab} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("门槛判读（|观察差| 对 m=3 门槛；— = 超出支撑不判）：")
    for lv in FILL:
        ia, ib = al[lv]["items_a"], al[lv]["items_b"]
        units = {i: ([ia[i]], [ib[i]]) for i in ia if i in ib}
        for metric, km in (("success", "success_rate"), ("pass_hat_2", "pass_hat_2")):
            d = P2.paired_item(units, km)["diff"]
            cs = []
            for src in (f"本批 L{lv} 臂内 none+rev", "本批 49K+57K 臂内合并", "p2b none+full L0（委托门槛来源）"):
                t = thr[(src, metric, 3)]
                cs.append(f"{src} t={'—' if t is None else f'{100 * t:.0f}'} → " + ("不判" if t is None else ("超噪声" if abs(d) >= t - P2.EPS else "噪声内")))
            L.append(f"- L{lv} {'成功率' if metric == 'success' else 'Pass^2'} 差 {pts(d)}：" + "；".join(cs))
    L.append("")

    # --- 第二条：防溢出
    L.append(f"### c2. 第二条（新口径）：管理臂每 run 实发峰值 ≤ ceiling {CEIL:,} 且 0 次 turn_abort reason={CG.ABORT_REASON}（ceiling 终止）\n")
    L.append("实发峰值 = max(gens[*].prompt_tokens)（O.sent_peak，引擎回报）。\n")
    L.append(f"| 臂 | level | runs | 实发峰值 中位 / 最大 | ≤ {CEIL:,} 的 run | > {CEIL:,} 的生成次数 | ≥ hard {HARD:,} 的 run | 峰值字段 ≠ 实发峰值 | turn_abort 原因 | reason={CG.ABORT_REASON}（ceiling 终止） |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    seen_reasons = set()
    for arm in ARMS:
        for lv in LEVELS:
            rs = [r for r in G[arm]["records"] if lvl(r) == lv]
            sp = [O.sent_peak(r) for r in rs]
            ab = Counter((e.get("detail") or {}).get("reason") for r in rs for e in O.seg(r, evs[arm]) if e["type"] == "turn_abort")
            seen_reasons |= set(ab)
            over = sum(1 for r in rs for g in r["gens"] if (g.get("prompt_tokens") or 0) > CEIL)
            pf = sum(1 for r in rs if r.get("peak_prompt_tokens") != O.sent_peak(r))
            L.append(f"| {arm} | {lv} | {len(rs)} | {med(sp):,.0f} / {max(sp):,} | {sum(1 for x in sp if x <= CEIL)}/{len(rs)} | {over} | "
                     f"{sum(1 for x in sp if x >= HARD)} | {pf} | {dict(ab) or '—'} | {ab.get(CG.ABORT_REASON, 0)} |")
    if not seen_reasons <= ABORT_REASONS_KNOWN:
        raise SystemExit(f"turn_abort reason 出现未知取值：{seen_reasons - ABORT_REASONS_KNOWN}（已知取值 {ABORT_REASONS_KNOWN}）")
    allrev = [r for r in G[REV]["records"]]
    L.append(f"\n- rev 全臂：实发峰值 ≤ {CEIL:,} 的 run {sum(1 for r in allrev if O.sent_peak(r) <= CEIL)}/{len(allrev)}，最大 {max(O.sent_peak(r) for r in allrev):,}；"
             f"全部事件里 reason={CG.ABORT_REASON} 的 turn_abort {sum(1 for e in evs[REV] if e['type'] == 'turn_abort' and (e.get('detail') or {}).get('reason') == CG.ABORT_REASON)}；"
             f"事件类型全集 {sorted(set(e['type'] for e in evs[REV]))}；turn_abort reason 全集（已核对 ⊆ 已知取值） {sorted(seen_reasons)}")
    alln = [r for r in G[NONE]["records"]]
    L.append(f"- none（对照，不受此条约束）：实发峰值 > {CEIL:,} 的 run {sum(1 for r in alln if O.sent_peak(r) > CEIL)}/{len(alln)}，最大 {max(O.sent_peak(r) for r in alln):,}"
             f"（max_model_len {MML:,}）；error 行 {len(G[NONE]['missing'])}")
    L.append("")

    # --- 第三条：耗时
    cur = P2.arm_minutes((N / "run_colab.log").read_text(encoding="utf-8"))
    L.append("### c3. 第三条：总耗时增量 ≤ +25%（Σwall = 去重行 wall_s 之和；增量 = rev ÷ none − 1）\n")
    L.append("| level | Σwall none min | Σwall rev min | 增量 | wall 中位 none / rev s |")
    L.append("|---|---|---|---|---|")
    tot = {arm: {} for arm in ARMS}
    for lv in LEVELS:
        wn = [r.get("wall_s") or 0 for r in G[NONE]["records"] if lvl(r) == lv]
        wr = [r.get("wall_s") or 0 for r in G[REV]["records"] if lvl(r) == lv]
        tot[NONE][lv], tot[REV][lv] = sum(wn), sum(wr)
        L.append(f"| {lv} | {sum(wn) / 60:.1f} | {sum(wr) / 60:.1f} | {100 * (sum(wr) / sum(wn) - 1):+.1f}% | {med(wn):.1f} / {med(wr):.1f} |")
    for lab, sel in (("49K+57K", FILL), ("三档合计（含 L0）", LEVELS)):
        n_, r_ = sum(tot[NONE][lv] for lv in sel), sum(tot[REV][lv] for lv in sel)
        L.append(f"| {lab} | {n_ / 60:.1f} | {r_ / 60:.1f} | {100 * (r_ / n_ - 1):+.1f}% | — |")
    L.append(f"\nrun_colab.log 臂墙钟：none {cur.get(NONE, float('nan')):.1f} min、rev {cur.get(REV, float('nan')):.1f} min（{100 * (cur[REV] / cur[NONE] - 1):+.1f}%）；"
             f"批首行 → DONE {P2.batch_span((N / 'run_colab.log').read_text(encoding='utf-8'))['minutes']:.1f} min。\n")

    # ============================================================ e. 机制
    L.append("## e. 机制（guardrail_events.jsonl 按行 guard_events_lines 切片；iter1 = detail.iteration == 1 即首次生成前）\n")
    L.append("| 批 / 臂 | level | runs | soft 事件（run） | hard iter1 / 后（run） | fold iter1 / 后（run） | step_compaction（按 detail.level） | step_readback_exempt | 切片 vs session 字段 |")
    L.append("|---|---|---|---|---|---|---|---|---|")

    def trig(GG, ev, arm, lv, lab):
        rs = [r for r in GG[arm]["records"] if lvl(r) == lv]
        c, runs, sc = Counter(), Counter(), Counter()
        h1 = hn = f1 = fn = mism = 0
        for r in rs:
            sg = O.seg(r, ev[arm])
            mism += sum(1 for e in sg if e.get("session") != O.sess_name(r))
            ts = Counter(e["type"] for e in sg)
            c.update(ts)
            runs.update({t: 1 for t in ts})
            for e in sg:
                d = e.get("detail") or {}
                if e["type"] == "context_hard":
                    h1 += d.get("iteration") == 1
                    hn += d.get("iteration") != 1
                if e["type"] == "context_guard_fold":
                    f1 += d.get("iteration") == 1
                    fn += d.get("iteration") != 1
                if e["type"] == "step_compaction":
                    sc[d.get("level")] += 1
        names = {O.sess_name(r) for r in rs}
        by_sess = sum(1 for e in ev[arm] if e.get("session") in names)
        seg_tot = sum(len(O.seg(r, ev[arm])) for r in rs)
        L.append(f"| {lab} | {lv} | {len(rs)} | {c['context_soft']}（{runs['context_soft']}） | {h1} / {hn}（{runs['context_hard']}） | "
                 f"{f1} / {fn}（{runs['context_guard_fold']}） | {c['step_compaction']}（{runs['step_compaction']}）{dict(sorted(sc.items())) if sc else ''} | "
                 f"{c['step_readback_exempt']} | {seg_tot} / {by_sess}，不符 {mism} |")
    for arm in ARMS:
        for lv in LEVELS:
            trig(G, evs, arm, lv, f"d48 {arm.replace('orch-turn-', '')}")
    for lv in FILL:
        trig(Gw, evw, W12, lv, "w12eval w1w2")
    L.append("")
    # fold 细节
    L.append("### e2. rev 臂 fold（context_guard_fold）细节\n")
    L.append("| level | fold 事件 | mode | passes 分布 | 每 run fold 次数分布（含 0） | before | after | gap = before − hard + 1 | 实省 = before − after | 实省 ÷ gap 中位（范围） | after < hard | hard − after | compacted | spilled |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for lv in LEVELS:
        rs = [r for r in G[REV]["records"] if lvl(r) == lv]
        fe = [(r, e["detail"]) for r in rs for e in O.seg(r, evs[REV]) if e["type"] == "context_guard_fold"]
        per = Counter(sum(1 for e in O.seg(r, evs[REV]) if e["type"] == "context_guard_fold") for r in rs)
        if not fe:
            L.append(f"| {lv} | 0 | — | — | {dist(per)} | — | — | — | — | — | — | — | — | — |")
            continue
        ds = [d for _, d in fe]
        gap = [d["before"] - HARD + 1 for d in ds]
        freed = [d["before"] - d["after"] for d in ds]
        rat = sorted(f / g for f, g in zip(freed, gap))
        L.append(f"| {lv} | {len(ds)} | {dict(Counter(d.get('mode') for d in ds))} | {dist(Counter(d.get('passes') for d in ds))} | {dist(per)} | "
                 f"{rng_s([d['before'] for d in ds])} | {rng_s([d['after'] for d in ds])} | {rng_s(gap)} | {rng_s(freed)} | "
                 f"{statistics.median(rat):.1f}（{rat[0]:.1f}–{rat[-1]:.1f}） | {sum(1 for d in ds if d['after'] < HARD)}/{len(ds)} | "
                 f"{rng_s([HARD - d['after'] for d in ds])} | {rng_s([d.get('compacted') for d in ds])} | {rng_s([d.get('spilled') for d in ds])} |")
    L.append("")
    # 首生成 prompt：rev vs w12eval w1w2 vs none
    L.append("首生成 prompt（gens[0].prompt_tokens）中位（范围）与首生成前 fold 的 run：\n")
    L.append("| 组 | level | 首生成前 fold 的 run | 首生成 prompt | fill.prompt_tokens_est 中位 |")
    L.append("|---|---|---|---|---|")
    for lab, GG, ev, arm in (("d48 none", G, evs, NONE), ("d48 rev", G, evs, REV), ("w12eval none", Gw, evw, NONE), ("w12eval w1w2", Gw, evw, W12)):
        for lv in FILL:
            rs = [r for r in GG[arm]["records"] if lvl(r) == lv]
            f1 = sum(1 for r in rs if any(e["type"] == "context_guard_fold" and (e.get("detail") or {}).get("iteration") == 1 for e in O.seg(r, ev[arm])))
            f0 = [r["gens"][0]["prompt_tokens"] for r in rs]
            if lab == "w12eval w1w2" and lv == 57344:
                if round(med(f0)) != W12_57K_FOLD_MED:
                    raise SystemExit(f"w12eval w1w2 57K pre-fold 首生成中位复现失败：{med(f0)} ≠ {W12_57K_FOLD_MED}")
            L.append(f"| {lab} | {lv} | {f1}/{len(rs)} | {rng_s(f0)} | {med([(r.get('fill') or {}).get('prompt_tokens_est') for r in rs]):,.0f} |")
    L.append("")
    # hard 事件位置与二次折
    hd = [e["detail"] for e in evs[REV] if e["type"] == "context_hard"]
    L.append(f"- rev context_hard 事件 {len(hd)}：detail.prompt_tokens 最小 {min(d['prompt_tokens'] for d in hd):,} / 最大 {max(d['prompt_tokens'] for d in hd):,}；"
             f"< {HARD:,} 的 {sum(1 for d in hd if d['prompt_tokens'] < HARD)}；limit 取值 {dict(Counter(d.get('limit') for d in hd))}；"
             f"ceiling 取值 {dict(Counter(d.get('ceiling') for d in hd))}；按档 {dict(Counter(d.get('level') for d in hd))}" if hd else "- rev context_hard 事件 0")
    sf = [e["detail"] for e in evs[REV] if e["type"] == "context_soft"]
    L.append(f"- rev context_soft 事件 {len(sf)}：iteration 分布 {dist(Counter(d.get('iteration') for d in sf))}；prompt_tokens 最小 {min(d['prompt_tokens'] for d in sf):,}")
    two = []
    for r in G[REV]["records"]:
        fl = [e["detail"] for e in O.seg(r, evs[REV]) if e["type"] == "context_guard_fold"]
        if len(fl) >= 2:
            two.append(f"L{r['level']} {r['item_id']} r{r['rep']}（{len(fl)} 次）")
    L.append(f"- rev 一 run 内 ≥ 2 次 fold（折后再涨回 ≥ ceiling 又折）：{len(two)} run{('：' + '；'.join(two)) if two else ''}")
    # 折后再到 ceiling：fold 之后的生成是否有 prompt ≥ ceiling
    back = 0
    for r in G[REV]["records"]:
        fl = [e["detail"].get("iteration") for e in O.seg(r, evs[REV]) if e["type"] == "context_guard_fold"]
        if fl:
            back += any((g.get("prompt_tokens") or 0) >= CEIL for g in r["gens"])
    L.append(f"- rev 有 fold 的 run 里发出过 ≥ ceiling 生成的 run：{back}")
    scd = Counter((lvl(r), e["detail"].get("level"), e["detail"].get("trigger")) for r in G[REV]["records"] for e in O.seg(r, evs[REV]) if e["type"] == "step_compaction")
    L.append(f"- rev step_compaction (档, detail.level, trigger) 计数：{dict(sorted(scd.items(), key=str))}")
    L.append("")

    # e3. 生成间 prompt 大幅下降：fold 之外，W2 step_compaction 落刀也让下一次生成的 prompt 变短
    L.append(f"### e3. 相邻两次生成 prompt 下降 > {DROP:,} tok 的位置（gens[i−1] → gens[i]，i ≥ 1；fold 在首生成前的不在此列）\n")
    L.append("| 批 / 臂 | level | 下降次数（run） | 下降前 prompt | 下降后 prompt | 下降前 < hard 51,200 的次数 | run 内首次下降后的最小 prompt 中位 |")
    L.append("|---|---|---|---|---|---|---|")
    for lab, GG, arm in (("d48 none", G, NONE), ("d48 rev", G, REV), ("w12eval w1w2", Gw, W12)):
        for lv in FILL:
            rs = [r for r in GG[arm]["records"] if lvl(r) == lv]
            pre, post, runs_, mins = [], [], 0, []
            for r in rs:
                pt_ = [g.get("prompt_tokens") or 0 for g in r["gens"]]
                dd = [i for i in range(1, len(pt_)) if pt_[i] < pt_[i - 1] - DROP]
                runs_ += bool(dd)
                pre += [pt_[i - 1] for i in dd]
                post += [pt_[i] for i in dd]
                if dd:
                    mins.append(min(pt_[dd[0]:]))
            L.append(f"| {lab} | {lv} | {len(pre)}（{runs_}） | {rng_s(pre)} | {rng_s(post)} | {sum(1 for x in pre if x < HARD)} | "
                     f"{'—' if not mins else format(med(mins), ',.0f')} |")
    L.append("")
    # reused_prefix 可信度：fold 落刀从最旧观察起改写 prompt，紧接的那次生成若真命中前缀缓存，reused 应 ≈ 前缀 + 未改段
    after = []
    for r in G[REV]["records"]:
        for e in O.seg(r, evs[REV]):
            if e["type"] == "context_guard_fold":
                i = (e["detail"].get("iteration") or 0) - 1
                if 0 < i < len(r["gens"]):
                    g = r["gens"][i]
                    after.append((g.get("reused_prefix") or 0) / (g.get("prompt_tokens") or 1))
    L.append(f"- reused_prefix 可信度：rev 臂 iteration > 1 的 fold 之后紧接的那次生成（gens[iteration−1]，其 prompt_tokens = fold 的 after）"
             f"{len(after)} 次，reused_prefix ÷ prompt_tokens 中位 {med(after):.3f}、最小 {min(after):.3f}。fold 从最旧观察（系统前缀之后第一条）起改写，"
             "真前缀缓存在此处应大面积失配；报 ≥ 0.9 说明远端 reused_prefix 不反映真实命中（推断：与 README「首生成 num_cached_tokens 不可信」同源）。\n"
             if after else "- reused_prefix 可信度：rev 臂无 iteration > 1 的 fold，无从核对\n")

    # ============================================================ f. 成本
    fr_ratio = {}
    for lv in LEVELS:
        gens_lv = sum(len(r["gens"]) for arm in ARMS for r in G[arm]["records"] if lvl(r) == lv)
        fr_lv = sum(r.get("full_renders") or 0 for arm in ARMS for r in G[arm]["records"] if lvl(r) == lv)
        fr_ratio[lv] = (fr_lv, gens_lv)
    fr_txt = "、".join(f"L{lv} {fr}/{gn}={fr / gn:.2f}" for lv, (fr, gn) in fr_ratio.items())
    L.append("## f. 成本（每 run；gens[1:] 计缓存未命中：miss = max(0, prompt_tokens − reused_prefix)；"
             "首生成不计（README：远端首生成 num_cached_tokens 不可信）；「近全量重填」= gens[1:] 中 reused_prefix < 0.5 × prompt_tokens 的生成。"
             "⚠️ e3 末条：fold 后 reused_prefix 仍 ≈ prompt，miss / 近全量重填两列**不能**读作真实前缀缓存失配；full_renders = 编排层全量渲染次数，"
             f"按档 full_renders÷生成（两臂合并，两臂同档取值相同）：{fr_txt}，也不是缓存信号。可用的成本证据只有 wall_s）\n")
    L.append("| 臂 | level | runs | wall 中位 s | Σwall min | 生成次数 Σ | full_renders Σ | miss tok Σ（gens[1:]） | miss tok 中位 / run | 近全量重填 生成数（run） |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for arm in ARMS:
        for lv in LEVELS:
            rs = [r for r in G[arm]["records"] if lvl(r) == lv]
            miss = [sum(max(0, (g.get("prompt_tokens") or 0) - (g.get("reused_prefix") or 0)) for g in r["gens"][1:]) for r in rs]
            cold = [sum(1 for g in r["gens"][1:] if (g.get("reused_prefix") or 0) < 0.5 * (g.get("prompt_tokens") or 0)) for r in rs]
            L.append(f"| {arm} | {lv} | {len(rs)} | {med([r.get('wall_s') for r in rs]):.1f} | {sum(r.get('wall_s') or 0 for r in rs) / 60:.1f} | "
                     f"{sum(len(r['gens']) for r in rs)} | {sum(r.get('full_renders') or 0 for r in rs)} | {sum(miss):,} | {med(miss):,.0f} | "
                     f"{sum(cold)}（{sum(1 for x in cold if x)}） |")
    L.append("")

    # ============================================================ g. 口径核对
    L.append("## g. 口径核对\n")
    sk = ("guard", "guard_thresholds", "step_compaction", "step_compaction_cfg", "agent_cfg_fingerprint", "level_reps", "seed", "prefix_mode",
          "tool_schema_mode_effective", "context_limit")
    for arm in ARMS:
        s = json.loads((N / arm / "summary.json").read_text(encoding="utf-8"))
        L.append(f"- {arm} summary.json：" + "；".join(f"{k}={json.dumps(s.get(k), ensure_ascii=False, sort_keys=True)}" for k in sk))
    for arm in ARMS:
        vals = {k: {json.dumps(r.get(k), sort_keys=True) for r in raw[arm]} for k in CFG_KEYS}
        multi = {k: sorted(v) for k, v in vals.items() if len(v) > 1}
        L.append(f"- {arm} 行内：guard_thresholds 取值 {sorted(vals['guard_thresholds'])}；取值不唯一的配置键 {multi or '无'}")
    diff_keys = [k for k in CFG_KEYS if {json.dumps(r.get(k), sort_keys=True) for r in raw[NONE]} != {json.dumps(r.get(k), sort_keys=True) for r in raw[REV]}]
    L.append(f"- none 与 rev 行内取值不同的配置键：{diff_keys}")
    def hmk(r):   # harness_mode_of 元组含 dict（guard_thresholds 等），按 JSON 串去重
        return json.dumps(RK.harness_mode_of(r), sort_keys=True, ensure_ascii=False)
    hm = {arm: {hmk(r) for r in raw[arm]} for arm in ARMS}
    hw = {arm: {hmk(r) for r in rows(W / arm / "transcripts.jsonl")} for arm in (NONE, W12)}
    L.append(f"- harness_mode_of（run_knee，guard_thresholds 缺 fold_mode 按 full 归一）每臂取值数：d48 none {len(hm[NONE])}、rev {len(hm[REV])}、"
             f"w12eval none {len(hw[NONE])}、w1w2 {len(hw[W12])}")
    if len(hm[NONE]) == 1 and len(hw[NONE]) == 1:
        a_, b_ = json.loads(next(iter(hm[NONE]))), json.loads(next(iter(hw[NONE])))
        L.append(f"- d48 none vs w12eval none harness_mode 不同的键：{[k for k, x, y in zip(RK.HARNESS_MODE_KEYS, a_, b_) if x != y] or '无'}"
                 f"；run_knee.harness_mismatch(w12eval none 行, d48 none 口径) = {RK.harness_mismatch(rows(W / NONE / 'transcripts.jsonl')[0], RK.harness_mode_of(raw[NONE][0]))}")
    if len(hm[REV]) == 1 and len(hw[W12]) == 1:
        a_, b_ = json.loads(next(iter(hm[REV]))), json.loads(next(iter(hw[W12])))
        L.append(f"- d48 rev vs w12eval w1w2 harness_mode 不同的键：" + "；".join(f"{k}: {y} → {x}" for k, x, y in zip(RK.HARNESS_MODE_KEYS, a_, b_) if x != y))
    log = (N / "run_colab.log").read_text(encoding="utf-8")
    bs = P2.batch_span(log)
    L.append(f"- run_colab.log：`START deploy` {bs['deploys']} 次、READY {bs['ready']}、stop rc=0 行 {log.count('stop rc=0')}、"
             f"tailnet_cleanup {[x.split('\"tailnet_cleanup\": ')[1].split('}')[0] for x in log.splitlines() if 'tailnet_cleanup' in x]}；"
             f"余额行（unit）{'有' if 'unit' in log else '无（费用数不能从本目录复算）'}")
    L.append("")
    out = "\n".join(L) + "\n"
    if a.out:
        Path(a.out).write_text(out, encoding="utf-8")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
