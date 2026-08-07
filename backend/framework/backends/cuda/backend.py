"""vLLM CUDA 后端：InferenceBackend 协议的进程内实现（A100/H100 档，Colab 联调）。

语义对齐 mlx/backend.py 逐条（协议见 core/backend_protocol.py）：
- **缓存**：vLLM 自动前缀缓存（enable_prefix_caching）在引擎内做物理复用，
  故 CacheHandle 退化为记账（token_ids 簿记 + 身份）；clone = 记账拷贝（物理
  共享由 APC 保证，天然不互污）；release = 记账清除。persist/restore 不支持
  （返回 False/None，server 冷路兜底——设计内语义）。
- **stream_generate 同步迭代器**：后端自持一条私有事件循环线程跑 AsyncLLMEngine，
  经有界队列桥接成同步生成器（协议要求在推理执行器线程内同步消费）；消费方
  弃置迭代器时 finally 向引擎 abort（段粒度取消对齐）。
- **工具段约束**：首选 features.tool_constraint（server 附的声明式规格）→ vLLM
  原生 structural-tag（触发标记前自由、标记内按 schema、收尾恢复自由——与方言
  stc/etc 锚定语义同构；2026-08-07 A100 实测 per-request logits_processors 已被
  vLLM 0.26 拒收，_TorchGuardAdapter 降级路留作旧版后备）。仪表：生成尾声按
  stc/etc token 数出约束段 token 数回填 guard.masked_positions（同进程对象）。
- **诚实边界（均响亮不静默）**：thinking 预算裁决未接——vLLM 0.26 原生位
  SamplingParams.thinking_token_budget 需引擎级 reasoning_config（思考起止 token
  序列）且强制 VLLM_USE_V2_MODEL_RUNNER=0（2026-08-07 源码勘定）；为上限兜底功能
  降级全局 runner 不划算，等 vLLM V2 支持后按此路径接线（max_tokens 仍封顶）。
  repetition_context_size 无对应物（vLLM 惩罚窗=全上下文）；vision/audio 多模态
  载荷按 vLLM multi_modal_data 形状透传但未实测；投机采样未接。

文本口径：SamplingParams(skip_special_tokens=False)——GenChunk.text 必须含频道/
工具标记（协议注释"全程累加即为完整原始文本"），可见性过滤是上游 stream_filter
的事。分词自理（add_special_tokens=False 防双 BOS——prompt 已经方言模板化）。
"""
from __future__ import annotations

import asyncio
import json
import os
import queue as _queue
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

from ...core.backend_protocol import GenChunk

_NEG_INF = float("-inf")


class CudaCacheHandle:
    """CacheHandle 的 vLLM 实现：纯记账（物理复用在引擎 APC 内）。"""
    __slots__ = ("token_ids",)

    def __init__(self, token_ids: Optional[List[int]] = None):
        self.token_ids = token_ids


class _TorchGuardAdapter:
    """ToolCallGuard → vLLM per-request logits processor（二参约定：
    (已生成 token 序列, logits) -> logits）。

    与 MLX 适配器的差异：vLLM 二参形态只喂已生成序列（不含 prompt），
    故无 prompt 边界簿记——seen 从 0 起步。掩码延迟导入 torch：
    allow=None（无约束步）路径完全不触碰 torch，协议形状可在无 CUDA 机金测试。"""

    def __init__(self, guard):
        self._g = guard
        self._seen = 0

    @property
    def active(self) -> bool:
        return self._g.active

    def __call__(self, past_token_ids, logits):
        if self._g.failed:
            return logits
        try:
            n = len(past_token_ids)
            if n > self._seen:
                for t in past_token_ids[self._seen:n]:
                    self._g.consume(int(t))
                self._seen = n
            allow = self._g.next_allowed()
            if allow is None:
                return logits
            return self._mask(logits, allow)
        except Exception as e:  # fail-open：绝不让约束器炸掉一轮生成（MLX 同款）
            self._g.fail_open(e)
            return logits

    @staticmethod
    def _mask(logits, allow):
        import torch
        vocab = logits.shape[-1]
        if isinstance(allow, int):
            keep = torch.zeros(vocab, dtype=torch.bool, device=logits.device)
            keep[allow] = True          # JSON 已完整：只放行收尾标记，强制闭合
        else:
            keep = torch.as_tensor(list(allow)[:vocab], dtype=torch.bool,
                                   device=logits.device)
            if keep.shape[-1] < vocab:
                pad = torch.zeros(vocab - keep.shape[-1], dtype=torch.bool,
                                  device=logits.device)
                keep = torch.cat([keep, pad])
        return logits.masked_fill(~keep, _NEG_INF)


