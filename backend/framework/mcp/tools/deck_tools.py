"""deck_* 工具组：PPT（演示文稿）制作的 MCP 包壳。

能力本体在 framework/ppt/（spec 契约、槽位展开、视觉路由、deck_ops 读写）；
本文件只做参数透传。deck 归会话：spec 落 sessions/{id}/deck/deck.json，
前端预览面板经 GET /api/v1/files/{session_id}/deck 实时投影，用户全程可见。

模型面向的是「槽位」而非栅格块：每页只给 layout + 内容槽（title/bullets/chart/...），
栅格坐标由 framework/ppt/spec.py 的布局模板确定性展开——小模型不摆坐标。
"""
from ..base import BaseTool
from ...agents import runtime
from ...ppt.deck_tools_cfg import PPT_CFG


def _session_or_error():
    session = runtime.get_current_session()
    if session is None:
        return None, "System Error: no active session context; deck tools must run inside a chat turn."
    return session, None


# two_col 单侧内容的复用 schema
_SIDE_SCHEMA = {
    "type": "object",
    "description": "Column content: give exactly one of bullets / text / chart / image "
                   "(plus an optional lucide icon name).",
    "properties": {
        "bullets": {"type": "array", "items": {"type": "string"}},
        "text": {"type": "string"},
        "chart": {"type": "object",
                  "description": "Same structure as the chart field of a chart slide."},
        "image": {"type": "object",
                  "properties": {"prompt": {"type": "string"}, "alt": {"type": "string"}}},
        "icon": {"type": "string", "description": "Optional lucide icon name, e.g. 'rocket'."},
    },
}

_CHART_SCHEMA = {
    "type": "object",
    "description": "Structured chart data (you never write plotting code; use REAL numbers, "
                   "never invented ones).",
    "properties": {
        "chart_type": {"type": "string", "enum": ["bar", "barh", "line", "pie", "scatter"]},
        "title": {"type": "string"},
        "labels": {"type": "array", "items": {"type": "string"},
                   "description": "Category names (x ticks for bar/line, slice names for pie)."},
        "series": {"type": "array",
                   "items": {"type": "object",
                             "properties": {"name": {"type": "string"},
                                            "values": {"type": "array",
                                                       "items": {"type": "number"}},
                                            "x": {"type": "array",
                                                  "items": {"type": "number"},
                                                  "description": "Scatter only."}},
                             "required": ["values"]}},
        "x_label": {"type": "string"},
        "y_label": {"type": "string"},
    },
    "required": ["chart_type", "series"],
}


class DeckInitTool(BaseTool):
    name = "deck_init"
    description = (
        "Start a presentation deck for this session (one deck per session; the user watches it "
        "build live in the preview panel). Call once, then add slides one at a time with "
        "deck_set_slide. Refuses to overwrite an existing deck unless overwrite=true."
    )
    requires_approval = False
    is_concurrency_safe = False
    parameters = {
        "properties": {
            "title": {"type": "string", "description": "Deck title (also used on the title slide you should add next)."},
            "theme": {"type": "string", "enum": ["light", "dark", "warm"],
                      "description": "Visual theme (default light)."},
            "overwrite": {"type": "boolean",
                          "description": "Set true ONLY to discard an existing deck and start fresh."},
        },
        "required": ["title"],
    }

    def execute(self, title: str, theme: str = "light", overwrite: bool = False) -> str:
        from ...ppt import deck_ops
        session, err = _session_or_error()
        if err:
            return err
        ok, msg = deck_ops.init_deck(session.session_dir, title, theme, bool(overwrite))
        return msg


