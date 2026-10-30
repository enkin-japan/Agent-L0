"""Compact list of each turn's response messages: role, tool call names, thoughts len, content len, tool_responses (name, len, render hint).
Usage: python3 -I resp_list.py <history.jsonl>"""
import json, sys
for ln, line in enumerate(open(sys.argv[1], encoding="utf-8"), 1):
    o = json.loads(line)
    print(f"== H{ln} status={o.get('status')} enable_thinking={o.get('enable_thinking')} n_resp={len(o.get('response') or [])} receipts={len((o.get('execution_receipts') or {}).get('items', []))} receipts_truncated={(o.get('execution_receipts') or {}).get('truncated')}")
    for i, m in enumerate(o.get("response") or []):
        th = m.get("thoughts"); thl = len(th) if isinstance(th, str) else (len(json.dumps(th)) if th else 0)
        calls = [(c.get("type"), c.get("name")) for c in (m.get("tool_calls") or [])]
        trs = [(t.get("name"), len(t.get("response") or ""), (t.get("render") or {}).get("hint")) for t in (m.get("tool_responses") or [])]
        c = m.get("content"); cl = len(c) if isinstance(c, str) else 0
        print(f"  {i:2d} {m.get('role'):9s} calls={calls} thoughts={thl} content={cl} tool_responses={trs} extra={[k for k in m if k not in ('role','tool_calls','thoughts','content','tool_responses')]}")
        if m.get("role") == "system" or (m.get("role") == "assistant" and cl and not calls): print(f"       content80={c[:80]!r}")
