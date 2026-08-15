#!/usr/bin/env python3
"""MTP 块大小扫描：一次装载，多档 block_total 对照，带逐轮接受分布。

为什么要重扫（此前 γ 扫描的盲区）：
  MLX 的 `qmv_wide` 内核按 **5 个输入向量一块** 切分（`quantized.cpp:322`
  `n_tiles = ceil(M/5)`，注释「Each tile re-reads the weights」）。验证步的 M
  恰等于 block_total。故 block_total ≤ 5 只读一遍权重，=6 起读两遍。
  而既有 γ 扫描测的是 block 4 / 6 / 8 / 12 —— **正好跳过 5 这个瓦片边界**。

本脚本一次装载目标件+草稿件，逐档跑同一 prompt，避免每档重装 31GB。

口径（全部随数记录，缺一即作废——本项目栽过跟头）：
  方言组装器出的 prompt、temp=0、rep 1.1 / rep_ctx 50、定长 max_tokens、
  上下文长度由第 6 参 CTX 决定（0=冷启动）、草稿件由第 2 参决定（bf16 或量化件）、
  每档 repeats 次取中位并报 min/max，批前批后各测一次基线查漂移。
  **已知局限**：各档在各自连续窗口内测量，单调热漂移会映射到档序上；
  实测批内漂移 0.0%–1.2%，远小于所要论证的效应（+4%~+8%），故未做交错。

正确性哨：报告投机路输出是否与基线逐字节相同。**不同不等于有错**——M>1 与 M=1
走不同 MLX 内核，浮点归约次序会在近似平局位翻。该分歧已由
`check_mtp_equivalence.py` 单独判定为良性——**该判定仅对 gemma4 档建立**
（§1.7.4 的决定性实验也是 gemma4）；其它方言档此处只报"不同"，不背书良性。
此哨只作提示，不作失败判据。

单 GPU 纪律：本脚本自己加载模型，跑之前确认没有别的真模型进程/推理服务在跑。
用法：
  python3 scripts/scan_mtp_block.py <目标件> <草稿件> [块列表] [重复数] [tokens] [ctx] [方言]
  python3 scripts/scan_mtp_block.py models/gemma-4-31b-it-8bit \\
      models/gemma-4-31b-it-assistant-bf16 2,3,4,5,6,7,8,10 3 400
"""
import asyncio
import hashlib
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 作为库被导入时（t4 探针 import build_prompt/run_once）不得继承宿主进程的
# argv——否则宿主的位置参数会被误读成本脚本的（评审 nit：宿主 ≥8 个参数且第 8
# 个不是方言名时，import 即死在下面的 fail-fast 上）。截断到只剩程序名=全走缺省。
_ARGS = sys.argv if __name__ == "__main__" else sys.argv[:1]
TARGET = _ARGS[1] if len(_ARGS) > 1 else "models/gemma-4-31b-it-8bit"
DRAFT = _ARGS[2] if len(_ARGS) > 2 else "models/gemma-4-31b-it-assistant-bf16"
BLOCKS = [int(b) for b in (_ARGS[3] if len(_ARGS) > 3 else "4,5,6").split(",")]
REPEATS = int(_ARGS[4]) if len(_ARGS) > 4 else 3
MAX_TOKENS = int(_ARGS[5]) if len(_ARGS) > 5 else 400
# 第 6 参 = 目标上下文长度（token）。>0 则先用**基线**生成一段真实回答塞进前一轮，
# 把上下文顶到该长度再开测——production 的形态是长对话，而 ctx=0 的数只说明冷启动。
CTX = int(_ARGS[6]) if len(_ARGS) > 6 else 0
# 第 7 参 = 方言（缺省 gemma4，行为不变）。qwen3_5 档必须传 qwen35：gemma4 模板
# 的特殊标记对 qwen tokenizer 是畸形 prompt，接受率会失真且全程不报错。
DIALECT = _ARGS[7] if len(_ARGS) > 7 else "gemma4"

