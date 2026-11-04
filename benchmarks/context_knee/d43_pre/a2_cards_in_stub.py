# D43 (a) b2 前置复算（analyst，2026-11-04；结论见 docs/plans/2026-10-31-context-management-plan.md §6 D43）：卡在观察内的偏移、是否落在 200 字桩首部、回读顺序
# 重跑（仓库根）：python3 -I benchmarks/context_knee/d43_pre/a2_cards_in_stub.py benchmarks/data/context-knee-20261102-b2 > a2_out.txt
# 原样搬自会话 scratchpad，只加这三行；结论数字以当日输出为准，不随 harness 改动更新。
"""D43 fact A2: per 57K run — card char offsets inside their (spilled) observation, whether inside the
200-char stub head, readback ordering, iterations spent, truncated readbacks. Usage: python3 -I a2_cards_in_stub.py <b2_dir>"""
import json, re, sys, collections
from pathlib import Path
B2 = Path(sys.argv[1]); LEVEL = 57344; DIGEST = 200
ARMS = ["orch-turn-w1", "orch-turn-w1w2", "orch-turn-catalog"]
H16 = re.compile(r"[0-9a-f]{16}(-\d+)?")
tot = collections.Counter(); offs = collections.defaultdict(list)
for arm in ARMS:
    for l in open(B2 / arm / "transcripts.jsonl"):
        r = json.loads(l)
        if r["level"] != LEVEL: continue
        cards = {c["kind"]: (c["value"], c["slot"]) for c in r["fill"]["cards"]}
        sd = B2 / arm / "sessions" / Path(r["session_dir"]).name / "spill"
        files = {f.stem: f.read_text(encoding="utf-8") for f in sd.glob("*.txt")}
        h16 = {s: t for s, t in files.items() if H16.fullmatch(s)}
        card_stem = {}
        for k, (v, slot) in cards.items():
            st = [s for s, t in h16.items() if v in t and not t.startswith("[Observation")]
            if not st: tot["card_not_spilled"] += 1; continue
            t = h16[st[0]]; o = t.strip().find(v)
            card_stem[k] = st[0]; offs[slot].append(o); tot["cards_spilled"] += 1
            tot["card_in_200char_head"] += (o + len(v) <= DIGEST)
            tot[f"card_obs_chars_max"] = max(tot["card_obs_chars_max"], len(t))
        rb = []; iters = 0
        for ti, t in enumerate(r["turns"]):
            got = False
            for c, f in zip(t.get("tool_calls", []), t.get("feed", []) or []):
                a = c.get("arguments") or {}
                if c.get("name") == "file_extract" and str(a.get("path", "")).startswith("spill:"):
                    got = True; stem = a["path"][6:].replace(".txt", "")
                    rb.append((stem, len(str(f.get("content", ""))), str(f.get("content", ""))[:90], a))
            iters += got
        if rb:
            ranks = {k: next((i + 1 for i, x in enumerate(rb) if x[0] == s), None) for k, s in card_stem.items()}
            nonrb = [x for x in rb if x[0] not in h16 and x[0] not in files]
            print(arm[10:], r["item_id"], r["rep"], "turns", len(r["turns"]), "rb_iters", iters, "rb", len(rb),
                  "handles_16hex", len(h16), "card_rank_in_reads", ranks, "args_with_range", sum(1 for x in rb if set(x[3]) - {"path"}),
                  "unresolved", len(nonrb))
            for x in rb:
                if "truncated" in x[2] or "compacted" in x[2] or x[0] not in files: print("   odd read:", x[0], x[1], x[2])
print(dict(tot))
for s, v in offs.items(): print(s, "offset chars: n", len(v), "min", min(v), "median", sorted(v)[len(v)//2], "max", max(v), "in_head", sum(1 for o in v if o < DIGEST))
