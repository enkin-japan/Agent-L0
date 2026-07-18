import SwiftUI

/// 私人 API 工具管理面板（声明式工具工厂的表单面）。
/// 铁律 4：校验/落盘/注册/试调全在后端（/api/v1/api-tools*），这里只渲染与回显。
/// 仓库手写声明（source=repo）只读展示；UI 添加的（source=user）可编辑/删除。
/// 生效语义如实转述：新增即时上架；覆盖已注册/删除 = 重启后端后生效（rescan 只增不删）。
private func toolsHintList(_ text: String, icon: String) -> some View {
    List {
        Label(text, systemImage: icon).foregroundStyle(.secondary)
    }
    .scrollDisabled(true)
}

private var toolsLoadingList: some View {
    List {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(L("加载中…")).foregroundStyle(.secondary)
        }
    }
    .scrollDisabled(true)
}

// MARK: - 表单草稿（客户端映射层：扁平参数行 ⇄ JSON Schema 片段）

struct ApiParamRow: Identifiable {
    let id = UUID()
    var name = ""
    var type = "string"          // string | number | integer | boolean
    var desc = ""
    var required = true
    var testValue = ""           // 试调实参（仅试调用，不入声明）
}

struct ApiToolDraft: Identifiable {
    var originalName: String?    // 非 nil = 编辑现有声明（name 锁定）
    var name = ""
    var description = ""
    var endpoint = ""
    var method = "GET"
    var params: [ApiParamRow] = []
    var authEnv = ""
    var authHeader = ""
    var authFormat = ""
    var secret = ""
    var hasStoredSecret = false
    var timeout = ""
    var approval = "default"     // default | require | skip
    var enabled = true

    var id: String { originalName ?? "＋new" }

    init() {}

    /// 从清单条目回填（编辑）。params 的 JSON Schema 片段拆回扁平行。
    init(item: ApiToolItem) {
        originalName = item.name
        name = item.name ?? ""
        description = item.description ?? ""
        endpoint = item.endpoint ?? ""
        method = item.method
        authEnv = item.authEnv ?? ""
        authHeader = item.authHeader ?? ""
        authFormat = item.authFormat ?? ""
        hasStoredSecret = item.hasSecret
        if let t = item.timeout { timeout = t == t.rounded() ? String(Int(t)) : String(t) }
        enabled = item.enabled
        if case .object(let p)? = item.params {
            var requiredNames: Set<String> = []
            if case .array(let req)? = p["required"] {
                for v in req { if case .string(let s) = v { requiredNames.insert(s) } }
            }
            if case .object(let props)? = p["properties"] {
                for key in props.keys.sorted() {
                    var row = ApiParamRow()
                    row.name = key
                    if case .object(let spec)? = props[key] {
                        if case .string(let t)? = spec["type"] { row.type = t }
                        if case .string(let d)? = spec["description"] { row.desc = d }
                    }
                    row.required = requiredNames.contains(key)
                    params.append(row)
                }
            }
        }
    }

    func toBody(arguments: JSONValue? = nil) -> AgentClient.ApiToolSaveBody {
        var paramsValue: JSONValue?
        let rows = params.filter { !$0.name.trimmingCharacters(in: .whitespaces).isEmpty }
        if !rows.isEmpty {
            var props: [String: JSONValue] = [:]
            for r in rows {
                var spec: [String: JSONValue] = ["type": .string(r.type)]
                if !r.desc.isEmpty { spec["description"] = .string(r.desc) }
                props[r.name] = .object(spec)
            }
            paramsValue = .object([
                "properties": .object(props),
                "required": .array(rows.filter(\.required).map { .string($0.name) }),
            ])
        }
        var authValue: JSONValue?
        if !authEnv.trimmingCharacters(in: .whitespaces).isEmpty {
            var a: [String: JSONValue] = ["env": .string(authEnv)]
            if !authHeader.isEmpty { a["header"] = .string(authHeader) }
            if !authFormat.isEmpty { a["format"] = .string(authFormat) }
            authValue = .object(a)
        }
        return AgentClient.ApiToolSaveBody(
            name: name.trimmingCharacters(in: .whitespaces),
            description: description,
            endpoint: endpoint.trimmingCharacters(in: .whitespaces),
            method: method,
            params: paramsValue,
            auth: authValue,
            headers: nil,
            timeout: Double(timeout),
            requires_approval: approval == "default" ? nil : approval == "require",
            enabled: enabled,
            secret: secret.isEmpty ? nil : secret,
            arguments: arguments)
    }

    /// 试调实参：按行内类型把试调值折成 JSON（空值行不发送）。
    func testArguments() -> JSONValue? {
        var args: [String: JSONValue] = [:]
        for r in params where !r.name.isEmpty && !r.testValue.isEmpty {
            switch r.type {
            case "number", "integer":
                args[r.name] = Double(r.testValue).map { .number($0) } ?? .string(r.testValue)
            case "boolean":
                args[r.name] = .bool(["true", "1", "yes"].contains(r.testValue.lowercased()))
            default:
                args[r.name] = .string(r.testValue)
            }
        }
        return args.isEmpty ? nil : .object(args)
    }
}

