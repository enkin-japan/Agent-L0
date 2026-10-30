"""token 去向分解（engine._token_breakdown）的方言感知金测试（3d/3f）。

qwen35 开思考时 prompt 收尾在已打开的 `<think>` 内、块不带频道名——按 gemma4 的
`thought` 频道名判，整段思考会被算成可见正文（实测 thinking 恒 0）。假分词器按字符计数。
"""
from types import SimpleNamespace

import pytest


class _Tok:
    special_tokens_map = {}

    def encode(self, s, add_special_tokens=False):
        return list(range(len(s)))


def _engine(dialect):
    from framework.core.engine import AgentEngine
    backend = SimpleNamespace(processor=SimpleNamespace(tokenizer=_Tok()))
    return AgentEngine(backend, kv_config={}, model_options={"dialect": dialect})


THINK = "Let me add the two quarters first."
ANSWER = "Total revenue is 2,469."


def test_gemma4_output_splits_thinking_and_visible():
    eng = _engine("gemma4")
    raw = f"<|channel>thought\n{THINK}<channel|>{ANSWER}"
    b = eng._token_breakdown(raw, 200, enable_thinking=True)
    assert (b["thinking"], b["visible"], b["tool_json"]) == (len(THINK), len(ANSWER), 0)


@pytest.mark.parametrize("seeded", [True, False], ids=["with_engine_seed", "raw_output"])
def test_qwen35_output_counts_preopened_block_as_thinking(seeded):
    eng = _engine("qwen35")
    raw = f"\n{THINK}\n</think>\n\n{ANSWER}"
    if seeded:                       # generate_once 把思考播种串拼在产出前
        raw = eng.dialect.thinking_prefill(True) + raw
    b = eng._token_breakdown(raw, 200, enable_thinking=True)
    assert (b["thinking"], b["visible"]) == (len(THINK), len(ANSWER))


def test_qwen35_unclosed_thinking_is_all_thinking():
    eng = _engine("qwen35")
    b = eng._token_breakdown(f"<think>\n{THINK} still going", 50, enable_thinking=True)
    assert b["thinking"] == len(f"{THINK} still going") and b["visible"] == 0


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_no_thinking_output_counts_zero_thinking(dialect):
    eng = _engine(dialect)
    b = eng._token_breakdown(ANSWER, 10, enable_thinking=False)
    assert (b["thinking"], b["visible"]) == (0, len(ANSWER))


def test_preopened_flag_is_a_dialect_fact():
    assert _engine("qwen35").dialect.thinking_preopened(True) is True
    assert _engine("qwen35").dialect.thinking_preopened(False) is False
    assert getattr(_engine("gemma4").dialect, "thinking_preopened", None) is None
