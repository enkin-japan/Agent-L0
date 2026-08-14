"""gemma-4 工具协议的官方紧凑 DSL 编码器（§7-8 迁移，2026-08-14）。

本模块是 chat_template.jinja 里 `format_argument` / `format_parameters` /
`format_function_declaration` / `format_tool_response_block` 四个宏的**逐字节移植**。

**为什么单独成模块**：这套编码有三个消费方，必须锚定同一份实现——
  · `prompt_builder`  组装声明/调用/观测三类块（模型看到的历史）；
  · `tool_grammar`    由同一批工具声明推出约束采样的 Lark 语法（模型被允许写的形状）；
  · 差分金测试        与 transformers 渲染的官方模板逐字节对表。
三者各写一遍必然漂移，而漂移的后果正是 §S.17：模型被约束成自己历史里没见过的形状。

**字节协议纪律**（§S.16 的代价换来的）：本文件的每个空白、每个逗号都对表官方模板。
改动前先跑 `backend/tests/test_gemma4_dsl.py`——它拿真 tokenizer 渲染官方 jinja
做基准，不是我方自说自话的期望值。

移植时的两处**有意偏离**，均为官方模板的缺陷兜底（其余逐字节一致）：
  · `parameters` 存在但缺 `type` 时，官方会吐出没有闭合的 `,parameters:{`（花括号
    不平衡）。此处缺省补 OBJECT。我方工具规格恒带 type，正常路径不触发。
  · 官方对 Python `None` 渲染成字面 `None`（jinja 的 `{{ none }}` 行为）。此处同样
    输出 `None` 保持一致——不是笔误，是对表结果。
"""
from typing import Any, Dict, List, Mapping, Sequence

# 原文字符串块的定界符（tokenizer 的 special_tokens_map 里叫 escape_token，单 token）。
# 块内内容整体保真：内部引号/换行/反斜杠都不需要转义——这是 run_python 传多行含引号
# 代码的唯一可靠通道（response_parser 的成对 <|"|> → json.dumps 分支与此对偶）。
ESCAPE = '<|"|>'

#: 定界符在**块内容里**的中和写法。原文块没有转义机制——内容里出现一个真的
#: `<|"|>` 就会当场闭合这个块，后面的内容全部溢出成结构。迁移前这条路是
#: `json.dumps`，它把 `"` 转义成 `\"`、顺带让 `<|"|>` 不再是那个特殊 token；
#: 迁到原文块后这层偶然的保护没了（2026-08-14 首过评审揪出）。
#: 中和后是普通字节（不构成任何特殊 token），人和模型读起来仍是同一个记号。
#: 触发面是真实的：任何工具输出（read_file 读到本文件、抓到的网页、纠错反馈回显
#: 的 raw_input）都可能带它，且工具输出是**外部可控**内容——不中和即为越权闭合
#: 观测块、进而伪造控制标记的注入面。
_ESCAPE_NEUTRAL = '<|\\"|>'


def _raw(text: str) -> str:
    """裹进原文块之前中和定界符（块内无转义机制，见 _ESCAPE_NEUTRAL）。"""
    return f"{ESCAPE}{str(text).replace(ESCAPE, _ESCAPE_NEUTRAL)}{ESCAPE}"

#: format_parameters 的 filter_keys 分支要滤掉的键（官方模板 standard_keys 原样）
_STANDARD_KEYS = ("description", "type", "properties", "required", "nullable")


def _dictsort(d: Mapping[str, Any]):
    """jinja `| dictsort` 的默认语义：按键排序、**大小写不敏感**（case_sensitive=False）。

    官方模板的声明体、调用实参、观测体三处全部经此排序——所以模型看到的键序恒为
    字典序。约束语法的键序也必须按同一口径（见 tool_grammar），否则约束会拒掉
    模型照着自己历史写出来的合法调用。
    """
    return sorted(d.items(), key=lambda kv: kv[0].lower() if isinstance(kv[0], str) else kv[0])


