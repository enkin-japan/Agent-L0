# D43 (a) b2 前置复算（analyst，2026-11-04；结论见 docs/plans/2026-10-31-context-management-plan.md §6 D43）：把 c_clue_noise.py 输出汇总成逐 run 行
# 重跑（仓库根）：python3 -I benchmarks/context_knee/d43_pre/c_summarize.py c_out_orch-turn-w1.txt
# 原样搬自会话 scratchpad，只加这三行；结论数字以当日输出为准，不随 harness 改动更新。
"""Summarize c_clue_noise.py output into per-run rows. Usage: python3 -I c_summarize.py c_out_<arm>.txt"""
import re, sys, collections
run = None; rows = []; kinds = collections.defaultdict(list)
for l in open(sys.argv[1]):
    if l.startswith("## run"): run = l.split()[2] + l.split()[3]; cur = {"run": run, "obs": l.split("obs=")[1].split()[0], "cards": []}; rows.append(cur)
    m = re.match(r"\s+card (\w+)\s+obs \w+ chars=(\d+) per-class=(\{.*?\}) naive_rank=(\d+)/(\d+) \(raw-escaped rank=(\d+|None)\) chars_to_include=(\d+); R1 rank/in_clue/len=\((\w+), (\w+), (\d+)\) R2=\((\w+), (\w+), (\d+)\)", l)
    if m:
        k = m.group(1); cur["cards"].append(f"{k[:4]}:naive {m.group(4)}/{m.group(5)} {m.group(7)}ch R1 {m.group(8)} R2 {m.group(11)}")
        kinds[k].append((int(m.group(4)), int(m.group(5)), int(m.group(7)), m.group(8), m.group(6)))
    if l.startswith("  run totals"): cur["tot"] = l.split(":", 1)[1].split(";")[0].strip(); cur["ne"] = l.split("clue ")[1].strip()
for r in rows: print(r["run"], "obs", r["obs"], r.get("tot"), r.get("ne"), "|", "; ".join(r["cards"]))
for k, v in kinds.items():
    nr = sorted(x[0] for x in v); nm = sorted(x[1] for x in v); ch = sorted(x[2] for x in v)
    print(k, "n", len(v), "naive rank median/max", nr[len(nr)//2], nr[-1], "matches in host obs median/max", nm[len(nm)//2], nm[-1],
          "chars-to-include median/max", ch[len(ch)//2], ch[-1], "R1 ranks", sorted(x[3] for x in v), "raw-escaped ranks differ", sum(1 for x in v if str(x[0]) != x[4]))
