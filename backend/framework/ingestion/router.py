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

# 办公文档扩展名（交给 markitdown）
OFFICE_EXTS = {".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls", ".odt", ".odp", ".ods", ".rtf", ".epub"}


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
        返回 {"images": [路径], "audios": [路径], "ingested_text": str, "attachments": [{path,type,name}]}。
        - images：原生图像 + 渲染出的 PDF 页（→ session.images，喂模型）
        - audios：仅原生音频能力开启时非空（→ session.audios，原生直喂）
        - ingested_text：所有解析/转写文本（带 provenance 标记），作为一个文本块注入用户消息
        - attachments：原始非原生文件登记（供分支重挂 / 媒体清理 / 前端）
        """
        result: Dict[str, Any] = {"images": [], "audios": [], "ingested_text": "", "attachments": []}
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
                            asr.transcribe_audio, str(p), model=self.asr_model, language=self.asr_language
                        )
                        text_parts.append(
                            f"[Audio transcript · {fname}]\n{transcript}\n"
                            f"[Original audio: {p}; to re-listen to a time range use "
                            f"file_extract(path, start=seconds, end=seconds)]"
                        )
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
                    body = await asyncio.to_thread(document.parse_document, str(p), max_chars=self.parse_max_chars)
                    text_parts.append(
                        f"[Document · {fname}]\n{body}\n[Original file: {p}; for full text use file_extract(path)]"
                    )
                    result["attachments"].append({"path": str(p), "type": "document", "name": fname})

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
