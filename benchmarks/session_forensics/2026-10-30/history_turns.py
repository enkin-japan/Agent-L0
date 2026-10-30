"""Per-turn summary of history.jsonl records (one record per turn).
Usage: python3 -I history_turns.py <history.jsonl> [trunc]"""
import json, sys
T = int(sys.argv[2]) if len(sys.argv) > 2 else 300
def s(v):
    v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    v = v.replace("\n", "\\n"); return v[:T] + (f"...<{len(v)}ch>" if len(v) > T else "")
for ln, line in enumerate(open(sys.argv[1], encoding="utf-8"), 1):
    o = json.loads(line)
    print(f"== H{ln} status={o.get('status')} prompt80={o.get('prompt','')[:80]!r}")
    for k in ("generation_tokens","generation_tokens_turn","generations","prompt_tokens","parse_error_count","reused_prefix","sub_generations","sub_generation_tokens","token_breakdown","peak_memory","generation_tps"):
        print(f"   {k}={s(o.get(k))}")
    print(f"   response({len(o.get('response') or '')}ch)={s(o.get('response'))}")
    er = (o.get("execution_receipts") or {})
    print(f"   receipts keys={list(er.keys())}")
    for it in er.get("items", []):
        print(f"     {it.get('seq')} {it.get('type')} {it.get('name')} {it.get('call_id')} detail={s(it.get('detail',''))[:160]}")
    for k, v in er.items():
        if k != "items": print(f"     {k}={s(v)}")
