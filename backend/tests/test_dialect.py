"""方言接缝金测试（EXECUTION_LEDGER R0）：ModelDialect 协议与注册面的不变量。

守的是什么：R0 给模型方言开了一条接缝（换 30-70B / MoE 目标模型时不必动 engine
承重代码）。接缝一旦漂移，症状**不会立刻暴露**——prompt 用错模板、约束 schema 与
回放格式脱锚都是"能跑但慢慢坏"，正是铁律 1 警告的那类退化。故逐条钉死。

注：132 项存量金测试走 FakeEngine，**不覆盖 AgentEngine 的方言装配**。本文件补的
就是那块空白。
"""
import pytest

from framework.core.dialect_protocol import DialectMarkers
from framework.dialects import DEFAULT_DIALECT, get_dialect_class
from framework.dialects.gemma4.dialect import Gemma4Dialect
from framework.dialects.gemma4.prompt_builder import PromptBuilder
from framework.dialects.gemma4.response_parser import ResponseParser
from framework.dialects.gemma4.tool_schema import build_tool_call_schema


class _Tok:
    special_tokens_map = {}

    def encode(self, s, add_special_tokens=False):
        return [1]


class _Proc:
    tokenizer = _Tok()


_TOOLS = [{"name": "rag_search", "description": "d",
           "parameters": {"properties": {"q": {"type": "string"}}, "required": ["q"]}}]
_MSGS = [{"role": "user", "content": "查天气"},
         {"role": "assistant", "content": "好", "thoughts": "想一想"}]


def _dialect(**options):
    return get_dialect_class(str(options.pop("dialect", ""))).create(
        _Proc(), options=options, env={})


# ---- 注册面 ----

def test_default_dialect_is_gemma4():
    """缺省即 gemma4：存量注册表条目不填 dialect 也不改行为（R0 零行为变化的一环）。"""
    assert DEFAULT_DIALECT == "gemma4"
    assert get_dialect_class("") is Gemma4Dialect
    assert get_dialect_class("gemma4") is Gemma4Dialect


def test_unknown_dialect_rejected_loudly():
    """未知方言必须炸而不是静默回落——回落等于拿错模板跑，症状隐蔽且致命。"""
    with pytest.raises(ValueError, match="未知模型方言"):
        get_dialect_class("qwen3")


# ---- 协议面 ----

def test_markers_sourced_from_parser():
    """markers 是方言事实的单一来源。此前引擎直读 parser.soc/eoc/stc/etc
    （parser 事实上兼任标记登记处），R0 只把该隐式职责显式化，取值不得变。"""
    d, rp = _dialect(), ResponseParser(_Proc())
    assert d.markers == DialectMarkers(soc=rp.soc, eoc=rp.eoc, stc=rp.stc, etc=rp.etc)


def test_build_prompt_matches_builder_bytewise():
    """方言的 build_prompt 必须与原 PromptBuilder.build 逐字节一致（搬运，非重写）。"""
    d = _dialect()
    pb = PromptBuilder(_Proc(), suppress_ghost_thinking=False, env={})
    assert d.build_prompt(messages=_MSGS, tools=_TOOLS, enable_thinking=True,
                          include_env=True) == \
        pb.build(messages=_MSGS, tools=_TOOLS, enable_thinking=True, include_env=True)


def test_parse_response_matches_parser():
    """方言的 parse_response 必须与原 ResponseParser.parse 同构。"""
    raw = ('<|channel>thought 想<channel|>好的。'
           '<|tool_call>{"name":"rag_search","arguments":{"q":"x"}}<tool_call|>')
    assert _dialect().parse_response(raw) == ResponseParser(_Proc()).parse(raw)


def test_thinking_prefill_uses_dialect_token_not_literal():
    """思考播种必须用方言的实际 token（原 engine.py 的注释即此要求）；关思考时为空串。"""
    d = _dialect()
    assert d.thinking_prefill(True) == f"{d.markers.soc}thought\n"
    assert d.thinking_prefill(False) == ""


def test_tool_call_schema_anchors_replay_format():
    """约束采样的 schema 必须与 prompt_builder 回放格式锚定同一形状。

    脱锚的后果：约束采样把模型逼成它自己历史里没见过的形状（生成质量断崖，
    且因 fail-open 兜底而不报错）。这里钉死 schema 的形状契约。
    """
    d = _dialect()
    schema = d.tool_call_schema(_TOOLS)
    assert schema == build_tool_call_schema(_TOOLS)
    branch = schema["anyOf"][0]["properties"]
    assert branch["type"] == {"const": "function"}      # 回放格式的 type 字段
    assert branch["name"] == {"const": "rag_search"}    # 回放格式的 name 字段
    assert "arguments" in branch                        # 回放格式的 arguments 字段


def test_empty_tools_schema_is_permissive():
    """无工具时不得产出空 anyOf（llguidance 会编译失败 → 整条约束链 fail-open）。"""
    assert _dialect().tool_call_schema([]) == {"type": "object"}


# ---- 模型级开关经 options 透传（不再出现在引擎签名里） ----

def test_model_option_reaches_dialect():
    """suppress_ghost_thinking 是 gemma-4 大杯的方言事实：经注册表条目 → options →
    方言自行解读。引擎不解读任何模型级开关（换模型家族不动引擎签名）。"""
    on = _dialect(suppress_ghost_thinking=True).build_prompt(messages=_MSGS)
    off = _dialect(suppress_ghost_thinking=False).build_prompt(messages=_MSGS)
    assert on != off
    assert on == PromptBuilder(_Proc(), suppress_ghost_thinking=True,
                               env={}).build(messages=_MSGS)


def test_env_gate_survives_the_seam():
    """隐私门控（个人环境事实只进主编排轮、不进沙箱）必须穿过方言层不变。
    与 test_prompt_env.py 同一条不变量，此处守的是它经 dialect 调用时依然成立。"""
    d = get_dialect_class("").create(
        _Proc(), options={}, env={"inject_machine": False, "location": "东京"})
    assert "东京" in d.build_prompt(messages=[], include_env=True)
    assert "东京" not in d.build_prompt(messages=[], include_env=False)
