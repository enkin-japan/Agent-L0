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

    @StateObject private var listing = RegistrationSetupState()
    private var items: [RegistrationItem]? { listing.items }
    private var failed: Bool { listing.queueError != nil }
    /// 展开看声明全文的注册单 id
    @State private var expanded: Set<String> = []
    /// 正在确认/驳回的注册单 id（操作期禁点，防重复放行）
    @State private var busyId: String?
    @State private var pendingReject: RegistrationItem?
    @State private var errorText: String?
    @State private var notice: String?
    @State private var editor: ApiToolEditorPresentation?
    private var loadedClient: AgentClient? { listing.client }


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
            if items != nil || !listing.tools.isEmpty {
                queueList(items ?? [])
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
        .onChange(of: store.client.map { ObjectIdentifier($0) }) { _, _ in
            listing.invalidate(); editor = nil
            busyId = nil; pendingReject = nil; notice = nil; errorText = nil
            Task { await reload() }
        }
        .safeAreaInset(edge: .top) {
            Button { Task { await reload() } } label: {
                Label(L("刷新"), systemImage: "arrow.clockwise")
            }.padding(6).disabled(busyId != nil)
        }
        .sheet(item: $editor) { presentation in
            ApiToolEditorView(draft: presentation.draft, targetClient: presentation.client,
                presentationIsCurrent: { editor?.id == presentation.id },
                onClose: { if editor?.id == presentation.id { editor = nil } }) { message in
                    notice = message
                    Task { await reload() }
                }.environmentObject(store)
        }
        .onDisappear { if editor == nil { listing.invalidate() } }
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
        await listing.reload(client: store.client, currentClient: { store.client })
    }

    private func confirm(_ item: RegistrationItem) async {
        guard busyId == nil, let client = loadedClient, store.client === client else { return }
        busyId = item.id
        defer { if store.client === client { busyId = nil } }
        let operation = ApiToolOperation(client: client) { store.client === client && listing.isCurrent(client) }
        do {
            let res = try await operation.perform { try await $0.confirmRegistration(id: item.id) }
            let detail = res.detail
            let message: String
            switch res.effect {
            case "registered":
                message = item.isApiTool ? L("已保存并加入工具清单；远程连接尚未验证。") : L("已确认并上架")
            case "saved":
                message = L("已确认并保存")
            // Availability is local configuration checking, not a remote API test.
            case "saved_unavailable", "unavailable":
                message = detail ?? (item.isApiTool
                    ? L("声明已保存，尚未加入工具清单；请补齐密钥等配置后再次保存。远程连接尚未验证。")
                    : L("已保存，但依赖工具尚未就绪。"))
            case "restart_required":
                message = L("已保存。工具继续运行；请在清单查看新配置是否需要重启后生效。")
            case "saved_norebuild":
                message = L("已保存（重启后端后生效）")
            default:
                message = detail ?? L("已确认")
            }
            listing.removePending(item.id)
            errorText = nil
            await reload()
            guard store.client === client else { return }
            if item.isApiTool {
                // Reopen the authoritative saved declaration, never reconstruct a
                // second declaration from the pending form or accept a key in chat.
                let tools = try await operation.perform { try await $0.listApiTools().tools }
                if let saved = tools.first(where: { $0.name == item.name && $0.isUserOwned && $0.problem == nil }) {
                    notice = nil
                    editor = ApiToolEditorPresentation(draft: ApiToolDraft(item: saved), client: client)
                    return
                }
            }
            notice = message
        } catch is CancellationError {
            return
        } catch {
            guard store.client === client else { return }
            errorText = error.localizedDescription
        }
    }

    private func reject(_ item: RegistrationItem) async {
        guard busyId == nil, let client = loadedClient, store.client === client else { return }
        busyId = item.id
        defer { if store.client === client { busyId = nil } }
        let operation = ApiToolOperation(client: client) { store.client === client && listing.isCurrent(client) }
        do {
            _ = try await operation.perform { try await $0.rejectRegistration(id: item.id) }
            listing.removePending(item.id)
            notice = L("已驳回")
            errorText = nil
            await reload()
        } catch is CancellationError {
            return
        } catch {
            guard store.client === client else { return }
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
            if let queueError = listing.queueError {
                Section { Text(L("待确认清单暂不可用：") + queueError).font(.caption).foregroundStyle(.secondary) }
            }
            if let toolsError = listing.toolsError {
                Section { Text(L("继续配置清单暂不可用：") + toolsError).font(.caption).foregroundStyle(.secondary) }
            }
            let unfinished = listing.tools.filter(\.needsSetup)
            if !unfinished.isEmpty {
                Section {
                    ForEach(unfinished) { tool in
                        Button {
                            guard let client = loadedClient, store.client === client else { return }
                            editor = ApiToolEditorPresentation(draft: ApiToolDraft(item: tool), client: client)
                        } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(tool.name ?? tool.file).fontWeight(.medium)
                                Text(tool.registered && tool.runtimeDeclarationCurrent == false
                                     ? L("已保存新配置，运行版本待重启更新") : L("已保存，继续配置密钥并保存"))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }.disabled(busyId != nil)
                    }
                } header: { Text(L("继续完成配置")) } footer: {
                    Text(L("声明已保留，无需重新填写。密钥只在设置表单录入；试调须由你显式发起。"))
                }
            }
            if list.isEmpty && items != nil && !failed {
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
                    Text(L("先审阅并确认保存，再为同一个工具补齐密钥等配置。密钥永远不走对话；试调可能产生实际操作。"))
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

    /// Confirmation saves this declaration before opening its existing settings form.
    private func secretWarning(_ env: String) -> some View {
        Label(L("确认保存后，将为同一个工具配置密钥：") + env,
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
