import SwiftUI

#if os(macOS)
/// app 收摊时收掉自己拉起的 sidecar（R5-4）——SwiftUI 生命周期没有 willTerminate 等价物。
/// external（devctl 起的）不碰，见 SidecarManager.shutdownSync。
final class MacAppDelegate: NSObject, NSApplicationDelegate {
    static weak var sidecar: SidecarManager?

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
            RootView()
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
                .task { push.bootstrap() }
                #if os(macOS)
                .task { await sidecar.autostart() }
                #endif
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
                .frame(minWidth: 760, minHeight: 520)
                #endif
        }

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
            Text(store.connectionOK == true
                 ? "\(L("已连接")) · \(store.settings.baseURL?.host() ?? "")"
                 : L("未连接"))
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
