#!/usr/bin/env python3
"""T4 判定器：读两臂落盘，找首个分歧产出步，比对该步两路 logprobs。

判据（§1.7.3 预注册）：
  设分歧步 D，基线在 D 的 top1-top2 间距 = gap；两路在 D 的共同 top-id 上的
  logprob 最大差 = diff。
  gap < diff  → 平局翻转（数值噪声，良性）；
  gap ≫ diff 且两路在被惩罚 token 上的 logprob 系统性不一致 → 接线嫌疑。
另报：分歧前全部对齐步的 diff 分布（两路数值噪声的地板），供量级对照。
"""
import json
import sys
from pathlib import Path

# 缺省读同目录入库件（08-16 复审修：首版硬编码会话级 scratchpad，入库即失联）；
# 或显式传两个路径：t4_verdict.py <base.json> <spec.json>。全给或全不给——
# 只给一个会静默混用显式 base + 缺省 spec（评审 nit）。
if len(sys.argv) not in (1, 3):
    sys.exit("用法: t4_verdict.py [<base.json> <spec.json>]（两个路径要么都给要么都省）")
_HERE = Path(__file__).resolve().parent
base = json.load(open(sys.argv[1] if len(sys.argv) > 1 else _HERE / "t4_base.json"))
spec = json.load(open(sys.argv[2] if len(sys.argv) > 2 else _HERE / "t4_spec.json"))

rows_b = base["rows"]
emit_b = [r["ids"][0] for r in rows_b]           # temp=0：每行产出即 top-1

# 投机臂：ROWS 只含走查内的行。产出序列 = [bonus 占位（=基线首 token，两路
# 首 token 必同——若不同 D=0 而占位也恰好触发重查）] + 各轮 new_tokens。
rows_s = spec["rows"]
walks = spec["walks"]
emit_s = [(emit_b[0], None)]                     # bonus：无 logits 行
for w in walks:
    for j, tok in enumerate(w["tokens"]):
        ri = w["row0"] + j
        assert rows_s[ri]["ids"][0] == tok, (
            f"走查行 {ri} top1 {rows_s[ri]['ids'][0]} ≠ 产出 {tok}：映射假设破产")
        emit_s.append((tok, ri))

print(f"基线产出 {len(emit_b)}  投机产出 {len(emit_s)}  "
      f"(报告值 base {base['tokens']} / spec {spec['tokens']})")

def lp(row):
    return dict(zip(row["ids"], row["vals"]))

def cross_diff(rb, rs):
    a, b = lp(rb), lp(rs)
    common = set(a) & set(b)
    if not common:
        return None
    return max(abs(a[i] - b[i]) for i in common)

n = min(len(emit_b), len(emit_s))
D = next((i for i in range(n) if emit_b[i] != emit_s[i][0]), None)
if D is None:
    print(f"前 {n} 个产出完全一致——本场没有分歧（分歧依赖近平局位，逐场随机）。")
    sys.exit(0)

pre = [cross_diff(rows_b[i], rows_s[emit_s[i][1]])
       for i in range(D) if emit_s[i][1] is not None]
pre = [d for d in pre if d is not None]
assert emit_s[D][1] is not None, "分歧在 bonus 位，需补录预填行"
rb, rs = rows_b[D], rows_s[emit_s[D][1]]
gap_b = rb["vals"][0] - rb["vals"][1]
gap_s = rs["vals"][0] - rs["vals"][1]
diff_D = cross_diff(rb, rs)
tok_b, tok_s = emit_b[D], emit_s[D][0]

print(f"\n首分歧步 D={D}: 基线出 {tok_b}，投机出 {tok_s}")
print(f"基线该步 top2: {rb['ids'][:2]} vals {rb['vals'][:2]}  gap={gap_b:.6f}")
print(f"投机该步 top2: {rs['ids'][:2]} vals {rs['vals'][:2]}  gap={gap_s:.6f}")
# diff_D 可为 None（top16 无交集）——下方判定行已用 `or 1e9` 兜住，这里同样兜
# （评审 nit：原 f"{diff_D:.6f}" 在 None 上抛 TypeError）
print("两路共同 top-id 最大 logprob 差 diff(D)="
      + (f"{diff_D:.6f}" if diff_D is not None else "N/A（top16 无交集）"))
if pre:
    print(f"分歧前 {len(pre)} 对齐步的 diff：中位 {sorted(pre)[len(pre)//2]:.6f} "
          f"最大 {max(pre):.6f}")
swapped = tok_b in rs["ids"][:2] and tok_s in rb["ids"][:2]
print(f"两路互见对方选择于各自 top2: {swapped}")
verdict = "平局翻转（良性）" if (gap_b < (diff_D or 1e9) and swapped) else "需进一步排查"
print(f"\n判定：gap({gap_b:.6f}) {'<' if gap_b < (diff_D or 1e9) else '>='} "
      f"diff({diff_D:.6f}) 且互换={swapped} → **{verdict}**")
