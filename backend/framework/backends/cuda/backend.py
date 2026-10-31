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
  ⚠️ §7-8 起 gemma-4 的调用体是紧凑 DSL，structural-tag 表达不了（它只吃 JSON
  schema），本后端对该方言必然落到后备路；而 vLLM 0.26 又拒 per-request LP ——
  **净效果是 CUDA 侧 gemma-4 工具段无约束**，靠解析纠错闭环兜底。补法（xgrammar
  的 grammar 形态 / 服务端全局 LP）需 A100 现场验证，属 §7-7 待办，不可臆测形态。
- **thinking 预算裁决**：两段式（A1 方案，2026-08-09 落地）——enable_thinking 时
  方言 prompt 以思考开符收尾（生成从思考频道内起步），段一 stop=思考闭符 +
  max_tokens=预算封顶；停因 length（超限）即强插「\n<闭符>」段二续跑，APC 使段二
  prefill 近零成本。不用 vLLM 原生 thinking_token_budget：它需引擎级
  reasoning_config 且强制 V1 runner（2026-08-07 源码勘定，为上限兜底降级全局
  runner 不划算）。
- **诚实边界（均响亮不静默）**：repetition_context_size 无对应物（vLLM 惩罚窗=
  全上下文）；audio 多模态载荷按 vLLM multi_modal_data 形状透传但未实测（vision
  已由 model_eval 26B 20/20 实测走通）。

文本口径：SamplingParams(skip_special_tokens=False)——GenChunk.text 必须含频道/
工具标记（协议注释"全程累加即为完整原始文本"），可见性过滤是上游 stream_filter
的事。分词自理（add_special_tokens=False 防双 BOS——prompt 已经方言模板化）。
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
import queue as _queue
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

from ...core.backend_protocol import GenChunk
from .options import engine_options, public_options
from .metrics import speculative_counters, unknown_metrics

_NEG_INF = float("-inf")


