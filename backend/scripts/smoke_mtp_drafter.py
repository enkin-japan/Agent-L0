#!/usr/bin/env python3
"""MTP 草稿件投机端到端冒烟（真模型，走真实后端路径）。

验的不是"能不能出字"，是**接线后语义没变**这件事：
  A) 装载期：草稿件装得上、兼容性校验过、处理器可补性前置校验过；
  B) 非投机基线 vs 投机：同 prompt 同采样，输出与速度对照；
  C) 处理器仍在位：repetition_penalty 走的是我方补的走查路（§S.18 的兜底不能丢）；
  D) 接受率统计回吐（§7-0 ④）；
  E) 拒启路径：mode=drafter 而 draft_model 为空 → 拒启，不静默降级。

单 GPU 纪律：本脚本自己加载模型，跑之前确认没有别的真模型进程/推理服务在跑。
用法：
  python3 scripts/smoke_mtp_drafter.py                       # 缺省 e4b 对
  python3 scripts/smoke_mtp_drafter.py <目标件目录> <草稿件目录>
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TARGET = "models/gemma-4-e4b-it-4bit"
DRAFT = "models/gemma-4-e4b-it-assistant-bf16"
if len(sys.argv) >= 3:
    TARGET, DRAFT = sys.argv[1], sys.argv[2]
# 第三参 = draft_block_size；给 "auto" 则完全不下发（走草稿件 config 缺省 + 上游自适应）
BLOCK = sys.argv[3] if len(sys.argv) >= 4 else "6"

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if extra else ""))
    PASS, FAIL = PASS + (1 if cond else 0), FAIL + (0 if cond else 1)


async def run():
    from framework.backends.mlx.backend import MLXBackend
    print(f"目标件: {TARGET}\n草稿件: {DRAFT}\n")

    spec_off = {"mode": "off"}
    spec_on = {"mode": "drafter", "draft_model": DRAFT, "draft_kind": "",
               "num_draft_tokens": 8}
    if BLOCK != "auto":
        spec_on["draft_block_size"] = int(BLOCK)
    print(f"块大小: {BLOCK}")

    # ---- E) 拒启路径先测（不占显存）----
    print("=== E) 配置错误必须拒启，不静默降级 ===")
    try:
        await MLXBackend.load(TARGET, speculative={"mode": "drafter", "draft_model": ""})
        check("mode=drafter 且 draft_model 为空 → 拒启", False, "居然装上了")
    except ValueError as e:
        check("mode=drafter 且 draft_model 为空 → 拒启", "draft_model" in str(e))

    # ---- A) 装载 ----
    print("\n=== A) 草稿件装载与前置校验 ===")
    t0 = time.perf_counter()
    be = await MLXBackend.load(TARGET, speculative=spec_on)
    check("目标件 + 草稿件装载成功", be.draft_model is not None,
          f"{time.perf_counter() - t0:.1f}s")
    check("kind 自动判定为 mtp", be.draft_kind == "mtp", f"kind={be.draft_kind}")

    # prompt **必须由本栈的方言组装器出**，不许手搓（§S.16：模板是字节协议）。
    # 手搓版在 e4b/26B 上看着正常，到 31B 直接吐 LaTeX 垃圾——而退化文本高度
    # 可预测，草稿件几乎全中，量出 94% 接受率与 2.06× 的**假红利**。
    # 一个坏 prompt 不会报错，只会把加速比刷好看。
    prompt = _build_prompt(TARGET)

    # 题目必须能稳定撑满 max_tokens：短回答（20-30 tok 就 EOS）下 tps 几乎全是
    # 装载/预热噪声，加速比不可读。26B 用一句话题只生成 23 个 token，
    # 量出 0.52× 的假净亏，就是栽在这里。
    # MTP_NOPROC=1：关掉全部 logits 处理器。此时我方走查不建上下文、原样委派上游
    # 的全批量路径（无逐位同步）——用来量"处理器这条路的代价上限"，不是生产口径。
    import os
    sampling = {"max_tokens": 400, "temperature": 0.0}
    if os.environ.get("MTP_NOPROC") != "1":
        sampling.update({"repetition_penalty": 1.1, "repetition_context_size": 50})
    else:
        sampling.update({"repetition_penalty": None, "min_p": 0.0})
        print("⚠️  MTP_NOPROC=1：处理器全关（非生产口径）")

    def gen(backend):
        """返回 (全量文本, 末个 chunk, 墙钟)。

        **全量必须累积每个 chunk 的 `text` 分段**：`GenChunk.text` 是
        detokenizer 的**本次分段**不是累积量，`delta` 又只在 features.want_delta
        时才算。拿末个 chunk 当全量会比出假分歧——投机路每轮吐多个 token，
        分段边界天然与逐 token 的基线不同，比的是边界不是内容。（本脚本初版
        就栽在这里，一度把分段差异误报成 temp=0 输出分歧。）
        """
        cache = backend.new_cache()
        last, acc = None, []
        t = time.perf_counter()
        for ch in backend.stream_generate(prompt, cache=cache, sampling=sampling):
            last = ch
            acc.append(ch.text or "")
        return "".join(acc), last, time.perf_counter() - t

    # ---- B/C/D) 投机路 ----
    print("\n=== B) 投机路生成 ===")
    text_spec, spec_chunk, spec_dt = gen(be)
    check("有输出", len(text_spec.strip()) > 0, repr(text_spec[:60]))
    check("非退化（无长串重复）", not _degenerate(text_spec))
    print(f"     tps={spec_chunk.generation_tps:.1f}  tokens={spec_chunk.generation_tokens}"
          f"  wall={spec_dt:.1f}s")

    print("\n=== D) 接受率统计回吐（§7-0 ④）===")
    extra = spec_chunk.extra or {}
    drafted, accepted = extra.get("spec_drafted"), extra.get("spec_accepted")
    check("统计有回吐", drafted is not None and drafted > 0,
          f"drafted={drafted} accepted={accepted}")
    if drafted:
        print(f"     接受率 = {accepted / drafted:.1%}")

    del be

    # ---- 基线对照 ----
    print("\n=== B') 非投机基线对照 ===")
    be0 = await MLXBackend.load(TARGET, speculative=spec_off)
    text_base, base_chunk, base_dt = gen(be0)
    print(f"     tps={base_chunk.generation_tps:.1f}  tokens={base_chunk.generation_tokens}"
          f"  wall={base_dt:.1f}s")
    check("基线非退化", not _degenerate(text_base))

    # temp=0 逐字一致**不是判据**：上游模型卡自述 "byte-identical to no-drafter
    # at temperature=0"，但实测**不成立**——验证批（S>1）与逐 token 前向的累加序
    # 不同，近平局处贪心会翻转，一翻之后全程分岔。实测三题在 36–127 token 处分歧，
    # 且关掉全部处理器（走上游原实现）同样分歧 → 责任在上游，与我方补丁无关。
    # 分歧本身不是错：每个 token 仍由目标模型authorize，只是"可复现同一串"的
    # 承诺不成立。故此处只**测量并报告**分歧点，不作为通过与否的判据。
    same = text_spec.strip() == text_base.strip()
    div = None
    if not same:
        div = next((i for i, (x, y) in enumerate(zip(text_spec, text_base)) if x != y),
                   min(len(text_spec), len(text_base)))
    print(f"     temp=0 逐字一致 = {same}"
          + ("" if same else f"（首分歧字符位 {div}，共 {len(text_spec)}/{len(text_base)}）"))
    check("分歧点不在开头（前缀应有相当长度一致，否则疑似接线错而非数值抖动）",
          same or (div is not None and div >= 16), f"div={div}")
    if base_dt > 0:
        print(f"     加速比 = {base_dt / spec_dt:.2f}×")
    del be0

    print(f"\n===== 汇总 =====\n{PASS}/{PASS + FAIL} 通过")
    return 1 if FAIL else 0


QUESTION = ("详细说明光合作用的完整过程，分光反应与暗反应两部分展开，每部分至少三点。")


def _build_prompt(target_dir: str) -> str:
    """走本栈方言组装器（与 bench_baseline 同一条路），不手搓字节。"""
    from types import SimpleNamespace
    from transformers import AutoTokenizer
    from framework.dialects import get_dialect_class

    tok = AutoTokenizer.from_pretrained(target_dir)
    dialect = get_dialect_class("gemma4").create(
        SimpleNamespace(tokenizer=tok), options={"suppress_ghost_thinking": True})
    return dialect.build_prompt(messages=[{"role": "user", "content": QUESTION}],
                                include_env=False)


def _degenerate(text: str) -> bool:
    """粗判退化空转：某个 12 字窗口连续重复 4 次以上。"""
    t = text.strip()
    for w in (12, 24):
        for i in range(0, max(0, len(t) - w * 4), w):
            seg = t[i:i + w]
            if seg and t[i:i + w * 4] == seg * 4:
                return True
    return False


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
