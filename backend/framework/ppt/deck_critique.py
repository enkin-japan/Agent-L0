"""评审回炉：渲染后多模态 critique → 单轮修订（ppt-production-todo 任务 B，PPT 质量杠杆）。

设计/执行都是「盲写」——只有把成品当图看才能发现视觉问题（标题截断/太挤/对比差/图文
失衡）。本模块把 deck 逐页截图，每页开一个一次性视觉会话（仿 agents/excel_sandbox 的
`_visual_qa` 图片单发格局，经 model_router 路由 vision 端点），让模型产出该页的修订
槽位，再经 deck_ops.set_slide 重展开回写。**单轮即止**（不无限回炉）。

隔离/安全：
 · 视觉会话是 EphemeralSession（全新 KV、不落盘、inject_env_context=False，个人事实不进）；
 · 只读 deck + 写本会话 assets/deck，无外发——免审批依据同 deck_export；
 · 回写走 set_slide（锁页拒改天然守住 + 替换保 id 稳定），chart 真实数字从 source 槽位
   原样带过；**无 source 槽位的旧页只报不改**（不让模型盲authored 丢数据）。
"""
import asyncio
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..agents.sandbox_runtime import EphemeralSession
from . import deck_ops
from .spec import slide_summary

CRITIQUE_SYSTEM_PROMPT = (
    "You are a slide-design reviewer. You are shown ONE rendered presentation slide (1280x720) "
    "together with its current authoring spec. Judge it as a viewer would, and look for VISUAL "
    "defects ONLY:\n"
    "- title or text truncated / clipped at the edges\n"
    "- overcrowding: too many bullets, or text too dense to read comfortably\n"
    "- poor contrast / unreadable text against its background\n"
    "- imbalance: a large empty area, or text colliding with a chart or image\n"
    "Hard rules: do NOT invent facts; do NOT change any numbers in a chart; do NOT rewrite "
    "content merely for style preference; keep it ONE slide (never split). Only fix concrete "
    "visual defects. If the slide is visually fine, return verdict='pass'.\n"
    "Call submit_review exactly once. When revising, start from the current spec and change ONLY "
    "what fixes the defect (e.g. shorten an over-long title, cut bullets from 8 to 5), and copy "
    "any chart/image content through unchanged."
)

_SUBMIT_REVIEW_SCHEMA = {
    "name": "submit_review",
    "description": "Report your visual review of this one slide. verdict='pass' when no visual "
                   "defect warrants a re-render; verdict='revise' plus a corrected slot when it does.",
    "parameters": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "revise"]},
            "issues": {"type": "array", "items": {"type": "string"},
                       "description": "Short, concrete visual defects found (only when revising)."},
            "slot": {"type": "object",
                     "description": "The corrected authoring slot — SAME schema as the deck "
                                    "authoring tool (layout + content fields: title/subtitle/"
                                    "bullets/left/right/chart/quote/image/caption/...). Required "
                                    "when verdict='revise'. Start from the current spec; change "
                                    "only what fixes the defect; keep chart/image data unchanged."},
        },
        "required": ["verdict"],
    },
}


async def _route_vision(engine):
    """视觉会话选路（仿 excel _visual_qa）：主模型无 vision 或专科更优时改走专科端点；
    选路/装配任何失败一律 fail-soft 回落主引擎并响亮留痕（评审是增强不是门禁）。"""
    qa_engine = engine
    try:
        from ..core.model_router import select_endpoint
        from ..agents.runtime import get_engine_pool
        pool = get_engine_pool()
        if pool is not None:
            name = select_endpoint("deck_critique", needs=("vision",))
            if name != "main":
                qa_engine = await pool.engine_for(name)
                print(f"🔀 [critique] 视觉回读路由 → 专科端点 {name!r}")
    except Exception as e:
        print(f"⚠️ [critique] 视觉路由失败，回落主引擎: {e}")
    return qa_engine


def _screenshot_pages(base: str, host_kind: str, url: str,
                      shots_dir: Path, timeout_s: int) -> List[Path]:
    """逐页截图 `.deck-print-page` 元素（1280×720 原图）。同步 Playwright（系统 Chrome），
    由调用方经 asyncio.to_thread 投入线程。复用 pdf_export 的等信标 + 图片就绪纪律。"""
    import os

    from ..utils.config_loader import load_config
    from playwright.sync_api import sync_playwright

    out: List[Path] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            if host_kind == "self-hosted":
                # 自托管页与后端同源：数据请求经上下文注入 Bearer 过鉴权（同 pdf_export）
                key = os.environ.get("AGENT_API_KEY") \
                    or (load_config().get("server") or {}).get("api_key") or ""
                if key:
                    page.set_extra_http_headers({"Authorization": f"Bearer {key}"})
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            page.wait_for_selector("#deck-print-ready, #deck-print-error",
                                   state="attached", timeout=timeout_s * 1000)
            if page.query_selector("#deck-print-error") is not None:
                raise RuntimeError(page.inner_text("#deck-print-error"))
            page.wait_for_function(
                "Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)",
                timeout=timeout_s * 1000)
            for i, el in enumerate(page.query_selector_all(".deck-print-page")):
                p = shots_dir / f"page_{i + 1}.png"
                el.screenshot(path=str(p))
                out.append(p)
        finally:
            browser.close()
    return out


def _current_spec_text(slide: Dict) -> Tuple[str, bool]:
    """喂给评审的「当前规格」文本 + 是否有 source 槽位（决定能否保真回写）。"""
    src = slide.get("source")
    if isinstance(src, dict):
        return json.dumps(src, ensure_ascii=False), True
    return f"(layout={slide.get('layout')}) {slide_summary(slide, 0)}", False


