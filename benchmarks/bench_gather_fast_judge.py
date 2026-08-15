#!/usr/bin/env python3
"""S3(iii) 初探：gather fast 判据（K%512 vs K%256）对 26b 专家矩阵的影响。

同一脚本两个 venv 各跑一遍（backend/.venv = wheel 0.32.0；backend/.venv-mlxsrc =
main 自建），比 gate/up（K=2816，main 下走 fast）与 down（K=704，两版都慢路，
阴性对照）。混杂声明与判读带见 prediction_fastjudge.md——本测是初探不是归因。

口径：E=128 真实形状、8 份独立层权重（足迹 6.5GB ≫ SLC）、真实 top-k 索引形态
（每 token 8 个不重复专家）、warmup 3 + 25 次中位、一次 eval 收全部输出。
单 GPU 纪律：跑前确认无其它真模型进程。
用法：<venv>/bin/python benchmarks/bench_gather_fast_judge.py <标签> [out.json]
"""
import json
import statistics
import sys
import time
from pathlib import Path

import mlx.core as mx

HIDDEN, MOE_INTER, E = 2816, 704, 128
LAYERS, TOPK = 8, 8
GROUP, BITS = 64, 8
WARMUP, ITERS = 3, 25
LABEL = sys.argv[1] if len(sys.argv) > 1 else "unlabeled"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(
    f"benchmarks/data/mtp-26b-sprint/fastjudge_{LABEL}.json")


def make_layer(key: int):
    ws = []
    for i, (n, k) in enumerate(((MOE_INTER, HIDDEN), (MOE_INTER, HIDDEN),
                                (HIDDEN, MOE_INTER))):
        w = mx.random.normal((E, n, k), dtype=mx.bfloat16,
                             key=mx.random.key(key * 7 + i))
        ws.append(mx.quantize(w, group_size=GROUP, bits=BITS))
    mx.eval(*[t for triple in ws for t in triple])
    return ws


def topk_indices(m_tok: int, key: int) -> mx.array:
    """每 token 从 128 里选 8 个不重复专家（真实路由形态），排序后返回 (m_tok*8,)。"""
    rows = []
    for t in range(m_tok):
        # argsort(uniform) 当洗牌用（两版 mlx 都有；permutation 未必）
        perm = mx.argsort(mx.random.uniform(shape=(E,), key=mx.random.key(key * 97 + t)))
        rows.append(perm[:TOPK].astype(mx.uint32))
    idx = mx.concatenate(rows)
    return mx.sort(idx)


def run_pass(layers, xs, idx, which: str, sorted_flag: bool) -> float:
    outs = []
    t0 = time.perf_counter()
    for (up, gate, down) in layers:
        if which in ("gateup", "all"):
            u = mx.gather_qmm(xs, *up, rhs_indices=idx, transpose=True,
                              group_size=GROUP, bits=BITS, mode="affine",
                              sorted_indices=sorted_flag)
            g = mx.gather_qmm(xs, *gate, rhs_indices=idx, transpose=True,
                              group_size=GROUP, bits=BITS, mode="affine",
                              sorted_indices=sorted_flag)
            outs += [u, g]
        if which in ("down", "all"):
            hs = mx.random.normal((xs.shape[0], 1, MOE_INTER), dtype=mx.bfloat16,
                                  key=mx.random.key(3))
            o = mx.gather_qmm(hs, *down, rhs_indices=idx, transpose=True,
                              group_size=GROUP, bits=BITS, mode="affine",
                              sorted_indices=sorted_flag)
            outs.append(o)
    mx.eval(*outs)
    return time.perf_counter() - t0


def main() -> int:
    print(f"[{LABEL}] mlx {mx.__version__}  设备 {mx.device_info().get('device_name')}")
    layers = [make_layer(li * 131 + 5) for li in range(LAYERS)]
    rows = []
    for m_tok in (1, 4):
        B = m_tok * TOPK
        idx = topk_indices(m_tok, key=B)
        xs = mx.random.normal((B, 1, HIDDEN), dtype=mx.bfloat16, key=mx.random.key(B))
        mx.eval(xs, idx)
        srt = B >= 16   # 生产：阈值 16
        for which in ("gateup", "down"):
            for _ in range(WARMUP):
                run_pass(layers, xs, idx, which, srt)
            ts = [run_pass(layers, xs, idx, which, srt) for _ in range(ITERS)]
            med = statistics.median(ts) * 1e3
            print(f"  M_tok={m_tok} B={B} {which:<7} 中位 {med:8.3f} ms"
                  f"（{LAYERS} 层, sorted={srt}）")
            rows.append({"m_tok": m_tok, "B": B, "which": which,
                         "sorted": srt, "median_ms": med})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"label": LABEL, "mlx_version": mx.__version__,
                               "layers": LAYERS, "rows": rows},
                              ensure_ascii=False, indent=1))
    print(f"落盘 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