def format_argument(argument: Any, escape_keys: bool = True) -> str:
    """jinja `format_argument` 宏的移植：把一个实参值编成 DSL 字面量。

    escape_keys 只影响 **mapping 的键**是否套 `<|"|>`：声明体里的映射用 True，
    调用实参与观测体用 False（官方在这两处显式传 escape_keys=False）。
    分支次序照抄官方：字符串必须先于序列判定（jinja 里 str 也满足 `is sequence`）。
    """
    if isinstance(argument, str):
        return _raw(argument)
    if isinstance(argument, bool):          # 必须先于 int：Python 的 bool 是 int 子类
        return "true" if argument else "false"
    if isinstance(argument, Mapping):
        parts = []
        for key, value in _dictsort(argument):
            k = _raw(key) if escape_keys else str(key)
            parts.append(f"{k}:{format_argument(value, escape_keys=escape_keys)}")
        return "{" + ",".join(parts) + "}"
    if isinstance(argument, Sequence) and not isinstance(argument, (str, bytes)):
        return "[" + ",".join(format_argument(i, escape_keys=escape_keys) for i in argument) + "]"
    return str(argument)


def _upper_type(value: Any) -> str:
    return str(value or "").upper()


def format_parameters(properties: Mapping[str, Any], required: Any,
                      filter_keys: bool = False) -> str:
    """jinja `format_parameters` 宏的移植：属性表 → `名字:{…}` 逗号串。

    每个属性内部的键序是**固定的**（官方模板的书写顺序，非字典序）：
    description → enum|items → nullable → properties|required → type。
    """
    out = []
    for key, value in _dictsort(properties):
        if filter_keys and key in _STANDARD_KEYS:
            continue
        if not isinstance(value, Mapping):
            value = {}
        chunks: List[str] = []
        vtype = _upper_type(value.get("type"))

        if value.get("description"):
            chunks.append(f"description:{_raw(value['description'])}")

        if vtype == "STRING":
            if value.get("enum"):
                chunks.append(f"enum:{format_argument(value['enum'])}")
        elif vtype == "ARRAY":
            items = value.get("items")
            if isinstance(items, Mapping) and items:
                item_chunks: List[str] = []
                for item_key, item_value in _dictsort(items):
                    if item_value is None:
                        continue
                    if item_key == "properties":
                        inner = (format_parameters(item_value, items.get("required") or [])
                                 if isinstance(item_value, Mapping) else "")
                        item_chunks.append("properties:{" + inner + "}")
                    elif item_key == "required":
                        reqs = ",".join(_raw(r) for r in item_value)
                        item_chunks.append(f"required:[{reqs}]")
                    elif item_key == "type":
                        if isinstance(item_value, str):
                            item_chunks.append(f"type:{format_argument(item_value.upper())}")
                        else:
                            item_chunks.append(
                                f"type:{format_argument([str(v).upper() for v in item_value])}")
                    else:
                        item_chunks.append(f"{item_key}:{format_argument(item_value)}")
                chunks.append("items:{" + ",".join(item_chunks) + "}")

        if value.get("nullable"):
            chunks.append("nullable:true")

        if vtype == "OBJECT":
            props = value.get("properties")
            if isinstance(props, Mapping):
                chunks.append("properties:{"
                              + format_parameters(props, value.get("required") or [])
                              + "}")
            else:
                # 官方兜底分支：OBJECT 但无 properties 键时，把 value 自身当属性表，
                # 滤掉 standard_keys（否则 type/description 会被当成属性名吐出来）
                chunks.append("properties:{"
                              + format_parameters(value, value.get("required") or [],
                                                  filter_keys=True)
                              + "}")
            if value.get("required"):
                reqs = ",".join(_raw(r) for r in value["required"])
                chunks.append(f"required:[{reqs}]")

        chunks.append(f"type:{_raw(vtype)}")
        out.append(f"{key}:{{" + ",".join(chunks) + "}")
    return ",".join(out)


def tool_function(tool: Mapping[str, Any]) -> Mapping[str, Any]:
    """取工具规格的函数体：兼容扁平（本栈 MCP 注册表）与 OpenAI 嵌套两种形态。"""
    fn = tool.get("function") if isinstance(tool, Mapping) else None
    return fn if isinstance(fn, Mapping) else tool


