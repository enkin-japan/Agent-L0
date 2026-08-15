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
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import mlx.core as mx
from mlx_vlm.generate import PromptCacheState

#: KV 落盘格式版本（T2）：字段/布局变更时递增，旧文件直接判不可恢复（冷路兜底），
#: 绝不做跨版本迁移——缓存是纯派生数据，重预填就能重建
CACHE_PERSIST_VERSION = "1"

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


def _load_drafter(target_model, draft_path: str, spec_cfg: Dict[str, Any]):
    """加载并校验投机草稿件，返回 `(draft_model, resolved_kind)`。

    **失败即抛，不降级**（§S.3）：用户显式配了草稿件却装不上/不兼容，那是配置错误。
    静默退回非投机会把它伪装成"怎么没变快"的性能玄学，而这类玄学本项目已经吃过
    两次亏（§S.3 / §S.15）。宁可起不来。
    """
    from mlx_vlm.speculative.drafters import (
        load_drafter,
        resolve_drafter_kind,
        validate_drafter_compatibility,
    )

    kind = (spec_cfg.get("draft_kind") or "").strip() or None
    # 处理器接线目前只覆盖 mtp 轮循环：mtp_processors.install() 只替换
    # `_mtp_acceptance_walk` 与 `_mtp_rounds_batch` 两类分派点，而 dflash/eagle3
    # 的轮循环**不消费 logits_processors**（上游 dflash.py/eagle3.py 对 processors
    # 零引用，08-16 复审实查）。放行即静默丢掉 repetition_penalty 与工具约束
    # guard——且下方的融合采样击倒也只对 mtp 路成立。fail-closed（§S.3）。
    # 注意可达性陷阱：model_type 不在上游 DRAFTER_KIND_BY_MODEL_TYPE 表里的
    # 草稿件（含 config.json 缺失/不可读，_peek 返回 None）会静默落到
    # DEFAULT_DRAFTER_KIND="dflash"，正好命中这里。
    # 判 kind 只需 peek config.json，故放在 load_drafter **之前**——拒启不必
    # 先白付几 GB 权重装载（评审建议）。
    resolved_kind = resolve_drafter_kind(draft_path, kind)
    if resolved_kind != "mtp":
        raise RuntimeError(
            f"草稿件 {draft_path} 解析为 kind={resolved_kind!r}，该轮循环不经过"
            " logits 处理器（repetition_penalty / 工具约束 guard 会静默失效）。"
            "本栈处理器接线目前仅覆盖 kind='mtp'；其它 kind 需单独接线，不得降级。"
            "若该件本应是 mtp：检查其 config.json 是否缺失/损坏（peek 失败会静默"
            "落到缺省 dflash）。"
        )
    draft_model, resolved_kind_post = load_drafter(draft_path, kind=kind)
    if resolved_kind_post != resolved_kind:  # 预判与装载判定分裂 = 上游语义漂移
        raise RuntimeError(
            f"resolve_drafter_kind={resolved_kind!r} 与 load_drafter"
            f"={resolved_kind_post!r} 不一致——上游判定语义漂移，需重审接线。"
        )
    # 用 resolved_kind 而非配置里那个：上游明说要按自动判定的结果分派
    validate_drafter_compatibility(target_model, draft_model, resolved_kind)

    # 处理器可补性前置校验：走查要逐位物化 logits 才能应用 repetition_penalty 与
    # 工具约束 guard。目标件若走"融合 target"快路（不经 logits）或缺少逐位投影，
    # 处理器就补不上去——那种形态必须在**装载期**否掉，而不是等生成到一半从流里
    # 抛错（那时 token 已经吐给客户端了）。
    lm = getattr(target_model, "language_model", target_model)
    if not hasattr(lm, "speculative_logits_from_hidden"):
        raise RuntimeError(
            f"{type(lm).__name__} 未实现 speculative_logits_from_hidden，"
            "无法在投机走查里逐位应用 logits 处理器（repetition_penalty / 工具"
            "约束 guard）。该模型形态需单独接线，不得带着失效的处理器跑。"
        )
    # 融合目标采样（qwen3_5 类形态自带）绕过 logits、因而绕过全部处理器。
    # 处置从「拒启」改为「显式击倒」（08-15 夜接线）：实例属性置 None 遮蔽类方法，
    # 上游两处分派均以 callable(getattr(...)) 判定（mtp.py:116/:145-146），置 None 即
    # 干净落回 speculative_logits_from_hidden 的 hidden 路——与 gemma 同形态，
    # 处理器批量走查照常。§S.3 反静默降级三重保留：①这里响亮打印；②击倒后
    # 立即复核，遮不住就照旧拒启；③走查侧仍有 target_tokens 兜底断言
    # （mtp_processors.py），万一上游改了分派判据会当场响。
    # 覆盖的漂移形态：上游若把 callable(getattr(...)) 判据改成 hasattr(...)，
    # 击倒后 hasattr 仍真、分派会去调 None() 而在 verify 链内抛
    # TypeError（mtp.py:120 `_mtp_verify_without_logits`/:149）——先于走查执行，所以**先响的是这里**，
    # 不是走查侧 target_tokens 断言（那两条断言在 acceptance_walk 体内，永远
    # 排在 verify 之后；非贪心采样时 :145 段整体跳过、断言根本不触发）。
    # 两种形态都不会静默降级，只是排障入口在 verify 不在守卫（08-16 复审修注）。
    for fused in ("speculative_argmax_from_hidden", "speculative_verify_logits"):
        if callable(getattr(lm, fused, None)):
            try:
                setattr(lm, fused, None)
            except AttributeError as exc:  # property/__slots__ 类只读形态
                raise RuntimeError(
                    f"{type(lm).__name__} 的融合目标采样 {fused}() 无法用实例属性"
                    f"遮蔽（{exc}）。融合路绕过全部处理器，不得降级——"
                    "该形态需单独接线。"
                ) from exc
            if callable(getattr(lm, fused, None)):
                raise RuntimeError(
                    f"{type(lm).__name__} 的融合目标采样 {fused}() 置 None 后仍可调"
                    "（__getattr__ 语义特殊？）。融合路绕过全部处理器，"
                    "不得降级——该形态需单独接线。"
                )
            print(f"[Engine] 已禁用 {type(lm).__name__}.{fused}（融合采样绕过 "
                  "logits 处理器；改走 hidden+批量投影走查路，与 gemma 同形态）")

    # 草稿深度天花板：官方三个 assistant 件的 config.json **都没有 block_size 键**
    # （实读 2026-08-14），落到上游默认 4 → 每轮草稿数恒为 3。而
    # `_effective_mtp_block_size` 把 configured 当**硬顶**，要突破得满足"最近 32 轮里
    # ≥65% 的轮次把 configured 深度全接受"——实测接受率 ~54% 时全中 3 个约 16%，
    # **永远够不到**。净效果：模型卡推荐的 draft_block_size=6 在所有已发布件上静默失效，
    # k 扫描会在 2/4/6 上量出同一个 γ=3。
    # 上游留了 `prefer_requested_block_size` 旁路：置真则直接采纳请求值。
    # 只在**显式配了块大小**时置真——不配就沿用上游自适应，行为不变。
    if spec_cfg.get("draft_block_size"):
        draft_model.prefer_requested_block_size = True
        print(f"[Engine] 草稿深度上限已解锁：采纳请求值 "
              f"draft_block_size={spec_cfg['draft_block_size']}"
              f"（否则被草稿件 config 缺省值 4 顶死）")

    print(f"[Engine] 投机草稿件已装载：{draft_path}（kind={resolved_kind}）")
    return draft_model, resolved_kind


