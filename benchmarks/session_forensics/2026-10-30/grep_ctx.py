"""Print up to N context snippets around a regex in a file (unicode-safe). Usage: python3 -I grep_ctx.py <file> <regex> [N] [ctx]"""
import re, sys
t = open(sys.argv[1], encoding="utf-8", errors="replace").read()
N = int(sys.argv[3]) if len(sys.argv) > 3 else 5; C = int(sys.argv[4]) if len(sys.argv) > 4 else 150
for i, m in enumerate(re.finditer(sys.argv[2], t)):
    if i >= N: break
    print(f"@{m.start()}: ...{t[max(0,m.start()-C):m.end()+C]!r}...")
print("TOTAL", len(re.findall(sys.argv[2], t)))
