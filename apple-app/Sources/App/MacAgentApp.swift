import SwiftUI
#if os(macOS)
import Sparkle
#endif

#if os(macOS)
/// 应用内更新（Sparkle 2）：feed/公钥在 Info.plist（SUFeedURL/SUPublicEDKey），
/// 更新包由 release-macos.sh 公证后发到本构建的 GitHub Release，appcast 推到 feed 专用的滚动 release。
/// 文件级单例：主窗口菜单与菜单栏常驻面板两个入口共用同一个 updater，
/// 且保证 App 结构体重建时不会重复实例化（Sparkle 要求进程内唯一）。
@MainActor let sparkleUpdater = SPUStandardUpdaterController(
    startingUpdater: true, updaterDelegate: nil, userDriverDelegate: nil)
#endif

#if os(macOS)
/// app 收摊时收掉自己拉起的 sidecar（R5-4）——SwiftUI 生命周期没有 willTerminate 等价物。
/// external（devctl 起的）不碰，见 SidecarManager.shutdownSync。
final class MacAppDelegate: NSObject, NSApplicationDelegate {
    static weak var sidecar: SidecarManager?
    static weak var retier: RetierMonitor?
    static weak var store: AgentStore?

    /// 执行层生命周期随 **app 进程**（R5-4 修）：启动时触发一次，与窗口无关——
    /// 挂在窗口 `.task` 上会随窗口重开重入，reap 误杀本代健在服务→凭空重启。
    /// autostart 自守卫 sidecarEnabled：新用户引导未完成时它仍为 false（引导完成的
    /// 回调里才置 true 并触发），老用户迁移后按其原开关值决定启不启——此处一律安全。
    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            // Sparkle 惰性单例必须在启动期显式碰一次：只在菜单按钮闭包里引用的话，
            // 用户不点它 updater 永不实例化，自动检查更新等于没开（首审 C4）。
            _ = sparkleUpdater
            await Self.sidecar?.autostart()
            // 换档巡检排在 autostart 之后：它要问的 backend 正是上一句拉起来的
            // （自身还有 60 秒首检延迟兜住拉起耗时，这里只是不抢在编队之前）。
            Self.retier?.start()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        // willTerminate 在主线程回调；两者都是 @MainActor，直接借道。
        // 顺序钉死：先断 app 自己的 SSE 长连（否则 backend 停机要等满 5 s 优雅超时），
        // 再同步等 sidecar 收摊（waitUntilExit 期间 run loop 仍转，主线程续体照常执行）。
        // test_native_quit_path 钉住这个顺序。
        MainActor.assumeIsolated {
            Self.store?.prepareForTermination()
            Self.sidecar?.shutdownSync()
        }
    }

    /// 红点 / ⌘W 只关窗口：进程留在菜单栏常驻（MenuBarExtra），⌘Q 或菜单栏「退出」才退出。
    /// MenuBarExtra 在场时 SwiftUI 本就不会因末窗关闭而退出，这里把语义钉成显式合同，
    /// 免得日后撤掉常驻面板时关窗变成退出。
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
}
#endif

#if os(macOS)
/// macOS 只连本机后端：离线缓存只有一个当前分区（地址 + 密钥）。换过密钥留下的分区
/// 再也不会被读写，删掉。排进 OfflineCachePruner 串行执行，保留集合在执行时按当时的设置
/// 重算；密钥读出来是空的就不删（OfflineCacheRoot.keepSet）。
enum MacOfflineCaches {
    @MainActor static func prune(_ settings: AppSettings) {
        OfflineCachePruner.shared.schedule { [weak settings] in
            await MainActor.run { () -> Set<String>? in
                guard let settings else { return nil }
                return OfflineCacheRoot.keepSet(macs: [], keys: [:], fallbackBaseURL: settings.baseURL,
                                                fallbackKey: settings.apiKey)
            }
        }
    }
}
#endif

