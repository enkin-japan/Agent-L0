"""RAG 检索侧纯函数金测试（评估采纳波：mem0 §3.2 子串召回 + grok-build §3.7 MMR）。

rag_server 导入即拉起 Chroma/配置/计算面分派，测试碰不得——两函数抽在
rag/retrieval_extras.py（零重依赖），本文件按路径 import 钉行为（S.7：没人
import 的入口要用金测试钉住，这里钉的是「backend 侧对 rag 纯函数层的行为契约」）。
"""
import importlib.util
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parents[2] / "rag" / "retrieval_extras.py"
_spec = importlib.util.spec_from_file_location("retrieval_extras", _MOD_PATH)
rx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rx)


# ---------- extract_identifiers ----------

def test_identifier_shapes_extracted():
    toks = rx.extract_identifiers(
        "commit 755ad0b 改了 config.yaml 里的 RAG_API_KEY，路径 rag/scripts/reembed.py")
    assert "755ad0b" in toks
    assert "config.yaml" in toks
    assert "RAG_API_KEY" in toks


def test_semantic_words_and_pure_numbers_excluded():
    toks = rx.extract_identifiers("hello world 用户偏好 2026 12345 tomorrow")
    assert toks == []          # 纯字母=语义词归向量；纯数字=日期/数量太噪


def test_cap_longest_first_and_dedupe():
    toks = rx.extract_identifiers(
        "a_1 bb_22 ccc_333 dddd_4444 eeeee_55555 A_1")
    assert len(toks) == rx.MAX_IDENT_TOKENS
    assert toks[0] == "eeeee_55555"            # 最长优先
    assert sum(1 for t in toks if t.lower() == "a_1") <= 1   # 大小写去重


def test_empty_and_none_inputs():
    assert rx.extract_identifiers("", "   ") == []


# ---------- mmr_select ----------

def _chunk(doc, score):
    return {"document": doc, "rerank_score": score}


def test_mmr_passthrough_when_no_choice():
    chunks = [_chunk("a", 0.9), _chunk("b", 0.8)]
    assert rx.mmr_select(chunks, 5) == chunks   # 候选不足名额：行为与旧截断全同


def test_mmr_keeps_top1_and_prefers_diversity():
    near_dup = "同一份文档的相邻切片，内容几乎一样，讲部署步骤第一步第二步"
    chunks = [
        _chunk(near_dup + "甲", 0.95),
        _chunk(near_dup + "乙", 0.94),          # 与第一名近重复
        _chunk("完全不同主题：RAG 密钥轮换的注意事项与两处同步", 0.90),
    ]
    out = rx.mmr_select(chunks, 2)
    assert out[0] is chunks[0]                  # 第一名绝不被牺牲
    assert out[1] is chunks[2]                  # 第二个名额让给不同主题而非近重复


def test_mmr_score_dominates_when_all_diverse():
    chunks = [_chunk(f"主题{i}完全不相干内容{'x' * i}", 0.9 - i * 0.1) for i in range(4)]
    out = rx.mmr_select(chunks, 3)
    assert [c["rerank_score"] for c in out] == [0.9, 0.8, 0.7]   # 无冗余时按分取
