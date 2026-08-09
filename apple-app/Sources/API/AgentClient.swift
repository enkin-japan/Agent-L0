import Foundation

enum AgentError: LocalizedError {
    case badURL
    case http(Int, String)

    var errorDescription: String? {
        switch self {
        case .badURL: return "服务器地址无效"
        case .http(let code, let detail):
            return detail.isEmpty ? "HTTP \(code)" : "HTTP \(code)：\(detail)"
        }
    }
}

/// SplitStack Agent API 客户端（frontend/src/api/agent.ts 的 Swift 直译）。
/// 原生端直连后端 14159（web 那层 5173 代理注入 Bearer 的机制不需要）：
/// key 存 Keychain，每个请求自带 Authorization 头。
final class AgentClient {
    let baseURL: URL
    private let apiKey: String
    private let session: URLSession
    /// 阻塞式长调用（/task/resume：续跑可能多步耗时）专用超时。
    private let longTimeout: TimeInterval = 3600

    init(baseURL: URL, apiKey: String) {
        self.baseURL = baseURL
        self.apiKey = apiKey
        let config = URLSessionConfiguration.default
        // SSE 心跳 15s 一帧，120s 收不到任何字节才算断（远宽于心跳间隔）
        config.timeoutIntervalForRequest = 120
        config.timeoutIntervalForResource = 24 * 3600
        self.session = URLSession(configuration: config)
    }

    // MARK: - 请求构造

    private func request(_ method: String, _ path: String, json: Encodable? = nil,
                         timeout: TimeInterval? = nil) throws -> URLRequest {
        guard let url = URL(string: path, relativeTo: baseURL) else { throw AgentError.badURL }
        var req = URLRequest(url: url)
        req.httpMethod = method
        if !apiKey.isEmpty {
            req.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        }
        if let timeout { req.timeoutInterval = timeout }
        if let json {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONEncoder().encode(json)
        }
        return req
    }

    private func chatFormRequest(_ path: String, params: ChatParams, files: [Upload]) throws -> URLRequest {
        var req = try request("POST", path)
        let boundary = "macagent-\(UUID().uuidString)"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let fields: [(String, String)] = [
            ("prompt", params.prompt),
            ("enable_thinking", params.enableThinking ? "true" : "false"),
            ("thinking_budget", String(params.thinkingBudget)),
            ("max_tokens", String(params.maxTokens)),
            ("temperature", String(params.temperature)),
            ("min_p", String(params.minP)),
        ]
        req.httpBody = Multipart.body(fields: fields, files: files, boundary: boundary)
        return req
    }

