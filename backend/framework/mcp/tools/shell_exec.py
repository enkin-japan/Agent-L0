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
import re
import shlex
import subprocess
from typing import Any, Dict

from ..base import BaseTool
from ...utils.config_loader import load_section


# ── 内层超时必须先于外层熔断触发（波 F2 前半）────────────────────────────────
# 病根：编排层对每次工具调用有 `agent.tool_timeout_seconds`（现 180s）的外层熔断，
# 到点**放弃等待**但被放弃的线程仍在后台跑；而本工具允许模型自报 timeout 到 600s 且
# 自身不声明 `timeout_seconds`。两者一减，实测存在**最长 420s 的孤儿窗口**：用户看到
# 「工具超时已放弃」，而那条命令还在机器上跑着，直到它自己的 600s 到点。
#
# 修法只有一行的量：把模型自报的 timeout 钳到「外层预算 − 余量」，让 subprocess 的
# 超时**先**触发——`subprocess.run` 超时时会 kill 掉子进程。
# **如实的说法**：直接子进程的孤儿窗口归零，**孙进程的不归零**（见下方边界）。
# 原注释写「孤儿窗口归零」是把收益说大了（首审 #11）。
#
# 余量要覆盖「外层 wait_for 已开始计时、而本函数还没进 subprocess」的那段：
# `_user_env()` 首次调用要起一个交互式 zsh 探 PATH（自带 5s 熔断），加上线程调度与
# 结果回传。取 15s。
#
# 已知边界（明写，不假装没有）：`subprocess.run` 的超时只 kill 直接子进程
# （`/bin/zsh -c ...`），**孙进程不受影响**——而钳位针对的恰恰是 `npm install` / `make`
# 这类会派生 worker 的命令，它们的孙进程仍会跑满原时长。另：`run()` 在 kill 后的
# `wait()` 无超时，卡在 D 状态的子进程照样会冲破外层预算。
# 要连坐整个进程组得换 `Popen(start_new_session=True)` + `os.killpg`——那是本条的
# **加强版（F2 后半）**，不是前置件；本波按 OSS 扫描的分工只做前半。
# ⚠️ 回落值必须与 `core/orchestrator.py` 里 `tool_timeout_seconds` 的回落值**逐字相同**
# （两侧注释互指，仿 `_BENIGN_EXIT1_MARK` 的跨模块契约）。此前两侧一个 180 一个 120：
# config 里缺这个键时外层是 120、内层上限却按 180 算成 165，**孤儿窗口以 45s 悄悄回来**
# ——而当时那条「内层 < 外层」的金测试比的是本模块自己重新推导的值，**永远看不见这个
# 漂移**（首审 Critical 2）。`test_shell_exec_timeout_clamp.py` 里有一条跨模块契约测试
# 钉死两侧同值。
_OUTER_BUDGET_FALLBACK_S = 180
def _derive_outer_budget(raw: Any) -> int:
    """把 config 里的原始值化成外层预算秒数；任何不成形的输入都回落到约定值。

    **import 期不许抛**：本模块此前零 config 依赖，而 `registry.py` 对工具导入失败只
    print 一行——配置里写成 `"180s"`、写成列表、或写成 0，都会让 agent 悄悄少了主力
    工具而没有任何别的信号（首审 #4）。
    单独成函数是为了能拿坏输入直接喂它：不这样的话，「防御性」这件事在本仓的真实
    config 下永远走不到，变异体也就杀不死（第一轮就是这么活下来的）。
    """
    # bool 单独挡：YAML 里的 `true` 会被 `int()` 悄悄化成 1，于是外层预算变 1 秒、
    # 内层上限跟着塌到 1 秒——全产品的 shell 命令等于被禁掉，而没有任何报错。
    # 与 `_clamp_timeout` 里那处是同一个坑（`int(True) == 1`）。
    if isinstance(raw, bool):
        return _OUTER_BUDGET_FALLBACK_S
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _OUTER_BUDGET_FALLBACK_S
    return v if v > 0 else _OUTER_BUDGET_FALLBACK_S


try:
    _OUTER_BUDGET_S = _derive_outer_budget(load_section("agent").get("tool_timeout_seconds"))
except Exception:                       # load_section 本身炸了（config 缺失/坏了）
    _OUTER_BUDGET_S = _OUTER_BUDGET_FALLBACK_S
