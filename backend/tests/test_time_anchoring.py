"""时间锚定纪律金测试（mem0 评估采纳，2026-07-18）。

守护的失败形态：沉淀进 agent_memory 的对话原文与滚动摘要里残留「明天/上周」类
相对时间——几个月后召回时已无法换算，等于存了会过期的谜语。锚定三件套：
①折叠文本每轮头部盖 [YYYY-MM-DD HH:MM] 日期戳（摘要器换算的依据 + 沉淀原文
自带日期）；②摘要指令明令相对转绝对；③笔记工具指引同句。本组钉住三处不被
后续 prompt 修剪静默削掉。
"""
from framework.memory.compactor import _turn_to_text
from framework.core.engine import AgentEngine
from framework.mcp.tools.memory_write import MemoryWriteTool


def test_turn_text_carries_date_stamp():
    turn = {"timestamp": 1786946400.0, "prompt": "明天帮我订票",
            "response": [{"role": "assistant", "content": "好的"}]}
    text = _turn_to_text(turn)
    first_line = text.splitlines()[0]
    # 具体日期随时区，锚定形状即可：首行是 [YYYY-MM-DD HH:MM]
    assert first_line.startswith("[20") and first_line.endswith("]")
    assert ":" in first_line and len(first_line) == len("[2026-07-18 14:00]")
    assert "User: 明天帮我订票" in text


def test_turn_text_survives_missing_or_bad_timestamp():
    assert _turn_to_text({"prompt": "hi", "response": []}).startswith("User: hi")
    out = _turn_to_text({"timestamp": "garbage", "prompt": "hi", "response": []})
    assert out.startswith("User: hi")  # 坏时间戳静默跳过，不冠错误行


def test_summary_instruction_grounds_time():
    ins = AgentEngine.SUMMARY_INSTRUCTION
    assert "[YYYY-MM-DD HH:MM]" in ins
    assert "absolute dates" in ins


def test_memory_write_body_demands_absolute_dates():
    desc = MemoryWriteTool.parameters["properties"]["body"]["description"]
    assert "absolute" in desc and "not 'today'" in desc
