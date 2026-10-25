import Foundation

// 与 frontend/src/types/agent-api.ts 逐一对应（只取 v1 需要的子集）。
// 字段全部走宽容解码：历史落盘轮次可能缺字段，缺了不应让整页历史解不出来。

struct ToolCallInfo: Codable {
    let type: String?
    let name: String?
    let arguments: [String: JSONValue]?
    let errorMsg: String?

    enum CodingKeys: String, CodingKey {
        case type, name, arguments
        case errorMsg = "error_msg"
    }
}

struct ToolResponseItem: Codable {
    let name: String?
    let response: String?
    /// 渲染格（R3/PROTOCOL §2.3.1）：工具声明的客户端渲染载荷。缺失/不认识 = 灰框。
    let render: ToolRender?
}

/// 工具渲染格：`hint` 刻意不收窄成枚举（web 端同一决定）——新 hint 只是"这版
/// app 不认识 → 灰框"，不是解码失败把整条消息拖下水。
struct ToolRender: Codable {
    let hint: String
    let payload: JSONValue?
}

/// citations 载荷的一条（rag_search 试点，R3）。字段容错：缺哪个都不崩。
struct CitationItem {
    let source: String
    let path: String?
    let score: Double?
    let snippet: String?

    static func from(_ payload: JSONValue?) -> [CitationItem] {
        guard case .object(let obj)? = payload,
              case .array(let items)? = obj["items"] else { return [] }
        return items.compactMap { item in
            guard case .object(let o) = item else { return nil }
            func str(_ k: String) -> String? {
                if case .string(let s)? = o[k] { return s }; return nil
            }
            func num(_ k: String) -> Double? {
                if case .number(let n)? = o[k] { return n }; return nil
            }
            guard let source = str("source") else { return nil }
            return CitationItem(source: source, path: str("path"),
                                score: num("score"), snippet: str("snippet"))
        }
    }
}

/// 轮次里的一条消息（assistant / tool / system）。role 之外的字段按需出现。
struct ResponseMessage: Codable {
    let role: String
    let content: String?
    let thoughts: String?
    let toolCalls: [ToolCallInfo]?
    let toolResponses: [ToolResponseItem]?

    enum CodingKeys: String, CodingKey {
        case role, content, thoughts
        case toolCalls = "tool_calls"
        case toolResponses = "tool_responses"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        role = (try? c.decode(String.self, forKey: .role)) ?? "system"
        content = try? c.decodeIfPresent(String.self, forKey: .content)
        thoughts = try? c.decodeIfPresent(String.self, forKey: .thoughts)
        toolCalls = try? c.decodeIfPresent([ToolCallInfo].self, forKey: .toolCalls)
        toolResponses = try? c.decodeIfPresent([ToolResponseItem].self, forKey: .toolResponses)
    }
}

struct PendingTool: Codable {
    let name: String
    let arguments: [String: JSONValue]?
}

struct AttachmentInfo: Codable {
    let path: String?
    let type: String?
    let name: String?
}

/// 一轮问答（POST /chat 响应体 = history turns 元素 = SSE turn_end 数据）。
struct ExecutionReceipt: Codable {
    let seq: Int?
    let ts: Double?
    let type: String
    let name: String
    let callId: String?
    let detail: String?
    let status: String?
    let truncated: Bool?
    enum CodingKeys: String, CodingKey {
        case seq, ts, type, name, detail, status, truncated
        case callId = "call_id"
    }
}

struct ExecutionReceipts: Codable {
    let items: [ExecutionReceipt]
    let truncated: Bool?
}

struct Turn: Codable {
    let status: String
    let prompt: String?
    let image: [String]?
    let audio: [String]?
    let ingestedText: String?
    let attachments: [AttachmentInfo]?
    let response: [ResponseMessage]?
    let pendingTools: [PendingTool]?
    let promptTokens: Double?
    let generationTokens: Double?
    let generationTps: Double?
    /// 轮次完结 epoch 秒（PROTOCOL.md §2.3 additive，2026-07-17）。旧历史行没有。
    let timestamp: Double?
    var executionReceipts: ExecutionReceipts? = nil
    var turnRef: String? = nil
    var retryOf: String? = nil
    var retryRootIndex: Int? = nil
    var retryParentIndex: Int? = nil

