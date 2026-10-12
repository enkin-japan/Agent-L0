import SwiftUI

/// 单条消息渲染：用户右侧气泡 / 助手左侧气泡（含边界）/ 系统居中灰条 / 工具折叠行。
/// 菜单直接挂在气泡上（挂整行会让 iOS 长按预览高亮整排、超出内容边界）。
/// 文本选中只在 iOS 开（macOS 上可选文本的系统菜单会吃掉自定义右键菜单）。
/// 主操作（复制/撤回重编/分支/重新生成）以气泡下方的小钮行为准（Claude AI 形态）；
/// 右键菜单保留同一组动作作为第二入口，后续还会往里放更多功能。
struct MessageView: View {
    let message: ChatMessage
    var auditOnly = false
    @EnvironmentObject var store: AgentStore
    #if os(macOS)
    @State private var hovering = false
    #endif

    /// 操作行可见性：macOS 上最后一条常显、旧消息悬停浮现（行始终占位，只变
    /// 透明度——绝不让布局跳动）；iOS 没有悬停，一律常显。
    private func actionRowOpacity(alwaysVisible: Bool) -> Double {
        #if os(macOS)
        return (alwaysVisible || hovering) ? 1 : 0
        #else
        return 1
        #endif
    }

    var body: some View {
        switch message.kind {
        case .user:
            userView
        case .assistant:
            assistantView
        case .system:
            SystemNoticeView(text: message.text)
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
                            .padding(Theme.s2)
                    }
                    .background(Theme.wash)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
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
            VStack(alignment: .trailing, spacing: Theme.s1) {
                Text(message.text)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(Theme.userBubble)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.radiusBubble))
                    .contextMenu {
                        Button {
                            Self.copy(message.text)
                        } label: {
                            Label(L("复制原文"), systemImage: "doc.on.doc")
                        }
                        // 分支操作只留在助手回复上（同一轮的分支语义，两处入口多余）
                        if store.canEditMessage(message, auditOnly: auditOnly) {
                            Button {
                                store.editLastTurn()
                            } label: {
                                Label(L("编辑并重发（撤销最近一次尝试）"), systemImage: "pencil.and.outline")
                            }
                        }
                    }
                // 操作钮随悬停隐显，时间戳常显（钮始终占位，时间戳不漂移）
                HStack(spacing: Theme.s1) {
                    HStack(spacing: Theme.s1) {
                        CopyActionButton(text: message.text, help: L("复制原文"))
                        if store.canEditMessage(message, auditOnly: auditOnly) {
                            MessageActionButton(icon: "pencil.and.outline",
                                                help: L("编辑并重发（撤销最近一次尝试）")) {
                                store.editLastTurn()
                            }
                        }
                    }
                    .opacity(actionRowOpacity(alwaysVisible: isLastUser || store.canEditMessage(message, auditOnly: auditOnly)))
                    if let ts = message.timestamp {
                        Text(Timestamps.messageLabel(ts))
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            #if os(macOS)
            .onHover { hovering = $0 }
            .animation(.easeInOut(duration: 0.12), value: hovering)
            #endif
        }
    }

    private var isLastUser: Bool {
        message.canModifyLatestAttempt(canonicalIndex: store.lastCanonicalIndex, auditOnly: auditOnly) &&
        message.id == store.messages.last(where: { $0.kind == .user })?.id
    }

    // MARK: - 助手气泡

    private var isLastAssistant: Bool {
        message.canModifyLatestAttempt(canonicalIndex: store.lastCanonicalIndex, auditOnly: auditOnly) &&
        message.id == store.messages.last(where: { $0.kind == .assistant })?.id
    }

