"""
摄入解析层（Ingestion）：统一入口，把上传文件按模态分路。

目标架构（文本+图像原生，其余转译）：
- 原生出口：图像直喂模型；文本型 PDF 渲染成图的页面也走这条（塞进 images）。
- 转写/解析出口（急解析，发生在推理 worker 之外）：音频→ASR、PDF文本、办公文档→
  文本块，带 provenance 标记内联进上下文，并把解析结果烘进历史（重放免重解析）。
- 懒出口：代码/大文件/未知类型只登记路径，不倾倒正文，靠 file_extract/shell 按需取。

三条铁律：①解析一次烘进持久化历史，重建不重解析；②解析在生成锁之外（纯 I/O + 独立
模型）；③原生模态由能力声明（inference.modalities）决定，音频默认转写、原生直喂仅在
声明含 "audio" 时启用。

每个文件的 provenance 标记都带原始文件的绝对路径——它就是 file_extract 的 handle
（工具无状态，路径直接写在上下文里，无需会话级注册表）。
"""
import asyncio
import mimetypes
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .parsers import asr, pdf, document, text as textparser

# 办公文档扩展名（xlsx/xlsm/xltx 走结构摘要，其余交给 markitdown）。
# 只列本地真解析得动的格式：markitdown 转换器实际覆盖 docx/pptx/xlsx/epub，
# .xls 需 requirements 里 markitdown 的 xls extra（xlrd），.xlsm/.xltx 靠 openpyxl
# 结构摘要。没有本地转换器的格式在 LEGACY_DOC_EXTS——宣告跟着能力走（S.3）：
# 假装支持只会让模型看到「解析失败」而不是可行动的转换指引。
OFFICE_EXTS = {".docx", ".pptx", ".xlsx", ".xlsm", ".xltx", ".xls", ".epub"}

# 无本地转换器的文档格式：不解析、登记路径并给转换出路。
# 前三者 macOS 自带 textutil 可转 docx；后三者需 PowerPoint/LibreOffice 级工具。
LEGACY_TEXTUTIL_EXTS = {".doc", ".rtf", ".odt"}
LEGACY_DOC_EXTS = LEGACY_TEXTUTIL_EXTS | {".ppt", ".odp", ".ods"}


def _spreadsheet_summary(path: str, max_chars: int) -> str:
    """惰性导入的结构摘要（openpyxl 只在真的摄入表格时加载）。"""
    from ..excel import summarize_for_ingestion
    return summarize_for_ingestion(path, max_chars=max_chars)


