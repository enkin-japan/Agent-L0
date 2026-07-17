import Foundation
import UserNotifications

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
    // 会话
    @Published var sessions: [SessionMetaItem] = []
    /// 剧本清单（斜杠候选提示）：随会话列表一并加载，含缺依赖者（候选里灰显）
    @Published var skills: [SkillItem] = []
    /// 持久化选中会话：杀进程/重启后自动回到原直播视图（铁律 4 的 ≤1s 恢复语义，
    /// web 靠刷新同 URL 天然满足，native 必须显式记住）。draft（未落盘）id 也会
    /// 存进去，恢复时经 restoreLastSession 的存在性校验过滤。
    @Published var selectedSessionId: String? {
        didSet {
            let ud = UserDefaults.standard
            if let id = selectedSessionId { ud.set(id, forKey: "session.lastSelected") }
            else { ud.removeObject(forKey: "session.lastSelected") }
        }
    }
    @Published var messages: [ChatMessage] = []
    // 活跃轮直播态（预览;turn_ended 权威对账后清空）
    @Published var isGenerating = false
    @Published var streamText = ""
    @Published var thinkingText = ""
    @Published var liveSteps: [String] = []
    @Published var activity: LiveActivity?
    @Published var taskSnapshot: TaskSnapshot?
    // 暂停态
    @Published var pendingTools: [PendingTool]?
    @Published var isSuspended = false
    // 错误横幅（自动消失的 toast 语义）
    @Published var errorMessage: String? {
        didSet {
            errorDismissTask?.cancel()
            guard errorMessage != nil else { return }
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
    // 内容搜索
    @Published var searchQuery = ""
    @Published var searchResults: [SearchHit]?
    @Published var isSearching = false
    /// App 是否前台活跃（RootView 从 scenePhase 同步）：后台完结的轮次发本地通知
    var isAppActive = true
    private var errorDismissTask: Task<Void, Never>?
    /// 会话历史缓存（内容搜索用）：last_updated 不变即命中
    private var historyCache: [String: (updated: Double, turns: [Turn])] = [:]

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
    /// 通知去重：同一会话 30s 内只报一次（自发轮次与全局监听两条路径都会命中完结）。
    private var notifiedAt: [String: Date] = [:]

    // ---- 附着流簿记（单附着策略：只对选中会话保持一条 /events 长连） ----
    private var attachTask: Task<Void, Never>?
    /// 已消费的最大账本 seq（重连游标；换会话时归零重定位）。
    private var cursor = 0
    /// 归约器状态（活跃轮的规范形投影;fixtures 锁定其行为）。
    private var live = NormalizedState()
    /// 本端乐观追加的用户气泡（turn_queued 到达时收编,不重复建）。
    private var optimisticPrompt: String?
    /// 发送暂存（outbox）：提交前落 UserDefaults,202 受理即清——App 在 multipart
    /// 上传途中被杀时,这是唯一能证明"有话没送达"的凭据。
    private let outboxKey = "macagent.outbox"
    /// 会话列表快照（冷启空窗填充）：sidecar 拉起前先摆上次的列表，服务端真相
    /// 到达即替换（loadSessions 的等值守卫兜底——不变则不闪）。
    private let sessionsSnapshotKey = "macagent.sessions.snapshot"

    /// 快照按后端地址隔离：连远程时闪出本机的会话列表是**错误信息**，
    /// 比空列表更糟；地址对不上宁可空着。
    private struct SessionsSnapshot: Codable {
        let backendURL: String
        let items: [SessionMetaItem]
    }

    init(settings: AppSettings) {
        self.settings = settings
        self.client = settings.makeClient()
        restoreSessionsSnapshot()
        startGlobalMonitor()
        startConnectionMonitor()
    }

    /// 设置变更后重建客户端并重新对账（选中会话重新附着）。
    func applySettings() {
        client = settings.makeClient()
        startGlobalMonitor()       // client 换了，监听循环重启
        startConnectionMonitor()   // 连接探活同理重启（失败计数清零 → 黄灯重试）
        // 可能换了后端：旧列表立即撤下，摆新地址的快照（没有就空着等真相）
        sessions = []
        restoreSessionsSnapshot()
        let sid = selectedSessionId
        detach()
        messages = []
        Task {
            await refreshConnection()
            await loadSessions()
            if let sid { await open(sessionId: sid) }
        }
    }

    func refreshConnection() async {
        guard let client else { connectionOK = false; connState = .offline; return }
        let ok = (try? await client.health()) ?? false
        connectionOK = ok
        if ok { connState = .connected; connFailures = 0 }
    }

    // MARK: - 连接状态监控（实机反馈：信号原来只在设置页关闭时刷一次，静态且滞后）

    /// 三态：连接中（黄，还在尝试）/ 已连接（绿）/ 未连接（红，连续失败 ~20s 判放弃）。
    enum ConnState { case connecting, connected, offline }

    /// 常驻探活循环：通了 15s 一巡（廉价 /health），断了 2s 一巡追赶 sidecar 启动；
    /// 连续 10 次失败（约 20s）才降级红灯——短暂波动/启动窗口都算「连接中」。
    func startConnectionMonitor() {
        connMonitorTask?.cancel()
        connFailures = 0
        if connState != .connected { connState = .connecting }
        connMonitorTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let ok = (try? await self.client?.health()) ?? false
                if Task.isCancelled { return }
                self.connectionOK = ok
                if ok {
                    self.connFailures = 0
                    self.connState = .connected
                    // 顺带巡刷会话列表（macOS 无下拉刷新后的自动更新通道）：
                    // 等值守卫兜底，列表没变不会引起任何视图重建
                    await self.loadSessions()
                } else {
                    self.connFailures += 1
                    self.connState = self.connFailures >= 10 ? .offline : .connecting
                }
                try? await Task.sleep(nanoseconds: ok ? 15_000_000_000 : 2_000_000_000)
            }
        }
    }

    // MARK: - 会话管理

    func loadSessions() async {
        guard let client else { return }
        do {
            var items = try await client.listSessions()
            items.sort {
                if ($0.pinned ?? false) != ($1.pinned ?? false) { return $0.pinned ?? false }
                return ($0.lastUpdated ?? 0) > ($1.lastUpdated ?? 0)
            }
            // 等值守卫（实机反馈）：下拉刷新常常拿回一模一样的列表，无脑重赋值会让
            // List 整体重建——刷新控件收起动画与内容重建打架，表现为瞬移回顶+双重残影
            if items != sessions {
                sessions = items
                saveSessionsSnapshot()   // 只有真变了才落快照（探活 15s 一巡，别空写）
            }
        } catch {
            errorMessage = L("会话列表加载失败：") + error.localizedDescription
        }
        // 剧本清单搭车刷新：变动频率极低（装依赖/加剧本），无须独立轮询；
        // 拉不到就保持旧值——候选提示是锦上添花，不值得为它弹错误横幅
        if let fetched = try? await client.skills() {
            skills = fetched
        }
    }

    /// 读上次的列表快照（仅当后端地址一致）。摆的是元数据（标题/轮数/时间戳），
    /// 点击行为照常：连接建立前拉历史会失败弹横幅，几秒窗口，不值得禁点。
    private func restoreSessionsSnapshot() {
        guard let data = UserDefaults.standard.data(forKey: sessionsSnapshotKey),
              let snap = try? JSONDecoder().decode(SessionsSnapshot.self, from: data),
              snap.backendURL == settings.baseURLString else { return }
        sessions = snap.items
    }

    private func saveSessionsSnapshot() {
        let snap = SessionsSnapshot(backendURL: settings.baseURLString, items: sessions)
        if let data = try? JSONEncoder().encode(snap) {
            UserDefaults.standard.set(data, forKey: sessionsSnapshotKey)
        }
    }

    func newSession() {
        detach()
        selectedSessionId = Self.makeSessionId()
        messages = []
        knownTurnCount = 0
    }

    /// 启动恢复：回到上次的会话（仅当它仍存在于会话列表——draft/已删除的过滤掉）。
    /// 在 loadSessions 之后调用；已有选中（深链等先到）则不抢。
    func restoreLastSession() {
        guard selectedSessionId == nil,
              let last = UserDefaults.standard.string(forKey: "session.lastSelected"),
              sessions.contains(where: { $0.sessionId == last }) else { return }
        select(sessionId: last)
    }

    func select(sessionId: String) {
        guard sessionId != selectedSessionId else { return }
        detach()
        selectedSessionId = sessionId
        messages = []
        Task { await open(sessionId: sessionId) }
    }

    /// 统一恢复入口：历史（落盘轮）→ outbox 回收提示 → 附着（活跃轮重放 + 实时续播）。
    private func open(sessionId: String) async {
        await loadHistory(sessionId: sessionId)
        guard selectedSessionId == sessionId else { return }
        // 发送暂存回收：上次 App 可能死在 multipart 上传途中——凭据仅剩 outbox
        if let pending = outboxRead(sessionId: sessionId) {
            outboxClear(sessionId: sessionId)
            let lastPrompt = messages.last(where: { $0.kind == .user })?.text
            if lastPrompt != pending {
                messages.append(.system(L("上次可能有一条消息未送达：「") + pending + L("」——请确认后按需重发。")))
            }
        }
        ensureAttached(sessionId: sessionId)
    }

    private func loadHistory(sessionId: String) async {
        guard let client else { return }
        do {
            let history = try await client.history(sessionId: sessionId)
            guard selectedSessionId == sessionId else { return }
            messages = history.turns.enumerated().flatMap { index, turn in
                ChatMessage.from(turn: turn, turnIndex: index)
            }
            knownTurnCount = history.turns.count
            // deck 探测（404=无，非错误）：有则聊天工具栏出现查看入口
            deck = try? await client.deck(sessionId: sessionId)
        } catch {
            errorMessage = L("历史加载失败：") + error.localizedDescription
        }
    }

    func deleteSession(_ item: SessionMetaItem) {
        Task {
            _ = try? await client?.deleteSession(sessionId: item.sessionId)
            if selectedSessionId == item.sessionId {
                detach()
                selectedSessionId = nil
                messages = []
            }
            await loadSessions()
        }
    }

    func togglePin(_ item: SessionMetaItem) {
        Task {
            _ = try? await client?.setPin(sessionId: item.sessionId, pinned: !(item.pinned ?? false))
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
        Task {
            _ = try? await client?.renameSession(sessionId: item.sessionId, title: trimmed)
            await loadSessions()
        }
    }

    // MARK: - 发消息（202 受理 + 事件流）

    func send(prompt: String, uploads: [Upload], thinking: Bool) {
        guard !isGenerating, let client else { return }
        let sid: String
        if let existing = selectedSessionId {
            sid = existing
        } else {
            sid = Self.makeSessionId()
            selectedSessionId = sid
        }
        // 首轮自动命名（实机反馈：新会话在侧栏顶着随机 id 很生硬）——用首条提问的
        // 首行截断当标题，写到服务端 meta（双端同源），用户随时可改名覆盖。
        let firstTurn = knownTurnCount == 0
            && !messages.contains(where: { $0.kind == .user || $0.kind == .assistant })

        var bubble = prompt
        if !uploads.isEmpty {
            bubble += "\n📎 " + uploads.map(\.filename).joined(separator: "、")
        }
        let userMessage = ChatMessage.user(bubble)
        let bubbleId = userMessage.id
        messages.append(userMessage)
        optimisticPrompt = prompt
        outboxWrite(sessionId: sid, prompt: prompt)
        resetPreview()
        isGenerating = true

        var params = ChatParams(prompt: prompt)
        params.enableThinking = thinking
        Task { [weak self] in
            do {
                _ = try await client.submitTurn(sessionId: sid, params: params, files: uploads)
                guard let self else { return }
                self.outboxClear(sessionId: sid)
                self.ensureAttached(sessionId: sid)  // 首条消息后会话才在服务端存在
                if firstTurn {
                    let title = Self.autoTitle(from: prompt)
                    if !title.isEmpty {
                        _ = try? await client.renameSession(sessionId: sid, title: title)
                        await self.loadSessions()
                    }
                }
            } catch let conflict as TurnConflictError {
                guard let self else { return }
                self.outboxClear(sessionId: sid)
                self.optimisticPrompt = nil
                self.messages.removeAll { $0.id == bubbleId }
                self.isGenerating = false
                _ = conflict
                self.errorMessage = L("该会话已有正在进行的轮次，请等它完结后再发送。")
            } catch {
                guard let self else { return }
                self.outboxClear(sessionId: sid)
                self.optimisticPrompt = nil
                self.messages.removeAll { $0.id == bubbleId }
                self.isGenerating = false
                self.errorMessage = L("发送失败：") + error.localizedDescription
            }
        }
    }

    // MARK: - 审批 / 任务控制

    func decideApproval(approve: Bool) {
        guard let client, let sid = selectedSessionId else { return }
        let tools = pendingTools
        pendingTools = nil
        isGenerating = true   // 乐观;事件流的 resumed/turn_started 确认
        Task { [weak self] in
            do {
                _ = try await client.submitApproval(sessionId: sid, approve: approve)
            } catch is TurnConflictError {
                // 决议目标已不存在（另一端已处理/已过期）：以服务端为准重载
                guard let self else { return }
                self.detach()
                self.messages = []
                await self.open(sessionId: sid)
            } catch {
                guard let self else { return }
                self.isGenerating = false
                self.pendingTools = tools
                self.errorMessage = L("审批提交失败：") + error.localizedDescription
            }
        }
    }

    /// 真取消：后端在最近检查点停止并整轮回滚；turn_ended(cancelled) 从事件流回来收尾。
    func cancelGeneration() {
        guard let client, let sid = selectedSessionId else { return }
        Task { _ = try? await client.cancelTask(sessionId: sid) }
    }

    func suspendTask() {
        guard let client, let sid = selectedSessionId else { return }
        Task { _ = try? await client.suspendTask(sessionId: sid) }
    }

    /// 恢复挂起任务（202 受理,续跑过程走事件流）。默认当前会话；任务中心可对任意会话恢复。
    func resumeTask(sessionId: String? = nil) {
        guard let client else { return }
        let sid: String
        if let sessionId, sessionId != selectedSessionId {
            select(sessionId: sessionId)
            sid = sessionId
        } else {
            guard let current = selectedSessionId else { return }
            sid = current
        }
        isSuspended = false
        isGenerating = true
        errorMessage = nil
        Task { [weak self] in
            do {
                _ = try await client.submitResume(sessionId: sid)
                self?.ensureAttached(sessionId: sid)
            } catch is TurnConflictError {
                // 挂起句柄已不存在（已被恢复/回收）：以服务端为准重载
                guard let self else { return }
                self.detach()
                self.messages = []
                await self.open(sessionId: sid)
            } catch {
                guard let self else { return }
                self.isGenerating = false
                self.isSuspended = true
                self.errorMessage = L("恢复提交失败：") + error.localizedDescription
            }
        }
    }

    /// 重新生成上一轮回答：取回上一条用户输入 → 回退最后一轮 → 原文自动重发。
    /// （原轮附件随回退清理，重发不含附件。）
    func regenerateLastTurn() {
        guard !isGenerating, let client, let sid = selectedSessionId,
              let lastUser = messages.last(where: { $0.kind == .user }) else { return }
        var text = lastUser.text
        if let range = text.range(of: "\n📎 ") {
            text = String(text[..<range.lowerBound])
        }
        Task {
            let res = try? await client.deleteLastTurn(sessionId: sid)
            guard res?.status == true else {
                errorMessage = res?.response ?? L("回退失败")
                return
            }
            messages = []
            await loadHistory(sessionId: sid)
            send(prompt: text, uploads: [], thinking: false)
        }
    }

    // MARK: - 事件附着（账本协议核心）

    /// 附着中的会话（与 attachTask 一起维护;循环退出后自清,可再次附着——
    /// 新会话首次附着会 404 退出,首条消息 202 受理后 send 再次调用本方法)。
    private var attachedSessionId: String?

    private func ensureAttached(sessionId: String) {
        guard !(attachedSessionId == sessionId && attachTask != nil) else { return }
        attachTask?.cancel()
        attachedSessionId = sessionId
        attachTask = Task { [weak self] in
            await self?.attachLoop(sessionId: sessionId)
            self?.attachEnded(sessionId: sessionId)
        }
    }

    /// 附着循环自然退出（404/会话删除）后的句柄自清（被 detach 取代的旧任务不越权）。
    private func attachEnded(sessionId: String) {
        if attachedSessionId == sessionId {
            attachedSessionId = nil
            attachTask = nil
        }
    }

    /// 断开附着并清空活跃轮簿记（换会话/设置变更时）。
    private func detach() {
        attachTask?.cancel()
        attachTask = nil
        attachedSessionId = nil
        cursor = 0
        live = NormalizedState()
        optimisticPrompt = nil
        isGenerating = false
        streamText = ""
        thinkingText = ""
        streamBuffer = ""
        thinkingBuffer = ""
        liveSteps = []
        activity = nil
        taskSnapshot = nil
        pendingTools = nil
        isSuspended = false
        errorMessage = nil
        deck = nil
        composerPrefill = nil
    }

    private func attachLoop(sessionId: String) async {
        guard let client else { return }
        var backoff: UInt64 = 1_000_000_000
        while !Task.isCancelled && selectedSessionId == sessionId {
            do {
                var after = cursor
                if after == 0 {
                    // 初次附着:/turn 定游标——有活跃轮从轮首整段重放(完整重建直播视图
                    // 含审批卡),无活跃轮从账本末尾只收新事件(落盘轮次由 /history 提供)。
                    let ts = try await client.turnState(sessionId: sessionId)
                    after = ts.turn.flatMap { ($0.firstSeq ?? 1) - 1 } ?? (ts.lastSeq ?? 0)
                    cursor = after
                    if ts.turn == nil {
                        isGenerating = false
                    }
                }
                for try await env in try client.attachEvents(sessionId: sessionId, after: after) {
                    backoff = 1_000_000_000
                    guard !Task.isCancelled, selectedSessionId == sessionId else { return }
                    if env.seq > 0 {
                        if env.seq <= cursor { continue }  // 重连重放的重复事件
                        cursor = env.seq
                    }
                    dispatch(env, sessionId: sessionId)
                }
                return  // 服务端正常收流（会话被删除）：结束附着
            } catch {
                if Task.isCancelled || selectedSessionId != sessionId { return }
                if case AgentError.http(404, _) = error { return }  // 会话尚不存在:首条消息受理后再附着
                // 断开是常态：静默指数退避重连（游标在,断线期间的事件重连即补）
                try? await Task.sleep(nanoseconds: backoff)
                backoff = min(backoff * 2, 10_000_000_000)
            }
        }
    }

    /// 事件分发：归约器推进活跃轮状态,再把结果投影进 UI（发布属性 + 消息流）。
    private func dispatch(_ env: EventEnvelope, sessionId: String) {
        if env.type == "stream_attached" { return }
        live = TurnReducer.reduce(live, env)
        // transcript 是夹具口径的对账字段，UI 从不读取——长期附着下随轮次无界累积
        // （每轮完整 response 含工具结果），store 层不留存。归约器保持纯函数，夹具不受影响。
        if !live.transcript.isEmpty { live.transcript.removeAll() }

        switch env.type {
        case "turn_queued":
            onTurnQueued(env)
        case "turn_started", "resumed":
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
            pendingTools = env.decode([PendingTool].self, at: "pending_tools") ?? []
        case "suspended":
            flushStreamNow()
            isGenerating = false
            isSuspended = true
        case "turn_ended":
            onTurnEnded(env, sessionId: sessionId)
        default:
            break  // 未知事件类型:忽略(向前兼容)
        }
    }

    /// 轮次受理：确保用户气泡存在——收编本端乐观气泡;其他来源（web/定时任务/别的设备）
    /// 的轮次按事件补建,本端从此实时看到别端发起的任务。
    private func onTurnQueued(_ env: EventEnvelope) {
        resetPreview()
        isGenerating = true
        if optimisticPrompt != nil {
            optimisticPrompt = nil  // 气泡已在 send 时追加
        } else {
            let prompt = env.str("prompt")
            if !prompt.isEmpty { messages.append(.user(prompt)) }
        }
    }

    /// 权威终态：撤下直播预览,按 turn_ended 的完整轮次记录对账（终态仅 4 种,
    /// 暂停态走 awaiting_approval/suspended 事件,不再经由 turn_end）。
    private func onTurnEnded(_ env: EventEnvelope, sessionId: String) {
        guard let turn = env.decode(Turn.self) else { return }
        resetPreview()
        isGenerating = false
        pendingTools = nil
        isSuspended = false

        let turnIndex: Int
        if case .number(let idx)? = env.data["turn_index"] {
            turnIndex = Int(idx)
        } else {
            turnIndex = knownTurnCount
        }
        messages.append(contentsOf: ChatMessage.from(
            turn: turn, includePrompt: false, turnIndex: turnIndex))
        knownTurnCount = turnIndex + 1

        // 后台完结的通知统一走全局 feed 监听（handleFeedLifecycle）——自发轮次那条
        // 快路径已并入，双路径 30s 去重的补丁不再需要。
        Task {
            await loadSessions()
            // 本轮可能刚生成/更新了 deck
            if let client, let sid = selectedSessionId, sid == sessionId {
                deck = try? await client.deck(sessionId: sid)
            }
        }
    }

    /// 直播步骤流投影：归约的 preview.steps → 展示行（含结果详情与正确的成败标记;
    /// pending = 生成中预告,执行前就知道要调什么——Phase 3 粒度）。
    private func renderSteps() {
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
        activity = nil
        taskSnapshot = nil
    }

    // MARK: - 发送暂存（outbox）

    private func outboxAll() -> [String: String] {
        (UserDefaults.standard.dictionary(forKey: outboxKey) as? [String: String]) ?? [:]
    }

    private func outboxWrite(sessionId: String, prompt: String) {
        var all = outboxAll()
        all[sessionId] = prompt
        UserDefaults.standard.set(all, forKey: outboxKey)
    }

    private func outboxRead(sessionId: String) -> String? {
        outboxAll()[sessionId]
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
            while !Task.isCancelled {
                do {
                    for try await item in try client.attachGlobalFeed(after: cursor) {
                        backoff = 1_000_000_000
                        if item.feedSeq > 0 {
                            if item.feedSeq <= cursor { continue }
                            cursor = item.feedSeq
                        }
                        guard item.type != "stream_attached" else { continue }
                        await self?.refreshGlobalSnapshot()
                        await self?.handleFeedLifecycle(item)
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
    private func refreshGlobalSnapshot() async {
        guard let client else { return }
        if let snap = try? await client.globalTasks() {
            globalTasks = snap.tasks
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

    /// 编辑并重发：取回上一条用户输入 → 删除最后一轮（后端回退记忆/KV）→ 回填输入框。
    func editLastTurn() {
        guard !isGenerating, let client, let sid = selectedSessionId,
              let lastUser = messages.last(where: { $0.kind == .user }) else { return }
        var text = lastUser.text
        if let range = text.range(of: "\n📎 ") {  // 剥掉气泡里的附件标注行
            text = String(text[..<range.lowerBound])
        }
        Task {
            let res = try? await client.deleteLastTurn(sessionId: sid)
            if res?.status != true {
                errorMessage = res?.response ?? L("回退失败")
                return
            }
            messages = []
            await loadHistory(sessionId: sid)
            await loadSessions()
            composerPrefill = text
        }
    }

    // MARK: - 分支 / 回退

    /// 从指定落盘轮次分支出新会话并切换过去（后端复制 [0, index] 并尽量热复用 KV）。
    func branch(fromTurnIndex index: Int) {
        guard let client, let sid = selectedSessionId else { return }
        Task {
            do {
                let res = try await client.branchSession(sessionId: sid, nodeIndex: index)
                guard res.status, let newId = res.sessionId else {
                    errorMessage = res.response ?? L("分支失败")
                    return
                }
                await loadSessions()
                select(sessionId: newId)
            } catch {
                errorMessage = L("分支失败：") + error.localizedDescription
            }
        }
    }

    /// 删除最后一轮问答（后端同步回退内存记忆与 KV），完成后重拉历史对账。
    func deleteLastTurn() {
        guard let client, let sid = selectedSessionId, !isGenerating else { return }
        Task {
            do {
                let res = try await client.deleteLastTurn(sessionId: sid)
                if !res.status {
                    errorMessage = res.response ?? "回退失败"
                }
                messages = []
                await loadHistory(sessionId: sid)
                await loadSessions()
            } catch {
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
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 90_000_000)
            guard let self else { return }
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

        // withTaskGroup 闭包不继承 MainActor：缓存组外快照、组内只收集、组后统一写回
        let cacheSnapshot = historyCache
        let collected: [(SessionMetaItem, [Turn])] = await withTaskGroup(
            of: (SessionMetaItem, [Turn])?.self
        ) { group in
            var pending = targets.makeIterator()
            var active = 0
            var out: [(SessionMetaItem, [Turn])] = []
            func enqueue() {
                while active < 5, let item = pending.next() {
                    active += 1
                    let cached = cacheSnapshot[item.sessionId]
                    group.addTask {
                        if let cached, cached.updated == (item.lastUpdated ?? -1) {
                            return (item, cached.turns)
                        }
                        guard let history = try? await client.history(sessionId: item.sessionId)
                        else { return nil }
                        return (item, history.turns)
                    }
                }
            }
            enqueue()
            for await result in group {
                active -= 1
                enqueue()
                if let result { out.append(result) }
            }
            return out
        }
        for (item, turns) in collected {
            historyCache[item.sessionId] = (item.lastUpdated ?? -1, turns)
            if let hit = Self.match(query: query, in: turns, session: item) {
                hits.append(hit)
            }
        }
        // 容量护栏：缓存的是全部会话的全文轮次，会话多时是可观的常驻内存——
        // 超限整体清掉（搜索是低频手动操作，重拉的代价可接受）
        if historyCache.count > 32 { historyCache.removeAll() }

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
