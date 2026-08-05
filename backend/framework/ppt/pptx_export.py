"""deck.json → .pptx 可编辑交付（OfficeCLI 集成第 2 站）。

定位（与 pdf_export 的分工）：PDF 是「预览即所得」的真身——前端 SlideCanvas 唯一
渲染器的同一投影；本层是 deck spec 的**第二投影**，产出用户可在 PowerPoint/Keynote
里继续编辑的真 .pptx。两投影读同一份 deck.json 单一真相，但排版引擎不同，细节
必有漂移——工具回执明示这一点，绝不谎称"和预览一模一样"。

确定性展开：12×12 栅格 → 16:9 英寸绝对坐标；角色→字号/颜色由 THEMES tokens
派生；chart 块嵌 visual_router 已渲染的 PNG 资产；image 块=风格化占位（与前端
同语义——文生图等生产机）；icon 块跳过（lucide 是前端原生渲染，无位图资产，
装饰性省略并入 stats 如实上报）。构建走 officecli batch（原子：任一条失败整文件
回滚）→ OpenXML validate 门禁，失败删产物如实报错。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 16:9 幻灯片画布（PowerPoint 默认宽屏）
SLIDE_W_IN = 13.333
SLIDE_H_IN = 7.5
_CELL_W = SLIDE_W_IN / 12
_CELL_H = SLIDE_H_IN / 12


def _hex(color: str, fallback: str = "000000") -> str:
    c = (color or fallback).lstrip("#")
    return c if len(c) in (6, 8) else fallback


def _font_family(css_stack: str, fallback: str = "Helvetica Neue") -> str:
    """CSS 字体栈取首个族名：\"'Georgia', 'PingFang SC', serif\" → Georgia。"""
    first = (css_stack or "").split(",")[0].strip().strip("'\"").strip()
    return first or fallback


def _geom(grid: Dict) -> Dict[str, str]:
    """栅格（1-based，12×12）→ 英寸绝对定位。"""
    return {
        "x": f"{(grid['col'] - 1) * _CELL_W:.3f}in",
        "y": f"{(grid['row'] - 1) * _CELL_H:.3f}in",
        "width": f"{grid['colSpan'] * _CELL_W:.3f}in",
        "height": f"{grid['rowSpan'] * _CELL_H:.3f}in",
    }


def _role_style(role: str, layout: str, pal: Dict) -> Dict[str, Any]:
    """块角色 → 字号/颜色/对齐（THEMES tokens 派生，与前端渲染器同语义近似）。"""
    primary, fg = _hex(pal.get("primary"), "1a56db"), _hex(pal.get("fg"), "1f2937")
    muted = _hex(pal.get("muted"), "6b7280")
    hero = layout in ("title", "section")
    styles = {
        "title": {"size": 40 if hero else 26, "bold": True, "color": primary,
                  **({"align": "center", "valign": "middle"} if hero else {})},
        "subtitle": {"size": 18, "color": muted, "align": "center"},
        "body": {"size": 15, "color": fg, "valign": "top"},
        "emphasis": {"size": 22, "italic": True, "color": fg,
                     "align": "center", "valign": "middle"},
        "caption": {"size": 12, "color": muted},
    }
    return dict(styles.get(role) or styles["body"])


