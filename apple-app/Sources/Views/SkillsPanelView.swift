import SwiftUI

/// 剧本管理面板（用户以 /名字 点名的流程提示词的表单面）。
/// 骨架照 ApiToolsPanelView：嵌入模式（embedded）、列表首行添加按钮 Section
/// （macOS 设置 sheet 的 toolbar 被取消/保存占据，嵌入面板的 toolbar 项浮不出来
/// ——添加入口必须做成列表行）、行内徽章、confirmationDialog 删除、
/// sheet(item:) 编辑器、macOS .formStyle(.grouped)。
/// 仓库内置剧本（source=repo）只读展示；UI 手写的（source=user）可编辑/删除。
private func skillsHintList(_ text: String, icon: String) -> some View {
    List {
        Label(text, systemImage: icon).foregroundStyle(.secondary)
    }
    .scrollDisabled(true)
}

private var skillsLoadingList: some View {
    List {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(L("加载中…")).foregroundStyle(.secondary)
        }
    }
    .scrollDisabled(true)
}

// MARK: - 表单草稿

struct SkillDraft: Identifiable {
    var originalName: String?    // 非 nil = 编辑现有剧本（name 锁定）
    var name = ""
    var description = ""
    var body = ""
    var requires = ""            // 逗号/空格分隔，保存时拆成数组

    var id: String { originalName ?? "＋new" }

    init() {}

    /// 从清单条目回填。user 来源的可用条目自带 body/requires（listing 契约）；
    /// 万一为 nil（舍弃条目/旧后端），正文留空——空正文禁存，不会静默清掉原剧本。
    init(item: SkillItem) {
        originalName = item.name
        name = item.name
        description = item.description
        body = item.body ?? ""
        requires = (item.requires ?? []).joined(separator: ", ")
    }

    /// 名字预检：小写字母/数字开头，后接小写字母、数字或连字符。
    var nameValid: Bool {
        name.range(of: "^[a-z0-9][a-z0-9-]*$", options: .regularExpression) != nil
    }

    func toBody() -> AgentClient.SkillSaveBody {
        let reqs = requires
            .split(whereSeparator: { $0 == "," || $0 == " " || $0 == "\n" || $0 == "\t" })
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        return AgentClient.SkillSaveBody(
            name: name.trimmingCharacters(in: .whitespaces),
            description: description,
            body: body,
            requires: reqs.isEmpty ? nil : reqs)
    }
}

// MARK: - 面板（清单）

struct SkillsPanelView: View {
    @EnvironmentObject var store: AgentStore
    @Environment(\.dismiss) private var dismiss
    var embedded = false

    @State private var skills: [SkillItem]?
    @State private var failed = false
    @State private var draft: SkillDraft?
    @State private var pendingDelete: SkillItem?
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
            if let skills {
                skillList(skills)
            } else if failed {
                skillsHintList(L("加载失败"), icon: "exclamationmark.triangle")
            } else {
                skillsLoadingList
            }
        }
        .navigationTitle(L("剧本"))
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { draft = SkillDraft() } label: { Image(systemName: "plus") }
                    .help(L("添加剧本"))
            }
            if !embedded {
                ToolbarItem(placement: .cancellationAction) {
                    Button(L("关闭")) { dismiss() }
                }
            }
        }
        .task { await reload() }
        .sheet(item: $draft) { d in
            SkillEditorView(draft: d) { message in
                notice = message
                Task { await reload() }
            }
            .environmentObject(store)
        }
        .confirmationDialog(L("确认删除该剧本？"), isPresented: .init(
            get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }),
            titleVisibility: .visible) {
            Button(L("删除"), role: .destructive) {
                if let item = pendingDelete { Task { await remove(item) } }
            }
        } message: {
            Text(L("删除后立即下架，该 /名字 不再可点名。"))
        }
        .alert(notice ?? "", isPresented: .init(
            get: { notice != nil }, set: { if !$0 { notice = nil } })) {
            Button(L("好")) { notice = nil }
        }
    }

    private func reload() async {
        do {
            skills = try await store.client?.skills() ?? []
            failed = false
        } catch {
            skills = nil
            failed = true
        }
    }

    private func remove(_ item: SkillItem) async {
        do {
            let res = try await store.client?.deleteSkill(name: item.name)
            switch res?.effect {
            case "removed": notice = L("已删除并下架")
            case "removed_norebuild": notice = L("已删除（重启后端后下架）")
            default: notice = L("已删除")
            }
            await reload()
        } catch {
            notice = error.localizedDescription
        }
    }

    private func skillList(_ items: [SkillItem]) -> some View {
        List {
            Section {
                Button { draft = SkillDraft() } label: {
                    Label(L("添加剧本"), systemImage: "plus.circle")
                }
                #if os(macOS)
                .buttonStyle(.plain).foregroundStyle(.tint)
                #endif
            }
            if items.isEmpty {
                Section {
                    Label(L("还没有剧本"), systemImage: "book.closed")
                        .foregroundStyle(.secondary)
                }
            } else {
                Section {
                    ForEach(items) { item in row(item) }
                } footer: {
                    Text(L("剧本 = 用户以 /名字 点名的流程提示词。仓库内置的只读；在这里添加的保存于数据目录。"))
                        .font(.caption)
                }
            }
        }
    }

    private func row(_ item: SkillItem) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(item.name).fontWeight(.medium)
                Spacer()
                badges(item)
            }
            if !item.description.isEmpty {
                Text(item.description).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture {
            if item.isUserOwned { draft = SkillDraft(item: item) }
        }
        .contextMenu {
            if item.isUserOwned {
                Button(L("编辑")) { draft = SkillDraft(item: item) }
                Button(L("删除"), role: .destructive) { pendingDelete = item }
            }
        }
    }

    @ViewBuilder
    private func badges(_ item: SkillItem) -> some View {
        if item.isUserOwned {
            Text(L("手写")).font(.caption2).foregroundStyle(.tint)
        } else {
            Text(L("内置")).font(.caption2).foregroundStyle(.secondary)
        }
        if item.available == false {
            Label(L("缺依赖"), systemImage: "exclamationmark.triangle")
                .font(.caption).foregroundStyle(.orange).labelStyle(.titleAndIcon)
                .help((item.missing ?? []).isEmpty
                      ? L("缺依赖")
                      : L("缺依赖：") + (item.missing ?? []).joined(separator: ", "))
        }
    }
}