#if os(iOS)
/// APNs 注册回调只走 UIApplicationDelegate——SwiftUI 生命周期没有等价物。
final class AppDelegate: NSObject, UIApplicationDelegate {
    static weak var pushManager: PushManager?

    func application(_ application: UIApplication,
                     didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { @MainActor in Self.pushManager?.handleDeviceToken(deviceToken) }
    }

    func application(_ application: UIApplication,
                     didFailToRegisterForRemoteNotificationsWithError error: Error) {
        print("[Push] APNs 注册失败：\(error.localizedDescription)")
    }
}
#endif

@main
struct MacAgentApp: App {
    @StateObject private var store: AgentStore
    @StateObject private var settings: AppSettings
    @StateObject private var push: PushManager
    @StateObject private var phoneLink: PhoneLink
    #if os(macOS)
    @StateObject private var sidecar: SidecarManager
    @StateObject private var retier: RetierMonitor
    @NSApplicationDelegateAdaptor(MacAppDelegate.self) private var macDelegate
    #endif
    #if os(iOS)
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    #endif

    init() {
        #if os(iOS)
        Keychain.restrictToThisDevice()
        #endif
        let settings = AppSettings()
        let store = AgentStore(settings: settings)
        let push = PushManager(settings: settings)
        push.configure(store: store)
        store.pushManager = push
        _settings = StateObject(wrappedValue: settings)
        _store = StateObject(wrappedValue: store)
        _push = StateObject(wrappedValue: push)
        let phoneLink = PhoneLink()
        #if os(macOS)
        phoneLink.configure(settings: settings)
        #else
        phoneLink.push = push
        push.phoneLink = phoneLink
        phoneLink.configure(settings: settings, store: store)
        #endif
        _phoneLink = StateObject(wrappedValue: phoneLink)
        #if os(macOS)
        let sidecar = SidecarManager()
        sidecar.configure(settings: settings)
        sidecar.onBackendUp = { [weak store] in
            Task { await store?.refreshConnection() }
        }
        _sidecar = StateObject(wrappedValue: sidecar)
        MacAppDelegate.sidecar = sidecar
        MacAppDelegate.store = store
        let retier = RetierMonitor()
        retier.configure(settings: settings)
        _retier = StateObject(wrappedValue: retier)
        MacAppDelegate.retier = retier
        MacOfflineCaches.prune(settings)
        #endif
        #if os(iOS)
        AppDelegate.pushManager = push
        #endif
    }

