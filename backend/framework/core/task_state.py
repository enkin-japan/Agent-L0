"""
任务状态对象（多步任务的计划与预算计数器）。

设计原则（编排架构方案）：
- 计划是数据：模型只能通过结构化操作（update_plan / complete_step 元工具）读写，
  每个操作由本模块校验合法性，非法操作原样打回让模型自纠，绝不允许自由改写状态；
- 规则是代码：预算上限、失败路由、终止判定属于 Orchestrator 的确定性护栏，
  本模块只负责状态存取、操作校验与上下文渲染；
- 渲染为上下文尾部小块：计划状态永远追加在 prompt 最尾部（见 PromptBuilder.build
  的 task_block），稳定前缀（system + 工具目录）与追加式 history 的 KV 缓存不受影响。
"""
import json
import time
from typing import Any, Dict, List, Optional, Tuple

STEP_PENDING = "pending"
STEP_DOING = "doing"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

# 计划规模的硬性边界（护栏，不是模型可协商的参数）
MAX_PLAN_STEPS = 12
MAX_STEP_TITLE_CHARS = 200

# 记事板（scratchpad）边界：模型自写的持久便签，跨步可见，抗上下文压缩。
# 有界记忆原则——model-facing 上下文必须有界，故 FIFO 淘汰而非无限累积。
MAX_NOTES = 12
MAX_NOTE_CHARS = 200

_STATUS_MARKS = {
    STEP_PENDING: "○",
    STEP_DOING: "▶",
    STEP_DONE: "✔",
    STEP_FAILED: "✗",
    STEP_SKIPPED: "⊘",
}