    private func send<T: Decodable>(_ req: URLRequest) async throws -> T {
        let (data, response) = try await session.data(for: req)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            let detail = String(data: data.prefix(300), encoding: .utf8) ?? ""
            throw AgentError.http(http.statusCode, detail)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    // MARK: - 健康检查（免鉴权）

    func health() async throws -> Bool {
        let req = try request("GET", "/health", timeout: 5)
        let (_, response) = try await session.data(for: req)
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    // MARK: - 会话

    func listSessions() async throws -> [SessionMetaItem] {
        let res: SessionListResponse = try await send(try request("GET", "/api/v1/sessions"))
        return res.sessions
    }

    func history(sessionId: String) async throws -> HistoryResponse {
        try await send(try request("GET", "/api/v1/chat/\(sessionId)/history"))
    }

    func deleteSession(sessionId: String) async throws -> SimpleActionResponse {
        try await send(try request("DELETE", "/api/v1/chat/\(sessionId)"))
    }

    func deleteLastTurn(sessionId: String) async throws -> SimpleActionResponse {
        try await send(try request("DELETE", "/api/v1/chat/\(sessionId)/turns/last"))
    }

    struct PinBody: Encodable { let pinned: Bool }

    func setPin(sessionId: String, pinned: Bool) async throws -> PinResponse {
        try await send(try request("PATCH", "/api/v1/chat/\(sessionId)/pin", json: PinBody(pinned: pinned)))
    }

    struct MetaBody: Encodable { let title: String? }

    /// 会话改名(服务端一等,Phase 4b:双端同源)。空串=清除改名回退派生标题。
    func renameSession(sessionId: String, title: String) async throws -> SimpleActionResponse {
        try await send(try request("PATCH", "/api/v1/chat/\(sessionId)/meta",
                                   json: MetaBody(title: title)))
    }

    // MARK: - 事件账本协议 v1(PROTOCOL.md §2.4~§2.6):轮次是可寻址资源

    /// 提交新轮次:受理即返(202 {turn_id, seq}),执行在后台,过程经 attachEvents 观察。
    /// 会话已有排队/运行中的轮次时抛 TurnConflictError(409)。
    func submitTurn(sessionId: String, params: ChatParams, files: [Upload]) async throws -> TurnAccepted {
        var req = try chatFormRequest("/api/v1/chat/\(sessionId)/turns", params: params, files: files)
        req.setValue(nil, forHTTPHeaderField: "Accept")
        return try await acceptedOrConflict(req)
    }

    /// 审批决议(202 受理即返):结果经事件流回来(resumed → … → turn_ended)。
    /// expectedTurnId:远程决议(通知按钮/灵动岛)带上,目标轮已换时 409 防错批;
    /// App 内实时审批卡不带(nil),行为与旧版完全一致。
    func submitApproval(sessionId: String, approve: Bool,
                        expectedTurnId: String? = nil) async throws -> TurnAccepted {
        let req = try request("POST", "/api/v1/chat/\(sessionId)/turns/approval",
                              json: ApprovalBody(approve: approve,
                                                 expected_turn_id: expectedTurnId))
        return try await acceptedOrConflict(req)
    }

    /// 定位应答体(get_location 工具回程)。字段名对齐后端 LocationReport(snake_case)。
    struct LocationReportBody: Encodable {
        let request_id: String
        let status: String        // ok | denied | error
        let latitude: Double?
        let longitude: Double?
        let place: String?
        let accuracy_m: Double?
        let error: String?
    }

    /// 上报定位应答:200=落位;409=请求已超时或已被别的客户端应答——都算了结,不抛错。
    func reportLocation(sessionId: String, body: LocationReportBody) async throws {
        let req = try request("POST", "/api/v1/chat/\(sessionId)/location", json: body)
        let (data, response) = try await session.data(for: req)
        if let http = response as? HTTPURLResponse,
           http.statusCode != 200, http.statusCode != 409 {
            let detail = String(data: data.prefix(300), encoding: .utf8) ?? ""
            throw AgentError.http(http.statusCode, detail)
        }
    }

    /// 恢复挂起轮次(202 受理即返):续跑过程经事件流观察。
    func submitResume(sessionId: String) async throws -> TurnAccepted {
        let req = try request("POST", "/api/v1/chat/\(sessionId)/turns/resume")
        return try await acceptedOrConflict(req)
    }

    private struct ConflictBody: Decodable { let reason: String? }

    private func acceptedOrConflict(_ req: URLRequest) async throws -> TurnAccepted {
        let (data, response) = try await session.data(for: req)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 409 {
            let body = try? JSONDecoder().decode(ConflictBody.self, from: data)
            throw TurnConflictError(reason: body?.reason ?? "conflict")
        }
        guard (200...299).contains(code) else {
            throw AgentError.http(code, String(data: data.prefix(300), encoding: .utf8) ?? "")
        }
        return try JSONDecoder().decode(TurnAccepted.self, from: data)
    }

    /// 当前轮次的可恢复状态(无活跃轮次时 turn 为 nil)+ 账本游标 last_seq。
    func turnState(sessionId: String) async throws -> TurnStateResponse {
        try await send(try request("GET", "/api/v1/chat/\(sessionId)/turn"))
    }

    /// 附着全局任务 feed(SSE,PROTOCOL.md §2.7):跨会话生命周期事件——
    /// 任务中心刷新门铃 + 后台通知的推送源(替代 /tasks 差分轮询)。
    func attachGlobalFeed(after: Int) throws -> AsyncThrowingStream<FeedItem, Error> {
        var req = try request("GET", "/api/v1/events?after=\(after)")
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let frames = SSEClient.frames(for: req, session: session)
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    for try await frame in frames {
                        guard let data = frame.data.data(using: .utf8),
                              let item = try? JSONDecoder().decode(FeedItem.self, from: data)
                        else { continue }
                        continuation.yield(item)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// 附着事件流(SSE):重放 seq > after 的事件后转实时续播,长连贯穿多个轮次。
    /// 服务端正常收流(会话删除)自然结束;网络断开抛错,由调用方带游标重连。
    func attachEvents(sessionId: String, after: Int) throws
        -> AsyncThrowingStream<EventEnvelope, Error> {
        var req = try request("GET", "/api/v1/chat/\(sessionId)/events?after=\(after)")
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let frames = SSEClient.frames(for: req, session: session)
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    for try await frame in frames {
                        guard let data = frame.data.data(using: .utf8),
                              let env = try? JSONDecoder().decode(EventEnvelope.self, from: data)
                        else { continue }
                        continuation.yield(env)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: - 非流式便捷入口(App Intents / Siri 一次性问答;内部走 TurnRunner 同一闭环)

    /// 非流式对话（阻塞到整轮完成）：App Intents / Siri 等一次性问答场景用。
    func chatBlocking(sessionId: String, params: ChatParams) async throws -> Turn {
        var req = try chatFormRequest("/api/v1/chat/\(sessionId)", params: params, files: [])
        req.setValue(nil, forHTTPHeaderField: "Accept")
        req.timeoutInterval = longTimeout
        return try await send(req)
    }

    struct ApprovalBody: Encodable {
        let approve: Bool
        let expected_turn_id: String?
    }

    // MARK: - 推送注册面（iOS APNs；macOS 本地通知不经后端）

    struct PushRegisterBody: Encodable {
        let device_id: String
        let platform: String
        let token: String?
        let push_to_start_token: String?
        let prefs: PushPrefsBody?
        let app_build: String?
    }

    struct PushPrefsBody: Encodable {
        let results: Bool?
        let schedule_start: Bool?
        let approval: Bool?
        let content_level: String?
    }

    struct PushRegisterResponse: Decodable {
        let status: Bool
        let pushEnabled: Bool?
        enum CodingKeys: String, CodingKey {
            case status
            case pushEnabled = "push_enabled"
        }
    }

    /// 设备注册/更新（upsert，幂等）：token 轮换、push-to-start token、偏好都走这里。
    func pushRegister(deviceId: String, token: String? = nil,
                      pushToStartToken: String? = nil,
                      prefs: PushPrefsBody? = nil,
                      appBuild: String? = nil) async throws -> PushRegisterResponse {
        try await send(try request("POST", "/api/v1/push/register", json: PushRegisterBody(
            device_id: deviceId, platform: "ios", token: token,
            push_to_start_token: pushToStartToken, prefs: prefs, app_build: appBuild)))
    }

    func pushUnregister(deviceId: String) async throws {
        struct R: Decodable { let status: Bool }
        let _: R = try await send(try request("DELETE", "/api/v1/push/devices/\(deviceId)"))
    }

    func pushUpdatePrefs(deviceId: String, prefs: PushPrefsBody) async throws {
        struct R: Decodable { let status: Bool }
        let _: R = try await send(try request(
            "PATCH", "/api/v1/push/devices/\(deviceId)/prefs", json: prefs))
    }

    struct LiveActivityTokenBody: Encodable {
        let device_id: String
        let session_id: String
        let turn_id: String
        let token: String
    }

    struct LiveActivityTokenResponse: Decodable {
        let status: Bool
        let active: Bool?
    }

    /// Live Activity 活动 token 上报。active=false 表示轮次已结束——调用方本地收尾孤儿活动。
    func pushLiveActivityToken(deviceId: String, sessionId: String, turnId: String,
                               token: String) async throws -> LiveActivityTokenResponse {
        try await send(try request("POST", "/api/v1/push/live-activity",
                                   json: LiveActivityTokenBody(
                                       device_id: deviceId, session_id: sessionId,
                                       turn_id: turnId, token: token)))
    }

    // MARK: - 会话资产 / 记忆 / 分支

    /// 拉取会话资产（图表 PNG 等）：带鉴权头 GET 相对路径。文件名含时间戳恒不复用，
    /// 后端发 immutable 长缓存头，URLCache 自动生效。
    /// 会话产物清单（检查器「文件」页签）。
    func sessionFiles(sessionId: String) async throws -> [AssetFileItem] {
        let res: SessionFilesResponse = try await send(try request("GET", "/api/v1/files/\(sessionId)"))
        return res.files
    }

    /// 下载会话产物到本地临时文件（流式落盘，视频可上 GB——绝不整块进内存）。
    /// 返回的 URL 在系统临时目录，调用方负责搬走或用完即弃。
    func downloadAsset(path: String, suggestedName: String) async throws -> URL {
        let req = try request("GET", path, timeout: 3600)
        let (tmp, response) = try await session.download(for: req)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw AgentError.http(http.statusCode, "")
        }
        // download(for:) 的临时文件名是随机串：换成真名（默认 app 打开/分享都靠扩展名）
        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("macagent-assets", isDirectory: true)
            .appendingPathComponent(suggestedName)
        try? FileManager.default.createDirectory(at: dest.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        try? FileManager.default.removeItem(at: dest)
        try FileManager.default.moveItem(at: tmp, to: dest)
        return dest
    }

    /// 产物的绝对 URL + 鉴权头（AVPlayer 流式播放用：AVURLAsset 自带 Range 断点能力，
    /// 但 Authorization 头得由我们注入）。
    func assetPlaybackSource(path: String) -> (url: URL, headers: [String: String])? {
        guard let url = URL(string: path, relativeTo: baseURL) else { return nil }
        let headers = apiKey.isEmpty ? [:] : ["Authorization": "Bearer \(apiKey)"]
        return (url, headers)
    }

    func fetchAsset(path: String) async throws -> Data {
        var req = try request("GET", path)
        req.cachePolicy = .returnCacheDataElseLoad
        let (data, response) = try await session.data(for: req)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw AgentError.http(http.statusCode, "")
        }
        return data
    }

    func memoryStatus(sessionId: String) async throws -> MemoryStatusResponse {
        try await send(try request("GET", "/api/v1/chat/\(sessionId)/memory"))
    }

    // MARK: - RAG 知识库管理（P1：经 backend 薄代理，RAG 数据面只听回环）

    func ragOverview() async throws -> RagOverviewResponse {
        try await send(try request("GET", "/api/v1/rag/overview"))
    }

    func ragDocuments(category: String) async throws -> RagDocumentsResponse {
        let encoded = category.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? category
        return try await send(try request("GET", "/api/v1/rag/documents?category=\(encoded)"))
    }

    struct RagDeleteBody: Encodable { let source_path: String }

    @discardableResult
    func ragDeleteDocument(sourcePath: String) async throws -> RagActionResponse {
        try await send(try request("DELETE", "/api/v1/rag/document",
                                   json: RagDeleteBody(source_path: sourcePath)))
    }

    @discardableResult
    func ragPurgeCategory(_ name: String) async throws -> RagActionResponse {
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        return try await send(try request("DELETE", "/api/v1/rag/category/\(encoded)"))
    }

    // MARK: - 私人 API 工具管理（声明式工具工厂表单面；校验/落盘/注册全在后端）

    struct ApiToolSaveBody: Encodable {
        var name: String
        var description: String
        var endpoint: String
        var method: String
        var params: JSONValue?
        var auth: JSONValue?
        var headers: JSONValue?
        var timeout: Double?
        var requires_approval: Bool?
        var enabled: Bool
        var secret: String?              // 密钥实值：后端写 0600 secrets 文件，不入声明
        var arguments: JSONValue?        // 仅试调端点读取；保存端点忽略
    }

    func listApiTools() async throws -> ApiToolListResponse {
        try await send(try request("GET", "/api/v1/api-tools"))
    }

    func saveApiTool(_ body: ApiToolSaveBody) async throws -> ApiToolActionResponse {
        try await send(try request("POST", "/api/v1/api-tools", json: body))
    }

    func testApiTool(_ body: ApiToolSaveBody) async throws -> ApiToolTestResponse {
        try await send(try request("POST", "/api/v1/api-tools/test", json: body,
                                   timeout: 90))
    }

    @discardableResult
    func deleteApiTool(name: String) async throws -> ApiToolActionResponse {
        try await send(try request("DELETE", "/api/v1/api-tools/\(name)"))
    }

    // MARK: - 待确认注册队列（对话式注册的人工闸）
    // 模型起草的工具/剧本声明落在待确认区，这组端点是**唯一**放行口（后端红线：
    // 模型没有确认工具，确认只在设置页）。确认即走与表单面同一条保存/上架实现。

    func listRegistrations() async throws -> [RegistrationItem] {
        let res: RegistrationListResponse = try await send(
            try request("GET", "/api/v1/registrations"))
        return res.registrations
    }

    /// 确认放行一份注册单。api_tool 的确认会当场做一次探活（可能走网络），
    /// 给和试调同一档的宽超时。
    @discardableResult
    func confirmRegistration(id: String) async throws -> RegistrationActionResponse {
        let encoded = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        return try await send(try request("POST", "/api/v1/registrations/\(encoded)/confirm",
                                          timeout: 90))
    }

    @discardableResult
    func rejectRegistration(id: String) async throws -> RegistrationActionResponse {
        let encoded = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        return try await send(try request("DELETE", "/api/v1/registrations/\(encoded)"))
    }

    /// 会话 Deck Spec；该会话没有 deck 时返回 nil（404 不是错误）。
    func deck(sessionId: String) async throws -> DeckSpec? {
        let req = try request("GET", "/api/v1/files/\(sessionId)/deck")
        let (data, response) = try await session.data(for: req)
        guard let http = response as? HTTPURLResponse else { return nil }
        if http.statusCode == 404 { return nil }
        if http.statusCode != 200 {
            throw AgentError.http(http.statusCode, "")
        }
        return try JSONDecoder().decode(DeckEnvelope.self, from: data).deck
    }

    struct BranchBody: Encodable {
        let node_index: Int
    }

    /// 从历史节点分支出新会话：复制 [0, nodeIndex] 闭区间，热态时克隆 KV Cache。
    func branchSession(sessionId: String, nodeIndex: Int) async throws -> BranchResponse {
        try await send(try request("POST", "/api/v1/chat/\(sessionId)/branch",
                                   json: BranchBody(node_index: nodeIndex)))
    }

    // MARK: - 定时任务（创建/编辑走对话里的 schedule_* 工具；这里只有直接控制面）

    func listSchedules() async throws -> [ScheduledTask] {
        let res: ScheduleListResponse = try await send(try request("GET", "/api/v1/schedules"))
        return res.schedules
    }

    struct ToggleBody: Encodable { let enabled: Bool }

    func toggleSchedule(id: String, enabled: Bool) async throws -> ScheduleActionResponse {
        try await send(try request("POST", "/api/v1/schedules/\(id)/toggle",
                                   json: ToggleBody(enabled: enabled)))
    }

    func runScheduleNow(id: String) async throws -> ScheduleActionResponse {
        try await send(try request("POST", "/api/v1/schedules/\(id)/run"))
    }

    func cancelScheduleRun(id: String) async throws -> ScheduleActionResponse {
        try await send(try request("POST", "/api/v1/schedules/\(id)/cancel"))
    }

    func deleteSchedule(id: String) async throws -> ScheduleActionResponse {
        try await send(try request("DELETE", "/api/v1/schedules/\(id)"))
    }

    func scheduleRuns(id: String, limit: Int = 20) async throws -> [ScheduleRun] {
        let res: ScheduleRunsResponse = try await send(
            try request("GET", "/api/v1/schedules/\(id)/runs?limit=\(limit)"))
        return res.runs
    }

    // MARK: - 任务控制

    func taskStatus(sessionId: String) async throws -> TaskStatusResponse {
        try await send(try request("GET", "/api/v1/chat/\(sessionId)/task"))
    }

    /// 全局任务视图（跨会话，含其他客户端发起的）：任务中心 + 后台完结监听共用。
    func globalTasks() async throws -> GlobalTasksResponse {
        try await send(try request("GET", "/api/v1/tasks"))
    }

    func tools() async throws -> [ToolInfo] {
        let res: ToolsResponse = try await send(try request("GET", "/api/v1/tools"))
        return res.tools
    }

    func skills() async throws -> [SkillItem] {
        let res: SkillsResponse = try await send(try request("GET", "/api/v1/skills"))
        return res.skills
    }

    // MARK: - 剧本管理（用户以 /名字 点名的流程提示词；校验/落盘/注册在后端）

    struct SkillSaveBody: Encodable {
        let name: String
        let description: String
        let body: String
        let requires: [String]?          // 依赖工具名；空数组发 nil
    }

    @discardableResult
    func saveSkill(_ body: SkillSaveBody) async throws -> SkillActionResponse {
        try await send(try request("POST", "/api/v1/skills", json: body))
    }

    @discardableResult
    func deleteSkill(name: String) async throws -> SkillActionResponse {
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        return try await send(try request("DELETE", "/api/v1/skills/\(encoded)"))
    }

    func systemPrompt(sessionId: String) async throws -> SystemPromptResponse {
        try await send(try request("GET", "/api/v1/chat/\(sessionId)/system"))
    }

    func cancelTask(sessionId: String) async throws -> SimpleActionResponse {
        try await send(try request("POST", "/api/v1/chat/\(sessionId)/task/cancel"))
    }

    func suspendTask(sessionId: String) async throws -> SimpleActionResponse {
        try await send(try request("POST", "/api/v1/chat/\(sessionId)/task/suspend"))
    }

}
