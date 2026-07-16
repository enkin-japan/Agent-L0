#if os(iOS)
import ActivityKit
import Foundation

/// Live Activity 契约（与后端 framework/push/dispatcher.py 的 content-state 字段一一对应，
/// 键名即线上 JSON——改任何一边都要两边同步 + 真机回归）。
/// attributes-type = "AgentActivityAttributes"（push-to-start 按此名实例化）。
struct AgentActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// 当前阶段名（工具短标签 / 计划当前步标题 / 收尾语）
        var stage: String
        /// running | awaiting_approval | suspended | ok | fail | warning | cancelled
        var status: String
        /// 计划进度：已完成步数 / 总步数（无计划时 0/0，UI 隐藏进度条）
        var done: Int
        var total: Int
        /// 计划窗口（≤6 步）：t=标题，s=pending|doing|done|failed
        var steps: [Step]?
        var detail: String?

        struct Step: Codable, Hashable {
            var t: String
            var s: String
        }
    }

    var sessionId: String
    var turnId: String
    var title: String
}
#endif
