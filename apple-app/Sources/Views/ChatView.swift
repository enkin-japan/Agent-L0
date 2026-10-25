import SwiftUI
#if os(iOS)
import UIKit
#endif

/// 会话主界面：消息列表 + 流式预览 + 活动条 + 审批卡片 + 挂起横幅 + 输入区。
struct ChatView: View {
    @EnvironmentObject var store: AgentStore
    @State private var showMemory = false
    @State private var showDeck = false
    @State private var exportDocument: MarkdownFileDocument?
    @State private var showExporter = false
    @State private var showTaskSteps = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(spacing: 0) {
            #if os(macOS)
            conversationHeader
            #endif
            if let error = store.errorMessage {
                banner(text: error, color: .red) { store.errorMessage = nil }
            }

            if let notice = store.offlineHistoryNotice {
                HStack(spacing: Theme.s2) {
                    Image(systemName: store.historyIsReadOnly ? "internaldrive" : "arrow.triangle.2.circlepath")
                    Text(notice).lineLimit(2)
                    Spacer(minLength: 0)
                }
                .font(.caption).foregroundStyle(.secondary)
                .padding(.horizontal, Theme.contentPadding).padding(.vertical, Theme.s2)
                .background(Theme.wash)
            }

            if store.selectedSessionIsDeleting {
                Text(L("会话正在删除，请等待删除结果后再操作。"))
                    .font(.caption).foregroundStyle(.orange).padding(8)
            }

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: Theme.s5) {
                        #if os(iOS)
                        // 长计划随消息滚动，避免展开时挤占输入与审批区域。
                        if store.isGenerating || store.isSuspended || store.pendingTools?.isEmpty == false {
                            compactTaskSummary
                        }
                        #endif
                        if store.messages.isEmpty && !store.isGenerating && !store.isSuspended
                            && store.pendingTools?.isEmpty != false {
                            ConversationWelcomeView()
                        }
                        // 右键/长按菜单在 MessageView 内部挂到气泡上（挂整行会让
                        // iOS 长按预览高亮整排、超出内容边界）
                        ForEach(store.messages) { message in
                            MessageView(message: message)
                            if let index = message.turnIndex,
                               store.messages.last(where: { $0.turnIndex == index })?.id == message.id,
                               let group = store.retryGroups.first(where: { $0.displayIndex == index }) {
                                RetryAttemptsView(group: group)
                            }
                        }

                        // 暂停态（待审批/挂起）也保留直播预览:半截正文/思考/步骤是
                        // 决策上下文（账本协议下暂停轮不再经 turn_end 落部分消息）
                        if store.isGenerating || ((store.pendingTools?.isEmpty == false || store.isSuspended)
                                                  && (!store.streamText.isEmpty || !store.thinkingText.isEmpty
                                                      || !store.liveSteps.isEmpty)) {
                            #if os(macOS)
                            // 常量高度内滚台：直播期外层布局零变化。流式内容任何高度
                            // 增长（正文/思考/步骤）都被吸收在这个恒定外框内——27 beta
                            // AppKit 对约束阶段重入硬抛（13:20/13:32 两份 .ips 同链：
                            // SplitViewChildController didUpdateMinSize），只钉代码块
                            // 不够，必须整台钉死。轮末权威消息落地时才有一次性布局变化。
                            ScrollView {
                                streamingPreview
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .defaultScrollAnchor(.bottom)
                            .frame(height: 320)
                            #else
                            streamingPreview
                            #endif
                        }

                        if let tools = store.pendingTools, !tools.isEmpty,
                           let target = store.pendingApprovalTarget {
                            ApprovalCard(tools: tools) { approve in
                                store.decideApproval(approve: approve, target: target)
                            }
                            .id(target.sessionId + ":" + target.turnId + ":" + String(target.seq))
                            .disabled(store.isSessionDeleting(target.sessionId) || store.historyIsReadOnly)
                        }

                        if store.isSuspended {
                            suspendedBanner
                        }

                        if let notice = store.retryStatusNotice {
                            Text(notice).font(.caption).foregroundStyle(.secondary)
                        }
                        if store.historyProjectionNeedsRefresh {
                            Button(L("重新同步尝试记录")) { store.refreshDisplayedHistory() }
                                .font(.caption)
                        }
                        if let feedback = store.controlFeedback {
                            Text(feedback).font(.caption).foregroundStyle(.secondary)
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .frame(maxWidth: Theme.readingWidth, alignment: .leading)
                    .padding(.horizontal, Theme.contentPadding)
                    .padding(.vertical, Theme.s5)
                    .frame(maxWidth: .infinity, alignment: .center)
                }
                .background(Theme.canvas)
                .onChange(of: store.messages.count) {
                    withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) {
                        proxy.scrollTo("bottom", anchor: .bottom)
                    }
                }
                .onChange(of: store.streamText) {
                    proxy.scrollTo("bottom", anchor: .bottom)
                }
                // 软键盘收纳（iOS）：下拉跟手收起 + 点聊天记录区域即收起
                .scrollDismissesKeyboard(.interactively)
                .tapToDismissKeyboard()
            }

