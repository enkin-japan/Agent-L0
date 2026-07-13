"""
PDF 解析器（pymupdf / fitz）。PDF 横跨原生与非原生两界，故按内容分路：
- 文本型（每页平均字符数 ≥ 阈值）：抽取文本，带 [第N页] 标记，走文本注入；
- 扫描/图像型（文本密度过低）：渲染页面为 PNG，走原生视觉路（与普通图片同机制）。
所有异常 fail-open：返回错误标记文本，不打断整轮。
"""
import re
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


def _parse_page_spec(spec: str, total: int) -> "tuple[Optional[List[int]], Optional[str]]":
    """解析页码规格 '3' / '3-5' / '3,5,7'（1-based）→ (0-based 下标列表, None)。
    含无法解析的片段、或没有任何页落在文档内 → (None, 错误说明)。
    绝不静默回退全文：坏参数拿到第 1 页的内容比一条报错致命得多——实测事故
    （会话 e2348829）：模型把页码写坏后两次拿回第 1 页，据此断定"提取在循环"
    并放弃了任务；显式报错它下一次就能自纠（同轮实证会写 '50'/'50-70'）。"""

    def _suggest() -> str:
        """从坏参数里抽出数字模式给修正建议。实测（复测会话 d618f0d6）：模型把备注粘进
        page（'50-80_pages_to_extract_...'）后，只报格式它会退化成单页爬行烧光步骤预算；
        直接给 \"did you mean page='50-80'\" 它下一次几乎必然照抄。仅数字落在文档内才建议。"""
        nums = [int(n) for n in re.findall(r"\d+", spec)[:4]]
        nums = [n for n in nums if 1 <= n <= total]
        if not nums:
            return ""
        if len(nums) == 2 and nums[0] != nums[1]:
            lo, hi = sorted(nums)
            sug = f"{lo}-{hi}"
        elif len(nums) <= 2:
            sug = str(nums[0])
        else:
            sug = ",".join(str(n) for n in nums)
        return f" Did you mean page='{sug}'?"

    def _bad(reason: str) -> "tuple[None, str]":
        shown = spec if len(spec) <= 60 else spec[:60] + "…"
        # 报错保持单行（换行转义）：首行会被 push_activity 当步骤摘要展示
        return None, (f"invalid page spec {shown!r}: {reason}.{_suggest()} "
                      f"Use '3', '3-5', or '2,4,6' "
                      f"(this PDF has {total} pages).").replace("\n", "\\n")

    pages: List[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                return _bad(f"cannot parse range '{part[:30]}'")
            if lo > hi:
                return _bad(f"range '{part[:30]}' is reversed")
            pages.extend(p - 1 for p in range(lo, hi + 1) if 1 <= p <= total)
        else:
            try:
                p = int(part)
            except ValueError:
                return _bad(f"cannot parse page number '{part[:30]}'")
            if 1 <= p <= total:
                pages.append(p - 1)
    if not pages:
        return _bad("no requested page exists in this document")
    return pages, None


def _ranges_desc(idxs: List[int]) -> str:
    """0-based 下标列表 → 1-based 紧凑区间描述（'50-57' / '2,4,6-9'），供续读路标复用。"""
    if not idxs:
        return ""
    nums = [i + 1 for i in idxs]
    out: List[str] = []
    s = e = nums[0]
    for n in nums[1:]:
        if n == e + 1:
            e = n
            continue
        out.append(f"{s}-{e}" if e > s else f"{s}")
        s = e = n
    out.append(f"{s}-{e}" if e > s else f"{s}")
    return ",".join(out)


def extract_pdf_pages(path, page: Optional[str] = None, *, max_chars: int = 12000) -> str:
    """逃生舱：按需抽取 PDF 指定页文本（page 缺省=全文）。fail-open 返回字符串。

    输出协议（弱模型的确定性翻页支点）：
    - 首行头部注明「总页数 | 本次包含哪些页 | 还剩哪些页 + 续读用的 page 参数」。
      路标放头部而非尾部——下游还有编排层观察截断，尾部路标会被剪掉，头部永远活着。
    - 截断按页对齐：装不下的页整页留给下一次调用，绝不吐半页（首页独页超预算除外）。
    - 页码规格非法 → 显式 System Error（见 _parse_page_spec，绝不静默回退全文）。
    """
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
        if page:
            idxs, err = _parse_page_spec(str(page), total)
            if err:
                return f"System Error: {err}"
        else:
            idxs = list(range(total))
        blocks: List[str] = []
        acc = 0
        for i in idxs:
            try:
                t = doc[i].get_text().strip()
            except Exception:
                t = ""
            block = f"[Page {i + 1}]\n{t}" if t else f"[Page {i + 1}] (no extractable text; possibly an image page)"
            if blocks and acc + len(block) > max_chars:
                break  # 页对齐截断：这一页放不下就整页留给下一次调用
            if not blocks and len(block) > max_chars:
                block = block[:max_chars] + "\n…[this single page exceeds the output budget]"
            blocks.append(block)
            acc += len(block)
        shown, remaining = idxs[:len(blocks)], idxs[len(blocks):]
        header = f"[PDF: {total} pages | showing: {_ranges_desc(shown)}"
        if remaining:
            rdesc = _ranges_desc(remaining)
            header += f' | remaining: {rdesc} — continue with page="{rdesc}"'
        header += "]"
        return header + "\n\n" + "\n\n".join(blocks)
    finally:
        doc.close()
