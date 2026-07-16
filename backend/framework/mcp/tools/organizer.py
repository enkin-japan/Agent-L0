"""日程组织三件套（D2 第二批）：日历读/写 + 提醒事项，AppleScript 薄包装。

零第三方依赖（EventKit 需 pyobjc，不值得为此扩 bundle）。三个 TCC 面都在这里：
首次调用 macOS 会弹「MacAgent 想控制 Calendar/Reminders」授权，拒绝后 osascript
返回 -1743——错误信息里给出系统设置指引，别让用户对着静默失败猜。

日期传递纪律：**绝不把日期字符串塞进 AppleScript 字面量**——AppleScript 的
`date "…"` 解析跟随系统语言/区域设置，同一脚本换台机器就碎。做法：Python 侧用
strptime 校验并拆成年/月/日/时/分数字，经 argv 交付，脚本里对 `current date`
逐字段赋值（先 day=1 再 year/month/day，防「当前日 31 号设 2 月」的翻转）。
文本参数同样走 argv（desktop.py notify_user 同款），无 AppleScript 注入面。

写操作（calendar_add/reminder_add）恒审批：写的是用户的真实日历/提醒数据，
且会同步到全部 iCloud 设备。calendar_list 只读免审批。
"""
import subprocess
from datetime import datetime, timedelta

from ..base import BaseTool
from .desktop import _darwin_with

# osascript 对 Calendar/Reminders 的 Apple Event 首次要 TCC 授权；应用未启动时
# tell 会拉起它（首次可能要几秒），超时给足
_OSA_TIMEOUT = 60

_TCC_HINT = ("If this failed with error -1743 the user denied automation access — ask them to "
             "enable it in System Settings → Privacy & Security → Automation → MacAgent.")

# 对 current date 逐字段赋值的公共前奏：argv 1-5 = year month day hour minute
_SET_DATE = (
    "set d to current date\n"
    "set day of d to 1\n"
    "set year of d to (item 1 of argv as integer)\n"
    "set month of d to (item 2 of argv as integer)\n"
    "set day of d to (item 3 of argv as integer)\n"
    "set hours of d to (item 4 of argv as integer)\n"
    "set minutes of d to (item 5 of argv as integer)\n"
    "set seconds of d to 0\n"
)


