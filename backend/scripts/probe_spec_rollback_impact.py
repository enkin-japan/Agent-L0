#!/usr/bin/env python3
"""Phase 1.3 追加诊断：滑窗旋转态下回滚偏差的分布级影响评估。

已知：旋转后的 RotatingKVCache 被拒草稿会覆盖环内最老条目，trim 无法恢复 → 有偏差。
本探针回答：这个偏差对「采样分布」的实际影响多大？
  - 确定性对照：同一路径跑两遍的 logits 差（噪声基线）
  - KL(ref ‖ spec)：回滚路径 vs 基线路径的 softmax 分布散度
  - top-20 重叠率与 top-20 概率质量差
  - 连续多轮回滚（模拟真实投机：每轮都有被拒草稿）后的累计漂移
"""
import sys
from pathlib import Path

import mlx.core as mx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
MODEL_DIR = str(Path(__file__).resolve().parent.parent / "models" / "gemma-4-e4b-it-4bit")


def main():
    from mlx_vlm import load
    from mlx_vlm.models import cache as cache_mod

    model, processor = load(MODEL_DIR)
    lm = model.language_model
    tok = processor.tokenizer if hasattr(processor, "tokenizer") else processor

    def feed(c, ids):
        out = lm(mx.array([ids], dtype=mx.int32), cache=c)
        logits = out.logits[:, -1, :].astype(mx.float32)
        mx.eval(logits)
        return logits

    def dist_report(name, la, lb):
        pa = mx.softmax(la, axis=-1)
        pb = mx.softmax(lb, axis=-1)
        kl = float(mx.sum(pa * (mx.log(pa + 1e-12) - mx.log(pb + 1e-12))))
        ta = set(mx.argsort(la)[-20:].tolist()[0] if la.ndim > 1 else mx.argsort(la)[-20:].tolist())
        tb = set(mx.argsort(lb)[-20:].tolist()[0] if lb.ndim > 1 else mx.argsort(lb)[-20:].tolist())
        overlap = len(ta & tb)
        # top-1 概率差
        p1a = float(pa.max())
        p1b = float(pb.max())
        print(f"  {name}: KL={kl:.6f}  top20重叠={overlap}/20  top1概率 {p1a:.4f} vs {p1b:.4f}  "
              f"argmax{'一致' if int(mx.argmax(la)) == int(mx.argmax(lb)) else '不一致'}")
        return kl

    base = tok.encode("The quick brown fox jumps over the lazy dog. " * 300,
                      add_special_tokens=False)
    P = base[:900]
    A = base[900:903]
    R = base[1000:1005]
    probe = base[903:904]

    # 1) 确定性对照：同一基线路径跑两遍
    c1 = cache_mod.make_prompt_cache(lm)
    feed(c1, P); feed(c1, A); ref1 = feed(c1, probe)
    c2 = cache_mod.make_prompt_cache(lm)
    feed(c2, P); feed(c2, A); ref2 = feed(c2, probe)
    print("确定性对照（应≈0）：")
    dist_report("baseline vs baseline", ref1, ref2)

    # 2) 单轮回滚偏差
    c3 = cache_mod.make_prompt_cache(lm)
    feed(c3, P)
    feed(c3, A + R)
    lm.rollback_speculative_cache(c3, None, len(A) - 1, len(A) + len(R))
    spec1 = feed(c3, probe)
    print("单轮回滚（5 个被拒草稿）：")
    dist_report("baseline vs rollback×1", ref1, spec1)

    # 3) 连续 10 轮回滚累计漂移（每轮 3 接受 + 5 拒绝，模拟低接受率投机）
    c4 = cache_mod.make_prompt_cache(lm)
    feed(c4, P)
    pos = 900
    for _ in range(10):
        a = base[pos:pos + 3]
        r = base[pos + 200:pos + 205]
        feed(c4, a + r)
        lm.rollback_speculative_cache(c4, None, len(a) - 1, len(a) + len(r))
        pos += 3
    # 对照：干净路径喂同样 30 个 token
    c5 = cache_mod.make_prompt_cache(lm)
    feed(c5, P)
    feed(c5, base[900:930])
    probe2 = base[930:931]
    drift = feed(c4, probe2)
    clean = feed(c5, probe2)
    print("连续 10 轮回滚累计（共 50 个被拒草稿写入环）：")
    dist_report("clean vs rollback×10", clean, drift)


if __name__ == "__main__":
    main()
