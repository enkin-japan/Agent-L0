"""期望匹配器（纯函数，金测试 test_model_eval_harness.py 钉规则）。

单值匹配 spec 是一个 dict，键为条件，**全部条件须同时成立**：
  {"eq": v}                 完全相等（类型敏感——int 15 ≠ str "15"，方言参数
                            类型还原是否正确正是要测的点）
  {"in": [v1, v2]}          在候选集内
  {"regex": "pat"}          str(value) 的 re.search（大小写敏感；不敏感自带 (?i)）
  {"type": "integer|number|string|boolean|array|object"}   JSON 类型判定
  {"contains": "s"}         str 包含 / array 成员包含
  {"contains_all": [...]}   多个 contains 全部成立
参数级修饰：
  {"optional": true, ...}   参数缺席即通过；出席则按其余条件判

文本断言 spec（match_text）：
  {"must_all": [...], "must_any": [...], "must_not": [...], "regex_any": [...]}
  regex_any：任一正则 re.search 命中即过——短数字答案用词边界（r"\b3\b"）防
  子串误判（"3" 落进 "13"），大小写不敏感自带 (?i)。
"""
import re
from typing import Any, Dict, List, Tuple

_JSON_TYPES = {
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}

_KNOWN_CONDS = {"eq", "in", "regex", "type", "contains", "contains_all", "optional"}


def _contains(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, (list, tuple)):
        return any(_contains(v, needle) for v in value)
    return needle in str(value)


def match_value(spec: Dict[str, Any], value: Any) -> Tuple[bool, str]:
    """按 spec 判单值。返回 (是否通过, 失败原因串——通过时为 "")。"""
    unknown = set(spec) - _KNOWN_CONDS
    if unknown:
        raise ValueError(f"匹配器不认识的条件键: {sorted(unknown)}（防拼错静默放行）")
    if "eq" in spec and value != spec["eq"]:
        return False, f"eq 失败: {value!r} != {spec['eq']!r}"
    if "in" in spec and value not in spec["in"]:
        return False, f"in 失败: {value!r} 不在 {spec['in']!r}"
    if "regex" in spec and not re.search(spec["regex"], str(value)):
        return False, f"regex 失败: {spec['regex']!r} 不匹配 {str(value)[:80]!r}"
    if "type" in spec:
        checker = _JSON_TYPES.get(spec["type"])
        if checker is None:
            raise ValueError(f"匹配器不认识的类型名: {spec['type']!r}")
        if not checker(value):
            return False, f"type 失败: 期望 {spec['type']}，实际 {type(value).__name__}"
    if "contains" in spec and not _contains(value, spec["contains"]):
        return False, f"contains 失败: {spec['contains']!r} 不在 {str(value)[:80]!r}"
    if "contains_all" in spec:
        for needle in spec["contains_all"]:
            if not _contains(value, needle):
                return False, f"contains_all 失败: 缺 {needle!r}"
    return True, ""


def match_args(spec: Dict[str, Dict[str, Any]], args: Dict[str, Any],
               allow_extra: bool = True) -> Tuple[bool, List[str]]:
    """按参数级 spec 判整个 arguments。返回 (是否通过, 失败原因列表)。

    allow_extra=False 时，args 出现 spec 之外的键即失败（幻觉参数探测）。
    """
    fails: List[str] = []
    for key, vspec in spec.items():
        if key not in args:
            if not vspec.get("optional"):
                fails.append(f"缺必需参数 {key!r}")
            continue
        ok, why = match_value({k: v for k, v in vspec.items() if k != "optional"},
                              args[key])
        if not ok:
            fails.append(f"参数 {key!r}: {why}")
    if not allow_extra:
        extra = set(args) - set(spec)
        if extra:
            fails.append(f"多余参数: {sorted(extra)}")
    return not fails, fails


def match_text(spec: Dict[str, List[str]], text: str) -> Tuple[bool, List[str]]:
    """文本断言：must_all 全含 / must_any 至少一含 / must_not 全不含 / regex_any 任一中。"""
    unknown = set(spec) - {"must_all", "must_any", "must_not", "regex_any"}
    if unknown:
        raise ValueError(f"文本断言不认识的键: {sorted(unknown)}")
    fails: List[str] = []
    for needle in spec.get("must_all", []):
        if needle not in text:
            fails.append(f"must_all 缺 {needle!r}")
    any_list = spec.get("must_any", [])
    if any_list and not any(n in text for n in any_list):
        fails.append(f"must_any 全缺: {any_list!r}")
    rx_list = spec.get("regex_any", [])
    if rx_list and not any(re.search(rx, text) for rx in rx_list):
        fails.append(f"regex_any 全不中: {rx_list!r}")
    for needle in spec.get("must_not", []):
        if needle in text:
            fails.append(f"must_not 出现 {needle!r}")
    return not fails, fails
