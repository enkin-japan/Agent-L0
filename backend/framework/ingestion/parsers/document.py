"""
办公文档解析器（markitdown → markdown）。一把梭覆盖 docx/pptx/xlsx 等，
保留标题/表格等结构。超上限截断并提示用 file_extract 取全文。fail-open。
"""
from pathlib import Path


def parse_document(path, *, max_chars: int = 12000) -> str:
    try:
        from markitdown import MarkItDown
    except Exception as e:
        return f"[Document parsing unavailable: markitdown not installed ({e})]"
    name = Path(path).name
    try:
        md = MarkItDown()
        text = (md.convert(str(path)).text_content or "").strip()
        if not text:
            return f"[Document \"{name}\" parsed to empty text]"
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[Truncated; for full text use file_extract(path)]"
        return text
    except Exception as e:
        return f"[Document \"{name}\" parsing failed: {e}]"
