"""reasoning_effort 透传（Qwen3.8 官方旋钮，D.24 第 4 层搭车件，2026-08-22）。

守的是什么（首过评审 C1-C3 后的定稿语义）：
- 注入**只由注册表键驱动，与 enable_thinking 解耦**（prompt_builder 模块头差异 4：
  生产 thinking_mode=dynamic 下每轮 iteration 0 恒开思考，若随思考门控，思考开关
  每翻转一次就打掉整条 KV 前缀——A8 实测过的 23s 级冷预填）；
- 不设键/空值 = 恒不注入（**零行为变化**，官方 medium 同形）；显式 xhigh/low =
  恒注入且跨思考开关字节恒定（前缀稳定性直接钉死）；
- 注入位置 = **开头的** system（messages[0]）内容之前（'\n\n' 相接）；非开头
  system（滚动摘要/护栏、增量路渲染的）绝不注入——官方对非开头 system 直接
  raise，我方宽容布局下的等价纪律；无开头 system 时单独成块排最前（工具兜底
  块之前——官方把指令放工具块开头）；
- 非法值装配期拒启（官方模板 raise_exception 同语义）；
- **句子字节与官方模板逐字一致**——差分基准是 transformers 渲染本机 Qwen3.8
  模板的实际输出（test_gemma4_dsl 同一条纪律），不是我方期望值。
"""
from pathlib import Path

import pytest

from framework.dialects import get_dialect_class
from framework.dialects.qwen35.prompt_builder import (REASONING_EFFORTS,
                                                      REASONING_INSTRUCTIONS)

_MODELS = Path(__file__).resolve().parents[1] / "models"


def _dialect(effort=None, **opts):
    if effort is not None:
        opts["reasoning_effort"] = effort
    return get_dialect_class("qwen35").create(None, options=opts, env={})


_MSGS = [{"role": "system", "content": "You are helpful."},
         {"role": "user", "content": "hi"}]
_TOOLS = [{"name": "t1", "description": "d",
           "parameters": {"properties": {}, "required": []}}]
_XHIGH = REASONING_INSTRUCTIONS["xhigh"]
_LOW = REASONING_INSTRUCTIONS["low"]


# ---------- 缺省 = 零行为变化 ----------

@pytest.mark.parametrize("thinking", [True, False])
def test_no_key_never_injects(thinking):
    out = _dialect().build_prompt(messages=list(_MSGS), tools=_TOOLS,
                                  enable_thinking=thinking)
    assert "Reasoning effort" not in out


@pytest.mark.parametrize("raw", [None, ""])
def test_absent_or_empty_key_is_no_injection_not_error(raw):
    out = _dialect(**{"reasoning_effort": raw}).build_prompt(
        messages=list(_MSGS), enable_thinking=True)
    assert "Reasoning effort" not in out


# ---------- 显式设键 ----------

def test_xhigh_injected_before_leading_system_content():
    out = _dialect("xhigh").build_prompt(messages=list(_MSGS),
                                         enable_thinking=True)
    assert f"<|im_start|>system\n{_XHIGH}\n\nYou are helpful.<|im_end|>" in out
    assert out.count("Reasoning effort") == 1


def test_production_shape_system_tools_thinking():
    """最常见生产形状：system + tools + 开思考——指令进首 system，工具块不带。"""
    out = _dialect("xhigh").build_prompt(messages=list(_MSGS), tools=_TOOLS,
                                         enable_thinking=True)
    assert out.count("Reasoning effort") == 1
    assert out.index(_XHIGH) < out.index("# Tools")
    assert f"<|im_start|>system\n{_XHIGH}\n\nYou are helpful.<|im_end|>" in out


def test_injection_stable_across_thinking_toggle():
    """前缀稳定性（评审 C2 的钉子）：思考开/关只许改生成头，历史与稳定前缀
    必须逐字节相同——qwen35 版的 test_thinking_toggle_only_hits_generation_head。"""
    d = _dialect("xhigh")
    on = d.build_prompt(messages=list(_MSGS), tools=_TOOLS, enable_thinking=True)
    off = d.build_prompt(messages=list(_MSGS), tools=_TOOLS, enable_thinking=False)
    head = "<|im_start|>assistant\n"
    assert on.rsplit(head, 1)[0] == off.rsplit(head, 1)[0]
    assert _XHIGH in on and _XHIGH in off      # 解耦：关思考同样在场且同位


def test_medium_injects_nothing():
    out = _dialect("medium").build_prompt(messages=list(_MSGS),
                                          enable_thinking=True)
    assert "Reasoning effort" not in out


def test_low_injects_low_sentence():
    out = _dialect("low").build_prompt(messages=list(_MSGS),
                                       enable_thinking=True)
    assert _LOW in out and _XHIGH not in out