# CTX>0 回灌守卫用的特殊标记册子（按方言）。两档都直接抄各自 tokenizer.json 的
# added_tokens 全表（gemma4 24 个 / qwen35 33 个；守卫是字符串包含检查，列全
# 零成本）——挑子集就是给守卫开洞（08-16 复审两连：qwen35 先前只列 6 个开标记
# 连 </tool_call> 都没有；gemma4 首版也只挑了 5 个，同一条理由适用于缺省方言）。
SPECIAL_MARKERS = {
    "gemma4": (
        "<pad>", "<eos>", "<bos>", "<unk>", "<mask>",
        "<|tool>", "<tool|>", "<|tool_call>", "<tool_call|>",
        "<|tool_response>", "<tool_response|>", '<|"|>', "<|think|>",
        "<|channel>", "<channel|>", "<|turn>", "<turn|>",
        "<|image>", "<|audio>", "<|image|>", "<|audio|>",
        "<image|>", "<audio|>", "<|video|>",
    ),
    "qwen35": (
        "<|endoftext|>", "<|im_start|>", "<|im_end|>",
        "<|object_ref_start|>", "<|object_ref_end|>",
        "<|box_start|>", "<|box_end|>", "<|quad_start|>", "<|quad_end|>",
        "<|vision_start|>", "<|vision_end|>", "<|vision_pad|>",
        "<|image_pad|>", "<|video_pad|>",
        "<tool_call>", "</tool_call>", "<tool_response>", "</tool_response>",
        "<|fim_prefix|>", "<|fim_middle|>", "<|fim_suffix|>", "<|fim_pad|>",
        "<|repo_name|>", "<|file_sep|>",
        "<think>", "</think>",
        "<|audio_start|>", "<|audio_end|>", "<|audio_pad|>",
        "<tts_pad>", "<tts_text_bos>", "<tts_text_eod>",
        "<tts_text_bos_single>",
    ),
}
# 立刻验方言名，两道闸都在装载前（评审：model_type 是 qwen3_5 而方言键是
# qwen35，手滑概率高；拖到 build_prompt 才炸要先白付 31GB 装载）：
# ①本脚本册子成员检查——注意 get_dialect_class("") 会静默回落缺省方言，
#   空串必须在这里显式拒掉，不能只靠注册表；②框架注册表检查。
if DIALECT not in SPECIAL_MARKERS:
    sys.exit(f"未知方言 {DIALECT!r}：本脚本已备标记册子的方言仅 "
             f"{sorted(SPECIAL_MARKERS)}（空串/别名同样拒）")
from framework.dialects import get_dialect_class as _validate_dialect  # noqa: E402
_validate_dialect(DIALECT)

PROMPT_TEXT = ("详细说明光合作用的完整过程，分光反应与暗反应两部分展开，"
               "每部分至少三点。")
# 造长上下文用的前置问题：**必须与测量问题不同主题**，否则前一轮答案会成为本轮
# 的现成草稿，接受率虚高（§4.4 那次 94.2% 假红利就是"文本可预测性"造出来的）。
CTX_QUESTION = ("请系统介绍板块构造学说：从大陆漂移假说讲到海底扩张，"
                "再到板块边界的三种类型及其代表性地貌，每部分展开论述。")

# 逐轮接受数（扁平列表，每轮一个 accepted），由走查探针填充；每档开跑前清空
ACCEPT_LOG: list = []


def report_moe_sort_state() -> str:
    """报告生产侧 MoE 排序阈值补丁的状态（**只读，本脚本不再自己打补丁**）。

    2026-08-15 之前这里有一份 bench 私有的 `install_moe_sort_patch`，由
    `MTP_FORCE_MOE_SORT=1` 开启。那份已迁进生产装载路
    （`framework/backends/mlx/patch/moe_sort_threshold.py`，随 gemma4_generate 导入
    即安装），bench 侧再留一份就会出现两个补丁互相覆盖、且"bench 能测到、生产拿不到"
    的老问题。此处只把**当前阈值**取回来随数打印——两张差 9% 的表若不带这个数，
    事后分不清是补丁生效还是热漂移。
    """
    try:
        from framework.backends.mlx.patch import moe_sort_threshold as P
        from mlx_vlm.models import switch_layers as SL
        on = SL.SwitchGLU.__call__ is P.switch_glu_call
        return (f"MoE 排序阈值补丁：{'已装' if on else '**未装**'}，"
                f"阈值={P.min_size()}（上游缺省 64）")
    except Exception as exc:  # 稠密档/接口变动都不该让 bench 挂掉
        return f"MoE 排序阈值补丁：状态未知（{type(exc).__name__}: {exc}）"


