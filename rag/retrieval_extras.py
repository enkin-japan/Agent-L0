"""检索侧小增强的纯函数层（评估采纳波，2026-07-18）：
① extract_identifiers——从查询里抽「长得像标识符」的 token（文件名/commit hash/
  键名/URL 片段），供数据面做 contains 子串预取。纯向量召回会把这类精确标识符
  摊平成语义噪声（mem0 评估 §3.2 的结论；不搬其英文中心的 BM25/spaCy 栈）。
② mmr_select——重排后的 MMR 多样性选择（grok-build 评估 §3.7）：防 top-k 全是
  同一文档的近邻块。相似度用**字符二元组 Jaccard**（中日文无空格分词也成立），
  纯 CPU 零依赖，不动计算面契约。

单独成模块的原因：rag_server 导入即拉起 Chroma/配置/计算面分派，测试没法碰；
这里零重依赖，backend 金测试按文件路径 import 钉住两函数的行为。
"""
import re
from typing import Any, Callable, Dict, List

# 标识符形状：字母数字与 _-./ 的连串，长度 ≥4；再要求含数字或分隔符
# （纯小写英文单词不算——那是语义词，归向量管）。纯数字串也不算（日期/数量太噪）。
_IDENT_RE = re.compile(r"[A-Za-z0-9_\-./]{4,}")
_HAS_MARK = re.compile(r"[\d._/\-]")

MAX_IDENT_TOKENS = 4          # 每次查询最多取几个标识符（最长优先）
_MMR_LAMBDA = 0.75            # 相关性 vs 多样性权重（护栏参数，实测再调）


def extract_identifiers(*texts: str, max_tokens: int = MAX_IDENT_TOKENS) -> List[str]:
    """抽标识符 token，最长优先、去重、cap。返回原始大小写（contains 大小写敏感）。"""
    seen = set()
    cands: List[str] = []
    for text in texts:
        for tok in _IDENT_RE.findall(text or ""):
            if tok.isdigit():
                continue                      # 纯数字：日期/数量，太噪
            if not _HAS_MARK.search(tok):
                continue                      # 纯字母词：语义词归向量管
            if tok.strip("./-_") == "":
                continue
            if tok.lower() in seen:
                continue
            seen.add(tok.lower())
            cands.append(tok)
    return sorted(cands, key=len, reverse=True)[:max_tokens]


def _bigrams(text: str) -> set:
    t = "".join((text or "").split())
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t} if t else set()


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def mmr_select(chunks: List[Dict[str, Any]], k: int, *,
               lambda_: float = _MMR_LAMBDA,
               text_of: Callable[[Dict[str, Any]], str] = lambda c: c.get("document", ""),
               score_of: Callable[[Dict[str, Any]], float] = lambda c: c.get("rerank_score", 0.0),
               ) -> List[Dict[str, Any]]:
    """贪心 MMR：每步选 λ·相关分 − (1−λ)·与已选集的最大相似度 最高者。

    入参 chunks 应已按相关分降序（首选必是最高分，MMR 不会牺牲第一名）。
    k ≥ len(chunks) 时原样返回（没得挑就不挑）。
    """
    if k >= len(chunks):
        return list(chunks)
    grams = [_bigrams(text_of(c)) for c in chunks]
    selected: List[int] = [0]                  # 已按分数降序：第一名直取
    remaining = list(range(1, len(chunks)))
    while remaining and len(selected) < k:
        best_i, best_val = None, None
        for i in remaining:
            max_sim = max(_jaccard(grams[i], grams[j]) for j in selected)
            val = lambda_ * score_of(chunks[i]) - (1.0 - lambda_) * max_sim
            if best_val is None or val > best_val:
                best_i, best_val = i, val
        selected.append(best_i)
        remaining.remove(best_i)
    return [chunks[i] for i in selected]