_CLAMP_MARGIN_S = 15
# ⚠️ 不设下限：下限一旦大于「外层 − 余量」就会把不变量顶破，而不变量比可用性优先。
# 代价如实写明——`agent.tool_timeout_seconds` 现在是**双向承重**的：把它调到 30s 以下，
# 全产品的 shell 命令上限会跟着塌到十几秒甚至 1 秒（首审 #3）。
_MAX_INNER_TIMEOUT_S = max(1, _OUTER_BUDGET_S - _CLAMP_MARGIN_S)
_DEFAULT_TIMEOUT_S = min(60, _MAX_INNER_TIMEOUT_S)


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

# ── 良性非零退出码（OSS 扫描 §6.1 实案，2026-08-17）──────────────────────────
# 下列命令的 exit 1 是「正常的否定答案」而非错误：grep/rg 无命中、diff/cmp 有差异、
# test 条件为假。而编排层的 D2 谎报围堵（orchestrator.py `_FAILED_RESP_RE`）按退出码
# 判失败——不区分就会把「确认某物不存在」这类高频只读答案当成硬失败，推回一次
# failure_swallow_nudge **逼模型向用户报告一个不存在的错误**，白烧一整次生成。
# 判定必须在本工具内做完：编排层只拿得到响应文本、拿不到命令，绝不可从文本反推
# 命令语义（那是两处真相）。命中即在首行打 `_BENIGN_EXIT1_MARK` 供编排层识别。
# 名单刻意不含：find（无命中返回 0，非零一律是真错）、fd（本机未安装，无命中退出码
# 语义无法实证——宁可漏判成失败，不可误判成良性）。
# 证据分级（刻意标清，勿混为一谈）：grep/diff/cmp/test 四条为**本机实测**
# （2026-08-17）——否定答案均为 exit 1 + stderr 空，真错误（路径不存在等）均为
# exit 2 + stderr 非空，故「exit 恰为 1 且 stderr 空」能干净分开两者；`rg` 一条按
# **ripgrep 官方文档**的 0/1/2 约定收入，本机 PATH 上没有 rg 二进制（非交互 zsh 看不到
# 它），无法实测。未装时该命令返回 127 + stderr，四条与门本就不放行，收入无风险。
_BENIGN_EXIT1_COMMANDS = {"grep", "rg", "diff", "cmp", "test"}

# 良性标记：与 orchestrator.py `_FAILED_RESP_RE` 的负向断言对应（两侧注释互指）。
# test_fabrication_guardrails.py 有一条跨模块契约测试钉死「本标记不被判为失败」——
# 它能挡住把标记改成不再被断言认出的漂移（真正会坏事的那种）；同前缀内的措辞微调
# 仍会通过，那种漂移不破坏契约。标记长度也是模型可读性与 UI 行宽的折中：它会成为
# 步骤卡的首行摘要（orchestrator 取 `splitlines()[0]`）。
_BENIGN_EXIT1_MARK = " (benign: negative result, not an error)"


def _clamp_timeout(requested: Any) -> "tuple[int, int]":
    """把模型自报的 timeout 钳到「外层预算 − 余量」。返回 (生效值, 被钳前的值或 0)。

    单独成函数是为了**可被秒级测试**：不这样的话，验钳位就得真 sleep 到上限
    （实测让这份测试跑了 8 分钟）。而这份测试在 pre-commit 里——每次提交多花九分钟的
    门，最终一定会被绕过、然后被删掉。
    """
    # bool 单独挡掉：`int(True) == 1`，于是 JSON 里的 "timeout": true 会变成
    # **1 秒超时**而不是回落到默认值（首审 #14）。
    if isinstance(requested, bool):
        req = _DEFAULT_TIMEOUT_S
    else:
        try:
            req = max(1, int(requested))
        except (TypeError, ValueError):
            req = _DEFAULT_TIMEOUT_S
    eff = min(req, _MAX_INNER_TIMEOUT_S)
    return eff, (req if req > eff else 0)


# ── 参数用错的指路（波 F10）────────────────────────────────────────────────
# 只认**明确是「参数不对」**的措辞，故意窄：漏报优于误报。挂在结果组装处而非异常
# 分支——首行必须仍是 `exit_code: N`（编排层的良性码判据与前端步骤卡都读它）。
#
# 「invalid option」收进来（首审 #10）：BSD/macOS 自带的 grep/sudo 正是用这个措辞报
# 参数错（`grep: invalid option -- Q`），排掉它会丢掉本机最主要的一类真阳性。
# 但**不收裸 "invalid"**：`grep: invalid pattern` 是数据问题，指去 man 没用。
#
# **只扫 stderr，不扫 stdout**（首审 #18）：`cat README missing` 这类命令会把文件正文
# 送进 stdout，正文里恰好有一行 "unknown option" 就会误触发。usage 走 stdout 的 CLI
# 确实存在，那部分按「漏报优于误报」放弃。
_USAGE_ERROR_RE = re.compile(
    r"(?im)^\s*(?:usage:"
    r"|.*\b(?:illegal option|unknown option|unrecognized option|invalid option)\b)")