class DeckSetSlideTool(BaseTool):
    name = "deck_set_slide"
    description = (
        "Add or replace ONE slide of the session deck. position 1..N replaces that slide, "
        "N+1 or omitted appends. Fill only the slots your chosen layout uses: "
        "title→title+subtitle; section→title; bullets→title+bullets (max 10, keep each short); "
        "two_col→title+left+right; chart→title+chart(+caption); quote→quote(+attribution); "
        "image_full→image(+title). Charts take structured data — use REAL numbers from the "
        "conversation or tool results, never invented ones. Images render as styled placeholders "
        "for now (generation arrives later) — still write a concrete prompt. "
        "Add slides one per call so the user can follow along in the live preview."
    )
    requires_approval = False
    is_concurrency_safe = False  # 读改写 deck.json，串行保证顺序一致
    parameters = {
        "properties": {
            "position": {"type": "integer",
                         "description": "1-based slide position: existing = replace, N+1/omit = append."},
            "layout": {"type": "string",
                       "enum": ["title", "section", "bullets", "two_col", "chart", "quote",
                                "image_full"]},
            "title": {"type": "string"},
            "subtitle": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"},
                        "description": "Bullet lines for the bullets layout."},
            "left": _SIDE_SCHEMA,
            "right": _SIDE_SCHEMA,
            "chart": _CHART_SCHEMA,
            "image": {"type": "object",
                      "properties": {"prompt": {"type": "string",
                                                "description": "Concrete description of the picture."},
                                     "alt": {"type": "string"}},
                      "description": "For image_full layout."},
            "quote": {"type": "string"},
            "attribution": {"type": "string"},
            "caption": {"type": "string"},
            "notes": {"type": "string", "description": "Speaker notes (optional)."},
        },
        "required": ["layout"],
    }

    def execute(self, layout: str, position: int = 0, **slots) -> str:
        from ...ppt import deck_ops
        session, err = _session_or_error()
        if err:
            return err
        slot = {"layout": layout, **{k: v for k, v in slots.items() if v is not None}}
        ok, msg = deck_ops.set_slide(session.session_dir, position, slot)
        return msg


class DeckRemoveSlideTool(BaseTool):
    name = "deck_remove_slide"
    description = "Remove one slide from the session deck by 1-based position."
    requires_approval = False
    is_concurrency_safe = False
    parameters = {
        "properties": {
            "position": {"type": "integer", "description": "1-based position of the slide to remove."},
        },
        "required": ["position"],
    }

    def execute(self, position: int) -> str:
        from ...ppt import deck_ops
        session, err = _session_or_error()
        if err:
            return err
        ok, msg = deck_ops.remove_slide(session.session_dir, int(position))
        return msg


class DeckExportPdfTool(BaseTool):
    name = "deck_export_pdf"
    description = (
        "Export the session deck as a PDF file (one page per slide, exactly as previewed). "
        "Returns a markdown link — include it in your reply so the user can download the file. "
        "Requires the deck to have at least one slide."
    )
    requires_approval = False   # 只读 deck + 写本会话 assets；无外发、无系统副作用
    is_concurrency_safe = False  # 独占无头 Chrome 实例，串行执行
    parameters = {"properties": {}, "required": []}

    def is_available(self) -> bool:
        try:
            import playwright  # noqa: F401
        except Exception as e:
            print(f"[MCP-deck_export_pdf] ⚠️ playwright 不可用（{e}），工具不注册。")
            return False
        return True

    def execute(self) -> str:
        from ...ppt import pdf_export
        from ...ppt.deck_tools_cfg import PPT_CFG
        session, err = _session_or_error()
        if err:
            return err
        ok, result = pdf_export.export_pdf(session.session_id, session.session_dir, PPT_CFG)
        if not ok:
            return f"System Error: {result}"
        url = f"/api/v1/files/{session.session_id}/assets/{result}"
        return ("PDF exported. Include this markdown link in your reply so the user can "
                f"download it:\n[deck PDF]({url})")


