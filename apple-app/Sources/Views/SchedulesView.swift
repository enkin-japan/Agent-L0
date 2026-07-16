import SwiftUI

/// 定时任务面板：列表 / 启停 / 立即运行 / 中止本次 / 删除 / 运行历史 / 跳转专属会话。
/// 创建与编辑不在这里——那是对话里 schedule_* 工具（经审批卡片）的职责，
/// 本面板只承载「不经过模型的用户控制权」（与 web 端同一设计边界）。
struct SchedulesView: View {
    @EnvironmentObject var store: AgentStore
    @Environment(\.dismiss) private var dismiss

    @State private var schedules: [ScheduledTask] = []
    @State private var runs: [String: [ScheduleRun]] = [:]
    @State private var notice: String?
    @State private var loading = true

    var body: some View {
        NavigationStack {
            Group {
                if loading && schedules.isEmpty {
                    ProgressView()
                } else if schedules.isEmpty {
                    ContentUnavailableView(L("暂无定时任务"),
                                           systemImage: "calendar.badge.clock",
                                           description: Text(L("在对话里让 Agent 创建，例如「每天早上 9 点帮我总结新闻」")))
                } else {
                    List {
                        if let notice {
                            Text(notice)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        ForEach(schedules) { task in
                            row(task)
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
                        Task { await act { try await store.client?.toggleSchedule(id: task.id, enabled: enabled) } }
                    }
                ))
                .labelsHidden()
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

            if let expanded = runs[task.id] {
                ForEach(expanded) { run in
                    runLine(run)
                        .padding(.leading, 12)
                }
            }

            HStack(spacing: 14) {
                if task.running == true {
                    Button(L("中止本次")) {
                        Task { await act { try await store.client?.cancelScheduleRun(id: task.id) } }
                    }
                } else {
                    Button(L("立即运行")) {
                        Task { await act { try await store.client?.runScheduleNow(id: task.id) } }
                    }
                }
                Button(runs[task.id] == nil ? L("运行历史") : L("收起历史")) {
                    Task { await toggleRuns(task.id) }
                }
                Spacer()
                Button(L("删除"), role: .destructive) {
                    Task { await act { try await store.client?.deleteSchedule(id: task.id) } }
                }
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
        if runs[id] != nil {
            runs[id] = nil
            return
        }
        runs[id] = (try? await store.client?.scheduleRuns(id: id)) ?? []
    }

    /// 执行动作 → 显示后端回话 → 刷新列表。
    private func act(_ action: () async throws -> ScheduleActionResponse?) async {
        do {
            let res = try await action()
            notice = res?.response
        } catch {
            notice = L("操作失败：") + error.localizedDescription
        }
        await reload()
    }

    private func reload() async {
        loading = true
        schedules = (try? await store.client?.listSchedules()) ?? []
        loading = false
    }
}
