"""qwen35 方言金测试（Qwen3.5/Ornith 家族：ChatML + XML 调用体）。

守的是什么：prompt 组装（工具声明/思考恒回放/工具调用回放/工具结果白名单/环境
门控）与响应解析（正常路 + 各类残缺形态不得漏垃圾进正文）的方言事实，以及协议
面（markers/thinking_prefill/tool_call_grammar 拒约束）与注册面的接缝不变量。
本文件不重写实现，只钉行为——与 test_dialect.py（gemma4）同一条铁律。
"""
import pytest

from framework.dialects import get_dialect_class
from framework.dialects.env_facts import compute_static_env_lines
from framework.dialects.gemma4.prompt_builder import PromptBuilder as Gemma4PromptBuilder


def _dialect():
    return get_dialect_class("qwen35").create(None, options={}, env={})


_TOOLS = [{"name": "rag_search", "description": "d",
           "parameters": {"properties": {"q": {"type": "string"}}, "required": ["q"]}}]


# ---- 1. build_prompt 形态 ----

def test_build_prompt_shape_with_system_user_and_tools():
    out = _dialect().build_prompt(
        messages=[{"role": "system", "content": "You are helpful."},
                  {"role": "user", "content": "hi"}],
        tools=_TOOLS, enable_thinking=True)
    assert "<|im_start|>system" in out
    assert '"name": "rag_search"' in out
    assert "<tools>" in out
    assert "ONLY reply in the following format" in out
    assert out.endswith("<|im_start|>assistant\n<think>\n")


def test_build_prompt_thinking_off_prefills_empty_think():
    out = _dialect().build_prompt(messages=[{"role": "user", "content": "hi"}],
                                  enable_thinking=False)
    assert out.endswith("<think>\n\n</think>\n\n")


def test_public_tools_declaration_is_byte_identical_to_builder_path():
    """D28 公开 format_tools_declaration（预算闸按它计数）：与 builder 内部路径、与
    抽出前的原表达式逐字节同；整段 prompt 里出现的正是它被 system 块包裹的形态。"""
    import json
    from framework.dialects.qwen35 import prompt_builder as pb
    tools = _TOOLS + [{"name": "写文件", "description": "中文描述 \"引号\"",
                       "parameters": {"properties": {}}}]
    pub = pb.format_tools_declaration(tools)
    legacy = ("# Tools\n\nYou have access to the following functions:\n\n"
              "<tools>\n" + "\n".join(json.dumps(t, ensure_ascii=False) for t in tools)
              + "\n</tools>" + pb._TOOL_FORMAT_GUIDE)
    builder = pb.Qwen35PromptBuilder(None)
    assert pub == legacy == builder._build_tools_declaration(tools)
    # 预算闸数的逐件声明 = 块内逐行（同口径的前提：闸数的就是 prompt 里那几行）
    assert [pb.format_tool_declaration(t) for t in tools] == \
        pub.split("<tools>\n", 1)[1].split("\n</tools>", 1)[0].split("\n")
    out = builder.build(messages=[{"role": "system", "content": "s"},
                                  {"role": "user", "content": "hi"}],
                        tools=tools, enable_thinking=False)
    assert f"<|im_start|>system\n{pub}<|im_end|>\n" in out
    assert pb.format_tools_declaration([]) == "" == pb.format_tools_declaration(None)


# ---- 2. 思考恒回放 ----

def test_assistant_thoughts_always_replayed():
    out = _dialect().build_prompt(
        messages=[{"role": "assistant", "content": "ok", "thoughts": "想一想"}])
    assert "<think>\n想一想\n</think>" in out


# ---- 3. 工具调用回放 ----

def test_tool_calls_replayed_in_xml_and_error_calls_skipped():
    out = _dialect().build_prompt(messages=[{
        "role": "assistant", "content": "",
        "tool_calls": [
            {"type": "function", "name": "x", "arguments": {"a": "多行\n值", "n": 5}},
            {"type": "error", "name": "y", "arguments": {}},
        ],
    }])
    assert "<tool_call>\n<function=x>" in out
    assert "<parameter=a>\n多行\n值\n</parameter>" in out
    assert "<parameter=n>\n5\n</parameter>" in out
    # error 调用不回放：只应出现一次 <tool_call> 开标记
    assert out.count("<tool_call>") == 1


# ---- 4. 工具结果白名单（隐私纪律） ----