// MARK: - 编辑表单（必填/可选分区）

struct SkillEditorView: View {
    @EnvironmentObject var store: AgentStore
    @Environment(\.dismiss) private var dismiss

    @State var draft: SkillDraft
    var onSaved: (String) -> Void

    @State private var saving = false
    @State private var errorText: String?

    private var bodyEmpty: Bool {
        draft.body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        NavigationStack {
            form
                .navigationTitle(draft.originalName == nil ? L("添加剧本") : L("编辑剧本"))
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button(L("取消")) { dismiss() }
                    }
                    ToolbarItem(placement: .confirmationAction) {
                        Button(L("保存")) { Task { await save() } }
                            .disabled(saving || !draft.nameValid
                                      || draft.description.isEmpty || bodyEmpty)
                    }
                }
        }
        #if os(macOS)
        .frame(minWidth: 540, minHeight: 600)
        #endif
    }

    private var form: some View {
        Form {
            Section {
                TextField(L("名字（小写字母数字与连字符）"), text: $draft.name)
                    .disabled(draft.originalName != nil)
                    #if os(iOS)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    #endif
                if !draft.name.isEmpty && !draft.nameValid {
                    Text(L("名字只能小写字母或数字开头，后接小写字母、数字或连字符。"))
                        .font(.caption).foregroundStyle(.orange)
                }
                TextField(L("说明（一句话点明这套流程做什么）"), text: $draft.description, axis: .vertical)
                    .lineLimit(2...4)
                TextEditor(text: $draft.body)
                    .font(.system(.body, design: .monospaced))
                    .frame(minHeight: 200)
            } header: { Text(L("必填")) } footer: {
                Text(L("正文 = 点名 /名字 时注入的流程提示词。"))
            }

            Section {
                TextField(L("依赖工具（逗号或空格分隔）"), text: $draft.requires)
                    #if os(iOS)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    #endif
            } header: { Text(L("可选")) } footer: {
                Text(L("列出该流程需要的工具名；缺任一个则剧本探活不过，候选里灰显为「缺依赖」。"))
            }

            if let errorText {
                Section { Text(errorText).foregroundStyle(.red).font(.caption) }
            }
        }
        #if os(macOS)
        .formStyle(.grouped)
        #endif
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            let res = try await store.client?.saveSkill(draft.toBody())
            let message: String
            switch res?.effect {
            case "registered": message = L("已保存并立即生效")
            case "saved_unavailable": message = res?.detail ?? L("已保存（缺依赖，补齐后可用）")
            case "saved_norebuild": message = L("已保存（重启后端后生效）")
            default: message = L("已保存")
            }
            dismiss()
            onSaved(message)
        } catch {
            errorText = error.localizedDescription
        }
    }
}
