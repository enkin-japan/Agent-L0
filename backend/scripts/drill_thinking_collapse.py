#!/usr/bin/env python3
"""思考预算强制收束 × MTP 草稿件——真机 drill（②′ 验收件，2026-08-23）。

验的是补课后的三件事（qwen38-8bit + MTP b5，生产口径采样）：
  A) 不再拒跑：08-14 守卫旧文（drafter × 开思考 → RuntimeError）已被真实现取代；
  B) 预算真收束：小预算（64）下 criteria 计数停在 预算+溢出上界 以内，
     且收束序列真发生（in_thinking 终态 False、budget_exceeded 曾置位）；
  C) 收束后回正常流：</think> 之后有非空正文，无退化空转；
  对照臂：大预算（100000）同题自然思考长度 ≫ 64，证明 B 的收束是被强制的
  而非模型自然停笔。

单 GPU 纪律：本脚本自己加载模型（~28G），跑之前确认无其他真模型进程。
用法：cd backend && .venv/bin/python <本文件>
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TARGET = "models/Qwen3.8-27B-8bit"
DRAFT = "models/Qwen3.8-27B-MTP-8bit"
BLOCK = 5
BUDGET_SMALL = 64
BUDGET_HUGE = 100000
# 溢出上界（模块头口径）：块中滞后 ≤ block-1，收束序列 2 步逐轮注入期间每轮
# 计数 +1（\n 计入思考计数），再留少量余裕
OVERSHOOT_BOUND = (BLOCK - 1) + 2 + 4

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if extra else ""))
    PASS, FAIL = PASS + (1 if cond else 0), FAIL + (0 if cond else 1)


QUESTION = ("详细说明光合作用的完整过程，分光反应与暗反应两部分展开，每部分至少三点。")


def _degenerate(text: str) -> bool:
    t = text.strip()
    for w in (12, 24):
        for i in range(0, max(0, len(t) - w * 4), w):
            seg = t[i:i + w]
            if seg and t[i:i + w * 4] == seg * 4:
                return True
    return False


async def run():
    from types import SimpleNamespace
    from transformers import AutoTokenizer
    from framework.backends.mlx.backend import MLXBackend
    from framework.dialects import get_dialect_class

    tok = AutoTokenizer.from_pretrained(TARGET)
    dialect = get_dialect_class("qwen35").create(SimpleNamespace(tokenizer=tok),
                                                options={})
    mk = dialect.markers
    prompt = dialect.build_prompt(messages=[{"role": "user", "content": QUESTION}],
                                  include_env=False, enable_thinking=True)
    prompt += dialect.thinking_prefill(True)   # 与 engine.py:363 同构：种子在开着的 <think> 内

    spec_on = {"mode": "drafter", "draft_model": DRAFT, "draft_kind": "",
               "num_draft_tokens": 8, "draft_block_size": BLOCK}
    sampling = {"temperature": 0.2, "min_p": 0.05, "max_tokens": 900,
                "repetition_penalty": 1.1, "repetition_context_size": 50}

    print(f"目标件: {TARGET}\n草稿件: {DRAFT}  block={BLOCK}\n")
    t0 = time.perf_counter()
    be = await MLXBackend.load(TARGET, speculative=spec_on)
    print(f"装载 {time.perf_counter() - t0:.1f}s  kind={be.draft_kind}")

    def gen(budget):
        features = {"enable_thinking": True, "thinking_budget": budget,
                    "thinking_start_token": mk.soc, "thinking_end_token": mk.eoc}
        cache = be.new_cache()
        acc, last = [], None
        t = time.perf_counter()
        for ch in be.stream_generate(prompt, cache=cache, sampling=sampling,
                                     features=features):
            last = ch
            acc.append(ch.text or "")
        proc = be.processor
        tk = proc.tokenizer if hasattr(proc, "tokenizer") else proc
        crit = getattr(tk, "thinking_budget_criteria", None)
        return "".join(acc), last, crit, time.perf_counter() - t

    # ---- 臂 1：小预算，逼收束 ----
    print(f"\n=== 臂 1：thinking_budget={BUDGET_SMALL}（逼收束）===")
    try:
        text_s, chunk_s, crit_s, dt_s = gen(BUDGET_SMALL)
    except RuntimeError as e:
        check("A) 不再拒跑（旧守卫已被真实现取代）", False, repr(e)[:120])
        print(f"\n===== 汇总 =====\n{PASS}/{PASS + FAIL} 通过")
        return 1
    check("A) 不再拒跑（旧守卫已被真实现取代）", True,
          f"tokens={chunk_s.generation_tokens} wall={dt_s:.1f}s")
    cnt = crit_s.thinking_token_count if crit_s else -1
    check("B1) 预算真被触到（计数 ≥ 预算）", cnt >= BUDGET_SMALL, f"count={cnt}")
    check(f"B2) 收束及时（计数 ≤ 预算+{OVERSHOOT_BOUND} 溢出上界）",
          BUDGET_SMALL <= cnt <= BUDGET_SMALL + OVERSHOOT_BOUND, f"count={cnt}")
    check("B3) 收束完成（终态不在思考中）", crit_s is not None and not crit_s.in_thinking)
    # 正文 = eoc 之后的部分；detokenizer 若吞了特殊 token 则整段就是正文
    body = text_s.split(mk.eoc, 1)[-1] if mk.eoc in text_s else text_s
    check("C1) 收束后有非空正文", len(body.strip()) > 0, repr(body.strip()[:60]))
    check("C2) 非退化", not _degenerate(text_s))
    extra = chunk_s.extra or {}
    check("MTP 真在跑（有草稿统计）", (extra.get("spec_drafted") or 0) > 0,
          f"drafted={extra.get('spec_drafted')} accepted={extra.get('spec_accepted')}")

    # ---- 臂 2：大预算对照，量自然思考长度 ----
    print(f"\n=== 臂 2：thinking_budget={BUDGET_HUGE}（对照：自然思考长度）===")
    text_h, chunk_h, crit_h, dt_h = gen(BUDGET_HUGE)
    nat = crit_h.thinking_token_count if crit_h else -1
    check("对照臂自然思考 > 小预算（证明臂 1 是被强制收束）", nat > BUDGET_SMALL,
          f"natural={nat}")
    check("对照臂未触发收束", crit_h is not None and not crit_h.budget_exceeded)

    print(f"\n===== 汇总 =====\n{PASS}/{PASS + FAIL} 通过")
    print(f"[数据] 臂1 思考计数={cnt} 生成={chunk_s.generation_tokens} tps={chunk_s.generation_tps:.1f}")
    print(f"[数据] 臂2 思考计数={nat} 生成={chunk_h.generation_tokens} tps={chunk_h.generation_tps:.1f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
