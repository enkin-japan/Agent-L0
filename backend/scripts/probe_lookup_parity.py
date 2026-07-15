#!/usr/bin/env python3
"""Phase 3 验收探针：prompt-lookup 投机采样的贪心一致性 + 加速比 + 接受率。

对同一 prompt 分别跑 关闭投机 / 开启投机（temperature=0，其余参数与 engine
生产口径一致：repetition_penalty=1.1/ctx50），断言 token 序列逐位一致，
并对比 decode tps。覆盖三类负载：
  A. 复读型（引用/重复指令——n-gram 命中率高，投机的主场）
  B. 自由生成型（低命中——检验无命中时的开销回退）
  C. 长上下文引用型（>512 滑窗旋转 + 前缀里找草稿）
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MODEL_DIR = str(Path(__file__).resolve().parent.parent / "models" / "gemma-4-e4b-it-4bit")

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    print(("  ✅ PASS " if cond else "  ❌ FAIL ") + name + (f"  [{extra}]" if extra else ""))
    PASS, FAIL = PASS + (1 if cond else 0), FAIL + (0 if cond else 1)


def run(model, processor, prompt, speculative, max_tokens=300):
    from framework.backends.mlx.patch.gemma4_generate import stream_generate
    from mlx_vlm.generate import PromptCacheState

    stats = {"drafted": 0, "accepted": 0, "rounds": 0}
    kwargs = dict(
        temperature=0.0, min_p=0.0, max_tokens=max_tokens,
        repetition_penalty=1.1, repetition_context_size=50,
        prompt_cache_state=PromptCacheState(),
        enable_thinking=False,
    )
    if speculative:
        kwargs["speculative"] = {"mode": "prompt_lookup", "num_draft_tokens": 8,
                                 "ngram_max": 3, "ngram_min": 2}
        kwargs["spec_stats"] = stats

    tokens, text, lps = [], "", []
    t0 = time.perf_counter()
    first_tok_t = None
    last = None
    for r in stream_generate(model, processor, prompt, None, None, **kwargs):
        if first_tok_t is None:
            first_tok_t = time.perf_counter()
        tokens.append(r.token)
        lps.append(r.logprobs)
        text += r.text
        last = r
    wall = time.perf_counter() - (first_tok_t or t0)
    n = last.generation_tokens if last else 0
    tps = n / wall if wall > 0 else 0.0
    return {"tokens": tokens, "text": text, "n": n, "tps": tps, "stats": stats,
            "logprobs": lps}


def main():
    from mlx_vlm import load
    from framework.dialects.gemma4.prompt_builder import PromptBuilder

    print(f"加载模型：{MODEL_DIR}")
    model, processor = load(MODEL_DIR)
    builder = PromptBuilder(processor)

    para = ("The quarterly report shows revenue increased by 14 percent while "
            "operating costs decreased by 6 percent, driven by automation of "
            "the fulfillment pipeline and renegotiated vendor contracts. ")
    cases = {
        "A.复读型": "Repeat the following paragraph exactly three times, verbatim:\n\n" + para,
        "B.自由生成型": "Write a short story about a lighthouse keeper who finds a mysterious map.",
        "C.长上下文引用型": ("Document:\n" + para * 12 +
                      "\n\nQuote the first two sentences of the document verbatim, then say DONE."),
    }

    overall = []
    for name, raw in cases.items():
        print(f"\n----- {name} -----")
        # 与生产一致：走 chat 模板（裸文本会让模型直接 EOS 或行为漂移）
        prompt = builder.build(messages=[{"role": "user", "content": raw}],
                               tools=None, enable_thinking=False)
        off = run(model, processor, prompt, speculative=False)
        on = run(model, processor, prompt, speculative=True)
        same = off["tokens"] == on["tokens"]
        note = f"len {len(off['tokens'])} vs {len(on['tokens'])}"
        if not same:
            # 允许的唯一分歧：首个分歧位是浮点近平局（top 候选 logprob 差 < 1e-3）。
            # 批量验证（S>1）与单步（S=1）核的累加顺序不同，平局位的 argmax 破缺
            # 可随路径翻转——两个候选同分，属良性分叉，分布不变性不受影响。
            import mlx.core as mx
            for i, (a, b) in enumerate(zip(off["tokens"], on["tokens"])):
                if a != b:
                    lp = off["logprobs"][i]
                    gap = (abs(float(lp[a]) - float(lp[b]))
                           if lp is not None else float("inf"))
                    same = gap < 1e-3
                    note = f"首个分歧 @{i}: {a} vs {b}，logprob 差 {gap:.6f}" + \
                           ("（近平局，判良性翻转）" if same else "（实质分歧！）")
                    break
        check(f"{name}: 贪心逐位一致（或近平局翻转）", same, note)
        st = on["stats"]
        acc = st["accepted"] / st["drafted"] if st["drafted"] else 0.0
        ratio = on["tps"] / off["tps"] if off["tps"] else 0.0
        print(f"    tps: off={off['tps']:.1f}  on={on['tps']:.1f}  加速比={ratio:.2f}x  "
              f"接受率={acc:.1%} ({st['accepted']}/{st['drafted']}, {st['rounds']} 轮)")
        overall.append((name, ratio, acc))

    print("\n===== 加速汇总 =====")
    for name, ratio, acc in overall:
        print(f"  {name}: {ratio:.2f}x  接受率 {acc:.1%}")
    print(f"\n===== 汇总 =====\n{PASS}/{PASS + FAIL} 通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
