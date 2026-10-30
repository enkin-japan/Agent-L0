"""Qwen3.5 家族方言（多模型接口准备波）：ChatML 模板 + XML 式工具调用体。

覆盖 Qwen3.5 与其衍生档 Ornith-1.0（架构同为 Qwen3_5ForConditionalGeneration，
chat template 逐行 diff 仅一处：思考回放范围——本方言取 Ornith 的"恒回放"口径，
对 Qwen3.5 是良性超集，且历史字节稳定对 KV 前缀友好）。

与 gemma4 方言的两点结构差异：
- markers 是**家族字面量**（<think>/</think>/<tool_call>/</tool_call>，两模型
  tokenizer 实测全部单 token），不经 special_tokens_map 取名。
- `tool_call_grammar` 抛 NotImplementedError：本家族调用体是 XML，既非 JSON 也非
  gemma-4 的紧凑 DSL——engine._tool_guard 捕获后走 fail-open（打印"永久禁用"一次）；
  inference/server 在启动探针里据此整段禁用（启动日志一行，不建引擎、不拒启）。
  解析纠错闭环兜底。这是**方言事实**不是故障；§7-8 已把 core/constrained 的语法面
  拓宽到 Lark，将来要约束 XML 在此实现一个 Lark 语法即可，不必再改 core。
"""
from typing import Any, Dict, List, Optional

from ...core.dialect_protocol import DialectMarkers, ToolCallGrammar
from .prompt_builder import Qwen35PromptBuilder, THINK_OPEN, THINK_CLOSE
from .response_parser import Qwen35ResponseParser


class Qwen35Dialect:
    """ModelDialect 的 Qwen3.5 家族实现。构造后只读，可被多次生成共享。"""

    @classmethod
    def create(cls, processor, *, options: Optional[Dict[str, Any]] = None,
               env: Optional[Dict[str, Any]] = None) -> "Qwen35Dialect":
        # 本家族无 suppress_ghost_thinking 之类开关：关思考的空 <think> 块预填
        # 恒开（官方模板 enable_thinking=false 的固定形态），无模型级差异。
        # reasoning_effort（Qwen3.8 官方旋钮，2026-08-22 透传波）：注册表条目级。
        # 不设键/空值 = 恒不注入（零行为变化，官方 medium 同形）；显式 xhigh/low
        # 才注入且**跨迭代恒定**（与 enable_thinking 解耦——门控语义见
        # prompt_builder 模块头差异 4，评审 C1-C3 的处置）。非法值在 builder
        # 构造时装配期拒启（注意：装配在权重加载之后，配错仍要付一次装载，
        # 评审 S6 已知代价——预检前移要动 model_selection 层，不为此扩面）。
        raw = (options or {}).get("reasoning_effort")
        effort = None if raw in (None, "") else str(raw)
        return cls(processor, env=env, reasoning_effort=effort)

    def __init__(self, processor, *, env: Optional[Dict[str, Any]] = None,
                 reasoning_effort: Optional[str] = None):
        self.reasoning_effort = reasoning_effort   # 观测面：评测/轨迹归因用
        self._builder = Qwen35PromptBuilder(processor, env=env,
                                            reasoning_effort=reasoning_effort)
        self._parser = Qwen35ResponseParser(processor)
        self._markers = DialectMarkers(soc=self._parser.soc, eoc=self._parser.eoc,
                                       stc=self._parser.stc, etc=self._parser.etc)

    @property
    def markers(self) -> DialectMarkers:
        return self._markers

    def build_prompt(self, messages: List[Dict[str, Any]],
                     tools: Optional[List[Dict[str, Any]]] = None,
                     enable_thinking: bool = False,
                     task_block: Optional[str] = None,
                     tail_notes: Optional[List[str]] = None,
                     include_env: bool = False,
                     env_extra: Optional[List[str]] = None) -> str:
        return self._builder.build(messages=messages, tools=tools,
                                   enable_thinking=enable_thinking,
                                   task_block=task_block, tail_notes=tail_notes,
                                   include_env=include_env, env_extra=env_extra)

    def build_prompt_delta(self, new_messages, *, enable_thinking=False,
                           task_block=None, tail_notes=None, include_env=False,
                           env_extra=None, prev_tail=None):
        # 上位解增量段：字节权威在 builder（与 build_prompt 同一处），此处纯转发
        return self._builder.build_prompt_delta(
            new_messages, enable_thinking=enable_thinking, task_block=task_block,
            tail_notes=tail_notes, include_env=include_env, env_extra=env_extra,
            prev_tail=prev_tail)

    def tail_state(self, task_block=None, include_env=False, env_extra=None):
        return self._builder.tail_state(task_block=task_block,
                                        include_env=include_env, env_extra=env_extra)

    def parse_response(self, raw: str) -> Dict[str, Any]:
        return self._parser.parse(raw)

    def thinking_prefill(self, enable_thinking: bool) -> str:
        # 开思考：prompt 尾在已打开的 <think> 内，生成种子与之字节一致。
        # 关思考：空 <think> 块在 build 里已随 prompt 预填，种子为空（gemma4 同构）。
        return f"{THINK_OPEN}\n" if enable_thinking else ""

    def thinking_preopened(self, enable_thinking: bool) -> bool:
        # 开思考时生成从 <think> 内部起步、块无频道名：首个 </think> 前全是思考
        return bool(enable_thinking)

    def tool_call_grammar(self, tools: List[Dict[str, Any]]) -> ToolCallGrammar:
        raise NotImplementedError(
            "qwen35 方言使用原生 XML 调用体（<function=…><parameter=…>），按设计不启用 "
            "约束采样——engine 将 fail-open 到自由采样 + 解析纠错闭环。")

    def render_tool_declaration(self, tool: Dict[str, Any]) -> str:
        # D0①(复审 S15):本方言前缀里工具声明是 <tools> 内每行一个 JSON 对象——
        # 取详情按同形回喂(数组形 json.dumps(found) 连自家前缀都不像)
        import json as _json
        return _json.dumps(tool, ensure_ascii=False)

    def parse_error_retry_hint(self) -> str:
        # 与 prompt_builder._TOOL_FORMAT_GUIDE 的教学格式锚定一致：本方言调用体是 XML 非 JSON
        return (
            "A tool call must be EXACTLY the XML form you were taught — one complete block:\n"
            "<tool_call>\n<function=example_function_name>\n"
            "<parameter=example_parameter_1>\nvalue_1\n</parameter>\n"
            "</function>\n</tool_call>\n"
            "Do NOT use JSON, do NOT nest a call inside your thinking, and do NOT omit the "
            "closing tags. Retry with ONE complete, correctly-formatted call."
        )
