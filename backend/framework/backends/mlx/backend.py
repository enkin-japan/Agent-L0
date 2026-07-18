"""MLX 推理后端：InferenceBackend 协议的进程内实现（Apple Silicon）。

P0 平移物：原 core/engine.py 的 MLX 物理层（模型加载 / KV 量化参数 / 投机采样
透传 / detokenizer 级流式差分）、原 memory/session.py 的 clone_kv_cache、
原 core/constrained.py 的 mx 数组掩码实现，全部收编至此。补丁本体（patch/）
一行未动。上层（AgentEngine / ChatSession / 沙箱）只见协议，不见 MLX 的名字。

线程纪律（与拆分前一致）：
- 模型必须在推理执行器线程内加载（MLX 惰性张量携带创建线程的流，主线程加载的
  模型首次跨线程前向会报 "There is no Stream(gpu, N) in current thread"）——
  load() 收到 executor 时在其中加载；
- stream_generate 是同步迭代器，由 AgentEngine 在同一执行器线程内消费；
  推理互斥锁 / 回调跳线程留在 AgentEngine（后端无关资产）。
"""
import asyncio
import copy
from typing import Any, Dict, Iterator, List, Optional

import mlx.core as mx
from mlx_vlm.generate import PromptCacheState

from .patch.gemma4_generate import stream_generate as _patched_stream_generate
from ...core.backend_protocol import GenChunk

_NEG_INF = float("-inf")


def clone_kv_cache(cache: Optional[List[Any]]) -> Optional[List[Any]]:
    """
    深拷贝一份 MLX KV Cache（含每层的 keys/values 张量）。

    分支复用的关键：拷贝出的 Cache 与源会话在物理内存上完全独立，
    因此后续对分支 Cache 的裁剪/续写（in-place 写入）绝不会污染源会话。
    经实测 mx.eval + copy.deepcopy 会产生独立的底层缓冲区。
    失败时返回 None，调用方据此回退到冷启动预填充（正确性不受影响）。
    """
    if not cache:
        return None
    try:
        # 先物化惰性图，保证 deepcopy 拷贝的是已求值的独立缓冲区
        for layer in cache:
            for attr in ("keys", "values"):
                tensor = getattr(layer, attr, None)
                if tensor is not None:
                    mx.eval(tensor)
        return copy.deepcopy(cache)
    except Exception as e:
        print(f"[Memory] KV Cache 克隆失败，分支将回退到冷启动预填充: {e}")
        return None


class MLXCacheHandle:
    """CacheHandle 的 MLX 实现：包一层 PromptCacheState（补丁层的前缀复用状态）。"""
    __slots__ = ("state",)

    def __init__(self, state: Optional[PromptCacheState] = None):
        self.state = state if state is not None else PromptCacheState()

    @property
    def token_ids(self) -> Optional[List[int]]:
        return self.state.token_ids


class _GuardAdapter:
    """把纯逻辑 ToolCallGuard（core/constrained.py）适配成 MLX 侧的两套协议：

    - 上游 generate_step 的 logits_processors 协议：(tokens, logits) -> logits，
      增量消费历史、返回掩码后的 logits；
    - 投机走查（patch.lookup_generate_step）的直连协议：consume(token_id) /
      mask_logits(logits) / active——补丁按 hasattr(consume, mask_logits) 识别。

    数组运算（mask 应用）全在这里；状态机与 llguidance bitmask 在纯逻辑 guard 内。
    """

    def __init__(self, guard):
        self._g = guard
        self._seen: Optional[int] = None  # 已消费的历史长度；首调用置为 prompt 边界

    @property
    def active(self) -> bool:
        return self._g.active

    def __call__(self, tokens: mx.array, logits: mx.array) -> mx.array:
        if self._g.failed:
            return logits
        try:
            n = int(tokens.size)
            if self._seen is None:
                self._seen = n            # prompt 边界：既往历史不进状态机
                return logits
            if n > self._seen:
                for t in tokens[self._seen:n].tolist():
                    self._g.consume(int(t))
                self._seen = n
            return self.mask_logits(logits)
        except Exception as e:  # fail-open：绝不让约束器炸掉一轮生成
            self._g.fail_open(e)
            return logits

    # ---- 投机走查直连协议 ----
    def consume(self, token_id: int) -> None:
        self._g.consume(token_id)
        if self._seen is not None:
            self._seen += 1

    def mask_logits(self, logits: mx.array) -> mx.array:
        if self._g.failed:
            return logits
        try:
            allow = self._g.next_allowed()
        except Exception as e:
            self._g.fail_open(e)
            return logits
        if allow is None:
            return logits
        neg = mx.full((), _NEG_INF, dtype=logits.dtype)
        if isinstance(allow, int):
            # JSON 已完整：只放行收尾标记，强制立刻闭合
            return mx.where(mx.arange(logits.shape[-1]) == allow, logits, neg)
        mask = mx.array(allow[: logits.shape[-1]])
        return mx.where(mask, logits, neg)


