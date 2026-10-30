import Foundation

/// 内容搜索命中（会话 + 首处命中摘录）。
struct SearchHit: Identifiable {
    let id = UUID()
    let sessionId: String
    let title: String
    let snippet: String
    let lastUpdated: Double?
}

/// 执行过程组里的一步（来自一条 `.tool` 消息；status/timestamp 仅在执行回执给得出时有值）。
struct ToolStep: Identifiable {
    let id: UUID
    let label: String
    let detail: String
    /// 回执里的工具返回状态（"ok" / "timeout" / …）；推不出时 nil，不冒充成功。
    let status: String?
    let thoughts: String?
    /// 「中途说明」（3b′）：来自一条 `.note` 消息，组内画成可折叠的弱化说明行，不计步数。
    var isNote = false
}

/// UI 侧消息模型：把后端轮次（Turn.response 的 role 消息）铺平成可渲染列表。
struct ChatMessage: Identifiable {
    enum Kind {
        case user
        case assistant
        case system
        /// 工具调用/返回的折叠行（detail 收在 DisclosureGroup 里）
        case tool
        /// 「中途说明」（3b′）：同一轮里后面还有工具步的助手正文——是过程说明而非答复，
        /// 显示投影里并入相邻的执行过程组；轮内最后一段正文始终留作气泡。
        case note
        /// 显示投影：同一轮里连续的 `.tool` 行合成的「执行过程」组（toolSteps）。
        /// 只由 `groupedForDisplay` 产生；`from(turn:)` 与 store.messages 仍是逐行的。
        case toolGroup
    }

    /// 组的 id 取其第一步的 id，所以同一份 messages 重算投影时组身份稳定。
    var id = UUID()
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
    /// tool 行：执行回执给出的返回状态 / 事件时刻（epoch 秒）；没有可靠对应的回执 → nil。
    var stepStatus: String?
    var stepTimestamp: Double?
    /// tool 行：这一行是一次调用（call）还是其返回（result），及工具名——用于把
    /// 调用+返回计为一步、以及与回执对账。解析失败行没有名字（nil）。
    /// notRun：作废的待审批调用——闭合其调用（计步），但从不是一次返回（不参与回执对账）。
    enum StepRole { case call, result, notRun }
    var stepRole: StepRole?
    var stepName: String?
    /// toolGroup：组内各行、调用次数（调用+返回算一步）、按回执时刻算出的耗时
    /// （秒；不足两个时刻 → nil），以及稳定的展开键（见 groupedForDisplay）。
    var toolSteps: [ToolStep] = []
    var toolInvocationCount = 0
    var stepsDuration: Double?
    var toolGroupKey: String?

    static func user(_ text: String) -> ChatMessage {
        ChatMessage(kind: .user, text: text)
    }

    static func assistant(_ text: String, thoughts: String? = nil) -> ChatMessage {
        ChatMessage(kind: .assistant, text: text, thoughts: thoughts)
    }

