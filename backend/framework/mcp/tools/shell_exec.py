"""
终端命令执行工具：桌面 Agent 的主力工具（大多数任务优先走终端）。

审批策略（安全默认 = 白名单放行 + 其余弹窗，而非黑名单拦截）：
- 只有「首命令属于只读白名单 且 不含 shell 元字符」的调用自动放行（如 ls/cat/grep）；
- 其余一切（写文件、装卸软件、sudo、管道/重定向/子命令、任何不认识的命令）一律弹窗审批。
黑名单（禁危险命令）是不安全的：管道、别名、子 shell、环境变量拼接都能绕过；
白名单则是 fail-safe——没被明确判定为只读的，都当作有副作用对待。

这是按调用动态审批（needs_approval）的典型用例：同一个工具，只读命令不打扰用户，
变更命令才弹窗。
"""
import os
import shlex
import subprocess
from typing import Any, Dict

from ..base import BaseTool


# ── 用户环境 PATH 注入（#84 随案架构缺口，2026-08-10 实案）────────────────────
# app sidecar / devctl 拉起的 backend 是非登录进程：工具 shell 看不到用户终端里
# nvm/pyenv/homebrew 注入的 PATH——实案：用户 nvm 里明明有 node v24，工具 shell 报
# `command not found: npm`，模型误判"没装 node"绕去 brew 重复装。修法：进程内首次
# 调用时经交互式登录 zsh 取一次真实 PATH（source .zprofile+.zshrc，5s 熔断），缓存
# 复用；取不到就用当前 env 原样兜底（绝不因环境探测失败挡命令执行）。
_USER_ENV_CACHE: Dict[str, str] = {}


def _user_env() -> Dict[str, str]:
    if not _USER_ENV_CACHE:
        env = dict(os.environ)
        try:
            r = subprocess.run(["/bin/zsh", "-lic", "print -r -- $PATH"],
                               capture_output=True, text=True, timeout=5)
            path = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
            if r.returncode == 0 and path and os.pathsep in path:
                env["PATH"] = path
        except Exception:
            pass                                   # 兜底：backend 自身 PATH
        _USER_ENV_CACHE.update(env)
    return dict(_USER_ENV_CACHE)

# 只读命令白名单：执行后不改变系统状态，可自动放行（无需审批）。
# 刻意不含：curl（-o 写盘 / -d 外发数据，是提示注入外传的第一通道）、
# networksetup（-set* 直接改网络配置）、env/printenv（会把进程环境变量——含
# AGENT_API_KEY 等密钥——整份倒进模型上下文）——一律走审批。
# awk 留在名单里安全：其程序体必带 {}，已被下方元字符检查拦截，实际只有
# 「awk 作为管道段」之类本就被拦的场景会带它，单独出现无法执行程序体。
_READONLY_COMMANDS = {
    "ls", "pwd", "cd", "echo", "cat", "head", "tail", "wc", "stat", "file",
    "find", "grep", "rg", "fd", "tree", "which", "type", "whereis", "realpath", "basename", "dirname",
    "date", "cal", "uptime", "whoami", "id", "hostname", "uname", "sw_vers", "arch",
    "df", "du", "ps", "top", "history",
    "ping", "dig", "nslookup", "host", "ifconfig",
    "man", "help", "true", "false", "sleep", "test",
    "sort", "uniq", "cut", "awk", "sed", "diff", "cmp", "md5", "shasum", "column",
    "jq", "yq", "xxd", "od", "strings",
    "python", "python3", "node", "ruby",  # 仅纯 flag 查询（如 --version）放行，见 _is_readonly
}

# 白名单命令里仍能写状态的「危险参数」：命中即转审批（fail-safe 补丁，而非依赖黑名单）
_FIND_WRITE_ARGS = {"-delete", "-exec", "-execdir", "-ok", "-okdir",
                    "-fls", "-fprint", "-fprint0", "-fprintf"}

# git 只读子命令白名单（其余 git 操作如 commit/push/reset 需审批）
_GIT_READONLY_SUBCOMMANDS = {
    "status", "log", "show", "diff", "branch", "remote", "config", "rev-parse",
    "describe", "blame", "shortlog", "tag", "ls-files", "ls-remote", "cat-file", "reflog",
}

# 危险 shell 元字符：可引入子命令、重定向、命令替换或历史展开，出现即无法静态
# 保证只读，一律审批。注意不含 glob 字符（* ? [ ]）——它们只影响参数展开，
# 对只读首命令而言结果仍是只读，否则 `find . -name '*.py'` 这类合法只读命令会被误判。
_SHELL_METACHARS = set(";|&<>`$(){}\n!")

# 解释器直接执行代码的参数：内容不可知，需审批
_CODE_EXEC_FLAGS = {"-e", "-c", "--eval", "--exec"}


