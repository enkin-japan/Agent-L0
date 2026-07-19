"""主体可见的 Excel 工程工具 excel_research：写码-验证-修复沙箱的包壳。

与 web_research / math_research 同格局：主 Agent 只调这一个工具、只拿回一份结果
摘要；沙箱循环、seatbelt REPL、验证链全封在 framework/{agents,excel}，主
MCPRegistry 不扫描内部件。

为什么不是一组 write_cell 工具：代码执行的表达力免费（循环/条件/异常处理模型直接
写），而门禁才是价值所在——SpreadsheetBench 上 SOTA+ReAct 代码执行 pass rate 42%，
失败主体是"错了但模型自称成功"。本沙箱的结构性解法（对齐 math 的 submit_answer
思想）：交付必须过 verify_workbook（LibreOffice 影子重算 + 契约断言 + 改动半径
审计），verdict 由 harness 从返回对象读取、GO 时哈希封印交付文件——模型既伪造
不了判决，也没法验证后偷改。

执行流同 web_research：本 execute 跑在 orchestrator 工具线程，经
run_coroutine_threadsafe 把沙箱协程投回主事件循环；主轮次此刻正 await 在本线程
上、已释放推理锁，沙箱取锁不与自身争抢，单 worker 不死锁。

安全边界（免审批依据）：seatbelt 禁网、文件写只放行 sessions/{id}/excel/work 子树；
用户原件只被复制、永不原地修改；产物经 assets 白名单路由出货。
"""
import asyncio
import concurrent.futures
from pathlib import Path

from ..base import BaseTool

# 配置统一在仓库根 config.yaml（excel_research 段），寻址走 config_loader
from ...utils.config_loader import load_section
_CFG = load_section("excel_research")


class ExcelResearchTool(BaseTool):
    name = "excel_research"
    description = (
        "Delegate a complete spreadsheet-engineering task to a sandboxed sub-module that edits or "
        "creates .xlsx/.xlsm workbooks with Python, then passes a hard delivery gate (real "
        "recalculation of every formula, contract checks, change-radius audit, visual check) "
        "before anything is returned. Use it whenever the user wants a workbook CREATED, EDITED, "
        "FIXED, or ANALYZED with computation — e.g. add formulas/summaries/pivots, clean data, "
        "build a financial model, repair broken formulas. Do NOT use it for merely reading values "
        "out of a spreadsheet (use file_extract or the ingested summary instead). Pass a complete, "
        "self-contained task description — include every sheet name, column, number and rule the "
        "user stated, verbatim; the sub-module cannot see the conversation. For files the user "
        "uploaded, pass file_path from the context provenance marker ([原文件：/abs/path]). "
        "The digest reports GO (with a download link you must relay to the user) or an honest "
        "failure/blocker — never present a non-GO result as success."
    )
    requires_approval = False   # 禁网沙箱 + 写域仅会话工作区 + 原件只读复制，无外发副作用
    # 有状态（会话工作区/持久 REPL），保守串行；沙箱内部取推理锁，与主生成互斥、不死锁
    is_concurrency_safe = False
    # 长任务：多轮生成 + 每轮 verify 一次 LibreOffice 重算，全局 180s 必超；
    # 覆盖值须 > excel_research.total_timeout（本工具自身的硬熔断先触发）
    timeout_seconds = int(_CFG.get("total_timeout", 540)) + 30

    parameters = {
        "properties": {
            "task": {
                "type": "string",
                "description": "The complete, self-contained spreadsheet task in natural language. "
                               "Include every requirement verbatim (sheet/column names, formulas, "
                               "target numbers, what must stay untouched); the sub-module has no "
                               "other memory.",
            },
            "file_path": {
                "type": "string",
                "description": "Absolute path of the input workbook (from the context provenance "
                               "marker). Omit when creating a workbook from scratch.",
            },
        },
        "required": ["task"],
    }

    def is_available(self) -> bool:
        """openpyxl / seatbelt / LibreOffice 任一缺失则不注册（门禁跑不起来就没有本工具）。"""
        try:
            import openpyxl  # noqa: F401
        except Exception as e:
            print(f"[MCP-excel_research] ⚠️ openpyxl 不可用（{e}），工具不注册。")
            return False
        from ...agents import excel_tools
        if not excel_tools.seatbelt_available():
            print("[MCP-excel_research] ⚠️ sandbox-exec 探活失败，工具不注册。")
            return False
        from ...excel import find_soffice, find_pdftoppm
        if not find_soffice():
            print("[MCP-excel_research] ⚠️ LibreOffice (soffice) 未安装，工具不注册。"
                  "安装：brew install --cask libreoffice")
            return False
        if not find_pdftoppm():
            # 渲染是增强层：缺 poppler 只降级视觉回读，不禁用工具
            print("[MCP-excel_research] ℹ️ pdftoppm 缺失（brew install poppler），"
                  "视觉回读降级，其余门禁不受影响。")
        return True

    def execute(self, task: str, file_path: str = None) -> str:
        from ...agents import runtime
        from ...agents.excel_sandbox import run_excel_task

        if not runtime.is_ready():
            return "System Error: excel-research runtime not initialized (engine/event loop missing)."

        session = runtime.get_current_session()
        if session is None:
            return ("System Error: no active session context; excel_research must run inside "
                    "a chat turn.")

        if file_path:
            # expanduser 与 file_nav/file_extract 同纪律：路径处理全工具面均匀
            p = Path(file_path).expanduser()
            if not p.is_file():
                return (f"System Error: input file not found: {file_path}. Pass the absolute "
                        "path from the provenance marker, or omit file_path to create a new "
                        "workbook.")
            if p.suffix.lower() not in (".xlsx", ".xlsm", ".xltx", ".csv", ".tsv"):
                return (f"System Error: unsupported input type {p.suffix!r} — the sub-module "
                        "handles .xlsx/.xlsm (and .csv/.tsv as raw data).")

        engine = runtime.get_engine()
        loop = runtime.get_loop()
        total_timeout = int(_CFG.get("total_timeout", 540))

        # 进度钩子必须在本工具线程取（ContextVar 随 to_thread 拷贝到此为止）：
        # run_coroutine_threadsafe 投回主循环的协程不继承本线程上下文，须作参数显式传入。
        progress_cb = runtime.get_progress_hook()

        try:
            fut = asyncio.run_coroutine_threadsafe(
                run_excel_task(engine, task, file_path or None,
                               Path(session.session_dir), session.session_id,
                               _CFG, progress=progress_cb),
                loop)
        except Exception as e:
            return f"System Error: could not schedule the excel-research sandbox: {e}"

        try:
            digest = fut.result(timeout=total_timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            digest = ("[excel_research: timed out before completion — nothing was verified "
                      "or delivered]")
        except Exception as e:
            return f"System Error: excel research raised an exception: {e}"

        return (
            "The following is the result from the excel-research sub-module. Its VERDICT line is "
            "authoritative: only a GO result was actually verified (formulas recalculated by a "
            "real engine). Relay the download link exactly as given; if it reports BLOCKED or "
            "NOT DELIVERED, tell the user honestly what is missing — never claim success.\n"
            f"<excel_research task=\"{task[:200]}\">\n{digest}\n</excel_research>"
        )
