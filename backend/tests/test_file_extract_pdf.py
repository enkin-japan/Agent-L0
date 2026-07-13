"""file_extract 的 PDF 翻页协议金测试。

守护的失败形态（实测事故，会话 e2348829）：模型把 page 参数写坏（'50-103\\n垃圾'）时，
旧实现静默回退成「全文从第 1 页」——模型两次拿回第 1 页后断定"提取在循环"并放弃任务。
本组测试锁定：①坏参数必须显式报错、绝不回退；②截断按页对齐且头部路标注明剩余页；
③start/end 对 PDF 显式重定向；④工具级预算保证路标活过编排层观察截断。
"""
import pytest

fitz = pytest.importorskip("fitz")

from framework.ingestion.parsers.pdf import extract_pdf_pages
from framework.mcp.tools.file_extract import FileExtractTool, _PDF_MAX_CHARS


def make_pdf(path, n_pages: int, chars_per_page: int = 60):
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page()
        # 单行超出页宽会被裁掉——按 50 字符一行铺满,保证抽取文本量可控
        lines = [f"P{i + 1}"]
        remaining = max(0, chars_per_page - len(lines[0]))
        while remaining > 0:
            lines.append("x" * min(50, remaining))
            remaining -= 50
        page.insert_text((72, 72), lines)
    doc.save(str(path))
    doc.close()


@pytest.fixture
def pdf5(tmp_path):
    p = tmp_path / "five.pdf"
    make_pdf(p, 5)
    return p


@pytest.fixture
def pdf30(tmp_path):
    p = tmp_path / "thirty.pdf"
    make_pdf(p, 30, chars_per_page=400)
    return p


# ---------- ①坏参数显式报错，绝不静默回退全文 ----------

def test_garbage_page_spec_errors_not_page1(pdf5):
    out = extract_pdf_pages(str(pdf5), page="2-5\nall exercises are captured, proceeding")
    assert out.startswith("System Error:")
    assert "invalid page spec" in out
    assert "5 pages" in out          # 报错自带文档规模，模型可据此重发
    assert "[Page 1]" not in out     # 事故形态：坏参数拿回第 1 页

def test_nonnumeric_page_errors(pdf5):
    out = extract_pdf_pages(str(pdf5), page="abc")
    assert out.startswith("System Error:") and "invalid page spec" in out

def test_out_of_range_page_errors(pdf5):
    out = extract_pdf_pages(str(pdf5), page="200")
    assert out.startswith("System Error:") and "5 pages" in out

def test_reversed_range_errors(pdf5):
    out = extract_pdf_pages(str(pdf5), page="5-3")
    assert out.startswith("System Error:") and "reversed" in out

def test_range_cropped_to_document_still_works(pdf5):
    # 区间部分越界沿用旧语义：裁剪到文档内（不算坏参数）
    out = extract_pdf_pages(str(pdf5), page="3-10")
    assert out.startswith("[PDF: 5 pages | showing: 3-5]")
    assert "[Page 3]" in out and "[Page 5]" in out and "[Page 2]" not in out


# ---------- ②头部路标 + 页对齐截断 ----------

def test_header_names_shown_pages(pdf5):
    out = extract_pdf_pages(str(pdf5), page="2-3")
    assert out.startswith("[PDF: 5 pages | showing: 2-3]")
    assert "[Page 2]" in out and "[Page 3]" in out
    assert "remaining" not in out    # 全装下了就没有续读路标

def test_page_aligned_truncation_with_continue_hint(pdf30):
    out = extract_pdf_pages(str(pdf30), page="1-30", max_chars=1000)
    header = out.splitlines()[0]
    assert "[PDF: 30 pages | showing: 1-2" in header
    assert 'continue with page="3-30"' in header   # 路标在头部，活过任何下游截断
    assert "[Page 2]" in out and "[Page 3]" not in out
    assert out.rstrip().endswith("x")               # 页对齐：最后一页是完整页，非半页

def test_full_doc_default_shows_all_when_small(pdf5):
    out = extract_pdf_pages(str(pdf5))
    assert out.startswith("[PDF: 5 pages | showing: 1-5]")

def test_single_page_over_budget_clipped_with_note(tmp_path):
    p = tmp_path / "fat.pdf"
    make_pdf(p, 1, chars_per_page=2000)
    out = extract_pdf_pages(str(p), page="1", max_chars=500)
    assert "exceeds the output budget" in out
    assert len(out) < 800


# ---------- ③④工具层：start/end 重定向 + 预算封顶 ----------

def test_pdf_start_end_redirects_to_page(pdf5):
    out = FileExtractTool().execute(path=str(pdf5), start=50, end=1000)
    assert out.startswith("System Error:") and "page=" in out

def test_pdf_page_with_start_ignored_gracefully(pdf5):
    # 同时给 page 与 start：page 有效即照常工作（start 冗余不致命）
    out = FileExtractTool().execute(path=str(pdf5), page="2", start=3)
    assert "[Page 2]" in out

def test_pdf_budget_capped_below_observation_limit(pdf30):
    out = FileExtractTool().execute(path=str(pdf30))  # 30×~400 字符，远超预算
    assert out.startswith("[PDF:")
    assert 'continue with page=' in out.splitlines()[0]
    # 编排层 observation_max_chars 默认 4000：整条输出须留出路标存活余量
    assert len(out) <= _PDF_MAX_CHARS + 200


# ---------- 坏参数的修正建议(did you mean) ----------

def test_garbage_spec_suggests_extracted_range(pdf5):
    """备注粘进 page 参数 → 报错直接给可照抄的修正建议(实测:只教格式会退化成单页爬行)。"""
    out = extract_pdf_pages(str(pdf5), page="2-5_pages_to_extract_exercises_contextual_range")
    assert out.startswith("System Error:")
    assert "Did you mean page='2-5'?" in out

def test_garbage_spec_single_number_suggestion(pdf5):
    out = extract_pdf_pages(str(pdf5), page="3-end_of_document_range")
    assert "Did you mean page='3'?" in out

def test_out_of_range_garbage_gets_no_suggestion(pdf5):
    """抽出的数字全部越界 → 不给建议(建议本身必须可执行)。"""
    out = extract_pdf_pages(str(pdf5), page="200_pages_range")
    assert out.startswith("System Error:")
    assert "Did you mean" not in out
