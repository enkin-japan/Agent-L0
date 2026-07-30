"""vision / text 维度共用打分：终答文本断言（关键词/排除词，全部规则判定）。

expect 形态：
  {"type": "text", "final": {"must_all": [...], "must_any": [...], "must_not": [...]}}

视觉题的正确率就是「答案文本含关键信息」——题面设计时关键信息须唯一可辨
（README 鉴定纪律第 2 条：判分可程序化），不搞语义相似度。
stray_calls 记录无端工具调用（视觉题通常不给工具；给了工具的题此值恒 0 参考）。
"""
from typing import Any, Dict

from ..executor import function_calls
from ..matchers import match_text


def score(traj: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    expect = item["expect"]
    if expect.get("type") != "text":
        raise ValueError(f"[{item['id']}] {item['dimension']} 维度 expect.type 须为 text")
    turns = traj["turns"]
    final_text = turns[-1]["text"] if turns else ""
    ok, fails = match_text(expect.get("final") or {}, final_text)
    aborted = bool(traj.get("aborted_reason"))
    return {"pass": ok and not aborted,
            "final_ok": ok,
            "stray_calls": sum(len(function_calls(t)) for t in turns),
            "aborted": aborted,
            "why": fails + ([traj["aborted_reason"]] if aborted else [])}
