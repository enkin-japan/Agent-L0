"""cot_purity 维度：思维链纯净度——不设专用题，随任何开思考的轨迹顺带采集。

「纯净度」的可操作定义（全部确定性信号，不做主观判断）：
  ctrl_leaks        方言控制标记漏进可见正文（parser 应剥净；出现即事故级不纯）
  dup_rate          thoughts 内部复读度：重复 16 字符窗占比（0=无复读）
  recite_rate       正文对 thoughts 的转述率：正文 12 字符窗落在 thoughts 里的占比
                    （高=思考内容被整段搬进答复，思/答不分层）
  scripts           thoughts 的文字系统构成：cjk/latin/other 三桶字母占比——
                    other（西里尔/谚文/希腊等）>0 是 9B 级模型思考跑飞的典型症状；
                    cjk/latin 互混不判罚（中英混思对双语模型属正常）
  empty_think       开思考却零思考输出（预填被跳过/立即收束）
长度只记字符数（think_chars）：后端 metrics 不单列思考 token，不做换算冒充精度。
"""
import re
from typing import Any, Dict, List

_MARKERS = ("<think>", "</think>", "<tool_call>", "</tool_call>",
            "<|im_start|>", "<|im_end|>", "<function=", "<parameter=",
            "<start_of_turn>", "<end_of_turn>")

_WS_RE = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS_RE.sub(" ", s.strip())


def dup_rate(s: str, n: int = 16) -> float:
    s = _norm(s)
    grams = [s[i:i + n] for i in range(max(0, len(s) - n + 1))]
    if not grams:
        return 0.0
    return round(1 - len(set(grams)) / len(grams), 3)


def recite_rate(text: str, thoughts: str, n: int = 12) -> float:
    text, thoughts = _norm(text), _norm(thoughts)
    grams = [text[i:i + n] for i in range(max(0, len(text) - n + 1))]
    if not grams or len(thoughts) < n:
        return 0.0
    pool = {thoughts[i:i + n] for i in range(len(thoughts) - n + 1)}
    return round(sum(1 for g in grams if g in pool) / len(grams), 3)


def script_ratios(s: str) -> Dict[str, float]:
    cjk = latin = other = 0
    for ch in s:
        if not ch.isalpha():
            continue
        o = ord(ch)
        if (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or
                0x3040 <= o <= 0x30FF or 0xF900 <= o <= 0xFAFF):
            cjk += 1
        elif o <= 0x024F:      # 基本拉丁 + 各类扩展（西欧变音符算 latin）
            latin += 1
        else:
            other += 1
    total = cjk + latin + other
    if not total:
        return {"cjk": 0.0, "latin": 0.0, "other": 0.0}
    return {"cjk": round(cjk / total, 3), "latin": round(latin / total, 3),
            "other": round(other / total, 3)}


def analyze(traj: Dict[str, Any], gen: Dict[str, Any]) -> Dict[str, Any]:
    """单轨迹纯净度画像（enable_thinking 关闭时返回 None 由调用方跳过）。"""
    if not bool((gen or {}).get("enable_thinking", True)):
        return None
    turns: List[Dict[str, Any]] = []
    for t in traj["turns"]:
        th, tx = t.get("thoughts") or "", t.get("text") or ""
        turns.append({
            "ctrl_leaks": [m for m in _MARKERS if m in tx],
            "think_chars": len(th),
            "empty_think": not th.strip(),
            "dup_rate": dup_rate(th),
            "recite_rate": recite_rate(tx, th),
            "scripts": script_ratios(th),
        })
    return {
        "turns": turns,
        "any_ctrl_leak": any(t["ctrl_leaks"] for t in turns),
        "max_dup_rate": max((t["dup_rate"] for t in turns), default=0.0),
        "max_recite_rate": max((t["recite_rate"] for t in turns), default=0.0),
        "max_other_script": max((t["scripts"]["other"] for t in turns), default=0.0),
        "empty_think_turns": sum(1 for t in turns if t["empty_think"]),
        "think_chars_total": sum(t["think_chars"] for t in turns),
    }


def aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """运行级聚合（records 为带 "cot" 字段的轨迹记录）。"""
    cots = [r["cot"] for r in records if r.get("cot")]
    if not cots:
        return {}
    n = len(cots)
    return {
        "n": n,
        "ctrl_leak_traj_rate": round(sum(1 for c in cots if c["any_ctrl_leak"]) / n, 3),
        "dup_rate_mean": round(sum(c["max_dup_rate"] for c in cots) / n, 3),
        "dup_rate_max": max(c["max_dup_rate"] for c in cots),
        "recite_rate_mean": round(sum(c["max_recite_rate"] for c in cots) / n, 3),
        "recite_rate_max": max(c["max_recite_rate"] for c in cots),
        "other_script_max": max(c["max_other_script"] for c in cots),
        "empty_think_turns_total": sum(c["empty_think_turns"] for c in cots),
        "think_chars_mean": round(sum(c["think_chars_total"] for c in cots) / n, 1),
    }
