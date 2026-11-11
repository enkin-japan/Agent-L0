import SwiftUI

/// 设置页「页面发布」（publish_page 工具），双平台共用：紧跟「邮件发送」一节。
/// publish_page 把 ~/AgentPages 里的一个 HTML 页面复制进这里设置的发布文件夹（用户自己的
/// web 服务器对外提供的目录）下一个长随机目录，返回「公开网址前缀/随机目录/文件名」的链接。
///
/// 后端是权威（照 MailSettingsSection）：出现时 GET；「保存」整体 PUT，失败就地显示后端的
/// 校验原因。两个字段都清空 = 关闭页面发布。
struct PublishSettingsSection: View {
    @EnvironmentObject var store: AgentStore
    @State private var loaded = false
    @State private var loading = false
    @State private var errorText: String?
    @State private var rootDir = ""
    @State private var baseUrl = ""
    @State private var configured = false
    @State private var saving = false
    @State private var saveError: String?
    @State private var savedNote = false

    var body: some View {
        Section {
            if loading && !loaded {
                Text(L("正在读取页面发布设置…")).font(.caption).foregroundStyle(.secondary)
            }
            if loaded {
                Text(configured ? L("已配置，可以发布页面。") : L("尚未配置：需要发布文件夹和公开网址。"))
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                TextField(L("发布文件夹（由网站对外提供的文件夹）"), text: $rootDir)
                    #if os(iOS)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    #endif
                TextField(L("公开网址前缀"), text: $baseUrl, prompt: Text(verbatim: "https://…"))
                    #if os(iOS)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .keyboardType(.URL)
                    #endif
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
            Text(L("页面发布"))
        } footer: {
            Text(L("发布的页面任何拿到链接的人都能打开。"))
                .font(.caption).foregroundStyle(.secondary)
        }
        .task(id: store.client.map { ObjectIdentifier($0) }) { await load() }
    }

    private func apply(_ s: PublishSettings) {
        rootDir = s.rootDir
        baseUrl = s.baseUrl
        configured = s.configured
    }

    private func load() async {
        guard let client = store.client else {
            loaded = false; errorText = L("尚未连接后端。"); return
        }
        loading = true
        defer { loading = false }
        do {
            let res = try await client.publishSettings()
            guard store.client === client else { return }
            apply(res)
            loaded = true
            errorText = nil
        } catch {
            guard store.client === client else { return }
            loaded = false
            if case AgentError.http(404, _) = error {
                errorText = L("当前后端不支持页面发布设置（需要更新执行层）。")
            } else {
                errorText = L("页面发布设置加载失败：") + ApprovalPolicySection.serverMessage(error)
            }
        }
    }

    private func save() async {
        guard let client = store.client else { return }
        let body = PublishSettingsBody(
            rootDir: rootDir.trimmingCharacters(in: .whitespacesAndNewlines),
            baseUrl: baseUrl.trimmingCharacters(in: .whitespacesAndNewlines))
        saving = true
        saveError = nil
        savedNote = false
        defer { saving = false }
        do {
            let res = try await client.updatePublishSettings(body)
            guard store.client === client else { return }
            apply(res)
            savedNote = true
        } catch {
            guard store.client === client else { return }
            saveError = L("保存失败：") + ApprovalPolicySection.serverMessage(error)
        }
    }
}
