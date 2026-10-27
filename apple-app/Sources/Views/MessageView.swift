import SwiftUI
import QuickLook

/// 单条消息渲染：用户右侧青瓷气泡 / 助手正文 / 系统居中灰条 / 工具折叠行。
/// 菜单直接挂在内容上（挂整行会让 iOS 长按预览高亮整排、超出内容边界）。
/// 文本选中只在 iOS 开（macOS 上可选文本的系统菜单会吃掉自定义右键菜单）。
/// 主操作（复制/撤回重编/分支/重新生成）以气泡下方的小钮行为准（Claude AI 形态）；
/// 右键菜单保留同一组动作作为第二入口，后续还会往里放更多功能。
struct MessageView: View {
    let message: ChatMessage
    var auditOnly = false
    @EnvironmentObject var store: AgentStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
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
                toolView
            }
        case .toolGroup:
            ToolGroupView(message: message)
        }
    }

    // MARK: - 用户气泡

    private var userView: some View {
        HStack {
            Spacer(minLength: Theme.s5)
            VStack(alignment: .trailing, spacing: Theme.s2) {
                Text(userPresentation.text)
                    .font(Theme.readingFont)
                    .selectableOnIOS()
                    .lineSpacing(4)
                    .padding(.horizontal, Theme.s4)
                    .padding(.vertical, Theme.s3)
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
                if !userPresentation.attachments.isEmpty {
                    MessageAttachmentLayout(spacing: Theme.s2) {
                        ForEach(Array(userPresentation.attachments.enumerated()), id: \.offset) { _, attachment in
                            MessageAttachmentChip(attachment: attachment)
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
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.12), value: hovering)
            #endif
        }
    }

    /// 审计页同样使用原始 canonical 下标；只有整条投影完全匹配，才拆分旧附件后缀。
    private var userPresentation: (text: String, attachments: [AttachmentInfo]) {
        guard let index = message.turnIndex, store.canonicalTurns.indices.contains(index),
              let prompt = store.canonicalTurns[index].prompt else { return (message.text, []) }
        let turn = store.canonicalTurns[index]
        let attachments = (turn.attachments ?? []).filter { $0.name?.isEmpty == false }
        var parts = (turn.attachments ?? []).compactMap(\.name)
        let mediaCount = (turn.image?.count ?? 0) + (turn.audio?.count ?? 0)
        if mediaCount > 0 { parts.append("\(L("媒体")) ×\(mediaCount)") }
        let projected = prompt + (parts.isEmpty ? "" : "\n📎 " + parts.joined(separator: "、"))
        guard projected == message.text else { return (message.text, []) }
        // 媒体没有可供下载的附件记录，仍按原投影保留数量信息。
        let mediaText = mediaCount > 0 ? "\n📎 \(L("媒体")) ×\(mediaCount)" : ""
        return (prompt + mediaText, attachments)
    }

    private var toolView: some View {
        DisclosureGroup {
            ToolDetailBody(thoughts: message.thoughts, detail: message.toolDetail ?? "")
                .padding(.top, Theme.s2)
        } label: {
            Label {
                Text(message.toolLabel ?? L("工具"))
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "wrench.and.screwdriver")
                    .foregroundStyle(Theme.accent)
            }
            .font(.caption.weight(.medium))
            .foregroundStyle(.secondary)
        }
        .padding(Theme.s3)
        .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
        .background(Theme.wash)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
        .overlay(RoundedRectangle(cornerRadius: Theme.radiusCard)
            .strokeBorder(Theme.separator, lineWidth: 1))
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
        HStack(alignment: .top, spacing: 0) {
            VStack(alignment: .leading, spacing: Theme.s2) {
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
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.12), value: hovering)
            #endif
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var assistantBubble: some View {
        VStack(alignment: .leading, spacing: Theme.s3) {
            HStack(spacing: Theme.s2) {
                Image(systemName: "asterisk")
                    .font(.caption.weight(.semibold))
                    .frame(width: 24, height: 24)
                    .background(Theme.selection)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
                    .accessibilityHidden(true)
                Text("MacAgent")
                    .font(.caption.weight(.semibold))
            }
            .foregroundStyle(Theme.accent)
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
        .frame(maxWidth: .infinity, alignment: .leading)
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
                .frame(width: Theme.controlHitSize, height: Theme.controlHitSize)
                .contentShape(Rectangle())
        }
        .buttonStyle(CeladonControlStyle())
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
                .frame(width: Theme.controlHitSize, height: Theme.controlHitSize)
                .contentShape(Rectangle())
        }
        .buttonStyle(CeladonControlStyle())
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

/// 引用仅使用工具声明的结构化载荷，展开后保留完整摘录和路径。
/// 工具行展开后的正文（思考 + 详细信息 + 复制）：单条工具行与执行过程组内各步共用。
struct ToolDetailBody: View {
    let thoughts: String?
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s2) {
            if let thoughts, !thoughts.isEmpty {
                Text(thoughts)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .selectableOnIOS()
            }
            HStack {
                Text(L("详细信息")).font(.caption.weight(.medium)).foregroundStyle(.secondary)
                Spacer(minLength: Theme.s2)
                CopyActionButton(text: detail, help: L("复制全文"))
            }
            Text(detail)
                .font(.system(.caption, design: .monospaced))
                .selectableOnIOS()
                .fixedSize(horizontal: false, vertical: true)
                .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// 步骤状态符号：执行中转圈（计划步骤用清单符号）/ ✓ / ✕ / ⏱；状态未知（无可靠回执）
/// 只画中性的工具符号。✓ 只表示「工具已返回」——后端只知道 ok / timeout。
struct StepGlyph: View {
    let status: String?
    var kind: String? = nil

    var body: some View {
        Group {
            switch status {
            case "running" where kind == "plan":
                Image(systemName: "list.bullet.clipboard").foregroundStyle(Theme.accent)
            case "running":
                ProgressView().controlSize(.mini)
            case "pending":
                Image(systemName: "circle.dotted").foregroundStyle(Theme.secondaryText)
            case "ok":
                Image(systemName: "checkmark").foregroundStyle(Theme.success)
            case "failed":
                Image(systemName: "xmark").foregroundStyle(Theme.danger)
            case "timeout":
                Image(systemName: "timer").foregroundStyle(Theme.warning)
            default:
                Image(systemName: "wrench.and.screwdriver").foregroundStyle(Theme.secondaryText)
            }
        }
        .font(.system(size: 10, weight: .semibold))
        .frame(width: 14, height: 14)
        .accessibilityLabel(Self.accessibilityText(status))
    }

    static func accessibilityText(_ status: String?) -> String {
        switch status {
        case "running": return L("执行中")
        case "pending": return L("准备调用")
        case "ok": return L("已返回")
        case "failed": return L("失败")
        case "timeout": return L("超时")
        default: return L("工具")
        }
    }
}

/// 一行紧凑步骤：符号 + 名称 + 单行截断的详情（直播预览与执行过程组共用）。
struct CompactStepRow: View {
    let status: String?
    var kind: String? = nil
    let label: String
    let detail: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            StepGlyph(status: status, kind: kind)
                .alignmentGuide(.firstTextBaseline) { $0[VerticalAlignment.center] + 4 }
            Text(label)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Theme.secondaryText)
                .lineLimit(1)
                .layoutPriority(1)
            if let line = Self.firstLine(detail) {
                Text(line)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    static func firstLine(_ detail: String) -> String? {
        let line = detail.split(whereSeparator: \.isNewline).first.map(String.init)?
            .trimmingCharacters(in: .whitespaces) ?? ""
        return line.isEmpty ? nil : String(line.prefix(200))
    }
}

/// 执行过程组：一轮里连续的工具行合成一个折叠块（默认折叠；展开态存在 AgentStore，
/// 会话打开期间保持）。展开后每步一行紧凑行，单步可再展开看完整详情。
struct ToolGroupView: View {
    let message: ChatMessage
    @EnvironmentObject var store: AgentStore

    private var title: String {
        let count = message.toolInvocationCount
        // L10n 只有一个键：英文单数单独处理（"1 step"），中日文无单复数。
        let unit = count == 1 && L10n.resolved == "en" ? "step" : L("步")
        var text = "\(L("执行过程")) · \(count) \(unit)"
        if let seconds = message.stepsDuration {
            text += " · " + (seconds < 10 ? String(format: "%.1f", seconds) : String(Int(seconds.rounded())))
                + " " + L("秒")
        }
        return text
    }

    private var key: String { message.toolGroupKey ?? message.id.uuidString }

    var body: some View {
        DisclosureGroup(isExpanded: Binding(
            get: { store.isToolGroupExpanded(key) },
            set: { store.setToolGroup(key, expanded: $0) })) {
            VStack(alignment: .leading, spacing: 3) {
                ForEach(message.toolSteps) { step in
                    ToolStepRow(step: step)
                }
            }
            .padding(.top, Theme.s2)
        } label: {
            Label {
                Text(title)
            } icon: {
                Image(systemName: "list.bullet.rectangle")
                    .foregroundStyle(Theme.accent)
            }
            .font(.caption.weight(.medium))
            .foregroundStyle(.secondary)
        }
        .padding(.horizontal, Theme.s3)
        .padding(.vertical, Theme.s2)
        .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
        .background(Theme.wash)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
    }
}

/// 组内一行：紧凑行作标题，展开看完整详情（复用单条工具行的正文）。引用格不进组。
private struct ToolStepRow: View {
    let step: ToolStep

    var body: some View {
        DisclosureGroup {
            ToolDetailBody(thoughts: step.thoughts, detail: step.detail)
                .padding(.vertical, Theme.s1)
        } label: {
            CompactStepRow(status: step.status, label: step.label, detail: step.detail)
        }
    }
}

struct CitationsView: View {
    let items: [CitationItem]
    let label: String

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s2) {
            Label(label, systemImage: "text.quote")
                .font(.caption.weight(.medium))
                .foregroundStyle(Theme.accent)
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                CitationCard(item: item, number: index + 1)
            }
        }
    }
}