def _is_readonly(command: str) -> bool:
    """静态判定命令是否为「确定的只读操作」；任何不确定都返回 False（→ 需审批）。"""
    if not command or not command.strip():
        return False
    # 含 shell 元字符：可能有管道/重定向/子命令，无法确保只读
    if any(ch in _SHELL_METACHARS for ch in command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False  # 引号不闭合等：判不了，交给审批
    if not tokens:
        return False

    head = os.path.basename(tokens[0])
    args = tokens[1:]

    # 解释器：只放行纯 flag 查询（如 --version）。带内联代码（-e/-c）或脚本文件 → 内容
    # 不可知，需审批；裸解释器（无参数）会挂 REPL 干等到超时，同样不放行。
    if head in {"python", "python3", "node", "ruby"}:
        if not args or any(a in _CODE_EXEC_FLAGS for a in args):
            return False
        return all(a.startswith("-") for a in args)

    if head == "git":
        sub = next((a for a in args if not a.startswith("-")), None)
        return sub in _GIT_READONLY_SUBCOMMANDS

    # find 的写动作参数（-delete/-exec/…）：命令本身在白名单，但这些参数会删文件/执行子命令
    if head == "find":
        return not any(a in _FIND_WRITE_ARGS for a in args)

    # sed 的就地改写（-i / -i'' / --in-place）：写文件，需审批
    if head == "sed":
        return not any(a == "--in-place" or a.startswith("-i") for a in args)

    return head in _READONLY_COMMANDS


class ShellExecTool(BaseTool):
    """在本机执行终端命令（只读命令自动放行，其余需用户审批）"""
    name = "run_shell_command"
    description = (
        "Run a shell command on the user's macOS machine. This is your PRIMARY tool — prefer the "
        "terminal for most tasks (files, processes, network, dev tools, system queries). "
        "Read-only commands (ls, cat, grep, find, git status, ...) run immediately; any command "
        "that changes state (writing files, installing, sudo, pipes/redirects, or unknown "
        "commands) automatically triggers a user confirmation dialog before running — so just "
        "call the tool directly, do NOT ask the user for permission in words. The tool response "
        "reports stdout, stderr and the exit code (and tells you if the user denied it). "
        "To create or modify FILES, use the write_file / edit_file tools instead — never shell "
        "heredocs (cat <<EOF) or echo redirection."
    )
    requires_approval = False  # 默认放行；实际是否弹窗由 needs_approval 按命令内容动态决定
    parameters = {
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run (executed via /bin/zsh -c on macOS)."
            },
            "cwd": {
                "type": "string",
                "description": "Optional working directory (absolute path). Defaults to the user's home."
            },
            "timeout": {
                "type": "integer",
                "description": "Optional per-command timeout in seconds (default 60, max 600)."
            }
        },
        "required": ["command"]
    }

    def validate_arguments(self, arguments: Dict[str, Any]) -> "str | None":
        """审批前的确定性语法预检（registry.validate_call 钩子）。

        只拦「引号不闭合」这类注定 zsh 报错的硬伤——实测事故（复测会话 d618f0d6）：
        模型把函数调用语法带进命令（mkdir -p ~/Desktop/W12_Exercises'）），用户批准了
        一条必败命令、执行 unmatched '、修正后又骗一次审批。shlex 是 POSIX 近似，
        对引号配对足够；heredoc 正文可含合法奇数引号（don't），见 << 即跳过预检
        （宁漏勿误杀——预检的唯一职责是省掉必败调用的审批打扰）。
        """
        command = str((arguments or {}).get("command", ""))
        if not command.strip():
            return "System Error: empty command."
        if "<<" in command:
            return None  # heredoc：shlex 无法正确解析正文，交给执行路径
        try:
            shlex.split(command)
        except ValueError as e:
            return (f"System Error: shell syntax error ({e}) — the command was NOT executed "
                    "and NOT sent for user approval. Fix the quoting and re-issue it "
                    "(do not carry function-call syntax like a trailing ') into the command).")
        return None

    def needs_approval(self, arguments: Dict[str, Any]) -> bool:
        # 只读命令放行，其余（含判不了的）一律审批
        return not _is_readonly(str(arguments.get("command", "")))

    def execute(self, command: str, cwd: str = "", timeout: int = 60) -> str:
        if not command or not command.strip():
            return "System Error: empty command."

        # 工作目录校验
        workdir = os.path.expanduser(cwd) if cwd else os.path.expanduser("~")
        if not os.path.isdir(workdir):
            return f"System Error: working directory does not exist: {workdir}"

        try:
            timeout = max(1, min(int(timeout), 600))
        except (TypeError, ValueError):
            timeout = 60

        try:
            proc = subprocess.run(
                command,
                shell=True,
                executable="/bin/zsh",
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_user_env(),
            )
        except subprocess.TimeoutExpired:
            return f"System Error: command timed out after {timeout}s (partial output discarded)."
        except UnicodeDecodeError:
            # cat/head 二进制文件（PDF/图片/音频）的典型死路——给可行动出路而不是
            # 裸抛解码错误（实机事故：file_extract 失败后模型退化 cat PDF，两头碰壁）
            return ("System Error: command output is not text (binary file?). "
                    "For PDF/document/audio files use the file_extract tool with the file path "
                    "instead of cat/head.")
        except Exception as e:
            return f"System Error: failed to run command: {str(e)}"

        # 组装对模型友好的结构化结果
        parts = [f"exit_code: {proc.returncode}"]
        stdout = (proc.stdout or "").rstrip()
        stderr = (proc.stderr or "").rstrip()
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        if not stdout and not stderr:
            parts.append("(no output)")
        return "\n".join(parts)
