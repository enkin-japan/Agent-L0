import asyncio
import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional, Callable

from . import prompt_stream
from .backend_protocol import InferenceBackend
from .dialect_protocol import ModelDialect
from .stream_filter import VisibleTextStreamer
from .constrained import ToolConstraintEngine, ToolCallGuard, llguidance as _llg
from ..memory.session import ChatSession


class AgentEngine:
    """
    推理引擎：封装底层大模型的单次生成（Prompt 组装 → 流式生成 → 输出解析）。
    多轮编排（ReAct 循环、计划推进、失败路由、审批闸门）由 Orchestrator 负责；
    本层对上彻底屏蔽推理后端的物理复杂性，并持有全局推理互斥锁。

    两条协议注入，本层对二者皆无知：
    - **物理承载** 经 InferenceBackend 协议（backends/，P0 手术产物）；
    - **模型方言** 经 ModelDialect 协议（dialects/，R0 手术产物）——prompt 模板、
      输出反序列化、频道/工具标记、调用体 schema 全部归方言，本层不认任何
      模型家族的字面量。

    本层保留的、既与后端无关也与方言无关的资产：VisibleTextStreamer / 推理互斥锁 /
    专用推理执行器线程 / token 去向分解仪表 / 工具约束状态机（core/constrained.py）。
    """
    @classmethod
    async def create(cls, model_path, *, kv_config=None,
                     model_options=None, env=None,
                     backend: str = "", max_concurrency: int = 1) -> "AgentEngine":
        """标准构造路径：**在推理线程内加载模型**，再装配引擎。

        进程内后端的惰性张量/计算流通常与创建线程绑定：主线程加载、推理线程
        前向会跨线程崩溃（细节见所选后端模块头注释）。把 load 本身放进专用
        推理线程，推理宇宙（权重/常量/KV/生成）从出生起就同属一个线程。

        max_concurrency（B1，config agent.max_inference_concurrency）：并发生成上限。
        进程内后端受"单线程 + 共享单例"物理约束强制钳制为 1（默认值，行为与现状全同）；
        仅远程后端（服务端自管并发）允许 >1——此时执行器线程数随之放宽。

        model_options（R0）：config.yaml inference.model_registry 的所选条目。方言名
        （dialect 键）与全部模型级行为开关（如 gemma-4 的 suppress_ghost_thinking）
        都在里面，由方言自行解读——本层不解读任何一个，故换模型家族不动本签名。
        """
        max_concurrency = max(1, int(max_concurrency))
        if max_concurrency > 1 and backend != "remote":
            print(f"[Engine] max_inference_concurrency={max_concurrency} 仅对 remote 后端生效，"
                  f"进程内后端强制钳制为 1（共享单例不可并发）")
            max_concurrency = 1
        # 条目级投机覆盖（D.21 qwen38 MTP 转正）：进程内路（app.py / run_eval 引擎
        # 直连）与 server 同口径——评测面即部署面。本路无 INFER_SPEC_* env 概念
        # （env 只由 server 消费并在彼处压过条目），故 env_forced 恒 False。
        from ..utils.model_selection import merge_entry_speculative
        kv_config = merge_entry_speculative(dict(kv_config or {}),
                                            model_options or {})
        executor = ThreadPoolExecutor(max_workers=max_concurrency,
                                      thread_name_prefix="inference")
        from ..backends import get_backend_class  # 后端命名收编在注册面（缺省即默认后端）
        backend_obj = await get_backend_class(backend).load(
            model_path, executor=executor, **(kv_config or {}))
        return cls(backend_obj, kv_config=kv_config,
                   model_options=model_options, env=env,
                   _executor=executor, max_concurrency=max_concurrency)

    def __init__(self, backend: InferenceBackend, kv_config=None,
                 model_options=None,
                 env=None, _executor: "ThreadPoolExecutor | None" = None,
                 max_concurrency: int = 1):
        self.backend = backend

        # 模型方言（R0）：prompt 模板 / 输出反序列化 / 频道与工具标记 / 调用体 schema
        # 全部归方言。方言名与模型级开关（gemma-4 大杯的 suppress_ghost_thinking 等）
        # 同走注册表条目——见 dialects/__init__.py。缺省即默认方言（gemma4）。
        from ..dialects import get_dialect_class  # 方言命名收编在注册面（惰性导入）
        model_options = model_options or {}
        self.dialect: ModelDialect = get_dialect_class(
            str(model_options.get("dialect") or "")).create(
                backend.processor, options=model_options, env=env)

        # [Phase 2] 工具调用段约束采样（llguidance schema mask）。
        # 懒构建：LLTokenizer ~1s，首次带 tools 的生成时建；缺库/建失败 → 永久禁用（fail-open）。
        kv_config = kv_config or {}
        self.constrained_tool_calls = bool(kv_config.get("constrained_tool_calls", False))
        self._constraint_engine = None
        self._constraint_disabled = _llg is None
        if self.constrained_tool_calls and self._constraint_disabled:
            print("[Engine] constrained_tool_calls=true 但 llguidance 不可用，约束采样禁用")

        # 推理互斥：底层生成流依赖共享可变单例（detokenizer、stopping/thinking
        # criteria、生成尾部的缓存清理等），并发请求逐 token 交错执行会互相污染文本流。
        # 因此进程内后端生成段必须串行；等待方挂起在信号量上不阻塞事件循环，
        # 其他请求的文件解析、历史查询、分支等非推理任务仍可并发。
        # B1 起为并发上限信号量：默认 1（与互斥锁行为全同）；远程后端可配 >1
        # （create 钳制进程内后端恒为 1），调度器 ClassPolicy/抢占/老化不动。
        self._inference_lock = asyncio.Semaphore(max(1, int(max_concurrency)))

        # 专用推理线程（单线程执行器）：进程内生成是同步计算，放在事件循环线程会把
        # 整个 API 饿死（prefill 一整块数秒内连 /health 都无响应——Phase 1 实机定性）。
        # 生成整段搬进本线程，事件循环全程自由；token 增量经 call_soon_threadsafe 回主
        # 循环写账本。单线程 + 推理锁双保险：所有生成/摘要串行，后端单例不被并发触碰。
        # 标准路径经 create() 传入已承载模型加载的执行器（推理宇宙同线程）；
        # 直接构造（测试/脚本）时自建——注意直接构造 + 主线程已加载的真模型仍有
        # 跨线程惰性常量风险，生产装配一律走 create()。
        self._inference_executor = _executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="inference")

        # T3 基座前缀缓存：system+tools 稳定前缀（M5 实测 9424 token，冷预填 ~16s，
        # 克隆 ~10ms）的共享预填缓存。app 启动与空闲回合经 prewarm_base_cache 装配
        # （key 未变即 no-op），主编排会话的处女缓存在 generate_once 改为克隆它起步。
        self._base_cache = None
        self._base_cache_key: Optional[str] = None

    # 装配便利面（app.py 依赖注入用）：模型与处理器由后端持有
    @property
    def model(self):
        return self.backend.model

    @property
    def processor(self):
        return self.backend.processor

    @staticmethod
    def _notes_index() -> "Optional[List[str]]":
        """长期记忆索引（R4）。笔记本未装配（测试/未启用）时返回 None。

        惰性反向 import：`core` 不静态依赖 `agents.runtime`（沿用 `orchestrator.py`
        取 runtime 句柄的先例）。索引自带行数/字节上限，见 `memory/notes.py`。
        """
        try:
            from ..agents import runtime
            store = runtime.get_note_store()
            return store.index_lines() if store is not None else None
        except Exception as e:
            # 记忆索引取不到只是"这轮想不起来"，绝不能因此让生成失败
            print(f"[Engine] ⚠️ 取长期记忆索引失败（本轮不注入）: {e}")
            return None

    # ---------- T3 基座前缀缓存 ----------

    @staticmethod
    def _cache_is_virgin(handle) -> bool:
        """缓存从未被预填过（新建会话或解冻后的首轮）。MLX 句柄看 state.cache，
        远程句柄看懒注册的 cache_id；未知形状一律按非处女处理（绝不动别人的缓存）。"""
        st = getattr(handle, "state", None)
        if st is not None:
            return getattr(st, "cache", None) is None
        if hasattr(handle, "cache_id"):
            return handle.cache_id is None
        return False

    def _adopt_base_cache(self, session: ChatSession) -> None:
        """处女缓存换成基座克隆起步：9.4k token 的 system+tools 预填直接命中。
        克隆走后端 clone_cache（与会话分支同一前例，session.py clone_brain_memory_from）；
        失败/不支持一律静默回退冷启动，正确性不受影响。"""
        if self._base_cache is None or session.brain_memory_state is None:
            return
        if not self._cache_is_virgin(session.brain_memory_state):
            return
        try:
            cloned = self.backend.clone_cache(self._base_cache)
        except Exception as e:
            print(f"[Engine] 基座缓存克隆失败（本轮冷启动）: {e}")
            return
        if cloned is None:
            return
        old = session.brain_memory_state
        session.brain_memory_state = cloned
        try:
            self.backend.release_cache(old)
        except Exception:
            pass
        print(f"[Engine] 会话 {getattr(session, 'session_id', '?')} 起步于基座前缀缓存")

    def _prepare_session_cache(self, session: ChatSession) -> None:
        """处女缓存的两级热启动（T2+T3）：解冻热恢复（含整段会话历史）优先，
        其次基座克隆（只含 system+tools），都不行走冷路。只动处女缓存。"""
        if session.brain_memory_state is None \
                or not self._cache_is_virgin(session.brain_memory_state):
            return
        persist_path = getattr(session, "kv_persist_path", None)
        if persist_path is not None and hasattr(self.backend, "restore_cache"):
            restored = None
            try:
                restored = self.backend.restore_cache(str(persist_path))
            except Exception as e:
                print(f"[Engine] KV 热恢复异常（退基座/冷路）: {e}")
            if restored is not None:
                old = session.brain_memory_state
                session.brain_memory_state = restored
                try:
                    self.backend.release_cache(old)
                except Exception:
                    pass
                print(f"[Engine] 会话 {session.session_id} 解冻热恢复（KV 从盘装载）")
                return
        self._adopt_base_cache(session)

    async def prewarm_base_cache(self, system_prompt: str,
                                 tools: Optional[List[Dict[str, Any]]] = None) -> bool:
        """预填基座前缀（system+tools，与真实轮次同一 builder 同一入参）。

        幂等：key（prompt 内容哈希）未变时 no-op，空闲回合可放心反复调；工具集变化
        （rescan 增减工具）时自动重建并释放旧基座。预填多出的尾部小段（日期块+生成头
        +1 token）由自动前缀匹配在真实轮次首用时裁掉，无需专门的 prefix-only 构建。
        返回是否真正做了预填。"""
        prompt = self.dialect.build_prompt(
            messages=[{"role": "system", "content": system_prompt}],
            tools=tools, include_env=False)
        key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if key == self._base_cache_key and self._base_cache is not None:
            return False
        cache = self.backend.new_cache()
        loop = asyncio.get_running_loop()

        def _consume():
            for _ in self.backend.stream_generate(
                    prompt, cache=cache,
                    sampling={"max_tokens": 1, "temperature": 0.0},
                    features={}):
                pass

        t0 = time.time()
        async with self._inference_lock:
            await loop.run_in_executor(self._inference_executor, _consume)
        old, self._base_cache, self._base_cache_key = self._base_cache, cache, key
        if old is not None:
            try:
                self.backend.release_cache(old)
            except Exception:
                pass
        print(f"[Engine] 基座前缀缓存就绪（{time.time() - t0:.1f}s，key {key[:8]}）")
        return True

    async def prewarm_session_cache(self, session: ChatSession,
                                    tools: Optional[List[Dict[str, Any]]] = None) -> bool:
        """T1：空闲时把会话当前上下文（压缩改写后的 [system]+[摘要]+[尾部]）预填进
        其缓存——生成 1 token 弃产出，纯为热 KV。与 generate_once 同 builder 同入参，
        下一真实轮的前缀匹配可命中到新用户消息之前的全部内容。成功清 context_dirty。"""
        if session.brain_memory_state is None:
            return False
        # 上位解（评审 M11）：流仍有效时预热是危险的 no-op——全量渲染字节与
        # 流字节在首个冻结 env 块处分叉，喂进会话缓存反而把下一轮增量路的纯追加
        # 命中打碎。流失效（压缩改写后正是如此）才值得预热。
        if prompt_stream.stream_valid(getattr(session, "prompt_stream", None),
                                      session.history, tools):
            return False
        prompt = self.dialect.build_prompt(
            messages=session.history, tools=tools,
            include_env=getattr(session, "inject_env_context", False),
            env_extra=self._notes_index(),
        )
        cache = session.brain_memory_state
        loop = asyncio.get_running_loop()

        def _consume():
            for _ in self.backend.stream_generate(
                    prompt, cache=cache,
                    sampling={"max_tokens": 1, "temperature": 0.0},
                    features={}):
                pass

        t0 = time.time()
        async with self._inference_lock:
            # 拿到锁后复检：等待期间若有用户轮进来把上下文又改了/会话冻结了，放弃本次
            if getattr(session, "is_cold", False) or session.brain_memory_state is not cache:
                return False
            await loop.run_in_executor(self._inference_executor, _consume)
        session.context_dirty = False
        print(f"[Engine] 会话 {session.session_id} 空闲预热完成（{time.time() - t0:.1f}s）")
        return True

    # ---------- 上位解：prompt 字节流「只增不改」组装 ----------

    def _assemble_prompt(self, session: ChatSession,
                         tools: Optional[List[Dict[str, Any]]], *,
                         enable_thinking: bool, task_block: Optional[str],
                         tail_notes: Optional[List[str]], include_env: bool,
                         env_extra: Optional[List[str]]):
        """本轮 prompt 组装：优先「已记录字节流 + 方言增量段」（跨轮 token 纯追加，
        T8 门对全部缓存类型放行复用），流失效/方言不支持则退全量重渲染。
        返回 (prompt, 尾块快照 tail_state 或 None, 增量偏移或 None)。
        增量偏移 = 流字节长（prompt[:offset] 即上轮已喂+已生成的字节）——后端据此
        走 token 级增量喂入（历史段取缓存记录、永不重分词，根除采样出非规范 BPE
        序列时的再分词错位）。None = 走了全量路。
        tail_state is None 表示方言不支持流机制——本轮不提交流。"""
        ps = getattr(session, "prompt_stream", None)
        delta_fn = getattr(self.dialect, "build_prompt_delta", None)
        if delta_fn is not None and prompt_stream.stream_valid(ps, session.history, tools):
            out = delta_fn(session.history[ps.seen + 1:],
                           enable_thinking=enable_thinking, task_block=task_block,
                           tail_notes=tail_notes, include_env=include_env,
                           env_extra=env_extra, prev_tail=ps.tail)
            if out is not None:
                delta, tail = out
                return ps.stream + delta, tail, len(ps.stream)
        prompt = self.dialect.build_prompt(
            messages=session.history, tools=tools, enable_thinking=enable_thinking,
            task_block=task_block, tail_notes=tail_notes,
            include_env=include_env, env_extra=env_extra)
        tail_fn = getattr(self.dialect, "tail_state", None)
        tail = (tail_fn(task_block=task_block, include_env=include_env,
                        env_extra=env_extra) if tail_fn is not None else None)
        return prompt, tail, None

    async def generate_once(self,
                            session: ChatSession,
                            tools: Optional[List[Dict[str, Any]]] = None,
                            grammar_tools: Optional[List[Dict[str, Any]]] = None,
                            task_block: Optional[str] = None,
                            temperature: float = 0.2,
                            min_p: float = 0.05,
                            enable_thinking: bool = False,
                            thinking_budget: int = 2048,
                            max_tokens: int = 4096,
                            tail_notes: Optional[List[str]] = None,
                            on_text_delta: Optional[Callable[[str], None]] = None,
                            on_thinking_delta: Optional[Callable[[str], None]] = None,
                            on_tool_pending: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        """
        执行一次完整生成并解析输出。

        - tools: 工具声明（稳定前缀的一部分，由 Orchestrator 启动时构建并保持不变）
        - grammar_tools: **约束采样的语法源**，与上面的 prompt 声明解耦（§6.5）。
          catalog 模式下 tools 是瘦目录（省前缀 token），语法却必须建在完整 schema 上，
          否则带参工具被钳成只能发空实参。为 None 即沿用 tools（直调路径语义不变）；
          传空列表则与「无工具」同义、直接关掉约束（`is not None` 判据，非笔误）。
          它不参与 prompt 组装，故不影响会话字节流「只增不改」的不变量。
          调用方有责任让它与 tools 同名同集——语法允许一个 prompt 里没声明的工具，
          等于放模型去调用一个它看不见的东西。
        - task_block: 任务状态块（可变区，追加在 prompt 最尾部，见前缀分层设计）
        - 取消：逐 token 段检查 session.cancel_requested，命中即中断生成返回
          cancelled=True（丢弃的部分生成由下次推理的 KV 前缀匹配自动裁剪）
        - on_text_delta: 真流式回调（可选）。每产出一段**用户可见正文**即同步调用一次
          （思考/工具调用区块经 VisibleTextStreamer 过滤，不会流出）。为 None 时行为与
          改动前完全一致——攒全文、末尾一次性 parse，终态仍走原路（保证 turn_end 权威）。
        - on_thinking_delta: 思考直播回调（Phase 3）：thought 频道内容逐段回吐。
        - on_tool_pending: 工具名预告回调（Phase 3）：生成进入工具调用块并解析出
          name 字段的瞬间调用一次——执行前就知道要调什么。
          三个回调均在主事件循环线程被调（推理线程经 call_soon_threadsafe 跳转）。
        """
        # T2+T3：主编排会话的处女缓存两级热启动——解冻热恢复优先，其次克隆基座
        # （沙箱替身系统提示不同，不采用：克隆了也会在前缀匹配处几乎全裁掉）
        if getattr(session, "inject_env_context", False):
            self._prepare_session_cache(session)

        formatted_prompt, _stream_tail, _delta_offset = self._assemble_prompt(
            session, tools,
            enable_thinking=enable_thinking,
            task_block=task_block,
            tail_notes=tail_notes,
            # 环境事实只注入主编排会话；沙箱一次性会话替身默认 False（隐私边界，opt-in）
            include_env=getattr(session, "inject_env_context", False),
            # 长期记忆索引（R4）：模型得先"知道自己知道"，才谈得上去召回正文。
            # 同样是个人事实，故与静态环境事实共用 include_env 这一道门（门在 builder 里，
            # 此处即便传了，沙箱也拿不到）。
            env_extra=self._notes_index(),
        )
        # 思考播种用方言的实际 token（而非硬编码字面量）：换模型/token 表时保持一致
        generated_text = self.dialect.thinking_prefill(enable_thinking)
        metrics: Dict[str, Any] = {}
        cancelled = False

        # 真流式过滤器：把原始 token 流增量过滤成可见正文（思考/工具块不流出正文,
        # 但分别经 on_thinking_delta 直播、on_tool_pending 预告——Phase 3 粒度钩子）。
        # enable_thinking 时 prompt 收尾在已打开的 thought 频道内，故以抑制态起步。
        # 钩子回调在推理线程触发,这里包一层 call_soon_threadsafe 回主循环(账本纪律)。
        streamer = None
        _loop_for_hooks = asyncio.get_running_loop()

        def _hop(cb: Optional[Callable[[str], None]]) -> Optional[Callable[[str], None]]:
            if cb is None:
                return None
            return lambda s: _loop_for_hooks.call_soon_threadsafe(cb, s)

        if on_text_delta is not None or on_thinking_delta is not None or on_tool_pending is not None:
            _mk = self.dialect.markers
            streamer = VisibleTextStreamer(
                soc=_mk.soc, eoc=_mk.eoc, stc=_mk.stc, etc=_mk.etc,
                in_thought=enable_thinking,
                on_thinking=_hop(on_thinking_delta),
                on_tool_pending=_hop(on_tool_pending),
            )

        # [Phase 2] 工具调用段约束采样：每次生成一个新 guard（纯逻辑状态机），
        # 经 features 交由后端挂载进它的 logits 处理协议；未启用时零开销。
        # 语法源与 prompt 声明解耦（§6.5）：catalog 模式下 `tools` 是瘦目录（省稳定
        # 前缀 token），语法却必须建在完整 schema 上，否则带参工具被钳成只能发空实参。
        # 调用方不传 grammar_tools 就沿用 tools——沙箱/摘要器/审计等直调路径语义不变。
        gtools = grammar_tools if grammar_tools is not None else tools
        guard = self._tool_guard(gtools)

        sampling = {
            "temperature": temperature,
            "min_p": min_p,
            "max_tokens": max_tokens,
            "repetition_penalty": 1.1,
            "repetition_context_size": 50,
        }
        features: Dict[str, Any] = {
            "enable_thinking": enable_thinking,
            "thinking_budget": thinking_budget,
            # token 级增量喂入（上位解）：增量路且纯文本会话才下发——多模态占位符
            # 的展开在 processor，增量分词覆盖不了，后端亦有同向守门
            "prompt_delta_offset": (_delta_offset
                                    if _delta_offset is not None
                                    and not session.images and not session.audios
                                    else None),
            # 预算锚点：本方言的频道 token（详见后端对预算裁决的说明）
            "thinking_start_token": self.dialect.markers.soc,
            "thinking_end_token": self.dialect.markers.eoc,
            # 展示增量：仅在真流式时要求后端计算（detokenizer 差分有每 token 成本）
            "want_delta": streamer is not None,
        }
        if guard is not None:
            features["tool_guard"] = guard
            # 工具声明随行（B1）：纯逻辑 guard 无法跨网逐 token 驱动，远程后端改发
            # 声明、由服务端约束引擎重建 guard 并把 constrained_steps 回填本地仪表；
            # 进程内后端忽略此键（直接驱动 tool_guard），语义单一来源
            features["tool_specs"] = gtools
            # 声明式约束规格：引擎原生结构化输出的后端（cuda/vLLM structural-tag）
            # 吃这个——**进程内路径也必须带**（2026-08-08 26B 评测案：只有 server 附
            # 此键，引擎直连评测全场无约束，bfcl-multiple 成批裸格式调用，架构缺口
            # 非模型智力）。guard 已建成 = 语法 builder 可用；XML 方言（qwen35）
            # 在 _tool_guard 内已 fail-open，走不到这里。MLX 逐 token 后端忽略此键。
            # kind 随行（§7-8）：后端据此判断自己能不能表达该语法——vLLM 的
            # structural-tag 只吃 JSON schema，拿 lark 语法必须拒而不是硬塞。
            _tc_grammar = self.dialect.tool_call_grammar(gtools)
            features["tool_constraint"] = {
                "kind": _tc_grammar.kind,
                "body": _tc_grammar.body,
                "begin": self.dialect.markers.stc,
                "end": self.dialect.markers.etc,
            }

        # 整段生成搬进专用推理线程（事件循环全程自由:API/心跳/事件流不再被计算饿死）。
        # 推理锁按单次生成粒度持有（工具调用等 I/O 期间由上层让出,供其他会话推理）;
        # 可见增量经 call_soon_threadsafe 回主循环（账本 emit 必须在循环线程,FIFO 保序）。
        loop = asyncio.get_running_loop()

        def _blocking_generate() -> Dict[str, Any]:
            gen_text = ""
            gen_metrics: Dict[str, Any] = {}
            gen_cancelled = False
            gen_extra: Dict[str, Any] = {}
            for chunk in self.backend.stream_generate(
                formatted_prompt,
                cache=session.brain_memory_state,
                images=session.images or None,
                audios=session.audios or None,
                sampling=sampling,
                features=features,
            ):
                gen_text += chunk.text
                gen_metrics = {
                    "prompt_tokens": chunk.prompt_tokens,
                    "generation_tokens": chunk.generation_tokens,
                    "prompt_tps": chunk.prompt_tps,
                    "generation_tps": chunk.generation_tps,
                    "peak_memory": chunk.peak_memory
                }
                if chunk.extra:
                    gen_extra = chunk.extra
                # 真流式：取本段的可见文本增量 → 过滤成可见正文 → 回吐主循环。
                # delta=None 表示后端无 detokenizer 级差分能力，退回规范文本段增量。
                if streamer is not None:
                    visible = streamer.feed(chunk.delta if chunk.delta is not None
                                            else chunk.text)
                    if visible and on_text_delta is not None:
                        loop.call_soon_threadsafe(on_text_delta, visible)
                # 取消检查点（token 段粒度;bool 读在 GIL 下安全）：立即停止消耗生成流
                if session.cancel_requested:
                    gen_cancelled = True
                    break
            # 冲刷过滤器尾部（未闭合频道的残留按语义丢弃）
            if streamer is not None and not gen_cancelled:
                tail = streamer.close()
                if tail and on_text_delta is not None:
                    loop.call_soon_threadsafe(on_text_delta, tail)
            return {"text": gen_text, "metrics": gen_metrics,
                    "cancelled": gen_cancelled, "extra": gen_extra}

        async with self._inference_lock:
            outcome = await loop.run_in_executor(self._inference_executor, _blocking_generate)
        generated_text += outcome["text"]
        metrics = outcome["metrics"] or metrics
        cancelled = outcome["cancelled"]

        if _delta_offset is not None and not cancelled:
            metrics["stream_delta"] = True

        # [Phase 3] 投机采样仪表：后端经 GenChunk.extra 回吐接受率统计
        extra = outcome.get("extra") or {}
        if extra.get("spec_drafted"):
            metrics["spec_drafted"] = extra["spec_drafted"]
            metrics["spec_accepted"] = extra.get("spec_accepted", 0)

        # 前缀命中仪表（T4R）：每次生成一行，命中率异常（本该热的轮次 reused≈0）
        # 直接在日志里现形——M7 里 app 实测 23tps vs 台架 34tps 的缺口靠它在实况定位
        if "reused_prefix" in extra:
            metrics["reused_prefix"] = extra["reused_prefix"]
            pt = metrics.get("prompt_tokens") or 0
            print(f"[Engine] 前缀命中 {extra['reused_prefix']}/{pt} tokens，"
                  f"gen {metrics.get('generation_tps', 0):.1f} tps")

        if guard is not None and guard.masked_positions:
            metrics["constrained_steps"] = guard.masked_positions
            print(f"[Constrained] 本次生成有 {guard.masked_positions} 步受工具 schema 约束"
                  + ("（曾 fail-open）" if guard.failed else ""))

        if cancelled:
            return {"cancelled": True, "thoughts": [], "text": "", "tool_calls": [], "metrics": metrics}

        # [仪表 Phase 0] token 去向分解：thinking / 可见正文 / 工具 JSON / 标记开销。
        # 收益评估的分母（约束采样只作用于 tool_json 段、thinking 分级只压 thinking 段）。
        metrics["token_breakdown"] = self._token_breakdown(
            generated_text, metrics.get("generation_tokens", 0) or 0
        )

        parsed = self.dialect.parse_response(generated_text)

        # 上位解流提交：成功生成后记录「实际喂入 prompt + 实际生成原始字节」。
        # 两种「完成但不提交」情形（详见 prompt_stream.commit docstring）：带一次性
        # 尾注（冻结进流会把只此一轮的提醒变成永久指令，评审 C1）、产出含解析失败
        # 调用（全量渲染刻意不回放 error 调用，评审 M4）——本轮照常吃增量提速。
        # 但此时**必须把旧流作废**而不是留着（二过评审 C1）：后端记录已随本轮前进，
        # 陈旧流在「编排器不追加消息就重试」（runaway 重试/max_tokens 升级）的路径
        # 上仍会通过 stream_valid，token 级喂入会把本想丢弃的坏产出按记录原样喂回
        # ——纯追加下门不响、无痕。置 None 强制下一轮全量重渲染。
        # （取消不同：消费方 break 让后端 update 未执行、记录未前进，旧流与记录
        # 仍一致，保留即可——残局由前缀匹配 + T8 门消化。）
        # 方言不支持（tail_state 缺位 → _stream_tail is None）则全程走全量路。
        if _stream_tail is not None:
            if not tail_notes \
                    and not any(c.get("type") == "error" for c in parsed["tool_calls"]):
                session.prompt_stream = prompt_stream.commit(
                    formatted_prompt, outcome["text"], session.history, tools,
                    _stream_tail)
            else:
                session.prompt_stream = None
        # 截断检测（供上层 max_tokens 恢复用）：底层生成流遇 EOS/停止符会 break、
        # 生成 token 数 < max_tokens；撞上 max_tokens 上限则是生成器自然耗尽、恰好产满
        # max_tokens 个 token。故 generation_tokens >= max_tokens 即判定本次生成被长度截断。
        truncated = (metrics.get("generation_tokens", 0) or 0) >= max_tokens
        return {
            "cancelled": False,
            "thoughts": parsed["thoughts"],
            "text": parsed["text"],
            "tool_calls": parsed["tool_calls"],
            "truncated": truncated,
            "metrics": metrics,
        }

    # ---------- 约束采样（inference-speedup Phase 2） ----------
    def _tool_guard(self, tools: Optional[List[Dict[str, Any]]]):
        """按需构建本次生成的工具调用约束 guard；关闭/无工具/构建失败一律返回 None。

        LLTokenizer 进程内建一次（~1s，摊销）；语法按工具集缓存；matcher 复用
        （推理互斥锁保证同一时刻只有一次生成在用它）。任何失败永久禁用并告警，
        绝不影响生成主路径。

        与 inference/server 的分级**刻意不同**（m9，2026-08-23）：server 的懒建曾在
        事件循环线程与推理线程并发碰 Rust 分词器（Already borrowed → 永久静默禁用），
        故改为 lifespan 预建 + 拒启/按次 fail-open。这里的懒建跑在生成线程、持推理锁，
        没有那条竞态，保持原策；别把两边"统一"回同一种。
        """
        if not self.constrained_tool_calls or self._constraint_disabled or not tools:
            return None
        try:
            if self._constraint_engine is None:
                tok = (self.processor.tokenizer
                       if hasattr(self.processor, "tokenizer") else self.processor)
                _mk = self.dialect.markers
                stc = tok.encode(_mk.stc, add_special_tokens=False)
                etc = tok.encode(_mk.etc, add_special_tokens=False)
                if len(stc) != 1 or len(etc) != 1:
                    raise RuntimeError("工具调用标记不是单 token，约束状态机无法锚定")
                # 语法由方言提供（R0/§7-8）：约束机制方言无关，调用体格式是方言事实
                self._constraint_engine = ToolConstraintEngine(
                    tok, stc[0], etc[0], self.dialect.tool_call_grammar)
                print("[Engine] 约束采样引擎就绪（llguidance）")
            eng = self._constraint_engine
            return ToolCallGuard(eng.matcher_for(tools), eng.stc_id, eng.etc_id, eng.vocab_size)
        except Exception as e:
            print(f"[Engine] 约束采样构建失败，永久禁用（纠错闭环兜底）: {type(e).__name__}: {e}")
            self._constraint_disabled = True
            return None

    # ---------- 仪表（inference-speedup Phase 0） ----------
    def _token_breakdown(self, generated_text: str, generation_tokens: int) -> Dict[str, int]:
        """
        按频道把单次生成的原始输出切段并重编码计数，得到 token 去向分解。
        重编码计数与实际生成 token 数在段边界处有 ±1 级误差（可接受，仪表口径）；
        差值记为 marker_overhead（频道/工具标记本身的开销），下限截到 0。
        """
        def enc(s: str) -> int:
            if not s:
                return 0
            return len(self.processor.tokenizer.encode(s, add_special_tokens=False))

        thinking = visible = tool_json = 0
        _mk = self.dialect.markers
        ch_pat = re.compile(
            rf"{re.escape(_mk.soc)}(.*?){re.escape(_mk.eoc)}", re.DOTALL)
        for m in ch_pat.finditer(generated_text):
            parts = re.split(r"\s+", m.group(1).strip(), maxsplit=1)
            body = parts[1] if len(parts) == 2 else ""
            if parts and parts[0] == "thought":
                thinking += enc(body)
            else:
                visible += enc(body)
        rest = ch_pat.sub("", generated_text)
        tool_pat = re.compile(
            rf"{re.escape(_mk.stc)}(.*?){re.escape(_mk.etc)}", re.DOTALL)
        for m in tool_pat.finditer(rest):
            tool_json += enc(m.group(1).strip())
        rest = tool_pat.sub("", rest)
        visible += enc(re.sub(r"<\|.*?\|>", "", rest).strip())
        overhead = max(0, generation_tokens - thinking - visible - tool_json)
        return {"thinking": thinking, "visible": visible,
                "tool_json": tool_json, "marker_overhead": overhead}

    # ---------- 上下文摘要（Step 2：摘要式压缩用） ----------
    SUMMARY_INSTRUCTION = (
        "You are a conversation-context compressor. Condense the conversation history below "
        "into a concise bullet-point summary, written in the same language the conversation "
        "itself is in.\n"
        "Keep: the user's long-term preferences and identity, confirmed key facts and data, "
        "unfinished tasks and agreements, important conclusions.\n"
        "Drop: small talk, procedural detail, resolved items with no future value.\n"
        "Ground time references: conversation turns are prefixed with [YYYY-MM-DD HH:MM] "
        "timestamps — resolve relative time words ('yesterday', 'next week', '明天') against "
        "them and write absolute dates in the summary. 'Trip next week' is useless a month "
        "later; 'trip the week of 2026-07-20' stays meaningful.\n"
        "If [Existing summary] is provided, merge [New conversation] into it and output the "
        "updated complete summary without losing still-valid information from the old one. "
        "Output only the summary body — no explanations, no preamble or suffix.\n"
        "Finally, rescue durable facts: if [New conversation] contains facts worth keeping "
        "across sessions (who the user is, stable preferences, confirmed decisions, "
        "ongoing-project facts), append a block starting with the exact line 'MEMO:' followed "
        "by at most 3 lines, each formatted as:\n"
        "- kebab-case-name | user|feedback|project|reference | one self-contained factual "
        "sentence with absolute dates\n"
        "If nothing qualifies, do not output a MEMO block at all."
    )

    async def summarize(self, content: str, prior_summary: str = "",
                        max_tokens: int = 640, temperature: float = 0.3,
                        cancel_check=None) -> str:
        # max_tokens 512→640：给尾部 MEMO 抢救块（压缩前记忆冲洗，≤3 行）留余量
        """
        对一段对话历史做要点摘要（摘要式压缩）。一次性裸生成：不带工具、不复用会话 KV
        （用后端新建的隔离缓存），与任何会话的推理状态隔离。持有推理锁保证与生成串行。
        cancel_check：可选无参回调，逐 token 段轮询；返回真则中断生成并返回空串
        （调用侧把空摘要视为失败、水位线不推进、下次触发重试——调度器抢占后台摘要靠它让位）。
        """
        parts = []
        if prior_summary.strip():
            parts.append("[Existing summary]\n" + prior_summary.strip())
        parts.append("[New conversation]\n" + content.strip())
        return await self.oneshot_instruction(
            self.SUMMARY_INSTRUCTION, "\n\n".join(parts),
            max_tokens=max_tokens, temperature=temperature, cancel_check=cancel_check)

    async def oneshot_instruction(self, instruction: str, content: str, *,
                                  max_tokens: int = 640, temperature: float = 0.3,
                                  cancel_check=None) -> str:
        """通用一次性裸生成（summarize 的抽象化，dream 笔记整理等系统回合共用）：
        不带工具、隔离缓存、持推理锁串行、被 cancel_check 抢占即返空串。"""
        messages = [
            {"role": "system", "content": instruction},
            {"role": "user", "content": content},
        ]
        prompt = self.dialect.build_prompt(messages=messages, tools=None, enable_thinking=False)

        fresh_cache = self.backend.new_cache()  # 隔离缓存：不碰任何会话的 KV
        loop = asyncio.get_running_loop()

        def _blocking_summarize() -> str:
            # [Phase 3] 摘要也走投机（后端按自身配置决定）：合并旧摘要时大段复述
            # [Existing summary]，命中率天然高
            text = ""
            for chunk in self.backend.stream_generate(
                prompt,
                cache=fresh_cache,
                sampling={"temperature": temperature, "min_p": 0.05, "max_tokens": max_tokens,
                          "repetition_penalty": 1.1, "repetition_context_size": 50},
                features={"enable_thinking": False},
            ):
                if cancel_check is not None and cancel_check():
                    return ""  # 被抢占：放弃本次摘要（无半途状态，下次触发重试）
                text += chunk.text
            return text

        try:
            async with self._inference_lock:
                text = await loop.run_in_executor(self._inference_executor, _blocking_summarize)
        finally:
            self.backend.release_cache(fresh_cache)
        if not text:
            return ""
        parsed = self.dialect.parse_response(text)
        return (parsed.get("text") or text).strip()
