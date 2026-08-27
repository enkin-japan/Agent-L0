"""文件导航双件套（D2 第一批）：list_dir 目录浏览 + find_files Spotlight 找文件。

为什么不让模型走 run_shell_command ls/mdfind：D1 实测 e4b 拼 shell 命令时引号/转义
经常翻车（S.9 修复前后皆有案例），专用工具的扁平 schema 是小模型的人机学下限保障；
且 mdfind 的查询语法（-name vs 裸查询 vs -onlyin）值得一层确定性包装。

两个都是纯只读，无审批、可并发。输出都设条数上限——目录/命中可能上千条，
observation_max_chars 截断会剪掉尾部提示，工具级先裁并如实报告总量。
"""
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ..base import BaseTool

_LIST_CAP = 200   # list_dir 最多列出的条目数
_FIND_CAP = 30    # find_files 最多返回的命中数


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


class ListDirTool(BaseTool):
    name = "list_dir"
    description = (
        "List the contents of a folder on the user's Mac: sub-folders first, then files with "
        "size and modified date. Use this to browse/navigate the file system (e.g. see what is "
        "in Downloads) before reading or opening a specific file."
    )
    requires_approval = False
    is_concurrency_safe = True  # 纯只读

    parameters = {
        "properties": {
            "path": {"type": "string",
                     "description": "Folder path, absolute or starting with ~ (e.g. ~/Downloads)."},
            "show_hidden": {"type": "boolean",
                            "description": "Also list entries starting with a dot. Default false."},
        },
        "required": ["path"],
    }

    def execute(self, path: str, show_hidden: bool = False) -> str:
        p = Path(str(path)).expanduser()
        if not p.exists():
            return f"System Error: path not found: {p}"
        if not p.is_dir():
            return (f"System Error: {p} is a file, not a folder. "
                    "Use file_extract to read it or open_path to open it.")
        try:
            entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            return f"System Error: permission denied reading {p}"
        if not show_hidden:
            entries = [e for e in entries if not e.name.startswith(".")]
        total = len(entries)
        lines = [f"{p}  ({total} entries)"]
        for e in entries[:_LIST_CAP]:
            try:
                if e.is_dir():
                    lines.append(f"  [dir]  {e.name}/")
                else:
                    st = e.stat()
                    mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
                    lines.append(f"  {_human_size(st.st_size):>9}  {mtime}  {e.name}")
            except OSError:
                lines.append(f"  [?]    {e.name}")
        if total > _LIST_CAP:
            lines.append(f"  … {total - _LIST_CAP} more entries not shown "
                         "(narrow down or use find_files)")
        return "\n".join(lines) if total else f"{p}  (empty folder)"


class FindFilesTool(BaseTool):
    name = "find_files"
    description = (
        "Find files on the user's Mac via Spotlight: matches file names first and automatically "
        "falls back to searching file contents. Use this whenever the user asks to locate a file "
        "('where is that PDF from last week') instead of guessing paths."
        # D2-减法(类A):返回条数/排序句删——输出首行自报 "N matches (showing M, newest first)"。
    )
    requires_approval = False
    is_concurrency_safe = True  # 只读查询
    timeout_seconds = 30

    parameters = {
        "properties": {
            "query": {"type": "string",
                      "description": "File name fragment (default), or content keywords when search_content=true."},
            "search_content": {"type": "boolean",
                               "description": "true = match inside file contents/metadata too (slower). Default false = file names only."},
            "folder": {"type": "string",
                       "description": "Optional folder to search within (e.g. ~/Documents). Omit = whole Mac."},
        },
        "required": ["query"],
    }

    def is_available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("mdfind") is not None

    def validate_arguments(self, arguments) -> "str | None":
        if not str(arguments.get("query", "")).strip():
            return "Invalid arguments: 'query' must be a non-empty string."
        return None

    def execute(self, query: str, search_content: bool = False, folder: str = None) -> str:
        scope = []
        if folder:
            fp = Path(str(folder)).expanduser()
            if not fp.is_dir():
                return f"System Error: folder not found: {fp}"
            scope = ["-onlyin", str(fp)]
        q = query.strip()

        def _mdfind(args):
            return subprocess.run(["mdfind", *scope, *args],
                                  capture_output=True, text=True, timeout=25)

        # 名字模式 0 命中时自动降级内容模式（D2 首批验收实证：e4b 会漏传
        # search_content，「在文稿里找提到 X 的文件」走成文件名搜索空手而归。
        # 参数保留——显式 true 仍只搜内容；降级是确定性兜底，不指望模型重试补参）。
        matched_by = "name"
        try:
            out = _mdfind([q] if search_content else ["-name", q])
            paths = [ln for ln in out.stdout.splitlines() if ln.strip()]
            if not paths and not search_content:
                out = _mdfind([q])
                paths = [ln for ln in out.stdout.splitlines() if ln.strip()]
                matched_by = "content"
        except subprocess.TimeoutExpired:
            return "System Error: Spotlight search timed out (25s); try a narrower query or folder."
        if out.returncode != 0:
            return f"System Error: mdfind failed: {out.stderr.strip() or out.returncode}"
        if not paths:
            where = f" in {folder}" if folder else ""
            # 结果受进程 TCC 授权过滤（实证：外置卷命中被静默滤掉）——如实披露
            return (f"No files matched {query!r}{where} by file name or content. Note: results "
                    "are limited to folders this app may access — files on external volumes or "
                    "protected folders stay invisible unless the app is granted Full Disk Access "
                    "(System Settings → Privacy & Security). Try different keywords or list_dir "
                    "a likely folder.")

        def _mtime(s: str) -> float:
            try:
                return Path(s).stat().st_mtime
            except OSError:
                return 0.0

        paths.sort(key=_mtime, reverse=True)
        shown = paths[:_FIND_CAP]
        note = " — no file NAME matched; these match by file CONTENT" if matched_by == "content" else ""
        lines = [f"{len(paths)} matches (showing {len(shown)}, newest first{note}):"]
        for s in shown:
            try:
                st = Path(s).stat()
                mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
                lines.append(f"  {mtime}  {_human_size(st.st_size):>9}  {s}")
            except OSError:
                lines.append(f"  ?           ?  {s}")
        return "\n".join(lines)
