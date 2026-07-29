"""Qwen3.5 家族输出反序列化：<think> 思考块 + <tool_call> XML 调用体。

与 gemma4/response_parser 同一铁律级不变量（test_response_parser 思想的 qwen35 版）：
**标记残缺的任何事故形态都不得把垃圾漏进正文**——有头无尾/有尾无头的工具调用一律
转 error 调用走纠错闭环；未闭合的 <think> 整段路由进 thoughts；error 的 raw_input
必须截断（会回显给模型并进账本）。

调用体格式（与 prompt_builder 回放格式锚定一致）：
  <tool_call><function=NAME><parameter=K>V</parameter>…</function></tool_call>
参数值类型还原：JSON 对象/数组/数字/布尔/null 尝试 json.loads（官方模板对非字符串
值 tojson 写入，读回对称）；解析不动或纯字符串保持原文——多行字符串正是该格式
相对 JSON 的全部优势，绝不强转。
"""
import json
import re
from typing import Any, Dict

_ERROR_RAW_MAX = 1500

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"
TOOL_CALL_OPEN = "<tool_call>"
TOOL_CALL_CLOSE = "</tool_call>"

_THINK_RE = re.compile(re.escape(THINK_OPEN) + r"(.*?)" + re.escape(THINK_CLOSE), re.DOTALL)
_TOOL_RE = re.compile(re.escape(TOOL_CALL_OPEN) + r"(.*?)" + re.escape(TOOL_CALL_CLOSE), re.DOTALL)
_FUNC_RE = re.compile(r"<function=([A-Za-z0-9_\-\.]+)>(.*?)</function>", re.DOTALL)
_PARAM_RE = re.compile(r"<parameter=([A-Za-z0-9_\-\.]+)>\n?(.*?)\n?</parameter>", re.DOTALL)
# 残留标记清扫（ChatML 控制 token 不得入正文）
_CTRL_RE = re.compile(r"<\|im_(?:start|end)\|>")


def _cap(raw: str) -> str:
    raw = raw.strip()
    return raw if len(raw) <= _ERROR_RAW_MAX else raw[:_ERROR_RAW_MAX] + " …[truncated]"


def _coerce(value: str) -> Any:
    """参数值类型还原：object/array/number/bool/null 走 JSON；字符串保持原文。

    只在整个值**看起来就是** JSON 字面量时才尝试（首字符判别），避免把普通文本
    （如以数字开头的句子）误交给 json.loads 截头解析。
    """
    v = value.strip()
    if not v:
        return value
    if v[0] in "{[" or v in ("true", "false", "null") \
            or re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", v):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return value
    return value


class Qwen35ResponseParser:
    """构造后只读。soc/eoc/stc/etc 为家族字面量事实（tokenizer 实测单 token）。"""

    def __init__(self, processor=None):
        self.soc = THINK_OPEN
        self.eoc = THINK_CLOSE
        self.stc = TOOL_CALL_OPEN
        self.etc = TOOL_CALL_CLOSE

    def parse(self, raw_response: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {"thoughts": [], "text": "", "tool_calls": []}

        # 1. 闭合思考块 → thoughts
        for m in _THINK_RE.finditer(raw_response):
            t = m.group(1).strip()
            if t:
                result["thoughts"].append(t)
        current = _THINK_RE.sub("", raw_response)

        # 2. 闭合工具调用块 → 解析 XML 调用体
        for m in _TOOL_RE.finditer(current):
            inner = m.group(1).strip()
            fm = _FUNC_RE.search(inner)
            if not fm:
                result["tool_calls"].append({
                    "type": "error",
                    "raw_input": _cap(inner),
                    "error_msg": ("Malformed tool call: expected an inner "
                                  "<function=NAME>...</function> block inside "
                                  "<tool_call></tool_call>."),
                })
                continue
            name = fm.group(1)
            args: Dict[str, Any] = {}
            for pm in _PARAM_RE.finditer(fm.group(2)):
                args[pm.group(1)] = _coerce(pm.group(2))
            result["tool_calls"].append({"type": "function", "name": name, "arguments": args})
        current = _TOOL_RE.sub("", current)

        # 2.5 有头无尾的工具调用（截断/忘写收尾/EOS）：整段转 error 走纠错闭环，
        # 绝不漏进正文（gemma4 §2.5 同款事故形态）
        if self.stc in current:
            idx = current.index(self.stc)
            dangling = current[idx + len(self.stc):].strip()
            result["tool_calls"].append({
                "type": "error",
                "raw_input": _cap(dangling),
                "error_msg": ("Unterminated tool call: the closing </tool_call> is missing — "
                              "your output ended before the call was complete. Re-issue it as "
                              "ONE complete call."),
            })
            current = current[:idx]

        # 2.6 有尾无头的残片（思考预算强插收尾等把调用头吞掉的形态）：转 error
        if self.etc in current:
            idx = current.rindex(self.etc)
            fragment = current[:idx].strip()
            result["tool_calls"].append({
                "type": "error",
                "raw_input": _cap(fragment),
                "error_msg": ("Corrupted tool call: only the TAIL of a call was found (its "
                              "beginning was cut off). The call was NOT executed. Re-issue it "
                              "as ONE complete call — and do not draft tool calls inside "
                              "your thinking."),
            })
            current = current[idx + len(self.etc):]

        # 2.7 未闭合 <think>（思考预算内 EOS/忘写收尾）：整段进 thoughts，不漏正文
        while self.soc in current:
            idx = current.rindex(self.soc)
            dangling = current[idx + len(self.soc):].strip()
            current = current[:idx]
            if dangling:
                result["thoughts"].append(dangling)

        # 3. 兜底正文：游离 </think>（预算强收尾后模型自行补发）与 ChatML 控制标记清除
        clean = _CTRL_RE.sub("", current.replace(self.eoc, "")).strip()
        if clean:
            result["text"] = clean

        result["thoughts"] = "\n".join(result["thoughts"]) if result["thoughts"] else ""
        return result
