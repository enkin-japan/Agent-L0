"""lc-hard-004 / lc-hard-008 L0 逐 run 证据：p2b（T1-4 写后验证门控改文案后）对 p2（改前），none / full 臂各 6 rep。

  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/data/context-knee-20261108-p2b/item004_008_evidence.py \
      [--out benchmarks/data/context-knee-20261108-p2b/item004_008_evidence.md]

只读 transcripts.jsonl / guardrail_events.jsonl（两批 + 直调参照 qwen-current）与题面 max_steps。CPU、标准库。
口径：
- 步数 = score.steps（longchain 评分器：function 调用总数）；预算 = 题面 expect.max_steps。
- 写后回读 = read_file / file_extract 的 path 在本 run 之前已被 write_file / edit_file / append_file 写过（写之后的读）；
  shell 回读 = run_shell_command 的命令串里出现此前已写过的路径（cat / grep / diff 等，按子串判，偏宽）。
- 纯步数败 = 判败理由只有「步数超限」（state_ok、final_text_ok 都真、未 aborted）。
- verify_nudge = 本 run guard_events_lines 切片里 type == verify_nudge 的事件数（编排器写后验证推回）。
- 收尾「re-read」= turn_response 含 re-read / re-reading（不分大小写）。
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "benchmarks" / "data"
BATCHES = [("p2", DATA / "context-knee-20261107-p2"), ("p2b", DATA / "context-knee-20261108-p2b")]
REF = DATA / "context-knee-20261031" / "qwen-current"
ARMS = ["orch-turn-none", "orch-turn-full"]
ITEMS = ["lc-hard-004", "lc-hard-008"]
WRITES = {"write_file", "edit_file", "append_file"}
READS = {"read_file", "file_extract"}


def budgets() -> Dict[str, int]:
    out = {}
    for ln in (ROOT / "backend/benchmarks/model_eval/items/longchain/hard.jsonl").read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            d = json.loads(ln)
            out[d["id"]] = d["expect"].get("max_steps")
    return out


def calls(r: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [c for t in r.get("turns") or [] for c in (t.get("tool_calls") or []) if c.get("type", "function") == "function"]


def rereads(r: Dict[str, Any]) -> (int, int):
    written: set = set()
    rd = sh = 0
    for c in calls(r):
        a = c.get("arguments") if isinstance(c.get("arguments"), dict) else {}
        nm = c.get("name")
        if nm in READS and a.get("path") in written:
            rd += 1
        elif nm == "run_shell_command" and any(w in str(a.get("command") or "") for w in written):
            sh += 1
        if nm in WRITES and a.get("path"):
            written.add(a["path"])
    return rd, sh


def load(d: Path) -> List[Dict[str, Any]]:
    return [json.loads(x) for x in (d / "transcripts.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def events(d: Path) -> Optional[List[Dict[str, Any]]]:
    p = d / "guardrail_events.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.is_file() else None


def nudges(r, ev) -> Optional[int]:
    gl = r.get("guard_events_lines")
    if ev is None or not gl:
        return None
    return sum(1 for e in ev[gl[0]:gl[1]] if e.get("type") == "verify_nudge")


def mode(xs):
    c = Counter(xs).most_common()
    return "—" if not c else "/".join(str(k) for k, v in c if v == c[0][1])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DATA / "context-knee-20261108-p2b" / "item004_008_evidence.md"))
    a = ap.parse_args(argv)
    bud = budgets()
    L = ["# lc-hard-004 / lc-hard-008 L0 逐 run：p2b（写后验证门控改文案 09c084f1 后）对 p2（改前）",
         "", "_脚本：`benchmarks/data/context-knee-20261108-p2b/item004_008_evidence.py`（口径见脚本头）；同 KNEE_SEED=1107、同 (item, rep) 种子。_", ""]
    ref = [r for r in load(REF) if r["item_id"] in ITEMS and int(r["level"]) == 0]
    L.append("直调参照 qwen-current L0：" + "；".join(
        f"{r['item_id']} r{r['rep']} 步数 {r['score'].get('steps')} {'过' if r['score'].get('pass') else '败'} 冗余 {r['score'].get('redundant_calls')}"
        for r in sorted(ref, key=lambda x: (x["item_id"], int(x["rep"])))) + "；预算 " + "、".join(f"{i} {bud[i]}" for i in ITEMS) + "。")
    L.append("")
    L.append("## 汇总（每格 6 run）\n")
    L.append("| 题 | 臂 | 批 | 过 | 步数众数 | 步数中位 | ≤预算 run | 纯步数败 | state_ok & final_text_ok | 写后回读 合计 / 众数 | 有写后回读的 run | shell 回读 合计 | redundant_calls 众数 | verify_nudge 合计 | 收尾含 re-read |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    detail = []
    for item in ITEMS:
        for arm in ARMS:
            for bn, bd in BATCHES:
                ev = events(bd / arm)
                rs = sorted((r for r in load(bd / arm) if r["item_id"] == item and int(r["level"]) == 0), key=lambda x: int(x["rep"]))
                rows = []
                for r in rs:
                    s = r["score"]
                    rd, sh = rereads(r)
                    why = [str(w) for w in s.get("why") or []]
                    pure = bool(why) and all(w.startswith("步数超限") for w in why)
                    rows.append(dict(rep=int(r["rep"]), steps=s.get("steps"), ok=bool(s.get("pass")), st=s.get("state_ok"), ft=s.get("final_text_ok"),
                                     ab=s.get("aborted"), rd=rd, sh=sh, red=s.get("redundant_calls"), nu=nudges(r, ev), pure=pure,
                                     rr=bool(re.search(r"re-?read", str(r.get("turn_response") or ""), re.I)), why=why))
                st = sorted(x["steps"] for x in rows)
                L.append(f"| {item} | {arm.replace('orch-turn-', '')} | {bn} | {sum(x['ok'] for x in rows)}/{len(rows)} | {mode(st)} | "
                         f"{st[len(st) // 2] if st else '—'} | {sum(1 for x in rows if x['steps'] <= bud[item])} | {sum(x['pure'] for x in rows)} | "
                         f"{sum(1 for x in rows if x['st'] and x['ft'])} | {sum(x['rd'] for x in rows)} / {mode([x['rd'] for x in rows])} | {sum(1 for x in rows if x['rd'])} | "
                         f"{sum(x['sh'] for x in rows)} | {mode([x['red'] for x in rows])} | "
                         f"{'—' if any(x['nu'] is None for x in rows) else sum(x['nu'] for x in rows)} | {sum(x['rr'] for x in rows)} |")
                for x in rows:
                    detail.append(f"| {item} | {arm.replace('orch-turn-', '')} | {bn} | r{x['rep']} | {x['steps']} / {bud[item]} | {'过' if x['ok'] else '败'} | "
                                  f"{x['st']} / {x['ft']} | {x['rd']} | {x['sh']} | {x['red']} | {x['nu']} | {'；'.join(x['why']) or '—'} |")
    L.append("")
    L.append("## 附：全 10 题、两批同名臂 × 档（verify_nudge 事件 = guard_events_lines 切片；写后回读口径同上）\n")
    L.append("| 臂 | level | 批 | run | verify_nudge 事件 | 有 verify_nudge 的 run | 写后回读 合计 | 有写后回读的 run |")
    L.append("|---|---|---|---|---|---|---|---|")
    for arm in ["orch-turn-none", "orch-turn-w1", "orch-turn-w1w2", "orch-turn-full"]:
        for lv in (0, 32768, 49152, 57344):
            for bn, bd in BATCHES:
                ev = events(bd / arm)
                rs = [r for r in load(bd / arm) if int(r["level"]) == lv]
                nu = [nudges(r, ev) for r in rs]
                rd = [rereads(r)[0] for r in rs]
                L.append(f"| {arm.replace('orch-turn-', '')} | {lv} | {bn} | {len(rs)} | "
                         f"{'—' if None in nu else sum(nu)} | {'—' if None in nu else sum(1 for x in nu if x)} | {sum(rd)} | {sum(1 for x in rd if x)} |")
    L.append("")
    for bn, bd in BATCHES:
        cnt = Counter()
        for arm in ["orch-turn-none", "orch-turn-w1", "orch-turn-w1w2", "orch-turn-full"]:
            ev = events(bd / arm)
            cnt.update(r["item_id"] for r in load(bd / arm) if nudges(r, ev))
        L.append(f"- {bn} 有 verify_nudge 的 run 按题（四臂全档合计）：" + "、".join(f"{k} {v}" for k, v in sorted(cnt.items()))
                 + f"；合计 {sum(cnt.values())} run、{len(cnt)} 题")
    L.append("")
    L.append("## 逐 run\n")
    L.append("| 题 | 臂 | 批 | rep | 步数 / 预算 | 判 | state_ok / final_text_ok | 写后回读 | shell 回读 | redundant_calls | verify_nudge | 判败理由 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    L += detail
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