private struct CitationCard: View {
    let item: CitationItem
    let number: Int
    @State private var expanded = false

    private var webURL: URL? {
        guard let path = item.path, let url = URL(string: path),
              let scheme = url.scheme?.lowercased(), ["https", "http"].contains(scheme),
              url.host != nil else { return nil }
        return url
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s2) {
            DisclosureGroup(isExpanded: $expanded) {
                VStack(alignment: .leading, spacing: Theme.s2) {
                    if let snippet = item.snippet, !snippet.isEmpty {
                        Text(snippet).font(.callout).selectableOnIOS()
                    }
                    if let path = item.path, !path.isEmpty {
                        Text(path)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .selectableOnIOS()
                    }
                    HStack {
                        if let url = webURL {
                            Link(destination: url) {
                                Label(L("打开来源"), systemImage: "arrow.up.right.square")
                            }
                            .font(.caption)
                        }
                        Spacer(minLength: Theme.s2)
                        CopyActionButton(text: [item.source, item.snippet, item.path].compactMap { $0 }.joined(separator: "\n"),
                                         help: L("复制引用"))
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, Theme.s2)
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: Theme.s2) {
                    Text("\(number)")
                        .font(.caption.monospacedDigit().weight(.semibold))
                        .foregroundStyle(Theme.accent)
                    Text(item.source)
                        .font(.callout.weight(.medium))
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: Theme.s1)
                    if let score = item.score {
                        Text(String(format: "%.2f", score))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .help(L("相关度"))
                            .accessibilityLabel(L("相关度") + " " + String(format: "%.2f", score))
                    }
                }
            }
            if !expanded, let snippet = item.snippet, !snippet.isEmpty {
                Text(snippet)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            }
        }
        .padding(Theme.s3)
        .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
        .overlay(RoundedRectangle(cornerRadius: Theme.radiusCard)
            .strokeBorder(Theme.separator, lineWidth: 1))
    }
}

