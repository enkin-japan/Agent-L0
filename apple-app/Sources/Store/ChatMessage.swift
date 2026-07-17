import Foundation

/// 内容搜索命中（会话 + 首处命中摘录）。
struct SearchHit: Identifiable {
    let id = UUID()
    let sessionId: String
    let title: String
    let snippet: String
    let lastUpdated: Double?
}

/// UI 侧消息模型：把后端轮次（Turn.response 的 role 消息）铺平成可渲染列表。
struct ChatMessage: Identifiable {
    enum Kind {
        case user
        case assistant
        case system
        /// 工具调用/返回的折叠行（detail 收在 DisclosureGroup 里）
        case tool
    }

    let id = UUID()
    let kind: Kind
    var text: String
    var thoughts: String?
    /// tool 行的标题（"调用 web_research" / "web_research 返回"）
    var toolLabel: String?
    /// tool 行的完整内容（展开后显示）
    var toolDetail: String?
    /// 渲染格（R3）：citations 试点的结构化载荷；空 = 按 toolDetail 画灰框（现状）
    var citations: [CitationItem] = []
    /// 所属轮次在 history.jsonl 里的下标（分支锚点）。仅已落盘的轮次有值。
    var turnIndex: Int?
    /// 生成指标（附在轮次最后一条助手消息下的小字，如 "128 tok · 24.3 tok/s"）
    var meta: String?
    /// 轮次完结时间（同轮消息同值）。旧历史行没有 timestamp 字段 → nil 不显示。
    var timestamp: Date?

    static func user(_ text: String) -> ChatMessage {
        ChatMessage(kind: .user, text: text)
    }

    static func assistant(_ text: String, thoughts: String? = nil) -> ChatMessage {
        ChatMessage(kind: .assistant, text: text, thoughts: thoughts)
    }

    static func system(_ text: String) -> ChatMessage {
        ChatMessage(kind: .system, text: text)
    }

    static func tool(label: String, detail: String) -> ChatMessage {
        ChatMessage(kind: .tool, text: "", toolLabel: label, toolDetail: detail)
    }
}

extension ChatMessage {
    /// 一轮 Turn → 消息列表（历史加载与 turn_end 对账共用同一映射，保证两条路径渲染一致）。
    /// includePrompt=false 用于 approval/resume 的增量轮（没有新的用户消息）。
    /// turnIndex：该轮在落盘历史里的下标（分支锚点），未落盘轮传 nil。
    static func from(turn: Turn, includePrompt: Bool = true, turnIndex: Int? = nil) -> [ChatMessage] {
        var out: [ChatMessage] = []
        if includePrompt, let prompt = turn.prompt, !prompt.isEmpty {
            var text = prompt
            let names = (turn.attachments ?? []).compactMap { $0.name }
            let mediaCount = (turn.image?.count ?? 0) + (turn.audio?.count ?? 0)
            if !names.isEmpty || mediaCount > 0 {
                var parts = names
                if mediaCount > 0 { parts.append("\(L("媒体")) ×\(mediaCount)") }
                text += "\n📎 " + parts.joined(separator: "、")
            }
            out.append(.user(text))
        }
        for msg in turn.response ?? [] {
            switch msg.role {
            case "assistant":
                if let calls = msg.toolCalls, !calls.isEmpty {
                    for call in calls {
                        if call.type == "error" {
                            out.append(.tool(label: L("工具调用解析失败"),
                                             detail: call.errorMsg ?? ""))
                        } else {
                            let name = call.name ?? "unknown"
                            out.append(.tool(label: "\(L("调用")) \(name)",
                                             detail: call.arguments?.compact(max: 4000) ?? "{}"))
                        }
                    }
                    // 带 tool_calls 的 assistant 消息也可能有过程性思考
                    if let thoughts = msg.thoughts, !thoughts.isEmpty, out.last != nil {
                        var last = out.removeLast()
                        last.thoughts = thoughts
                        out.append(last)
                    }
                } else if let content = msg.content, !content.isEmpty {
                    out.append(.assistant(content, thoughts: msg.thoughts))
                }
            case "tool":
                for item in msg.toolResponses ?? [] {
                    var m = ChatMessage.tool(label: "\(item.name ?? L("工具")) \(L("返回"))",
                                             detail: item.response ?? "")
                    // 渲染格分派（R3，与 web 的 renderByHint 同一决策表）：
                    // 认识的 hint 走结构化渲染，不认识/缺载荷降级灰框——fail-open
                    if item.render?.hint == "citations" {
                        m.citations = CitationItem.from(item.render?.payload)
                    }
                    out.append(m)
                }
            case "system":
                if let content = msg.content, !content.isEmpty {
                    out.append(.system(content))
                }
            default:
                if let content = msg.content, !content.isEmpty {
                    out.append(.system("[\(msg.role)] \(content)"))
                }
            }
        }
        if let turnIndex {
            for i in out.indices { out[i].turnIndex = turnIndex }
        }
        if let ts = turn.timestamp {
            let date = Date(timeIntervalSince1970: ts)
            for i in out.indices { out[i].timestamp = date }
        }
        // 生成指标附在本轮最后一条助手消息下
        if let tokens = turn.generationTokens, tokens > 0,
           let lastAssistant = out.lastIndex(where: { $0.kind == .assistant }) {
            var meta = "\(Int(tokens)) tok"
            if let tps = turn.generationTps, tps > 0 {
                meta += String(format: " · %.1f tok/s", tps)
            }
            out[lastAssistant].meta = meta
        }
        return out
    }
}
