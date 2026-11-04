# D43 (a) 实施期 b2 重放（implementer，2026-11-04；数字见提交说明「口径与成本」）：一审修补（冒号只认 JSON 键/行首）后多出的非空线索归因到哪条规则
# 重跑（仓库根）：backend/.venv/bin/python benchmarks/context_knee/d43_pre/attrib_nonempty.py benchmarks/data/context-knee-20261102-b2
# 原样搬自会话 scratchpad，只加这三行与相对 sys.path；结论数字以当日输出为准。
"""Attribute the post-fixup rise in non-empty clues (149->194 w1) by toggling single changes back."""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))
from framework.memory import compactor as C
B2 = Path(sys.argv[1]); H16 = re.compile(r"[0-9a-f]{16}(-\d+)?")
runs = []
n = 0
for l in open(B2 / "orch-turn-w1" / "transcripts.jsonl"):
    r = json.loads(l)
    if r["level"] != 57344: continue
    n += 1
    if n > 10: break
    sd = B2 / "orch-turn-w1" / "sessions" / Path(r["session_dir"]).name / "spill"
    obs = []
    for f in sorted(sd.glob("*.txt")):
        if not H16.fullmatch(f.stem): continue
        raw = f.read_text(encoding="utf-8")
        if "legacy-2025q3" not in raw and not raw.startswith('{"todos"'): continue
        try: json.loads(raw)
        except Exception: continue
        obs.append(raw)
    runs.append(obs)
def count():
    ne = 0
    for obs in runs:
        kc = C.assign_key_counts(obs)
        ne += sum(bool(C.observation_clue(t, head=t.strip()[:200].rstrip(), key_counts=kc)) for t in obs)
    return ne
print("current", count())
orig_vis = C._visible_in
C._visible_in = lambda tok, hay: tok in hay
print("substring head filter", count())
C._visible_in = orig_vis
orig_re = C._CLUE_ASSIGN_RE
C._CLUE_ASSIGN_RE = re.compile(orig_re.pattern.replace(r'"(?P<qk>[A-Za-z_][\w.]{2,})"[ \t]*:', r'(?!)(?P<qk>x)'), re.ASCII)
print("no JSON quoted-key", count())
C._CLUE_ASSIGN_RE = orig_re
import collections
qk = collections.Counter()
for obs in runs:
    kc = C.assign_key_counts(obs)
    for t in obs:
        c = C.observation_clue(t, head=t.strip()[:200].rstrip(), key_counts=kc)
        dec = C._clue_decode(t)
        for m in C._CLUE_ASSIGN_RE.finditer(dec):
            if m.group("qk") and f'{m.group("qk")} = ' in c:
                qk[f'{m.group("qk")} = {m.group("v")[:20]}'] += 1
print("quoted-key tokens in clues:", qk.most_common(8), sum(qk.values()))