    static func note(_ text: String, thoughts: String? = nil) -> ChatMessage {
        ChatMessage(kind: .note, text: text, thoughts: thoughts)
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
        let response = turn.response ?? []
        // 3b′ 中途说明：正文之后本轮还有工具步 → 过程说明；轮内最后一段正文（含以提问收尾、
        // 计划未完成的轮）始终是气泡。
        let lastTextIndex = response.lastIndex { msg in
            msg.role == "assistant" && (msg.toolCalls ?? []).isEmpty && !(msg.content ?? "").isEmpty
        }
        let lastStepIndex = response.lastIndex { msg in
            (msg.role == "assistant" && !(msg.toolCalls ?? []).isEmpty) ||
            (msg.role == "tool" && !(msg.toolResponses ?? []).isEmpty)
        }
        for (responseIndex, msg) in response.enumerated() {
            switch msg.role {
            case "assistant":
                if let calls = msg.toolCalls, !calls.isEmpty {
                    for call in calls {
                        if call.type == "error" {
                            out.append(.tool(label: L("工具调用解析失败"),
                                             detail: call.errorMsg ?? ""))
                        } else {
                            let name = call.name ?? "unknown"
                            var row = ChatMessage.tool(label: "\(L("调用")) \(name)",
                                                       detail: call.arguments?.compact(max: 4000) ?? "{}")
                            row.stepRole = .call
                            row.stepName = name
                            out.append(row)
                        }
                    }
                    // 带 tool_calls 的 assistant 消息也可能有过程性思考
                    if let thoughts = msg.thoughts, !thoughts.isEmpty, out.last != nil {
                        var last = out.removeLast()
                        last.thoughts = thoughts
                        out.append(last)
                    }
                } else if let content = msg.content, !content.isEmpty {
                    if let lastStepIndex, responseIndex < lastStepIndex, responseIndex != lastTextIndex {
                        out.append(.note(content, thoughts: msg.thoughts))
                    } else {
                        out.append(.assistant(content, thoughts: msg.thoughts))
                    }
                }
            case "tool":
                for item in msg.toolResponses ?? [] {
                    if item.response == "Operation cancelled: the user sent a new message without approving." {
                        var row = ChatMessage.tool(label: (item.name ?? L("工具")) + " · " + L("未执行"),
                                                   detail: L("此待审批调用已因新消息作废，未获批执行。"))
                        row.stepRole = .notRun
                        row.stepName = item.name
                        out.append(row)
                        continue
                    }
                    var m = ChatMessage.tool(label: "\(item.name ?? L("工具")) \(L("返回"))",
                                             detail: item.response ?? "")
                    m.stepRole = .result
                    m.stepName = item.name
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
        attachReceipts(turn.executionReceipts?.items ?? [], to: &out)
        // Reuse the ordinary tool disclosure rows for journal facts in both live and history paths.
        let hasToolMessages = out.contains { $0.kind == .tool }
        if stopped || !hasToolMessages, let facts = turn.executionReceipts {
            // 3c：回执先并入它所描述的应答行（一次执行 = 一个 call_id），只有对不上任何
            // 应答行的回执才各追加一次——不再为已经显示的调用/返回再铺一组「已开始/已返回」。
            for item in mergeReceipts(facts.items, into: &out) {
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
                // These rows ARE the receipts, so their status and time are exact.
                var row = ChatMessage.tool(label: item.name + " · " + L(started ? "已开始" : "已返回"), detail: detail)
                row.stepStatus = started ? nil : item.status
                row.stepTimestamp = item.ts
                row.stepRole = started ? .call : .result
                row.stepName = item.name
                out.append(row)
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

extension ChatMessage {
    /// Response rows carry no call id (the backend mints it at execution time and keeps it
    /// only in the journal), so receipts cannot be matched per call. Per tool name and role,
    /// attach status/time only when the rows and receipts are equally many — then the k-th
    /// row is the k-th execution. A denied or abandoned call leaves a row without a receipt,
    /// the counts differ, and that name stays unknown (nil) instead of shifting results.
    fileprivate static func attachReceipts(_ receipts: [ExecutionReceipt], to rows: inout [ChatMessage]) {
        guard !receipts.isEmpty else { return }
        func key(_ role: StepRole, _ name: String) -> String { (role == .call ? "c\u{1F}" : "r\u{1F}") + name }
        var rowIndices: [String: [Int]] = [:]
        for i in rows.indices where rows[i].kind == .tool {
            guard let role = rows[i].stepRole, role != .notRun, let name = rows[i].stepName else { continue }
            rowIndices[key(role, name), default: []].append(i)
        }
        var receiptGroups: [String: [ExecutionReceipt]] = [:]
        for item in receipts {
            let role: StepRole
            switch item.type {
            case "tool_started": role = .call
            case "tool_result": role = .result
            default: continue
            }
            receiptGroups[key(role, item.name), default: []].append(item)
        }
        for (k, indices) in rowIndices {
            guard let matched = receiptGroups[k], matched.count == indices.count else { continue }
            for (i, receipt) in zip(indices, matched) {
                rows[i].stepTimestamp = receipt.ts
                if rows[i].stepRole == .result { rows[i].stepStatus = receipt.status }
            }
        }
    }

    /// 停止轮的回执并入（3c）。应答行不带 call_id，回执带：同一 call_id 的 started/result
    /// 是同一次执行。返回对不上任何应答行、需要各追加一次的回执：
    /// 1. 返回按内容认行：result 回执的 detail 与某条同名返回行全文相同（回执被截断时为其前缀）
    ///    → 就是那一行；同一 call_id 的 started 回执并入该行之前最近的同名调用行。
    /// 2. 调用按等量认行：剩余同名调用行与 started 回执一样多 → 第 k 行就是第 k 次执行
    ///    （attachReceipts 同理；数量不等时不猜，避免把被拒调用的行冒认成后来那次执行）。
    ///    返回行不做按序兜底：内容对不上的返回宁可另起一行，也不冒认。
    /// 同一 (call_id, type) 的重复回执只算一次。
    fileprivate static func mergeReceipts(_ items: [ExecutionReceipt], into out: inout [ChatMessage]) -> [ExecutionReceipt] {
        var seen = Set<String>()
        let receipts = items.filter { item in
            guard item.type == "tool_started" || item.type == "tool_result" else { return false }
            guard let callId = item.callId, !callId.isEmpty else { return true }
            return seen.insert(item.type + "\u{1F}" + callId).inserted
        }
        var claimedRows = Set<UUID>()
        var merged = Set<Int>()   // receipt indices already represented by a row
        func isRow(_ row: ChatMessage, _ role: StepRole, _ name: String) -> Bool {
            row.kind == .tool && row.stepRole == role && row.stepName == name && !claimedRows.contains(row.id)
        }
        func sameContent(_ row: ChatMessage, _ item: ExecutionReceipt) -> Bool {
            let detail = item.detail ?? "", shown = row.toolDetail ?? ""
            return shown == detail || (item.truncated == true && !detail.isEmpty && shown.hasPrefix(detail))
        }
        func mergeStarted(_ k: Int, into i: Int) {
            out[i].stepTimestamp = receipts[k].ts
            // 「执行已开始」是安全信号：并入调用行后要在标签上留痕，否则与从未执行的提案行无从分辨
            let started = L("已开始")
            if !(out[i].toolLabel ?? "").hasSuffix(started) { out[i].toolLabel = (out[i].toolLabel ?? "") + " · " + started }
            claimedRows.insert(out[i].id); merged.insert(k)
        }
        // 1. a retained result row with the receipt's content IS that execution's return
        for (k, item) in receipts.enumerated() {
            let started = item.type == "tool_started"
            if !started, let existing = out.firstIndex(where: { isRow($0, .result, item.name) && sameContent($0, item) }) {
                var detail = out[existing].toolDetail ?? ""
                // 全文相同的行就是回执本身：回执被截断，这一行同样不完整
                if detail == (item.detail ?? "") && item.truncated == true { detail += "\n" + L("记录已截断，部分内容未保留。") }
                detail += "\n" + L("工具已返回；返回状态不保证外部操作成功。")
                if let status = item.status { detail += "\n" + L("返回状态：") + status }
                out[existing].toolLabel = item.name + " · " + L("已返回")
                out[existing].toolDetail = detail
                out[existing].stepStatus = item.status
                out[existing].stepTimestamp = item.ts
                claimedRows.insert(out[existing].id); merged.insert(k)
                guard let callId = item.callId, !callId.isEmpty,
                      let s = receipts.indices.first(where: { !merged.contains($0) && receipts[$0].type == "tool_started" && receipts[$0].callId == callId }),
                      let c = out.indices[..<existing].last(where: { isRow(out[$0], .call, item.name) }) else { continue }
                mergeStarted(s, into: c)
            }
        }
        // 2. calls: equal remaining counts per name → k-th call row is the k-th execution
        for name in Set(receipts.map(\.name)) {
            let pending = receipts.indices.filter { !merged.contains($0) && receipts[$0].type == "tool_started" && receipts[$0].name == name }
            let open = out.indices.filter { isRow(out[$0], .call, name) }
            guard !pending.isEmpty, pending.count == open.count else { continue }
            for (k, i) in zip(pending, open) { mergeStarted(k, into: i) }
        }
        return receipts.indices.filter { !merged.contains($0) }.map { receipts[$0] }
    }

    /// 显示投影：同一轮（turnIndex 相同）里连续的普通 `.tool` 行合成一条 `.toolGroup`；
    /// 引用格（citations）行留在顶层原位（build 77 的摆放），并打断连续段。
    /// 纯函数；输入的逐行消息原样保留在 store.messages（导出、审批/重试逻辑、原生测试都读它）。
    /// 展开键 = "turnIndex:组首行在该轮工具行中的序号"，同一轮重投影得到同一个键。
    static func groupedForDisplay(_ messages: [ChatMessage]) -> [ChatMessage] {
        var out: [ChatMessage] = []
        var run: [ChatMessage] = []
        var runOrdinal = 0
        var toolOrdinal: [Int: Int] = [:]   // turnIndex → tool rows seen so far in that turn
        func flush() {
            guard let first = run.first else { return }
            var group = ChatMessage(id: first.id, kind: .toolGroup, text: "")
            group.turnIndex = first.turnIndex
            group.timestamp = first.timestamp
            group.toolGroupKey = first.turnIndex.map { "\($0):\(runOrdinal)" } ?? first.id.uuidString
            // 只有中途说明、没有工具步的段（被引用格隔开）不成组，原样留给 MessageView
            guard run.contains(where: { $0.kind == .tool }) else { out.append(contentsOf: run); run.removeAll(); return }
            group.toolSteps = run.map {
                $0.kind == .note
                    ? ToolStep(id: $0.id, label: L("中途说明"), detail: $0.text, status: nil, thoughts: $0.thoughts, isNote: true)
                    : ToolStep(id: $0.id, label: $0.toolLabel ?? L("工具"), detail: $0.toolDetail ?? "",
                               status: $0.stepStatus, thoughts: $0.thoughts)
            }
            group.toolInvocationCount = invocationCount(run.filter { $0.kind == .tool })
            let times = run.compactMap(\.stepTimestamp)
            if times.count >= 2, let lo = times.min(), let hi = times.max(), hi > lo {
                group.stepsDuration = hi - lo
            }
            out.append(group)
            run.removeAll()
        }
        for message in messages {
            if message.kind == .note {
                // 中途说明并入相邻执行过程组（同一轮）；不占工具行序号，组键保持稳定
                if let last = run.last, last.turnIndex != message.turnIndex { flush() }
                if run.isEmpty { runOrdinal = message.turnIndex.map { toolOrdinal[$0, default: 0] } ?? 0 }
                run.append(message)
                continue
            }
            guard message.kind == .tool else { flush(); out.append(message); continue }
            let ordinal = message.turnIndex.map { toolOrdinal[$0, default: 0] } ?? 0
            if let index = message.turnIndex { toolOrdinal[index] = ordinal + 1 }
            if !message.citations.isEmpty { flush(); out.append(message); continue }
            if let last = run.last, last.turnIndex != message.turnIndex { flush() }
            if run.isEmpty { runOrdinal = ordinal }
            run.append(message)
        }
        flush()
        return out
    }

    /// 调用+返回算一步：返回行若能接上同名的未闭合调用就不另计；其余行各算一步。
    static func invocationCount(_ rows: [ChatMessage]) -> Int {
        var open: [String: Int] = [:]
        var count = 0
        for row in rows {
            let name = row.stepName ?? ""
            switch row.stepRole {
            case .call:
                count += 1
                open[name, default: 0] += 1
            case .result, .notRun:
                if let n = open[name], n > 0 { open[name] = n - 1 } else { count += 1 }
            case nil:
                count += 1
            }
        }
        return count
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
