import Foundation
import UserNotifications

/// A request token can be checked by disk actors without hopping back to MainActor.
/// The lock protects only the token; file IO never holds it.
final class OfflineSessionIndexFence: @unchecked Sendable {
    private let lock = NSLock()
    private var current = UUID()
    @discardableResult func advance() -> UUID {
        lock.lock(); defer { lock.unlock() }
        current = UUID()
        return current
    }
    func matches(_ request: UUID) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return current == request
    }
}

/// 审批卡片捕获的账本身份；seq 区分同一轮中后续的一批审批。
struct PendingApprovalTarget: Equatable {
    let sessionId: String
    let turnId: String
    let seq: Int
}

struct RetryConfirmationTarget: Equatable {
    let id: UUID
    let sessionId: String
    let sourceRef: String
    let canonicalRef: String
    let canonicalIndex: Int
    let sourcePrompt: String
    let isResend: Bool
    let clientTurnId: String
    let selectionEpoch: UUID
    let clientIdentity: ObjectIdentifier
}

private struct EditableAttemptTarget: Equatable {
    let prompt: String
    let pausedTurnId: String?
    let canonicalCount: Int
    let canonicalRef: String?
}

/// 应用主状态（@MainActor）：会话列表 + 当前会话消息 + 活跃轮直播态 + 任务控制。
/// 账本协议（PROTOCOL.md）：发送/审批/恢复一律 202 受理即返,过程与终态从
/// GET /events 附着流回来——store 是账本事件的归约器投影(TurnReducer)＋UI 副作用。
/// 断开是常态:静默指数退避重连,重放游标补齐断档;刷新/杀进程后经
/// /history + /turn + 事件重放完整恢复(含审批卡与直播预览)。
@MainActor
final class AgentStore: ObservableObject {
    // 连接
    @Published var connectionOK: Bool?
    @Published var connState: ConnState = .connecting
    @Published var computeState: ComputeState = .unknown
    /// Latest authenticated /health data-plane failure (Contract H `rag_state`:
    /// "unreachable" / "unauthorized", else nil); only qualifies the 「后端启动中」 label.
    @Published private(set) var ragNotice: String?
    // 会话
    @Published var sessions: [SessionMetaItem] = []
    @Published var retentionPolicy: SessionRetentionPolicy?
    /// 剧本清单（斜杠候选提示）：随会话列表一并加载，含缺依赖者（候选里灰显）
    @Published var skills: [SkillItem] = []
    /// 持久化选中会话：杀进程/重启后自动回到原直播视图（铁律 4 的 ≤1s 恢复语义，
    /// web 靠刷新同 URL 天然满足，native 必须显式记住）。draft（未落盘）id 也会
    /// 存进去，恢复时经 restoreLocalHistory / loadSessions 的存在性校验过滤。
    @Published var selectedSessionId: String? {
        didSet {
            // Expanded execution groups belong to the open session only.
            if oldValue != selectedSessionId, !expandedToolGroups.isEmpty { expandedToolGroups.removeAll() }
            guard persistSelectedSession, let namespace = client?.cacheNamespace else { return }
            let selected = selectedSessionId, cache = offlineHistory
            Task { try? await cache.saveSelection(selected, namespace: namespace) }
        }
    }
    /// Local history never grants turn/control authority. Cleared only by online recovery.
    @Published private(set) var historyIsReadOnly = false
    @Published private(set) var offlineSavedSessionIDs: Set<String> = []
    @Published private(set) var offlineSyncInProgress = false
    @Published private(set) var offlineAttachmentsComplete = false
    /// Attachment mirroring is waiting for an unmetered path, not failing.
    @Published private(set) var offlineMirroringDeferred = false
    var offlineHistoryNotice: String? {
        if historyIsReadOnly {
            return L(!sessions.isEmpty && offlineSavedSessionIDs.count < sessions.count
                ? "离线阅读 · 部分记录尚未同步" : "离线阅读 · 本地已保存")
        }
        if offlineSyncInProgress { return L("正在保存历史与附件…") }
        if !sessions.isEmpty && offlineSavedSessionIDs.count < sessions.count { return L("历史同步未完成") }
        if !sessions.isEmpty && offlineMirroringDeferred { return L("正文已保存 · 附件等待 Wi-Fi") }
        if !sessions.isEmpty && !offlineAttachmentsComplete { return L("正文已保存 · 附件同步未完成") }
        return sessions.isEmpty ? nil : L("已保存到本地")
    }
    private var persistSelectedSession = true
    private let offlineHistory: OfflineHistoryStore
    private var localRestoreTask: Task<Void, Never>?
    private var historySyncTask: Task<Void, Never>?
    private var historySyncRequested = false
    private var currentDisplayIndices: [Int]?
    private var currentHistoryGeneration: String?
    private var currentCacheEpoch = 0
    private var currentCanonicalMetadata: SessionMetaItem?
    private var canonicalProjectionRevision = UUID()
    private func rejectReadOnlyHistory() -> Bool {
        guard historyIsReadOnly else { return false }
        errorMessage = L("当前为离线阅读；连接并同步后可继续操作。")
        return true
    }
    @Published var messages: [ChatMessage] = [] {
        didSet { displayMessages = ChatMessage.groupedForDisplay(messages) }
    }
    /// 显示投影：连续工具行合成「执行过程」组（ChatView 渲染它；逻辑一律读 messages）。
    @Published private(set) var displayMessages: [ChatMessage] = []
    /// 用户展开过的执行过程组，按稳定键（turnIndex + 组首行在该轮工具行里的序号，见
    /// ChatMessage.toolGroupKey）记：同一会话内历史重投影后仍保持展开；换会话即清空。
    @Published var expandedToolGroups: Set<String> = []
    func isToolGroupExpanded(_ key: String) -> Bool { expandedToolGroups.contains(key) }
    func setToolGroup(_ key: String, expanded: Bool) {
        if expanded { expandedToolGroups.insert(key) } else { expandedToolGroups.remove(key) }
    }
    @Published private(set) var canonicalTurns: [Turn] = []
    @Published private(set) var retryGroups: [RetryGroup] = []
    @Published private(set) var lastTurnRef: String?
    @Published var retryConfirmationTarget: RetryConfirmationTarget?
    @Published private(set) var retryStatusNotice: String?
    @Published private(set) var historyProjectionNeedsRefresh = false
    private var retryConfirmationId: UUID?
    var lastCanonicalIndex: Int? { canonicalTurns.isEmpty ? nil : canonicalTurns.count - 1 }
    var retryActionTitle: String {
        guard let sid = selectedSessionId else { return L("重试最近一次尝试") }
        let pending = outboxAll()[sid]
        return L(pending?["operation"] == "retry" && pending?["retry_uncertain"] != "0"
                 ? "重发未确认的重试" : "重试最近一次尝试")
    }

    private var editableAttemptTarget: EditableAttemptTarget? {
        guard !historyIsReadOnly, !isGenerating, controlRequest == nil, !selectedSessionIsDeleting else { return nil }
        if ledgerPhase == "awaiting_approval" || ledgerPhase == "suspended" {
            guard let id = liveTurnId, let prompt = liveRawPrompt else { return nil }
            return EditableAttemptTarget(prompt: prompt, pausedTurnId: id,
                canonicalCount: canonicalTurns.count, canonicalRef: lastTurnRef)
        }
        guard ledgerPhase == "none", !historyProjectionNeedsRefresh,
              let prompt = canonicalTurns.last?.prompt else { return nil }
        return EditableAttemptTarget(prompt: prompt, pausedTurnId: nil,
            canonicalCount: canonicalTurns.count, canonicalRef: lastTurnRef)
    }

    var canEditLastAttempt: Bool { editableAttemptTarget != nil }

    func canEditMessage(_ message: ChatMessage, auditOnly: Bool) -> Bool {
        guard !auditOnly, let source = editableAttemptTarget else { return false }
        if source.pausedTurnId != nil { return message.id == liveUserMessageId }
        return message.canModifyLatestAttempt(canonicalIndex: lastCanonicalIndex, auditOnly: false) &&
            message.id == messages.last(where: { $0.kind == .user })?.id
    }

    private func ownsEditReply(_ source: EditableAttemptTarget) -> Bool {
        guard !isGenerating, controlRequest == nil, canonicalTurns.count == source.canonicalCount,
              lastTurnRef == source.canonicalRef else { return false }
        if let id = source.pausedTurnId {
            return liveTurnId == id || (liveTurnId == nil && lastEndedId == id && ledgerPhase == "none")
        }
        return ledgerPhase == "none"
    }

