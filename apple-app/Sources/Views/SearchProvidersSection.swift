import SwiftUI

/// 设置页「搜索」（build 85 批 C），双平台共用：macOS 在「搜索」标签页，iPhone 在设置页
/// 「数据」一节推入。用户添加的搜索 API（Brave / Tavily）被优先使用、多个时随机轮流，
/// 全部不可用时后端自动改用内置搜索（SearXNG）。
///
/// 后端是权威（照 ApprovalPolicySection）：出现时 GET；开关乐观更新、失败回退并就地显示原因。
/// 密钥红线：密钥只在「添加」表单的 SecureField 与那一次请求体里出现——不进 UserDefaults、
/// 不进日志、不进错误提示（后端 422 也不回显输入）；保存成功即清空输入框。后端只回 has_secret。
struct SearchProvidersSection: View {
    @EnvironmentObject var store: AgentStore
    @State private var types: [SearchProviderType] = []
    @State private var providers: [SearchProvider] = []
    @State private var loaded = false
    @State private var loading = false
    @State private var errorText: String?
    @State private var busy: Set<String> = []
    @State private var testResults: [String: String] = [:]
    @State private var pendingDelete: SearchProvider?
    @State private var newType = ""
    @State private var newLabel = ""
    @State private var newSecret = ""
    @State private var adding = false
    @State private var addError: String?

    var body: some View {
        Section {
            Text(L("已添加的搜索 API 会被优先使用（多个时随机轮流）；全部不可用时自动改用内置搜索。"))
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if loading && !loaded {
                Text(L("正在读取搜索设置…")).font(.caption).foregroundStyle(.secondary)
            }
            if loaded && providers.isEmpty {
                Text(L("还没有添加搜索 API，目前使用内置搜索。")).font(.caption).foregroundStyle(.secondary)
            }
            ForEach(providers) { provider in
                row(provider)
            }
            if let errorText {
                HStack(alignment: .firstTextBaseline) {
                    Text(errorText).font(.caption).foregroundStyle(Theme.danger)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: Theme.s2)
                    if !loaded {
                        Button(L("重试")) { Task { await load() } }.font(.caption)
                    }
                }
            }
        } header: {
            Text(L("搜索 API"))
        } footer: {
            Text(L("密钥只保存在后端数据目录的受限文件里（0600），不会回传到 app。"))
                .font(.caption).foregroundStyle(.secondary)
        }
        .task(id: store.client.map { ObjectIdentifier($0) }) { await load() }
        .confirmationDialog(L("删除这个搜索 API？"),
                            isPresented: Binding(get: { pendingDelete != nil },
                                                 set: { if !$0 { pendingDelete = nil } }),
                            presenting: pendingDelete) { provider in
            Button(L("删除"), role: .destructive) { Task { await delete(provider) } }
            Button(L("取消"), role: .cancel) { pendingDelete = nil }
        } message: { _ in
            Text(L("密钥会一并从后端删除。"))
        }

