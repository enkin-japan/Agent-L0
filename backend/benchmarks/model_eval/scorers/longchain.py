"""longchain 维度打分：终态断言（执行≠成功，以 mock 环境终态为准）+ 过程效率。

expect 形态：
  {"type": "end_state",
   "asserts": [
     {"path": ["files", "report.md"], "exists": true, "contains": "总数"},
     {"path": ["files", "draft.txt"], "exists": false},
     {"path": ["todos"], "contains_obj": {"text": "买菜", "done": true}}
   ],
   "max_steps": 6}          # 可省：不设步数上限

path 用列表寻址（文件名含点，点分路径会碎）。断言条件 = exists / contains_obj
（列表中存在字典超集元素）/ 其余全部复用 matchers.match_value 词汇。

过程指标（attention 基准同思路——过程痕迹全部确定性）：
  steps            function 调用总数
  redundant_calls  同名同参重复调用次数（死循环倾向）
  env_errors       环境错误回执次数（找不到文件等）
  recovered        出过环境错误但终态断言仍全过（错误恢复能力）
"""
import json
from typing import Any, Dict, List, Tuple

from ..executor import function_calls
from ..matchers import match_value

_MISSING = object()


def _resolve(state: Dict[str, Any], path: List[str]):
    node = state
    for p in path:
        if isinstance(node, dict) and p in node:
            node = node[p]
        else:
            return _MISSING
    return node


def check_assert(state: Dict[str, Any], spec: Dict[str, Any]) -> Tuple[bool, str]:
    path = spec.get("path")
    if not isinstance(path, list) or not path:
        raise ValueError(f"end_state 断言缺列表形 path: {spec!r}")
    value = _resolve(state, [str(p) for p in path])
    label = "/".join(str(p) for p in path)
    if "exists" in spec:
        if bool(value is not _MISSING) != bool(spec["exists"]):
            return False, f"{label}: exists 期望 {spec['exists']}，实际 {value is not _MISSING}"
        if not spec["exists"]:
            return True, ""
    if value is _MISSING:
        return False, f"{label}: 路径不存在"
    if "contains_obj" in spec:
        want = spec["contains_obj"]
        hit = isinstance(value, list) and any(
            isinstance(el, dict) and all(el.get(k) == v for k, v in want.items())
            for el in value)
        if not hit:
            return False, f"{label}: 无元素匹配 {want!r}（实际 {value!r}）"
    rest = {k: v for k, v in spec.items()
            if k not in ("path", "exists", "contains_obj")}
    if rest:
        ok, why = match_value(rest, value)
        if not ok:
            return False, f"{label}: {why}"
    return True, ""


def score(traj: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    expect = item["expect"]
    if expect.get("type") != "end_state":
        raise ValueError(f"[{item['id']}] longchain 维度 expect.type 须为 end_state")
    state = traj.get("env_state")
    if state is None:
        raise ValueError(f"[{item['id']}] 轨迹缺 env_state（题面须声明 task.env）")

    why: List[str] = []
    for spec in expect.get("asserts") or []:
        ok, msg = check_assert(state, spec)
        if not ok:
            why.append(msg)
    state_ok = not why

    all_calls = [c for t in traj["turns"] for c in function_calls(t)]
    steps = len(all_calls)
    seen: Dict[str, int] = {}
    for c in all_calls:
        key = f"{c.get('name')}::{json.dumps(c.get('arguments'), sort_keys=True, ensure_ascii=False)}"
        seen[key] = seen.get(key, 0) + 1
    redundant = sum(n - 1 for n in seen.values() if n > 1)

    env_errors = 0
    for t in traj["turns"]:
        for f in t.get("feed") or []:
            try:
                if isinstance(json.loads(f.get("content") or ""), dict) \
                        and "error" in json.loads(f["content"]):
                    env_errors += 1
            except (json.JSONDecodeError, TypeError):
                pass

    max_steps = expect.get("max_steps")
    steps_ok = steps <= max_steps if max_steps else True
    if not steps_ok:
        why.append(f"步数超限: {steps} > {max_steps}")
    aborted = bool(traj.get("aborted_reason"))
    if aborted:
        why.append(traj["aborted_reason"])
    final_text_ok = bool((traj["turns"][-1]["text"] or "").strip()) if traj["turns"] else False
    if not final_text_ok:
        why.append("无收尾答复")

    return {"pass": state_ok and steps_ok and final_text_ok and not aborted,
            "state_ok": state_ok, "steps": steps, "steps_ok": steps_ok,
            "redundant_calls": redundant, "env_errors": env_errors,
            "recovered": env_errors > 0 and state_ok,
            "final_text_ok": final_text_ok, "aborted": aborted, "why": why}
