// Test-only dependencies. No production credentials, defaults or updater are read.
@_exported import Combine
import Foundation
import SwiftUI

enum Keychain {
    static func load(_ account: String) -> String { "" }
    static func save(_ account: String, _ key: String) {}
    static func loadAPIKey() -> String { "" }
    static func saveAPIKey(_ key: String) {}
}

@MainActor final class AppSettings: ObservableObject {
    static let localBackend = "http://127.0.0.1:14159"
    @Published var baseURLString = "https://design-preview.invalid"
    @Published var phoneSyncEnabled = true
    @Published var apiKey = "synthetic-preview-only"
    @Published var appearance = "light"
    @Published var language = "zh" {
        didSet { L10n.current = AppLanguage(rawValue: language) ?? .zh }
    }
    @Published var notifEnabled = false
    @Published var notifResults = false
    @Published var notifScheduleStart = false
    @Published var notifApproval = false
    @Published var notifContentLevel = "placeholder"
    @Published var sidecarEnabled = false
    @Published var sidecarRepoRoot = ""
    @Published var inferApiKey = ""
    @Published var ragApiKey = ""
    @Published var ragComputeApiKey = ""
    @Published var computeHost = ""
    @Published var kvPersistEnabled = false
    @Published var kvTTLDays = 14
    // Keep the real AgentStore's global/connection monitors behind their startup gate.
    @Published var setupCompleted = false

    init() { L10n.current = .zh }
    var colorScheme: ColorScheme? { appearance == "dark" ? .dark : .light }
    var baseURL: URL? { URL(string: baseURLString) }
    func makeClient() -> AgentClient? {
        guard let baseURL else { return nil }
        return AgentClient(baseURL: baseURL, apiKey: apiKey, session: DesignPreviewTransport.session(), cacheRoot: FileManager.default.temporaryDirectory.appendingPathComponent("MacAgentDesignPreview-offline-" + UUID().uuidString))
    }
}

@MainActor struct DesignPreviewUpdater {
    func checkForUpdates(_ sender: Any?) {}
}
@MainActor let sparkleUpdater = DesignPreviewUpdater()

extension Bundle {
    var appVersionLabel: String { "Design preview · synthetic fixtures" }
}

/// Intercepts every scheme/host. Never forwards a request to another transport.
/// SSE remains attached until process exit instead of completing/reconnecting in a loop.
final class DesignPreviewTransport: URLProtocol {
    static var scenario = "conversation" // Set once, before any session is created.
    private static let lock = NSLock()
    private static var held: [DesignPreviewTransport] = []
    private static var requestCount = 0
    private static var streamCount = 0
    private var sentHeaders = false

