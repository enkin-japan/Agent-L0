import asyncio
import re
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
    def __init__(self, model, processor, kv_config=None):
        self.model = model
        self.processor = processor

        self.builder = PromptBuilder(processor)
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
                            on_text_delta: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        """
        执行一次完整生成并解析输出。

        - tools: 工具声明（稳定前缀的一部分，由 Orchestrator 启动时构建并保持不变）
        - task_block: 任务状态块（可变区，追加在 prompt 最尾部，见前缀分层设计）
        - 取消：逐 token 段检查 session.cancel_requested，命中即中断生成返回
          cancelled=True（丢弃的部分生成由下次推理的 KV 前缀匹配自动裁剪）
        - on_text_delta: 真流式回调（可选）。每产出一段**用户可见正文**即同步调用一次
          （思考/工具调用区块经 VisibleTextStreamer 过滤，不会流出）。为 None 时行为与
          改动前完全一致——攒全文、末尾一次性 parse，终态仍走原路（保证 turn_end 权威）。
        """
        formatted_prompt = self.builder.build(
            messages=session.history,
            tools=tools,
            enable_thinking=enable_thinking,
            task_block=task_block,
            tail_notes=tail_notes,
        )
        # 思考播种用 parser 的实际 token（而非硬编码字面量）：换模型/token 表时保持一致
        generated_text = f"{self.parser.soc}thought\n" if enable_thinking else ""
        metrics: Dict[str, Any] = {}
        cancelled = False

        # 真流式过滤器：把原始 token 流增量过滤成可见正文（思考/工具块不流出）。
        # enable_thinking 时 prompt 收尾在已打开的 thought 频道内，故以抑制态起步。
        streamer = None
        detok = None
        prev_decoded = ""
        if on_text_delta is not None:
            streamer = VisibleTextStreamer(
                soc=self.parser.soc, eoc=self.parser.eoc,
                stc=self.parser.stc, etc=self.parser.etc,
                in_thought=enable_thinking,
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

        # 整段生成持有推理锁串行执行（锁按单次生成粒度持有，
        # 工具调用等网络 I/O 期间由上层让出，供其他会话推理）
        async with self._inference_lock:
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
                max_tokens=max_tokens,
                min_p=min_p,
                repetition_penalty=1.1,
                repetition_context_size=50,
                **kv_kwargs,
            ):
                generated_text += result.text
                metrics = {
                    "prompt_tokens": result.prompt_tokens,
                    "generation_tokens": result.generation_tokens,
                    "prompt_tps": result.prompt_tps,
                    "generation_tps": result.generation_tps,
                    "peak_memory": result.peak_memory
                }
                # 真流式：取本 token 的可见文本增量 → 过滤成可见正文 → 回吐（同步、不阻塞）
                if streamer is not None:
                    if detok is not None:
                        # 盯 detokenizer 当前解码全文做差（▁ 归一为空格，保证前缀单调延伸）
                        cur = (detok.text + detok._unflushed).replace("▁", " ")
                        if cur.startswith(prev_decoded):
                            delta = cur[len(prev_decoded):]
                            if delta:
                                visible = streamer.feed(delta)
                                if visible:
                                    on_text_delta(visible)
                        # trim_space 反常重排等情形跳过一次增量、只推进基线，避免重复回吐
                        prev_decoded = cur
                    else:
                        visible = streamer.feed(result.text)
                        if visible:
                            on_text_delta(visible)
                # 取消检查点（token 段粒度）：立即停止消耗生成流
                if session.cancel_requested:
                    cancelled = True
                    break
                # 释放事件循环：非推理协程（上传解析、历史/分支接口）保持响应
                await asyncio.sleep(0)

        # 冲刷过滤器尾部（未闭合频道的残留按语义丢弃）
        if streamer is not None and not cancelled:
            tail = streamer.close()
            if tail:
                on_text_delta(tail)

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
        text = ""
        async with self._inference_lock:
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
                await asyncio.sleep(0)
        parsed = self.parser.parse(text)
        return (parsed.get("text") or text).strip()
