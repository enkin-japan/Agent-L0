"""run_math：数学计算沙箱（单发直调）——模型不擅长"算"，把计算交给解释器保证正确。

定位：一次调用、确定性、结果紧凑的快速计算入口。执行核心（seatbelt 沙箱、
预载环境、Unicode 转写、报错提示）在 framework/agents/math_tools.py，与
math_research 子 agent 共用；本文件只是 MCP 包壳（与 web_research 同格局）。

多步攻坚（多个相互依赖的计算、需要验算的完整问题）应走 math_research——
其最终答案由解释器输出程序化提取，不经模型转写。
"""
from ..base import BaseTool
from ...agents import math_tools


class RunMathTool(BaseTool):
    name = "run_math"
    description = (
        "Execute Python in an offline math sandbox and return its output. SymPy is star-imported "
        "(symbols, solve, integrate, diff, limit, simplify, factor, Matrix, Rational, pi, oo, ...) "
        "with symbols x, y, z, t, n, k predeclared, plus numpy as np, math, Fraction, Decimal, "
        "statistics; scipy is importable. Use this for "
        "ANY non-trivial calculation — multi-digit arithmetic, equations, calculus, linear algebra, "
        "statistics, unit conversions — instead of computing in your head; if the result "
        "contradicts what you remember, the sandbox is right and your memory is wrong — always "
        "report the sandbox's value. Write SHORT high-level calls (solve(x**2-5*x+6, x), not a "
        "hand-coded quadratic formula) in plain ASCII Python — no unicode math symbols like ±. "
        "For symbolic work always use the pre-imported sympy names (sin, cos, exp, sqrt, pi, oo) — "
        "never math.* or np.* on a symbolic variable. The last line, if an expression, is echoed "
        "like a REPL (no print needed); use print() for intermediate values. When feasible, verify "
        "your answer in the same call (e.g. substitute the solution back). For a multi-step problem "
        "that needs several dependent computations, use math_research instead. No network and no "
        "file access; runs are stateless — each call must be self-contained."
    )
    requires_approval = False       # 免审批的依据是 math_tools 里的 seatbelt 沙箱：禁网、禁写、超时 kill
    is_concurrency_safe = True      # 无状态纯计算子进程，只读无副作用
    parameters = {
        "properties": {
            "code": {
                "type": "string",
                "description": "Self-contained Python code to run. Math libraries are pre-imported.",
            },
            "timeout": {
                "type": "integer",
                "description": "Optional wall-clock limit in seconds (default 20, max 60) for heavy "
                               "symbolic computation.",
            },
        },
        "required": ["code"],
    }

    def is_available(self) -> bool:
        """沙箱三件套探活：seatbelt、sympy、numpy 任一缺失则不注册（绝不无沙箱裸跑）。"""
        try:
            import sympy  # noqa: F401
            import numpy  # noqa: F401
        except Exception as e:
            print(f"[MCP-run_math] ⚠️ 数学依赖不可用（{e}），工具不注册。")
            return False
        if not math_tools.sandbox_available():
            print("[MCP-run_math] ⚠️ sandbox-exec 探活失败，拒绝无沙箱执行代码，工具不注册。")
            return False
        return True

    def execute(self, code: str, timeout: int = math_tools.DEFAULT_TIMEOUT) -> str:
        r = math_tools.execute_code(code, timeout)
        if not r["ok"]:
            if r["output"].startswith("System Error:"):
                return r["output"]
            return "Math sandbox error (fix the code and retry):\n" + r["output"]
        return r["output"] or math_tools.EMPTY_OUTPUT_MSG