async def _review_page(qa_engine, spec_text: str, pos: int, png: Path, cfg: dict) -> Optional[dict]:
    """单页视觉回读：一次性会话喂截图 + 当前规格 → 一次 submit_review。返回 arguments 或 None。"""
    opening = (f"This is rendered slide {pos} of the deck. Its current authoring spec is:\n"
               f"{spec_text}\n\nInspect the rendered image for visual defects, then call submit_review.")
    sess = EphemeralSession(qa_engine.backend, CRITIQUE_SYSTEM_PROMPT, opening, images=[str(png)])
    r = await qa_engine.generate_once(sess, tools=[_SUBMIT_REVIEW_SCHEMA],
                                      temperature=0.1, max_tokens=int(cfg.get("max_tokens", 500)))
    for c in r.get("tool_calls", []) or []:
        if c.get("type") != "error" and c.get("name") == "submit_review":
            return c.get("arguments", {}) or {}
    return None  # 没给合法 tool call → 视为无意见（不强改）


def _format_digest(revised: List[Tuple[int, List[str]]], passed: List[int],
                   skipped: List[str]) -> str:
    parts = [f"Deck critique done: {len(revised)} slide(s) revised, {len(passed)} passed"
             + (f", {len(skipped)} skipped" if skipped else "") + "."]
    for pos, issues in revised:
        parts.append(f"- slide {pos} revised: " + ("; ".join(issues) if issues else "visual fix applied"))
    if passed:
        parts.append("- passed (no visual defect): " + ", ".join(str(p) for p in passed))
    if skipped:
        parts.append("- skipped: " + "; ".join(skipped))
    if revised:
        parts.append("Re-render / re-export to see the revised slides.")
    return "\n".join(parts)


async def critique_deck(engine, session_id: str, session_dir: Path, cfg: dict) -> str:
    """评审回炉主流程：截图 → 逐页视觉回读 → 修订回写。返回给上层的摘要文本。"""
    if not cfg.get("enabled", True):
        return "Deck critique is disabled in config (ppt.critique.enabled=false)."
    deck = deck_ops.load_deck(session_dir)
    if deck is None or not deck.get("slides"):
        return "No deck with slides to review yet — build one with deck_init / deck_set_slide first."

    from . import pdf_export
    from .deck_tools_cfg import PPT_CFG
    # 打印宿主选择要顶层 ppt.frontend_base_url（不在 critique 子段里）——传整段 ppt 配置。
    base, host_kind = pdf_export._pick_print_host(PPT_CFG)
    if base is None:
        return ("System Error: deck critique unavailable — neither the frontend service nor the "
                "self-hosted print bundle is present (run `./devctl.sh bundle-print`).")
    try:
        import playwright  # noqa: F401
    except Exception as e:
        return f"System Error: deck critique unavailable — playwright missing ({e})."

    timeout_s = int(cfg.get("screenshot_timeout", 40))
    url = f"{base}/print/deck?session={session_id}"
    shots_dir = session_dir / "assets" / "_critique"
    shots_dir.mkdir(parents=True, exist_ok=True)
    try:
        pages = await asyncio.to_thread(_screenshot_pages, base, host_kind, url, shots_dir, timeout_s)
    except Exception as e:
        return f"System Error: deck critique screenshot failed: {e} (print host {host_kind} @ {base})."

    slides = deck["slides"]
    if len(pages) != len(slides):
        # 页数对不上（渲染漂移/未就绪）：只评能一一对上的前 min 页，其余留痕
        print(f"⚠️ [critique] 页数不符：截到 {len(pages)} 页 vs deck {len(slides)} 页，按较小者评审")
    n = min(len(pages), len(slides))
    max_pages = int(cfg.get("max_pages", 12))
    qa_engine = await _route_vision(engine)

    revised: List[Tuple[int, List[str]]] = []
    passed: List[int] = []
    skipped: List[str] = []
    for i in range(n):
        pos = i + 1
        slide, png = slides[i], pages[i]
        if i >= max_pages:
            skipped.append(f"p{pos}(over max_pages={max_pages})")
            continue
        if slide.get("lock", {}).get("locked"):
            skipped.append(f"p{pos}(locked)")
            continue
        spec_text, has_source = _current_spec_text(slide)
        review = await _review_page(qa_engine, spec_text, pos, png, cfg)
        if not review or review.get("verdict") != "revise":
            passed.append(pos)
            continue
        issues = [str(x) for x in (review.get("issues") or [])]
        if not has_source:
            # 无源槽位 → 不能保真重展开，只报不改（旧 deck 兼容路径）
            skipped.append(f"p{pos}(revise reported but no source spec to rewrite safely: "
                           + ("; ".join(issues) or "visual defect") + ")")
            continue
        new_slot = review.get("slot")
        if not isinstance(new_slot, dict) or not new_slot.get("layout"):
            skipped.append(f"p{pos}(revise without a valid slot: {'; '.join(issues) or 'n/a'})")
            continue
        ok, msg = deck_ops.set_slide(session_dir, pos, new_slot)
        if ok:
            revised.append((pos, issues))
        else:
            skipped.append(f"p{pos}(writeback rejected: {msg})")

    if n < len(slides):
        skipped.append(f"pages {n + 1}..{len(slides)} not reviewed (screenshot count mismatch)")
    return _format_digest(revised, passed, skipped)
