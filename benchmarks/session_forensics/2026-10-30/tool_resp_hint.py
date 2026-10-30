"""For history 'tool_responses' messages print the tail (spill hint) of each response and thoughts length per assistant msg.
Usage: python3 -I tool_resp_hint.py <history.jsonl>"""
import json, sys
for ln, line in enumerate(open(sys.argv[1], encoding="utf-8"), 1):
    o = json.loads(line); r = o.get("response")
    if not isinstance(r, list): continue
    for i, m in enumerate(r):
        if m.get("role") == "assistant":
            th = m.get("thoughts"); th = th if isinstance(th, str) else json.dumps(th, ensure_ascii=False)
            print(f"H{ln}.{i} assistant thoughts_len={len(th or '')} head={ (th or '')[:150].replace(chr(10),' ')!r}")
        for tr in m.get("tool_responses") or []:
            s = json.dumps(tr, ensure_ascii=False)
            print(f"H{ln}.{i} tool_response keys={list(tr.keys())} len={len(s)} tail={s[-330:]!r}")
