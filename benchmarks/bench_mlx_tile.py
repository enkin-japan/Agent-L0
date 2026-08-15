#!/usr/bin/env python3
"""T6a 的 A/B 台：同一份源码，打补丁前 vs 后，量 `qmv_wide` 的 M 曲线。

判据只用「同一份 main 源码的前后对照」——自建件是 MLX main，wheel 是 0.32.0，
拿自建去比 wheel 会把"版本差"混进来（这正是我在别处栽过的口径病）。

工作集自证：每次 bench 前打印等效带宽，**超过峰值七成就说明在测缓存**（§4.20 教训）。
用法：python3 tile_ab.py <bits> <out.json>
"""
import json
import sys
import time

import mlx.core as mx

BITS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
OUT = sys.argv[2] if len(sys.argv) > 2 else None
GS, L = 64, 60
H, I = 5376, 21504                      # 31B: hidden / intermediate
SHAPES = [("qkv", 6144, H), ("o", H, 8192), ("gate", I, H), ("down", H, I)]
PEAK = 546.0                            # M4 Max GB/s

info = mx.device_info()
print(f"设备 {info.get('device_name')} / {info.get('architecture')}  "
      f"mlx {mx.__version__}  量化 {BITS}bit g{GS}")

WS = [(mx.quantize(mx.random.normal((n, k)).astype(mx.float16),
                   group_size=GS, bits=BITS), k) for _, n, k in SHAPES]
# 每次迭代请求的权重字节（含 scale/bias：每 GS 个权重 2 个 fp16）
BYTES = sum(n * k * BITS / 8 + (n * k / GS) * 4 for _, n, k in SHAPES)
print(f"单次迭代权重字节 {BYTES/1e6:.1f} MB（×{L} 层 = {BYTES*L/1e9:.2f} GB）")


def bench(M, iters=25):
    xs = [mx.random.normal((M, k)).astype(mx.float16) for _, k in WS]
    def f():
        return [mx.quantized_matmul(x, w, s, b, transpose=True,
                                    group_size=GS, bits=BITS)
                for x, ((w, s, b), _) in zip(xs, WS)]
    mx.eval(f())                        # 预热（含内核 JIT / 首次特化）
    ts = []
    for _ in range(iters):
        t0 = time.perf_counter()
        mx.eval(f())
        ts.append(time.perf_counter() - t0)
    ts.sort()
    return ts[len(ts) // 2]


rows = []
print(f"\n{'M':>3}{'×60层 ms':>11}{'每位边际':>10}{'相对M=1':>9}{'等效GB/s':>10}{'占峰值':>8}")
print("-" * 52)
prev = base = None
for M in range(1, 13):
    med = bench(M)
    tot = med * 1000 * L
    bw = BYTES / med / 1e9
    base = tot if base is None else base
    marg = "—" if prev is None else f"{tot - prev:.2f}"
    flag = "  ⚠️缓存?" if bw / PEAK > 0.7 else ""
    print(f"{M:>3}{tot:>11.2f}{marg:>10}{tot/base:>8.2f}×{bw:>10.0f}"
          f"{bw/PEAK*100:>7.1f}%{flag}")
    rows.append({"M": M, "ms_x60": tot, "gbs": bw})
    prev = tot

if OUT:
    json.dump({"bits": BITS, "mlx": mx.__version__, "rows": rows},
              open(OUT, "w"), indent=1)
    print(f"\n落盘 {OUT}")