/// Uploaded originals resolve through the backend manifest or its offline copy.
private struct MessageAttachmentChip: View {
    let attachment: AttachmentInfo
    @EnvironmentObject private var store: AgentStore
    @State private var showingDetails = false
    @State private var quickLookURL: URL?
    @State private var opening = false
    @State private var openError: String?

    var body: some View {
        Button { showingDetails.toggle() } label: {
            Label(attachment.name ?? L("附件"), systemImage: "doc.text")
                .font(.caption)
                .lineLimit(1)
                .truncationMode(.middle)
                .padding(.horizontal, Theme.s2)
                .frame(minHeight: Theme.controlHitSize)
                .foregroundStyle(.secondary)
                .background(Theme.card)
                .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
                .overlay(RoundedRectangle(cornerRadius: Theme.radiusSmall)
                    .strokeBorder(Theme.separator, lineWidth: 1))
        }
        .buttonStyle(CeladonControlStyle())
        .help(attachment.name ?? L("附件"))
        .popover(isPresented: $showingDetails) {
            VStack(alignment: .leading, spacing: Theme.s3) {
                Text(attachment.name ?? L("附件")).font(.headline)
                if let type = attachment.type { Text(type).font(.caption).foregroundStyle(.secondary) }
                if let path = attachment.path {
                    Text(path).font(.caption.monospaced()).textSelection(.enabled)
                }
                if let openError { Text(openError).font(.caption).foregroundStyle(.secondary) }
                HStack {
                    Button(L("打开")) {
                        guard let client = store.client, let sid = store.selectedSessionId else { return }
                        opening = true; openError = nil
                        Task {
                            defer { opening = false }
                            do {
                                let url = try await client.downloadAttachment(sessionId: sid, attachment: attachment)
                                guard store.client === client, store.selectedSessionId == sid else { return }
                                showingDetails = false
                                quickLookURL = url
                            } catch {
                                guard store.client === client, store.selectedSessionId == sid else { return }
                                openError = L("附件尚未保存到本机，或原文件已不可用。")
                            }
                        }
                    }
                    .disabled(opening)
                    CopyActionButton(text: [attachment.name, attachment.type, attachment.path].compactMap { $0 }.joined(separator: "\n"),
                                     help: L("复制附件信息"))
                    Spacer()
                    Button(L("关闭")) { showingDetails = false }
                }
            }
            .padding(Theme.s4)
            .frame(idealWidth: 280, maxWidth: 360)
        }
        .quickLookPreview($quickLookURL)
    }
}