def test_tool_responses_expose_only_whitelisted_keys():
    out = _dialect().build_prompt(messages=[{
        "role": "tool",
        "tool_responses": [{"name": "t", "content": "obs", "_safe": True,
                            "render": {"hint": "chart"}}],
    }])
    assert '"name": "t"' in out
    assert '"content": "obs"' in out
    assert "_safe" not in out
    assert "render" not in out
    assert "<|im_start|>user\n<tool_response>" in out


# ---- 5. include_env 门控 ----

def test_include_env_gates_machine_facts_and_extra():
    d = _dialect()
    out_false = d.build_prompt(messages=[], include_env=False, env_extra=["探试线EXTRA"])
    out_true = d.build_prompt(messages=[], include_env=True, env_extra=["探试线EXTRA"])
    assert "Operating system:" not in out_false
    assert "探试线EXTRA" not in out_false
    assert "Current date:" in out_true
    assert "探试线EXTRA" in out_true


# ---- 6. parse_response 正常路 ----

_HAPPY_RAW = ('<think>T</think>\n\n正文\n\n<tool_call>\n<function=f>\n'
              '<parameter=s>\nhello\nworld\n</parameter>\n'
              '<parameter=n>\n42\n</parameter>\n'
              '<parameter=obj>\n{"k": 1}\n</parameter>\n'
              '</function>\n</tool_call>')

_HAPPY_TOOLS = [{"name": "f", "description": "d",
                 "parameters": {"type": "object",
                                "properties": {"s": {"type": "string"},
                                               "n": {"type": "integer"},
                                               "obj": {"type": "object"}},
                                "required": []}}]


def test_parse_response_happy_path():
    result = _dialect().parse_response(_HAPPY_RAW, tools=_HAPPY_TOOLS)
    assert result["thoughts"] == "T"
    assert result["text"] == "正文"
    assert len(result["tool_calls"]) == 1
    call = result["tool_calls"][0]
    assert call["type"] == "function"
    assert call["name"] == "f"
    assert call["arguments"] == {"s": "hello\nworld", "n": 42, "obj": {"k": 1}}


def test_parse_response_without_schema_keeps_raw_text():
    """D46：无 schema 不猜类型——形态像数字/JSON 的参数也原文交给工具层。"""
    result = _dialect().parse_response(_HAPPY_RAW)
    assert result["tool_calls"][0]["arguments"] == {
        "s": "hello\nworld", "n": "42", "obj": '{"k": 1}'}


