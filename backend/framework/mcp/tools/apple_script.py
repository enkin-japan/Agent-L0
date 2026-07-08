"""
AppleScript / JXA 执行工具：终端与 GUI 自动化之间的应用级中间层。

许多「看起来像 GUI」的任务（读写邮件、建日历事件、控制 Safari 标签页、操作 Finder、
播放音乐等）在 macOS 上有官方脚本接口，走 osascript 是结构化调用，
远比 Accessibility 点击可靠。工具优先级：能 shell 就 shell，其次 osascript，
Accessibility 仅作最后兜底。

审批策略：恒需审批。AppleScript 能做的事等价于任意应用级操作（含删除、发送、
系统设置更改），无法像 shell 那样按内容静态判定只读，因此一律弹窗由用户确认。
"""
import shutil
import subprocess
import sys
from typing import Any, Dict

from ..base import BaseTool


def _osascript_available() -> bool:
    return sys.platform == "darwin" and shutil.which("osascript") is not None


class AppleScriptTool(BaseTool):
    """执行 AppleScript / JXA 脚本，驱动 macOS 应用（应用级操作，恒需用户审批）"""
    name = "run_applescript"
    description = (
        "Run an AppleScript (or JavaScript for Automation) script on macOS to control native "
        "apps — Mail, Calendar, Reminders, Notes, Finder, Safari, Music, System Events, etc. "
        "Prefer this over GUI clicking whenever the target app has a scripting interface, but "
        "prefer plain shell commands over this when a task can be done in the terminal. "
        "Every run triggers a user confirmation dialog before executing (AppleScript can perform "
        "any app-level action), so call the tool directly — do NOT ask the user for permission in "
        "words. The response returns the script's output or the error."
    )
    requires_approval = True  # 恒需审批（应用级操作不可静态判定只读）
    parameters = {
        "properties": {
            "script": {
                "type": "string",
                "description": "The AppleScript (or JXA) source to execute."
            },
            "language": {
                "type": "string",
                "enum": ["applescript", "javascript"],
                "description": "Script language. Defaults to 'applescript'."
            },
            "timeout": {
                "type": "integer",
                "description": "Optional timeout in seconds (default 60, max 600)."
            }
        },
        "required": ["script"]
    }

    def is_available(self) -> bool:
        return _osascript_available()

    def execute(self, script: str, language: str = "applescript", timeout: int = 60) -> str:
        if not script or not script.strip():
            return "System Error: empty script."

        try:
            timeout = max(1, min(int(timeout), 600))
        except (TypeError, ValueError):
            timeout = 60

        # -l JavaScript 切 JXA；脚本经 stdin 传入，避免命令行转义与长度限制
        cmd = ["osascript"]
        if language == "javascript":
            cmd += ["-l", "JavaScript"]
        cmd += ["-"]

        try:
            proc = subprocess.run(
                cmd,
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"System Error: script timed out after {timeout}s."
        except Exception as e:
            return f"System Error: failed to run osascript: {str(e)}"

        stdout = (proc.stdout or "").rstrip()
        stderr = (proc.stderr or "").rstrip()
        if proc.returncode != 0:
            # osascript 权限不足（未授予自动化/辅助功能）的典型报错也在 stderr 里，原文返回助模型判断
            return f"Script failed (exit {proc.returncode}):\n{stderr or '(no error message)'}"
        return stdout if stdout else "(script ran successfully with no output)"
