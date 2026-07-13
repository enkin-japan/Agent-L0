"""iOS 推送层（APNs 直发，无第三方中转）。

架构定位（CHARTER 铁律 2）：通知是**账本的读者**——NotificationDispatcher 订阅
GlobalFeed / 会话 journal，把生命周期事件映射为 APNs 推送。不存在旁路事件源。

隐私边界：APNs 是纯出站 HTTPS（后端仅回环绑定不变，不开任何入站口）；
payload 内容受 per-device 内容分级约束（full / category / placeholder），
"推送即门铃，真相永远在账本"——丢通知不丢状态，回 app 走统一 /turn+/events 对账。

macOS 客户端不经此层（app 常驻订阅 feed 发本地通知，零出网）。
"""
from .apns import APNsClient, APNsDisabled
from .registry import PushRegistry
from .dispatcher import NotificationDispatcher

__all__ = ["APNsClient", "APNsDisabled", "PushRegistry", "NotificationDispatcher"]
