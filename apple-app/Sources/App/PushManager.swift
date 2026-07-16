import Foundation
import UserNotifications
#if os(iOS)
import ActivityKit
import UIKit
#else
import AppKit
#endif

/// 通知枢纽（双平台一份）：
/// - 通知类别注册（APPROVAL：允许=authenticationRequired 必须解锁，拒绝=免解锁——
///   非对称设计：拒绝是安全方向可随处按，放行必须过 Face ID/密码）；
/// - 深链：点通知/审批动作 → 打开对应会话（恢复走统一 /turn + /events 路，无补偿路径）；
/// - iOS：APNs 设备注册 + 偏好同步 + Live Activity 起停与 token 上报（含 push-to-start）；
/// - macOS：postLocal 本地通知出口（数据源=app 已有的全局 feed 监听，零出网）。
@MainActor
final class PushManager: NSObject, ObservableObject {
    static let approvalCategoryId = "APPROVAL"
    static let approveActionId = "APPROVE_ACTION"
    static let denyActionId = "DENY_ACTION"

    let settings: AppSettings
    private weak var store: AgentStore?

    /// OS 层通知授权状态（设置页据此显示"系统已拒绝"等失效原因）
    @Published var authStatus: UNAuthorizationStatus?

    /// app 生成并持久化的稳定设备标识（后端注册表主键；APNs token 轮换它不变）
    let deviceId: String

    #if os(iOS)
    private var apnsToken: String?
    private var activityTokenTasks: [String: Task<Void, Never>] = [:]   // activity.id →
    #endif

    init(settings: AppSettings) {
        self.settings = settings
        if let id = UserDefaults.standard.string(forKey: "push.deviceId") {
            deviceId = id
        } else {
            let id = UUID().uuidString.lowercased()
            UserDefaults.standard.set(id, forKey: "push.deviceId")
            deviceId = id
        }
        super.init()
    }

    func configure(store: AgentStore) {
        self.store = store
    }

    // MARK: - 启动