// MARK: - 面板（清单）

struct ApiToolsPanelView: View {
    @EnvironmentObject var store: AgentStore
    @Environment(\.dismiss) private var dismiss
    var embedded = false

    @State private var tools: [ApiToolItem]?
    @State private var failed = false
    @State private var draft: ApiToolDraft?
    @State private var pendingDelete: ApiToolItem?
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
            if let tools {
                if tools.isEmpty {
                    toolsHintList(L("还没有 API 工具声明"), icon: "wrench.and.screwdriver")
                } else {
                    toolList(tools)
                }
            } else if failed {
                toolsHintList(L("加载失败"), icon: "exclamationmark.triangle")
            } else {
                toolsLoadingList
            }
        }
        .navigationTitle(L("API 工具"))
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { draft = ApiToolDraft() } label: { Image(systemName: "plus") }
                    .help(L("添加 API 工具"))
            }
            if !embedded {
                ToolbarItem(placement: .cancellationAction) {
                    Button(L("关闭")) { dismiss() }
                }
            }
        }
        .task { await reload() }
        .sheet(item: $draft) { d in
            ApiToolEditorView(draft: d) { message in
                notice = message
                Task { await reload() }
            }
            .environmentObject(store)
        }
        .confirmationDialog(L("确认删除该声明？"), isPresented: .init(
            get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }),
            titleVisibility: .visible) {
            Button(L("删除"), role: .destructive) {
                if let item = pendingDelete { Task { await remove(item) } }
            }
        } message: {
            Text(L("已注册的实例要重启后端才会真正下架。"))
        }
        .alert(notice ?? "", isPresented: .init(
            get: { notice != nil }, set: { if !$0 { notice = nil } })) {
            Button(L("好")) { notice = nil }
        }
    }

    private func reload() async {
        do {
            tools = try await store.client?.listApiTools().tools ?? []
            failed = false
        } catch {
            tools = nil
            failed = true
        }
    }

    private func remove(_ item: ApiToolItem) async {
        guard let name = item.name else { return }
        do {
            let res = try await store.client?.deleteApiTool(name: name)
            notice = res?.effect == "restart_required"
                ? L("已删除声明；运行中的实例在重启后端后下架") : L("已删除")
            await reload()
        } catch {
            notice = error.localizedDescription
        }
    }

    private func toolList(_ items: [ApiToolItem]) -> some View {
        List {
            Section {
                ForEach(items) { item in row(item) }
            } footer: {
                Text(L("仓库 api_tools/ 里的手写声明只读；在这里添加的声明保存于数据目录。修改或删除已注册的工具需重启后端生效。"))
                    .font(.caption)
            }
        }
    }

    private func row(_ item: ApiToolItem) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(item.name ?? item.file).fontWeight(.medium)
                Text(item.method)
                    .font(.caption2.monospaced())
                    .padding(.horizontal, 4).padding(.vertical, 1)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 3))
                if !item.isUserOwned {
                    Text(L("手写")).font(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
                statusBadge(item)
            }
            if let desc = item.description, !desc.isEmpty {
                Text(desc).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            if let problem = item.problem {
                Text(problem).font(.caption).foregroundStyle(.red)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture {
            if item.isUserOwned && item.problem == nil { draft = ApiToolDraft(item: item) }
        }
        .contextMenu {
            if item.isUserOwned {
                if item.problem == nil {
                    Button(L("编辑")) { draft = ApiToolDraft(item: item) }
                }
                Button(L("删除"), role: .destructive) { pendingDelete = item }
            }
        }
    }

    @ViewBuilder
    private func statusBadge(_ item: ApiToolItem) -> some View {
        if item.problem != nil {
            Label(L("坏声明"), systemImage: "xmark.circle")
                .font(.caption).foregroundStyle(.red).labelStyle(.titleAndIcon)
        } else if item.registered {
            Label(L("已上架"), systemImage: "checkmark.circle.fill")
                .font(.caption).foregroundStyle(.green).labelStyle(.titleAndIcon)
        } else if !item.enabled {
            Text(L("已停用")).font(.caption).foregroundStyle(.secondary)
        } else {
            Text(L("未上架")).font(.caption).foregroundStyle(.orange)
        }
    }
}

// MARK: - 编辑表单（必选/可选分区，逐项提示）

struct ApiToolEditorView: View {
    @EnvironmentObject var store: AgentStore
    @Environment(\.dismiss) private var dismiss

    @State var draft: ApiToolDraft
    var onSaved: (String) -> Void

    @State private var saving = false
    @State private var testing = false
    @State private var testResult: String?
    @State private var errorText: String?

    var body: some View {
        NavigationStack {
            form
                .navigationTitle(draft.originalName == nil ? L("添加 API 工具") : L("编辑 API 工具"))
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button(L("取消")) { dismiss() }
                    }
                    ToolbarItem(placement: .confirmationAction) {
                        Button(L("保存")) { Task { await save() } }
                            .disabled(saving || draft.name.isEmpty
                                      || draft.description.isEmpty || draft.endpoint.isEmpty)
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
                TextField(L("工具名"), text: $draft.name)
                    .disabled(draft.originalName != nil)
                    #if os(iOS)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    #endif
                TextField(L("说明（模型全凭这句话决定何时调用）"), text: $draft.description, axis: .vertical)
                    .lineLimit(2...4)
                TextField(L("地址（可含 {参数} 路径模板）"), text: $draft.endpoint)
                    #if os(iOS)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    #endif
                Picker(L("方法"), selection: $draft.method) {
                    ForEach(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"], id: \.self) { Text($0) }
                }
            } header: { Text(L("必填")) } footer: {
                Text(L("工具名用小写蛇形（如 weather_home）。说明要写清触发场景与返回内容，例如\"查询我家里 NAS 的状态，用户问 NAS 时调用\"。"))
            }

            Section {
                ForEach($draft.params) { $row in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            TextField(L("参数名"), text: $row.name)
                                #if os(iOS)
                                .textInputAutocapitalization(.never).autocorrectionDisabled()
                                #endif
                            Picker("", selection: $row.type) {
                                Text("string").tag("string")
                                Text("number").tag("number")
                                Text("integer").tag("integer")
                                Text("boolean").tag("boolean")
                            }
                            .labelsHidden().fixedSize()
                            #if os(macOS)
                            Toggle(L("必需"), isOn: $row.required)
                                .toggleStyle(.checkbox).fixedSize()
                            #else
                            Toggle(L("必需"), isOn: $row.required).fixedSize()
                            #endif
                        }
                        TextField(L("参数说明"), text: $row.desc)
                            .font(.caption)
                    }
                }
                .onDelete { draft.params.remove(atOffsets: $0) }
                Button {
                    draft.params.append(ApiParamRow())
                } label: { Label(L("添加参数"), systemImage: "plus.circle") }
            } header: { Text(L("参数（可选）")) } footer: {
                Text(L("扁平参数覆盖大多数私人 API；嵌套结构请手写 yaml。路径模板里的 {参数} 也要在此声明。"))
            }

            Section {
                TextField(L("密钥环境变量名（如 MY_API_KEY）"), text: $draft.authEnv)
                    #if os(iOS)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    #endif
                if !draft.authEnv.isEmpty {
                    TextField(L("请求头（默认 Authorization）"), text: $draft.authHeader)
                    TextField(L("格式（默认 Bearer {key}）"), text: $draft.authFormat)
                    SecureField(draft.hasStoredSecret ? L("密钥值（已配置，留空保持不变）") : L("密钥值"),
                                text: $draft.secret)
                }
            } header: { Text(L("鉴权（可选）")) } footer: {
                Text(L("密钥值保存在后端数据目录的受限文件里（0600），不进声明文件、不进 git、不回传。"))
            }

            Section {
                TextField(L("超时秒（默认 30）"), text: $draft.timeout)
                Picker(L("审批"), selection: $draft.approval) {
                    Text(L("跟随方法默认")).tag("default")
                    Text(L("强制审批")).tag("require")
                    Text(L("免审批")).tag("skip")
                }
                Toggle(L("启用"), isOn: $draft.enabled)
            } header: { Text(L("高级（可选）")) } footer: {
                Text(L("默认：GET/HEAD 免审批，变更类方法（POST 等）每次调用弹审批。停用的声明保留但不注册。"))
            }

            Section {
                ForEach($draft.params) { $row in
                    if !row.name.isEmpty {
                        TextField(L("试调值：") + row.name, text: $row.testValue)
                    }
                }
                Button {
                    Task { await test() }
                } label: {
                    if testing {
                        HStack(spacing: 6) { ProgressView().controlSize(.small); Text(L("试调中…")) }
                    } else {
                        Label(L("试调一次"), systemImage: "bolt")
                    }
                }
                .disabled(testing || draft.endpoint.isEmpty)
                if let testResult {
                    ScrollView {
                        Text(testResult)
                            .font(.caption.monospaced())
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                    }
                    .frame(maxHeight: 160)
                }
            } header: { Text(L("试调")) } footer: {
                Text(L("按当前表单真实调用一次并回显响应（不保存）。保存前先试调可即时发现声明错误。"))
            }

            if let errorText {
                Section { Text(errorText).foregroundStyle(.red).font(.caption) }
            }
        }
        #if os(macOS)
        .formStyle(.grouped)
        #endif
    }

    private func test() async {
        testing = true
        defer { testing = false }
        do {
            let res = try await store.client?.testApiTool(draft.toBody(arguments: draft.testArguments()))
            testResult = res?.result ?? ""
            errorText = nil
        } catch {
            testResult = nil
            errorText = error.localizedDescription
        }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            let res = try await store.client?.saveApiTool(draft.toBody())
            let message: String
            switch res?.effect {
            case "registered": message = L("已保存并上架")
            case "unavailable": message = L("已保存但探活未过（检查密钥），补齐后重扫可上架")
            case "restart_required": message = L("已保存；该工具已在运行中，重启后端后生效")
            case "saved_disabled": message = L("已保存（停用状态，不会注册）")
            default: message = L("已保存")
            }
            dismiss()
            onSaved(message)
        } catch {
            errorText = error.localizedDescription
        }
    }
}
