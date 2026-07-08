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


class FileExtractTool(BaseTool):
    name = "file_extract"
    description = (
        "Re-parse a previously ingested file by its path to get precise/full content that the "
        "inline ingestion summary omitted. Find the path in context provenance markers like "
        "'[原文件：/abs/path]' or '[原音频：/abs/path]'. Modes by file type: "
        "PDF -> pass page='N' or 'N-M' to extract those pages' text; "
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
            return pdf.extract_pdf_pages(str(p), page=page, max_chars=max_chars)

        mime_audio = suf in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm", ".aiff", ".wma"}
        if mime_audio:
            return asr.transcribe_audio(str(p), start=start, end=end)

        if suf in {".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls", ".odt", ".odp", ".ods", ".rtf", ".epub"}:
            return document.parse_document(str(p), max_chars=max_chars)

        # 代码 / 纯文本：按行区间读取
        s = int(start) if start is not None else None
        e = int(end) if end is not None else None
        return textparser.read_text_range(str(p), start=s, end=e, max_chars=max_chars)