def format_function_declaration(tool: Mapping[str, Any]) -> str:
    """jinja `format_function_declaration` 宏的移植：单个工具 → `declaration:…{…}`。"""
    fn = tool_function(tool)
    name = fn.get("name", "")
    desc = fn.get("description", "") or ""
    body = f"declaration:{name}{{description:{_raw(desc)}"

    params = fn.get("parameters")
    if params:
        body += ",parameters:{"
        props = params.get("properties") if isinstance(params, Mapping) else None
        if props:
            body += "properties:{" + format_parameters(props, params.get("required") or []) + "},"
        if params.get("required"):
            reqs = ",".join(_raw(r) for r in params["required"])
            body += f"required:[{reqs}],"
        # 官方此处是 `{%- if params['type'] -%}`：缺 type 就不闭合 parameters 的花括号。
        # 缺省补 OBJECT（见模块 docstring 的「有意偏离」）。
        body += f"type:{_raw(_upper_type(params.get('type') or 'object'))}}}"

    response = fn.get("response")
    if isinstance(response, Mapping):
        body += ",response:{"
        if response.get("description"):
            body += f"description:{_raw(response['description'])},"
        if _upper_type(response.get("type")) == "OBJECT":
            body += f"type:{_raw('OBJECT')}}}"
    return body + "}"


def format_tool_call(name: str, arguments: Any) -> str:
    """jinja 的 tool_calls 分支移植：`call:名字{k:v,…}`（不含 <|tool_call> 起止标记）。

    实参是 mapping 时按 dictsort 展开、键裸写（escape_keys=False）；是字符串时原样
    吐出（官方对 OpenAI 风格的 JSON 字符串实参就是这么放行的）。
    """
    if isinstance(arguments, Mapping):
        inner = ",".join(f"{k}:{format_argument(v, escape_keys=False)}"
                         for k, v in _dictsort(arguments))
    elif isinstance(arguments, str):
        inner = arguments
    else:
        inner = ""
    return f"call:{name}{{{inner}}}"


class DslParseError(ValueError):
    """调用体实参不符合紧凑 DSL 语法。"""


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _scalar(token: str) -> Any:
    t = token.strip()
    if not t:
        raise DslParseError("空的标量值")
    if t == "true":
        return True
    if t == "false":
        return False
    if t in ("null", "None", "none"):
        return None
    if t[0] in "\"'":
        # 值被引号包裹 = 模型吐的是 JSON（旧习惯复发）。**必须判失败**：硬收会得到
        # 带引号的字符串（run_python 会去执行字面量 `"print(1)"` 并报成功），
        # 判失败则落到下游 JSON 后备路，那条路能正确还原。（首过评审揪出。）
        raise DslParseError("值被引号包裹，疑似 JSON 调用体")
    if ":" in t:
        # 裸标量里不该出现 ':' —— 出现即漏了逗号（`{a:1 b:2}` 会把 `1 b:2` 整个
        # 吞成 a 的值、b 静默消失）。宁可报错走纠错闭环，也不交出少一个参数的调用。
        raise DslParseError("标量值里出现 ':'，疑似缺少分隔逗号")
    try:
        return int(t)
    except ValueError:
        pass
    try:
        f = float(t)
    except ValueError:
        return t    # 模型偶尔漏写 <|"|> 的裸词：宽容收下，交给上层工具校验
    if f != f or f in (float("inf"), float("-inf")):
        # NaN/Inf 不是合法实参：它们能通过 float()，却会在下游账本/日志的
        # json.dumps 处炸掉（那时已经离事发点很远了）。就地判失败。
        raise DslParseError("NaN/Inf 不是合法实参")
    return f


def _parse_value(s: str, i: int):
    if s.startswith(ESCAPE, i):
        # 原文字符串块：内容整体保真，内部不做任何转义还原——块内的引号、换行、
        # 反斜杠、甚至 `{a:1}` 这类看起来像结构的字节，全都只是内容。
        # （迁移前这里是「先把块换成 JSON 串、再用正则给裸键补引号」，那个正则会
        #  伸进字符串内容里补引号：run_python 传含 `{a:1}` 的代码、或任何含
        #  `, 词:` 的字符串值，一律解析失败。2026-08-14 往返测试揪出。）
        j = s.find(ESCAPE, i + len(ESCAPE))
        if j < 0:
            raise DslParseError("原文字符串块未闭合")
        return s[i + len(ESCAPE):j], j + len(ESCAPE)
    if i < len(s) and s[i] == "{":
        return _parse_object(s, i)
    if i < len(s) and s[i] == "[":
        return _parse_array(s, i)
    j = i
    while j < len(s) and s[j] not in ",}]":
        j += 1
    return _scalar(s[i:j]), j


