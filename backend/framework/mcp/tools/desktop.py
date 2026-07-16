"""桌面日常五件套（D2 第一批）：打开/剪贴板双向/通知/系统状态。

全部是 macOS 原生命令的薄包装（open/pbpaste/pbcopy/osascript/pmset），零第三方依赖。
探活统一要求 darwin + 对应命令在 PATH（bundle 内 PATH 被 SidecarManager 收窄过，
/usr/bin 恒在）。参数走 argv 传递而非字符串拼接——osascript 的 message 经 `on run argv`
交付，物理上不存在 AppleScript 注入面。

审批面：
- open_path 动态审批（shell_exec 同路数的白名单放行）：http(s) 网址、文件夹、常见
  文档类型自动放行；可执行物（.app/.sh/.command/带执行位）、安装包、未知扩展一律弹窗
  ——`open` 对这些等价于「运行」，不是「查看」。
- clipboard_write 覆盖用户剪贴板、notify_user 弹一条通知：副作用轻且完全可见，免审批。
- clipboard_read 只读但涉隐私：description 里约束「用户提及剪贴板内容时才调」。
"""
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from ..base import BaseTool

_CLIP_READ_CAP = 8000  # 剪贴板读取给模型的字符上限（超出如实截断并声明）

# open_path 自动放行的「查看类」文档扩展：默认应用只会展示不会执行。
# 刻意不含：.zip/.dmg/.pkg（打开=解压/挂载/安装，写盘副作用）、.app/.command/.sh/
# .scpt/.workflow（打开=运行）、其余未知扩展（fail-safe 走审批）。
_VIEW_SAFE_EXTS = {
    ".pdf", ".txt", ".md", ".rtf", ".log", ".json", ".csv", ".html", ".htm",
    ".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".svg", ".tiff", ".bmp",
    ".mp4", ".mov", ".m4v", ".mp3", ".m4a", ".wav", ".aiff", ".flac",
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".key", ".pages", ".numbers",
    ".epub",
}


def _darwin_with(*binaries: str) -> bool:
    return sys.platform == "darwin" and all(shutil.which(b) for b in binaries)


def _is_view_safe(target: str) -> bool:
    """静态判定「打开它只是查看」；任何不确定都返回 False（→ 需审批）。"""
    t = str(target or "").strip()
    if not t:
        return False
    scheme = urlparse(t).scheme.lower()
    if scheme in {"http", "https"}:
        return True
    if scheme and scheme != "file":
        return False  # javascript:/其他 scheme：交给审批（execute 侧还会再拒）
    p = Path(t).expanduser()
    if not p.exists():
        return False
    if p.is_dir():
        return p.suffix.lower() != ".app"  # .app 是目录形态的可执行物
    try:
        import os
        if os.access(p, os.X_OK):
            return False  # 带执行位的文件：打开可能等于运行
    except OSError:
        return False
    return p.suffix.lower() in _VIEW_SAFE_EXTS