    static func session() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [DesignPreviewTransport.self]
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        configuration.timeoutIntervalForResource = 120
        return URLSession(configuration: configuration)
    }

    static var counts: (requests: Int, streams: Int) {
        lock.lock(); defer { lock.unlock() }
        return (requestCount, streamCount)
    }
    static func appendToken(seq: Int, text: String) {
        lock.lock(); let streams = held; lock.unlock()
        let event: [String: Any] = ["seq": seq, "turn_id": DesignPreviewFixtures.turnID,
            "type": "token_delta", "data": ["text": text]]
        let data = Data(("data: " + String(data: try! JSONSerialization.data(withJSONObject: event), encoding: .utf8)! + "\n\n").utf8)
        for stream in streams { stream.deliver(data, type: "text/event-stream", finish: false) }
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock(); Self.requestCount += 1; Self.lock.unlock()
        guard request.httpMethod == nil || request.httpMethod == "GET" else {
            respond(["status": false, "reason": "preview_read_only", "response": "离线预览不执行操作。"], status: 409)
            return
        }
        let path = request.url?.path ?? ""
        if path == "/health" { respond(["status": "ok"]); return }
        if path == "/api/v1/sessions" { respond(DesignPreviewFixtures.sessions); return }
        if path.hasSuffix("/skills") { respond(["status": true, "skills": []]); return }
        if path.hasSuffix("/history") { respond(DesignPreviewFixtures.history); return }
        if path.hasSuffix("/turn") { respond(DesignPreviewFixtures.turnState); return }
        if path.hasSuffix("/events") {
            Self.lock.lock(); Self.streamCount += 1; Self.held.append(self); Self.lock.unlock()
            let after = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?
                .first(where: { $0.name == "after" }).flatMap { Int($0.value ?? "") } ?? 0
            let attached: [String: Any] = ["seq": 0, "type": "stream_attached", "data": ["v": 1, "last_seq": 30, "generation": "design-preview"]]
            let events = [attached] + DesignPreviewFixtures.events.filter { ($0["seq"] as? Int ?? 0) > after }
            let bytes = events.map { "data: " + String(data: try! JSONSerialization.data(withJSONObject: $0), encoding: .utf8)! + "\n\n" }.joined()
            deliver(Data(bytes.utf8), type: "text/event-stream", finish: false)
            return
        }
        if path.hasSuffix("/tasks") {
            let tasks: [[String: Any]] = DesignPreviewFixtures.active ? [["session_id": DesignPreviewFixtures.sessionID,
                "kind": "chat", "status": Self.scenario == "approval" ? "awaiting_approval" : "running"]] : []
            respond(["status": true, "tasks": tasks, "running": []]); return
        }
        if path.hasSuffix("/schedules") { respond(["status": true, "schedules": []]); return }
        if path.hasSuffix("/tools") { respond(["status": true, "tools": []]); return }
        if path.hasSuffix("/files") { respond(["status": true, "files": []]); return }
        respond(["status": false, "reason": "preview_fixture_absent"], status: 404)
    }
    override func stopLoading() {
        Self.lock.lock(); Self.held.removeAll { $0 === self }; Self.lock.unlock()
    }
    private func respond(_ body: Any, status: Int = 200) {
        deliver(try! JSONSerialization.data(withJSONObject: body), status: status, type: "application/json", finish: true)
    }
    private func deliver(_ data: Data, status: Int = 200, type: String, finish: Bool) {
        if !sentHeaders {
            sentHeaders = true
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil,
                headerFields: ["Content-Type": type, "Cache-Control": "no-store"])!, cacheStoragePolicy: .notAllowed)
        }
        client?.urlProtocol(self, didLoad: data)
        if finish { client?.urlProtocolDidFinishLoading(self) }
    }
}