def structural_tag_json(constraint: Dict[str, Any]) -> str:
    """声明式工具约束（server 附的 features.tool_constraint）→ vLLM structural_tag
    规格 JSON。纯函数：无 vLLM/torch 依赖，金测试本地可锚形状。

    形态对齐 vLLM 0.26 xgrammar 后端的 legacy structural tag（begin/schema/end +
    triggers）——触发标记出现前自由生成，标记内按 JSON schema 掩码，收尾恢复自由，
    与方言 stc/etc 锚定的 ToolCallGuard 语义同构。"""
    import json
    return json.dumps({
        "structures": [{
            "begin": constraint["begin"],
            "schema": constraint["schema"],
            "end": constraint["end"],
        }],
        "triggers": [constraint["begin"]],
    }, ensure_ascii=False)


def _count_constrained_tokens(token_ids: List[int], tokenizer,
                              constraint: Dict[str, Any]) -> int:
    """仪表：数出 stc..etc 约束段内的 token 数（不含标记本身）。

    structural-tag 由引擎掩码，guard 收不到逐 token 事件，事后按标记 token 清点
    ——口径与 masked_positions（被约束的采样步数）对齐：段内每 token 都是被
    掩码采样出的一步；收尾 etc 由引擎强制，也算约束步（+1 在 etc 分支）。
    标记若非单 token（防御：server 侧已拒），返回 0 而非猜测。"""
    try:
        beg = tokenizer.encode(constraint["begin"], add_special_tokens=False)
        end = tokenizer.encode(constraint["end"], add_special_tokens=False)
        if len(beg) != 1 or len(end) != 1:
            return 0
        n = 0
        active = False
        for t in token_ids:
            if not active and t == beg[0]:
                active = True
                continue
            if active and t == end[0]:
                active = False
                n += 1                      # 强制闭合的 etc 本身是一次约束步
                continue
            if active:
                n += 1
        return n
    except Exception:
        return 0


