"""引擎直连执行器：跑一道题，产出轨迹（turns + metrics + 墙钟）。

评测面即部署面：prompt 由方言组装、输出由方言 parser 还原、纠错回喂用编排层
同款信封（parse_error_feedback + 方言 parse_error_retry_hint）——测出的纠错
成功率才是部署里会发生的那个数。工具执行是**脚本化回喂**（题面给定响应表），
不碰真实工具：评的是模型的调用行为，不是工具本身。
"""
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from framework.core.orchestrator import _PARSE_HINT_FALLBACK, parse_error_feedback
from framework.memory.session import ChatSession

from .mock_envs import env_response_content, make_env

# 跨模型可比的固定极简系统提示（与实机联调探针同款；编排全量提示的行为归 attention 基准）
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant. Use tools when needed."

# 无脚本回喂表时的未知工具回执（幻觉工具名/越权调用的确定性反馈）
_UNKNOWN_TOOL_RESPONSE = json.dumps(
    {"error": "Tool not available. Use only the declared tools, or answer directly."})


def feed_for_calls(calls: List[Dict[str, Any]],
                   tool_responses: Dict[str, Any],
                   format_hint: str,
                   parse_error_counts: Dict[str, int],
                   env=None) -> List[Dict[str, Any]]:
    """按本轮调用列表构造回喂（纯函数，金测试钉形态）。

    - error 调用 → 编排层同款纠错信封（按 raw_input 计复读次数，升级止损文案）；
    - function 调用 → mock 环境 handler（env 存在时，长链维度）或题面脚本响应
      （对象 tojson）；两处都查无此名 → 未知工具回执。
    """
    feed: List[Dict[str, Any]] = []
    for call in calls:
        if call.get("type") == "error":
            raw = str(call.get("raw_input", "")).strip()
            parse_error_counts[raw] = parse_error_counts.get(raw, 0) + 1
            feed.append({"name": "system",
                         "response": parse_error_feedback(
                             call, parse_error_counts[raw], format_hint)})
            continue
        name = call.get("name", "")
        if env is not None:
            content = env_response_content(env.handle(name, call.get("arguments") or {}))
        elif name in tool_responses:
            resp = tool_responses[name]
            content = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
        else:
            content = _UNKNOWN_TOOL_RESPONSE
        feed.append({"name": name, "content": content})
    return feed


def _default_max_turns(task: Dict[str, Any]) -> int:
    if task.get("env"):
        return 8            # 长链：多步工具链 + 容错回合
    # 有脚本回喂表 = 期望闭环（调用→回喂→终答），留出一次纠错回合；否则单轮观测
    return 3 if task.get("tool_responses") else 1


async def run_item(engine, item: Dict[str, Any], repeat_idx: int = 0,
                   system_prompt: Optional[str] = None) -> Dict[str, Any]:
    """跑一道题的完整轨迹。返回：
    {"item_id", "repeat", "turns": [{thoughts, text, tool_calls, metrics, wall_s}...],
     "wall_s", "aborted_reason"}
    """
    task = item["task"]
    gen = item.get("gen") or {}
    env = make_env(task["env"], task.get("env_init")) if task.get("env") else None
    tools = (env.tools() if env is not None else None) or task.get("tools") or None
    tool_responses = task.get("tool_responses") or {}
    max_turns = int(task.get("max_turns") or _default_max_turns(task))

    hint_fn = getattr(getattr(engine, "dialect", None), "parse_error_retry_hint", None)
    format_hint = hint_fn() if callable(hint_fn) else _PARSE_HINT_FALLBACK

    turns: List[Dict[str, Any]] = []
    parse_error_counts: Dict[str, int] = {}
    t0 = time.monotonic()
    aborted_reason = ""
    with tempfile.TemporaryDirectory() as td:
        session = ChatSession(f"eval-{item['id']}-r{repeat_idx}", Path(td),
                              system_prompt=task.get("system") or system_prompt
                              or DEFAULT_SYSTEM_PROMPT,
                              backend=engine.backend)
        images = [str(Path(item.get("_src_dir", ".")) / p)
                  for p in task.get("images") or []]
        if images:
            missing = [p for p in images if not Path(p).exists()]
            if missing:
                raise FileNotFoundError(f"[{item['id']}] 图片资产缺失: {missing}")
            # 部署同款通路（turn_runner）：session.images 供后端取像素，
            # add_user_message 在 content 里放 image_url 块供方言渲染视觉占位
            session.images.extend(images)
            session.add_user_message(task["user"], images=images)
        else:
            session.history.append({"role": "user", "content": task["user"]})
        for _turn_i in range(max_turns):
            tt0 = time.monotonic()
            r = await engine.generate_once(
                session, tools=tools,
                enable_thinking=bool(gen.get("enable_thinking", True)),
                thinking_budget=int(gen.get("thinking_budget", 1024)),
                max_tokens=int(gen.get("max_tokens", 2048)),
                temperature=float(gen.get("temperature", 0.2)))
            turns.append({"thoughts": r.get("thoughts") or "",
                          "text": r.get("text") or "",
                          "tool_calls": r.get("tool_calls") or [],
                          "metrics": r.get("metrics") or {},
                          "wall_s": round(time.monotonic() - tt0, 2)})
            calls = r.get("tool_calls") or []
            if not calls:
                break
            session.history.append({"role": "assistant",
                                    "thoughts": r.get("thoughts") or "",
                                    "content": r.get("text") or "",
                                    "tool_calls": calls})
            feed = feed_for_calls(calls, tool_responses, format_hint,
                                  parse_error_counts, env=env)
            turns[-1]["feed"] = feed     # 回喂入轨迹：错误恢复行为是长链测量面
            session.history.append({"role": "tool", "tool_responses": feed})
        else:
            # 纯发射题（无脚本回喂/无 mock 环境，max_turns=1）：末轮发出调用就是
            # 预期终态，不算耗尽——闭环题（有回喂面）耗尽回合仍在发调用才是事故
            if env is not None or tool_responses:
                aborted_reason = f"回合上限 {max_turns} 耗尽仍在发调用"

    traj = {"item_id": item["id"], "repeat": repeat_idx, "turns": turns,
            "wall_s": round(time.monotonic() - t0, 2),
            "aborted_reason": aborted_reason}
    if env is not None:
        traj["env_state"] = env.snapshot()
    return traj


def function_calls(turn: Dict[str, Any]) -> List[Dict[str, Any]]:
    """轨迹辅助：取一轮里的合法 function 调用。"""
    return [c for c in turn.get("tool_calls", []) if c.get("type") == "function"]


def error_calls(turn: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [c for c in turn.get("tool_calls", []) if c.get("type") == "error"]


def trajectory_counts(traj: Dict[str, Any]) -> Tuple[int, int]:
    """(function 调用总数, error 调用总数)。"""
    fn = sum(len(function_calls(t)) for t in traj["turns"])
    er = sum(len(error_calls(t)) for t in traj["turns"])
    return fn, er
