import SwiftUI

/// 顶对齐的提示态（空/离线/失败）：List 撑满高度把导航栏钉在顶部，提示行自上而下
/// 排布、下方留白——不用 ContentUnavailableView 的居中布局（实机反馈：空分类时
/// 标题被挤到屏幕中央）。
private func hintList(_ text: String, icon: String) -> some View {
    List {
        Label(text, systemImage: icon)
            .foregroundStyle(.secondary)
    }
    .scrollDisabled(true)
}

private var loadingList: some View {
    List {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(L("加载中…")).foregroundStyle(.secondary)
        }
    }
    .scrollDisabled(true)
}

/// 知识库管理面板（记忆治理波 P1）：用户对自己 RAG 数据的基础管理权——
/// 一眼看到有哪些分类/文档（含入库日期、过时标记），可删单篇文档或整个分类。
/// 数据全部来自 backend 薄代理（铁律 4：客户端只渲染）；删除在服务端归档先行
/// （原文入 storage/.trash，可手工找回），文案如实告知。
struct KnowledgePanelView: View {
    @EnvironmentObject var store: AgentStore
    #if os(macOS)
    @EnvironmentObject var retier: RetierMonitor
    #endif
    @Environment(\.dismiss) private var dismiss

    /// 嵌入模式（设置页内）：不套自己的 NavigationStack（复用设置页的外层栈推入，
    /// iOS 嵌套双栈会坏导航）、不带「关闭」钮、不定窗口尺寸。
    var embedded = false

    @State private var categories: [RagCategory]?
    @State private var offline = false
    @State private var overviewStale = false
    @State private var serviceStatus: RagRetierStatus?
    @State private var reloadID = UUID()
    @State private var refreshing = false

    var body: some View {
        if embedded {
            content
        } else {
            NavigationStack { content }
            #if os(macOS)
                .frame(minWidth: 480, minHeight: 420)
            #endif
        }
    }

    private var content: some View {
        Group {
            if let categories {
                if categories.isEmpty {
                    hintList(L("知识库为空"), icon: "books.vertical")
                } else {
                    categoryList(categories)
                }
            } else if offline {
                hintList(L("RAG 服务离线"), icon: "wifi.slash")
            } else {
                loadingList
            }
        }
        .navigationTitle(L("知识库"))
        .safeAreaInset(edge: .top) {
            // Keep this in the panel body: an embedded settings sheet can consume
            // the navigation toolbar's primary action slot.
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(capabilityLabel).font(.caption).foregroundStyle(.secondary)
                    if overviewStale {
                        Text(L("知识库列表更新失败，显示最近一次记录。"))
                            .font(.caption).foregroundStyle(.orange)
                    }
                    #if os(macOS)
                    if let progress = retier.rebuildProgress { Text(progress).font(.caption) }
                    if let notice = retier.manualNotice { Text(L("最近一次检查或操作结果：") + notice).font(.caption).foregroundStyle(.secondary) }
                    #endif
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                #if os(macOS)
                Button(L("检查并重建知识库")) {
                    Task { await retier.requestManualRebuild(); await reload() }
                }
                .buttonStyle(.bordered)
                .disabled(retier.isBusy)
                #endif
                Button { Task { await reload() } } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.bordered)
                .accessibilityLabel(L("刷新知识库状态"))
                .help(L("刷新知识库状态"))
                .disabled(refreshing)
            }
            .padding(.horizontal).padding(.vertical, 8)
        }
        .toolbar {
            if !embedded {
                ToolbarItem(placement: .cancellationAction) {
                    Button(L("关闭")) { dismiss() }
                }
            }
        }
        .task { await reload() }
        .onChange(of: store.client.map { ObjectIdentifier($0) }) {
            reloadID = UUID(); refreshing = false; categories = nil; serviceStatus = nil
            offline = false; overviewStale = false
            Task { await reload() }
        }
    }

    private func reload() async {
        guard !refreshing else { return }
        let id = UUID()
        reloadID = id
        refreshing = true
        defer { if reloadID == id { refreshing = false } }
        guard let client = store.client else { offline = true; serviceStatus = nil; return }
        // Management can remain available while computation is offline. A failed
        // computation-status request must not discard a successful document list.
        async let status = try? client.ragRetierStatus()
        do {
            let result = try await client.ragOverview()
            guard reloadID == id, store.client === client else { return }
            categories = result.categories
            offline = false; overviewStale = false
        } catch {
            guard reloadID == id, store.client === client else { return }
            overviewStale = categories != nil
            offline = categories == nil
        }
        let fetched = await status
        guard reloadID == id, store.client === client else { return }
        serviceStatus = fetched
    }

    private var capabilityLabel: String {
        switch serviceStatus?.availability ?? .unknown {
        case .unknown: return L("知识库计算状态未知，刷新后重试。")
        case .computeOffline: return L("知识库计算服务不可达；语义检索与新内容入库暂不可用。可继续管理已加载的文档。")
        case .rebuilding: return L("知识库正在重建，语义检索与入库暂不可用。")
        case .needsRebuild: return L("知识库需要重建，请先重建再使用语义检索。")
        case .reachable: return L("知识库计算服务可达。")
        }
    }

    private func categoryList(_ cats: [RagCategory]) -> some View {
        List {
            let agentCats = cats.filter(\.isAgentMemory)
            let userCats = cats.filter { !$0.isAgentMemory }
            Section(L("我的知识")) {
                ForEach(userCats) { cat in categoryRow(cat) }
            }
            if !agentCats.isEmpty {
                Section {
                    ForEach(agentCats) { cat in categoryRow(cat) }
                } header: {
                    Text(L("Agent 记忆"))
                } footer: {
                    Text(L("这是 Agent 的长期记忆（对话沉淀与笔记镜像）。删除后 Agent 将失去相应回忆；笔记镜像删除后仍会随笔记改写再次生成。"))
                        .font(.caption)
                }
            }
        }
        .refreshable { await reload() }
    }

    private func categoryRow(_ cat: RagCategory) -> some View {
        NavigationLink {
            KnowledgeDocumentsView(category: cat, onChanged: { await reload() })
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Image(systemName: cat.isAgentMemory ? "brain" : "books.vertical")
                        .foregroundStyle(cat.isAgentMemory ? Color.purple : Color.accentColor)
                    Text(cat.name).font(.headline)
                    Spacer()
                    Text("\(cat.docCount) \(L("篇"))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if !cat.description.isEmpty {
                    Text(cat.description)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            .padding(.vertical, 2)
        }
    }
}

