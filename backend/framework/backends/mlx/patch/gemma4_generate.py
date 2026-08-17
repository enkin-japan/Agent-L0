import functools
import time
from typing import Any, Dict, Generator, List, Optional, Union

import mlx.core as mx
import mlx.nn as nn
from transformers import PreTrainedTokenizer
from mlx_lm.sample_utils import make_logits_processors, make_sampler

from mlx_vlm.models import cache
from mlx_vlm.utils import ThinkingBudgetCriteria, StoppingCriteria

from .gemma4_utils import prepare_inputs
from .rotating_cache_trim import install as _install_rotating_trim
try:  # 纯性能补丁：mlx_vlm 挪走 switch_layers 时降级为不装，不拖垮后端
    from .moe_sort_threshold import install as _install_moe_sort
except Exception as _exc:  # pragma: no cover - 上游模块结构变动时的降级路径
    # 阴性声明照 moe_sort_threshold.py 自家纪律：**每一种拒装都打一行**。
    # 静默不装 = 归档里两张差 9% 的表分不出来源。
    print(f"[MoE] 专家排序阈值补丁未安装：import 失败 {_exc!r}，沿用上游阈值 64")
    _install_moe_sort = None
from .mtp_processors import install as _install_mtp_processors
from .mtp_processors import processor_context as mtp_processor_context
from .mtp_processors import record_emitted as mtp_record_emitted
from mlx_vlm.generate import (
    GenerationResult,
    normalize_resize_shape,
    wired_limit,
    generate_step,
    maybe_quantize_kv_cache,
    DEFAULT_THINKING_START_TOKEN,
    DEFAULT_THINKING_END_TOKEN,
)


# 线程本地流(对齐上游 mlx_vlm.generate 同名全局的做法):普通 mx.new_stream 的流
# 只在创建它的线程可用——引擎把生成搬进专用推理线程后,主线程 import 时建的流会报
# "There is no Stream(gpu, N) in current thread"。thread-local 流按线程惰性解析,
# 生成在哪个线程跑都各自成流,与上游辅助函数(共享其自己的 thread-local 流)天然兼容。
generation_stream = mx.new_thread_local_stream(mx.default_device())

# §S.15 根修：旋转态 RotatingKVCache.trim 只改记账不搬内容（回滚后 logits
# max|Δ|=22）。装在这里而非 backend.py：凡用到本模块的投机/MTP 回滚路径都必须
# 带着这个修正，二者不可分离。幂等。
_install_rotating_trim()

# [MTP] 上游草稿件投机路不接 logits 处理器（repetition_penalty / 工具约束 guard
# 会静默失效）。同上，与本模块不可分离。幂等。
_install_mtp_processors()

# [MTP·MoE] 上游 SwitchGLU 的排序阈值 64 让 MTP 验证批（M×top_k = 16..48）
# 永远够不到排序路，同一专家被逐 token 重读。降到 16 后 26B 生产实测 1.37×
# （同一次装载内开/关交错 A/B：+9.0%，输出逐字符相同）。幂等；
# 稠密模型不经过 SwitchGLU，装了也不触发。
# 与上两条不同的是：mlx_vlm 若挪走 switch_layers，本模块的 import 会连带把
# gemma4_generate 整个拖挂（=后端起不来）。这是个**性能**补丁，不该有这种权力，
# 故容错降级——拿不到就不装，后端照常起。
if _install_moe_sort is not None:
    try:
        _install_moe_sort()
    except Exception as _exc:  # pragma: no cover - 安装期异常同样不该拖垮后端
        # 上一段注释宣称的容错意图只覆盖了 import，裸调用仍能把后端拖挂。
        print(f"[MoE] 专家排序阈值补丁未安装：install 失败 {_exc!r}，沿用上游阈值 64")


# ============================================================================
# prompt-lookup 投机采样（inference-speedup Phase 3）
#
# 结构镜像上游 generate_step（prefill 完全同构），decode 段替换为
# 「n-gram 草稿 → 主模型批量验证 → 逐位采样走查 → 拒绝回滚」的轮循环。
# 每轮内仍逐 token yield：外层 stream_generate 的 detokenizer / 停止判定 /
# thinking 预算 / 流式过滤协议全部不变。
#
# 正确性锚点：
# - 每个产出 token 都是主模型在「其真实前缀」下按同一 sampler 采出的样本，
#   草稿只决定验证批的形状，不改变分布（贪心下与普通路径逐 token 一致）；
# - 拒绝回滚用 gemma4 的 rollback_speculative_cache（探针实测：短上下文与滑窗
#   旋转态均逐位无损 max|Δ|=0.000000，含 TurboQuant 量化态）。旋转态的无损性
#   依赖 rotating_cache_trim 的 §S.15 根修——上游原实现在该态 max|Δ|=22；
# - repetition_penalty / 约束采样 guard 在验证走查中逐位应用，语义与普通
#   路径对齐（guard 只消费已产出 token，被拒草稿不污染其状态机）。
# ============================================================================


def _mtp_processor_bridge(gen, processors, prompt_tokens, penalty_window,
                          draft_model=None, spec_stats=None):
    """把 MTP 走查的处理器上下文套在解码生成器外层，并逐 token 登记已产出。

    做成包装生成器而非 with 块，是为了不动下方那段长消费循环的缩进。语义等价：
    生成器体在 `with` 内执行，消费方两次 `next()` 之间上下文保持进入态（线程本地，
    而生成恒在专用推理线程内串行）。登记发生在**上游吐出之后、消费方拿到之前**，
    因而必定早于下一轮走查——生成器惰性保证了这个次序。
    """
    snap = None
    if draft_model is not None and spec_stats is not None:
        from mlx_vlm.speculative.common import (
            speculative_stats_since,
            speculative_stats_snapshot,
        )
        snap = speculative_stats_snapshot(draft_model)

    with mtp_processor_context(processors, prompt_tokens=prompt_tokens,
                               penalty_window=penalty_window):
        idx = 0
        for item in gen:
            tok = item[0]
            if not isinstance(tok, int):
                # 上游若改吐 mx/numpy 0-d 标量：能无损取整的就取整继续登记
                # （登记不缺位，守护不降级）；其余形态一律响——静默跳过 =
                # 处理器登记全程停摆（guard 一个 token 都消费不到、惩罚窗口
                # 看不见生成段），与走查里"融合 target 不得降级"同一口径。
                item = getattr(tok, "item", None)
                val = item() if callable(item) else None
                if not isinstance(val, int):
                    raise RuntimeError(
                        f"MTP 解码生成器产出的 token 类型为 {type(tok).__name__} 而非 int："
                        "上游产出类型变了，处理器登记会静默停摆（repetition_penalty 与"
                        "工具约束 guard 双双失效），须适配后再放行。"
                    )
                tok = val
            mtp_record_emitted(tok, idx)
            idx += 1
            if snap is not None:
                # 接受率观测（§7-0 ④）：草稿件把轮次/接受/草稿数记在自身的
                # 终身计数器上，这里按快照取差，与 prompt_lookup 路同一容器回吐
                rounds, accepted, drafted = speculative_stats_since(draft_model, snap)
                if rounds is not None:
                    spec_stats["rounds"] = rounds
                    spec_stats["accepted"] = accepted
                    spec_stats["drafted"] = drafted
            yield item


