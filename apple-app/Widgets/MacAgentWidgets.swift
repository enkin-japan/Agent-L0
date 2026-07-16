import ActivityKit
import SwiftUI
import WidgetKit

/// Widget 扩展入口：只承载 Live Activity（灵动岛 + 锁屏实时卡）。
/// 数据流：后端 dispatcher 经 APNs liveactivity 推送 ContentState；本扩展纯渲染。
@main
struct MacAgentWidgets: WidgetBundle {
    var body: some Widget {
        AgentLiveActivity()
    }
}

// MARK: - 状态视觉映射

private func statusColor(_ s: String) -> Color {
    switch s {
    case "running": return .blue
    case "awaiting_approval": return .orange
    case "suspended": return .gray
    case "ok": return .green
    case "fail": return .red
    case "warning": return .yellow
    default: return .secondary
    }
}

private func statusIcon(_ s: String) -> String {
    switch s {
    case "running": return "gearshape.2.fill"
    case "awaiting_approval": return "lock.shield.fill"
    case "suspended": return "pause.circle.fill"
    case "ok": return "checkmark.circle.fill"
    case "fail": return "xmark.circle.fill"
    case "warning": return "exclamationmark.triangle.fill"
    default: return "circle"
    }
}

private func stepIcon(_ s: String) -> (name: String, color: Color) {
    switch s {
    case "done": return ("checkmark.circle.fill", .green)
    case "doing": return ("arrow.triangle.2.circlepath", .blue)
    case "failed": return ("xmark.circle.fill", .red)
    default: return ("circle", .secondary)
    }
}

// MARK: - Live Activity

struct AgentLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: AgentActivityAttributes.self) { context in
            LockScreenView(context: context)
                .activityBackgroundTint(Color.black.opacity(0.55))
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    HStack(spacing: 6) {
                        Image(systemName: "brain")
                            .foregroundStyle(.purple)
                        Text(context.attributes.title)
                            .font(.caption.bold())
                            .lineLimit(1)
                    }
                    .padding(.leading, 4)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    ProgressBadge(state: context.state)
                        .padding(.trailing, 4)
                }
                DynamicIslandExpandedRegion(.center) {
                    HStack(spacing: 5) {
                        Image(systemName: statusIcon(context.state.status))
                            .foregroundStyle(statusColor(context.state.status))
                        Text(context.state.stage)
                            .font(.footnote)
                            .lineLimit(1)
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    ExpandedBottom(context: context)
                }
            } compactLeading: {
                Image(systemName: "brain")
                    .foregroundStyle(context.state.status == "awaiting_approval"
                                     ? .orange : .purple)
            } compactTrailing: {
                CompactTrailing(state: context.state)
            } minimal: {
                Image(systemName: statusIcon(context.state.status))
                    .foregroundStyle(statusColor(context.state.status))
            }
            .widgetURL(URL(string: "macagent://session/\(context.attributes.sessionId)"))
            .keylineTint(.purple)
        }
    }
}

/// 紧凑右侧：等待授权亮锁形；有计划显示 done/total；否则转菊花语义的图标。
private struct CompactTrailing: View {
    let state: AgentActivityAttributes.ContentState

    var body: some View {
        if state.status == "awaiting_approval" {
            Image(systemName: "lock.shield.fill").foregroundStyle(.orange)
        } else if state.total > 0 {
            Text("\(state.done)/\(state.total)")
                .font(.caption2.monospacedDigit().bold())
                .foregroundStyle(statusColor(state.status))
        } else {
            Image(systemName: statusIcon(state.status))
                .foregroundStyle(statusColor(state.status))
        }
    }
}

private struct ProgressBadge: View {
    let state: AgentActivityAttributes.ContentState

    var body: some View {
        if state.total > 0 {
            Text("\(state.done)/\(state.total)")
                .font(.caption.monospacedDigit().bold())
                .foregroundStyle(statusColor(state.status))
        } else {
            Image(systemName: statusIcon(state.status))
                .foregroundStyle(statusColor(state.status))
        }
    }
}

/// 展开面底部：计划步骤（≤6，后端已取窗）+ 等待授权时的互动行。
/// 安全设计：岛上只有"拒绝"（免解锁的安全方向）；"允许"必须进 App/通知按钮（要解锁）。
private struct ExpandedBottom: View {
    let context: ActivityViewContext<AgentActivityAttributes>

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let steps = context.state.steps, !steps.isEmpty {
                StepsList(steps: steps, compact: true)
            }
            if context.state.status == "awaiting_approval" {
                HStack(spacing: 10) {
                    Button(intent: DenyApprovalIntent(
                        sessionId: context.attributes.sessionId,
                        turnId: context.attributes.turnId)) {
                        Label(L("拒绝"), systemImage: "hand.raised.fill")
                            .font(.footnote.bold())
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.red)
                    Text(L("轻点卡片进 App 允许"))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .padding(.top, 2)
            }
        }
        .padding(.horizontal, 4)
    }
}

private struct StepsList: View {
    let steps: [AgentActivityAttributes.ContentState.Step]
    var compact = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(Array(steps.enumerated()), id: \.offset) { _, step in
                let icon = stepIcon(step.s)
                HStack(spacing: 5) {
                    Image(systemName: icon.name)
                        .font(.caption2)
                        .foregroundStyle(icon.color)
                    Text(step.t)
                        .font(.caption2)
                        .lineLimit(1)
                        .foregroundStyle(step.s == "doing" ? .primary : .secondary)
                }
            }
        }
    }
}

/// 锁屏/StandBy 卡片：标题行 + 阶段 + 进度条 + 步骤窗 + 授权互动行。
private struct LockScreenView: View {
    let context: ActivityViewContext<AgentActivityAttributes>

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "brain").foregroundStyle(.purple)
                Text(context.attributes.title)
                    .font(.subheadline.bold())
                    .lineLimit(1)
                Spacer()
                ProgressBadge(state: context.state)
            }
            HStack(spacing: 5) {
                Image(systemName: statusIcon(context.state.status))
                    .font(.footnote)
                    .foregroundStyle(statusColor(context.state.status))
                Text(context.state.stage)
                    .font(.footnote)
                    .lineLimit(1)
            }
            if context.state.total > 0 {
                ProgressView(value: Double(context.state.done),
                             total: Double(max(context.state.total, 1)))
                    .tint(statusColor(context.state.status))
            }
            if let steps = context.state.steps, !steps.isEmpty,
               context.state.status != "ok" {
                StepsList(steps: steps)
            }
            if context.state.status == "awaiting_approval" {
                HStack(spacing: 10) {
                    Button(intent: DenyApprovalIntent(
                        sessionId: context.attributes.sessionId,
                        turnId: context.attributes.turnId)) {
                        Label(L("拒绝"), systemImage: "hand.raised.fill")
                            .font(.footnote.bold())
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.red)
                    Text(L("轻点卡片进 App 允许"))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
        .foregroundStyle(.white)
    }
}