    enum CodingKeys: String, CodingKey {
        case status, prompt, image, audio, response, attachments, timestamp
        case turnRef = "turn_ref"
        case retryOf = "retry_of"
        case retryRootIndex = "retry_root_index"
        case retryParentIndex = "retry_parent_index"
        case executionReceipts = "execution_receipts"
        case ingestedText = "ingested_text"
        case pendingTools = "pending_tools"
        case promptTokens = "prompt_tokens"
        case generationTokens = "generation_tokens"
        case generationTps = "generation_tps"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        status = try c.decode(String.self, forKey: .status)
        prompt = try? c.decodeIfPresent(String.self, forKey: .prompt)
        image = try? c.decodeIfPresent([String].self, forKey: .image)
        audio = try? c.decodeIfPresent([String].self, forKey: .audio)
        ingestedText = try? c.decodeIfPresent(String.self, forKey: .ingestedText)
        attachments = try? c.decodeIfPresent([AttachmentInfo].self, forKey: .attachments)
        response = try? c.decodeIfPresent([ResponseMessage].self, forKey: .response)
        pendingTools = try? c.decodeIfPresent([PendingTool].self, forKey: .pendingTools)
        promptTokens = try? c.decodeIfPresent(Double.self, forKey: .promptTokens)
        generationTokens = try? c.decodeIfPresent(Double.self, forKey: .generationTokens)
        generationTps = try? c.decodeIfPresent(Double.self, forKey: .generationTps)
        timestamp = try? c.decodeIfPresent(Double.self, forKey: .timestamp)
        executionReceipts = try? c.decodeIfPresent(ExecutionReceipts.self, forKey: .executionReceipts)
        turnRef = try? c.decodeIfPresent(String.self, forKey: .turnRef)
        retryOf = try? c.decodeIfPresent(String.self, forKey: .retryOf)
        retryRootIndex = try? c.decodeIfPresent(Int.self, forKey: .retryRootIndex)
        retryParentIndex = try? c.decodeIfPresent(Int.self, forKey: .retryParentIndex)
    }

}

struct SessionMetaItem: Codable, Identifiable, Hashable {
    let sessionId: String
    let title: String?
    let turnCount: Int?
    let createdAt: Double?
    let lastUpdated: Double?
    let pinned: Bool?
    let active: Bool?

    var id: String { sessionId }

    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case title
        case turnCount = "turn_count"
        case createdAt = "created_at"
        case lastUpdated = "last_updated"
        case pinned, active
    }
}

struct SessionListResponse: Codable {
    let status: Bool
    let sessions: [SessionMetaItem]
    var retentionPolicy: SessionRetentionPolicy? = nil
    enum CodingKeys: String, CodingKey {
        case status, sessions
        case retentionPolicy = "retention_policy"
    }
}

struct SessionRetentionPolicy: Codable, Equatable {
    let enabled: Bool
    let retentionDays: Double
    let emptyTTLHours: Double
    let scanIntervalSeconds: Int
    enum CodingKeys: String, CodingKey {
        case enabled
        case retentionDays = "retention_days"
        case emptyTTLHours = "empty_ttl_hours"
        case scanIntervalSeconds = "scan_interval_seconds"
    }
}

struct RetryGroup: Codable {
    let rootIndex: Int
    let displayIndex: Int
    let turnIndices: [Int]
    enum CodingKeys: String, CodingKey {
        case rootIndex = "root_index"
        case displayIndex = "display_index"
        case turnIndices = "turn_indices"
    }
}

struct HistoryResponse: Codable {
    let status: Bool
    let sessionId: String
    let turns: [Turn]
    var replayAfter: Int? = nil
    var generation: String? = nil
    var lastTurnRef: String? = nil
    var displayTurnIndices: [Int]? = nil
    var retryGroups: [RetryGroup]? = nil

