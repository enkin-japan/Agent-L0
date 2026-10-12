import SwiftUI

/// 一次序列化生成摘要和全文，保证展开审阅与摘要使用同一份待执行参数。
struct ApprovalArguments {
    let fullText: String
    var summary: String { fullText.count > 500 ? String(fullText.prefix(500)) + "…" : fullText }

    init(_ arguments: [String: JSONValue]) {
        fullText = arguments.compact(max: Int.max)
    }
}

/// 敏感工具审批卡片：账本 awaiting_approval 时出现，决议受理后由事件流续跑。
struct ApprovalCard: View {
    let tools: [PendingTool]
    let onDecision: (Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(L("以下敏感操作等待你的授权"), systemImage: "exclamationmark.shield")
                .font(.headline)
                .foregroundStyle(Theme.warning)
            ForEach(Array(tools.enumerated()), id: \.offset) { _, tool in
                VStack(alignment: .leading, spacing: 4) {
                    Text(tool.name)
                        .font(.system(.callout, design: .monospaced))
                        .bold()
                    if let args = tool.arguments, !args.isEmpty {
                        let presentation = ApprovalArguments(args)
                        Text(presentation.summary)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                        if presentation.fullText.count > 500 {
                            DisclosureGroup("完整参数") {
                                Text(presentation.fullText)
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                    }
                }
                .padding(Theme.s2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.wash)
                .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
            }
            HStack {
                Button(role: .destructive) {
                    onDecision(false)
                } label: {
                    Label(L("拒绝"), systemImage: "xmark")
                }
                Spacer()
                Button {
                    onDecision(true)
                } label: {
                    Label(L("批准执行"), systemImage: "checkmark")
                }
                .buttonStyle(.borderedProminent)
                .tint(Theme.accent)
            }
        }
        .padding(Theme.s4)
        .background(Theme.warning.opacity(0.08))
        .overlay(RoundedRectangle(cornerRadius: Theme.radiusBubble)
            .strokeBorder(Theme.warning.opacity(0.45), lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusBubble))
    }
}