enum DesignPreviewFixtures {
    static let sessionID = "preview-kyoto-weekend"
    static let turnID = "t_preview_kyoto_approval"
    static let approvalSeq = 29
    static let timestamp = Date().timeIntervalSince1970
    static var active: Bool { ["approval", "streaming"].contains(DesignPreviewTransport.scenario) }
    static let pendingTools: [[String: Any]] = [["name": "write_file", "arguments": [
        "path": "/preview/京都周末.md", "content": "京都周末：周六东山，周日岚山。此为合成待审批示例，尚未写入。"
    ]]]
    static var sessions: [String: Any] {
        let titles = ["京都周末，慢一点也很好", "把读书笔记整理成一页", "下周的三件重要小事", "一份清爽的项目周报", "秋天的家庭晚餐"]
        return ["status": true, "sessions": titles.enumerated().map { index, title -> [String: Any] in
            ["session_id": index == 0 ? sessionID : "preview-session-\(index)", "title": title,
             "turn_count": index == 0 ? 1 : 3, "pinned": index == 0,
             "created_at": timestamp - Double(index * 86400), "last_updated": timestamp - Double(index * 86400),
             "active": index == 0 && active]
        }]
    }
    static var history: [String: Any] {
        let answer = """
        ## 留一点时间给京都
        以**少换乘、慢慢走**为主线，周六留给东山，周日再去岚山。

        | 时间 | 周六 · 东山 | 周日 · 岚山 |
        | --- | --- | --- |
        | 上午 | 清水寺 → 二年坂 | 竹林小径 → 天龙寺 |
        | 下午 | 鸭川散步、找家咖啡馆 | 渡月桥，沿河慢慢走 |

        - 住宿选京都站附近，减少带行李换乘。
        - 每天只安排一个区域，给天气和偶遇留出余地。

        > 预算按两人计算：交通与餐饮约 ¥18,000，住宿另计。这是用于界面检查的合成行程。
        """
        let compact = DesignPreviewTransport.scenario == "approval"
        let response: [[String: Any]] = compact ? [
            ["role": "assistant", "content": "**京都周末 · 慢慢走**\n\n周六：清水寺 → 二年坂 → 鸭川。\n\n周日：岚山竹林 → 天龙寺 → 渡月桥。\n\n两人交通与餐饮约 ¥18,000，住宿另计。"]
        ] : [
            ["role": "assistant", "tool_calls": [["type": "function", "name": "file_extract", "arguments": ["path": "synthetic/京都旅行清单.pdf"]]]],
            ["role": "tool", "tool_responses": [["name": "file_extract", "response": "合成资料：想去清水寺、鸭川和岚山；不安排密集景点。"]]],
            ["role": "assistant", "content": answer]
        ]
        let turn: [String: Any] = ["status": "success", "prompt": compact ? "京都周末，想慢慢走。参考这份清单。" : "想和朋友去京都过个轻松的周末，帮我排一个不赶路的两日行程。参考这份清单。",
            "attachments": [["name": "京都旅行清单.pdf", "type": "document", "path": "synthetic/京都旅行清单.pdf"]],
            "response": response, "timestamp": timestamp - 180, "generation_tokens": 328, "generation_tps": 31.4,
            "turn_ref": "preview-canonical-kyoto-1"]
        return ["status": true, "session_id": sessionID, "turns": [turn], "replay_after": 20,
                "last_turn_ref": "preview-canonical-kyoto-1", "generation": "design-preview"]
    }
    static var turnState: [String: Any] {
        guard active else { return ["status": true, "turn": NSNull(), "last_seq": 20] }
        return ["status": true, "last_seq": DesignPreviewTransport.scenario == "approval" ? approvalSeq : 28,
                "turn": ["turn_id": turnID, "state": DesignPreviewTransport.scenario == "approval" ? "awaiting_approval" : "running",
                         "prompt": "把行程整理成一份 Markdown，先让我确认保存位置。", "first_seq": 21, "last_seq": 29,
                         "pending_tools": DesignPreviewTransport.scenario == "approval" ? pendingTools : []]]
    }
    static var events: [[String: Any]] {
        guard active else { return [] }
        func event(_ seq: Int, _ type: String, _ data: [String: Any]) -> [String: Any] {
            ["seq": seq, "turn_id": turnID, "ts": timestamp + Double(seq) / 10, "type": type, "data": data]
        }
        let task: [String: Any] = ["goal": "整理京都周末行程", "plan": [
            ["id": 1, "title": "核对旅行偏好", "status": "done"],
            ["id": 2, "title": "整理两日安排", "status": "done"],
            ["id": 3, "title": "确认后保存文件", "status": "doing"]]]
        let longCode = "```swift\n" + (1...18).map { "let stop\($0) = KyotoStop(day: \($0 % 2 + 1), title: \"沿着鸭川慢慢走，给周末留一点空白\", notes: \"这是检验窄窗中长代码与水平滚动边界的合成内容\")" }.joined(separator: "\n") + "\n```"
        var result = [
            event(21, "turn_queued", ["prompt": "把行程整理成一份 Markdown，先让我确认保存位置。", "origin": "chat"]),
            event(22, "turn_started", [:]),
            event(23, "plan_updated", ["task": task]),
            event(24, "thinking_delta", ["text": "沿用已确认的慢行节奏，将资料整理成便于出门查看的清单。保存文件需要你确认。"]),
            event(25, "tool_call_pending", ["name": "file_extract"]),
            event(26, "tool_started", ["call_id": "preview-read", "name": "file_extract", "args": ["path": "synthetic/京都旅行清单.pdf"]]),
            event(27, "tool_result", ["call_id": "preview-read", "name": "file_extract", "response": "合成资料已读：两天、少换乘、散步为主。", "status": "ok"]),
            event(28, "token_delta", ["text": DesignPreviewTransport.scenario == "streaming"
                ? "### 正在整理随身行程\n\n下面保留路线数据，方便之后调整。\n\n" + longCode
                : "行程已整理好。确认后将保存到下方位置，当前还没有写入文件。"])
        ]
        if DesignPreviewTransport.scenario == "approval" {
            // Compact approval fixture keeps the decision and preceding itinerary in
            // one native viewport. Tool progress is covered by the streaming fixture.
            result = Array(result.prefix(2)) + [event(approvalSeq, "awaiting_approval", ["pending_tools": pendingTools])]
        }
        return result
    }
}
