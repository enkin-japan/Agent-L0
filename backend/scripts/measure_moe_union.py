#!/usr/bin/env python3
"""量「专家并集税」：MoE 验证批里 M 个 token 到底摊开读了多少专家。

为什么必须实测（此前只是［推断］）：
  MTP 的经济学前提是「验证 M 个位置 ≈ 验证 1 个位置一样贵」——对稠密件成立
  （权重读一遍，喂几行都一样）。但 MoE 的专家是**逐 token 路由**的：
  M 个位置各选 top-k 个专家，`GatherQMM` 要读的是这 M 个位置所选专家的**并集**。
  并集有多大，取决于相邻 token 的路由有多相关——**这一点纯靠推理算不出来**：
    · 若路由彼此独立且均匀：|并集| ≈ E·(1-(1-k/E)^M)，128 专家 top-8 时
      M=1/2/4/5 → 8 / 15.5 / 29.1 / 35.2，即 M=5 要读 4.4 倍的专家字节；
    · 若相邻 token 高度走同一批专家：|并集| 接近 8，几乎没有税。
  两种极端对 MTP 的结论完全相反，所以只能测。

做法：包住 `Router.__call__`，按**每层每次前向**记录该次调用里
      M（位置数）与 |并集|（去重后的专家数）。不改任何数值，只观察。

单 GPU 纪律：本脚本自己加载模型，跑之前确认没有别的真模型进程/推理服务在跑。
用法：python3 scripts/measure_moe_union.py [目标件] [草稿件] [块列表] [tokens]
"""
import asyncio
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TARGET = sys.argv[1] if len(sys.argv) > 1 else "models/gemma-4-26b-a4b-it-8bit"
DRAFT = sys.argv[2] if len(sys.argv) > 2 else "models/gemma-4-26b-a4b-it-assistant-4bit"
BLOCKS = [int(b) for b in (sys.argv[3] if len(sys.argv) > 3 else "3,4,5,6").split(",")]
MAX_TOKENS = int(sys.argv[4]) if len(sys.argv) > 4 else 200

PROMPT_TEXT = ("详细说明光合作用的完整过程，分光反应与暗反应两部分展开，"
               "每部分至少三点。")

# {M: [并集大小, ...]}，跨层跨轮累计
UNION: dict = defaultdict(list)


def install_router_probe(_model):
    """记录每次路由的 (M, |专家并集|)。只读，不改数值。返回卸载函数。

    **必须打在类上，不能打在实例上**：Python 的特殊方法（`__call__`）是按**类型**
    查找的，`r(x)` 实际走 `type(r).__call__(r, x)`，给实例赋 `r.__call__ = ...`
    根本不会被调用——初版就是这么写的，探针一条都没记到却不报错。
    """
    import mlx.core as mx
    from mlx_vlm.models.gemma4.language import Router

    orig = Router.__call__

    def wrapped(self, x):
        idx, w = orig(self, x)
        # idx 形如 [..., M, top_k]
        mx.eval(idx)
        m = idx.shape[-2] if idx.ndim >= 2 else 1
        UNION[m].append(len(set(idx.reshape(-1).tolist())))
        return idx, w

    Router.__call__ = wrapped
    return lambda: setattr(Router, "__call__", orig)


def build_prompt(target_dir: str) -> str:
    from types import SimpleNamespace
    from transformers import AutoTokenizer
    from framework.dialects import get_dialect_class
    tok = AutoTokenizer.from_pretrained(target_dir)
    d = get_dialect_class("gemma4").create(
        SimpleNamespace(tokenizer=tok), options={"suppress_ghost_thinking": True})
    return d.build_prompt(messages=[{"role": "user", "content": PROMPT_TEXT}],
                          include_env=False)


async def main() -> int:
    from framework.backends.mlx.backend import MLXBackend

    print(f"目标件: {TARGET}\n草稿件: {DRAFT}\n块列表: {BLOCKS}  tokens: {MAX_TOKENS}\n")
    be = await MLXBackend.load(TARGET, speculative={
        "mode": "drafter", "draft_model": DRAFT, "draft_block_size": BLOCKS[0]})

    cfg = be.model.config.text_config if hasattr(be.model.config, "text_config") \
        else be.model.config
    E = int(getattr(cfg, "num_experts", 0))
    K = int(getattr(cfg, "top_k_experts", 0))
    print(f"专家总数 E={E}  每 token top-k={K}\n")

    install_router_probe(be.model)
    sampling = {"max_tokens": MAX_TOKENS, "temperature": 0.0,
                "repetition_penalty": 1.1, "repetition_context_size": 50}
    prompt = build_prompt(TARGET)

    for blk in BLOCKS:
        be.speculative["draft_block_size"] = blk
        UNION.clear()
        cache = be.new_cache()
        for _ in be.stream_generate(prompt, cache=cache, sampling=sampling):
            pass
        print(f"=== block_total={blk}（γ={blk-1}）===")
        print(f"{'M':>3}{'次数':>8}{'并集中位':>10}{'并集均值':>10}"
              f"{'独立假设':>10}{'相对 M=1':>10}{'税(vs 理想)':>12}")
        print("-" * 66)
        # M=1 的基准**按定义就是 K**：单 token 恰好选 top-k 个专家，并集无从谈起。
        # 不能等实测——投机路的目标件永远以 M=block_total 调用，M=1 那行根本不出现，
        # 初版据此取基准，于是整列算成 nan。
        base = float(K)
        for m in sorted(UNION):
            vals = UNION[m]
            med = statistics.median(vals)
            mean = statistics.mean(vals)
            indep = E * (1 - (1 - K / E) ** m) if E and K else float("nan")
            rel = mean / base if base else float("nan")
            print(f"{m:>3}{len(vals):>8}{med:>10.1f}{mean:>10.2f}"
                  f"{indep:>10.1f}{rel:>10.2f}×{(rel - 1) * 100:>11.0f}%")
        print()

    print("读法：『相对 M=1』就是**专家字节读取量相对单 token 解码的倍数**。")
    print("      理想（稠密件那样）是 1.00×；『独立假设』是路由完全不相关时的上界。")
    print("      实测落在两者之间的位置，就是并集税的真实量级。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