class TaskState:
    """一次多步任务的完整可序列化状态：目标、计划、计数器。"""

    def __init__(self, goal: str = ""):
        self.goal = goal
        self.plan: List[Dict[str, Any]] = []  # {"id", "title", "status", "note"}
        self.notes: List[str] = []   # 记事板：模型经 remember 元工具写入的持久便签（跨步可见）
        self.replan_count = 0        # 整体重规划（replace/clear 后再建）次数
        self.step_iterations = 0     # 当前步已消耗的推理迭代（内层 ReAct 预算）
        self.step_failures: Dict[str, int] = {}  # step_id(str) -> 累计失败次数
        self.recent_calls: List[str] = []  # 本轮物理工具调用指纹（重复调用拦截）
        self.created_at = time.time()
        self._next_id = 1

    # ---------- 查询 ----------
    def current_step(self) -> Optional[Dict[str, Any]]:
        """当前步 = 第一个 doing；没有 doing 则将第一个 pending 提升为 doing。"""
        for step in self.plan:
            if step["status"] == STEP_DOING:
                return step
        for step in self.plan:
            if step["status"] == STEP_PENDING:
                step["status"] = STEP_DOING
                return step
        return None

    def has_open_steps(self) -> bool:
        return any(s["status"] in (STEP_PENDING, STEP_DOING) for s in self.plan)

    def all_done(self) -> bool:
        return bool(self.plan) and not self.has_open_steps()

    # ---------- 结构化操作（模型经 update_plan 元工具进入） ----------
    def _new_step(self, title: str) -> Dict[str, Any]:
        step = {"id": self._next_id, "title": title, "status": STEP_PENDING, "note": ""}
        self._next_id += 1
        return step

    @staticmethod
    def _valid_title(title: Any) -> bool:
        return isinstance(title, str) and 0 < len(title.strip()) <= MAX_STEP_TITLE_CHARS

    def apply_op(self, args: Dict[str, Any]) -> Tuple[bool, str]:
        """
        校验并应用一次计划操作。返回 (是否成功, 反馈文本)。
        失败反馈会作为 tool_response 喂回模型触发自纠——失败时计划保持原样。
        """
        op = args.get("op")

        if op == "replace":
            steps = args.get("steps")
            if not isinstance(steps, list) or not steps:
                return False, "Invalid op: 'replace' requires non-empty 'steps' (list of strings)."
            if len(steps) > MAX_PLAN_STEPS:
                return False, f"Invalid op: plan cannot exceed {MAX_PLAN_STEPS} steps. Merge related steps."
            if not all(self._valid_title(t) for t in steps):
                return False, (f"Invalid op: every step must be a non-empty string "
                               f"within {MAX_STEP_TITLE_CHARS} chars.")
            self.plan = [self._new_step(t.strip()) for t in steps]
            self.step_iterations = 0
            return True, "Plan replaced.\n" + self.render_plan()

        if op == "add":
            title = args.get("title")
            if not self._valid_title(title):
                return False, "Invalid op: 'add' requires a non-empty 'title' string."
            if len(self.plan) >= MAX_PLAN_STEPS:
                return False, f"Invalid op: plan already has {MAX_PLAN_STEPS} steps (hard limit)."
            step = self._new_step(title.strip())
            position = args.get("position")
            if isinstance(position, int) and 0 <= position <= len(self.plan):
                self.plan.insert(position, step)
            else:
                self.plan.append(step)
            return True, f"Step {step['id']} added.\n" + self.render_plan()

        if op == "remove":
            step_id = args.get("step_id")
            for i, step in enumerate(self.plan):
                if step["id"] == step_id:
                    if step["status"] == STEP_DOING:
                        self.step_iterations = 0
                    self.plan.pop(i)
                    return True, f"Step {step_id} removed.\n" + self.render_plan()
            return False, f"Invalid op: no step with id={step_id!r}. Check the plan listing."

        if op == "reorder":
            order = args.get("order")
            ids = [s["id"] for s in self.plan]
            if not isinstance(order, list) or sorted(order) != sorted(ids):
                return False, (f"Invalid op: 'order' must be a permutation of current step ids {ids}.")
            by_id = {s["id"]: s for s in self.plan}
            self.plan = [by_id[i] for i in order]
            return True, "Plan reordered.\n" + self.render_plan()

        if op == "clear":
            self.plan = []
            self.step_iterations = 0
            return True, "Plan cleared. Answer the user directly, or build a new plan with op='replace'."

        return False, ("Invalid op: 'op' must be one of "
                       "replace / add / remove / reorder / clear.")

    def complete_current(self, result: str = "") -> Tuple[bool, str]:
        """当前步标记完成，推进到下一步；result 为一句话结果摘要（记入计划便于回看）。"""
        step = self.current_step()
        if step is None:
            return False, "No open step to complete. Update the plan or answer the user."
        step["status"] = STEP_DONE
        step["note"] = (result or "").strip()[:MAX_STEP_TITLE_CHARS]
        self.step_iterations = 0
        nxt = self.current_step()
        if nxt is not None:
            return True, f"Step {step['id']} done. Now on step {nxt['id']}: {nxt['title']}"
        return True, f"Step {step['id']} done. All steps finished — give the user the final answer now."

    def fail_current(self, note: str = "") -> Optional[Dict[str, Any]]:
        """
        当前步标记失败（由护栏代码调用），返回失败的步。
        失败计数按标题（而非 id）累计：模型「删掉又原样加回」同一步会拿到新 id，
        以标题为键才能识破这种重试循环。
        """
        step = self.current_step()
        if step is None:
            return None
        step["status"] = STEP_FAILED
        step["note"] = note[:MAX_STEP_TITLE_CHARS]
        key = step["title"].strip().lower()
        self.step_failures[key] = self.step_failures.get(key, 0) + 1
        self.step_iterations = 0
        return step

    def skip_current(self, note: str = "") -> Optional[Dict[str, Any]]:
        """当前步降级跳过（带着现有信息继续）。"""
        step = self.current_step()
        if step is None:
            return None
        step["status"] = STEP_SKIPPED
        step["note"] = note[:MAX_STEP_TITLE_CHARS]
        self.step_iterations = 0
        return step

    # ---------- 记事板（模型经 remember 元工具进入） ----------
    def remember(self, note: str) -> Tuple[bool, str]:
        """
        把一条短便签写入记事板。返回 (是否成功, 反馈文本)。
        有界护栏：单条截到 MAX_NOTE_CHARS；精确去重（不重复记同一句）；
        超过 MAX_NOTES 时 FIFO 淘汰最旧一条——永不因"记事板满"回绝模型
        （回绝一个弱模型只会打乱它的注意力，近因便签更可能有用）。
        """
        note = (note or "").strip()
        if not note:
            return False, "Invalid note: 'note' must be a non-empty string (a concrete fact/value to reuse later)."
        note = note[:MAX_NOTE_CHARS]
        if note in self.notes:
            return True, "Already on your scratchpad — no need to save it again.\n" + self.render_notes()
        self.notes.append(note)
        if len(self.notes) > MAX_NOTES:
            self.notes.pop(0)  # FIFO：丢最旧的一条，保持有界
        return True, "Saved to scratchpad.\n" + self.render_notes()

    # ---------- 上下文渲染 ----------
    def render_notes(self) -> str:
        if not self.notes:
            return "(scratchpad is empty)"
        return "\n".join(f"  - {n}" for n in self.notes)

    def render_plan(self) -> str:
        if not self.plan:
            return "(plan is empty)"
        lines = []
        for step in self.plan:
            mark = _STATUS_MARKS.get(step["status"], "?")
            note = f"  — {step['note']}" if step.get("note") else ""
            cursor = "  <- current step" if step["status"] == STEP_DOING else ""
            lines.append(f"  {mark} [{step['id']}] {step['title']}{note}{cursor}")
        return "\n".join(lines)

    def render(self, react_max: int, replan_max: int) -> str:
        """渲染为 prompt 尾部的任务状态块（可变区，必须保持在整个上下文最尾部）。"""
        # 记事板仅在非空时渲染，保持任务块精简（空板时靠 remember 工具说明 + 步界提醒发现）
        notes_block = (f"Scratchpad (values you saved — reuse these, do not re-derive):\n"
                       f"{self.render_notes()}\n") if self.notes else ""
        return (
            "[Current task state | system-maintained; modifiable only via the "
            "update_plan / complete_step / remember tools]\n"
            f"Goal: {self.goal}\n"
            f"Plan:\n{self.render_plan()}\n"
            f"{notes_block}"
            f"Budget: current-step iterations {self.step_iterations}/{react_max}; "
            f"re-plans {self.replan_count}/{replan_max}\n"
            "Rules: focus on completing the current step; call complete_step as soon as it is "
            "done; the moment a step yields a value you'll need later (a url, id, path, number), "
            "save it with remember; after all steps are done, output the final answer to the user "
            "directly (plain text, no more tool calls)."
        )

    # ---------- 序列化（/task 接口、挂起恢复、冷启动） ----------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "plan": [dict(s) for s in self.plan],
            "notes": list(self.notes),
            "replan_count": self.replan_count,
            "step_iterations": self.step_iterations,
            "step_failures": dict(self.step_failures),
            "created_at": self.created_at,
            "next_id": self._next_id,
        }

    @classmethod
    def from_snapshot(cls, data: Dict[str, Any]) -> "TaskState":
        task = cls(goal=str(data.get("goal", "")))
        task.plan = [dict(s) for s in data.get("plan", []) if isinstance(s, dict)]
        task.notes = [str(n) for n in (data.get("notes") or []) if str(n).strip()][:MAX_NOTES]
        task.replan_count = int(data.get("replan_count", 0))
        task.step_iterations = int(data.get("step_iterations", 0))
        task.step_failures = {str(k): int(v) for k, v in (data.get("step_failures") or {}).items()}
        task.created_at = float(data.get("created_at", time.time()))
        task._next_id = int(data.get("next_id", 1 + max((s.get("id", 0) for s in task.plan), default=0)))
        return task


def call_fingerprint(name: str, arguments: Dict[str, Any]) -> str:
    """物理工具调用指纹：同名同参的重复调用拦截（死循环最常见形态）。"""
    try:
        args_key = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        args_key = str(arguments)
    return f"{name}:{args_key}"
