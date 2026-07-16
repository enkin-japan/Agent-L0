import SwiftUI

/// 侧栏会话列表：后端磁盘真相（/api/v1/sessions），pin 置顶，右键/长按删除与置顶。
/// 全局控件（定时任务/设置/新对话）挂本视图 toolbar（容器级 toolbar 在 iPhone 不渲染）。
/// 行交互平台分流：macOS 用 List selection（双栏高亮语义）；iOS 行是 Button →
/// store.select → navigationDestination 推入（selection 绑定在 iPhone 上同值重点不触发导航）。
struct SessionListView: View {
    @EnvironmentObject var store: AgentStore
    @State private var showSettings = false
    @State private var showSchedules = false
    @State private var renaming: SessionMetaItem?
    @State private var renameText = ""

    var body: some View {
        sessionList
            // 下拉刷新是 iOS 习语；macOS 上刷新控件收起与 List 更新天然打架
            //（实机反馈：等值守卫后仍有残影）——macOS 撤掉，改由探活循环顺带
            // 自动刷新列表（15s 一巡 + 等值守卫 = 不变零重建）
            #if os(iOS)
            .refreshable { await store.loadSessions() }
            #endif
            #if os(macOS)
            // 高频入口不许折叠：窄侧栏下 toolbar 图标会收进 » 溢出菜单（实机反馈
            // 「添加新会话折叠起来了」）——顶上放一枚全宽主色按钮，toolbar 那枚保留。
            .safeAreaInset(edge: .top) {
                Button {
                    store.newSession()
                } label: {
                    Label(L("新对话"), systemImage: "square.and.pencil")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .padding(.horizontal, Theme.s3)
                .padding(.vertical, Theme.s2)
                .background(.bar)
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
            .safeAreaInset(edge: .bottom) {
                HStack(spacing: 6) {
                    Circle()
                        .fill(connectionColor)
                        .frame(width: 7, height: 7)
                    Text(connectionLabel)
                    Spacer()
                    Text(Bundle.main.appVersionLabel)
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(.bar)
            }
            .toolbar {
                ToolbarItemGroup(placement: controlsPlacement) {
                    Button {
                        showSchedules = true
                    } label: {
                        Image(systemName: "calendar.badge.clock")
                    }
                    .help(L("定时任务"))
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                    .help(L("连接设置"))
                }
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        store.newSession()
                    } label: {
                        Image(systemName: "square.and.pencil")
                    }
                    .help(L("新对话"))
                }
            }
            .sheet(isPresented: $showSettings) {
                SettingsView()
            }
            .sheet(isPresented: $showSchedules) {
                SchedulesView()
            }
            .task {
                await store.refreshConnection()
                await store.loadSessions()
                store.restoreLastSession()   // 杀进程/重启回到原会话（铁律 4 恢复语义）
                // sidecar 开着时冷启必然有几秒「未连接」窗口——弹设置页是误伤，
                // 底部黄灯（连接中）已说明状态；真故障由 SidecarManager 的五态呈现
                if store.connectionOK != true, !store.settings.sidecarEnabled {
                    showSettings = true
                }
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
        .searchable(text: $store.searchQuery, prompt: Text(L("搜索会话内容…")))
        .onSubmit(of: .search) {
            Task { await store.runSearch() }
        }
        .onChange(of: store.searchQuery) {
            if store.searchQuery.isEmpty { store.clearSearch() }
        }
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
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            if !store.isSearching, store.searchResults?.isEmpty == true {
                Text(L("没有命中的会话")).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var normalList: some View {
        #if os(iOS)
        List {
            taskCenterSection
            if let draft = draftRow {
                Label(draft, systemImage: "square.and.pencil")
            }
            ForEach(store.sessions) { item in
                Button {
                    store.select(sessionId: item.sessionId)
                } label: {
                    row(item)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .contextMenu { rowMenu(item) }
            }
        }
        #else
        List(selection: Binding(
            get: { store.selectedSessionId },
            set: { newValue in
                if let id = newValue { store.select(sessionId: id) }
            }
        )) {
            taskCenterSection
            if let draft = draftRow {
                Label(draft, systemImage: "square.and.pencil")
                    .tag(store.selectedSessionId ?? "")
            }
            ForEach(store.sessions) { item in
                row(item)
                    .tag(item.sessionId)
                    .contextMenu { rowMenu(item) }
            }
        }
        #endif
    }

    /// 任务中心：全局调度快照（跨会话的跑/排队/挂起），体现「重 task 轻会话」。
    @ViewBuilder
    private var taskCenterSection: some View {
        if !store.globalTasks.isEmpty {
            Section(L("任务")) {
                ForEach(store.globalTasks) { task in
                    HStack(spacing: 8) {
                        taskStatusIcon(task.status)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(sessionTitle(task.sessionId)).lineLimit(1).font(.callout)
                            Text(taskDetail(task))
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        if task.status == "suspended" {
                            Button(L("恢复")) {
                                store.resumeTask(sessionId: task.sessionId)
                            }
                            .font(.caption)
                            .buttonStyle(.borderless)
                        }
                    }
                    .contentShape(Rectangle())
                    .onTapGesture { store.select(sessionId: task.sessionId) }
                }
            }
        }
    }

    @ViewBuilder
    private func taskStatusIcon(_ status: String) -> some View {
        switch status {
        case "running":
            ProgressView().controlSize(.small)
        case "queued":
            Image(systemName: "clock").foregroundStyle(.orange)
        case "suspended":
            Image(systemName: "pause.circle").foregroundStyle(.blue)
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
        default:
            return task.status
        }
    }

    private func kindLabel(_ kind: String?) -> String {
        switch kind {
        case "chat": return L("对话")
        case "resume": return L("续跑")
        case "summarize": return L("记忆压缩")
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
                store.deleteSession(item)
            }
        }
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
        case .connected: return Theme.success
        case .connecting: return Theme.warning
        case .offline: return .red
        }
    }

    private var connectionLabel: String {
        switch store.connState {
        case .connected: return L("已连接")
        case .connecting: return L("连接中…")
        case .offline: return L("未连接")
        }
    }

    /// 尚未落盘的新会话（服务端还没有条目）显示为草稿行。
    private var draftRow: String? {
        guard let sid = store.selectedSessionId,
              !store.sessions.contains(where: { $0.sessionId == sid }) else { return nil }
        return L("新对话")
    }

    private func row(_ item: SessionMetaItem) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                if item.pinned == true {
                    Image(systemName: "pin.fill")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                }
                Text(item.title?.isEmpty == false ? item.title! : item.sessionId)
                    .lineLimit(1)
                // 调度状态徽标（跑/排队/挂起）
                if let task = store.globalTasks.first(where: { $0.sessionId == item.sessionId }) {
                    switch task.status {
                    case "running": ProgressView().controlSize(.mini)
                    case "queued": Image(systemName: "clock").font(.caption2).foregroundStyle(.orange)
                    case "suspended": Image(systemName: "pause.circle").font(.caption2).foregroundStyle(.blue)
                    default: EmptyView()
                    }
                }
            }
            HStack(spacing: 6) {
                Text("\(item.turnCount ?? 0) \(L("轮"))")
                if let updated = item.lastUpdated {
                    Text(Date(timeIntervalSince1970: updated), style: .relative)
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }
}
