"""F3 后半:rag_search 空结果细分的金测试。

「扫到了库但全被相关性阈值筛掉」与「目标库空/不可用」必须是两句不同的话——混成
一句时模型只会反复换 query 空转。(初版设想的「放宽 rerank_top_k」已被复审证伪并
撤销:results 空 ⟺ confident_chunks 空,rerank_top_k 只作用于已空的列表。)
"""
import pytest

from framework.mcp.tools import rag_search as rs


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture()
def stub_post(monkeypatch):
    """替身 requests.post:按测试设定的 payload 应答;记录请求以供阳性对照。"""
    calls = []

    def install(payload):
        def _post(url=None, json=None, headers=None, timeout=None, **kw):
            calls.append(json)
            return _Resp(payload)
        monkeypatch.setattr(rs.requests, "post", _post)
        return calls

    return install


def _run():
    tool = rs.RAGSearchTool.__new__(rs.RAGSearchTool)   # 绕过探活构造
    return tool.execute("报销流程", "docs", "报销 流程", rerank_top_k=3)


def test_filtered_out_states_facts_without_false_confidence(stub_post):
    """扫到了库但全被相关性阈值筛掉:只陈述事实（N 个最近邻全低于阈值）,引向换措辞
    重试一次或如实报告未找到。**绝不许宣称「库里有料」**——total_chunks_retrieved 是
    无阈值的最近邻池,非空库上几乎恒 >0,据此打包票是在制造对缺失内容的假信心
    （复审 Critical）。"""
    calls = stub_post({"results": [], "total_chunks_retrieved": 7})
    out = _run()
    assert "7" in out and "below the relevance cutoff" in out
    assert "retry ONCE" in out and "not found" in out         # 给出收敛的下一步
    assert "does contain" not in out and "Do NOT conclude" not in out   # 假信心话术禁入
    assert calls, "替身没接上请求"                             # 阳性对照:真的走到了 HTTP 层


def test_truly_empty_says_store_unavailable(stub_post):
    """池子就是零（目标库空/不存在）:话术引向换 category / 先摄入,不提重排。"""
    stub_post({"results": [], "total_chunks_retrieved": 0})
    out = _run()
    assert "empty or unavailable" in out
    assert "reranking" not in out


def test_missing_field_degrades_to_truly_empty(stub_post):
    """旧版 RAG 服务无 total_chunks_retrieved 字段:按空库口径处理,不炸不猜。"""
    stub_post({"results": []})
    out = _run()
    assert "empty or unavailable" in out


def test_dirty_field_degrades_instead_of_erroring(stub_post):
    """脏值（非数值）:降级为空库口径——不许把良性空结果炸成 System Error 触发
    谎报围堵（复审 S6）。"""
    stub_post({"results": [], "total_chunks_retrieved": "n/a"})
    out = _run()
    assert "empty or unavailable" in out
    assert "System Error" not in out


def test_nonempty_results_untouched(stub_post):
    """阳性对照:有结果时走原格式化路径,细分话术一个字不出现。夹具字段按
    format_results 实读的键(title/source_path/created_at/seq——复审 L11 订正,
    初版注释声称的「真实字段形」用的是它不读的键)。"""
    stub_post({"results": [{"document": "报销需 30 日内提交。",
                            "metadata": {"title": "员工手册", "seq": 3,
                                         "source_path": "/kb/员工手册.md",
                                         "created_at": "2026-08-01"},
                            "rerank_score": 0.9}],
               "total_chunks_retrieved": 5})
    out = _run()
    text = out.text if hasattr(out, "text") else str(out)
    assert "No results" not in text
    assert "报销需 30 日内提交" in text                        # 真实字段真的被渲染进上下文
