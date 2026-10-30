"""Dump history.jsonl compactly: line, role, keys, content length, truncated content/tool_calls.
Usage: python3 -I history_dump.py <history.jsonl> [trunc]"""
import json, sys
T = int(sys.argv[2]) if len(sys.argv) > 2 else 200
def s(v):
    v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    v = v.replace("\n", "\\n")
    return v[:T] + (f"...<{len(v)}ch>" if len(v) > T else "")
for ln, line in enumerate(open(sys.argv[1], encoding="utf-8"), 1):
    o = json.loads(line)
    keys = sorted(o.keys())
    extra = {k: o[k] for k in keys if k not in ("role", "content", "tool_calls", "reasoning_content", "thinking")}
    print(f"H{ln} role={o.get('role')} keys={keys} extra={s(extra)}")
    for k in ("reasoning_content", "thinking"):
        if o.get(k): print(f"   {k}: {s(o[k])}")
    if o.get("content"): print(f"   content: {s(o['content'])}")
    if o.get("tool_calls"): print(f"   tool_calls: {s(o['tool_calls'])}")
