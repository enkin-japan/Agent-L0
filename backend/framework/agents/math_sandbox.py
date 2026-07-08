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
from typing import Dict, List, Optional, Tuple

from mlx_vlm.generate import PromptCacheState

from . import math_tools


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


class _EphemeralSession:
    """一次性会话替身：只暴露 generate_once 读取的字段，全新 KV、用完即弃。不落盘。"""
    def __init__(self, problem: str):
        self.brain_memory_state = PromptCacheState()
        self.history: List[Dict] = [
            {"role": "system", "content": MATH_SANDBOX_SYSTEM_PROMPT},
            {"role": "user", "content": problem},
        ]
        self.images: List[str] = []
        self.audios: List[str] = []
        self.cancel_requested = False


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


async def run_math_research(engine, problem: str, cfg: dict) -> str:
    """
    沙箱主循环（协程，运行在主事件循环上）。返回结果摘要文本；其中 ANSWER 段
    为解释器输出原样嵌入。engine.generate_once 内部持有推理锁，与其他会话生成串行。
    """
    sess = _EphemeralSession(problem)
    max_steps = int(cfg.get("max_steps", 8))
    exec_timeout = int(cfg.get("exec_timeout", math_tools.DEFAULT_TIMEOUT))
    parse_retries = 0
    max_parse_retries = int(cfg.get("max_parse_retries", 2))
    nudged = False  # 无工具调用直接给结论时，推回一次（先验答题的典型形态）
    ok_log: List[Tuple[str, str]] = []  # 成功执行的 (code, output)，供摘要核对与兜底
    last_submit: Optional[Tuple[str, str]] = None  # 最近一次成功 submit（output, explanation）

    for step in range(max_steps):
        # max_tokens 收紧到 1024：沙箱产物只该是短代码调用/短结论，实测 2048 预算下
        # 失控生成单次就能写掉近 2000 token，直接吃穿整体墙钟。
        result = await engine.generate_once(sess, tools=_TOOLS, temperature=0.2, max_tokens=1024)
        if result.get("cancelled"):
            return "[math_research: generation cancelled]"
        print(f"[math_sandbox] step {step + 1}/{max_steps}: "
              f"calls={[c.get('name') for c in result.get('tool_calls', []) if c.get('type') != 'error']}")

        raw_calls = result.get("tool_calls", [])
        calls = [c for c in raw_calls if c.get("type") != "error"]
        text = result.get("text", "")
        thoughts = result.get("thoughts", "")

        if not calls:
            errors = [c for c in raw_calls if c.get("type") == "error"]
            if errors and parse_retries < max_parse_retries:
                # 工具格式崩了：反馈正确示例让它重试（对齐 research_sandbox 的容错闭环）
                parse_retries += 1
                bad = errors[0]
                sess.history.append({"role": "assistant", "content": text,
                                     "thoughts": thoughts, "tool_calls": raw_calls})
                feedback = (
                    "System Error: your tool call could not be parsed and was not executed.\n"
                    f"Your invalid output: {bad.get('raw_input')}\n"
                    f"Parser error: {bad.get('error_msg')}\n"
                    "A tool call must be valid JSON with double quotes, with parameters inside an "
                    "arguments object. For example:\n"
                    '{"name": "run_python", "arguments": {"code": "solve(x**2 - 5*x + 6, x)"}}\n'
                    "Resend immediately in the correct format."
                )
                sess.history.append({"role": "tool",
                                     "tool_responses": [{"name": "system", "response": feedback}]})
                continue
            if not nudged:
                # 纯文本结论 = 想靠记忆答题：推回，强制走 submit_answer
                nudged = True
                sess.history.append({"role": "assistant", "content": text,
                                     "thoughts": thoughts, "tool_calls": []})
                sess.history.append({"role": "user", "content": (
                    "You stated a conclusion without computing it. Recall is not acceptable here. "
                    "Compute the result with run_python, verify it, then call submit_answer — its "
                    "printed output is the only channel for the final answer.")})
                continue
            # 已推回过仍拒绝提交：诚实降级，标注未经验证
            note = "[math_research: the sub-module concluded WITHOUT submitting a computed answer — treat as UNVERIFIED]"
            if ok_log:
                code, out = ok_log[-1]
                note += f"\nLast successful computation for reference:\n>>> {' '.join(code.split())[:160]}\n{out}"
            return (text.strip() + "\n\n" + note) if text.strip() else note

        sess.history.append({"role": "assistant", "content": text,
                             "thoughts": thoughts, "tool_calls": calls})

        responses = []
        for c in calls:
            name = c.get("name")
            args = c.get("arguments", {}) or {}
            if name == "run_python":
                r = await asyncio.to_thread(
                    math_tools.execute_code, args.get("code", ""),
                    args.get("timeout", exec_timeout))
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
                r = await asyncio.to_thread(
                    math_tools.execute_code, args.get("answer_code", ""), exec_timeout)
                if r["ok"] and r["output"]:
                    # 提交成功：答案即解释器输出，立即收束返回
                    return _format_digest(r["output"], str(args.get("explanation", "")), ok_log)
                # answer_code 跑不出结果：反馈报错，循环继续
                msg = r["output"] if not r["ok"] else (
                    "answer_code produced no output — it must PRINT the final answer "
                    "(or end with a bare expression).")
                responses.append({"name": name,
                                  "response": "submit_answer rejected, fix answer_code and resubmit:\n" + msg})
            else:
                responses.append({"name": name,
                                  "response": f"Unknown tool {name!r} (this sandbox only has run_python / submit_answer)"})
        sess.history.append({"role": "tool", "tool_responses": responses})

    # 步数用尽：最后一次机会，强制 submit_answer
    sess.history.append({"role": "user", "content": (
        "Step budget exhausted. Call submit_answer NOW with answer_code that recomputes and prints "
        "your best verified result. This is the final step.")})
    result = await engine.generate_once(sess, tools=_TOOLS, temperature=0.2, max_tokens=1024)
    for c in result.get("tool_calls", []) or []:
        if c.get("type") != "error" and c.get("name") == "submit_answer":
            args = c.get("arguments", {}) or {}
            r = await asyncio.to_thread(
                math_tools.execute_code, args.get("answer_code", ""), exec_timeout)
            if r["ok"] and r["output"]:
                return _format_digest(r["output"], str(args.get("explanation", "")), ok_log)
    # 到死没提交：拿最后一次成功执行兜底，明确标注未经 submit 校验
    if ok_log:
        code, out = ok_log[-1]
        return ("[math_research: step budget exhausted before a formal submit_answer; "
                "returning the LAST SUCCESSFUL computation instead]\n"
                f">>> {' '.join(code.split())[:160]}\n{out}")
    return "[math_research: step budget exhausted with no successful computation]"
