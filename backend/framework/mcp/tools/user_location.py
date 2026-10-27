"""get_location：按需获取用户当前位置——来自发消息的那台设备，不是后端主机。

位置是半易变事实（人会动）：不进静态环境块（那里只放稳定事实，时区粗锚点已覆盖
国家级），精确值走本工具按需查——与「日期被动注入 + get_current_time 按需」完全对称。

数据源是**客户端设备**（CoreLocation）：坐在 Mac 前发问 → Mac 的 Wi-Fi 定位；
在外用 iPhone 发问 → iPhone 的 GPS。首次调用会在用户设备上触发系统定位授权弹窗
（TCC 一次性许诺）；拒绝 / 无客户端附着 / 超时都明确降级为「不可用」，
模型退回时区粗推断或直接问用户。坐标截到 3 位小数（约百米级）——天气/附近类
任务足够，减少精确轨迹进上下文。
"""
from ..base import BaseTool
from ...agents import runtime
from ...core import client_query

# 通道等待上限：首调可能要等用户响应系统授权弹窗，给足人类反应时间
_WAIT_SECONDS = 25.0

_FALLBACK_HINT = "可退回环境信息里的时区做粗略推断，或直接询问用户所在地。"


class GetLocationTool(BaseTool):
    name = "get_location"
    description = (
        "Get the user's current physical location (place name + coarse coordinates), measured "
        "by the device the user is chatting from (their Mac or iPhone), NOT the server. Use it "
        "for weather, nearby places, local recommendations, or any task whose answer depends on "
        "where the user is right now. The first call may show a system permission dialog on the "
        "user's device, so the result can take a few seconds. If it returns unavailable "
        "(no client connected / permission denied / timeout), fall back to the timezone in your "
        "environment info or ask the user. Takes no arguments."
    )
    requires_approval = False        # 同意与否由设备端系统定位授权弹窗承担（TCC）
    effect = "read"     # 审批策略分类：只读取，进程外无副作用
    is_concurrency_safe = True       # 只读、无副作用
    timeout_seconds = 40             # 编排层等待上限 = 通道 25s + 余量
    parameters = {"properties": {}, "required": []}

    def execute(self) -> str:
        session = runtime.get_current_session()
        if session is None:
            return f"定位不可用：无会话上下文。{_FALLBACK_HINT}"
        reply = client_query.request(session, "location_request", timeout=_WAIT_SECONDS)
        if reply is None:
            return (f"定位不可用：没有在线客户端应答（用户设备离线、未附着会话或未响应授权）。"
                    f"{_FALLBACK_HINT}")
        if reply.get("status") != "ok":
            reason = reply.get("error") or "客户端拒绝授权或定位失败"
            return f"定位不可用：{reason}。{_FALLBACK_HINT}"
        parts = []
        place = (reply.get("place") or "").strip()
        if place:
            parts.append(place)
        lat, lon = reply.get("latitude"), reply.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            coord = f"lat {round(float(lat), 3)}, lon {round(float(lon), 3)}"
            acc = reply.get("accuracy_m")
            if isinstance(acc, (int, float)) and acc > 0:
                coord += f" (±{int(acc)}m)"
            parts.append(coord)
        if not parts:
            return f"定位不可用：客户端应答缺少坐标。{_FALLBACK_HINT}"
        return "用户当前位置（来自其正在使用的设备，刚刚测得）：" + "；".join(parts)
