# D43 (a) 实施期 b2 重放（implementer，2026-11-04；数字见提交说明「口径与成本」）：已实现的 observation_clue 在 b2 真实 spill 上重放：三卡覆盖与非空线索数（对 analyst 的 28/28、30/30、149/504）
# 重跑（仓库根）：backend/.venv/bin/python benchmarks/context_knee/d43_pre/validate_r2.py benchmarks/data/context-knee-20261102-b2 orch-turn-w1 10  （catalog 同法；存档 validate_r2_out.txt 与引用数字是 NRUNS=10 的结果，阶段复审 M5 改正）
# 原样搬自会话 scratchpad，只加这三行与相对 sys.path；结论数字以当日输出为准。
"""Validate the implemented observation_clue on b2 real spills vs analyst numbers (28/28 w1, 30/30 catalog, 149/504)."""
import json, re, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))
from framework.memory.compactor import observation_clue, assign_key_counts
B2 = Path(sys.argv[1]); ARM = sys.argv[2]; NRUNS = int(sys.argv[3]); LEVEL = 57344
H16 = re.compile(r"[0-9a-f]{16}(-\d+)?")
runs = 0; agg = collections.Counter(); samples = []
for l in open(B2 / ARM / "transcripts.jsonl"):
    r = json.loads(l)
    if r["level"] != LEVEL: continue
    runs += 1
    if runs > NRUNS: break
    cards = {c["value"]: c["kind"] for c in r["fill"]["cards"]}
    sd = B2 / ARM / "sessions" / Path(r["session_dir"]).name / "spill"
    obs = []
    for f in sorted(sd.glob("*.txt")):
        if not H16.fullmatch(f.stem): continue
        raw = f.read_text(encoding="utf-8")
        if "legacy-2025q3" not in raw and not raw.startswith('{"todos"'): continue
        try: json.loads(raw)
        except Exception: continue
        obs.append(raw)
    kc = assign_key_counts(obs)
    for raw in obs:
        head = raw.strip()[:200].rstrip()
        c = observation_clue(raw, head=head, key_counts=kc)
        agg["obs"] += 1; agg["nonempty"] += bool(c)
        for v, kind in cards.items():
            if v not in raw: continue
            agg["card_obs"] += 1
            inc = v in c; inh = v in head
            agg["in_clue"] += inc; agg["in_head"] += inh; agg["in_clue_or_head"] += inc or inh
            if not (inc or inh): samples.append((kind, v, c))
            elif len(samples) < 6: samples.append(("OK", kind, c))
print(ARM, dict(agg))
for s in samples[:12]: print(" ", s)
