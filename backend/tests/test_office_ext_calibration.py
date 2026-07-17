"""办公文档扩展名表的名实校准金测试（markitdown 评估收尾，2026-07-18）。

守护的失败形态：扩展名表声明支持但 markitdown 无本地转换器（.doc/.ppt/.odt/.odp/
.ods/.rtf），文件走到 parse_document 后 fail-open 成「解析失败」——模型看到失败
而非可行动出路；.xlsm/.xltx 更糟：不在 file_extract 旧表里，二进制流进文本读取器
变乱码。本组锁定：①两张表互斥且口径共享（S.7 防拷贝漂移）；②legacy 格式在摄入
与 file_extract 两侧都给转换指引（textutil 可转的三种给命令）；③宏表走 openpyxl
结构摘要不走 markitdown。
"""
import pytest

from framework.ingestion.router import (
    Ingestion, LEGACY_DOC_EXTS, LEGACY_TEXTUTIL_EXTS, OFFICE_EXTS,
)
from framework.mcp.tools.file_extract import FileExtractTool


# ---------- 表本身 ----------

def test_office_and_legacy_disjoint():
    assert not (OFFICE_EXTS & LEGACY_DOC_EXTS)


def test_textutil_subset_of_legacy():
    assert LEGACY_TEXTUTIL_EXTS < LEGACY_DOC_EXTS


def test_office_exts_only_locally_convertible():
    # markitdown 本地转换器真实覆盖 + openpyxl 结构摘要覆盖的格式，一个不多一个不少
    assert OFFICE_EXTS == {".docx", ".pptx", ".xlsx", ".xlsm", ".xltx", ".xls", ".epub"}


# ---------- 摄入路由 ----------

@pytest.mark.parametrize("fname,expected", [
    ("a.docx", "document"), ("a.pptx", "document"), ("a.xlsx", "document"),
    ("a.xls", "document"), ("a.epub", "document"), ("a.XLSM", "document"),
    ("a.doc", "legacy_doc"), ("a.rtf", "legacy_doc"), ("a.odt", "legacy_doc"),
    ("a.ppt", "legacy_doc"), ("a.odp", "legacy_doc"), ("a.ods", "legacy_doc"),
])
def test_route_calibration(fname, expected):
    ing = Ingestion()
    assert ing._route(fname) == expected


# ---------- file_extract 分派 ----------

def _touch(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"\xd0\xcf\x11\xe0 fake binary")
    return p


def test_file_extract_legacy_textutil_hint(tmp_path):
    tool = FileExtractTool()
    for ext in (".doc", ".rtf", ".odt"):
        out = tool.execute(str(_touch(tmp_path, f"f{ext}")))
        assert out.startswith("System Error"), ext
        assert "textutil -convert docx" in out, ext


def test_file_extract_legacy_no_tool_hint(tmp_path):
    tool = FileExtractTool()
    for ext in (".ppt", ".odp", ".ods"):
        out = tool.execute(str(_touch(tmp_path, f"f{ext}")))
        assert out.startswith("System Error"), ext
        assert "textutil" not in out, ext          # 别指一条走不通的路
        assert ".pptx/.xlsx" in out, ext


def test_file_extract_macro_workbook_uses_structural_summary(tmp_path, monkeypatch):
    import framework.excel as excel_mod
    called = {}

    def fake_summary(path, max_chars=12000):
        called["path"] = path
        return "SUMMARY"

    monkeypatch.setattr(excel_mod, "summarize_for_ingestion", fake_summary)
    tool = FileExtractTool()
    out = tool.execute(str(_touch(tmp_path, "wb.xlsm")))
    assert out == "SUMMARY"
    assert called["path"].endswith("wb.xlsm")


def test_file_extract_xls_goes_to_markitdown(tmp_path, monkeypatch):
    from framework.ingestion.parsers import document as doc_mod
    monkeypatch.setattr(doc_mod, "parse_document",
                        lambda path, max_chars=12000: "MARKITDOWN")
    tool = FileExtractTool()
    assert tool.execute(str(_touch(tmp_path, "old.xls"))) == "MARKITDOWN"


def test_xls_extra_installed():
    # requirements 的 xls extra（xlrd）真的在环境里——没有它 .xls 又变假支持
    import xlrd  # noqa: F401