class _StreamBridge:
    """有界同步队列 + 异步空间通知：背压不阻塞引擎的事件循环。"""

    def __init__(self, loop):
        self._loop = loop
        self._queue = _queue.Queue(maxsize=256)
        self._space = asyncio.Event()
        self._closed = threading.Event()
        self._ready = threading.Condition()

    @property
    def closed(self):
        return self._closed.is_set()

    async def put(self, item):
        while not self._closed.is_set():
            # 先清通知再查队列，消费线程在检查前后腾出空间都不会丢唤醒。
            self._space.clear()
            try:
                with self._ready:
                    self._queue.put_nowait(item)
                    self._ready.notify()
                return
            except _queue.Full:
                await self._space.wait()
        raise asyncio.CancelledError

    def get(self, timeout):
        deadline = time.monotonic() + timeout
        with self._ready:
            while True:
                try:
                    item = self._queue.get_nowait()
                    break
                except _queue.Empty:
                    if self.closed:
                        raise RuntimeError("CUDA 生成已停止（后端 shutdown）")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    self._ready.wait(remaining)
        self._loop.call_soon_threadsafe(self._space.set)
        return item

    def close(self):
        with self._ready:
            self._closed.set()
            self._ready.notify_all()
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._space.set)


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
    与方言 stc/etc 锚定的 ToolCallGuard 语义同构。

    **只吃 JSON schema 形态的语法**（§7-8）：structural tag 的 `schema` 字段就是
    JSON schema，表达不了 gemma-4 迁移后的紧凑 DSL（裸键、`<|"|>` 定界原文串）。
    拿 lark 语法硬塞会把模型约束成 JSON——那与提示词里的声明格式**正好相反**，
    比不约束更坏。故此处显式抛出，由调用方落到 per-request logits processor 后备路
    （那条路走的是 llguidance + 方言的正确 Lark 语法，语义完整）。"""
    import json
    kind = constraint.get("kind", "json_schema")
    if kind != "json_schema":
        raise ValueError(
            f"structural-tag 只能表达 json_schema 形态的调用体语法，本方言是 {kind!r}"
            "——退回 logits processor 后备路（llguidance 驱动，语法正确）")
    return json.dumps({
        "structures": [{
            "begin": constraint["begin"],
            "schema": constraint["body"],
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


def _trim_after_stop(token_ids: List[int], stop_ids: List[int]) -> List[int]:
    """把段一 token_ids 截到首个停符序列末尾，与 vLLM 已截断的 text 对齐。

    vLLM 的 stop 字符串只截 text、不截 token_ids：投机解码（MTP）一步接受多个
    token 时，停符后同步被接受的 token（实测形态：</think> 后紧跟的「\\n\\n<tool_call>」）
    会留在 token_ids 里却不在 text 里。段二若拿未截的 ids 续跑，模型「已写过」调用头
    而客户端从未收到——解析器只见尾巴（2026-10-24 Colab Qwen3.8+MTP3 事故）。
    取**首个**命中：vLLM 在 text 首次出现停符处即截断，首个 id 序列命中按构造就是
    那个停符。找不到停符序列则原样返回（不猜测）。恒返回新 list，不与入参别名。"""
    n = len(stop_ids)
    if n == 0:
        return list(token_ids)
    for i in range(len(token_ids) - n + 1):
        if token_ids[i:i + n] == stop_ids:
            return token_ids[:i + n]
    return list(token_ids)


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
        self._cfg = deepcopy(cfg.get("cuda") or {})
        self._runtime_options = None
        self._speculative_enabled = False
        self.processor = None            # server 方言层依赖（backend.processor）
        self._engine = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._per_request_lp_ok = True   # 首次被拒后降级并只告警一次
        self._structural_tag_ok = True   # 同上（紧凑 DSL 方言下恒被拒，见 structural_tag_json）
        self._stop_ids: Dict[str, Optional[List[int]]] = {}   # 思考闭符→ids（方言内恒定）
        self._overshoot_logged = False   # 段一越界截断：MTP 下每轮都会发生，只告警一次
        self._stop_mismatch_logged = False
        # 两段式思考预算：外部取消只知外层 request_id，段二真实 rid 经此表间接寻址
        self._rid_map: Dict[str, str] = {}
        # 仅私有循环访问；shutdown 先取消/收拢生产者，再停止循环。
        self._producers: Dict[str, tuple] = {}
        self._shutdown_started = False
        # 同步流提交与shutdown关闭入场必须线性化，不能把协程交给已停循环。
        self._lifecycle_lock = threading.Lock()

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

        options = engine_options(self._cfg, os.environ)
        # Keep the receipt separate: vLLM mutates speculative_config while creating
        # its engine config (e.g. adds the MTP model and parallel configuration).
        self._runtime_options = public_options(options)
        self._speculative_enabled = options["speculative_config"] is not None
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
            disable_log_stats=False,  # real speculative counters for /metrics
            **deepcopy(options),
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

    def runtime_options(self) -> Optional[Dict[str, Any]]:
        """Applied startup options only; execution evidence lives in metrics."""
        return deepcopy(self._runtime_options)

    async def speculative_metrics(self) -> Dict[str, Any]:
        """Read vLLM's real registry after flushing its stats on its owning loop.

        vLLM 0.24 AsyncLLM has no LLM.get_metrics facade. Its public metrics
        reader and do_log_stats are used instead. Missing support stays unknown.
        No generation/model operation is initiated by this diagnostics path.
        """
        engine, loop = self._engine, self._loop
        if engine is None or loop is None or not loop.is_running():
            return unknown_metrics("engine_unavailable")

        async def read():
            from vllm.v1.metrics.reader import get_metrics_snapshot
            await engine.do_log_stats()
            return speculative_counters(get_metrics_snapshot())

        try:
            future = asyncio.run_coroutine_threadsafe(read(), loop)
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=3)
        except Exception:
            # Never leak exception strings/model paths in the diagnostics response.
            return unknown_metrics()

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
        with self._lifecycle_lock:
            if self._shutdown_started:
                return
            eng, self._engine = self._engine, None
            if self._loop is None or not self._loop.is_running():
                return
            self._shutdown_started = True

            async def _shutdown():
                try:
                    producers = list(self._producers.values())
                    for task, bridge in producers:
                        bridge.close()
                        if not task.cancelling():
                            task.cancel()
                    await asyncio.gather(*(task for task, _ in producers),
                                         return_exceptions=True)
                    if eng is not None and hasattr(eng, "shutdown"):
                        eng.shutdown()
                finally:
                    self._loop.stop()

            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)

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

        q = _StreamBridge(self._loop)
        request_id = uuid.uuid4().hex
        with self._lifecycle_lock:
            if (self._shutdown_started or self._engine is None
                    or self._loop is None or not self._loop.is_running()):
                raise RuntimeError("CUDA 后端已 shutdown（不能受理生成）")
            fut = asyncio.run_coroutine_threadsafe(
                self._produce(q, request_id, prompt, cache, images, audios,
                              sampling, features, self._engine),
                self._loop)
        aborted = threading.Event()

        def _abort_hook():
            # 调用方（server 事件循环线程）跨线程取消：与下方 finally 同一顺序——先投递
            # 任务取消，再 close 桥（唤醒阻塞在 get 的推理线程；_produce 的 finally 见
            # q.closed 即走 engine.abort）。预填期（首包前）取消也由此生效，不必等
            # 600 s 的 get 超时。两步都线程安全且非阻塞（CancelScope 钩子契约）。
            aborted.set()
            if not fut.done():
                fut.cancel()
            q.close()

        scope = features.get("cancel_scope")
        unhook = scope.add_hook(_abort_hook) if hasattr(scope, "add_hook") else None
        try:
            while True:
                try:
                    item = q.get(timeout=600)
                except _queue.Empty:
                    raise RuntimeError("CUDA 生成 600s 无产出（引擎挂死？）")
                except RuntimeError:
                    if aborted.is_set():
                        raise RuntimeError("CUDA 生成已被调用方取消") from None
                    raise
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if unhook is not None:
                unhook()        # 正常结束后再 cancel 不得打到已完成的请求上
            if not fut.done() and not q.closed:
                # 先投递任务取消，再唤醒背压；反序会先进入abort，随后被cancel打断。
                fut.cancel()
                q.close()

    def _align_to_stop(self, gen_ids: List[int], end_tok: str) -> List[int]:
        """段一自然停后把 ids 截到思考闭符（见 _trim_after_stop），两种异常各告警一次。

        前提（注释即契约）：截断只修段二上下文与记账，不回拨 tool_guard——qwen35 约束
        在 server 启动探针即整段禁用（guard 恒 None），gemma4 在 vLLM 上 per-request LP
        被拒；若将来 guard 在 CUDA 生效且能见到越界 token，须同时回拨其状态。"""
        if end_tok not in self._stop_ids:
            try:
                self._stop_ids[end_tok] = list(
                    self._tokenizer.encode(end_tok, add_special_tokens=False))
            except Exception as e:
                self._stop_ids[end_tok] = None
                print(f"[CUDA] ⚠ 思考闭符 {end_tok!r} 分词失败（{e}）——段一 ids 不做对齐")
        stop_ids = self._stop_ids[end_tok]
        if not stop_ids:
            return gen_ids
        trimmed = _trim_after_stop(gen_ids, stop_ids)
        if len(trimmed) < len(gen_ids):
            if not self._overshoot_logged:
                self._overshoot_logged = True
                print(f"[CUDA] ⚠ 思考段停符后多出 {len(gen_ids) - len(trimmed)} 个 token"
                      f"（投机解码越界），已截断对齐 text——后续同类静默截断")
        elif gen_ids[-len(stop_ids):] != stop_ids and not self._stop_mismatch_logged:
            self._stop_mismatch_logged = True
            print(f"[CUDA] ⚠ text 含思考闭符但 ids 里找不到 {stop_ids}——分词口径不一致，"
                  f"段一越界 token 无法截断（工具调用头可能丢失）")
        return trimmed

    async def _abort(self, request_id: str, engine=None):
        try:
            # 两段式在飞时真实 rid 可能是 "<request_id>-p2"（映射表间接寻址）
            await (engine or self._engine).abort(
                self._rid_map.get(request_id, request_id))
        except Exception:
            pass

    async def _produce(self, q: _StreamBridge, request_id: str, prompt: str,
                       cache, images, audios, sampling, features, engine):
        """私有循环内的生产者：喂引擎、逐段折算 GenChunk 入队。
        满队列只挂起本任务，abort、引擎心跳及其他请求仍可运行。"""
        task = asyncio.current_task()
        self._producers[request_id] = (task, q)
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
            # 可选采样键按需透传（Qwen3.8 / Gemma 4 模型卡推荐 top_p / top_k / presence_penalty；
            # 调用方不给就不传，沿用 vLLM 缺省）。2026-10-31 P0 拐点测量加入：此前这些键被静默丢弃。
            for _k, _cast in (("top_p", float), ("top_k", int),
                              ("presence_penalty", float), ("frequency_penalty", float)):
                if sampling.get(_k) is not None:
                    sp_kwargs[_k] = _cast(sampling[_k])
            # vLLM 投机解码硬拒 min_p。依启动时实际解析的引擎配置判断：
            # env off/null 须保留普通采样，config-only MTP 也须移除 min_p。
            if self._speculative_enabled:
                sp_kwargs.pop("min_p")
            guard = features.get("tool_guard")
            constraint = features.get("tool_constraint")
            st_used = False

            def _build_sp(**overrides):
                """按相同的约束优先级构建 SamplingParams（两段式思考预算须逐段重建：
                max_tokens/stop 每段不同，约束语义两段一致）。"""
                nonlocal st_used
                kw = dict(sp_kwargs)
                kw.update(overrides)
                if constraint and self._structural_tag_ok:
                    # 首选：vLLM 原生 structural-tag（触发前自由、标记内按 schema）。
                    # 构建被拒（老版无此参数/形态变更）则响亮落到 logits processor 后备。
                    try:
                        from vllm.sampling_params import StructuredOutputsParams
                        sp_ = SamplingParams(**kw,
                                             structured_outputs=StructuredOutputsParams(
                                                 structural_tag=structural_tag_json(constraint)))
                        st_used = True
                        return sp_
                    except Exception as e:
                        # 一次性：对紧凑 DSL 方言这是**恒定**不可用（语法不是 JSON
                        # schema），每请求刷一行会淹掉日志（两段式思考预算下还是两行）。
                        self._structural_tag_ok = False
                        print(f"[CUDA] ⚠ structural-tag 约束构建被拒（{e}）——"
                              f"本进程后续改走 per-request logits processor 后备路")
                if guard is not None and self._per_request_lp_ok:
                    try:
                        return SamplingParams(**kw,
                                              logits_processors=[_TorchGuardAdapter(guard)])
                    except Exception as e:
                        self._per_request_lp_ok = False
                        print(f"[CUDA] ⚠ 本 vLLM 版本不支持 per-request logits_processors"
                              f"（{e}）——工具段约束降级为无约束+上游解析")
                return SamplingParams(**kw)

            mm: Dict[str, Any] = {}
            if images:
                from PIL import Image
                mm["image"] = [Image.open(p).convert("RGB") for p in images]
            if audios:
                import soundfile as sf
                mm["audio"] = [sf.read(p) for p in audios]

            t0 = time.time()
            t_first: Optional[float] = None
            n_prompt = len(ids)
            base_tokens = 0            # 前序段已计入的生成 token 数（两段式跨段累计）

            async def _run_phase(p_ids: List[int], sp_, rid: str):
                """跑一段生成、逐 chunk 入队；返回最后一个 completion（含全段
                token_ids/finish_reason）。计数口径对上层保持单段假象：
                generation_tokens = base_tokens + 本段。"""
                nonlocal t_first
                self._rid_map[request_id] = rid
                gi: Any = {"prompt_token_ids": p_ids}
                if mm:
                    gi["multi_modal_data"] = mm
                prev_text = ""
                comp = None
                async for out in engine.generate(gi, sp_, rid):
                    comp = out.outputs[0]
                    if t_first is None and comp.token_ids:
                        t_first = time.time()
                    seg = comp.text[len(prev_text):]
                    prev_text = comp.text
                    if not seg and not comp.finish_reason:
                        continue
                    n_gen = base_tokens + len(comp.token_ids)
                    now = time.time()
                    prompt_tps = n_prompt / max(t_first - t0, 1e-6) if t_first else 0.0
                    gen_tps = (n_gen / max(now - t_first, 1e-6)) if t_first else 0.0
                    extra: Dict[str, Any] = {}
                    cached = getattr(out, "num_cached_tokens", None)
                    if cached is not None:
                        extra["reused_prefix"] = int(cached)   # T4R 仪表：0 也回吐
                    await q.put(GenChunk(
                        text=seg,
                        delta=seg if features.get("want_delta") else None,
                        prompt_tokens=n_prompt,
                        generation_tokens=n_gen,
                        prompt_tps=round(prompt_tps, 1),
                        generation_tps=round(gen_tps, 1),
                        peak_memory=self._peak_gb(),
                        extra=extra,
                    ))
                return comp

            # ---- thinking 预算裁决（A1 两段式）----
            # enable_thinking=True 时方言 prompt 以「<soc>thought\n」收尾=生成从思考
            # 频道内起步：段一 stop=思考闭符 + max_tokens=预算封顶；停因=stop 即自然
            # 收束，停因=length 即超限强插「\n<闭符>」（与 MLX ThinkingBudgetCriteria
            # 同语义）；段二从段一末续跑（APC 使段二 prefill 近零成本）。
            # enable_thinking=False 时频道已被 builder 预开即闭，预算无的放矢，单段照旧。
            # 边界（注释即契约）：只裁决起步思考段——正文中模型自发重开思考不再设限
            # （gemma4 方言不出现该形态；MLX 侧 criteria 同样只按首段计数）。
            end_tok = features.get("thinking_end_token")
            think_budget = features.get("thinking_budget")
            two_phase = (think_budget is not None and end_tok
                         and features.get("enable_thinking"))
            all_gen_ids: List[int] = []
            if two_phase:
                budget = max(1, min(int(think_budget), sp_kwargs["max_tokens"]))
                sp1 = _build_sp(max_tokens=budget, stop=[end_tok],
                                include_stop_str_in_output=True)
                comp1 = await _run_phase(ids, sp1, request_id)
                if comp1 is not None:
                    all_gen_ids = list(comp1.token_ids)
                    if end_tok in comp1.text:
                        # 自然停：ids 对齐到 text（截掉投机解码越过停符的 token）。
                        # generation_tokens 口径＝保留的 token（与 cache.token_ids 一致），
                        # 段一已流出的 chunk 计数可能略高，段二起回落到对齐值。
                        all_gen_ids = self._align_to_stop(all_gen_ids, end_tok)
                        base_tokens = len(all_gen_ids)
                    else:
                        # 预算耗尽：强闭思考频道，闭符进规范文本（parser 见到成对标记）
                        forced = "\n" + end_tok
                        forced_ids = self._tokenizer.encode(
                            forced, add_special_tokens=False)
                        all_gen_ids += forced_ids
                        base_tokens = len(all_gen_ids)
                        await q.put(GenChunk(
                            text=forced,
                            delta=forced if features.get("want_delta") else None,
                            prompt_tokens=n_prompt,
                            generation_tokens=base_tokens,
                            prompt_tps=0.0, generation_tps=0.0,
                            peak_memory=self._peak_gb(),
                            extra={"thinking_budget_forced_close": True},
                        ))
                    remaining = int(sp_kwargs["max_tokens"]) - base_tokens
                    if remaining > 0:
                        sp2 = _build_sp(max_tokens=remaining)
                        comp2 = await _run_phase(ids + all_gen_ids, sp2,
                                                 f"{request_id}-p2")
                        if comp2 is not None:
                            all_gen_ids += list(comp2.token_ids)
            else:
                sp = _build_sp()
                comp = await _run_phase(ids, sp, request_id)
                if comp is not None:
                    all_gen_ids = list(comp.token_ids)

            # token_ids 记账：prompt + 已生成（APC 复用与上层裁剪判断的依据）
            if isinstance(cache, CudaCacheHandle) and all_gen_ids:
                cache.token_ids = list(ids) + all_gen_ids
            # 仪表回填：structural-tag 由引擎掩码，guard 收不到逐 token 事件——
            # 尾声按 stc/etc 数出约束段 token 数写回 masked_positions（server done
            # 事件的 constrained_steps 口径；同进程对象直改）。
            if st_used and guard is not None and all_gen_ids:
                guard.masked_positions = _count_constrained_tokens(
                    all_gen_ids, self._tokenizer, constraint)
            await q.put(None)
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            await q.put(e)
        finally:
            try:
                if q.closed or task.cancelling():
                    # close can mark the bridge while the producer is running,
                    # before its queued Task.cancel callback executes. Cleanup
                    # must survive that later cancellation and stay owned until
                    # the engine has finished aborting, including during shutdown.
                    cleanup = asyncio.create_task(self._abort(request_id, engine))
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            continue
                    cleanup.result()
            finally:
                self._rid_map.pop(request_id, None)
                self._producers.pop(request_id, None)

    @staticmethod
    def _peak_gb() -> float:
        try:
            import torch
            return round(torch.cuda.max_memory_allocated() / (1 << 30), 2)
        except Exception:
            return 0.0
