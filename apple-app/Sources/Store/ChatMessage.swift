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
    func canModifyLatestAttempt(canonicalIndex: Int?, auditOnly: Bool) -> Bool {
        !auditOnly && turnIndex != nil && turnIndex == canonicalIndex
    }

    static func statusLabel(_ status: String) -> String {
        switch status {
        case "success": return L("成功")
        case "fail": return L("失败")
        case "warning": return L("警告")
        case "cancelled": return L("已取消")
        default: return status
        }
    }

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
        let abandoned = turn.status == "warning" && (turn.response ?? []).contains { message in
            message.content == "The suspended task was interrupted by a new user message and abandoned (not continued)." ||
            (message.toolResponses ?? []).contains { $0.response == "Operation cancelled: the user sent a new message without approving." }
        }
        let stopped = turn.status == "cancelled" || turn.status == "fail" || abandoned
        if stopped {
            out.append(.system(L(abandoned
                ? "上一任务已被新消息中止，后续执行已停止。已完成的副作用不会自动撤销；请查看执行记录后决定是否重试。"
                : turn.status == "cancelled" ? "本轮已取消，后续执行已停止。已完成的副作用不会自动撤销；请查看执行记录后决定是否重试。"
                : "本轮执行失败，后续执行已停止。已完成的副作用不会自动撤销；请查看执行记录后决定是否重试。")))
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
                    if item.response == "Operation cancelled: the user sent a new message without approving." {
                        out.append(.tool(label: (item.name ?? L("工具")) + " · " + L("未执行"),
                                         detail: L("此待审批调用已因新消息作废，未获批执行。")))
                        continue
                    }
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
                if stopped { continue }
                if let content = msg.content, !content.isEmpty {
                    out.append(.system(content))
                }
            default:
                if let content = msg.content, !content.isEmpty {
                    out.append(.system("[\(msg.role)] \(content)"))
                }
            }
        }
        // Reuse the ordinary tool disclosure rows for journal facts in both live and history paths.
        let hasToolMessages = out.contains { $0.kind == .tool }
        if stopped || !hasToolMessages, let facts = turn.executionReceipts {
            for item in facts.items where item.type == "tool_started" || item.type == "tool_result" {
                let started = item.type == "tool_started"
                var detail = item.detail ?? ""
                if started && item.truncated == true {
                    detail += (detail.isEmpty ? "" : "\n") + L("参数已省略，记录不完整。")
                } else if item.truncated == true {
                    detail += "\n" + L("记录已截断，部分内容未保留。")
                }
                if !started {
                    detail += "\n" + L("工具已返回；返回状态不保证外部操作成功。")
                    if let status = item.status { detail += "\n" + L("返回状态：") + status }
                }
                // A retained response may already contain this exact result (not a proposed call).
                // Enrich that same disclosure row instead of duplicating its complete payload.
                if !started, let existing = out.firstIndex(where: {
                    $0.kind == .tool && $0.toolLabel == "\(item.name) \(L("返回"))" &&
                    $0.toolDetail == (item.detail ?? "")
                }) {
                    out[existing].toolLabel = item.name + " · " + L("已返回")
                    out[existing].toolDetail = detail
                    continue
                }
                out.append(.tool(label: item.name + " · " + L(started ? "已开始" : "已返回"), detail: detail))
            }
            if facts.truncated == true { out.append(.system(L("执行记录不完整，不能据此排除其他副作用。"))) }
            if stopped && facts.items.isEmpty { out.append(.system(L("本轮未记录工具事件；这不能证明没有副作用。"))) }
        } else if stopped && turn.executionReceipts == nil {
            out.append(.system(L("本轮执行记录不可用；请核实已完成的操作后再重试。")))
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

/// Server projection validation only. Family/root legality belongs to the backend.
enum HistoryProjection {
    static func indices(_ history: HistoryResponse) -> [Int] {
        let count = history.turns.count
        guard let requested = history.displayTurnIndices,
              (count == 0 || !requested.isEmpty), Set(requested).count == requested.count,
              requested.allSatisfy({ $0 >= 0 && $0 < count }) else { return Array(0..<count) }
        let validGroups = validGroups(history, displayed: requested)
        let covered = Set(requested).union(validGroups.flatMap(\.turnIndices))
        guard covered == Set(0..<count) else { return Array(0..<count) }
        return requested
    }

    static func groups(_ history: HistoryResponse, displayed: [Int]) -> [RetryGroup] {
        guard displayed == history.displayTurnIndices else { return [] }
        let groups = validGroups(history, displayed: displayed)
        let covered = Set(displayed).union(groups.flatMap(\.turnIndices))
        guard covered == Set(history.turns.indices) else { return [] }
        return groups
    }

    private static func validGroups(_ history: HistoryResponse, displayed: [Int]) -> [RetryGroup] {
        var winners = Set<Int>()
        return (history.retryGroups ?? []).filter { group in
            let indices = group.turnIndices
            guard displayed.contains(group.displayIndex), indices.count >= 2,
                  Set(indices).count == indices.count,
                  indices.allSatisfy({ $0 >= 0 && $0 < history.turns.count }),
                  indices.contains(group.rootIndex), indices.contains(group.displayIndex),
                  winners.insert(group.displayIndex).inserted else { return false }
            return true
        }
    }
}