            ComposerView()
        }
        .background(Theme.canvas)
        #if os(iOS)
        .navigationTitle(chatTitle)
        #endif
        .onChange(of: store.selectedSessionId) { showTaskSteps = false }
        // 产物链接接管：消息里的 [xxx](/api/v1/files/{sid}/assets/{file}) 是相对路径
        // 且端点要 Bearer 头——丢给系统必死（-50 / 浏览器 401）。命中白名单就改开
        // 检查器文件页签并高亮该文件；外部 http(s) 链接照旧交给系统浏览器。
        .environment(\.openURL, OpenURLAction { url in
            guard let name = Self.assetName(from: url) else { return .systemAction }
            store.revealedAssetName = nil   // 同名重点也要触发 onChange：先清再置
            store.showInspector = true
            DispatchQueue.main.async { store.revealedAssetName = name }
            return .handled
        })
        .inspector(isPresented: $store.showInspector) {
            InspectorView()
                .inspectorColumnWidth(min: 260, ideal: 300, max: 420)
        }
        .toolbar {
            #if os(iOS)
            if store.deck != nil {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        showDeck = true
                    } label: {
                        Image(systemName: "play.rectangle")
                    }
                    .help(L("查看演示文稿"))
                    .accessibilityLabel(L("查看演示文稿"))
                }
            }
            ToolbarItem(placement: .primaryAction) {
                Button {
                    store.showInspector.toggle()
                } label: {
                    Image(systemName: "sidebar.trailing")
                }
                .help(L("检查器（计划 / 幻灯片 / 文件 / 工具集）"))
                .accessibilityLabel(L("任务详情"))
            }
            ToolbarItem(placement: .primaryAction) {
                conversationMenu
            }
            #endif
        }
        .confirmationDialog(L(store.retryConfirmationTarget?.isResend == true
                              ? "重发未确认的重试？" : "重试最近一次尝试？"),
            isPresented: Binding(get: { store.retryConfirmationTarget != nil },
                                 set: { if !$0 { store.cancelRetryConfirmation() } }),
            titleVisibility: .visible, presenting: store.retryConfirmationTarget) { target in
                Button(L(target.isResend ? "确认重发" : "确认重试")) { store.confirmRetry(target) }
                    .disabled(store.isSessionDeleting(target.sessionId) || store.historyIsReadOnly)
                Button(L("取消"), role: .cancel) { store.cancelRetryConfirmation() }
            } message: { target in
                Text(target.sourcePrompt + "\n\n" + L("工具可能再次执行。重试成功会更新当前结果，所有尝试都会保留。"))
            }
        .sheet(isPresented: $showMemory) {
            MemoryPanelView()
        }
        .sheet(isPresented: $showDeck) {
            if let deck = store.deck, let sid = store.selectedSessionId {
                DeckViewer(deck: deck, sessionId: sid)
            }
        }
        .fileExporter(isPresented: $showExporter,
                      document: exportDocument,
                      contentType: ConversationExport.contentType,
                      defaultFilename: exportTitle) { _ in }
    }

    private var conversationMenu: some View {
                Menu {
                    Button {
                        showMemory = true
                    } label: {
                        Label(L("会话记忆…"), systemImage: "memorychip")
                    }
                    Button {
                        exportDocument = MarkdownFileDocument(
                            text: ConversationExport.markdown(title: exportTitle,
                                                              messages: store.messages))
                        showExporter = true
                    } label: {
                        Label(L("导出会话 (Markdown)…"), systemImage: "square.and.arrow.up")
                    }
                    .disabled(store.messages.isEmpty)
                    Button(store.retryActionTitle) { store.requestRetryConfirmation() }
                        .disabled(store.isGenerating || store.isSuspended || store.canonicalTurns.isEmpty || store.selectedSessionIsDeleting)
                    Button(L("编辑最近一次尝试并重发")) { store.editLastTurn() }
                        .disabled(!store.canEditLastAttempt)
                    Button(role: .destructive) {
                        store.deleteLastTurn()
                    } label: {
                        Label(L("撤销最近一次尝试"), systemImage: "arrow.uturn.backward")
                    }
                    .disabled(store.isGenerating || store.messages.isEmpty || store.selectedSessionIsDeleting)
                } label: {
                    Image(systemName: "ellipsis.circle")
                        .frame(width: Theme.controlHitSize, height: Theme.controlHitSize)
                        .modifier(CeladonHoverSurface(cornerRadius: Theme.radiusSmall))
                }
                .buttonStyle(CeladonControlStyle())
                .accessibilityLabel(L("更多操作"))
    }

    private var conversationHeader: some View {
        HStack(spacing: Theme.s3) {
            VStack(alignment: .leading, spacing: 3) {
                Text(chatTitle).font(.headline).lineLimit(1)
                if store.isGenerating || store.isSuspended || store.pendingTools?.isEmpty == false {
                    TaskPhaseLabel()
                } else {
                    Text(L("对话")).font(.caption).foregroundStyle(Theme.secondaryText)
                }
            }
            Spacer(minLength: Theme.s2)
            if store.deck != nil {
                Button { showDeck = true } label: {
                    Image(systemName: "play.rectangle")
                        .frame(width: Theme.controlHitSize, height: Theme.controlHitSize)
                }
                    .help(L("查看演示文稿")).accessibilityLabel(L("查看演示文稿"))
            }
            conversationMenu
            Button { store.showInspector.toggle() } label: {
                Image(systemName: "sidebar.trailing")
                    .frame(width: Theme.controlHitSize, height: Theme.controlHitSize)
            }
            .help(L("任务详情")).accessibilityLabel(L("任务详情"))
        }
        .buttonStyle(CeladonControlStyle())
        .foregroundStyle(Theme.text)
        .padding(.horizontal, Theme.contentPadding)
        .padding(.vertical, Theme.s3)
        .frame(minHeight: 62)
        .background(Theme.canvas)
        .overlay(alignment: .bottom) { Rectangle().fill(Theme.separator).frame(height: 1) }
    }

    private var chatTitle: String {
        guard let sid = store.selectedSessionId,
              let title = store.sessions.first(where: { $0.sessionId == sid })?.title,
              !title.isEmpty else { return L("新对话") }
        return title
    }

    /// 只是既有任务投影的折叠展示；不推断完成、不发送控制请求。
    private var compactTaskSummary: some View {
        DisclosureGroup(isExpanded: $showTaskSteps) {
            VStack(alignment: .leading, spacing: Theme.s3) {
                if let plan = store.taskSnapshot?.plan, !plan.isEmpty {
                    TaskPlanSteps(plan: plan)
                } else if let current = store.activity?.current, !current.isEmpty {
                    Text(current).font(.callout).foregroundStyle(.secondary)
                }
                Button(L("查看任务详情")) { store.showInspector = true }
                    .frame(minHeight: Theme.controlHitSize)
            }
            .padding(.top, Theme.s2)
        } label: {
            HStack(spacing: Theme.s2) {
                TaskPhaseLabel()
                Spacer(minLength: Theme.s1)
                if let plan = store.taskSnapshot?.plan, !plan.isEmpty {
                    Text("\(plan.filter { $0.status == "done" }.count) / \(plan.count)")
                        .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        .accessibilityLabel(L("已完成步骤") + " \(plan.filter { $0.status == "done" }.count) / \(plan.count)")
                }
            }
            .frame(minHeight: 36)
        }
        .padding(.horizontal, Theme.s3)
        .padding(.vertical, Theme.s1)
        .background(Theme.sidebar, in: RoundedRectangle(cornerRadius: Theme.radiusCard))
    }

    /// 产物链接白名单解析：/api/v1/files/{sid}/assets/{file} → 文件名；其余 nil。
    /// 只认无 scheme 的相对链接（工具回执的固定形态）；带 host 的绝对 http(s) 链接
    /// 一律交给系统——避免把撞巧同路径的外站链接也劫下来。
    static func assetName(from url: URL) -> String? {
        guard url.scheme == nil else { return nil }
        let parts = url.path.split(separator: "/").map(String.init)
        // ["api", "v1", "files", sid, "assets", file]
        guard parts.count == 6, parts[0] == "api", parts[1] == "v1",
              parts[2] == "files", parts[4] == "assets" else { return nil }
        return parts[5]
    }

    /// 导出文件名/标题：会话标题优先，兜底会话 ID。
    private var exportTitle: String {
        guard let sid = store.selectedSessionId else { return "conversation" }
        let title = store.sessions.first(where: { $0.sessionId == sid })?.title
        return (title?.isEmpty == false ? title! : sid)
    }

    /// 流式过程预览：思考 + 正文 token + 工具步骤 + 任务计划（turn_end 后整体被权威消息替换）。
    private var streamingPreview: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("MacAgent", systemImage: "asterisk")
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.accent)
            if !store.thinkingText.isEmpty {
                DisclosureGroup {
                    Text(store.thinkingText)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } label: {
                    Label(L("思考中…"), systemImage: "brain")
                        .font(.caption)
                        .foregroundStyle(Theme.thoughts)
                }
            }

            if !store.streamText.isEmpty {
                MarkdownText(text: store.streamText, streaming: true)
            }

            VStack(alignment: .leading, spacing: Theme.s2) {
                ForEach(Array(store.liveSteps.suffix(6).enumerated()), id: \.offset) { _, step in
                    Label(step, systemImage: "circle.dotted")
                        .font(.caption)
                        .foregroundStyle(Theme.secondaryText)
                }
                HStack(spacing: Theme.s2) {
                    ProgressView().controlSize(.small).tint(Theme.accent)
                    Text(activityLine)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    if store.canSuspend {
                        Button(L("挂起")) { store.suspendTask() }
                            .font(.caption)
                            .buttonStyle(.borderless)
                    }

                }
            }
            .padding(Theme.s3)
            .background(Theme.sidebar, in: RoundedRectangle(cornerRadius: Theme.radiusCard))
            .overlay(RoundedRectangle(cornerRadius: Theme.radiusCard).strokeBorder(Theme.separator))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var activityLine: String {
        if let current = store.activity?.current, !current.isEmpty { return current }
        if let task = store.taskSnapshot, let plan = task.plan, !plan.isEmpty {
            let done = plan.filter { $0.status == "done" }.count
            if let doing = plan.first(where: { $0.status == "doing" }) {
                return "\(L("步骤")) \(done + 1)/\(plan.count)：\(doing.title)"
            }
            return "\(L("任务推进中"))（\(done)/\(plan.count)）"
        }
        return L("生成中…")
    }

    private var suspendedBanner: some View {
        HStack {
            Label(L("任务已挂起（推理资源已让出，进度保留在后端）"), systemImage: "pause.circle")
                .font(.callout)
            Spacer()
            Button(L("恢复续跑")) { store.resumeTask() }
                .buttonStyle(.borderedProminent)
                .disabled(store.controlRequest != nil || store.selectedSessionIsDeleting)
        }
        .padding(Theme.s3)
        .background(Theme.accent.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
    }

    private func banner(text: String, color: Color, dismiss: @escaping () -> Void) -> some View {
        HStack {
            Text(text).font(.caption)
            Spacer()
            Button {
                dismiss()
            } label: {
                Image(systemName: "xmark")
            }
            .buttonStyle(.plain)
        }
        .padding(8)
        .background(color.opacity(0.12))
    }
}

