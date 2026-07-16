"""文件整理三件套（D2 第二批）：move（含改名）/copy/trash，恒审批。

move/copy 纯 Python（shutil），trash 走 Finder——进的是废纸篓不是 rm，用户可恢复
（这也是不提供硬删除工具的原因：可逆优先，真要 rm 有 run_shell_command 的审批路径）。
纪律：**绝不静默覆盖**——目标已存在时拒绝并让模型换名/先挪走；目标是已存在目录时
语义为「挪入/拷入该目录」（Finder 直觉）。审批弹窗里用户看到的就是 source/destination
原文，路径都先 expanduser 展开再执行。
"""
import shutil
import subprocess
from pathlib import Path

from ..base import BaseTool
from .desktop import _darwin_with


def _resolve_dest(src: Path, destination: str) -> "tuple[Path, str | None]":
    """目标语义：已存在目录 = 挪入其中；已存在文件 = 拒绝覆盖；其余 = 目标全名。"""
    dst = Path(str(destination)).expanduser()
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        return dst, (f"destination already exists: {dst} — refusing to overwrite. "
                     "Pick another name, or trash/move the existing file first.")
    if not dst.parent.is_dir():
        return dst, f"destination folder does not exist: {dst.parent}"
    return dst, None


class MoveFileTool(BaseTool):
    name = "move_file"
    description = (
        "Move or rename a file/folder. If 'destination' is an existing folder the item moves "
        "into it keeping its name; otherwise 'destination' is the new full path (= rename). "
        "Never overwrites existing files."
    )
    requires_approval = True  # 改变用户文件布局
    is_concurrency_safe = False
    is_read_only = False

    parameters = {
        "properties": {
            "source": {"type": "string", "description": "Existing file or folder path."},
            "destination": {"type": "string", "description": "Target folder, or new full path to rename to."},
        },
        "required": ["source", "destination"],
    }

    def validate_arguments(self, arguments) -> "str | None":
        src = Path(str(arguments.get("source", ""))).expanduser()
        if not str(arguments.get("source", "")).strip() or not src.exists():
            return f"Invalid arguments: source not found: {arguments.get('source')!r}."
        _, err = _resolve_dest(src, str(arguments.get("destination", "")))
        return f"Invalid arguments: {err}" if err else None

    def execute(self, source: str, destination: str) -> str:
        src = Path(str(source)).expanduser()
        if not src.exists():
            return f"System Error: source not found: {source}"
        dst, err = _resolve_dest(src, destination)
        if err:
            return f"System Error: {err}"
        shutil.move(str(src), str(dst))
        return f"Moved {src} → {dst}"


class CopyFileTool(BaseTool):
    name = "copy_file"
    description = (
        "Copy a file/folder. If 'destination' is an existing folder the copy lands inside it "
        "keeping its name; otherwise 'destination' is the copy's full path. "
        "Never overwrites existing files."
    )
    requires_approval = True  # 写盘
    is_concurrency_safe = False
    is_read_only = False

    parameters = {
        "properties": {
            "source": {"type": "string", "description": "Existing file or folder path."},
            "destination": {"type": "string", "description": "Target folder, or the copy's full path."},
        },
        "required": ["source", "destination"],
    }

    validate_arguments = MoveFileTool.validate_arguments

    def execute(self, source: str, destination: str) -> str:
        src = Path(str(source)).expanduser()
        if not src.exists():
            return f"System Error: source not found: {source}"
        dst, err = _resolve_dest(src, destination)
        if err:
            return f"System Error: {err}"
        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))
        return f"Copied {src} → {dst}"


class TrashFileTool(BaseTool):
    name = "trash_file"
    description = (
        "Move a file/folder to the macOS Trash (recoverable — this is NOT permanent deletion). "
        "Use this whenever the user asks to delete something."
    )
    requires_approval = True  # 移出用户可见位置
    is_concurrency_safe = False
    is_read_only = False
    timeout_seconds = 45

    parameters = {
        "properties": {
            "path": {"type": "string", "description": "File or folder to move to the Trash."},
        },
        "required": ["path"],
    }

    def is_available(self) -> bool:
        return _darwin_with("osascript")

    def validate_arguments(self, arguments) -> "str | None":
        p = Path(str(arguments.get("path", ""))).expanduser()
        if not str(arguments.get("path", "")).strip() or not p.exists():
            return f"Invalid arguments: path not found: {arguments.get('path')!r}."
        return None

    def execute(self, path: str) -> str:
        p = Path(str(path)).expanduser()
        if not p.exists():
            return f"System Error: path not found: {path}"
        # Finder 的 delete = 进废纸篓；路径经 argv 交付（无 AppleScript 注入面）
        script = ("on run argv\n"
                  "tell application \"Finder\" to delete (POSIX file (item 1 of argv) as alias)\n"
                  "end run")
        out = subprocess.run(["osascript", "-e", script, str(p)],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return (f"System Error: Finder refused: {out.stderr.strip() or out.returncode}. "
                    "If this is error -1743 the user denied automation access — System Settings "
                    "→ Privacy & Security → Automation → MacAgent → Finder.")
        return f"Moved to Trash (recoverable): {p}"
