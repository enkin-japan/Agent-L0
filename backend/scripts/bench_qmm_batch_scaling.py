"""MLX 量化矩阵乘的批量摊薄缺陷复现件（可直接上报上游）。

结论：`mx.quantized_matmul` 在 M<=2 时权重读取摊薄良好（S=1→2 边际 0.11ms），
M>2 后**每多一行约多花 9ms**（31B 形状 ×60 层），既达不到峰值算力（S=4 时 3.2
TFLOPS = 9% of 35T）也达不到峰值带宽（203GB/s = 37% of 546）。
同形状 **fp16 稠密对照在 S>=4 后完全平坦**（181→173→171ms）——那是正常 GEMM 的
签名（权重读一遍，喂几行都一样）。故这是量化路专有的内核缺陷，非硬件限制。
`mx.compile` 对该曲线**零影响**（-0.0%~+0.5%），说明开销在 Metal 内核内部而非派发。

对本项目的意义：MTP 验证步 S=γ+1，该缺陷直接给 Mac 上的投机采样定了天花板——
31B-4bit 若能摊薄可达 2.0×，实际只有 1.53×；8bit 档因目标步本就贵，已在 99% 上界。

原注：内核微基准 v2：分离「量化矩阵乘」与「注意力」随验证位数 S 的成本曲线。

v1 的坑：每个矩阵乘单独 mx.eval，S 小时固定派发开销占主导（有效带宽只有 135GB/s，
且单层×60层 68.7ms 比整模型实测 40ms 还大 = 测的是派发不是吞吐）。
v2 改为**一次 eval 覆盖全部输出**，并把注意力单独成组。

问题：31B-4bit 验证步每多一位实测多花 ~9ms，峰值算力只需 1.8ms。是谁的锅？
"""
import time
import mlx.core as mx
import mlx.core.fast as mxf

H, I, L = 5376, 21504, 60
NH, NKV, HD, SW = 32, 16, 256, 1024
GS, BITS = 64, 4


def qweight(o, i):
    return mx.quantize(mx.random.normal((o, i)).astype(mx.float16),
                       group_size=GS, bits=BITS)


WS = [qweight(6144, H), qweight(H, 8192), qweight(I, H), qweight(H, I)]


def bench(fn, iters=30):
    mx.eval(fn())
    t0 = time.perf_counter()
    for _ in range(iters):
        mx.eval(fn())
    return (time.perf_counter() - t0) / iters


def mm_layer(S):
    xs = [mx.random.normal((S, w[0].shape[1] * (32 // BITS))).astype(mx.float16)
          for w in WS]
    def f():
        return [mx.quantized_matmul(x, w, s, b, transpose=True,
                                    group_size=GS, bits=BITS)
                for x, (w, s, b) in zip(xs, WS)]
    return f


def attn_layer(S, kvlen):
    q = mx.random.normal((1, NH, S, HD)).astype(mx.float16)
    k = mx.random.normal((1, NKV, kvlen, HD)).astype(mx.float16)
    v = mx.random.normal((1, NKV, kvlen, HD)).astype(mx.float16)
    def f():
        return mxf.scaled_dot_product_attention(q, k, v, scale=1.0 / (HD ** 0.5))
    return f


print("31B 形状（hidden 5376 / inter 21504 / 60 层 / 4bit-g64 / 32Q-16KV heads / hd 256）")
print(f"KV 长度取滑窗上限 {SW}\n")
print(f"{'S':>3}{'矩阵乘×60 ms':>14}{'注意力×60 ms':>14}{'合计 ms':>10}{'相对S=1':>10}{'每位边际ms':>12}")
print("-" * 64)
prev = None
for S in (1, 2, 3, 4, 6, 8):
    mm = bench(mm_layer(S)) * L * 1000
    at = bench(attn_layer(S, SW)) * L * 1000
    tot = mm + at
    marg = "—" if prev is None else f"{(tot - prev[0]) / (S - prev[1]):.2f}"
    base = tot if prev is None else prev[2]
    print(f"{S:>3}{mm:>14.2f}{at:>14.2f}{tot:>10.2f}"
          f"{tot / (prev[2] if prev else tot):>10.2f}×{marg:>12}")
    prev = (tot, S, prev[2] if prev else tot)

print("\n=== 对照：mx.compile 后同一条曲线（提案3 落在此处）===")
print(f"{'S':>3}{'裸 ms':>10}{'compile ms':>12}{'变化':>9}")
print("-" * 34)
for S in (1, 2, 4, 6, 8):
    f = mm_layer(S)
    cf = mx.compile(f)
    raw = bench(f) * L * 1000
    com = bench(cf) * L * 1000
    print(f"{S:>3}{raw:>10.2f}{com:>12.2f}{(com/raw-1):>8.1%}")

print("\n=== 对照：同形状 fp16 稠密矩阵乘（看是不是量化路专有）===")
WD = [mx.random.normal((o, i)).astype(mx.float16) for o, i in
      ((6144, H), (H, 8192), (I, H), (H, I))]
print(f"{'S':>3}{'fp16 ms×60':>12}{'每位边际':>10}")
print("-" * 26)
prev = None
for S in (1, 2, 4, 6, 8):
    xs = [mx.random.normal((S, w.shape[1])).astype(mx.float16) for w in WD]
    g = lambda: [x @ w.T for x, w in zip(xs, WD)]
    t = bench(g) * L * 1000
    marg = "—" if prev is None else f"{(t-prev[0])/(S-prev[1]):.2f}"
    print(f"{S:>3}{t:>12.2f}{marg:>10}")
    prev = (t, S)
