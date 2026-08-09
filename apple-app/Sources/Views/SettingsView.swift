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
    #if os(macOS)
    @State private var keyCopied = false
    @State private var copyResetTask: Task<Void, Never>?
    private enum RegenPhase { case idle, restarting, done }
    @State private var regenPhase: RegenPhase = .idle
    // Tab 选中态必须显式绑定：store/push 是高频发布源（连接状态、流式事件），
    // 每次 body 重算时无绑定的 TabView 会重置回第一页——实机症状即"点击不切页/
    // 切过去又弹回第一页"。@State 存活于重渲染，选中页才稳得住。
    private enum SettingsTab: Hashable, CaseIterable, Identifiable {
        case connection, notification, knowledge, apiTools, skills, registrations,
             sidecar, appearance
        var id: Self { self }
        var title: String {
            switch self {
            case .connection:    return L("连接")
            case .notification:  return L("通知")
            case .knowledge:     return L("知识库")
            case .apiTools:      return L("API 工具")
            case .skills:        return L("剧本")
            case .registrations: return L("待确认")
            case .sidecar:       return L("执行层")
            case .appearance:    return L("外观")
            }
        }
        var icon: String {
            switch self {
            case .connection:    return "network"
            case .notification:  return "bell"
            case .knowledge:     return "books.vertical"
            case .apiTools:      return "wrench.and.screwdriver"
            case .skills:        return "book.closed"
            case .registrations: return "tray.full"
            case .sidecar:       return "gearshape.2"
            case .appearance:    return "paintpalette"
            }
        }
    }
    @State private var tab: SettingsTab = .connection
    #endif

    var body: some View {
        NavigationStack {
            // macOS 分 Tab（系统设置窗的惯例；单页 Form 七个 Section 已实证拥挤——
            // 实机查验时用户在里面找不到新增项）；iOS 保持单列滚动（sheet 的惯例）。
            #if os(macOS)
            // 自定义顶部标签条替代系统 TabView（C 项）：系统 macOS TabView 的 tabItem
            // 标题在其分段格内靠右贴边、且对齐不可控（无 API 干预 Label 在 tab cell
            // 里的对齐）。改为等宽居中的标签条——每格 VStack(图标/标题) 在 maxWidth
            // 撑满格内自然居中。选中态仍由 @State tab 驱动（保留此前修过的「切页弹回
            // 第一页」修复，机制不退化）；内容随 tab 切换，全部仍在外层 NavigationStack
            // 内（知识库的 NavigationLink 推页不受影响）。
            VStack(spacing: 0) {
                settingsTabBar
                Divider()
                settingsTabContent
            }
            .navigationTitle(L("设置"))
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button(L("保存")) { save() }
                }
                ToolbarItem(placement: .cancellationAction) {
                    Button(L("取消")) { dismiss() }
                }
            }
            #else
            Form {
                connectionSections
                notificationSection
                // 知识库入口（全局数据实体归设置；NavigationLink 推入外层栈）
                Section(L("数据")) {
                    NavigationLink {
                        KnowledgePanelView(embedded: true)
                    } label: {
                        Label(L("知识库"), systemImage: "books.vertical")
                    }
                    NavigationLink {
                        ApiToolsPanelView(embedded: true)
                    } label: {
                        Label(L("API 工具"), systemImage: "wrench.and.screwdriver")
                    }
                    NavigationLink {
                        SkillsPanelView(embedded: true)
                    } label: {
                        Label(L("剧本"), systemImage: "book.closed")
                    }
                    NavigationLink {
                        RegistrationsPanelView(embedded: true)
                    } label: {
                        Label(L("待确认"), systemImage: "tray.full")
                    }
                }
                appearanceSection
                versionSection
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
            #endif
        }
        .onAppear {
            urlText = store.settings.baseURLString
            keyText = store.settings.apiKey
            push.refreshAuthStatus()
        }
        #if os(macOS)
        .frame(minWidth: 520, minHeight: 420)
        #endif
    }

    #if os(macOS)
    /// 顶部标签条：等宽格，格内图标+标题居中（C 项对齐修复的落点）。
    private var settingsTabBar: some View {
        HStack(spacing: 0) {
            ForEach(SettingsTab.allCases) { t in
                Button { tab = t } label: {
                    VStack(spacing: 3) {
                        Image(systemName: t.icon).font(.system(size: 15))
                        Text(t.title).font(.caption)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, Theme.s1)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .foregroundStyle(tab == t ? Theme.accent : Color.secondary)
            }
        }
        .padding(.horizontal, Theme.s2)
        .padding(.top, Theme.s2)
    }

    /// 当前标签内容：与原 TabView 各 tag 一一对应，全部仍在外层 NavigationStack 内。
    @ViewBuilder
    private var settingsTabContent: some View {
        switch tab {
        case .connection:
            Form { connectionSections }.formStyle(.grouped)
        case .notification:
            Form { notificationSection }.formStyle(.grouped)
        // 知识库是全局数据实体（跨会话），归设置而非会话菜单。embedded：复用本页
        // 外层 NavigationStack 推入文档页。
        case .knowledge:
            KnowledgePanelView(embedded: true)
        // 私人 API 工具（声明式工具工厂表单面）：同为全局数据实体，归设置
        case .apiTools:
            ApiToolsPanelView(embedded: true)
        // 剧本（用户以 /名字 点名的流程提示词）：同为全局数据实体，归设置
        case .skills:
            SkillsPanelView(embedded: true)
        // 待确认注册（模型起草的声明的人工闸）：确认只在设置页，模型没有确认工具
        case .registrations:
            RegistrationsPanelView(embedded: true)
        case .sidecar:
            Form { sidecarSection; versionSection }.formStyle(.grouped)
        case .appearance:
            Form { appearanceSection }.formStyle(.grouped)
        }
    }
    #endif

    // MARK: - 分区（macOS 各归各 Tab；iOS 顺序铺开）

    @ViewBuilder
    private var connectionSections: some View {
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
                    #if os(macOS)
                    // 工作端才有这两枚钮：复制（SecureField 抄不出来——iPhone 填 key
                    // 的真实痛点）；重新生成 = key 轮换，仅内建执行层可用（external/
                    // repo 布局的 key 在 .env.local，这里换了对不上）。
                    HStack(spacing: Theme.s2) {
                        // 反馈在按钮原位画勾（用户定：挤开邻钮的旁注难看）；ZStack
                        // 定宽避免勾/字互换时布局跳动
                        Button { copyKey() } label: {
                            ZStack {
                                Text(L("复制")).opacity(keyCopied ? 0 : 1)
                                Image(systemName: "checkmark")
                                    .foregroundStyle(Theme.success)
                                    .opacity(keyCopied ? 1 : 0)
                            }
                        }
                        .disabled(keyText.isEmpty)
                        if sidecarManaged {
                            // 三态反馈：重启中…（禁点）→ ✓ 已更新（1.5s）→ 复位
                            switch regenPhase {
                            case .idle:
                                Button(L("重新生成")) { regenerateKey() }
                            case .restarting:
                                Button(L("重启中…")) {}.disabled(true)
                            case .done:
                                Label(L("已更新"), systemImage: "checkmark")
                                    .foregroundStyle(Theme.success)
                            }
                            Text(L("重新生成会立即重启执行层；其他设备须重新填入新 Key"))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    #endif
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
    }

    @ViewBuilder
    private var notificationSection: some View {
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
    }

    @ViewBuilder
    private var appearanceSection: some View {
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
    }

    @ViewBuilder
    private var sidecarSection: some View {
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
                        // 长度回显：SecureField 打点看不出存了什么——「存没存上/存的
                        // 是不是清洗后的值」必须可见（实机三轮 401 的教训）。与设备 A
                        // `.env.local` 里的字符数对得上即为正确。
                        Text("\(L("已存长度"))：INFER \(store.settings.inferApiKey.count) · RAG \(store.settings.ragApiKey.count)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        // 密钥是拉起时注入的（R5-6）：漏填/改错的实症是推理/RAG 一律
                        // 401（实机查验撞过）。这里把「生效需重启」说破，并给按钮。
                        Text(L("密钥即改即存入钥匙串；对正在运行的执行层，点下方重启后生效。"))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        // 算力端地址（非密钥）：一个字段覆盖 INFER_REMOTE_HOST /
                        // RAG_COMPUTE_HOST 两个变量（两服务同机）——换算力机零重打包。
                        TextField(L("算力端地址（留空 = 按内置配置）"),
                                  text: computeHostBinding, prompt: Text("100.x.x.x"))
                            .autocorrectionDisabled()
                        Text(L("填算力机的 Tailscale IP。改后点下方\"重启执行层\"生效。"))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        // 语义分工（用户拍板 2026-07-18）：这枚 = 纯重启（故障自救/
                        // 应用改过的密钥），**不动任何 key**；换 AGENT_API_KEY 走
                        // 连接页的「重新生成」。旧标签「（应用新密钥）」读起来像会
                        // 动 key，正名为纯动作。
                        Button(L("重启执行层")) {
                            Task { await sidecar.restart() }
                        }
                        .disabled(sidecar.state == .external || sidecar.state == .disabled)
                        LabeledContent(L("状态"), value: sidecarStateLabel)
                        if case .failed(let detail) = sidecar.state {
                            // lib_edge.sh 的原话（端口占用者清单/依赖缺失/日志尾），不二次转译
                            Text(detail)
                                .font(.caption.monospaced())
                                .foregroundStyle(Theme.warning)
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
    }

    @ViewBuilder
    private var versionSection: some View {
                Section {
                    LabeledContent(L("App 版本"), value: Bundle.main.appVersionLabel)
                }
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

    /// 算力端地址即改即存（UserDefaults didSet），注入 spawn env 需重启执行层生效。
    private var computeHostBinding: Binding<String> {
        Binding(get: { store.settings.computeHost },
                set: { store.settings.computeHost = $0 })
    }

    /// 设备 A 服务密钥即改即存（Keychain didSet），运行中的执行层重启后生效。
    /// 粘贴清洗：`.env.local` 的值带双引号（shell source 会剥，env 注入不会）——
    /// 整段拷贝时引号/换行混进来就是稳定 401（实机查验第二轮的根因）。
    private func sidecarKeyBinding(_ key: ReferenceWritableKeyPath<AppSettings, String>)
        -> Binding<String> {
        Binding(get: { store.settings[keyPath: key] },
                set: { store.settings[keyPath: key] = SidecarManager.cleanKey($0) })
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

    #if os(macOS)
    /// 内建执行层在管（含故障态）才轮换得动 key。写成显式 switch 而非链式 ==：
    /// ViewBuilder 里裸 `.failed` 会被类型检查器解析到 SwiftUI 自家的枚举上去
    /// （实报 PencilSqueezeGesturePhase，Release 构建失败）。
    private var sidecarManaged: Bool {
        switch sidecar.state {
        case .running, .starting, .failed: return true
        case .disabled, .external: return false
        }
    }

    /// 复制当前 key 到剪贴板（配合 iPhone 通用剪贴板跨端填 key）。1.5s 反馈复位。
    private func copyKey() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(keyText, forType: .string)
        copyResetTask?.cancel()
        withAnimation(.easeOut(duration: 0.15)) { keyCopied = true }
        copyResetTask = Task {
            try? await Task.sleep(for: .seconds(1.5))
            if !Task.isCancelled {
                withAnimation(.easeIn(duration: 0.2)) { keyCopied = false }
            }
        }
    }

    /// key 轮换：新随机值即刻生效（钥匙串 + 本 app 客户端 + 执行层带新 env 重启）。
    /// 旧 key 随即作废——其他拨号端（iPhone 等）401，须重新填入，这是轮换的本意。
    private func regenerateKey() {
        let newKey = SidecarManager.randomKey()
        keyText = newKey
        store.settings.apiKey = newKey
        store.applySettings()
        regenPhase = .restarting
        Task {
            // restart() 等到执行层重新就绪（state==running）才返回——「已更新」
            // 出现即代表新 key 已实际生效，不是空头反馈
            await sidecar.restart()
            withAnimation { regenPhase = .done }
            try? await Task.sleep(for: .seconds(1.5))
            withAnimation { regenPhase = .idle }
        }
    }
    #endif

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
