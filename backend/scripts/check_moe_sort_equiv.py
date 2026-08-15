#!/usr/bin/env python3
"""MoE 排序阈值补丁的数值等价判据 + A/B 加速比。

# 为什么"接受率相同"不够

接受率是 argmax 比较的结果，**argmax 能掩盖 logits 层面的差异**——只要差异没大到
翻转任何一个 top-1，接受率就一模一样。真正的判据是**产出文本**。
排序路（`_gather_sort` → `sorted_indices=True` → `_scatter_unsort`）在数学上是恒等
重排，理应逐字符相同。若不同，说明重排/还原有偏差，补丁不能用。

# 口径

同一次装载内开/关补丁，同一 prompt、temp=0、同 block；先各跑一次丢弃（排序路首次
调用会 JIT 特化 MLX 内核，落在计时里会把开的那一侧算贵），再各跑 REPEATS 次取中位。
**开/关交错跑**，避免单调热漂移全压在后跑的那一侧（26B 档实测批内漂移可达 7.5%）。

单 GPU 纪律：本脚本自己加载模型，跑之前确认没有别的真模型进程在跑。

用法：python3 scripts/check_moe_sort_equiv.py [目标件] [草稿件] [block] [repeats]
"""
import asyncio
import hashlib
import os
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 缺省模型路径是相对 backend/ 的；不 chdir 的话从仓库根跑会在 tokenizer 那步
# 才炸，且报的是"找不到模型"而非"你在错的目录"。与 mtp_cost_decomp.py 一致。
os.chdir(str(Path(__file__).resolve().parents[1]))

TARGET = sys.argv[1] if len(sys.argv) > 1 else "models/gemma-4-26b-a4b-it-8bit"
DRAFT = sys.argv[2] if len(sys.argv) > 2 else "models/gemma-4-26b-a4b-it-assistant-4bit"
BLOCK = int(sys.argv[3]) if len(sys.argv) > 3 else 4
REPEATS = int(sys.argv[4]) if len(sys.argv) > 4 else 3
MAX_TOKENS = 300

PROMPT_TEXT = "详细说明光合作用的完整过程，分光反应与暗反应两部分展开，每部分至少三点。"


def build_prompt(target_dir: str) -> str:
    from transformers import AutoTokenizer
    from framework.dialects import get_dialect_class
    tok = AutoTokenizer.from_pretrained(target_dir)
    d = get_dialect_class("gemma4").create(
        SimpleNamespace(tokenizer=tok), options={"suppress_ghost_thinking": True})
    return d.build_prompt(messages=[{"role": "user", "content": PROMPT_TEXT}],
                          include_env=False)


def gen(be, prompt, sampling):
    """返回 (全量文本, 仅解码 tps)。文本必须**累加** ch.text——它是本次分段而非累积量。"""
    parts, last = [], None
    for ch in be.stream_generate(prompt, cache=be.new_cache(), sampling=sampling):
        if ch.text:
            parts.append(ch.text)
        last = ch
    if not last or not last.generation_tps:
        raise RuntimeError("后端未报 generation_tps，拒绝退回含预填的墙钟估计值")
    return "".join(parts), last.generation_tps


