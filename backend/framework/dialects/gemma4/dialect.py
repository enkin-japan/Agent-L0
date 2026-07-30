"""gemma-4 方言（EXECUTION_LEDGER R0 收编）：频道式模板 + <|tool_call> JSON 调用体。

组成（全部在本目录内，从 core/ 原样搬来，**内部逻辑一行未改**）：
- `prompt_builder.PromptBuilder`      模板组装（原 core/prompt_builder.py）
- `response_parser.ResponseParser`    输出反序列化（原 core/response_parser.py）
- `tool_schema.build_tool_call_schema` 约束采样的调用体 schema（原 core/constrained.py）

本类只做一件事：把三者组装成 `core.dialect_protocol.ModelDialect` 的实现。
它不含任何新逻辑——R0 的验收标准是「行为零变化」，任何"顺便优化"都会让这个
唯一可信的标准失效。

**方言标记的来源**：markers 取自 ResponseParser 的属性。此前引擎直接读
`self.parser.soc/eoc/stc/etc`——parser 事实上兼任了方言标记登记处。R0 把这个
隐式职责显式化为 `dialect.markers`，取值来源与结果完全不变。

**gemma-4 家族覆盖**：e4b / 12b / 26b-a4b(MoE) / 31b 共用本方言；大杯（12b/26b/31b）
关思考时须预填空思考频道（幽灵思考抑制），经注册表条目的 `suppress_ghost_thinking`
开关传入——那是**模型级方言事实**，故解读责任在此，不在 AgentEngine。
"""
from typing import Any, Dict, List, Optional

from ...core.dialect_protocol import DialectMarkers
from .prompt_builder import PromptBuilder
from .response_parser import ResponseParser
from .tool_schema import build_tool_call_schema


class Gemma4Dialect:
    """ModelDialect 的 gemma-4 实现。构造后只读，可被多次生成共享。"""

    @classmethod
    def create(cls, processor, *, options: Optional[Dict[str, Any]] = None,
               env: Optional[Dict[str, Any]] = None) -> "Gemma4Dialect":
        options = options or {}
        return cls(processor,
                   suppress_ghost_thinking=bool(options.get("suppress_ghost_thinking", False)),
                   env=env)

    def __init__(self, processor, *, suppress_ghost_thinking: bool = False,
                 env: Optional[Dict[str, Any]] = None):
        self._builder = PromptBuilder(processor,
                                      suppress_ghost_thinking=suppress_ghost_thinking,
                                      env=env)
        self._parser = ResponseParser(processor)
        # 标记的单一来源：parser 从 tokenizer 的 special_tokens_map 取（缺失时用默认字面量）
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
        # 用方言的实际 token（而非硬编码字面量）：换模型/token 表时保持一致
        return f"{self._markers.soc}thought\n" if enable_thinking else ""

    def tool_call_schema(self, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        return build_tool_call_schema(tools)

    def parse_error_retry_hint(self) -> str:
        # 原编排层硬编码文案逐字搬入（行为零变化）：本方言调用体是 JSON
        return (
            "A tool call must be VALID JSON with double quotes — the exact same format as "
            "the tool calls you already made successfully. For example, to update the plan:\n"
            '{"name": "update_plan", "arguments": {"op": "replace", "steps": ["step one", "step two"]}}\n'
            "Do NOT use single quotes, a 'call:' prefix, or bare braces. Retry with correct JSON."
        )
