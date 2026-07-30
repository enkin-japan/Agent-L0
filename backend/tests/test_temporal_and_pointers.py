"""时间双轴与凭证指针金测试（数据架构采纳波 R3）。

守的是什么：
① 笔记 valid_until（事实生效终点）与 updated（记录时间）构成双时间轴的最小形态——
  过期必须在**索引行**显式标注（模型看到"已过期"，而非自信引用旧事实：个人记忆
  系统最高频事故）；格式非法就地拒绝（错日期比没日期更毒）。
② chunk 凭证指针（seq/char_start）与取相邻块链路的接线点（rag 独立 venv，源码级钉板）。
"""
from datetime import date, timedelta
from pathlib import Path

import pytest

from framework.memory.notes import NoteStore

RAG_SERVER_SRC = (Path(__file__).resolve().parents[2] / "rag" / "rag_server.py").read_text(
    encoding="utf-8")


def _store(tmp_path):
    return NoteStore(tmp_path / "notes")


def test_valid_until_roundtrip_and_index_tail(tmp_path):
    s = _store(tmp_path)
    future = (date.today() + timedelta(days=30)).isoformat()
    s.write("insurance-policy", "家庭保险单要点", "reference", "保单号 X，年缴。",
            valid_until=future)
    line = [l for l in s.index_lines() if "insurance-policy" in l][0]
    assert f"(valid until {future})" in line and "EXPIRED" not in line
    # 落盘 frontmatter 可回读（重启后语义不丢）
    s2 = NoteStore(tmp_path / "notes")
    assert s2.get("insurance-policy").valid_until == future


def test_expired_note_flagged_in_index_and_memory_md(tmp_path):
    s = _store(tmp_path)
    past = (date.today() - timedelta(days=3)).isoformat()
    s.write("old-wifi-password", "访客 WiFi 密码", "reference", "密码 abc。",
            valid_until=past)
    line = [l for l in s.index_lines() if "old-wifi-password" in l][0]
    assert f"[EXPIRED {past}" in line and "verify before" in line
    memory_md = (tmp_path / "notes" / "MEMORY.md").read_text(encoding="utf-8")
    assert f"⚠️已过期 {past}" in memory_md


def test_durable_note_unchanged_and_bad_date_rejected(tmp_path):
    """耐久事实（不带 valid_until）索引行与既有格式逐字同形（零行为变化）。"""
    s = _store(tmp_path)
    s.write("prefers-terse", "回复要短", "feedback", "长话短说。")
    line = [l for l in s.index_lines() if "prefers-terse" in l][0]
    assert line == "- prefers-terse (feedback): 回复要短"
    with pytest.raises(ValueError, match="ISO 日期"):
        s.write("x-note", "d", "user", "b", valid_until="下个月")
    with pytest.raises(ValueError, match="ISO 日期"):
        s.write("x-note", "d", "user", "b", valid_until="2026-13-40")


def test_memory_write_tool_exposes_valid_until():
    from framework.mcp.tools.memory_write import MemoryWriteTool
    props = MemoryWriteTool.parameters["properties"]
    assert "valid_until" in props and "valid_until" not in MemoryWriteTool.parameters["required"]
    assert "EXPIRED" in props["valid_until"]["description"]


# ---- 凭证指针（rag 侧源码钉板 + 工具面） ----

def test_chunk_pointer_wiring_pinned():
    """seq/char_start 注入与取邻块端点：漏接=指针链路断裂且症状隐蔽（旧库照常跑）。"""
    for anchor in ('"seq": i, "char_start": off',
                   'def document_context(source_path: str, seq: int, n: int = 1):',
                   'chunk_pairs = split_text_to_chunks(raw_document)'):
        assert anchor in RAG_SERVER_SRC, f"rag_server 缺凭证指针接线: {anchor}"


def test_rag_context_tool_surface():
    from framework.mcp.tools.rag_search import RagGetContextTool, format_results
    t = RagGetContextTool
    assert t.name == "rag_get_context"
    assert set(t.parameters["required"]) == {"source_path", "seq"}
    # 检索结果头必须透出 Chunk 序号（模型据此调 rag_get_context）；旧切片无 seq 则省略
    text, cites = format_results("q", "c", [
        {"rerank_score": 0.9, "document": "正文",
         "metadata": {"title": "T", "source_path": "/s/p.txt", "seq": 2}}])
    assert "Chunk: 2" in text
    text2, _ = format_results("q", "c", [
        {"rerank_score": 0.9, "document": "正文",
         "metadata": {"title": "T", "source_path": "/s/p.txt"}}])
    assert "Chunk:" not in text2