        if loaded {
            Section {
                Picker(L("类型"), selection: $newType) {
                    ForEach(types) { Text($0.label).tag($0.type) }
                }
                TextField(L("标签（可空）"), text: $newLabel)
                SecureField(L("API 密钥"), text: $newSecret)
                    #if os(iOS)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    #endif
                HStack {
                    if let addError {
                        Text(addError).font(.caption).foregroundStyle(Theme.danger)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    Button(adding ? L("保存中…") : L("添加")) { Task { await add() } }
                        .disabled(adding || newType.isEmpty
                                  || newSecret.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            } header: {
                Text(L("添加搜索 API"))
            }
        }
    }

    @ViewBuilder
    private func row(_ provider: SearchProvider) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Toggle(provider.title(types: types), isOn: enabledBinding(provider))
                .disabled(busy.contains(provider.id))
            Text("\(provider.secretText) · \(provider.statusText)")
                .font(.caption).foregroundStyle(.secondary)
            HStack(spacing: Theme.s2) {
                Button(busy.contains(provider.id) ? L("测试中…") : L("测试")) {
                    Task { await test(provider) }
                }
                .disabled(busy.contains(provider.id) || !provider.hasSecret)
                Text(L("测试会消耗一次搜索额度")).font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Button(L("删除"), role: .destructive) { pendingDelete = provider }
                    .disabled(busy.contains(provider.id))
            }
            .buttonStyle(.borderless)
            if let result = testResults[provider.id] {
                Text(result).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }

    private func replace(_ item: SearchProvider) {
        if let i = providers.firstIndex(where: { $0.id == item.id }) { providers[i] = item }
    }

    private func enabledBinding(_ provider: SearchProvider) -> Binding<Bool> {
        Binding(get: {
            providers.first { $0.id == provider.id }?.enabled ?? provider.enabled
        }, set: { value in
            guard let client = store.client,
                  let current = providers.first(where: { $0.id == provider.id }),
                  current.enabled != value else { return }
            replace(SearchProvider(id: current.id, type: current.type, label: current.label,
                                   enabled: value, hasSecret: current.hasSecret,
                                   status: current.status, statusAt: current.statusAt))
            busy.insert(provider.id)
            errorText = nil
            Task {
                do {
                    let item = try await client.updateSearchProvider(
                        id: provider.id, SearchProviderPatch(enabled: value))
                    if store.client === client { replace(item) }
                } catch {
                    if store.client === client {
                        replace(current)
                        errorText = L("保存失败：") + ApprovalPolicySection.serverMessage(error)
                    }
                }
                busy.remove(provider.id)
            }
        })
    }

    private func load() async {
        guard let client = store.client else {
            loaded = false; errorText = L("尚未连接后端。"); return
        }
        loading = true
        defer { loading = false }
        do {
            let res = try await client.searchProviders()
            guard store.client === client else { return }
            types = res.types
            providers = res.providers
            if !types.contains(where: { $0.type == newType }) { newType = types.first?.type ?? "" }
            loaded = true
            errorText = nil
        } catch {
            guard store.client === client else { return }
            loaded = false
            if case AgentError.http(404, _) = error {
                errorText = L("当前后端不支持搜索 API 设置（需要 build 85 及以上的执行层）。")
            } else {
                errorText = L("搜索设置加载失败：") + ApprovalPolicySection.serverMessage(error)
            }
        }
    }

    private func add() async {
        guard let client = store.client else { return }
        let secret = newSecret.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !secret.isEmpty, !newType.isEmpty else { return }
        let label = newLabel.trimmingCharacters(in: .whitespacesAndNewlines)
        adding = true
        addError = nil
        defer { adding = false }
        do {
            let item = try await client.addSearchProvider(SearchProviderCreateBody(
                type: newType, label: label.isEmpty ? nil : label, secret: secret))
            guard store.client === client else { return }
            providers.append(item)
            newSecret = ""
            newLabel = ""
        } catch {
            guard store.client === client else { return }
            addError = L("添加失败：") + ApprovalPolicySection.serverMessage(error)
        }
    }

    private func delete(_ provider: SearchProvider) async {
        pendingDelete = nil
        guard let client = store.client else { return }
        busy.insert(provider.id)
        defer { busy.remove(provider.id) }
        do {
            try await client.deleteSearchProvider(id: provider.id)
            guard store.client === client else { return }
            providers.removeAll { $0.id == provider.id }
            testResults[provider.id] = nil
        } catch {
            guard store.client === client else { return }
            errorText = L("删除失败：") + ApprovalPolicySection.serverMessage(error)
        }
    }

    private func test(_ provider: SearchProvider) async {
        guard let client = store.client else { return }
        busy.insert(provider.id)
        defer { busy.remove(provider.id) }
        do {
            let result = try await client.testSearchProvider(id: provider.id)
            guard store.client === client else { return }
            testResults[provider.id] = result.text
            if let res = try? await client.searchProviders(), store.client === client {
                providers = res.providers      // 刷新「最近状态」
            }
        } catch {
            guard store.client === client else { return }
            testResults[provider.id] = L("测试失败：") + ApprovalPolicySection.serverMessage(error)
        }
    }
}
