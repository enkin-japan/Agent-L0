"""write_document：markdown → .docx 可编辑交付（OfficeCLI 集成第 1 站）。

模型面只见 markdown（它本来就写得好），版式由 framework/office/md_docx 确定性
展开——deck 槽位同哲学，小模型不摆坐标。交付门禁在转换层：officecli batch 原子
（任一条失败整文件回滚）→ OpenXML validate → 摄入侧 markitdown 独立读回交叉
校验（写读两套引擎，防同引擎自证）。任一门禁失败即删产物、如实报错。

产物走 assets 白名单出货（与 excel/chart 同路）：sessions/{id}/assets/，
链接 /api/v1/files/{session_id}/assets/{fname}。本工具只写会话目录、不出网，
免审批。二进制缺失 → is_available=False 不注册，装上后 rescan 热补。
"""
from pathlib import Path

from ..base import BaseTool
from ...agents import runtime


class WriteDocumentTool(BaseTool):
    name = "write_document"
    description = (
        "Create a polished, editable Word document (.docx) from Markdown and give the user a "
        "download link. Use when the user wants a document/report/letter/summary as a FILE "
        "(deliverable), not just chat text. Write the full document content in Markdown: "
        "# headings (levels 1-4), **bold**, *italic*, `code`, [links](url), bullet lists "
        "(2-space indent nests), numbered lists, GFM pipe tables, ``` code blocks, > quotes, "
        "and images by LOCAL absolute path on their own line: ![caption](/abs/path.png) — "
        "e.g. a chart you just rendered. Do NOT use for spreadsheets (use excel_research) "
        "or slide decks (use deck tools)."
    )
    requires_approval = False
    is_concurrency_safe = False  # 写会话资产目录
    parameters = {
        "properties": {
            "file_name": {
                "type": "string",
                "description": "Short descriptive name for the file (no path, no extension), "
                               "e.g. 'q4-sales-report'.",
            },
            "markdown": {
                "type": "string",
                "description": "The COMPLETE document content in Markdown. This is the whole "
                               "document — include all sections, tables and text.",
            },
        },
        "required": ["file_name", "markdown"],
    }

    def is_available(self) -> bool:
        from ...office.officecli import find_officecli, probe_version
        if find_officecli() is None:
            print("[MCP-write_document] ℹ️ officecli 未安装（brew install officecli），"
                  "工具不注册；装上后热重扫自动补上。")
            return False
        print(f"[MCP-write_document] officecli {probe_version() or '?'} 就绪")
        return True

    def execute(self, file_name: str, markdown: str) -> str:
        from ...agents import chart_tools
        from ...office.md_docx import write_docx
        from ...office.officecli import OfficeCliError

        session = runtime.get_current_session()
        if session is None:
            return ("System Error: no active session context; write_document must run "
                    "inside a chat turn.")
        if not (markdown or "").strip():
            return "System Error: 'markdown' is empty — pass the full document content."

        fname = chart_tools.asset_filename(file_name, kind="doc", ext="docx")
        assets = Path(session.session_dir) / "assets"
        out = assets / fname
        try:
            # 图片相对路径的根 = 会话资产目录（render_chart 产物就在这里）
            stats = write_docx(markdown, out, image_root=assets)
        except OfficeCliError as e:
            detail = f" Detail: {e.detail}" if getattr(e, "detail", None) else ""
            return (f"System Error: document generation failed and nothing was delivered "
                    f"(the build is atomic). Reason: {e}.{detail} Fix the Markdown "
                    f"(e.g. malformed table) and try again.")
        except Exception as e:
            return f"System Error: document generation crashed: {e}"

        url = f"/api/v1/files/{session.session_id}/assets/{fname}"
        warn = ""
        if stats["warnings"]:
            warn = "\nWarnings (tell the user honestly): " + "; ".join(stats["warnings"])
        return (
            f"Document created and verified (OpenXML valid, {stats['coverage']:.0%} of "
            f"sampled text confirmed by independent read-back): {stats['paragraphs']} "
            f"paragraphs, {stats['tables']} table(s).\n"
            f"Download link (relay to the user as Markdown): [{file_name}.docx]({url})"
            f"{warn}"
        )
