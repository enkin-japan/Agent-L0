import SwiftUI

/// 设置页「邮件发送」（send_email 工具），双平台共用：紧跟「搜索 API」一节，同一标签页 /
/// 同一推入页。只有这里列出的联系人能收到 agent 发的邮件，每次发送前都会出审批卡确认。
///
/// 后端是权威（照 SearchProvidersSection）：出现时 GET；「保存」整体 PUT，失败就地显示后端的
/// 校验原因（后端 422 不回显输入）。
/// 密码红线：密码只在 SecureField 与那一次请求体里出现——不进 UserDefaults、不进日志、不进
/// 错误提示；已保存时输入框为空、显示占位「已保存」，不输入新密码就什么都不发。后端只回 has_password。
struct MailSettingsSection: View {
    @EnvironmentObject var store: AgentStore
    @State private var loaded = false
    @State private var loading = false
    @State private var errorText: String?
    @State private var host = "smtp.fastmail.com"
    @State private var port = 465
    @State private var username = ""
    @State private var fromAddr = ""
    @State private var newPassword = ""
    @State private var hasPassword = false
    @State private var clearPassword = false
    @State private var configured = false
    @State private var contacts: [ContactRow] = []
    @State private var saving = false
    @State private var saveError: String?
    @State private var savedNote = false

    private struct ContactRow: Identifiable, Equatable {
        let id = UUID()
        var name: String
        var address: String
    }

    var body: some View {
        Section {
            if loading && !loaded {
                Text(L("正在读取邮件设置…")).font(.caption).foregroundStyle(.secondary)
            }
            if loaded {
                Text(configured ? L("已配置，可以发送邮件。")
                                : L("尚未配置完成：需要用户名、App 专用密码和至少一个联系人。"))
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                TextField(L("服务器"), text: $host)
                    #if os(iOS)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .keyboardType(.URL)
                    #endif
                TextField(L("用户名（完整邮件地址）"), text: $username)
                    #if os(iOS)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .keyboardType(.emailAddress)
                    #endif
                TextField(L("发件地址（可空，默认同用户名）"), text: $fromAddr)
                    #if os(iOS)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .keyboardType(.emailAddress)
                    #endif
                SecureField(hasPassword ? L("已保存（输入新密码可替换）") : L("App 专用密码"),
                            text: $newPassword)
                    #if os(iOS)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    #endif
                    .disabled(clearPassword)
                if hasPassword {
                    Toggle(L("保存时清除已存的密码"), isOn: $clearPassword)
                }
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
            Text(L("邮件发送"))
        }
        .task(id: store.client.map { ObjectIdentifier($0) }) { await load() }

        if loaded {
            Section {
                ForEach($contacts) { $row in
                    HStack(spacing: Theme.s2) {
                        TextField(L("名字"), text: $row.name)
                        TextField(L("邮件地址"), text: $row.address)
                            #if os(iOS)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                            .keyboardType(.emailAddress)
                            #endif
                        Button(role: .destructive) {
                            contacts.removeAll { $0.id == row.id }
                        } label: {
                            Image(systemName: "minus.circle")
                        }
                        .buttonStyle(.borderless)
                        .accessibilityLabel(L("删除联系人"))
                    }
                }
                Button {
                    contacts.append(ContactRow(name: "", address: ""))
                } label: {
                    Label(L("添加联系人"), systemImage: "plus.circle")
                }
                .buttonStyle(.borderless)
                .disabled(contacts.count >= 20)
                HStack {
                    if let saveError {
                        Text(saveError).font(.caption).foregroundStyle(Theme.danger)
                            .fixedSize(horizontal: false, vertical: true)
                    } else if savedNote {
                        Text(L("已保存")).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button(saving ? L("保存中…") : L("保存")) { Task { await save() } }
                        .disabled(saving)
                }
            } header: {
                Text(L("联系人"))
            } footer: {
                Text(L("只有列出的联系人能收到邮件；每次发送前都会请你确认。"))
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private func apply(_ s: MailSettings) {
        host = s.host
        port = s.port
        username = s.username
        fromAddr = s.fromAddr
        hasPassword = s.hasPassword
        configured = s.configured
        contacts = s.contacts.map { ContactRow(name: $0.name, address: $0.address) }
        newPassword = ""
        clearPassword = false
    }

    private func load() async {
        guard let client = store.client else {
            loaded = false; errorText = L("尚未连接后端。"); return
        }
        loading = true
        defer { loading = false }
        do {
            let res = try await client.mailSettings()
            guard store.client === client else { return }
            apply(res)
            loaded = true
            errorText = nil
        } catch {
            guard store.client === client else { return }
            loaded = false
            if case AgentError.http(404, _) = error {
                errorText = L("当前后端不支持邮件发送设置（需要更新执行层）。")
            } else {
                errorText = L("邮件设置加载失败：") + ApprovalPolicySection.serverMessage(error)
            }
        }
    }

    private func save() async {
        guard let client = store.client else { return }
        let trimmed = { (s: String) in s.trimmingCharacters(in: .whitespacesAndNewlines) }
        let rows = contacts
            .map { MailContact(name: trimmed($0.name), address: trimmed($0.address)) }
            .filter { !($0.name.isEmpty && $0.address.isEmpty) }
        let password = trimmed(newPassword)
        let body = MailSettingsBody(host: trimmed(host), port: port, username: trimmed(username),
                                    fromAddr: trimmed(fromAddr), contacts: rows,
                                    password: clearPassword || password.isEmpty ? nil : password,
                                    clearPassword: clearPassword ? true : nil)
        saving = true
        saveError = nil
        savedNote = false
        defer { saving = false }
        do {
            let res = try await client.updateMailSettings(body)
            guard store.client === client else { return }
            apply(res)
            savedNote = true
        } catch {
            guard store.client === client else { return }
            saveError = L("保存失败：") + ApprovalPolicySection.serverMessage(error)
        }
    }
}
