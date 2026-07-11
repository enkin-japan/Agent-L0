"""PDF 导出：无头 Chrome 打印 /print/deck 路由。

渲染器全程只有一份（前端 SlideCanvas）——PDF 是「预览即所得」的同一投影，
不在后端复刻第二个渲染器（两个渲染器必然漂移）。

打印页宿主两级（Phase 4b 解除后端→前端倒挂）：
1. 前端服务可达（dev: Vite 5173 / 生产: Nginx）→ 打它（开发期渲染器最新鲜）;
2. 否则打后端自托管的构建产物（backend/static/webapp,由
   `devctl.sh bundle-print` 生成）——后端单独跑也能导 PDF。
自托管页与后端同源,数据请求经 Playwright 上下文注入 Bearer 头过鉴权。

Playwright 同步 API 在 MCP 工具线程里跑（与 web_tools T2 同前提）；
复用系统 Chrome（channel="chrome"），无需 playwright install。
"""
import os
import socket
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from ..agents import chart_tools
from ..utils.config_loader import load_config
from . import deck_ops

DEFAULT_TIMEOUT = 40  # 秒：加载 + 图片就绪 + 打印的总预算，须 < agent.tool_timeout_seconds

# 自托管打印包位置(app.py 同一路径挂载静态路由)
SELF_HOSTED_BUNDLE = Path(__file__).resolve().parents[2] / "static" / "webapp"


def _reachable(base: str, timeout: float = 1.0) -> bool:
    """快速 TCP 探活(不发 HTTP):前端服务在不在。"""
    try:
        u = urlparse(base)
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


def _pick_print_host(cfg: dict) -> Tuple[Optional[str], str]:
    """选打印页宿主:前端可达优先(最新鲜),否则后端自托管包。返回 (base, 描述)。"""
    frontend = str(cfg.get("frontend_base_url", "http://localhost:5173")).rstrip("/")
    if _reachable(frontend):
        return frontend, "frontend"
    if (SELF_HOSTED_BUNDLE / "index.html").exists():
        conf = load_config().get("inference") or {}
        return (f"http://127.0.0.1:{conf.get('port', 14159)}", "self-hosted")
    return None, "none"


def export_pdf(session_id: str, session_dir: Path, cfg: dict) -> Tuple[bool, str]:
    """导出会话 deck 为 PDF 存入 assets/。返回 (ok, 文件名或错误消息)。"""
    deck = deck_ops.load_deck(session_dir)
    if deck is None:
        return False, "No deck in this session yet — build one with deck_init / deck_set_slide first."
    if not deck["slides"]:
        return False, "The deck has no slides yet — add slides before exporting."

    base, host_kind = _pick_print_host(cfg)
    if base is None:
        return False, ("PDF export unavailable: neither the frontend service nor the "
                       "self-hosted print bundle is present (run `./devctl.sh bundle-print`).")
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
                if host_kind == "self-hosted":
                    # 自托管页与后端同源:页面的 /api 数据请求经上下文注入 Bearer 过鉴权
                    # (前端宿主路径由代理注入,无需此步)
                    key = os.environ.get("AGENT_API_KEY") \
                        or (load_config().get("server") or {}).get("api_key") or ""
                    if key:
                        page.set_extra_http_headers({"Authorization": f"Bearer {key}"})
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
        return False, (f"PDF export failed: {e} (print host: {host_kind} @ {base}). "
                       "If neither the frontend nor the self-hosted bundle works, "
                       "run `./devctl.sh bundle-print` and retry.")

    if not out.exists() or out.stat().st_size < 1000:
        return False, "PDF export produced an empty file."
    return True, fname
