"""PDF 导出：无头 Chrome 打印前端 /print/deck 路由。

渲染器全程只有一份（前端 SlideCanvas）——PDF 是「预览即所得」的同一投影，
不在后端复刻第二个渲染器（两个渲染器必然漂移）。代价是导出依赖前端服务在跑
（dev: Vite 5173 / 生产: Nginx），不可用时给出明确报错而不是静默降级。

Playwright 同步 API 在 MCP 工具线程里跑（与 web_tools T2 同前提）；
复用系统 Chrome（channel="chrome"），无需 playwright install。
"""
from pathlib import Path
from typing import Tuple

from ..agents import chart_tools
from . import deck_ops

DEFAULT_TIMEOUT = 40  # 秒：加载 + 图片就绪 + 打印的总预算，须 < agent.tool_timeout_seconds


def export_pdf(session_id: str, session_dir: Path, cfg: dict) -> Tuple[bool, str]:
    """导出会话 deck 为 PDF 存入 assets/。返回 (ok, 文件名或错误消息)。"""
    deck = deck_ops.load_deck(session_dir)
    if deck is None:
        return False, "No deck in this session yet — build one with deck_init / deck_set_slide first."
    if not deck["slides"]:
        return False, "The deck has no slides yet — add slides before exporting."

    base = str(cfg.get("frontend_base_url", "http://localhost:5173")).rstrip("/")
    timeout_s = int(cfg.get("export_timeout", DEFAULT_TIMEOUT))
    url = f"{base}/print/deck?session={session_id}"
    fname = chart_tools.asset_filename(deck["meta"].get("title", ""), kind="deck", ext="pdf")
    out = session_dir / "assets" / fname
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return False, f"PDF export unavailable: playwright missing ({e})."

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
                # 等前端渲染完成信标（或错误信标），再等所有图表 PNG 真正加载完
                page.wait_for_selector("#deck-print-ready, #deck-print-error",
                                       state="attached", timeout=timeout_s * 1000)
                if page.query_selector("#deck-print-error") is not None:
                    err = page.inner_text("#deck-print-error")
                    return False, f"deck print page reported: {err}"
                page.wait_for_function(
                    "Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)",
                    timeout=timeout_s * 1000)
                page.pdf(path=str(out), prefer_css_page_size=True, print_background=True)
            finally:
                browser.close()
    except Exception as e:
        return False, (f"PDF export failed: {e}. The frontend at {base} must be running "
                       "(the print route renders the slides for printing).")

    if not out.exists() or out.stat().st_size < 1000:
        return False, "PDF export produced an empty file."
    return True, fname
