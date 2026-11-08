"""拐点 harness 编排器模式（W-K，`run_knee.py --orchestrator`）的纯装配件。

- 工具：mock env 的每个工具一个 `EnvTool`（schema = env.tools()，执行 = env.handle → JSON）；
  `--prefix prod|catalog` 时生产工具各一个可用桩 `orch_stubs.UsableStub`（schema 取 prefix.py 同一份
  声明，执行 = 按工具名的确定性离线行为：vfs 桥接 / 检索恒空 / 计算与常态 / 离线不可用，W-K2）。
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

from context_knee.orch_stubs import (UsableStub, VfsBridge, missing_required, missing_text,
                                     offline_text)
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


#: 未知工具回执的 JSON 前缀（mock env 同款；可用桩落地后生产工具名不应再出现，计 stub_unknown）
UNKNOWN_PREFIX = "Unknown tool:"


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
    """只读 spill 回读 + file_extract 可用桩：path 以 `spill:` 开头时按
    framework/core/spill.resolve_spill_ref 的规则（落盘名正则 + realpath 必须落在本会话 spill 目录内，
    穿越 / 符号链接出逃一律拒）读本会话 spill 文件、返回原文（可选 1 起的 start/end 行区间）；其他
    path 是 file_extract 桩（W-K2）：有 vfs 桥时读 mock env 的文件（生产文本分支同形：行号前缀 +
    `[名 lines a-b of N]` 头），没有时回离线不可用。session / bridge 由 harness 挂上。
    readbacks = 成功 spill 回读次数；served = 非 spill 路径的桩调用次数（计 stub_served）。"""

    effect = "read"
    is_concurrency_safe = True

    def __init__(self, schema: Optional[Dict[str, Any]] = None, summary: str = ""):
        self.adopt(schema or SPILL_MIN_SCHEMA, summary)
        self.session = None
        self.readbacks = 0
        self.served = 0
        self.failed = 0
        self.failures: List[str] = []
        self.bridge: Optional[VfsBridge] = None
        self.env_tools: Tuple[str, ...] = ()
        self._count_lock = threading.Lock()

    def adopt(self, schema: Dict[str, Any], summary: str = "") -> None:
        """声明取生产 file_extract 的那一份（prod 档）或最小声明（mock 档）。"""
        self.name = SPILL_TOOL
        self.description = schema.get("description") or SPILL_TOOL
        self.parameters = copy.deepcopy(schema.get("parameters") or {})
        self.summary = summary or ""

    def execute(self, path: Any = None, start: Any = None, end: Any = None,
                max_chars: Any = None, **_ignored) -> str:
        p = str(path or "").strip()
        if missing_required(self.parameters, {"path": path}):
            with self._count_lock:
                self.served += 1
                text = missing_text(SPILL_TOOL, ["path"])
                self.failed += 1
                self.failures.append(text)
            return text
        if not p.startswith(SPILL_HANDLE_PREFIX):
            with self._count_lock:
                self.served += 1
            if self.bridge is None:
                return offline_text(SPILL_TOOL, self.env_tools)
            try:
                return self.bridge.extract(path, start, end, max_chars)
            except Exception as e:      # noqa: BLE001 —— 桩的意外不冒成 registry 的 System Error
                return f"Error: file_extract failed in the offline sandbox: {type(e).__name__}: {e}"
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
    """真 MCPRegistry（空 tools_dir，不扫任何生产实现）+ 生产可用桩（生产声明序）+ env 工具。
    env 有 vfs 工具集时建一座 VfsBridge（与 EnvTool 同一把锁），vfs 类桩与 file_extract 经它读写
    env；没有时这些桩回离线不可用。spill_tool：prod / catalog 档在生产 file_extract 的位置顶替其桩
    （声明取生产那份）；mock 档（桩里没有 file_extract）注册在 env 工具之后、用最小声明。注册日志
    按 run 重复且无信息量，吞掉。"""
    tools_dir = Path(tools_dir)
    tools_dir.mkdir(parents=True, exist_ok=True)
    if any(tools_dir.glob("*.py")):
        raise RuntimeError(f"编排器模式的 tools_dir 必须为空目录：{tools_dir}")
    summaries = summaries or {}
    lock = threading.Lock()
    env_schemas = list(env.tools()) if env is not None else []
    env_names = tuple(s["name"] for s in env_schemas)
    bridge = VfsBridge(env, lock) if VfsBridge.supports(env) else None
    if spill_tool is not None:
        spill_tool.bridge, spill_tool.env_tools = bridge, env_names
    with contextlib.redirect_stdout(io.StringIO()):
        reg = MCPRegistry(tools_dir=tools_dir)
        placed = False
        for s in stubs:
            if spill_tool is not None and s["name"] == SPILL_TOOL:
                spill_tool.adopt(s, summaries.get(s["name"], ""))
                reg.register(spill_tool)
                placed = True
            else:
                reg.register(UsableStub(s, summaries.get(s["name"], ""), bridge=bridge,
                                        env_tools=env_names))
        for s in env_schemas:
            reg.register(EnvTool(env, s, lock))
        if spill_tool is not None and not placed:
            reg.register(spill_tool)
    return reg


def agent_cfg(base: Dict[str, Any], *, guard: bool, step: bool, goal_verify: bool,
              max_total_iterations: Optional[int] = None, hard_tokens: Optional[int] = None,
              fold_mode: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """config.yaml agent 段（深拷贝）+ 开关覆盖 → (cfg, 覆盖项)。max_total_iterations 只在 agent 段
    本有该键时覆盖（编排器的回合熔断键；与 executor 的 max_turns 对齐）。hard_tokens / fold_mode
    非 None 时覆盖 context_guard 同名键（W1 复测臂）；不在此校验，交给 resolve_context_guard 装配期拒。"""
    cfg = copy.deepcopy(base or {})
    over: Dict[str, Any] = {}
    cg = dict(cfg.get("context_guard") or {})
    cg["enabled"] = bool(guard)
    over["context_guard.enabled"] = bool(guard)
    if hard_tokens is not None:
        cg["hard_tokens"] = hard_tokens
        over["context_guard.hard_tokens"] = hard_tokens
    if fold_mode is not None:
        cg["fold_mode"] = fold_mode
        over["context_guard.fold_mode"] = fold_mode
    cfg["context_guard"] = cg
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


def norm_guard_thresholds(g: Any) -> Any:
    """guard_thresholds 的比对/展示归一：fold_mode 键 2026-11-09（D48）才加入，之前的行 / meta 都是
    full 口径（当时只有一种 fold），缺键按 "full" 补齐再比——旧档案目录（w12eval / p2b …）的 full 臂
    仍可续跑 / 追加，而 minimal 对 full 照样拒；analyze_orch.py 的唯一性判定与 thr 展示列同一归一，
    不然同臂一半有 fold_mode 一半缺键会被误判成「不唯一」。非 dict 原样返回。"""
    if isinstance(g, dict) and "fold_mode" not in g:
        return {**g, "fold_mode": "full"}
    return g


#: keep_mock_tools_full 的并入规则版本（D44 (iii)，2026-11-06）：effective_mode = 按生效 tool_schema_mode 判
#: （此前按 --prefix 字面值，只 catalog 档并入）。run_knee 记入行 / meta 的 mock_merge_rule 并恒比
#: （_ALWAYS_COMPARED），缺键 = 旧规则的目录，续跑 / 追加拒
MOCK_MERGE_RULE = "effective_mode"


def keep_mock_tools_full(cfg: Dict[str, Any], asm,
                         model_options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """编排器生效披露为 catalog 时：题目 mock 工具并入本 run 的 agent.tool_routing.core，使编排器的
    catalog 披露里它们恒为完整 schema、只有生产工具集走目录行——与直调 catalog 档（prefix.assemble
    「其后 mock 工具完整」）同一对待，两种模式的 catalog 臂才可比。只改本 run 的 cfg 副本，不碰
    config.yaml / 生产路径；生效 full 原样返回。

    判据是**生效的** tool_schema_mode = tool_disclosure.resolve_tool_schema_mode(model_options, cfg)
    （与 Orchestrator 构造时、行键 tool_schema_mode_effective 同一解析），不是 --prefix 字面值：
    D44 (iii)（2026-11-05）起 Qwen 条目也是 catalog，--prefix prod 随生产生效为 catalog，须与
    --prefix catalog 同样并入，两臂才可比。model_options 传交给 Orchestrator 的那份。"""
    from framework.core.tool_disclosure import resolve_tool_schema_mode
    if resolve_tool_schema_mode(model_options, cfg) != "catalog":
        return cfg
    out = copy.deepcopy(cfg)
    tr = dict(out.get("tool_routing") or {})
    core = list(tr.get("core") or [])
    core += [t["name"] for t in asm.mock_tools if t["name"] not in core]
    tr["core"] = core
    out["tool_routing"] = tr
    return out


# ---------------- 轨迹映射 ----------------

def _is_meta(call: Dict[str, Any]) -> bool:
    return call.get("type") != "error" and call.get("name") in META_NAMES


def stub_served(registry: Optional[MCPRegistry]) -> Dict[str, int]:
    """{生产工具名: 本 run 桩执行次数}（UsableStub.served + file_extract 的非 spill 调用；0 不列）。"""
    out: Dict[str, int] = {}
    for name, tool in sorted(((getattr(registry, "_tools", None) or {}).items())):
        n = getattr(tool, "served", 0) if isinstance(tool, (UsableStub, SpillReadTool)) else 0
        if n:
            out[name] = int(n)
    return out


def _stub_tools(registry: Optional[MCPRegistry]):
    return [(n, t) for n, t in sorted(((getattr(registry, "_tools", None) or {}).items()))
            if isinstance(t, (UsableStub, SpillReadTool))]


def stub_failed(registry: Optional[MCPRegistry]) -> int:
    """本 run 桩发出的 `System Error:` 失败回执数（写类失败 + 缺必填参数）。"""
    return sum(int(getattr(t, "failed", 0) or 0) for _n, t in _stub_tools(registry))


def _feed_texts(history: List[Dict[str, Any]], start: int):
    for m in history[start:]:
        if m.get("role") == "tool":
            for tr in m.get("tool_responses") or []:
                if isinstance(tr, dict) and tr.get("name") not in META_NAMES:
                    yield tr.get("name"), str(tr.get("response", ""))


def stub_fields(registry: Optional[MCPRegistry], history: Optional[List[Dict[str, Any]]] = None,
                start: int = 0) -> Dict[str, Any]:
    """三个桩计数字段（timeout / 异常行也记）：stub_served / stub_unknown（history 给了才数）/ stub_failed。"""
    unknown = sum(1 for _n, text in _feed_texts(history or [], start or 0) if _is_unknown_tool(text))
    return {"stub_served": stub_served(registry), "stub_unknown": unknown,
            "stub_failed": stub_failed(registry)}


def _is_unknown_tool(text: str) -> bool:
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(obj, dict) and str(obj.get("error", "")).startswith(UNKNOWN_PREFIX)


def traj_from_history(history: List[Dict[str, Any]], start: int, *, item_id: str, rep: int,
                      env, aborted_reason: str = "", wall_s: float = 0.0,
                      registry: Optional[MCPRegistry] = None
                      ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """history[start:]（本 run 自己追加的段：run_turn 写的 assistant / tool / 护栏 system）→ executor
    轨迹形态 + 附加计数。

    - assistant 消息 → 一个 turn：tool_calls 只含非元工具调用（function 与解析失败 error 项，
      executor 同形态），元工具按名计入 meta_calls；text = content。
    - tool 消息 → 前一个 turn 的 feed：每条 {"name", "content": tr["response"]}（评分器读 content
      里 JSON 的 "error"）；元工具的回执不进 feed（与 tool_calls 对齐）。"Duplicate call blocked" /
      "System Error" 开头的观察另计 dup_blocked / system_errors，照常进 feed。
    - system 消息（编排层护栏提醒）不成 turn。
    - 桩计数（W-K2）：stub_served = 本 run 注册表里可用桩的执行次数（registry 给了才有；含 vfs
      桥接与离线回执，不含 spill 回读），stub_unknown = feed 里 `{"error": "Unknown tool: …"}` 回执数
      （绊线：应恒为 0），stub_failed = 桩自己发的 `System Error:`（写类失败 / 缺必填参数）——按桩记下
      的回执原文逐条认领，**不计** system_errors（它仍只表示编排层回执 / 注册表未知名）。
    """
    claim: Dict[Tuple[Any, str], int] = {}
    for name, tool in _stub_tools(registry):
        for text in getattr(tool, "failures", None) or []:
            claim[(name, text)] = claim.get((name, text), 0) + 1
    turns: List[Dict[str, Any]] = []
    meta_calls: Dict[str, int] = {}
    dup = syserr = unknown = 0
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
                    key = (tr.get("name"), text)
                    if claim.get(key):
                        claim[key] -= 1         # 桩自己的失败回执：计 stub_failed，不计 system_errors
                    else:
                        syserr += 1
                if _is_unknown_tool(text):
                    unknown += 1
                feed.append({"name": tr.get("name"), "content": text})
            if turns and feed:
                turns[-1].setdefault("feed", []).extend(feed)
    traj = {"item_id": item_id, "repeat": rep, "turns": turns, "wall_s": round(wall_s, 2),
            "aborted_reason": aborted_reason}
    if env is not None:
        traj["env_state"] = env.snapshot()
    return traj, {"meta_calls": meta_calls, "dup_blocked": dup, "system_errors": syserr,
                  "stub_served": stub_served(registry), "stub_unknown": unknown,
                  "stub_failed": stub_failed(registry)}


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
