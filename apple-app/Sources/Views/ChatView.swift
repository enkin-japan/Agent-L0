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

    var body: some View {
        VStack(spacing: 0) {
            if let error = store.errorMessage {
                banner(text: error, color: .red) { store.errorMessage = nil }
            }

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        // 右键/长按菜单在 MessageView 内部挂到气泡上（挂整行会让
                        // iOS 长按预览高亮整排、超出内容边界）
                        ForEach(store.messages) { message in
                            MessageView(message: message)
                        }

                        // 暂停态（待审批/挂起）也保留直播预览:半截正文/思考/步骤是
                        // 决策上下文（账本协议下暂停轮不再经 turn_end 落部分消息）
                        if store.isGenerating || ((store.pendingTools?.isEmpty == false || store.isSuspended)
                                                  && (!store.streamText.isEmpty || !store.thinkingText.isEmpty
                                                      || !store.liveSteps.isEmpty)) {
                            streamingPreview
                        }

                        if let tools = store.pendingTools, !tools.isEmpty {
                            ApprovalCard(tools: tools) { approve in
                                store.decideApproval(approve: approve)
                            }
                        }

                        if store.isSuspended {
                            suspendedBanner
                        }

                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(16)
                }
                .background(Theme.canvas)
                .onChange(of: store.messages.count) {
                    withAnimation { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                .onChange(of: store.streamText) {
                    proxy.scrollTo("bottom", anchor: .bottom)
                }
                // 软键盘收纳（iOS）：下拉跟手收起 + 点聊天记录区域即收起
                .scrollDismissesKeyboard(.interactively)
                .tapToDismissKeyboard()
            }

            Divider()
            ComposerView()
        }
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
            if store.deck != nil {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        showDeck = true
                    } label: {
                        Image(systemName: "play.rectangle")
                    }
                    .help(L("查看演示文稿"))
                }
            }
            ToolbarItem(placement: .primaryAction) {
                Button {
                    store.showInspector.toggle()
                } label: {
                    Image(systemName: "sidebar.trailing")
                }
                .help(L("检查器（计划 / 幻灯片 / 文件 / 工具集）"))
            }
            ToolbarItem(placement: .primaryAction) {
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
                    Button(role: .destructive) {
                        store.deleteLastTurn()
                    } label: {
                        Label(L("删除最后一轮问答"), systemImage: "arrow.uturn.backward")
                    }
                    .disabled(store.isGenerating || store.messages.isEmpty)
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
            }
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

            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(store.liveSteps.suffix(6).enumerated()), id: \.offset) { _, step in
                    Text(step)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                HStack(spacing: Theme.s2) {
                    ProgressView().controlSize(.small).tint(Theme.accent)
                    Text(activityLine)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button(L("挂起")) { store.suspendTask() }
                        .font(.caption)
                        .buttonStyle(.borderless)
                        .help(L("挂起"))
                }
            }
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
