# D43 (a) b2 前置复算（analyst，2026-11-04；结论见 docs/plans/2026-10-31-context-management-plan.md §6 D43）：57K 档 spill 回读实况（回读次数、截断、卡命中、aborted）
# 重跑（仓库根）：python3 -I benchmarks/context_knee/d43_pre/a_readback.py benchmarks/data/context-knee-20261102-b2 > a_out.txt
# 原样搬自会话 scratchpad，只加这三行；结论数字以当日输出为准，不随 harness 改动更新。
"""D43 fact A: spill read-back reality in b2 57K arms. Usage: python3 -I a_readback.py <b2_dir> > out"""
import json, re, sys, collections
from pathlib import Path
B2 = Path(sys.argv[1]); LEVEL = 57344
ARMS = ["orch-turn-none", "orch-turn-w1", "orch-turn-w1w2", "orch-turn-catalog"]
SEAM = ("[observation truncated", "Observation compacted", "Observation superseded")
def is_spill(a): p = str((a or {}).get("path", "")); return p.startswith("spill:") or "/spill/" in p
detail = []
for arm in ARMS:
    agg = collections.Counter(); abort_kinds = collections.Counter()
    for l in open(B2 / arm / "transcripts.jsonl"):
        r = json.loads(l)
        if r["level"] != LEVEL: continue
        cards = {c["kind"]: c["value"] for c in r["fill"]["cards"]}
        sd = B2 / arm / "sessions" / Path(r["session_dir"]).name / "spill"
        files = {f.stem: f.read_text(encoding="utf-8") for f in sd.glob("*.txt")} if sd.is_dir() else {}
        # which card values sit in a 16-hex (compaction) spill = card observation was stubbed
        stubbed = {k: sorted(s for s, t in files.items() if v in t and re.fullmatch(r"[0-9a-f]{16}(-\d+)?", s)) for k, v in cards.items()}
        agg["runs"] += 1
        ab = r["score"].get("aborted"); reason = r.get("aborted_reason") or ""
        if ab: agg["aborted"] += 1; abort_kinds["context_guard" if "context" in reason.lower() or "上下文" in reason else reason[:30]] += 1
        calls = feeds_hit = 0; seen_in_feed = set(); rb_lens = []; rb_trunc = 0; card_rb = 0; rb_paths = []
        for ti, t in enumerate(r["turns"]):
            for c, f in zip(t.get("tool_calls", []), t.get("feed", []) or []):
                content = str(f.get("content", ""))
                hits = {k for k, v in cards.items() if v in content}
                if c.get("name") == "file_extract" and is_spill(c.get("arguments")):
                    calls += 1; rb_lens.append(len(content)); rb_paths.append(c["arguments"]["path"])
                    if any(s in content for s in SEAM): rb_trunc += 1
                    if hits: card_rb += 1
                for k in hits: seen_in_feed.add((k, c.get("name") or str(c)[:40]))
        final = r.get("turn_response") or ""
        fin_hits = sorted(k for k, v in cards.items() if v in final)
        agg["rb_calls"] += calls; agg["rb_runs"] += bool(calls); agg["rb_trunc"] += rb_trunc
        agg["rb_card_results"] += card_rb; agg["rb_card_runs"] += bool(card_rb)
        agg["final_any_card"] += bool(fin_hits); agg["final_card_values"] += len(fin_hits)
        agg["recall_hits_score"] += r["score"].get("recall_hits") or 0
        agg["card_obs_stubbed"] += sum(1 for v in stubbed.values() if v)
        agg["card_obs_total"] += len(cards)
        uniq = len(set(rb_paths)); agg["rb_unique_handles"] += uniq
        card_stems = {s for v in stubbed.values() for s in v}
        agg["rb_card_handle_reads"] += sum(1 for p in rb_paths if p.split(":")[-1].split("/")[-1].replace(".txt", "") in card_stems)
        agg["spill_files"] += len(files)
        agg["rb_chars_sum"] += sum(rb_lens); agg["rb_chars_max"] = max(agg["rb_chars_max"], max(rb_lens or [0]))
        detail.append(dict(arm=arm, item=r["item_id"], rep=r["rep"], aborted=ab, reason=reason[:40], recall_hits=r["score"].get("recall_hits"), recall_ok=r["score"].get("recall_ok"), status=r.get("turn_status"),
                           rb_calls=calls, rb_unique=uniq, rb_card_results=card_rb, rb_trunc=rb_trunc, final_cards=fin_hits,
                           card_seen_in_feed=sorted(seen_in_feed), stubbed={k: bool(v) for k, v in stubbed.items()}, spill_files=len(files)))
    print(arm, dict(agg), "abort_kinds", dict(abort_kinds))
print()
for d in detail:
    if d["rb_calls"] or d["final_cards"] or d["recall_hits"]: print(json.dumps(d, ensure_ascii=False))