    init(status: Bool, sessionId: String, turns: [Turn], replayAfter: Int? = nil,
         generation: String? = nil, lastTurnRef: String? = nil,
         displayTurnIndices: [Int]? = nil, retryGroups: [RetryGroup]? = nil) {
        self.status = status; self.sessionId = sessionId; self.turns = turns
        self.replayAfter = replayAfter; self.generation = generation; self.lastTurnRef = lastTurnRef
        self.displayTurnIndices = displayTurnIndices; self.retryGroups = retryGroups
    }

    enum CodingKeys: String, CodingKey {
        case status, turns, generation
        case lastTurnRef = "last_turn_ref"
        case displayTurnIndices = "display_turn_indices"
        case retryGroups = "retry_groups"
        case replayAfter = "replay_after"
        case sessionId = "session_id"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        status = try c.decode(Bool.self, forKey: .status)
        sessionId = try c.decode(String.self, forKey: .sessionId)
        turns = try c.decode([Turn].self, forKey: .turns)
        replayAfter = try? c.decodeIfPresent(Int.self, forKey: .replayAfter)
        generation = try? c.decodeIfPresent(String.self, forKey: .generation)
        lastTurnRef = try? c.decodeIfPresent(String.self, forKey: .lastTurnRef)
        displayTurnIndices = try? c.decodeIfPresent([Int].self, forKey: .displayTurnIndices)
        retryGroups = try? c.decodeIfPresent([RetryGroup].self, forKey: .retryGroups)
    }

}

struct SimpleActionResponse: Codable {
    let status: Bool
    let response: String?
    var reason: String? = nil
}

struct PinResponse: Codable {
    let status: Bool
    let pinned: Bool?
    let response: String?
}

struct TaskStep: Codable, Identifiable {
    let id: Int
    let title: String
    let status: String
    let note: String?
}

struct TaskSnapshot: Codable {
    let goal: String?
    let plan: [TaskStep]?
}

struct ActivityStep: Codable {
    let kind: String?
    let label: String?
    let detail: String?
}

struct LiveActivity: Codable {
    let phase: String?
    let current: String?
    let steps: [ActivityStep]?
}

struct TaskStatusResponse: Codable {
    let status: Bool
    let active: Bool?
    let generating: Bool?
    let task: TaskSnapshot?
    let activity: LiveActivity?
}

/// 全局单 worker 调度快照里的一个任务句柄（GET /api/v1/tasks）。
struct TaskHandleInfo: Codable, Identifiable {
    let sessionId: String
    /// running / queued / suspended
    let status: String
    /// chat / resume / summarize / scheduled …
    let kind: String?
    let waitingSeconds: Double?
    let runningSeconds: Double?

    var id: String { sessionId }

    enum CodingKeys: String, CodingKey {
        case status, kind
        case sessionId = "session_id"
        case waitingSeconds = "waiting_seconds"
        case runningSeconds = "running_seconds"
    }
}

struct GlobalTasksResponse: Codable {
    let status: Bool
    let running: [String]?   // 占用 worker 的 session_id 们（多 worker 化后为列表）
    let tasks: [TaskHandleInfo]
}

/// 已注册工具（GET /api/v1/tools）。
struct ToolInfo: Codable, Identifiable {
    let name: String
    let description: String?
    let requiresApproval: Bool?

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, description
        case requiresApproval = "requires_approval"
    }
}

struct ToolsResponse: Codable {
    let status: Bool
    let tools: [ToolInfo]
}

/// 剧本条目（GET /api/v1/skills，斜杠候选提示用 + 设置里的剧本管理面板）。
/// available=false = 缺依赖被探活舍弃（missing 列缺什么），候选里灰显——
/// 点名它仍会发出：后端会前置自动重扫，依赖刚装好就即时可用。
/// source/file 是新增字段（旧后端无 → nil，按 repo/只读处理）：
/// repo=仓库内置（只读）| user=UI 手写（可编辑/删除）。
struct SkillItem: Codable, Identifiable {
    let name: String
    let description: String
    let available: Bool?
    let missing: [String]?
    let source: String?
    let file: String?
    // 仅 user 来源的可用条目携带（编辑器预填用）；内置/舍弃条目为 nil
    let body: String?
    let requires: [String]?

