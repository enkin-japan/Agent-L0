# D43 (a) b2 前置复算（analyst，2026-11-04；结论见 docs/plans/2026-10-31-context-management-plan.md §6 D43）：卡值来源（回读 / 桩首部 / 原文）与带中缝的回读
# 重跑（仓库根）：python3 -I benchmarks/context_knee/d43_pre/a3_sources.py benchmarks/data/context-knee-20261102-b2 > a3_out.txt
# 原样搬自会话 scratchpad，只加这三行；结论数字以当日输出为准，不随 harness 改动更新。
"""D43 fact A3: for each card that was read back or answered — was its value already visible in the 200-char
stub head? plus the readbacks whose result carried a truncation/compaction seam. Usage: python3 -I a3_sources.py <b2_dir>"""
import json, re, sys
from pathlib import Path
B2 = Path(sys.argv[1]); LEVEL = 57344; DIGEST = 200
H16 = re.compile(r"[0-9a-f]{16}(-\d+)?")
SEAM = ("[observation truncated", "Observation compacted", "Observation superseded")
for arm in ["orch-turn-w1", "orch-turn-w1w2", "orch-turn-catalog"]:
    for l in open(B2 / arm / "transcripts.jsonl"):
        r = json.loads(l)
        if r["level"] != LEVEL: continue
        cards = {c["kind"]: c["value"] for c in r["fill"]["cards"]}
        sd = B2 / arm / "sessions" / Path(r["session_dir"]).name / "spill"
        files = {f.stem: f.read_text(encoding="utf-8") for f in sd.glob("*.txt")}
        head_vis = {}
        for k, v in cards.items():
            st = [t for s, t in files.items() if H16.fullmatch(s) and v in t and not t.startswith("[Observation")]
            head_vis[k] = (st[0].strip().find(v) + len(v) <= DIGEST) if st else "raw"
        reads = []
        for t in r["turns"]:
            for c, f in zip(t.get("tool_calls", []), t.get("feed", []) or []):
                a = c.get("arguments") or {}
                if c.get("name") == "file_extract" and str(a.get("path", "")).startswith("spill:"):
                    content = str(f.get("content", ""))
                    hits = [k for k, v in cards.items() if v in content]
                    reads.append(hits)
                    if any(s in content for s in SEAM):
                        i = min(content.find(s) for s in SEAM if s in content)
                        print("SEAM-READ", arm[10:], r["item_id"], r["rep"], a["path"], len(content), repr(content[max(0, i-60):i+260]))
        fin = [k for k, v in cards.items() if v in (r.get("turn_response") or "")]
        first_hit = {k: next((i + 1 for i, h in enumerate(reads) if k in h), None) for k in cards}
        if reads or fin:
            print(arm[10:], r["item_id"], r["rep"], "head_visible", head_vis, "first_read_with_card", first_hit, "final", fin)