class DeckExportPptxTool(BaseTool):
    name = "deck_export_pptx"
    description = (
        "Export the session deck as an EDITABLE PowerPoint file (.pptx) the user can open and "
        "modify in PowerPoint/Keynote. Layout is a close approximation of the preview (minor "
        "styling differences are expected — the PDF export matches the preview exactly; this "
        "one is for editing). Returns a markdown link — include it in your reply. Requires the "
        "deck to have at least one slide."
    )
    requires_approval = False   # 只读 deck + 写本会话 assets；无外发、无系统副作用
    is_concurrency_safe = False  # 与 deck 写操作串行
    parameters = {"properties": {}, "required": []}

    def is_available(self) -> bool:
        from ...office.officecli import find_officecli
        if find_officecli() is None:
            print("[MCP-deck_export_pptx] ℹ️ officecli 未安装，工具不注册；装上后热重扫自动补上。")
            return False
        return True

    def execute(self) -> str:
        from ...ppt import pptx_export
        session, err = _session_or_error()
        if err:
            return err
        ok, result, stats = pptx_export.export_pptx(session.session_id, session.session_dir)
        if not ok:
            return f"System Error: {result}"
        url = f"/api/v1/files/{session.session_id}/assets/{result}"
        note = ""
        if stats and (stats["placeholders"] or stats["warnings"]):
            bits = []
            if stats["placeholders"]:
                bits.append(f"{stats['placeholders']} visual(s) exported as styled placeholders "
                            "(images render on the production setup)")
            bits.extend(stats["warnings"])
            note = "\nNote (tell the user honestly): " + "; ".join(bits)
        return (f"Editable PowerPoint exported ({stats['slides']} slides, validated). Include "
                f"this markdown link in your reply so the user can download it:\n"
                f"[deck PPTX]({url}){note}")


class DeckCritiqueTool(BaseTool):
    name = "deck_critique"
    description = (
        "Review the finished deck the way a viewer sees it: screenshot every slide, inspect each "
        "rendered page for VISUAL defects (title/text truncation, overcrowding, poor contrast, "
        "text colliding with a chart/image), and automatically apply a ONE-ROUND fix to the slides "
        "that need it. Call this AFTER the deck is built, then re-export to see the result. It only "
        "fixes concrete visual problems — never invents facts or changes chart numbers — and leaves "
        "locked slides untouched. Requires the deck to have at least one slide."
    )
    requires_approval = False    # 只读 deck + 写本会话 deck/assets；无外发（同 deck_export）
    is_concurrency_safe = False  # 独占无头 Chrome + 读改写 deck.json，串行
    # 多页视觉回读远超全局 180s 单工具熔断：声明覆盖值（同 web/excel_research 格局）
    timeout_seconds = int((PPT_CFG.get("critique") or {}).get("total_timeout", 240)) + 30

    def is_available(self) -> bool:
        try:
            import playwright  # noqa: F401
        except Exception as e:
            print(f"[MCP-deck_critique] ⚠️ playwright 不可用（{e}），工具不注册。")
            return False
        return True

    def execute(self) -> str:
        import asyncio
        import concurrent.futures

        from ...ppt.deck_critique import critique_deck
        session, err = _session_or_error()
        if err:
            return err
        if not runtime.is_ready():
            return "System Error: critique runtime not initialized (engine/event loop missing)."
        cfg = PPT_CFG.get("critique") or {}
        engine = runtime.get_engine()
        loop = runtime.get_loop()
        total_timeout = int(cfg.get("total_timeout", 240))
        try:
            fut = asyncio.run_coroutine_threadsafe(
                critique_deck(engine, session.session_id, session.session_dir, cfg), loop)
        except Exception as e:
            return f"System Error: could not schedule deck critique: {e}"
        try:
            return fut.result(timeout=total_timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            return "[deck critique timed out before completion — the deck was left unchanged]"
        except Exception as e:
            return f"System Error: deck critique raised an exception: {e}"


class DeckStatusTool(BaseTool):
    name = "deck_status"
    description = (
        "Show the session deck's current outline: slide count, per-slide layout/title, pending "
        "visuals, locked slides. Call before editing an existing deck to see what's there."
    )
    requires_approval = False
    effect = "read"     # 审批策略分类：只读取，进程外无副作用
    is_concurrency_safe = True
    is_read_only = True
    parameters = {"properties": {}, "required": []}

    def execute(self) -> str:
        from ...ppt import deck_ops
        session, err = _session_or_error()
        if err:
            return err
        return deck_ops.outline(session.session_dir)