/// 根据提议宽度换行，长文件名也不会撑开聊天列。
private struct MessageAttachmentLayout: Layout {
    let spacing: CGFloat

    private func arrange(_ subviews: Subviews, width: CGFloat) -> (CGSize, [CGPoint]) {
        var points: [CGPoint] = []
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        var usedWidth: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.init(width: width, height: nil))
            if x > 0, x + size.width > width {
                x = 0; y += rowHeight + spacing; rowHeight = 0
            }
            points.append(CGPoint(x: x, y: y))
            usedWidth = max(usedWidth, x + size.width)
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return (CGSize(width: usedWidth, height: y + rowHeight), points)
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let naturalWidth = subviews.reduce(CGFloat.zero) { $0 + $1.sizeThatFits(.unspecified).width }
            + spacing * CGFloat(max(0, subviews.count - 1))
        let width = proposal.width.flatMap { $0.isFinite ? $0 : nil } ?? naturalWidth
        return arrange(subviews, width: max(1, width)).0
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let (_, points) = arrange(subviews, width: bounds.width)
        for (subview, point) in zip(subviews, points) {
            subview.place(at: CGPoint(x: bounds.minX + point.x, y: bounds.minY + point.y),
                          proposal: .init(width: bounds.width, height: nil))
        }
    }
}
