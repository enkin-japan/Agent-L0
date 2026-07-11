#!/usr/bin/env python3
"""视觉回读层：workbook → LibreOffice → PDF → pdftoppm → PNG。

让多模态模型"看见"自己产出的表，而不是从 XML 里猜。能抓到一整类校验脚本
抓不到的问题：列宽把数字挤成 ####、表头错位、数字格式没生效、图表压住数据。

pdftoppm（poppler）缺失时优雅降级：返回 error，verify 链把它记为
非阻塞备注——视觉层是增强，不是门禁的硬依赖。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .recalc import find_soffice, _soffice_env

_PDFTOPPM_FALLBACKS = ("/opt/homebrew/bin/pdftoppm", "/usr/local/bin/pdftoppm")


def find_pdftoppm() -> Optional[str]:
    p = shutil.which("pdftoppm")
    if p:
        return p
    for cand in _PDFTOPPM_FALLBACKS:
        if Path(cand).is_file():
            return cand
    return None


def _to_pdf(src: Path, outdir: Path, timeout: int) -> Path:
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError("LibreOffice not found")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [soffice, "--headless", "--norestore", "--nolockcheck",
             f"-env:UserInstallation=file://{tmp}/profile",
             "--convert-to", "pdf:calc_pdf_Export",
             "--outdir", str(outdir), str(src.resolve())],
            capture_output=True, timeout=timeout, check=True, env=_soffice_env(tmp),
        )
    pdf = outdir / (src.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError("LibreOffice 没有产出 PDF")
    return pdf


def _to_png(pdf: Path, outdir: Path, dpi: int, pages: Optional[str], timeout: int) -> list:
    pdftoppm = find_pdftoppm()
    if not pdftoppm:
        raise RuntimeError("pdftoppm not found (brew install poppler)")
    cmd = [pdftoppm, "-png", "-r", str(dpi)]
    if pages:
        first, _, last = pages.partition("-")
        cmd += ["-f", first, "-l", last or first]
    cmd += [str(pdf), str(outdir / "page")]
    subprocess.run(cmd, capture_output=True, check=True, timeout=timeout)
    return sorted(str(p) for p in outdir.glob("page-*.png"))


def render_workbook(path, outdir, *, dpi: int = 100, pages: Optional[str] = None,
                    timeout: int = 120) -> dict:
    """渲染成 PNG。返回 {"pdf": ..., "pages": [...]} 或 {"error": ...}。
    outdir 内旧的 page-*.png 会先清掉，避免上一轮的页面残留混进本轮回读。"""
    src, out = Path(path), Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("page-*.png"):
        stale.unlink(missing_ok=True)
    try:
        pdf = _to_pdf(src, out, timeout)
        page_files = _to_png(pdf, out, dpi, pages, timeout)
        return {"pdf": str(pdf), "pages": page_files}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--outdir", default="./preview")
    ap.add_argument("--dpi", type=int, default=100)
    ap.add_argument("--pages", help="如 1 或 1-3")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    result = render_workbook(args.path, args.outdir, dpi=args.dpi,
                             pages=args.pages, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