def _parse_array(s: str, i: int):
    i += 1                                  # 吃掉 '['
    out: List[Any] = []
    i = _skip_ws(s, i)
    if i < len(s) and s[i] == "]":
        return out, i + 1
    while True:
        val, i = _parse_value(s, _skip_ws(s, i))
        out.append(val)
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1
            continue
        if i < len(s) and s[i] == "]":
            return out, i + 1
        raise DslParseError("数组里缺 ',' 或 ']'")


def _parse_object(s: str, i: int):
    i += 1                                  # 吃掉 '{'
    out: Dict[str, Any] = {}
    i = _skip_ws(s, i)
    if i < len(s) and s[i] == "}":
        return out, i + 1
    while True:
        i = _skip_ws(s, i)
        if s.startswith(ESCAPE, i):         # 键被套了原文块（官方调用体不这么写，宽容收）
            key, i = _parse_value(s, i)
        else:
            j = s.find(":", i)
            if j < 0:
                raise DslParseError("键之后缺 ':'")
            key = s[i:j].strip()
            i = j
        if not key:
            raise DslParseError("空键名")
        if any(c in key for c in ",{}[]") or any(c in key for c in " \t\r\n"):
            # 键名扫描到下一个 ':' 为止，结构噪声会整段变成键名：`{a:1,,b:2}` 曾解析出
            # 键 ',b'、`{a:1 b:2}` 曾把 b 吞掉。这类「像模像样但是错的」实参会被工具
            # 照单执行——静默错比报错坏得多。（首过评审揪出。）
            raise DslParseError(f"键名含非法字符: {key!r}")
        if key[0] in "\"'":
            # 键被引号包裹 = 模型吐的其实是 JSON（旧习惯复发）。这里必须**判失败**
            # 而不是硬收：硬收会得到 '"a"' 这样带引号的键名，工具侧参数对不上却
            # 一路装作成功。判失败则落到下游的 JSON 后备路，那条路能正确还原。
            raise DslParseError("键被引号包裹，疑似 JSON 调用体")
        i = _skip_ws(s, i)
        if i >= len(s) or s[i] != ":":
            raise DslParseError("键之后缺 ':'")
        val, i = _parse_value(s, _skip_ws(s, i + 1))
        out[str(key)] = val
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1
            continue
        if i < len(s) and s[i] == "}":
            return out, i + 1
        raise DslParseError("对象里缺 ',' 或 '}'")


def parse_arguments(body: str) -> Dict[str, Any]:
    """`{k:v,…}` → dict。`format_tool_call` 的逆运算（往返金测试守着）。

    严于结构、宽于标量：括号/定界符不合规直接抛（交给上层纠错闭环重发），
    但漏写定界符的裸词按字符串收下——模型偶发的小毛病不值得废一整轮。
    """
    s = body.strip()
    if not s.startswith("{"):
        raise DslParseError("实参必须以 '{' 开头")
    value, end = _parse_object(s, 0)
    if _skip_ws(s, end) != len(s):
        raise DslParseError("实参 '}' 之后有多余字节")
    return value


def format_tool_response(name: str, response: Any) -> str:
    """jinja `format_tool_response_block` 宏的移植（不含 <|tool_response> 起止标记）。

    mapping → `response:名字{k:v,…}`；其余一律裹成 `response:名字{value:…}`。
    """
    if isinstance(response, Mapping):
        inner = ",".join(f"{k}:{format_argument(v, escape_keys=False)}"
                         for k, v in _dictsort(response))
        return f"response:{name}{{{inner}}}"
    return f"response:{name}{{value:{format_argument(response, escape_keys=False)}}}"