    func bootstrap() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        registerCategories()
        refreshAuthStatus()
        if settings.notifEnabled {
            requestAuthorizationAndRegister()
        }
        #if os(iOS)
        observePushToStart()
        observeActivityUpdates()
        #endif
    }

    private func registerCategories() {
        // 允许：authenticationRequired——锁屏上按下先解锁再放行（批的是在 Mac 上执行敏感
        // 操作，这是安全底线）。拒绝：免解锁（安全方向）。都不带 .foreground（后台直批）。
        let approve = UNNotificationAction(
            identifier: Self.approveActionId, title: L("允许执行"),
            options: [.authenticationRequired])
        let deny = UNNotificationAction(
            identifier: Self.denyActionId, title: L("拒绝"),
            options: [.destructive])
        let approval = UNNotificationCategory(
            identifier: Self.approvalCategoryId, actions: [approve, deny],
            intentIdentifiers: [], options: [])
        UNUserNotificationCenter.current().setNotificationCategories([approval])
    }

    func refreshAuthStatus() {
        UNUserNotificationCenter.current().getNotificationSettings { [weak self] s in
            Task { @MainActor in self?.authStatus = s.authorizationStatus }
        }
    }

    private func requestAuthorizationAndRegister() {
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound, .badge]) { [weak self] granted, _ in
            Task { @MainActor in
                self?.refreshAuthStatus()
                guard granted else { return }
                #if os(iOS)
                UIApplication.shared.registerForRemoteNotifications()
                #endif
            }
        }
    }

    // MARK: - 设置同步（设置页开关改动后调）

    func settingsChanged() {
        if settings.notifEnabled {
            requestAuthorizationAndRegister()
            #if os(iOS)
            syncPrefsToBackend()
            #endif
        } else {
            #if os(iOS)
            // 总开关关：注销后端设备（macOS 本地路径由 postLocal 的开关判断兜住）
            let id = deviceId
            if let client = settings.makeClient() {
                Task { try? await client.pushUnregister(deviceId: id) }
            }
            #endif
        }
    }

    #if os(iOS)
    private func currentPrefs() -> AgentClient.PushPrefsBody {
        AgentClient.PushPrefsBody(
            results: settings.notifResults,
            schedule_start: settings.notifScheduleStart,
            approval: settings.notifApproval,
            content_level: settings.notifContentLevel)
    }

    /// APNs 设备 token 回调（AppDelegate 转发）：注册/更新到后端。
    func handleDeviceToken(_ tokenData: Data) {
        let hex = tokenData.map { String(format: "%02x", $0) }.joined()
        apnsToken = hex
        guard settings.notifEnabled, let client = settings.makeClient() else { return }
        let id = deviceId
        let prefs = currentPrefs()
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String
        Task {
            do {
                _ = try await client.pushRegister(deviceId: id, token: hex,
                                                  prefs: prefs, appBuild: build)
            } catch {
                print("[Push] 设备注册失败（下次启动/换 token 重试）：\(error.localizedDescription)")
            }
        }
    }

    func syncPrefsToBackend() {
        guard let client = settings.makeClient() else { return }
        let id = deviceId
        let prefs = currentPrefs()
        Task {
            do {
                try await client.pushUpdatePrefs(deviceId: id, prefs: prefs)
            } catch {
                // 设备可能尚未注册（404）：走一次 upsert 补注册
                _ = try? await client.pushRegister(deviceId: id, token: self.apnsToken,
                                                   prefs: prefs, appBuild: nil)
            }
        }
    }
    #endif

    // MARK: - macOS 本地通知出口（数据源：AgentStore 的全局 feed 监听；零出网）

    enum LocalKind {
        case result(status: String)
        case approval(tools: [String])
        case approvalAutodenied
        case scheduleStart
    }

    /// 按偏好开关 + 内容分级组装并投递本地通知。iOS 不走本函数（横幅由 APNs 远程投递，
    /// 本地再发一份会双横幅）。
    func postLocal(kind: LocalKind, sessionId: String, turnId: String?,
                   sessionTitle: String, fullBody: String? = nil) {
        guard settings.notifEnabled else { return }
        let level = settings.notifContentLevel
        var categoryId: String?
        var kindTag = ""
        var body: String

        switch kind {
        case .result(let status):
            guard settings.notifResults else { return }
            kindTag = "result"
            let mark: String
            switch status {
            case "success": mark = L("任务完成 ✓")
            case "fail": mark = L("任务失败 ✗")
            case "warning": mark = L("任务完成（有警告）")
            default: mark = L("任务结束")
            }
            body = (level == "full" ? (fullBody ?? mark) : mark)
        case .approval(let tools):
            guard settings.notifApproval else { return }
            kindTag = "approval"
            categoryId = Self.approvalCategoryId
            body = (level == "full" && !tools.isEmpty)
                ? L("等待授权：") + tools.prefix(4).joined(separator: "、")
                : L("有敏感操作等待你的授权")
        case .approvalAutodenied:
            guard settings.notifApproval else { return }
            kindTag = "approval_autodenied"
            body = L("定时任务遇到需授权操作，已自动拒绝")
        case .scheduleStart:
            guard settings.notifScheduleStart else { return }
            kindTag = "schedule_start"
            body = L("定时任务开始运行")
        }

        let content = UNMutableNotificationContent()
        if level == "placeholder" {
            content.title = "MacAgent"
            content.body = L("有新事件，点开查看。")
        } else {
            content.title = sessionTitle.isEmpty ? "MacAgent" : "「\(sessionTitle)」"
            content.body = body
        }
        if let categoryId { content.categoryIdentifier = categoryId }
        content.sound = .default
        content.threadIdentifier = sessionId
        var ma: [String: Any] = ["kind": kindTag, "session_id": sessionId]
        if let turnId { ma["turn_id"] = turnId }
        content.userInfo = ["ma": ma]
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString,
                                  content: content, trigger: nil))
    }

    // MARK: - 审批远程决议（通知按钮 → 后端；带 expected_turn_id 防错批）

    private func decideRemote(sessionId: String, turnId: String?, approve: Bool) {
        guard let client = settings.makeClient() else { return }
        Task { @MainActor in
            do {
                _ = try await client.submitApproval(sessionId: sessionId, approve: approve,
                                                    expectedTurnId: turnId)
            } catch is TurnConflictError {
                self.postPlain(title: "MacAgent",
                               body: L("这条审批已过期或已被处理，未生效。"))
            } catch {
                // 回程网络断（隧道没开等）：任务仍在等待，明确告知而不是静默吞掉
                self.postPlain(title: "MacAgent",
                               body: L("无法连接后端，审批未送达——任务仍在等待你的决定。"))
            }
        }
    }

    private func postPlain(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString,
                                  content: content, trigger: nil))
    }

    private func openSession(_ sessionId: String) {
        store?.select(sessionId: sessionId)
        #if os(macOS)
        NSApp.activate(ignoringOtherApps: true)
        #endif
    }

    // MARK: - Live Activity（iOS：起活动 / token 上报 / 本地收尾 / push-to-start 去重）

    #if os(iOS)
    /// 轮次开始（数据源：全局 feed 监听）：起一个灵动岛/锁屏活动并上报推送 token。
    /// 后台/挂起时 request 会被系统拒绝——push-to-start（17.2+）覆盖那半边。
    func activityTurnStarted(sessionId: String, turnId: String, title: String) {
        guard settings.notifEnabled,
              ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        // 去重：push-to-start 可能已把同轮活动建出来（app 随后被唤醒），只补挂观察
        if let existing = Activity<AgentActivityAttributes>.activities
            .first(where: { $0.attributes.turnId == turnId }) {
            observeActivityTokens(existing)
            return
        }
        let attrs = AgentActivityAttributes(sessionId: sessionId, turnId: turnId,
                                            title: String(title.prefix(24)))
        let state = AgentActivityAttributes.ContentState(
            stage: L("启动中"), status: "running", done: 0, total: 0, steps: [], detail: nil)
        do {
            let act = try Activity.request(
                attributes: attrs,
                content: .init(state: state, staleDate: Date().addingTimeInterval(150)),
                pushType: .token)
            observeActivityTokens(act)
        } catch {
            // 常见于后台调用（系统只允许前台起活动）：静默即可，横幅通知仍在
        }
    }

    /// 轮次结束的本地快收尾（后端 end 推送同样会到；app 活着时本地无延迟）。
    func activityTurnEnded(turnId: String, status: String) {
        for act in Activity<AgentActivityAttributes>.activities
        where act.attributes.turnId == turnId {
            var state = act.content.state
            state.status = (status == "success") ? "ok" : status
            state.stage = (status == "success") ? L("已完成 ✓")
                : (status == "fail" ? L("执行失败 ✗") : L("已结束"))
            let content = ActivityContent(state: state, staleDate: nil)
            Task { await act.end(content, dismissalPolicy: .after(
                Date().addingTimeInterval(1800))) }
        }
    }

    /// 「已读即清」：用户进入 app（点锁屏进度卡/灵动岛、或任何方式回到前台）时，
    /// 把已完结的进度活动立即撤下——与一次性完成横幅"点击即清"的语义对齐。
    /// 完结后的 30 分钟锁屏滞留（本地 .after(1800) / 后端 dismissal-date）只服务
    /// "没看手机"的窗口；人已进 app 即视为已读。running/awaiting_approval/suspended
    /// 是活着的轮次（锁屏授权提示尤其不能误清），一律不动。
    func sweepEndedActivities() {
        for act in Activity<AgentActivityAttributes>.activities {
            let alive = ["running", "awaiting_approval", "suspended"]
                .contains(act.content.state.status)
            if !alive || act.activityState == .ended || act.activityState == .dismissed {
                Task { await act.end(nil, dismissalPolicy: .immediate) }
            }
        }
    }

    private func observeActivityTokens(_ act: Activity<AgentActivityAttributes>) {
        guard activityTokenTasks[act.id] == nil else { return }
        let sessionId = act.attributes.sessionId
        let turnId = act.attributes.turnId
        activityTokenTasks[act.id] = Task { [weak self] in
            for await tokenData in act.pushTokenUpdates {
                let hex = tokenData.map { String(format: "%02x", $0) }.joined()
                await self?.uploadActivityToken(act, sessionId: sessionId,
                                                turnId: turnId, token: hex)
            }
            await MainActor.run { [weak self] in
                self?.activityTokenTasks[act.id] = nil
            }
        }
    }

    private func uploadActivityToken(_ act: Activity<AgentActivityAttributes>,
                                     sessionId: String, turnId: String,
                                     token: String) async {
        guard let client = settings.makeClient() else { return }
        do {
            let res = try await client.pushLiveActivityToken(
                deviceId: deviceId, sessionId: sessionId, turnId: turnId, token: token)
            if res.active == false {
                // 轮次已结束（孤儿活动，如 push-to-start 迟到）：本地收尾
                await act.end(nil, dismissalPolicy: .immediate)
            }
        } catch {
            print("[Push] 活动 token 上报失败：\(error.localizedDescription)")
        }
    }

    /// push-to-start token（iOS 17.2+）：上报后，定时任务在 app 被杀时也能点亮灵动岛。
    private func observePushToStart() {
        guard #available(iOS 17.2, *) else { return }
        Task { [weak self] in
            for await tokenData in Activity<AgentActivityAttributes>.pushToStartTokenUpdates {
                let hex = tokenData.map { String(format: "%02x", $0) }.joined()
                guard let self, let client = self.settings.makeClient() else { continue }
                _ = try? await client.pushRegister(deviceId: self.deviceId,
                                                   pushToStartToken: hex)
            }
        }
    }

    /// push-to-start 创建的活动从 activityUpdates 冒出（系统后台唤醒 app）：
    /// 挂上 token 观察，把活动 token 上报给后端才开始收阶段更新。
    private func observeActivityUpdates() {
        Task { [weak self] in
            for await act in Activity<AgentActivityAttributes>.activityUpdates {
                await MainActor.run { self?.observeActivityTokens(act) }
            }
        }
    }
    #endif
}