def _parse_when(when: str) -> "datetime | None":
    """校验并解析 'YYYY-MM-DD HH:MM'（容忍 ISO 的 T 分隔）。"""
    try:
        return datetime.strptime(str(when).strip().replace("T", " "), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _date_argv(d: datetime) -> list:
    return [str(d.year), str(d.month), str(d.day), str(d.hour), str(d.minute)]


def _osascript(script: str, *argv: str) -> "subprocess.CompletedProcess":
    return subprocess.run(["osascript", "-e", script, *argv],
                          capture_output=True, text=True, timeout=_OSA_TIMEOUT)


class CalendarAddTool(BaseTool):
    name = "calendar_add"
    description = (
        "Create an event in the user's macOS Calendar (syncs to their devices). Provide the "
        "start time as 'YYYY-MM-DD HH:MM' (24h). Ask the user before guessing missing details "
        "like the date or duration."
    )
    requires_approval = True  # 写用户真实日历
    is_concurrency_safe = False
    is_read_only = False
    timeout_seconds = _OSA_TIMEOUT + 15

    parameters = {
        "properties": {
            "title": {"type": "string", "description": "Event title."},
            "when": {"type": "string", "description": "Start time, exactly 'YYYY-MM-DD HH:MM' (24h)."},
            "duration_minutes": {"type": "integer", "description": "Length in minutes. Default 60."},
            "calendar": {"type": "string", "description": "Calendar name. Omit = first calendar."},
        },
        "required": ["title", "when"],
    }

    def is_available(self) -> bool:
        return _darwin_with("osascript")

    def validate_arguments(self, arguments) -> "str | None":
        if not str(arguments.get("title", "")).strip():
            return "Invalid arguments: 'title' must be a non-empty string."
        if _parse_when(arguments.get("when", "")) is None:
            return ("Invalid arguments: 'when' must be exactly 'YYYY-MM-DD HH:MM' (24h), "
                    f"got {arguments.get('when')!r}.")
        return None

    def execute(self, title: str, when: str, duration_minutes: int = 60,
                calendar: str = None) -> str:
        start = _parse_when(when)
        if start is None:
            return f"System Error: 'when' must be 'YYYY-MM-DD HH:MM', got {when!r}."
        minutes = max(1, int(duration_minutes or 60))
        target = ('calendar named (item 7 of argv)' if calendar
                  else 'first calendar')
        script = (
            "on run argv\n" + _SET_DATE +
            "set d2 to d + (item 6 of argv as integer) * minutes\n"
            "tell application \"Calendar\"\n"
            f"  tell {target}\n"
            "    make new event with properties {summary:(item 8 of argv), start date:d, end date:d2}\n"
            "    return name of it\n"
            "  end tell\n"
            "end tell\n"
            "end run")
        argv = _date_argv(start) + [str(minutes), calendar or "-", str(title)]
        out = _osascript(script, *argv)
        if out.returncode != 0:
            return (f"System Error: Calendar refused: {out.stderr.strip() or out.returncode}. "
                    + _TCC_HINT)
        cal_name = out.stdout.strip() or (calendar or "default")
        return (f"Created event '{title}' on calendar '{cal_name}', "
                f"{start:%Y-%m-%d %H:%M} for {minutes} min.")


class CalendarListTool(BaseTool):
    name = "calendar_list"
    description = (
        "List the user's macOS Calendar events for a day or the next few days "
        "('what's on my schedule'). Read-only."
    )
    requires_approval = False
    is_concurrency_safe = True  # 只读
    timeout_seconds = _OSA_TIMEOUT + 15

    parameters = {
        "properties": {
            "date": {"type": "string", "description": "Start day 'YYYY-MM-DD'. Omit = today."},
            "days": {"type": "integer", "description": "How many days ahead to include (1-31). Default 1."},
        },
        "required": [],
    }

    def is_available(self) -> bool:
        return _darwin_with("osascript")

    def execute(self, date: str = None, days: int = 1) -> str:
        if date:
            start = _parse_when(f"{str(date).strip()} 00:00")
            if start is None:
                return f"System Error: 'date' must be 'YYYY-MM-DD', got {date!r}."
        else:
            start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        span = min(max(1, int(days or 1)), 31)
        end = start + timedelta(days=span)
        # whose 过滤在 Calendar 的 AppleScript 桥上是服务端求值，比逐事件遍历快得多
        script = (
            "on run argv\n" + _SET_DATE +
            "set d2 to d + (item 6 of argv as integer) * days\n"
            "set out to \"\"\n"
            "tell application \"Calendar\"\n"
            "  repeat with c in calendars\n"
            "    set evs to (every event of c whose start date ≥ d and start date < d2)\n"
            "    repeat with e in evs\n"
            "      set out to out & (start date of e as string) & \" | \" & (summary of e) "
            "& \" [\" & (name of c) & \"]\" & linefeed\n"
            "    end repeat\n"
            "  end repeat\n"
            "end tell\n"
            "return out\n"
            "end run")
        out = _osascript(script, *_date_argv(start), str(span))
        if out.returncode != 0:
            return (f"System Error: Calendar refused: {out.stderr.strip() or out.returncode}. "
                    + _TCC_HINT)
        body = out.stdout.strip()
        window = f"{start:%Y-%m-%d} +{span}d"
        if not body:
            return f"No calendar events between {window}."
        return f"Events ({window}):\n{body}"


class ReminderAddTool(BaseTool):
    name = "reminder_add"
    description = (
        "Create a to-do in the user's macOS Reminders (syncs to their devices), optionally with "
        "a due time 'YYYY-MM-DD HH:MM'. Use for 'remind me to …' requests."
    )
    requires_approval = True  # 写用户真实提醒数据
    is_concurrency_safe = False
    is_read_only = False
    timeout_seconds = _OSA_TIMEOUT + 15

    parameters = {
        "properties": {
            "title": {"type": "string", "description": "What to remind about."},
            "due": {"type": "string", "description": "Optional due time, exactly 'YYYY-MM-DD HH:MM' (24h)."},
            "list": {"type": "string", "description": "Reminders list name. Omit = default list."},
        },
        "required": ["title"],
    }

    def is_available(self) -> bool:
        return _darwin_with("osascript")

    def validate_arguments(self, arguments) -> "str | None":
        if not str(arguments.get("title", "")).strip():
            return "Invalid arguments: 'title' must be a non-empty string."
        due = arguments.get("due")
        if due and _parse_when(due) is None:
            return f"Invalid arguments: 'due' must be 'YYYY-MM-DD HH:MM' (24h), got {due!r}."
        return None

    def execute(self, title: str, due: str = None, list: str = None) -> str:
        target = 'list named (item 7 of argv)' if list else 'default list'
        if due:
            when = _parse_when(due)
            if when is None:
                return f"System Error: 'due' must be 'YYYY-MM-DD HH:MM', got {due!r}."
            props = "{name:(item 6 of argv), due date:d}"
            date_part = _SET_DATE
            argv = _date_argv(when) + [str(title), list or "-"]
        else:
            props = "{name:(item 1 of argv)}"
            date_part = ""
            target = 'list named (item 2 of argv)' if list else 'default list'
            argv = [str(title)] + ([list] if list else [])
        script = (
            "on run argv\n" + date_part +
            "tell application \"Reminders\"\n"
            f"  tell {target}\n"
            f"    make new reminder with properties {props}\n"
            "    return name of it\n"
            "  end tell\n"
            "end tell\n"
            "end run")
        out = _osascript(script, *argv)
        if out.returncode != 0:
            return (f"System Error: Reminders refused: {out.stderr.strip() or out.returncode}. "
                    + _TCC_HINT)
        list_name = out.stdout.strip() or (list or "default")
        due_part = f", due {due}" if due else ""
        return f"Created reminder '{title}' in list '{list_name}'{due_part}."
