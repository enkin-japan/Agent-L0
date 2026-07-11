#!/usr/bin/env python3
"""交付门禁总入口（The Gate）：文件交出去之前必须且只需过这一道。
串起整条验证链，返回 GO / NO-GO 判决 + 结构化失败原因（直接回灌给模型去修）。

核心设计——影子重算（shadow recalc）：
  LibreOffice 的往返改写不是格式中立的（实测会把 number_format 从 `yyyy-mm-dd`
  改写成 `yyyy\\-mm-dd`、把未指定字体实体化成 Cambria）。就地重算 = 拿一次全表
  格式污染换一次公式验证，对"编辑用户已有文件"主场景不可接受。所以：
    交付件 = openpyxl 原件 + fullCalcOnLoad（Excel 打开自动重算，格式零污染）
    验证件 = 影子副本过 LibreOffice，只用来取真实值做断言
  代价：交付件无缓存值，下游程序化读取（pandas/data_only）需自行重算；给人看不受影响。

对上游 excel_skill 的两处硬化：
 1. 外链检查提到 fullCalcOnLoad 补丁【之前】——补丁本身是一次 openpyxl 保存，
    会剥掉外链缓存值；上游先补丁后检查，门禁自己就把它警告的破坏做了一遍。
 2. 报告含交付件 sha256（补丁后计算）：沙箱门禁在 GO 时记下哈希、finish 时复验，
    "验证通过后又偷偷改一版"在任何代码路径下都会被抓，不再依赖字符串嗅探写操作。

verdict 从本函数返回值读取（excel_research 沙箱 harness 直调），不经 stdout：
模型伪造不了判决。链路：
  外链检查 → fullCalcOnLoad → shadow copy → recalc(shadow)
  → L1/L2/L3 validate(shadow) → diff(before, target) → render(shadow)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

from openpyxl import load_workbook

from .recalc import external_links, recalc_workbook
from .render import render_workbook
from .validate import validate_workbook
from .wb_diff import diff_workbooks


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_full_calc_on_load(path: Path) -> bool:
    """交付件必须带 fullCalcOnLoad，否则用户在 Excel 里看到的是一片空白公式。"""
    keep_vba = path.suffix.lower() == ".xlsm"
    wb = load_workbook(path, data_only=False, keep_vba=keep_vba)
    if wb.calculation.fullCalcOnLoad:
        return False
    wb.calculation.fullCalcOnLoad = True
    wb.save(path)
    return True


def verify_workbook(path, *, contract: Optional[dict] = None,
                    before: Optional[str] = None,
                    max_changed: Optional[int] = None,
                    render: bool = False,
                    preview_dir: Optional[str] = None,
                    recalc_timeout: int = 120,
                    render_dpi: int = 100,
                    render_timeout: int = 120,
                    report_path: Optional[str] = None) -> dict:
    """完整门禁。返回 report dict：verdict=GO|NO-GO、blockers、stages、sha256。

    - contract:   L2/L3 断言（None = 只跑 L1）
    - before:     改动前原件路径 → blast radius 审计
    - max_changed: 改动单元格上限，超了即 NO-GO
    - render:     额外渲染 PNG 到 preview_dir（供视觉回读；失败仅记备注，不阻塞）
    - report_path: 顺带把报告写成 JSON 文件（CLI/排障用；harness 用返回值）
    """
    target = Path(path)
    report: dict = {"file": str(target), "stages": {}}
    blockers: list = []

    def _finish() -> dict:
        report["verdict"] = "NO-GO" if blockers else "GO"
        report["blockers"] = blockers
        if blockers:
            report["next_step"] = (
                "读 stages 里对应失败项的 failures/error 定位到具体单元格并修复，"
                "然后重新 verify。不要用硬编码值去掩盖公式错误。")
        if target.exists():
            report["sha256"] = _sha256(target)  # 门禁哈希：GO 后文件再被改动即失效
        if report_path:
            try:
                Path(report_path).write_text(
                    json.dumps(report, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
            except Exception:
                pass  # 报告落盘失败不影响判决
        return report

    if not target.exists():
        blockers.append("file_not_found")
        report["error"] = f"file not found: {target}"
        return _finish()
    if target.suffix.lower() not in (".xlsx", ".xlsm"):
        blockers.append("unsupported_format")
        report["error"] = f"unsupported extension: {target.suffix} (only .xlsx/.xlsm)"
        return _finish()

    # 0. 外链红线（必须先于 fullCalcOnLoad 补丁——补丁即一次 openpyxl 保存）
    try:
        links = external_links(target)
    except Exception as e:
        blockers.append("workbook_unreadable")
        report["error"] = f"cannot open workbook: {type(e).__name__}: {e}"
        return _finish()
    if links:
        blockers.append("external_links")
        report["stages"]["external_links"] = {
            "links": links,
            "hint": ("外部链接的缓存值是其数据的唯一载体，任何 openpyxl 保存/重算都会"
                     "摧毁它。先用 data_only=True 读原件把这些单元格的值抄成字面量，"
                     "去掉外链引用，再重新 verify。"),
        }
        return _finish()

    # 1. 交付件自身：确保 Excel 打开时会重算
    report["stages"]["full_calc_on_load"] = {"patched": _ensure_full_calc_on_load(target)}

    with tempfile.TemporaryDirectory() as tmp:
        shadow = Path(tmp) / f"shadow{target.suffix}"
        shutil.copy2(target, shadow)

        # 2. 影子重算（拿真实缓存值）
        recalc_report = recalc_workbook(shadow, timeout=recalc_timeout)
        report["stages"]["recalc"] = recalc_report
        if "error" in recalc_report:
            blockers.append("recalc_failed")
            return _finish()

        # 3. L1/L2/L3 验证（跑在影子上——它才有真实缓存值）
        val = validate_workbook(shadow, contract)
        report["stages"]["validate"] = val
        if not val["passed"]:
            blockers.append("validation_failed")

        # 4. blast radius 审计（diff 跑在交付件上：审计的是将要交出去的字节）
        if before:
            try:
                d = diff_workbooks(before, target, cap=50)
                if max_changed is not None and d["total_changed"] > max_changed:
                    d["blast_radius_exceeded"] = max_changed
                    blockers.append("blast_radius_exceeded")
                report["stages"]["diff"] = d
            except Exception as e:
                report["stages"]["diff"] = {"error": f"{type(e).__name__}: {e}"}
                blockers.append("diff_failed")

        # 5. 视觉回读渲染（增强层：失败只记备注，不阻塞门禁）
        if render:
            out = Path(preview_dir) if preview_dir else target.parent / "preview"
            r = render_workbook(shadow, out, dpi=render_dpi, timeout=render_timeout)
            report["stages"]["render"] = r
            if "pages" in r:
                report["preview_pages"] = r["pages"]

    return _finish()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="待交付的 workbook")
    ap.add_argument("--contract", help="contract.json 路径")
    ap.add_argument("--before", help="改动前原件，用于 blast-radius 审计")
    ap.add_argument("--max-changed", type=int)
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--preview-dir")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--out", help="报告 JSON 落盘路径")
    args = ap.parse_args()

    contract = json.loads(Path(args.contract).read_text()) if args.contract else None
    report = verify_workbook(
        args.path, contract=contract, before=args.before, max_changed=args.max_changed,
        render=args.render, preview_dir=args.preview_dir,
        recalc_timeout=args.timeout, report_path=args.out)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["verdict"] == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
