import SwiftUI

/// 待确认注册审批队列面板（对话式注册的人工闸）。
/// 模型在对话里起草的工具/剧本声明落进后端的待确认区，本面板是**唯一**放行口
/// （后端红线：模型没有确认工具，确认只在设置页）。确认走后端与表单面同一条
/// 保存/上架实现，客户端只负责审阅、转述 effect、刷新。
/// 骨架照 SkillsPanelView：embedded 嵌入模式、加载/空/失败三态、行内错误横幅、
/// confirmationDialog 驳回、alert 回显结果文案。
private func registrationsHintList(_ text: String, icon: String) -> some View {
    List {
        Label(text, systemImage: icon).foregroundStyle(.secondary)
    }
    .scrollDisabled(true)
}

private var registrationsLoadingList: some View {
    List {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(L("加载中…")).foregroundStyle(.secondary)
        }
    }
    .scrollDisabled(true)
}

// MARK: - 面板（队列）

struct RegistrationsPanelView: View {
    @EnvironmentObject var store: AgentStore
    @Environment(\.dismiss) private var dismiss
    var embedded = false

    @State private var items: [RegistrationItem]?
    @State private var failed = false
    /// 展开看声明全文的注册单 id
    @State private var expanded: Set<String> = []
    /// 正在确认/驳回的注册单 id（操作期禁点，防重复放行）
    @State private var busyId: String?
    @State private var pendingReject: RegistrationItem?
    @State private var errorText: String?
    @State private var notice: String?

    var body: some View {
        if embedded {
            content
        } else {
            NavigationStack { content }
            #if os(macOS)
                .frame(minWidth: 520, minHeight: 440)
            #endif
        }
    }

