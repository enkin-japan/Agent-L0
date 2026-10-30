"""Print the tail of each truncated tool_result (where the spill path hint lives) and any 'macagent' path occurrences.
Usage: python3 -I spill_hint.py <journal.jsonl>"""
import json, sys, re
for line in open(sys.argv[1], encoding="utf-8"):
    o = json.loads(line)
    if o.get("type") != "tool_result": continue
    r = o["data"].get("response", "")
    paths = re.findall(r"/Users/[^\s\"'\])]*", r)
    print(f"seq={o['seq']} call={o['data'].get('call_id')} truncated={o['data'].get('truncated')} len={len(r)} paths={paths}")
    if o["data"].get("truncated"): print("   TAIL:", r[-260:].replace("\n", "\\n"))
