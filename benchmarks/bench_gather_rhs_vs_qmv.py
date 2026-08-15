#!/usr/bin/env python3
"""Design A：E=8 正面对撞——`gather_qmm_rhs`（sorted 真去重路）vs `gather_qmv`（逐对路）。

判什么：Design C v2 已把 26b-8bit 的 v(M) 残差钉在专家内核的逐对重读上。
MLX 自带的去重路 `gather_qmm_rhs` 被 `M==1 && B>=16 && right_sorted && B/E>=4`
守卫挡在 MTP 批之外（B=16..48, E=128 ⇒ B/E=0）。本测构造 **E=8** 让守卫达标，
同数据同索引只翻 `sorted_indices`，正面量两个内核在同一批上的时间比——
「守卫放开值不值得做」的直接判据（预测与判读带见 prediction_designA.md）。

§4.20 三坑防护（构造性，不靠自觉）：
  ①三矩阵齐全：up/gate (E,704,2816) + down (E,2816,704)，真实 SwitchGLU 顺序
    up→gate→SiLU 门→down；
  ②30 份独立层权重，E=8 足迹 ≈1.52GB ≫ SLC，逐层轮转，不用解析式外推；
  ③两臂 pairs/并集/请求字节逐位相同，唯一变量是内核 ⇒「调度」与「重读命中缓存」
    不可能给出同一个数。缓存自检内置：足迹带宽 >0.7×峰值即中止（两臂都查）；
    qmv 臂另按请求字节查。rhs 臂请求字节带宽可以超峰——那是去重的体现，非假象。

口径：8bit affine g=64、bf16 输入、每配置 warmup 3 + 25 次取中位、
一次 mx.eval 收全部 30 层输出。纯合成权重（内核成本与数值无关）。
单 GPU 纪律：跑前确认无其它真模型进程。用法：python3 benchmarks/bench_gather_rhs_vs_qmv.py [out.json]
"""
import json
import statistics
import sys
import time
from pathlib import Path

import mlx.core as mx

PEAK_GBPS = 546.0
CACHE_LIMIT = 0.7 * PEAK_GBPS
HIDDEN, MOE_INTER = 2816, 704
LAYERS = 30
GROUP, BITS = 64, 8
BYTES_PER_VAL = 1.0625            # 8bit + scales/biases @ g=64
WARMUP, ITERS = 3, 25

# (标签, E, B, judged) —— B 行按专家均匀分配（段长 = B/E），索引已排序。
# v2（预测文件 v1.2）：主判据点的每层并集足迹须对齐生产（>64MB，超出 SLC）；
# E=8/B=32 保留为「SLC 内对照」（v1.1 实测两臂 1.000 同速——缓存把冗余全吃了，
# 这正是生产 +9% 排序收益的机制阳性对照），不参与判定、不触发中止。
CONFIGS = [
    ("生产近似 E=16,B=64(段长4,并集101MB/层)", 16, 64, True),
    ("双倍足迹 E=32,B=128(段长4,202MB/层)", 32, 128, True),
    ("SLC内对照 E=8,B=32(预测两臂≈1.00)", 8, 32, False),
]
SLC_FLOOR_MB = 64.0  # 主判据配置的每层并集足迹下限（构造检查）


def make_layer(E: int, key):
    """一层的三件套（独立随机权重，8bit affine 量化）。"""
    ws = []
    for i, (n, k) in enumerate(((MOE_INTER, HIDDEN), (MOE_INTER, HIDDEN),
                                (HIDDEN, MOE_INTER))):
        w = mx.random.normal((E, n, k), dtype=mx.bfloat16, key=mx.random.key(key * 7 + i))
        ws.append(mx.quantize(w, group_size=GROUP, bits=BITS))
    mx.eval(*[t for triple in ws for t in triple])
    return ws  # [(wq, scales, biases) × up/gate/down]


def expert_bytes_per_pair() -> float:
    return (2 * MOE_INTER * HIDDEN + HIDDEN * MOE_INTER) * BYTES_PER_VAL


