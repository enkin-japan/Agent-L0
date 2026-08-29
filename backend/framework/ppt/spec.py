"""Deck Spec：PPT 系统的单一真相（数据契约 + 校验 + 槽位展开）。

设计源头见 /Volumes/enkin/project/agent/PPT.md。本文件是它落到 Agent 的裁剪版：

 · 单一真相：sessions/{id}/deck/deck.json。人和模型的修改都归一化为对它的读写；
   渲染（前端 SlideRenderer）、预览、PDF 导出全是它的下游投影。
 · 模型面向的 API 是「槽位」而非「块」：小模型直接摆 12×12 栅格块必然溢出/重叠，
   因此 deck_set_slide 只收 layout + 槽位内容（title/bullets/chart/...），由本层的
   确定性模板展开成带栅格坐标的 blocks——这正是 PPT.md 的「执行进程不需要高智商，
   用纯代码」。栅格坐标保留在 spec 里，供渲染器与后续人工编辑（P6 锁页协同）使用。
 · tokens 是发散的围栏：主题只能从 THEMES 预设选，块的样式继承 tokens，
   模型永远不直接写颜色/坐标。
 · id 稳定：slide/block id 生成后不变，锁页、diff、评审定位、增量渲染全靠它。

视觉资产的解析（chart 渲染回填 / image 占位）在 visual_router.py，不在本层。
"""
import uuid
from typing import Dict, List, Optional, Tuple

GRID_COLS = 12
GRID_ROWS = 12

LAYOUTS = ("title", "section", "bullets", "two_col", "chart", "quote", "image_full")

# 主题预设（tokens）：palette/font 是渲染与图表的共同围栏。
# 前端 SlideRenderer 把 palette 映射成 CSS 变量；chart 分支把 palette 喂给 chart_tools。
THEMES: Dict[str, Dict] = {
    "light": {
        "palette": {"primary": "#1a56db", "accent": "#e8590c", "bg": "#ffffff",
                    "fg": "#1f2937", "muted": "#6b7280", "surface": "#f3f4f6",
                    "series": ["#1a56db", "#e8590c", "#0e9f6e", "#b91c1c",
                               "#7e3af2", "#0694a2", "#d61f69", "#6b7280"]},
        "font": {"heading": "'Helvetica Neue', 'PingFang SC', 'Hiragino Sans', sans-serif",
                 "body": "'Helvetica Neue', 'PingFang SC', 'Hiragino Sans', sans-serif"},
    },
    "dark": {
        "palette": {"primary": "#60a5fa", "accent": "#fbbf24", "bg": "#111827",
                    "fg": "#f9fafb", "muted": "#9ca3af", "surface": "#1f2937",
                    "series": ["#60a5fa", "#fbbf24", "#34d399", "#f87171",
                               "#a78bfa", "#22d3ee", "#f472b6", "#9ca3af"]},
        "font": {"heading": "'Helvetica Neue', 'PingFang SC', 'Hiragino Sans', sans-serif",
                 "body": "'Helvetica Neue', 'PingFang SC', 'Hiragino Sans', sans-serif"},
    },
    "warm": {
        "palette": {"primary": "#9a3412", "accent": "#166534", "bg": "#fffbf5",
                    "fg": "#292524", "muted": "#78716c", "surface": "#fef3e2",
                    "series": ["#9a3412", "#166534", "#1d4ed8", "#a21caf",
                               "#b45309", "#0f766e", "#be123c", "#78716c"]},
        "font": {"heading": "'Georgia', 'PingFang SC', 'Hiragino Mincho ProN', serif",
                 "body": "'Helvetica Neue', 'PingFang SC', 'Hiragino Sans', sans-serif"},
    },
}
DEFAULT_THEME = "light"


class DeckSpecError(ValueError):
    """槽位输入校验失败：message 直接回喂模型（fix-and-retry 闭环），须具体可操作。"""


def _bid() -> str:
    return "b-" + uuid.uuid4().hex[:8]


def new_slide_id() -> str:
    return "s-" + uuid.uuid4().hex[:8]


def new_deck(title: str, theme: str = DEFAULT_THEME) -> Dict:
    if theme not in THEMES:
        raise DeckSpecError(f"theme must be one of {list(THEMES)}, got {theme!r}.")
    return {
        "meta": {"title": str(title), "aspect": "16:9", "theme": theme,
                 "tokens": THEMES[theme]},
        "slides": [],
    }


# ---------- 槽位输入的净化 ----------

def _clean_str(slot: Dict, key: str, required: bool = False, max_len: int = 600) -> str:
    v = slot.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise DeckSpecError(f"layout '{slot.get('layout')}' requires a non-empty '{key}'.")
        return ""
    if not isinstance(v, str):
        raise DeckSpecError(f"'{key}' must be a string.")
    return v.strip()[:max_len]


def _clean_bullets(v, where: str, max_items: int = 10) -> List[str]:
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x.strip() for x in v):
        raise DeckSpecError(f"{where} must be a non-empty array of strings.")
    if len(v) > max_items:
        raise DeckSpecError(f"{where} has {len(v)} items (max {max_items}); "
                            "split into two slides instead.")
    return [x.strip()[:300] for x in v]


