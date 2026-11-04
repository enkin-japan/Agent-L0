# D43 (a) b2 前置复算（analyst，2026-11-04；结论见 docs/plans/2026-10-31-context-management-plan.md §6 D43）：线索抽取器噪声密度与规则 R1/R2 的卡覆盖（真实 b2 spill）
# 重跑（仓库根）：python3 -I benchmarks/context_knee/d43_pre/c_clue_noise.py benchmarks/data/context-knee-20261102-b2 orch-turn-w1 10 > c_out_orch-turn-w1.txt
# 原样搬自会话 scratchpad，只加这三行；结论数字以当日输出为准，不随 harness 改动更新。
"""D43 fact C: clue-extractor noise density on real b2 57K filler observations (16-hex spill files =
full text of each compacted filler observation). Unit = one run's filler (~50 observations, one card per
card-host observation). Usage: python3 -I c_clue_noise.py <b2_dir> [arm] [n_runs]"""
import json, re, sys, collections
from pathlib import Path
B2 = Path(sys.argv[1]); ARM = sys.argv[2] if len(sys.argv) > 2 else "orch-turn-w1"
NRUNS = int(sys.argv[3]) if len(sys.argv) > 3 else 10; LEVEL = 57344; DIGEST = 200; MAXTOK = 8; MAXCH = 240
H16 = re.compile(r"[0-9a-f]{16}(-\d+)?")
RX = {  # the delegation's candidate classes
    "path": re.compile(r"[\w./-]*/[\w.-]+\.[A-Za-z0-9]{1,5}\b"),
    "id": re.compile(r"\b[A-Z]{2,}-\d{3,}\b"),
    "assign": re.compile(r"\b[A-Za-z_][\w.-]*\s*[:=]\s*[A-Za-z0-9_.-]+"),
    "url": re.compile(r"https?://[^\s\"']+"),
    "hex": re.compile(r"\b[0-9a-f]{12,}\b"),
}
ORDER = ["id", "path", "assign", "url", "hex"]
def matches(text):
    out = []
    for cls, rx in RX.items():
        out += [(m.start(), cls, m.group(0)) for m in rx.finditer(text)]
    return sorted(out)
def key_of(tok): return re.split(r"\s*[:=]\s*", tok, maxsplit=1)[0]
def card_token_hit(tok, v): return v in tok
def clue(toks):
    """cap 8 tokens / 240 chars, joined with ' · '; returns kept list"""
    kept, n = [], 0
    for t in toks:
        add = len(t) + (3 if kept else 0)
        if len(kept) >= MAXTOK or n + add > MAXCH: break
        kept.append(t); n += add
    return kept
def rule(text, own_path, df=None):
    """proposed rule: decoded text; drop own path + tokens inside the 200-char stub head; dedupe;
    assignments only if the key occurs once in this observation (singleton) [and, with df, in <=1
    observation of the session]; class order id > path > assign > url > hex, then appearance."""
    ms = matches(text); kc = collections.Counter(key_of(t) for _, c, t in ms if c == "assign")
    seen, ranked = set(), []
    for cls in ORDER:
        for pos, c, t in ms:
            if c != cls or t in seen or t == own_path or pos + len(t) <= DIGEST: continue
            if c == "assign" and (kc[key_of(t)] > 1 or (df is not None and df[key_of(t)] > 1)): continue
            if c == "path" and own_path and t in own_path: continue
            seen.add(t); ranked.append(t)
    return ranked
runs = 0; agg = collections.Counter()
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
        try: j = json.loads(raw)
        except Exception: continue
        own = j.get("path", "") if isinstance(j, dict) else ""
        obs.append((f.stem, raw, own))
    df = collections.Counter()
    for _, raw, _ in obs:
        df.update({key_of(t) for _, c, t in matches(raw.replace("\\n", "\n")) if c == "assign"})
    cls_tot = collections.Counter(); nonempty = {"R1": 0, "R2": 0}
    print(f"\n## run {r['item_id']} r{r['rep']} obs={len(obs)} cards={list(cards.values())}")
    for stem, raw, own in obs:
        dec = raw.replace("\\n", "\n")       # decode JSON \n escapes (raw text glues 'n' onto keys)
        ms = matches(dec); cls_tot.update(c for _, c, _ in ms)
        r1, r2 = rule(dec, own), rule(dec, own, df)
        nonempty["R1"] += bool(clue(r1)); nonempty["R2"] += bool(clue(r2))
        for v, kind in cards.items():
            if v not in raw: continue
            pc = collections.Counter(c for _, c, _ in ms)
            raw_rank = next((i + 1 for i, (_, _, t) in enumerate(ms) if v in t), None)
            rawesc_rank = next((i + 1 for i, (_, _, t) in enumerate(matches(raw)) if v in t), None)
            first8 = [t for _, _, t in ms[:8]]
            chars_needed = len(" · ".join(t for _, _, t in ms[:raw_rank])) if raw_rank else None
            res = {}
            for name, rk in (("R1", r1), ("R2", r2)):
                k = clue(rk); rank = next((i + 1 for i, t in enumerate(rk) if v in t), None)
                res[name] = (rank, any(v in t for t in k), len(" · ".join(k)))
                agg[f"{name}_card_in_clue"] += any(v in t for t in k)
                agg[f"{name}_card_in_clue_or_head"] += any(v in t for t in k) or raw.strip().find(v) + len(v) <= DIGEST
            agg["card_obs"] += 1; agg["naive_in_first8"] += bool(raw_rank and raw_rank <= 8)
            agg["naive_fits_240"] += bool(chars_needed is not None and chars_needed <= MAXCH and raw_rank <= 8)
            agg["card_in_stub_head"] += raw.strip().find(v) + len(v) <= DIGEST
            print(f"  card {kind:11s} obs {stem} chars={len(raw)} per-class={dict(pc)} naive_rank={raw_rank}/{len(ms)}"
                  f" (raw-escaped rank={rawesc_rank}) chars_to_include={chars_needed}; R1 rank/in_clue/len={res['R1']} R2={res['R2']}")
            print(f"     first8: {first8}")
            print(f"     R1 clue: {clue(r1)}")
    found = {v for v in cards if any(v in raw for _, raw, _ in obs)}
    for v in set(cards) - found:
        hosts = [f.name for f in sd.glob("*.txt") if v in f.read_text(encoding="utf-8")]
        print(f"  MISSING card {cards[v]} {v}: not in any filtered 16-hex filler spill; files containing it: {hosts}")
        agg["card_not_spilled"] += 1
    print(f"  run totals per class (all {len(obs)} obs): {dict(cls_tot)}; obs with non-empty clue R1={nonempty['R1']} R2={nonempty['R2']}")
    agg["obs"] += len(obs); agg["R1_nonempty"] += nonempty["R1"]; agg["R2_nonempty"] += nonempty["R2"]
    for k, v in cls_tot.items(): agg[f"total_{k}"] += v
print("\nAGG", dict(agg))