def install_accept_probe():
    """包住 `_mtp_acceptance_walk` 记录每轮 accepted；返回卸载函数。

    该函数返回 (accepted, new_tokens)，accepted 语义是首次不匹配前的命中数、
    不含 bonus（见 mlx_vlm/speculative/common.py:151-157）。
    """
    from mlx_vlm.speculative import mtp as M
    orig = M._mtp_acceptance_walk

    def wrapped(*a, **k):
        r = orig(*a, **k)
        # 不吞异常：探针坏掉与"没有轮次"必须可区分，否则 fmt_dist 只打印一个"—"
        ACCEPT_LOG.append(int(r[0]))
        return r

    M._mtp_acceptance_walk = wrapped
    return lambda: setattr(M, "_mtp_acceptance_walk", orig)


def _dialect(target_dir: str):
    from types import SimpleNamespace
    from transformers import AutoTokenizer
    from framework.dialects import get_dialect_class
    tok = AutoTokenizer.from_pretrained(target_dir)
    return get_dialect_class(DIALECT).create(
        SimpleNamespace(tokenizer=tok), options={"suppress_ghost_thinking": True}), tok


def build_prompt(target_dir: str, question: str = PROMPT_TEXT, prior=None) -> str:
    """prior=(问, 答) 时组装成两轮对话，用于把上下文顶长。"""
    d, _ = _dialect(target_dir)
    msgs = []
    if prior:
        msgs += [{"role": "user", "content": prior[0]},
                 {"role": "assistant", "content": prior[1]}]
    msgs.append({"role": "user", "content": question})
    return d.build_prompt(messages=msgs, include_env=False)


def n_tokens(target_dir: str, text: str) -> int:
    _, tok = _dialect(target_dir)
    return len(tok.encode(text, add_special_tokens=False))


def run_once(be, prompt: str, sampling: dict):
    """跑一轮，返回 (解码 tps, tokens, 秒, 全量文本, spec_drafted, spec_accepted, 墙钟 tps)。

    **两个 tps 必须分开**：墙钟 tps = n/dt 含预填，长上下文格里预填会淹没解码差异；
    投机采样只改解码段，故对表一律用后端报的 `generation_tps`（不含预填）。
    ctx=0 时两者几乎相等，长 ctx 时可能差几倍——混用即得出错误结论。

    文本必须**累加** ch.text —— GenChunk.text 是 detokenizer 的本次分段而非累积量，
    直接拿它当全量输出比对会比出假分歧（本项目 §4.3 的原错）。
    """
    cache = be.new_cache()
    parts, last = [], None
    t0 = time.perf_counter()
    for ch in be.stream_generate(prompt, cache=cache, sampling=sampling):
        if ch.text:
            parts.append(ch.text)
        last = ch
    dt = time.perf_counter() - t0
    ex = (last.extra or {}) if last else {}
    text = "".join(parts)
    n = last.generation_tokens if last else 0
    # **不兜底**：拿不到 generation_tps 时宁可抛错，也不能悄悄退回 n/dt——
    # 后者含预填，长上下文格里能低几倍，混进同一列就是无声的系统性偏差。
    if not last or not last.generation_tps:
        raise RuntimeError(
            "后端未报 generation_tps，无法给出仅解码口径的 tps。"
            "拒绝退回墙钟 n/dt（含预填，与本表其余格不同口径）。")
    return (last.generation_tps, n, dt, text,
            ex.get("spec_drafted"), ex.get("spec_accepted"),
            n / dt if dt else 0.0)


def fmt_dist(accs: list, gamma: int) -> str:
    """P(accepted>=k)，k=1..gamma —— 逐位命中率，vLLM 侧同名量可直接对表。"""
    if not accs:
        return "—"
    n = len(accs)
    return " ".join(f"{sum(1 for a in accs if a >= k) / n:5.1%}"
                    for k in range(1, gamma + 1))