// MARK: - UNUserNotificationCenterDelegate

extension PushManager: UNUserNotificationCenterDelegate {
    /// 前台收到通知：正看着的会话不弹横幅（内容就在眼前），其他会话照常提示。
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler:
            @escaping (UNNotificationPresentationOptions) -> Void) {
        let ma = notification.request.content.userInfo["ma"] as? [String: Any]
        let sid = ma?["session_id"] as? String
        Task { @MainActor in
            if let sid, self.store?.isAppActive == true,
               self.store?.selectedSessionId == sid {
                completionHandler([])
            } else {
                completionHandler([.banner, .list, .sound])
            }
        }
    }

    /// 通知交互：允许/拒绝按钮 → 远程决议；点通知本体 → 深链进会话。
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void) {
        let userInfo = response.notification.request.content.userInfo
        let ma = userInfo["ma"] as? [String: Any]
        let sid = ma?["session_id"] as? String
        let turnId = ma?["turn_id"] as? String
        let action = response.actionIdentifier
        Task { @MainActor in
            defer { completionHandler() }
            guard let sid else { return }
            switch action {
            case Self.approveActionId:
                self.decideRemote(sessionId: sid, turnId: turnId, approve: true)
            case Self.denyActionId:
                self.decideRemote(sessionId: sid, turnId: turnId, approve: false)
            case UNNotificationDefaultActionIdentifier:
                self.openSession(sid)
            default:
                break
            }
        }
    }
}