    var id: String { name }
    var isUserOwned: Bool { source == "user" }
}

struct SkillsResponse: Codable {
    let status: Bool
    let skills: [SkillItem]
}

/// 剧本增删响应（POST/DELETE /api/v1/skills*）。effect 语义随端点：
/// 保存 = registered（即时生效）/ saved_unavailable（缺依赖）/ saved_norebuild（重启生效）；
/// 删除 = removed（即时下架）/ removed_norebuild（重启下架）。
struct SkillActionResponse: Codable {
    let status: Bool?
    let effect: String
    let detail: String?
}

/// 会话产物文件（GET /api/v1/files/{sid}，检查器「文件」页签）。
/// 只含 assets/（Agent 生成/制作/下载的规范出口）。
struct AssetFileItem: Codable, Identifiable, Equatable {
    let name: String
    let size: Int64
    let mtime: Double

    var id: String { name }
    /// 出货路径（fetchAsset / 流式播放共用口径）
    func assetPath(sessionId: String) -> String {
        "/api/v1/files/\(sessionId)/assets/\(name)"
    }
}

struct SessionFilesResponse: Codable {
    let status: Bool
    let files: [AssetFileItem]
}

struct SystemPromptResponse: Codable {
    let status: Bool
    let systemPrompt: String?
    /// "session"=会话在用；"default"=会话未创建时的默认值
    let source: String?

    enum CodingKeys: String, CodingKey {
        case status, source
        case systemPrompt = "system_prompt"
    }
}

struct MemoryStatusResponse: Codable {
    let status: Bool
    let loaded: Bool?
    let lastPromptTokens: Int?
    let totalTurns: Int?
    let summarizedTurns: Int?
    let sedimentedTurns: Int?
    let hasSummary: Bool?
    let summaryChars: Int?
    let highThreshold: Int?
    let lowThreshold: Int?
    let keepTailTurns: Int?
    let sedimentToRag: Bool?

    enum CodingKeys: String, CodingKey {
        case status, loaded
        case lastPromptTokens = "last_prompt_tokens"
        case totalTurns = "total_turns"
        case summarizedTurns = "summarized_turns"
        case sedimentedTurns = "sedimented_turns"
        case hasSummary = "has_summary"
        case summaryChars = "summary_chars"
        case highThreshold = "high_threshold"
        case lowThreshold = "low_threshold"
        case keepTailTurns = "keep_tail_turns"
        case sedimentToRag = "sediment_to_rag"
    }
}

struct BranchResponse: Codable {
    let status: Bool
    let sessionId: String?
    let response: String?

    enum CodingKeys: String, CodingKey {
        case status, response
        case sessionId = "session_id"
    }
}

// ---------- RAG 知识库管理（记忆治理波 P1；backend /api/v1/rag/* 薄代理） ----------

struct RagCategory: Codable, Identifiable {
    let name: String
    let description: String
    let docCount: Int
    let chunkCount: Int

    var id: String { name }
    /// agent_memory / agent_notes 是 Agent 的记忆，UI 须与用户知识视觉区分并警示
    var isAgentMemory: Bool { name == "agent_memory" || name == "agent_notes" }

    enum CodingKeys: String, CodingKey {
        case name, description
        case docCount = "doc_count"
        case chunkCount = "chunk_count"
    }
}

struct RagOverviewResponse: Codable {
    let categories: [RagCategory]
}

// ---------- 私人 API 工具管理（backend /api/v1/api-tools；声明式工具工厂表单面） ----------

