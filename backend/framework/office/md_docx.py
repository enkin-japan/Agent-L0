"""markdown 子集 → .docx 确定性转换（officecli batch 指令生成）。

设计对齐 deck 槽位哲学：模型面只见 markdown（它本来就写得好），坐标/样式/OOXML
细节全部由本层确定性展开——小模型不摆版式。转换分两层：

  md_to_commands(md)  纯函数：markdown → batch 指令清单（可金测试，无需二进制）
  write_docx(md, out) 落地：create → batch（原子）→ validate → markitdown 读回
                      交叉校验（写用 officecli、读用摄入侧独立实现，防同引擎自证）

子集范围（超出子集的行按普通段落处理，不炸不丢）：
  # ~ ###### 标题（映射 Heading1..4，5/6 并入 4）· 段落（软换行合并）
  **粗体** *斜体* `行内码` [链接](url) · - / * / + 无序（2 空格缩进=下一级）
  1. 有序 · GFM 管道表格（首行表头加底色）· ``` 围栏代码块 · > 引用
  --- 分隔线（忽略）· 独行 ![alt](本地路径) 图片（仅本地已存在文件，不取网络）
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .officecli import OfficeCliError, batch, run_cli, validate

# ---- 样式定义（每文档一次，置于 batch 首部） ----

_STYLE_DEFS: List[Dict[str, Any]] = [
    {"id": "Heading1", "size": "18pt", "bold": True, "color": "1F3864",
     "spaceBefore": "18pt", "spaceAfter": "8pt"},
    {"id": "Heading2", "size": "14pt", "bold": True, "color": "2E5395",
     "spaceBefore": "14pt", "spaceAfter": "6pt"},
    {"id": "Heading3", "size": "12pt", "bold": True, "color": "404040",
     "spaceBefore": "10pt", "spaceAfter": "4pt"},
    {"id": "Heading4", "size": "11pt", "bold": True, "italic": True, "color": "404040",
     "spaceBefore": "8pt", "spaceAfter": "4pt"},
]

_HEADER_SHD = "2E5395"   # 表头底色（白字）
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp"}
_IMG_DEFAULT_WIDTH = "14cm"

# ---- 行内解析 ----

_INLINE_RE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<bold>\*\*(?:[^*\n]|\*(?!\*))+\*\*)"
    r"|(?P<italic>\*[^*\n]+\*)"
    r"|(?P<link>\[[^\]\n]+\]\([^)\s]+\))"
)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def _parse_inline(text: str) -> List[Dict[str, Any]]:
    """行内 markdown → 分段清单 [{text, bold/italic/code/url}]；无标记时单段直通。"""
    segs: List[Dict[str, Any]] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            segs.append({"text": text[pos:m.start()]})
        if m.lastgroup == "code":
            segs.append({"text": m.group()[1:-1], "code": True})
        elif m.lastgroup == "bold":
            segs.append({"text": m.group()[2:-2], "bold": True})
        elif m.lastgroup == "italic":
            segs.append({"text": m.group()[1:-1], "italic": True})
        elif m.lastgroup == "link":
            lm = _LINK_RE.match(m.group())
            segs.append({"text": lm.group(1), "url": lm.group(2)})
        pos = m.end()
    if pos < len(text):
        segs.append({"text": text[pos:]})
    return [s for s in segs if s["text"]]


def _plain(segs: List[Dict[str, Any]]) -> str:
    return "".join(s["text"] for s in segs)


# ---- 块解析 ----

_H_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
_IMG_RE = re.compile(r"^!\[([^\]]*)\]\(([^)\s]+)\)\s*$")


def _split_table_row(line: str) -> List[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    # 尊重转义管道 \|（模型输出表格常见）
    cells = re.split(r"(?<!\\)\|", row)
    return [c.replace("\\|", "|").strip() for c in cells]


class _Emitter:
    """batch 指令发射器：跟踪 p/tbl 各自的 1-based 位置索引（officecli 路径按
    元素类型分别计数，非全体块统一计数）。"""

    def __init__(self, image_root: Optional[Path]):
        self.commands: List[Dict[str, Any]] = []
        self.p_count = 0
        self.tbl_count = 0
        self.image_root = image_root
        self.warnings: List[str] = []
        self.text_probes: List[str] = []   # 读回交叉校验采样

    def _probe(self, text: str):
        t = text.strip()
        if len(t) >= 6:
            self.text_probes.append(t)

    def add_paragraph(self, segs: List[Dict[str, Any]],
                      props: Optional[Dict[str, Any]] = None):
        props = dict(props or {})
        self.p_count += 1
        self._probe(_plain(segs))
        simple = len(segs) == 1 and set(segs[0]) <= {"text"}
        if simple:
            props["text"] = segs[0]["text"]
            self.commands.append({"command": "add", "parent": "/body",
                                  "type": "paragraph", "props": props})
            return
        # 混排：空段落定块级属性，分段逐个追加 run/hyperlink
        self.commands.append({"command": "add", "parent": "/body",
                              "type": "paragraph", "props": props})
        parent = f"/body/p[{self.p_count}]"
        for s in segs:
            if "url" in s:
                self.commands.append({"command": "add", "parent": parent,
                                      "type": "hyperlink",
                                      "props": {"url": s["url"], "text": s["text"]}})
                continue
            rp: Dict[str, Any] = {"text": s["text"]}
            if s.get("bold"):
                rp["bold"] = True
            if s.get("italic"):
                rp["italic"] = True
            if s.get("code"):
                rp["font"] = "Courier New"
            self.commands.append({"command": "add", "parent": parent,
                                  "type": "run", "props": rp})

    def add_image(self, alt: str, src: str):
        p = Path(src).expanduser()
        if self.image_root is not None and not p.is_absolute():
            p = self.image_root / p
        if p.suffix.lower() not in _IMG_EXTS or not p.is_file():
            self.warnings.append(f"image skipped (not a local image file): {src}")
            self.add_paragraph([{"text": f"[image unavailable: {alt or src}]",
                                 "italic": True}])
            return
        self.p_count += 1
        self.commands.append({"command": "add", "parent": "/body",
                              "type": "paragraph", "props": {"align": "center"}})
        self.commands.append({"command": "add", "parent": f"/body/p[{self.p_count}]",
                              "type": "picture",
                              "props": {"src": str(p), "width": _IMG_DEFAULT_WIDTH,
                                        "alt": alt or p.stem}})

    def add_table(self, header: List[str], rows: List[List[str]]):
        cols = len(header)
        self.tbl_count += 1
        self.commands.append({"command": "add", "parent": "/body", "type": "table",
                              "props": {"rows": len(rows) + 1, "cols": cols}})
        tbl = f"/body/tbl[{self.tbl_count}]"
        for c, cell in enumerate(header, 1):
            self._probe(cell)
            self.commands.append({"command": "set", "path": f"{tbl}/tr[1]/tc[{c}]",
                                  "props": {"text": cell, "bold": True,
                                            "color": "FFFFFF", "shd": _HEADER_SHD}})
        for r, row in enumerate(rows, 2):
            for c in range(cols):
                cell = row[c] if c < len(row) else ""
                if not cell:
                    continue
                self._probe(cell)
                self.commands.append({"command": "set",
                                      "path": f"{tbl}/tr[{r}]/tc[{c + 1}]",
                                      "props": {"text": cell}})


def md_to_commands(md: str, *, image_root: Optional[Path] = None
                   ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """markdown → (batch 指令清单, 统计/告警/校验采样)。纯函数，不碰二进制。"""
    em = _Emitter(image_root)
    for sd in _STYLE_DEFS:
        props = {"type": "paragraph", "basedOn": "Normal", "next": "Normal",
                 "qFormat": True, **{k: v for k, v in sd.items() if k != "id"}}
        em.commands.append({"command": "add", "parent": "/styles", "type": "style",
                            "props": {"id": sd["id"], **props}})

    lines = md.replace("\r\n", "\n").split("\n")
    i, n = 0, len(lines)
    para_buf: List[str] = []

    def flush_para():
        nonlocal para_buf
        if para_buf:
            em.add_paragraph(_parse_inline(" ".join(s.strip() for s in para_buf)))
            para_buf = []

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):            # 围栏代码块：逐行等宽、原样保缩进
            flush_para()
            i += 1
            code_lines = []
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1                                 # 吃掉闭栏（或文件尾）
            for cl in code_lines:
                em.add_paragraph([{"text": cl or " ", "code": True}],
                                 props={"spaceAfter": "0pt"})
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        m = _H_RE.match(stripped)
        if m:
            flush_para()
            level = min(len(m.group(1)), 4)
            em.add_paragraph(_parse_inline(m.group(2).strip()),
                             props={"style": f"Heading{level}"})
            i += 1
            continue

        if _HR_RE.match(stripped):
            flush_para()
            i += 1
            continue

        m = _IMG_RE.match(stripped)
        if m:
            flush_para()
            em.add_image(m.group(1), m.group(2))
            i += 1
            continue

        m = _LIST_RE.match(line)
        if m:
            flush_para()
            indent, marker, body = m.groups()
            level = min(len(indent.replace("\t", "  ")) // 2, 4)
            style = "bullet" if marker in "-*+" else "ordered"
            props: Dict[str, Any] = {"listStyle": style}
            if level:
                props["numLevel"] = level
            em.add_paragraph(_parse_inline(body.strip()), props=props)
            i += 1
            continue

        if stripped.startswith(">"):
            flush_para()
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            em.add_paragraph(_parse_inline(" ".join(q for q in quote if q)),
                             props={"italic": True, "indent": "0.75cm",
                                    "color": "595959"})
            continue

        # GFM 表：本行含 |，下一行是分隔行
        if "|" in stripped and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            flush_para()
            header = _split_table_row(stripped)
            i += 2
            rows: List[List[str]] = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_table_row(lines[i]))
                i += 1
            em.add_table(header, rows)
            continue

        para_buf.append(line)
        i += 1

    flush_para()
    stats = {"paragraphs": em.p_count, "tables": em.tbl_count,
             "commands": len(em.commands), "warnings": em.warnings,
             "text_probes": em.text_probes}
    return em.commands, stats


# ---- 落地 + 门禁 ----

def _norm_for_match(text: str) -> str:
    """比对归一化：markitdown 会忠实还原 **粗体**/[链接](url) 等 markdown 记号，
    而采样存的是纯文本——两侧同规则剥记号+去空白后再比。"""
    text = _LINK_RE.sub(r"\1", text)          # [x](url) → x
    text = re.sub(r"[*`_#|>]", "", text)
    return re.sub(r"\s+", "", text)


def _readback_coverage(docx_path: Path, probes: List[str]) -> float:
    """读回交叉校验：摄入侧 markitdown（独立实现）抽文本，采样命中率。
    表格/样式细节不在此校验——它守的是"文字内容一个不丢"。"""
    from ..ingestion.parsers.document import parse_document
    extracted = parse_document(str(docx_path), max_chars=200_000)
    norm = _norm_for_match(extracted)
    sample = probes[:60]
    if not sample:
        return 1.0
    hit = sum(1 for t in sample if _norm_for_match(t) in norm)
    return hit / len(sample)


def write_docx(md: str, out_path: Path, *, image_root: Optional[Path] = None,
               min_coverage: float = 0.9) -> Dict[str, Any]:
    """md → .docx 全链：create → batch（原子）→ validate → 读回覆盖率门禁。
    任一门禁失败即删产物并抛 OfficeCliError——绝不交半成品。"""
    commands, stats = md_to_commands(md, image_root=image_root)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_cli(["create", str(out_path)])
        batch(out_path, commands)
        v = validate(out_path)
        if not v.get("success"):
            raise OfficeCliError("generated document failed OpenXML validation", v)
        coverage = _readback_coverage(out_path, stats["text_probes"])
        if coverage < min_coverage:
            raise OfficeCliError(
                f"read-back cross-check failed: only {coverage:.0%} of sampled text "
                f"found by the independent extractor (threshold {min_coverage:.0%})")
    except Exception:
        out_path.unlink(missing_ok=True)
        raise
    stats["coverage"] = coverage
    return stats
