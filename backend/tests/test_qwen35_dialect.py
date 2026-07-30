"""qwen35 方言金测试（Qwen3.5/Ornith 家族：ChatML + XML 调用体）。

守的是什么：prompt 组装（工具声明/思考恒回放/工具调用回放/工具结果白名单/环境
门控）与响应解析（正常路 + 各类残缺形态不得漏垃圾进正文）的方言事实，以及协议
面（markers/thinking_prefill/tool_call_schema 拒约束）与注册面的接缝不变量。
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

def test_parse_response_happy_path():
    raw = ('<think>T</think>\n\n正文\n\n<tool_call>\n<function=f>\n'
           '<parameter=s>\nhello\nworld\n</parameter>\n'
           '<parameter=n>\n42\n</parameter>\n'
           '<parameter=obj>\n{"k": 1}\n</parameter>\n'
           '</function>\n</tool_call>')
    result = _dialect().parse_response(raw)
    assert result["thoughts"] == "T"
    assert result["text"] == "正文"
    assert len(result["tool_calls"]) == 1
    call = result["tool_calls"][0]
    assert call["type"] == "function"
    assert call["name"] == "f"
    assert call["arguments"] == {"s": "hello\nworld", "n": 42, "obj": {"k": 1}}


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


def test_tool_call_schema_not_implemented():
    with pytest.raises(NotImplementedError):
        _dialect().tool_call_schema([])


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


def test_param_python_literals_coerced():
    """Python 风格 True/False/None 还原为原语（2026-07-30 评测实测两模型都发
    'True'——家族性习惯）；作为多行字符串**内容一部分**的 True 一词不受影响。"""
    from framework.dialects.qwen35.response_parser import _coerce
    assert _coerce("True") is True and _coerce("False") is False
    assert _coerce("None") is None
    assert _coerce("true") is True          # JSON 小写路径不回退
    assert _coerce("The flag is True") == "The flag is True"
    assert _coerce("True\n但这是段落") == "True\n但这是段落"
