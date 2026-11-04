# D43 (a) 实施期 b2 重放（implementer，2026-11-04；数字见提交说明「口径与成本」）：b2 57K 每 run 线索合计字数中位 + 守卫（桩不比原文短则去线索）去掉的条数
# 重跑（仓库根）：backend/.venv/bin/python benchmarks/context_knee/d43_pre/clue_cost.py benchmarks/data/context-knee-20261102-b2
# 原样搬自会话 scratchpad，只加这三行与相对 sys.path；结论数字以当日输出为准。
"""Clue chars per 57K run on b2 (run-wide key counts over 16-hex spill files) + stubs where the clue guard would drop it."""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))
from framework.memory.compactor import observation_clue, assign_key_counts
B2 = Path(sys.argv[1]); H16 = re.compile(r"[0-9a-f]{16}(-\d+)?")
for arm in ("orch-turn-w1", "orch-turn-catalog"):
    per_run = []; dropped = 0
    for l in open(B2 / arm / "transcripts.jsonl"):
        r = json.loads(l)
        if r["level"] != 57344: continue
        sd = B2 / arm / "sessions" / Path(r["session_dir"]).name / "spill"
        obs = [f.read_text(encoding="utf-8") for f in sorted(sd.glob("*.txt")) if H16.fullmatch(f.stem)]
        kc = assign_key_counts(obs); tot = 0
        for t in obs:
            head = t.strip()[:200].rstrip(); c = observation_clue(t, head=head, key_counts=kc)
            plain = len(head) + len(f" …[Observation compacted; originally {len(t)} chars]")
            if c and plain + len(c) >= len(t): dropped += 1
            else: tot += len(c)
        per_run.append(tot)
    per_run.sort()
    print(f"COST {arm} 57K: clue chars per run (after guard) min/median/max = {per_run[0]}/{per_run[len(per_run)//2]}/{per_run[-1]} over {len(per_run)} runs; clues dropped by guard = {dropped}")
