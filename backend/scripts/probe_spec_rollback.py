#!/usr/bin/env python3
"""Phase 1.3 前置检查：投机采样所需的「生成中途细粒度 KV 回滚」是否无损。

对比两条路径喂完相同 token 序列后的下一步 logits：
  基线：prefill(P) → feed(A)            → feed(probe) → logits_ref
  回滚：prefill(P) → feed(A+R)（R=被拒草稿）→ rollback_speculative_cache 裁掉 R
                                        → feed(probe) → logits_spec
无损判据：logits_ref 与 logits_spec 在数值容差内一致（bf16 累积误差级别）。

覆盖三个口径：
  1. 稠密 / 短上下文（未触发滑窗旋转）
  2. 稠密 / 长上下文（> sliding_window=512，RotatingKVCache 已旋转——最可疑场景）
  3. TurboQuant kv_bits=3.5 / 长上下文（量化态 trim + 覆盖写语义）

用法：python3 scripts/probe_spec_rollback.py
"""
import sys
import functools
from pathlib import Path

import mlx.core as mx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MODEL_DIR = str(Path(__file__).resolve().parent.parent / "models" / "gemma-4-e4b-it-4bit")

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    print(("  ✅ PASS " if cond else "  ❌ FAIL ") + name + (f"  [{extra}]" if extra else ""))
    PASS, FAIL = PASS + (1 if cond else 0), FAIL + (0 if cond else 1)


def main():
    from mlx_vlm import load
    from mlx_vlm.models import cache as cache_mod
    from mlx_vlm.generate import maybe_quantize_kv_cache

    # 测的是生产栈：装上 §S.15 根修（旋转态 trim 时序化+物理切尾）。
    # 不装则「稠密/长上下文」与「TurboQuant/长上下文」两格 max|Δ|=22。
    from framework.backends.mlx.patch.rotating_cache_trim import install
    install()

    print(f"加载模型：{MODEL_DIR}")
    model, processor = load(MODEL_DIR)
    lm = model.language_model
    tok = processor.tokenizer if hasattr(processor, "tokenizer") else processor

    check("模型实现 rollback_speculative_cache", hasattr(lm, "rollback_speculative_cache"))

    def feed(c, ids):
        out = lm(mx.array([ids], dtype=mx.int32), cache=c)
        logits = out.logits[:, -1, :]
        mx.eval(logits, *[s for cc in c for s in (cc.state if isinstance(cc.state, (list, tuple)) else [cc.state]) if s is not None])
        return logits

    def run_case(label, prompt_len, kv_bits=None):
        # 词表内取稳定 token 序列（避开特殊区），内容无所谓，只比状态一致性
        base = tok.encode("The quick brown fox jumps over the lazy dog. " * 200,
                          add_special_tokens=False)
        P = base[:prompt_len]
        A = base[prompt_len:prompt_len + 3]        # 接受的 3 个 token
        R = base[prompt_len + 100:prompt_len + 105]  # 被拒的 5 个草稿 token（取不同片段）
        probe = base[prompt_len + 3:prompt_len + 4]  # 回滚后喂入的探针 token

        quant = None
        if kv_bits is not None:
            quant = functools.partial(maybe_quantize_kv_cache, quantized_kv_start=0,
                                      kv_group_size=64, kv_bits=kv_bits,
                                      kv_quant_scheme="turboquant")

        def fresh():
            return cache_mod.make_prompt_cache(lm)

        # 基线：P → (量化) → A → probe
        c1 = fresh()
        feed(c1, P)
        if quant:
            quant(c1)
        feed(c1, A)
        ref = feed(c1, probe)

        # 回滚：P → (量化) → A+R 一次喂入 → rollback 裁掉 R → probe
        c2 = fresh()
        feed(c2, P)
        if quant:
            quant(c2)
        block = A + R                      # 模拟 verify_input：3 个接受 + 5 个被拒
        feed(c2, block)
        # rollback(accepted, block_size)：trim = block_size - accepted - 1
        # 语义对齐 _mtp_rounds：accepted=len(A)-1 时保留 A、裁掉 R
        lm.rollback_speculative_cache(c2, None, len(A) - 1, len(block))
        spec = feed(c2, probe)

        diff = float(mx.abs(ref - spec).max())
        # 判据：argmax 一致 + 数值差在 bf16 噪声级
        same_top = int(mx.argmax(ref)) == int(mx.argmax(spec))
        check(f"{label}: argmax 一致", same_top)
        check(f"{label}: logits 最大偏差 < 0.05", diff < 0.05, f"max|Δ|={diff:.6f}")
        # offset 校验：回滚后缓存长度应为 prompt+A+probe
        expect = prompt_len + len(A) + len(probe)
        offs = {int(getattr(c, "offset", -1)) for c in c2}
        check(f"{label}: 全层 offset == {expect}", offs == {expect} or offs == {expect % 512, expect},
              f"offsets={sorted(offs)[:4]}")

    run_case("稠密/短上下文(128)", 128)
    run_case("稠密/长上下文(900, 滑窗已旋转)", 900)
    run_case("TurboQuant3.5/长上下文(900)", 900, kv_bits=3.5)

    print(f"\n===== 汇总 =====\n{PASS}/{PASS + FAIL} 通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
