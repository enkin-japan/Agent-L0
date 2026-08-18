import SwiftUI

/// 记忆面板（只读）：可视化当前会话的有界记忆——上下文水位 vs 折叠阈值、
/// 已折叠进滚动摘要的轮次、被折叠原文是否沉淀进 RAG。
struct MemoryPanelView: View {
    @EnvironmentObject var store: AgentStore
    @Environment(\.dismiss) private var dismiss

    @State private var status: MemoryStatusResponse?
    @State private var failed = false

    var body: some View {
        NavigationStack {
            Group {
                if let status {
                    content(status)
                } else if failed {
                    ContentUnavailableView(L("加载失败"), systemImage: "exclamationmark.triangle")
                } else {
                    ProgressView()
                }
            }
            .navigationTitle(L("会话记忆"))
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(L("关闭")) { dismiss() }
                }
            }
        }
        .task {
            guard let sid = store.selectedSessionId else { failed = true; return }
            do {
                status = try await store.client?.memoryStatus(sessionId: sid)
            } catch {
                failed = true
            }
        }
        #if os(macOS)
        .frame(minWidth: 420, minHeight: 380)
        #endif
    }

    private func content(_ s: MemoryStatusResponse) -> some View {
        Form {
            Section(L("上下文水位")) {
                let tokens = s.lastPromptTokens ?? 0
                let high = max(s.highThreshold ?? 1, 1)
                LabeledContent(L("当前上下文"), value: s.loaded == true ? "\(tokens) tokens" : L("冷会话（KV 已释放）"))
                if s.loaded == true {
                    ProgressView(value: min(Double(tokens) / Double(high), 1.0)) {
                        EmptyView()
                    } currentValueLabel: {
                        Text("\(L("折叠阈值")) \(high) tokens")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            Section(L("滚动摘要")) {
                LabeledContent(L("全量历史"), value: "\(s.totalTurns ?? 0) \(L("轮"))")
                LabeledContent(L("已折叠"), value: "\(s.summarizedTurns ?? 0) \(L("轮"))")
                if s.hasSummary == true {
                    LabeledContent(L("摘要体量"), value: "\(s.summaryChars ?? 0) \(L("字符"))")
                } else {
                    LabeledContent(L("摘要"), value: L("尚未产生"))
                }
                LabeledContent(L("保留尾部"), value: "\(s.keepTailTurns ?? 0) \(L("轮不折叠"))")
            }
            Section(L("长期沉淀")) {
                // 「折叠原文入 RAG」这个说法自零 LLM 沉淀（波 F5）起就不准了：沉淀走
                // 自己的水位线，短会话（够不着折叠）同样入库。
                LabeledContent(L("原文入 RAG"),
                               value: s.sedimentToRag == true ? L("开启（可被检索召回）") : L("关闭"))
                LabeledContent(L("已沉淀"), value: "\(s.sedimentedTurns ?? 0) \(L("轮"))")
            }
        }
        .formStyle(.grouped)
    }
}
