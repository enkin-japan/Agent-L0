"""Qwen3.5 家族输出反序列化：<think> 思考块 + <tool_call> XML 调用体。

与 gemma4/response_parser 同一铁律级不变量（test_response_parser 思想的 qwen35 版）：
**标记残缺的任何事故形态都不得把垃圾漏进正文**——有头无尾/有尾无头的工具调用一律
转 error 调用走纠错闭环；未闭合的 <think> 整段路由进 thoughts；error 的 raw_input
必须截断（会回显给模型并进账本）。

调用体格式（与 prompt_builder 回放格式锚定一致）：
  <tool_call><function=NAME><parameter=K>V</parameter>…</function></tool_call>
参数值类型还原（D46）：**有 schema 就按 schema 还原，没有 schema 就原文不动。**
调用方（engine.generate_once）把本轮的完整工具 schema（grammar_tools 优先）传进
parse；参数声明为 object/array/integer/number/boolean 的按声明类型还原（官方模板对
非字符串值 tojson 写入，读回对称），string 型与查不到声明的参数一个字节都不动——
多行字符串正是该格式相对 JSON 的全部优势，绝不强转。
为什么不看形态：旧的 `_coerce` 只凭「首字符 { / [、整值 true/null、数字形态」就
json.loads，string 型参数（write_file.content、edit_file.new_string）被还原成
dict/list/int/bool，file_edit 再 str() 落盘成 Python repr（单引号 / True / None）；
模型上下文里看到的却是 prompt_builder 重新 json.dumps 的「正确」JSON，发现不了
（lc-hard-009 写 JSON 文件 13 个 Qwen 臂 0 过、Gemma 18/18）。
"""
import json
import re
from typing import Any, Dict, List, Optional

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


# M1：加 re.ASCII——\d 只认 0-9；非 ASCII 数字（阿拉伯数字 "٣"、全角 "３"）一律
# 走不匹配分支原文返回，不被当成数字吞掉。
# M2：integer 的正则额外容忍 "5.0" / "-7.000" 这种小数点全是 0 的形态（上游 tojson
# 整数常见写法）；第二组用 split(".")[0] 取整数部分转 int，不经 float 以避免大整数
# 精度损失。"5.5" / "5." 不匹配，原文返回。
_INT_RE = re.compile(r"-?\d+(\.0+)?", re.ASCII)
_NUM_RE = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", re.ASCII)
_NULL_LITERALS = ("null", "None")


def _coerce_scalar(v: str, t: str, raw: str) -> Any:
    """按单一 type（已 strip 过的 v）还原；还原不了原样返回 raw。

    raw 必须是调用方传入的原对象、失败路径原封不动传回它——调用方靠
    `result is not raw` 判断本次类型尝试是否命中（成功结果恒是非字符串的
    bool/int/float/dict/list/None，与字符串 raw 天然不是同一对象）。
    - string → raw；
    - boolean：true/True → True，false/False → False，否则 raw。Python 风格
      True/False 必须收：2026-07-30 评测框架实测 Qwen3.5 家族惯性发射 'True'；
    - integer / number：见 _INT_RE / _NUM_RE 的规则（M1 ASCII、M2 容忍 "5.0"）；
    - object / array：首字符 { / [ 且 json.loads 成功且结果是 dict / list；
    - 未知 type → raw。不做嵌套还原：object/array 内部类型由 json.loads 决定。
    """
    if t == "string":
        return raw
    if t == "boolean":
        if v in ("true", "True"):
            return True
        if v in ("false", "False"):
            return False
        return raw
    if t == "integer":
        return int(v.split(".")[0]) if _INT_RE.fullmatch(v) else raw
    if t == "number":
        m = _NUM_RE.fullmatch(v)
        if not m:
            return raw
        return int(v) if m.group(1) is None and m.group(2) is None else float(v)
    if t in ("object", "array"):
        opener, kind = ("{", dict) if t == "object" else ("[", list)
        if not v.startswith(opener):
            return raw
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            return raw
        return parsed if isinstance(parsed, kind) else raw
    return raw


def _coerce_typed(value: str, prop_schema: Optional[Dict[str, Any]]) -> Any:
    """按参数 schema 还原单个参数值；还原不了一律返回原文（让工具层的校验报错）。

    规则（D46 + 一审修补 M1-M3，见模块 docstring 的「为什么」）：
    - prop_schema 为 None（工具/参数无声明、调用方未传 tools）→ 原文，不 strip；
    - 取 types 列表：
      a. type 为字符串 → [type]；
      b. type 为列表（用户自注册 API 工具可原样带联合类型）→ 原列表；
      c. type 缺失（a/b 都不是）但带 anyOf 或 oneOf（dict 列表）→ 取各分支的
         type：分支 type 为 str 展开、为 list 并入、缺 type 的分支忽略；anyOf
         与 oneOf 同时出现时只取先出现的那个键；
      d. 以上都没有 → 原文；
    - types 含 "string" → 原文，一个字节都不动（多行、首尾换行、看着像 JSON 都保留）；
    - 非 string 型：strip 后为 null/None → None；
    - 否则按 types 顺序逐个尝试 `_coerce_scalar`，第一个命中（返回值不是原 value
      本身）的就是结果；全部未命中 → 原文；
    - 单一类型的还原细则见 `_coerce_scalar`。
    """
    if prop_schema is None:
        return value
    t = prop_schema.get("type")
    if isinstance(t, str):
        types: List[str] = [t]
    elif isinstance(t, list):
        types = t
    else:
        types = []
        for key in ("anyOf", "oneOf"):
            branches = prop_schema.get(key)
            if isinstance(branches, list):
                for branch in branches:
                    if not isinstance(branch, dict):
                        continue
                    bt = branch.get("type")
                    if isinstance(bt, str):
                        types.append(bt)
                    elif isinstance(bt, list):
                        types.extend(bt)
                break
        if not types:
            return value
    if "string" in types:
        return value
    v = value.strip()
    if v in _NULL_LITERALS:
        return None
    for one in types:
        result = _coerce_scalar(v, one, value)
        if result is not value:
            return result
    return value


def _properties_index(tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """{工具名: properties}。tools 为扁平 schema 列表（mcp/base.py get_schema 形态）。"""
    index: Dict[str, Dict[str, Any]] = {}
    for tool in tools or ():
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        params = tool.get("parameters")
        props = params.get("properties") if isinstance(params, dict) else None
        index[tool["name"]] = props if isinstance(props, dict) else {}
    return index


class Qwen35ResponseParser:
    """构造后只读。soc/eoc/stc/etc 为家族字面量事实（tokenizer 实测单 token）。"""

    def __init__(self, processor=None):
        self.soc = THINK_OPEN
        self.eoc = THINK_CLOSE
        self.stc = TOOL_CALL_OPEN
        self.etc = TOOL_CALL_CLOSE

    def parse(self, raw_response: str,
              tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """tools：本轮完整工具 schema（扁平列表）。None / 空列表 = 无 schema，所有参数
        保持原文（D46：宁可让工具层校验打回，也不凭形态猜类型写坏 string 参数）。"""
        result: Dict[str, Any] = {"thoughts": [], "text": "", "tool_calls": []}
        props_by_tool = _properties_index(tools)

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
            props = props_by_tool.get(name, {})
            args: Dict[str, Any] = {}
            for pm in _PARAM_RE.finditer(fm.group(2)):
                prop = props.get(pm.group(1))
                args[pm.group(1)] = _coerce_typed(
                    pm.group(2), prop if isinstance(prop, dict) else None)
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