async def main() -> int:
    from framework.backends.mlx.backend import MLXBackend

    print(f"目标件: {TARGET}\n草稿件: {DRAFT}")
    print(f"块列表: {BLOCKS}  重复: {REPEATS}  max_tokens: {MAX_TOKENS}  ctx目标: {CTX}\n")

    sampling = {"max_tokens": MAX_TOKENS, "temperature": 0.0,
                "repetition_penalty": 1.1, "repetition_context_size": 50}

    # 装载时必须带一个非空 draft_block_size，否则 backend 不会开
    # `prefer_requested_block_size` 旁路，逐档下发的块大小会被草稿件 config
    # 的天花板（=4）吃掉（见 backends/mlx/backend.py:174）。
    t0 = time.perf_counter()
    if min(BLOCKS) < 2:
        raise SystemExit("block_total 必须 ≥2：=1 时 MTP 轮循环在首个 token 后即退出，"
                         "该行会给出无意义的 tps 且不报错。")
    be = await MLXBackend.load(TARGET, speculative={
        "mode": "drafter", "draft_model": DRAFT, "draft_block_size": BLOCKS[0]})
    # 整个扫描的前提是块大小旁路真的打开了；否则每一档都被草稿件 config 的缺省 4
    # 顶死，扫出来的是同一个 γ，而表面看毫无异常（首过评审 M4）。
    if not getattr(be.draft_model, "prefer_requested_block_size", False):
        raise SystemExit("草稿深度上限旁路未生效，逐档下发的块大小会被缺省值顶死，"
                         "扫描无意义。检查 backends/mlx/backend.py 的 _load_drafter。")
    print(f"装载完成 {time.perf_counter() - t0:.1f}s\n")

    prompt = build_prompt(TARGET)
    if CTX > 0:
        # 用**基线**（非投机）生成一段真实回答当前一轮，把上下文顶到 CTX。
        # 用基线生成是必须的：拿投机路的产出当上下文会让被测对象参与造自己的输入。
        be.speculative["mode"] = "off"
        warm = {"max_tokens": CTX, "temperature": 0.0,
                "repetition_penalty": 1.1, "repetition_context_size": 50}
        print(f"=== 造长上下文（基线生成 ≤{CTX} tok 的前一轮回答）===")
        tw = time.perf_counter()
        prior_answer = run_once(be, build_prompt(TARGET, CTX_QUESTION), warm)[3]
        # 回灌前必须查特殊标记：后端不 skip_special_tokens，解码文本里若混进轮/频道
        # 标记，再经方言组装就会变成**真的**控制 token，造出畸形 prompt 且全程不报错，
        # 接受率随之失真（首过评审 M6）。
        # 标记表随方言走（评审 Critical：qwen35 场只查 gemma 标记＝守卫静默空转，
        # 恰是本守卫要防的"畸形 prompt 不报错"形态）；册子本体在模块顶
        # SPECIAL_MARKERS，与方言 fail-fast 同一本账。
        bad = [m for m in SPECIAL_MARKERS[DIALECT]
               if m in prior_answer]
        if bad:
            raise RuntimeError(f"前一轮答里含特殊标记 {bad}，回灌会造出畸形 prompt。"
                               "请改用 skip_special_tokens 或换 prompt。")
        prompt = build_prompt(TARGET, PROMPT_TEXT, prior=(CTX_QUESTION, prior_answer))
        got = n_tokens(TARGET, prompt)
        print(f"前一轮答 {len(prior_answer)} 字符 / {time.perf_counter()-tw:.1f}s；"
              f"最终 prompt = {got} tok（目标 {CTX}）")
        # 模型可能远早于 CTX 就 EOS，届时"ctx=3000"的标签会是假的（首过评审 M5）。
        if got < CTX * 0.8:
            raise RuntimeError(f"实际上下文仅 {got} tok，不足目标 {CTX} 的 80%："
                               "模型提前结束了，本格标签会与事实不符。")
        print()

    print(report_moe_sort_state())
    uninstall = install_accept_probe()
    try:
        results: dict = {}

        def baseline(tag: str):
            be.speculative["mode"] = "off"
            rows = [run_once(be, prompt, sampling) for _ in range(REPEATS)]
            results[tag] = rows
            tps = [r[0] for r in rows]
            wall = [r[6] for r in rows]
            print(f"{tag:<14} 解码tps {statistics.median(tps):7.2f}  "
                  f"(min {min(tps):.2f} max {max(tps):.2f})  "
                  f"墙钟tps {statistics.median(wall):6.2f}  "
                  f"tok {rows[0][1]}  sha {hashlib.sha256(rows[0][3].encode()).hexdigest()[:12]}")
            return rows

        print("=== 基线（mode=off）===")
        base_rows = baseline("baseline-前")
        base_tps = statistics.median([r[0] for r in base_rows])
        base_sha = hashlib.sha256(base_rows[0][3].encode()).hexdigest()

        print("\n=== 投机各档 ===")
        be.speculative["mode"] = "drafter"
        for blk in BLOCKS:
            be.speculative["draft_block_size"] = blk
            rows, dists = [], []
            for _ in range(REPEATS):
                ACCEPT_LOG.clear()
                r = run_once(be, prompt, sampling)
                rows.append(r)
                dists.append(list(ACCEPT_LOG))
            results[f"block-{blk}"] = rows
            tps = [r[0] for r in rows]
            med = statistics.median(tps)
            drafted = sum(r[4] or 0 for r in rows)
            accepted = sum(r[5] or 0 for r in rows)
            rate = accepted / drafted if drafted else 0.0
            gamma = blk - 1
            same = hashlib.sha256(rows[0][3].encode()).hexdigest() == base_sha
            # 交叉校验：走查探针与后端仪表必须对得上，否则 spec_stats 丢失时会
            # 悄悄退化成"接受率 0.0%"却仍打印 2.4× 的加速比（首过评审 M8）。
            all_acc = sum(dists, [])
            if not all_acc:
                raise RuntimeError(f"block {blk}: 走查探针一轮都没记到，接受率统计不可信。")
            if accepted and abs(sum(all_acc) - accepted) > max(2, 0.02 * accepted):
                raise RuntimeError(
                    f"block {blk}: 探针接受数 {sum(all_acc)} 与后端仪表 {accepted} 不符。")
            # AL 用**精确式** 1 + E[每轮接受数]（≡ 1 + Σ_k P(≥k)）。
            # 近似式 1 + γ·(accepted/drafted) 在尾轮草稿数不足 γ 时会把 AL 抬高
            # 最多约 1.6%（首过评审 M3）。
            al = 1 + sum(all_acc) / len(all_acc)
            ntok = [r[1] for r in rows]
            base_ntok = statistics.median([r[1] for r in base_rows])
            dev = abs(statistics.median(ntok) - base_ntok) / max(1, base_ntok)
            # 输出与基线不同 **不等于** 有错：M>1 与 M=1 走不同 MLX 内核，浮点归约
            # 次序不同会在近似平局位翻。良性判定（check_mtp_equivalence.py +
            # §1.7.4）**仅对 gemma4 档建立**（关处理器时同样分歧）；其它方言此处
            # 只报"不同"，不背书良性。只作提示，不作失败判据。
            print(f"block {blk:>2} (γ={gamma})  解码tps {med:7.2f} "
                  f"(min {min(tps):.2f} max {max(tps):.2f})  "
                  f"加速 {med / base_tps:5.2f}×  "
                  # 轮数报**全部重复之和**，与 接受率/AL 同一个分母；
                  # 早先只打 dists[0] 会让一行里出现两个分母（首过评审 Minor）
                  f"接受率 {rate:5.1%}  AL {al:4.2f}  轮 {len(all_acc):>4}  "
                  f"tok {statistics.median(ntok):.0f}"
                  f"{' ⚠️长度偏离基线%.0f%%' % (dev * 100) if dev > 0.02 else ''}  "
                  f"{'输出同基线' if same else ('输出与基线不同(数值,已判良性)' if DIALECT == 'gemma4' else '输出与基线不同(数值;等价性判据仅对 gemma4 建立)')}")
            print(f"{'':>13}P(≥k) k=1..{gamma}: {fmt_dist(all_acc, gamma)}")

        print()
        after = baseline("baseline-后")
        drift = statistics.median([r[0] for r in after]) / base_tps - 1
        print(f"\n基线漂移 {drift:+.1%}  "
              f"{'（可接受）' if abs(drift) < 0.05 else '⚠️ 超 5%，本批数据存疑'}")
    finally:
        uninstall()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
