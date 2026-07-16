import SwiftUI

/// 单条消息渲染：用户右侧气泡 / 助手左侧气泡（含边界）/ 系统居中灰条 / 工具折叠行。
/// 菜单直接挂在气泡上（挂整行会让 iOS 长按预览高亮整排、超出内容边界）。
/// 文本选中只在 iOS 开（macOS 上可选文本的系统菜单会吃掉自定义右键菜单）；
/// macOS 的复制走悬停按钮 + 右键菜单。
struct MessageView: View {
    let message: ChatMessage
    @EnvironmentObject var store: AgentStore
    #if os(macOS)
    @State private var hovering = false
    #endif

    var body: some View {
        switch message.kind {
        case .user:
            userView
        case .assistant:
            assistantView
        case .system:
            Text(message.text)
                .font(.caption)
                .foregroundStyle(.secondary)
                .selectableOnIOS()
                .padding(8)
                .frame(maxWidth: .infinity)
                .background(Color.primary.opacity(0.04))
                .clipShape(RoundedRectangle(cornerRadius: 8))
        case .tool:
            // 渲染格（R3）：认识的 hint 走结构化视图，其余保持灰框（fail-open，与 web
            // 的 renderByHint 同一决策表）。灰框照旧收在 DisclosureGroup 里。
            if !message.citations.isEmpty {
                CitationsView(items: message.citations,
                              label: message.toolLabel ?? L("工具"))
            } else {
                DisclosureGroup {
                    ScrollView(.horizontal, showsIndicators: false) {
                        Text(message.toolDetail ?? "")
                            .font(.system(.caption, design: .monospaced))
                            .selectableOnIOS()
                            .padding(8)
                    }
                    .background(Color.primary.opacity(0.05))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                } label: {
                    Label(message.toolLabel ?? L("工具"), systemImage: "wrench.and.screwdriver")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: - 用户气泡

    private var userView: some View {
        HStack {
            Spacer(minLength: 48)
            Text(message.text)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(Color.accentColor.opacity(0.16))
                .clipShape(RoundedRectangle(cornerRadius: 14))
                .contextMenu {
                    Button {
                        Self.copy(message.text)
                    } label: {
                        Label(L("复制原文"), systemImage: "doc.on.doc")
                    }
                    // 分支操作只留在助手回复上（同一轮的分支语义，两处入口多余）
                    if isLastUser, !store.isGenerating {
                        Button {
                            store.editLastTurn()
                        } label: {
                            Label(L("编辑并重发（回退这一轮）"), systemImage: "pencil.and.outline")
                        }
                    }
                }
        }
    }

    private var isLastUser: Bool {
        message.id == store.messages.last(where: { $0.kind == .user })?.id
    }

    // MARK: - 助手气泡

    private var isLastAssistant: Bool {
        message.id == store.messages.last(where: { $0.kind == .assistant })?.id
    }

    private var assistantView: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 6) {
                if let thoughts = message.thoughts, !thoughts.isEmpty {
                    DisclosureGroup {
                        Text(thoughts)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .selectableOnIOS()
                    } label: {
                        Label(L("思考过程"), systemImage: "brain")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                MarkdownText(text: message.text)
                if let meta = message.meta {
                    Text(meta)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.primary.opacity(0.045))
            .clipShape(RoundedRectangle(cornerRadius: 14))
            .overlay(alignment: .topTrailing) {
                #if os(macOS)
                if hovering {
                    Button {
                        Self.copy(message.text)
                    } label: {
                        Image(systemName: "doc.on.doc")
                            .font(.caption)
                    }
                    .buttonStyle(.borderless)
                    .padding(8)
                    .help(L("复制回复（markdown 原文）"))
                }
                #endif
            }
            .contextMenu {
                Button {
                    Self.copy(message.text)
                } label: {
                    Label(L("复制回复"), systemImage: "doc.on.doc")
                }
                if let index = message.turnIndex {
                    Button {
                        store.branch(fromTurnIndex: index)
                    } label: {
                        Label(L("从这一轮分支出新会话"), systemImage: "arrow.triangle.branch")
                    }
                }
                if isLastAssistant, !store.isGenerating {
                    Button {
                        store.regenerateLastTurn()
                    } label: {
                        Label(L("重新生成回答"), systemImage: "arrow.clockwise")
                    }
                }
            }
            #if os(macOS)
            .onHover { hovering = $0 }
            #endif
            Spacer(minLength: 36)
        }
    }

    // MARK: - 剪贴板

    static func copy(_ text: String) {
        #if os(macOS)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        #else
        UIPasteboard.general.string = text
        #endif
    }
}

extension View {
    /// 文本选中仅 iOS 开启：macOS 的可选文本会让系统文本菜单抢掉自定义右键菜单。
    @ViewBuilder
    func selectableOnIOS() -> some View {
        #if os(iOS)
        self.textSelection(.enabled)
        #else
        self
        #endif
    }
}

/// 渲染格 citations（R3 试点，R5-7 落地）：与 web 的 `Citations` 同一信息面——
/// 来源 + 分数（等宽数字右对齐）+ 摘录（限三行）+ 路径。默认展开（引用是结论的
/// 依据，折叠等于白做）；标题行保留工具名，与灰框的心智一致。
struct CitationsView: View {
    let items: [CitationItem]
    let label: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(label, systemImage: "text.quote")
                .font(.caption)
                .foregroundStyle(.secondary)
            ForEach(Array(items.enumerated()), id: \.offset) { _, c in
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Image(systemName: "doc.text")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        Text(c.source)
                            .font(.caption.weight(.medium))
                            .lineLimit(1)
                        Spacer(minLength: 8)
                        if let score = c.score {
                            Text(String(format: "%.2f", score))
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                    }
                    if let snippet = c.snippet, !snippet.isEmpty {
                        Text(snippet)
                            .font(.caption)
                            .foregroundStyle(.primary.opacity(0.8))
                            .lineLimit(3)
                    }
                    if let path = c.path, !path.isEmpty {
                        Text(path)
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary.opacity(0.7))
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.primary.opacity(0.04))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            }
        }
    }
}