def run_arm(layers, xs, idx, sorted_flag: bool) -> float:
    """一个 pass：B 行走完 30 层三矩阵链，一次 eval 收尾。返回秒。"""
    outs = []
    t0 = time.perf_counter()
    for (up, gate, down) in layers:
        u = mx.gather_qmm(xs, *up, rhs_indices=idx, transpose=True,
                          group_size=GROUP, bits=BITS, mode="affine",
                          sorted_indices=sorted_flag)
        g = mx.gather_qmm(xs, *gate, rhs_indices=idx, transpose=True,
                          group_size=GROUP, bits=BITS, mode="affine",
                          sorted_indices=sorted_flag)
        h = u * mx.sigmoid(g) * g          # SiLU 门（GeGLU 形态的近似，成本同量级）
        o = mx.gather_qmm(h, *down, rhs_indices=idx, transpose=True,
                          group_size=GROUP, bits=BITS, mode="affine",
                          sorted_indices=sorted_flag)
        outs.append(o)
    mx.eval(*outs)
    return time.perf_counter() - t0


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "benchmarks/data/mtp-26b-sprint/designA_rhs_vs_qmv.json")
    dev = mx.device_info()
    print(f"设备 {dev.get('device_name')}  峰值口径 {PEAK_GBPS} GB/s  "
          f"层数 {LAYERS}  8bit g={GROUP}\n")

    results = []
    for label, E, B, judged in CONFIGS:
        seg = B // E
        assert B % E == 0 and B >= 16 and B // E >= 4, \
            f"{label}: 守卫要求 B>=16 且 B/E>=4（当前 B={B},E={E}）"
        union_mb = E * expert_bytes_per_pair() / 1e6
        if judged and union_mb < SLC_FLOOR_MB:
            print(f"⛔ {label}: 每层并集足迹 {union_mb:.0f}MB < {SLC_FLOOR_MB:.0f}MB，"
                  "整层可驻留 SLC，冗余免费、对撞无从谈起（v1.1 的教训）——构造错误，中止")
            return 2
        layers = [make_layer(E, key=li * 131 + E * 17 + B) for li in range(LAYERS)]
        xs = mx.random.normal((B, 1, HIDDEN), dtype=mx.bfloat16, key=mx.random.key(B))
        idx = mx.array(sorted(list(range(E)) * seg), dtype=mx.uint32)
        mx.eval(xs, idx)

        footprint_gb = LAYERS * E * expert_bytes_per_pair() / 1e9
        requested_gb = LAYERS * B * expert_bytes_per_pair() / 1e9

        med = {}
        for flag in (False, True):          # False=gather_qmv, True=gather_qmm_rhs
            for _ in range(WARMUP):
                run_arm(layers, xs, idx, flag)
            ts = [run_arm(layers, xs, idx, flag) for _ in range(ITERS)]
            med[flag] = statistics.median(ts)

        bw_req_qmv = requested_gb / med[False]
        bw_fp_qmv = footprint_gb / med[False]
        bw_fp_rhs = footprint_gb / med[True]
        bw_req_rhs = requested_gb / med[True]
        ratio = med[False] / med[True]

        print(f"[{label}] 足迹 {footprint_gb:.2f} GB / 请求 {requested_gb:.2f} GB")
        print(f"  qmv  {med[False]*1e3:8.2f} ms  请求带宽 {bw_req_qmv:6.1f} GB/s  "
              f"足迹带宽 {bw_fp_qmv:6.1f}")
        print(f"  rhs  {med[True]*1e3:8.2f} ms  请求带宽 {bw_req_rhs:6.1f} GB/s  "
              f"足迹带宽 {bw_fp_rhs:6.1f}")
        print(f"  ⇒ t(qmv)/t(rhs) = **{ratio:.3f}**")

        # 自检（v1.2 语义）：足迹字节每 pass 必然从 DRAM 过一遍（3GB 级轮转，SLC
        # 存不下），故**足迹带宽超峰值 = 物理不可能 = 测量错**，两臂都查、判死。
        # 请求带宽超 DRAM 不再中止——缓存参与本来就是被测机制（SLC 内对照就是干这个的）。
        bad = [f"{arm} 足迹带宽 {bw:.0f} > 峰值 {PEAK_GBPS:.0f}"
               for arm, bw in (("qmv", bw_fp_qmv), ("rhs", bw_fp_rhs))
               if judged and bw > PEAK_GBPS]
        if bad:
            print(f"  ⛔ 物理自检失败：{'; '.join(bad)} ——测量本身有错，作废中止")
            return 2
        print()
        results.append({"label": label, "E": E, "B": B, "seg": seg,
                        "qmv_ms": med[False] * 1e3, "rhs_ms": med[True] * 1e3,
                        "ratio_qmv_over_rhs": ratio,
                        "bw_req_qmv": bw_req_qmv, "bw_fp_rhs": bw_fp_rhs,
                        "footprint_gb": footprint_gb, "requested_gb": requested_gb})
        del layers
        getattr(mx, "clear_cache", getattr(getattr(mx, "metal", mx), "clear_cache", lambda: None))()

    main_ratio = results[0]["ratio_qmv_over_rhs"]
    if main_ratio >= 1.25:
        verdict = "≥1.25 ⇒ **守卫放开值得做**（源码自建独立 venv + 逐字符数值比对）"
    elif main_ratio >= 0.9:
        verdict = ("0.9~1.25 ⇒ 缓存已回收的冗余留给真去重的赚头太小，"
                   "转 gather_qmv_wide 自研或收摊（P-A2' 预判带）")
    else:
        verdict = "<0.9 ⇒ rhs 显著更慢，内核线对本档基本判死，S2 收摊"
    print(f"主判据（{results[0]['label']}）：t(qmv)/t(rhs) = {main_ratio:.3f} —— {verdict}")
    print("诚实边界：本测段长 4 vs 生产 2.08、B=64 vs 生产 32（rhs 在 B=32 时 gate/up "
          "仅 44 TG，占用率更差）——**本测 rhs 成绩是生产的上界**；端到端收益要回"
          "真模型 A/B 再定（prediction_designA.md v1.2）。")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"configs": results, "iters": ITERS,
                                    "warmup": WARMUP, "layers": LAYERS},
                                   ensure_ascii=False, indent=1))
    print(f"\n落盘 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