class OpenPathTool(BaseTool):
    name = "open_path"
    description = (
        "Open a file, folder, or web URL on the user's Mac in its default application "
        "(Finder / browser / Preview…). Use after locating the target with find_files/list_dir. "
        "Documents, folders and http(s) URLs open directly; executables/installers need approval."
    )
    requires_approval = False  # 动态：_is_view_safe 之外的一切弹窗
    is_concurrency_safe = False
    is_read_only = False

    parameters = {
        "properties": {
            "target": {"type": "string",
                       "description": "Absolute/~ file or folder path, or an http(s) URL."},
        },
        "required": ["target"],
    }

    def is_available(self) -> bool:
        return _darwin_with("open")

    def needs_approval(self, arguments) -> bool:
        return not _is_view_safe(str(arguments.get("target", "")))

    def validate_arguments(self, arguments) -> "str | None":
        t = str(arguments.get("target", "")).strip()
        if not t:
            return "Invalid arguments: 'target' must be a non-empty path or URL."
        scheme = urlparse(t).scheme.lower()
        if scheme in {"http", "https"}:
            return None
        if scheme and scheme != "file":
            return f"Invalid arguments: only http(s) URLs or local paths are allowed, got scheme {scheme!r}."
        if not Path(t).expanduser().exists():
            return f"Invalid arguments: path not found: {t} (locate it first with find_files/list_dir)."
        return None

    def execute(self, target: str) -> str:
        err = self.validate_arguments({"target": target})
        if err:
            return f"System Error: {err}"
        t = str(target).strip()
        if not urlparse(t).scheme:
            t = str(Path(t).expanduser())
        out = subprocess.run(["open", t], capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return f"System Error: open failed: {out.stderr.strip() or out.returncode}"
        return f"Opened {t} in its default application."


class ClipboardReadTool(BaseTool):
    name = "clipboard_read"
    description = (
        "Read the current TEXT content of the user's clipboard. Call this only when the user "
        "refers to something they copied ('summarize what I just copied'); never poll it."
    )
    requires_approval = False
    is_concurrency_safe = True  # 只读

    parameters = {"properties": {}, "required": []}

    def is_available(self) -> bool:
        return _darwin_with("pbpaste")

    def execute(self) -> str:
        out = subprocess.run(["pbpaste"], capture_output=True, timeout=10)
        text = out.stdout.decode("utf-8", errors="replace").strip()
        if not text:
            return "The clipboard is empty or holds non-text content (e.g. an image/file)."
        if len(text) > _CLIP_READ_CAP:
            return (f"Clipboard text ({len(text)} chars, first {_CLIP_READ_CAP} shown):\n"
                    + text[:_CLIP_READ_CAP])
        return f"Clipboard text ({len(text)} chars):\n{text}"


class ClipboardWriteTool(BaseTool):
    name = "clipboard_write"
    description = (
        "Put text onto the user's clipboard (replaces its current content) so they can paste it "
        "anywhere. Use when the user asks to copy something for them."
    )
    requires_approval = False  # 副作用轻且用户立即可见
    is_concurrency_safe = False
    is_read_only = False

    parameters = {
        "properties": {
            "text": {"type": "string", "description": "The exact text to place on the clipboard."},
        },
        "required": ["text"],
    }

    def is_available(self) -> bool:
        return _darwin_with("pbcopy")

    def execute(self, text: str) -> str:
        data = str(text).encode("utf-8")
        subprocess.run(["pbcopy"], input=data, timeout=10, check=True)
        return f"Copied {len(str(text))} characters to the clipboard."


class NotifyUserTool(BaseTool):
    name = "notify_user"
    description = (
        "Show a macOS notification banner to the user. Use it to announce that a long background "
        "or a scheduled job has finished — not as a substitute for answering in chat."
    )
    requires_approval = False
    is_concurrency_safe = False
    is_read_only = False

    parameters = {
        "properties": {
            "message": {"type": "string", "description": "Notification body text (keep it one line)."},
            "title": {"type": "string", "description": "Optional title. Default 'Agent'."},
        },
        "required": ["message"],
    }

    def is_available(self) -> bool:
        return _darwin_with("osascript")

    def execute(self, message: str, title: str = "Agent") -> str:
        # message/title 经 argv 交付（on run argv），不进脚本字面量——无注入面
        script = ("on run argv\n"
                  "display notification (item 1 of argv) with title (item 2 of argv)\n"
                  "end run")
        out = subprocess.run(["osascript", "-e", script, str(message), str(title)],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return f"System Error: notification failed: {out.stderr.strip() or out.returncode}"
        return "Notification shown."


class SystemStatusTool(BaseTool):
    name = "system_status"
    description = (
        "Report the Mac's current battery level/charging state, free disk space, memory size "
        "and uptime. Use for questions like 'how much battery/disk do I have left'."
    )
    requires_approval = False
    is_concurrency_safe = True  # 只读

    parameters = {"properties": {}, "required": []}

    def is_available(self) -> bool:
        return _darwin_with("pmset", "sysctl")

    def execute(self) -> str:
        lines = []
        try:
            batt = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                                  text=True, timeout=10).stdout.strip()
            lines.append("Battery: " + (" ".join(batt.split("\n")[-1].split()) if batt else "n/a"))
        except Exception as e:
            lines.append(f"Battery: unavailable ({e})")
        try:
            du = shutil.disk_usage("/")
            lines.append(f"Disk (/): {du.free / 1e9:.1f} GB free of {du.total / 1e9:.1f} GB "
                         f"({du.free / du.total * 100:.0f}% free)")
        except Exception as e:
            lines.append(f"Disk: unavailable ({e})")
        try:
            mem = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=10).stdout.strip()
            lines.append(f"Memory: {int(mem) / (1024 ** 3):.0f} GB installed")
        except Exception as e:
            lines.append(f"Memory: unavailable ({e})")
        try:
            up = subprocess.run(["uptime"], capture_output=True, text=True, timeout=10).stdout.strip()
            if up:
                lines.append(f"Uptime: {up}")
        except Exception:
            pass
        return "\n".join(lines)