struct ApiToolItem: Codable, Identifiable {
    let file: String
    let source: String            // repo=仓库手写（只读）| user=UI 添加
    let problem: String?          // 非空 = 坏声明原因
    let name: String?
    let description: String?
    let endpoint: String?
    let method: String
    let enabled: Bool
    let params: JSONValue?        // {properties: {...}, required: [...]}
    let headers: JSONValue?
    let timeout: Double?
    let authEnv: String?
    let authHeader: String?
    let authFormat: String?
    let hasSecret: Bool
    let requiresApproval: Bool
    var approvalOverride: Bool? = nil
    let hasCanary: Bool
    let registered: Bool
    var runtimeDeclarationCurrent: Bool? = nil

    var id: String { source + "/" + file }
    var isUserOwned: Bool { source == "user" }
    var needsSetup: Bool {
        isUserOwned && problem == nil &&
            ((enabled && (!registered || (authEnv?.isEmpty == false && !hasSecret))) || runtimeDeclarationCurrent == false)
    }

    enum CodingKeys: String, CodingKey {
        case file, source, problem, name, description, endpoint, method, enabled
        case params, headers, timeout, registered
        case authEnv = "auth_env"
        case authHeader = "auth_header"
        case authFormat = "auth_format"
        case hasSecret = "has_secret"
        case requiresApproval = "requires_approval"
        case approvalOverride = "approval_override"
        case hasCanary = "has_canary"
        case runtimeDeclarationCurrent = "runtime_declaration_current"
    }
}

struct ApiToolListResponse: Codable {
    let tools: [ApiToolItem]
}

struct ApiToolActionResponse: Codable {
    let effect: String            // registered | unavailable | saved_disabled | restart_required | deleted
    let detail: String?
}

struct ApiToolTestResponse: Codable {
    let result: String
}

// ---------- 待确认注册队列（对话式注册的人工闸；backend /api/v1/registrations） ----------

/// 一份待确认注册单：模型在对话里起草的声明落在待确认区，等用户在设置页放行。
/// payload 是异构声明 dict（api_tool 一套形状、skill 另一套），照 ApiToolItem.params
/// 的做法用 JSONValue 兜住——客户端只审阅与转述，不对形状做假设。
struct RegistrationItem: Codable, Identifiable {
    let id: String
    let kind: String              // api_tool | skill
    let name: String
    let status: String?           // pending（坏文件为 corrupt）
    let proposedAt: String?
    let note: String?
    let payload: [String: JSONValue]?
    /// 仅 api_tool 且声明了 auth 时出现：auth.env 那个变量名是否已录过密钥实值
    let hasSecret: Bool?

    enum CodingKeys: String, CodingKey {
        case id, kind, name, status, note, payload
        case proposedAt = "proposed_at"
        case hasSecret = "has_secret"
    }

    var isApiTool: Bool { kind == "api_tool" }

    private func str(_ key: String) -> String? {
        if case .string(let s)? = payload?[key], !s.isEmpty { return s }
        return nil
    }

    var summary: String? { str("description") }
    var endpoint: String? { str("endpoint") }
    var method: String? { str("method") }
    /// skill 正文（点名 /名字 时注入的流程提示词）
    var skillBody: String? { str("body") }

    /// api_tool 的参数键（payload.params.properties 的键；无参数则空）
    var paramKeys: [String] {
        guard case .object(let params)? = payload?["params"],
              case .object(let props)? = params["properties"] else { return [] }
        return props.keys.sorted()
    }

    /// 鉴权变量名（auth.env）；nil = 该声明不需要密钥
    var authEnv: String? {
        guard case .object(let auth)? = payload?["auth"],
              case .string(let env)? = auth["env"], !env.isEmpty else { return nil }
        return env
    }

    /// 密钥还没录：确认前得先去 API 工具页的密钥框补 auth.env 的实值
    /// （红线①：密钥不走对话通道，声明里物理上带不了密钥）
    var secretMissing: Bool { isApiTool && authEnv != nil && hasSecret == false }

    /// 声明全文（等宽展示用）：键序稳定，斜杠不转义（地址读得出来）
    var payloadText: String {
        guard let payload else { return "{}" }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        guard let data = try? encoder.encode(JSONValue.object(payload)),
              let text = String(data: data, encoding: .utf8) else { return "{}" }
        return text
    }
}