class _NgramIndex:
    """上下文 n-gram → 该 n-gram 最近一次出现「之后」的位置。

    注册始终滞后尾部一个位置（尾部 n-gram 即查询键本身，注册了只会自指），
    查询命中返回严格更早的出现点，草稿=该出现点之后的续篇（可能被活尾截短）。

    刻意只取「最近出现」：生成态下它往往紧贴活尾、草稿短，但接受率极高
    （模型在延续自己刚写的模式）。实测优于「次近出现换整长草稿」——长草稿
    在散文段大量被拒，白烧验证 forward，还放大批量/单步浮点差异导致的
    贪心翻转概率（双槽版实测 0.81x + 逐位一致破裂；单槽版 1.6~2.2x + 全一致）。
    """

    def __init__(self, ns: List[int]):
        self.ns = sorted(set(n for n in ns if n > 0), reverse=True)
        self.maps: Dict[int, Dict[tuple, int]] = {n: {} for n in self.ns}
        self.registered = 0  # 已注册的「n-gram 结束位置」上界（不含）

    def catch_up(self, hist: List[int]) -> None:
        """把 n-gram 注册推进到 len(hist)-1（留尾部最后一位不注册）。"""
        upto = len(hist) - 1
        for end in range(self.registered, upto):
            for n in self.ns:
                if end + 1 >= n:
                    self.maps[n][tuple(hist[end + 1 - n: end + 1])] = end + 1
        self.registered = max(self.registered, upto)

    def draft(self, hist: List[int], k: int) -> List[int]:
        if k <= 0:
            return []
        self.catch_up(hist)
        for n in self.ns:
            if len(hist) < n:
                continue
            pos = self.maps[n].get(tuple(hist[-n:]))
            if pos is not None and pos < len(hist):
                return hist[pos: pos + k]
        return []


