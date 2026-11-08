"""W1/W2 评价批（context-knee-20261108-w12eval）证据：一致性 / 漂移 / error 行 / §2 三条验收 / W1-W2 触发 / 57K pre-fold。

  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/data/context-knee-20261108-w12eval/w12eval_evidence.py \
      [--data benchmarks/data/context-knee-20261108-w12eval] [--p2b benchmarks/data/context-knee-20261108-p2b] \
      [--out benchmarks/data/context-knee-20261108-w12eval/w12eval_evidence.md]

只读：两批 orch-turn-{none,w1w2,full}/transcripts.jsonl、guardrail_events.jsonl、sessions/、smoke-orch-*/transcripts.jsonl、
run_colab.log。CPU、标准库 + benchmarks/context_knee 的 analyze / analyze_orch / analyze_p2；bootstrap 种子 A.BOOT_SEED → 输出确定。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT / "backend", ROOT / "benchmarks"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))
from context_knee import analyze as A  # noqa: E402
from context_knee import analyze_orch as O  # noqa: E402
from context_knee import analyze_p2 as P2  # noqa: E402
from context_knee.quality import COHERENT_THRESHOLDS  # noqa: E402

NONE, W12, FULL = "orch-turn-none", "orch-turn-w1w2", "orch-turn-full"
FILL = (32768, 49152, 57344)
COPIED = {NONE: 120, W12: 80}          # 拷贝时行数（rep 0–1 + none L0 6 rep）
P2B_THR = {("success", 3): 23, ("pass_hat_2", 3): 27, ("success", 2): 30}   # 委托给出的 p2b 门槛（none+full L0 合并）
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
    return "0.0" if s == "-0.0" else s   # 规范化负零：−0.0 → 0.0


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "benchmarks/data/context-knee-20261108-w12eval"))
    ap.add_argument("--p2b", default=str(ROOT / "benchmarks/data/context-knee-20261108-p2b"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    N, P = Path(a.data), Path(a.p2b)
    L = [f"# w12eval 证据（`{Path(__file__).name}` 生成；data = `{N.name}`，p2b = `{P.name}`）\n"]

    # ------------------------------------------------------------ 1a 拷贝前缀逐字节
    L.append("## 1a. 拷贝前缀逐字节核对（本批前 k 行 / 前 k 字节 vs p2b 同名文件全文）\n")
    L.append("| 臂 | 文件 | p2b 行数 | 本批行数 | 前缀 sha256[:16] 本批 / p2b | 逐字节相同 |")
    L.append("|---|---|---|---|---|---|")
    for arm, k in COPIED.items():
        for fn in ("transcripts.jsonl", "guardrail_events.jsonl"):
            pb = (P / arm / fn).read_bytes()
            nb = (N / arm / fn).read_bytes()
            L.append(f"| {arm} | {fn} | {pb.count(b'\n')} | {nb.count(b'\n')} | {sha(nb[:len(pb)])} / {sha(pb)} | {nb[:len(pb)] == pb} |")
        assert len([x for x in (P / arm / "transcripts.jsonl").read_text(encoding="utf-8").splitlines() if x]) == k
        # sessions：p2b 每个会话目录在本批都在、且逐文件 sha 相同
        ps = sorted(d.name for d in (P / arm / "sessions").iterdir() if d.is_dir())
        miss, diff, nfiles = 0, 0, 0
        for s in ps:
            nd = N / arm / "sessions" / s
            if not nd.is_dir():
                miss += 1
                continue
            for f in (P / arm / "sessions" / s).rglob("*"):
                if f.is_file():
                    nfiles += 1
                    g = nd / f.relative_to(P / arm / "sessions" / s)
                    if not g.is_file() or g.read_bytes() != f.read_bytes():
                        diff += 1
        ns = sum(1 for d in (N / arm / "sessions").iterdir() if d.is_dir())
        L.append(f"| {arm} | sessions/ | {len(ps)} 目录 | {ns} 目录 | p2b 目录缺 {miss}、文件 {nfiles} 个中不同 {diff} | {miss == 0 and diff == 0} |")
    L.append("")

    # ------------------------------------------------------------ 1b 冒烟漂移
    sa = rows(next(P.glob("smoke-orch-*/transcripts.jsonl")))[0]
    sb = rows(next(N.glob("smoke-orch-*/transcripts.jsonl")))[0]
    skip = {"turns", "gens", "turn_response", "stamp", "wall_s", "session_dir", "score", "final_text_quality", "turn_metrics",
            "peak_prompt_tokens", "peak_prompt_measured", "env_state", "guard_events", "guard_events_lines", "meta_calls",
            "full_renders", "audit_gens", "stub_served", "tool_effects", "dup_blocked", "turn_status", "aborted_reason",
            "repetition_events", "spill_readbacks", "system_errors", "audit_generations", "stub_unknown", "stub_failed"}
    cfgdiff = [k for k in sorted(set(sa) | set(sb)) if k not in skip and sa.get(k) != sb.get(k)]
    fd = P2.first_divergent_turn(sa, sb)
    L.append("## 1b. 冒烟漂移探针（p2b `smoke-orch-20261108-123017-A` vs 本批 `smoke-orch-20261108-211442-A`）\n")
    L.append(f"- 输入：level {sa['level']} / {sa['item_id']} / r{sa['rep']} / seed {sa['sampling_seed']} vs {sb['sampling_seed']}；"
             f"非输出字段差：{cfgdiff or '无'}；agent_cfg_fingerprint {sa['agent_cfg_fingerprint']} / {sb['agent_cfg_fingerprint']}")
    L.append(f"- output_sig 相同：{P2.output_sig(sa) == P2.output_sig(sb)}；first_divergent_turn = {fd}；轮数 {len(sa['turns'])} / {len(sb['turns'])}；"
             f"pass {sa['score']['pass']} / {sb['score']['pass']}")
    L.append(f"- gens prompt_tokens：{[g['prompt_tokens'] for g in sa['gens']]} / {[g['prompt_tokens'] for g in sb['gens']]}")
    L.append(f"- gens generation_tokens：{[g['generation_tokens'] for g in sa['gens']]} / {[g['generation_tokens'] for g in sb['gens']]}")
    if fd is not None and fd < min(len(sa["turns"]), len(sb["turns"])):
        for f in ("thoughts", "text", "tool_calls"):
            x = json.dumps(sa["turns"][fd][f], ensure_ascii=False)
            y = json.dumps(sb["turns"][fd][f], ensure_ascii=False)
            if x != y:
                i = next((j for j, (c, d) in enumerate(zip(x, y)) if c != d), min(len(x), len(y)))
                L.append(f"- turn {fd}.{f}：长度 {len(x)} / {len(y)}，首个不同字符位置 {i}；p2b `{x[max(0, i - 20):i + 50]}` / 本批 `{y[max(0, i - 20):i + 50]}`")
    for arm in (NONE, W12):
        for r in rows(P / arm / "transcripts.jsonl"):
            if r["level"] == sa["level"] and r["item_id"] == sa["item_id"] and r["rep"] == sa["rep"]:
                L.append(f"- p2b {arm} 同 (level,item,rep) 行 vs p2b 冒烟：output_sig 相同 {P2.output_sig(r) == P2.output_sig(sa)}；"
                         f"vs 本批冒烟 first_divergent_turn = {P2.first_divergent_turn(r, sb)}")
    L.append("")

    # ------------------------------------------------------------ 载入（A.load_group 去重口径）
    G = {arm: A.load_group(N / arm) for arm in (NONE, W12)}
    raw = {arm: rows(N / arm / "transcripts.jsonl") for arm in (NONE, W12)}
    evs = {arm: O.load_events(N / arm) for arm in (NONE, W12)}
    for arm in (NONE, W12):
        assert len(G[arm]["records"]) == len(raw[arm]), arm

    # ------------------------------------------------------------ 1c 输入只随 rep 变
    L.append("## 1c. 新 rep 2–5 的输入与配置（前 k 行 = 拷贝行，其余 = 本批新行；k 见 COPIED）\n")
    for arm in (NONE, W12):
        old, new = raw[arm][:COPIED[arm]], raw[arm][COPIED[arm]:]
        cd = []
        for k in CFG_KEYS:
            vo = {json.dumps(r.get(k), sort_keys=True) for r in old}
            vn = {json.dumps(r.get(k), sort_keys=True) for r in new}
            if vo != vn:
                cd.append(f"{k}: 旧 {sorted(vo)[:3]} 新 {sorted(vn)[:3]}")
        badseed = sum(1 for r in raw[arm] if r.get("sampling_seed") != 1107 * 1000 + int(r["rep"]) or not r.get("sampling_seed_sent"))
        newreps = Counter((int(r["level"]), int(r["rep"])) for r in new)
        sys_sha = defaultdict(set)
        for i, r in enumerate(raw[arm]):
            f = N / arm / "sessions" / O.sess_name(r) / "system.json"
            sys_sha["旧" if i < COPIED[arm] else "新"].add(sha(f.read_bytes()) if f.is_file() else "<缺>")
        L.append(f"- {arm}：新行 {len(new)}，(level, rep) 计数 {dict(sorted(newreps.items()))}；新旧行配置键取值集合不同的键：{cd or '无'}；"
                 f"seed ≠ 1107×1000+rep 或未发送的行 {badseed}；system.json sha 旧 {sorted(sys_sha['旧'])} / 新 {sorted(sys_sha['新'])}")
    # 填充 fingerprint：同 (level,item) 跨 rep 应各不相同；同 (level,item,rep) 跨臂应相同
    fp = {arm: {(int(r["level"]), r["item_id"], int(r["rep"])): r for r in raw[arm]} for arm in (NONE, W12)}
    keys = sorted(set(fp[NONE]) & set(fp[W12]))
    same_fp = sum(1 for k in keys if P2.fill_fp(fp[NONE][k]) == P2.fill_fp(fp[W12][k]))
    same_task = sum(1 for k in keys if fp[NONE][k].get("task_fp") == fp[W12][k].get("task_fp"))
    same_meas = sum(1 for k in keys if (fp[NONE][k].get("fill") or {}).get("prompt_tokens_measured")
                    == (fp[W12][k].get("fill") or {}).get("prompt_tokens_measured"))
    L.append(f"- none vs w1w2 同 (level,item,rep)：配对 {len(keys)}，fill.fingerprint 相同 {same_fp}，task_fp 相同 {same_task}，"
             f"fill.prompt_tokens_measured 相同 {same_meas}")
    for arm in (NONE, W12):
        bad, tot = 0, 0
        task_bad = 0
        for lv in FILL:
            for it in sorted({k[1] for k in fp[arm] if k[0] == lv}):
                rs = [fp[arm][(lv, it, rep)] for rep in range(6) if (lv, it, rep) in fp[arm]]
                tot += 1
                if len({P2.fill_fp(r) for r in rs}) != len(rs):
                    bad += 1
                if len({r.get("task_fp") for r in rs}) != 1:
                    task_bad += 1
        L.append(f"- {arm} 填充档 (level,item) {tot} 组：6 个 rep 的 fill.fingerprint 不全互异的组 {bad}；task_fp 跨 rep 不唯一的组 {task_bad}")
    # 1d：同 VM 内共同随机数——32768 档 none 与 w1w2 首生成输入相同（W1 soft 只记事件，p2b 已证 rep 0–1 首轮 20/20 逐字相同）
    for rr in (range(0, 2), range(2, 6)):
        c = Counter()
        fdt = Counter()
        for k in keys:
            if k[0] != 32768 or k[2] not in rr:
                continue
            x, y = fp[NONE][k], fp[W12][k]
            t0 = [[x["turns"][0].get(f) for f in ("thoughts", "text", "tool_calls")], [y["turns"][0].get(f) for f in ("thoughts", "text", "tool_calls")]]
            c[(x["gens"][0]["prompt_tokens"] == y["gens"][0]["prompt_tokens"] and P2.fill_fp(x) == P2.fill_fp(y), t0[0] == t0[1])] += 1
            fdt[P2.first_divergent_turn(x, y)] += 1
        L.append(f"- 1d. L32768 none vs w1w2 rep {rr.start}–{rr.stop - 1}（{'p2b VM' if rr.start == 0 else '本批 VM'}）：(首生成输入相同, 首轮输出逐字相同) → "
                 f"{dict(sorted(c.items()))}；first_divergent_turn 分布 {dict(sorted(fdt.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)))}（None = 全程逐字相同）")
    lv, it = 49152, "lc-hard-001"
    smp = [fp[NONE][(lv, it, rep)] for rep in range(6)]
    L.append(f"- 抽样 none L{lv} {it}：rep→(fingerprint, prompt_tokens_measured, gens[0].prompt_tokens) "
             + "；".join(f"r{r['rep']} {P2.fill_fp(r)}/{(r.get('fill') or {}).get('prompt_tokens_measured')}/{r['gens'][0]['prompt_tokens']}" for r in smp))
    L.append("")

    # ------------------------------------------------------------ 2 error 行
    L.append("## 2. 含 `\"error\"` 字样的行\n")
    for arm in (NONE, W12):
        for ln, line in enumerate((N / arm / "transcripts.jsonl").read_text(encoding="utf-8").splitlines(), 1):
            if '"error"' not in line:
                continue
            r = json.loads(line)
            calls = [(ti, ci, c) for ti, t in enumerate(r.get("turns") or []) for ci, c in enumerate(t.get("tool_calls") or [])
                     if c.get("type") == "error"]
            ev = Counter(e["type"] for e in O.seg(r, evs[arm]))
            in_records = any(x is r or (x["level"], x["item_id"], x["rep"]) == (r["level"], r["item_id"], r["rep"]) for x in G[arm]["records"])
            L.append(f"- {arm} 第 {ln} 行：level {r['level']} / {r['item_id']} / r{r['rep']}；顶层 `error` 键 {'error' in r}；"
                     f"turn_status {r.get('turn_status')}；system_errors {r.get('system_errors')}；type=error 的工具调用 "
                     + "；".join(f"turns[{ti}].tool_calls[{ci}] `{c.get('error_msg', '')[:90]}` raw `{str(c.get('raw_input'))[:60]!r}`" for ti, ci, c in calls)
                     + f"；score.pass {r['score']['pass']} why {r['score'].get('why')}；事件 parse_error {ev.get('parse_error', 0)}；"
                     f"进 A.load_group 有效行（计入分母）{in_records}；该组 missing（error 行）{len(G[arm]['missing'])}")
    L.append("")

    # ------------------------------------------------------------ 3 §2 验收
    aggs = {arm: A.aggregate(G[arm], dict(COHERENT_THRESHOLDS), {x for x in A.DEFAULT_WALL_INVALID.split(",") if x}) for arm in (NONE, W12)}
    kn = A.knee(aggs[NONE], "pass_hat_2", [0, 4096], 0.20, 0.10)
    knb = A.knee(aggs[NONE], "success_rate", [0, 4096], 0.20, 0.10)
    blo, blv = P2.band_knee(aggs[NONE])
    knee = kn.get("knee")
    kt = int(knee) if knee else None
    if kt is None:
        raise SystemExit("拐点缺失，无法判 §2")
    sel_fill = [lv for lv in FILL if lv >= kt]   # 拐点以上的填充档；kt=None 已在上面拦掉，不会 0.7*None
    L.append("## 3. §2 验收（none → w1w2，b − a；只按题聚类 bootstrap 95%，种子 "
             f"{A.BOOT_SEED}、{A.BOOT_N} 次）\n")
    L.append(f"- 拐点（none）：Pass^2 规则 {kn['verdict']}（基线 L{kn.get('base_level')} {pct(kn.get('base'))}）；成功率规则 {knb['verdict']}；"
             f"成功率带拐点（L0 按题 CI 下界 {pct(blo)}）→ {blv}")
    # 零分布：本批 none L0（6 rep）；p2b none + full L0 合并（委托给出的门槛来源）；本批各填充档臂内 6 rep（补充）
    mats = {NONE: P2.item_matrix(P2.keyed(G[NONE]["records"], 0))}
    gp = {arm: A.load_group(P / arm) for arm in (NONE, FULL)}
    nulls = {}
    for metric in ("success", "pass_hat_2"):
        for m in (2, 3):
            nulls[("本批 none L0", metric, m)] = P2.split_null(mats[NONE], m, metric)
            pool = []
            for arm in (NONE, FULL):
                pool += P2.split_null(P2.item_matrix(P2.keyed(gp[arm]["records"], 0)), m, metric)
            nulls[("p2b none+full L0", metric, m)] = sorted(pool)
            for lv in FILL:
                pool = []
                for arm in (NONE, W12):
                    pool += P2.split_null(P2.item_matrix(P2.keyed(G[arm]["records"], lv)), m, metric)
                nulls[(f"本批 L{lv} 臂内（none+w1w2）", metric, m)] = sorted(pool)
    L.append("\n零分布门槛（P(|Δ|≥t) ≤ 5% 的最小 t；— = 超出支撑，括号 = 最大值:尾概率）：\n")
    L.append("| 零分布 | 拆分数 m=3 | 成功率 m=3 | Pass^2 m=3 | 成功率 m=2 | Pass^2 m=2 |")
    L.append("|---|---|---|---|---|---|")
    for src in ["本批 none L0", "p2b none+full L0"] + [f"本批 L{lv} 臂内（none+w1w2）" for lv in FILL]:
        cells = []
        for metric, m in (("success", 3), ("pass_hat_2", 3), ("success", 2), ("pass_hat_2", 2)):
            xs = nulls[(src, metric, m)]
            t, p = P2.null_threshold(xs)
            if src == "p2b none+full L0" and (metric, m) in P2B_THR:
                # 委托给出的 p2b 门槛（none+full L0 合并）：对重算值断言，不是摆设常量
                assert t is not None and round(100 * t) == P2B_THR[(metric, m)], (src, metric, m, t, P2B_THR[(metric, m)])
            cells.append(f"≥{100 * t:.0f}（{100 * p:.1f}%）" if t is not None else f"—（{100 * xs[-1]:.0f}:{100 * P2.tail_p(xs, xs[-1]):.1f}%）")
        L.append(f"| {src} | {len(nulls[(src, 'success', 3)])} | " + " | ".join(cells) + " |")
    L.append("")
    al = {lv: P2.aligned(G[NONE]["records"], G[W12]["records"], lv, (NONE, W12)) for lv in FILL}

    def ratio_ci(sel, metric):
        """w1w2 ÷ none 的合并率之比（成功率 = Σ通过 / Σrun；Pass^2 = 逐题逐档无偏均值），按题聚类 bootstrap。"""
        items = sorted(set.intersection(*[set(al[lv]["items_a"]) & set(al[lv]["items_b"]) for lv in sel]))

        def per(i, side):
            vs = [al[lv]["items_" + side][i] for lv in sel]
            if metric == "success":
                return sum(sum(v) for v in vs), sum(len(v) for v in vs)
            return sum(A.pass_hat_2_of(v) for v in vs), len(vs)
        ua = {i: per(i, "a") for i in items}
        ub = {i: per(i, "b") for i in items}

        def r(ids):
            na, da = sum(ua[i][0] for i in ids), sum(ua[i][1] for i in ids)
            nb, db = sum(ub[i][0] for i in ids), sum(ub[i][1] for i in ids)
            return None if na == 0 else (nb / db) / (na / da)
        rng = random.Random(A.BOOT_SEED)
        bs = sorted(x for x in (r([rng.choice(items) for _ in items]) for _b in range(A.BOOT_N)) if x is not None)
        return r(items), bs[int(0.025 * len(bs))], bs[int(0.975 * len(bs)) - 1], A.BOOT_N - len(bs)
    L.append("| level | 成功率 none → w1w2 | 差 [CI]（升/降） | 比 [CI]（≥0.9） | 尾概率 本批L0 m=3 / p2b m=3 / 本档臂内 m=3 | "
             "Pass^2 none → w1w2 | 差 [CI]（升/降） | 比 [CI]（≥0.9） | 尾概率 本批L0 m=3 / p2b m=3 / 本档臂内 m=3 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for lab, sel in [(str(lv), [lv]) for lv in sel_fill] + [("三填充档合并", list(sel_fill))]:
        if len(sel) == 1:
            lv = sel[0]
            ia, ib = al[lv]["items_a"], al[lv]["items_b"]
            units = {i: ([ia[i]], [ib[i]]) for i in ia if i in ib}
            ps, ph = P2.paired_item(units, "success_rate"), P2.paired_item(units, "pass_hat_2")
            ra, rb = (P2.items_rate(ia), P2.items_rate(ib)), (P2.items_ph2(ia), P2.items_ph2(ib))
        else:
            ps, ph, _ = P2.pooled_aligned(al, sel)
            ra = (sum(sum(sum(v) for v in al[lv]["items_a"].values()) for lv in sel) / sum(sum(len(v) for v in al[lv]["items_a"].values()) for lv in sel),
                  sum(sum(sum(v) for v in al[lv]["items_b"].values()) for lv in sel) / sum(sum(len(v) for v in al[lv]["items_b"].values()) for lv in sel))
            rb = (statistics.mean(P2.items_ph2(al[lv]["items_a"]) for lv in sel), statistics.mean(P2.items_ph2(al[lv]["items_b"]) for lv in sel))
        cells = []
        for p, (x, y), metric in ((ps, ra, "success"), (ph, rb, "pass_hat_2")):
            rr, lo, hi, nn = ratio_ci(sel, metric)
            if len(sel) == 1:
                tp = " / ".join(f"{100 * P2.tail_p(nulls[(src, metric, 3)], p['diff']):.0f}%" for src in
                                ("本批 none L0", "p2b none+full L0", f"本批 L{sel[0]} 臂内（none+w1w2）"))
            else:
                tp = "（合并无零分布）"
            cells += [f"{pct(x)} → {pct(y)}", f"{pts(p['diff'])} [{pts(p['lo'])},{pts(p['hi'])}]（{p['up']}/{p['down']}）",
                      f"{rr:.2f} [{lo:.2f},{hi:.2f}]" + (f"（{nn} 次分母 0 丢弃）" if nn else ""), tp]
        L.append(f"| {lab} | " + " | ".join(cells) + " |")
    L.append("")

    # rep 段拆分：rep 0–1（p2b VM，= p2b 结论所用数据）对 rep 2–5（本批 VM）
    L.append("rep 段拆分（成功率 k/n；差 = w1w2 − none 按题配对，只按题 bootstrap）：\n")
    L.append("| level | rep 段 | none | w1w2 | 差 [CI]（升/降） |")
    L.append("|---|---|---|---|---|")
    for sel in [[lv] for lv in FILL] + [list(FILL)]:
        for rr in (range(0, 2), range(2, 6)):
            units = {}
            ka = kb = na = nb = 0
            for lv in sel:
                ia = A.per_item([r for r in G[NONE]["records"] if int(r["level"]) == lv and int(r["rep"]) in rr])
                ib = A.per_item([r for r in G[W12]["records"] if int(r["level"]) == lv and int(r["rep"]) in rr])
                ka += sum(sum(v) for v in ia.values())
                na += sum(len(v) for v in ia.values())
                kb += sum(sum(v) for v in ib.values())
                nb += sum(len(v) for v in ib.values())
                for i in ia:
                    if i in ib:
                        units.setdefault(i, ([], []))
                        units[i][0].append(ia[i])
                        units[i][1].append(ib[i])
            p = P2.paired_item(units, "success_rate")
            L.append(f"| {'三填充档合并' if len(sel) > 1 else sel[0]} | rep {rr.start}–{rr.stop - 1} | {ka}/{na} = {pct(ka / na)} | {kb}/{nb} = {pct(kb / nb)} | "
                     f"{pts(p['diff'])} [{pts(p['lo'])},{pts(p['hi'])}]（{p['up']}/{p['down']}） |")
    L.append("")

    # 峰值 prompt（kt / sel_fill 已在 §2 前算好并拦过 None）
    L.append(f"峰值 prompt（实发峰值 = max(gens.prompt_tokens)，O.sent_peak；拐点 = none Pass^2 拐点档 {kt} → 0.7× = {0.7 * kt:,.0f} tok）：\n")
    alt = 49152                         # 对照：D1/D2 现值所依据的「49K 内无拐点」口径
    L.append(f"| 臂 | level | 实发峰值 中位 / 最小 / 最大 | 中位 ÷ 拐点 | ≤ 0.7×拐点 的 run | ≤ 0.7×{alt}（= {0.7 * alt:,.0f}）的 run（对照） | 首生成 prompt 中位 / 最小 / 最大 | fill.prompt_tokens_est 中位 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for arm in (NONE, W12):
        for lv in sel_fill:
            rs = [r for r in G[arm]["records"] if int(r["level"]) == lv]
            sp = [O.sent_peak(r) for r in rs]
            f0 = [r["gens"][0]["prompt_tokens"] for r in rs]
            est = [(r.get("fill") or {}).get("prompt_tokens_est") for r in rs]
            L.append(f"| {arm} | {lv} | {med(sp):,.0f} / {min(sp):,} / {max(sp):,} | {med(sp) / kt:.2f} | {sum(1 for x in sp if x <= 0.7 * kt)}/{len(sp)} | {sum(1 for x in sp if x <= 0.7 * alt)}/{len(sp)} | "
                     f"{med(f0):,.0f} / {min(f0):,} / {max(f0):,} | {med(est):,.0f} |")
    L.append("")

    # 用时
    cur = P2.arm_minutes((N / "run_colab.log").read_text(encoding="utf-8"))
    L.append("用时（Σwall = 去重行 wall_s 之和，含 rep 0–1 拷贝行（p2b VM）；增量 = w1w2 ÷ none − 1）：\n")
    L.append("| level | Σwall none min | Σwall w1w2 min | 增量（≤ +25%） | wall 中位 none / w1w2 s |")
    L.append("|---|---|---|---|---|")
    tn = tw = 0.0
    for lv in FILL:
        wn = [r.get("wall_s") or 0 for r in G[NONE]["records"] if int(r["level"]) == lv]
        ww = [r.get("wall_s") or 0 for r in G[W12]["records"] if int(r["level"]) == lv]
        tn += sum(wn)
        tw += sum(ww)
        L.append(f"| {lv} | {sum(wn) / 60:.1f} | {sum(ww) / 60:.1f} | {100 * (sum(ww) / sum(wn) - 1):+.1f}% | {med(wn):.1f} / {med(ww):.1f} |")
    L.append(f"| 三填充档合并 | {tn / 60:.1f} | {tw / 60:.1f} | {100 * (tw / tn - 1):+.1f}% | — |")
    for rr in (range(0, 2), range(2, 6)):
        wn = sum(r.get("wall_s") or 0 for r in G[NONE]["records"] if int(r["level"]) > 0 and int(r["rep"]) in rr)
        ww = sum(r.get("wall_s") or 0 for r in G[W12]["records"] if int(r["level"]) > 0 and int(r["rep"]) in rr)
        L.append(f"| 三填充档 rep {rr.start}–{rr.stop - 1} | {wn / 60:.1f} | {ww / 60:.1f} | {100 * (ww / wn - 1):+.1f}% | — |")
    L.append(f"\n本批 run_colab.log 臂墙钟（只含新 rep 2–5 的 120 run）：none {cur.get(NONE, float('nan')):.1f} min、w1w2 {cur.get(W12, float('nan')):.1f} min"
             f"（{100 * (cur[W12] / cur[NONE] - 1):+.1f}%）\n")

    # ------------------------------------------------------------ 4 W1/W2 触发
    L.append("## 4. W1 / W2 触发（guardrail_events.jsonl，按行 guard_events_lines 切片；括号 = 有该事件的 run 数；"
             "fold/hard 的 iter1 = detail.iteration == 1，即首次生成前）\n")
    L.append("| 臂 | level | runs | context_soft | context_hard（iter1 / 后） | context_guard_fold（iter1 / 后） | step_compaction（按 detail.level） | step_readback_exempt | 事件 session 字段与切片一致 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for arm in (NONE, W12):
        for lv in FILL:
            rs = [r for r in G[arm]["records"] if int(r["level"]) == lv]
            c, runs = Counter(), Counter()
            fold1 = foldn = hard1 = hardn = 0
            sc = Counter()
            mism = 0
            for r in rs:
                seg = O.seg(r, evs[arm])
                mism += sum(1 for e in seg if e.get("session") != O.sess_name(r))
                ts = Counter(e["type"] for e in seg)
                c.update(ts)
                runs.update({t: 1 for t in ts})
                for e in seg:
                    d = e.get("detail") or {}
                    if e["type"] == "context_guard_fold":
                        fold1 += d.get("iteration") == 1
                        foldn += d.get("iteration") != 1
                    if e["type"] == "context_hard":
                        hard1 += d.get("iteration") == 1
                        hardn += d.get("iteration") != 1
                    if e["type"] == "step_compaction":
                        sc[d.get("level")] += 1
            # 交叉核对：按 session 字段数事件总量
            names = {O.sess_name(r) for r in rs}
            by_sess = sum(1 for e in evs[arm] if e.get("session") in names)
            seg_tot = sum(len(O.seg(r, evs[arm])) for r in rs)
            L.append(f"| {arm} | {lv} | {len(rs)} | {c['context_soft']}（{runs['context_soft']}） | {hard1} / {hardn}（{runs['context_hard']}） | "
                     f"{fold1} / {foldn}（{runs['context_guard_fold']}） | {c['step_compaction']}（{runs['step_compaction']}）"
                     f"{dict(sorted(sc.items())) if sc else ''} | {c['step_readback_exempt']}（{runs['step_readback_exempt']}） | "
                     f"切片 {seg_tot} / session {by_sess}，不符 {mism} |")
    L.append("")
    # 每 rep 段（旧 rep 0–1 vs 新 rep 2–5）的 57K 首生成前 fold 与首 prompt
    for arm in (W12,):
        for lv in (49152, 57344):
            for rr in (range(0, 2), range(2, 6)):
                rs = [r for r in G[arm]["records"] if int(r["level"]) == lv and int(r["rep"]) in rr]
                f1 = sum(1 for r in rs if any(e["type"] == "context_guard_fold" and (e.get("detail") or {}).get("iteration") == 1
                                              for e in O.seg(r, evs[arm])))
                f0 = [r["gens"][0]["prompt_tokens"] for r in rs]
                L.append(f"- {arm} L{lv} rep {rr.start}–{rr.stop - 1}：首生成前 fold 的 run {f1}/{len(rs)}；首生成 prompt 中位 {med(f0):,.0f}（{min(f0):,}–{max(f0):,}）")
    L.append("")
    out = "\n".join(L) + "\n"
    if a.out:
        Path(a.out).write_text(out, encoding="utf-8")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
