import SwiftUI
#if os(macOS)
import AppKit
#endif

/// 侧栏会话列表：后端磁盘真相（/api/v1/sessions），pin 置顶，右键/长按删除与置顶。
/// macOS 的入口留在侧栏内；iOS 的全局控件挂本视图 toolbar。
/// 行交互平台分流：macOS 用 List selection（双栏高亮语义）；iOS 行是 Button →
/// store.select → navigationDestination 推入（selection 绑定在 iPhone 上同值重点不触发导航）。
struct SessionListView: View {
    @EnvironmentObject var store: AgentStore
    #if os(macOS)
    @EnvironmentObject var sidecar: SidecarManager
    @FocusState private var searchFocused: Bool
    @Environment(\.titleBarClearance) private var titleBarClearance
    #endif
    @State private var showSettings = false
    @State private var showSchedules = false
    @State private var showKnowledge = false
    @State private var tasksExpanded = false
    @State private var renaming: SessionMetaItem?
    @State private var renameText = ""
    // 多选（批量删除）：macOS 走 ⌘/⇧ 原生点选；iOS 长按菜单进编辑态圈选。
    @State private var multiSelection = Set<String>()
    @State private var confirmBulkDelete = false
    @State private var deleting = false
    @State private var deleteFailures: [String: String] = [:]
    @State private var deleteRequestID = UUID()
    @State private var deletionSelectionProjection: Set<String>?
    #if os(iOS)
    @State private var editMode: EditMode = .inactive
    #endif

