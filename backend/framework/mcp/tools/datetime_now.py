"""get_current_time：返回当前精确时间（日期 + 时:分:秒 + 星期 + 时区）。

系统已被动注入"当前日期"（只到天，KV 缓存友好），足以处理"明天/最近/今年"这类相对时间；
当模型确实需要精确到时分秒或时区时（如"现在几点"、计时、给输出打时间戳），再主动调用本工具。
无参数、无副作用、免审批。
"""
from datetime import datetime

from ..base import BaseTool


class CurrentTimeTool(BaseTool):
    name = "get_current_time"
    description = (
        "Return the current local date-time precise to the second, with weekday and timezone. "
        "The system prompt already gives you today's date at day granularity, so call this ONLY "
        "when you need the exact clock time (hours/minutes/seconds) or the timezone — e.g. 'what "
        "time is it now', measuring elapsed time, or timestamping output. Takes no arguments."
    )
    requires_approval = False
    is_concurrency_safe = True  # 只读、无副作用，可与其他只读调用并发
    parameters = {"properties": {}, "required": []}

    def execute(self) -> str:
        now = datetime.now().astimezone()
        weekday = ["Monday", "Tuesday", "Wednesday", "Thursday",
                   "Friday", "Saturday", "Sunday"][now.weekday()]
        return now.strftime(f"%Y-%m-%d {weekday} %H:%M:%S %Z (UTC%z)")
