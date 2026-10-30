"""Structural checks on a journal: per-turn turn_started count, call_id null/dup, started/result pairing,
events after a turn's turn_ended, largest line. Usage: python3 -I journal_checks.py <journal.jsonl>"""
import json, sys, collections
ev = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8")]; ev = [e for e in ev if "seq" in e]
started = collections.Counter(); result = collections.Counter(); null_ids = 0; ended = {}; after = []
ts_per_turn = collections.Counter()
for e in ev:
    t, d = e["type"], e.get("data") or {}
    if e["turn_id"] in ended: after.append((e["seq"], t))
    if t == "turn_started": ts_per_turn[e["turn_id"]] += 1
    if t in ("tool_started", "tool_result"):
        cid = d.get("call_id")
        if cid is None: null_ids += 1
        (started if t == "tool_started" else result)[cid] += 1
    if t == "turn_ended": ended[e["turn_id"]] = e["seq"]
big = max(ev, key=lambda e: len(json.dumps(e, ensure_ascii=False)))
print("turn_started per turn:", dict(ts_per_turn))
print("tool_started", sum(started.values()), "tool_result", sum(result.values()), "null call_id", null_ids)
print("dup call_id started:", [k for k, v in started.items() if v > 1], "dup result:", [k for k, v in result.items() if v > 1])
print("unpaired started:", sorted(set(started) - set(result)), "unpaired result:", sorted(set(result) - set(started)))
print("events after own turn_ended:", after)
print("largest event: seq", big["seq"], big["type"], len(json.dumps(big, ensure_ascii=False)), "chars")
