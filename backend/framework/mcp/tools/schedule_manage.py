"""schedule_*：定时任务的模型侧管理工具组（创建/修改/查看/删除走对话协商）。

安全设计：create/update/delete 需审批——审批卡片即「在聊天界面展示任务细节供用户
确认」，一石二鸟；且无人值守的定时运行会自动拒绝审批（schedule/runner.py），
故定时任务无法在无人值守时创建/修改别的定时任务，阻断注入内容自我增殖。

仓库句柄经 framework.schedule.get_store()（lifespan 先于 MCPRegistry 注入，
对齐 agents/runtime.py 的进程全局惯例）。运行结果落在专属会话 schedule_{id}。
"""
import time

from ..base import BaseTool
from ...schedule import store as schedule_store
from ...schedule.store import ScheduleSpecError, describe_spec

_SPEC_SCHEMA = {
    "type": "object",
    "description": (
        'When to run. One of: {"type": "interval", "minutes": 30} / '
        '{"type": "daily", "at": "08:00"} / {"type": "weekly", "weekday": 0, "at": "09:00"} / '
        '{"type": "once", "at": "2026-07-09 15:00"}. '
        "Times are the user's local time, 24h format."
    ),
    "properties": {
        "type": {"type": "string", "enum": ["interval", "daily", "weekly", "once"]},
        "minutes": {"type": "integer", "description": "interval only: run every N minutes."},
        "at": {"type": "string", "description": 'daily/weekly: "HH:MM"; once: "YYYY-MM-DD HH:MM".'},
        "weekday": {"type": "integer", "description": "weekly only: 0=Monday .. 6=Sunday."},
    },
    "required": ["type"],
}

# D2-减法(类B):full 模式下 create/update 各渲染一份 _SPEC_SCHEMA,同述逐字节两遍;
# update 侧换短述指回 create;description 里的 weekday 括注同波删除(字段述逐字同义)。
# ⚠️ properties/required 是**同引用**——绝不要原地改 _SPEC_SCHEMA_UPDATE["properties"],
# 那会连带改掉 schedule_create;要分叉先深拷贝。
_SPEC_SCHEMA_UPDATE = {**_SPEC_SCHEMA, "description": (
    "Same format as schedule_create's spec. Times are the user's local time, 24h format.")}


