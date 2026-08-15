#!/usr/bin/env python3
"""把 A100 侧的 31B 逐格 JSON 汇成跨平台对照表（稠密档，T1 的补格）。

用法：python3 benchmarks/compare_31b.py <cells目录> [前缀]
      python3 benchmarks/compare_31b.py /tmp/cells d31_fp8

为什么要单独一个脚本而不是手抄：本项目栽过"拿三项不对齐的数当跨平台差距"
（`MTP_PROJECT.md` §4.18）。这里把口径字段（quant / max_model_len / prompt_tokens /
max_tokens / draft）一并打出来，**对不齐就当场看得见**。

报的量：
  加速比 = tps(γ) / tps(γ=0)
  D      = AL / 加速比    ← **这才是跨平台可比的量**（无量纲，硬件带宽自动约掉）
本机侧同口径的 D 由 `backend/scripts/mtp_cost_decomp.py` 给出。
"""
import json
import sys
from pathlib import Path

CELLS = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/cells")
PREFIX = sys.argv[2] if len(sys.argv) > 2 else "d31_fp8"

# 本机 31b-8bit + **同一个官方 bf16 草稿件**（`mtp_cost_decomp.py` 修正后重测，
# repeats=3、净漂移 −0.8%、探针扰动 −0.5%）。基线 13.88 tps。
MLX = {  # γ: (加速比, AL, D)
    1: (1.585, 1.739, 1.097), 2: (1.982, 2.299, 1.160),
    3: (2.243, 2.714, 1.213), 4: (2.327, 3.008, 1.292),
    5: (1.967, 3.252, 1.653), 7: (1.668, 3.333, 1.999),
    9: (1.265, 3.627, 2.875),
}


def main() -> int:
    rows = {}
    for f in sorted(CELLS.glob(f"{PREFIX}_g*.json")):
        d = json.loads(f.read_text())
        rows[int(d["gamma"])] = d
    if 0 not in rows:
        print(f"缺基线格（γ=0），无法算加速比。目录 {CELLS} 前缀 {PREFIX}")
        return 1

    base = rows[0]
    base_tps = base["tps"]

    print(f"目标件 {base['target']}   量化 {base.get('quant')}   "
          f"草稿件 {rows[max(rows)].get('draft')}")
    print(f"口径 prompt {base['prompt_tokens']} tok / max_tokens {base['max_tokens']} / "
          f"max_model_len {base.get('max_model_len')} / eager={base.get('eager')}")
    print(f"基线 {base_tps:.2f} tps\n")

    # 口径漂移哨：各格之间只允许 γ 不同
    keys = ("target", "quant", "max_model_len", "max_tokens", "prompt_tokens", "eager")
    for g, d in sorted(rows.items()):
        diff = [k for k in keys if d.get(k) != base.get(k)]
        if diff:
            print(f"⚠️ γ={g} 与基线格口径不一致：{diff} —— 该格不可与基线相除")

    print(f"{'γ':>3} {'tps':>8} {'加速比':>8} {'AL':>7} {'接受率':>8} {'轮数':>6} "
          f"{'D=AL/加速':>10} | {'mlx 加速':>9} {'mlx D':>7} {'D 差':>7}")
    print("-" * 92)
    for g, d in sorted(rows.items()):
        if g == 0:
            print(f"{0:>3} {d['tps']:>8.2f} {1.0:>8.3f} {1.0:>7.3f} "
                  f"{'—':>8} {0:>6} {1.0:>10.3f} |")
            continue
        sp = d["tps"] / base_tps
        D = d["AL"] / sp
        m = MLX.get(g)
        tail = (f"| {m[0]:>9.3f} {m[2]:>7.3f} {(D/m[2]-1)*100:>+6.1f}%" if m else "|")
        print(f"{g:>3} {d['tps']:>8.2f} {sp:>8.3f} {d['AL']:>7.3f} "
              f"{d['rate']*100:>7.1f}% {d['rounds']:>6} {D:>10.3f} {tail}")

    print("\n逐位接受率 P(接受数≥k)：")
    for g, d in sorted(rows.items()):
        if g and d.get("P_ge_k"):
            print(f"  γ={g}: " + "  ".join(f"{p*100:5.1f}%" for p in d["P_ge_k"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
