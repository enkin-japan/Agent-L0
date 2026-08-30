import SwiftUI
#if os(macOS)
import Sparkle
#endif

#if os(macOS)
/// 应用内更新（Sparkle 2）：feed/公钥在 Info.plist（SUFeedURL/SUPublicEDKey），
/// 更新包由 release-macos.sh 公证后连同 appcast 推到公共发布仓的滚动 release。
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
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        // willTerminate 在主线程回调；shutdownSync 是 @MainActor，直接借道
        MainActor.assumeIsolated { Self.sidecar?.shutdownSync() }
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
    #if os(macOS)
    @StateObject private var sidecar: SidecarManager
    @NSApplicationDelegateAdaptor(MacAppDelegate.self) private var macDelegate
    #endif
    #if os(iOS)
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    #endif

    init() {
        let settings = AppSettings()
        let store = AgentStore(settings: settings)
        let push = PushManager(settings: settings)
        push.configure(store: store)
        store.pushManager = push
        _settings = StateObject(wrappedValue: settings)
        _store = StateObject(wrappedValue: store)
        _push = StateObject(wrappedValue: push)
        #if os(macOS)
        let sidecar = SidecarManager()
        sidecar.configure(settings: settings)
        _sidecar = StateObject(wrappedValue: sidecar)
        MacAppDelegate.sidecar = sidecar
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
                #if os(macOS)
                .environmentObject(sidecar)
                #endif
                .preferredColorScheme(settings.colorScheme)
                .tint(Theme.accent)   // 全局主色·青瓷（R5-7 色彩设计）：系统控件一并跟随
                // 语言切换：整树重建让 L() 全部重查表（切换是低频操作，代价可接受）
                .id(settings.language)
                // 灵动岛"查看详情"的 widgetURL 深链：macagent://session/{id}
                .onOpenURL { url in
                    guard url.scheme == "macagent", url.host() == "session" else { return }
                    let sid = url.lastPathComponent
                    if !sid.isEmpty { store.select(sessionId: sid) }
                    #if os(iOS)
                    // app 已在前台点灵动岛进来时 scenePhase 不变化，这里补一次已读清扫
                    push.sweepEndedActivities()
                    #endif
                }
                #if os(macOS)
                // 动态最窄：检查器开 = 三栏（侧栏 260 + 聊天区实测下限 ~420 +
                // 检查器 260 + 余量），关 = 双栏照旧 760。恒定 860 实机复测仍
                // 溢出错位（聊天列不肯压到 414 以下，两侧同时被裁）——按开合
                // 取值，窄窗只在真需要三栏时才被顶宽。
                .frame(minWidth: store.showInspector ? 960 : 760, minHeight: 520)
                #endif
        }
        #if os(macOS)
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
                .id(settings.language)
        }
        #endif
    }
}

#if os(macOS)
struct MenuBarPanel: View {
    @EnvironmentObject var store: AgentStore
    @EnvironmentObject var sidecar: SidecarManager
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
                Text("\(L("已连接")) · \(store.settings.baseURL?.host() ?? "")")
            case .connecting:
                Text(L("连接中…"))
            case .offline:
                Text(L("未连接"))
            }
            if let label = sidecarLabel {
                Text(label)
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
