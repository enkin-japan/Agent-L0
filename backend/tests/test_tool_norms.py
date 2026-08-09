"""工具行为准则在位测试（#83，2026-08-10）。

准则是写给 26B 档看的行为契约（L4 站实证：描述里没写的准则等于不存在——
skill_invoke 2/5→5/5、cloud_consult provider 幻觉、luna 空正文三案同根）。
本测试钉的不是措辞，是**准则的存在性**：关键约束语义消失即红。
"""
from framework.agents.research_sandbox import (READER_SYSTEM_PROMPT,
                                               SANDBOX_SYSTEM_PROMPT)
from framework.mcp.tools.doc_write import WriteDocumentTool
from framework.mcp.tools.excel_research import ExcelResearchTool
from framework.mcp.tools.rag_search import RAGSearchTool
from framework.mcp.tools.render_chart import RenderChartTool
from framework.mcp.tools.web_research import WebResearchTool


def test_search_sandbox_language_norm_present():
    """信源社区语言检索 + 跨语言交叉 + 主源优先 + 不给简单题加戏。"""
    p = SANDBOX_SYSTEM_PROMPT
    assert "SOURCE COMMUNITY" in p
    assert "English queries" in p and "Japanese queries" in p
    assert "BOTH English and the topic's native language" in p
    assert "original/primary source" in p
    assert "redundant multilingual searches" in p   # 防过度矫正的反向准则


def test_reader_keeps_original_names():
    assert "ORIGINAL" in READER_SYSTEM_PROMPT      # 专名/数字保真不翻译丢失


def test_web_research_language_and_timeframe():
    d = WebResearchTool.description
    assert "do NOT pre-translate" in d
    assert "timeframe" in d
    assert "conversation's language" in d          # 输出语言跟用户


def test_rag_search_scoped_to_local_kb():
    """旧文案把「外部验证」划给 RAG——已纠错，本地库与公网职权分明。"""
    d = RAGSearchTool.description
    assert "external sources for verification" not in d
    assert "LOCAL" in d and "web_research" in d
    assert "retry ONCE" in d                       # 未命中先换词重试再下结论


def test_deliverable_language_follows_user():
    assert "language the USER is conversing in" in WriteDocumentTool.description
    assert "language the USER is conversing in" in RenderChartTool.description
    assert "conversation language" in ExcelResearchTool.description
