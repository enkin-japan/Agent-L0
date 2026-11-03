"""D47 验收批次（trace-d47-both）对 D45 批 trace-d45-both 的复算（只读，CPU、标准库）。

  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/summary_trace/analyze_d47.py \
      benchmarks/data/context-knee-20261103-d47 benchmarks/data/context-knee-20261103-d45

输出 markdown 的「自动部分」各节（A–E），ANALYSIS.md 的自动部分即本脚本 stdout 原样。
读取：两目录下 trace-*-both/{transcripts.jsonl, meta.json, guardrail_events.jsonl}，以及每对 turn 1 会话目录
sessions/<item>-r<rep>-<hash>-t1/history.jsonl（只用来取 turn 1 的调用序列与用户原文）。
口径与 analyze_d45.py 相同（复用其 load / paired / cost / per_item / METRICS / ci_of / contrast / section）：
- 配对 = 同目录同 (item, rep) 的 pre / post 两行；error 行不进配对。
- 重复功 = repeated + blocked；Δ = POST − PRE，先在 (题, rep) 上求差再按题平均。
- 跨批差（d47 − d45）= 按题求「该题各 rep 均值」之差再按题平均；95% CI = 题级 bootstrap（B=1000，seed 0）。
- turn 1 一致：宽口径 = analyze_d45 同法（turn1_env_state 与 turn1_calls 计数都相同）；
  调用序列相同 = turn 1 的 (name, args sort_keys JSON) 调用序列逐条相同（取自 t1 会话 history.jsonl，不要求宽口径也成立）；
  完整响应逐字相同 = 调用序列相同的更严子集，t1 history.jsonl 的 response 列表逐条 (role, content, tool_calls) 相同
  （忽略 thoughts 自由文本——那是模型每次生成都会变的解释性文字，不是可比较的结构化输出）。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_d45 import (METRICS, ci_of, ci_s, contrast, cost, load, med, paired,  # noqa: E402
                         per_item, quant, section, why_cat)

TS_HDR = "[Task state at end of turn]"
SCR = "[Scratchpad]"
TS_CAP = 3000  # compactor.SUMMARY_TASK_STATE_CHARS
KANA = re.compile(r"[぀-ヿ]")
HDR_RE = re.compile(r"^\[[A-Z][A-Za-z ]+\]", re.M)


def ts_segment(si: str) -> Optional[str]:
    """summary_input 里最后一个 [Task state at end of turn] 块的正文（块头之后到串尾；compactor 把它放在该轮最后）。"""
    if not si or TS_HDR not in si:
        return None
    return si.rsplit(TS_HDR + "\n", 1)[-1] if (TS_HDR + "\n") in si else si.rsplit(TS_HDR, 1)[-1]


def scratch_values(seg: str) -> List[str]:
    """[Scratchpad] 节下的各行（去掉行首 `- ` / 序号），到下一个节头为止。"""
    m = re.search(re.escape(SCR) + r"[ \t]*\n?(.*?)(?=^\[[A-Z][A-Za-z ]+\]|\Z)", seg, re.S | re.M)
    if not m:
        return []
    out = []
    for ln in m.group(1).splitlines():
        v = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", ln).strip()
        if v:
            out.append(v)
    return out


def t1_dir(batch: Path, r: Dict[str, Any]) -> Path:
    p = Path(r.get("turn1_session_dir") or "")
    if p.is_dir():
        return p
    return batch / "sessions" / p.name


def t1_record(batch: Path, r: Dict[str, Any]) -> Dict[str, Any]:
    f = t1_dir(batch, r) / "history.jsonl"
    recs = [json.loads(ln) for ln in f.open(encoding="utf-8") if ln.strip()]
    if len(recs) != 1:
        raise SystemExit(f"{f}: 期望 1 条轮次记录，实为 {len(recs)}")
    return recs[0]


def t1_call_seq(rec: Dict[str, Any]) -> List[Tuple[str, str]]:
    return [(str(c.get("name")), json.dumps(c.get("arguments"), sort_keys=True, ensure_ascii=False))
            for m in rec.get("response") or [] if m.get("role") == "assistant"
            for c in (m.get("tool_calls") or []) if isinstance(c, dict)]


def t1_full_eq(ra: Dict[str, Any], rb: Dict[str, Any]) -> bool:
    """t1 history.jsonl 的 response 列表逐条 (role, content, tool_calls) 相同（忽略 thoughts）。"""
    strip = lambda h: [(m.get("role"), m.get("content"), json.dumps(m.get("tool_calls"), sort_keys=True, ensure_ascii=False))
                        for m in h]
    return strip(ra.get("response") or []) == strip(rb.get("response") or [])


def main(argv: List[str]) -> int:
    d47, d45 = Path(argv[0]), Path(argv[1])
    dirs = {"d47-both": d47 / "trace-d47-both", "d45-both": d45 / "trace-d45-both"}
    raw = {a: load(d) for a, d in dirs.items()}
    arms = {a: paired(r) for a, r in raw.items()}
    recs = {a: {k: t1_record(dirs[a], v["pre"]) for k, v in arms[a].items()} for a in arms}
    P = print
    P("## 自动部分（`benchmarks/summary_trace/analyze_d47.py`，seed 0，B=1000）\n")

    # ---------------- A ----------------
    P("### A. 完整性（transcripts.jsonl / meta.json / guardrail_events.jsonl / t1 会话 history.jsonl）\n")
    P("| 批 | 目录 | 行 | error 行 | 配对 | 空摘要（POST） | turn1_status | turn1_plan_built | turn1_record_has_task_state | cont_status PRE / POST | stamp | meta 除 stamp 外与对照批相同 |")
    P("|---|---|---|---|---|---|---|---|---|---|---|---|")
    metas = {a: json.loads((d / "meta.json").read_text(encoding="utf-8")) for a, d in dirs.items()}
    strip = lambda m: {k: v for k, v in m.items() if k != "stamp"}
    same_meta = strip(metas["d47-both"]) == strip(metas["d45-both"])
    for a, p in arms.items():
        pre = [v["pre"] for v in p.values()]; post = [v["post"] for v in p.values()]
        P(f"| {a} | `{dirs[a]}` | {len(raw[a])} | {sum('error' in r for r in raw[a])} | {len(p)} | "
          f"{sum(not (r.get('summary') or '').strip() for r in post)} | {dict(Counter(r.get('turn1_status') for r in pre))} | "
          f"{dict(Counter(str(r.get('turn1_plan_built')) for r in pre))} | {dict(Counter(str(r.get('turn1_record_has_task_state')) for r in pre))} | "
          f"{dict(Counter(r.get('cont_status') for r in pre))} / {dict(Counter(r.get('cont_status') for r in post))} | "
          f"{metas[a].get('stamp')} | {same_meta} |")
    P()
    P("guardrail_events.jsonl 事件类型计数（turn 1 会话 `-t1` / 续跑会话 `-pre` `-post`）：\n")
    P("| 批 | 事件总数 | 按 (type, 会话后缀) |")
    P("|---|---|---|")
    for a, d in dirs.items():
        ev = [json.loads(ln) for ln in (d / "guardrail_events.jsonl").open(encoding="utf-8") if ln.strip()]
        c = Counter((e.get("type"), str(e.get("session", "")).rsplit("-", 1)[-1]) for e in ev)
        P(f"| {a} | {len(ev)} | {', '.join(f'{t}@{s}={n}' for (t, s), n in sorted(c.items()))} |")
    P()

    # ---------------- B ----------------
    P("### B. D47 直接效应（本批内；turn 1 每对一次，取 PRE 行）\n")
    P("| 批 | remember 调用总数 | remember ≥1 的对 | 有便签对（turn1_task_state_has_scratchpad） | turn1_task_state_chars min/p25/中位/p75/max | "
      f"摘要器输入含 `{TS_HDR}` | 其中含 `{SCR}` | 其中含 `[Plan]` | cont_live_task PRE / POST |")
    P("|---|---|---|---|---|---|---|---|---|")
    for a, p in arms.items():
        pre = [v["pre"] for v in p.values()]; post = [v["post"] for v in p.values()]
        rc = [r.get("turn1_remember_calls") for r in pre]
        has_rc = any(x is not None for x in rc)
        sp = [r.get("turn1_task_state_has_scratchpad") for r in pre]
        segs = [ts_segment(r.get("summary_input") or "") for r in post]
        segs_p = [s for s in segs if s is not None]
        P(f"| {a} | {sum(x or 0 for x in rc) if has_rc else '缺键'} | {sum((x or 0) >= 1 for x in rc) if has_rc else '缺键'} | "
          f"{sum(bool(x) for x in sp) if any(x is not None for x in sp) else '缺键'} | "
          f"{quant([r.get('turn1_task_state_chars') for r in pre]) if any('turn1_task_state_chars' in r for r in pre) else '缺键'} | "
          f"{len(segs_p)}/{len(post)} | {sum(SCR in s for s in segs_p)} | {sum('[Plan]' in s for s in segs_p)} | "
          f"{dict(Counter(str(r.get('cont_live_task')) for r in pre))} / {dict(Counter(str(r.get('cont_live_task')) for r in post))} |")
    P()
    # 便签值逐字进 [Key values]
    p47 = arms["d47-both"]
    sv = [(k, scratch_values(ts_segment(v["post"].get("summary_input") or "") or "")) for k, v in sorted(p47.items())]
    sv = [(k, vals) for k, vals in sv if vals]
    if sv:
        hit = sum(sum(x in section(p47[k]["post"].get("summary") or "", "Key values") for x in vals) for k, vals in sv)
        tot = sum(len(vals) for _, vals in sv)
        P(f"- 便签值逐字进 POST 摘要 [Key values]：{hit}/{tot} 个值，{len(sv)} 对有便签。")
    else:
        P("- 便签值逐字进 POST 摘要 [Key values]：**不可算**——本批没有一对的摘要器输入带 `[Scratchpad]`（分母为 0，不是命中率 0）。")
    # task_state 段的构成
    comp = Counter()
    for k, v in p47.items():
        seg = ts_segment(v["post"].get("summary_input") or "")
        if seg is None:
            comp["无段"] += 1
            continue
        heads = tuple(h.strip("[]") for h in HDR_RE.findall(seg))
        comp["+".join(heads) or "无节头"] += 1
    P(f"- d47 摘要器输入 task_state 段的节头构成（按对）：{dict(comp)}")
    goal_eq = Counter()
    for k, v in p47.items():
        seg = ts_segment(v["post"].get("summary_input") or "") or ""
        g = section(seg, "Goal")
        goal_eq[g == (recs["d47-both"][k].get("prompt") or "").strip()] += 1
    P(f"- d47 task_state 段 [Goal] 与 turn 1 用户原文（t1 history.jsonl `prompt`）逐字相同：{dict(goal_eq)}")
    P()
    P("POST 摘要 [Goal] 节（D47 的间接效应：摘要器是否逐字抄目标）：\n")
    P("| 批 | 摘要以 `[Goal]` 开头 | [Goal] 非空 | [Goal] == turn 1 用户原文（逐字） | [Goal] 含用户原文（子串） | [Goal] 字符中位 |")
    P("|---|---|---|---|---|---|")
    for a, p in arms.items():
        gs = [(section(v["post"].get("summary") or "", "Goal"), (recs[a][k].get("prompt") or "").strip()) for k, v in p.items()]
        P(f"| {a} | {sum((v['post'].get('summary') or '').lstrip().startswith('[Goal]') for v in p.values())}/{len(p)} | "
          f"{sum(bool(g) for g, _ in gs)} | {sum(g == u for g, u in gs)} | {sum(bool(u) and u in g for g, u in gs)} | {med([len(g) for g, _ in gs])} |")
    P()
    P("分层（探索性，非预注册）：POST 摘要 [Goal] 是否逐字等于用户原文 → POST pass / Δ重复功均：\n")
    P("| 批 | 层 | 对 | POST pass | PRE pass | Δ重复功均 |")
    P("|---|---|---|---|---|---|")
    for a, p in arms.items():
        for val in (True, False):
            ks = [k for k, v in p.items()
                  if (section(v["post"].get("summary") or "", "Goal") == (recs[a][k].get("prompt") or "").strip()) is val]
            if ks:
                P(f"| {a} | goal_verbatim={val} | {len(ks)} | {sum(bool(p[k]['post']['score']) for k in ks)} | "
                  f"{sum(bool(p[k]['pre']['score']) for k in ks)} | {sum(cost(p[k]['post']) - cost(p[k]['pre']) for k in ks) / len(ks):+.2f} |")
    P()

    # ---------------- C ----------------
    P("### C. 每批汇总与跨批差（d47 − d45，按题；正 = d47 更高；pass 单位百分点）\n")
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
    keys = sorted(set(arms["d47-both"]) & set(arms["d45-both"]))
    loose = [k for k in keys if arms["d47-both"][k]["pre"]["turn1_env_state"] == arms["d45-both"][k]["pre"]["turn1_env_state"]
             and arms["d47-both"][k]["pre"]["turn1_calls"] == arms["d45-both"][k]["pre"]["turn1_calls"]]
    seq_same = [k for k in keys if t1_call_seq(recs["d47-both"][k]) == t1_call_seq(recs["d45-both"][k])]
    full_eq = [k for k in keys if t1_full_eq(recs["d47-both"][k], recs["d45-both"][k])]
    diff_status = [k for k in seq_same if arms["d47-both"][k]["pre"]["turn1_status"] != arms["d45-both"][k]["pre"]["turn1_status"]]
    seq_same_status = [k for k in seq_same if k not in diff_status]
    P(f"- turn 1 一致（同 (题, rep)）：宽口径（env 快照 + 调用数）{len(loose)}/{len(keys)}；调用序列相同 {len(seq_same)}/{len(keys)}；"
      f"完整 turn 1 响应逐字相同（t1 history.jsonl 的 response 列表相等）{len(full_eq)}/{len(keys)}。")
    P(f"- 调用序列相同的对：{', '.join(f'{i.replace('lc-hard-', '')}r{r}' for i, r in seq_same) or '无'}；"
      f"其中 turn1_status 不同：{', '.join(f'{i.replace('lc-hard-', '')}r{r}' for i, r in diff_status) or '无'}。")
    P(f"- 完整响应逐字相同的对：{', '.join(f'{i.replace('lc-hard-', '')}r{r}' for i, r in full_eq) or '无'}。")
    P()
    w = {"d47-both": 1, "d45-both": -1}

    def sub(ks):
        return {a: {k: arms[a][k] for k in ks} for a in arms}

    P("| 子集 | 对 / 题 | POST pass 差 | PRE pass 差 | POST−PRE pass gap 之差 | Δ重复功之差 | 子集内 POST pass d47 / d45 | 子集内 Δ重复功均 d47 / d45 |")
    P("|---|---|---|---|---|---|---|---|")
    for name, ks in (("全部", keys), ("turn 1 宽口径一致", loose), ("调用序列相同", seq_same),
                     ("调用序列相同且 turn1_status 相同", seq_same_status)):
        if not ks:
            P(f"| {name} | 0 | — | — | — | — | — | — |")
            continue
        s = sub(ks)
        nit = len({i for i, _ in ks})
        po = {a: sum(bool(s[a][k]['post']['score']) for k in ks) for a in s}
        dc = {a: sum(cost(s[a][k]['post']) - cost(s[a][k]['pre']) for k in ks) / len(ks) for a in s}
        P(f"| {name} | {len(ks)} / {nit} | {ci_s(contrast(s, w, 'pass_post'), pct=True)} | {ci_s(contrast(s, w, 'pass_pre'), pct=True)} | "
          f"{ci_s(contrast(s, w, 'pass_gap'), pct=True)} | {ci_s(contrast(s, w, 'dcost'))} | "
          f"{po['d47-both']}/{len(ks)} / {po['d45-both']}/{len(ks)} | {dc['d47-both']:+.2f} / {dc['d45-both']:+.2f} |")
    P()
    P("按题 POST pass（通过 rep / 3）与 PRE pass：\n")
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

    # ---------------- D ----------------
    P(f"### D. 预算（POST summary_input_chars；task_state 段长度，截断上限 {TS_CAP} 字 + `…`）\n")
    P("| 批 | summary_input_chars min/p25/中位/p75/max | task_state 段字符 min/p25/中位/p75/max | 段 > 3000（compactor 截断触发） | [Goal] 以 `…` 结尾（task_state.SUMMARY_GOAL_CHARS=300 截断） | 输入 > 24000 字 |")
    P("|---|---|---|---|---|---|")
    for a, p in arms.items():
        post = [v["post"] for v in p.values()]
        segs = [ts_segment(r.get("summary_input") or "") for r in post]
        segs = [s for s in segs if s is not None]
        P(f"| {a} | {quant([r.get('summary_input_chars') for r in post])} | {quant([len(s) for s in segs]) if segs else '—（无段）'} | "
          f"{sum(len(s) > TS_CAP for s in segs) if segs else '—'} | "
          f"{sum(section(s, 'Goal').endswith('…') for s in segs) if segs else '—'} | "
          f"{sum((r.get('summary_input_chars') or 0) > 24000 for r in post)} |")
    P()

    # ---------------- E ----------------
    P("### E. 语言漂移（假名 U+3040–30FF 出现；题面 lang 均为 zh/en）\n")
    JA_TERMS = ("日语", "日本語", "Japanese", "日文")
    ATTR_TERMS = ("用户", "匹配")
    P("| 批 | 题面 lang | 摘要器输入含假名（POST） | POST 摘要含假名 | 摘要 [User] 节提到日语 | 摘要 [Decisions] 节提到日语误归因 | cont_response 含假名 PRE / POST |")
    P("|---|---|---|---|---|---|---|")
    for a, p in arms.items():
        pre = [v["pre"] for v in p.values()]; post = [v["post"] for v in p.values()]
        P(f"| {a} | {dict(Counter(r.get('lang') for r in pre))} | {sum(bool(KANA.search(r.get('summary_input') or '')) for r in post)} | "
          f"{sum(bool(KANA.search(r.get('summary') or '')) for r in post)} | "
          f"{sum(any(t in section(r.get('summary') or '', 'User') for t in ('日语', '日本語', 'Japanese')) for r in post)} | "
          f"{sum(any(t in section(r.get('summary') or '', 'Decisions') for t in JA_TERMS) and any(t in section(r.get('summary') or '', 'Decisions') for t in ATTR_TERMS) for r in post)} | "
          f"{sum(bool(KANA.search(r.get('cont_response') or '')) for r in pre)} / {sum(bool(KANA.search(r.get('cont_response') or '')) for r in post)} |")
    P()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
