import SwiftUI

/// 设置。iPhone 只经 iCloud 钥匙串 / 扫码连接 Mac，不给手填入口（2026-10-27 用户决定）；
/// macOS 恒连本机回环，AGENT 密钥只在 devctl / repo 布局手填（bundle 布局自动生成）。
struct SettingsView: View {
    @EnvironmentObject var store: AgentStore
    @EnvironmentObject var push: PushManager
    #if os(macOS)
    @EnvironmentObject var sidecar: SidecarManager
    #endif
    @Environment(\.dismiss) private var dismiss

    #if os(macOS)
    @State private var urlText = ""
    @State private var keyText = ""
    @State private var testResult: String?
    @State private var keyCopied = false
    @State private var copyResetTask: Task<Void, Never>?
    private enum RegenPhase { case idle, restarting, done }
    @State private var regenPhase: RegenPhase = .idle
    // 「重启执行层」独立三态（与上面的「重新生成密钥」同模式但各自计时，
    // 共用一个实例会让两枚钮互相串反馈）
    private enum RestartPhase { case idle, restarting, done, failed }
    @State private var restartPhase: RestartPhase = .idle
    // 「立即清除算力端会话缓存」两态：飞行中禁点 + 进行中文案；结果就地一行小字，不弹窗
    @State private var kvClearing = false
    @State private var kvClearResult: String?
    // Tab 选中态必须显式绑定：store/push 是高频发布源（连接状态、流式事件），
    // 每次 body 重算时无绑定的 TabView 会重置回第一页——实机症状即"点击不切页/
    // 切过去又弹回第一页"。@State 存活于重渲染，选中页才稳得住。
    private enum SettingsTab: Hashable, CaseIterable, Identifiable {
        case connection, notification, permissions, knowledge, apiTools, skills, registrations,
             sidecar, appearance
        var id: Self { self }
        var title: String {
            switch self {
            case .connection:    return L("连接")
            case .notification:  return L("通知")
            case .permissions:   return L("权限")
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
            case .permissions:   return "hand.raised"
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
                // 审批策略（Contract P）：配对的 iPhone 同样能读写，自成一节紧随连接设置
                ApprovalPolicySection(title: L("审批策略"))
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
                // iOS 页上已无待保存的输入：Mac 列表点选即生效，通知/外观即改即存
                ToolbarItem(placement: .confirmationAction) {
                    Button(L("完成")) { dismiss() }
                }
            }
            #endif
        }
        .onAppear {
            #if os(macOS)
            urlText = store.settings.baseURLString
            keyText = store.settings.apiKey
            #endif
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
        case .permissions:
            Form { ApprovalPolicySection(title: L("权限")) }.formStyle(.grouped)
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
                #if os(iOS)
                // iPhone 连 Mac 只有两条路：iCloud 钥匙串同步的 Mac 列表、扫 Mac 上的配对
                // 二维码（2026-10-27 用户决定）。不给地址 / 密钥输入框，也没有「高级」折叠。
                PhoneConnectionSection()
                #else
                agentKeySection
                Section {
                    Button(L("测试连接")) { test() }
                    if let result = testResult {
                        Text(result).font(.caption)
                    }
                }
                MacPhoneLinkSection()
                #endif
    }

