import SwiftUI

/// 检查器面板（对齐 web 端右侧栏）：计划 / 幻灯片 / 工具集。
/// （系统提示词展示已按用户要求移除；工具只列名字。）
/// macOS 挂在 .inspector 的右侧栏；iOS 自适应为 sheet/列。
struct InspectorView: View {
    @EnvironmentObject var store: AgentStore

    enum Tab: String, CaseIterable {
        case plan, deck, tools

        var label: String {
            switch self {
            case .plan: return L("计划")
            case .deck: return L("幻灯片")
            case .tools: return L("工具集")
            }
        }
    }

    @State private var tab: Tab = .plan
    @State private var planSnapshot: TaskSnapshot?
    @State private var toolList: [ToolInfo]?
    @State private var showDeckViewer = false

    var body: some View {
        VStack(spacing: 0) {
            Picker("", selection: $tab) {
                ForEach(Tab.allCases, id: \.self) { item in
                    Text(item.label).tag(item)
                }
            }
            .pickerStyle(.segmented)
            .padding(10)
            Divider()
            ScrollView {
                content
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(12)
            }
        }
        .task(id: tab) { await load() }
        .task(id: store.selectedSessionId) {
            planSnapshot = nil
            await load()
        }
    }

    @ViewBuilder
    private var content: some View {
        switch tab {
        case .plan:
            planContent
        case .deck:
            deckContent
        case .tools:
            toolsContent
        }
    }

    // MARK: - 计划（当前会话的多步任务；生成中优先显示 SSE 实时快照）

    @ViewBuilder
    private var planContent: some View {
        let snapshot = store.taskSnapshot ?? planSnapshot
        if let task = snapshot, let plan = task.plan, !plan.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                if let goal = task.goal, !goal.isEmpty {
                    Text(goal).font(.callout).bold()
                }
                ForEach(plan) { step in
                    HStack(alignment: .top, spacing: 8) {
                        Text(Self.stepIcon(step.status))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(step.title).font(.callout)
                            if let note = step.note, !note.isEmpty {
                                Text(note).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                if let activity = store.activity, let current = activity.current,
                   !current.isEmpty, store.isGenerating {
                    Divider()
                    Label(current, systemImage: "bolt")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        } else {
            emptyHint(L("当前会话没有进行中的多步任务计划"))
        }
    }

    static func stepIcon(_ status: String) -> String {
        switch status {
        case "done": return "✅"
        case "doing": return "▶️"
        case "failed": return "❌"
        case "skipped": return "⏭"
        default: return "○"
        }
    }

    // MARK: - 幻灯片（deck 缩略图纵列，点开放映）

    @ViewBuilder
    private var deckContent: some View {
        if let deck = store.deck, let sid = store.selectedSessionId {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text(deck.meta.title).font(.callout).bold()
                    Spacer()
                    Button {
                        showDeckViewer = true
                    } label: {
                        Label(L("放映"), systemImage: "play.rectangle")
                    }
                    .font(.caption)
                }
                ForEach(Array(deck.slides.enumerated()), id: \.element.id) { index, slide in
                    VStack(alignment: .leading, spacing: 2) {
                        GeometryReader { geo in
                            let scale = geo.size.width / SlideGeometry.designW
                            SlideCanvasView(slide: slide, meta: deck.meta, sessionId: sid)
                                .frame(width: SlideGeometry.designW, height: SlideGeometry.designH)
                                .scaleEffect(scale, anchor: .topLeading)
                        }
                        .aspectRatio(16 / 9, contentMode: .fit)
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                        .overlay(RoundedRectangle(cornerRadius: 6)
                            .stroke(Color.primary.opacity(0.1)))
                        Text("\(index + 1)").font(.caption2).foregroundStyle(.secondary)
                    }
                    .onTapGesture { showDeckViewer = true }
                }
            }
            .sheet(isPresented: $showDeckViewer) {
                DeckViewer(deck: deck, sessionId: sid)
            }
        } else {
            emptyHint(L("当前会话没有演示文稿。让 Agent「做一份 PPT」即可生成。"))
        }
    }

    // MARK: - 工具集（只列名字；需审批的带锁标）

    @ViewBuilder
    private var toolsContent: some View {
        if let tools = toolList {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(tools) { tool in
                    HStack(spacing: 6) {
                        Text(tool.name)
                            .font(.system(.callout, design: .monospaced))
                        if tool.requiresApproval == true {
                            Image(systemName: "lock")
                                .font(.caption2)
                                .foregroundStyle(.orange)
                                .help(L("需审批"))
                        }
                    }
                }
            }
        } else {
            ProgressView().frame(maxWidth: .infinity)
        }
    }

    private func emptyHint(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.top, 24)
    }

    // MARK: - 按页懒加载

    private func load() async {
        guard let client = store.client else { return }
        switch tab {
        case .plan:
            if !store.isGenerating, let sid = store.selectedSessionId {
                planSnapshot = (try? await client.taskStatus(sessionId: sid))?.task
            }
        case .tools:
            if toolList == nil {
                toolList = (try? await client.tools()) ?? []
            }
        case .deck:
            break
        }
    }
}
