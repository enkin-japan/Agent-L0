# D43 (a) b2 前置复算（analyst，2026-11-04；结论见 docs/plans/2026-10-31-context-management-plan.md §6 D43）：单行 57K 观察落盘后生产 file_extract 能否读到 10/50/90% 处的卡
# 重跑（仓库根）：python3 -I benchmarks/context_knee/d43_pre/b_sim_single_line.py . > b_sim_out.txt
# 原样搬自会话 scratchpad，只加这三行；结论数字以当日输出为准，不随 harness 改动更新。
"""D43 fact B sim: a single-line 57K-char observation spilled to disk; can production file_extract
(read_text_range) + orchestrator cap (truncate_observation, cap 4000) reach cards at 10/50/90%?
Loads the two production modules by file path (no package import). Usage: python3 -I b_sim_single_line.py <repo>"""
import importlib.util, json, sys, tempfile
from pathlib import Path
REPO = Path(sys.argv[1]); CAP = 4000
def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
text = load("textparser", "backend/framework/ingestion/parsers/text.py")
spill = load("spillmod", "backend/framework/core/spill.py")
filler_lines = [f"2026-09-20T13:{i%60:02d}:17Z INFO  [ingest-{i%9}] batch {i} committed rows={i*7%5000} lag_ms={i%900}" for i in range(900)]
cards = {0.1: "halyard: loaded config from /opt/halyard/conf/halyard-abc123.toml",
         0.5: "escalation: incident tracked as ticket OPS-12345", 0.9: "queue_depth_alert_threshold = 4321"}
for frac, c in sorted(cards.items(), reverse=True): filler_lines.insert(int(frac * len(filler_lines)), c)
obs = json.dumps({"path": "/srv/x.log", "content": "\n".join(filler_lines)})   # filler.py:213 shape
print("observation chars", len(obs), "newlines", obs.count("\n"), "card offsets", {k: obs.find(v) for k, v in cards.items()})
d = Path(tempfile.mkdtemp()); f = d / "abcdef0123456789.txt"; f.write_text(obs, encoding="utf-8")
def model_sees(out):
    return out if len(out) <= CAP else spill.truncate_observation(out, CAP, ("/x/spill/deadbeef.txt", len(out)))
def found(seen): return [k for k, v in cards.items() if v in seen]
for mc in (12000, 3400, 60000):
    out = text.read_text_range(str(f), max_chars=mc); seen = model_sees(out)
    print(f"prod default-range max_chars={mc}: tool out {len(out)} chars -> model sees {len(seen)}; cards visible {found(seen)}")
out = text.read_text_range(str(f), start=2, end=5); print("start=2,end=5:", out[:80])
# blind sweep: tail window of the capped view ends at max_chars; how many calls to cover 57K?
covered = set(); calls = 0; mc = CAP
while mc < len(obs) + 1400:
    seen = model_sees(text.read_text_range(str(f), max_chars=mc)); calls += 1; covered |= set(found(seen)); mc += 1300
print("blind max_chars sweep: calls", calls, "cards eventually seen", sorted(covered))
# bench SpillReadTool returns the raw text (orch_tools.py:136) -> capped by the orchestrator
seen = model_sees(obs); print("bench raw readback: model sees", len(seen), "cards visible", found(seen))