    // 活跃轮直播态（预览;turn_ended 权威对账后清空）
    @Published var isGenerating = false
    @Published var streamText = ""
    @Published var thinkingText = ""
    @Published var liveSteps: [String] = []
    /// 直播步骤的结构化投影（与 liveSteps 同源 live.preview.steps；ChatView 画紧凑行）。
    @Published private(set) var liveStepItems: [NormalizedStep] = []
    /// 本轮最近一次审批的结果，取自账本 `resumed` 事件的 trigger（approve / deny）；
    /// 预览级状态：新一轮受理、轮次终态、换会话时清空。
    enum ApprovalOutcome: Equatable { case approved, denied }
    @Published private(set) var approvalOutcome: ApprovalOutcome?
    @Published var activity: LiveActivity?
    @Published var taskSnapshot: TaskSnapshot?
    @Published var pendingTools: [PendingTool]?
    @Published private(set) var pendingApprovalTarget: PendingApprovalTarget?
    @Published var isSuspended = false
    @Published private(set) var ledgerPhase = "none"
    enum ControlKind { case cancel, suspend, resume }
    struct ControlRequest {
        let id: UUID
        let kind: ControlKind
        let turnId: String
        /// Fixed at dispatch, never advanced by later recovery snapshots or replay.
        let observedSeq: Int
        var accepted: Bool
    }
    @Published private(set) var controlRequest: ControlRequest?
    var suspendPending: Bool { controlRequest?.kind == .suspend }
    static func hasControllableChat(kind: String?) -> Bool { kind != "summarize" && kind != "prewarm" }
    static func canCancelTask(phase: String) -> Bool { ["queued", "running"].contains(phase) }
    var canSuspend: Bool { !historyIsReadOnly && ledgerPhase == "running" && liveTurnId != nil && controlRequest == nil && !selectedSessionIsDeleting }
    var controlFeedback: String? {
        guard let controlRequest else { return nil }
        if !controlRequest.accepted { return L("正在发送控制请求…") }
        switch controlRequest.kind {
        case .suspend: return L("挂起已受理，等待工具结束后的安全点。")
        case .cancel: return L("停止已受理，等待安全点确认；已完成的副作用不会自动撤销。")
        case .resume: return L("恢复已受理，等待账本确认继续执行。")
        }
    }
    // 普通错误自动消失；未完成的历史同步保留提示直到明确恢复。
    @Published var errorMessage: String? {
        didSet {
            errorDismissTask?.cancel()
            guard errorMessage != nil, errorMessage != incompleteHistoryError else { return }
            errorDismissTask = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 6_000_000_000)
                if !Task.isCancelled { self?.errorMessage = nil }
            }
        }
    }
    // 当前会话的 Deck（有则聊天工具栏出现查看入口）
    @Published var deck: DeckSpec?
    // 「编辑并重发」把上一条用户输入回填进输入框的通道
    @Published var composerPrefill: String?
    /// 消息里点了产物链接 → 打开检查器「文件」页签并高亮该文件的通道
    /// （ChatView 据此开 inspector，InspectorView 据此切页签+高亮，用完置回 nil）
    @Published var revealedAssetName: String?
    /// 检查器开合（上移到 store：窗口 minWidth 要按它动态取值——三栏全开的实测
    /// 最低需求 ~934pt，恒定大 min 又会惩罚不开检查器的窄窗用户）
    @Published var showInspector = false
    // 任务中心：全局调度快照（跨会话，监听循环持续刷新）
    @Published var globalTasks: [TaskHandleInfo] = []
    enum TaskSnapshotFreshness { case unknown, fresh, stale }
    @Published private(set) var globalTasksFreshness: TaskSnapshotFreshness = .unknown
    private var globalSnapshotRequest = UUID()

    @Published private(set) var schedules: [ScheduledTask] = []
    @Published private(set) var schedulesFreshness: TaskSnapshotFreshness = .unknown
    @Published private(set) var schedulesError: String?
    @Published private(set) var schedulesLoading = false
    @Published private(set) var scheduleRunHistory: [String: [ScheduleRun]] = [:]
    @Published private(set) var scheduleRunsFreshness: [String: TaskSnapshotFreshness] = [:]
    @Published private(set) var scheduleRunsError: [String: String] = [:]
    private var schedulesRequest = UUID()
    private var scheduleRunsRequests: [String: UUID] = [:]
    @Published private(set) var deletingSessionIDs: Set<String> = []
    private var sessionDeleteRequests: [String: UUID] = [:]
    private var submissionIntentEpoch = UUID()
    private struct SubmissionHTTP { let sessionId: String; let client: ObjectIdentifier }
    @Published private var submissionHTTPRequests: [String: SubmissionHTTP] = [:]
    func hasSubmissionInFlight(_ sid: String) -> Bool {
        guard let client else { return false }
        return submissionHTTPRequests.values.contains { $0.sessionId == sid && $0.client == ObjectIdentifier(client) }
    }

    private func shouldBlockSubmission(_ sid: String, client: AgentClient, receipt: String,
                                       epoch: UUID) -> Bool {
        guard self.client === client, isSessionDeleting(sid) else { return false }
        if outboxAll()[sid]?["receipt"] == receipt { outboxClear(sessionId: sid) }
        if isCurrent(sid, epoch, client), optimisticReceipt == receipt {
            throwSubmissionFailure(receipt: receipt)
            errorMessage = L("会话正在删除，请等待删除结果后再操作。")
        }
        return true
    }

    func isSessionDeleting(_ sid: String) -> Bool { deletingSessionIDs.contains(sid) }
    var selectedSessionIsDeleting: Bool { selectedSessionId.map(isSessionDeleting) ?? false }
    private func rejectDeletingSession(_ sid: String) -> Bool {
        guard isSessionDeleting(sid) else { return false }
        errorMessage = L("会话正在删除，请等待删除结果后再操作。")
        return true
    }
    // 内容搜索
    @Published var searchQuery = ""
    @Published var searchResults: [SearchHit]?
    @Published var isSearching = false
    /// App 是否前台活跃（RootView 从 scenePhase 同步）：后台完结的轮次发本地通知
    var isAppActive = true
    private var errorDismissTask: Task<Void, Never>?

    let settings: AppSettings
    private(set) var client: AgentClient?
    /// 通知枢纽（MacAgentApp 注入）：macOS 本地通知出口 + iOS Live Activity 起停。
    weak var pushManager: PushManager?
    /// feed 是瘦身流（turn_started 不带 origin/prompt）：从 turn_queued 记下轮次元数据。
    private var feedTurnMeta: [String: (origin: String, prompt: String)] = [:]
    /// 当前会话已落盘的轮次数（新完结轮的分支下标 = 该值，随后自增）。
    private var knownTurnCount = 0
    /// 后台全局任务监听（macOS：进程后台常驻可轮询；iOS 挂起后自然停摆）。
    private var monitorTask: Task<Void, Never>?
    private var connMonitorTask: Task<Void, Never>?
    private var connFailures = 0
    private var connectionRequest = 0
    private let sessionIndexFence = OfflineSessionIndexFence()
    private func invalidateSessionIndexRequests() { sessionIndexFence.advance() }
    private func ownsSessionIndex(_ client: AgentClient, request: UUID) -> Bool {
        self.client === client && sessionIndexFence.matches(request) && !Task.isCancelled
    }
    /// 通知去重：同一会话 30s 内只报一次（自发轮次与全局监听两条路径都会命中完结）。
    private var notifiedAt: [String: Date] = [:]

    // ---- 附着流簿记（单附着策略：只对选中会话保持一条 /events 长连） ----
    private var attachTask: Task<Void, Never>?
    private var attachmentId: UUID?
    private var selectionEpoch = UUID()
    private var replayAfter: Int?
    private var recoveryTask: Task<Bool, Never>?
    private var recoveryId: UUID?
    // Distinguish no requirement from a thin terminal with an unknown/invalid index.
    private struct MissingThinRecord { let index: Int? }
    private var missingThinRecord: MissingThinRecord?
    private var incompleteHistoryError: String?
    private func clearIncompleteHistoryError() {
        if let recorded = incompleteHistoryError, errorMessage == recorded { errorMessage = nil }
        incompleteHistoryError = nil
    }

    private func canonicalIndex(_ value: JSONValue?) -> Int? {
        guard case .number(let n)? = value, n.isFinite,
              n >= 0, n < Double(Int.max), n.rounded(.towardZero) == n else { return nil }
        return Int(n)
    }

    private func isCurrent(_ sid: String, _ epoch: UUID, _ client: AgentClient) -> Bool {
        selectedSessionId == sid && selectionEpoch == epoch && self.client === client
    }
    /// 已消费的最大账本 seq（重连游标；换会话时归零重定位）。
    private var cursor = 0
    /// 归约器状态（活跃轮的规范形投影;fixtures 锁定其行为）。
    private var live = NormalizedState()
    /// 本端乐观追加的用户气泡（turn_queued 到达时收编,不重复建）。
    private var optimisticOperation: String?
    private var optimisticPrompt: String?
    private var optimisticClientTurnId: String?
    private var optimisticReceipt: String?
    private var optimisticUserMessageId: UUID?
    private var optimisticTurnId: String?
    private var optimisticFirstSeq: Int?
    private var liveRawPrompt: String?
    private var liveTurnId: String?
    private var liveOutboxReceipt: String?
    private var lastEndedId: String?
    private var liveUserMessageId: UUID?
    /// 发送暂存（outbox）：提交前落 UserDefaults,202 受理即清——App 在 multipart
    /// 上传途中被杀时,这是唯一能证明"有话没送达"的凭据。
    private let outboxKey = "macagent.outbox"
    init(settings: AppSettings, offlineCacheRoot: URL? = nil) {
        self.settings = settings
        let madeClient = settings.makeClient()
        self.offlineHistory = OfflineHistoryStore(root: offlineCacheRoot ?? madeClient?.offlineCacheRoot)
        self.client = madeClient
        localRestoreTask = Task { [weak self] in await self?.restoreLocalHistory() }
        // 连接后置（首启引导）：引导未完成前不发起任何探活/附着——新装 app 在填完
        // 地址/密钥前空转尝试没有意义。引导完成时 OnboardingView 调 applySettings()
        // 重启这两个监听（applySettings 无条件启动，那时 setupCompleted 已为 true）。
        if settings.setupCompleted {
            startGlobalMonitor()
            startConnectionMonitor()
        }
    }

    /// 设置变更后重建客户端并重新对账（选中会话重新附着）。
    func applySettings() {
        let previousSelection = selectedSessionId
        invalidateSessionIndexRequests()
        detach()
        client = settings.makeClient()
        clearHistoryProjection()
        globalTasks = []
        globalTasksFreshness = .unknown
        globalSnapshotRequest = UUID()
        schedulesRequest = UUID(); scheduleRunsRequests.removeAll()
        schedules = []; schedulesFreshness = .unknown; schedulesError = nil; schedulesLoading = false
        scheduleRunHistory = [:]; scheduleRunsFreshness = [:]; scheduleRunsError = [:]
        sessionDeleteRequests.removeAll(); deletingSessionIDs.removeAll(); submissionHTTPRequests.removeAll()
        startGlobalMonitor()       // client 换了，监听循环重启
        startConnectionMonitor()   // 连接探活同理重启（失败计数清零 → 黄灯重试）
        historySyncTask?.cancel(); historySyncTask = nil
        localRestoreTask?.cancel()
        sessions = []; retentionPolicy = nil; offlineSavedSessionIDs = []
        offlineSyncInProgress = false; offlineAttachmentsComplete = false
        offlineMirroringDeferred = false
        persistSelectedSession = false
        selectedSessionId = nil; messages = []; historyIsReadOnly = false
        persistSelectedSession = true
        let appliedClient = client
        localRestoreTask = Task { [weak self] in await self?.restoreLocalHistory() }
        Task {
            await localRestoreTask?.value
            guard client === appliedClient else { return }
            // The previous selection belongs to the previous connection. Re-select it
            // only if this backend's own local index knows it; otherwise leave the
            // choice to loadSessions, which sees the authoritative list.
            if selectedSessionId == nil, let previousSelection,
               sessions.contains(where: { $0.sessionId == previousSelection }) {
                select(sessionId: previousSelection)
            }
            await refreshConnection()
            guard client === appliedClient else { return }
            await loadSessions()
        }
    }

    func refreshConnection() async {
        guard let client else {
            if connectionOK != false { connectionOK = false }
            if connState != .offline { connState = .offline }
            if computeState != .unknown { computeState = .unknown }
            return
        }
        connectionRequest += 1
        let request = connectionRequest
        let status = await client.connectionStatus()
        guard !Task.isCancelled, self.client === client, request == connectionRequest else { return }
        recordConnection(status)
    }

    /// One place applies a probe result: access, engine phase and compute label.
    /// Published values are assigned only when they change — the monitor ticks every
    /// 2 s during startup and every view observing the store would re-render otherwise.
    private func recordConnection(_ status: BackendConnectionStatus) {
        recordBackendAccess(status.access, engineStarting: status.engineStarting)
        let notice = status.engineStarting ? status.ragNotice : nil
        if ragNotice != notice { ragNotice = notice }
        let compute = status.compute
        if computeState != compute { computeState = compute }
    }

    // MARK: - 连接状态监控（实机反馈：信号原来只在设置页关闭时刷一次，静态且滞后）

    /// Backend access only; an authenticated API does not prove every compute capability.
    /// `engineStarting`: authenticated, but /health reports `engine_ready == false`.
    enum ConnState { case connecting, connected, engineStarting, offline, unauthorized, reachable }

    /// 「后端启动中」, qualified while the data plane has not answered yet.
    var engineStartingLabel: String {
        switch ragNotice {
        case "unreachable": return L("后端启动中") + L("（等待检索服务）")
        case "unauthorized": return L("后端启动中") + L("（检索服务密钥不匹配）")
        default: return L("后端启动中")
        }
    }

    typealias ComputeState = ComputeAvailability
    var computeStatusLabel: String {
        switch computeState {
        case .ready: return L("计算端就绪")
        case .starting: return L("计算端启动中")
        case .unavailable: return L("计算端未就绪")
        case .unknown: return L("计算端待确认")
        case .unsupported: return L("计算端状态未知")
        case .probing: return L("计算端连接中")
        case .unreachable: return L("计算端不可达")
        case .unauthorized: return L("计算端密钥不匹配")
        }
    }
    var computeStatusDetail: String {
        switch computeState {
        case .ready: return L("后端已确认推理计算端就绪；知识库能力在知识库面板单独检查。")
        case .starting: return L("计算端正在启动，本地历史仍可阅读。")
        case .unavailable: return L("计算端尚未就绪，本地历史仍可阅读。")
        case .unknown: return L("尚未取得经过认证的计算端状态。")
        case .unsupported: return L("当前后端未提供计算端状态。")
        case .probing: return L("后端正在探测推理计算端，本地历史仍可阅读。")
        case .unreachable: return L("后端连不上推理计算端：请检查算力端地址与网络。本地历史仍可阅读。")
        case .unauthorized: return L("推理计算端拒绝了后端的密钥：请核对两端的 INFER 密钥。")
        }
    }

    /// Only a refused or absent backend turns the displayed history into a reading
    /// projection. `.reachable` means the health endpoint answered but the index
    /// probe did not settle in time — a transient failure that must not take Stop /
    /// Suspend / Approve away from a live turn.
    private func recordBackendAccess(_ access: BackendAccess, engineStarting: Bool = false) {
        let ok = access == .authenticated
        if connectionOK != ok { connectionOK = ok }
        func setConn(_ state: ConnState) { if connState != state { connState = state } }
        switch access {
        case .authenticated:
            connFailures = 0; setConn(engineStarting ? .engineStarting : .connected)
            // This backend just authenticated, so a session it recovered and is
            // still attached to keeps its control authority. A cache-only
            // projection (no attachment) stays read only.
            if historyIsReadOnly, selectedSessionIsAttached { historyIsReadOnly = false }
        case .unauthorized:
            connFailures = 0; setConn(.unauthorized)
            markHistoryReadOnly()
        case .reachable: connFailures = 0; setConn(.reachable)
        case .offline:
            connFailures += 1
            setConn(connFailures >= 10 ? .offline : .connecting)
            // An attached live stream is its own authority, and health is probed
            // every 2s while unreachable: a single missed probe must not take
            // Approve / Stop away mid-turn. Honour the same debounce the signal
            // uses — only the settled offline verdict freezes an attached session.
            if connState == .offline || !selectedSessionIsAttached { markHistoryReadOnly() }
        }
    }

    /// The selected session was recovered online and still owns a live attachment.
    private var selectedSessionIsAttached: Bool {
        guard let sid = selectedSessionId else { return false }
        return attachedSessionId == sid && attachTask != nil
    }

    /// The local copy never carries execution or approval authority.
    private func markHistoryReadOnly() {
        if !canonicalTurns.isEmpty, optimisticReceipt == nil { historyIsReadOnly = true }
    }

    /// Existing monitor verifies reachability and backend authentication every 15s;
    /// unreachable services retry every 2s to follow sidecar startup, and so does an
    /// authenticated backend that is still starting (engine not installed, or the first
    /// compute-node probe pending — `BackendConnectionStatus.pollIntervalSeconds`). The session index is refreshed on
    /// the slow cadence only, so the fast phase does not double the index traffic.
    /// 连续 10 次失败（约 20s）才降级红灯——短暂波动/启动窗口都算「连接中」。
    func startConnectionMonitor() {
        connMonitorTask?.cancel()
        connectionRequest += 1
        connFailures = 0
        connState = .connecting
        computeState = .unknown
        ragNotice = nil
        connectionOK = nil
        connMonitorTask = Task { [weak self] in
            var lastSessionsLoad: Date?
            var wasStarting = false
            while !Task.isCancelled {
                guard let self else { return }
                guard let client = self.client else { return }
                self.connectionRequest += 1
                let request = self.connectionRequest
                let status = await client.connectionStatus()
                let access = status.access
                if Task.isCancelled || self.client !== client { return }
                if request != self.connectionRequest {
                    try? await Task.sleep(nanoseconds: 2_000_000_000)
                    continue
                }
                self.recordConnection(status)
                if access == .authenticated {
                    // 顺带巡刷会话列表（macOS 无下拉刷新后的自动更新通道）：
                    // 等值守卫兜底，列表没变不会引起任何视图重建。快轮询（启动中）期间
                    // 只按慢节奏刷，刚就绪的那一轮立即补一次。
                    let becameReady = !status.stillStarting && wasStarting
                    if becameReady || lastSessionsLoad.map({ Date().timeIntervalSince($0) >= 14 }) ?? true {
                        lastSessionsLoad = Date()
                        await self.loadSessions()
                    }
                }
                wasStarting = status.stillStarting
                try? await Task.sleep(nanoseconds: status.pollIntervalSeconds * 1_000_000_000)
            }
        }
    }

    // MARK: - 会话管理

    /// Cached reading should survive ordinary transport loss; authentication,
    /// malformed responses, TLS failures and other unknown errors stay visible.
    /// One classifier, shared with the client's own cache fallbacks.
    private static func isOfflineTransportFailure(_ error: Error) -> Bool {
        OfflineTransportFailure.matches(error)
    }

    func loadSessions() async {
        guard let client else { return }
        let request = sessionIndexFence.advance(), fence = sessionIndexFence
        do {
            let index = try await client.sessionIndex()
            guard ownsSessionIndex(client, request: request), index.status else { return }
            let previousIDs = Set(sessions.map(\.sessionId))
            retentionPolicy = index.retentionPolicy
            var items = index.sessions
            items.sort {
                if ($0.pinned ?? false) != ($1.pinned ?? false) { return $0.pinned ?? false }
                return ($0.lastUpdated ?? 0) > ($1.lastUpdated ?? 0)
            }
            // 等值守卫（实机反馈）：下拉刷新常常拿回一模一样的列表，无脑重赋值会让
            // List 整体重建——刷新控件收起动画与内容重建打架，表现为瞬移回顶+双重残影
            if items != sessions {
                sessions = items
            }
            // An index that exists but cannot be read (iOS keeps it closed while the
            // device is locked) only skips this round's index write: no banner, and the
            // retention, selection and history steps below still run.
            var saved = true
            do {
                saved = try await offlineHistory.saveIndex(items, retentionPolicy: index.retentionPolicy,
                    namespace: client.cacheNamespace, isCurrent: { fence.matches(request) })
            } catch is OfflineHistoryStore.IndexUnreadable {}
            guard ownsSessionIndex(client, request: request), saved else { return }
            let savedIndex = await offlineHistory.readableIndex(namespace: client.cacheNamespace)
            guard ownsSessionIndex(client, request: request) else { return }
            if let savedIndex { offlineSavedSessionIDs = savedSessionIDs(matching: savedIndex) }
            if let selected = selectedSessionId, previousIDs.contains(selected),
               !items.contains(where: { $0.sessionId == selected }), !isGenerating,
               !hasSubmissionInFlight(selected) {
                detach(); selectedSessionId = nil; clearHistoryProjection(); messages = []; knownTurnCount = 0
            }
            let reclaimed = await client.offlineAssets.retainSessions(Set(items.map(\.sessionId)), namespace: client.cacheNamespace,
                isCurrent: { fence.matches(request) })
            guard ownsSessionIndex(client, request: request) else { return }
            // Removed sessions freed space: mirrors the budget had cut short get
            // another attempt on the next pass.
            if reclaimed {
                try? await offlineHistory.retryPartialAssetSessions(namespace: client.cacheNamespace)
                guard ownsSessionIndex(client, request: request) else { return }
            }
            // Upgrade path: authenticate/list first, then restore only an ID which this
            // connection actually returned. Legacy titles/bodies are never imported.
            if selectedSessionId == nil, !items.isEmpty {
                let local = await offlineHistory.loadIndex(namespace: client.cacheNamespace)
                guard ownsSessionIndex(client, request: request), selectedSessionId == nil else { return }
                struct LegacyConnection: Decodable { let backendURL: String }
                let legacyData = UserDefaults.standard.data(forKey: "macagent.sessions.snapshot")
                let legacy = legacyData.flatMap { try? JSONDecoder().decode(LegacyConnection.self, from: $0) }
                let legacyID = legacy?.backendURL == settings.baseURLString
                    ? UserDefaults.standard.string(forKey: "session.lastSelected") : nil
                let candidate = [local.selectedSessionId, legacyID].compactMap { $0 }
                    .first(where: { id in items.contains { $0.sessionId == id } })
                if let candidate { select(sessionId: candidate) }
            }
            startHistorySync(client: client)
            if historyIsReadOnly, recoveryTask == nil, attachTask == nil, let selected = selectedSessionId {
                Task { await open(sessionId: selected) }
            }
        } catch {
            guard ownsSessionIndex(client, request: request) else { return }
            if sessions.isEmpty || !Self.isOfflineTransportFailure(error) {
                errorMessage = L("会话列表加载失败：") + error.localizedDescription
            }
        }
        // 剧本清单搭车刷新：变动频率极低（装依赖/加剧本），无须独立轮询；
        // 拉不到就保持旧值——候选提示是锦上添花，不值得为它弹错误横幅
        if let fetched = try? await client.skills(), ownsSessionIndex(client, request: request) {
            skills = fetched
        }
    }

    private func savedSessionIDs(matching index: OfflineHistoryStore.Index) -> Set<String> {
        Set(sessions.compactMap { item in
            index.invalidated?.contains(item.sessionId) != true
                && index.versions[item.sessionId] == OfflineHistoryStore.Version(item) ? item.sessionId : nil
        })
    }

    private func refreshSavedSessionIDs(client: AgentClient) async {
        let index = await offlineHistory.loadIndex(namespace: client.cacheNamespace)
        guard self.client === client, !Task.isCancelled else { return }
        offlineSavedSessionIDs = savedSessionIDs(matching: index)
    }

    /// Startup reads local files before initiating any network wait.
    func restoreLocalHistory() async {
        guard let client else { return }
        let index = await offlineHistory.loadIndex(namespace: client.cacheNamespace)
        guard self.client === client, !Task.isCancelled else { return }
        if sessions.isEmpty { sessions = index.sessions; retentionPolicy = index.retentionPolicy }
        offlineSavedSessionIDs = savedSessionIDs(matching: index)
        if selectedSessionId == nil, let selected = index.selectedSessionId,
           sessions.contains(where: { $0.sessionId == selected }) {
            select(sessionId: selected)
        }
    }

    /// Two histories at a time; each result is persisted and released before another is fetched.
    /// A pass first decides what it owes: an index refresh that changed nothing must
    /// not re-list attachments, nor flap the sync notice and the Files tab every 15s.
    private func startHistorySync(client: AgentClient) {
        historySyncRequested = true
        guard historySyncTask == nil else { return }
        historySyncTask = Task { [weak self] in
            guard let self else { return }
            defer {
                if self.client === client {
                    self.historySyncTask = nil
                    self.setOfflineSyncInProgress(false)
                }
            }
            repeat {
                self.historySyncRequested = false
                let targets = self.sessions
                let cache = self.offlineHistory
                // Asking the path policy once per pass keeps a metered device quiet:
                // the deferred work never enters the pass, so nothing is requested,
                // no progress flag flips, and the Files tab is not reloaded.
                let mayMirror = NetworkPathPolicy.shared.allowsAutomaticMirroring
                var work: [(item: SessionMetaItem, history: Bool, assets: Bool)] = []
                var deferred = false
                // An unreadable index (device locked) makes every session look stale; a
                // pass now would refetch every history only to fail saving it.
                guard await cache.readableIndex(namespace: client.cacheNamespace) != nil else { continue }
                guard self.client === client, !Task.isCancelled else { return }
                for item in targets {
                    let history = await cache.needsHistory(item, namespace: client.cacheNamespace)
                    var assets = await cache.needsAssetSync(item, namespace: client.cacheNamespace)
                    guard self.client === client, !Task.isCancelled else { return }
                    if assets, !mayMirror { assets = false; deferred = true }
                    if history || assets { work.append((item, history, assets)) }
                }
                let partial = await cache.partialAssetSessions(namespace: client.cacheNamespace)
                guard self.client === client, !Task.isCancelled else { return }
                self.setMirroringDeferred(deferred)
                // Per pass, not per connection: a pass that mirrors everything it
                // owes reports complete even after an earlier pass failed. Mirrors
                // the budget cut short stay visible until they are completed.
                self.setAttachmentsComplete(partial.isEmpty && !deferred)
                if work.isEmpty { continue }
                self.setOfflineSyncInProgress(true)
                // Text first, two at a time; each result is persisted and released
                // before another is fetched.
                var historyFailed = Set<String>()
                await withTaskGroup(of: String?.self) { group in
                    var pending = work.filter(\.history).makeIterator()
                    func enqueue() {
                        guard !Task.isCancelled, let target = pending.next() else { return }
                        group.addTask {
                            guard !Task.isCancelled else { return nil }
                            let item = target.item
                            let cacheEpoch = await cache.epoch(sessionId: item.sessionId, namespace: client.cacheNamespace)
                            guard let history = try? await client.history(sessionId: item.sessionId),
                                  history.status, history.sessionId == item.sessionId else { return item.sessionId }
                            await self.cacheBackgroundHistory(history, metadata: item, client: client, expectedEpoch: cacheEpoch)
                            return nil
                        }
                    }
                    enqueue(); enqueue()
                    for await failed in group { if let failed { historyFailed.insert(failed) }; enqueue() }
                }
                guard self.client === client, !Task.isCancelled else { return }
                // Attachments strictly one session at a time: two mirrors would admit
                // against the same free space and could each report "complete" after
                // the other evicted their bytes.
                for target in work where target.assets {
                    guard !Task.isCancelled else { continue }
                    // Text did not land this pass: its attachments are not mirrored either,
                    // so the notice must not read "saved locally".
                    if historyFailed.contains(target.item.sessionId) {
                        self.markAttachmentsIncomplete(client: client)
                        continue
                    }
                    let item = target.item
                    let token = await cache.beginAssetSync(sessionId: item.sessionId, namespace: client.cacheNamespace)
                    guard self.client === client, !Task.isCancelled else { return }
                    let namespace = client.cacheNamespace
                    let result = await client.syncOfflineAssets(sessionId: item.sessionId,
                        markEvicted: { ids in try await cache.markAssetsEvicted(ids, namespace: namespace) })
                    guard self.client === client, !Task.isCancelled else { return }
                    switch result.state {
                    case .complete, .partial:
                        // The version advances either way so the 15s refresh stops
                        // asking; `partial` — or an eviction raised since `token` —
                        // keeps the session marked as not fully saved on this device.
                        do {
                            try await cache.recordAssetSync(item, namespace: client.cacheNamespace,
                                                            complete: result.state == .complete, token: token)
                        } catch {
                            // Not recorded: the version does not advance, so the next pass
                            // retries; this pass is not reported as saved.
                            self.markAttachmentsIncomplete(client: client)
                        }
                    case .deferred, .failed:
                        break
                    }
                    // Whoever lost bytes to this session was marked before the bytes went
                    // (markEvicted above); this pass's view of the cache is not complete.
                    if !result.evicted.isEmpty {
                        self.markAttachmentsIncomplete(client: client)
                    }
                    if result.state == .deferred { self.markMirroringDeferred(client: client) }
                    if result.state != .complete { self.markAttachmentsIncomplete(client: client) }
                    guard self.client === client, !Task.isCancelled else { return }
                }
                guard self.client === client, !Task.isCancelled else { return }
            } while self.historySyncRequested
        }
    }

    /// Published only on a real change: the notice and the Files tab reload react to
    /// this flag, and an idle pass must leave both alone.
    private func setOfflineSyncInProgress(_ value: Bool) {
        if offlineSyncInProgress != value { offlineSyncInProgress = value }
    }

    private func setAttachmentsComplete(_ value: Bool) {
        if offlineAttachmentsComplete != value { offlineAttachmentsComplete = value }
    }

    private func setMirroringDeferred(_ value: Bool) {
        if offlineMirroringDeferred != value { offlineMirroringDeferred = value }
    }

    private func markAttachmentsIncomplete(client: AgentClient) {
        if self.client === client { setAttachmentsComplete(false) }
    }

    /// The path turned metered while the pass was running: the attachments are
    /// waiting for Wi-Fi, which is a different message from "sync incomplete".
    private func markMirroringDeferred(client: AgentClient) {
        if self.client === client { setMirroringDeferred(true) }
    }

    private func cacheBackgroundHistory(_ history: HistoryResponse, metadata: SessionMetaItem, client: AgentClient, expectedEpoch: Int) async {
        guard self.client === client, !Task.isCancelled,
              sessions.contains(where: { $0.sessionId == metadata.sessionId && $0.lastUpdated == metadata.lastUpdated && $0.turnCount == metadata.turnCount }),
              !isSessionDeleting(metadata.sessionId) else { return }
        do {
            let saved = try await offlineHistory.saveHistory(history, metadata: metadata, namespace: client.cacheNamespace, requireListed: true, expectedEpoch: expectedEpoch)
            if saved, self.client === client {
                await refreshSavedSessionIDs(client: client)
                guard self.client === client, !Task.isCancelled else { return }
                // A suffix already present before validation may have been deleted remotely.
                // Only terminals appended during this comparison can extend its saved base.
                guard canonicalTurns.count == history.turns.count else { return }
                // A terminal can append while the off-main comparison is suspended.
                // Prove the entire saved prefix against a stable current projection before
                // adopting its index version, then save any authoritative terminal suffix.
                let selection = selectionEpoch
                while isCurrent(history.sessionId, selection, client), !Task.isCancelled,
                      canonicalTurns.count >= history.turns.count {
                    let revision = canonicalProjectionRevision
                    let prefix = Array(canonicalTurns.prefix(history.turns.count))
                    let matches = await offlineHistory.canonicalTurnsMatch(prefix, history.turns)
                    guard isCurrent(history.sessionId, selection, client), !Task.isCancelled,
                          sessions.first(where: { $0.sessionId == history.sessionId }).map(OfflineHistoryStore.Version.init) == OfflineHistoryStore.Version(metadata) else { return }
                    if canonicalProjectionRevision != revision { continue }
                    guard matches else { return }
                    currentCanonicalMetadata = metadata
                    if canonicalTurns.count > history.turns.count {
                        persistCurrentHistory(sessionId: history.sessionId, client: client)
                    }
                    break
                }
            }
        } catch { /* Keep the existing readable copy and retry on the next index refresh. */ }
    }

    func newSession() {
        detach()
        selectedSessionId = Self.makeSessionId()
        clearHistoryProjection()
        messages = []
        knownTurnCount = 0
    }

    func select(sessionId: String) {
        guard sessionId != selectedSessionId else { return }
        detach()
        selectedSessionId = sessionId
        clearHistoryProjection()
        messages = []
        Task { await open(sessionId: sessionId) }
    }

    /// 统一恢复入口：历史（落盘轮）→ outbox 回收提示 → 附着（活跃轮重放 + 实时续播）。
    private func open(sessionId: String) async {
        guard let client, selectedSessionId == sessionId else { return }
        let epoch = selectionEpoch
        if canonicalTurns.isEmpty, optimisticReceipt == nil,
           let cached = await offlineHistory.loadHistory(sessionId: sessionId, namespace: client.cacheNamespace),
           isCurrent(sessionId, epoch, client), !Task.isCancelled,
           canonicalTurns.isEmpty, optimisticReceipt == nil {
            applyHistoryProjection(cached, metadata: sessions.first { $0.sessionId == sessionId })
            historyIsReadOnly = true
            let cachedDeck = await client.cachedDeck(sessionId: sessionId)
            guard isCurrent(sessionId, epoch, client) else { return }
            deck = cachedDeck
        }
        if let recoveryTask {
            guard await recoveryTask.value, isCurrent(sessionId, epoch, client) else { return }
        } else {
            let id = UUID()
            recoveryId = id
            let task = Task { await self.loadHistory(sessionId: sessionId) }
            recoveryTask = task
            let loaded = await task.value
            if recoveryId == id { recoveryTask = nil; recoveryId = nil }
            guard loaded, isCurrent(sessionId, epoch, client) else { return }
        }
        // 发送暂存回收：上次 App 可能死在 multipart 上传途中——凭据仅剩 outbox
        if optimisticReceipt == nil, let pending = outboxRead(sessionId: sessionId) {
            // **不清条目**（首审 H3）：清了键,用户按提示重发就换新键 = 服务端判不了重,
            // 恰好在「杀 App 后重开」这条移动端主路上把幂等做没了。提示只出一次
            // (hinted 标记),条目留到 202/turn_ended 对账清仓。
            if !pending.hinted {
                outboxMarkHinted(sessionId: sessionId)
                if outboxAll()[sessionId]?["operation"] == "retry" {
                    messages.append(.system(L("上次任务重试可能已受理；请查看尝试记录，再确认是否重发同一次重试。")))
                } else {
                    messages.append(.system(L("上次可能有一条消息未送达：「") + pending.prompt + L("」——请确认后按需重发。")))
                }
            }
        }
        ensureAttached(sessionId: sessionId)
    }

    @discardableResult
    private func loadHistory(sessionId: String) async -> Bool {
        guard let client, selectedSessionId == sessionId else { return false }
        let epoch = selectionEpoch
        // Bind the response to the index version observed before this request.
        let requestMetadata = sessions.first { $0.sessionId == sessionId }
        let cacheEpoch = await offlineHistory.epoch(sessionId: sessionId, namespace: client.cacheNamespace)
        guard isCurrent(sessionId, epoch, client), !Task.isCancelled else { return false }
        currentCacheEpoch = cacheEpoch
        do {
            var history = try await client.history(sessionId: sessionId)
            var projectionMetadata = requestMetadata
            guard history.status, history.sessionId == sessionId else { throw AgentError.http(200, L("历史读取未被受理。")) }
            guard isCurrent(sessionId, epoch, client), !Task.isCancelled else { return false }
            // Index/backfill may have advanced while this foreground request was in flight.
            // Prefer its exact saved snapshot; never install an old body under the new version.
            let latestMetadata = sessions.first { $0.sessionId == sessionId }
            if requestMetadata.map(OfflineHistoryStore.Version.init) != latestMetadata.map(OfflineHistoryStore.Version.init) {
                guard let latestMetadata,
                      let latest = await offlineHistory.loadHistory(sessionId: sessionId, namespace: client.cacheNamespace,
                          matchingVersion: OfflineHistoryStore.Version(latestMetadata)),
                      isCurrent(sessionId, epoch, client), !Task.isCancelled,
                      sessions.first(where: { $0.sessionId == sessionId }).map(OfflineHistoryStore.Version.init) == OfflineHistoryStore.Version(latestMetadata) else {
                    guard isCurrent(sessionId, epoch, client), !Task.isCancelled else { return false }
                    historyProjectionNeedsRefresh = true
                    if optimisticReceipt == nil { historyIsReadOnly = true }
                    return false
                }
                history = latest
                projectionMetadata = latestMetadata
            }
            if let required = missingThinRecord,
               required.index.map({ history.turns.indices.contains($0) }) != true {
                // A successful HTTP response is not a complete canonical projection.
                // Keep the prior display and newer submit; stop before reattaching.
                historyProjectionNeedsRefresh = true
                incompleteHistoryError = L("历史记录尚不完整；原展示已保留，请手动同步后再继续。")
                errorMessage = incompleteHistoryError
                return false
            }
            clearIncompleteHistoryError()
            missingThinRecord = nil
            // Preserve only the current submit's optimistic bubble until terminal reconciliation.
            let optimistic = optimisticReceipt == nil ? [] : messages.filter { $0.id == optimisticUserMessageId }
            applyHistoryProjection(history, optimistic: optimistic, metadata: projectionMetadata)
            retryStatusNotice = nil
            historyProjectionNeedsRefresh = false
            knownTurnCount = history.turns.count
            replayAfter = min(history.replayAfter ?? 0, optimisticFirstSeq.map { $0 - 1 } ?? Int.max)
            // A just-accepted submit can already be in history before its first attachment.
            // Replaying its known queued identity reconciles the optimistic bubble without guessing text.
            cursor = replayAfter ?? 0
            let saved = try? await offlineHistory.saveHistory(history,
                metadata: projectionMetadata, namespace: client.cacheNamespace, expectedEpoch: cacheEpoch)
            guard isCurrent(sessionId, epoch, client), !Task.isCancelled else { return false }
            if saved == true { await refreshSavedSessionIDs(client: client) }
            else { offlineSavedSessionIDs.remove(sessionId) }
            guard isCurrent(sessionId, epoch, client), !Task.isCancelled else { return false }
            let fetchedDeck = try? await client.deck(sessionId: sessionId)
            guard isCurrent(sessionId, epoch, client), !Task.isCancelled else { return false }
            deck = fetchedDeck
            return true
        } catch {
            guard isCurrent(sessionId, epoch, client), !Task.isCancelled else { return false }
            if !canonicalTurns.isEmpty { historyIsReadOnly = true }
            if canonicalTurns.isEmpty || historyProjectionNeedsRefresh || !Self.isOfflineTransportFailure(error) {
                errorMessage = L("历史加载失败：") + error.localizedDescription
            }
            return false
        }
    }

    private func applyHistoryProjection(_ history: HistoryResponse, optimistic: [ChatMessage] = [], metadata: SessionMetaItem? = nil) {
        let displayed = HistoryProjection.indices(history)
        canonicalTurns = history.turns; lastTurnRef = history.lastTurnRef
        retryGroups = HistoryProjection.groups(history, displayed: displayed)
        currentDisplayIndices = history.displayTurnIndices
        currentHistoryGeneration = history.generation
        currentCanonicalMetadata = metadata
        canonicalProjectionRevision = UUID()
        messages = displayed.flatMap { ChatMessage.from(turn: history.turns[$0], turnIndex: $0) } + optimistic
        knownTurnCount = history.turns.count
    }

    private func persistCurrentHistory(sessionId: String, client: AgentClient) {
        let snapshot = HistoryResponse(status: true, sessionId: sessionId, turns: canonicalTurns,
            replayAfter: cursor, generation: currentHistoryGeneration, lastTurnRef: lastTurnRef,
            displayTurnIndices: currentDisplayIndices, retryGroups: retryGroups.isEmpty ? nil : retryGroups)
        let metadata = currentCanonicalMetadata, cache = offlineHistory
        let expectedEpoch = currentCacheEpoch
        offlineSavedSessionIDs.remove(sessionId)
        Task {
            let saved = try? await cache.saveHistory(snapshot, metadata: metadata, namespace: client.cacheNamespace, expectedEpoch: expectedEpoch)
            guard self.client === client else { return }
            if saved == true { await refreshSavedSessionIDs(client: client) }
            else { offlineSavedSessionIDs.remove(sessionId) }
        }
    }

    struct ManagementDeleteResult {
        var deleted: Set<String> = []
        var failures: [String: String] = [:]
        var obsolete = false
        var failedIDs: Set<String> { Set(failures.keys) }
    }

    /// One awaited receipt per resource. Failure is never projected as deletion.
    private func collectDeleteReceipts(_ ids: [String], client: AgentClient,
        action: (AgentClient, String) async throws -> (Bool, String?)) async -> ManagementDeleteResult {
        var result = ManagementDeleteResult()
        for id in ids {
            guard self.client === client else { result.obsolete = true; break }
            do {
                let (accepted, reason) = try await action(client, id)
                guard self.client === client else { result.obsolete = true; break }
                if accepted { result.deleted.insert(id) }
                else { result.failures[id] = reason ?? L("删除未被受理，请检查后重试。") }
            } catch {
                guard self.client === client else { result.obsolete = true; break }
                result.failures[id] = L("删除未确认：") + error.localizedDescription
            }
        }
        return result
    }

    func deleteSession(_ item: SessionMetaItem) async -> ManagementDeleteResult {
        await deleteSessions([item])
    }

    func deleteSessions(_ items: [SessionMetaItem]) async -> ManagementDeleteResult {
        let ids = Array(Set(items.map(\.sessionId))).sorted()
        guard let client else { return ManagementDeleteResult(failures: Dictionary(uniqueKeysWithValues: ids.map { ($0, L("连接状态未知，请刷新后重试。")) })) }
        let epoch = selectionEpoch, selected = selectedSessionId
        let receipt = optimisticReceipt, intent = submissionIntentEpoch
        let savedReceipt = selected.flatMap { outboxAll()[$0]?["receipt"] }
        let savedReceipts = Dictionary(uniqueKeysWithValues: ids.compactMap { id in outboxAll()[id]?["receipt"].map { (id, $0) } })
        let operation = UUID()
        let available = ids.filter { !isSessionDeleting($0) && !hasSubmissionInFlight($0) }
        let blocked = ids.filter { isSessionDeleting($0) || hasSubmissionInFlight($0) }
        for id in available { sessionDeleteRequests[id] = operation; deletingSessionIDs.insert(id) }
        defer {
            for id in available where sessionDeleteRequests[id] == operation {
                sessionDeleteRequests[id] = nil; deletingSessionIDs.remove(id)
            }
        }
        var result = await collectDeleteReceipts(available, client: client) { client, id in
            let response = try await client.deleteSession(sessionId: id)
            let confirmed = response.status || response.reason == "not_found"
            if self.client === client, confirmed { invalidateSessionIndexRequests() }
            return (confirmed, response.response)
        }
        for id in blocked {
            result.failures[id] = isSessionDeleting(id)
                ? L("会话正在删除，请等待删除结果后再操作。")
                : L("会话有正在提交的请求，本次未删除。请等待结果后重试。")
        }
        guard self.client === client, !result.obsolete else { return result }
        if let sid = selected, result.deleted.contains(sid), isCurrent(sid, epoch, client),
           submissionIntentEpoch == intent,
           (optimisticReceipt == nil || optimisticReceipt == receipt),
           (outboxAll()[sid]?["receipt"] == nil || outboxAll()[sid]?["receipt"] == savedReceipt) {
            detach(); selectedSessionId = nil; clearHistoryProjection(); knownTurnCount = 0; messages = []
        }
        for id in result.deleted where outboxAll()[id]?["receipt"] == savedReceipts[id] {
            outboxClear(sessionId: id)
        }
        try? await offlineHistory.removeSessions(result.deleted, namespace: client.cacheNamespace)
        // A confirmed deletion frees space; mirrors cut short by the budget retry.
        if await client.offlineAssets.removeSessions(result.deleted, namespace: client.cacheNamespace) {
            try? await offlineHistory.retryPartialAssetSessions(namespace: client.cacheNamespace)
        }
        guard self.client === client else { return result }
        offlineSavedSessionIDs.subtract(result.deleted)
        sessions.removeAll { result.deleted.contains($0.sessionId) }
        await loadSessions()
        return result
    }

    func deleteKnowledgeDocuments(_ paths: [String]) async -> ManagementDeleteResult {
        let ids = Array(Set(paths)).sorted()
        guard let client else { return ManagementDeleteResult(failures: Dictionary(uniqueKeysWithValues: ids.map { ($0, L("连接状态未知，请刷新后重试。")) })) }
        return await collectDeleteReceipts(ids, client: client) { client, path in
            let response = try await client.ragDeleteDocument(sourcePath: path)
            // Legacy endpoint confirms by HTTP; explicit business refusals throw in API.
            return (true, response.msg)
        }
    }

    func togglePin(_ item: SessionMetaItem) {
        guard let client, !rejectDeletingSession(item.sessionId) else { return }
        Task {
            guard self.client === client, !rejectDeletingSession(item.sessionId) else { return }
            do {
                _ = try await client.setPin(sessionId: item.sessionId, pinned: !(item.pinned ?? false))
                if self.client === client { invalidateSessionIndexRequests() }
            } catch {
                guard self.client === client else { return }
                errorMessage = L("保留状态保存失败，请重试。")
            }
            await loadSessions()
        }
    }

    /// 首轮自动标题：取首条提问的第一行、压缩空白、截 20 字（超长加省略号）。
    static func autoTitle(from prompt: String) -> String {
        let firstLine = prompt.split(separator: "\n", maxSplits: 1,
                                     omittingEmptySubsequences: true).first ?? ""
        let squashed = firstLine.split(separator: " ").joined(separator: " ")
        if squashed.count <= 20 { return String(squashed) }
        return squashed.prefix(20) + "…"
    }

    /// 会话改名(服务端一等,Phase 4b:双端同源)——写后端后刷新列表。
    func rename(_ item: SessionMetaItem, to title: String) {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let client, !rejectDeletingSession(item.sessionId) else { return }
        Task {
            guard self.client === client, !rejectDeletingSession(item.sessionId) else { return }
            let response = try? await client.renameSession(sessionId: item.sessionId, title: trimmed)
            if self.client === client, response?.status == true { invalidateSessionIndexRequests() }
            await loadSessions()
        }
    }

    // MARK: - 发消息（202 受理 + 事件流）

    func send(prompt: String, uploads: [Upload], thinking: Bool) {
        guard !rejectReadOnlyHistory() else { return }
        guard !isGenerating, let client else { return }
        let sid: String
        if let existing = selectedSessionId {
            sid = existing
        } else {
            sid = Self.makeSessionId()
            selectedSessionId = sid
        }
        guard !rejectDeletingSession(sid) else { return }
        // 首轮自动命名（实机反馈：新会话在侧栏顶着随机 id 很生硬）——用首条提问的
        // 首行截断当标题，写到服务端 meta（双端同源），用户随时可改名覆盖。
        let epoch = selectionEpoch
        let firstTurn = knownTurnCount == 0
            && !messages.contains(where: { $0.kind == .user || $0.kind == .assistant })

        var bubble = prompt
        if !uploads.isEmpty {
            bubble += "\n📎 " + uploads.map(\.filename).joined(separator: "、")
        }
        let userMessage = ChatMessage.user(bubble)
        let bubbleId = userMessage.id
        messages.append(userMessage)
        optimisticUserMessageId = bubbleId
        optimisticPrompt = prompt
        // 键纪律(B3):纯文本同文重发复用旧键;**带附件不复用也不存键**——键只比对得了
        // 文本,同文换附件若被判重,新附件会被静默丢弃(首审 M6)
        let clientTurnId = uploads.isEmpty ? outboxKeyFor(sessionId: sid, prompt: prompt)
                                           : UUID().uuidString.lowercased()
        outboxWrite(sessionId: sid, prompt: prompt,
                    key: uploads.isEmpty ? clientTurnId : nil)
        let submittedReceipt = outboxAll()[sid]!["receipt"]!
        submissionIntentEpoch = UUID()
        optimisticReceipt = submittedReceipt
        optimisticOperation = "send"
        optimisticClientTurnId = clientTurnId
        optimisticTurnId = nil
        optimisticFirstSeq = nil
        resetPreview()
        isGenerating = true

        var params = ChatParams(prompt: prompt)
        params.clientTurnId = clientTurnId
        params.enableThinking = thinking
        submissionHTTPRequests[submittedReceipt] = SubmissionHTTP(sessionId: sid, client: ObjectIdentifier(client))
        Task { [weak self] in
            defer { self?.submissionHTTPRequests[submittedReceipt] = nil }
            guard self?.shouldBlockSubmission(sid, client: client, receipt: submittedReceipt, epoch: epoch) != true else { return }
            do {
                let accepted = try await client.submitTurn(sessionId: sid, params: params, files: uploads)
                self?.submissionHTTPRequests[submittedReceipt] = nil
                guard let self else { return }
                if self.client === client, accepted.status { self.invalidateSessionIndexRequests() }
                if self.client === client, self.outboxAll()[sid]?["receipt"] == submittedReceipt {
                    self.outboxClear(sessionId: sid)
                }
                guard self.isCurrent(sid, epoch, client) else { return }
                if firstTurn && accepted.duplicate != true {
                    let title = Self.autoTitle(from: prompt)
                    if !title.isEmpty {
                        _ = try? await client.renameSession(sessionId: sid, title: title)
                        guard self.isCurrent(sid, epoch, client) else { return }
                        await self.loadSessions()
                    }
                }
                await self.acceptSubmission(accepted, sessionId: sid, epoch: epoch, client: client,
                                            receipt: submittedReceipt, bubbleId: bubbleId)
            } catch let conflict as TurnConflictError {
                self?.submissionHTTPRequests[submittedReceipt] = nil
                guard let self else { return }
                if self.client === client, self.outboxAll()[sid]?["receipt"] == submittedReceipt { self.outboxClear(sessionId: sid) }
                guard self.isCurrent(sid, epoch, client), self.optimisticReceipt == submittedReceipt, self.optimisticTurnId == nil else { return }
                self.clearOptimistic()
                self.messages.removeAll { $0.id == bubbleId }
                self.isGenerating = false
                _ = conflict
                self.errorMessage = L("该会话已有正在进行的轮次，请等它完结后再发送。")
            } catch {
                self?.submissionHTTPRequests[submittedReceipt] = nil
                guard let self else { return }
                guard self.isCurrent(sid, epoch, client), self.optimisticReceipt == submittedReceipt, self.optimisticTurnId == nil else { return }
                // 网络类失败**不清 outbox**（B3）:请求可能已达、只是 202 回程丢了——
                // 键留着,用户重发同文即复用,服务端判重不二次执行。真冲突(409)才清。
                self.clearOptimistic()
                self.messages.removeAll { $0.id == bubbleId }
                self.isGenerating = false
                self.errorMessage = L("发送失败：") + error.localizedDescription
            }
        }
    }

    /// Common R15 acceptance ownership for plain submit and canonical retry.
    private func acceptSubmission(_ accepted: TurnAccepted, sessionId sid: String, epoch: UUID,
                                  client: AgentClient, receipt: String, bubbleId: UUID?) async {
        guard accepted.status else { throwSubmissionFailure(receipt: receipt); return }
        if self.client === client { invalidateSessionIndexRequests() }
        if self.client === client, outboxAll()[sid]?["receipt"] == receipt { outboxClear(sessionId: sid) }
        guard isCurrent(sid, epoch, client), optimisticReceipt == receipt else { return }
        if accepted.duplicate == true {
            if let bubbleId { messages.removeAll { $0.id == bubbleId } }
            clearOptimistic()
            isGenerating = false
            recoverHistoryProjection(sessionId: sid)
            return
        }
        optimisticTurnId = accepted.turnId
        optimisticFirstSeq = accepted.seq
        if let turnId = accepted.turnId, lastEndedId == turnId {
            if let bubbleId { messages.removeAll { $0.id == bubbleId } }
            clearOptimistic()
            isGenerating = false
        } else if let turnId = accepted.turnId, liveTurnId == turnId {
            if let queuedUser = liveUserMessageId, queuedUser != bubbleId {
                messages.removeAll { $0.id == queuedUser }
            }
            liveUserMessageId = bubbleId
        } else if accepted.turnId != nil, let seq = accepted.seq, cursor >= seq {
            recoverHistoryProjection(sessionId: sid)
            return
        }
        ensureAttached(sessionId: sid)
    }

    private func throwSubmissionFailure(receipt: String) {
        guard optimisticReceipt == receipt else { return }
        if let bubble = optimisticUserMessageId { messages.removeAll { $0.id == bubble } }
        clearOptimistic()
        isGenerating = false
        errorMessage = L("请求未被受理；原记录已保留。")
    }

    // MARK: - 审批 / 任务控制

    func decideApproval(approve: Bool, target: PendingApprovalTarget) {
        guard !rejectReadOnlyHistory() else { return }
        guard let client, selectedSessionId == target.sessionId,
              pendingApprovalTarget == target, pendingTools?.isEmpty == false else { return }
        let sid = target.sessionId
        guard !rejectDeletingSession(sid) else { return }
        let epoch = selectionEpoch
        let tools = pendingTools
        pendingTools = nil
        isGenerating = true   // 乐观;事件流的 resumed/turn_started 确认
        Task { [weak self] in
            do {
                guard let self, self.isCurrent(sid, epoch, client) else { return }
                if self.rejectDeletingSession(sid) {
                    if self.pendingApprovalTarget == target { self.pendingTools = tools; self.isGenerating = false }
                    return
                }
                _ = try await client.submitApproval(sessionId: sid, approve: approve,
                                                    expectedTurnId: target.turnId,
                                                    expectedApprovalSeq: target.seq)
            } catch is TurnConflictError {
                // 决议目标已不存在（另一端已处理/已过期）：以服务端为准重载
                guard let self, self.isCurrent(sid, epoch, client),
                      self.pendingApprovalTarget == target else { return }
                self.detach()
                self.messages = []
                await self.open(sessionId: sid)
            } catch {
                guard let self, self.isCurrent(sid, epoch, client),
                      self.pendingApprovalTarget == target else { return }
                self.isGenerating = false
                self.pendingTools = tools
                self.errorMessage = L("审批提交失败：") + error.localizedDescription
            }
        }
    }

    /// The server controls the current session; captured identity fences only client projection writes.
    func cancelGeneration() { requestControl(.cancel) }
    func suspendTask() { requestControl(.suspend) }

    func resumeTask(sessionId: String? = nil) {
        if let sessionId, sessionId != selectedSessionId {
            controlTask(sessionId: sessionId, kind: .resume)
        } else { requestControl(.resume) }
    }

    /// Task center uses one read of /turn for an unselected session, then existing session control APIs.
    /// No additional attachment or polling loop is introduced.
    func controlTask(sessionId sid: String, kind: ControlKind) {
        guard let client else { return }
        guard !rejectDeletingSession(sid) else { return }
        if sid != selectedSessionId { select(sessionId: sid) }
        let epoch = selectionEpoch
        Task { [weak self] in
            do {
                let snapshot = try await client.turnState(sessionId: sid)
                guard let self, self.isCurrent(sid, epoch, client) else { return }
                guard snapshot.status, let turn = snapshot.turn, let turnId = turn.turnId,
                      !turnId.isEmpty else {
                    self.errorMessage = L("无法确认任务轮次，请刷新后再操作。")
                    return
                }
                self.requestControl(kind, confirmedTurn: (turnId, turn.state,
                    max(snapshot.lastSeq ?? 0, turn.lastSeq ?? 0)))
            } catch {
                guard let self, self.isCurrent(sid, epoch, client) else { return }
                self.errorMessage = L("控制请求失败：") + error.localizedDescription
            }
        }
    }

    private func requestControl(_ kind: ControlKind, confirmedTurn: (String, String, Int)? = nil) {
        guard let client, let sid = selectedSessionId, controlRequest == nil else { return }
        guard !rejectDeletingSession(sid) else { return }
        guard confirmedTurn != nil || !rejectReadOnlyHistory() else { return }
        let snapshotIsCurrent = confirmedTurn.map { $0.2 >= cursor } ?? false
        let turnId = snapshotIsCurrent ? confirmedTurn?.0 : liveTurnId
        let phase = snapshotIsCurrent ? (confirmedTurn?.1 ?? ledgerPhase) : ledgerPhase
        let observedSeq = max(cursor, confirmedTurn?.2 ?? 0)
        guard let turnId, !turnId.isEmpty,
              (kind == .suspend ? phase == "running" : kind == .resume ? phase == "suspended" : Self.canCancelTask(phase: phase)) else {
            errorMessage = L("当前任务状态不支持此操作。")
            return
        }
        // A newer ledger round supersedes a one-shot task-center snapshot.
        guard lastEndedId != turnId,
              liveTurnId == nil || liveTurnId == turnId || snapshotIsCurrent else { return }
        let id = UUID(), epoch = selectionEpoch
        controlRequest = ControlRequest(id: id, kind: kind, turnId: turnId, observedSeq: observedSeq, accepted: false)
        Task { [weak self] in
            do {
                guard let self, self.isCurrent(sid, epoch, client), self.controlRequest?.id == id else { return }
                if self.rejectDeletingSession(sid) { self.controlRequest = nil; return }
                let accepted: Bool
                let reason: String?
                switch kind {
                case .cancel:
                    let result = try await client.cancelTask(sessionId: sid)
                    accepted = result.status; reason = result.response
                case .suspend:
                    let result = try await client.suspendTask(sessionId: sid)
                    accepted = result.status; reason = result.response
                case .resume:
                    let result = try await client.submitResume(sessionId: sid)
                    accepted = result.status; reason = nil
                }
                guard self.isCurrent(sid, epoch, client), self.controlRequest?.id == id,
                      self.liveTurnId == nil || self.liveTurnId == turnId || self.cursor <= observedSeq else { return }
                if accepted {
                    self.controlRequest?.accepted = true
                    self.ensureAttached(sessionId: sid)
                } else {
                    self.controlRequest = nil
                    self.errorMessage = reason ?? L("控制请求未被受理。")
                }
            } catch {
                guard let self, self.isCurrent(sid, epoch, client), self.controlRequest?.id == id,
                      self.liveTurnId == nil || self.liveTurnId == turnId || self.cursor <= observedSeq else { return }
                self.controlRequest = nil
                self.errorMessage = L("控制请求失败：") + error.localizedDescription
            }
        }
    }

    /// Existing UI entry now opens a bound confirmation, never DELETE+plain text fallback.
    func regenerateLastTurn() { requestRetryConfirmation() }

    func requestRetryConfirmation() {
        guard !rejectReadOnlyHistory() else { return }
        guard !isGenerating, !isSuspended, ledgerPhase != "awaiting_approval",
              !historyProjectionNeedsRefresh, pendingTools?.isEmpty != false, let client, let sid = selectedSessionId else { return }
        guard !rejectDeletingSession(sid) else { return }
        guard let canonicalRef = lastTurnRef, !canonicalRef.isEmpty, !canonicalTurns.isEmpty else {
            errorMessage = L("此服务未提供可重试的任务引用；原记录未删除。")
            return
        }
        let pending = outboxAll()[sid]
        let uncertain = pending?["operation"] == "retry" && pending?["retry_uncertain"] != "0"
        let source = uncertain ? (pending?["source_ref"] ?? canonicalRef) : canonicalRef
        let key = uncertain ? (pending?["key"] ?? UUID().uuidString.lowercased()) : UUID().uuidString.lowercased()
        let target = RetryConfirmationTarget(id: UUID(), sessionId: sid, sourceRef: source,
            canonicalRef: canonicalRef, canonicalIndex: canonicalTurns.count - 1,
            sourcePrompt: (uncertain ? pending?["prompt"] : canonicalTurns.last?.prompt) ?? "",
            isResend: uncertain, clientTurnId: key, selectionEpoch: selectionEpoch,
            clientIdentity: ObjectIdentifier(client))
        retryConfirmationId = target.id
        retryConfirmationTarget = target
    }

    func cancelRetryConfirmation() { retryConfirmationId = nil; retryConfirmationTarget = nil }

    func confirmRetry(_ target: RetryConfirmationTarget) {
        guard !rejectReadOnlyHistory() else { return }
        retryConfirmationTarget = nil
        guard let client, retryConfirmationId == target.id,
              isCurrent(target.sessionId, target.selectionEpoch, client),
              ObjectIdentifier(client) == target.clientIdentity, lastTurnRef == target.canonicalRef,
              lastCanonicalIndex == target.canonicalIndex,
              !isGenerating, !isSuspended, ledgerPhase != "awaiting_approval",
              !historyProjectionNeedsRefresh, pendingTools?.isEmpty != false else {
            retryConfirmationId = nil
            errorMessage = L("任务已变化，重试确认已失效，请重新确认。")
            return
        }
        retryConfirmationId = nil
        let sid = target.sessionId, epoch = selectionEpoch
        guard !rejectDeletingSession(sid) else { return }
        let prompt = canonicalTurns.last?.prompt ?? ""
        outboxWrite(sessionId: sid, prompt: prompt, key: target.clientTurnId,
                    operation: "retry", sourceRef: target.sourceRef)
        let receipt = outboxAll()[sid]!["receipt"]!
        submissionIntentEpoch = UUID()
        optimisticReceipt = receipt
        optimisticOperation = "retry"
        optimisticPrompt = prompt
        optimisticClientTurnId = target.clientTurnId
        optimisticUserMessageId = nil
        optimisticTurnId = nil
        optimisticFirstSeq = nil
        isGenerating = true
        resetPreview()
        submissionHTTPRequests[receipt] = SubmissionHTTP(sessionId: sid, client: ObjectIdentifier(client))
        Task { [weak self] in
            defer { self?.submissionHTTPRequests[receipt] = nil }
            guard self?.shouldBlockSubmission(sid, client: client, receipt: receipt, epoch: epoch) != true else { return }
            do {
                let accepted = try await client.submitRetry(sessionId: sid, sourceRef: target.sourceRef,
                                                            clientTurnId: target.clientTurnId)
                self?.submissionHTTPRequests[receipt] = nil
                guard let self else { return }
                await self.acceptSubmission(accepted, sessionId: sid, epoch: epoch, client: client,
                                            receipt: receipt, bubbleId: nil)
            } catch {
                self?.submissionHTTPRequests[receipt] = nil
                guard let self, self.isCurrent(sid, epoch, client),
                      self.optimisticReceipt == receipt, self.optimisticTurnId == nil else { return }
                self.clearOptimistic()
                self.isGenerating = false
                if let conflict = error as? TurnConflictError {
                    if conflict.reason != "retry_lookup_unavailable" { self.markRetryRejected(sessionId: sid, receipt: receipt) }
                    switch conflict.reason {
                    case "retry_inputs_unavailable":
                        self.errorMessage = L("重试所需输入或附件不完整。请检查尝试记录，并重新附上所需输入。")
                    case "retry_source_changed":
                        self.historyProjectionNeedsRefresh = true
                        self.errorMessage = L("任务已变化，请先同步尝试记录，再确认重试。")
                    case "retry_lookup_unavailable":
                        self.historyProjectionNeedsRefresh = true
                        self.errorMessage = L("目前无法核验重试是否已受理。请先检查尝试记录，再决定是否重发。")
                    default:
                        self.errorMessage = L("任务重试未确认；原记录已保留：") + error.localizedDescription
                    }
                } else if case AgentError.http(404, _) = error {
                    self.markRetryRejected(sessionId: sid, receipt: receipt)
                    self.errorMessage = L("此服务不支持任务重试；原记录未删除。")
                } else { self.errorMessage = L("任务重试未确认；原记录已保留：") + error.localizedDescription }
            }
        }
    }

    private func markRetryRejected(sessionId: String, receipt: String) {
        var all = outboxAll()
        guard all[sessionId]?["receipt"] == receipt else { return }
        all[sessionId]?["retry_uncertain"] = "0"
        UserDefaults.standard.set(all, forKey: outboxKey)
    }

    private func clearHistoryProjection() {
        canonicalTurns = []; retryGroups = []; lastTurnRef = nil
        currentDisplayIndices = nil; currentHistoryGeneration = nil; currentCanonicalMetadata = nil
        // The epoch belongs to the projection it was read with: a later save must
        // never carry the previous session's deletion fence.
        currentCacheEpoch = 0
        canonicalProjectionRevision = UUID()
        retryStatusNotice = nil; historyProjectionNeedsRefresh = false
    }

    /// Re-enter the existing history -> turn -> events path, retaining newer outbox ownership.
    private func recoverHistoryProjection(sessionId: String) {
        guard selectedSessionId == sessionId else { return }
        historyProjectionNeedsRefresh = true
        attachmentId = nil
        attachTask?.cancel(); attachTask = nil; attachedSessionId = nil
        replayAfter = nil
        // open/loadHistory preserve optimistic receipt/bubble and original display on failure.
        Task { await open(sessionId: sessionId) }
    }

    func refreshDisplayedHistory() {
        guard let sid = selectedSessionId else { return }
        clearIncompleteHistoryError()
        // Manual sync retries the same coverage requirement; it cannot waive it.
        recoverHistoryProjection(sessionId: sid)
    }

    // MARK: - 事件附着（账本协议核心）

    /// 附着中的会话（与 attachTask 一起维护;循环退出后自清,可再次附着——
    /// 新会话首次附着会 404 退出,首条消息 202 受理后 send 再次调用本方法)。
    private var attachedSessionId: String?

    private func ensureAttached(sessionId: String) {
        guard selectedSessionId == sessionId, let client else { return }
        guard !(attachedSessionId == sessionId && attachTask != nil) else { return }
        if replayAfter == nil {
            Task { await open(sessionId: sessionId) }
            return
        }
        attachTask?.cancel()
        let id = UUID(), epoch = selectionEpoch
        attachmentId = id
        attachedSessionId = sessionId
        attachTask = Task { [weak self] in
            await self?.attachLoop(sessionId: sessionId, epoch: epoch, client: client, id: id)
            self?.attachEnded(id: id)
        }
    }

    /// Cleanup belongs to one task incarnation, including A -> B -> A.
    private func attachEnded(id: UUID) {
        if attachmentId == id {
            attachedSessionId = nil
            attachmentId = nil
            attachTask = nil
        }
    }

    /// 断开附着并清空活跃轮簿记（换会话/设置变更时）。
    private func detach() {
        selectionEpoch = UUID()
        historyIsReadOnly = false
        currentCacheEpoch = 0
        cancelRetryConfirmation()
        controlRequest = nil
        ledgerPhase = "none"
        recoveryTask?.cancel()
        recoveryTask = nil
        recoveryId = nil
        attachmentId = nil
        replayAfter = nil
        missingThinRecord = nil
        incompleteHistoryError = nil
        pendingApprovalTarget = nil
        attachTask?.cancel()
        attachTask = nil
        attachedSessionId = nil
        cursor = 0
        live = NormalizedState()
        clearOptimistic()
        liveUserMessageId = nil
        liveTurnId = nil
        liveRawPrompt = nil
        liveOutboxReceipt = nil
        lastEndedId = nil
        isGenerating = false
        streamText = ""
        thinkingText = ""
        streamBuffer = ""
        thinkingBuffer = ""
        flushScheduled = false
        liveSteps = []
        liveStepItems = []
        approvalOutcome = nil
        activity = nil
        taskSnapshot = nil
        pendingTools = nil
        isSuspended = false
        errorMessage = nil
        deck = nil
        composerPrefill = nil
    }

    private func attachLoop(sessionId: String, epoch: UUID, client: AgentClient, id: UUID) async {
        var backoff: UInt64 = 1_000_000_000
        var anchored = false
        func ownsAttachment() -> Bool {
            !Task.isCancelled && isCurrent(sessionId, epoch, client) && attachmentId == id
        }
        while ownsAttachment() {
            do {
                var after = cursor
                if !anchored {
                    let ts = try await client.turnState(sessionId: sessionId)
                    guard ownsAttachment() else { return }
                    after = min(replayAfter ?? 0, ts.turn.flatMap { ($0.firstSeq ?? 1) - 1 } ?? (ts.lastSeq ?? 0))
                    cursor = after
                    historyIsReadOnly = false
                    anchored = true
                    if ts.turn == nil && optimisticReceipt == nil { isGenerating = false }
                }
                for try await env in try client.attachEvents(sessionId: sessionId, after: after) {
                    guard ownsAttachment() else { return }
                    backoff = 1_000_000_000
                    if env.seq > 0 {
                        if env.seq <= cursor { continue }
                        cursor = env.seq
                    }
                    if env.type == "turn_ended", env.data["discarded"] != .bool(true),
                       (env.data["prompt"] == nil || env.data["response"] == nil) {
                        let index = canonicalIndex(env.data["turn_index"])
                        if index == nil || index! < 0 || index! >= knownTurnCount {
                            // One same-entrance load must cover this record before reattachment.
                            missingThinRecord = MissingThinRecord(index: index)
                            recoverHistoryProjection(sessionId: sessionId)
                            return
                        }
                    }
                    dispatch(env, sessionId: sessionId)
                }
                return
            } catch {
                if !ownsAttachment() { return }
                if !canonicalTurns.isEmpty, optimisticReceipt == nil { historyIsReadOnly = true }
                anchored = false
                if case AgentError.http(404, _) = error { return }
                if case SSEError.badStatus(404) = error { return }
                try? await Task.sleep(nanoseconds: backoff)
                backoff = min(backoff * 2, 10_000_000_000)
            }
        }
    }

    /// 事件分发：归约器推进活跃轮状态,再把结果投影进 UI（发布属性 + 消息流）。
    private func dispatch(_ env: EventEnvelope, sessionId: String) {
        if env.type == "stream_attached" { return }
        live = TurnReducer.reduce(live, env)
        ledgerPhase = live.turnState
        if let request = controlRequest {
            let newerThanRequest = env.seq > request.observedSeq
            let newRound = newerThanRequest && env.type == "turn_queued" && env.turnId != request.turnId
            let matchesAction: Bool
            switch request.kind {
            case .cancel: matchesAction = env.type == "turn_ended"
            case .suspend: matchesAction = ["suspended", "turn_ended"].contains(env.type)
            case .resume: matchesAction = ["resumed", "turn_started", "turn_ended"].contains(env.type)
            }
            let confirmed = newerThanRequest && env.turnId == request.turnId && matchesAction
            if newRound || confirmed { controlRequest = nil }
        }
        // transcript 是夹具口径的对账字段，UI 从不读取——长期附着下随轮次无界累积
        // （每轮完整 response 含工具结果），store 层不留存。归约器保持纯函数，夹具不受影响。
        if !live.transcript.isEmpty { live.transcript.removeAll() }

        if ["turn_queued", "turn_started", "resumed", "suspended", "turn_ended"].contains(env.type) {
            pendingApprovalTarget = nil
        }

        switch env.type {
        case "turn_queued":
            onTurnQueued(env)
        case "turn_started", "resumed":
            if env.type == "resumed" {
                switch env.str("trigger") {
                case "approve": approvalOutcome = .approved
                case "deny": approvalOutcome = .denied
                default: break   // resume after suspend: no approval decision
                }
            }
            if liveTurnId == nil { liveTurnId = env.turnId }
            isGenerating = true
            pendingTools = nil
            isSuspended = false
        case "token_delta":
            streamBuffer += env.str("text")
            scheduleStreamFlush()
        case "thinking_delta":
            thinkingBuffer += env.str("text")
            scheduleStreamFlush()
        case "tool_call_pending", "tool_started", "tool_progress", "tool_result",
             "step_completed", "step_failed":
            renderSteps()
        case "queue_moved":
            // 排队位次可见(Phase 3):单 worker 忙碌时,黑盒排队变成明确位次
            if case .number(let pos)? = env.data["position"] {
                let p = Int(pos)
                activity = LiveActivity(
                    phase: "running",
                    current: p == 0 ? L("排队中 · 下一个执行") : L("排队中 · 前面还有") + " \(p)",
                    steps: nil)
            }
        case "plan_updated":
            taskSnapshot = env.decode(TaskSnapshot.self, at: "task")
        case "awaiting_approval":
            flushStreamNow()
            isGenerating = false
            approvalOutcome = nil
            pendingTools = env.decode([PendingTool].self, at: "pending_tools") ?? []
            pendingApprovalTarget = env.turnId.flatMap { id in
                id.isEmpty ? nil : PendingApprovalTarget(sessionId: sessionId, turnId: id, seq: env.seq)
            }
        case "suspended":
            if liveTurnId == nil { liveTurnId = env.turnId }
            flushStreamNow()
            isGenerating = false
            isSuspended = true
        case "turn_ended":
            onTurnEnded(env, sessionId: sessionId)
        case "location_request":
            // 客户端反查(get_location 工具):本端即"用户正在用的设备",取一次定位回 POST。
            // 首次触发系统授权弹窗;拒绝/失败也要应答——后端工具在阻塞等待,静默会白等 25s。
            respondLocation(requestId: env.str("request_id"), sessionId: sessionId)
        default:
            break  // 未知事件类型:忽略(向前兼容)
        }
    }

    private func respondLocation(requestId: String, sessionId: String) {
        guard let client, !requestId.isEmpty else { return }
        Task {
            let body: AgentClient.LocationReportBody
            do {
                let fix = try await LocationProvider.shared.currentFix()
                body = .init(request_id: requestId, status: "ok",
                             latitude: fix.latitude, longitude: fix.longitude,
                             place: fix.place, accuracy_m: fix.accuracyM, error: nil)
            } catch LocationProvider.Failure.noFix(let reason) {
                body = .init(request_id: requestId, status: "error",
                             latitude: nil, longitude: nil, place: nil,
                             accuracy_m: nil, error: reason)
            } catch {
                body = .init(request_id: requestId, status: "denied",
                             latitude: nil, longitude: nil, place: nil,
                             accuracy_m: nil, error: L("用户未授权定位"))
            }
            // 409(迟到/他端已答)在 client 层已吞;其余网络错误静默——通道 fail-open
            try? await client.reportLocation(sessionId: sessionId, body: body)
        }
    }

    /// 轮次受理：确保用户气泡存在——收编本端乐观气泡;其他来源（web/定时任务/别的设备）
    /// 的轮次按事件补建,本端从此实时看到别端发起的任务。
    private func onTurnQueued(_ env: EventEnvelope) {
        resetPreview()
        isGenerating = true
        liveTurnId = env.turnId
        if case .string(let prompt)? = env.data["prompt"] { liveRawPrompt = prompt }
        else { liveRawPrompt = nil }
        let clientTurnId = env.str("client_turn_id")
        if !clientTurnId.isEmpty, let sid = selectedSessionId,
           outboxAll()[sid]?["key"] == clientTurnId {
            liveOutboxReceipt = outboxAll()[sid]?["receipt"]
        } else { liveOutboxReceipt = nil }
        if optimisticReceipt != nil,
           ((env.turnId != nil && optimisticTurnId == env.turnId) || (!clientTurnId.isEmpty && optimisticClientTurnId == clientTurnId)) {
            liveUserMessageId = optimisticUserMessageId
            optimisticTurnId = env.turnId
            optimisticFirstSeq = env.seq
        } else {
            let prompt = env.str("prompt")
            liveUserMessageId = nil
            if !prompt.isEmpty && env.data["retry_of"] == nil {
                let message = ChatMessage.user(prompt)
                messages.append(message)
                liveUserMessageId = message.id
            }
        }
    }

    private func clearOptimistic() {
        optimisticOperation = nil
        optimisticPrompt = nil
        optimisticClientTurnId = nil
        optimisticReceipt = nil
        optimisticUserMessageId = nil
        optimisticTurnId = nil
        optimisticFirstSeq = nil
    }

    /// 权威终态：撤下直播预览,按 turn_ended 的完整轮次记录对账（终态仅 4 种,
    /// 暂停态走 awaiting_approval/suspended 事件,不再经由 turn_end）。
    private func onTurnEnded(_ env: EventEnvelope, sessionId: String) {
        let discarded = env.data["discarded"] == .bool(true)
        let index = canonicalIndex(env.data["turn_index"])
        let archived = index.map { $0 >= 0 && $0 < knownTurnCount } ?? false
        let turn = discarded || archived ? nil : env.decode(Turn.self)
        let retryTerminal = env.data["retry_of"] != nil || turn?.retryOf != nil ||
            (optimisticOperation == "retry" && optimisticTurnId == env.turnId && env.turnId != nil)
        guard discarded || archived || turn != nil else { return }
        if !archived { invalidateSessionIndexRequests() }
        if let id = env.turnId { lastEndedId = id }
        if let turnId = env.turnId, liveTurnId == turnId {
            if let id = liveUserMessageId { messages.removeAll { $0.id == id } }
            if let receipt = liveOutboxReceipt, outboxAll()[sessionId]?["receipt"] == receipt {
                outboxClear(sessionId: sessionId)
            }
            liveUserMessageId = nil
            liveTurnId = nil
            liveRawPrompt = nil
            liveOutboxReceipt = nil
        }
        if let turnId = env.turnId, optimisticTurnId == turnId {
            if let id = optimisticUserMessageId { messages.removeAll { $0.id == id } }
            if let receipt = optimisticReceipt, outboxAll()[sessionId]?["receipt"] == receipt {
                outboxClear(sessionId: sessionId)
            }
            clearOptimistic()
        }
        resetPreview()
        isGenerating = optimisticReceipt != nil
        pendingTools = nil
        isSuspended = false

        if !discarded {
            if !archived { retryConfirmationId = nil }
            let turnIndex = index ?? knownTurnCount
            if let turn, !retryTerminal {
                lastTurnRef = turn.turnRef
                if turnIndex == canonicalTurns.count {
                    canonicalTurns.append(turn)
                    canonicalProjectionRevision = UUID()
                    currentDisplayIndices?.append(turnIndex)
                }
                // Authoritative full record includes the prompt even if queued was not replayed.
                messages.append(contentsOf: ChatMessage.from(turn: turn, turnIndex: turnIndex))
            }
            knownTurnCount = max(knownTurnCount, turnIndex + 1)
        }

        if retryTerminal && !archived && !discarded {
            retryStatusNotice = L("最近一次尝试：") + ChatMessage.statusLabel(env.str("status"))
            recoverHistoryProjection(sessionId: sessionId)
        }

        guard let client else { return }
        if !retryTerminal && !discarded && !archived { persistCurrentHistory(sessionId: sessionId, client: client) }
        let epoch = selectionEpoch
        Task {
            await loadSessions()
            guard isCurrent(sessionId, epoch, client) else { return }
            let fetchedDeck = try? await client.deck(sessionId: sessionId)
            guard isCurrent(sessionId, epoch, client) else { return }
            deck = fetchedDeck
        }
    }

    /// 直播步骤流投影：归约的 preview.steps → 展示行（含结果详情与正确的成败标记;
    /// pending = 生成中预告,执行前就知道要调什么——Phase 3 粒度）。
    private func renderSteps() {
        liveStepItems = live.preview.steps
        liveSteps = live.preview.steps.map { step in
            let mark = step.kind == "plan" ? "📋" : "⚙️"
            switch step.status {
            case "pending":
                return "⏳ \(step.label)…"
            case "running":
                let d = step.detail.isEmpty ? "" : "（\(step.detail.prefix(60))）"
                return "\(mark) \(step.label)\(d)"
            case "ok":
                let head = step.detail.split(separator: "\n").first.map(String.init) ?? ""
                return "✓ \(step.label)" + (head.isEmpty ? "" : " — \(head.prefix(80))")
            case "timeout":
                return "✗ \(step.label) " + L("超时")
            case "failed":
                return "✗ \(step.label)" + (step.detail.isEmpty ? "" : " — \(step.detail.prefix(80))")
            default:
                return "• \(step.label)"
            }
        }
        // 活动行：执行中的工具优先,其次是生成中预告的工具(无则清空,ChatView 回退通用文案)
        if let running = live.preview.steps.last(where: { $0.status == "running" }) {
            activity = LiveActivity(phase: "running", current: running.label +
                (running.detail.isEmpty ? "" : "（\(running.detail.prefix(40))）"), steps: nil)
        } else if let pending = live.preview.steps.last(where: { $0.status == "pending" }) {
            activity = LiveActivity(phase: "running",
                                    current: L("准备调用") + " \(pending.label)…", steps: nil)
        } else {
            activity = nil
        }
    }

    private func resetPreview() {
        streamText = ""
        thinkingText = ""
        streamBuffer = ""
        thinkingBuffer = ""
        liveSteps = []
        liveStepItems = []
        approvalOutcome = nil
        activity = nil
        taskSnapshot = nil
    }

    // MARK: - 发送暂存（outbox）

    private func outboxAll() -> [String: [String: String]] {
        guard let raw = UserDefaults.standard.dictionary(forKey: outboxKey) else { return [:] }
        var out: [String: [String: String]] = [:]
        for (sid, v) in raw {
            if let d = v as? [String: String] { out[sid] = d }
            else if let s = v as? String { out[sid] = ["prompt": s] }  // 旧格式(纯文本)迁移
        }
        return out
    }

    private func outboxWrite(sessionId: String, prompt: String, key: String? = nil,
                             operation: String = "send", sourceRef: String? = nil) {
        var all = outboxAll()
        var entry = ["prompt": prompt, "receipt": UUID().uuidString, "operation": operation]
        if let sourceRef { entry["source_ref"] = sourceRef; entry["retry_uncertain"] = "1" }
        if let key { entry["key"] = key }
        all[sessionId] = entry
        UserDefaults.standard.set(all, forKey: outboxKey)
    }

    private func outboxRead(sessionId: String) -> (prompt: String, key: String?, hinted: Bool)? {
        guard let e = outboxAll()[sessionId], let p = e["prompt"] else { return nil }
        return (p, e["key"], e["hinted"] == "1")
    }

    private func outboxMarkHinted(sessionId: String) {
        var all = outboxAll()
        guard var e = all[sessionId] else { return }
        e["hinted"] = "1"
        all[sessionId] = e
        UserDefaults.standard.set(all, forKey: outboxKey)
    }

    /// 提交幂等键（B3）:同会话同文重发 = 上一次提交的重试,复用旧键让服务端判重;
    /// 换了内容才换键。蜂窝网下「请求已达、202 回程丢失」是常态,不复用键的重发
    /// 就是一次货真价实的重复执行(可能带审批副作用)。
    private func outboxKeyFor(sessionId: String, prompt: String) -> String {
        if outboxAll()[sessionId]?["operation"] != "retry",
           let pending = outboxRead(sessionId: sessionId), pending.prompt == prompt,
           let k = pending.key { return k }
        return UUID().uuidString.lowercased()
    }

    private func outboxClear(sessionId: String) {
        var all = outboxAll()
        if all.removeValue(forKey: sessionId) != nil {
            UserDefaults.standard.set(all, forKey: outboxKey)
        }
    }

    // MARK: - 后台通知（自发轮次 + 全局监听两条路径，共用去重）

    /// 前后台切换入口（RootView 从 scenePhase 调）。监听循环常开（前台喂任务中心 UI），
    /// 只有「后台完结→通知」这半边随前后台开关。iOS 转后台数秒即被系统挂起，
    /// 循环自然停摆——iOS 通知只覆盖挂起前完结的轮次。
    func setAppActive(_ active: Bool) {
        isAppActive = active
    }

    /// 常驻监听（Phase 3b:全局 feed 驱动,替代 6s 差分轮询）:
    /// ① 生命周期事件到达即刷新任务中心快照(/tasks 仍是秒数等数据的权威,feed 是门铃);
    /// ② 后台时通知不再靠"消失即完结"的猜测——turn_ended/awaiting_approval 事件直达;
    /// ③ 30s 慢对账兜底(断线/漏事件自愈);feed 断开静默退避重连。
    private func startGlobalMonitor() {
        monitorTask?.cancel()
        monitorTask = Task { [weak self] in
            guard let client = self?.client else { return }

            // 慢对账兜底
            let fallback = Task { [weak self] in
                while !Task.isCancelled {
                    await self?.refreshGlobalSnapshot()
                    try? await Task.sleep(nanoseconds: 30_000_000_000)
                }
            }
            defer { fallback.cancel() }

            var backoff: UInt64 = 1_000_000_000
            var cursor = 0
            var generation: String? = nil
            while !Task.isCancelled {
                do {
                    var reanchor = false
                    for try await item in try client.attachGlobalFeed(after: cursor) {
                        backoff = 1_000_000_000
                        if item.type == "stream_attached" {
                            // 代际围栏（B2，PROTOCOL §2.7）：feed_seq 纯内存、后端重启归零,
                            // 旧游标不清零的话新代事件全撞上 `<= cursor` 被静默丢弃——
                            // 通知(轮次完结/待审批)真丢。代际变化 = 游标作废,清零重连;
                            // 重启后环里只有新代事件,after=0 重放不会重复通知。
                            // 会话事件流不适用(其 seq 有水位余量,跨重启单调)。
                            let gen = item.str("generation")
                            if !gen.isEmpty {
                                if let known = generation, known != gen {
                                    generation = gen
                                    cursor = 0
                                    reanchor = true
                                    break
                                }
                                generation = gen
                            }
                            continue
                        }
                        if item.feedSeq > 0 {
                            if item.feedSeq <= cursor { continue }
                            cursor = item.feedSeq
                        }
                        await self?.refreshGlobalSnapshot()
                        await self?.handleFeedLifecycle(item)
                    }
                    if reanchor {
                        // 重锚限速(首审 M8):两个活后端轮流应答/崩溃循环时,每次首帧都
                        // 触发重锚——不加档就是无上限热重连环。1s 足以压到无害。
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                        continue // 带新游标(0)重连,不是正常收流
                    }
                    return // 服务端正常收流
                } catch {
                    if Task.isCancelled { return }
                    try? await Task.sleep(nanoseconds: backoff)
                    backoff = min(backoff * 2, 15_000_000_000)
                }
            }
        }
    }

    /// 任务中心快照刷新(/tasks 仍是秒数等数据的权威;feed 只当"该刷新了"的门铃)。
    func refreshGlobalSnapshot() async {
        guard let client else { return }
        async let scheduled: Void = refreshSchedules(client: client)
        let request = UUID()
        globalSnapshotRequest = request
        do {
            let snap = try await client.globalTasks()
            guard self.client === client, globalSnapshotRequest == request else { return }
            if snap.status {
                globalTasks = snap.tasks; globalTasksFreshness = .fresh
            } else {
                globalTasksFreshness = globalTasksFreshness == .unknown ? .unknown : .stale
            }
        } catch {
            guard self.client === client, globalSnapshotRequest == request else { return }
            globalTasksFreshness = globalTasksFreshness == .unknown ? .unknown : .stale
        }
        await scheduled
    }

    /// Authoritative schedule snapshots share the feed + existing 30s fallback.
    func refreshSchedules() async {
        guard let client else { return }
        await refreshSchedules(client: client)
    }

    private func refreshSchedules(client: AgentClient) async {
        guard self.client === client else { return }
        let request = UUID(); schedulesRequest = request; schedulesLoading = true
        defer { if self.client === client, schedulesRequest == request { schedulesLoading = false } }
        do {
            let fetched = try await client.listSchedules()
            guard self.client === client, schedulesRequest == request else { return }
            schedules = fetched; schedulesFreshness = .fresh; schedulesError = nil
            let liveIDs = Set(fetched.map(\.id))
            scheduleRunHistory = scheduleRunHistory.filter { liveIDs.contains($0.key) }
            scheduleRunsFreshness = scheduleRunsFreshness.filter { liveIDs.contains($0.key) }
            scheduleRunsError = scheduleRunsError.filter { liveIDs.contains($0.key) }
            scheduleRunsRequests = scheduleRunsRequests.filter { liveIDs.contains($0.key) }
            for id in scheduleRunHistory.keys.sorted() {
                guard self.client === client, schedulesRequest == request else { return }
                await refreshScheduleRuns(id)
            }
        } catch {
            guard self.client === client, schedulesRequest == request else { return }
            schedulesFreshness = schedulesFreshness == .unknown ? .unknown : .stale
            schedulesError = L("定时任务更新失败，最近记录可能已过时。")
        }
    }

    enum ScheduleMutation { case enabled(Bool), runNow, cancelRun, delete }
    func canMutateSchedule(_ id: String) -> Bool {
        schedulesFreshness == .fresh && schedules.contains { $0.id == id } &&
            !isSessionDeleting("schedule_" + id) && !hasSubmissionInFlight("schedule_" + id)
    }
    func mutateSchedule(_ id: String, mutation: ScheduleMutation) async throws -> ScheduleActionResponse {
        guard let client else { throw AgentError.http(0, L("连接状态未知，请刷新后重试。")) }
        guard canMutateSchedule(id) else {
            let reason = isSessionDeleting("schedule_" + id)
                ? L("会话正在删除，请等待删除结果后再操作。")
                : L("定时任务状态尚未确认或正在提交请求，请刷新后重试。")
            throw AgentError.http(409, reason)
        }
        let receipt = UUID().uuidString
        submissionHTTPRequests[receipt] = SubmissionHTTP(sessionId: "schedule_" + id, client: ObjectIdentifier(client))
        defer { submissionHTTPRequests[receipt] = nil }
        switch mutation {
        case .enabled(let enabled): return try await client.toggleSchedule(id: id, enabled: enabled)
        case .runNow: return try await client.runScheduleNow(id: id)
        case .cancelRun: return try await client.cancelScheduleRun(id: id)
        case .delete: return try await client.deleteSchedule(id: id)
        }
    }

    func refreshScheduleRuns(_ id: String) async {
        guard let client else { return }
        let request = UUID(); scheduleRunsRequests[id] = request
        do {
            let fetched = try await client.scheduleRuns(id: id)
            guard self.client === client, scheduleRunsRequests[id] == request else { return }
            scheduleRunHistory[id] = fetched; scheduleRunsFreshness[id] = .fresh; scheduleRunsError[id] = nil
        } catch {
            guard self.client === client, scheduleRunsRequests[id] == request else { return }
            scheduleRunsFreshness[id] = scheduleRunsFreshness[id] == .fresh || scheduleRunsFreshness[id] == .stale ? .stale : .unknown
            scheduleRunsError[id] = L("运行历史更新失败，最近记录可能已过时。")
        }
    }

    /// feed 生命周期事件 → 通知路由。平台分流（防双横幅）：
    /// - macOS：本地通知（postLocal，零出网）——进程常驻（菜单栏），feed 全程可达；
    /// - iOS：横幅由后端 APNs 远程投递（挂起后 feed 停摆，本地不再发），这里只负责
    ///   Live Activity 的起停钩子（起活动要求前台，正好是 feed 活着的窗口）。
    private func handleFeedLifecycle(_ item: FeedItem) async {
        guard let sid = item.sessionId else { return }
        let turnId = item.turnId ?? ""

        switch item.type {
        case "turn_queued":
            feedTurnMeta[turnId] = (origin: item.str("origin"), prompt: item.str("prompt"))
            if feedTurnMeta.count > 128 { feedTurnMeta.removeAll() }   // 容量护栏（量级极小）
        case "turn_started":
            let meta = feedTurnMeta[turnId]
            #if os(iOS)
            pushManager?.activityTurnStarted(
                sessionId: sid, turnId: turnId,
                title: notifTitle(sid, fallback: meta?.prompt ?? ""))
            #else
            if meta?.origin == "schedule", !isAppActive, shouldNotify(sid + "#start") {
                pushManager?.postLocal(kind: .scheduleStart, sessionId: sid, turnId: turnId,
                                       sessionTitle: notifTitle(sid, fallback: meta?.prompt ?? ""))
            }
            #endif
        case "awaiting_approval":
            #if os(macOS)
            guard !isAppActive, shouldNotify(sid) else { return }
            let origin = feedTurnMeta[turnId]?.origin ?? "chat"
            let title = notifTitle(sid, fallback: feedTurnMeta[turnId]?.prompt ?? "")
            if origin == "schedule" {
                // 无人值守语义：runner 紧接着自动拒绝——推通报，不推互动审批卡
                pushManager?.postLocal(kind: .approvalAutodenied, sessionId: sid,
                                       turnId: turnId, sessionTitle: title)
            } else {
                pushManager?.postLocal(kind: .approval(tools: item.toolNames()),
                                       sessionId: sid, turnId: turnId, sessionTitle: title)
            }
            #endif
        case "turn_ended":
            let status = item.str("status")
            #if os(iOS)
            pushManager?.activityTurnEnded(turnId: turnId, status: status)
            #else
            // 用户自己取消的不通知
            if !isAppActive, status != "cancelled", !status.isEmpty {
                await notifyFinishedSession(sid, turnId: turnId, status: status)
            }
            #endif
            feedTurnMeta[turnId] = nil
        default:
            break
        }
    }

    /// 通知标题：会话名 → 轮次 prompt 头 → 会话 id。
    private func notifTitle(_ sessionId: String, fallback: String) -> String {
        if let t = sessions.first(where: { $0.sessionId == sessionId })?.title, !t.isEmpty {
            return t
        }
        let p = fallback.replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespaces)
        return p.isEmpty ? sessionId : String(p.prefix(24))
    }

    #if os(macOS)
    /// 全局监听发现某会话完结：取末轮助手正文做 full 档正文（分级在 postLocal 内应用）。
    private func notifyFinishedSession(_ sessionId: String, turnId: String,
                                       status: String) async {
        guard shouldNotify(sessionId) else { return }
        var fullBody: String?
        if status != "fail",
           let history = try? await client?.history(sessionId: sessionId),
           let last = history.turns.last {
            let text = (last.response ?? [])
                .filter { $0.role == "assistant" }
                .compactMap(\.content).joined()
            if !text.isEmpty { fullBody = String(text.prefix(90)) }
        }
        pushManager?.postLocal(kind: .result(status: status), sessionId: sessionId,
                               turnId: turnId,
                               sessionTitle: notifTitle(sessionId, fallback: ""),
                               fullBody: fullBody)
    }
    #endif

    private func shouldNotify(_ key: String) -> Bool {
        if let last = notifiedAt[key], Date().timeIntervalSince(last) < 30 { return false }
        notifiedAt[key] = Date()
        return true
    }

    /// Only canonical session input locations provide a retained-file identity.
    /// A display name or an arbitrary absolute path is never a substitute for provenance.
    private static func canonicalInputReference(_ path: String, sessionId: String) -> OfflineInputReference? {
        let decoded = path.removingPercentEncoding ?? path
        guard !decoded.contains("://"), !decoded.contains("\\") else { return nil }
        let parts = decoded.split(separator: "/").map(String.init)
        guard !parts.contains(".."), !parts.contains("."), parts.count >= 4 else { return nil }
        let isStorage = parts[parts.count - 4] == "sessions" && parts[parts.count - 3] == sessionId
        let isAPI = path.hasPrefix("/api/v1/files/") && parts.count >= 6 && parts[3] == sessionId &&
            (parts.count == 6 || (parts.count == 7 && parts[4] == "offline"))
        guard isStorage || isAPI else { return nil }
        return OfflineInputReference(kind: parts[parts.count - 2], path: parts.last!)
    }
    private static func inputReferences(in turns: [Turn], sessionId: String) -> Set<OfflineInputReference> {
        var retained = Set<OfflineInputReference>()
        for turn in turns {
            for path in turn.image ?? [] {
                if let ref = canonicalInputReference(path, sessionId: sessionId), ref.kind == "image" { retained.insert(ref) }
            }
            for path in turn.audio ?? [] {
                if let ref = canonicalInputReference(path, sessionId: sessionId), ref.kind == "audio" { retained.insert(ref) }
            }
            for item in turn.attachments ?? [] {
                guard let path = item.path, let ref = canonicalInputReference(path, sessionId: sessionId) else { continue }
                if let type = item.type?.lowercased().split(separator: "/").first.map(String.init) {
                    let expected = ["audio", "image"].contains(type) ? type : "attach"
                    guard ref.kind == expected else { continue }
                }
                retained.insert(ref)
            }
        }
        return retained
    }

    /// 编辑并重发：取回上一条用户输入 → 删除最后一轮（后端回退记忆/KV）→ 回填输入框。
    func editLastTurn() {
        guard let client, let sid = selectedSessionId else { return }
        guard !rejectDeletingSession(sid), let source = editableAttemptTarget else { return }
        let epoch = selectionEpoch
        let retainedInputs = Self.inputReferences(in: source.pausedTurnId == nil ? Array(canonicalTurns.dropLast()) : canonicalTurns, sessionId: sid)
        Task {
            // The captured queued/canonical source must still be the target before sending DELETE.
            guard isCurrent(sid, epoch, client), !rejectDeletingSession(sid), editableAttemptTarget == source else { return }
            let res = try? await client.deleteLastTurn(sessionId: sid)
            if self.client === client, res?.status == true { invalidateSessionIndexRequests() }
            guard isCurrent(sid, epoch, client), ownsEditReply(source) else { return }
            if res?.status != true {
                errorMessage = res?.response ?? L("回退失败")
                return
            }
            invalidateSessionIndexRequests()
            try? await client.offlineAssets.invalidateInputs(retaining: retainedInputs, namespace: client.cacheNamespace, sid: sid)
            try? await offlineHistory.invalidateHistory(sessionId: sid, namespace: client.cacheNamespace)
            guard isCurrent(sid, epoch, client), ownsEditReply(source) else { return }
            offlineSavedSessionIDs.remove(sid)
            detach(); clearHistoryProjection(); messages = []; knownTurnCount = 0
            let recoveredEpoch = selectionEpoch
            await open(sessionId: sid)
            guard isCurrent(sid, recoveredEpoch, client) else { return }
            await loadSessions()
            guard isCurrent(sid, recoveredEpoch, client) else { return }
            // These are raw inputs, not decorated user bubbles; literal newline/📎 is user content.
            composerPrefill = source.prompt
        }
    }

    // MARK: - 分支 / 回退

    /// 从指定落盘轮次分支出新会话并切换过去（后端复制 [0, index] 并尽量热复用 KV）。
    func branch(fromTurnIndex index: Int) {
        guard !rejectReadOnlyHistory() else { return }
        guard let client, let sid = selectedSessionId else { return }
        guard !rejectDeletingSession(sid) else { return }
        let epoch = selectionEpoch
        Task {
            guard isCurrent(sid, epoch, client), !rejectDeletingSession(sid) else { return }
            do {
                let res = try await client.branchSession(sessionId: sid, nodeIndex: index)
                if self.client === client, res.status, res.sessionId != nil { invalidateSessionIndexRequests() }
                guard isCurrent(sid, epoch, client) else { return }
                guard res.status, let newId = res.sessionId else {
                    errorMessage = res.response ?? L("分支失败")
                    return
                }
                invalidateSessionIndexRequests()
                await loadSessions()
                guard isCurrent(sid, epoch, client) else { return }
                select(sessionId: newId)
            } catch {
                guard isCurrent(sid, epoch, client) else { return }
                errorMessage = L("分支失败：") + error.localizedDescription
            }
        }
    }

    /// 删除最后一轮问答（后端同步回退内存记忆与 KV），完成后重拉历史对账。
    func deleteLastTurn() {
        guard !rejectReadOnlyHistory() else { return }
        guard let client, let sid = selectedSessionId, !isGenerating else { return }
        guard !rejectDeletingSession(sid) else { return }
        let epoch = selectionEpoch
        let paused = ledgerPhase == "awaiting_approval" || ledgerPhase == "suspended"
        let retainedInputs = Self.inputReferences(in: paused ? canonicalTurns : Array(canonicalTurns.dropLast()), sessionId: sid)
        Task {
            guard isCurrent(sid, epoch, client), !rejectDeletingSession(sid) else { return }
            do {
                let res = try await client.deleteLastTurn(sessionId: sid)
                if self.client === client, res.status { invalidateSessionIndexRequests() }
                guard isCurrent(sid, epoch, client) else { return }
                if !res.status {
                    errorMessage = res.response ?? "回退失败"
                    return
                }
                invalidateSessionIndexRequests()
                try? await client.offlineAssets.invalidateInputs(retaining: retainedInputs, namespace: client.cacheNamespace, sid: sid)
                try? await offlineHistory.invalidateHistory(sessionId: sid, namespace: client.cacheNamespace)
                guard isCurrent(sid, epoch, client) else { return }
                offlineSavedSessionIDs.remove(sid)
                detach(); clearHistoryProjection(); messages = []; knownTurnCount = 0
                await open(sessionId: sid)
                await loadSessions()
            } catch {
                guard isCurrent(sid, epoch, client) else { return }
                errorMessage = L("回退失败：") + error.localizedDescription
            }
        }
    }

    // MARK: - 流式节流

    // token 逐字触发 MarkdownUI 全文重排太贵（长回复掉帧），
    // 先攒缓冲，约 90ms 合并刷一次 @Published。
    private var streamBuffer = ""
    private var thinkingBuffer = ""
    private var flushScheduled = false

    private func scheduleStreamFlush() {
        guard !flushScheduled else { return }
        flushScheduled = true
        let epoch = selectionEpoch
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 90_000_000)
            guard let self, self.selectionEpoch == epoch else { return }
            self.flushScheduled = false
            self.flushStreamNow()
        }
    }

    private func flushStreamNow() {
        if !streamBuffer.isEmpty {
            streamText += streamBuffer
            streamBuffer = ""
        }
        if !thinkingBuffer.isEmpty {
            thinkingText += thinkingBuffer
            thinkingBuffer = ""
        }
    }

    // MARK: - 内容搜索（后端暂无搜索端点：客户端拉 history 全文扫，缓存按 last_updated 失效）

    func runSearch() async {
        let query = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty, let client else {
            searchResults = nil
            return
        }
        isSearching = true
        defer { isSearching = false }

        if sessions.isEmpty { await loadSessions() }
        let targets = sessions
        var hits: [SearchHit] = []

        let cache = offlineHistory
        await withTaskGroup(of: SearchHit?.self) { group in
            var pending = targets.makeIterator()
            func enqueue() {
                guard let item = pending.next() else { return }
                group.addTask {
                    // Only a copy saved under this exact index version may answer a
                    // search; anything older could quote deleted or superseded text.
                    let cached = await cache.loadHistory(sessionId: item.sessionId, namespace: client.cacheNamespace,
                        matchingVersion: OfflineHistoryStore.Version(item))
                    let history: HistoryResponse?
                    if let cached { history = cached }
                    else { history = try? await client.history(sessionId: item.sessionId) }
                    guard let history else { return nil }
                    return await Self.match(query: query, in: history.turns, session: item)
                }
            }
            enqueue(); enqueue()
            for await hit in group { if let hit { hits.append(hit) }; enqueue() }
        }
        guard self.client === client, searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == query else { return }
        // 稳定排序：最近更新的会话在前
        hits.sort { ($0.lastUpdated ?? 0) > ($1.lastUpdated ?? 0) }
        searchResults = hits
    }

    /// 在一个会话里找第一处命中（用户输入 + 助手正文），产出 ±40 字符摘录。
    private static func match(query: String, in turns: [Turn],
                              session: SessionMetaItem) -> SearchHit? {
        for turn in turns {
            var texts: [String] = []
            if let prompt = turn.prompt { texts.append(prompt) }
            for msg in turn.response ?? [] where msg.role == "assistant" || msg.role == "system" {
                if let content = msg.content { texts.append(content) }
            }
            for text in texts {
                let lower = text.lowercased()
                guard let range = lower.range(of: query) else { continue }
                let start = lower.index(range.lowerBound, offsetBy: -40,
                                        limitedBy: lower.startIndex) ?? lower.startIndex
                let end = lower.index(range.upperBound, offsetBy: 40,
                                      limitedBy: lower.endIndex) ?? lower.endIndex
                let snippet = String(text[start..<end])
                    .replacingOccurrences(of: "\n", with: " ")
                return SearchHit(sessionId: session.sessionId,
                                 title: session.title?.isEmpty == false ? session.title! : session.sessionId,
                                 snippet: "…\(snippet)…",
                                 lastUpdated: session.lastUpdated)
            }
        }
        return nil
    }

    func clearSearch() {
        searchQuery = ""
        searchResults = nil
    }

    // MARK: - 工具

    /// 会话 ID：满足后端白名单（首字符字母数字，其余 [A-Za-z0-9._-]）。
    private static func makeSessionId() -> String {
        "app-" + String(UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: "").prefix(12))
    }
}