    var body: some Scene {
        WindowGroup(id: "main") {
            // 首启强制引导（双端）：未完成初始设置前整窗只显示 OnboardingView，
            // 完成后（setupCompleted 翻真）切到主界面。执行层随 app 进程启动（见
            // MacAppDelegate），连接由 AgentStore 按 setupCompleted 后置——引导页
            // 里一律不空转。push.bootstrap（iOS APNs 注册 + 后端设备登记）同样后置
            // 到主界面出现时才跑，避免引导阶段就向后端注册。
            Group {
                if settings.setupCompleted {
                    RootView()
                        .task { push.bootstrap() }
                } else {
                    OnboardingView()
                }
            }
                .environmentObject(store)
                .environmentObject(settings)
                .environmentObject(push)
                .environmentObject(phoneLink)
                #if os(macOS)
                .environmentObject(sidecar)
                .environmentObject(retier)
                #endif
                .preferredColorScheme(settings.colorScheme)
                .tint(Theme.accent)   // 全局主色·青瓷（R5-7 色彩设计）：系统控件一并跟随
                // 语言切换：整树重建让 L() 全部重查表（切换是低频操作，代价可接受）
                .id(settings.language)
                // 灵动岛"查看详情"的 widgetURL 深链：macagent://session/{id}
                .onOpenURL { url in
                    // 扫码配对：macagent://pair?u=&c=（只在 iPhone 上有意义；确认框见下）
                    if let link = PairingLink(url: url) {
                        #if os(iOS)
                        phoneLink.pendingLink = link
                        #else
                        _ = link
                        #endif
                        return
                    }
                    guard url.scheme == "macagent", url.host() == "session" else { return }
                    let sid = url.lastPathComponent
                    if !sid.isEmpty { store.select(sessionId: sid) }
                    #if os(iOS)
                    // app 已在前台点灵动岛进来时 scenePhase 不变化，这里补一次已读清扫
                    push.sweepEndedActivities()
                    #endif
                }
                #if os(iOS)
                // 链接谁都能触发（网页/其他 app）：换密钥前必须让用户看清目标主机并确认
                .alert(L("连接到这台 Mac？"), isPresented: Binding(
                    get: { phoneLink.pendingLink != nil },
                    set: { if !$0 { phoneLink.pendingLink = nil } }),
                       presenting: phoneLink.pendingLink) { link in
                    Button(L("确认连接")) { Task { await phoneLink.claim(link) } }
                    Button(L("取消"), role: .cancel) {}
                } message: { link in
                    // 完整地址（含协议与端口），不只主机名：这是用户辨认真假的唯一依据
                    Text(link.base.absoluteString + "\n" + L("将用一次性配对码为本机领取这台 Mac 的连接凭据，并切换到这台 Mac。"))
                }
                // 同一 Apple ID 的握手：请求已发出，等你在 Mac 上点允许
                .alert(L("等待 Mac 同意"), isPresented: Binding(
                    get: { phoneLink.handshake != nil },
                    set: { if !$0 { phoneLink.handshakeDismissed() } }),
                       presenting: phoneLink.handshake) { handshake in
                    switch handshake.state {
                    case .waiting:
                        Button(L("取消"), role: .cancel) { phoneLink.cancelHandshake() }
                    default:
                        Button(L("重试")) { phoneLink.retryHandshake(handshake.macId) }
                        Button(L("好"), role: .cancel) { phoneLink.cancelHandshake() }
                    }
                } message: { handshake in
                    switch handshake.state {
                    case .waiting where handshake.code.isEmpty:
                        // 75 旧条目的共享密钥换票据：没有核对码，也不用在 Mac 上点允许
                        Text(L("正在与「") + handshake.macName + L("」配对…"))
                    case .waiting:
                        Text(L("请在「") + handshake.macName + L("」上点「允许」。核对码 ") + handshake.code
                             + "\n" + L("请确认 Mac 上显示的是同一个核对码。"))
                    case .denied:
                        Text(L("Mac 拒绝了这次连接请求。"))
                    case .expired:
                        Text(L("等了 5 分钟没有回应，请重新请求。"))
                    case .failed(let reason):
                        Text(L("连接请求没有送达：") + reason)
                    }
                }
                .alert(L("配对失败"), isPresented: Binding(
                    get: { phoneLink.claimError != nil },
                    set: { if !$0 { phoneLink.claimError = nil } })) {
                    Button(L("好")) {}
                } message: {
                    Text(phoneLink.claimError ?? "")
                }
                #endif
                #if os(macOS)
                // iPhone 请求连接：每部 iPhone 都要在这里由你同意一次（核对码与 iPhone 上一致才点允许）
                .alert(L("iPhone 请求连接"), isPresented: Binding(
                    get: { phoneLink.prompt != nil },
                    set: { if !$0 { phoneLink.prompt = nil } }),
                       presenting: phoneLink.prompt) { request in
                    Button(L("允许")) { Task { await phoneLink.decide(request, approve: true) } }
                    Button(L("拒绝"), role: .cancel) { Task { await phoneLink.decide(request, approve: false) } }
                } message: { request in
                    Text("「\(request.name)」" + L("请求连接这台 Mac。核对码 ") + request.code
                         + "\n" + L("请确认 iPhone 上显示的是同一个核对码，不认识的请求请拒绝。"))
                }
                #endif
                #if os(macOS)
                // 动态最窄：检查器开 = 三栏（侧栏 260 + 聊天区实测下限 ~420 +
                // 检查器 260 + 余量），关 = 双栏照旧 760。恒定 860 实机复测仍
                // 溢出错位（聊天列不肯压到 414 以下，两侧同时被裁）——按开合
                // 取值，窄窗只在真需要三栏时才被顶宽。
                .frame(minWidth: store.showInspector ? 960 : 760, minHeight: 520)
                #endif
        }
        #if os(macOS)
        // 标题栏隐藏、内容通顶：系统三色按钮（关闭 / 最小化 / 全屏）留在左上角原生位置，
        // 悬停符号、⌥缩放、全屏退出全由系统包办；侧栏头部为按钮让位见 SessionListView.sidebarHeader。
        // RootView 不得再隐藏窗口工具栏：hiddenTitleBar 下那一句会连三色按钮一起抹掉（build 80 复现）。
        .windowStyle(.hiddenTitleBar)
        .commands {
            // 惯例位置：应用菜单「关于」之后
            CommandGroup(after: .appInfo) {
                Button(L("检查更新…")) { sparkleUpdater.checkForUpdates(nil) }
            }
        }
        #endif

        #if os(macOS)
        // 菜单栏常驻：主窗口关掉后仍可看连接状态、一键回到对话；
        // 进程常驻也是 macOS 本地通知全程可达的前提（feed 监听不死）。
        MenuBarExtra("MacAgent", systemImage: "brain") {
            MenuBarPanel()
                .environmentObject(store)
                .environmentObject(settings)
                .environmentObject(sidecar)
                .environmentObject(retier)
                .id(settings.language)
        }
        #endif
    }
}

