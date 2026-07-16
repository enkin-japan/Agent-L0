import SwiftUI

/// 敏感工具审批卡片：pending_approval 终态时出现，决议走 /approval/stream 续跑。
struct ApprovalCard: View {
    let tools: [PendingTool]
    let onDecision: (Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(L("以下敏感操作等待你的授权"), systemImage: "exclamationmark.shield")
                .font(.headline)
            ForEach(Array(tools.enumerated()), id: \.offset) { _, tool in
                VStack(alignment: .leading, spacing: 4) {
                    Text(tool.name)
                        .font(.system(.callout, design: .monospaced))
                        .bold()
                    if let args = tool.arguments, !args.isEmpty {
                        Text(args.compact(max: 500))
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                }
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.primary.opacity(0.05))
                .clipShape(RoundedRectangle(cornerRadius: 8))
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
            }
        }
        .padding(14)
        .background(Color.orange.opacity(0.1))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.orange.opacity(0.5)))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }
}