# ---------- 块构造 ----------

def _text_block(role: str, grid: Tuple[int, int, int, int], *,
                text: str = "", bullets: Optional[List[str]] = None) -> Dict:
    content = {"bullets": bullets} if bullets is not None else {"text": text}
    return {"id": _bid(), "type": "text", "role": role,
            "grid": {"col": grid[0], "row": grid[1], "colSpan": grid[2], "rowSpan": grid[3]},
            "content": content, "visual_request": None}


def _chart_block(grid: Tuple[int, int, int, int], chart_spec: Dict, alt: str) -> Dict:
    """chart 块：先挂 visual_request，由 visual_router 渲染回填 content.asset。"""
    return {"id": _bid(), "type": "chart", "role": "body",
            "grid": {"col": grid[0], "row": grid[1], "colSpan": grid[2], "rowSpan": grid[3]},
            "content": {"asset": None, "alt": alt},
            "visual_request": {"kind": "chart", "spec": chart_spec}}


def _image_block(grid: Tuple[int, int, int, int], prompt: str, alt: str) -> Dict:
    """image 块：本机阶段恒为占位（asset=None）；生产机文生图子 agent 依 visual_request
    生成资产并回填 content.asset——接口契约见 docs/history/ppt-production-todo.md（仓库根，已归档）。"""
    return {"id": _bid(), "type": "image", "role": "body",
            "grid": {"col": grid[0], "row": grid[1], "colSpan": grid[2], "rowSpan": grid[3]},
            "content": {"asset": None, "alt": alt},
            "visual_request": {"kind": "image", "prompt": prompt}}


def _icon_block(grid: Tuple[int, int, int, int], name: str) -> Dict:
    """icon 块：lucide 图标名直接透传，前端原生渲染（未知名字降级为圆点）。"""
    return {"id": _bid(), "type": "icon", "role": "body",
            "grid": {"col": grid[0], "row": grid[1], "colSpan": grid[2], "rowSpan": grid[3]},
            "content": {"name": str(name)}, "visual_request": None}


def _side_blocks(side: Dict, col: int, where: str) -> List[Dict]:
    """two_col 单侧的槽位 → blocks。支持 bullets / text / chart / image / icon+text。"""
    if not isinstance(side, dict):
        raise DeckSpecError(f"{where} must be an object like {{\"bullets\": [...]}} or "
                            f"{{\"chart\": {{...}}}} or {{\"text\": \"...\"}}.")
    blocks: List[Dict] = []
    row, span = 3, 9
    if side.get("icon"):
        blocks.append(_icon_block((col, row, 1, 1), side["icon"]))
        row += 1
        span -= 1
    if side.get("bullets") is not None:
        blocks.append(_text_block("body", (col, row, 6, span),
                                  bullets=_clean_bullets(side["bullets"], f"{where}.bullets")))
    elif side.get("text"):
        blocks.append(_text_block("body", (col, row, 6, span), text=str(side["text"])[:800]))
    elif side.get("chart") is not None:
        blocks.append(_chart_block((col, row, 6, span), side["chart"],
                                   alt=str(side.get("alt", "chart"))))
    elif side.get("image") is not None:
        img = side["image"] if isinstance(side["image"], dict) else {"prompt": str(side["image"])}
        blocks.append(_image_block((col, row, 6, span),
                                   prompt=str(img.get("prompt", ""))[:500],
                                   alt=str(img.get("alt", "image"))[:120]))
    elif not blocks:
        raise DeckSpecError(f"{where} needs one of: bullets, text, chart, image "
                            "(optionally with an 'icon' name).")
    return blocks


# ---------- 布局模板：槽位 → blocks（确定性展开，栅格坐标钉死在这里） ----------

# 各布局实际消费的内容槽位。守卫用：模型给了布局不显示的内容字段时报错回喂，
# 绝不静默丢内容——实测弱模型把整页要点塞进 section 布局，内容凭空蒸发且无人知晓
# （回执报成功、模型以为已写入、用户拿到空壳页）。非内容类的未知杂字段仍静默忽略。
_LAYOUT_SLOTS = {
    "title": ("title", "subtitle"),
    "section": ("title",),
    "bullets": ("title", "bullets"),
    "two_col": ("title", "left", "right"),
    "chart": ("title", "chart", "caption"),
    "quote": ("quote", "attribution"),
    "image_full": ("image", "title"),
}
_CONTENT_KEYS = frozenset(
    {"title", "subtitle", "bullets", "left", "right", "chart", "caption",
     "quote", "attribution", "image"})


