import SwiftUI

/// 会话导航路由（iOS 用 item 绑定驱动 push/pop）。
struct SessionRoute: Identifiable, Hashable {
    let id: String
}

/// 导航骨架。平台分流：
/// - iOS 用 NavigationStack + navigationDestination(item:)——返回时系统自动把绑定
///   清回 nil，同一行可反复进入（此前用 List selection + onDisappear 手工清理，
///   时机不可靠，会出现「进入即弹回/只能看一次」的死锁）。
/// - macOS 保留手工定宽双栏，避免流式更新触发系统分栏的约束重入。
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
        // macOS 27 beta（26A5388g）临时形态：手工分栏替代 NavigationSplitView。
        // 三份 .ips（13:20/13:32/13:41）同链—— SplitViewChildController
        // didUpdateMinSize → 约束阶段重入 → AppKit 硬抛 SIGABRT；流式内容任何
        // 高度变化都可触发，逐点钉尺寸（代码块降级/常量高度内滚台）两轮未根除，
        // 故把该机制从存在上移除：HStack + 定宽侧栏走不进 SplitViewChildController。
        // 代价：侧栏不可拖宽、无折叠动画。27 正式版修复重入执法后可整段回退
        // NavigationSplitView（git 可查）。
        NavigationStack {
            HStack(spacing: 0) {
                SessionListView()
                    .frame(width: 260)
                    .background(Theme.sidebar)
                Rectangle().fill(Theme.separator).frame(width: 1)
                Group {
                    if store.selectedSessionId != nil {
                        ChatView()
                    } else {
                        ConversationWelcomeView()
                            .padding(Theme.contentPadding)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Theme.canvas)
            }
            .frame(minWidth: 760, minHeight: 480)
            .toolbar(.hidden, for: .windowToolbar)
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