class CudaVLLMBackend:
    """InferenceBackend 的 vLLM 进程内实现。"""

    @classmethod
    async def load(cls, model_path, *, executor=None, **cfg) -> "CudaVLLMBackend":
        """装配引擎与 processor。executor 参数收下不用：vLLM 自管理其工作线程/进程，
        本后端把引擎钉在自己的私有事件循环线程上（生成桥接见 stream_generate）。"""
        self = cls(cfg)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._boot, str(model_path))
        return self

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self.model_key = ""
        self._cfg = dict(cfg.get("cuda") or {})
        self.processor = None            # server 方言层依赖（backend.processor）
        self._engine = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._per_request_lp_ok = True   # 首次被拒后降级并只告警一次

    # ---- 装配（阻塞，跑在 load 的线程池里） ----

    def _boot(self, model_path: str):
        from transformers import AutoProcessor, AutoTokenizer
        from vllm import AsyncEngineArgs
        from vllm.engine.async_llm_engine import AsyncLLMEngine

        self.model_key = model_path
        trust = bool(self._cfg.get("trust_remote_code", False))
        try:
            self.processor = AutoProcessor.from_pretrained(model_path,
                                                           trust_remote_code=trust)
        except Exception:
            self.processor = AutoTokenizer.from_pretrained(model_path,
                                                           trust_remote_code=trust)
        self._tokenizer = getattr(self.processor, "tokenizer", self.processor)

        args = AsyncEngineArgs(
            model=model_path,
            # INFER_GPU_FRAC：多实例分层常驻的每实例显存占比覆盖（env 非空即赢，
            # 与 INFER_PORT/INFER_MODEL_REPO 同惯例）——单卡跑两档模型时各分一块，
            # config 单值只描述独占形态
            gpu_memory_utilization=float(
                os.environ.get("INFER_GPU_FRAC", "").strip()
                or self._cfg.get("gpu_memory_utilization", 0.92)),
            # INFER_QUANT：量化档 env 覆盖（FP4 能力偏移评测用，2026-08-08 用户新增）
            # ——A100 无原生 FP4，评测经 bitsandbytes NF4 代理；"none" 显式关量化
            quantization=(lambda q: None if q in ("", "none") else q)(
                os.environ.get("INFER_QUANT", "").strip()
                or str(self._cfg.get("quantization") or "")),
            max_model_len=int(self._cfg.get("max_model_len", 32768)),
            tensor_parallel_size=int(self._cfg.get("tensor_parallel", 1)),
            enable_prefix_caching=bool(self._cfg.get("enable_prefix_caching", True)),
            enforce_eager=bool(self._cfg.get("enforce_eager", False)),
            trust_remote_code=trust,
            # INFER_SPEC_CONFIG：投机解码 env 覆盖（MTP/assistant 草稿评测用，
            # 2026-08-08）——整段 JSON 直传 vLLM，如
            # '{"model":"google/gemma-4-26B-A4B-it-assistant","num_speculative_tokens":2}'
            speculative_config=(lambda s: json.loads(s) if s else None)(
                os.environ.get("INFER_SPEC_CONFIG", "").strip()),
            # INFER_KV_DTYPE：KV 缓存精度 env 覆盖（FP8 KV 漂移评测用）；空=auto
            kv_cache_dtype=(os.environ.get("INFER_KV_DTYPE", "").strip() or "auto"),
        )
        # 引擎与其所有 await 都发生在私有循环线程（同步协议桥接的宇宙边界）
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        name="vllm-loop", daemon=True)
        self._thread.start()

        async def _make():
            return AsyncLLMEngine.from_engine_args(args)

        self._engine = asyncio.run_coroutine_threadsafe(_make(), self._loop).result()
        print(f"[CUDA] vLLM 引擎就绪: {model_path} "
              f"(quant={args.quantization}, max_len={args.max_model_len})")

    # ---- 缓存生命周期（记账语义，物理复用=引擎 APC） ----

    def new_cache(self) -> CudaCacheHandle:
        return CudaCacheHandle()

    def clone_cache(self, src) -> Optional[CudaCacheHandle]:
        ids = getattr(src, "token_ids", None)
        return CudaCacheHandle(list(ids) if ids else None)

    def release_cache(self, h) -> None:
        if isinstance(h, CudaCacheHandle):
            h.token_ids = None

    def persist_cache(self, h, dest: str) -> bool:
        return False                     # 不支持：server 冷路兜底（设计内）

    def restore_cache(self, src: str) -> Optional[CudaCacheHandle]:
        return None

    def shutdown(self) -> None:
        eng, self._engine = self._engine, None
        if eng is not None and self._loop is not None:
            try:
                if hasattr(eng, "shutdown"):
                    self._loop.call_soon_threadsafe(eng.shutdown)
            except Exception:
                pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ---- 生成：私有循环 → 有界队列 → 同步迭代器 ----

    def stream_generate(self, prompt: str, *, cache,
                        images: Optional[List[str]] = None,
                        audios: Optional[List[str]] = None,
                        sampling: Optional[Dict[str, Any]] = None,
                        features: Optional[Dict[str, Any]] = None,
                        ) -> Iterator[GenChunk]:
        if self._engine is None:
            raise RuntimeError("CUDA 后端未装配（load 未完成或已 shutdown）")
        sampling = sampling or {}
        features = features or {}
        if features.get("thinking_budget") is not None:
            print("[CUDA] ⚠ thinking 预算裁决未实现（Colab 联调清单）——本轮不限预算")

        q: _queue.Queue = _queue.Queue(maxsize=256)
        request_id = uuid.uuid4().hex
        fut = asyncio.run_coroutine_threadsafe(
            self._produce(q, request_id, prompt, cache, images, audios,
                          sampling, features),
            self._loop)
        _SENTINEL = object()
        try:
            while True:
                try:
                    item = q.get(timeout=600)
                except _queue.Empty:
                    raise RuntimeError("CUDA 生成 600s 无产出（引擎挂死？）")
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if not fut.done():
                asyncio.run_coroutine_threadsafe(
                    self._abort(request_id), self._loop)

    async def _abort(self, request_id: str):
        try:
            await self._engine.abort(request_id)
        except Exception:
            pass

    async def _produce(self, q: _queue.Queue, request_id: str, prompt: str,
                       cache, images, audios, sampling, features):
        """私有循环内的生产者：喂引擎、逐段折算 GenChunk 入队。队列有界=背压；
        put 在私有循环线程里阻塞是可接受的（该循环只服务生成）。"""
        try:
            from vllm import SamplingParams

            ids = self._tokenizer.encode(prompt, add_special_tokens=False)
            sp_kwargs: Dict[str, Any] = dict(
                temperature=float(sampling.get("temperature", 0.2)),
                min_p=float(sampling.get("min_p", 0.05)),
                max_tokens=int(sampling.get("max_tokens", 4096)),
                repetition_penalty=float(sampling.get("repetition_penalty", 1.1)),
                skip_special_tokens=False,          # 规范文本须含频道/工具标记
                spaces_between_special_tokens=False,
            )
            # vLLM 0.26 投机解码硬拒 min_p/logit_bias（请求期 ValueError）——
            # 投机评测档（INFER_SPEC_CONFIG 在位）弃用 min_p，采样漂移由评分兜底
            if os.environ.get("INFER_SPEC_CONFIG", "").strip():
                sp_kwargs.pop("min_p")
            guard = features.get("tool_guard")
            constraint = features.get("tool_constraint")
            sp = None
            st_used = False
            if constraint:
                # 首选：vLLM 原生 structural-tag（触发前自由、标记内按 schema）。
                # 构建被拒（老版无此参数/形态变更）则响亮落到 logits processor 后备。
                try:
                    from vllm.sampling_params import StructuredOutputsParams
                    sp = SamplingParams(**sp_kwargs,
                                        structured_outputs=StructuredOutputsParams(
                                            structural_tag=structural_tag_json(constraint)))
                    st_used = True
                except Exception as e:
                    print(f"[CUDA] ⚠ structural-tag 约束构建被拒（{e}）——"
                          f"尝试 per-request logits processor 后备路")
            if sp is None and guard is not None and self._per_request_lp_ok:
                try:
                    sp = SamplingParams(**sp_kwargs,
                                        logits_processors=[_TorchGuardAdapter(guard)])
                except Exception as e:
                    self._per_request_lp_ok = False
                    print(f"[CUDA] ⚠ 本 vLLM 版本不支持 per-request logits_processors"
                          f"（{e}）——工具段约束降级为无约束+上游解析")
            if sp is None:
                sp = SamplingParams(**sp_kwargs)

            gen_input: Any = {"prompt_token_ids": ids}
            mm: Dict[str, Any] = {}
            if images:
                from PIL import Image
                mm["image"] = [Image.open(p).convert("RGB") for p in images]
            if audios:
                import soundfile as sf
                mm["audio"] = [sf.read(p) for p in audios]
            if mm:
                gen_input["multi_modal_data"] = mm

            t0 = time.time()
            t_first: Optional[float] = None
            prev_text = ""
            n_prompt = len(ids)
            last_out = None
            async for out in self._engine.generate(gen_input, sp, request_id):
                last_out = out
                comp = out.outputs[0]
                if t_first is None and comp.token_ids:
                    t_first = time.time()
                seg = comp.text[len(prev_text):]
                prev_text = comp.text
                if not seg and not comp.finish_reason:
                    continue
                n_gen = len(comp.token_ids)
                now = time.time()
                prompt_tps = n_prompt / max(t_first - t0, 1e-6) if t_first else 0.0
                gen_tps = (n_gen / max(now - t_first, 1e-6)) if t_first else 0.0
                extra: Dict[str, Any] = {}
                cached = getattr(out, "num_cached_tokens", None)
                if cached is not None:
                    extra["reused_prefix"] = int(cached)   # T4R 仪表：0 也回吐
                q.put(GenChunk(
                    text=seg,
                    delta=seg if features.get("want_delta") else None,
                    prompt_tokens=n_prompt,
                    generation_tokens=n_gen,
                    prompt_tps=round(prompt_tps, 1),
                    generation_tps=round(gen_tps, 1),
                    peak_memory=self._peak_gb(),
                    extra=extra,
                ))
            # token_ids 记账：prompt + 已生成（APC 复用与上层裁剪判断的依据）
            if isinstance(cache, CudaCacheHandle) and last_out is not None:
                cache.token_ids = list(ids) + list(last_out.outputs[0].token_ids)
            # 仪表回填：structural-tag 由引擎掩码，guard 收不到逐 token 事件——
            # 尾声按 stc/etc 数出约束段 token 数写回 masked_positions（server done
            # 事件的 constrained_steps 口径；同进程对象直改）。
            if st_used and guard is not None and last_out is not None:
                guard.masked_positions = _count_constrained_tokens(
                    list(last_out.outputs[0].token_ids), self._tokenizer, constraint)
            q.put(None)
        except BaseException as e:
            q.put(e)

    @staticmethod
    def _peak_gb() -> float:
        try:
            import torch
            return round(torch.cuda.max_memory_allocated() / (1 << 30), 2)
        except Exception:
            return 0.0
