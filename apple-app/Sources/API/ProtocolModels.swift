import Foundation

// 事件账本协议 v1(仓库根 PROTOCOL.md §2)。与 web 端 types/protocol.ts 同源于协议文档,
// 行为一致性由 protocol/fixtures/ 夹具锁定(Tests/reducer_fixtures_test.swift 回放)。

/// 事件信封:seq 会话内单调递增(重连游标),data 形状随 type(松散容器,按需窄化)。
struct EventEnvelope: Codable {
    let seq: Int
    let turnId: String?
    let ts: Double?
    let type: String
    let data: [String: JSONValue]

    enum CodingKeys: String, CodingKey {
        case seq, ts, type, data
        case turnId = "turn_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        seq = (try? c.decode(Int.self, forKey: .seq)) ?? 0
        turnId = try? c.decodeIfPresent(String.self, forKey: .turnId)
        ts = try? c.decodeIfPresent(Double.self, forKey: .ts)
        type = (try? c.decode(String.self, forKey: .type)) ?? "unknown"
        data = (try? c.decode([String: JSONValue].self, forKey: .data)) ?? [:]
    }

    init(seq: Int, turnId: String?, ts: Double?, type: String, data: [String: JSONValue]) {
        self.seq = seq
        self.turnId = turnId
        self.ts = ts
        self.type = type
        self.data = data
    }
}

extension EventEnvelope {
    /// data 里的字符串字段(缺失/非字符串给空串——账本字段契约由夹具锁定)。
    func str(_ key: String) -> String {
        if case .string(let s)? = data[key] { return s }
        return ""
    }

    /// 把 data 的某个子树重解码成强类型(Turn / TaskSnapshot / [PendingTool] 等)。
    func decode<T: Decodable>(_ type: T.Type, at key: String? = nil) -> T? {
        let node: JSONValue = key.map { data[$0] ?? .null } ?? .object(data)
        guard let raw = try? JSONEncoder().encode(node) else { return nil }
        return try? JSONDecoder().decode(T.self, from: raw)
    }
}

/// GET /chat/{sid}/turn 的 turn 字段:进行中轮次的可恢复状态(PROTOCOL.md §2.5)。
struct TurnStateSnapshot: Codable {
    let turnId: String?
    /// queued | running | awaiting_approval | suspended
    let state: String
    let prompt: String?
    let partialText: String?
    let partialThinking: String?
    let pendingTools: [PendingTool]?
    let task: TaskSnapshot?
    let firstSeq: Int?
    let lastSeq: Int?
    let queuePosition: Int?

    enum CodingKeys: String, CodingKey {
        case state, prompt, task
        case turnId = "turn_id"
        case partialText = "partial_text"
        case partialThinking = "partial_thinking"
        case pendingTools = "pending_tools"
        case firstSeq = "first_seq"
        case lastSeq = "last_seq"
        case queuePosition = "queue_position"
    }
}

struct TurnStateResponse: Codable {
    let status: Bool
    let turn: TurnStateSnapshot?
    let lastSeq: Int?

    enum CodingKeys: String, CodingKey {
        case status, turn
        case lastSeq = "last_seq"
    }
}

/// POST /turns(/approval|/resume) 的 202 受理回执。
struct TurnAccepted: Codable {
    let status: Bool
    let turnId: String?
    let seq: Int?
    /// B3 幂等键命中：202 返回的是既有轮（可能早已完结），客户端须重新对账而非等事件。
    let duplicate: Bool?

    enum CodingKeys: String, CodingKey {
        case status, seq, duplicate
        case turnId = "turn_id"
    }
}

/// 受理冲突(409):会话已有排队/运行中的轮次,或决议/恢复的目标态不存在。
struct TurnConflictError: LocalizedError {
    let reason: String
    var errorDescription: String? { "turn conflict: \(reason)" }
}

extension FeedItem {
    /// data 里的字符串字段(缺失/非字符串给空串,与 EventEnvelope.str 同契约)。
    func str(_ key: String) -> String {
        if case .string(let s)? = data?[key] { return s }
        return ""
    }

    /// awaiting_approval 的 pending_tools 工具名列表(通知正文 full 档用)。
    func toolNames() -> [String] {
        guard case .array(let arr)? = data?["pending_tools"] else { return [] }
        return arr.compactMap {
            if case .object(let o) = $0, case .string(let n)? = o["name"] { return n }
            return nil
        }
    }
}

/// 全局任务 feed 信封(PROTOCOL.md §2.7):跨会话生命周期事件 + 独立 feed_seq 游标。
struct FeedItem: Codable {
    let feedSeq: Int
    let sessionId: String?
    let turnId: String?
    let ts: Double?
    let type: String
    let data: [String: JSONValue]?

    enum CodingKeys: String, CodingKey {
        case ts, type, data
        case feedSeq = "feed_seq"
        case sessionId = "session_id"
        case turnId = "turn_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        feedSeq = (try? c.decode(Int.self, forKey: .feedSeq)) ?? 0
        sessionId = try? c.decodeIfPresent(String.self, forKey: .sessionId)
        turnId = try? c.decodeIfPresent(String.self, forKey: .turnId)
        ts = try? c.decodeIfPresent(Double.self, forKey: .ts)
        type = (try? c.decode(String.self, forKey: .type)) ?? "unknown"
        data = try? c.decodeIfPresent([String: JSONValue].self, forKey: .data)
    }
}