# 透明包裹层：真正出错的是它们后面那个命令。`sudo apt instal` 指去 `man sudo` 是纯误导。
_CMD_WRAPPERS = {"sudo", "env", "time", "nohup", "nice", "command", "stdbuf", "xargs", "doas"}

# 有子命令体系的 CLI：`git lgo` 的正解是 `man git-log`，不是 `man git`。
_SUBCOMMAND_CLIS = {"git", "brew", "docker", "npm", "yarn", "pnpm", "cargo", "kubectl",
                    "apt", "apt-get", "systemctl", "gcloud", "aws", "gh", "pip", "pip3"}

_USAGE_HINT = ("\nhint: this looks like a wrong flag, not a wrong idea. "
               "Check the real flag before re-issuing — do not retry with another guessed "
               "flag. {how} (man pages are long and get truncated in the middle, so grep "
               "the part you need rather than reading the whole page).")


def _hint_target(command: str):
    """这条命令的报错该指向哪个命令名；判不准返回 None，否则返回 (命令名, 子命令或 None)。

    ⚠️ 含 shell 元字符一律放弃归因：`echo hi | grep -Q x` 的失败是 grep 的，而首 token
    是 echo——指去 `man echo` **比不指更坏**（首审 Critical 1，实测复现）。这一条与
    `_is_benign_exit1` 复用同一份 `_SHELL_METACHARS`，理由也同一个：管道/子命令的
    退出码与报错都归属最后一段，无法归因给首命令。
    """
    if any(ch in _SHELL_METACHARS for ch in command):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    # 剥掉 VAR=VAL 前缀与透明包裹层
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if "=" in t and not t.startswith("-") and t.split("=", 1)[0].isidentifier():
            i += 1
            continue
        if os.path.basename(t) in _CMD_WRAPPERS:
            i += 1
            continue
        break
    if i >= len(tokens):
        return None
    head = os.path.basename(tokens[i])
    # 首 token 不像个命令名（路径片段、变量展开残留…）就别硬编一条 man 指令出来
    if not head or not head.replace("-", "").replace("_", "").replace(".", "").isalnum():
        return None
    return head, next((a for a in tokens[i + 1:] if not a.startswith("-")), None)


def _usage_hint(command: str, returncode: int, stderr: str) -> str:
    """命中「参数用错」的措辞时给一行指路；判不准就不给（漏报优于误报）。"""
    if returncode == 0:
        return ""
    if not _USAGE_ERROR_RE.search(stderr or ""):
        return ""
    target = _hint_target(command)
    if target is None:
        return ""
    head, sub = target
    # 子命令体系的 CLI：`man git` 讲不到 `git log` 的旗标。刻意**不**编 `man git-lgo`
    # 这种页名——子命令本身就是打错的那一半时它根本不存在，而「No manual entry」
    # 只会再浪费一轮（首审 #6 提的 `man git-log` 只在子命令拼对时才成立）。
    if head in _SUBCOMMAND_CLIS and sub:
        how = f"Run `{head} {sub} --help`"
    else:
        how = f"Run `man {head}`"
    return _USAGE_HINT.format(how=how)


