"""数学攻坚沙箱循环（math_research 子 agent）：多步"写码-执行-验证"，答案不经模型转写。

与 research_sandbox（web_research）同格局：复用主模型权重 + generate_once，一次性
EphemeralSession（全新 KV，不碰真实会话），工具手工装配、主 MCPRegistry 永不扫描。

本沙箱要解决的核心问题不是"没有计算能力"（run_math 已提供），而是实测暴露的
两个模型层失效模式：
 1. 转写失真——工具算对了，模型抄错数字；
 2. 先验压过工具——模型明知结果冲突仍用记忆里的错误答案推翻解释器输出。
因此结构性约束：最终答案必须经 submit_answer(answer_code) 提交，上层拿到的
ANSWER 是解释器对 answer_code 的执行输出——模型的自然语言只能解释方法，
永远没有机会"转写"或"推翻"数值本身。
"""
import asyncio
from typing import List, Optional, Tuple

from . import math_tools
from .sandbox_runtime import EphemeralSession, SandboxSpec, note_usage, run_sandbox


MATH_SANDBOX_SYSTEM_PROMPT = (
    "You are a math-solving sub-module. You can only use two tools — run_python and submit_answer "
    "— and have no other capabilities (no network, no file access, no OS control).\n"
    "Task: solve the math problem below by COMPUTING, never by recalling.\n"
    "Rules (mandatory):\n"
    "1. Every numeric or symbolic result must come from run_python execution. Never state a value "
    "from memory — if you remember the answer, verify it by computing it anyway; the interpreter "
    "is always right and your memory is always suspect.\n"
    "2. Write SHORT high-level SymPy calls (solve, integrate, diff, limit, simplify, Matrix ...) "
    "in plain ASCII Python. sympy is star-imported with x, y, z, t, n, k predeclared; numpy as np, "
    "math, Fraction, Decimal are available. On symbolic variables use sympy's sin/cos/exp/sqrt/pi, "
    "never math.* or np.*.\n"
    "3. Each run is STATELESS — every call must be self-contained (re-declare everything).\n"
    "4. Verify before submitting: substitute the solution back, or recompute via an independent "
    "method, in a run_python call.\n"
    "5. Finish by calling submit_answer: answer_code must be self-contained code that PRINTS (or "
    "echoes as its last expression) the final answer; its execution output is delivered to the "
    "upper agent verbatim as the authoritative answer. Make it print a complete, labeled result "
    "(e.g. print(f'integral value: {val}') or a dict of named results).\n"
    "6. Only if the problem is genuinely unsolvable (ill-posed, missing data), stop calling tools "
    "and explain why in plain text."
)

_RUN_PYTHON_SCHEMA = {
    "name": "run_python",
    "description": "Execute self-contained Python in the offline math sandbox (sympy star-imported, "
                   "x/y/z/t/n/k predeclared, numpy as np). Last bare expression is echoed like a "
                   "REPL; use print() for intermediate values. Stateless between calls.",
    "parameters": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Self-contained Python code to run."},
            "timeout": {"type": "integer",
                        "description": "Optional wall-clock seconds (default 20, max 60)."},
        },
        "required": ["code"],
    },
}
_SUBMIT_ANSWER_SCHEMA = {
    "name": "submit_answer",
    "description": "Submit the final answer. answer_code is self-contained Python whose printed "
                   "output IS the final answer delivered verbatim to the upper agent — recompute "
                   "the result inside it and print it with labels. explanation briefly states the "
                   "method (no numbers needed there).",
    "parameters": {
        "type": "object",
        "properties": {
            "answer_code": {"type": "string",
                            "description": "Self-contained code that prints the complete final "
                                           "answer (labeled)."},
            "explanation": {"type": "string",
                            "description": "One-to-three sentences on the method used and how it "
                                           "was verified."},
        },
        "required": ["answer_code"],
    },
}
_TOOLS = [_RUN_PYTHON_SCHEMA, _SUBMIT_ANSWER_SCHEMA]


def _format_digest(answer_output: str, explanation: str,
                   verified_log: List[Tuple[str, str]]) -> str:
    """组装交回上层的结果：ANSWER 段为解释器原样输出（权威），解释只谈方法。"""
    parts = []
    if explanation.strip():
        parts.append(f"Method: {explanation.strip()}")
    parts.append("ANSWER (printed by the sandbox interpreter, authoritative — "
                 "quote these values verbatim):\n" + answer_output)
    if verified_log:
        log_lines = []
        for code, out in verified_log[-2:]:  # 最近两条成功执行，供上层核对过程
            one_line = " ".join(code.split())
            if len(one_line) > 160:
                one_line = one_line[:160] + "…"
            log_lines.append(f">>> {one_line}\n{out}")
        parts.append("Verified computation log:\n" + "\n".join(log_lines))
    return "\n\n".join(parts)