class Ingestion:
    def __init__(self, inference_cfg: Optional[Dict[str, Any]] = None,
                 ingestion_cfg: Optional[Dict[str, Any]] = None,
                 file_handler=None):
        inference_cfg = inference_cfg or {}
        # 能力声明：模型原生支持的模态。含 "audio" 才走原生音频直喂，否则一律转写。
        self.modalities = set(inference_cfg.get("modalities") or ["text", "image"])
        self.native_audio = "audio" in self.modalities

        cfg = ingestion_cfg or {}
        self.asr_model = cfg.get("asr_model", asr.DEFAULT_ASR_MODEL)
        self.asr_language = cfg.get("asr_language")  # None=自动检测
        self.asr_provider = cfg.get("asr_provider")  # None=默认转写实现（backends 注册面）
        self.pdf_min_chars = int(cfg.get("pdf_text_min_chars_per_page", 50))
        self.pdf_render_max_pages = int(cfg.get("pdf_render_max_pages", 20))
        self.parse_max_chars = int(cfg.get("parse_max_chars", 12000))
        self.text_inline_max = int(cfg.get("text_inline_max_chars", 4000))

        # 复用底层媒体原语：图像落盘沿用简单复制；原生音频 VAD 切片沿用 FileHandler
        self.file_handler = file_handler

    # ---------- 分类 ----------
    def _route(self, filename: str) -> str:
        mime, _ = mimetypes.guess_type(filename)
        suf = Path(filename).suffix.lower()
        if mime and mime.startswith("image/"):
            return "image"
        if mime and mime.startswith("audio/"):
            return "audio"
        if mime == "application/pdf" or suf == ".pdf":
            return "pdf"
        if suf in OFFICE_EXTS:
            return "document"
        if suf in LEGACY_DOC_EXTS:
            return "legacy_doc"
        if textparser.is_code(filename):
            return "code"
        if (mime and mime.startswith("text/")) or suf in {".txt", ".md", ".log", ".csv"}:
            return "text"
        return "attach"

    def _save(self, file, dest_dir: Path, name: str) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(file.filename).suffix
        p = dest_dir / f"{name}{ext}"
        with open(p, "wb") as buf:
            shutil.copyfileobj(file.file, buf)
        return p

    # ---------- 主入口 ----------
    async def ingest(self, files, image_dir: Path, audio_dir: Path, attach_dir: Path) -> Dict[str, Any]:
        """
        返回 {"images": [路径], "audios": [路径], "ingested_text": str, "attachments": [{path,type,name}],
              "voice_transcripts": [{path, transcript, block, marker_line}]}。
        - images：原生图像 + 渲染出的 PDF 页（→ session.images，喂模型）
        - audios：仅原生音频能力开启时非空（→ session.audios，原生直喂）
        - ingested_text：所有解析/转写文本（带 provenance 标记），作为一个文本块注入用户消息
        - attachments：原始非原生文件登记（供分支重挂 / 媒体清理 / 前端）
        - voice_transcripts：仅非 native_audio 的 audio 分支产生（其余为空）——每项带
          纯转写 transcript 与两种拼法（block=完整转写块 / marker_line=仅 provenance 一行），
          供 turn_runner 决定是否把语音升格为本轮用户文本（升格时块降为仅标记行去重）。
        """
        result: Dict[str, Any] = {"images": [], "audios": [], "ingested_text": "",
                                  "attachments": [], "voice_transcripts": []}
        if not files:
            return result

        text_parts: List[str] = []
        ts = datetime.now().strftime("%Y%m%d%H%M%S")

        for file in files:
            if not file or not file.filename:
                continue
            route = self._route(file.filename)
            name = f"{ts}_{uuid.uuid4().hex}"
            fname = file.filename

            try:
                if route == "image":
                    p = self._save(file, image_dir, name)
                    result["images"].append(str(p))

                elif route == "audio":
                    if self.native_audio and self.file_handler is not None:
                        # 原生音频能力：沿用 VAD 切片 + 原生直喂（能力门控 fallback）
                        paths = await self.file_handler.separate_audio(file, name, audio_dir)
                        result["audios"].extend(str(p) for p in paths)
                    else:
                        # 默认路径：整段落盘 → ASR 转写 → 文本注入；原音频留作 attachment
                        p = self._save(file, audio_dir, name)
                        transcript = await asyncio.to_thread(
                            asr.transcribe_audio, str(p), model=self.asr_model,
                            language=self.asr_language, provider=self.asr_provider
                        )
                        # provenance 一行是 file_extract 的重听 handle；升格语音时块降到
                        # 仅这一行（转写正文已成用户文本，不再在摄入块里重复占预算）。
                        marker_line = (f"[Original audio: {p}; to re-listen to a time range use "
                                       f"file_extract(path, start=seconds, end=seconds)]")
                        block = f"[Audio transcript · {fname}]\n{transcript}\n{marker_line}"
                        text_parts.append(block)
                        # ASR fail-open 标记（静音/服务不可达/解码失败）不是"用户的话"，
                        # 不进升格通道——块保持完整留在摄入文本里，模型仍能看到失败原因
                        if not transcript.startswith(("[Empty transcript",
                                                      "[Audio transcription")):
                            result["voice_transcripts"].append({
                                "path": str(p), "transcript": transcript,
                                "block": block, "marker_line": marker_line,
                            })
                        result["attachments"].append({"path": str(p), "type": "audio", "name": fname})

                elif route == "pdf":
                    p = self._save(file, attach_dir, name)
                    parsed = await asyncio.to_thread(
                        pdf.parse_pdf, str(p), image_dir,
                        min_chars_per_page=self.pdf_min_chars,
                        render_max_pages=self.pdf_render_max_pages,
                        max_chars=self.parse_max_chars,
                    )
                    if parsed.get("images"):
                        result["images"].extend(parsed["images"])
                    if parsed.get("mode") == "text":
                        text_parts.append(
                            f"[PDF · {fname} · {parsed.get('pages', '?')} pages]\n{parsed['text']}\n"
                            f"[Original file: {p}; for exact pages use file_extract(path, page=\"N\")]"
                        )
                    else:
                        text_parts.append(f"{parsed['text']}\n[Original file: {p}]")
                    result["attachments"].append({"path": str(p), "type": "pdf", "name": fname})

                elif route == "document":
                    p = self._save(file, attach_dir, name)
                    suf = Path(fname).suffix.lower()
                    if suf in {".xlsx", ".xlsm", ".xltx"}:
                        # 电子表格：结构摘要（R1C1 公式折叠）替代 markitdown 全文 dump——
                        # 整表 dump 是 token 泄漏点+幻觉温床，且 markdown 化丢光公式/错误
                        # 单元格信号。失败（加密/损坏等）回落 markitdown 老路径。
                        try:
                            body = await asyncio.to_thread(
                                _spreadsheet_summary, str(p), self.parse_max_chars)
                            text_parts.append(
                                f"[Spreadsheet · {fname}]\n{body}\n"
                                f"[Original file: {p}; to CREATE/EDIT/FIX/compute on a workbook "
                                f"use excel_research(task, file_path=this path); for a plain "
                                f"text dump use file_extract(path)]"
                            )
                        except Exception:
                            body = await asyncio.to_thread(
                                document.parse_document, str(p), max_chars=self.parse_max_chars)
                            text_parts.append(
                                f"[Document · {fname}]\n{body}\n"
                                f"[Original file: {p}; for full text use file_extract(path)]"
                            )
                    else:
                        body = await asyncio.to_thread(document.parse_document, str(p), max_chars=self.parse_max_chars)
                        text_parts.append(
                            f"[Document · {fname}]\n{body}\n[Original file: {p}; for full text use file_extract(path)]"
                        )
                    result["attachments"].append({"path": str(p), "type": "document", "name": fname})

                elif route == "legacy_doc":
                    # 无本地转换器：登记路径 + 可行动的转换指引（textutil 可转的三种给命令）
                    p = self._save(file, attach_dir, name)
                    suf = Path(fname).suffix.lower()
                    if suf in LEGACY_TEXTUTIL_EXTS:
                        hint = (f"no local converter for {suf}; convert it first via shell: "
                                f"textutil -convert docx \"{p}\" (writes a .docx next to it), "
                                f"then file_extract the converted file")
                    else:
                        hint = (f"no local converter for {suf}; ask the user for a "
                                f".pptx/.xlsx export of this file")
                    text_parts.append(
                        f"[Document · {fname} · not parsed]\nPath: {p}\n[{hint}]")
                    result["attachments"].append({"path": str(p), "type": "attach", "name": fname})

                elif route in ("code", "text"):
                    p = self._save(file, attach_dir, name)
                    info = textparser.peek_text(str(p))
                    lines = info.get("lines", 0)
                    chars = info.get("chars", 0)
                    # 代码一律懒加载；纯文本小于上限才内联，超限转懒
                    if route == "text" and 0 < chars <= self.text_inline_max:
                        try:
                            content = Path(p).read_text(encoding="utf-8", errors="replace")
                        except Exception as e:
                            content = f"[read failed: {e}]"
                        text_parts.append(f"[Text · {fname} · {lines} lines]\n```\n{content}\n```\n[Original file: {p}]")
                    else:
                        kind = "Code file" if route == "code" else "Text file"
                        text_parts.append(
                            f"[{kind} · {fname} · {lines} lines · not inlined]\nPath: {p}\n"
                            f"[For content use file_extract(path, start=line, end=line) or read via shell]"
                        )
                    result["attachments"].append({"path": str(p), "type": route, "name": fname})

                else:  # attach：未知类型，只登记
                    p = self._save(file, attach_dir, name)
                    text_parts.append(f"[Attachment · {fname}] saved at {p} (not parsed; for content use file_extract(path) or shell)")
                    result["attachments"].append({"path": str(p), "type": "attach", "name": fname})

            except Exception as e:
                # 单个文件解析异常不拖垮整批：登记为一条错误标记
                text_parts.append(f"[File \"{fname}\" ingestion failed: {e}]")
            finally:
                try:
                    file.file.close()
                except Exception:
                    pass

        result["ingested_text"] = "\n\n".join(p for p in text_parts if p)
        return result
