import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional, Callable

from mlx_vlm.generate import PromptCacheState

# 导入底层防腐层（隔离 MLX 补丁与复杂接口）
from ..patch.gemma4_generate import stream_generate

from .prompt_builder import PromptBuilder
from .response_parser import ResponseParser
from .stream_filter import VisibleTextStreamer
from .constrained import ToolConstraintEngine, ToolCallGuard, llguidance as _llg
from ..memory.session import ChatSession


class AgentEngine:
    """
    推理引擎：封装底层大模型的单次生成（Prompt 组装 → 流式生成 → 输出解析）。
    多轮编排（ReAct 循环、计划推进、失败路由、审批闸门）由 Orchestrator 负责；
    本层对上彻底屏蔽 MLX 的物理复杂性，并持有全局推理互斥锁。
    """
    @classmethod
    async def create(cls, model_path, *, kv_config=None,
                     suppress_ghost_thinking: bool = False, env=None) -> "AgentEngine":
        """标准构造路径:**在推理线程内加载模型**,再装配引擎。

        MLX 的惰性张量携带创建时线程的流:主线程加载的模型,其 __init__ 里的
        非参数常量(rope 频率/缩放因子等,不在 parameters() 树、加载期不被物化)
        会在推理线程首次前向时触发跨线程流派发,报
        "There is no Stream(gpu, N) in current thread"。把 load 本身放进专用
        推理线程,推理宇宙(权重/常量/KV/生成)从出生起就同属一个线程,无一漏网。
        """
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx-inference")
        loop = asyncio.get_running_loop()
        from mlx_vlm.utils import load as _vlm_load
        model, processor = await loop.run_in_executor(executor, lambda: _vlm_load(model_path))
        return cls(model, processor, kv_config=kv_config,
                   suppress_ghost_thinking=suppress_ghost_thinking, env=env, _executor=executor)

    def __init__(self, model, processor, kv_config=None, suppress_ghost_thinking: bool = False,
                 env=None, _executor: "ThreadPoolExecutor | None" = None):
        self.model = model
        self.processor = processor

        # suppress_ghost_thinking：Gemma-4 大杯（26b/31b）关思考时须预填空思考频道，
        # 见 PromptBuilder 注释；按所选模型的注册表条目由 app.py 传入。
        self.builder = PromptBuilder(processor, suppress_ghost_thinking=suppress_ghost_thinking, env=env)
        self.parser = ResponseParser(processor)

        # KV Cache 量化配置（默认关闭）：kv_bits 为 None 时走稠密缓存，
        # 与未配置时行为完全一致；设为小数（如 3.5）则底层 generate_step 启用 TurboQuant。
        kv_config = kv_config or {}
        self.kv_bits = kv_config.get("kv_bits", None)
        self.kv_quant_scheme = kv_config.get("kv_quant_scheme", "turboquant")
        self.quantized_kv_start = int(kv_config.get("quantized_kv_start", 0))

        # [Phase 2] 工具调用段约束采样（llguidance schema mask）。
        # 懒构建：LLTokenizer ~1s，首次带 tools 的生成时建；缺库/建失败 → 永久禁用（fail-open）。
        self.constrained_tool_calls = bool(kv_config.get("constrained_tool_calls", False))
        self._constraint_engine = None
        self._constraint_disabled = _llg is None
        if self.constrained_tool_calls and self._constraint_disabled:
            print("[Engine] constrained_tool_calls=true 但 llguidance 不可用，约束采样禁用")

        # [Phase 3] prompt-lookup 投机采样配置（mode=off 时不带 kwargs，路径与改动前一致）
        spec_cfg = kv_config.get("speculative") or {}
        self.speculative = dict(spec_cfg) if spec_cfg.get("mode", "off") != "off" else None
        if self.speculative:
            print(f"[Engine] 投机采样启用：{self.speculative}")

        # 推理互斥锁：底层 stream_generate 依赖共享可变单例
        # （processor.detokenizer、tokenizer.stopping_criteria/thinking_budget_criteria、
        # 结尾的 mx.clear_cache 等），并发请求逐 token 交错执行会互相污染文本流。
        # 因此 GPU 生成段必须串行；等待方挂起在锁上不阻塞事件循环，
        # 其他请求的文件解析、历史查询、分支等非推理任务仍可并发。
        self._inference_lock = asyncio.Lock()

        # 专用推理线程（单线程执行器）：MLX 生成是同步计算,放在事件循环线程会把
        # 整个 API 饿死(prefill 一整块数秒内连 /health 都无响应——Phase 1 实机定性)。
        # 生成整段搬进本线程,事件循环全程自由;token 增量经 call_soon_threadsafe 回主
        # 循环写账本。单线程 + 推理锁双保险:所有生成/摘要串行,MLX 单例不被并发触碰。
        # 流的线程适配:补丁层 generation_stream 为 thread-local 流(见 patch 注释),
        # 生成在执行器线程各自成流。标准路径经 create() 传入已承载模型加载的执行器
        # (推理宇宙同线程);直接构造(测试/脚本)时自建——注意直接构造 + 主线程
        # 已加载的真模型仍有跨线程惰性常量风险,生产装配一律走 create()。
        self._inference_executor = _executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mlx-inference")

    async def generate_once(self,
                            session: ChatSession,
                            tools: Optional[List[Dict[str, Any]]] = None,
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
        formatted_prompt = self.builder.build(
            messages=session.history,
            tools=tools,
            enable_thinking=enable_thinking,
            task_block=task_block,
            tail_notes=tail_notes,
            # 环境事实只注入主编排会话；沙箱一次性会话替身默认 False（隐私边界，opt-in）
            include_env=getattr(session, "inject_env_context", False),
        )
        # 思考播种用 parser 的实际 token（而非硬编码字面量）：换模型/token 表时保持一致
        generated_text = f"{self.parser.soc}thought\n" if enable_thinking else ""
        metrics: Dict[str, Any] = {}
        cancelled = False

        # 真流式过滤器：把原始 token 流增量过滤成可见正文（思考/工具块不流出正文,
        # 但分别经 on_thinking_delta 直播、on_tool_pending 预告——Phase 3 粒度钩子）。
        # enable_thinking 时 prompt 收尾在已打开的 thought 频道内，故以抑制态起步。
        # 钩子回调在推理线程触发,这里包一层 call_soon_threadsafe 回主循环(账本纪律)。
        streamer = None
        detok = None
        prev_decoded = ""
        _loop_for_hooks = asyncio.get_running_loop()

        def _hop(cb: Optional[Callable[[str], None]]) -> Optional[Callable[[str], None]]:
            if cb is None:
                return None
            return lambda s: _loop_for_hooks.call_soon_threadsafe(cb, s)

        if on_text_delta is not None or on_thinking_delta is not None or on_tool_pending is not None:
            streamer = VisibleTextStreamer(
                soc=self.parser.soc, eoc=self.parser.eoc,
                stc=self.parser.stc, etc=self.parser.etc,
                in_thought=enable_thinking,
                on_thinking=_hop(on_thinking_delta),
                on_tool_pending=_hop(on_tool_pending),
            )
            # 增量取字直接盯 detokenizer 的当前解码全文（text + 未 flush 缓冲）做差，而非
            # result.text（=last_segment）：SPM 分词器对无空格语言（中文）会把文本攒在
            # _unflushed 里、到 ▁ 词边界或 finalize 才 flush，导致 last_segment 中途恒空、
            # 整段答复在结尾一次性吐出。盯 text+_unflushed 可让中文也逐 token 出。
            # 该 detokenizer 与底层 stream_generate 用的是同一实例（processor.detokenizer）。
            detok = self.processor.detokenizer
            if not (hasattr(detok, "text") and hasattr(detok, "_unflushed")):
                detok = None  # 非 SPM 类 detokenizer：退回 last_segment 增量

        # KV 量化参数仅在启用时才透传给底层 generate_step；
        # 关闭（kv_bits=None）时不带这些 kwargs，稠密路径与改动前完全一致
        kv_kwargs = {}
        if self.kv_bits is not None:
            kv_kwargs = {
                "kv_bits": self.kv_bits,
                "kv_quant_scheme": self.kv_quant_scheme,
                "quantized_kv_start": self.quantized_kv_start,
            }

        # [Phase 2] 工具调用段约束采样：每次生成一个新 guard（状态机），
        # 经 generate_step 原生的 logits_processors 钩子挂载；未启用时零开销。
        guard = self._tool_guard(tools)
        if guard is not None:
            kv_kwargs["logits_processors"] = [guard]

        # [Phase 3] 投机采样：透传配置 + 接受率统计回传容器
        spec_stats = None
        if self.speculative:
            spec_stats = {"drafted": 0, "accepted": 0, "rounds": 0}
            kv_kwargs["speculative"] = self.speculative
            kv_kwargs["spec_stats"] = spec_stats

        # 整段生成搬进专用推理线程（事件循环全程自由:API/心跳/事件流不再被计算饿死）。
        # 推理锁按单次生成粒度持有（工具调用等 I/O 期间由上层让出,供其他会话推理）;
        # 可见增量经 call_soon_threadsafe 回主循环（账本 emit 必须在循环线程,FIFO 保序）。
        loop = asyncio.get_running_loop()

        def _blocking_generate() -> Dict[str, Any]:
            gen_text = ""
            gen_metrics: Dict[str, Any] = {}
            gen_cancelled = False
            prev = prev_decoded
            for result in stream_generate(
                model=self.model,
                processor=self.processor,
                prompt=formatted_prompt,
                image=session.images or None,
                audio=session.audios or None,
                temperature=temperature,
                prompt_cache_state=session.brain_memory_state,
                enable_thinking=enable_thinking,
                thinking_budget=thinking_budget,
                # 预算锚点须是本模型的频道 token：不传则底层默认 <think>/</think>（Qwen
                # 方言，Gemma-4 词表中不存在），预算计数失准、超限还会强插垃圾字面量。
                # 锚定 soc/eoc 后，超限强制收尾即为 "\n<channel|>"——方言内干净关频道。
                thinking_start_token=self.parser.soc,
                thinking_end_token=self.parser.eoc,
                max_tokens=max_tokens,
                min_p=min_p,
                repetition_penalty=1.1,
                repetition_context_size=50,
                **kv_kwargs,
            ):
                gen_text += result.text
                gen_metrics = {
                    "prompt_tokens": result.prompt_tokens,
                    "generation_tokens": result.generation_tokens,
                    "prompt_tps": result.prompt_tps,
                    "generation_tps": result.generation_tps,
                    "peak_memory": result.peak_memory
                }
                # 真流式：取本 token 的可见文本增量 → 过滤成可见正文 → 回吐主循环
                if streamer is not None:
                    if detok is not None:
                        # 盯 detokenizer 当前解码全文做差（▁ 归一为空格，保证前缀单调延伸）
                        cur = (detok.text + detok._unflushed).replace("▁", " ")
                        if cur.startswith(prev):
                            delta = cur[len(prev):]
                            if delta:
                                visible = streamer.feed(delta)
                                if visible and on_text_delta is not None:
                                    loop.call_soon_threadsafe(on_text_delta, visible)
                        # trim_space 反常重排等情形跳过一次增量、只推进基线，避免重复回吐
                        prev = cur
                    else:
                        visible = streamer.feed(result.text)
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
            return {"text": gen_text, "metrics": gen_metrics, "cancelled": gen_cancelled}

        async with self._inference_lock:
            outcome = await loop.run_in_executor(self._inference_executor, _blocking_generate)
        generated_text += outcome["text"]
        metrics = outcome["metrics"] or metrics
        cancelled = outcome["cancelled"]

        if spec_stats and spec_stats.get("drafted"):
            metrics["spec_drafted"] = spec_stats["drafted"]
            metrics["spec_accepted"] = spec_stats["accepted"]

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

        parsed = self.parser.parse(generated_text)
        # 截断检测（供上层 max_tokens 恢复用）：底层 stream_generate 遇 EOS/停止符会 break、
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
        """
        if not self.constrained_tool_calls or self._constraint_disabled or not tools:
            return None
        try:
            if self._constraint_engine is None:
                tok = (self.processor.tokenizer
                       if hasattr(self.processor, "tokenizer") else self.processor)
                stc = tok.encode(self.parser.stc, add_special_tokens=False)
                etc = tok.encode(self.parser.etc, add_special_tokens=False)
                if len(stc) != 1 or len(etc) != 1:
                    raise RuntimeError("工具调用标记不是单 token，约束状态机无法锚定")
                self._constraint_engine = ToolConstraintEngine(tok, stc[0], etc[0])
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
        ch_pat = re.compile(
            rf"{re.escape(self.parser.soc)}(.*?){re.escape(self.parser.eoc)}", re.DOTALL)
        for m in ch_pat.finditer(generated_text):
            parts = re.split(r"\s+", m.group(1).strip(), maxsplit=1)
            body = parts[1] if len(parts) == 2 else ""
            if parts and parts[0] == "thought":
                thinking += enc(body)
            else:
                visible += enc(body)
        rest = ch_pat.sub("", generated_text)
        tool_pat = re.compile(
            rf"{re.escape(self.parser.stc)}(.*?){re.escape(self.parser.etc)}", re.DOTALL)
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
        "If [Existing summary] is provided, merge [New conversation] into it and output the "
        "updated complete summary without losing still-valid information from the old one. "
        "Output only the summary body — no explanations, no preamble or suffix."
    )

    async def summarize(self, content: str, prior_summary: str = "",
                        max_tokens: int = 512, temperature: float = 0.3,
                        cancel_check=None) -> str:
        """
        对一段对话历史做要点摘要（摘要式压缩）。一次性裸生成：不带工具、不复用会话 KV
        （用全新的 PromptCacheState），与任何会话的推理状态隔离。持有推理锁保证与生成串行。
        cancel_check：可选无参回调，逐 token 段轮询；返回真则中断生成并返回空串
        （调用侧把空摘要视为失败、水位线不推进、下次触发重试——调度器抢占后台摘要靠它让位）。
        """
        parts = []
        if prior_summary.strip():
            parts.append("[Existing summary]\n" + prior_summary.strip())
        parts.append("[New conversation]\n" + content.strip())
        messages = [
            {"role": "system", "content": self.SUMMARY_INSTRUCTION},
            {"role": "user", "content": "\n\n".join(parts)},
        ]
        prompt = self.builder.build(messages=messages, tools=None, enable_thinking=False)

        fresh_cache = PromptCacheState()  # 隔离缓存：不碰任何会话的 KV
        # [Phase 3] 摘要也走投机：合并旧摘要时大段复述 [Existing summary]，命中率天然高
        spec_kwargs = {"speculative": self.speculative} if self.speculative else {}
        loop = asyncio.get_running_loop()

        def _blocking_summarize() -> str:
            text = ""
            for result in stream_generate(
                model=self.model,
                processor=self.processor,
                prompt=prompt,
                image=None,
                audio=None,
                temperature=temperature,
                prompt_cache_state=fresh_cache,
                enable_thinking=False,
                max_tokens=max_tokens,
                min_p=0.05,
                repetition_penalty=1.1,
                repetition_context_size=50,
                **spec_kwargs,
            ):
                if cancel_check is not None and cancel_check():
                    return ""  # 被抢占：放弃本次摘要（无半途状态，下次触发重试）
                text += result.text
            return text

        async with self._inference_lock:
            text = await loop.run_in_executor(self._inference_executor, _blocking_summarize)
        if not text:
            return ""
        parsed = self.parser.parse(text)
        return (parsed.get("text") or text).strip()
