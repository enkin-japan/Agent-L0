#!/usr/bin/env python3
"""§S.15 定位探针：旋转态 trim 到底坏在哪一步（几何 vs 掩码）。

〔2026-08-14 根修后留用〕本探针当初用来定位伤口（结论：几何，不是掩码），
现留作**几何检视器**——它不加载补丁，跑出来看到的是上游原始行为；要看修后行为，
在 main() 里 install() 一下即可。后续 MTP 波动 KV 布局时，这是最快的对照工具。

不比 logits，只盯 RotatingKVCache 的三个量（offset / _idx / keys.shape[2]）
和一个事实：**被拒草稿 R 的 KV 内容，回滚后是否还物理留在缓存里**。

判据：
  - is_trimmable() 在旋转态返回 False = 上游自己声明该缓存不可裁；
    而 rollback_speculative_cache 无条件调 trim() —— 若成立，伤口在调用方。
  - 回滚 + 喂探针后，把缓存内容与「基线路径」逐位比对：
    若残留 R 的行 → trim 只改了记账、没搬内容。

用法：python3 scripts/probe_spec_rollback_geometry.py
"""
import sys
from pathlib import Path

import mlx.core as mx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MODEL_DIR = str(Path(__file__).resolve().parent.parent / "models" / "gemma-4-e4b-it-4bit")


def main():
    from mlx_vlm import load
    from mlx_vlm.models import cache as cache_mod

    print(f"加载模型：{MODEL_DIR}\n")
    model, processor = load(MODEL_DIR)
    lm = model.language_model
    tok = processor.tokenizer if hasattr(processor, "tokenizer") else processor

    def feed(c, ids):
        out = lm(mx.array([ids], dtype=mx.int32), cache=c)
        logits = out.logits[:, -1, :]
        mx.eval(logits)
        return logits

    def snap(c, tag):
        """打印第一个 Rotating 层的几何。"""
        for i, cc in enumerate(c):
            if type(cc).__name__ == "RotatingKVCache":
                shp = None if cc.keys is None else cc.keys.shape[2]
                print(f"    [{tag}] layer{i} {type(cc).__name__}: "
                      f"offset={cc.offset} _idx={cc._idx} buf={shp} "
                      f"max_size={cc.max_size} keep={cc.keep} "
                      f"is_trimmable={cc.is_trimmable()}")
                return cc
        print(f"    [{tag}] 无 RotatingKVCache 层")
        return None

    base = tok.encode("The quick brown fox jumps over the lazy dog. " * 200,
                      add_special_tokens=False)

    for label, prompt_len in (("短上下文(128, 未旋转)", 128),
                              ("长上下文(900, 已旋转)", 900)):
        print(f"=== {label} ===")
        P = base[:prompt_len]
        A = base[prompt_len:prompt_len + 3]
        R = base[prompt_len + 100:prompt_len + 105]
        probe = base[prompt_len + 3:prompt_len + 4]

        # ---- 基线路径：P → A → probe（缓存里从未出现过 R）
        c1 = cache_mod.make_prompt_cache(lm)
        feed(c1, P)
        feed(c1, A)
        feed(c1, probe)
        rc1 = snap(c1, "基线 终态")

        # ---- 回滚路径：P → A+R → rollback → probe
        c2 = cache_mod.make_prompt_cache(lm)
        feed(c2, P)
        snap(c2, "回滚 prefill后")
        block = A + R
        feed(c2, block)
        rc2 = snap(c2, "回滚 喂A+R后")
        lm.rollback_speculative_cache(c2, None, len(A) - 1, len(block))
        snap(c2, "回滚 trim后")
        feed(c2, probe)
        rc2 = snap(c2, "回滚 喂probe后")

        # ---- 内容对比：两条路径的有效 KV 是否逐位一致
        if rc1 is not None and rc2 is not None:
            k1, k2 = rc1.keys, rc2.keys
            print(f"    缓冲区形状: 基线={k1.shape[2]} 回滚={k2.shape[2]}")
            if k1.shape == k2.shape:
                d = float(mx.abs(k1 - k2).max())
                print(f"    整缓冲区 max|Δkeys| = {d:.6f}")
                # 逐行定位：哪些槽位不同
                per_row = mx.abs(k1 - k2).max(axis=(0, 1, 3))
                mx.eval(per_row)
                bad = [i for i, v in enumerate(per_row.tolist()) if v > 1e-3]
                print(f"    不一致槽位数 = {len(bad)}"
                      + (f"，前 12 个 = {bad[:12]}" if bad else ""))
            else:
                print("    形状不同，无法逐位比对 —— 本身即几何分叉的证据")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