def expand_slide(slot: Dict) -> Dict:
    """把槽位输入展开成完整 Slide。校验失败抛 DeckSpecError（消息可直接回喂模型）。"""
    layout = slot.get("layout")
    if layout not in LAYOUTS:
        raise DeckSpecError(f"layout must be one of {list(LAYOUTS)}, got {layout!r}.")

    dropped = sorted(k for k in _CONTENT_KEYS - set(_LAYOUT_SLOTS[layout])
                     if slot.get(k) not in (None, "", [], {}))
    if dropped:
        raise DeckSpecError(
            f"layout '{layout}' only displays {list(_LAYOUT_SLOTS[layout])} and would DROP your "
            f"{dropped} content. Nothing was saved. Call deck_set_slide again with the same "
            f"position but a layout that shows this content — e.g. 'bullets' for bullet points, "
            f"'chart' for a chart, 'two_col' for two columns.")

    blocks: List[Dict] = []
    if layout == "title":
        blocks.append(_text_block("title", (2, 4, 10, 3), text=_clean_str(slot, "title", True)))
        sub = _clean_str(slot, "subtitle")
        if sub:
            blocks.append(_text_block("subtitle", (2, 7, 10, 2), text=sub))
    elif layout == "section":
        blocks.append(_text_block("title", (2, 5, 10, 3), text=_clean_str(slot, "title", True)))
    elif layout == "bullets":
        blocks.append(_text_block("title", (1, 1, 12, 2), text=_clean_str(slot, "title", True)))
        blocks.append(_text_block("body", (1, 3, 12, 9),
                                  bullets=_clean_bullets(slot.get("bullets"), "bullets")))
    elif layout == "two_col":
        blocks.append(_text_block("title", (1, 1, 12, 2), text=_clean_str(slot, "title", True)))
        if slot.get("left") is None or slot.get("right") is None:
            raise DeckSpecError("two_col requires both 'left' and 'right' objects "
                                '(each like {"bullets": [...]} / {"text": "..."} / '
                                '{"chart": {...}} / {"image": {"prompt": "..."}}).')
        blocks += _side_blocks(slot["left"], 1, "left")
        blocks += _side_blocks(slot["right"], 7, "right")
    elif layout == "chart":
        blocks.append(_text_block("title", (1, 1, 12, 2), text=_clean_str(slot, "title", True)))
        if not isinstance(slot.get("chart"), dict):
            raise DeckSpecError("chart layout requires a 'chart' object: "
                                '{"chart_type": "bar", "labels": [...], '
                                '"series": [{"name": "...", "values": [...]}]}.')
        blocks.append(_chart_block((2, 3, 10, 8), slot["chart"],
                                   alt=_clean_str(slot, "title") or "chart"))
        cap = _clean_str(slot, "caption")
        if cap:
            blocks.append(_text_block("caption", (2, 11, 10, 1), text=cap))
    elif layout == "quote":
        blocks.append(_text_block("emphasis", (2, 4, 10, 4), text=_clean_str(slot, "quote", True)))
        attr = _clean_str(slot, "attribution")
        if attr:
            blocks.append(_text_block("caption", (2, 9, 10, 1), text=attr))
    elif layout == "image_full":
        img = slot.get("image")
        if not isinstance(img, dict) or not str(img.get("prompt", "")).strip():
            raise DeckSpecError('image_full requires an \'image\' object: '
                                '{"prompt": "description of the picture", "alt": "..."}.')
        blocks.append(_image_block((1, 1, 12, 12), prompt=str(img["prompt"])[:500],
                                   alt=str(img.get("alt", "image"))[:120]))
        t = _clean_str(slot, "title")
        if t:
            blocks.append(_text_block("title", (2, 10, 10, 2), text=t))

    return {
        "id": new_slide_id(),
        "layout": layout,
        "blocks": blocks,
        "notes": _clean_str(slot, "notes", max_len=1000),
        "lock": {"locked": False, "dirty_by": "agent"},
    }


# ---------- 状态摘要（deck_status 工具 / 工具回执用，避免把整份 JSON 灌进上下文） ----------

def slide_summary(slide: Dict, index: int) -> str:
    bits = []
    for b in slide["blocks"]:
        c = b.get("content", {})
        if b["type"] == "text" and b.get("role") == "title" and c.get("text"):
            bits.append(c["text"][:40])
            break
    if not bits:
        for b in slide["blocks"]:
            c = b.get("content", {})
            if c.get("text"):
                bits.append(c["text"][:40])
                break
    label = bits[0] if bits else "(untitled)"
    extras = []
    n_bullets = sum(len(b["content"].get("bullets") or []) for b in slide["blocks"]
                    if b["type"] == "text")
    if n_bullets:
        extras.append(f"{n_bullets} bullets")
    for b in slide["blocks"]:
        if b["type"] == "chart":
            extras.append("chart" + ("" if b["content"].get("asset") else " PENDING"))
        if b["type"] == "image":
            extras.append("image" + ("" if b["content"].get("asset") else " placeholder"))
    if slide.get("lock", {}).get("locked"):
        extras.append("LOCKED")
    extra = f" ({', '.join(extras)})" if extras else ""
    return f"{index}. [{slide['layout']}] {label}{extra}"


def deck_outline(deck: Dict) -> str:
    meta = deck["meta"]
    lines = [f"Deck \"{meta['title']}\" — theme {meta['theme']}, "
             f"{len(deck['slides'])} slide(s):"]
    for i, s in enumerate(deck["slides"], 1):
        lines.append("  " + slide_summary(s, i))
    if not deck["slides"]:
        lines.append("  (empty — add slides with deck_set_slide)")
    return "\n".join(lines)