class MLXBackend:
    """InferenceBackend 的 MLX 进程内实现。"""

    @classmethod
    async def load(cls, model_path, *, executor=None, **cfg) -> "MLXBackend":
        """在指定执行器线程内加载模型（推理宇宙同线程），装配后端。

        executor 缺省时在当前线程同步加载——仅限脚本/试验场景；生产装配一律
        经 AgentEngine.create 传入专用推理执行器。
        """
        from mlx_vlm.utils import load as _vlm_load
        if executor is None:
            model, processor = _vlm_load(model_path)
        else:
            loop = asyncio.get_running_loop()
            model, processor = await loop.run_in_executor(
                executor, lambda: _vlm_load(model_path))
        return cls(model, processor, cfg)

    def __init__(self, model, processor, cfg: Optional[Dict[str, Any]] = None):
        self.model = model
        self.processor = processor
        cfg = cfg or {}

        # KV Cache 量化配置（默认关闭）：kv_bits 为 None 时走稠密缓存，
        # 与未配置时行为完全一致；设为小数（如 3.5）则底层 generate_step 启用 TurboQuant。
        self.kv_bits = cfg.get("kv_bits", None)
        self.kv_quant_scheme = cfg.get("kv_quant_scheme", "turboquant")
        self.quantized_kv_start = int(cfg.get("quantized_kv_start", 0))

        # prompt-lookup 投机采样配置（mode=off 时不带 kwargs，路径与未启用时一致）
        spec_cfg = cfg.get("speculative") or {}
        self.speculative = dict(spec_cfg) if spec_cfg.get("mode", "off") != "off" else None
        if self.speculative:
            print(f"[Engine] 投机采样启用：{self.speculative}")

    # ---- 缓存生命周期 ----
    def new_cache(self) -> MLXCacheHandle:
        return MLXCacheHandle()

    def clone_cache(self, src) -> Optional[MLXCacheHandle]:
        state = getattr(src, "state", None)
        if state is None or state.cache is None:
            return None
        cloned = clone_kv_cache(state.cache)
        if cloned is None:
            return None
        h = MLXCacheHandle()
        h.state.cache = cloned
        h.state.token_ids = list(state.token_ids) if state.token_ids else None
        return h

    def release_cache(self, h) -> None:
        state = getattr(h, "state", None)
        if state is not None and state.cache:
            state.cache.clear()

    def shutdown(self) -> None:
        mx.clear_cache()  # 强制释放苹果芯片的显存/内存

    # ---- 生成 ----
    def stream_generate(self, prompt: str, *, cache,
                        images: Optional[List[str]] = None,
                        audios: Optional[List[str]] = None,
                        sampling: Optional[Dict[str, Any]] = None,
                        features: Optional[Dict[str, Any]] = None,
                        ) -> Iterator[GenChunk]:
        sampling = sampling or {}
        features = features or {}

        # KV 量化参数仅在启用时才透传给底层 generate_step；
        # 关闭（kv_bits=None）时不带这些 kwargs，稠密路径与未启用时完全一致
        kv_kwargs: Dict[str, Any] = {}
        if self.kv_bits is not None:
            kv_kwargs = {
                "kv_bits": self.kv_bits,
                "kv_quant_scheme": self.kv_quant_scheme,
                "quantized_kv_start": self.quantized_kv_start,
            }

        # 工具调用段约束采样：纯逻辑 guard 由上层构建，这里包上数组适配器挂载
        guard = features.get("tool_guard")
        if guard is not None:
            kv_kwargs["logits_processors"] = [_GuardAdapter(guard)]

        # 投机采样：透传配置 + 接受率统计容器（逐 chunk 经 extra 回吐）
        spec_stats: Optional[Dict[str, int]] = None
        if self.speculative:
            spec_stats = {"drafted": 0, "accepted": 0, "rounds": 0}
            kv_kwargs["speculative"] = self.speculative
            kv_kwargs["spec_stats"] = spec_stats

        # thinking 预算裁决：仅在上层给出预算时启用（摘要等裸生成不带）
        think_kwargs: Dict[str, Any] = {}
        thinking_budget = features.get("thinking_budget")
        if thinking_budget is not None:
            think_kwargs = {
                "thinking_budget": thinking_budget,
                # 预算锚点须是本模型的频道 token：不传则底层默认 <think>/</think>（Qwen
                # 方言，Gemma-4 词表中不存在），预算计数失准、超限还会强插垃圾字面量。
                "thinking_start_token": features.get("thinking_start_token"),
                "thinking_end_token": features.get("thinking_end_token"),
            }

        # 展示增量（GenChunk.delta）：盯 detokenizer 的当前解码全文（text + 未 flush
        # 缓冲）做差，而非 result.text（=last_segment）：SPM 分词器对无空格语言（中文）
        # 会把文本攒在 _unflushed 里、到 ▁ 词边界或 finalize 才 flush，导致 last_segment
        # 中途恒空、整段答复在结尾一次性吐出。盯 text+_unflushed 可让中文也逐 token 出。
        # 该 detokenizer 与底层 stream_generate 用的是同一实例（processor.detokenizer）。
        detok = None
        prev = ""
        if features.get("want_delta"):
            d = self.processor.detokenizer
            if hasattr(d, "text") and hasattr(d, "_unflushed"):
                detok = d
            # 非 SPM 类 detokenizer：delta=None，调用方退回 text 增量

        for result in _patched_stream_generate(
            model=self.model,
            processor=self.processor,
            prompt=prompt,
            image=images,
            audio=audios,
            temperature=sampling.get("temperature", 0.2),
            prompt_cache_state=cache.state,
            enable_thinking=features.get("enable_thinking", False),
            max_tokens=sampling.get("max_tokens", 4096),
            min_p=sampling.get("min_p", 0.05),
            repetition_penalty=sampling.get("repetition_penalty", 1.1),
            repetition_context_size=sampling.get("repetition_context_size", 50),
            **think_kwargs,
            **kv_kwargs,
        ):
            delta: Optional[str] = None
            if features.get("want_delta"):
                if detok is not None:
                    # ▁ 归一为空格，保证前缀单调延伸；trim_space 反常重排等情形
                    # 跳过一次增量、只推进基线，避免重复回吐
                    cur = (detok.text + detok._unflushed).replace("▁", " ")
                    delta = cur[len(prev):] if cur.startswith(prev) else ""
                    prev = cur
                else:
                    delta = result.text
            extra: Dict[str, Any] = {}
            if spec_stats and spec_stats.get("drafted"):
                extra = {"spec_drafted": spec_stats["drafted"],
                         "spec_accepted": spec_stats["accepted"]}
            # 前缀命中仪表（T4R）：0 也回吐——冷预填就该被看见
            rp = getattr(cache.state, "last_reused_prefix", None)
            if rp is not None:
                extra["reused_prefix"] = rp
            yield GenChunk(
                text=result.text,
                delta=delta,
                prompt_tokens=result.prompt_tokens,
                generation_tokens=result.generation_tokens,
                prompt_tps=result.prompt_tps,
                generation_tps=result.generation_tps,
                peak_memory=result.peak_memory,
                extra=extra,
            )