    var body: some View {
        sessionList
            .safeAreaInset(edge: .bottom) {
                if deleting { ProgressView(L("正在删除所选项目…")).padding(8) }
                else if !deleteFailures.isEmpty {
                    Text(L("部分项目未确认删除，失败项仍保留选中，可检查后重试。"))
                        .font(.caption).foregroundStyle(.orange).padding(8)
                }
            }
            .onChange(of: store.client.map { ObjectIdentifier($0) }) {
                deleteRequestID = UUID(); deleting = false; deleteFailures = [:]
            }
            // 下拉刷新是 iOS 习语；macOS 上刷新控件收起与 List 更新天然打架
            //（实机反馈：等值守卫后仍有残影）——macOS 撤掉，改由探活循环顺带
            // 自动刷新列表（15s 一巡 + 等值守卫 = 不变零重建）
            #if os(iOS)
            .refreshable { await store.loadSessions() }
            #endif
            #if os(macOS)
            .safeAreaInset(edge: .top, spacing: 0) {
                sidebarHeader
            }
            #endif
            .alert(L("重命名会话"), isPresented: Binding(
                get: { renaming != nil },
                set: { if !$0 { renaming = nil } })) {
                TextField(L("标题"), text: $renameText)
                Button(L("取消"), role: .cancel) { renaming = nil }
                Button(L("保存")) {
                    if let item = renaming { store.rename(item, to: renameText) }
                    renaming = nil
                }
            }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                sidebarFooter
            }
            #if os(iOS)
            .toolbar {
                ToolbarItemGroup(placement: controlsPlacement) {
                    Button {
                        showSchedules = true
                    } label: {
                        Image(systemName: "calendar.badge.clock")
                    }
                    .help(L("定时任务"))
                    .accessibilityLabel(L("定时任务"))
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                    .help(L("连接设置"))
                    .accessibilityLabel(L("连接设置"))
                }
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        store.newSession()
                    } label: {
                        Image(systemName: "square.and.pencil")
                    }
                    .help(L("新对话"))
                    .accessibilityLabel(L("新对话"))
                }
            }
            #endif
            .sheet(isPresented: $showSettings) {
                SettingsView()
            }
            .sheet(isPresented: $showSchedules) {
                SchedulesView()
            }
            .sheet(isPresented: $showKnowledge) {
                KnowledgePanelView()
            }
            .task {
                await store.restoreLocalHistory()
                await store.refreshConnection()
                await store.loadSessions()
                // 杀进程/重启回到原会话（铁律 4 恢复语义）：restoreLocalHistory 读本地
                // 索引选回，loadSessions 再用权威列表校验存在性——此处无需额外动作。
                // 连接状态与手动设置入口已可见；离线阅读不自动打开设置遮挡正文。
            }
    }

    @ViewBuilder
    private var sessionList: some View {
        Group {
            if store.searchResults != nil || store.isSearching {
                searchResultList
            } else {
                normalList
            }
        }
        .scrollContentBackground(.hidden)
        .background(Theme.sidebar)
        .tint(Theme.accent)
        #if os(iOS)
        .searchable(text: $store.searchQuery, prompt: Text(L("搜索会话内容…")))
        #endif
        .onSubmit(of: .search) {
            Task { await store.runSearch() }
        }
        .onChange(of: store.searchQuery) {
            if store.searchQuery.isEmpty { store.clearSearch() }
        }
    }

    #if os(macOS)
    private var sidebarHeader: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "asterisk")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(Theme.accentInk)
                    .frame(width: 28, height: 28)
                    .background(Theme.accent, in: RoundedRectangle(cornerRadius: 9))
                Text("MacAgent").font(.system(size: 16, weight: .semibold))
            }
            .padding(.horizontal, 9)
            // 窗口标题栏已隐藏（hiddenTitleBar），系统三色按钮占侧栏左上约 28pt 高的一条：
            // Logo 行从按钮条下 12pt 起（安全区没推开时由 titleBarClearance 补足那 28pt），
            // 尺寸节奏与原来的 20pt 顶距一致。
            .padding(.top, 12 + titleBarClearance)
            .padding(.bottom, 20)

            Button { store.newSession() } label: {
                Label(L("新对话"), systemImage: "square.and.pencil")
                    .font(.callout.weight(.medium))
                    .frame(maxWidth: .infinity, minHeight: 38)
                    .contentShape(RoundedRectangle(cornerRadius: 9))
            }
            .buttonStyle(CeladonControlStyle(prominent: true, cornerRadius: 9))
            .help(L("新对话"))

            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField(L("搜索会话内容…"), text: $store.searchQuery)
                    .textFieldStyle(.plain)
                    .focused($searchFocused)
                    .font(.callout)
                    .onSubmit { Task { await store.runSearch() } }
                if !store.searchQuery.isEmpty {
                    Button { store.clearSearch() } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .help(L("清除搜索"))
                    .accessibilityLabel(L("清除搜索"))
                }
            }
            .padding(.horizontal, 10)
            .frame(minHeight: 32)
            .background(Theme.wash, in: RoundedRectangle(cornerRadius: 7))
            .overlay(RoundedRectangle(cornerRadius: 7)
                .strokeBorder(searchFocused ? Theme.accent : Color.clear, lineWidth: 1.5))
            .padding(.top, 16)
            .padding(.bottom, 12)
        }
        .padding(.horizontal, 12)
        .background(Theme.sidebar)
    }
    #endif

    private var sidebarFooter: some View {
        VStack(alignment: .leading, spacing: 8) {
            #if os(macOS)
            if store.settings.sidecarEnabled, sidecar.configuration?.incomplete == true {
                Button { showSettings = true } label: {
                    Label(L("算力端尚未配置 · 打开设置"), systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(Theme.warning)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .buttonStyle(.plain)
            }
            #endif
            HStack(spacing: 6) {
                connectionStatus(label: backendCompactLabel, color: connectionColor)
                    .help(connectionLabel)
                    .accessibilityLabel(connectionLabel)
                Rectangle().fill(Theme.separator).frame(width: 1, height: 12)
                connectionStatus(label: store.computeStatusLabel, color: computeColor)
                    .help(store.computeStatusDetail)
                    .accessibilityLabel(store.computeStatusDetail)
                Spacer(minLength: 0)
                #if os(macOS)
                Button { showSettings = true } label: {
                    Image(systemName: "slider.horizontal.3")
                        .frame(width: 28, height: 28)
                        .contentShape(RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(CeladonControlStyle())
                .help(L("连接设置"))
                .accessibilityLabel(L("连接设置"))
                #else
                Text(Bundle.main.appVersionLabel)
                #endif
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 16)
        .padding(.top, 12)
        .padding(.bottom, 12)
        .background(Theme.sidebar)
    }

    private func connectionStatus(label: String, color: Color) -> some View {
        HStack(spacing: 4) {
            Circle().fill(color).frame(width: 5, height: 5)
                .accessibilityHidden(true)
            Text(label).font(.caption2).lineLimit(1).minimumScaleFactor(0.85)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var backendCompactLabel: String {
        switch store.connState {
        case .connected: return store.connectedLabel
        case .engineStarting: return store.engineStartingLabel
        case .connecting: return L("后端连接中")
        case .offline: return L("后端未连接")
        case .unauthorized: return L("后端未认证")
        case .reachable: return L("后端待确认")
        }
    }

    private var computeColor: Color {
        switch store.computeState {
        case .ready: return Theme.success
        case .starting, .probing: return Theme.warning
        case .unavailable, .unreachable, .unauthorized: return Theme.danger
        case .unknown, .unsupported: return Theme.secondaryText
        }
    }

    private func sidebarShortcut(_ title: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label { Text(title).foregroundStyle(.primary) } icon: {
                Image(systemName: icon).foregroundStyle(Theme.secondaryText)
            }
                .font(.callout)
                .frame(maxWidth: .infinity, minHeight: 34, alignment: .leading)
                .padding(.horizontal, 10)
                .contentShape(RoundedRectangle(cornerRadius: 7))
        }
        .buttonStyle(CeladonControlStyle())
        .listRowSeparator(.hidden)
        .listRowBackground(Color.clear)
        .listRowInsets(EdgeInsets(top: 0, leading: 12, bottom: 0, trailing: 12))
    }

    /// 内容搜索结果（回车触发；按会话正文命中，不只标题）。
    private var searchResultList: some View {
        List {
            if store.isSearching {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(L("正在全文搜索…")).font(.caption).foregroundStyle(.secondary)
                }
            }
            ForEach(store.searchResults ?? []) { hit in
                Button {
                    store.select(sessionId: hit.sessionId)
                } label: {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(hit.title).lineLimit(1)
                        Text(hit.snippet)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
                    .modifier(CeladonHoverSurface(selected: store.selectedSessionId == hit.sessionId))
                    .contentShape(RoundedRectangle(cornerRadius: 9))
                }
                .buttonStyle(CeladonControlStyle())
                .listRowSeparator(.hidden)
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 2, leading: 12, bottom: 2, trailing: 12))
            }
            if !store.isSearching, store.searchResults?.isEmpty == true {
                Text(L("没有命中的会话")).font(.caption).foregroundStyle(.secondary)
            }
        }
        .listStyle(.plain)
    }

    @ViewBuilder
    private var normalList: some View {
        // 会话行必须有自己的 Section：裸行会被吸附进上一个分组的卡片里——
        // 实机截图实证「任务」卡与第一条会话行融成一整块，毫无隔断
        #if os(iOS)
        // 编辑态 = 多选（长按菜单「选择多个」进入）：行换成可勾选形态（Button 会
        // 抢编辑态的点选），双指滑动圈选是 List(selection:) 的系统赠品。
        List(selection: $multiSelection) {
            taskCenterSection
            ForEach(sessionGroups) { group in
                Section {
                    if group.id == "today", let draft = draftRow {
                        draftSessionRow(draft)
                    }
                    ForEach(group.items) { item in
                        if editMode == .active {
                            row(item).tag(item.sessionId)
                        } else {
                            Button {
                                store.select(sessionId: item.sessionId)
                            } label: {
                                row(item)
                            }
                            .buttonStyle(CeladonControlStyle(cornerRadius: 9))
                            .contextMenu {
                                rowMenu(item)
                                Button(L("选择多个"), systemImage: "checkmark.circle") {
                                    multiSelection = [item.sessionId]
                                    editMode = .active
                                }
                            }
                        }
                    }
                } header: {
                    sectionHeading(group.title)
                }
                .listRowSeparator(.hidden)
                .listRowBackground(Color.clear)
            }
        }
        .environment(\.editMode, $editMode)
        .toolbar {
            if editMode == .active {
                ToolbarItemGroup(placement: .bottomBar) {
                    Button(role: .destructive) {
                        confirmBulkDelete = true
                    } label: {
                        Text("\(L("删除所选"))（\(multiSelection.count)）")
                    }
                    .disabled(multiSelection.isEmpty || deleting)
                    Spacer()
                    Button(L("完成")) {
                        editMode = .inactive
                        multiSelection.removeAll()
                    }
                }
            }
        }
        .confirmationDialog(bulkDeleteTitle, isPresented: $confirmBulkDelete,
                            titleVisibility: .visible) {
            Button(L("删除"), role: .destructive) { bulkDelete() }
            Button(L("取消"), role: .cancel) {}
        } message: {
            Text(L("删除会话记录不会撤销已经发出的操作。"))
        }
        #else
        // Set 选择 = ⌘ 点选 / ⇧ 范围选免费获得；单选仍即点即入会话（onChange 分流）。
        List(selection: $multiSelection) {
            sidebarTaskNavigation
            sidebarShortcut(L("定时任务"), icon: "calendar") { showSchedules = true }
            sidebarShortcut(L("知识库"), icon: "books.vertical") { showKnowledge = true }
            ForEach(sessionGroups) { group in
                Section {
                    if group.id == "today", let draft = draftRow {
                        draftSessionRow(draft)
                            .tag(store.selectedSessionId ?? "")
                    }
                    ForEach(group.items) { item in
                        row(item)
                            .tag(item.sessionId)
                    }
                } header: {
                    sectionHeading(group.title)
                }
                .listRowSeparator(.hidden)
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 2, leading: 12, bottom: 2, trailing: 12))
            }
        }
        .listStyle(.plain)
        .environment(\.defaultMinListRowHeight, 30)
        .onAppear {
            if let id = store.selectedSessionId, multiSelection.count <= 1 {
                multiSelection = [id]
            }
        }
        // 选区级右键菜单：单选 = 原三项（重命名/置顶/删除），多选 = 批量删除。
        // （行级 .contextMenu 与 forSelectionType 互斥——行级会赢，多选菜单永远出不来）
        .contextMenu(forSelectionType: String.self) { ids in
            if ids.count > 1 {
                Button("\(L("删除所选"))（\(ids.count)）", role: .destructive) {
                    multiSelection = ids
                    confirmBulkDelete = true
                }
            } else if let id = ids.first,
                      let item = store.sessions.first(where: { $0.sessionId == id }) {
                rowMenu(item)
            }
        }
        .onChange(of: multiSelection) {
            if let projected = deletionSelectionProjection {
                deletionSelectionProjection = nil
                if projected == multiSelection { return }
            }
            if multiSelection.count == 1, let id = multiSelection.first,
               id != store.selectedSessionId {
                store.select(sessionId: id)
            }
        }
        // 别处换会话（新对话/搜索/深链）时选区跟随；多选进行中不打扰
        .onChange(of: store.selectedSessionId) {
            if let id = store.selectedSessionId, multiSelection.count <= 1 {
                multiSelection = [id]
            }
        }
        .confirmationDialog(bulkDeleteTitle, isPresented: $confirmBulkDelete,
                            titleVisibility: .visible) {
            Button(L("删除"), role: .destructive) { bulkDelete() }
            Button(L("取消"), role: .cancel) {}
        } message: {
            Text(L("删除会话记录不会撤销已经发出的操作。"))
        }
        #endif
    }

    private var bulkDeleteTitle: String {
        "\(L("确认删除所选会话？"))（\(multiSelection.count)）\(L("（不可恢复）"))"
    }

    /// Only confirmed items leave the selection; a retry contains only remaining failures.
    private func bulkDelete() {
        let items = store.sessions.filter { multiSelection.contains($0.sessionId) }
        Task { await deleteItems(items) }
    }

    private func deleteItems(_ items: [SessionMetaItem]) async {
        guard !deleting, let client = store.client else { return }
        let request = UUID(); deleteRequestID = request; deleting = true
        defer { if deleteRequestID == request { deleting = false } }
        let result = await store.deleteSessions(items)
        guard deleteRequestID == request, store.client === client, !result.obsolete else { return }
        let remaining = multiSelection.subtracting(result.deleted)
        // Receipt projection must not navigate away from a newer active incarnation.
        if store.selectedSessionId != nil, remaining != multiSelection {
            deletionSelectionProjection = remaining
        }
        multiSelection = remaining
        deleteFailures = result.failures
        #if os(iOS)
        if remaining.isEmpty && result.failures.isEmpty { editMode = .inactive }
        #endif
    }

    /// 任务中心只展示全局快照；未知、过期与真正的空任务分别呈现。
    private var taskSummary: String {
        if store.globalTasksFreshness == .unknown { return L("尚未同步") }
        if store.globalTasksFreshness == .stale { return L("待更新") }
        let approvals = store.globalTasks.filter { $0.status == "awaiting_approval" }.count
        if approvals > 0 { return "\(approvals) \(L("待确认"))" }
        if store.globalTasks.isEmpty { return L("无活动任务") }
        return "\(store.globalTasks.count) \(L("活动任务"))"
    }

    #if os(macOS)
    private var sidebarTaskNavigation: some View {
        Group {
            Button { tasksExpanded.toggle() } label: {
                HStack(spacing: 9) {
                    Image(systemName: "checkmark.circle").foregroundStyle(.secondary)
                    Text(L("任务"))
                    Spacer(minLength: 4)
                    Text(taskSummary)
                        .font(.caption)
                        .foregroundStyle(store.globalTasksFreshness == .fresh && store.globalTasks.isEmpty ? Color.secondary : Theme.warning)
                        .lineLimit(1)
                    Image(systemName: tasksExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
                }
                .font(.callout)
                .padding(.horizontal, 10)
                .frame(minHeight: 34)
                .contentShape(RoundedRectangle(cornerRadius: 7))
            }
            .buttonStyle(CeladonControlStyle())
            .accessibilityValue(taskSummary)
            if tasksExpanded { taskCenterContent }
        }
        .listRowSeparator(.hidden)
        .listRowBackground(Color.clear)
        .listRowInsets(EdgeInsets(top: 0, leading: 12, bottom: 0, trailing: 12))
    }
    #endif

    private var taskCenterSection: some View {
        Section {
            DisclosureGroup(isExpanded: $tasksExpanded) {
                taskCenterContent
            } label: {
                HStack {
                    Label(L("任务"), systemImage: "checkmark.circle")
                    Spacer()
                    Text(taskSummary).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .listRowSeparator(.hidden)
        .listRowBackground(Theme.card)
    }

    @ViewBuilder
    private var taskCenterContent: some View {
        if store.globalTasksFreshness == .unknown {
            taskStateNote(L("任务状态尚未确认，请等待状态同步。"), color: .secondary)
        } else if store.globalTasksFreshness == .stale {
            taskStateNote(L("任务状态更新失败，显示最近一次记录。"), color: Theme.warning)
        } else if store.globalTasks.isEmpty {
            taskStateNote(L("当前没有活动任务"), color: .secondary)
        }
        ForEach(store.globalTasks) { task in
            VStack(alignment: .leading, spacing: 7) {
                HStack(alignment: .top, spacing: 8) {
                    taskStatusIcon(task.status).frame(width: 16)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(sessionTitle(task.sessionId)).lineLimit(1).font(.callout)
                        Text(taskDetail(task)).font(.caption).foregroundStyle(.secondary)
                        if !AgentStore.hasControllableChat(kind: task.kind) {
                            Text(L("后台维护，无法确认可控制的对话轮次。"))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .contentShape(Rectangle())
                .onTapGesture { store.select(sessionId: task.sessionId) }
                HStack(spacing: 12) {
                    Spacer(minLength: 0)
                    Group {
                      if AgentStore.hasControllableChat(kind: task.kind) {
                        if task.status == "suspended" {
                            Button(L("恢复")) { store.controlTask(sessionId: task.sessionId, kind: .resume) }
                                .font(.caption).buttonStyle(.borderless)
                        } else if task.status == "running" {
                            Button(L("挂起")) { store.controlTask(sessionId: task.sessionId, kind: .suspend) }
                                .font(.caption).buttonStyle(.borderless)
                        }
                        if AgentStore.canCancelTask(phase: task.status) {
                            Button(L("停止")) { store.controlTask(sessionId: task.sessionId, kind: .cancel) }
                                .font(.caption).buttonStyle(.borderless)
                        }
                      }
                    }
                    .disabled(store.globalTasksFreshness != .fresh || store.controlRequest != nil || store.isSessionDeleting(task.sessionId))
                }
            }
            .padding(10)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 9))
            .padding(.vertical, 3)
        }
    }

    private func taskStateNote(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(color)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
    }

    @ViewBuilder
    private func taskStatusIcon(_ status: String) -> some View {
        switch status {
        case "running":
            ProgressView().controlSize(.small)
        case "queued":
            Image(systemName: "clock").foregroundStyle(Theme.warning)
        case "suspended":
            Image(systemName: "pause.circle").foregroundStyle(Theme.accent)
        case "awaiting_approval":
            Image(systemName: "hand.raised").foregroundStyle(Theme.warning)
        default:
            Image(systemName: "circle")
        }
    }

    private func taskDetail(_ task: TaskHandleInfo) -> String {
        switch task.status {
        case "running":
            let seconds = Int(task.runningSeconds ?? 0)
            return "\(kindLabel(task.kind)) · \(L("已运行")) \(seconds)s"
        case "queued":
            let seconds = Int(task.waitingSeconds ?? 0)
            return "\(L("排队中 · 已等待")) \(seconds)s"
        case "suspended":
            return L("已挂起（进度保留）")
        case "awaiting_approval":
            return L("需要你确认")
        default:
            return task.status
        }
    }

    private func kindLabel(_ kind: String?) -> String {
        switch kind {
        case "chat": return L("对话")
        case "resume": return L("续跑")
        case "summarize": return L("记忆压缩")
        case "prewarm": return L("后台维护")
        case "scheduled": return L("定时任务")
        case "approval": return L("审批续跑")
        default: return kind ?? L("任务")
        }
    }

    private func sessionTitle(_ sessionId: String) -> String {
        let title = store.sessions.first(where: { $0.sessionId == sessionId })?.title
        return title?.isEmpty == false ? title! : sessionId
    }

    @ViewBuilder
    private func rowMenu(_ item: SessionMetaItem) -> some View {
        Group {
            Button(L("重命名")) {
                renameText = item.title ?? ""
                renaming = item
            }
            Button(item.pinned == true ? L("取消置顶保留") : L("置顶保留")) {
                store.togglePin(item)
            }
            Button(L("删除会话"), role: .destructive) {
                Task { await deleteItems([item]) }
            }
            .disabled(deleting || store.isSessionDeleting(item.sessionId) || store.hasSubmissionInFlight(item.sessionId))
            .help(L("删除会话记录不会撤销已经发出的操作。"))
        }
        .disabled(store.isSessionDeleting(item.sessionId))
    }

    private var controlsPlacement: ToolbarItemPlacement {
        #if os(iOS)
        return .topBarLeading
        #else
        return .automatic
        #endif
    }

    /// 三态信号（实机反馈：原来只在设置页关闭时刷一次，静态且滞后）：
    /// 绿=已连接；黄=连接中（探活在跑、还没放弃——含 sidecar 启动窗口）；红=未连接。
    private var connectionColor: Color {
        switch store.connState {
        // 已连接但检索附注在（引擎未装配、RAG 定格失败）：文案带故障，颜色不能是绿
        case .connected: return store.ragNotice == nil ? Theme.success : Theme.warning
        case .connecting, .engineStarting: return Theme.warning
        case .unauthorized, .reachable: return Theme.warning
        case .offline: return .red
        }
    }

    private var connectionLabel: String {
        switch store.connState {
        case .connected: return store.connectedLabel
        case .engineStarting: return store.engineStartingLabel
        case .connecting: return L("连接中…")
        case .offline: return L("未连接")
        case .unauthorized: return L("服务可达 · 请检查连接密钥")
        case .reachable: return L("服务可达 · 业务状态未知")
        }
    }

    /// 尚未落盘的新会话（服务端还没有条目）显示为草稿行。
    private var draftRow: String? {
        guard let sid = store.selectedSessionId,
              !store.sessions.contains(where: { $0.sessionId == sid }) else { return nil }
        return L("新对话")
    }

    private struct SessionGroup: Identifiable {
        let id: String
        let title: String
        let items: [SessionMetaItem]
    }

    private var sessionGroups: [SessionGroup] {
        let calendar = Calendar.current
        let unpinned = store.sessions.filter { $0.pinned != true }
        func bucket(_ item: SessionMetaItem) -> String {
            guard let timestamp = item.lastUpdated ?? item.createdAt else { return "earlier" }
            let date = Date(timeIntervalSince1970: timestamp)
            if calendar.isDateInToday(date) { return "today" }
            if calendar.isDateInYesterday(date) { return "yesterday" }
            return "earlier"
        }
        return [
            SessionGroup(id: "pinned", title: L("置顶"), items: store.sessions.filter { $0.pinned == true }),
            SessionGroup(id: "today", title: L("今天"), items: unpinned.filter { bucket($0) == "today" }),
            SessionGroup(id: "yesterday", title: L("昨天"), items: unpinned.filter { bucket($0) == "yesterday" }),
            SessionGroup(id: "earlier", title: L("更早"), items: unpinned.filter { bucket($0) == "earlier" })
        ].filter { !$0.items.isEmpty || ($0.id == "today" && draftRow != nil) }
    }

    private func sectionHeading(_ title: String) -> some View {
        Text(title)
            .font(.caption)
            .foregroundStyle(.secondary)
            .textCase(nil)
            .padding(.top, 13)
            .padding(.bottom, 5)
            .accessibilityAddTraits(.isHeader)
    }

    private func isSelected(_ id: String) -> Bool {
        #if os(macOS)
        return multiSelection.contains(id)
        #else
        return editMode == .active ? multiSelection.contains(id) : store.selectedSessionId == id
        #endif
    }

    private func draftSessionRow(_ title: String) -> some View {
        // 草稿行的选中态必须跟着真实选区（macOS 的表格选区 / iOS 编辑态多选），
        // 否则列表里会同时出现两处选中底色。
        Label(title, systemImage: "square.and.pencil")
            .font(.callout.weight(.medium))
            .foregroundStyle(Theme.selectionText)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .modifier(CeladonHoverSurface(selected: isSelected(store.selectedSessionId ?? "")))
            #if os(macOS)
            .background(SidebarNativeSelectionStyle().allowsHitTesting(false))
            #endif
    }

    private func row(_ item: SessionMetaItem) -> some View {
        let selected = isSelected(item.sessionId)
        return VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 5) {
                if item.pinned == true {
                    Image(systemName: "pin.fill")
                        .font(.caption2)
                        .foregroundStyle(selected ? Theme.selectionText : Color.secondary)
                }
                Text(item.title?.isEmpty == false ? item.title! : item.sessionId)
                    .font(.body.weight(selected ? .semibold : .regular))
                    .foregroundStyle(selected ? Theme.selectionText : Color.primary)
                    .lineLimit(1)
            }
            HStack(spacing: 5) {
                if let task = store.globalTasks.first(where: { $0.sessionId == item.sessionId }) {
                    taskStatusIcon(task.status)
                        .font(.caption)
                        .controlSize(.mini)
                    Text(store.globalTasksFreshness == .fresh ? taskDetail(task) : L("最近记录") + " · " + taskDetail(task))
                        .lineLimit(1)
                } else {
                    Text("\(item.turnCount ?? 0) \(L("轮"))")
                    if let updated = item.lastUpdated {
                        Text("·")
                        Text(Timestamps.listLabel(Date(timeIntervalSince1970: updated)))
                    }
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .modifier(CeladonHoverSurface(selected: selected))
        .contentShape(RoundedRectangle(cornerRadius: 9))
        #if os(macOS)
        .background(SidebarNativeSelectionStyle().allowsHitTesting(false))
        #endif
    }
}

#if os(macOS)
/// The probe is hosted inside a row, so it can only affect this List's table.
/// Native selection, key navigation and selection context menus remain owned by List.
private struct SidebarNativeSelectionStyle: NSViewRepresentable {
    func makeNSView(context: Context) -> Probe { Probe() }
    func updateNSView(_ nsView: Probe, context: Context) { nsView.updateSelectionStyle() }

    final class Probe: NSView {
        override func viewDidMoveToSuperview() {
            super.viewDidMoveToSuperview()
            updateSelectionStyle()
        }
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            updateSelectionStyle()
        }
        func updateSelectionStyle() {
            applyToAncestorTable()
            // SwiftUI can attach the row host after make/updateNSView returns.
            DispatchQueue.main.async { [weak self] in self?.applyToAncestorTable() }
        }
        private func applyToAncestorTable() {
            var ancestor = superview
            while let view = ancestor {
                if let table = view as? NSTableView {
                    table.selectionHighlightStyle = .none
                    return
                }
                ancestor = view.superview
            }
        }
    }
}
#endif