    private var content: some View {
        Group {
            if let items {
                queueList(items)
            } else if failed {
                registrationsHintList(L("加载失败"), icon: "exclamationmark.triangle")
            } else {
                registrationsLoadingList
            }
        }
        .navigationTitle(L("待确认"))
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { Task { await reload() } } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help(L("刷新"))
            }
            if !embedded {
                ToolbarItem(placement: .cancellationAction) {
                    Button(L("关闭")) { dismiss() }
                }
            }
        }
        .task { await reload() }
        .confirmationDialog(L("确认驳回该注册单？"), isPresented: .init(
            get: { pendingReject != nil }, set: { if !$0 { pendingReject = nil } }),
            titleVisibility: .visible) {
            Button(L("驳回"), role: .destructive) {
                if let item = pendingReject { Task { await reject(item) } }
            }
        } message: {
            Text(L("驳回即删除这份草稿，什么都不会生效；模型可以重新起草。"))
        }
        .alert(notice ?? "", isPresented: .init(
            get: { notice != nil }, set: { if !$0 { notice = nil } })) {
            Button(L("好")) { notice = nil }
        }
    }

    // MARK: 数据

    private func reload() async {
        do {
            items = try await store.client?.listRegistrations() ?? []
            failed = false
        } catch {
            items = nil
            failed = true
        }
    }

    private func confirm(_ item: RegistrationItem) async {
        busyId = item.id
        defer { busyId = nil }
        do {
            let res = try await store.client?.confirmRegistration(id: item.id)
            let detail = res?.detail
            switch res?.effect {
            case "registered":
                notice = L("已确认并上架")
            case "saved":
                notice = L("已确认并保存")
            // 探活未过：后端把原因写在 detail 里，原话转述（多半是密钥没录）
            case "saved_unavailable", "unavailable":
                notice = detail ?? L("已保存但探活未过（检查密钥），补齐后重扫可上架")
            case "restart_required":
                notice = L("已保存；该工具已在运行中，重启后端后生效")
            case "saved_norebuild":
                notice = L("已保存（重启后端后生效）")
            default:
                notice = detail ?? L("已确认")
            }
            errorText = nil
            await reload()
        } catch {
            errorText = error.localizedDescription
        }
    }

    private func reject(_ item: RegistrationItem) async {
        busyId = item.id
        defer { busyId = nil }
        do {
            _ = try await store.client?.rejectRegistration(id: item.id)
            notice = L("已驳回")
            errorText = nil
            await reload()
        } catch {
            errorText = error.localizedDescription
        }
    }

    // MARK: 列表

    private func queueList(_ list: [RegistrationItem]) -> some View {
        List {
            if let errorText {
                Section {
                    Label(errorText, systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(Theme.danger)
                        .textSelection(.enabled)
                }
            }
            if list.isEmpty {
                Section {
                    Label(L("没有待确认的注册单"), systemImage: "tray")
                        .foregroundStyle(.secondary)
                } footer: {
                    Text(L("模型在对话里起草的工具/剧本声明会排队到这里，经你确认后才生效。"))
                        .font(.caption)
                }
            } else {
                Section {
                    ForEach(list) { item in card(item) }
                } footer: {
                    Text(L("模型起草的声明不会自动生效——确认才上架，驳回即丢弃。密钥永远不走对话，只在 API 工具页的密钥框录入。"))
                        .font(.caption)
                }
            }
        }
    }

    // MARK: 单张注册单

    private func card(_ item: RegistrationItem) -> some View {
        VStack(alignment: .leading, spacing: Theme.s2) {
            HStack(spacing: Theme.s2) {
                kindBadge(item)
                Text(item.name).fontWeight(.medium)
                Spacer()
                if let at = item.proposedAt, !at.isEmpty {
                    Text(at).font(.caption2).foregroundStyle(.secondary)
                }
            }
            if let summary = item.summary {
                Text(summary).font(.caption).foregroundStyle(.secondary)
            }
            if let note = item.note, !note.isEmpty {
                Label(note, systemImage: "text.bubble")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if item.secretMissing, let env = item.authEnv {
                secretWarning(env)
            }
            disclosureToggle(item)
            if expanded.contains(item.id) { details(item) }
            actions(item)
        }
        .padding(.vertical, Theme.s1)
    }

    private func kindBadge(_ item: RegistrationItem) -> some View {
        Text(item.isApiTool ? L("API 工具") : L("剧本"))
            .font(.caption2)
            .padding(.horizontal, 4).padding(.vertical, 1)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 3))
    }

    /// 密钥未录的显眼警示：确认能过，但工具探活多半不通——先去密钥框补值再确认。
    private func secretWarning(_ env: String) -> some View {
        Label(L("确认前请先到 API 工具页密钥框为 ") + env + L(" 录入密钥"),
              systemImage: "key")
            .font(.caption)
            .foregroundStyle(Theme.warning)
            .textSelection(.enabled)
            .padding(Theme.s2)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.warning.opacity(0.12),
                        in: RoundedRectangle(cornerRadius: Theme.radiusSmall))
    }

    private func disclosureToggle(_ item: RegistrationItem) -> some View {
        let open = expanded.contains(item.id)
        return Button {
            if open { expanded.remove(item.id) } else { expanded.insert(item.id) }
        } label: {
            Label(open ? L("收起声明") : L("查看声明全文"),
                  systemImage: open ? "chevron.down" : "chevron.right")
                .font(.caption)
        }
        .buttonStyle(.borderless)
        .foregroundStyle(Theme.accent)
    }

    /// 展开区：先给要点（api_tool 的地址/方法/参数键、skill 的正文），再给全文。
    private func details(_ item: RegistrationItem) -> some View {
        VStack(alignment: .leading, spacing: Theme.s1) {
            if item.isApiTool {
                detailLine(L("地址"), item.endpoint ?? "—")
                detailLine(L("方法"), item.method ?? "GET")
                detailLine(L("参数"), item.paramKeys.isEmpty
                           ? L("无") : item.paramKeys.joined(separator: ", "))
                if let env = item.authEnv { detailLine(L("密钥变量"), env) }
            } else if let skillBody = item.skillBody {
                Text(L("正文")).font(.caption).foregroundStyle(.secondary)
                Text(skillBody)
                    .font(.caption.monospaced())
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Text(L("声明全文")).font(.caption).foregroundStyle(.secondary)
            ScrollView {
                Text(item.payloadText)
                    .font(.caption.monospaced())
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 220)
        }
        .padding(Theme.s2)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.wash, in: RoundedRectangle(cornerRadius: Theme.radiusSmall))
    }

    private func detailLine(_ label: String, _ value: String) -> some View {
        HStack(alignment: .top, spacing: Theme.s2) {
            Text(label)
                .font(.caption).foregroundStyle(.secondary)
                .frame(width: 64, alignment: .leading)
            Text(value)
                .font(.caption.monospaced())
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func actions(_ item: RegistrationItem) -> some View {
        HStack(spacing: Theme.s4) {
            if busyId == item.id {
                ProgressView().controlSize(.small)
                Text(L("处理中…")).font(.caption).foregroundStyle(.secondary)
            } else {
                Button { Task { await confirm(item) } } label: {
                    Label(L("确认"), systemImage: "checkmark.circle")
                }
                .foregroundStyle(Theme.success)
                Button(role: .destructive) { pendingReject = item } label: {
                    Label(L("驳回"), systemImage: "xmark.circle")
                }
            }
            Spacer()
        }
        .font(.callout)
        .buttonStyle(.borderless)
        .disabled(busyId != nil)
    }
}
