"""tool_call 维度打分：工具调用格式准确率 + 选择正确率 + 纠错能力 + 无工具克制。

expect 两种形态：
  {"type": "tool_call",
   "call": {"name": "...", "args": {参数级匹配 spec}, "allow_extra": true},
   "final": {文本断言 spec，可省——省则不考终答}}
  {"type": "no_call",            # 陷阱题：声明了工具但不该调
   "final": {文本断言 spec，可省——省则只要求非空终答}}

产出字段（除 pass 外全是画像素材，聚合进 summary）：
  first_shot_ok   首轮首个调用即完全合格（格式+名字+参数）——路由最关心的数
  valid_call_ok   任一轮出现完全合格调用
  parse_errors    解析错误调用总数
  corrected       出过解析错误但最终发出合格调用（纠错闭环有效性）
  hallucinated_tools  调用了未声明工具的名字列表
  final_ok        终答文本断言（有 final 规格时）
  restraint_ok    陷阱题克制判定（no_call 专属）
"""
from typing import Any, Dict, List

from ..executor import error_calls, function_calls
from ..matchers import match_args, match_text


def _call_matches(call: Dict[str, Any], call_spec: Dict[str, Any]) -> bool:
    if call.get("name") != call_spec.get("name"):
        return False
    ok, _ = match_args(call_spec.get("args") or {}, call.get("arguments") or {},
                       allow_extra=call_spec.get("allow_extra", True))
    return ok


def score(traj: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    expect = item["expect"]
    kind = expect.get("type")
    if kind not in ("tool_call", "no_call"):
        raise ValueError(f"[{item['id']}] tool_call 维度 expect.type 须为 "
                         f"tool_call|no_call，得到 {kind!r}")
    turns = traj["turns"]
    declared = {t["name"] for t in (item["task"].get("tools") or [])}
    all_fn = [c for t in turns for c in function_calls(t)]
    parse_errors = sum(len(error_calls(t)) for t in turns)
    hallucinated = sorted({c.get("name", "") for c in all_fn
                           if declared and c.get("name") not in declared})
    final_text = turns[-1]["text"] if turns else ""
    final_spec = expect.get("final")
    if final_spec:
        final_ok, final_fails = match_text(final_spec, final_text)
    elif kind == "no_call":
        # 陷阱题不给 final 规格时至少要求非空直答（克制≠沉默）
        final_ok, final_fails = bool(final_text.strip()), []
    else:
        # 纯发射题（无脚本回喂、单轮观测）：调用即收束，正文为空是正常形态
        final_ok, final_fails = True, []
    why: List[str] = list(final_fails)

    result: Dict[str, Any] = {
        "parse_errors": parse_errors,
        "hallucinated_tools": hallucinated,
        "final_ok": final_ok,
        "turns_used": len(turns),
        "aborted": bool(traj.get("aborted_reason")),
    }

    if kind == "no_call":
        restraint_ok = not all_fn and parse_errors == 0
        if not restraint_ok:
            why.append(f"陷阱题失守：发出 {len(all_fn)} 个调用/{parse_errors} 个解析错误")
        result.update({"restraint_ok": restraint_ok,
                       "pass": restraint_ok and final_ok and not result["aborted"],
                       "why": why})
        return result

    call_spec = expect.get("call") or {}
    first_calls = turns[0].get("tool_calls", []) if turns else []
    first_shot_ok = bool(first_calls) \
        and first_calls[0].get("type") == "function" \
        and _call_matches(first_calls[0], call_spec)
    valid_call_ok = any(_call_matches(c, call_spec) for c in all_fn)
    if not valid_call_ok:
        got = [(c.get("name"), c.get("arguments")) for c in all_fn]
        why.append(f"无完全合格调用（期望 {call_spec.get('name')}，实际 {got!r}）")
    corrected = parse_errors > 0 and valid_call_ok
    result.update({
        "first_shot_ok": first_shot_ok,
        "valid_call_ok": valid_call_ok,
        "corrected": corrected,
        "pass": valid_call_ok and final_ok and not result["aborted"],
        "why": why,
    })
    return result