async def run_math_research(engine, problem: str, cfg: dict,
                            usage_sink: Optional[dict] = None) -> str:
    """
    沙箱主循环（协程，运行在主事件循环上）。返回结果摘要文本；其中 ANSWER 段
    为解释器输出原样嵌入。循环骨架见 sandbox_runtime.run_sandbox。

    usage_sink：父轮的用量回收槽（F6）。包壳在工具线程取出后作参数传入——投回主循环的
    协程不继承工具线程 ContextVar。None = 无轮次上下文，记账空转。
    """
    sess = EphemeralSession(engine.backend, MATH_SANDBOX_SYSTEM_PROMPT, problem)
    max_steps = int(cfg.get("max_steps", 8))
    exec_timeout = int(cfg.get("exec_timeout", math_tools.DEFAULT_TIMEOUT))
    ok_log: List[Tuple[str, str]] = []  # 成功执行的 (code, output)，供摘要核对与兜底

    async def _run(code: str, timeout=None) -> dict:
        return await asyncio.to_thread(math_tools.execute_code, code,
                                       timeout if timeout is not None else exec_timeout)

    async def _dispatch(calls):
        responses = []
        for c in calls:
            name = c.get("name")
            args = c.get("arguments", {}) or {}
            if name == "run_python":
                r = await _run(args.get("code", ""), args.get("timeout", exec_timeout))
                if r["ok"]:
                    if r["output"]:
                        ok_log.append((args.get("code", ""), r["output"]))
                        responses.append({"name": name, "response": r["output"]})
                    else:
                        responses.append({"name": name, "response": math_tools.EMPTY_OUTPUT_MSG})
                else:
                    responses.append({"name": name,
                                      "response": "Sandbox error (fix the code and retry):\n" + r["output"]})
            elif name == "submit_answer":
                r = await _run(args.get("answer_code", ""))
                if r["ok"] and r["output"]:
                    # 提交成功：答案即解释器输出，立即收束返回
                    return responses, _format_digest(r["output"], str(args.get("explanation", "")), ok_log)
                # answer_code 跑不出结果：反馈报错，循环继续
                msg = r["output"] if not r["ok"] else (
                    "answer_code produced no output — it must PRINT the final answer "
                    "(or end with a bare expression).")
                responses.append({"name": name,
                                  "response": "submit_answer rejected, fix answer_code and resubmit:\n" + msg})
            else:
                responses.append({"name": name,
                                  "response": f"Unknown tool {name!r} (this sandbox only has run_python / submit_answer)"})
        return responses, None

    def _give_up(text: str) -> str:
        """已推回过仍拒绝提交：诚实降级，标注未经验证。"""
        note = "[math_research: the sub-module concluded WITHOUT submitting a computed answer — treat as UNVERIFIED]"
        if ok_log:
            code, out = ok_log[-1]
            note += f"\nLast successful computation for reference:\n>>> {' '.join(code.split())[:160]}\n{out}"
        return (text.strip() + "\n\n" + note) if text.strip() else note

    async def _exhausted() -> str:
        """步数用尽：最后一次机会，强制 submit_answer。"""
        sess.history.append({"role": "user", "content": (
            "Step budget exhausted. Call submit_answer NOW with answer_code that recomputes and prints "
            "your best verified result. This is the final step.")})
        result = await engine.generate_once(sess, tools=_TOOLS, temperature=0.2, max_tokens=1024)
        note_usage(usage_sink, generations=1,
                   generation_tokens=(result.get("metrics") or {}).get("generation_tokens", 0) or 0)
        for c in result.get("tool_calls", []) or []:
            if c.get("type") != "error" and c.get("name") == "submit_answer":
                args = c.get("arguments", {}) or {}
                r = await _run(args.get("answer_code", ""))
                if r["ok"] and r["output"]:
                    return _format_digest(r["output"], str(args.get("explanation", "")), ok_log)
        # 到死没提交：拿最后一次成功执行兜底，明确标注未经 submit 校验
        if ok_log:
            code, out = ok_log[-1]
            return ("[math_research: step budget exhausted before a formal submit_answer; "
                    "returning the LAST SUCCESSFUL computation instead]\n"
                    f">>> {' '.join(code.split())[:160]}\n{out}")
        return "[math_research: step budget exhausted with no successful computation]"

    return await run_sandbox(engine, SandboxSpec(
        tag="math_research",
        session=sess,
        tools=_TOOLS,
        max_steps=max_steps,
        # max_tokens 收紧到 1024：沙箱产物只该是短代码调用/短结论，实测 2048 预算下
        # 失控生成单次就能写掉近 2000 token，直接吃穿整体墙钟。
        max_tokens=1024,
        usage_sink=usage_sink,
        dispatch=_dispatch,
        on_give_up=_give_up,
        on_exhausted=_exhausted,
        max_parse_retries=int(cfg.get("max_parse_retries", 2)),
        # 格式教学由方言提供（见 SandboxSpec.parse_feedback）；这里只补本沙箱的引导
        parse_feedback="Resend immediately in the correct format.",
        # 纯文本结论 = 想靠记忆答题：推回，强制走 submit_answer
        nudge=("You stated a conclusion without computing it. Recall is not acceptable here. "
               "Compute the result with run_python, verify it, then call submit_answer — its "
               "printed output is the only channel for the final answer."),
    ))