def test_parse_response_unknown_tool_or_param_keeps_raw_text():
    """schema 里查不到的工具名 / 参数名同样原文（不回退到形态猜测）。"""
    other = [{"name": "g", "description": "d",
              "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}]
    partial = [{"name": "f", "description": "d",
                "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}]
    assert _dialect().parse_response(_HAPPY_RAW, tools=other)["tool_calls"][0]["arguments"] == {
        "s": "hello\nworld", "n": "42", "obj": '{"k": 1}'}
    assert _dialect().parse_response(_HAPPY_RAW, tools=partial)["tool_calls"][0]["arguments"] == {
        "s": "hello\nworld", "n": 42, "obj": '{"k": 1}'}


_WRITE_FILE_TOOLS = [{"name": "write_file", "description": "d",
                      "parameters": {"type": "object",
                                     "properties": {"path": {"type": "string"},
                                                    "content": {"type": "string"}},
                                     "required": ["path", "content"]}}]


@pytest.mark.parametrize("content", [
    '{"a": 1, "b": [true, null]}', "[1, 2, 3]", "true", "null", "1e3", "12.50",
    "True", "None",
])
def test_string_param_kept_byte_for_byte(content):
    """D46 核心守卫：string 型参数长得再像 JSON 字面量也原样保留。旧形态启发式把它们
    还原成 dict/list/bool/None/float，file_edit 再 str() 落盘成 Python repr。"""
    raw = ("<tool_call>\n<function=write_file>\n<parameter=path>\nout.json\n</parameter>\n"
           f"<parameter=content>\n{content}\n</parameter>\n</function>\n</tool_call>")
    result = _dialect().parse_response(raw, tools=_WRITE_FILE_TOOLS)
    got = result["tool_calls"][0]["arguments"]["content"]
    assert type(got) is str
    assert got == content


# ---- 7. parse 残缺形态：不得漏垃圾进正文 ----

def test_parse_unterminated_tool_call_becomes_error():
    raw = "前<tool_call>\n<function=f>\n<parameter=a>\nv\n</parameter>\n</function>\n"
    result = _dialect().parse_response(raw)
    assert result["text"] == "前"
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["type"] == "error"
    assert "<tool_call>" not in result["text"]


def test_parse_headless_tool_call_fragment_becomes_error():
    raw = "前</tool_call>后"
    result = _dialect().parse_response(raw)
    assert result["text"] == "后"
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["type"] == "error"
    assert "</tool_call>" not in result["text"]
    assert "前" not in result["text"]


def test_parse_tool_call_missing_function_block_becomes_error():
    raw = "<tool_call>\njust text no function\n</tool_call>"
    result = _dialect().parse_response(raw)
    assert result["text"] == ""
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["type"] == "error"


def test_parse_unclosed_think_routes_to_thoughts_not_text():
    raw = "<think>只有开头没有结尾的思考"
    result = _dialect().parse_response(raw)
    assert result["thoughts"] == "只有开头没有结尾的思考"
    assert result["text"] == ""


def test_parse_stray_markers_stripped_from_text():
    raw = "正文</think>还有<|im_end|>尾巴"
    result = _dialect().parse_response(raw)
    assert "</think>" not in result["text"]
    assert "<|im_end|>" not in result["text"]


# ---- 8. 协议面 ----

def test_thinking_prefill():
    d = _dialect()
    assert d.thinking_prefill(True) == "<think>\n"
    assert d.thinking_prefill(False) == ""


def test_markers():
    d = _dialect()
    m = d.markers
    assert m.soc == "<think>"
    assert m.eoc == "</think>"
    assert m.stc == "<tool_call>"
    assert m.etc == "</tool_call>"


def test_tool_call_grammar_not_implemented():
    with pytest.raises(NotImplementedError):
        _dialect().tool_call_grammar([])


# ---- 9. 注册面 ----

def test_unknown_dialect_error_mentions_qwen35():
    with pytest.raises(ValueError, match="qwen35"):
        get_dialect_class("不存在")


# ---- 10. env_facts 回归钉（gemma4 委托 env_facts，输出须逐字节一致） ----

class _FakeTok:
    special_tokens_map = {}


class _FakeProcessor:
    tokenizer = _FakeTok()


def test_env_facts_matches_gemma4_delegate():
    env = {"inject_machine": True, "inject_timezone": True,
           "language": "中文", "location": "东京", "about": "工程背景"}
    gemma4_pb = Gemma4PromptBuilder(_FakeProcessor(), env=env)
    assert gemma4_pb._compute_static_env_lines() == compute_static_env_lines(env)


def test_coerce_typed_follows_schema():
    """_coerce_typed 逐类型钉死（D46）：按声明类型还原，还原不了返回原文。
    Python 风格 True/False/None 照收（2026-07-30 评测实测 Qwen3.5 家族惯性发 'True'）。"""
    from framework.dialects.qwen35.response_parser import _coerce_typed as c
    b, i, n = {"type": "boolean"}, {"type": "integer"}, {"type": "number"}
    o, a = {"type": "object"}, {"type": "array"}
    # boolean
    assert c("true", b) is True and c("True", b) is True
    assert c("false", b) is False and c("False", b) is False
    assert c("yes", b) == "yes"
    # integer
    assert c("42", i) == 42 and type(c("42", i)) is int
    assert c("-7", i) == -7
    assert c("4.2", i) == "4.2"
    # number
    assert c("42", n) == 42 and type(c("42", n)) is int
    assert c("1e3", n) == 1000.0 and type(c("1e3", n)) is float
    assert c("12.50", n) == 12.5
    assert c("abc", n) == "abc"
    # object / array
    assert c('{"k": [1, true]}', o) == {"k": [1, True]}
    assert c("{broken", o) == "{broken"
    assert c("[1, 2]", o) == "[1, 2]"
    assert c("[1, 2]", a) == [1, 2]
    assert c('{"k": 1}', a) == '{"k": 1}'
    # 非 string 型的 null / None → None
    for t in (b, i, n, o, a):
        assert c("null", t) is None and c("None", t) is None
    # 无 schema / 缺 type / 联合类型含 string → 原文（不 strip）
    assert c("\n42 ", None) == "\n42 "
    assert c("42", {"description": "no type"}) == "42"
    assert c("42", {"type": ["string", "integer"]}) == "42"
    assert c("null", {"type": ["integer", "string"]}) == "null"
    # 联合类型不含 string → 第一个非 null 类型
    assert c("42", {"type": ["null", "integer"]}) == 42
    assert c("null", {"type": ["null", "integer"]}) is None
    # string → 一个字节都不动
    assert c(" True\n", {"type": "string"}) == " True\n"


def test_coerce_typed_rejects_non_ascii_digits():
    """M1：`\\d` 加 re.ASCII 后只认 0-9；阿拉伯数字 / 全角数字一律当不匹配原文返回，
    不被 Unicode 数字类字符悄悄吞成整数/浮点数。"""
    from framework.dialects.qwen35.response_parser import _coerce_typed as c
    i, n = {"type": "integer"}, {"type": "number"}
    assert c("٣", i) == "٣"          # 阿拉伯-印度数字 3
    assert c("٣.٥", n) == "٣.٥"       # 阿拉伯-印度数字 3.5
    assert c("３", i) == "３"         # 全角数字 3


def test_coerce_typed_integer_tolerates_trailing_zero_decimal():
    """M2：integer 容忍 "5.0" / "-7.000" 这种小数点全 0 的形态（上游 tojson 整数常见
    写法），经 split 取整数部分转 int（不经 float，避免大整数精度损失）；"5.5" / "5."
    仍原文。boolean 的 "1"/"0" 不收（保持原文，行为不变）。"""
    from framework.dialects.qwen35.response_parser import _coerce_typed as c
    i, b = {"type": "integer"}, {"type": "boolean"}
    assert c("5.0", i) == 5 and type(c("5.0", i)) is int
    assert c("-7.000", i) == -7
    assert c("5.5", i) == "5.5"
    assert c("5.", i) == "5."
    assert c("1", b) == "1" and c("0", b) == "0"


def test_coerce_typed_strip_and_number_edge_cases():
    """M4：integer/number 的 strip 覆盖空格与制表符；number 支持负数指数与普通负
    小数（既有用例只覆盖了正数/科学计数法的部分形态）。"""
    from framework.dialects.qwen35.response_parser import _coerce_typed as c
    i, n = {"type": "integer"}, {"type": "number"}
    assert c(" 42 ", i) == 42
    assert c("\t42\t", i) == 42
    assert c("-1.5e-3", n) == -0.0015
    assert c("-2.5", n) == -2.5


def test_coerce_typed_union_anyof_oneof():
    """M3+N2：type 为列表、或 type 缺失但带 anyOf/oneOf 时，按分支类型逐个尝试
    还原，第一个命中的就是结果；含 string 分支恒原文；全部不命中原文。"""
    from framework.dialects.qwen35.response_parser import _coerce_typed as c
    # type 列表：["array", "object"] 按顺序逐个尝试
    arr_obj = {"type": ["array", "object"]}
    assert c("{}", arr_obj) == {}
    assert c("[]", arr_obj) == []
    # anyOf：[{type: integer}, {type: null}]
    any_int_null = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert c("5", any_int_null) == 5
    assert c("null", any_int_null) is None
    assert c("x", any_int_null) == "x"
    # anyOf 含 string 分支 → 恒原文
    any_with_string = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    assert c("5", any_with_string) == "5"
    # oneOf 同 anyOf 一例
    one_int_null = {"oneOf": [{"type": "integer"}, {"type": "null"}]}
    assert c("5", one_int_null) == 5
    # 既有用例保留：["null", "integer"] 收到 "42" → 42
    assert c("42", {"type": ["null", "integer"]}) == 42


def test_properties_index_tolerates_malformed_tool_schema():
    """M4：tools 列表里形态不对的条目（tool 条目非 dict / parameters 非 dict /
    properties 非 dict / 单个 prop 非 dict）都不炸、都原文——走 parse() 整条路径
    （不只直测 _properties_index），确认 parse 不抛异常。"""
    raw = "<tool_call>\n<function=f>\n<parameter=n>\n42\n</parameter>\n</function>\n</tool_call>"
    bad_tools_variants = [
        ["not-a-dict"],
        [{"name": "f", "parameters": "not-a-dict"}],
        [{"name": "f", "parameters": {"properties": "not-a-dict"}}],
        [{"name": "f", "parameters": {"properties": {"n": "integer"}}}],
    ]
    for tools in bad_tools_variants:
        result = _dialect().parse_response(raw, tools=tools)
        assert result["tool_calls"][0]["arguments"] == {"n": "42"}