def _is_benign_exit1(command: str, returncode: int, stderr: str) -> bool:
    """判定「本次 exit 1 是正常否定答案」。四条与门，任一不成立即当作真失败。

    方向是 fail-safe：判不准一律不打标（退回既有的「非零即失败」行为），
    宁可多推回一次，也不可把真失败标成良性——后者会让 D2 护栏漏掉真事故。

    知情取舍（评审提出，确认接受）：名单里这几个命令也正是模型**自检**的惯用手法
    （`grep -q pat out` / `test -f path` / `diff expected actual`），那里的 exit 1
    意思是「自检没通过」。放行后 D2 的失败推回不再覆盖这类自检——它在文本层与
    「没找到就是答案」不可分，而后者是桌面 agent 的高频正常路径，误报代价（每次白烧
    一整轮生成 + 逼模型报告不存在的错误）远大于此。写后未复查另有 T1-4 verify 门
    覆盖；「搜不到说明我写失败了」属语义级判断，本就归模型不归正则。
    """
    if returncode != 1 or stderr.strip():   # strip：只有空白的 stderr 等同于没有
        return False
    # 元字符复用 `_is_readonly` 的同一份常量（单一来源，不另立一份免得漂移）：管道/
    # 子命令的退出码归属最后一段，无法归因给首命令。
    # 代价（已知）：该集合是为「能否静态保证只读」设计的，比归因所需更宽——含
    # `( ) { } $ !`，于是 `grep 'x{2}' f`、`grep '\$V' f` 这类**引号内**带正则元字符的
    # 命令也会落到「不打标」，仍吃一次误报推回。方向安全（漏判成失败），故接受。
    # ⚠️ 若将来为审批面放宽 `_SHELL_METACHARS`，本判定会随之放宽——那是不安全方向
    # （可能把管道的退出码误归给首命令），届时必须在这里另立一份更窄的集合。
    if any(ch in _SHELL_METACHARS for ch in command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    return os.path.basename(tokens[0]) in _BENIGN_EXIT1_COMMANDS


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
        "call the tool directly, do NOT ask the user for permission in words. "
        # D2-减法(类A):响应格式句删——stdout/stderr/exit_code/拒批文案均由代码产出,
        # 模型在首次调用的响应里即见,常驻声明无需复述。
        "To create or modify FILES, use the write_file / edit_file tools instead — never shell "
        "heredocs (cat <<EOF) or echo redirection.\n"
        # 波 F10：CLI 参数的先验记忆不可信，本地 4bit 模型尤甚。横切行为规范挂在工具
        # 描述上（本仓既有归宿），不新建剧本——剧本的 requires 只有本工具，会永远探活
        # 通过、永远挂在索引里稀释预算，且规范只在载入它的那一轮生效。
        "Your memory of CLI flags is often stale or invented. Before using a flag you are "
        "not certain about, check the authoritative source on this machine: `man <cmd>` is "
        "read-only and never prompts the user. `<cmd> --help` is free only when <cmd> itself "
        "is a read-only command (ls, grep, git...); for anything else (brew, docker, an "
        "unfamiliar binary) it costs the user one extra approval — which is exactly the case "
        "where you needed it. If a command fails with a usage/unknown-option error, do not "
        "retry with another guessed flag; read the manual first, and grep it rather than "
        "dumping the whole page (long man output gets truncated in the middle)."
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
                # 原写 "max 600" —— 钳位落地后这句就成了对模型说的假话。上限现算。
                "description": (
                    # D2-减法(类A):钳位机理复述删——钳位由 _clamp_timeout 代码强制,
                    # 模型只需知道上限与「超了会被钳」。
                    f"Optional per-command timeout in seconds "
                    f"(default {_DEFAULT_TIMEOUT_S}, max {_MAX_INNER_TIMEOUT_S}; "
                    f"larger values are capped)."
                ),
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

        timeout, clamped_from = _clamp_timeout(timeout)

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
            # 被钳过就明说，否则模型只会看到「我要了 600s 却 165s 就超时」而无从理解，
            # 多半会原样重试一次同样注定被钳的调用。
            extra = ("" if not clamped_from else
                     f" (your requested {clamped_from}s was capped at {timeout}s: the "
                     f"orchestrator abandons any tool after {_OUTER_BUDGET_S}s, so a longer "
                     f"inner timeout would only leave the command running unattended). "
                     f"Split the work into steps that each finish within {timeout}s, or run it "
                     f"in the background and poll for results.")
            return (f"System Error: command timed out after {timeout}s "
                    f"(partial output discarded).{extra}")
        except UnicodeDecodeError:
            # cat/head 二进制文件（PDF/图片/音频）的典型死路——给可行动出路而不是
            # 裸抛解码错误（实机事故：file_extract 失败后模型退化 cat PDF，两头碰壁）
            return ("System Error: command output is not text (binary file?). "
                    "For PDF/document/audio files use the file_extract tool with the file path "
                    "instead of cat/head.")
        except Exception as e:
            return f"System Error: failed to run command: {str(e)}"

        # 组装对模型友好的结构化结果。良性非零码在首行随退出码打标：模型据此知道
        # 「没找到」是答案不是错误，编排层的失败判据据此放行（见 _is_benign_exit1）。
        mark = (_BENIGN_EXIT1_MARK
                if _is_benign_exit1(command, proc.returncode, proc.stderr or "") else "")
        parts = [f"exit_code: {proc.returncode}{mark}"]
        stdout = (proc.stdout or "").rstrip()
        stderr = (proc.stderr or "").rstrip()
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        if not stdout and not stderr:
            parts.append("(no output)")
        # 参数用错的指路（F10）：追加在末尾，首行仍是 exit_code
        return "\n".join(parts) + _usage_hint(command, proc.returncode, stderr)
