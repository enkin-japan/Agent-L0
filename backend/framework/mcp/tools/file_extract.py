"""
逃生舱工具 file_extract：按需回源、重解析先前摄入的文件。

摄入层（Ingestion）默认只把文件的「净荷」急解析成带路标的文本注入上下文（音频转写、
PDF 文本、文档摘录），代码/大文件则只登记路径不倾倒。当模型需要急解析省略掉的精确/
完整内容时——某一 PDF 页、某段音频时间区间、文档全文、代码/文本的某行区间——用本工具
凭 path 回源重解析。

path 就是上下文 provenance 标记里那串「原文件：/绝对/路径」。工具无状态，路径即 handle。
按扩展名分派到对应解析器，一律 fail-open 返回字符串。
"""
from pathlib import Path
from typing import Optional

from ..base import BaseTool
from ...ingestion.parsers import asr, pdf, document, text as textparser
# 扩展名口径与摄入层共享单一真相（防两份拷贝漂移，S.7）
from ...ingestion.router import LEGACY_DOC_EXTS, LEGACY_TEXTUTIL_EXTS, OFFICE_EXTS

# PDF 单次输出预算：编排层单条观察截断 observation_max_chars 默认 4000——工具级预算
# 压到 3400，保证「页对齐截断 + 头部续读路标」完整活过编排层截断（否则路标被剪掉，
# 模型不知道还剩哪些页）。若下调 observation_max_chars，此值需同步收紧。
_PDF_MAX_CHARS = 3400


class FileExtractTool(BaseTool):
    name = "file_extract"
    description = (
        "Re-parse a previously ingested file by its path to get precise/full content that the "
        "inline ingestion summary omitted. Find the path in context provenance markers like "
        "'[原文件：/abs/path]' or '[原音频：/abs/path]'. Modes by file type: "
        "PDF -> pass page='N' or 'N-M' to extract those pages' text (output is paginated; "
        "its header names the remaining pages to fetch next); "
        "audio -> pass start/end (seconds) to transcribe a time range; "
        "code/text -> pass start/end (line numbers) to read that line range; "
        "document -> full text. Read-only."
    )
    requires_approval = False
    is_concurrency_safe = True  # 只读文件解析，可与其他只读调用并发

    parameters = {
        "properties": {
            "path": {"type": "string", "description": "Absolute path of the ingested file (from a context provenance marker)."},
            "page": {"type": "string", "description": "PDF only: page spec like '3', '3-5', or '2,4,6' (1-based). Omit for full text."},
            "start": {"type": "number", "description": "Audio: start second. Code/text: start line (1-based)."},
            "end": {"type": "number", "description": "Audio: end second. Code/text: end line (1-based)."},
            "max_chars": {"type": "integer", "description": "Max characters to return (default 12000)."},
        },
        "required": ["path"],
    }

    def execute(self, path: str, page: Optional[str] = None,
                start: Optional[float] = None, end: Optional[float] = None,
                max_chars: int = 12000) -> str:
        p = Path(path)
        if not p.exists():
            return f"System Error: file not found: {path}"
        suf = p.suffix.lower()

        if suf == ".pdf":
            # start/end 对 PDF 无意义——静默忽略会让模型误以为拿到了指定区间
            # （实测事故形态：start=50,end=1000 拿回全文头部，误判工具在循环）。
            if page is None and (start is not None or end is not None):
                return ("System Error: for PDF files use page='N' or 'N-M' (1-based); "
                        "start/end mean seconds (audio) or line numbers (code/text) "
                        "and do not apply to PDFs.")
            return pdf.extract_pdf_pages(str(p), page=page,
                                         max_chars=min(int(max_chars), _PDF_MAX_CHARS))

        mime_audio = suf in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm", ".aiff", ".wma"}
        if mime_audio:
            return asr.transcribe_audio(str(p), start=start, end=end)

        if suf in LEGACY_DOC_EXTS:
            # 无本地转换器：给可行动出路而不是让二进制流进文本读取器变乱码
            if suf in LEGACY_TEXTUTIL_EXTS:
                return (f"System Error: no local converter for {suf}. Convert it first via "
                        f"shell: textutil -convert docx \"{p}\" (writes a .docx next to it), "
                        f"then call file_extract on the converted file.")
            return (f"System Error: no local converter for {suf} (needs PowerPoint/LibreOffice). "
                    f"Ask the user for a .pptx/.xlsx export of this file.")

        if suf in {".xlsm", ".xltx"}:
            # markitdown 只认 .xlsx——宏表/模板走 openpyxl 结构摘要（与摄入层同路）
            from ...excel import summarize_for_ingestion
            try:
                return summarize_for_ingestion(str(p), max_chars=max_chars)
            except Exception as e:
                return f"System Error: spreadsheet summary failed for {p.name}: {e}"

        if suf in OFFICE_EXTS:
            return document.parse_document(str(p), max_chars=max_chars)

        # 代码 / 纯文本：按行区间读取
        s = int(start) if start is not None else None
        e = int(end) if end is not None else None
        return textparser.read_text_range(str(p), start=s, end=e, max_chars=max_chars)