def _fmt_ts(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def _fmt_task(t: dict) -> str:
    state = "enabled" if t.get("enabled") else "DISABLED"
    return (f"- {t['id']} \"{t['name']}\" [{state}] {describe_spec(t['spec'])}, "
            f"next run {_fmt_ts(t.get('next_run'))}\n  prompt: {t['prompt'][:120]}")


class ScheduleCreateTool(BaseTool):
    name = "schedule_create"
    description = (
        "Create a recurring or one-shot scheduled task: at the scheduled time the agent runs "
        "'prompt' automatically in a dedicated conversation the user can open later. Use when "
        "the user asks for something periodic ('every morning', 'each Friday') or at a future "
        "time ('tomorrow at 3pm'). Requires the user's approval — the approval dialog shows "
        "them the exact name/prompt/schedule, so fill all fields carefully and completely."
    )
    requires_approval = True     # 审批卡片=任务细节确认；防无人值守/注入内容排任务
    # 钉死（build 78）：这张卡是文档化的安全闸（见模块 docstring），写档 "auto" 不得放行
    approval_pinned = True
    is_concurrency_safe = False
    is_read_only = False

    parameters = {
        "properties": {
            "name": {"type": "string", "description": "Short human-readable title, in the user's language."},
            "prompt": {"type": "string",
                       "description": "Complete standalone instruction to execute on each run "
                                      "(the run has no other context — spell everything out)."},
            "spec": _SPEC_SCHEMA,
        },
        "required": ["name", "prompt", "spec"],
    }

    def execute(self, name: str, prompt: str, spec: dict) -> str:
        store = schedule_store.get_store()
        if store is None:
            return "System Error: schedule store not initialized."
        from ...agents import runtime
        session = runtime.get_current_session()
        created_by = getattr(session, "session_id", "") if session else ""
        try:
            task = store.create(name, prompt, spec, created_by=created_by)
        except ScheduleSpecError as e:
            return f"Schedule spec error (fix the arguments and retry): {e}"
        return (f"Scheduled task created: {task['id']} \"{task['name']}\" — "
                f"{describe_spec(task['spec'])}, first run {_fmt_ts(task['next_run'])}. "
                "Results will appear in a dedicated conversation. Tell the user it is set up; "
                "they can change or remove it anytime by asking you, or from the Schedules panel.")


class ScheduleUpdateTool(BaseTool):
    name = "schedule_update"
    description = (
        "Modify an existing scheduled task: change its name, prompt, schedule spec, or "
        "enable/disable it. Provide only the fields to change. Call schedule_list first if "
        "you are not sure of the schedule_id. Requires the user's approval."
    )
    requires_approval = True
    # 钉死（build 78）：改 prompt/时间同样是注入内容自我增殖的入口，卡片即确认，"auto" 不放行
    approval_pinned = True
    is_concurrency_safe = False
    is_read_only = False

    parameters = {
        "properties": {
            "schedule_id": {"type": "string", "description": "Target task id (from schedule_list)."},
            "name": {"type": "string", "description": "New title (optional)."},
            "prompt": {"type": "string", "description": "New run instruction (optional)."},
            "spec": _SPEC_SCHEMA_UPDATE,
            "enabled": {"type": "boolean", "description": "Enable (true) or pause (false) the task (optional)."},
        },
        "required": ["schedule_id"],
    }

    def execute(self, schedule_id: str, name: str = None, prompt: str = None,
                spec: dict = None, enabled: bool = None) -> str:
        store = schedule_store.get_store()
        if store is None:
            return "System Error: schedule store not initialized."
        try:
            task = store.update(schedule_id, name=name, prompt=prompt, spec=spec, enabled=enabled)
        except ScheduleSpecError as e:
            return f"Schedule spec error (fix the arguments and retry): {e}"
        if task is None:
            return (f"No scheduled task with id '{schedule_id}'. Call schedule_list to see "
                    "existing tasks and use an exact id.")
        return "Scheduled task updated:\n" + _fmt_task(task)


class ScheduleDeleteTool(BaseTool):
    name = "schedule_delete"
    description = (
        "Permanently delete a scheduled task and its run history (its past conversation is "
        "kept). To pause a task instead of deleting it, use schedule_update with "
        "enabled=false. Requires the user's approval."
    )
    requires_approval = True
    approval_pinned = True       # 同 create/update：审批卡即安全门，write=auto 不越过
    is_concurrency_safe = False
    is_read_only = False

    parameters = {
        "properties": {
            "schedule_id": {"type": "string", "description": "Target task id (from schedule_list)."},
        },
        "required": ["schedule_id"],
    }

    def execute(self, schedule_id: str) -> str:
        store = schedule_store.get_store()
        if store is None:
            return "System Error: schedule store not initialized."
        if store.delete(schedule_id):
            return f"Scheduled task {schedule_id} deleted."
        return (f"No scheduled task with id '{schedule_id}'. Call schedule_list to see "
                "existing tasks and use an exact id.")


class ScheduleListTool(BaseTool):
    name = "schedule_list"
    description = (
        "List all scheduled tasks with their ids, schedules, next run times, and prompts. "
        "Call this before schedule_update / schedule_delete, or when the user asks what "
        "is scheduled."
    )
    requires_approval = False
    effect = "read"     # 审批策略分类：只读取，进程外无副作用
    is_concurrency_safe = True
    is_read_only = True

    parameters = {"properties": {}, "required": []}

    def execute(self) -> str:
        store = schedule_store.get_store()
        if store is None:
            return "System Error: schedule store not initialized."
        tasks = store.list_tasks()
        if not tasks:
            return "No scheduled tasks exist yet."
        return f"{len(tasks)} scheduled task(s):\n" + "\n".join(_fmt_task(t) for t in tasks)