/// 读取已有轮次投影；未知或没有活跃轮次时只称为计划，不把缺失信息当作成功。
struct TaskPhaseLabel: View {
    @EnvironmentObject var store: AgentStore

    private var presentation: (title: String, symbol: String, color: Color) {
        if store.pendingTools?.isEmpty == false {
            return (L("需要你确认"), "hand.raised", Theme.warning)
        }
        if store.isSuspended { return (L("已挂起（进度保留）"), "pause.circle", Theme.accent) }
        if store.ledgerPhase == "queued" { return (L("排队中"), "clock", Theme.accent) }
        if store.isGenerating { return (L("正在处理"), "circle.dotted", Theme.accent) }
        return (L("任务计划"), "list.bullet", .secondary)
    }

    var body: some View {
        Label(presentation.title, systemImage: presentation.symbol)
            .font(.caption.weight(.medium))
            .foregroundStyle(presentation.color)
    }
}

private extension View {
    /// 点击聊天记录区域收起软键盘（仅 iOS 有软键盘）。simultaneousGesture 与子视图
    /// 手势并行——不抢气泡的按钮/上下文菜单/长按选择，点到哪都能顺手收键盘。
    @ViewBuilder
    func tapToDismissKeyboard() -> some View {
        #if os(iOS)
        simultaneousGesture(TapGesture().onEnded {
            UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder),
                                            to: nil, from: nil, for: nil)
        })
        #else
        self
        #endif
    }
}

