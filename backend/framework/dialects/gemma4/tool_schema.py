"""gemma-4 方言的工具调用体 schema（R0 从 core/constrained.py 收编，逻辑一行未改）。

**为什么归方言**：本 schema 锚定的是 prompt_builder 回放给模型看的调用体格式
（`{"type": "function", "name": <tool>, "arguments": {…}}`，type 可省略）——这是
**方言事实**，不是约束机制的一部分。core/constrained.py 的状态机（起止标记夹一段
JSON、llguidance 做 logits mask）框架无关且方言无关，故留在 core，只由方言喂参数。

⚠️ 改动本文件前先看 prompt_builder._build_tool_calls：**两者必须锚定同一格式**。
schema 与回放格式一旦漂移，约束采样会把模型逼成它自己历史里没见过的形状。
"""
from typing import Any, Dict, List


def build_tool_call_schema(tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把注册工具的声明编成调用体 JSON schema（anyOf 按工具分支）。

    锚定 prompt_builder 回放给模型看的格式：
    {"type": "function", "name": <tool>, "arguments": {…}}（type 可省略）。
    """
    branches = []
    for t in tools:
        params = t.get("parameters") or {}
        arg_schema: Dict[str, Any] = {"type": "object"}
        props = params.get("properties") or {}
        if props:
            # 剥掉与 JSON 校验无关的键（description 等保留无害，但裁剪可缩小语法）
            arg_schema["properties"] = props
        required = params.get("required") or []
        if required:
            arg_schema["required"] = list(required)
        arg_schema["additionalProperties"] = False
        branches.append({
            "type": "object",
            "properties": {
                "type": {"const": "function"},
                "name": {"const": t.get("name")},
                "arguments": arg_schema,
            },
            "required": ["name", "arguments"],
            "additionalProperties": False,
        })
    return {"anyOf": branches} if branches else {"type": "object"}