def lookup_generate_step(
    input_ids: mx.array,
    model: nn.Module,
    pixel_values,
    mask,
    *,
    max_tokens: int = 256,
    temperature: float = 0.0,
    repetition_penalty: Optional[float] = None,
    repetition_context_size: Optional[int] = 50,
    top_p: float = 1.0,
    min_p: float = 0.0,
    top_k: int = 0,
    logit_bias: Optional[Dict[int, float]] = None,
    prompt_cache: Optional[List[Any]] = None,
    kv_bits: Optional[float] = None,
    kv_group_size: int = 64,
    kv_quant_scheme: str = "turboquant",
    quantized_kv_start: int = 0,
    logits_processors: Optional[List[Any]] = None,
    prefill_step_size: Optional[int] = 2048,
    thinking_budget_criteria=None,
    context_token_ids: Optional[List[int]] = None,
    spec_num_draft: int = 8,
    spec_ngram_max: int = 3,
    spec_ngram_min: int = 2,
    spec_stats: Optional[Dict[str, int]] = None,
    **kwargs,
) -> Generator:
    """prompt-lookup 投机版 generate_step（B=1；视觉/音频经同一 get_input_embeddings 前缀路径）。"""
    quantize_cache_fn = functools.partial(
        maybe_quantize_kv_cache,
        quantized_kv_start=quantized_kv_start,
        kv_group_size=kv_group_size,
        kv_bits=kv_bits,
        kv_quant_scheme=kv_quant_scheme,
    )
    sampler = make_sampler(temp=temperature, top_p=top_p, min_p=min_p, top_k=top_k)

    # 处理器分流：guard（约束采样）走 consume/mask 直连协议；
    # 其余（repetition_penalty / logit_bias）走 (tokens, logits) 协议、按窗口喂历史。
    plain_procs = make_logits_processors(logit_bias, repetition_penalty, repetition_context_size)
    guard = None
    for p in (logits_processors or []):
        if hasattr(p, "consume") and hasattr(p, "mask_logits"):
            guard = p
        else:
            plain_procs.append(p)
    penalty_window = max(int(repetition_context_size or 50), 64)

    if prompt_cache is None:
        prompt_cache = cache.make_prompt_cache(model.language_model)
    lm = model.language_model if hasattr(model, "language_model") else model

    # ---------- prefill（与上游 generate_step 同构：embeddings + 分块 + 末步采样） ----------
    with mx.stream(generation_stream):
        embedding_output = model.get_input_embeddings(
            input_ids, pixel_values, mask=mask, **kwargs
        )
        inputs_embeds = embedding_output.inputs_embeds
        extra = {
            k: v for k, v in embedding_output.to_dict().items()
            if k != "inputs_embeds" and v is not None
        }
        if getattr(model, "no_chunked_prefill", False):
            prefill_step_size = None
        if prefill_step_size is not None and inputs_embeds.shape[1] > prefill_step_size:
            while inputs_embeds.shape[1] > 1:
                n_to_process = min(prefill_step_size, inputs_embeds.shape[1] - 1)
                lm(
                    inputs=input_ids[:, :n_to_process],
                    inputs_embeds=inputs_embeds[:, :n_to_process],
                    cache=prompt_cache,
                    n_to_process=n_to_process,
                    **extra,
                )
                quantize_cache_fn(prompt_cache)
                mx.eval([c.state for c in prompt_cache])
                inputs_embeds = inputs_embeds[:, n_to_process:]
                input_ids = input_ids[:, n_to_process:]
                mx.clear_cache()

        outputs = lm(
            input_ids, inputs_embeds=inputs_embeds, cache=prompt_cache, **extra
        )
        logits = outputs.logits[:, -1, :]
        # 惩罚窗口历史：与上游一致 = 实际喂过 _step 的 token（分块后仅剩末段）
        hist: List[int] = input_ids.flatten().tolist()
        if plain_procs:
            for proc in plain_procs:
                logits = proc(mx.array(hist[-penalty_window:]), logits)
        if guard is not None:
            logits = guard.mask_logits(logits)
        quantize_cache_fn(prompt_cache)
        logprobs = logits - mx.logsumexp(logits)
        y = sampler(logprobs)
        mx.eval(y)

    first = int(y.item())
    yield first, logprobs.squeeze(0)

    # ---------- decode：n-gram 草稿 + 批量验证轮循环 ----------
    # n-gram 语料 = 完整上下文（含 KV 复用前缀）+ 已产出 token；
    # 惩罚窗口历史 hist 与语料分开维护（对齐上游语义）。
    corpus: List[int] = list(context_token_ids or hist)
    corpus.append(first)
    hist.append(first)
    if guard is not None:
        guard.consume(first)
    ngram = _NgramIndex([spec_ngram_max, spec_ngram_min])
    tb = thinking_budget_criteria
    # 强制收束**只可能**在开思考态发生：ThinkingBudgetCriteria.in_thinking 的初值
    # 就是 enable_thinking，且只在 `enable_thinking and token==start` 一条路上转真
    # → budget_exceeded 只在 in_thinking 下置位 → forced_token_id 关思考态永不被设。
    # `enable_thinking` 在 criteria 构造后不再改写（上游只在 __init__ 赋值一次，
    # 本栈不碰），故本旗与 tb 一样全程恒定。
    # 用它而非 `tb is not None` 判预发闸：thinking_budget 缺省 2048 使 tb 几乎恒非空，
    # 而生产 enable_thinking 缺省 False——拿 tb 当判据会在**所有**生产请求上白白
    # 砍掉流水重叠，而那条不可回滚的路径根本不可达。
    forced_collapse_possible = tb is not None and getattr(tb, "enable_thinking", False)

    b = first          # 已产出、尚未入缓存的最新 token（缓存恒缺最后一个产出）
    emitted = 1
    token_dtype = mx.int32
    # AIMD 自适应草稿长度：全收加档、全拒减半（下限 1）。自由散文等低命中负载
    # 会快速收敛到 k=1（验证批 S=2，损耗≈普通 decode），高命中负载保持满档。
    cur_k = spec_num_draft
    # 冷却退避：连续全拒 ≥3 轮说明当前文段无复读结构，暂停出草稿（指数退避，
    # 上限 64 轮），期间即普通 decode；任一轮有接受立即复位。防止低命中负载
    # 无休止白烧验证 forward（实测自由散文 0% 接受时可省回 ~8% 吞吐）。
    miss_streak = 0
    cooldown = 0

    def _sample_row(row: mx.array):
        if plain_procs:
            for proc in plain_procs:
                row = proc(mx.array(hist[-penalty_window:]), row)
        if guard is not None:
            row = guard.mask_logits(row)
        lp = row - mx.logsumexp(row)
        t = int(sampler(lp).item())
        return t, lp

    def _advance(tok: int) -> None:
        corpus.append(tok)
        hist.append(tok)
        if guard is not None:
            guard.consume(tok)

    # SSM/GDN 混合模型（Qwen3.5 系：线性注意力层的 ArraysCache 不可裁）拒绝回滚
    # 需要验证 forward 时逐层捕获的状态元组（rollback_speculative_cache 从 init_state
    # 重放已接受前缀）；传 None 直接崩（2026-07-30 评测框架首跑抓到，此前两轮实机
    # 联调恰好未触发部分拒绝分支）。纯 KV 模型（gemma4）裁缓存即回滚，无需捕获。
    needs_gdn = any(getattr(l, "is_linear", False) for l in getattr(lm, "layers", []))

    def _launch(blk: List[int], draft: List[int]):
        """异步发起一次验证 forward，并把「惩罚+采样」预折叠进同一异步图。

        关键：草稿被接受的前缀就是各行的假定历史（hist + draft[:i]），发射时即可
        逐行预构惩罚窗口与采样节点——同步路径只剩一次 .tolist()，对齐上游普通
        路径的流水线形态。guard（约束采样）激活时无法入图（llguidance 状态机在
        python 侧），返回 pre=None 走逐位回退。走查中若 guard 中途激活，其后各行
        的预折叠结果作废、同样转逐位（logits_all 原始行仍在，可带 mask 重采样）。
        """
        with mx.stream(generation_stream):
            # capture_layer_ids=[] 只为触发 gdn_sink 创建（空列表不捕获 hidden）
            out = lm(mx.array([blk], dtype=token_dtype), cache=prompt_cache,
                     **({"capture_layer_ids": []} if needs_gdn else {}))
            gdn = out.gdn_states if needs_gdn else None
            logits_all = out.logits
            pre = None
            if guard is None or not guard.active:
                rows = []
                for i in range(len(blk)):
                    row = logits_all[:, i, :]
                    if plain_procs:
                        win = mx.array((hist + draft[:i])[-penalty_window:])
                        for proc in plain_procs:
                            row = proc(win, row)
                    rows.append(sampler(row - mx.logsumexp(row)))
                pre = mx.concatenate(rows)
        mx.async_eval(pre if pre is not None else logits_all)
        return logits_all, pre, gdn

    def _discard_block(n: int, gdn=None) -> None:
        """整块作废：缓存回退 n 条（强制收束打断已预发的下一轮时用）。

        〔§S.15 后按不变式已不可达〕预发只在 `not forced_collapse_possible` 时发生，
        而本函数的两个调用点都在 `tb.forced_token_id` 分支内（该旗只在开思考态才
        可能置位），故 `pending` 恒为 None、二者不相交。
        保留是因为它还带着 SSM/GDN 分支（别的模型形态会用），且一旦将来有人放宽
        预发条件，旋转态的整块作废会在 rotating_cache_trim 里**显式抛错**而非
        静默给出错误几何——不变式破了要响，不能哑。
        """
        if n <= 0:
            return
        with mx.stream(generation_stream):
            if not needs_gdn:
                # accepted=-1 → trim = block_size，即整块裁掉
                lm.rollback_speculative_cache(prompt_cache, None, -1, n)
                return
            # SSM 模型不走 accepted=-1（n=0 的零长重放核未定义）：可裁层照裁，
            # 线性层直接从捕获元组恢复 forward 前的状态与 conv 尾窗
            ssm = [c for c in prompt_cache if c is not None and not c.is_trimmable()]
            for c in prompt_cache:
                if c is not None and c.is_trimmable():
                    c.trim(n)
            for j, c in enumerate(ssm):
                (_q, _k, _v, _a, _b, _al, _dt, init_state,
                 _mask, conv_input, K) = gdn[j]
                c[1] = init_state
                c[0] = conv_input[:, : K - 1]

    def _next_draft() -> List[int]:
        nonlocal cooldown
        if cooldown > 0:
            cooldown -= 1
            return []
        return ngram.draft(corpus, cur_k)

    # 轮间流水线：walk 完本轮 → 回滚 → 立即异步预发下一轮 forward，再逐个 yield
    # 本轮 token——python 侧消费（detokenizer/流式回调/停止判定）与 GPU 上
    # 下一轮的 forward 重叠，对齐上游普通路径 async_eval 的重叠模式。
    pending = None  # (block, draft, logits_all, pre, gdn)

    while emitted < max_tokens:
        # thinking 预算强制收束：外层在上一个 yield 后可能置位 forced_token_id。
        # 语义对齐上游 apply_forced_token：丢弃采样、改发强制 token（缓存仍需推进 b）。
        if tb is not None and getattr(tb, "forced_token_id", None) is not None:
            if pending is not None:
                _discard_block(len(pending[0]), pending[4])
                pending = None
            forced = tb.forced_token_id
            tb.forced_token_id = None
            with mx.stream(generation_stream):
                lm(mx.array([[b]], dtype=token_dtype), cache=prompt_cache)
                quantize_cache_fn(prompt_cache)
            _advance(forced)
            b = forced
            emitted += 1
            yield forced, None
            continue

        if pending is not None:
            block, draft, logits_all, pre, gdn = pending
            pending = None
        else:
            draft = _next_draft()
            block = [b] + draft
            logits_all, pre, gdn = _launch(block, draft)

        # ---- 走查：预折叠行直接取值；guard 激活（含中途激活）后转逐位带 mask ----
        vals = pre.tolist() if pre is not None else None
        sequential = vals is None
        new_tokens: List[int] = []
        accepted = 0
        for i in range(len(block)):
            if sequential:
                t, _lp = _sample_row(logits_all[:, i, :])
            else:
                t = int(vals[i])
            new_tokens.append(t)
            _advance(t)
            if guard is not None and guard.active and not sequential:
                sequential = True  # guard 刚被 stc 激活：其后预折叠行无 mask，作废转逐位
            if i < len(draft) and t == draft[i]:
                accepted += 1
                continue
            break

        if draft:
            if spec_stats is not None:
                spec_stats["drafted"] = spec_stats.get("drafted", 0) + len(draft)
                spec_stats["accepted"] = spec_stats.get("accepted", 0) + accepted
                spec_stats["rounds"] = spec_stats.get("rounds", 0) + 1
            if accepted == len(draft):
                cur_k = min(spec_num_draft, cur_k + 2)
            elif accepted == 0:
                cur_k = max(1, cur_k // 2)
            if accepted > 0:
                miss_streak = 0
            else:
                miss_streak += 1
                if miss_streak >= 3:
                    cooldown = min(64, 8 << min(miss_streak - 3, 3))

        # 拒绝回滚：缓存写入了整个 block（len(block) 条），有效的是 b + 已接受草稿
        rejected = len(block) - (accepted + 1)
        if rejected > 0:
            with mx.stream(generation_stream):
                lm.rollback_speculative_cache(prompt_cache, gdn, accepted, len(block))
        with mx.stream(generation_stream):
            quantize_cache_fn(prompt_cache)

        b = new_tokens[-1]

        # ---- 预发下一轮（与下方 yield 段的 python 消费重叠）----
        # 开思考态不预发（§S.15）：强制收束会调 _discard_block 整块作废已预发的块，
        # 而**整块作废在旋转态不可逆**——rollback 用 accepted=-1 即 trim=block_size，
        # 切完只剩 max_size-1 条，凑满窗所需的最老 token 已被 _update_concat 的前裁
        # 淘汰、不复存在。`forced_collapse_possible` 全程恒定（见其定义），故本条使
        # 「pending 非空 ⟹ 强制收束分支不可达 ⟹ _discard_block 不可达」成为不变式。
        # 代价只落在开思考态的请求上（生产缺省关思考，不受影响）；若将来开思考态
        # 也要这层重叠，正解是预发前快照将被淘汰的那几行 KV 以支持无损恢复，另案。
        if emitted + len(new_tokens) < max_tokens and not forced_collapse_possible:
            nd = _next_draft()
            nb = [b] + nd
            nl, npre, ngdn = _launch(nb, nd)
            pending = (nb, nd, nl, npre, ngdn)

        for j, tok in enumerate(new_tokens):
            emitted += 1
            yield tok, None
            if emitted >= max_tokens:
                return
            # 外层在 yield 间隙可能触发 thinking 强制收束
            if tb is not None and getattr(tb, "forced_token_id", None) is not None:
                if pending is not None:
                    _discard_block(len(pending[0]), pending[4])  # 预发轮作废（其起点采样将被强制替代）
                    pending = None
                if j < len(new_tokens) - 1:
                    # 块中截断：未产出的已接受部分从缓存裁掉（保留 block 前 j+1 条）
                    extra_trim = (accepted + 1) - (j + 1)
                    if extra_trim > 0:
                        with mx.stream(generation_stream):
                            lm.rollback_speculative_cache(
                                prompt_cache, gdn, j, accepted + 1)
                    # 作废的产出从惩罚历史/语料收回（guard 无法收回，fail-open 语义可容忍：
                    # 强制收束只在 thinking 段发生，guard 在该段本就不激活）
                    n_cut = len(new_tokens) - 1 - j
                    del corpus[len(corpus) - n_cut:]
                    del hist[len(hist) - n_cut:]
                    # 截断后钳制注册指针（截掉区间的条目已失效——只影响命中率，
                    # 不影响正确性：坏草稿会被验证拒绝）
                    ngram.registered = min(ngram.registered, max(0, len(corpus) - 1))
                    b = tok
                break

        if emitted % 256 == 0:
            mx.clear_cache()


# ---- 前缀复用的回滚能力门（T8 根修，2026-08-17）--------------------------
# 复用 = 把缓存回滚到公共前缀、只喂新增段。回滚不是每种缓存都做得到：
#   · 稠密 KVCache：物理切片，恒可；
#   · TurboQuantKVCache：自带 trim/offset 协议，按 is_trimmable() 判；
#   · RotatingKVCache：线性态记账 trim 无损；**旋转态**必须走 §S.15 的
#     时序化+物理切尾，且被淘汰出窗的 token 已不存在时语义上无法回滚
#     （rotating_cache_trim 同一判据）。修复前 stream_generate 里按稠密语义
#     切 `[:prefix_len]`——旋转态 `keys.shape[2]`（=窗宽）常 < prefix_len，
#     条件恒假 → 缓存原样带着分叉尾复用，窗内容/offset 双错。gemma 系滑窗
#     仅 512/1024，多轮超窗即中——08-13 bench v1「26B 复用后瞬间 EOS、31B
#     窜孤立 <channel|>」的形态与此完全吻合；
#   · 无 keys 协议的递归状态缓存（qwen3_5 线性注意力 ArraysCache）：根本
#     没有回滚接口。修复前被裁剪循环整个跳过 → 全注意力层回滚了、线性层
#     还带着上一轮的尾巴，且重喂段被其状态**二次消化**（A8 评审 C1 实锤，
#     2026-08-17）。
# 门的口径：任何一层无法无损回滚 → 整轮放弃复用退冷预填（慢但正确），
# 响亮日志。静默近似是 §S.15 的成因，不再重演。

def _offset_int(c) -> Optional[int]:
    """缓存的流位置（已消费 token 数）；无 offset 协议返回 None。

    不用 `or 0` 布尔化——BatchKVCache 的 offset 是 mx.array，布尔化会抛。
    """
    v = getattr(c, "offset", None)
    return None if v is None else int(v)


def _dispatch(c) -> str:
    """缓存回滚能力分派（blocker 与 trim 共用同一张表，防两侧口径漂移）：

    rotating  — RotatingKVCache（isinstance；hasattr(max_size) 太脆——
                StaticPrefixKVCache 也带 max_size 却无 _temporal_order）
    dense     — keys 是稠密 mx.array：物理切片恒可
    trimproto — 自带 trim/is_trimmable 协议的打包结构（TurboQuant / 上游
                QuantizedKVCache / HybridQuantKVCache：keys 是 tuple，按稠密
                切片会炸 'tuple' has no 'shape'）
    stateful  — 其余（qwen3_5 线性注意力 ArraysCache 等递归状态）：无回滚接口
    """
    if isinstance(c, cache.RotatingKVCache):
        return "rotating"
    if isinstance(getattr(c, "keys", None), mx.array):
        return "dense"
    if hasattr(c, "trim") and hasattr(c, "is_trimmable"):
        return "trimproto"
    return "stateful"


def _prefix_reuse_blocker(kv_cache, prefix_len: int) -> Optional[str]:
    """返回 None=全部缓存可无损回滚到 prefix_len；否则返回放弃原因。只读不动缓存。

    excess 一律逐层算（offset 各归各）——层间 offset 理论上同步，但门与
    trim 必须同口径，全局 max 口径曾让两侧隐式不一致（首过评审 M5）。
    """
    offsets = [o for o in (_offset_int(c) for c in kv_cache if c is not None)
               if o is not None]
    if not offsets:
        # 整组缓存都不暴露流位置：分叉尾存在与否不可判。fail-open 正是本门
        # 要消灭的缺陷形态（首过评审 C1），此处必须拒绝而不是放行
        return "缓存组无任何 offset 承载层，流位置不可判"
    stream_excess = max(offsets) - prefix_len
    if stream_excess == 0:
        return None          # 纯追加：无需回滚，任何缓存类型都安全
    if stream_excess < 0:
        # 缓存比声称的前缀还短：不是纯追加而是流状态异常——放行会让
        # [offset, prefix_len) 的 token 一个都不进模型（阶段复审 m12：
        # find_prefix_length 以 len(token_ids) 封顶、正常路径不可达，
        # 但本门的自我声明是 fail-closed，不留唯一一个不判定的放行口）
        return (f"缓存流位置 {max(offsets)} 短于公共前缀 {prefix_len}，"
                "流状态异常")
    for i, c in enumerate(kv_cache):
        if c is None:
            continue
        kind = _dispatch(c)
        off = _offset_int(c)
        excess = (off - prefix_len) if off is not None else None
        if kind == "dense":
            continue         # 稠密：按 offset 物理切片恒可行
        if kind == "trimproto":
            if excess is not None and excess > 0 and not c.is_trimmable():
                return f"层 {i} {type(c).__name__} 不可回裁（is_trimmable=False）"
            continue
        if kind == "stateful":
            state = getattr(c, "cache", None)
            has_state = (any(x is not None for x in state)
                         if isinstance(state, list) else True)
            if has_state:
                return f"层 {i} {type(c).__name__} 是递归状态缓存，无回滚接口"
            continue
        # rotating：复刻 rotating_cache_trim 的可裁判据（不动缓存）
        if c.keys is None or excess is None or excess <= 0:
            continue
        n = min(off, excess)
        if off < c.max_size:
            continue         # 线性态记账 trim 无损
        valid = int(c._temporal_order(c.keys).shape[2])
        kept = valid - n
        if n >= valid or (kept < c.max_size and valid != off):
            return (f"层 {i} RotatingKVCache 旋转态不可回裁"
                    f"（excess={n}, 时序序 {valid} 条, offset={off}, "
                    f"max_size={c.max_size}）")
    return None


def _trim_caches_to(kv_cache, prefix_len: int) -> None:
    """把混合类型缓存组整体回滚到 prefix_len（前提：`_prefix_reuse_blocker` 已判 None）。"""
    for c in kv_cache:
        if c is None:
            continue
        kind = _dispatch(c)
        off = _offset_int(c)
        excess = (off - prefix_len) if off is not None else None
        if excess is None or excess <= 0:
            continue
        if kind == "rotating":
            # 一律走 §S.15 修正版 trim——线性态记账回退、旋转态时序化+物理切尾
            # （修复前的稠密切片在旋转态是错的）
            c.trim(excess)
        elif kind == "trimproto":
            # 打包量化态无稠密张量，用自身 trim/offset 协议回退
            if c.is_trimmable():
                c.trim(excess)
        elif kind == "dense":
            # 稠密：物理切片。excess 按 offset 判（首过评审 C2：物理缓冲是
            # 256 步预分配的，按 keys.shape[2] 判会把未写入的全零行抬进有效区）
            c.keys = c.keys[:, :, :prefix_len, :]
            c.values = c.values[:, :, :prefix_len, :]
            c.offset = prefix_len
        # stateful：blocker 保证 excess>0 时根本走不到这里


def generate(
    model: nn.Module,
    processor: PreTrainedTokenizer,
    prompt: str,
    image: Union[str, List[str]] = None,
    audio: Union[str, List[str]] = None,
    video: Union[str, List[str]] = None,
    verbose: bool = False,
    **kwargs,
) -> GenerationResult:
    """
    Generate text from the model.

    Args:
       model (nn.Module): The language model.
       tokenizer (PreTrainedTokenizer): The tokenizer.
       prompt (str): The string prompt.
       temperature (float): The temperature for sampling (default 0).
       max_tokens (int): The maximum number of tokens (default 100).
       verbose (bool): If ``True``, print tokens and timing information
           (default ``False``).
       formatter (Optional[Callable]): A function which takes a token and a
           probability and displays it.
       repetition_penalty (float, optional): The penalty factor for repeating tokens.
       repetition_context_size (int, optional): The number of tokens to consider for repetition penalty.
    """

    if verbose:
        print("=" * 10)
        files = []
        if image is not None:
            files.extend(image)
        if audio is not None:
            files.extend(audio)
        if video is not None:
            files.extend(video if isinstance(video, list) else [video])

        print(f"Files: {files}", "\n")

        print("Prompt:", prompt)

    text = ""
    last_response = None

    eos_tokens = kwargs.get("eos_tokens", None)
    stopping_criteria = kwargs.get("stopping_criteria", None)

    # Get the tokenizer
    tokenizer = processor.tokenizer if hasattr(processor, "tokenizer") else processor

    # Add custom EOS tokens to the stopping criteria
    if eos_tokens is not None:
        tokenizer.stopping_criteria.add_eos_token_ids(eos_tokens)

    # Use custom stopping criteria
    elif stopping_criteria is not None:
        if isinstance(stopping_criteria, StoppingCriteria) or callable(
            stopping_criteria
        ):
            tokenizer.stopping_criteria = stopping_criteria
        else:
            raise ValueError(
                "stopping_criteria must be an instance of StoppingCriteria or a callable"
            )
    else:
        tokenizer.stopping_criteria.reset(model.config.eos_token_id)

    for response in stream_generate(
        model, processor, prompt, image, audio, video, **kwargs
    ):
        if verbose:
            print(response.text, end="", flush=True)
        text += response.text
        last_response = response

    if verbose:
        print("\n" + "=" * 10)
        if len(text) == 0:
            print("No text generated for this prompt")
            return GenerationResult(
                text=text,
                token=None,
                logprobs=None,
                prompt_tokens=0,
                generation_tokens=0,
                total_tokens=0,
                prompt_tps=0.0,
                generation_tps=0.0,
                peak_memory=mx.get_peak_memory() / 1e9,
            )
        print(
            f"Prompt: {last_response.prompt_tokens} tokens, "
            f"{last_response.prompt_tps:.3f} tokens-per-sec"
        )
        print(
            f"Generation: {last_response.generation_tokens} tokens, "
            f"{last_response.generation_tps:.3f} tokens-per-sec"
        )
        print(f"Peak memory: {last_response.peak_memory:.3f} GB")

    return GenerationResult(
        text=text,
        token=last_response.token,
        logprobs=last_response.logprobs,
        prompt_tokens=last_response.prompt_tokens,
        generation_tokens=last_response.generation_tokens,
        total_tokens=last_response.total_tokens,
        prompt_tps=last_response.prompt_tps,
        generation_tps=last_response.generation_tps,
        peak_memory=last_response.peak_memory,
    )



def stream_generate(
    model: nn.Module,
    processor: PreTrainedTokenizer,
    prompt: str,
    image: Union[str, List[str]] = None,
    audio: Union[str, List[str]] = None,
    **kwargs,
) -> Union[str, Generator[str, None, None]]:
    """
    A generator producing text based on the given prompt from the model.

    Args:
        model (nn.Module): The model to use for generation.
        processor (PreTrainedTokenizer): The tokenizer/processor.
        prompt (str): The input prompt text.
        image (Union[str, List[str]], optional): Image path(s) or URL(s).
        audio (Union[str, List[str]], optional): Audio file path(s).
        prefill_step_size (int, optional): Number of tokens to process per prefill
          step. When set, enables chunked prefill which processes long prompts in
          smaller chunks to reduce peak memory usage.
        kwargs: Additional options passed to :func:`generate_step`.
          See :func:`generate_step` for more details.

    Yields:
        Generator[GenerationResult]: A generator producing GenerationResult objects
          containing the generated text, tokens, and statistics.
    """
    tokenizer = processor.tokenizer if hasattr(processor, "tokenizer") else processor

    # [Phase 3] 投机采样配置（在 prepare_inputs 之前弹出，避免陌生 kwarg 下渗）
    speculative_cfg = kwargs.pop("speculative", None) or {}
    spec_stats = kwargs.pop("spec_stats", None)
    # 草稿件对象由后端加载后透传（配置里只有路径；模型不进 config 字典）
    draft_model = kwargs.pop("draft_model", None)
    draft_kind = kwargs.pop("draft_kind", None)

    # [MTP] 处理器归一：把 repetition_penalty / logit_bias 等标量旋钮**在这里**
    # 统一构造成处理器列表交给 generate_step，而不是让它自己在内部再造一份。
    # 理由：上游草稿件投机路（run_speculative_rounds）根本不接处理器，我们要在
    # MTP 走查里补应用（mtp_processors），而"补应用的那份"必须与"上游 _step 用的
    # 那份"是同一个列表——各造一份必然随上游演进而漂移。标量旋钮除构造处理器外
    # 别无他用（ar.py 全文只在 make_logits_processors 调用处出现），故等价。
    # 条件与下方分派判据保持一致：prompt_lookup 优先级更高，若两者同时配上，
    # 这里不该把标量旋钮搬走——lookup_generate_step 自建处理器，搬走即丢惩罚。
    # （后端只在 mode=drafter 时才装草稿件，故实际不会同时成立；此处是让代码本身
    #  不依赖那个外部约束。）
    use_mtp = (speculative_cfg.get("mode") == "drafter") and draft_model is not None
    if speculative_cfg.get("mode") == "drafter" and draft_model is None:
        raise RuntimeError(
            "speculative.mode=drafter 但没有草稿件——静默按非投机跑会把配置错误"
            "伪装成「怎么没变快」的性能玄学（§S.3）。请配 speculative.draft_model。"
        )
    mtp_processors_list: Optional[List[Any]] = None
    mtp_penalty_window = max(int(kwargs.get("repetition_context_size") or 50), 64)
    if use_mtp:
        # 必须用 mlx_vlm 那版（= generate_step 内部用的同一个），不是本文件顶部
        # 导入的 mlx_lm 版——两者不是同一个函数，混用会让 MTP 路与非投机路的
        # 惩罚语义悄悄分叉
        from mlx_vlm.sample_utils import (
            make_logits_processors as _vlm_make_logits_processors,
        )
        mtp_processors_list = _vlm_make_logits_processors(
            kwargs.pop("logit_bias", None),
            kwargs.pop("repetition_penalty", None),
            kwargs.pop("repetition_context_size", 20),
            kwargs.pop("presence_penalty", None),
            kwargs.pop("presence_context_size", 20),
            kwargs.pop("frequency_penalty", None),
            kwargs.pop("frequency_context_size", 20),
        )
        mtp_processors_list.extend(kwargs.pop("logits_processors", None) or [])
        kwargs["logits_processors"] = mtp_processors_list

    # Set up thinking budget criteria if requested
    thinking_budget = kwargs.pop("thinking_budget", None)
    thinking_end_token = kwargs.pop("thinking_end_token", DEFAULT_THINKING_END_TOKEN)
    thinking_start_token = kwargs.pop(
        "thinking_start_token", DEFAULT_THINKING_START_TOKEN
    )
    enable_thinking = kwargs.pop("enable_thinking", False)

    # Skip special tokens
    skip_special_tokens = kwargs.pop("skip_special_tokens", False)
    skip_special_token_ids = (
        set(tokenizer.all_special_ids)
        if skip_special_tokens and hasattr(tokenizer, "all_special_ids")
        else []
    )

    add_special_tokens = (
        getattr(processor, "chat_template", None) is None
        if model.config.model_type in ["gemma3", "gemma3n", "gemma4"]
        else True
    )

    resize_shape = normalize_resize_shape(kwargs.pop("resize_shape", None))
    image_token_index = getattr(model.config, "image_token_index", None)
    vision_cache = kwargs.pop("vision_cache", None)

    if kwargs.get("input_ids", None) is not None:
        input_ids = kwargs.pop("input_ids")
        pixel_values = kwargs.pop("pixel_values", None)
        mask = kwargs.pop("mask", None)
    else:
        inputs = prepare_inputs(
            processor,
            images=image,
            audio=audio,
            prompts=prompt,
            image_token_index=image_token_index,
            resize_shape=resize_shape,
            add_special_tokens=add_special_tokens,
            **kwargs,
        )
        input_ids = inputs.get("input_ids", None)
        pixel_values = inputs.get("pixel_values", None)
        mask = inputs.get("attention_mask", None)
        data_kwargs = {
            k: v
            for k, v in inputs.items()
            if k not in ["input_ids", "pixel_values", "attention_mask"]
        }
        kwargs.update(data_kwargs)

    # Vision feature caching: reuse cached image features across turns
    if vision_cache is not None and image is not None and pixel_values is not None:
        cached = vision_cache.get(image)
        if cached is not None:
            kwargs["cached_image_features"] = cached
        elif hasattr(model, "encode_image"):
            features = model.encode_image(pixel_values)
            mx.eval(features)
            vision_cache.put(image, features)
            kwargs["cached_image_features"] = features

    # Prompt cache reuse: skip common prefix from previous turn
    prompt_cache_state = kwargs.pop("prompt_cache_state", None)
    reused_prefix_len = 0
    full_input_ids_list = input_ids.flatten().tolist()

    # 前缀命中可观测性（T4R）：挂在 state 上供后端读走进 GenChunk.extra——
    # 命中退化（装配层把易变段挪到了前面）只有被看见才会被修。先记 0，命中时更新。
    # reuse_blocked 同理（T8 门）：被门拒和压根没命中在 reused_prefix=0 上同貌，
    # 只有分开回吐才能在线上区分退化与未命中（首过评审 M7）。
    if prompt_cache_state is not None:
        prompt_cache_state.last_reused_prefix = 0
        prompt_cache_state.reuse_blocked = None

    # [MTP] 缓冲缓存形态一律不复用前缀。MTP 跑过一轮后，上游
    # `_buffer_mtp_target_cache` 会把 RotatingKVCache **就地**换成
    # BufferedRotatingKVCache（同一个 prompt_cache 列表对象，也就是我们持久化的
    # 那份）。它的 `keys.shape[2]` 是带回滚缓冲的**容量**而非有效长度，旋转后有效
    # 内容跨 [start_position, offset)——下面那段按稠密语义切 [:prefix_len] 会留下
    # 错的 token 且 start_position 变陈旧，表现为跨轮上下文静默错乱。
    # 判据必须放在这里：再往下 input_ids 就被截断了，那时候再放弃就成了拿残缺
    # prompt 冷预填，比不复用更糟。
    if prompt_cache_state is not None and prompt_cache_state.cache is not None \
            and any(hasattr(c, "start_position") for c in prompt_cache_state.cache
                    if c is not None):
        print("[Memory] MTP 缓冲缓存形态：本轮放弃前缀复用（退冷预填，慢但正确）")
    elif prompt_cache_state is not None and prompt_cache_state.cache is not None:
        prefix_len = prompt_cache_state.find_prefix_length(full_input_ids_list)
        _reusable = prefix_len > 0 and prefix_len < input_ids.shape[1]
        if _reusable:
            # 回滚能力门（T8 根修）必须在 input_ids 截断**之前**判：过了这行再
            # 放弃就成了拿残缺 prompt 冷预填（与上面 MTP 缓冲形态判据同一条纪律）
            _blocker = _prefix_reuse_blocker(prompt_cache_state.cache, prefix_len)
            if _blocker is not None:
                print(f"[Memory] 前缀复用放弃（退冷预填，慢但正确）：{_blocker}")
                prompt_cache_state.reuse_blocked = _blocker
                _reusable = False
        if _reusable:
            reused_prefix_len = prefix_len
            prompt_cache_state.last_reused_prefix = prefix_len
            # Trim to only new tokens
            input_ids = input_ids[:, prefix_len:]
            new_ids = input_ids.flatten().tolist()

            # Only skip vision if no image tokens in the new (trimmed) tokens
            image_token_id = getattr(model.config, "image_token_id", None) or getattr(
                model.config, "image_token_index", None
            )

            has_image_in_new = image_token_id is not None and image_token_id in new_ids
            if not has_image_in_new:
                pixel_values = None
                kwargs.pop("cached_image_features", None)
            else:
#####################################################################
# 补丁部分 开始
#####################################################################
                if pixel_values is not None :
                    # 1. 确定萝卜总数（探针证实 pixel_values 是个 List）
                    if isinstance(pixel_values, list):
                        total_images = len(pixel_values)
                    elif hasattr(pixel_values, "shape"):
                        is_batched = len(pixel_values.shape) >= 4 and pixel_values.shape[0] == 1
                        total_images = pixel_values.shape[1] if is_batched else pixel_values.shape[0]
                    else:
                        total_images = 0

                    # 2. 确定坑位总数
                    total_holes = full_input_ids_list.count(image_token_id)
                    
                    if total_images > 0 and total_holes > 0:
                        # 计算单张图片膨胀后的 Token 数量（比如 256 个）
                        tokens_per_image = total_holes // total_images
                        
                        if tokens_per_image > 0:
                            # 3. 盘点历史文本中包含了多少个旧坑位
                            prefix_ids = full_input_ids_list[:prefix_len]
                            holes_in_prefix = prefix_ids.count(image_token_id)
                            
                            # 4. 换算出历史旧图的数量
                            images_in_prefix = round(holes_in_prefix / tokens_per_image)
                            
                            # 5. 精准正向切除！(只留下新的萝卜)
                            if images_in_prefix > 0:
                                if isinstance(pixel_values, list):
                                    pixel_values = pixel_values[images_in_prefix:]
                                elif hasattr(pixel_values, "shape"):
                                    if is_batched:
                                        pixel_values = pixel_values[:, images_in_prefix:]
                                    else:
                                        pixel_values = pixel_values[images_in_prefix:]
            
            # 新增音频缓存感知
            audio_token_id = getattr(model.config, "audio_token_id", None) or getattr(
                model.config, "audio_token_index", None
            )
            has_audio_in_new = audio_token_id is not None and audio_token_id in new_ids

            if not has_audio_in_new:
                kwargs.pop("input_features", None)
                kwargs.pop("input_features_mask", None)
            else:
                if "input_features" in kwargs and audio_token_id is not None:
                    input_features = kwargs["input_features"]
                    
                    # 1. 确定当前传入了多少个音频（特征张量的第 0 维，比如 2）
                    total_audios = input_features.shape[0] if hasattr(input_features, "shape") else 0
                    
                    if total_audios > 0:
                        # 2. 核心逻辑：统计历史缓存文本 (prefix) 中包含了几个"独立的音频块"
                        prefix_ids = full_input_ids_list[:prefix_len]
                        audios_in_prefix = 0
                        in_audio_block = False
                        
                        for token_id in prefix_ids:
                            if token_id == audio_token_id:
                                # 遇到 audio token 时，如果之前不在区块内，说明发现了一个新音频
                                if not in_audio_block:
                                    audios_in_prefix += 1
                                    in_audio_block = True
                            else:
                                # 遇到非 audio token（比如对话文本、<boa>、<eoa>），区块结束
                                in_audio_block = False
                        
                        # 3. 精准正向切除！(剔除被 KV Cache 记住的旧音频特征)
                        if audios_in_prefix > 0:
                            kwargs["input_features"] = input_features[audios_in_prefix:]
                            # 同步切除 mask
                            if "input_features_mask" in kwargs:
                                kwargs["input_features_mask"] = kwargs["input_features_mask"][audios_in_prefix:]

#####################################################################
# 补丁部分 结束
#####################################################################

            # Reuse the saved KV cache (trimmed to prefix length).
            # 缓存可能是混合类型（量化/滑窗/稠密/递归状态），逐类型回滚见模块级
            # `_trim_caches_to`（T8 根修）；能力门已在上面判过 None 才会走到这里。
            kv_cache = prompt_cache_state.cache
            _trim_caches_to(kv_cache, prefix_len)
            kwargs["prompt_cache"] = kv_cache

    if thinking_budget is not None:
        thinking_start_token_id = tokenizer.encode(
            thinking_start_token, add_special_tokens=False
        )[-1]
        enable_thinking = enable_thinking and (
            thinking_start_token_id in input_ids.flatten().tolist()
        )
        tokenizer.thinking_budget_criteria = ThinkingBudgetCriteria(
            tokenizer=tokenizer,
            thinking_budget=thinking_budget,
            thinking_end_token=thinking_end_token,
            thinking_start_token=thinking_start_token,
            enable_thinking=enable_thinking,
        )
        kwargs["thinking_budget_criteria"] = tokenizer.thinking_budget_criteria
    else:
        tokenizer.thinking_budget_criteria = None

    # Ensure we have a prompt_cache we can track for reuse
    if "prompt_cache" not in kwargs:
        kwargs["prompt_cache"] = cache.make_prompt_cache(
            model.language_model,
            max_kv_size=kwargs.get("max_kv_size", None),
        )
    tracked_cache = kwargs["prompt_cache"]

    total_prompt_tokens = reused_prefix_len + input_ids.size

    with wired_limit(model, [generation_stream]):
        detokenizer = processor.detokenizer
        detokenizer.reset()
        thinking_criteria = getattr(tokenizer, "thinking_budget_criteria", None)
        # [Phase 3] prompt-lookup 投机路径：外层协议（逐 token yield / 停止判定 /
        # detokenizer / thinking 预算）完全不变，仅替换 decode 生成器。
        if speculative_cfg.get("mode") == "prompt_lookup":
            gen = lookup_generate_step(
                input_ids, model, pixel_values, mask,
                context_token_ids=full_input_ids_list,
                spec_num_draft=int(speculative_cfg.get("num_draft_tokens", 8)),
                spec_ngram_max=int(speculative_cfg.get("ngram_max", 3)),
                spec_ngram_min=int(speculative_cfg.get("ngram_min", 2)),
                spec_stats=spec_stats,
                **kwargs,
            )
        elif use_mtp:
            # 上游投机路 return 在 pop_forced_token_id 之前（ar.py），thinking 预算的
            # 强制收束在草稿件模式下**不会执行**。prompt_lookup 路自己实现了它，
            # drafter 路没有——所以这里不能默默跑，否则"预算到点自动收束"这个承诺
            # 在这条路上是空的。生产 enable_thinking 缺省 False，故正常不触发。
            if thinking_criteria is not None and getattr(
                    thinking_criteria, "enable_thinking", False):
                raise RuntimeError(
                    "speculative.mode=drafter 与开思考态的 thinking_budget 不兼容："
                    "上游草稿件投机路不消费 forced_token_id，预算到点不会强制收束。"
                    "请关思考跑投机，或改用 mode=prompt_lookup（该路实现了强制收束）。"
                )
            # [MTP] 草稿件投机：解码循环整段交给上游 generate_step，我方只补两件——
            # ① 处理器上下文（上游投机路不接处理器，见 mtp_processors 模块头）；
            # ② 逐 token 登记已产出，供走查拼出真实历史。
            gen = generate_step(
                input_ids, model, pixel_values, mask,
                draft_model=draft_model,
                draft_kind=draft_kind,
                draft_block_size=speculative_cfg.get("draft_block_size") or None,
                **kwargs,
            )
            gen = _mtp_processor_bridge(
                gen, mtp_processors_list,
                # 惩罚窗口历史必须与上游同口径 = 实际喂进 _step 的 token
                # （= input_ids；分块预填后上游只剩末段，这里取全量是**更长**的
                #  窗口上界，再由 penalty_window 截断，不会少喂）
                prompt_tokens=input_ids.flatten().tolist(),
                penalty_window=mtp_penalty_window,
                draft_model=draft_model,
                spec_stats=spec_stats,
            )
        else:
            gen = generate_step(input_ids, model, pixel_values, mask, **kwargs)
        tic = time.perf_counter()

        generated_tokens = []
        for n, (token, logprobs) in enumerate(gen):
            if n == 0:
                prompt_time = time.perf_counter() - tic
                prompt_tps = total_prompt_tokens / prompt_time
                tic = time.perf_counter()

            generated_tokens.append(token)

            # Check thinking budget and force token if needed
            if thinking_criteria is not None:
                thinking_criteria(token)

            # Stop generation if the token is in the eos_token_ids
            if tokenizer.stopping_criteria(token):
                break

            detokenizer.add_token(token, skip_special_token_ids=skip_special_token_ids)

            # Yield the last segment if streaming
            yield GenerationResult(
                text=detokenizer.last_segment,
                token=token,
                logprobs=logprobs,
                prompt_tokens=total_prompt_tokens,
                generation_tokens=n + 1,
                total_tokens=total_prompt_tokens + n + 1,
                prompt_tps=prompt_tps,
                generation_tps=(n + 1) / (time.perf_counter() - tic),
                peak_memory=mx.get_peak_memory() / 1e9,
            )

        detokenizer.finalize()
        yield GenerationResult(
            text=detokenizer.last_segment,
            token=token,
            logprobs=logprobs,
            prompt_tokens=total_prompt_tokens,
            generation_tokens=n + 1,
            total_tokens=total_prompt_tokens + n + 1,
            prompt_tps=prompt_tps,
            generation_tps=(n + 1) / (time.perf_counter() - tic),
            peak_memory=mx.get_peak_memory() / 1e9,
        )

        # Save cache state for potential reuse on next turn
        if prompt_cache_state is not None:
            all_ids = full_input_ids_list + [
                t.item() if hasattr(t, "item") else t for t in generated_tokens
            ]
            prompt_cache_state.update(all_ids, tracked_cache)

        # Cleanup after generation
        mx.clear_cache()