async def main() -> int:
    # 先 import 后端（会 import gemma4_generate，补丁在那里安装）
    from framework.backends.mlx.backend import MLXBackend
    from framework.backends.mlx.patch import moe_sort_threshold as P

    be = await MLXBackend.load(TARGET, speculative={
        "mode": "drafter", "draft_model": DRAFT, "draft_block_size": BLOCK})
    prompt = build_prompt(TARGET)
    samp = {"max_tokens": MAX_TOKENS, "temperature": 0.0,
            "repetition_penalty": 1.1, "repetition_context_size": 50}

    print(f"目标 {TARGET}\n草稿 {DRAFT}\nblock={BLOCK} repeats={REPEATS} "
          f"tokens={MAX_TOKENS}  阈值={P.min_size()}")
    print("口径：同一次装载内开/关交错，temp=0，先各跑一次丢弃（排序内核首次特化）\n")

    from mlx_vlm.models import switch_layers as SL

    def run(on: bool):
        if on:
            P.install()
            # **拒装必须炸**：install() 在上游漂移 / 环境变量关闭 / 接口变动时会
            # 拒绝安装并返回 False。若不检查，开关两侧跑的是同一条上游路径，
            # 本脚本会打印"净增益 0.0% + 输出逐字符相同 ✅"——
            # 给一个**根本没装上**的补丁发数值安全证书（首过评审 Major）。
            if SL.SwitchGLU.__call__ is not P.switch_glu_call:
                raise RuntimeError(
                    "补丁未安装（install() 拒绝了，原因见上一行阴性声明）。"
                    "此时开/关两侧等价，等价性判据无意义，故直接失败。")
        else:
            P.uninstall()
            if SL.SwitchGLU.__call__ is not P._ORIGINAL_CALL:
                raise RuntimeError("uninstall() 未能恢复上游实现，对照组不干净。")
        return gen(be, prompt, samp)

    # 预热：两条路各跑一次并丢弃（排序路首次调用会 JIT 特化 MLX 内核）
    run(True)
    run(False)

    on_txts, off_txts = [], []
    on_tps, off_tps = [], []
    for i in range(REPEATS):
        # **ABBA 交错**：单调漂移在 ABBA 下才真正抵消；一律 A→B 会给 A 留下
        # 一个方向固定的偏置（首过评审 Minor）。
        if i % 2 == 0:
            t, s = run(True)
            t2, s2 = run(False)
        else:
            t2, s2 = run(False)
            t, s = run(True)
        on_txts.append(t); off_txts.append(t2)
        on_tps.append(s); off_tps.append(s2)
        print(f"  第{i+1}轮  {'开→关' if i % 2 == 0 else '关→开'}  "
              f"开 {s:7.2f} tps   关 {s2:7.2f} tps")

    P.install()                               # 收尾回到生产态

    # 每侧内部也要自比：temp=0 下同侧多次应逐字符相同，否则说明有非确定性，
    # 跨侧比对本身就不成立。
    for tag, txts in (("开", on_txts), ("关", off_txts)):
        if len(set(txts)) != 1:
            print(f"  ⚠️ {tag}排序侧 {len(set(txts))} 种不同输出 —— 该侧自身不确定，"
                  "跨侧等价性判据不成立")
            return 1
    on_txt, off_txt = on_txts[0], off_txts[0]

    med_on, med_off = statistics.median(on_tps), statistics.median(off_tps)
    sha = lambda s: hashlib.sha256(s.encode()).hexdigest()[:12]
    print(f"\n开排序 中位 {med_on:7.2f} tps  ({len(on_txt)} 字符, sha {sha(on_txt)})")
    print(f"关排序 中位 {med_off:7.2f} tps  ({len(off_txt)} 字符, sha {sha(off_txt)})")
    print(f"净增益 {(med_on/med_off - 1)*100:+.1f}%")

    print("\n=== 数值判定 ===")
    if on_txt == off_txt:
        print("  ✅ 开/关排序输出**逐字符相同** ⇒ 排序只改分组不改数值，补丁数值安全。")
        return 0
    n = min(len(on_txt), len(off_txt))
    i = next((k for k in range(n) if on_txt[k] != off_txt[k]), n)
    print(f"  ⚠️ 第 {i} 字符起分歧（开 {len(on_txt)} 字符 / 关 {len(off_txt)} 字符）")
    print(f"      开: …{on_txt[max(0,i-25):i]}⟦{on_txt[i:i+12]}⟧")
    print(f"      关: …{off_txt[max(0,i-25):i]}⟦{off_txt[i:i+12]}⟧")
    print("  分歧不必然是错——重排会改浮点归约次序，可能在近似平局位翻。")
    print("  但**必须记录**，不能再宣称『只改分组不改数值』。")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