    #if os(macOS)
    /// AGENT 密钥：bundle 布局由 app 自管（引导页 / 执行层首启自动生成、拉起时注入），
    /// 只摆一行状态 + 重新生成；devctl / repo 布局密钥在仓库 `.env.local`，须手填同值。
    @ViewBuilder
    private var agentKeySection: some View {
                Section("API Key") {
                    if agentKeyManagedByApp {
                        Label(L("后端密钥已自动生成并同步"), systemImage: "checkmark.seal")
                            .foregroundStyle(.secondary)
                        // 重新生成 = key 轮换：写钥匙串 → 本 app 客户端 → 执行层带新 env 重启
                        // （bundle 布局 resolveLayout 从 settings.apiKey 注入 AGENT_API_KEY）。
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
                        Text(L("重新生成会立即重启执行层；仍在用旧共享密钥的客户端（旧版手填或 devctl 工具）会收到 401，须重新配对或同步密钥。经你同意或扫码连上的 iPhone 用的是自己的设备票据，不受影响。"))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        if store.settings.phoneSyncEnabled {
                            Text(L("iCloud 钥匙串里只有地址与连接请求凭据，没有这把 Key。"))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    } else {
                        SecureField(L("AGENT_API_KEY（存入钥匙串）"), text: $keyText)
                        // 复制（SecureField 抄不出来）。不给「重新生成」：这些布局的后端从
                        // .env.local 取密钥，这里换了重启后也对不上。
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
                        Text(L("与仓库根 .env.local 里的 AGENT_API_KEY 相同；后端未启用鉴权时留空。"))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
    }
    #endif

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
                        SecureField("RAG_COMPUTE_API_KEY", text: sidecarKeyBinding(\.ragComputeApiKey))
                        Text(L("知识库计算服务使用算力端的 RAG_COMPUTE_API_KEY，与本机数据服务的密钥不同。"))
                            .font(.caption).foregroundStyle(.secondary)
                        DisclosureGroup(L("知识库数据服务密钥（高级）")) {
                            SecureField("RAG_API_KEY", text: sidecarKeyBinding(\.ragApiKey))
                            Text(L("本机数据服务留空时自动生成；旧版整库远端部署须填写远端的 RAG_API_KEY。修改计算服务密钥不会更改此项。"))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        // 长度回显：SecureField 打点看不出存了什么——「存没存上/存的
                        // 是不是清洗后的值」必须可见（实机三轮 401 的教训）。与设备 A
                        // `.env.local` 里的字符数对得上即为正确。
                        Text("\(L("已存长度"))：INFER \(store.settings.inferApiKey.count) · RAG COMPUTE \(store.settings.ragComputeApiKey.count)")
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
                        // 会话 KV 落盘（kv_persist）：决策权在执行端，缺省关（KV 实质是
                        // 会话内容）。开关与天数经 spawn env 注入，故与算力端地址同域。
                        Toggle(L("会话缓存落盘（算力端热恢复）"), isOn: kvPersistBinding)
                        Text(L("开启后，冻结会话的 KV 会存到算力端磁盘，解冻快约 3-13 秒；到期自动清除。开关改动点\"重启执行层\"生效；关闭后不再写入新缓存，历史残留用下方按钮清除。"))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Stepper(value: kvTTLDaysBinding, in: 1...365) {
                            Text("\(L("留存天数"))：\(store.settings.kvTTLDays)")
                        }
                        .disabled(!store.settings.kvPersistEnabled)
                        // 清除钮**不看开关状态**恒可用：关掉开关之后清历史残留正是主用例。
                        // 两态反馈（照重启钮的三态简化）：清除中…（禁点）→ 就地一行结果。
                        if kvClearing {
                            Button(L("清除中…")) {}.disabled(true)
                        } else {
                            Button(L("立即清除算力端会话缓存")) { clearKVStore() }
                        }
                        if let result = kvClearResult {
                            Text(result)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                        // 语义分工（用户拍板 2026-07-18）：这枚 = 纯重启（故障自救/
                        // 应用改过的密钥），**不动任何 key**；换 AGENT_API_KEY 走
                        // 连接页的「重新生成」。旧标签「（应用新密钥）」读起来像会
                        // 动 key，正名为纯动作。
                        // 三态反馈：重启中…（禁点）→ ✓ 已重启（1.5s）→ 复位；
                        // 重启要好几秒，零反馈时用户只能盯着不动的按钮猜。
                        switch restartPhase {
                        case .idle:
                            Button(L("重启执行层")) { restartSidecar() }
                                .disabled(sidecar.state == .external || sidecar.state == .disabled)
                        case .restarting:
                            Button(L("重启中…")) {}.disabled(true)
                        case .done:
                            Label(L("已重启"), systemImage: "checkmark")
                                .foregroundStyle(Theme.success)
                        case .failed:
                            // 不二次转译：直接摆 sidecar 自己的状态文本（详情在下方 failed 分支）
                            Label(sidecarStateLabel, systemImage: "exclamationmark.triangle")
                                .foregroundStyle(Theme.warning)
                        }
                        LabeledContent(L("状态"), value: sidecarStateLabel)
                        if sidecar.configuration?.incomplete == true {
                            Text(L("设置已保存，算力端地址尚未配置。请填写上方地址后重启执行层。"))
                                .font(.caption).foregroundStyle(Theme.warning)
                        } else if sidecar.configuration?.status == "unknown" {
                            Text(L("尚未读取到有效配置；保存设置不代表服务已就绪。"))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        // 布局形态显性化（#85）：bundle 判据失败时会静默退 repo 布局，
                        // 密钥来源随之从钥匙串换成 .env.local——那正是 2026-08-10 全天
                        // 401 的根因。形态与密钥来源必须写在脸上，不能靠翻日志推断。
                        if let layoutLabel = sidecarLayoutLabel {
                            LabeledContent(L("布局"), value: layoutLabel)
                        }
                        if sidecar.isBareBundle {
                            Label(L("包内无执行层（裸包）——完整功能需用 bundle-macos.sh 的全栈包"),
                                  systemImage: "shippingbox")
                                .font(.caption)
                                .foregroundStyle(Theme.warning)
                        }
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

    /// 会话 KV 落盘开关/天数即改即存（UserDefaults didSet，天数在 setter 里钳 1...365）；
    /// 两者都是 spawn 时注入的 env，对正在跑的执行层要点「重启执行层」才生效。
    private var kvPersistBinding: Binding<Bool> {
        Binding(get: { store.settings.kvPersistEnabled },
                set: { store.settings.kvPersistEnabled = $0 })
    }

    private var kvTTLDaysBinding: Binding<Int> {
        Binding(get: { store.settings.kvTTLDays },
                set: { store.settings.kvTTLDays = $0 })
    }

    /// 设备 A 服务密钥即改即存（Keychain didSet），运行中的执行层重启后生效。
    /// 粘贴清洗：`.env.local` 的值带双引号（shell source 会剥，env 注入不会）——
    /// 整段拷贝时引号/换行混进来就是稳定 401（实机查验第二轮的根因）。
    private func sidecarKeyBinding(_ key: ReferenceWritableKeyPath<AppSettings, String>)
        -> Binding<String> {
        Binding(get: { store.settings[keyPath: key] },
                set: { store.settings[keyPath: key] = SidecarManager.cleanKey($0) })
    }

    /// 布局形态 + 密钥来源（nil = 尚未解析出布局，不显示该行）。
    /// 在跑的那个后端自报的形态与本 app 解析出的不一致时（external 收养的 dev 后端），
    /// 把对方的形态一并摆出来——「我按哪套起 / 实际连上的是哪套」是两件事。
    private var sidecarLayoutLabel: String? {
        guard let kind = sidecar.layoutKind else { return nil }
        var label = kind == "bundle"
            ? L("bundle（密钥：钥匙串）")
            : L("repo（dev 桥接，密钥来源 .env.local）")
        if let peer = sidecar.peerLayout, peer != kind {
            label += "（\(L("在跑的后端自报"))：\(peer)）"
        }
        return label
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

    #if os(macOS)
    private func save() {
        store.settings.baseURLString = urlText.trimmingCharacters(in: .whitespacesAndNewlines)
        // 自管布局下输入框不在页上：keyText 只是打开页面时的快照，执行层其间生成/轮换的
        // 新 key 不得被它写回旧值
        if !agentKeyManagedByApp {
            store.settings.apiKey = keyText.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        store.applySettings()
        MacOfflineCaches.prune(store.settings)
        dismiss()
    }

    /// AGENT 密钥归 app 自管 = bundle 布局 + 内建执行层在管（含故障态）+ 14159 上的后端
    /// 没探到（nil）或自报 bundle。自报 repo / unknown（未设 AGENT_LAYOUT 起的后端）时
    /// 指纹不符的提示会让用户「同步 .env.local 与 app 设置的密钥」，输入框必须在。
    /// 未启用 / 收养外部后端 / repo 布局一律手填。
    /// 写成显式 switch 而非链式 ==：ViewBuilder 里裸 `.failed` 会被类型检查器解析到
    /// SwiftUI 自家的枚举上去（实报 PencilSqueezeGesturePhase，Release 构建失败）。
    private var agentKeyManagedByApp: Bool {
        guard sidecar.layoutKind == "bundle",
              sidecar.peerLayout == nil || sidecar.peerLayout == "bundle" else { return false }
        switch sidecar.state {
        case .running, .starting, .failed: return true
        case .disabled, .external: return false
        }
    }

    /// 复制当前 key 到剪贴板（devctl / repo 布局下给其他本机工具用）。1.5s 反馈复位。
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
    /// 旧 key 随即作废——手动填了它的客户端 401，须重新填入，这是轮换的本意。经同意或扫码
    /// 连上的 iPhone 用自己的设备票据，不受影响；iCloud 条目里本来就没有 key（76 起）。
    private func regenerateKey() {
        let newKey = SidecarManager.randomKey()
        keyText = newKey
        store.settings.apiKey = newKey
        store.applySettings()
        MacOfflineCaches.prune(store.settings)
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

    /// 立即清除算力端已落盘的会话 KV（DELETE /api/v1/compute/kvstore，backend 薄代理）。
    /// 与落盘开关状态无关：关掉开关后清历史残留正是主用例。
    private func clearKVStore() {
        guard let base = store.settings.baseURL, base.scheme != nil,
              let url = URL(string: "/api/v1/compute/kvstore", relativeTo: base) else {
            kvClearResult = L("❌ 地址无效")
            return
        }
        var req = URLRequest(url: url)
        req.httpMethod = "DELETE"
        // 比 backend 代理（30s）再宽一档：让「代理先答」成为常态、URLSession 超时成为例外
        req.timeoutInterval = 40
        let key = store.settings.apiKey
        if !key.isEmpty {
            req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }
        kvClearing = true
        kvClearResult = nil
        Task {
            do {
                let (data, resp) = try await URLSession.shared.data(for: req)
                let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
                if code == 200,
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    let deleted = obj["deleted"] as? Int ?? 0
                    let mb = Double(obj["bytes"] as? Int ?? 0) / 1_048_576
                    kvClearResult = "\(L("已清除")) \(deleted) \(L("份"))"
                        + "（\(String(format: "%.1f", mb)) MB）"
                } else {
                    // backend 不可达/算力端不可达是 503 + {"detail": ...}：把它的原话带给用户
                    kvClearResult = "❌ " + kvErrorDetail(from: data, code: code)
                }
            } catch {
                kvClearResult = "❌ " + error.localizedDescription
            }
            kvClearing = false
        }
    }

    /// FastAPI 的错误体是 {"detail": ...}；解不出就退回状态码，不把整段 HTML/空串糊给用户。
    private func kvErrorDetail(from data: Data, code: Int) -> String {
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let detail = obj["detail"] as? String, !detail.isEmpty {
            return detail
        }
        return "HTTP \(code)"
    }

    /// 纯重启（不动任何 key）：restart() 等到执行层重新就绪才返回，
    /// 所以返回后 state==.running 即"真的起来了"，否则摆状态文本让用户看到没起来。
    private func restartSidecar() {
        restartPhase = .restarting
        Task {
            await sidecar.restart()
            let ok = sidecar.state == .running
            withAnimation { restartPhase = ok ? .done : .failed }
            try? await Task.sleep(for: .seconds(ok ? 1.5 : 3))
            withAnimation { restartPhase = .idle }
        }
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
    #endif
}

/// 两类工具的审批模式（Contract P），双平台共用：macOS 在「权限」标签页，iPhone 在设置页
/// 「审批策略」一节。后端是权威：出现时 GET，改动时只 PATCH 改动的那个键；失败回退选择并在
/// 节内显示服务端给的原因（旧执行层对配对设备的 PATCH 回 403，原文照显）。AppSettings 里的
/// 镜像值只撑首拉前的显示。
struct ApprovalPolicySection: View {
    @EnvironmentObject var store: AgentStore
    var title: String = L("权限")
    @State private var loaded = false
    @State private var loading = false
    @State private var saving = false
    @State private var errorText: String?

    private enum PolicyClass { case read, write }

    var body: some View {
        Section {
            Picker(L("读取 / 检索类"), selection: binding(.read)) { modeOptions }
                .disabled(!loaded || saving)
            Picker(L("写入 / 删除类"), selection: binding(.write)) { modeOptions }
                .disabled(!loaded || saving)
            if loading && !loaded {
                Text(L("正在读取后端的权限设置…")).font(.caption).foregroundStyle(.secondary)
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
            Text(title)
        } footer: {
            Text(L("读取 / 检索类：只查看或检索信息、不改变任何东西的工具。写入 / 删除类：会修改、发送或删除内容的工具。Shell 命令按每条命令单独归类：只读命令算读取类；会改变东西的 Shell 命令始终询问，定时任务的创建/修改与 API 工具注册同样始终询问，「自动放行」只对其他写入工具生效。「按工具默认」沿用各工具自身的审批设置。"))
                .font(.caption).foregroundStyle(.secondary)
        }
        .task(id: store.client.map { ObjectIdentifier($0) }) { await load() }
    }

    @ViewBuilder
    private var modeOptions: some View {
        Text(L("自动放行")).tag("auto")
        Text(L("按工具默认")).tag("default")
        Text(L("每次询问")).tag("ask")
    }

    private func current(_ cls: PolicyClass) -> String {
        cls == .read ? store.settings.approvalPolicyRead : store.settings.approvalPolicyWrite
    }

    private func mirror(_ cls: PolicyClass, _ mode: String) {
        if cls == .read { store.settings.approvalPolicyRead = mode }
        else { store.settings.approvalPolicyWrite = mode }
    }

    private func adopt(_ policy: ApprovalPolicy) {
        if ApprovalPolicy.modes.contains(policy.read) { mirror(.read, policy.read) }
        if ApprovalPolicy.modes.contains(policy.write) { mirror(.write, policy.write) }
    }

    private func binding(_ cls: PolicyClass) -> Binding<String> {
        Binding(get: { current(cls) }, set: { mode in
            let previous = current(cls)
            guard mode != previous, let client = store.client else { return }
            mirror(cls, mode)
            saving = true
            errorText = nil
            Task {
                let patch = cls == .read ? ApprovalPolicyPatch(read: mode) : ApprovalPolicyPatch(write: mode)
                do {
                    let policy = try await client.updateApprovalPolicy(patch)
                    guard store.client === client else { saving = false; return }
                    adopt(policy)
                } catch {
                    guard store.client === client else { saving = false; return }
                    if current(cls) == mode { mirror(cls, previous) }
                    errorText = L("权限设置保存失败：") + Self.serverMessage(error)
                }
                saving = false
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
            let policy = try await client.approvalPolicy()
            guard store.client === client else { return }
            adopt(policy)
            loaded = true
            errorText = nil
        } catch {
            guard store.client === client else { return }
            loaded = false
            if case AgentError.http(404, _) = error {
                errorText = L("当前后端不支持权限设置（需要 build 78 及以上的执行层）。")
            } else {
                errorText = L("权限设置加载失败：") + Self.serverMessage(error)
            }
        }
    }

    /// 服务端拒绝的原因原文：FastAPI 的 {"detail": "…"}（如配对设备 403「只有 Mac 本机能…」）
    /// 取出 detail 字符串，带上状态码；取不出就用错误本身的描述。
    static func serverMessage(_ error: Error) -> String {
        if case AgentError.http(let code, let body) = error,
           let data = body.data(using: .utf8),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            for key in ["detail", "reason", "response"] {
                if let text = object[key] as? String, !text.isEmpty { return "HTTP \(code)：\(text)" }
            }
        }
        return error.localizedDescription
    }
}