class _SlideEmitter:
    def __init__(self, deck: Dict, assets_dir: Path):
        self.commands: List[Dict[str, Any]] = []
        self.assets_dir = assets_dir
        self.stats = {"slides": 0, "shapes": 0, "pictures": 0,
                      "placeholders": 0, "icons_skipped": 0, "warnings": []}
        tokens = deck["meta"].get("tokens", {})
        self.pal = tokens.get("palette", {})
        fonts = tokens.get("font", {})
        self.f_head = _font_family(fonts.get("heading", ""))
        self.f_body = _font_family(fonts.get("body", ""))
        self.slide_idx = 0
        self.shape_idx = 0

    # ---- 底层发射 ----

    def _slide(self):
        self.slide_idx += 1
        self.shape_idx = 0
        self.stats["slides"] += 1
        self.commands.append({"command": "add", "parent": "/", "type": "slide",
                              "props": {"background": _hex(self.pal.get("bg"), "ffffff")}})

    def _shape(self, grid: Dict, props: Dict[str, Any]) -> int:
        self.shape_idx += 1
        self.stats["shapes"] += 1
        self.commands.append({"command": "add", "parent": f"/slide[{self.slide_idx}]",
                              "type": "shape", "props": {**_geom(grid), **props}})
        return self.shape_idx

    # ---- 块分派 ----

    def _text_block(self, block: Dict, layout: str):
        content = block.get("content", {})
        style = _role_style(block.get("role", "body"), layout, self.pal)
        font = self.f_head if block.get("role") in ("title", "subtitle") else self.f_body
        base = {"font": font, "autoFit": "shrink", **style}
        bullets = content.get("bullets")
        if bullets:
            idx = self._shape(block["grid"], base)
            for line in bullets:
                self.commands.append({
                    "command": "add", "parent": f"/slide[{self.slide_idx}]/shape[{idx}]",
                    "type": "paragraph",
                    "props": {"text": line, "list": "bullet",
                              "size": style.get("size", 15),
                              "color": style.get("color"), "font": font}})
            return
        text = content.get("text", "")
        if text:
            self._shape(block["grid"], {**base, "text": text})

    def _picture_or_placeholder(self, block: Dict, kind: str):
        content = block.get("content", {})
        asset = content.get("asset")
        if asset and (self.assets_dir / asset).is_file():
            self.stats["pictures"] += 1
            self.commands.append({"command": "add", "parent": f"/slide[{self.slide_idx}]",
                                  "type": "picture",
                                  "props": {**_geom(block["grid"]),
                                            "src": str(self.assets_dir / asset)}})
            return
        # 占位：与前端风格化占位同语义（image 等生产机文生图 / chart 渲染缺失）
        self.stats["placeholders"] += 1
        label = content.get("alt") or kind
        prompt = (block.get("visual_request") or {}).get("prompt", "")
        if prompt:
            label += f"\n({prompt[:80]})"
        if asset:
            self.stats["warnings"].append(f"asset missing on disk: {asset}")
        self._shape(block["grid"], {
            "text": label, "size": 14, "italic": True,
            "color": _hex(self.pal.get("muted"), "6b7280"),
            "fill": _hex(self.pal.get("surface"), "f3f4f6"),
            "align": "center", "valign": "middle", "font": self.f_body})

    def emit_slide(self, slide: Dict):
        self._slide()
        layout = slide.get("layout", "bullets")
        for block in slide.get("blocks", []):
            btype = block.get("type")
            if btype == "text":
                self._text_block(block, layout)
            elif btype in ("chart", "image"):
                self._picture_or_placeholder(block, btype)
            elif btype == "icon":
                self.stats["icons_skipped"] += 1   # lucide 前端原生渲染，无位图可嵌
        notes = slide.get("notes")
        if notes:
            self.commands.append({"command": "add", "parent": f"/slide[{self.slide_idx}]",
                                  "type": "notes", "props": {"text": notes}})


def deck_to_commands(deck: Dict, assets_dir: Path) -> Tuple[List[Dict], Dict]:
    """deck spec → officecli batch 指令（纯函数，金测试锚点）。"""
    em = _SlideEmitter(deck, assets_dir)
    for slide in deck.get("slides", []):
        em.emit_slide(slide)
    return em.commands, em.stats


def export_pptx(session_id: str, session_dir: Path) -> Tuple[bool, str, Optional[Dict]]:
    """导出会话 deck 为 .pptx 进 assets。返回 (ok, 文件名或错误信息, stats)。"""
    from ..agents.chart_tools import asset_filename
    from ..office.officecli import OfficeCliError, batch, run_cli, validate
    from . import deck_ops

    deck = deck_ops.load_deck(Path(session_dir))
    if deck is None:
        return False, "no deck in this session yet (deck_init first).", None
    if not deck.get("slides"):
        return False, "the deck has no slides yet.", None

    assets = Path(session_dir) / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    commands, stats = deck_to_commands(deck, assets)
    fname = asset_filename(deck["meta"].get("title", "deck"), kind="deck", ext="pptx")
    out = assets / fname
    try:
        run_cli(["create", str(out)])
        batch(out, commands)
        v = validate(out)
        if not v.get("success"):
            raise OfficeCliError("exported pptx failed OpenXML validation", v)
    except OfficeCliError as e:
        out.unlink(missing_ok=True)
        detail = f" ({e.detail})" if getattr(e, "detail", None) else ""
        return False, f"pptx export failed, nothing delivered: {e}{detail}", None
    return True, fname, stats
