"""Print specific response messages (content + thoughts) of one history turn. Usage: python3 -I show_msgs.py <history.jsonl> <turn_line> <idx,...>"""
import json, sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
o = json.loads(lines[int(sys.argv[2]) - 1])
for i in map(int, sys.argv[3].split(",")):
    m = o["response"][i]
    print(f"--- [{i}] role={m.get('role')}")
    for k in ("thoughts", "content"):
        if m.get(k): print(f"  {k}: {m[k]!r}")
    if m.get("tool_calls"): print("  tool_calls:", json.dumps(m["tool_calls"], ensure_ascii=False)[:300])
