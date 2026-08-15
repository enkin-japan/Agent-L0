#!/usr/bin/env python3
"""T6a：把 MLX `qmv_wide` 的输入向量瓦片上限从 5 提到 9。

两处改动（缺一不可）：
  ① mlx/backend/metal/quantized.cpp  `n_tiles = (M + 4) / 5` → `(M + CAP-1) / CAP`
     —— 决定 M 被切成几块；每块重读一遍权重。
  ② mlx/backend/metal/kernels/quantized.metal  `instantiate_quantized_all_wide`
     —— 内核按 `vecs_per_tg` 模板特化，只实例化了 2..5；不补实例化，①改了也没内核可派发。

为什么是 9 而不是更大：同文件 `get_qmv_batch_limit` 在本机（gen16/size 's'、K,N>4096）
返回 **10**，M≥10 会走 GEMM 路。故瓦片上限做到 9 恰好覆盖 dispatch_qmv 的全区间。

代价与风险：`U result[vecs_per_tg]` 与 `const device T* xv[vecs_per_tg]` 是每线程寄存器
数组，9 比 5 多约 27 个寄存器，**可能掉占用率**。这正是要实测的：
若掉占用率带来的损失大于省下的一遍权重读取，本改动就不成立。

用法：python3 patch_tile.py [apply|revert] [CAP]
"""
import re
import subprocess
import sys
from pathlib import Path

SRC = Path("/Volumes/enkin/project/mlx-src")
CPP = SRC / "mlx/backend/metal/quantized.cpp"
METAL = SRC / "mlx/backend/metal/kernels/quantized.metal"
MODE = sys.argv[1] if len(sys.argv) > 1 else "apply"
CAP = int(sys.argv[2]) if len(sys.argv) > 2 else 9

ORIG_CPP = "int n_tiles = (M + 4) / 5; // ceil(M / 5); tile size caps at 5"
ORIG_METAL_TAIL = ("  instantiate_quantized_wide_wrap(affine_qmv_wide, type, "
                   "group_size, bits, 5, 8)")


def apply() -> None:
    c = CPP.read_text()
    assert ORIG_CPP in c, "quantized.cpp 里的瓦片常量没对上（上游改过？）"
    c = c.replace(ORIG_CPP,
                  f"int n_tiles = (M + {CAP - 1}) / {CAP}; // [T6a] 瓦片上限 5 → {CAP}")
    CPP.write_text(c)

    m = METAL.read_text()
    assert ORIG_METAL_TAIL in m, "quantized.metal 的实例化列表没对上"
    extra = "".join(
        f" \\\n  instantiate_quantized_wide_wrap(affine_qmv_wide, type, "
        f"group_size, bits, {n}, 8)"
        for n in range(6, CAP + 1))
    m = m.replace(ORIG_METAL_TAIL, ORIG_METAL_TAIL + extra)
    METAL.write_text(m)
    print(f"已打补丁：瓦片上限 → {CAP}，新增实例化 vecs_per_tg = 6..{CAP}")


def revert() -> None:
    subprocess.run(["git", "-C", str(SRC), "checkout", "--",
                    "mlx/backend/metal/quantized.cpp",
                    "mlx/backend/metal/kernels/quantized.metal"], check=True)
    print("已还原两个文件")


if __name__ == "__main__":
    (apply if MODE == "apply" else revert)()
    subprocess.run(["git", "-C", str(SRC), "diff", "--stat"], check=False)