    private var assistantView: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: Theme.s1) {
                assistantBubble
                // 操作钮随悬停隐显，时间戳常显（钮始终占位，时间戳不漂移）
                HStack(spacing: Theme.s1) {
                    HStack(spacing: Theme.s1) {
                        CopyActionButton(text: message.text, help: L("复制回复（markdown 原文）"))
                        if let index = message.turnIndex {
                            MessageActionButton(icon: "arrow.triangle.branch",
                                                help: L("从这一轮分支出新会话")) {
                                store.branch(fromTurnIndex: index)
                            }
                            .disabled(store.selectedSessionIsDeleting)
                        }
                        if isLastAssistant, !store.isGenerating, !store.selectedSessionIsDeleting {
                            MessageActionButton(icon: "arrow.clockwise",
                                                help: store.retryActionTitle) {
                                store.regenerateLastTurn()
                            }
                        }
                    }
                    .opacity(actionRowOpacity(alwaysVisible: isLastAssistant))
                    if let ts = message.timestamp {
                        Text(Timestamps.messageLabel(ts))
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            #if os(macOS)
            .onHover { hovering = $0 }
            .animation(.easeInOut(duration: 0.12), value: hovering)
            #endif
            Spacer(minLength: 36)
        }
    }

    private var assistantBubble: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let thoughts = message.thoughts, !thoughts.isEmpty {
                DisclosureGroup {
                    Text(thoughts)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .selectableOnIOS()
                        .padding(Theme.s2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Theme.thoughtsWash)
                        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
                } label: {
                    Label(L("思考过程"), systemImage: "brain")
                        .font(.caption)
                        .foregroundStyle(Theme.thoughts)
                }
            }
            MarkdownText(text: message.text)
            if let meta = message.meta {
                Text(meta)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(Theme.s3)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusBubble))
        .overlay(RoundedRectangle(cornerRadius: Theme.radiusBubble)
            .strokeBorder(Theme.hairline, lineWidth: 1))
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
                .disabled(store.selectedSessionIsDeleting)
            }
            if isLastAssistant, !store.isGenerating, !store.selectedSessionIsDeleting {
                Button {
                    store.regenerateLastTurn()
                } label: {
                    Label(store.retryActionTitle, systemImage: "arrow.clockwise")
                }
            }
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

/// 系统条（含后端报错）：只亮首行结论，traceback 之类的长尾折叠进「详细信息」。
/// 全量堆栈铺在会话里既难看又没信息密度（实机反馈）——但一个字都不丢，
/// 排障时展开即得、可复制。
struct SystemNoticeView: View {
    let text: String

    private var headline: Substring {
        text.prefix(while: { $0 != "\n" })
    }
    private var detail: String {
        String(text.dropFirst(headline.count)).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s1) {
            Text(String(headline))
                .font(.caption)
                .foregroundStyle(.secondary)
                .selectableOnIOS()
                .frame(maxWidth: .infinity)
            if !detail.isEmpty {
                DisclosureGroup {
                    ScrollView(.horizontal, showsIndicators: false) {
                        Text(detail)
                            .font(.system(.caption2, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .selectableOnIOS()
                            .padding(Theme.s1)
                    }
                } label: {
                    Text(L("详细信息"))
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                .contextMenu {
                    Button {
                        MessageView.copy(text)
                    } label: {
                        Label(L("复制全文"), systemImage: "doc.on.doc")
                    }
                }
            }
        }
        .padding(Theme.s2)
        .frame(maxWidth: .infinity)
        .background(Theme.wash)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
    }
}

/// 气泡下方的操作小钮（Claude AI 形态）：无边框图标钮 + 悬停提示。
struct MessageActionButton: View {
    let icon: String
    let help: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 24, height: 24)
                .contentShape(Rectangle())
        }
        .buttonStyle(.borderless)
        .help(help)
        .accessibilityLabel(help)
    }
}

/// 复制小钮：点击写入剪贴板后图标翻成成功色对勾，1.5s 复位（成功反馈）。
/// 连点时取消上一枚复位任务重新计时，不会闪跳。
struct CopyActionButton: View {
    let text: String
    let help: String
    @State private var copied = false
    @State private var resetTask: Task<Void, Never>?

    var body: some View {
        Button {
            MessageView.copy(text)
            withAnimation(.easeOut(duration: 0.15)) { copied = true }
            resetTask?.cancel()
            resetTask = Task {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                guard !Task.isCancelled else { return }
                withAnimation(.easeIn(duration: 0.2)) { copied = false }
            }
        } label: {
            Image(systemName: copied ? "checkmark" : "doc.on.doc")
                .font(.caption)
                .foregroundStyle(copied ? Theme.success : Color.secondary)
                .frame(width: 24, height: 24)
                .contentShape(Rectangle())
        }
        .buttonStyle(.borderless)
        .help(copied ? L("已复制") : help)
        .accessibilityLabel(copied ? L("已复制") : help)
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
                .foregroundStyle(Theme.accent)
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
                .padding(Theme.s2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.card)
                .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
                .overlay(RoundedRectangle(cornerRadius: Theme.radiusCard)
                    .strokeBorder(Theme.hairline, lineWidth: 1))
            }
        }
    }
}
