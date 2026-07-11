"""Excel 工程能力层：感知 / 重算 / 验证 / 审计 / 渲染 / 门禁。

收编自 excel_skill 方案（仓库根 excel_skill/ 为上游参考件），适配本项目：
 · 全部模块化为可导入函数（excel_research 沙箱 harness 直调，verdict 不走
   stdout 解析——门禁判定必须来自返回对象，模型无法伪造）；CLI main 保留，
   供手工冒烟与门禁召回测试。
 · macOS 原生 LibreOffice headless（brew cask，无 Docker），soffice/pdftoppm
   带绝对路径回退（app 进程 PATH 可能不含 /opt/homebrew/bin）。
 · 核心取舍沿用上游：影子重算（交付件格式零污染，验证件过 LibreOffice 取
   真实值）；R1C1 公式折叠感知（整表 dump 是 token 泄漏点 + 幻觉温床）。
"""
from .wb_inspect import inspect_workbook, summarize_for_ingestion
from .recalc import recalc_workbook, external_links, find_soffice
from .validate import validate_workbook
from .wb_diff import diff_workbooks
from .render import render_workbook, find_pdftoppm
from .verify import verify_workbook

__all__ = [
    "inspect_workbook", "summarize_for_ingestion",
    "recalc_workbook", "external_links", "find_soffice",
    "validate_workbook", "diff_workbooks",
    "render_workbook", "find_pdftoppm",
    "verify_workbook",
]