/// 单分类的文档清单：标题 / 入库日期 / 切片数 / 体积 / 过时徽章；
/// 滑动或右键删单篇，工具栏删整个分类（均二次确认）。
/// 多选批量删除照会话列表同款：macOS ⌘ 点选/⇧ 范围选（List selection 原生），
/// iOS 长按菜单「选择多个」进编辑态圈选。
struct KnowledgeDocumentsView: View {
    @EnvironmentObject var store: AgentStore
    @Environment(\.dismiss) private var dismiss

    let category: RagCategory
    var onChanged: () async -> Void

    @State private var documents: [RagDocument]?
    @State private var failed = false
    @State private var pendingDelete: RagDocument?
    @State private var confirmPurge = false
    @State private var working = false
    @State private var multiSelection = Set<String>()   // RagDocument.id = sourcePath
    @State private var confirmBulkDelete = false
    @State private var deleteFailures: [String: String] = [:]
    @State private var deleteRequestID = UUID()
    @State private var documentRequestID = UUID()
    #if os(iOS)
    @State private var editMode: EditMode = .inactive
    #endif

    var body: some View {
        Group {
            if let documents {
                if documents.isEmpty {
                    hintList(L("此分类暂无文档"), icon: "doc")
                } else {
                    docList(documents)
                }
            } else if failed {
                hintList(L("加载失败"), icon: "exclamationmark.triangle")
            } else {
                loadingList
            }
        }
        .navigationTitle(category.name)
        .safeAreaInset(edge: .top) {
            if !deleteFailures.isEmpty {
                Text(L("部分项目未确认删除，失败项仍保留选中，可检查后重试。"))
                    .font(.caption).foregroundStyle(.orange).padding(8)
            } else if failed, documents != nil {
                Text(L("文档更新失败，显示最近一次记录。"))
                    .font(.caption).foregroundStyle(.orange).padding(8)
            }
        }
        .onChange(of: store.client.map { ObjectIdentifier($0) }) {
            deleteRequestID = UUID(); documentRequestID = UUID(); working = false
            documents = nil; failed = false; deleteFailures = [:]; multiSelection = []
            Task { await reload() }
        }
        .toolbar {
            ToolbarItem(placement: .destructiveAction) {
                Button(role: .destructive) {
                    confirmPurge = true
                } label: {
                    Label(L("删除分类"), systemImage: "trash")
                }
                .disabled(working)
            }
        }
        .task { await reload() }
        .confirmationDialog(
            "\(L("确认删除所选文档？"))（\(multiSelection.count)）",
            isPresented: $confirmBulkDelete,
            titleVisibility: .visible
        ) {
            Button(L("删除"), role: .destructive) {
                Task { await bulkDelete() }
            }
            Button(L("取消"), role: .cancel) {}
        } message: {
            Text(L("切片将从检索中清除，原文移入回收目录（可手工找回）。"))
        }
        .confirmationDialog(
            L("确认删除文档？"),
            isPresented: Binding(get: { pendingDelete != nil },
                                 set: { if !$0 { pendingDelete = nil } }),
            titleVisibility: .visible
        ) {
            Button(L("删除"), role: .destructive) {
                if let doc = pendingDelete { Task { await delete(doc) } }
            }
        } message: {
            if let doc = pendingDelete {
                Text("\(doc.title)\n\(L("切片将从检索中清除，原文移入回收目录（可手工找回）。"))")
            }
        }
        .confirmationDialog(
            L("确认删除分类及其全部文档？"),
            isPresented: $confirmPurge,
            titleVisibility: .visible
        ) {
            Button(L("删除分类"), role: .destructive) {
                Task { await purge() }
            }
        } message: {
            Text(category.isAgentMemory
                 ? L("这是 Agent 的长期记忆，删除后 Agent 将失去相应回忆。原文移入回收目录（可手工找回）。")
                 : L("该分类的全部文档将从检索中清除，原文移入回收目录（可手工找回）。"))
        }
    }

