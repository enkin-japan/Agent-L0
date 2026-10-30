"""Reconstruct the final (cancelled) generation of a turn from journal + history + backend.log.
Usage: python3 -I repetition_window.py <session_dir> <backend.log> <log_lo> <log_hi> <orchestrator.py>
Prints: journal timing around the last tool_result and turn_ended, history metrics of the last
generation, implied duration, budget comparisons, and every guard-ish backend.log line in range."""
import json, sys, re, datetime
sd, logp, lo, hi, orch = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
J = [json.loads(l) for l in open(f"{sd}/journal.jsonl", encoding="utf-8")]
ev = [o for o in J if "seq" in o]
ts = lambda o: datetime.datetime.fromtimestamp(o["ts"]).strftime("%H:%M:%S.%f")[:-3]
end = next(o for o in ev if o["type"] == "turn_ended")
last_res = [o for o in ev if o["type"] == "tool_result"][-1]
print(f"J last tool_result seq={last_res['seq']} {ts(last_res)} name={last_res['data'].get('name')}")
print(f"J turn_ended seq={end['seq']} {ts(end)} status={end['data'].get('status')}")
print(f"J window seconds={end['ts']-last_res['ts']:.3f} unpersisted_seqs={end['seq']-last_res['seq']-1}")
H = [json.loads(l) for l in open(f"{sd}/history.jsonl", encoding="utf-8")][-1]
g, tps = H["generation_tokens"], H["generation_tps"]
print(f"H status={H['status']} last_gen prompt_tokens={H['prompt_tokens']} generation_tokens={g} tps={tps} reused={H['reused_prefix']}")
print(f"H implied_decode_seconds={g/tps:.2f}  (window minus decode = {end['ts']-last_res['ts']-g/tps:.2f}s for prefill+cancel latency)")
src = open(orch, encoding="utf-8").read()
esc = int(re.search(r"^ESCALATED_MAX_TOKENS = (\d+)", src, re.M).group(1))
print(f"H max_tokens={H['max_tokens']} thinking_budget={H['thinking_budget']} ESCALATED_MAX_TOKENS={esc}")
print(f"H gen/max_tokens={g/H['max_tokens']:.3f} gen/thinking_budget={g/H['thinking_budget']:.3f} truncated_rule(gen>=max)={g>=H['max_tokens']}")
tb = H["token_breakdown"]; tot = H["generation_tokens_turn"]
print(f"H turn gen tokens={tot} generations(non-cancelled)={H['generations']} minus_last={tot-g} breakdown_sum={sum(tb.values())} breakdown={tb}")
L = open(logp, encoding="utf-8", errors="replace").read().splitlines()
pat = re.compile(r"\[Engine\]|\[Orchestrator\]|\[Constrained\]|runaway|truncat|复读|repeat|duplicate|max_tokens|约束|task/cancel|turns HTTP|\[web_research\] step")
main = 0
for i in range(lo - 1, min(hi, len(L))):
    s = L[i]
    if pat.search(s):
        tag = ""
        if s.startswith("[Engine]"):
            nxt = L[i + 1] if i + 1 < len(L) else ""
            tag = "  <sub:web_research>" if nxt.startswith("[web_research]") else ""
        print(f"LOG {i+1}: {s[:200]}{tag}")
# grep guard markers repo-wide-in-range
cnt = {k: sum(1 for s in L[lo-1:hi] if k in s) for k in ("transition →", "runaway", "[Constrained]", "max_tokens_escalate", "max_tokens_recovery", "duplicate")}
print("LOG marker counts in range:", cnt)
m = re.search(r"前缀命中 (\d+)/(\d+) tokens，gen ([\d.]+) tps", next(s for s in L[lo-1:hi][::-1] if s.startswith("[Engine]")))
print(f"LOG last Engine line in range: reused={m.group(1)} prompt={m.group(2)} tps={m.group(3)} | matches history last gen: {int(m.group(2))==H['prompt_tokens'] and float(m.group(3))==tps}")
# main-loop generations (Engine lines not followed by a web_research step line) and their tps
mains = []
for i in range(lo - 1, min(hi, len(L))):
    s = L[i]
    mm = re.search(r"^\[Engine\] 前缀命中 (\d+)/(\d+) tokens，gen ([\d.]+) tps", s)
    if mm and not (i + 1 < len(L) and L[i + 1].startswith("[web_research]")):
        mains.append((i + 1, int(mm.group(2)), float(mm.group(3))))
print("MAIN-ish gens (line, prompt, tps):", mains)
prev = [t for (_, p, t) in mains[:-1] if p > 20000]
print(f"last tps={mains[-1][2]} vs mean tps of prior gens with prompt>20000 ({len(prev)}): {sum(prev)/len(prev):.1f} ratio={mains[-1][2]/(sum(prev)/len(prev)):.2f}")
rec = int(re.search(r"^MAX_TOKENS_RECOVERIES = (\d+)", src, re.M).group(1))
worst = H["max_tokens"] + esc * (1 + rec)
print(f"worst-case tokens of a non-tool degenerate loop before acceptance: {H['max_tokens']} + {esc}*(1+{rec}) = {worst} ; at {tps} tps = {worst/tps/60:.1f} min")
print(f"remaining to thinking-budget force-close: {H['thinking_budget']-g} tok = {(H['thinking_budget']-g)/tps:.1f}s ; remaining to max_tokens: {H['max_tokens']-g} tok = {(H['max_tokens']-g)/tps:.1f}s")