def test_mid_stream_system_never_injected():
    msgs = list(_MSGS) + [{"role": "system", "content": "mid-stream guardrail"}]
    out = _dialect("xhigh").build_prompt(messages=msgs, enable_thinking=True)
    assert out.count("Reasoning effort") == 1
    assert "<|im_start|>system\nmid-stream guardrail<|im_end|>" in out


def test_non_leading_system_gets_standalone_block_not_mid_injection():
    """system 不在开头（评审 S4）：绝不中段注入——单独成块排最前。"""
    msgs = [{"role": "user", "content": "hi"},
            {"role": "system", "content": "rolling summary"},
            {"role": "user", "content": "again"}]
    out = _dialect("xhigh").build_prompt(messages=msgs, enable_thinking=True)
    assert out.startswith(f"<|im_start|>system\n{_XHIGH}<|im_end|>\n")
    assert "<|im_start|>system\nrolling summary<|im_end|>" in out
    assert out.count("Reasoning effort") == 1


def test_no_system_message_standalone_block_before_tools():
    out = _dialect("xhigh").build_prompt(
        messages=[{"role": "user", "content": "hi"}], tools=_TOOLS,
        enable_thinking=True)
    assert out.startswith(f"<|im_start|>system\n{_XHIGH}<|im_end|>\n")
    assert out.index(_XHIGH) < out.index("# Tools")


def test_invalid_effort_rejected_at_assembly():
    with pytest.raises(ValueError, match="reasoning_effort"):
        _dialect("extreme")


def test_effort_recorded_for_observability():
    assert _dialect("low").reasoning_effort == "low"
    assert _dialect().reasoning_effort is None


def test_delta_rendered_system_not_injected():
    """增量路渲染的中段 system（滚动摘要/护栏）不得带效率指令。"""
    d = _dialect("xhigh")
    d.build_prompt(messages=list(_MSGS), enable_thinking=True)  # 建前缀
    out = d.build_prompt_delta(
        [{"role": "system", "content": "rolling summary"},
         {"role": "user", "content": "again"}],
        enable_thinking=True, prev_tail=d.tail_state())
    assert out is not None                  # 守卫不许被被测代码跳过（评审 m11）
    delta, _tail = out
    assert "Reasoning effort" not in delta


def test_gemma4_entry_with_effort_warns_but_serves(capsys):
    """qwen35 专属旋钮设到 gemma 条目：响亮提示、不拒启、不注入（评审 S16）。"""
    from types import SimpleNamespace

    class _Tok(dict):
        special_tokens_map = {}

        def encode(self, *a, **k):
            return [0]

    proc = SimpleNamespace(tokenizer=_Tok())
    d = get_dialect_class("gemma4").create(
        proc, options={"reasoning_effort": "xhigh"}, env={})
    assert "reasoning_effort" in capsys.readouterr().out
    assert d is not None


# ---------- 差分基准：官方模板渲染（本机无依据件则跳过） ----------

def _qwen_dir():
    for name in ("Qwen3.8-27B-4bit",):  # D27 唯一在册 qwen38 MLX 档
        d = _MODELS / name
        if (d / "chat_template.jinja").exists():
            return d
    return None


_QDIR = _qwen_dir()


@pytest.fixture(scope="module")
def qtok():
    transformers = pytest.importorskip("transformers")
    return transformers.AutoTokenizer.from_pretrained(str(_QDIR))


@pytest.mark.skipif(_QDIR is None,
                    reason="本机无 Qwen3.8 模型目录（差分基准需官方 chat template）")
@pytest.mark.parametrize("effort", REASONING_EFFORTS)
def test_sentence_bytes_match_official_template(effort, qtok):
    official = qtok.apply_chat_template(
        [{"role": "system", "content": "You are helpful."},
         {"role": "user", "content": "hi"}],
        tokenize=False, add_generation_prompt=True,
        enable_thinking=True, reasoning_effort=effort)
    ours = REASONING_INSTRUCTIONS[effort]
    if effort == "medium":
        assert "Reasoning effort" not in official
    else:
        start = official.index("Reasoning effort")
        assert official[start:start + len(ours)] == ours


@pytest.mark.skipif(_QDIR is None,
                    reason="本机无 Qwen3.8 模型目录（差分基准需官方 chat template）")
def test_our_injected_segment_equals_official_segment(qtok):
    """把我方 build 的注入段与官方渲染的注入段直接对齐（评审 m17：不许两测各钉
    一头、靠碰巧同一字面量间接相等）。"""
    official = qtok.apply_chat_template(
        [{"role": "system", "content": "You are helpful."},
         {"role": "user", "content": "hi"}],
        tokenize=False, add_generation_prompt=True, enable_thinking=True)
    ours = _dialect("xhigh").build_prompt(messages=list(_MSGS),
                                          enable_thinking=True)
    seg = "<|im_start|>system\n" + _XHIGH + "\n\nYou are helpful."
    assert seg in official and seg in ours
