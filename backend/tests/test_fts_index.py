"""FTS 关键词腿金测试（数据架构采纳波 R2）：预分词规则 + 索引 CRUD + fail-open。

守的是什么：入索引与查询必须走**同一套** bigram 变换（两侧不一致 = 关键词腿静默
失明，且向量腿还在，症状完全隐蔽）。rag 独立 venv，零重依赖模块按路径装载
（retrieval_extras 同惯例）；FTS5 由 python 内置 sqlite 提供。
"""
import importlib.util
import sys
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[2] / "rag"

spec = importlib.util.spec_from_file_location("fts_index", RAG_ROOT / "fts_index.py")
fts_index = importlib.util.module_from_spec(spec)
sys.modules["fts_index"] = fts_index
spec.loader.exec_module(fts_index)


def test_segment_cjk_bigram_and_ascii_words():
    seg = fts_index.segment_for_fts
    assert seg("王秀兰是房东") == "王秀 秀兰 兰是 是房 房东"
    assert seg("ERR_5023 报错了") == "err_5023 报错 错了"       # 标识符原子保留 + 小写
    assert seg("cron 0 3 * * * 跑备份") == "cron 0 3 跑备 备份"  # 星号是语法噪声，丢弃
    assert seg("周") == "周"                                     # 单字连串保单字
    assert seg("") == "" and seg(None) == ""


def test_match_query_terms_quoted_deduped():
    q = fts_index.build_match_query("周师傅 电话", "周师傅 保养店")
    assert '"周师" OR "师傅"' in q and q.count('"周师"') == 1     # 跨文本去重
    assert fts_index.build_match_query("") == ""
    assert '"err_5023"' in fts_index.build_match_query("ERR_5023")  # 语法字符靠引号转义


def test_index_crud_and_search(tmp_path):
    idx = fts_index.FtsIndex(tmp_path / "fts.sqlite3")
    assert idx.available
    idx.add_chunks("household", ["c1", "c2"],
                   ["房东王秀兰，月租 4200 元", "退租需提前 60 天书面通知"], "/s/rent.txt")
    idx.add_chunks("device_tech", ["c3"], ["路由器报错误码 ERR_5023"], "/s/router.txt")

    hits = idx.search("王秀兰是谁", limit=5)
    assert [h["chunk_id"] for h in hits][:1] == ["c1"]
    assert hits[0]["db"] == "household" and hits[0]["source_path"] == "/s/rent.txt"
    assert [h["chunk_id"] for h in idx.search("ERR_5023 是什么", limit=5)][:1] == ["c3"]

    idx.delete_ids(["c1"])
    assert all(h["chunk_id"] != "c1" for h in idx.search("王秀兰", limit=5))
    idx.delete_where_source("/s/router.txt")
    assert idx.search("ERR_5023", limit=5) == []
    idx.drop_db("household")
    assert idx.search("退租 提前", limit=5) == []


def test_search_fail_open_when_unavailable(tmp_path):
    """FTS 不可用时全部操作静默让路——关键词腿是增强，绝不许拖垮主检索。"""
    idx = fts_index.FtsIndex(tmp_path / "fts.sqlite3")
    idx.available = False
    idx.add_chunks("d", ["x"], ["文本"], "/s/a.txt")   # 不抛
    assert idx.search("文本") == []
    idx.delete_ids(["x"]); idx.delete_where_source("/s/a.txt"); idx.drop_db("d")


def test_rag_server_sync_points_pinned():
    """接线点钉死：入库/删除/真删/重分拣/重索引清理都必须同步 FTS——漏一处即索引
    漂移（幽灵命中或漏命中，症状隐蔽）。源码级钉板（rag_server 不可直接 import）。"""
    src = (RAG_ROOT / "rag_server.py").read_text(encoding="utf-8")
    for anchor in ("FTS.add_chunks(target_category, ids, chunks",
                   "FTS.delete_ids(ids)",
                   "FTS.delete_where_source(sp)",
                   "FTS.delete_where_source(str(file))",
                   "FTS.delete_where_source(source_path)",
                   "FTS.drop_db(db_name)",
                   "FTS.search(req.query, req.optimized_query"):
        assert anchor in src, f"rag_server 缺 FTS 同步点: {anchor}"