#if os(macOS)
struct MenuBarPanel: View {
    @EnvironmentObject var store: AgentStore
    @EnvironmentObject var sidecar: SidecarManager
    @EnvironmentObject var retier: RetierMonitor
    @Environment(\.openWindow) private var openWindow

    private var sidecarLabel: String? {
        switch sidecar.state {
        case .disabled: return nil          // 没开内建执行层就不占一行
        case .external: return L("执行层：外部（devctl）")
        case .starting: return L("执行层：启动中…")
        case .running:  return L("执行层：运行中")
        case .failed:   return L("执行层：故障（见设置）")
        }
    }

    var body: some View {
        Group {
            switch store.connState {
            case .connected:
                Text("\(store.connectedLabel) · \(store.settings.baseURL?.host() ?? "")")
            case .engineStarting:
                Text(store.engineStartingLabel)
            case .connecting:
                Text(L("连接中…"))
            case .offline:
                Text(L("未连接"))
            case .unauthorized:
                Text(L("服务可达 · 请检查连接密钥"))
            case .reachable:
                Text(L("服务可达 · 业务状态未知"))
            }
            // 后端可达时计算端是另一道独立状态（定格失败由它承载，PROTOCOL §2.10）；其它态无意义
            if store.connState == .connected || store.connState == .engineStarting {
                Text(store.computeStatusLabel)
            }
            if let label = sidecarLabel {
                Text(label)
            }
            // 换档重建期间知识库不可用：主窗口关着也要能看见它在跑（RetierMonitor）
            if let p = retier.rebuildProgress {
                Text(p)
            }
            if store.isGenerating {
                Text(L("正在生成…"))
            }
            Divider()
            Button(L("打开 MacAgent")) {
                activateMainWindow()
            }
            Button(L("新对话")) {
                store.newSession()
                activateMainWindow()
            }
            Divider()
            // 菜单栏常驻面板也给一个入口：主窗口关掉时更新入口不消失
            Button(L("检查更新…")) {
                NSApp.activate(ignoringOtherApps: true)
                sparkleUpdater.checkForUpdates(nil)
            }
            Button(L("退出")) {
                NSApp.terminate(nil)
            }
        }
    }

    private func activateMainWindow() {
        NSApp.activate(ignoringOtherApps: true)
        openWindow(id: "main")
    }
}
#endif
