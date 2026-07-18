"""RAG 时效可见化金测试（记忆治理波 U2）。

守护的失败形态：①片段头丢了入库日期——模型失去自判时效的依据，「答错」不再
降级为「带保留地答」；②存量旧切片（无 created_at）被猜出一个假日期；③格式化
函数被搬回类方法——实例化触发网络探活，测试与离线环境双双被拖下水。
"""
from framework.mcp.tools.rag_search import format_results


def _doc(text="正文", score=0.9, **meta):
    return {"document": text, "rerank_score": score,
            "metadata": {"source_path": "/s/p.txt", "title": "t", **meta}}


def test_created_at_surfaces_date_only():
    text, cites = format_results("q", "c", [_doc(created_at="2026-07-18T09:30:00")])
    assert "Ingested: 2026-07-18" in text
    assert "09:30" not in text                      # 日期粒度，不带时刻
    assert cites[0]["created_at"] == "2026-07-18"


def test_legacy_chunk_without_created_at_omits_cleanly():
    text, cites = format_results("q", "c", [_doc()])
    assert "Ingested" not in text                   # 缺字段=整段省略，不猜
    assert "created_at" not in cites[0]
    assert "Path: /s/p.txt" in text                 # 既有契约不动（管理工具靠它引用）


def test_mixed_batch_keeps_order_and_score_format():
    text, cites = format_results("q", "c", [
        _doc("新", score=0.91, created_at="2026-07-01T00:00:00"),
        _doc("旧", score=0.35),
    ])
    assert text.index("Result 1") < text.index("Result 2")
    assert "Match Score: 0.91" in text and "Match Score: 0.35" in text
    assert len(cites) == 2 and cites[1].get("created_at") is None


def test_stale_mark_surfaces_in_header_and_citation():
    text, cites = format_results("q", "c", [_doc(stale="2026-07 用户指出价目表已换版")])
    assert "STALE: 2026-07 用户指出价目表已换版" in text
    assert cites[0]["stale"] == "2026-07 用户指出价目表已换版"
    clean_text, clean_cites = format_results("q", "c", [_doc()])
    assert "STALE" not in clean_text and "stale" not in clean_cites[0]
