"""List model-facing messages stored in history record 'response' lists; print role, tool name, len, and spill/path hints.
Usage: python3 -I history_msgs.py <history.jsonl>"""
import json, sys, re
for ln, line in enumerate(open(sys.argv[1], encoding="utf-8"), 1):
    o = json.loads(line); r = o.get("response")
    if not isinstance(r, list): print(f"H{ln} response not list: {type(r)}"); continue
    for i, m in enumerate(r):
        c = m.get("content") or ""; c = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        hints = re.findall(r"[^\n]{0,80}(?:spill|macagent)[^\n]{0,120}", c)
        tc = m.get("tool_calls")
        print(f"H{ln}.{i} role={m.get('role')} name={m.get('name')} keys={sorted(m.keys())} len={len(c)} tool_calls={json.dumps(tc, ensure_ascii=False)[:220] if tc else None}")
        for h in hints[:3]: print("     HINT:", h)
