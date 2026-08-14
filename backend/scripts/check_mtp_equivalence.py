#!/usr/bin/env python3
"""MTP 等价性判据：投机路与基线的输出分歧是「数值噪声」还是「处理器接线错」。

背景：`scan_mtp_block.py` 在 31b-8bit 全部块档上都报「输出与基线不同」。
temp=0 下投机采样**不保证逐字节等同**——批量验证（M>1）与逐位解码（M=1）走的是
不同的 MLX 内核（`qmv` vs `qmv_wide`/`qmm`），浮点归约次序不同，近似平局位会翻。
但它**也可能**是我方补的 logits 处理器把惩罚历史对错了位（那是真缺陷）。

判别法（关键是让两种嫌疑各自可以单独出现）：
  1. 基线自比：同参数跑两次 —— 必须逐字相同，否则基线本身不确定，后面都别谈。
  2. **关处理器**（rep_penalty=1.0 → 处理器链为空）下 基线 vs 投机：
     · 仍分歧 ⇒ 分歧来自内核数值，与处理器无关（良性）
     · 一致   ⇒ 分歧由处理器引入，即我方补丁的历史对位有错（缺陷）
  3. 开处理器下 基线 vs 投机：与 2 对照，看处理器是否**加重**分歧。

报告首个分歧的**字符下标**与上下文：下标靠后 = 累积数值漂移；下标为 0 = 接线错。

单 GPU 纪律：本脚本自己加载模型，跑之前确认没有别的真模型进程/推理服务在跑。
用法：python3 scripts/check_mtp_equivalence.py [目标件] [草稿件] [block]
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TARGET = sys.argv[1] if len(sys.argv) > 1 else "models/gemma-4-31b-it-8bit"
DRAFT = sys.argv[2] if len(sys.argv) > 2 else "models/gemma-4-31b-it-assistant-bf16"
BLOCK = int(sys.argv[3]) if len(sys.argv) > 3 else 5
MAX_TOKENS = 400

PROMPT_TEXT = ("详细说明光合作用的完整过程，分光反应与暗反应两部分展开，"
               "每部分至少三点。")


def build_prompt(target_dir: str) -> str:
    from types import SimpleNamespace
    from transformers import AutoTokenizer
    from framework.dialects import get_dialect_class
    tok = AutoTokenizer.from_pretrained(target_dir)
    d = get_dialect_class("gemma4").create(
        SimpleNamespace(tokenizer=tok), options={"suppress_ghost_thinking": True})
    return d.build_prompt(messages=[{"role": "user", "content": PROMPT_TEXT}],
                          include_env=False)


def gen(be, prompt, sampling) -> str:
    cache = be.new_cache()
    parts = []
    for ch in be.stream_generate(prompt, cache=cache, sampling=sampling):
        if ch.text:
            parts.append(ch.text)
    return "".join(parts)


def diverge(a: str, b: str):
    """返回 (首个不同字符下标 或 None, 该处上下文)。"""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            lo = max(0, i - 30)
            return i, (f"…{a[lo:i]}⟦{a[i:i+12]}⟧  vs  "
                       f"…{b[lo:i]}⟦{b[i:i+12]}⟧")
    if len(a) != len(b):
        return n, f"前 {n} 字符相同，长度不同（{len(a)} vs {len(b)}）"
    return None, "逐字相同"


def report(tag: str, a: str, b: str, total_hint: int):
    idx, ctx = diverge(a, b)
    if idx is None:
        print(f"  ✅ {tag}：逐字相同（{len(a)} 字符）")
        return True
    pct = idx / max(1, total_hint)
    print(f"  ⚠️ {tag}：首个分歧在第 {idx} 字符（约全文 {pct:.0%} 处）")
    print(f"      {ctx}")
    return False


async def main() -> int:
    from framework.backends.mlx.backend import MLXBackend

    print(f"目标件: {TARGET}\n草稿件: {DRAFT}\nblock: {BLOCK}\n")
    prompt = build_prompt(TARGET)
    be = await MLXBackend.load(TARGET, speculative={
        "mode": "drafter", "draft_model": DRAFT, "draft_block_size": BLOCK})

    base_sampling = {"max_tokens": MAX_TOKENS, "temperature": 0.0,
                     "repetition_penalty": 1.1, "repetition_context_size": 50}
    # 必须显式给 None：backend.py:431 在键缺失时会兜底成 1.1，而 mlx_vlm 的
    # make_logits_processors 判据是 `penalty is not None and penalty != 0`
    # —— 给 1.0 仍会造出一个（数值上无操作的）处理器，处理器链就不是空的了。
    noproc_sampling = {"max_tokens": MAX_TOKENS, "temperature": 0.0,
                       "repetition_penalty": None}

    print("=== 1) 基线自比（确定性前提）===")
    be.speculative["mode"] = "off"
    b1 = gen(be, prompt, base_sampling)
    b2 = gen(be, prompt, base_sampling)
    det = report("基线×2（开处理器）", b1, b2, len(b1))
    if not det:
        print("  基线自身不确定 —— 后续判别无意义，停。")
        return 1

    print("\n=== 2) 关处理器：基线 vs 投机（隔离数值嫌疑）===")
    nb = gen(be, prompt, noproc_sampling)
    nb2 = gen(be, prompt, noproc_sampling)
    # 关处理器这一路也必须先自证确定，否则第 2 步的分歧可能来自它自身不确定，
    # 而被误记到内核头上。
    if not report("基线×2（关处理器）", nb, nb2, len(nb)):
        print("  关处理器的基线不确定 —— 该路的判别无意义，停。")
        return 1
    be.speculative["mode"] = "drafter"
    ns = gen(be, prompt, noproc_sampling)
    step2_diverged = not report("关处理器 基线 vs 投机", nb, ns, len(nb))

    print("\n=== 3) 开处理器：基线 vs 投机（对照 2）===")
    ss = gen(be, prompt, base_sampling)
    step3_diverged = not report("开处理器 基线 vs 投机", b1, ss, len(b1))

    print("\n=== 4) 投机自比（投机路自身确定性）===")
    ss2 = gen(be, prompt, base_sampling)
    spec_det = report("投机×2（开处理器）", ss, ss2, len(ss))

    # ---- 5) 处理器到底有没有接到投机路上 ----
    # 这一步是**判别成立的前提**，缺了它整套判别是假的：
    # 若处理器在投机路上被整个丢掉（上游原本就是这个毛病，见 patch/mtp_processors.py
    # 的立项理由），则 ns == ss —— 而第 2 步仍会因内核数值差异而分歧，
    # 于是脚本会照样打印"良性"。**真缺陷与真良性给出同一个结论。**
    # 故必须直接验证"开/关处理器会不会改变投机路的输出"。
    print("\n=== 5) 处理器是否真的作用到了投机路（判别的前提）===")
    proc_reaches_spec = not report("投机: 关处理器 vs 开处理器", ns, ss, len(ns))
    # 同样验证基线侧惩罚确实咬得动（prompt 太短时 rep 可能根本不触发，
    # 那样"两边都不变"就不能证明接线坏，只能证明这个 prompt 测不出来）。
    proc_bites_base = not report("基线: 关处理器 vs 开处理器", nb, b1, len(nb))

    print("\n=== 判定 ===")
    ok = True
    if not proc_bites_base:
        print("  ⚠️ 基线上开/关处理器输出一致 ⇒ 本 prompt 下 repetition_penalty 根本没咬到，")
        print("     第 5 步失去分辨力。请换更长/更重复的 prompt 重测，本次判别不成立。")
        ok = False
    elif not proc_reaches_spec:
        print("  ❌ 投机路开/关处理器输出一致，而基线上不一致 ⇒ **处理器没接到投机路上**。")
        print("     这正是上游原有的缺陷形态，必须修，不是数值噪声。")
        ok = False
    if not spec_det:
        print("  ❌ 投机路自身不确定，任何逐字比对都不可信。")
        ok = False
    if ok:
        if step2_diverged:
            print("  关处理器时**仍然**分歧（且已证处理器确实作用于投机路）")
            print("  ⇒ 分歧源自 M>1 与 M=1 走不同 MLX 内核的浮点归约次序差异，")
            print("     与 logits 处理器接线无关。属**良性**：投机采样在 temp=0 下")
            print("     只保证分布等价，不保证逐字节等同。")
        elif step3_diverged:
            print("  关处理器时一致、开处理器时分歧 ⇒ **处理器接线有错**，")
            print("  惩罚历史在投机路上对位不正确。这是必须修的缺陷。")
        else:
            print("  全程逐字相同 ⇒ 投机路与基线完全等价，无任何分歧。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
