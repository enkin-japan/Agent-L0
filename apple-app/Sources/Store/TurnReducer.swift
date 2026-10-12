import Foundation

// 事件归约器(PROTOCOL.md §5 的原生端实现)。
// 纯函数:reduce(state, envelope) -> state,零依赖、不碰 UI——行为由
// protocol/fixtures/ 夹具锁定(与 web 端 lib/turn-reducer.ts 共享同一批夹具,
// Tests/reducer_fixtures_test.swift 回放断言)。不透明子树(transcript 消息、
// pending_tools、task)原样以 JSONValue 承载,保证与夹具逐字节同构。

struct NormalizedStep: Equatable {
    let kind: String          // "tool" | "plan"
    var label: String
    var detail: String
    var status: String        // "running" | "ok" | "timeout" | "failed"
    /// 内部簿记:tool_started/result 的配对键(夹具比对时剥除,不进公共形状)。
    var callId: String?
}

struct NormalizedPreview: Equatable {
    var prompt = ""
    var text = ""
    var thinking = ""
    var steps: [NormalizedStep] = []
    var task: JSONValue? = nil
}

struct NormalizedState: Equatable {
    /// none | queued | running | awaiting_approval | suspended
    var turnState = "none"
    /// 已完结轮次的消息(turn_ended 对账并入:user 前置 + response 原样)。
    var transcript: [JSONValue] = []
    var preview = NormalizedPreview()
    var pendingTools: [JSONValue] = []
}

enum TurnReducer {
    static func reduce(_ state: NormalizedState, _ env: EventEnvelope) -> NormalizedState {
        var s = state
        let d = env.data
        switch env.type {
        case "turn_queued":
            // 新轮开始:预览整体重置(上一轮已以 turn_ended 收尾或被放弃收尾)
            s.turnState = "queued"
            s.preview = NormalizedPreview()
            s.preview.prompt = env.str("prompt")
            s.pendingTools = []

        case "turn_started":
            s.turnState = "running"

        case "token_delta":
            s.preview.text += env.str("text")

        case "thinking_delta":
            s.preview.thinking += env.str("text")

        case "tool_call_pending":
            // 生成中预告:工具名在执行前先亮出(无 call_id;tool_started 到达时被收编)
            s.preview.steps.append(NormalizedStep(
                kind: "tool", label: env.str("name"), detail: "",
                status: "pending", callId: nil))

        case "tool_started":
            let name = env.str("name")
            let started = NormalizedStep(kind: "tool", label: name, detail: "",
                                         status: "running", callId: optionalStr(d["call_id"]))
            // 收编同名 pending 步(生成预告→真正执行,不重复占行);无则追加
            if let i = s.preview.steps.firstIndex(where: { $0.status == "pending" && $0.label == name }) {
                s.preview.steps[i] = started
            } else {
                s.preview.steps.append(started)
            }

        case "tool_progress":
            // 执行中的进度:按 call_id 更新对应 running 步的 detail(无 call_id 更新最后一个 running 步)
            let label = [env.str("label"), env.str("detail")]
                .filter { !$0.isEmpty }.joined(separator: " · ")
            let callId = optionalStr(d["call_id"])
            for i in stride(from: s.preview.steps.count - 1, through: 0, by: -1) {
                let step = s.preview.steps[i]
                let match = callId != nil ? step.callId == callId : step.status == "running"
                if match && step.status == "running" {
                    s.preview.steps[i].detail = label
                    break
                }
            }

        case "tool_result":
            let callId = optionalStr(d["call_id"])
            for i in s.preview.steps.indices
            where s.preview.steps[i].callId != nil && s.preview.steps[i].callId == callId {
                s.preview.steps[i].detail = env.str("response")
                s.preview.steps[i].status = env.str("status") == "timeout" ? "timeout" : "ok"
            }

        case "plan_updated":
            let task = d["task"] ?? .null
            s.preview.task = task == .null ? nil : task

        case "step_completed":
            s.preview.steps.append(NormalizedStep(
                kind: "plan", label: env.str("title"), detail: env.str("result"),
                status: "ok", callId: nil))

        case "step_failed":
            s.preview.steps.append(NormalizedStep(
                kind: "plan", label: env.str("title"), detail: env.str("reason"),
                status: "failed", callId: nil))

        case "awaiting_approval":
            s.turnState = "awaiting_approval"
            if case .array(let tools)? = d["pending_tools"] { s.pendingTools = tools }
            else { s.pendingTools = [] }

        case "suspended":
            s.turnState = "suspended"

        case "resumed":
            // 重新排队(随后有新的 turn_started);审批卡随决议收起
            s.turnState = "queued"
            s.pendingTools = []

        case "turn_ended":
            // 唯一权威终态:预览撤下,轮次消息(user 前置 + response 原样)并入 transcript
            var transcript = s.transcript
            if d["discarded"] != .bool(true) {
                transcript.append(.object(["role": .string("user"),
                                           "content": .string(env.str("prompt"))]))
                if case .array(let response)? = d["response"] {
                    transcript.append(contentsOf: response)
                }
            }
            s = NormalizedState()
            s.transcript = transcript

        default:
            break // 未知事件类型:忽略(向前兼容,协议硬性要求)
        }
        return s
    }

    private static func optionalStr(_ v: JSONValue?) -> String? {
        if case .string(let s)? = v { return s }
        return nil
    }
}

extension NormalizedState {
    /// 规范形 JSON 投影(夹具比对用;`_` 前缀内部簿记不出现)。
    func toJSONValue() -> JSONValue {
        .object([
            "turn_state": .string(turnState),
            "transcript": .array(transcript),
            "preview": .object([
                "prompt": .string(preview.prompt),
                "text": .string(preview.text),
                "thinking": .string(preview.thinking),
                "steps": .array(preview.steps.map {
                    .object(["kind": .string($0.kind), "label": .string($0.label),
                             "detail": .string($0.detail), "status": .string($0.status)])
                }),
                "task": preview.task ?? .null,
            ]),
            "pending_tools": .array(pendingTools),
        ])
    }
}

extension JSONValue: Equatable {
    static func == (lhs: JSONValue, rhs: JSONValue) -> Bool {
        switch (lhs, rhs) {
        case (.null, .null): return true
        case (.bool(let a), .bool(let b)): return a == b
        case (.number(let a), .number(let b)): return a == b
        case (.string(let a), .string(let b)): return a == b
        case (.array(let a), .array(let b)): return a == b
        case (.object(let a), .object(let b)): return a == b
        default: return false
        }
    }
}
