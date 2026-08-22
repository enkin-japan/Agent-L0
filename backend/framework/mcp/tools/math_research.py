"""主体可见的数学攻坚工具 math_research：多步"写码-执行-验证"沙箱的包壳。

与 web_research 同格局：主 Agent 只调这一个工具、只拿回一份结果摘要；沙箱循环、
执行器全封在 framework/agents（math_sandbox.py / math_tools.py），主 MCPRegistry
不扫描内部件。

为什么不直接用 run_math：实测（gemma-4-e4b）暴露两个模型层失效——工具算对但
模型抄错数字（转写失真）、模型用错误先验推翻正确的工具输出。本沙箱的结构性
解法：最终答案必须经 submit_answer(answer_code) 提交，ANSWER 段是解释器对
answer_code 的执行输出原文——数值不经过任何模型转写。

执行流同 web_research：本 execute 跑在 orchestrator 的工具线程里，经
run_coroutine_threadsafe 把沙箱协程投回主事件循环；此刻主轮次正 await 在本线程
上、已释放推理锁，沙箱取锁不与自身争抢，单 worker 不死锁。
"""
import asyncio
import concurrent.futures

from ..base import BaseTool
from ...agents import math_tools

# 配置统一在仓库根 config.yaml（math_research 段），寻址走 config_loader
from ...utils.config_loader import load_section
_CFG = load_section("math_research")


class MathResearchTool(BaseTool):
    name = "math_research"
    description = (
        "Delegate a complete math problem to a computation sub-module that solves it in an offline "
        "Python/SymPy sandbox with multiple compute-and-verify rounds, and returns a digest whose "
        "ANSWER section is printed directly by the interpreter (authoritative). Use this for "
        "multi-step problems: word problems, systems of equations, calculus chains, optimization, "
        "probability/statistics — anything needing several dependent computations or verification. "
        "For one quick calculation, call run_math directly instead. Pass a complete, self-contained "
        "problem statement (all numbers, units and conditions; the sub-module has no other memory). "
        "When answering the user, quote the values from the ANSWER section verbatim — digit for "
        "digit — even if they contradict what you expected; the interpreter is right and your "
        "memory is wrong."
    )
    requires_approval = False   # 沙箱禁网禁写（math_tools seatbelt），无副作用
    # 只读纯计算：无本地副作用，可与其他只读调用并发（沙箱内部取推理锁，与主生成互斥、不死锁）
    is_concurrency_safe = True

    parameters = {
        "properties": {
            "problem": {
                "type": "string",
                "description": "The complete, self-contained math problem in natural language "
                               "(include every number, unit and condition; the sub-module cannot "
                               "see the conversation).",
            },
        },
        "required": ["problem"],
    }

    def is_available(self) -> bool:
        """依赖与 run_math 相同：seatbelt、sympy、numpy 任一缺失则不注册。"""
        try:
            import sympy  # noqa: F401
            import numpy  # noqa: F401
        except Exception as e:
            print(f"[MCP-math_research] ⚠️ 数学依赖不可用（{e}），工具不注册。")
            return False
        if not math_tools.sandbox_available():
            print("[MCP-math_research] ⚠️ sandbox-exec 探活失败，工具不注册。")
            return False
        return True

    def execute(self, problem: str) -> str:
        from ...agents import runtime
        from ...agents.math_sandbox import run_math_research

        if not runtime.is_ready():
            return "System Error: math-research runtime not initialized (engine/event loop missing)."

        engine = runtime.get_engine()
        loop = runtime.get_loop()
        total_timeout = int(_CFG.get("total_timeout", 110))  # 须 < agent.tool_timeout_seconds

        # 用量槽必须在本工具线程取（ContextVar 随 to_thread 拷贝到此为止）：
        # run_coroutine_threadsafe 投回主循环的协程不继承本线程上下文，须作参数显式传入。
        usage_sink = runtime.get_usage_sink()

        try:
            fut = asyncio.run_coroutine_threadsafe(
                run_math_research(engine, problem, _CFG, usage_sink=usage_sink), loop)
        except Exception as e:
            return f"System Error: could not schedule the math-research sandbox: {e}"

        try:
            digest = fut.result(timeout=total_timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            digest = "[math research timed out before completion; no verified answer below]"
        except Exception as e:
            return f"System Error: math research raised an exception: {e}"

        return (
            "The following is the result from the math-research sub-module. The ANSWER section "
            "(if present) was printed by the sandbox interpreter and is authoritative — quote its "
            "values verbatim, digit for digit, even if they contradict your expectation.\n"
            f"<math_research problem=\"{problem[:200]}\">\n{digest}\n</math_research>"
        )