/// Audit uses the existing message and execution-facts mapper; it never invokes tools.
struct RetryAttemptsView: View {
    let group: RetryGroup
    @EnvironmentObject var store: AgentStore
    var body: some View {
        DisclosureGroup {
            ForEach(group.turnIndices, id: \.self) { index in
                if index >= 0 && index < store.canonicalTurns.count {
                    let turn = store.canonicalTurns[index]
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text("#\(index) · \(ChatMessage.statusLabel(turn.status))").font(.caption)
                            if index == group.displayIndex {
                                Text(L("当前展示版本")).font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button {
                                store.branch(fromTurnIndex: index)
                            } label: { Image(systemName: "arrow.triangle.branch") }
                                .help(L("从这一轮分支出新会话"))
                                .disabled(store.selectedSessionIsDeleting)
                        }
                        if let timestamp = turn.timestamp {
                            Text(Timestamps.messageLabel(Date(timeIntervalSince1970: timestamp)))
                                .font(.caption2).foregroundStyle(.secondary)
                        } else {
                            Text(L("时间未记录")).font(.caption2).foregroundStyle(.secondary)
                        }
                        ForEach(ChatMessage.from(turn: turn, turnIndex: index)) { message in
                            MessageView(message: message, auditOnly: true)
                        }
                    }
                    .padding(8)
                }
            }
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(L("尝试记录") + " (\(group.turnIndices.count))").font(.caption)
                if let latest = group.turnIndices.last, latest < store.canonicalTurns.count {
                    Text(L("最近一次尝试：") + ChatMessage.statusLabel(store.canonicalTurns[latest].status))
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
    }
}