class MLXBackend:
    """InferenceBackend 的 MLX 进程内实现。"""

    @classmethod
    async def load(cls, model_path, *, executor=None, **cfg) -> "MLXBackend":
        """在指定执行器线程内加载模型（推理宇宙同线程），装配后端。

        executor 缺省时在当前线程同步加载——仅限脚本/试验场景；生产装配一律
        经 AgentEngine.create 传入专用推理执行器。
        """
        from mlx_vlm.utils import load as _vlm_load

        spec_cfg = cfg.get("speculative") or {}
        draft_path = ""
        if spec_cfg.get("mode") == "drafter":
            draft_path = (spec_cfg.get("draft_model") or "").strip()
            if not draft_path:
                raise ValueError(
                    "speculative.mode=drafter 但 speculative.draft_model 为空。"
                    "静默按非投机跑会把配置错误伪装成「怎么没变快」的性能玄学"
                    "（§S.3），故此处拒启。"
                )

        def _load_all():
            """目标件与草稿件在**同一线程**加载：MLX 流是线程本地的。"""
            m, p = _vlm_load(model_path)
            d = _load_drafter(m, draft_path, spec_cfg) if draft_path else (None, None)
            return m, p, d

        if executor is None:
            model, processor, drafter = _load_all()
        else:
            loop = asyncio.get_running_loop()
            model, processor, drafter = await loop.run_in_executor(executor, _load_all)
        return cls(model, processor, {**cfg, "model_path": str(model_path)},
                   drafter=drafter)

    def __init__(self, model, processor, cfg: Optional[Dict[str, Any]] = None,
                 drafter=None):
        self.model = model
        self.processor = processor
        self.draft_model, self.draft_kind = drafter or (None, None)
        cfg = cfg or {}

        # KV Cache 量化配置（默认关闭）：kv_bits 为 None 时走稠密缓存，
        # 与未配置时行为完全一致；设为小数（如 3.5）则底层 generate_step 启用 TurboQuant。
        # 模型身份（T2 落盘产物的归属校验：换模型后旧 KV 文件必须被拒收）
        self.model_key = str(cfg.get("model_path", ""))
        # KV 落盘开关（Q9-a 用户拍板：由用户决定）：false = 不写盘也不读盘，
        # 冻结/解冻退回释放+基座克隆的现状路径（隐私优先形态）
        self.kv_persist_enabled = bool(cfg.get("kv_persist", True))
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

    # ---- 缓存持久化（T2）：冻结落盘 / 解冻热恢复 ----

    def persist_cache(self, h, dest: str) -> bool:
        """把缓存序列化到 dest（safetensors）。M3 实测：8k 会话 153MB、save ~0.1s，
        vs 解冻重预填 ~13s。空缓存返回 False；任何失败清理半成品并返回 False（冷路兜底）。

        格式：L{i}_{j} 扁平张量 + metadata（version/model/layers/token_ids）。层对象经
        state/meta_state 协议原样往返（KVCache/RotatingKVCache 均实测续写一致）；state
        含非张量元素的层型（如量化缓存的嵌套结构）不支持——整体放弃，不出半份文件。"""
        if not self.kv_persist_enabled:
            return False
        state = getattr(h, "state", None)
        if state is None or not state.cache:
            return False
        dest_p = Path(dest)
        # 临时名必须保住 .safetensors 后缀：mx.save_safetensors 会给其他后缀自动
        # 追加 .safetensors，写出的文件名和 replace 的源名对不上（实测踩过）
        tmp = dest_p.with_name(f"{dest_p.stem}.tmp.safetensors")
        try:
            tensors: Dict[str, Any] = {}
            layers = []
            for i, c in enumerate(state.cache):
                st = c.state
                if not isinstance(st, (tuple, list)):
                    st = (st,)
                if not all(isinstance(a, mx.array) for a in st):
                    return False          # 嵌套/非张量 state：本版不落盘
                for j, arr in enumerate(st):
                    tensors[f"L{i}_{j}"] = arr
                meta = c.meta_state
                if isinstance(meta, str):
                    # KVCache 的 meta_state 是空串（无元状态）：必须存成空，
                    # 回设非空会触发 "no meta_state but a meta_state was set"
                    meta = (meta,) if meta else ()
                layers.append({"cls": type(c).__name__, "n": len(st),
                               "meta": list(meta or ())})
            metadata = {
                "version": CACHE_PERSIST_VERSION,
                "model": self.model_key,
                "layers": json.dumps(layers),
                "token_ids": json.dumps(state.token_ids or []),
            }
            dest_p.parent.mkdir(parents=True, exist_ok=True)
            mx.save_safetensors(str(tmp), tensors, metadata=metadata)
            tmp.replace(dest_p)
            return True
        except Exception as e:
            print(f"[Memory] KV 缓存落盘失败（解冻将走冷路）: {e}")
            tmp.unlink(missing_ok=True)
            return False

    def restore_cache(self, src: str) -> Optional[MLXCacheHandle]:
        """从 persist_cache 产物重建句柄。文件缺失/版本不符/模型不符/任何异常 → None。"""
        if not self.kv_persist_enabled:
            return None
        p = Path(src)
        if not p.is_file():
            return None
        try:
            tensors, metadata = mx.load(str(p), return_metadata=True)
            if metadata.get("version") != CACHE_PERSIST_VERSION:
                return None
            if metadata.get("model", "") != self.model_key:
                print(f"[Memory] KV 落盘产物属于其他模型，忽略: {p.name}")
                return None
            from mlx_vlm.models import cache as cache_mod
            layers: List[Any] = []
            for i, lm in enumerate(json.loads(metadata["layers"])):
                cls = getattr(cache_mod, str(lm["cls"]), None)
                if cls is None:
                    return None
                try:
                    c = cls()
                except TypeError:
                    c = cls(None)     # RotatingKVCache(max_size)：占位，meta_state 恢复真值
                arrs = tuple(tensors[f"L{i}_{j}"] for j in range(int(lm["n"])))
                c.state = arrs if len(arrs) > 1 else arrs[0]
                meta = lm.get("meta") or []
                if meta:
                    c.meta_state = tuple(meta)
                layers.append(c)
            h = MLXCacheHandle()
            h.state.cache = layers
            token_ids = json.loads(metadata.get("token_ids", "[]"))
            h.state.token_ids = token_ids or None
            return h
        except Exception as e:
            print(f"[Memory] KV 缓存恢复失败（冷路兜底）: {e}")
            return None

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
            if self.draft_model is not None:
                # 草稿件模式：模型对象走 kwargs，不进 config 字典
                kv_kwargs["draft_model"] = self.draft_model
                kv_kwargs["draft_kind"] = self.draft_kind

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
