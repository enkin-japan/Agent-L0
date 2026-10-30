"""Python port of apple-app ChatMessage.from(turn:) + groupedForDisplay (tree @1ca9254,
apple-app/Sources/Store/ChatMessage.swift:105-322) applied to history.jsonl, to list what the
app renders on history replay / live turn_ended landing, and which rows are collapsible.
Simplifications: L() localisation is identity; citations only via render.hint=="citations".
Usage: python3 -I replay_projection.py <history.jsonl>"""
import json, sys
CANCEL_NEWMSG = "Operation cancelled: the user sent a new message without approving."
def from_turn(t, ti):
    out = []
    if t.get("prompt"): out.append(dict(kind="user", text=t["prompt"]))
    resp = t.get("response") or []
    abandoned = t.get("status") == "warning" and any(
        m.get("content") == "The suspended task was interrupted by a new user message and abandoned (not continued)." or
        any(x.get("response") == CANCEL_NEWMSG for x in (m.get("tool_responses") or [])) for m in resp)
    stopped = t.get("status") in ("cancelled", "fail") or abandoned
    if stopped: out.append(dict(kind="system", text=f"<stopped notice: {t.get('status')}>"))
    for m in resp:
        r = m.get("role")
        if r == "assistant":
            calls = m.get("tool_calls") or []
            if calls:
                for c in calls:
                    if c.get("type") == "error": out.append(dict(kind="tool", label="工具调用解析失败", detail=c.get("error_msg", "")))
                    else: out.append(dict(kind="tool", label=f"调用 {c.get('name')}", detail="args", role="call", name=c.get("name")))
                if m.get("thoughts") and out: out[-1]["thoughts"] = m["thoughts"]
            elif m.get("content"):
                out.append(dict(kind="assistant", text=m["content"], thoughts=m.get("thoughts")))
        elif r == "tool":
            for it in m.get("tool_responses") or []:
                if it.get("response") == CANCEL_NEWMSG:
                    out.append(dict(kind="tool", label=f"{it.get('name')} · 未执行", detail="", role="notRun", name=it.get("name"))); continue
                row = dict(kind="tool", label=f"{it.get('name') or '工具'} 返回", detail=it.get("response") or "", role="result", name=it.get("name"))
                if (it.get("render") or {}).get("hint") == "citations": row["citations"] = True
                out.append(row)
        elif r == "system":
            if stopped: continue
            if m.get("content"): out.append(dict(kind="system", text=m["content"]))
        else:
            if m.get("content"): out.append(dict(kind="system", text=f"[{r}] {m['content']}"))
    facts = t.get("execution_receipts")
    has_tool = any(o["kind"] == "tool" for o in out)
    merged = appended = 0
    if (stopped or not has_tool) and facts:
        for it in facts.get("items", []):
            if it.get("type") not in ("tool_started", "tool_result"): continue
            started = it["type"] == "tool_started"
            if not started:
                ex = next((o for o in out if o["kind"] == "tool" and o.get("label") == f"{it['name']} 返回" and o.get("detail") == (it.get("detail") or "")), None)
                if ex: ex["label"] = f"{it['name']} · 已返回"; merged += 1; continue
            out.append(dict(kind="tool", label=f"{it['name']} · {'已开始' if started else '已返回'}", detail=it.get("detail") or "", role="call" if started else "result", name=it["name"], receipt=it.get("seq"))); appended += 1
        if facts.get("truncated"): out.append(dict(kind="system", text="<执行记录不完整>"))
        if stopped and not facts.get("items"): out.append(dict(kind="system", text="<本轮未记录工具事件>"))
    for o in out: o["ti"] = ti
    return out, dict(stopped=stopped, receipts_merged=merged, receipts_appended=appended)
def grouped(msgs):
    out, run = [], []
    def flush():
        if run: out.append(dict(kind="toolGroup", steps=list(run), ti=run[0]["ti"])); run.clear()
    for m in msgs:
        if m["kind"] != "tool": flush(); out.append(m); continue
        if m.get("citations"): flush(); out.append(m); continue
        if run and run[-1]["ti"] != m["ti"]: flush()
        run.append(m)
    flush(); return out
msgs = []
for ti, line in enumerate(open(sys.argv[1], encoding="utf-8")):
    rows, info = from_turn(json.loads(line), ti); msgs += rows
    print(f"turn {ti}: {info}")
for i, d in enumerate(grouped(msgs)):
    k = d["kind"]
    if k == "toolGroup":
        print(f"D{i} toolGroup turn={d['ti']} steps={len(d['steps'])} COLLAPSIBLE(default folded): " + ", ".join(s["label"] for s in d["steps"])[:400])
    elif k == "assistant":
        print(f"D{i} assistant turn={d['ti']} content_len={len(d['text'])} ALWAYS-VISIBLE content80={d['text'][:80]!r} thoughts_len={len(d.get('thoughts') or '')} (thoughts collapsible)")
    elif k == "tool":
        print(f"D{i} tool(top-level{' citations' if d.get('citations') else ''}) {d.get('label')}")
    else:
        print(f"D{i} {k} turn={d['ti']} {d.get('text','')[:80]!r}")
