"""最终答复连贯度启发式（纯记录，不进 pass 判定）。

探针里出现过「终态断言通过、答复却是多语种乱码」——字符层重复抓不到这种语义崩坏，但它有个
稳定信号：漂进任务语言之外的文字系统（zh 题的乱码里冒出日文假名夹在英文词中间）。
  expected_scripts      按题 lang：zh → 拉丁 + CJK 统一表意文字；en → 拉丁；ja → 拉丁 + CJK + 假名；
                        lang 未知按 zh 口径（最宽的常见集）
  foreign_script_ratio  不属预期文字系统的字母类字符（Unicode L*：假名/谚文/西里尔/泰文…）/ 全部字母类字符
  mixed_token_ratio     同一空白 token 内同时含拉丁字母与 CJK/假名的 token 数 / token 数
                        （zh/ja 题只记录不进判据——中文里夹英文标识符是正常写法）
  kana_hangul_count     假名 + 谚文字符数（原值）
  dup_ngram_ratio       RepetitionDetector 同款 16-gram 重复占比（直接调 _dup_ratio，不复制实现）
  script_mix / longest_run  旧指标，照旧记录，不进判据
  coherent  foreign_script_ratio < 0.01 且 dup_ngram_ratio < 0.3 且（仅 en 题）mixed_token_ratio < 0.05；
            空答复或纯空白答复为 None。summarize.py --coherent-* 可改阈值并按记录 lang 重算，不必重跑。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from framework.core.repetition_guard import RepetitionDetector

COHERENT_THRESHOLDS = {"dup_ngram_ratio": 0.3, "foreign_script_ratio": 0.01,
                       "mixed_token_ratio": 0.05}
EXPECTED_SCRIPTS = {"zh": ("latin", "han"), "en": ("latin",), "ja": ("latin", "han", "kana")}
_MIXED_IN_CRITERION = ("en",)


def letter_script(ch: str) -> str:
    """字母类字符的文字系统：latin / han / kana / hangul / other（其它字母）。"""
    o = ord(ch)
    if ch.isascii() or 0x00C0 <= o <= 0x024F or 0x1E00 <= o <= 0x1EFF or 0xFF21 <= o <= 0xFF5A:
        return "latin"
    if 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or o >= 0x20000:
        return "han"
    if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF or 0xFF66 <= o <= 0xFF9F:
        return "kana"
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "hangul"
    return "other"


def _expected(lang: Optional[str]) -> tuple:
    return EXPECTED_SCRIPTS.get(lang or "zh", EXPECTED_SCRIPTS["zh"])
_MAX_CHARS = 20000
_DETECTOR = RepetitionDetector(ngram=16)


def _script(ch: str) -> str:
    o = ord(ch)
    if (0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
            or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF or 0xFF66 <= o <= 0xFF9F):
        return "cjk"
    if ch.isascii() and ch.isalpha() or (0x00C0 <= o <= 0x024F and ch.isalpha()):
        return "latin"
    return "other"


def longest_run(text: str, max_unit: int = 4) -> int:
    best = 1 if text else 0
    n = len(text)
    for L in range(1, max_unit + 1):
        i = 0
        while i + L <= n:
            unit = text[i:i + L]
            if unit.strip():
                k = 1
                while text[i + k * L:i + (k + 1) * L] == unit:
                    k += 1
                if k > best:
                    best = k
                if k > 1:
                    i += (k - 1) * L
                    continue
            i += 1
    return best


def is_coherent(q: Dict[str, Any], thresholds: Optional[Dict[str, float]] = None) -> Optional[bool]:
    if not q or not q.get("len_chars") or "foreign_script_ratio" not in q or q.get("blank"):
        return None
    t = {**COHERENT_THRESHOLDS, **(thresholds or {})}
    ok = (q["foreign_script_ratio"] < t["foreign_script_ratio"]
          and q["dup_ngram_ratio"] < t["dup_ngram_ratio"])
    if q.get("lang") in _MIXED_IN_CRITERION:
        ok = ok and q["mixed_token_ratio"] < t["mixed_token_ratio"]
    return ok


def final_text_quality(text: str, lang: Optional[str] = None) -> Dict[str, Any]:
    text = (text or "")[:_MAX_CHARS]
    expected = _expected(lang)
    alpha = [letter_script(c) for c in text if c.isalpha()]
    foreign = sum(1 for sc in alpha if sc not in expected)
    tokens = text.split()
    mixed_tok = 0
    for w in tokens:
        scs = {letter_script(c) for c in w if c.isalpha()}
        if "latin" in scs and scs & {"han", "kana"}:
            mixed_tok += 1
    chars = [c for c in text if not c.isspace()]
    counts = {"cjk": 0, "latin": 0, "other": 0}
    for c in chars:
        counts[_script(c)] += 1
    total = max(len(chars), 1)
    words = []
    mixed = 0
    for w in text.split():
        letters = [_script(c) for c in w if _script(c) != "other"]
        if not letters:
            continue
        if "cjk" in letters and "latin" in letters:
            mixed += 1
        words.append("cjk" if letters.count("cjk") >= letters.count("latin") else "latin")
    switches = sum(1 for a, b in zip(words, words[1:]) if a != b)
    q = {
        "len_chars": len(text),
        "script_mix": {"cjk": round(counts["cjk"] / total, 3),
                       "latin": round(counts["latin"] / total, 3),
                       "other": round(counts["other"] / total, 3),
                       "words": len(words), "switches": switches,
                       "switch_rate": round(switches / len(words), 3) if words else 0.0,
                       "mixed_words": mixed},
        "dup_ngram_ratio": round(_DETECTOR._dup_ratio(text), 3),
        "longest_run": longest_run(text),
        "lang": lang,
        "blank": not text.strip(),          # 纯空白答复与空答复同样不判连贯
        "expected_scripts": list(expected),
        "letters": len(alpha),
        "foreign_script_ratio": round(foreign / len(alpha), 4) if alpha else 0.0,
        "mixed_token_ratio": round(mixed_tok / len(tokens), 4) if tokens else 0.0,
        "kana_hangul_count": sum(1 for sc in alpha if sc in ("kana", "hangul")),
    }
    q["coherent"] = is_coherent(q)
    return q
