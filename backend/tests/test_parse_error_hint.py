"""解析纠错文案的方言归属金测试（评测框架波 E0）。

守的是什么：容错闭环回喂"正确格式教学"时，格式段是**方言事实**——gemma4 教 JSON
调用体、qwen35 教 XML 调用体。此前编排层硬编码 JSON 教学，qwen35 模型发错格式后
会被文案往 JSON 方向带偏（越纠越错，且污染评测的纠错成功率指标）。逐条钉死：
教学段来自 dialect.parse_error_retry_hint()，且与各自 prompt 教学格式锚定一致。
"""
import inspect

from framework.core import orchestrator
from framework.core.orchestrator import _PARSE_HINT_FALLBACK, parse_error_feedback
from framework.dialects.gemma4.dialect import Gemma4Dialect
from framework.dialects.qwen35.dialect import Qwen35Dialect
from framework.dialects.qwen35.prompt_builder import _TOOL_FORMAT_GUIDE


class _Tok:
    special_tokens_map = {}

    def encode(self, s, add_special_tokens=False):
        return [1]


class _Proc:
    tokenizer = _Tok()


def test_gemma4_hint_teaches_json_verbatim():
    """gemma4 教学段 = 原编排层硬编码文案逐字（E0 对 gemma4 行为零变化）。"""
    hint = Gemma4Dialect.create(_Proc(), options={}, env={}).parse_error_retry_hint()
    assert "VALID JSON with double quotes" in hint
    assert '{"name": "update_plan", "arguments"' in hint
    assert "Retry with correct JSON." in hint
    # 决不能出现 XML 教学（两方言文案串味 = 接缝白开）
    assert "<tool_call>" not in hint


def test_qwen35_hint_teaches_xml_not_json():
    """qwen35 教学段教 XML 调用体，且明确禁止 JSON——这正是接缝要修的事故方向。"""
    hint = Qwen35Dialect.create(_Proc(), options={}, env={}).parse_error_retry_hint()
    assert "<tool_call>" in hint and "</tool_call>" in hint
    assert "<function=" in hint and "<parameter=" in hint
    assert "Do NOT use JSON" in hint
    # 决不能把模型往 JSON 带（原事故形态）
    assert "VALID JSON" not in hint


def test_qwen35_hint_anchored_to_prompt_teaching():
    """纠错教学与 prompt 首次教学必须是同一格式骨架（锚定一致，防各改各的漂移）。"""
    hint = Qwen35Dialect.create(_Proc(), options={}, env={}).parse_error_retry_hint()
    for anchor in ("<tool_call>\n<function=example_function_name>\n",
                   "<parameter=example_parameter_1>\nvalue_1\n</parameter>\n",
                   "</function>\n</tool_call>"):
        assert anchor in hint and anchor in _TOOL_FORMAT_GUIDE


def test_feedback_envelope_first_and_repeat():
    """信封形态：首错=原文回显+错误原文+方言教学段；复读=升级止损文案（不再回显）。"""
    call = {"raw_input": "<function=x> broken", "error_msg": "Malformed tool call"}
    first = parse_error_feedback(call, 1, "HINT-SENTINEL")
    assert first.startswith("System Error: your tool call could not be parsed.")
    assert "<function=x> broken" in first and "Malformed tool call" in first
    assert first.endswith("HINT-SENTINEL")
    repeat = parse_error_feedback(call, 3, "HINT-SENTINEL")
    assert "3 times" in repeat and "STOP repeating it" in repeat
    # 复读升级不再含格式教学（模型已两见教学，此时要止损不要复读）
    assert "HINT-SENTINEL" not in repeat


def test_orchestrator_reads_hint_from_dialect():
    """编排层错误分支必须经 dialect.parse_error_retry_hint 取教学段（getattr 兜底
    _PARSE_HINT_FALLBACK 只为 FakeEngine 类无方言场景），不得再硬编码格式教学。"""
    src = inspect.getsource(orchestrator)
    assert "parse_error_retry_hint" in src
    assert src.count("VALID JSON with double quotes") == 0  # 硬编码 JSON 教学已迁出
    assert "one complete, well-formed block" in _PARSE_HINT_FALLBACK
