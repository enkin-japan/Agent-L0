"""Slice backend.log by HH:MM:SS stamps (log has no dates; file starts at the 10-30 15:45 boot).
Prints line range of [start,end] and a tag histogram; with --grep REGEX prints matching lines (cut to 300ch).
Usage: python3 -I log_window.py <log> HH:MM:SS HH:MM:SS [--grep REGEX] [--all]"""
import re, sys, collections
p, a, b = sys.argv[1:4]; g = None; allp = "--all" in sys.argv
if "--grep" in sys.argv: g = re.compile(sys.argv[sys.argv.index("--grep") + 1])
ts = re.compile(r"\b(\d\d:\d\d:\d\d)\b"); cur = None; first = last = None; tags = collections.Counter()
lines = open(p, encoding="utf-8", errors="replace").read().split("\n")
for i, l in enumerate(lines, 1):
    m = ts.search(l[:40])
    if m: cur = m.group(1)
    if cur and a <= cur <= b:
        first = first or i; last = i
        t = re.match(r"\[([^\]]+)\]|(\w+):", l); tags[(t.group(1) or t.group(2)) if t else "?"] += 1
        if allp or (g and g.search(l)): print(f"{i}: {l[:300]}")
print("RANGE", first, last, "TAGS", tags.most_common(25))
