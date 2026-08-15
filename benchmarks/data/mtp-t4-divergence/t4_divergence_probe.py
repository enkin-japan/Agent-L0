#!/usr/bin/env python3
"""T4 决定性判法：基线 vs 投机在分歧步的 logits 落盘比对（§1.7.3）。

口径 = §1.7.1 原分歧场：31b-8bit + 8bit 草稿件、block5、temp=0、
rep 1.1 / rep_ctx 50（处理器开）、同一条光合作用 prompt、400 tok。

钩点：包装 gemma4_generate.make_sampler（唯一调用点 :215，基线与走查共用同一
sampler 对象），记录它看到的每行 post-processor logprobs 的 top-16 (ids, vals)。
投机臂另包 _mtp_acceptance_walk 记每轮 (accepted, n_new)，用于把"走查行"映射回
"产出 token 序号"。

用法：python t4_divergence_probe.py <base|spec> <out.json>
两臂各一进程（单 GPU 串行；进程隔离顺带清 Metal 状态）。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
BACKEND = Path("/Volumes/enkin/project/Agent/backend")
sys.path.insert(0, str(BACKEND))

import mlx.core as mx  # noqa: E402

ARM, OUT = sys.argv[1], sys.argv[2]
TARGET = str(BACKEND / "models/gemma-4-31b-it-8bit")
DRAFT = str(BACKEND / "models/gemma-4-31b-it-assistant-8bit")
BLOCK = 5
PROMPT_TEXT = ("详细说明光合作用的完整过程，分光反应与暗反应两部分展开，"
               "每部分至少三点。")
TOPK = 16

ROWS: list = []      # 录制行：{"ids": [...], "vals": [...]}
WALKS: list = []     # 投机臂：每轮 (row0, rows, accepted, n_new, tokens)
STATE = {"record": True}   # 投机臂缺省关（草稿件共用同一 sampler，只在走查内开）


def install_sampler_probe():
    """包装 make_sampler 工厂。真正的调用点在上游 `mlx_vlm/generate/ar.py:280`
    （drafter 与基线两条路共用；经 _generate_module_override 兜底解析 ar 模块
    全局，故 patch ar.make_sampler 即命中）。gemma4_generate 侧一并包
    （prompt-lookup 路的 :215），本探针不走但保持一致。"""
    from framework.backends.mlx.patch import gemma4_generate as G
    from mlx_vlm.generate import ar as AR

    def make_factory(orig_factory):
        def factory(*a, **k):
            inner = orig_factory(*a, **k)

            def wrapped(logprobs):
                if not STATE["record"]:
                    return inner(logprobs)
                lp = logprobs.reshape(-1, logprobs.shape[-1])
                idx = mx.argsort(-lp, axis=-1)[..., :TOPK]
                vals = mx.take_along_axis(lp, idx, axis=-1)
                mx.eval(idx, vals)
                for r in range(int(lp.shape[0])):
                    ROWS.append({"ids": [int(i) for i in idx[r].tolist()],
                                 "vals": [float(v) for v in vals[r].tolist()]})
                return inner(logprobs)

            return wrapped
        return factory

    G.make_sampler = make_factory(G.make_sampler)
    AR.make_sampler = make_factory(AR.make_sampler)


def install_walk_probe():
    from mlx_vlm.speculative import mtp as M
    orig = M._mtp_acceptance_walk

    def wrapped(*a, **k):
        before = len(ROWS)
        STATE["record"] = True
        try:
            acc, new_tokens = orig(*a, **k)
        finally:
            STATE["record"] = False
        WALKS.append({"row0": before, "rows": len(ROWS) - before,
                      "accepted": int(acc), "n_new": len(new_tokens),
                      "tokens": [int(t) for t in new_tokens]})
        return acc, new_tokens

    M._mtp_acceptance_walk = wrapped


async def main():
    from framework.backends.mlx.backend import MLXBackend
    from scripts.scan_mtp_block import build_prompt, run_once

    install_sampler_probe()
    if ARM == "spec":
        STATE["record"] = False   # 只在走查内录（草稿/预填不录）
        install_walk_probe()

    be = await MLXBackend.load(TARGET, speculative={
        "mode": "drafter", "draft_model": DRAFT, "draft_block_size": BLOCK})
    if ARM == "base":
        be.speculative["mode"] = "off"

    sampling = {"max_tokens": 400, "temperature": 0.0,
                "repetition_penalty": 1.1, "repetition_context_size": 50}
    prompt = build_prompt(TARGET, PROMPT_TEXT)
    t0 = time.perf_counter()
    tps, n, dt, text, drafted, accepted, _ = run_once(be, prompt, sampling)
    print(f"{ARM}: {n} tok {tps:.2f} tps {time.perf_counter()-t0:.1f}s "
          f"rows={len(ROWS)} walks={len(WALKS)}")

    json.dump({"arm": ARM, "block": BLOCK, "text": text, "tokens": n,
               "rows": ROWS, "walks": WALKS}, open(OUT, "w"),
              ensure_ascii=False)


if __name__ == "__main__":
    asyncio.run(main())
