"""拐点 harness 编排器模式（W-K，`run_knee.py --orchestrator`）的纯装配件。

- 工具：mock env 的每个工具一个 `EnvTool`（schema = env.tools()，执行 = env.handle → JSON）；
  `--prefix prod` 时生产工具各一个 `StubTool`（schema 取 prefix.py 同一份声明，执行恒回
  `{"error": "Unknown tool: <名>"}`，与 mock env 未知工具回执同口径——生产工具只声明、不执行）。
  注册表是真 `MCPRegistry`，tools_dir 指向产物目录下的空目录（不扫任何生产工具实现）。
- agent 段配置：config.yaml 的 agent 段深拷贝后按开关覆盖（不改 config.yaml）。
- 轨迹映射：run_turn 结束后把本 run 自己追加的 history 段映射成 executor 轨迹形态，交给
  longchain 评分器（评分器不改）。
- 事件计数：guardrail_events.jsonl（已被 run_knee 改指到产物目录）按 run 的字节区间计数。

本模块不 import run_knee：run_knee 作为 __main__ 跑时，再 import 一次会得到第二份模块实例
（信号状态 _INTERRUPT 就分家了）。
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from framework.core.orchestrator import META_TOOL_NAMES
from framework.core.spill import SPILL_HANDLE_PREFIX, resolve_spill_ref
from framework.mcp.base import BaseTool
from framework.mcp.registry import MCPRegistry

#: mock env 里只读的工具（其余都改状态）。只读 = effect "read" + 可并发 + 只读指纹；改状态 =
#: effect "write" + 串行 + 写屏障。与生产同名工具的分类一致：写类若也标只读，编排层的「零变更
#: 却声称已做」守门（_is_local_mutation）会对每个「已写入 …」的正常收尾误推一次，写后复读
#: 也会被重复调用屏障拦掉（写屏障只在非只读调用后解禁复读）。
READ_ONLY_ENV_TOOLS = frozenset({"list_files", "read_file", "todo_list"})

#: 编排器自带的元工具（不注册、不进轨迹 tool_calls，另计 meta_calls）
META_NAMES = frozenset(META_TOOL_NAMES)

#: 本 run 计入 dup_blocked / system_errors 的观察前缀（orchestrator 的固定文案）
DUP_PREFIX = "Duplicate call blocked"
SYSERR_PREFIX = "System Error"


class EnvTool(BaseTool):
    """一个 mock env 工具。env.handle 在 to_thread 线程里跑（只读批会并发）：同一 env 的调用
    经一把锁串行，env 的状态（含故障配额计数）不出竞态。"""

    def __init__(self, env, schema: Dict[str, Any], lock: threading.Lock):
        self.name = schema["name"]
        self.description = schema.get("description") or schema["name"]
        self.parameters = copy.deepcopy(schema.get("parameters") or {})
        ro = self.name in READ_ONLY_ENV_TOOLS
        self.effect = "read" if ro else "write"
        self.is_concurrency_safe = ro
        self.is_read_only = ro
        self._env = env
        self._lock = lock

    def execute(self, **arguments) -> str:
        with self._lock:
            resp = self._env.handle(self.name, arguments)
        return json.dumps(resp, ensure_ascii=False)


class StubTool(BaseTool):
    """生产工具桩：声明照生产，执行恒回未知工具回执（mock_envs 同款 JSON，评分器计 env_errors）。"""

    effect = "read"
    is_concurrency_safe = True

    def __init__(self, schema: Dict[str, Any], summary: str = ""):
        self.name = schema["name"]
        self.description = schema.get("description") or schema["name"]
        self.parameters = copy.deepcopy(schema.get("parameters") or {})
        self.summary = summary or ""

    def execute(self, **arguments) -> str:
        return json.dumps({"error": f"Unknown tool: {self.name}"}, ensure_ascii=False)


#: spill 回读工具名：W2 / ceiling / 截断的桩给的回读句柄是 `file_extract(path="spill:<stem>")`
SPILL_TOOL = "file_extract"
#: mock 档没有生产 file_extract 声明时用的最小声明（只为让桩里的句柄调得通）
SPILL_MIN_SCHEMA = {
    "name": SPILL_TOOL,
    "description": ("Read back the full text of an earlier tool observation that was compacted or "
                    "truncated, by its handle path=\"spill:<id>\". Optional start/end = 1-based "
                    "line range."),
    "parameters": {"type": "object",
                   "properties": {"path": {"type": "string"}, "start": {"type": "integer"},
                                  "end": {"type": "integer"}},
                   "required": ["path"]}}


class SpillReadTool(BaseTool):
    """只读 spill 回读：path 以 `spill:` 开头时按 framework/core/spill.resolve_spill_ref 的规则
    （落盘名正则 + realpath 必须落在本会话 spill 目录内，穿越 / 符号链接出逃一律拒）读本会话
    spill 文件、返回原文（可选 1 起的 start/end 行区间）；其他任何 path 回
    `{"error": "Unknown tool: file_extract"}`——生产 file_extract 不执行。session 由 harness 在
    会话建好后挂上。readbacks = 成功回读次数。"""

    effect = "read"
    is_concurrency_safe = True

    def __init__(self, schema: Optional[Dict[str, Any]] = None, summary: str = ""):
        self.adopt(schema or SPILL_MIN_SCHEMA, summary)
        self.session = None
        self.readbacks = 0

    def adopt(self, schema: Dict[str, Any], summary: str = "") -> None:
        """声明取生产 file_extract 的那一份（prod 档）或最小声明（mock 档）。"""
        self.name = SPILL_TOOL
        self.description = schema.get("description") or SPILL_TOOL
        self.parameters = copy.deepcopy(schema.get("parameters") or {})
        self.summary = summary or ""

    def execute(self, path: Any = None, start: Any = None, end: Any = None, **_ignored) -> str:
        p = str(path or "").strip()
        if not p.startswith(SPILL_HANDLE_PREFIX):
            return json.dumps({"error": f"Unknown tool: {SPILL_TOOL}"}, ensure_ascii=False)
        target = resolve_spill_ref(self.session, p)
        if target is None:
            return json.dumps({"error": f"spill handle not found: {p}"}, ensure_ascii=False)
        text = target.read_text(encoding="utf-8")
        if isinstance(start, int) or isinstance(end, int):
            lines = text.splitlines()
            lo = max(int(start), 1) if isinstance(start, int) else 1
            hi = int(end) if isinstance(end, int) else len(lines)
            text = "\n".join(lines[lo - 1:hi])
        self.readbacks += 1
        return text


def stub_schemas(asm) -> List[Dict[str, Any]]:
    """--prefix 某档要造桩的生产工具声明（完整 schema）：语法面全集去掉题目 mock 工具与元工具。
    数据源 = prefix.assemble（与直调模式同一份；撞名已在那里剔除）。mock 档 = []。"""
    if asm.mode == "mock":
        return []
    mock = {t["name"] for t in asm.mock_tools}
    return [s for s in (asm.grammar_tools or []) if s["name"] not in mock
            and s["name"] not in META_NAMES]


def build_registry(tools_dir: Path, env, stubs: Sequence[Dict[str, Any]],
                   summaries: Optional[Dict[str, str]] = None,
                   spill_tool: Optional[SpillReadTool] = None) -> MCPRegistry:
    """真 MCPRegistry（空 tools_dir，不扫任何生产实现）+ 生产桩（生产声明序）+ env 工具。
    spill_tool：prod 档在生产 file_extract 的位置顶替其桩（声明取生产那份）；mock 档（桩里没有
    file_extract）注册在 env 工具之后、用最小声明。注册日志按 run 重复且无信息量，吞掉。"""
    tools_dir = Path(tools_dir)
    tools_dir.mkdir(parents=True, exist_ok=True)
    if any(tools_dir.glob("*.py")):
        raise RuntimeError(f"编排器模式的 tools_dir 必须为空目录：{tools_dir}")
    summaries = summaries or {}
    with contextlib.redirect_stdout(io.StringIO()):
        reg = MCPRegistry(tools_dir=tools_dir)
        placed = False
        for s in stubs:
            if spill_tool is not None and s["name"] == SPILL_TOOL:
                spill_tool.adopt(s, summaries.get(s["name"], ""))
                reg.register(spill_tool)
                placed = True
            else:
                reg.register(StubTool(s, summaries.get(s["name"], "")))
        lock = threading.Lock()
        for s in (env.tools() if env is not None else []):
            reg.register(EnvTool(env, s, lock))
        if spill_tool is not None and not placed:
            reg.register(spill_tool)
    return reg


def agent_cfg(base: Dict[str, Any], *, guard: bool, step: bool, goal_verify: bool,
              max_total_iterations: Optional[int] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """config.yaml agent 段（深拷贝）+ 开关覆盖 → (cfg, 覆盖项)。max_total_iterations 只在 agent 段
    本有该键时覆盖（编排器的回合熔断键；与 executor 的 max_turns 对齐）。"""
    cfg = copy.deepcopy(base or {})
    over: Dict[str, Any] = {}
    cg = dict(cfg.get("context_guard") or {})
    cg["enabled"] = bool(guard)
    cfg["context_guard"] = cg
    over["context_guard.enabled"] = bool(guard)
    sc = dict(cfg.get("step_compaction") or {})
    sc["enabled"] = bool(step)
    cfg["step_compaction"] = sc
    over["step_compaction.enabled"] = bool(step)
    cfg["goal_verify"] = bool(goal_verify)
    over["goal_verify"] = bool(goal_verify)
    if max_total_iterations is not None and "max_total_iterations" in cfg:
        cfg["max_total_iterations"] = int(max_total_iterations)
        over["max_total_iterations"] = int(max_total_iterations)
    return cfg, over


# ---------------- 轨迹映射 ----------------

def _is_meta(call: Dict[str, Any]) -> bool:
    return call.get("type") != "error" and call.get("name") in META_NAMES


def traj_from_history(history: List[Dict[str, Any]], start: int, *, item_id: str, rep: int,
                      env, aborted_reason: str = "", wall_s: float = 0.0
                      ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """history[start:]（本 run 自己追加的段：run_turn 写的 assistant / tool / 护栏 system）→ executor
    轨迹形态 + 附加计数。

    - assistant 消息 → 一个 turn：tool_calls 只含非元工具调用（function 与解析失败 error 项，
      executor 同形态），元工具按名计入 meta_calls；text = content。
    - tool 消息 → 前一个 turn 的 feed：每条 {"name", "content": tr["response"]}（评分器读 content
      里 JSON 的 "error"）；元工具的回执不进 feed（与 tool_calls 对齐）。"Duplicate call blocked" /
      "System Error" 开头的观察另计 dup_blocked / system_errors，照常进 feed。
    - system 消息（编排层护栏提醒）不成 turn。
    """
    turns: List[Dict[str, Any]] = []
    meta_calls: Dict[str, int] = {}
    dup = syserr = 0
    for m in history[start:]:
        role = m.get("role")
        if role == "assistant":
            calls = list(m.get("tool_calls") or [])
            for c in calls:
                if _is_meta(c):
                    meta_calls[c["name"]] = meta_calls.get(c["name"], 0) + 1
            th = m.get("thoughts")
            turns.append({"thoughts": th if isinstance(th, str) else "\n".join(th or []),
                          "text": m.get("content") if isinstance(m.get("content"), str) else "",
                          "tool_calls": [c for c in calls if not _is_meta(c)],
                          "metrics": {}, "wall_s": None})
        elif role == "tool":
            feed = []
            for tr in m.get("tool_responses") or []:
                if not isinstance(tr, dict) or tr.get("name") in META_NAMES:
                    continue
                text = str(tr.get("response", ""))
                if text.startswith(DUP_PREFIX):
                    dup += 1
                elif text.startswith(SYSERR_PREFIX):
                    syserr += 1
                feed.append({"name": tr.get("name"), "content": text})
            if turns and feed:
                turns[-1].setdefault("feed", []).extend(feed)
    traj = {"item_id": item_id, "repeat": rep, "turns": turns, "wall_s": round(wall_s, 2),
            "aborted_reason": aborted_reason}
    if env is not None:
        traj["env_state"] = env.snapshot()
    return traj, {"meta_calls": meta_calls, "dup_blocked": dup, "system_errors": syserr}


def aborted_reason_for(status: str, abort_reasons: Sequence[str], max_turns: int) -> str:
    """run_turn 结果 → 轨迹 aborted_reason（评分器据此判 aborted）。success = ""；本 run 的
    turn_abort 事件：context_guard 原样、max_iterations 用 executor 的回合耗尽原句，其余取事件
    reason；没有 turn_abort 的非 success：cancelled / pending_approval / suspended 记 status；
    warning 而无 turn_abort（D2 推回后仍坚持的降级收尾）不算 aborted——模型给了收尾答复，
    状态另记 turn_status。"""
    if status == "success":
        return ""
    for r in abort_reasons:
        if r == "max_iterations":
            return f"回合上限 {max_turns} 耗尽仍在发调用"
        if r:
            return str(r)
    if status in ("cancelled", "pending_approval", "suspended"):
        return status
    return ""


# ---------------- 事件计数 ----------------

class EventCursor:
    """guardrail_events.jsonl 的 run 级游标：mark() 记当前字节偏移与行号，since() 读出该区间
    的事件，按 type 计数，turn_abort 另取 reason。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lines = self._count_lines()

    def _count_lines(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("rb") as f:
            return sum(1 for _ in f)

    def mark(self) -> Tuple[int, int]:
        size = self.path.stat().st_size if self.path.exists() else 0
        return size, self.lines

    def since(self, mark: Tuple[int, int], session_id: Optional[str] = None
              ) -> Tuple[Dict[str, int], List[str], List[int]]:
        off, line0 = mark
        if not self.path.exists():
            return {}, [], [line0, line0]
        with self.path.open("rb") as f:
            f.seek(off)
            raw = f.read()
        rows = [ln for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        self.lines = line0 + len(rows)
        counts: Dict[str, int] = {}
        aborts: List[str] = []
        for ln in rows:
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if session_id is not None and ev.get("session") not in (None, session_id):
                continue
            t = str(ev.get("type"))
            counts[t] = counts.get(t, 0) + 1
            if t == "turn_abort":
                aborts.append(str((ev.get("detail") or {}).get("reason") or ""))
        return counts, aborts, [line0, self.lines]
