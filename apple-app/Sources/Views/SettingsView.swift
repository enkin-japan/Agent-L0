import SwiftUI

/// 连接设置：服务器地址 + API Key（Keychain）+ 连通性测试 + 通知偏好。
struct SettingsView: View {
    @EnvironmentObject var store: AgentStore
    @EnvironmentObject var push: PushManager
    #if os(macOS)
    @EnvironmentObject var sidecar: SidecarManager
    #endif
    @Environment(\.dismiss) private var dismiss

    @State private var urlText = ""
    @State private var keyText = ""
    @State private var testResult: String?

    var body: some View {
        NavigationStack {
            Form {
                Section(L("后端地址")) {
                    TextField("http://127.0.0.1:14159", text: $urlText)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        #endif
                    Text(L("本机运行填回环地址；跨设备走 Tailscale：推荐在 Mac 上 `tailscale serve` 反代 14159，这里填 https://<mac>.<tailnet>.ts.net"))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Section("API Key") {
                    SecureField(L("AGENT_API_KEY（存入钥匙串）"), text: $keyText)
                    Text(L("与仓库根 .env 里的 AGENT_API_KEY 相同；后端未启用鉴权时留空。"))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Section {
                    Button(L("测试连接")) { test() }
                    if let result = testResult {
                        Text(result).font(.caption)
                    }
                }
                Section {
                    Toggle(L("启用通知"), isOn: notifBinding(\.notifEnabled))
                    if push.authStatus == .denied {
                        Text(L("系统已拒绝本 App 的通知权限——请到系统设置里为 MacAgent 打开。"))
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                    if store.settings.notifEnabled {
                        Toggle(L("任务结果"), isOn: notifBinding(\.notifResults))
                        Toggle(L("鉴权要求（建议保持开启）"), isOn: notifBinding(\.notifApproval))
                        Toggle(L("定时任务开始"), isOn: notifBinding(\.notifScheduleStart))
                        Picker(L("通知内容"), selection: contentLevelBinding) {
                            Text(L("完整内容")).tag("full")
                            Text(L("仅类别（推荐）")).tag("category")
                            Text(L("仅提示有新事件")).tag("placeholder")
                        }
                    }
                } header: {
                    Text(L("通知"))
                } footer: {
                    #if os(iOS)
                    Text(L("iPhone 通知经 Apple 推送服务（APNs）投递；「仅类别」不把任务内容发出 Mac。灵动岛与锁屏实时活动随任务自动出现。"))
                        .font(.caption).foregroundStyle(.secondary)
                    #else
                    Text(L("Mac 通知完全本地投递，不经任何外部服务。"))
                        .font(.caption).foregroundStyle(.secondary)
                    #endif
                }
                Section(L("外观与语言")) {
                    Picker(L("外观"), selection: appearanceBinding) {
                        Text(L("跟随系统")).tag("system")
                        Text(L("浅色")).tag("light")
                        Text(L("深色")).tag("dark")
                    }
                    Picker(L("语言"), selection: languageBinding) {
                        Text(L("跟随系统")).tag("system")
                        Text("中文").tag("zh")
                        Text("日本語").tag("ja")
                        Text("English").tag("en")
                    }
                }
                #if os(macOS)
                Section {
                    // 路径框常显：它是开关的**前置输入**（先填路径再开），藏在开关后面
                    // 会造成「说明书让先填、界面上却没有」的死锁——实机查验当场撞上
                    TextField(L("仓库路径"), text: sidecarRepoRootBinding,
                              prompt: Text("/path/to/Agent"))
                        .autocorrectionDisabled()
                    Toggle(L("app 内建执行层（sidecar）"), isOn: sidecarEnabledBinding)
                    if store.settings.sidecarEnabled {
                        // 拨设备 A 的两把服务密钥（R5-6，只在 bundle 布局被消费；
                        // repo 布局由 dev 桥接读仓库 .env.local，这里填了也无害）
                        SecureField("INFER_API_KEY", text: sidecarKeyBinding(\.inferApiKey))
                        SecureField("RAG_API_KEY", text: sidecarKeyBinding(\.ragApiKey))
                        LabeledContent(L("状态"), value: sidecarStateLabel)
                        if case .failed(let detail) = sidecar.state {
                            // lib_edge.sh 的原话（端口占用者清单/依赖缺失/日志尾），不二次转译
                            Text(detail)
                                .font(.caption.monospaced())
                                .foregroundStyle(.orange)
                                .textSelection(.enabled)
                        }
                    }
                } header: {
                    Text(L("执行层"))
                } footer: {
                    Text(L("由 app 拉起并监护 backend + SearXNG（顺序与就绪判据在 scripts/lib_edge.sh，与 devctl 同一真相）。已有 devctl 启动的后端时只连接、不接管。"))
                        .font(.caption).foregroundStyle(.secondary)
                }
                #endif
                Section {
                    LabeledContent(L("App 版本"), value: Bundle.main.appVersionLabel)
                }
            }
            .navigationTitle(L("连接设置"))
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button(L("保存")) { save() }
                }
                ToolbarItem(placement: .cancellationAction) {
                    Button(L("取消")) { dismiss() }
                }
            }
        }
        .onAppear {
            urlText = store.settings.baseURLString
            keyText = store.settings.apiKey
            push.refreshAuthStatus()
        }
        #if os(macOS)
        .frame(minWidth: 480, minHeight: 360)
        #endif
    }

    #if os(macOS)
    /// 开关即改即生效：开 = 立刻 autostart；关 = 收掉自己拉起的（external 不碰）。
    private var sidecarEnabledBinding: Binding<Bool> {
        Binding(get: { store.settings.sidecarEnabled },
                set: { on in
                    store.settings.sidecarEnabled = on
                    Task { await sidecar.startStop() }
                })
    }

    private var sidecarRepoRootBinding: Binding<String> {
        Binding(get: { store.settings.sidecarRepoRoot },
                set: { store.settings.sidecarRepoRoot = $0 })
    }

    /// 设备 A 服务密钥即改即存（Keychain didSet），下次 up 生效
    private func sidecarKeyBinding(_ key: ReferenceWritableKeyPath<AppSettings, String>)
        -> Binding<String> {
        Binding(get: { store.settings[keyPath: key] },
                set: { store.settings[keyPath: key] = $0 })
    }

    private var sidecarStateLabel: String {
        switch sidecar.state {
        case .disabled: return L("未启用")
        case .external: return L("外部后端在跑（devctl），只连接不接管")
        case .starting: return L("启动中…")
        case .running:  return sidecar.restartCount > 0
            ? "\(L("运行中"))（\(L("已自动重启")) \(sidecar.restartCount)）"
            : L("运行中")
        case .failed:   return L("故障")
        }
    }
    #endif

    /// 外观/语言即改即生效（不走「保存」按钮，那是连接设置的语义）
    private var appearanceBinding: Binding<String> {
        Binding(get: { store.settings.appearance },
                set: { store.settings.appearance = $0 })
    }

    /// 通知开关即改即生效：写偏好后同步（iOS 推到后端注册表；macOS 本地立即生效）。
    private func notifBinding(_ key: ReferenceWritableKeyPath<AppSettings, Bool>) -> Binding<Bool> {
        Binding(get: { store.settings[keyPath: key] },
                set: { store.settings[keyPath: key] = $0
                       push.settingsChanged() })
    }

    private var contentLevelBinding: Binding<String> {
        Binding(get: { store.settings.notifContentLevel },
                set: { store.settings.notifContentLevel = $0
                       push.settingsChanged() })
    }

    private var languageBinding: Binding<String> {
        Binding(get: { store.settings.language },
                set: { store.settings.language = $0 })
    }

    private func save() {
        store.settings.baseURLString = urlText.trimmingCharacters(in: .whitespacesAndNewlines)
        store.settings.apiKey = keyText.trimmingCharacters(in: .whitespacesAndNewlines)
        store.applySettings()
        dismiss()
    }

    private func test() {
        testResult = L("测试中…")
        let url = urlText.trimmingCharacters(in: .whitespacesAndNewlines)
        let key = keyText.trimmingCharacters(in: .whitespacesAndNewlines)
        Task {
            guard let base = URL(string: url), base.scheme != nil else {
                testResult = L("❌ 地址无效")
                return
            }
            let client = AgentClient(baseURL: base, apiKey: key)
            do {
                let ok = try await client.health()
                if !ok {
                    testResult = L("❌ 后端返回异常")
                } else {
                    // /health 免鉴权，再打一个带鉴权的端点验证 key
                    _ = try await client.listSessions()
                    testResult = L("✅ 连接与鉴权正常")
                }
            } catch AgentError.http(let code, _) where code == 401 || code == 403 {
                testResult = L("⚠️ 服务可达，但 API Key 不正确")
            } catch let error as URLError where error.code == .cannotFindHost {
                testResult = L("❌ 域名无法解析，请确认本设备 Tailscale 已连接且启用了 Tailscale DNS")
            } catch let error as URLError where error.code == .cannotConnectToHost {
                testResult = L("❌ 域名可解析但端口不通：确认对端 tailscale serve / 后端进程在运行。")
            } catch {
                testResult = L("❌ 无法连接：") + error.localizedDescription
            }
        }
    }
}
