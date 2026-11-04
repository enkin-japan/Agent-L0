"""D47-next (b)+(a) 验收批次（trace-d47b-both）对 D47 批（trace-d47-both）与 D45 批（trace-d45-both）的复算（只读，CPU、标准库）。

  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/summary_trace/analyze_d47b.py \
      benchmarks/data/context-knee-20261104-d47b benchmarks/data/context-knee-20261103-d47 \
      benchmarks/data/context-knee-20261103-d45

输出 markdown 的「自动部分」各节（A–F），ANALYSIS.md 的自动部分即本脚本 stdout 原样。
读取：三目录下 trace-*-both/{transcripts.jsonl, meta.json, guardrail_events.jsonl}，以及每对 turn 1 会话目录
sessions/<item>-r<rep>-<hash>-t1/history.jsonl（turn 1 调用序列、用户原文、no_plan_nudge 文案与其后一步）。
口径与 analyze_d45.py / analyze_d47.py 相同（只 import，不改它们）：
- 配对 = 同目录同 (item, rep) 的 pre / post 两行；error 行不进配对。
- 重复功 = repeated + blocked；Δ = POST − PRE，先在 (题, rep) 上求差再按题平均。
- 跨批差（A − B）= 按题求「该题各 rep 均值」之差再按题平均；95% CI = 题级 bootstrap（B=1000，seed 0）。
  三批后端都是 remote，meta 的 seed 7 不作用于采样：同 (题, rep) 只是标签对齐，不是同一次采样的复现。
- turn 1 一致（逐两批比）：宽口径 = turn1_env_state 与 turn1_calls 都相同；调用序列相同 = analyze_d47.t1_call_seq 相等；
  完整响应逐字相同 = analyze_d47.t1_full_eq。
- task_state 段（摘要器输入里的 `[Task state at end of turn]` 块）在本批通常不存在（D47-next (b)：纯 [Goal] 不盖章），
  缺段的对单列「无段」，段长 / [Goal] 截断等列在没有段时打印「—（无段）」，不是 0。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_d45 import (BATCH_BUDGET, METRICS, ci_of, ci_s, contrast, cost, load, med,  # noqa: E402
                         paired, per_item, quant, section, why_cat)
from analyze_d47 import (HDR_RE, KANA, SCR, TS_CAP, TS_HDR, scratch_values,  # noqa: E402
                         t1_call_seq, t1_full_eq, t1_record, ts_segment)

NUDGE_KEY = "update_plan NOW"
NUDGE_REMEMBER = "first save with remember"
GOAL_CAP = 300  # task_state.SUMMARY_GOAL_CHARS


def quant_med(xs) -> str:
    """像 analyze_d45.quant 的 min/p25/p75/max，但中位换成 statistics.median（经 analyze_d45.med），
    与「去掉 task_state 段后 chars 中位」同一定义，两者才可比（reviewer F1）。"""
    nums = [x for x in xs if isinstance(x, (int, float))]
    if not nums:
        return "—"
    s = sorted(nums)
    q = lambda f: s[min(len(s) - 1, int(round(f * (len(s) - 1))))]
    return f"{s[0]} / {q(.25)} / {med(nums)} / {q(.75)} / {s[-1]}"


def short(k) -> str:
    return f"{k[0].replace('lc-hard-', '')}r{k[1]}"


def after_nudge(rec: Dict[str, Any]) -> List[str]:
    """turn 1 response 里每条 no_plan_nudge（system，含 `update_plan NOW`）之后第一条 assistant 消息的形态。"""
    resp = rec.get("response") or []
    out = []
    for i, m in enumerate(resp):
        if m.get("role") == "system" and NUDGE_KEY in (m.get("content") or ""):
            nxt = [x for x in resp[i + 1:] if x.get("role") == "assistant"][:1]
            if not nxt:
                out.append("<无后续 assistant>")
                continue
            names = [c.get("name") for c in (nxt[0].get("tool_calls") or []) if isinstance(c, dict)]
            out.append(("calls:" + ",".join(sorted(set(names)))) if names else "text")
    return out


def nudge_texts(rec: Dict[str, Any]) -> List[str]:
    return [m.get("content") or "" for m in rec.get("response") or []
            if m.get("role") == "system" and NUDGE_KEY in (m.get("content") or "")]


def main(argv: List[str]) -> int:
    dn, d1, d2 = Path(argv[0]), Path(argv[1]), Path(argv[2])
    dirs = {"d47b-both": dn / "trace-d47b-both", "d47-both": d1 / "trace-d47-both", "d45-both": d2 / "trace-d45-both"}
    NEW, C1, C2 = "d47b-both", "d47-both", "d45-both"
    raw = {a: load(d) for a, d in dirs.items()}
    arms = {a: paired(r) for a, r in raw.items()}
    recs = {a: {k: t1_record(dirs[a], v["pre"]) for k, v in arms[a].items()} for a in arms}
    evs = {a: [json.loads(ln) for ln in (d / "guardrail_events.jsonl").open(encoding="utf-8") if ln.strip()]
           for a, d in dirs.items()}
    metas = {a: json.loads((d / "meta.json").read_text(encoding="utf-8")) for a, d in dirs.items()}
    P = print
    P("## 自动部分（`benchmarks/summary_trace/analyze_d47b.py`，seed 0，B=1000）\n")

    # ---------------- A ----------------
    P("### A. 完整性（transcripts.jsonl / meta.json / guardrail_events.jsonl / t1 会话 history.jsonl）\n")
    strip = lambda m: {k: v for k, v in m.items() if k != "stamp"}
    P("| 批 | 目录 | 行 | error 行 | 配对 | 空摘要（POST） | turn1_status | cont_status PRE / POST | stamp | meta 除 stamp 外与 d47b 相同 |")
    P("|---|---|---|---|---|---|---|---|---|---|")
    for a, p in arms.items():
        pre = [v["pre"] for v in p.values()]; post = [v["post"] for v in p.values()]
        P(f"| {a} | `{dirs[a]}` | {len(raw[a])} | {sum('error' in r for r in raw[a])} | {len(p)} | "
          f"{sum(not (r.get('summary') or '').strip() for r in post)} | {dict(Counter(r.get('turn1_status') for r in pre))} | "
          f"{dict(Counter(r.get('cont_status') for r in pre))} / {dict(Counter(r.get('cont_status') for r in post))} | "
          f"{metas[a].get('stamp')} | {strip(metas[a]) == strip(metas[NEW])} |")
    P()
    P("guardrail_events.jsonl 事件计数（会话后缀 `t1` / `pre` / `post`）：\n")
    P("| 批 | 事件总数 | 按 (type, 会话后缀) |")
    P("|---|---|---|")
    for a in dirs:
        c = Counter((e.get("type"), str(e.get("session", "")).rsplit("-", 1)[-1]) for e in evs[a])
        P(f"| {a} | {len(evs[a])} | {', '.join(f'{t}@{s}={n}' for (t, s), n in sorted(c.items()))} |")
    P()
    P("turn 1 no_plan_nudge 文案（t1 history.jsonl 里含 `update_plan NOW` 的 system 消息；(a) 是否落到数据里）：\n")
    P("| 批 | 含提醒的 t1 会话 | 提醒条数 | 不同文案数 | 含「first save with remember」 |")
    P("|---|---|---|---|---|")
    for a in dirs:
        txt = [t for r in recs[a].values() for t in nudge_texts(r)]
        P(f"| {a} | {sum(bool(nudge_texts(r)) for r in recs[a].values())}/{len(recs[a])} | {len(txt)} | {len(set(txt))} | "
          f"{sum(NUDGE_REMEMBER in t for t in txt)} |")
    P()
    P("- REMEMBER_SCHEMA 描述（(a) 的另一半）不在任何落盘产物里（t1 history.jsonl / journal.jsonl / transcripts 都不记工具声明），"
      "本脚本无法从数据核验它；只能以代码（HEAD 的 `model_visible_text.snap`）为准。")
    P()

    # ---------------- B ----------------
    P("### B. 判据：turn 1 的 remember 调用率（turn 1 每对一次，取 PRE 行；字段来自 run_trace.py:324-337）\n")
    P("| 批 | turn1_remember_calls 总数 | remember ≥1 的对 | turn1_task_state_has_scratchpad | turn1_record_has_task_state | "
      "turn1_task_state_chars min/p25/中位/p75/max | turn1_plan_built | t1 history 里元工具调用（remember / update_plan） |")
    P("|---|---|---|---|---|---|---|---|")
    for a, p in arms.items():
        pre = [v["pre"] for v in p.values()]
        rc = [r.get("turn1_remember_calls") for r in pre]
        has = any(x is not None for x in rc)
        sp = [r.get("turn1_task_state_has_scratchpad") for r in pre]
        names = Counter(n for r in recs[a].values() for n, _ in t1_call_seq(r))
        P(f"| {a} | {sum(x or 0 for x in rc) if has else '缺键'} | {sum((x or 0) >= 1 for x in rc) if has else '缺键'} | "
          f"{sum(bool(x) for x in sp) if any(x is not None for x in sp) else '缺键'} | "
          f"{dict(Counter(str(r.get('turn1_record_has_task_state')) for r in pre))} | "
          f"{quant([r.get('turn1_task_state_chars') for r in pre]) if any('turn1_task_state_chars' in r for r in pre) else '缺键'} | "
          f"{dict(Counter(str(r.get('turn1_plan_built')) for r in pre))} | "
          f"remember={names.get('remember', 0)}, update_plan={names.get('update_plan', 0)} |")
    P()
    P("- 注：d47b 的 `turn1_record_has_task_state` 只在有 plan 或有便签时为 True（纯 [Goal] 不盖章），"
      "与 d47 的 30/30（每轮都盖 [Goal]）按构造不可比。")
    P()
    P("turn 1 每条 no_plan_nudge 之后第一条 assistant 消息的形态（text = 无工具调用直接作答）：\n")
    P("| 批 | 提醒后第一步 | 其中含 remember |")
    P("|---|---|---|")
    for a in dirs:
        aft = Counter(x for r in recs[a].values() for x in after_nudge(r))
        P(f"| {a} | {dict(sorted(aft.items()))} | {sum(n for x, n in aft.items() if 'remember' in x)} |")
    P()
    P("续跑支（PRE / POST 续跑轮）的元工具调用（行字段 meta_calls）与该轮 no_plan_nudge（行字段 guard_events）：\n")
    P("| 批 | 支 | meta_calls 合计 | 有 no_plan_nudge 的行 | 这些行里 meta_calls 含 remember | 有 remember 的行（题 rep: guard_events / score） |")
    P("|---|---|---|---|---|---|")
    for a, p in arms.items():
        for br in ("pre", "post"):
            rows = [(k, v[br]) for k, v in sorted(p.items())]
            mc = Counter()
            for _, r in rows:
                mc.update(r.get("meta_calls") or {})
            nud = [(k, r) for k, r in rows if (r.get("guard_events") or {}).get("no_plan_nudge")]
            rem = [(k, r) for k, r in rows if (r.get("meta_calls") or {}).get("remember")]
            P(f"| {a} | {br} | {dict(mc) or '{}'} | {len(nud)} | {sum(bool((r.get('meta_calls') or {}).get('remember')) for _, r in nud)} | "
              f"{'; '.join(f'{short(k)}: {r.get("guard_events")} / score={r.get("score")}' for k, r in rem) or '—'} |")
    P()

    # ---------------- C ----------------
    P(f"### C. 便签是否进摘要器输入与 POST [Key values]（POST 行 summary_input / summary）\n")
    P(f"| 批 | 摘要器输入含 `{TS_HDR}` | 其中含 `{SCR}` | 其中含 `[Plan]` | 段节头构成（按对） | 便签值逐字进 [Key values] |")
    P("|---|---|---|---|---|---|")
    for a, p in arms.items():
        post = {k: v["post"] for k, v in sorted(p.items())}
        segs = {k: ts_segment(r.get("summary_input") or "") for k, r in post.items()}
        segs_p = {k: s for k, s in segs.items() if s is not None}
        comp = Counter("+".join(h.strip("[]") for h in HDR_RE.findall(s)) or "无节头" for s in segs_p.values())
        comp["无段"] = len(segs) - len(segs_p)
        sv = [(k, scratch_values(s)) for k, s in segs_p.items()]
        sv = [(k, vals) for k, vals in sv if vals]
        if sv:
            hit = sum(sum(x in section(post[k].get("summary") or "", "Key values") for x in vals) for k, vals in sv)
            kv = f"{hit}/{sum(len(v) for _, v in sv)} 个值（{len(sv)} 对有便签）"
        else:
            kv = "**不可算**（0 对带 `[Scratchpad]`，分母为 0）"
        P(f"| {a} | {len(segs_p)}/{len(post)} | {sum(SCR in s for s in segs_p.values())} | "
          f"{sum('[Plan]' in s for s in segs_p.values())} | {dict(c for c in comp.items() if c[1])} | {kv} |")
    P()

    # ---------------- D ----------------
    P("### D. pass 与重复功：每批汇总与跨批差（按题；正 = 前者更高；pass 单位百分点；同 (题, rep) 只是标签对齐）\n")
    P("| 批 | pass PRE | pass POST | POST−PRE pass（题级 95%） | 重复功 PRE→POST 均 | Δ重复功（题级 95%） | 首 prompt 中位 PRE / POST | 摘要字符中位 | turn1_calls 中位 |")
    P("|---|---|---|---|---|---|---|---|---|")
    for a, p in arms.items():
        pre = [v["pre"] for v in p.values()]; post = [v["post"] for v in p.values()]
        n = len(p)
        P(f"| {a} | {sum(bool(r['score']) for r in pre)}/{n} | {sum(bool(r['score']) for r in post)}/{n} | "
          f"{ci_s(ci_of(list(per_item(p, METRICS['pass_gap']).values())), pct=True)} | "
          f"{sum(map(cost, pre))/n:.2f} → {sum(map(cost, post))/n:.2f} | {ci_s(ci_of(list(per_item(p, METRICS['dcost']).values())))} | "
          f"{med([r.get('first_prompt_tokens') for r in pre])} / {med([r.get('first_prompt_tokens') for r in post])} | "
          f"{med([r.get('summary_chars') for r in post])} | {med([r.get('turn1_calls') for r in pre])} |")
    P()
    P("| 对照 | 子集 | 对 / 题 | POST pass 差 | PRE pass 差 | POST−PRE pass gap 之差 | Δ重复功之差 | 子集内 POST pass 前 / 后 | 子集内 Δ重复功均 前 / 后 |")
    P("|---|---|---|---|---|---|---|---|---|")
    for x, y in ((NEW, C1), (NEW, C2), (C1, C2)):
        keys = sorted(set(arms[x]) & set(arms[y]))
        loose = [k for k in keys if arms[x][k]["pre"]["turn1_env_state"] == arms[y][k]["pre"]["turn1_env_state"]
                 and arms[x][k]["pre"]["turn1_calls"] == arms[y][k]["pre"]["turn1_calls"]]
        seq = [k for k in keys if t1_call_seq(recs[x][k]) == t1_call_seq(recs[y][k])]
        seq_st = [k for k in seq if arms[x][k]["pre"]["turn1_status"] == arms[y][k]["pre"]["turn1_status"]]
        w = {x: 1, y: -1}
        for name, ks in (("全部", keys), ("turn 1 宽口径一致", loose), ("调用序列相同", seq),
                         ("调用序列相同且 turn1_status 相同", seq_st)):
            if not ks:
                P(f"| {x} − {y} | {name} | 0 | — | — | — | — | — | — |")
                continue
            s = {a: {k: arms[a][k] for k in ks} for a in (x, y)}
            po = {a: sum(bool(s[a][k]['post']['score']) for k in ks) for a in s}
            dc = {a: sum(cost(s[a][k]['post']) - cost(s[a][k]['pre']) for k in ks) / len(ks) for a in s}
            P(f"| {x} − {y} | {name} | {len(ks)} / {len({i for i, _ in ks})} | {ci_s(contrast(s, w, 'pass_post'), pct=True)} | "
              f"{ci_s(contrast(s, w, 'pass_pre'), pct=True)} | {ci_s(contrast(s, w, 'pass_gap'), pct=True)} | "
              f"{ci_s(contrast(s, w, 'dcost'))} | {po[x]}/{len(ks)} / {po[y]}/{len(ks)} | {dc[x]:+.2f} / {dc[y]:+.2f} |")
    P()
    for x, y in ((NEW, C1), (NEW, C2)):
        keys = sorted(set(arms[x]) & set(arms[y]))
        seq = [k for k in keys if t1_call_seq(recs[x][k]) == t1_call_seq(recs[y][k])]
        full = [k for k in keys if t1_full_eq(recs[x][k], recs[y][k])]
        P(f"- {x} vs {y}：调用序列相同 {len(seq)}/{len(keys)}（{', '.join(map(short, seq)) or '无'}）；"
          f"完整 turn 1 响应逐字相同 {len(full)}/{len(keys)}（{', '.join(map(short, full)) or '无'}）。")
    P()
    P("按题 POST / PRE pass（通过 rep / 3）：\n")
    items = sorted({it for p in arms.values() for it, _ in p})
    P("| 批 | 支 | " + " | ".join(i.replace("lc-hard-", "") for i in items) + " |")
    P("|---|---|" + "---|" * len(items))
    for a, p in arms.items():
        for br in ("post", "pre"):
            P(f"| {a} | {br} | " + " | ".join(str(sum(bool(v[br]['score']) for (it, _), v in p.items() if it == i)) for i in items) + " |")
    P()
    P("失败原因（score_why 分类计数，一行可多因）：\n")
    P("| 批 | 支 | 步数超限 | 回合上限耗尽 | 无收尾答复 | env 判错 | 不计「步数超限」的 pass |")
    P("|---|---|---|---|---|---|---|")
    for a, p in arms.items():
        for br in ("pre", "post"):
            rows = [v[br] for v in p.values()]
            c = Counter(why_cat(x) for r in rows for x in (r.get("score_why") or []))
            env = sum(n for k, n in c.items() if k.startswith("env:"))
            ok = sum(all(why_cat(x) == "步数超限" for x in (r.get("score_why") or [])) for r in rows)
            P(f"| {a} | {br} | {c['步数超限']} | {c['回合上限耗尽']} | {c['无收尾答复']} | {env} | {ok}/{len(rows)} |")
    P()

    # ---------------- E ----------------
    P(f"### E. 摘要器输入形态与预算（POST summary_input_chars；task_state 段截断上限 {TS_CAP} 字；[Goal] 截断 {GOAL_CAP} 字 + `…`）\n")
    P("| 批 | summary_input_chars min/p25/中位（statistics.median）/p75/max | 含 task_state 段 | 段字符 min/p25/中位/p75/max | 段 > 3000 | 段内 [Goal] 以 `…` 结尾 | "
      "去掉 task_state 段后 chars 中位（statistics.median） | 输入 > 24000 | summary_chars 中位 | summary_tokens_est 中位 |")
    P("|---|---|---|---|---|---|---|---|---|---|")
    for a, p in arms.items():
        post = [v["post"] for v in p.values()]
        si = [r.get("summary_input") or "" for r in post]
        segs = [s for s in (ts_segment(x) for x in si) if s is not None]
        # 与 ts_segment 同一套「按最后一次 TS_HDR 切」的 rsplit 约定（去掉的是 ts_segment 会取走的那一段）：
        bare = [len((x.rsplit(TS_HDR + "\n", 1)[0] if (TS_HDR + "\n") in x else x.rsplit(TS_HDR, 1)[0]).rstrip("\n"))
                if TS_HDR in x else len(x) for x in si]
        P(f"| {a} | {quant_med([r.get('summary_input_chars') for r in post])} | {len(segs)}/{len(post)} | "
          f"{quant([len(s) for s in segs]) if segs else '—（无段）'} | {sum(len(s) > TS_CAP for s in segs) if segs else '—'} | "
          f"{sum(section(s, 'Goal').endswith('…') for s in segs) if segs else '—'} | {med(bare)} | "
          f"{sum((r.get('summary_input_chars') or 0) > BATCH_BUDGET for r in post)} | {med([r.get('summary_chars') for r in post])} | "
          f"{med([r.get('summary_tokens_est') for r in post])} |")
    P()
    P("- `summary_input_chars` 与 `len(summary_input)` 是否处处相等："
      + "；".join(f"{a} {sum((v['post'].get('summary_input_chars') == len(v['post'].get('summary_input') or '')) for v in p.values())}/{len(p)}"
                 for a, p in arms.items()) + "。")
    P()
    P("POST 摘要 [Goal] 节（D47 的间接效应在去掉 [Goal] 段后是否回落）：\n")
    P("| 批 | 摘要以 `[Goal]` 开头 | [Goal] 非空 | [Goal] == turn 1 用户原文（逐字） | [Goal] 含用户原文（子串） | [Goal] 字符中位 |")
    P("|---|---|---|---|---|---|")
    for a, p in arms.items():
        gs = [(section(v["post"].get("summary") or "", "Goal"), (recs[a][k].get("prompt") or "").strip()) for k, v in p.items()]
        P(f"| {a} | {sum((v['post'].get('summary') or '').lstrip().startswith('[Goal]') for v in p.values())}/{len(p)} | "
          f"{sum(bool(g) for g, _ in gs)} | {sum(g == u for g, u in gs)} | {sum(bool(u) and u in g for g, u in gs)} | {med([len(g) for g, _ in gs])} |")
    P()

    # ---------------- F ----------------
    P("### F. 异常与漂移（error 行、空摘要、假名 U+3040–30FF）\n")
    P("| 批 | error 行 | 空摘要（POST） | 题面 lang | 摘要器输入含假名（POST） | POST 摘要含假名 | turn 1 最终答复含假名（t1 history 末条 assistant 文本） | cont_response 含假名 PRE / POST |")
    P("|---|---|---|---|---|---|---|---|")
    for a, p in arms.items():
        pre = [v["pre"] for v in p.values()]; post = [v["post"] for v in p.values()]
        t1_last = []
        for r in recs[a].values():
            txt = [m.get("content") for m in r.get("response") or [] if m.get("role") == "assistant" and isinstance(m.get("content"), str)]
            t1_last.append(txt[-1] if txt else "")
        P(f"| {a} | {sum('error' in r for r in raw[a])} | {sum(not (r.get('summary') or '').strip() for r in post)} | "
          f"{dict(Counter(r.get('lang') for r in pre))} | {sum(bool(KANA.search(r.get('summary_input') or '')) for r in post)} | "
          f"{sum(bool(KANA.search(r.get('summary') or '')) for r in post)} | {sum(bool(KANA.search(t)) for t in t1_last)} | "
          f"{sum(bool(KANA.search(r.get('cont_response') or '')) for r in pre)} / {sum(bool(KANA.search(r.get('cont_response') or '')) for r in post)} |")
    P()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
