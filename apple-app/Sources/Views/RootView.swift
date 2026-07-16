import SwiftUI

/// 会话导航路由（iOS 用 item 绑定驱动 push/pop）。
struct SessionRoute: Identifiable, Hashable {
    let id: String
}

/// 导航骨架。平台分流：
/// - iOS 用 NavigationStack + navigationDestination(item:)——返回时系统自动把绑定
///   清回 nil，同一行可反复进入（此前用 List selection + onDisappear 手工清理，
///   时机不可靠，会出现「进入即弹回/只能看一次」的死锁）。
/// - macOS 保持 NavigationSplitView 双栏。
struct RootView: View {
    @EnvironmentObject var store: AgentStore
    @EnvironmentObject var push: PushManager
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        content
            .onChange(of: scenePhase) {
                // 转后台 → 启动全局任务监听（含 web 端发起的轮次）；回前台 → 停
                store.setAppActive(scenePhase == .active)
                #if os(iOS)
                // 回前台=已读：已完结的锁屏进度活动就地撤下（点进度卡进 app 也走这里）
                if scenePhase == .active { push.sweepEndedActivities() }
                #endif
            }
    }

    @ViewBuilder
    private var content: some View {
        #if os(iOS)
        NavigationStack {
            SessionListView()
                .navigationTitle("MacAgent")
                .navigationDestination(item: chatRoute) { _ in
                    ChatView()
                        .navigationBarTitleDisplayMode(.inline)
                }
        }
        #else
        NavigationSplitView {
            SessionListView()
                .navigationTitle("MacAgent")
                .navigationSplitViewColumnWidth(min: 220, ideal: 260)
        } detail: {
            if store.selectedSessionId != nil {
                ChatView()
            } else {
                ContentUnavailableView(L("选择或新建一个会话"),
                                       systemImage: "bubble.left.and.bubble.right",
                                       description: Text("\(L("左上角 ✎ 开始新对话")) · \(Bundle.main.appVersionLabel)"))
            }
        }
        #endif
    }

    #if os(iOS)
    private var chatRoute: Binding<SessionRoute?> {
        Binding(
            get: { store.selectedSessionId.map { SessionRoute(id: $0) } },
            set: { route in
                if route == nil {
                    store.selectedSessionId = nil  // 系统 pop 时回调，时机可靠
                }
            }
        )
    }
    #endif
}