struct RegistrationListResponse: Codable {
    let status: Bool
    let registrations: [RegistrationItem]
}

/// 确认/驳回响应。effect 语义随端点：
/// 确认 = registered（即时上架）/ saved / saved_unavailable（缺依赖或探活未过）/
/// unavailable / restart_required（重启后端后生效）/ saved_norebuild；
/// 驳回 = rejected。detail 在 saved_unavailable / unavailable 时带原因。
struct RegistrationActionResponse: Codable {
    let status: Bool?
    let effect: String
    let detail: String?
    let id: String?
}

struct RagDocument: Codable, Identifiable {
    let sourcePath: String
    let title: String
    let createdAt: String     // ISO 串；空 = 未知
    let chunks: Int
    let sizeBytes: Int
    let stale: String         // 非空 = 已标记过时（U3），值为原因

    var id: String { sourcePath }
    var createdDate: String { String(createdAt.prefix(10)) }

    enum CodingKeys: String, CodingKey {
        case title, chunks, stale
        case sourcePath = "source_path"
        case createdAt = "created_at"
        case sizeBytes = "size_bytes"
    }
}

struct RagDocumentsResponse: Codable {
    let category: String
    let documents: [RagDocument]
}

struct RagActionResponse: Codable {
    let msg: String?
    var status: Bool? = nil // Older data-plane responses use HTTP status + msg only.
}

// ---------- 定时任务 ----------

struct ScheduleSpec: Codable {
    let type: String?
    let minutes: Int?
    let at: String?
    let weekday: Int?

    /// 人话描述（与 web 侧展示口径一致）。
    var summary: String {
        switch type {
        case "interval": return "每 \(minutes ?? 0) 分钟"
        case "daily": return "每天 \(at ?? "")"
        case "weekly":
            let names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            let day = (weekday ?? 0) >= 0 && (weekday ?? 0) < 7 ? names[weekday!] : "?"
            return "每\(day) \(at ?? "")"
        case "once": return "单次 \(at ?? "")"
        default: return type ?? "未知"
        }
    }
}

struct ScheduleRun: Codable, Identifiable {
    let runId: String?
    let startedAt: Double?
    let finishedAt: Double?
    let status: String?
    let summary: String?
    let sessionId: String?

    var id: String { runId ?? UUID().uuidString }

    enum CodingKeys: String, CodingKey {
        case status, summary
        case runId = "run_id"
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case sessionId = "session_id"
    }
}

struct ScheduledTask: Codable, Identifiable {
    let id: String
    let name: String?
    let prompt: String?
    let spec: ScheduleSpec?
    let enabled: Bool?
    let nextRun: Double?
    let running: Bool?
    let lastRun: ScheduleRun?

    enum CodingKeys: String, CodingKey {
        case id, name, prompt, spec, enabled, running
        case nextRun = "next_run"
        case lastRun = "last_run"
    }
}

struct ScheduleListResponse: Codable {
    let status: Bool
    let schedules: [ScheduledTask]
}

struct ScheduleActionResponse: Codable {
    let status: Bool
    let response: String?
}

struct ScheduleRunsResponse: Codable {
    let status: Bool
    let runs: [ScheduleRun]
}

/// 生成参数（与 web 端 ChatRequestParams 对应；system 走后端默认，不在 app 端定制）。
struct ChatParams {
    var prompt: String
    var enableThinking: Bool = false
    var thinkingBudget: Int = 2048
    var maxTokens: Int = 4096
    var temperature: Double = 0.2
    var minP: Double = 0.05
    /// 提交幂等键（B3）：响应回程丢失后的重发带同键 → 服务端 202 返回原轮,不重复执行。
    var clientTurnId: String? = nil
}

/// 待上传的附件（文件/照片/录音统一形态）。
struct Upload: Identifiable {
    let id = UUID()
    let filename: String
    let mime: String
    let data: Data
}
