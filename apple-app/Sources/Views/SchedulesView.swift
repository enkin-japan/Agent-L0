import SwiftUI

/// 定时任务面板：列表 / 启停 / 立即运行 / 中止本次 / 删除 / 运行历史 / 跳转专属会话。
/// 创建与编辑不在这里——那是对话里 schedule_* 工具（经审批卡片）的职责，
/// 本面板只承载「不经过模型的用户控制权」（与 web 端同一设计边界）。
struct SchedulesView: View {
    @EnvironmentObject var store: AgentStore
    @Environment(\.dismiss) private var dismiss

    private var schedules: [ScheduledTask] { store.schedules }
    @State private var expandedRuns = Set<String>()
    @State private var notice: String?
    @State private var acting = false
    @State private var actionRequest = UUID()

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 8) {
                if let notice { Text(notice).font(.caption).foregroundStyle(.secondary).padding(.horizontal) }
                if let error = store.schedulesError { Text(error).font(.caption).foregroundStyle(.orange).padding(.horizontal) }
                Group {
                if store.schedulesLoading && store.schedulesFreshness == .unknown {
                    ProgressView()
                } else if store.schedulesFreshness == .unknown {
                    ContentUnavailableView(L("定时任务状态尚未确认"), systemImage: "questionmark.circle",
                                           description: Text(L("请刷新后查看定时任务状态。")))
                } else if schedules.isEmpty {
                    ContentUnavailableView(L("暂无定时任务"),
                                           systemImage: "calendar.badge.clock",
                                           description: Text(L("在对话里让 Agent 创建，例如「每天早上 9 点帮我总结新闻」")))
                } else {
                    List {
                        ForEach(schedules) { task in
                            row(task)
                        }
                    }
                }
                }
            }
            .navigationTitle(L("定时任务"))
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(L("关闭")) { dismiss() }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task { await reload() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                }
            }
        }
        .task { await reload() }
        .onChange(of: store.client.map { ObjectIdentifier($0) }) {
            actionRequest = UUID(); acting = false; notice = nil; expandedRuns = []
        }
        #if os(macOS)
        .frame(minWidth: 520, minHeight: 420)
        #endif
    }

    private func row(_ task: ScheduledTask) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                if task.running == true {
                    ProgressView().controlSize(.small)
                }
                Text(task.name ?? task.id).bold()
                Spacer()
                Toggle("", isOn: Binding(
                    get: { task.enabled ?? false },
                    set: { enabled in
                        Task { await act(task.id, mutation: .enabled(enabled)) }
                    }
                ))
                .labelsHidden()
                .disabled(acting || !store.canMutateSchedule(task.id))
            }
            HStack(spacing: 8) {
                Text(task.spec?.summary ?? "")
                if let next = task.nextRun {
                    Text(L("下次") + " " + Date(timeIntervalSince1970: next)
                        .formatted(date: .abbreviated, time: .shortened))
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            if let last = task.lastRun {
                runLine(last, prefix: L("上次"))
            }

            if expandedRuns.contains(task.id) {
                if let error = store.scheduleRunsError[task.id] { Text(error).font(.caption).foregroundStyle(.orange) }
                if store.scheduleRunHistory[task.id] == nil && store.scheduleRunsError[task.id] == nil {
                    ProgressView(L("加载中…"))
                }
                let expanded = store.scheduleRunHistory[task.id] ?? []
                ForEach(expanded) { run in
                    runLine(run)
                        .padding(.leading, 12)
                }
            }

            HStack(spacing: 14) {
                if task.running == true {
                    Button(L("中止本次")) {
                        Task { await act(task.id, mutation: .cancelRun) }
                    }
                    .disabled(acting || !store.canMutateSchedule(task.id))
                } else {
                    Button(L("立即运行")) {
                        Task { await act(task.id, mutation: .runNow) }
                    }
                    .disabled(acting || !store.canMutateSchedule(task.id))
                }
                Button(expandedRuns.contains(task.id) ? L("收起历史") : L("运行历史")) {
                    Task { await toggleRuns(task.id) }
                }
                Spacer()
                Button(L("删除"), role: .destructive) {
                    Task { await act(task.id, mutation: .delete) }
                }
                    .disabled(acting || !store.canMutateSchedule(task.id))
            }
            .font(.caption)
            .buttonStyle(.borderless)
        }
        .padding(.vertical, 4)
    }

    private func runLine(_ run: ScheduleRun, prefix: String = "") -> some View {
        HStack(spacing: 6) {
            Text(statusIcon(run.status))
            if let started = run.startedAt {
                Text((prefix.isEmpty ? "" : prefix + " ") + Date(timeIntervalSince1970: started)
                    .formatted(date: .numeric, time: .shortened))
            }
            Text(run.summary ?? "").lineLimit(1)
            Spacer()
            if let sid = run.sessionId {
                Button(L("查看")) {
                    store.select(sessionId: sid)
                    dismiss()
                }
                .buttonStyle(.borderless)
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }

    private func statusIcon(_ status: String?) -> String {
        switch status {
        case "success": return "✅"
        case "fail": return "❌"
        case "cancelled": return "⛔️"
        case "denied_approval": return "🔒"
        case "warning": return "⚠️"
        default: return "•"
        }
    }

    private func toggleRuns(_ id: String) async {
        if expandedRuns.contains(id) { expandedRuns.remove(id); return }
        expandedRuns.insert(id)
        await store.refreshScheduleRuns(id)
    }

    private func act(_ id: String, mutation: AgentStore.ScheduleMutation) async {
        guard !acting, let client = store.client else { return }
        let request = UUID(); actionRequest = request; acting = true
        defer { if actionRequest == request { acting = false } }
        do {
            let result = try await store.mutateSchedule(id, mutation: mutation)
            guard actionRequest == request, store.client === client else { return }
            notice = result.status ? result.response : result.response ?? L("操作未被受理，请检查后重试。")
        } catch {
            guard actionRequest == request, store.client === client else { return }
            notice = L("操作失败：") + error.localizedDescription
        }
        guard store.client === client, actionRequest == request else { return }
        await store.refreshSchedules()
    }

    private func reload() async { await store.refreshSchedules() }
}
