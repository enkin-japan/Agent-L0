"""D48b 批（context-knee-20261109-d48b）证据：输入 / 冒烟漂移 / L0 锚 / §2 三条（rev、w1 各对 none）/ 零分布门槛 /
D48b 密度校准 fold / W1–W2 归因 / 时长 / 跨批 / 口径。以 ../context-knee-20261109-d48/d48_evidence.py 为底。

  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/data/context-knee-20261109-d48b/d48b_evidence.py \
      --out benchmarks/data/context-knee-20261109-d48b/d48b_evidence.md
  （--data / --d48 / --p2b 缺省即本批 / d48 批 / p2b 批；--out 缺省打印到 stdout）

臂：orch-turn-none（guard off / step off）、orch-turn-w1w2-rev（guard on + step on + --guard-hard-tokens 51200
--guard-fold-mode minimal）、orch-turn-w1（guard on + step off，命令行无 hard / fold 开关，取 config.yaml 缺省——
7b83b3bb 起即 hard 51,200 + fold minimal，行内 guard_thresholds 核对见 g 节）。档位 L0（2 rep）/ 49152 / 57344（6 rep）；
KNEE_SEED 1107；三臂同一 VM 顺序跑。对照：d48 批 none / rev（不同 VM、同种子；HEAD 061bfb6e 之前的代码，fold 无 density）；
p2b 批 none + full L0（委托门槛 23 / 27 的来源，断言复现）。
只读：三批 orch-turn-*/transcripts.jsonl、guardrail_events.jsonl、summary.json、run_colab.log、smoke-orch-*/transcripts.jsonl；
sessions/ 只数目录。CPU、标准库 + backend/framework/core/context_guard（ABORT_REASON 常量）+ benchmarks/context_knee 的
analyze / analyze_orch / analyze_p2 / run_knee；bootstrap 种子 A.BOOT_SEED → 输出确定。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta
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

NONE, REV, W1, FULL = "orch-turn-none", "orch-turn-w1w2-rev", "orch-turn-w1", "orch-turn-full"
ARMS = (NONE, REV, W1)
MG = (REV, W1)                       # 管理臂
SHORT = {NONE: "none", REV: "rev", W1: "w1"}
FILL = (49152, 57344)
LEVELS = (0,) + FILL
CEIL, HARD, SOFT, MML = 53248, 51200, 24576, 65536
SEED = 1107
DROP = 2000
P2B_THR = {("success", 3): 23, ("pass_hat_2", 3): 27, ("success", 2): 30}                 # p2b none+full L0：断言复现
D48_THR = {(49152, "success"): 23, (49152, "pass_hat_2"): 23, (57344, "success"): 27,      # d48 证据 d 节填充档臂内 m=3
           (57344, "pass_hat_2"): 20, ("合并", "success"): 23, ("合并", "pass_hat_2"): 23}  # none+rev：断言复现
D48_57K_FIRST_MED = 41104            # d48 §1.8：rev 57K 首生成 prompt 中位：断言复现
D48_57K_RATIO = "2.6"                # d48 §1.8：rev 57K 实省 ÷ 缺口中位：断言复现
ABORT_REASONS_KNOWN = {CG.ABORT_REASON, "max_sub_generations", "repetition_guard", "runaway_call", "parse_loop", "max_iterations"}
GUARD_EV = ("context_soft", "context_hard", "context_guard_fold", "step_compaction", "step_readback_exempt", "turn_abort")
CFG_KEYS = ("model", "revision", "dialect", "backend", "tasks", "seed", "token_counter", "sampling_overrides", "entry_sampling",
            "thinking_replay_mode", "thinking_replay_k", "thinking_replay_source", "context_limit", "prefix_mode", "prefix_decl",
            "prefix_base_tokens", "prefix_n_tools", "mode", "fill_placement", "guard", "step_compaction", "turn_compaction",
            "goal_verify", "fill_shape", "guard_thresholds", "step_compaction_cfg", "agent_cfg_fingerprint",
            "orchestrator_cfg_source", "tool_schema_mode_effective", "stub_impl", "stub_clue", "mock_merge_rule",
            "verify_gate_rule", "sampling_seed_sent", "max_total_iterations", "context_guard_bound")
_TS = r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"


def rows(p: Path):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


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


def qs(xs, f="{:.2f}"):
    """中位（Q1–Q3；min–max）；n < 2 时只给中位。"""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return "—"
    if len(xs) < 2:
        return f.format(xs[0])
    q1, _, q3 = statistics.quantiles(xs, n=4, method="inclusive")
    return f"{f.format(statistics.median(xs))}（{f.format(q1)}–{f.format(q3)}；{f.format(xs[0])}–{f.format(xs[-1])}）"


def dist(c: Counter) -> str:
    return "{" + ", ".join(f"{k}:{v}" for k, v in sorted(c.items(), key=lambda kv: (kv[0] is None, kv[0] if kv[0] is not None else 0))) + "}"


def lvl(r) -> int:
    return int(r["level"])


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "benchmarks/data/context-knee-20261109-d48b"))
    ap.add_argument("--d48", default=str(ROOT / "benchmarks/data/context-knee-20261109-d48"))
    ap.add_argument("--p2b", default=str(ROOT / "benchmarks/data/context-knee-20261108-p2b"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    N, D, PB = Path(a.data), Path(a.d48), Path(a.p2b)
    L = [f"# D48b 批证据（`{Path(__file__).name}` 生成；data = `{N.name}`，对照 d48 = `{D.name}`、p2b = `{PB.name}`）\n",
         f"常量：ceiling {CEIL:,}（= max_model_len {MML:,} − reserve 12,288）、hard {HARD:,}、soft {SOFT:,}；"
         f"bootstrap {A.BOOT_N} 次、种子 {A.BOOT_SEED}，只按题重抽（P2.paired_item 同法）。臂简称 none / rev / w1。\n"]

    # ============================================================ 载入
    raw = {arm: rows(N / arm / "transcripts.jsonl") for arm in ARMS}
    G = {arm: A.load_group(N / arm) for arm in ARMS}
    evs = {arm: O.load_events(N / arm) for arm in ARMS}
    Gd = {arm: A.load_group(D / arm) for arm in (NONE, REV)}
    evd = {arm: O.load_events(D / arm) for arm in (NONE, REV)}
    th, wi = dict(COHERENT_THRESHOLDS), {x for x in A.DEFAULT_WALL_INVALID.split(",") if x}
    aggs = {arm: A.aggregate(G[arm], th, wi) for arm in ARMS}
    R = {arm: {(lvl(r), r["item_id"], int(r["rep"])): r for r in G[arm]["records"]} for arm in ARMS}
    # 常量对行内字段核对：管理臂（本批 rev / w1、d48 rev）每行 hard / soft / ceiling / context_limit 必须等于脚本常量
    for lab, rs_ in (("本批 rev", raw[REV]), ("本批 w1", raw[W1]), ("d48 rev", rows(D / REV / "transcripts.jsonl"))):
        for r in rs_:
            gt = r.get("guard_thresholds") or {}
            got = (gt.get("hard"), gt.get("soft"), (r.get("context_limit") or 0) - (gt.get("reserve") or 0), r.get("context_limit"))
            if got != (HARD, SOFT, CEIL, MML):
                raise SystemExit(f"{lab} 行内阈值与常量不符：(hard, soft, ceiling, context_limit) = {got} ≠ {(HARD, SOFT, CEIL, MML)}")
    Rd = {arm: {(lvl(r), r["item_id"], int(r["rep"])): r for r in Gd[arm]["records"]} for arm in (NONE, REV)}

    # ============================================================ a. 输入与去重
    L.append("## a. 输入与去重\n")
    L.append(f"- DONE：`{(N / 'DONE').read_text(encoding='utf-8').strip()}`；arms_status：`{' / '.join((N / 'arms_status.txt').read_text(encoding='utf-8').split(chr(10))).strip(' /')}`")
    L.append(f"- 常量核对：本批 rev / w1 与 d48 rev 每行 guard_thresholds.hard = {HARD:,}、soft = {SOFT:,}、context_limit − reserve = {CEIL:,}、context_limit = {MML:,}（脚本断言）\n")
    L.append("| 臂 | 原始行 | A.load_group 有效 | missing(error) | 丢弃重复 | 顶层 error 键行 | 含 `\"error\"` 字样行 | 按档 run 数 | sessions 目录数 | seed≠1107×1000+rep 或未发送 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for arm in ARMS:
        lines = (N / arm / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()
        cnt = Counter(lvl(r) for r in G[arm]["records"])
        nsess = sum(1 for d in (N / arm / "sessions").iterdir() if d.is_dir())
        bad = sum(1 for r in raw[arm] if r.get("sampling_seed") != SEED * 1000 + int(r["rep"]) or not r.get("sampling_seed_sent"))
        L.append(f"| {arm} | {len(raw[arm])} | {len(G[arm]['records'])} | {len(G[arm]['missing'])} | {len(G[arm].get('dropped') or [])} | "
                 f"{sum(1 for r in raw[arm] if 'error' in r)} | {sum(1 for x in lines if '\"error\"' in x)} | {dict(sorted(cnt.items()))} | {nsess} | {bad} |")
    keys = sorted(set(R[NONE]) & set(R[REV]) & set(R[W1]))
    for x, y in ((NONE, REV), (NONE, W1)):
        sf = sum(1 for k in keys if P2.fill_fp(R[x][k]) == P2.fill_fp(R[y][k]))
        stf = sum(1 for k in keys if R[x][k].get("task_fp") == R[y][k].get("task_fp"))
        L.append(f"- {SHORT[x]} vs {SHORT[y]} 同 (level,item,rep) {len(keys)} 对：fill.fingerprint 相同 {sf}、task_fp 相同 {stf}")
    for arm in (NONE, REV):
        kk = sorted(set(R[arm]) & set(Rd[arm]))
        sf = sum(1 for k in kk if P2.fill_fp(R[arm][k]) == P2.fill_fp(Rd[arm][k]))
        L.append(f"- 跨批 d48 {SHORT[arm]} → 本批 {SHORT[arm]} 同键 {len(kk)} 对：fill.fingerprint 相同 {sf}")
    for arm in ARMS:
        bad = tot = 0
        for lv in FILL:
            for it in sorted({k[1] for k in R[arm] if k[0] == lv}):
                rs = [R[arm][k] for k in sorted(R[arm]) if k[0] == lv and k[1] == it]
                tot += 1
                bad += len({P2.fill_fp(r) for r in rs}) != len(rs)
        L.append(f"- {arm} 填充档 (level,item) {tot} 组：rep 间 fill.fingerprint 不全互异的组 {bad}")
    for arm in ARMS:
        for ln, line in enumerate((N / arm / "transcripts.jsonl").read_text(encoding="utf-8").splitlines(), 1):
            if '"error"' not in line:
                continue
            r = json.loads(line)
            calls = [f"turns[{ti}].tool_calls[{ci}]" for ti, t in enumerate(r.get("turns") or [])
                     for ci, c in enumerate(t.get("tool_calls") or []) if c.get("type") == "error"]
            L.append(f"  - {arm} 第 {ln} 行 L{r['level']} {r['item_id']} r{r['rep']}：顶层 error 键 {'error' in r}；type=error 工具调用 {calls or '无'}；"
                     f"score.pass {r['score']['pass']}")
    L.append("")

    # ============================================================ s. 冒烟漂移探针
    smd = next(D.glob("smoke-orch-*"))
    smn = next(N.glob("smoke-orch-*"))
    sw, sd = rows(smd / "transcripts.jsonl")[0], rows(smn / "transcripts.jsonl")[0]
    skip = {"turns", "gens", "turn_response", "stamp", "wall_s", "session_dir", "score", "final_text_quality", "turn_metrics",
            "peak_prompt_tokens", "peak_prompt_measured", "env_state", "guard_events", "guard_events_lines", "meta_calls",
            "full_renders", "audit_gens", "stub_served", "tool_effects", "dup_blocked", "turn_status", "aborted_reason",
            "repetition_events", "spill_readbacks", "system_errors", "audit_generations", "stub_unknown", "stub_failed"}
    cfgdiff = [k for k in sorted(set(sw) | set(sd)) if k not in skip and sw.get(k) != sd.get(k)]
    fd = P2.first_divergent_turn(sw, sd)
    L.append(f"## s. 冒烟漂移探针（d48 `{smd.name}` vs 本批 `{smn.name}`）\n")
    L.append(f"- 输入 L{sw['level']} {sw['item_id']} r{sw['rep']} / L{sd['level']} {sd['item_id']} r{sd['rep']}，seed {sw['sampling_seed']} / {sd['sampling_seed']}；"
             f"fill.fingerprint 相同 {P2.fill_fp(sw) == P2.fill_fp(sd)}；task_fp 相同 {sw.get('task_fp') == sd.get('task_fp')}")
    for k in cfgdiff:
        L.append(f"  - 非输出字段差 `{k}`：{json.dumps(sw.get(k), ensure_ascii=False, sort_keys=True)} → {json.dumps(sd.get(k), ensure_ascii=False, sort_keys=True)}")
    if not cfgdiff:
        L.append("  - 非输出字段差：无")
    L.append(f"- output_sig（全部轮输出 + 最终答复）逐字相同 {P2.output_sig(sw) == P2.output_sig(sd)}；最终答复 turn_response 相同 {sw.get('turn_response') == sd.get('turn_response')}；"
             f"first_divergent_turn = {fd}；轮数 {len(sw['turns'])} / {len(sd['turns'])}；生成次数 {len(sw['gens'])} / {len(sd['gens'])}；pass {sw['score']['pass']} / {sd['score']['pass']}")
    L.append(f"- gens prompt_tokens：d48 {[g['prompt_tokens'] for g in sw['gens']]} / 本批 {[g['prompt_tokens'] for g in sd['gens']]}")
    if fd is not None and fd < min(len(sw["turns"]), len(sd["turns"])):
        for f in ("thoughts", "text", "tool_calls"):
            x, y = json.dumps(sw["turns"][fd][f], ensure_ascii=False), json.dumps(sd["turns"][fd][f], ensure_ascii=False)
            if x != y:
                i = next((j for j, (c, d) in enumerate(zip(x, y)) if c != d), min(len(x), len(y)))
                L.append(f"- turn {fd}.{f}：长度 {len(x)} / {len(y)}，首个不同字符位置 {i}")
    k0 = (int(sd["level"]), sd["item_id"], int(sd["rep"]))
    for arm in ARMS:
        r = R[arm].get(k0)
        if r is not None:
            L.append(f"- 本批 {SHORT[arm]} 同键行 vs 本批冒烟（同 VM）：output_sig 相同 {P2.output_sig(r) == P2.output_sig(sd)}；first_divergent_turn = {P2.first_divergent_turn(r, sd)}")
    for arm in (NONE, REV):
        r = Rd[arm].get(k0)
        if r is not None:
            L.append(f"- d48 {SHORT[arm]} 同键行 vs 本批冒烟（跨 VM）：output_sig 相同 {P2.output_sig(r) == P2.output_sig(sd)}；first_divergent_turn = {P2.first_divergent_turn(r, sd)}")
    L.append("")

    # ============================================================ b. L0 口径锚 + 同 VM 共同随机数
    L.append("## b. L0 口径锚与同 VM 共同随机数\n")
    L.append("| 组 | L0 rep | 成功率 k/n | Pass^2 |")
    L.append("|---|---|---|---|")
    ad = {arm: A.aggregate(Gd[arm], th, wi) for arm in (NONE, REV)}
    for lab, ag, gg in [(f"本批 {SHORT[x]}", aggs[x], G[x]) for x in ARMS] + [(f"d48 {SHORT[x]}", ad[x], Gd[x]) for x in (NONE, REV)]:
        x = ag[0]
        L.append(f"| {lab} | {len({int(r['rep']) for r in gg['records'] if lvl(r) == 0})} | "
                 f"{x['pass_k']}/{x['n']} = {pct(x['success_rate'])} | {A.fmt_k(x['pass2_k'])}/{x['n_items2']} = {pct(x['pass_hat_2'])} |")
    L.append("")
    for x, y in ((NONE, REV), (NONE, W1), (W1, REV)):
        k0s = [k for k in keys if k[0] == 0]
        same0 = sum(1 for k in k0s if P2.output_sig(R[x][k]) == P2.output_sig(R[y][k]))
        pin0 = sum(1 for k in k0s if [g["prompt_tokens"] for g in R[x][k]["gens"]] == [g["prompt_tokens"] for g in R[y][k]["gens"]])
        L.append(f"- L0 {SHORT[x]} vs {SHORT[y]}：输出逐字相同 {same0}/{len(k0s)}；gens prompt_tokens 序列全同 {pin0}/{len(k0s)}")
    for arm in ARMS:
        c = Counter(e["type"] for r in G[arm]["records"] if lvl(r) == 0 for e in O.seg(r, evs[arm]))
        L.append(f"- {arm} L0 护栏事件：" + "、".join(f"{t} {c.get(t, 0)}" for t in GUARD_EV))
    for x, y in ((NONE, REV), (NONE, W1), (W1, REV)):
        for lv in FILL:
            kk = [k for k in keys if k[0] == lv]
            c, fdt = Counter(), Counter()
            full = 0
            for k in kk:
                p, q = R[x][k], R[y][k]
                same_in = p["gens"][0]["prompt_tokens"] == q["gens"][0]["prompt_tokens"] and P2.fill_fp(p) == P2.fill_fp(q)
                t0 = all(p["turns"][0].get(f) == q["turns"][0].get(f) for f in ("thoughts", "text", "tool_calls")) if p["turns"] and q["turns"] else False
                c[(same_in, t0)] += 1
                fdt[P2.first_divergent_turn(p, q)] += 1
                full += P2.output_sig(p) == P2.output_sig(q)
            L.append(f"- L{lv} {SHORT[x]} vs {SHORT[y]}（同 VM）：(首生成输入相同, 首轮输出相同) → {dict(sorted(c.items()))}；全程逐字相同 {full}/{len(kk)}；"
                     f"first_divergent_turn {dist(fdt)}")
    L.append("")

    # ============================================================ b2. 跨午夜：同 VM 首轮分叉与运行日期
    # 运行日期 = 该 run 切片（guard_events_lines）首个事件的 time 字段的日期；切片无事件的 run 退回估计
    # （run_colab.log 臂 start + 行序累加 wall_s）。日期进入输入的位置：qwen35/prompt_builder.py:271-272 `Current date:`（见 ANALYSIS §1.3）。
    L.append("### b2. 同 VM 首轮分叉按运行日期拆（日期 = run 切片首个事件 time；无事件的 run 用 臂 start + 行序累加 wall_s 估计）\n")
    logtxt = (N / "run_colab.log").read_text(encoding="utf-8")
    st_ = {m.group(2): ts(m.group(1)) for m in (re.match(_TS + r" ARM (\S+) .*start:", x) for x in logtxt.splitlines()) if m}
    en_ = {m.group(2): ts(m.group(1)) for m in (re.match(_TS + r" ARM (\S+) harness rc=", x) for x in logtxt.splitlines()) if m}
    day, est_day, moved, straddle = {}, {}, [], []
    for arm in ARMS:
        t = st_[arm]
        nofb = 0
        for idx, r in enumerate(raw[arm]):
            k = (lvl(r), r["item_id"], int(r["rep"]))
            est_day[(arm, k)] = t.date().isoformat()
            sg = O.seg(r, evs[arm])
            if sg:
                d0, d1 = sg[0]["time"], sg[-1]["time"]
                day[(arm, k)] = d0[:10]
                if d0[:10] != d1[:10]:
                    straddle.append((arm, idx, k, t, d0, d1))
                if d0[:10] != est_day[(arm, k)]:
                    moved.append((arm, idx, k, t, d0, d1))
            else:
                day[(arm, k)] = est_day[(arm, k)]
                nofb += 1
            t = t + timedelta(seconds=r.get("wall_s") or 0)
        L.append(f"- {SHORT[arm]}：切片无事件、退回估计的 run {nofb}；按日期的 run 数 {dict(sorted(Counter((k[1][0], v) for k, v in day.items() if k[0] == arm).items()))}；"
                 f"估计末 run 结束 {t.strftime('%m-%d %H:%M:%S')} 对 log harness rc 行 {en_[arm].strftime('%m-%d %H:%M:%S')}（差 {(en_[arm] - t).total_seconds():.0f} s）")
    L.append("")

    def t0same(p, q):
        return all(p["turns"][0].get(f) == q["turns"][0].get(f) for f in ("thoughts", "text", "tool_calls")) if p["turns"] and q["turns"] else False
    for lab, lst in (("事件日期 ≠ 累加估计日期（归属改变）", moved), ("切片事件跨午夜（首末事件日期不同）", straddle)):
        L.append(f"- {lab}：{len(lst)} run")
        for arm, idx, k, t, d0, d1 in lst:
            ref = NONE if arm != NONE else REV
            L.append(f"  - {SHORT[arm]} transcripts 第 {idx + 1} 行 L{k[0]} {k[1]} r{k[2]}：估计起点 {t.strftime('%m-%d %H:%M:%S')}，首事件 {d0}，末事件 {d1}；"
                     f"首轮与 {SHORT[ref]} 同键行逐字相同 {t0same(R[arm][k], R[ref][k])}")
    L.append("")
    L.append("| 对 | level | (两侧同日?, 首生成 prompt_tokens 相同?, 首轮输出逐字相同?) → run 数 |")
    L.append("|---|---|---|")
    for x, y in ((NONE, REV), (NONE, W1), (W1, REV)):
        for lv in LEVELS:
            c = Counter()
            for k in keys:
                if k[0] != lv:
                    continue
                p, q = R[x][k], R[y][k]
                c[(day[(x, k)] == day[(y, k)], p["gens"][0]["prompt_tokens"] == q["gens"][0]["prompt_tokens"], t0same(p, q))] += 1
            L.append(f"| {SHORT[x]} vs {SHORT[y]} | {lv} | {dict(sorted(c.items()))} |")
    L.append("\n- 读法：首生成 token 数相同、同种子，首轮分叉集中在跨日期的对 → 输入里的 `Current date: YYYY-MM-DD (Weekday)` 随本机日期变（两种日期写法 token 数相同）；"
             "同 VM 共同随机数只在同日期的 run 之间成立。首事件时间晚于首次渲染，跨午夜边界的 run 归属可能差一天（见上「归属改变」行的首轮比对）。"
             "另：analyze_p2 自动部分 W1 节报 d48 → 本批 rev 57K 的 system.json sha 60/60 相同（该数取自 analyze_p2，非本脚本计算），与日期块拼在 prompt 尾部、不在 system.json 一致。\n")

    # ============================================================ d. 零分布
    L.append("## d. 零分布门槛（V2c 同法：同一臂内两组不相交 m 个 rep 当两条假臂，枚举全部无序拆分得 |Δ|；"
             "门槛 t = P(|Δ|≥t) ≤ 5% 的最小可取值；— = 超出支撑）\n")
    nulls = {}

    def pool(groups, arms, lvs, metric, m):
        out = []
        for gg in groups:
            for arm in arms:
                for lv in lvs:
                    out += P2.split_null(P2.item_matrix(P2.keyed(gg[arm]["records"], lv)), m, metric)
        return sorted(out)
    gp = {arm: A.load_group(PB / arm) for arm in (NONE, FULL)}
    srcs = []
    for metric in ("success", "pass_hat_2"):
        for m in (2, 3):
            for lv in FILL:
                nulls[(f"本批 L{lv} 臂内三臂", metric, m)] = pool([G], ARMS, [lv], metric, m)
                nulls[(f"d48 L{lv} 臂内 none+rev", metric, m)] = pool([Gd], (NONE, REV), [lv], metric, m)
            nulls[("本批 49K+57K 臂内三臂", metric, m)] = pool([G], ARMS, FILL, metric, m)
            nulls[("d48 49K+57K 臂内 none+rev", metric, m)] = pool([Gd], (NONE, REV), FILL, metric, m)
            nulls[("p2b none+full L0", metric, m)] = pool([gp], (NONE, FULL), [0], metric, m)
    srcs = [f"本批 L{lv} 臂内三臂" for lv in FILL] + ["本批 49K+57K 臂内三臂"] + \
           [f"d48 L{lv} 臂内 none+rev" for lv in FILL] + ["d48 49K+57K 臂内 none+rev", "p2b none+full L0"]
    thr = {}
    L.append("| 零分布 | 拆分数 m=3 | 成功率 m=3 | Pass^2 m=3 | 拆分数 m=2 | 成功率 m=2 | Pass^2 m=2 |")
    L.append("|---|---|---|---|---|---|---|")
    for src in srcs:
        cells = []
        for metric, m in (("success", 3), ("pass_hat_2", 3), ("success", 2), ("pass_hat_2", 2)):
            xs = nulls[(src, metric, m)]
            t, p = P2.null_threshold(xs)
            thr[(src, metric, m)] = t
            if src.startswith("p2b") and (metric, m) in P2B_THR and (t is None or round(100 * t) != P2B_THR[(metric, m)]):
                raise SystemExit(f"p2b 门槛复现失败：{(src, metric, m, t)} ≠ {P2B_THR[(metric, m)]}")
            if src.startswith("d48") and m == 3:
                kk = "合并" if "49K+57K" in src else int(src.split("L")[1].split()[0])
                if t is None or round(100 * t) != D48_THR[(kk, metric)]:
                    raise SystemExit(f"d48 门槛复现失败：{(src, metric, t)} ≠ {D48_THR[(kk, metric)]}")
            cells.append("—（0 拆分）" if not xs else (f"≥{100 * t:.0f}（{100 * p:.1f}%）" if t is not None else f"—（{100 * xs[-1]:.0f}:{100 * P2.tail_p(xs, xs[-1]):.1f}%）"))
        L.append(f"| {src} | {len(nulls[(src, 'success', 3)])} | {cells[0]} | {cells[1]} | {len(nulls[(src, 'success', 2)])} | {cells[2]} | {cells[3]} |")
    L.append("\n- 本批 L0 各臂 2 rep，拆不出零分布；本批同法零分布 = 填充档臂内 6 rep（每臂每档 10 种 m=3 拆分，三臂 30 种 / 两档合并 60 种）。"
             "d48 行复现 d48 证据 d 节 23/23、27/20、23/23，p2b 行复现 23 / 27（均脚本断言）。\n")

    # ============================================================ c1 / e2a：成对比较
    def pair_tab(x, y, title):
        al = {lv: P2.aligned(G[x]["records"], G[y]["records"], lv, (x, y)) for lv in FILL}

        def ratio_ci(sel, metric):
            items = sorted(set.intersection(*[set(al[lv]["items_a"]) & set(al[lv]["items_b"]) for lv in sel]))

            def per(i, side):
                vs = [al[lv]["items_" + side][i] for lv in sel]
                if metric == "success":
                    return sum(sum(v) for v in vs), sum(len(v) for v in vs)
                return sum(A.pass_hat_2_of(v) for v in vs), len(vs)
            ua, ub = {i: per(i, "a") for i in items}, {i: per(i, "b") for i in items}

            def rr(ids):
                na, da = sum(ua[i][0] for i in ids), sum(ua[i][1] for i in ids)
                nb, db = sum(ub[i][0] for i in ids), sum(ub[i][1] for i in ids)
                return None if na == 0 else (nb / db) / (na / da)
            rng = random.Random(A.BOOT_SEED)
            bs = sorted(v for v in (rr([rng.choice(items) for _ in items]) for _b in range(A.BOOT_N)) if v is not None)
            return rr(items), bs[int(0.025 * len(bs))], bs[int(0.975 * len(bs)) - 1], A.BOOT_N - len(bs)
        L.append(title + "\n")
        L.append(f"| level | 成功率 {SHORT[x]} → {SHORT[y]} | 差 [CI]（题升/降） | 比 [CI] | 尾概率 本档三臂 / 合并三臂 m=3 | "
                 f"Pass^2 {SHORT[x]} → {SHORT[y]} | 差 [CI]（题升/降） | 比 [CI] | 尾概率 本档三臂 / 合并三臂 m=3 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        res = {}
        for lab, sel in [(str(lv), [lv]) for lv in FILL] + [("49K+57K 合并", list(FILL))]:
            if len(sel) == 1:
                lv = sel[0]
                ia, ib = al[lv]["items_a"], al[lv]["items_b"]
                units = {i: ([ia[i]], [ib[i]]) for i in ia if i in ib}
                ps, ph = P2.paired_item(units, "success_rate"), P2.paired_item(units, "pass_hat_2")
                ra, rb = (P2.items_rate(ia), P2.items_rate(ib)), (P2.items_ph2(ia), P2.items_ph2(ib))
            else:
                ps, ph, _ = P2.pooled_aligned(al, sel)
                ra = tuple(sum(sum(sum(v) for v in al[lv]["items_" + s].values()) for lv in sel)
                           / sum(sum(len(v) for v in al[lv]["items_" + s].values()) for lv in sel) for s in "ab")
                rb = tuple(statistics.mean(P2.items_ph2(al[lv]["items_" + s]) for lv in sel) for s in "ab")
            cells = []
            for p, (u, v), metric in ((ps, ra, "success"), (ph, rb, "pass_hat_2")):
                r_, lo, hi, nn = ratio_ci(sel, metric)
                res[(lab, metric)] = (p["diff"], r_, lo, hi, u, v)
                tp = " / ".join(f"{100 * P2.tail_p(nulls[(src, metric, 3)], p['diff']):.0f}%" for src in
                                ((f"本批 L{sel[0]} 臂内三臂",) if len(sel) == 1 else ()) + ("本批 49K+57K 臂内三臂",))
                cells += [f"{pct(u)} → {pct(v)}", f"{pts(p['diff'])} [{pts(p['lo'])},{pts(p['hi'])}]（{p['up']}/{p['down']}）",
                          f"{r_:.3f} [{lo:.2f},{hi:.2f}]" + (f"（{nn} 次分母 0 丢弃）" if nn else ""), tp]
            L.append(f"| {lab} | " + " | ".join(cells) + " |")
        L.append("")
        L.append("门槛判读（|观察差| 对 m=3 门槛）：")
        for lab in [str(lv) for lv in FILL] + ["49K+57K 合并"]:
            cs = []
            for metric in ("success", "pass_hat_2"):
                dd = res[(lab, metric)][0]
                src = f"本批 L{lab} 臂内三臂" if lab != "49K+57K 合并" else "本批 49K+57K 臂内三臂"
                t = thr[(src, metric, 3)]
                cs.append(f"{'成功率' if metric == 'success' else 'Pass^2'} {pts(dd)} 对 t={'—' if t is None else f'{100 * t:.0f}'} → "
                          + ("不判" if t is None else ("超噪声" if abs(dd) >= t - P2.EPS else "噪声内")))
            L.append(f"- {lab}：" + "；".join(cs))
        L.append("")
        return res

    L.append(f"## c. §2 三条（按题配对、只按题重抽 bootstrap 95%，种子 {A.BOOT_SEED}；b − a）\n")
    kn = {arm: A.knee(aggs[arm], "pass_hat_2", [0, 4096], 0.20, 0.10)["verdict"] for arm in ARMS}
    L.append(f"- 本批各臂拐点（Pass^2 规则，L0 只 2 rep）：{kn}\n")
    pair_tab(NONE, REV, "### c1a. 第一条：rev 对 none（门：比 ≥ 0.90，§2 原文两项都是 90%）")
    pair_tab(NONE, W1, "### c1b. 第一条：w1 对 none（门：比 ≥ 0.90）")

    # --- c2 防溢出
    L.append(f"### c2. 第二条：管理臂每 run 实发峰值 ≤ ceiling {CEIL:,} 且 0 次 turn_abort reason={CG.ABORT_REASON}\n")
    L.append(f"| 臂 | level | runs | 实发峰值 中位 / 最大 | ≤ {CEIL:,} 的 run | > {CEIL:,} 的生成次数 | ≥ hard 的 run | 峰值字段 ≠ 实发峰值 | turn_abort 原因 | reason={CG.ABORT_REASON} |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    seen = set()
    for arm in ARMS:
        for lv in LEVELS:
            rs = [r for r in G[arm]["records"] if lvl(r) == lv]
            sp = [O.sent_peak(r) for r in rs]
            ab = Counter((e.get("detail") or {}).get("reason") for r in rs for e in O.seg(r, evs[arm]) if e["type"] == "turn_abort")
            seen |= set(ab)
            over = sum(1 for r in rs for g in r["gens"] if (g.get("prompt_tokens") or 0) > CEIL)
            pf = sum(1 for r in rs if r.get("peak_prompt_tokens") != O.sent_peak(r))
            L.append(f"| {arm} | {lv} | {len(rs)} | {med(sp):,.0f} / {max(sp):,} | {sum(1 for v in sp if v <= CEIL)}/{len(rs)} | {over} | "
                     f"{sum(1 for v in sp if v >= HARD)} | {pf} | {dict(ab) or '—'} | {ab.get(CG.ABORT_REASON, 0)} |")
    allev = {arm: Counter((e.get("detail") or {}).get("reason") for e in evs[arm] if e["type"] == "turn_abort") for arm in ARMS}
    for arm in ARMS:
        seen |= set(allev[arm])
    if not seen <= ABORT_REASONS_KNOWN:
        raise SystemExit(f"turn_abort reason 出现未知取值：{seen - ABORT_REASONS_KNOWN}（已知 {ABORT_REASONS_KNOWN}）")
    for arm in MG:
        rs = G[arm]["records"]
        L.append(f"\n- {SHORT[arm]} 全臂：实发峰值 ≤ {CEIL:,} 的 run {sum(1 for r in rs if O.sent_peak(r) <= CEIL)}/{len(rs)}，最大 {max(O.sent_peak(r) for r in rs):,}；"
                 f"guardrail_events 全文件 turn_abort reason 分布 {dict(allev[arm])}，reason={CG.ABORT_REASON} {allev[arm].get(CG.ABORT_REASON, 0)}")
    rs = G[NONE]["records"]
    L.append(f"- none（对照）：实发峰值 > {CEIL:,} 的 run {sum(1 for r in rs if O.sent_peak(r) > CEIL)}/{len(rs)}，最大 {max(O.sent_peak(r) for r in rs):,}；"
             f"turn_abort {dict(allev[NONE])}；reason 全集（⊆ 已知取值，已核）{sorted(seen)}\n")

    # --- c3 耗时
    log = (N / "run_colab.log").read_text(encoding="utf-8")
    logd = (D / "run_colab.log").read_text(encoding="utf-8")
    cur, prv = P2.arm_minutes(log), P2.arm_minutes(logd)
    L.append("### c3. 第三条：总耗时增量 ≤ +25%（Σwall = 去重行 wall_s 之和）\n")
    L.append("| level | Σwall none | Σwall rev | Σwall w1 | rev/none | w1/none | wall 中位 none / rev / w1 s |")
    L.append("|---|---|---|---|---|---|---|")
    tot = {arm: {} for arm in ARMS}
    for lv in LEVELS:
        w = {arm: [r.get("wall_s") or 0 for r in G[arm]["records"] if lvl(r) == lv] for arm in ARMS}
        for arm in ARMS:
            tot[arm][lv] = sum(w[arm])
        L.append(f"| {lv} | {sum(w[NONE]) / 60:.1f} | {sum(w[REV]) / 60:.1f} | {sum(w[W1]) / 60:.1f} | {100 * (sum(w[REV]) / sum(w[NONE]) - 1):+.1f}% | "
                 f"{100 * (sum(w[W1]) / sum(w[NONE]) - 1):+.1f}% | {med(w[NONE]):.1f} / {med(w[REV]):.1f} / {med(w[W1]):.1f} |")
    for lab, sel in (("49K+57K", FILL), ("三档合计（含 L0）", LEVELS)):
        s = {arm: sum(tot[arm][lv] for lv in sel) for arm in ARMS}
        L.append(f"| {lab} | {s[NONE] / 60:.1f} | {s[REV] / 60:.1f} | {s[W1] / 60:.1f} | {100 * (s[REV] / s[NONE] - 1):+.1f}% | {100 * (s[W1] / s[NONE] - 1):+.1f}% | — |")
    L.append(f"\nrun_colab.log 臂墙钟：none {cur[NONE]:.1f}、rev {cur[REV]:.1f}（{100 * (cur[REV] / cur[NONE] - 1):+.1f}%）、w1 {cur[W1]:.1f} min（{100 * (cur[W1] / cur[NONE] - 1):+.1f}%）。\n")

    # ============================================================ e1. D48b 专项：密度校准 fold
    L.append("## e1. D48b 专项：minimal fold（context_guard_fold；gap = before − hard + 1，实省 = before − after，目标 实省÷gap ≈ 1.1–1.3）\n")
    L.append("| 批 / 臂 | level | fold 事件 | 有 fold 的 run | iter1 / 后 | passes | mode / method | before | after | after < hard | hard − after | 实省 | gap | 实省 ÷ gap 中位（Q1–Q3；范围） | compacted | density | density_raw；= density 的次数 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    fold_stats = {}
    for lab, GG, ev, arm in (("d48 rev", Gd, evd, REV), ("本批 rev", G, evs, REV), ("本批 w1", G, evs, W1)):
        for lv in LEVELS:
            rs = [r for r in GG[arm]["records"] if lvl(r) == lv]
            ds = [e["detail"] for r in rs for e in O.seg(r, ev[arm]) if e["type"] == "context_guard_fold"]
            nr = sum(1 for r in rs if any(e["type"] == "context_guard_fold" for e in O.seg(r, ev[arm])))
            if not ds:
                L.append(f"| {lab} | {lv} | 0 | 0/{len(rs)} | — | — | — | — | — | — | — | — | — | — | — | — | — |")
                continue
            gap = [d["before"] - HARD + 1 for d in ds]
            freed = [d["before"] - d["after"] for d in ds]
            rat = [f / g for f, g in zip(freed, gap)]
            fold_stats[(lab, lv)] = {"rat": rat, "after": [d["after"] for d in ds], "dens": [d.get("density") for d in ds]}
            i1 = sum(1 for d in ds if d.get("iteration") == 1)
            dn = [d.get("density") for d in ds if isinstance(d.get("density"), (int, float))]
            dr = [d.get("density_raw") for d in ds if isinstance(d.get("density_raw"), (int, float))]
            eqn = sum(1 for d in ds if "density" in d and d.get("density") == d.get("density_raw"))
            L.append(f"| {lab} | {lv} | {len(ds)} | {nr}/{len(rs)} | {i1} / {len(ds) - i1} | {dist(Counter(d.get('passes') for d in ds))} | "
                     f"{dict(Counter(d.get('mode') for d in ds))} / {dict(Counter(d.get('method', '（无键）') for d in ds))} | {rng_s([d['before'] for d in ds])} | "
                     f"{rng_s([d['after'] for d in ds])} | {sum(1 for d in ds if d['after'] < HARD)}/{len(ds)} | {rng_s([HARD - d['after'] for d in ds])} | "
                     f"{rng_s(freed)} | {rng_s(gap)} | {qs(rat)} | {rng_s([d.get('compacted') for d in ds])} | {qs(dn) if dn else '（无键）'} | "
                     f"{qs(dr) if dr else '（无键）'}；{eqn} |")
    r48 = fold_stats[("d48 rev", 57344)]["rat"]
    if f"{statistics.median(r48):.1f}" != D48_57K_RATIO:
        raise SystemExit(f"d48 rev 57K 实省÷缺口中位复现失败：{statistics.median(r48)} ≠ {D48_57K_RATIO}")
    for lab in ("本批 rev", "本批 w1"):
        for lv in FILL:
            fs, f48 = fold_stats.get((lab, lv)), fold_stats.get(("d48 rev", lv))
            if fs and f48:
                L.append(f"- 系数确已生效（检验）{lab} L{lv}：d48 rev 同档 实省÷gap 中位 {statistics.median(f48['rat']):.2f} ÷ 本批 fold density 中位 "
                         f"{statistics.median(fs['dens']):.2f} = {statistics.median(f48['rat']) / statistics.median(fs['dens']):.2f}，实测 {statistics.median(fs['rat']):.2f}"
                         f"（只说明整段混合密度系数确实乘上去了；剩余超折的来源拆分见 e1c）")
    L.append("")
    # 首生成 prompt
    L.append("首生成 prompt（gens[0].prompt_tokens；57K 管理臂 = 首生成前 fold 的折后值）：\n")
    L.append("| 组 | level | 首生成前 fold 的 run | 首生成 prompt 中位（范围） | hard − 首生成 中位 | fill.prompt_tokens_est 中位 |")
    L.append("|---|---|---|---|---|---|")
    first = {}
    for lab, GG, ev, arm in (("d48 none", Gd, evd, NONE), ("d48 rev", Gd, evd, REV), ("本批 none", G, evs, NONE),
                             ("本批 rev", G, evs, REV), ("本批 w1", G, evs, W1)):
        for lv in FILL:
            rs = [r for r in GG[arm]["records"] if lvl(r) == lv]
            f1 = sum(1 for r in rs if any(e["type"] == "context_guard_fold" and (e.get("detail") or {}).get("iteration") == 1 for e in O.seg(r, ev[arm])))
            f0 = [r["gens"][0]["prompt_tokens"] for r in rs]
            first[(lab, lv)] = med(f0)
            L.append(f"| {lab} | {lv} | {f1}/{len(rs)} | {rng_s(f0)} | {HARD - med(f0):,.0f} | {med([(r.get('fill') or {}).get('prompt_tokens_est') for r in rs]):,.0f} |")
    if round(first[("d48 rev", 57344)]) != D48_57K_FIRST_MED:
        raise SystemExit(f"d48 rev 57K 首生成中位复现失败：{first[('d48 rev', 57344)]} ≠ {D48_57K_FIRST_MED}")
    L.append("")
    # measure_density
    L.append("### e1b. 逐代实测密度 measure_density（gens[*]，= 分词器 token ÷ fallback_estimate；本批新字段，d48 无）与 fold density 对照\n")
    L.append("| 臂 | level | 生成数 | 有 measure_density | measure_method | measure_source | measure_tokens = prompt_tokens | measure_density 中位（Q1–Q3；范围） | gens[0] 的 measure_density |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for arm in ARMS:
        for lv in LEVELS:
            gs = [g for r in G[arm]["records"] if lvl(r) == lv for g in r["gens"]]
            md = [g.get("measure_density") for g in gs if isinstance(g.get("measure_density"), (int, float))]
            g0 = [r["gens"][0].get("measure_density") for r in G[arm]["records"] if lvl(r) == lv]
            L.append(f"| {SHORT[arm]} | {lv} | {len(gs)} | {len(md)} | {dict(Counter(g.get('measure_method') for g in gs))} | {dict(Counter(g.get('measure_source') for g in gs))} | "
                     f"{sum(1 for g in gs if g.get('measure_tokens') == g.get('prompt_tokens'))} | {qs(md, '{:.3f}')} | {qs(g0, '{:.3f}')} |")
    L.append("")
    L.append("fold 事件 density_raw（折前那次量值）对其后紧接发出的生成 gens[iteration−1].measure_density（折后 prompt），以及折前上一次发出的生成 gens[iteration−2]：\n")
    L.append("| 臂 | level | fold 数 | density_raw 中位 | 折后生成 density 中位 | 折后 − raw 中位 | 折前上一生成 density 中位 | 折后生成 prompt = fold.after 的次数 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for arm in MG:
        for lv in LEVELS:
            pr = []
            for r in G[arm]["records"]:
                if lvl(r) != lv:
                    continue
                for e in O.seg(r, evs[arm]):
                    if e["type"] != "context_guard_fold":
                        continue
                    d = e["detail"]
                    i = (d.get("iteration") or 0) - 1
                    nxt = r["gens"][i] if 0 <= i < len(r["gens"]) else None
                    prv_ = r["gens"][i - 1] if 1 <= i <= len(r["gens"]) else None
                    pr.append((d.get("density_raw"), nxt and nxt.get("measure_density"), prv_ and prv_.get("measure_density"),
                               nxt is not None and nxt.get("prompt_tokens") == d["after"]))
            if not pr:
                continue
            L.append(f"| {SHORT[arm]} | {lv} | {len(pr)} | {med([p[0] for p in pr]):.3f} | {med([p[1] for p in pr]):.3f} | "
                     f"{med([p[1] - p[0] for p in pr if p[0] is not None and p[1] is not None]):+.3f} | "
                     f"{'—' if med([p[2] for p in pr]) is None else format(med([p[2] for p in pr]), '.3f')} | {sum(p[3] for p in pr)}/{len(pr)} |")
    L.append("")
    L.append("### e1c. 被折内容的边际密度与剩余超折拆分\n")
    L.append("边际密度 ρ = (before − after) ÷ (before ÷ density_raw − after ÷ d_after)，d_after = 折后首个生成 gens[iteration−1].measure_density"
             "（只取该生成 prompt_tokens = fold.after 的事件）；即被折掉部分的 实测 token ÷ fallback_estimate。"
             "密度错配 = ρ ÷ density_raw（候选正文比整段混合密度高多少）；折叠粒度 = (实省 ÷ gap) ÷ 密度错配（按校准后估值本应刚好够用、实际多折的倍数：minimal 按整条消息落刀，"
             "同一消息里其余观察一并折，orchestrator.py:655-656）。逐事件算后取中位（Q1–Q3；范围）。\n")
    L.append("| 臂 | level | 事件 | ρ 边际密度 | density_raw（混合） | 密度错配 ρ ÷ raw | 折叠粒度 | 实省 ÷ gap | 错配 × 粒度 = 实省÷gap 的事件 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for arm in MG:
        for lv in FILL:
            res = []
            for r in G[arm]["records"]:
                if lvl(r) != lv:
                    continue
                for e in O.seg(r, evs[arm]):
                    if e["type"] != "context_guard_fold":
                        continue
                    d = e["detail"]
                    i = (d.get("iteration") or 0) - 1
                    g = r["gens"][i] if 0 <= i < len(r["gens"]) else None
                    if g is None or g.get("prompt_tokens") != d["after"] or not isinstance(g.get("measure_density"), (int, float)) \
                            or not isinstance(d.get("density_raw"), (int, float)):
                        continue
                    b_, a_, db, da = d["before"], d["after"], d["density_raw"], g["measure_density"]
                    rho = (b_ - a_) / (b_ / db - a_ / da)
                    ratio = (b_ - a_) / (b_ - HARD + 1)
                    mis = rho / db
                    res.append((rho, db, mis, ratio / mis, ratio))
            if not res:
                continue
            ok = sum(1 for x in res if abs(x[2] * x[3] - x[4]) < 1e-9)
            L.append(f"| {SHORT[arm]} | {lv} | {len(res)} | {qs([x[0] for x in res])} | {qs([x[1] for x in res])} | {qs([x[2] for x in res])} | "
                     f"{qs([x[3] for x in res])} | {qs([x[4] for x in res])} | {ok}/{len(res)} |")
    L.append("\n- 读法：剩余超折 = 密度错配 × 折叠粒度。若改用被折候选的边际密度校准（消掉错配项），实省 ÷ gap 预期 ≈ 折叠粒度中位，落在目标带 1.1–1.3 的上沿附近（推断，未测）。\n")
    for lab, GG, ev, arm in (("d48 rev", Gd, evd, REV), ("本批 rev", G, evs, REV), ("本批 w1", G, evs, W1)):
        two = sum(1 for r in GG[arm]["records"] if sum(1 for e in O.seg(r, ev[arm]) if e["type"] == "context_guard_fold") >= 2)
        back = sum(1 for r in GG[arm]["records"] if any(e["type"] == "context_guard_fold" for e in O.seg(r, ev[arm]))
                   and any((g.get("prompt_tokens") or 0) >= CEIL for g in r["gens"]))
        hd = [e["detail"] for e in ev[arm] if e["type"] == "context_hard"]
        L.append(f"- {lab}：一 run 内 ≥ 2 次 fold 的 run {two}；有 fold 的 run 里发出过 ≥ ceiling 生成的 run {back}；context_hard {len(hd)} 次"
                 + (f"（prompt {min(d['prompt_tokens'] for d in hd):,}–{max(d['prompt_tokens'] for d in hd):,}，< hard {sum(1 for d in hd if d['prompt_tokens'] < HARD)}；"
                    f"iteration=1 {sum(1 for d in hd if d.get('iteration') == 1)}）" if hd else ""))
    L.append("")

    # ============================================================ e2. W1 / W2 归因
    L.append("## e2. W1 / W2 归因（rev = W1 + W2，w1 = 只 W1；两臂 guard_thresholds 相同，见 g）\n")
    pair_tab(W1, REV, "### e2a. rev − w1（正 = 加 W2 更好）")
    L.append("### e2c. 分叉对的通过翻转（配对键 (level,item,rep)；只数 output_sig 不同的对；按 b2 日期（首事件 time）拆同日 / 跨日；同题多 rep 相关未校正）\n")
    L.append("| 对 a → b | level | 同日：分叉对 / a 过 b 不过 / a 不过 b 过 | 跨日：分叉对 / a 过 b 不过 / a 不过 b 过 |")
    L.append("|---|---|---|---|")
    for x, y in ((NONE, REV), (NONE, W1), (W1, REV)):
        for lv in FILL:
            cc = {True: Counter(), False: Counter()}
            for k in keys:
                if k[0] != lv:
                    continue
                p, q = R[x][k], R[y][k]
                if P2.output_sig(p) == P2.output_sig(q):
                    continue
                sd_ = day[(x, k)] == day[(y, k)]
                cc[sd_]["n"] += 1
                cc[sd_]["a"] += bool(p["score"]["pass"]) and not q["score"]["pass"]
                cc[sd_]["b"] += bool(q["score"]["pass"]) and not p["score"]["pass"]
            L.append(f"| {SHORT[x]} → {SHORT[y]} | {lv} | {cc[True]['n']} / {cc[True]['a']} / {cc[True]['b']} | {cc[False]['n']} / {cc[False]['a']} / {cc[False]['b']} |")
    L.append("")
    L.append("### e2d. 同日配对的通过翻转与符号检验（只用两侧同日的 (level,item,rep) 对 = 同 VM 共同随机数配对；同题多 rep 相关未校正）\n")
    L.append("| 对 a → b | level | 同日对 | 其中分叉 | a 过 b 不过 | a 不过 b 过 |")
    L.append("|---|---|---|---|---|---|")
    agg = Counter()
    for x, y in ((NONE, REV), (W1, REV), (NONE, W1)):
        for lv in LEVELS:
            n_ = dv = aw = bw = 0
            for k in keys:
                if k[0] != lv or day[(x, k)] != day[(y, k)]:
                    continue
                p, q = R[x][k], R[y][k]
                n_ += 1
                if P2.output_sig(p) == P2.output_sig(q):
                    continue
                dv += 1
                aw += bool(p["score"]["pass"]) and not q["score"]["pass"]
                bw += bool(q["score"]["pass"]) and not p["score"]["pass"]
            L.append(f"| {SHORT[x]} → {SHORT[y]} | {lv} | {n_} | {dv} | {aw} | {bw} |")
            if y == REV:
                agg["a"] += aw
                agg["b"] += bw
    nn, kk = agg["a"] + agg["b"], min(agg["a"], agg["b"])
    p2s = min(1.0, 2 * sum(math.comb(nn, j) for j in range(kk + 1)) / 2 ** nn) if nn else None
    L.append(f"\n- rev 作 b 侧的同日配对合计（none → rev 与 w1 → rev，全部档）：对侧过 rev 不过 {agg['a']}、rev 过对侧不过 {agg['b']}；"
             f"精确双侧符号检验 p = {'—' if p2s is None else f'{p2s:.4f}'}（n = {nn}）。none → w1 无同日对。\n")
    L.append("### e2b. W2 事件（step_compaction / step_readback_exempt；按 (level, detail.level, trigger)）\n")
    for arm in ARMS:
        sc = Counter((lvl(r), (e["detail"] or {}).get("level"), (e["detail"] or {}).get("trigger")) for r in G[arm]["records"]
                     for e in O.seg(r, evs[arm]) if e["type"] == "step_compaction")
        runs = sum(1 for r in G[arm]["records"] if any(e["type"] == "step_compaction" for e in O.seg(r, evs[arm])))
        tot_file = sum(1 for e in evs[arm] if e["type"] == "step_compaction")
        ex = sum(1 for e in evs[arm] if e["type"] == "step_readback_exempt")
        trig = Counter((e["detail"] or {}).get("trigger") for e in evs[arm] if e["type"] == "step_compaction")
        L.append(f"- {arm}：step_compaction 全文件 {tot_file}（切片内 run {runs}）；trigger 分布 {dict(trig) or '—'}（trigger=hard {trig.get('hard', 0)}）；"
                 f"按 (档, level, trigger) {dict(sorted(sc.items(), key=str)) or '—'}；step_readback_exempt {ex}")
    w1sc = sum(1 for e in evs[W1] if e["type"] in ("step_compaction", "step_readback_exempt"))
    L.append(f"- 核对：w1 臂 W2 事件 {w1sc} 次 → {'✓ 为 0' if w1sc == 0 else '✗ 非 0'}\n")
    # e3 落刀
    L.append(f"### e3. 相邻两次生成 prompt 下降 > {DROP:,} tok（gens[i−1] → gens[i]，i ≥ 1）\n")
    L.append("| 批 / 臂 | level | 下降次数（run） | 下降前 prompt | 下降后 prompt | 下降前 < hard 的次数 | run 内首次下降后最小 prompt 中位 |")
    L.append("|---|---|---|---|---|---|---|")
    for lab, GG, arm in (("d48 rev", Gd, REV), ("本批 none", G, NONE), ("本批 rev", G, REV), ("本批 w1", G, W1)):
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
            L.append(f"| {lab} | {lv} | {len(pre)}（{runs_}） | {rng_s(pre)} | {rng_s(post)} | {sum(1 for v in pre if v < HARD)} | "
                     f"{'—' if not mins else format(med(mins), ',.0f')} |")
    L.append("")
    # 机制总表
    L.append("### e4. 护栏事件按档（切片 guard_events_lines）\n")
    L.append("| 批 / 臂 | level | runs | soft（run） | hard iter1 / 后（run） | fold iter1 / 后（run） | step_compaction（run） |")
    L.append("|---|---|---|---|---|---|---|")
    for lab, GG, ev, arm in (("d48 rev", Gd, evd, REV), ("本批 rev", G, evs, REV), ("本批 w1", G, evs, W1)):
        for lv in LEVELS:
            rs = [r for r in GG[arm]["records"] if lvl(r) == lv]
            c, runs = Counter(), Counter()
            h1 = hn = f1 = fn = 0
            for r in rs:
                sg = O.seg(r, ev[arm])
                tt = Counter(e["type"] for e in sg)
                c.update(tt)
                runs.update({t: 1 for t in tt})
                for e in sg:
                    it = (e.get("detail") or {}).get("iteration")
                    if e["type"] == "context_hard":
                        h1 += it == 1
                        hn += it != 1
                    if e["type"] == "context_guard_fold":
                        f1 += it == 1
                        fn += it != 1
            L.append(f"| {lab} | {lv} | {len(rs)} | {c['context_soft']}（{runs['context_soft']}） | {h1} / {hn}（{runs['context_hard']}） | "
                     f"{f1} / {fn}（{runs['context_guard_fold']}） | {c['step_compaction']}（{runs['step_compaction']}） |")
    L.append("")

    # ============================================================ x. 跨批同臂（d48 → 本批，跨 VM 同种子，非配对）
    L.append("## x. 跨批同臂（d48 → 本批；跨 VM、同种子同输入，不是同 VM 配对）\n")
    L.append("| 臂 | level | 同键 | 输出逐字相同 | 首生成 prompt 相同 | 成功率 d48 → 本批 | Pass^2 d48 → 本批 | 差（成功率 / Pass^2）[CI] |")
    L.append("|---|---|---|---|---|---|---|---|")
    for arm in (NONE, REV):
        for lv in LEVELS:
            kk = sorted(k for k in set(R[arm]) & set(Rd[arm]) if k[0] == lv)
            same = sum(1 for k in kk if P2.output_sig(R[arm][k]) == P2.output_sig(Rd[arm][k]))
            fin = sum(1 for k in kk if R[arm][k]["gens"][0]["prompt_tokens"] == Rd[arm][k]["gens"][0]["prompt_tokens"])
            al = P2.aligned(Gd[arm]["records"], G[arm]["records"], lv, ("d48", "d48b"))
            ia, ib = al["items_a"], al["items_b"]
            units = {i: ([ia[i]], [ib[i]]) for i in ia if i in ib}
            ps, ph = P2.paired_item(units, "success_rate"), P2.paired_item(units, "pass_hat_2")
            L.append(f"| {SHORT[arm]} | {lv} | {len(kk)} | {same} | {fin} | {pct(P2.items_rate(ia))} → {pct(P2.items_rate(ib))} | "
                     f"{pct(P2.items_ph2(ia))} → {pct(P2.items_ph2(ib))} | {pts(ps['diff'])} [{pts(ps['lo'])},{pts(ps['hi'])}] / "
                     f"{pts(ph['diff'])} [{pts(ph['lo'])},{pts(ph['hi'])}] |")
    L.append("")

    # ============================================================ t. 时长
    L.append("## t. 时长与租约（run_colab.log）\n")
    for lab, lg, arms in (("本批", log, ARMS), ("d48", logd, (NONE, REV))):
        st = {m.group(2): m.group(1) for m in (re.match(_TS + r" ARM (\S+) .*start:", x) for x in lg.splitlines()) if m}
        en = {m.group(2): m.group(1) for m in (re.match(_TS + r" ARM (\S+) harness rc=", x) for x in lg.splitlines()) if m}
        mins = P2.arm_minutes(lg)
        L.append(f"- {lab}：" + "；".join(f"{SHORT[x]} {st[x][11:16]}→{en[x][11:16]}（{mins[x]:.1f} min）" for x in arms))
        lines_ = lg.splitlines()
        t0 = ts(re.match(_TS, lines_[0]).group(1))
        dep = [ts(m.group(1)) for m in (re.match(_TS + r" START deploy", x) for x in lines_) if m]
        rdy = [ts(m.group(1)) for m in (re.match(_TS + r" READY ", x) for x in lines_) if m]
        stp = [ts(m.group(1)) for m in (re.match(_TS + r" stop rc=", x) for x in lines_) if m]
        mh = re.search(r"MAX_VM_HOURS=(\d+)", lg)
        lease_lines = [x for x in lines_ if re.search(r"lease|租约|切段|rotate", x, re.I) and "config:" not in x]
        bs = P2.batch_span(lg)
        L.append(f"  - START deploy {len(dep)} 次（{', '.join(d.strftime('%H:%M:%S') for d in dep)}）；READY {', '.join(d.strftime('%H:%M:%S') for d in rdy)}；"
                 f"末次 START deploy → stop rc 行 {(stp[-1] - dep[-1]).total_seconds() / 3600:.2f} h（MAX_VM_HOURS={mh.group(1) if mh else '?'}）；"
                 f"首行 → DONE {bs['minutes']:.1f} min（{bs['minutes'] / 60:.2f} h，首行 {t0.strftime('%H:%M:%S')}）；"
                 f"非 config 行里 lease/租约/切段/rotate 字样 {len(lease_lines)} 行；tailnet_cleanup "
                 f"{[x.split('\"tailnet_cleanup\": ')[1].split('}')[0] for x in lines_ if 'tailnet_cleanup' in x]}")
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
        L.append(f"- {arm} 行内取值不唯一的配置键：{multi or '无'}")

    def cfgset(rs, k):
        return {json.dumps(r.get(k), sort_keys=True) for r in rs}
    for x, y in ((NONE, REV), (NONE, W1), (W1, REV)):
        L.append(f"- 行内配置键 {SHORT[x]} ≠ {SHORT[y]}：{[k for k in CFG_KEYS if cfgset(raw[x], k) != cfgset(raw[y], k)]}")
    rd = {arm: rows(D / arm / "transcripts.jsonl") for arm in (NONE, REV)}
    for arm in (NONE, REV):
        L.append(f"- 行内配置键 d48 {SHORT[arm]} ≠ 本批 {SHORT[arm]}：" + "；".join(
            f"{k}: {sorted(cfgset(rd[arm], k))} → {sorted(cfgset(raw[arm], k))}" for k in CFG_KEYS if cfgset(rd[arm], k) != cfgset(raw[arm], k)))

    def hmk(r):
        return json.dumps(RK.harness_mode_of(r), sort_keys=True, ensure_ascii=False)
    hm = {arm: {hmk(r) for r in raw[arm]} for arm in ARMS}
    L.append(f"- harness_mode_of 每臂取值数：{ {SHORT[x]: len(hm[x]) for x in ARMS} }")
    if len(hm[W1]) == 1 and len(hm[REV]) == 1:
        a_, b_ = json.loads(next(iter(hm[W1]))), json.loads(next(iter(hm[REV])))
        L.append(f"- harness_mode w1 → rev 不同的键：" + "；".join(f"{k}: {x} → {y}" for k, x, y in zip(RK.HARNESS_MODE_KEYS, a_, b_) if x != y))
    L.append(f"- run_colab.log：余额行（unit）{'有' if 'unit' in log else '无（费用数不能从本目录复算）'}；"
             f"臂命令行：" + "；".join(f"{m.group(1)} `{m.group(2).strip()}`" for m in (re.search(r"ARM (\S+) \[.*?\] start: (.*)", x) for x in log.splitlines()) if m))
    L.append("")
    out = "\n".join(L) + "\n"
    if a.out:
        Path(a.out).write_text(out, encoding="utf-8")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
