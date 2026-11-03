"""engine × 方言解析接线（D46）：generate_once 必须把本轮完整工具 schema 交给
parse_response——grammar_tools 优先，缺省沿用 tools。

qwen35 的 XML 调用体不带类型，参数类型只能按 schema 还原；engine 不传（或传了 catalog
模式的瘦目录 tools 而不是 grammar_tools）时，integer/object 参数会以原文抵达工具层。
假后端逐块吐出固定文本，不加载模型。
"""
import asyncio
from types import SimpleNamespace

import pytest

_FULL = [{"name": "f", "description": "d",
          "parameters": {"type": "object",
                         "properties": {"n": {"type": "integer"},
                                        "s": {"type": "string"}},
                         "required": ["n"]}}]
# catalog 模式的瘦目录：同名但不带 properties（prompt 声明用，不是语法源）
_SLIM = [{"name": "f", "description": "d"}]

_OUT = ("<tool_call>\n<function=f>\n<parameter=n>\n42\n</parameter>\n"
        "<parameter=s>\n42\n</parameter>\n</function>\n</tool_call>")


class _Tok:
    special_tokens_map = {}

    def encode(self, s, add_special_tokens=False):
        return list(range(len(s)))


class _Backend:
    def __init__(self, text):
        self.processor = SimpleNamespace(tokenizer=_Tok())
        self.text = text

    def stream_generate(self, prompt, **kwargs):
        yield SimpleNamespace(text=self.text, delta=None, prompt_tokens=3,
                              generation_tokens=1, prompt_tps=1.0,
                              generation_tps=1.0, peak_memory=0.0, extra={})


def _session():
    return SimpleNamespace(history=[{"role": "system", "content": "s"},
                                    {"role": "user", "content": "hi"}],
                           images=[], audios=[], brain_memory_state=None,
                           cancel_requested=False, inject_env_context=False,
                           prompt_stream=None, session_id="d46-s",
                           journal=SimpleNamespace(current_turn_id="turn-1"))


def _run(**kw):
    from framework.core.engine import AgentEngine
    eng = AgentEngine(_Backend(_OUT), kv_config={}, model_options={"dialect": "qwen35"})
    seen = []
    real = eng.dialect.parse_response

    def spy(raw, tools=None):
        seen.append(tools)
        return real(raw, tools=tools)

    eng.dialect.parse_response = spy
    r = asyncio.run(eng.generate_once(_session(), **kw))
    assert len(seen) == 1
    return r, seen[0]


def test_grammar_tools_win_over_slim_catalog():
    r, seen = _run(tools=_SLIM, grammar_tools=_FULL)
    assert seen is _FULL
    assert r["tool_calls"][0]["arguments"] == {"n": 42, "s": "42"}


def test_tools_used_when_no_grammar_tools():
    r, seen = _run(tools=_FULL)
    assert seen is _FULL
    assert r["tool_calls"][0]["arguments"] == {"n": 42, "s": "42"}


@pytest.mark.parametrize("kw", [{"tools": None}, {"tools": _FULL, "grammar_tools": []}],
                         ids=["no_tools", "empty_grammar_tools"])
def test_no_schema_keeps_raw_text(kw):
    r, seen = _run(**kw)
    assert seen == kw.get("grammar_tools", kw["tools"])
    assert r["tool_calls"][0]["arguments"] == {"n": "42", "s": "42"}
