"""维度打分器注册面。每个打分器：score(trajectory, item) -> dict（含 "pass" 总判定）。

全部规则判定（README「判分口径」）。未注册维度响亮 KeyError——静默跳过等于
把没打分的题混进画像。
"""
from . import longchain as _longchain
from . import text_answer as _text_answer
from . import tool_call as _tool_call

SCORERS = {
    "tool_call": _tool_call.score,
    "longchain": _longchain.score,
    "vision": _text_answer.score,
    "text": _text_answer.score,
}


def get_scorer(dimension: str):
    if dimension not in SCORERS:
        raise KeyError(f"维度 {dimension!r} 没有打分器（现有: {sorted(SCORERS)}）")
    return SCORERS[dimension]
