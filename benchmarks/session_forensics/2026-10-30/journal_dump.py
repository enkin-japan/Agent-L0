"""Dump a session journal.jsonl compactly: seq, local time, type, data fields (strings truncated).
Usage: python3 -I journal_dump.py <journal.jsonl> [trunc]"""
import json, sys, datetime, collections
path = sys.argv[1]; T = int(sys.argv[2]) if len(sys.argv) > 2 else 90
def short(v):
    if isinstance(v, str):
        s = v.replace("\n", "\\n")
        return (s[:T] + f"...<{len(v)}ch>") if len(v) > T else s
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}={short(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(short(x) for x in v[:5]) + (f", ...+{len(v)-5}" if len(v) > 5 else "") + "]"
    return repr(v)
counts = collections.Counter(); prev = None
for ln, line in enumerate(open(path, encoding="utf-8"), 1):
    o = json.loads(line)
    if "seq" not in o:
        print(f"L{ln} NO-SEQ {short(o)}"); continue
    t = datetime.datetime.fromtimestamp(o["ts"]).strftime("%H:%M:%S.%f")[:-3]
    counts[o["type"]] += 1
    gap = "" if prev is None or o["seq"] == prev + 1 else f" (gap {prev}->{o['seq']})"
    prev = o["seq"]
    print(f"L{ln} seq={o['seq']}{gap} {t} turn={o.get('turn_id')} {o['type']} bytes={len(line)} {short(o.get('data'))}")
print("COUNTS", dict(counts))
