# D43 (a) b2 前置复算（analyst，2026-11-04；结论见 docs/plans/2026-10-31-context-management-plan.md §6 D43）：57K 档 spill 文件形态（换行数、字符数）与生产回读体积估算
# 重跑（仓库根）：python3 -I benchmarks/context_knee/d43_pre/b_spill_shape.py benchmarks/data/context-knee-20261102-b2 > b_out.txt
# 原样搬自会话 scratchpad，只加这三行；结论数字以当日输出为准，不随 harness 改动更新。
"""D43 fact B: on-disk shape of every b2 57K spill file (newline count, bytes, chars) + prod-readback size estimate.
Usage: python3 -I b_spill_shape.py <b2_dir>"""
import json, re, sys, collections
from pathlib import Path
B2 = Path(sys.argv[1]); LEVEL = 57344; CAP = 4000
for arm in ["orch-turn-w1", "orch-turn-w1w2", "orch-turn-catalog"]:
    st = collections.Counter(); chars = []
    for l in open(B2 / arm / "transcripts.jsonl"):
        r = json.loads(l)
        if r["level"] != LEVEL: continue
        for f in (B2 / arm / "sessions" / Path(r["session_dir"]).name / "spill").glob("*.txt"):
            b = f.read_bytes(); t = b.decode("utf-8")
            st["files"] += 1; st["16hex" if re.fullmatch(r"[0-9a-f]{16}(-\d+)?", f.stem) else "other_stem"] += 1
            st[f"newlines={min(t.count(chr(10)), 2)}"] += 1; chars.append(len(t))
            # production read_text_range output: "[name lines 1-1 of 1]\n    1 | " + text
            prod = len(f"[{f.name} lines 1-1 of 1]\n    1 | ") + len(t)
            st["prod_readback_over_cap"] += prod > CAP
            st["bytes"] += len(b)
    chars.sort()
    print(arm, dict(st), "chars min/median/p95/max", chars[0], chars[len(chars)//2], chars[int(len(chars)*.95)], chars[-1])