    private func reload() async {
        let request = UUID(); documentRequestID = request
        guard let client = store.client else { failed = true; return }
        do {
            let result = try await client.ragDocuments(category: category.name)
            guard store.client === client, documentRequestID == request else { return }
            documents = result.documents; failed = false
        } catch {
            guard store.client === client, documentRequestID == request else { return }
            failed = true
        }
    }

    @ViewBuilder
    private func docList(_ docs: [RagDocument]) -> some View {
        #if os(macOS)
        // Set 选择 = ⌘ 点选 / ⇧ 范围选免费获得。选区级右键菜单：多选=批量删，
        // 单选=删该篇（行级 .contextMenu 与 forSelectionType 互斥，行级会赢——
        // 会话列表同一坑，见 SessionListView）。
        List(selection: $multiSelection) {
            ForEach(docs) { doc in
                docRow(doc).tag(doc.id)
            }
        }
        .contextMenu(forSelectionType: String.self) { ids in
            if ids.count > 1 {
                Button("\(L("删除所选"))（\(ids.count)）", role: .destructive) {
                    multiSelection = ids
                    confirmBulkDelete = true
                }
            } else if let id = ids.first, let doc = docs.first(where: { $0.id == id }) {
                Button(role: .destructive) { pendingDelete = doc } label: {
                    Label(L("删除文档"), systemImage: "trash")
                }
            }
        }
        .refreshable { await reload() }
        #else
        // 编辑态 = 多选（长按菜单「选择多个」进入），行换可勾选形态；
        // 非编辑态保留右滑删除与长按菜单。
        List(selection: $multiSelection) {
            ForEach(docs) { doc in
                if editMode == .active {
                    docRow(doc).tag(doc.id)
                } else {
                    docRow(doc)
                        .swipeActions(edge: .trailing) {
                            Button(role: .destructive) { pendingDelete = doc } label: {
                                Label(L("删除"), systemImage: "trash")
                            }
                        }
                        .contextMenu {
                            Button(role: .destructive) { pendingDelete = doc } label: {
                                Label(L("删除文档"), systemImage: "trash")
                            }
                            Button(L("选择多个"), systemImage: "checkmark.circle") {
                                multiSelection = [doc.id]
                                editMode = .active
                            }
                        }
                }
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
                    .disabled(multiSelection.isEmpty || working)
                    Spacer()
                    Button(L("完成")) {
                        editMode = .inactive
                        multiSelection.removeAll()
                    }
                }
            }
        }
        .refreshable { await reload() }
        #endif
    }

    private func docRow(_ doc: RagDocument) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(doc.title).font(.body).lineLimit(1)
                if !doc.stale.isEmpty {
                    Text(L("已标记过时"))
                        .font(.caption2)
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(Color.orange.opacity(0.2), in: Capsule())
                        .foregroundStyle(.orange)
                        .help(doc.stale)
                }
                Spacer()
                if !doc.createdDate.isEmpty {
                    Text(doc.createdDate)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            HStack(spacing: 8) {
                Text("\(doc.chunks) \(L("切片"))")
                if doc.sizeBytes > 0 {
                    Text(ByteCountFormatter.string(fromByteCount: Int64(doc.sizeBytes),
                                                   countStyle: .file))
                }
                if doc.chunks == 0 {
                    // 孤儿文件（有原文无切片=入库残缺）：如实标出，用户可删后重导
                    Text(L("未索引")).foregroundStyle(.orange)
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            if let reason = deleteFailures[doc.sourcePath] { Text(reason).font(.caption2).foregroundStyle(.orange) }
        }
        .padding(.vertical, 2)
    }

    private func delete(_ doc: RagDocument) async { await deletePaths([doc.sourcePath]) }

    private func bulkDelete() async { await deletePaths(Array(multiSelection)) }

    private func deletePaths(_ paths: [String]) async {
        guard !working, let client = store.client else { return }
        let request = UUID(); deleteRequestID = request; working = true
        defer { if deleteRequestID == request { working = false } }
        let result = await store.deleteKnowledgeDocuments(paths)
        guard deleteRequestID == request, store.client === client, !result.obsolete else { return }
        deleteFailures = result.failures
        multiSelection.subtract(result.deleted)
        documents?.removeAll { result.deleted.contains($0.sourcePath) }
        #if os(iOS)
        if multiSelection.isEmpty && result.failures.isEmpty { editMode = .inactive }
        #endif
        await reload()
        guard deleteRequestID == request, store.client === client else { return }
        await onChanged()
    }

    private func purge() async {
        working = true
        defer { working = false }
        do {
            try await store.client?.ragPurgeCategory(category.name)
            await onChanged()
            dismiss()
        } catch {
            failed = true
        }
    }
}
