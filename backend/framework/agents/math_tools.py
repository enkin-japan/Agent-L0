"""数学沙箱执行核心：seatbelt 子进程跑 Python，计算正确性由解释器保证。

被两处共用（与 web_tools.py 同格局——能力本体放 agents，MCP 层只是包壳）：
 · framework/mcp/tools/run_math.py    —— 主模型直调的单发计算工具；
 · framework/agents/math_sandbox.py   —— math_research 子 agent 的内部执行器。

安全边界（免审批的依据）：
 · seatbelt（sandbox-exec）：禁全部网络（含 localhost，否则成为绕过 web_tools
   SSRF 防线的后门）、禁全部文件写入（只放行 /dev/null）；读放行（python 要读 venv）；
 · 子进程隔离 + 墙钟超时 kill（sandbox-exec 直接 exec 目标进程，同 PID，杀得干净）；
 · python -I（隔离模式：不读环境变量/用户 site，venv site-packages 仍生效）。
"""
import subprocess
import sys
from typing import Dict

# seatbelt 规则：SBPL 后写的规则优先级更高，故 allow default 之后逐条收紧。
_SANDBOX_PROFILE = (
    "(version 1)"
    "(allow default)"
    "(deny network*)"
    '(deny file-write*)'
    '(allow file-write-data (literal "/dev/null"))'
)

# 子进程内的执行壳：argv[1] 为模型代码。预载常用数学环境（sympy 符号版
# sqrt/pi 等后导入、覆盖 math 的浮点版，默认精确计算）；末行若是表达式则
# 按 REPL 语义回显其值。
_HARNESS = r"""
import ast, sys, traceback
ns = {}
exec(compile(
    "import math, cmath, itertools, statistics\n"
    "from fractions import Fraction\n"
    "from decimal import Decimal, getcontext\n"
    "import numpy as np\n"
    "from sympy import *\n"
    "from sympy.abc import x, y, z, t, n, k\n",
    "<prelude>", "exec"), ns)
try:
    tree = ast.parse(sys.argv[1])
except SyntaxError:
    traceback.print_exc(limit=0)
    sys.exit(1)
echo = tree.body.pop().value if tree.body and isinstance(tree.body[-1], ast.Expr) else None
try:
    exec(compile(tree, "<code>", "exec"), ns)
    if echo is not None:
        val = eval(compile(ast.Expression(echo), "<code>", "eval"), ns)
        if val is not None:
            print(repr(val))
except Exception:
    traceback.print_exc(limit=3)
    sys.exit(1)
"""

DEFAULT_TIMEOUT = 20   # 秒；调用方可调高，硬上限须 < agent.tool_timeout_seconds
MAX_TIMEOUT = 60
MAX_OUTPUT_CHARS = 4000  # 观察注入上限，防大数组 repr 撑爆上下文

EMPTY_OUTPUT_MSG = ("(no output — end your code with a bare expression to echo its value, "
                    "or print() what you need)")

# 小模型手滑高频字符的无歧义转写（实测：模型爱在代码里写 × ² 全角括号等，
# 每次都换来一轮 SyntaxError 重试）。只转语义唯一确定的；± 这类有歧义的不碰，
# 留给报错闭环让模型自己改。
_UNICODE_FIXES = str.maketrans({
    "×": "*", "÷": "/", "−": "-", "–": "-",
    "²": "**2", "³": "**3",
    "（": "(", "）": ")", "［": "[", "］": "]", "｛": "{", "｝": "}",
    "，": ",", "：": ":", "；": ";", "＝": "=", "＋": "+",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    " ": " ", "　": " ",
})


def sandbox_available() -> bool:
    """seatbelt 探活：sandbox-exec 跑不起来就绝不无沙箱执行代码。"""
    try:
        probe = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", _SANDBOX_PROFILE, "/usr/bin/true"],
            capture_output=True,
        )
        return probe.returncode == 0
    except Exception:
        return False


def execute_code(code: str, timeout: int = DEFAULT_TIMEOUT) -> Dict:
    """
    在 seatbelt 沙箱里执行一段数学 Python。返回 {"ok": bool, "output": str}：
    ok=True 时 output 为程序 stdout（已截断，可能为空）；
    ok=False 时 output 为面向模型的报错文本（Traceback + 高频坑定向提示）。
    """
    if not code or not code.strip():
        return {"ok": False, "output": "System Error: empty code."}
    code = code.translate(_UNICODE_FIXES)
    try:
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    try:
        proc = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", _SANDBOX_PROFILE,
             sys.executable, "-I", "-B", "-c", _HARNESS, code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": (
            f"System Error: computation timed out after {timeout}s. Simplify the approach "
            f"(e.g. avoid brute force / infinite loops), or raise the timeout parameter "
            f"(max {MAX_TIMEOUT}s) if the symbolic computation is legitimately heavy.")}
    except Exception as e:
        return {"ok": False, "output": f"System Error: failed to launch math sandbox: {e}"}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if len(stdout) > MAX_OUTPUT_CHARS:
        stdout = stdout[:MAX_OUTPUT_CHARS] + "\n…[output truncated]"
    if len(stderr) > MAX_OUTPUT_CHARS:
        stderr = stderr[:MAX_OUTPUT_CHARS] + "\n…[stderr truncated]"

    if proc.returncode != 0:
        err = stderr or stdout or f"exit code {proc.returncode} with no output"
        # 高频坑定向提示：math.*/np.* 套符号变量。表现形态有两种——
        # math.sin(x) → TypeError: Cannot convert expression to float；
        # np.sin(x)   → AttributeError/TypeError: Symbol has no callable sin method。
        # 小模型对裸 Traceback 常常连续踩同一个坑，报错里直接给出改法。
        if ("Cannot convert expression to float" in err
                or "no attribute" in err and "Symbol" in err
                or "no callable" in err and "Symbol" in err
                or "TypeError" in err and ("math." in code or "np." in code)):
            err += ("\nHint: math.* and np.* functions only accept NUMBERS. On symbolic "
                    "variables (x, y, ...) use the pre-imported sympy functions instead — "
                    "e.g. integrate(x*sin(x), (x, 0, pi)) with sympy's sin and pi.")
        return {"ok": False, "output": err}
    return {"ok": True, "output": stdout}
