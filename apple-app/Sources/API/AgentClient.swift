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
    let cacheNamespace: String
    let offlineCacheRoot: URL?
    let offlineAssets: OfflineAssetStore
    private let apiKey: String
    private let session: URLSession
    /// 阻塞式长调用（/task/resume：续跑可能多步耗时）专用超时。
    private let longTimeout: TimeInterval = 3600

    init(baseURL: URL, apiKey: String, session: URLSession? = nil, cacheRoot: URL? = nil,
         mirrorLimits: OfflineAssetStore.MirrorLimits = .production) {
        self.baseURL = baseURL
        self.apiKey = apiKey
        self.cacheNamespace = OfflineHistoryStore.namespace(baseURL: baseURL, apiKey: apiKey)
        self.offlineCacheRoot = cacheRoot
        self.offlineAssets = OfflineAssetStore(root: cacheRoot, limits: mirrorLimits)
        if let session {
            self.session = session
            return
        }
        let config = URLSessionConfiguration.default
        // SSE 心跳 15s 一帧，120s 收不到任何字节才算断（远宽于心跳间隔）
        config.timeoutIntervalForRequest = 120
        config.timeoutIntervalForResource = 24 * 3600
        self.session = URLSession(configuration: config)
    }

    /// app 退出前调用：显式取消本 session 上所有在途请求（含 /events SSE 长连），
    /// 让 backend 侧的连接立刻断开——uvicorn 优雅停机只等在途请求，连接一断就不再等
    /// timeout_graceful_shutdown（5 s）。不作废 session：退出途中仍可能有协程在同一
    /// session 上发新请求（离线资产同步、历史同步、连接探测），对已作废 session 建任务
    /// 会抛 NSGenericException 把 app 崩在退出路上（reviewer 首审 M1）。
    /// getAllTasks 的回调在 session 自己的队列上执行，不依赖调用线程继续转。
    func cancelInFlightRequests() {
        session.getAllTasks { tasks in tasks.forEach { $0.cancel() } }
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
        var fields: [(String, String)] = [
            ("prompt", params.prompt),
            ("enable_thinking", params.enableThinking ? "true" : "false"),
            ("thinking_budget", String(params.thinkingBudget)),
            ("max_tokens", String(params.maxTokens)),
            ("temperature", String(params.temperature)),
            ("min_p", String(params.minP)),
        ]
        if let key = params.clientTurnId, !key.isEmpty {
            fields.append(("client_turn_id", key))   // 提交幂等键(B3)
        }
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

    func backendAccess() async -> BackendAccess { await connectionStatus().access }

    /// One health read per monitor cycle, followed by the existing authenticated read.
    func connectionStatus() async -> BackendConnectionStatus {
        let health: BackendHealth?
        do {
            let (data, response) = try await session.data(for: request("GET", "/health", timeout: 5))
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                return BackendConnectionStatus(access: .offline, health: nil)
            }
            health = try? JSONDecoder().decode(BackendHealth.self, from: data)
        } catch { return BackendConnectionStatus(access: .offline, health: nil) }
        let access: BackendAccess
        do {
            let index = try await sessionIndex(timeout: 5)
            access = index.status ? .authenticated : .reachable
        } catch AgentError.http(let code, _) where code == 401 || code == 403 {
            access = .unauthorized
        } catch { access = .reachable }
        return BackendConnectionStatus(access: access, health: health)
    }

    // MARK: - 会话

    func listSessions() async throws -> [SessionMetaItem] {
        try await sessionIndex().sessions
    }

    func sessionIndex(timeout: TimeInterval? = nil) async throws -> SessionListResponse {
        try await send(try request("GET", "/api/v1/sessions", timeout: timeout))
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
        let result: PinResponse = try await send(try request("PATCH", "/api/v1/chat/\(sessionId)/pin", json: PinBody(pinned: pinned)))
        guard result.status else { throw AgentError.http(409, result.response ?? "无法修改保留状态") }
        return result
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

    struct RetryBody: Encodable {
        let expected_turn_ref: String
        let client_turn_id: String
    }

    /// Canonical task retry: backend copies complete inputs/settings; no DELETE or text fallback.
    func submitRetry(sessionId: String, sourceRef: String, clientTurnId: String) async throws -> TurnAccepted {
        let req = try request("POST", "/api/v1/chat/\(sessionId)/turns/retry",
                              json: RetryBody(expected_turn_ref: sourceRef, client_turn_id: clientTurnId))
        return try await acceptedOrConflict(req)
    }

    /// 审批决议(202 受理即返):结果经事件流回来(resumed → … → turn_ended)。
    /// expectedTurnId:实时卡片与远程决议均携带展示轮次，目标已换时 409 防错批；
    /// 保持可选以兼容旧调用方。
    func submitApproval(sessionId: String, approve: Bool,
                        expectedTurnId: String? = nil,
                        expectedApprovalSeq: Int? = nil) async throws -> TurnAccepted {
        let req = try request("POST", "/api/v1/chat/\(sessionId)/turns/approval",
                              json: ApprovalBody(approve: approve,
                                                 expected_turn_id: expectedTurnId,
                                                 expected_approval_seq: expectedApprovalSeq))
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
        let expected_approval_seq: Int?
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
    func cachedSessionFiles(sessionId: String) async -> [AssetFileItem]? {
        guard let data = await offlineAssets.data(namespace: cacheNamespace, sid: sessionId, key: "files"),
              let cached = try? JSONDecoder().decode(SessionFilesResponse.self, from: data) else { return nil }
        return cached.files
    }

    func cachedInputFiles(sessionId: String) async -> [OfflineFileEntry] {
        guard let data = await offlineAssets.data(namespace: cacheNamespace, sid: sessionId, key: "manifest"),
              let manifest = try? JSONDecoder().decode(OfflineFileManifest.self, from: data),
              manifest.status, manifest.session_id == sessionId else { return [] }
        return manifest.files.filter { $0.kind != "assets" }
    }

    func sessionFiles(sessionId: String) async throws -> [AssetFileItem] {
        let requestEpoch = try await offlineAssets.epoch(namespace: cacheNamespace, sid: sessionId)
        do {
            let res: SessionFilesResponse = try await send(try request("GET", "/api/v1/files/\(sessionId)", timeout: 5))
            guard res.status else { throw AgentError.http(200, L("文件清单暂不可用")) }
            try? await offlineAssets.save(JSONEncoder().encode(res), namespace: cacheNamespace,
                sid: sessionId, key: "files", expectedEpoch: requestEpoch)
            try await offlineAssets.validate(namespace: cacheNamespace, sid: sessionId, expectedEpoch: requestEpoch)
            return res.files
        } catch {
            // A cached list stands in only for lost transport. Authorization,
            // server and decoding failures must reach the caller.
            guard OfflineTransportFailure.matches(error),
                  let cached = await cachedSessionFiles(sessionId: sessionId) else { throw error }
            return cached
        }
    }

    /// Traversal is rejected as a path SEGMENT: `report..final.pdf` is an ordinary
    /// file name, while `a/../b` is an escape. Mirrors canonicalInputReference.
    private func assetSession(_ path: String) throws -> String {
        let decoded = path.removingPercentEncoding ?? path
        let parts = decoded.split(separator: "/").map(String.init)
        guard path.hasPrefix("/api/v1/files/"), parts.count >= 5,
              !path.contains("?"), !path.contains("#"), !decoded.contains("\\"),
              !parts.contains(".."), !parts.contains(".") else { throw AgentError.badURL }
        return parts[3]
    }

    /// Returned export copies retain the original filename; media itself remains
    /// in the durable cache so closing a preview cannot erase the offline copy.
    func downloadAsset(path: String, suggestedName: String, expectedEpoch: Int? = nil) async throws -> URL {
        let sid = try assetSession(path)
        let requestEpoch: Int
        if let expectedEpoch { requestEpoch = expectedEpoch }
        else { requestEpoch = try await offlineAssets.epoch(namespace: cacheNamespace, sid: sid) }
        try await offlineAssets.validate(namespace: cacheNamespace, sid: sid, expectedEpoch: requestEpoch, key: path)
        let local: URL
        var downloaded: URL?
        defer { if let downloaded { try? FileManager.default.removeItem(at: downloaded) } }
        if let cached = await offlineAssets.file(namespace: cacheNamespace, sid: sid, path: path) {
            local = cached
        } else {
            let (temporary, response) = try await session.download(for: request("GET", path, timeout: 3600))
            downloaded = temporary
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                throw AgentError.http((response as? HTTPURLResponse)?.statusCode ?? 0, "")
            }
            try? await offlineAssets.install(temporary, namespace: cacheNamespace, sid: sid, path: path, expectedEpoch: requestEpoch)
            local = await offlineAssets.file(namespace: cacheNamespace, sid: sid, path: path) ?? temporary
        }
        try await offlineAssets.validate(namespace: cacheNamespace, sid: sid, expectedEpoch: requestEpoch, key: path)
        // One deterministic export location per cached asset, overwritten in place:
        // repeated previews of the same file must not pile up full temporary copies.
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("macagent-assets")
            .appendingPathComponent(OfflineHistoryStore.digest(cacheNamespace + "\n" + path))
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let dest = directory.appendingPathComponent((suggestedName as NSString).lastPathComponent)
        if FileManager.default.fileExists(atPath: dest.path) {
            try FileManager.default.removeItem(at: dest)
        }
        try FileManager.default.copyItem(at: local, to: dest)
        return dest
    }

    func assetPlaybackSource(path: String) async -> (url: URL, headers: [String: String])? {
        guard let sid = try? assetSession(path),
              let requestEpoch = try? await offlineAssets.epoch(namespace: cacheNamespace, sid: sid),
              (try? await offlineAssets.validate(namespace: cacheNamespace, sid: sid, expectedEpoch: requestEpoch, key: path)) != nil else { return nil }
        if let cached = await offlineAssets.file(namespace: cacheNamespace, sid: sid, path: path) {
            guard (try? await offlineAssets.validate(namespace: cacheNamespace, sid: sid, expectedEpoch: requestEpoch, key: path)) != nil else { return nil }
            return (cached, [:])
        }
        guard let url = URL(string: path, relativeTo: baseURL) else { return nil }
        return (url, apiKey.isEmpty ? [:] : ["Authorization": "Bearer \(apiKey)"])
    }

    func fetchAsset(path: String) async throws -> Data {
        let sid = try assetSession(path)
        let requestEpoch = try await offlineAssets.epoch(namespace: cacheNamespace, sid: sid)
        try await offlineAssets.validate(namespace: cacheNamespace, sid: sid, expectedEpoch: requestEpoch, key: path)
        if let cached = await offlineAssets.data(namespace: cacheNamespace, sid: sid, key: path) {
            try await offlineAssets.validate(namespace: cacheNamespace, sid: sid, expectedEpoch: requestEpoch, key: path)
            return cached
        }
        let (data, response) = try await session.data(for: request("GET", path))
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            throw AgentError.http((response as? HTTPURLResponse)?.statusCode ?? 0, "")
        }
        try? await offlineAssets.save(data, namespace: cacheNamespace, sid: sid, key: path, expectedEpoch: requestEpoch)
        try await offlineAssets.validate(namespace: cacheNamespace, sid: sid, expectedEpoch: requestEpoch, key: path)
        return data
    }

    /// The result of one automatic mirroring pass. `partial` means the pass reached
    /// the end of the manifest but the budget left files out, so the session is not
    /// fully saved on this device even though nothing failed. `evicted` names other
    /// sessions whose copies were dropped to make room.
    struct OfflineSyncResult: Sendable, Equatable {
        enum State: Sendable, Equatable { case complete, partial, deferred, failed }
        var state: State
        var evicted: Set<String> = []
    }

    /// Serial disk downloads bound memory and connection usage. A failed transfer
    /// leaves its old manifest in place so the next pass retries it.
    /// `markEvicted` is called with the other sessions about to lose copies, before
    /// their bytes are removed (see OfflineAssetStore.admitAutomatic).
    func syncOfflineAssets(sessionId: String,
                           markEvicted: @escaping @Sendable (Set<String>) async throws -> Void = { _ in }) async -> OfflineSyncResult {
        // Metered or low-data paths carry no automatic media copies; history text
        // keeps syncing, and any file still opens on demand.
        guard NetworkPathPolicy.shared.allowsAutomaticMirroring else { return OfflineSyncResult(state: .deferred) }
        var evicted = Set<String>()
        var skipped = false
        // The session's own missing bytes are expressed by the failed state, not as
        // an eviction of itself (which would bump its own pass token).
        let failure = { OfflineSyncResult(state: .failed, evicted: evicted.subtracting([sessionId])) }
        do {
            let requestEpoch = try await offlineAssets.epoch(namespace: cacheNamespace, sid: sessionId)
            let manifest: OfflineFileManifest = try await send(try request("GET",
                "/api/v1/chat/\(sessionId)/offline-files", timeout: 15))
            guard manifest.status, manifest.session_id == sessionId else { return failure() }
            for item in manifest.files {
                guard try assetSession(item.path) == sessionId, item.size >= 0,
                      ["assets", "image", "audio", "attach"].contains(item.kind) else { return failure() }
                if item.kind != "assets" {
                    guard OfflineInputReference.assetPath(item.path) == OfflineInputReference(kind: item.kind, path: item.name) else { return failure() }
                }
            }
            try await offlineAssets.acceptManifest(manifest, namespace: cacheNamespace, sid: sessionId, expectedEpoch: requestEpoch)
            let oldData = await offlineAssets.data(namespace: cacheNamespace, sid: sessionId, key: "manifest")
            let old = oldData.flatMap { try? JSONDecoder().decode(OfflineFileManifest.self, from: $0) }
            // What is already on disk is recorded as each file lands, not at the end:
            // a failure on the last file must not make the next pass download the
            // whole session again. An older cache seeds the record from its manifest.
            var installed = await installedRevisions(sessionId: sessionId)
            if installed.isEmpty, let old {
                for file in old.files { installed[file.cachePath(sessionId: sessionId)] = file.revision }
            }
            for item in manifest.files {
                try await offlineAssets.validate(namespace: cacheNamespace, sid: sessionId, expectedEpoch: requestEpoch)
                guard try assetSession(item.path) == sessionId, item.size >= 0,
                      ["assets", "image", "audio", "attach"].contains(item.kind) else { return failure() }
                let key = item.cachePath(sessionId: sessionId)
                let exists = await offlineAssets.file(namespace: cacheNamespace, sid: sessionId, path: key) != nil
                if exists, installed[key] == item.revision { continue }
                // Oversized or over-budget files are left on the server on purpose:
                // they remain downloadable, and the cache stays within its budget.
                // Making room is never free — whoever lost bytes is reported back.
                let admission = await offlineAssets.admitAutomatic(size: item.size, namespace: cacheNamespace) { owners in
                    // The session's own losses are expressed by `skipped`, not as an
                    // eviction of itself (which would bump its own pass token).
                    let others = owners.subtracting([sessionId])
                    if !others.isEmpty { try await markEvicted(others) }
                }
                evicted.formUnion(admission.evicted)
                guard admission.accepted else { skipped = true; continue }
                var req = try request("GET", item.path, timeout: 120)
                req.cachePolicy = .reloadIgnoringLocalCacheData
                let (temporary, response) = try await session.download(for: req)
                defer { try? FileManager.default.removeItem(at: temporary) }
                guard (response as? HTTPURLResponse)?.statusCode == 200,
                      (try temporary.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init) == item.size
                else { return failure() }
                try await offlineAssets.install(temporary, namespace: cacheNamespace, sid: sessionId, path: key, expectedEpoch: requestEpoch)
                installed[key] = item.revision
                try await saveInstalledRevisions(installed, sessionId: sessionId, expectedEpoch: requestEpoch)
            }
            // A file evicted later in this same pass is not mirrored either.
            if evicted.contains(sessionId) { skipped = true }
            let keys = Set(manifest.files.map { $0.cachePath(sessionId: sessionId) })
            for previous in old?.files ?? [] where !keys.contains(previous.cachePath(sessionId: sessionId)) {
                await offlineAssets.remove(namespace: cacheNamespace, sid: sessionId, key: previous.cachePath(sessionId: sessionId), expectedEpoch: requestEpoch)
            }
            let outputs = manifest.files.filter { $0.kind == "assets" }.map {
                AssetFileItem(name: $0.name, size: $0.size,
                    mtime: (Double($0.revision.split(separator: ":").first ?? "0") ?? 0) / 1_000_000_000)
            }
            try await offlineAssets.save(JSONEncoder().encode(SessionFilesResponse(status: true, files: outputs)),
                namespace: cacheNamespace, sid: sessionId, key: "files", expectedEpoch: requestEpoch)
            _ = try await refreshDeck(sessionId: sessionId, expectedEpoch: requestEpoch)
            try await offlineAssets.save(JSONEncoder().encode(manifest), namespace: cacheNamespace, sid: sessionId, key: "manifest", expectedEpoch: requestEpoch)
            try await saveInstalledRevisions(installed.filter { keys.contains($0.key) },
                sessionId: sessionId, expectedEpoch: requestEpoch)
            return OfflineSyncResult(state: skipped ? .partial : .complete, evicted: evicted.subtracting([sessionId]))
        } catch { return failure() }
    }

    /// Cache path -> revision of every file this connection has actually written for
    /// the session. Kept apart from the manifest, which is only published once the
    /// whole pass succeeded.
    private struct InstalledRevisions: Codable {
        let session_id: String
        var entries: [String: String]
    }

    private func installedRevisions(sessionId: String) async -> [String: String] {
        guard let data = await offlineAssets.data(namespace: cacheNamespace, sid: sessionId, key: "installed"),
              let record = try? JSONDecoder().decode(InstalledRevisions.self, from: data),
              record.session_id == sessionId else { return [:] }
        return record.entries
    }

    private func saveInstalledRevisions(_ entries: [String: String], sessionId: String, expectedEpoch: Int) async throws {
        try await offlineAssets.save(JSONEncoder().encode(InstalledRevisions(session_id: sessionId, entries: entries)),
            namespace: cacheNamespace, sid: sessionId, key: "installed", expectedEpoch: expectedEpoch)
    }

    /// Resolve only a file declared by the session manifest, never a raw local
    /// path from a tool/model response. The saved manifest works without HTTP.
    func downloadAttachment(sessionId: String, attachment: AttachmentInfo) async throws -> URL {
        let requestEpoch = try await offlineAssets.epoch(namespace: cacheNamespace, sid: sessionId)
        let saved = await offlineAssets.data(namespace: cacheNamespace, sid: sessionId, key: "manifest")
        let manifest: OfflineFileManifest
        if let saved, let cached = try? JSONDecoder().decode(OfflineFileManifest.self, from: saved) {
            manifest = cached
        } else {
            manifest = try await send(try request("GET", "/api/v1/chat/\(sessionId)/offline-files", timeout: 5))
        }
        guard manifest.status, manifest.session_id == sessionId else { throw AgentError.badURL }
        try await offlineAssets.validate(namespace: cacheNamespace, sid: sessionId, expectedEpoch: requestEpoch)
        let filename = attachment.path.map { ($0 as NSString).lastPathComponent } ?? attachment.name
        let matches = manifest.files.filter { $0.kind != "assets" && $0.name == filename }
        guard matches.count == 1, let match = matches.first,
              try assetSession(match.path) == sessionId else {
            throw CocoaError(.fileReadNoSuchFile)
        }
        return try await downloadAsset(path: match.path, suggestedName: attachment.name ?? match.name, expectedEpoch: requestEpoch)
    }

    func memoryStatus(sessionId: String) async throws -> MemoryStatusResponse {
        try await send(try request("GET", "/api/v1/chat/\(sessionId)/memory"))
    }

    // MARK: - RAG 知识库管理（P1：经 backend 薄代理，RAG 数据面只听回环）

    func ragOverview() async throws -> RagOverviewResponse {
        try await send(try request("GET", "/api/v1/rag/overview"))
    }

    func ragRetierStatus() async throws -> RagRetierStatus {
        try await send(try request("GET", "/api/v1/rag/retier", timeout: 10))
    }

    func ragDocuments(category: String) async throws -> RagDocumentsResponse {
        let encoded = category.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? category
        return try await send(try request("GET", "/api/v1/rag/documents?category=\(encoded)"))
    }

    struct RagDeleteBody: Encodable { let source_path: String }

    @discardableResult
    func ragDeleteDocument(sourcePath: String) async throws -> RagActionResponse {
        let result: RagActionResponse = try await send(try request("DELETE", "/api/v1/rag/document",
                                   json: RagDeleteBody(source_path: sourcePath)))
        guard result.status != false else { throw AgentError.http(200, result.msg ?? L("删除未被受理。")) }
        return result
    }

    @discardableResult
    func ragPurgeCategory(_ name: String) async throws -> RagActionResponse {
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        let result: RagActionResponse = try await send(try request("DELETE", "/api/v1/rag/category/\(encoded)"))
        guard result.status != false else { throw AgentError.http(200, result.msg ?? L("删除未被受理。")) }
        return result
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

    func updateApiTool(name: String, changes: [String: JSONValue]) async throws -> ApiToolActionResponse {
        try await send(try request("PATCH", "/api/v1/api-tools/\(name)", json: changes))
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
    func cachedDeck(sessionId: String) async -> DeckSpec? {
        guard let data = await offlineAssets.data(namespace: cacheNamespace, sid: sessionId, key: "deck") else { return nil }
        return try? JSONDecoder().decode(DeckEnvelope.self, from: data).deck
    }

    func deck(sessionId: String) async throws -> DeckSpec? {
        do { return try await refreshDeck(sessionId: sessionId) }
        catch {
            // Same discipline as the file list: only lost transport reads the cache.
            guard OfflineTransportFailure.matches(error),
                  let cached = await cachedDeck(sessionId: sessionId) else { throw error }
            return cached
        }
    }

    private func refreshDeck(sessionId: String, expectedEpoch: Int? = nil) async throws -> DeckSpec? {
        let requestEpoch: Int
        if let expectedEpoch { requestEpoch = expectedEpoch }
        else { requestEpoch = try await offlineAssets.epoch(namespace: cacheNamespace, sid: sessionId) }
        let req = try request("GET", "/api/v1/files/\(sessionId)/deck", timeout: 15)
        let (data, response) = try await session.data(for: req)
        guard let http = response as? HTTPURLResponse else { throw AgentError.badURL }
        try await offlineAssets.validate(namespace: cacheNamespace, sid: sessionId, expectedEpoch: requestEpoch)
        if http.statusCode == 404 {
            await offlineAssets.remove(namespace: cacheNamespace, sid: sessionId, key: "deck", expectedEpoch: requestEpoch)
            return nil
        }
        guard http.statusCode == 200 else { throw AgentError.http(http.statusCode, "") }
        let deck = try JSONDecoder().decode(DeckEnvelope.self, from: data).deck
        try await offlineAssets.save(data, namespace: cacheNamespace, sid: sessionId, key: "deck", expectedEpoch: requestEpoch)
        return deck
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
        guard res.status else { throw AgentError.http(200, L("定时任务读取未被受理，请刷新重试。")) }
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
        guard res.status else { throw AgentError.http(200, L("运行历史读取未被受理，请刷新重试。")) }
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

    /// Contract P：审批策略（GET 读全量；PATCH 只发改动的键，回全量）。旧后端没有该路由 → 404 抛出。
    func approvalPolicy() async throws -> ApprovalPolicy {
        try await send(try request("GET", "/api/v1/approval-policy", timeout: 10))
    }

    func updateApprovalPolicy(_ patch: ApprovalPolicyPatch) async throws -> ApprovalPolicy {
        try await send(try request("PATCH", "/api/v1/approval-policy", json: patch, timeout: 10))
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
