"""
PDF 解析器（pymupdf / fitz）。PDF 横跨原生与非原生两界，故按内容分路：
- 文本型（每页平均字符数 ≥ 阈值）：抽取文本，带 [第N页] 标记，走文本注入；
- 扫描/图像型（文本密度过低）：渲染页面为 PNG，走原生视觉路（与普通图片同机制）。
所有异常 fail-open：返回错误标记文本，不打断整轮。
"""
from pathlib import Path
from typing import Any, Dict, List, Optional


def parse_pdf(path, image_dir, *, min_chars_per_page: int = 50,
              render_max_pages: int = 20, max_chars: int = 12000) -> Dict[str, Any]:
    """
    返回 {"text": str, "images": [png_path,...], "pages": int, "mode": "text"|"image"|"error"}。
    text：注入上下文的文本（文本型=正文含页码；图像型=一条说明标记）；
    images：图像型渲染出的页面 PNG 路径（走原生视觉），文本型为空。
    """
    try:
        import fitz  # PyMuPDF
    except Exception as e:
        return {"text": f"[PDF parsing unavailable: pymupdf not installed ({e})]", "images": [], "pages": 0, "mode": "error"}

    try:
        doc = fitz.open(str(path))
    except Exception as e:
        return {"text": f"[PDF open failed: {e}]", "images": [], "pages": 0, "mode": "error"}

    name = Path(path).name
    try:
        n = doc.page_count
        if n == 0:
            return {"text": f"[PDF \"{name}\" is empty]", "images": [], "pages": 0, "mode": "text"}

        texts: List[str] = []
        total = 0
        for i in range(n):
            try:
                t = doc[i].get_text().strip()
            except Exception:
                t = ""
            texts.append(t)
            total += len(t)
        density = total / n

        # 文本型：抽文本，带页码，超上限截断并提示用 file_extract 取余页
        if density >= min_chars_per_page:
            parts: List[str] = []
            acc = 0
            truncated = False
            for i, t in enumerate(texts):
                if not t:
                    continue
                block = f"[Page {i + 1}]\n{t}"
                parts.append(block)
                acc += len(block)
                if acc >= max_chars:
                    truncated = True
                    break
            body = "\n\n".join(parts)
            if truncated:
                body += f"\n\n[Truncated; this PDF has {n} pages — for the rest use file_extract(path, page=\"N\")]"
            return {"text": body, "images": [], "pages": n, "mode": "text"}

        # 图像/扫描型：渲染页面为 PNG，走原生视觉
        images: List[str] = []
        stem = Path(path).stem
        dest = Path(image_dir)
        dest.mkdir(parents=True, exist_ok=True)
        limit = min(n, render_max_pages)
        for i in range(limit):
            try:
                pix = doc[i].get_pixmap(dpi=150)
                out = dest / f"{stem}_p{i + 1}.png"
                pix.save(str(out))
                images.append(str(out))
            except Exception:
                continue
        note = f"[PDF \"{name}\" is scanned/image-based, {n} pages total; first {len(images)} pages rendered as images for reading]"
        if n > len(images):
            note += f" (for other pages extract text with file_extract(path, page=\"N\"))"
        return {"text": note, "images": images, "pages": n, "mode": "image"}
    finally:
        doc.close()


def _parse_page_spec(spec: str, total: int) -> List[int]:
    """解析页码规格 '3' / '3-5' / '3,5,7'（1-based）为 0-based 下标列表，越界自动裁剪。"""
    pages: List[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            for p in range(lo, hi + 1):
                if 1 <= p <= total:
                    pages.append(p - 1)
        else:
            try:
                p = int(part)
            except ValueError:
                continue
            if 1 <= p <= total:
                pages.append(p - 1)
    return pages or list(range(total))


def extract_pdf_pages(path, page: Optional[str] = None, *, max_chars: int = 12000) -> str:
    """逃生舱：按需抽取 PDF 指定页文本（page 缺省=全文）。fail-open 返回字符串。"""
    try:
        import fitz
    except Exception as e:
        return f"[PDF parsing unavailable: pymupdf not installed ({e})]"
    try:
        doc = fitz.open(str(path))
    except Exception as e:
        return f"[PDF open failed: {e}]"
    try:
        total = doc.page_count
        idxs = _parse_page_spec(page, total) if page else list(range(total))
        parts: List[str] = []
        acc = 0
        for i in idxs:
            try:
                t = doc[i].get_text().strip()
            except Exception:
                t = ""
            block = f"[Page {i + 1}]\n{t}" if t else f"[Page {i + 1}] (no extractable text; possibly an image page)"
            parts.append(block)
            acc += len(block)
            if acc >= max_chars:
                parts.append("[Truncated; narrow the page range]")
                break
        return "\n\n".join(parts)
    finally:
        doc.close()
