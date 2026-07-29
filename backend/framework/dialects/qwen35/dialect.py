"""Qwen3.5 家族方言（多模型接口准备波）：ChatML 模板 + XML 式工具调用体。

覆盖 Qwen3.5 与其衍生档 Ornith-1.0（架构同为 Qwen3_5ForConditionalGeneration，
chat template 逐行 diff 仅一处：思考回放范围——本方言取 Ornith 的"恒回放"口径，
对 Qwen3.5 是良性超集，且历史字节稳定对 KV 前缀友好）。

与 gemma4 方言的两点结构差异：
- markers 是**家族字面量**（<think>/</think>/<tool_call>/</tool_call>，两模型
  tokenizer 实测全部单 token），不经 special_tokens_map 取名。
- `tool_call_schema` 抛 NotImplementedError：本家族调用体是 XML 非 JSON，
  core/constrained 的 JSON-schema 约束不适用——engine._tool_guard 捕获后走
  fail-open（打印"永久禁用"一次），解析纠错闭环兜底。这是**方言事实**不是故障；
  将来若要约束 XML 需扩展 core/constrained 的语法面（另行成波）。
"""
from typing import Any, Dict, List, Optional

from ...core.dialect_protocol import DialectMarkers
from .prompt_builder import Qwen35PromptBuilder, THINK_OPEN, THINK_CLOSE
from .response_parser import Qwen35ResponseParser


class Qwen35Dialect:
    """ModelDialect 的 Qwen3.5 家族实现。构造后只读，可被多次生成共享。"""

    @classmethod
    def create(cls, processor, *, options: Optional[Dict[str, Any]] = None,
               env: Optional[Dict[str, Any]] = None) -> "Qwen35Dialect":
        # 本家族无 suppress_ghost_thinking 之类开关：关思考的空 <think> 块预填
        # 恒开（官方模板 enable_thinking=false 的固定形态），无模型级差异
        return cls(processor, env=env)

    def __init__(self, processor, *, env: Optional[Dict[str, Any]] = None):
        self._builder = Qwen35PromptBuilder(processor, env=env)
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

    def parse_response(self, raw: str) -> Dict[str, Any]:
        return self._parser.parse(raw)

    def thinking_prefill(self, enable_thinking: bool) -> str:
        # 开思考：prompt 尾在已打开的 <think> 内，生成种子与之字节一致。
        # 关思考：空 <think> 块在 build 里已随 prompt 预填，种子为空（gemma4 同构）。
        return f"{THINK_OPEN}\n" if enable_thinking else ""

    def tool_call_schema(self, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        raise NotImplementedError(
            "qwen35 方言使用原生 XML 调用体（<function=…><parameter=…>），按设计不启用 "
            "JSON 约束采样——engine 将 fail-open 到自由采样 + 解析纠错闭环。")
