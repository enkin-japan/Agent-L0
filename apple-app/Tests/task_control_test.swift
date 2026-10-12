import Foundation
final class RecoveryTransport: URLProtocol {
    static let lock = NSLock()
    static var handler: (RecoveryTransport) -> Void = { _ in fatalError("unconfigured") }
    static var requests: [URLRequest] = [], held: [RecoveryTransport] = []
    static func matching(_ suffix: String) -> [URLRequest] { lock.lock(); defer { lock.unlock() }; return requests.filter { $0.url!.path.hasSuffix(suffix) } }
    static func hold(_ t: RecoveryTransport) { lock.lock(); held.append(t); lock.unlock() }
    static func takeHeld() -> RecoveryTransport { lock.lock(); defer { lock.unlock() }; return held.removeFirst() }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { Self.lock.lock(); Self.requests.append(request); Self.lock.unlock(); Self.handler(self) }
    override func stopLoading() {}
    private var headersSent = false
    func respond(_ body: Any, status: Int = 200) { deliver(try! JSONSerialization.data(withJSONObject: body), status: status, type: "application/json", finish: true) }
    func deliver(_ data: Data, status: Int = 200, type: String = "text/event-stream", finish: Bool = false) {
        if !headersSent { headersSent = true; client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type":type])!, cacheStoragePolicy: .notAllowed) }
        if !data.isEmpty { client?.urlProtocol(self, didLoad: data) }
        if finish { client?.urlProtocolDidFinishLoading(self) }
    }
}
func check(_ condition: @autoclosure () -> Bool, _ message: String) { if !condition() { print("FAIL: \(message)"); exit(1) } }
@main struct ControlTests {
    @MainActor static func wait(_ condition: () -> Bool, _ message: String) async {
        for _ in 0..<500 { if condition() { return }; try? await Task.sleep(nanoseconds: 5_000_000) }
        check(false, "timeout " + message)
    }
    static func sse(_ transport: RecoveryTransport, _ events: [[String:Any]]) {
        let bytes = events.map { "data: " + String(data:try! JSONSerialization.data(withJSONObject:$0),encoding:.utf8)! + "\n\n" }.joined()
        transport.deliver(Data(bytes.utf8))
    }
    static func event(_ seq: Int, _ type: String, _ data: [String:Any] = [:], turn: String = "t_receipts") -> [String:Any] {
        ["seq":seq,"ts":seq,"turn_id":turn,"type":type,"data":data]
    }
    @MainActor static func reviewControl(_ mode: String, root: String, terminal: [String:Any]) async {
        if mode == "review-paused-ui" {
            let project = URL(fileURLWithPath: root).deletingLastPathComponent().deletingLastPathComponent()
            let path = CommandLine.arguments.count > 3 ? URL(fileURLWithPath: CommandLine.arguments[3]) : project.appendingPathComponent("apple-app/Sources/Views/SessionListView.swift")
            let ui = try! String(contentsOf:path,encoding:.utf8)
            check(AgentStore.canCancelTask(phase:"queued") && AgentStore.canCancelTask(phase:"running") &&
                  !AgentStore.canCancelTask(phase:"suspended") && !AgentStore.canCancelTask(phase:"awaiting_approval"),
                  "cancel eligibility follows actual worker phases")
            check(ui.contains("if AgentStore.canCancelTask(phase: task.status) {\n                            Button(L(\"停止\"))"),"paused task center UI omits Stop via shared phase eligibility")
            print("PASS " + mode);exit(0)
        }
        let pause = mode.hasPrefix("review-paused-cancel")
        let cancel = mode == "review-cancel-action"
        let snapshotFloor = mode == "review-turn-floor" ? ["last_seq":5] : [:]
        RecoveryTransport.handler = { t in
            let path = t.request.url!.path
            if path.hasSuffix("/history") {
                if pause { t.respond(["status":true,"session_id":"A","turns":[],"replay_after":0]) }
                else { RecoveryTransport.hold(t) }
            } else if path.hasSuffix("/turn") {
                var turn: [String:Any] = ["turn_id":"t_receipts","state":cancel ? "running" : "suspended","first_seq":1]
                turn.merge(snapshotFloor) { _, new in new }
                var response: [String:Any] = ["status":true,"turn":turn]
                if mode != "review-turn-floor" { response["last_seq"] = 5 }
                if mode == "review-attach-advanced" && RecoveryTransport.matching("/turn").count > 1 {
                    turn["state"] = "queued";turn["last_seq"] = 6;response["turn"] = turn;response["last_seq"] = 6
                }
                t.respond(response)
            } else if path.hasSuffix("/deck") { t.respond([:],status:404) }
            else if path.hasSuffix("/events") { RecoveryTransport.hold(t); t.deliver(Data()) }
            else if path.hasSuffix("/resume") || path.hasSuffix("/cancel") { RecoveryTransport.hold(t) }
            else { t.respond(["status":true,"sessions":[],"skills":[]]) }
        }
        func held(_ suffix: String) -> RecoveryTransport {
            RecoveryTransport.lock.lock();defer { RecoveryTransport.lock.unlock() }
            let index = RecoveryTransport.held.firstIndex { $0.request.url!.path.hasSuffix(suffix) }!
            return RecoveryTransport.held.remove(at:index)
        }
        let store = AgentStore(settings:AppSettings())
        if pause {
            store.select(sessionId:"A")
            await wait({ RecoveryTransport.matching("/events").count == 1 },"paused stream")
            let stream = held("/events")
            let type = mode.hasSuffix("approval") ? "awaiting_approval" : "suspended"
            sse(stream,[event(1,"turn_queued",["prompt":"paused"]),event(2,"turn_started"),event(3,type,["pending_tools":[["name":"write_probe"]]])])
            await wait({ store.ledgerPhase == type },"confirmed paused state")
            store.cancelGeneration()
            try? await Task.sleep(nanoseconds:80_000_000)
            check(RecoveryTransport.matching("/cancel").isEmpty,"paused state never sends /task/cancel")
            check(store.controlRequest == nil,"paused cancel does not fabricate sending state")
            print("PASS " + mode);exit(0)
        }
        store.controlTask(sessionId:"B",kind:cancel ? .cancel : .resume)
        let endpoint = cancel ? "/cancel" : "/resume"
        await wait({ RecoveryTransport.matching(endpoint).count == 1 && RecoveryTransport.matching("/history").count == 1 },"control before held history completes")
        let ack = held(endpoint)
        let ackLate = mode.contains("late") || mode == "review-old-round-ack"
        if !ackLate {
            ack.respond(["status":true,"turn_id":"t_receipts"],status:cancel ? 200 : 202)
            await wait({ store.controlRequest?.accepted == true },"control accepted before initial attachment")
        }
        held("/history").respond(["status":true,"session_id":"B","turns":[],"replay_after":0])
        await wait({ RecoveryTransport.matching("/events").count == 1 },"initial attachment after history")
        let stream = held("/events")
        if mode.hasPrefix("review-old-round") {
            sse(stream,[event(1,"turn_queued",["prompt":"old"],turn:"t_old"),event(2,"turn_started",turn:"t_old")])
            await wait({ store.ledgerPhase == "running" },"old round replayed")
            try? await Task.sleep(nanoseconds:60_000_000)
            check(store.controlRequest != nil,"older queued round cannot supersede snapshot-owned request")
            if ackLate {
                ack.respond(["status":true,"turn_id":"t_receipts"],status:202)
                await wait({ store.controlRequest?.accepted == true },"ack while older projection is replaying")
            }
        }
        let start = mode.hasPrefix("review-old-round") ? 3 : 1
        var old: [[String:Any]] = []
        if start == 1 { old += [event(1,"turn_queued",["prompt":"paused"]),event(2,"turn_started")] }
        old += [event(3,"suspended"),event(4,"resumed"),event(5,cancel ? "turn_started" : "suspended")]
        sse(stream,old)
        await wait({ store.ledgerPhase == (cancel ? "running" : "suspended") },"old snapshot milestones replayed")
        try? await Task.sleep(nanoseconds:70_000_000)
        check(store.controlRequest != nil,"old suspended/resumed replay cannot confirm newer control request")
        if mode == "review-cancel-action" {
            sse(stream,[event(6,"suspended"),event(7,"resumed")])
            await wait({ store.ledgerPhase == "running" },"unrelated cancel milestones")
            try? await Task.sleep(nanoseconds:70_000_000)
            check(store.controlRequest?.accepted == true,"unrelated suspension/resumption never confirms cancel")
        } else if mode == "review-resume-action" {
            sse(stream,[event(6,"suspended")])
            await wait({ store.ledgerPhase == "suspended" },"unrelated suspend milestone")
            try? await Task.sleep(nanoseconds:70_000_000)
            check(store.controlRequest?.accepted == true,"new suspension never confirms resume")
        }
        if mode == "review-resume-late-http" {
            ack.respond(["status":false,"response":"late rejection"],status:503)
            await wait({ store.errorMessage != nil },"late current HTTP error visible")
            check(store.controlRequest == nil,"current late rejection ends only request feedback")
        } else if mode == "review-resume-aba-late-http" {
            store.select(sessionId:"A")
            await wait({ RecoveryTransport.matching("/history").count == 2 },"A recovery")
            held("/history").respond(["status":true,"session_id":"A","turns":[],"replay_after":0])
            await wait({ RecoveryTransport.matching("/events").count == 2 },"A stream")
            _ = held("/events")
            store.select(sessionId:"B")
            await wait({ RecoveryTransport.matching("/history").count == 3 },"second B recovery")
            held("/history").respond(["status":true,"session_id":"B","turns":[],"replay_after":0])
            await wait({ RecoveryTransport.matching("/events").count == 3 },"second B stream")
            _ = held("/events")
            ack.respond(["status":false,"response":"obsolete rejection"],status:503)
            try? await Task.sleep(nanoseconds:80_000_000)
            check(store.errorMessage == nil && store.controlRequest == nil,"ABA late HTTP never mutates new selection incarnation")
        } else if mode == "review-new-round" {
            sse(stream,[event(6,"turn_queued",["prompt":"new"],turn:"t_new")])
            await wait({ store.controlRequest == nil },"newer round supersedes request")
        } else {
            if cancel {
                var ended=terminal;ended["turn_index"]=0
                sse(stream,[event(8,"turn_ended",ended)])
            } else { sse(stream,[event(mode == "review-attach-advanced" ? 6 : 8,"resumed")]) }
            await wait({ store.controlRequest == nil },"new action-matched journal confirmation")
        }
        print("PASS " + mode);exit(0)
    }
    @MainActor static func main() async throws {
        let root = CommandLine.arguments[1], mode = CommandLine.arguments[2]
        let raw = try String(contentsOfFile: root + "/015-cancelled-execution-receipts.events.jsonl", encoding: .utf8)
        let events = try raw.split(separator: "\n").map { try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String:Any] }
        let terminal = events.last!["data"] as! [String:Any]
        if mode.hasPrefix("review-") { await reviewControl(mode,root:root,terminal:terminal);return }
        func mapped(_ value: [String:Any]) throws -> [ChatMessage] {
            ChatMessage.from(turn:try JSONDecoder().decode(Turn.self,from:JSONSerialization.data(withJSONObject:value)))
        }
        if mode.hasPrefix("facts") && !["facts-history","facts-live"].contains(mode) {
            var value = terminal
            switch mode {
            case "facts-missing": value.removeValue(forKey:"execution_receipts")
            case "facts-empty": value["execution_receipts"] = ["items":[],"truncated":false]
            case "facts-warning-generic":
                value["status"]="warning"; value["response"]=[["role":"system","content":"guard budget warning"]]
            case "facts-abandoned-suspended":
                value["status"]="warning"
                value["response"]=[["role":"system","content":"The suspended task was interrupted by a new user message and abandoned (not continued)."]]
            case "facts-abandoned-existing":
                value["status"]="warning"
                value["response"]=[
                    ["role":"tool","tool_responses":[["name":"write_file","response":"Tool returned after writing report.txt"]]],
                    ["role":"system","content":"The suspended task was interrupted by a new user message and abandoned (not continued)."]
                ]
            case "facts-abandoned-approval":
                value["status"]="warning"
                value["response"]=[["role":"tool","tool_responses":[["name":"pending_api","response":"Operation cancelled: the user sent a new message without approving."]]]]
            case "facts-truncated-result":
                var facts=value["execution_receipts"] as! [String:Any]
                var items=facts["items"] as! [[String:Any]]
                items[1]["truncated"]=true;facts["items"]=items;value["execution_receipts"]=facts
            case "facts-fail": value["status"] = "fail"; value["response"] = [["role":"system","content":"Traceback: internal.py; directly resend"]]
            case "facts-success": value["status"] = "success";value["response"] = [["role":"tool","tool_responses":[["name":"write_file","response":"ordinary result"]]]]
            default: break
            }
            let messages=try mapped(value)
            if mode == "facts-warning-generic" {
                check(messages.contains { $0.text == "guard budget warning" } && !messages.contains { $0.text.contains("已被新消息中止") || $0.text.contains("本轮已取消") },"generic warning is never inferred to be cancelled or abandoned")
            }
            if mode == "facts-abandoned-existing" {
                check(messages.filter { $0.toolDetail?.contains("Tool returned after writing report.txt") == true }.count == 1,"retained exact tool result is reused without whole-payload duplication")
            }
            if mode == "facts-missing" {
                check(messages.contains { $0.text.contains("执行记录不可用") }, "missing record stays unknown")
            } else if mode == "facts-empty" {
                check(messages.contains { $0.text.contains("不能证明没有副作用") }, "empty record does not prove no effects")
            } else if mode == "facts-success" {
                check(messages.filter { $0.kind == .tool }.count == 1,"successful ordinary details not duplicated")
            } else {
                check(messages.filter { $0.kind == .tool }.count == (mode == "facts-abandoned-approval" ? 3 : 2),"cancel retains started and returned execution facts")
                if mode.hasPrefix("facts-abandoned") {
                    check(messages.contains { $0.text.contains("已被新消息中止") },"paused abandon has localized stopped advice")
                    if mode == "facts-abandoned-approval" { check(messages.contains { $0.toolLabel?.contains("未执行") == true },"abandoned proposed call never masquerades as executed") }
                }
                check(messages.contains { $0.toolDetail?.contains("参数已省略") == true },"omitted arguments visible")
                check(messages.contains { $0.text.contains("执行记录不完整") },"whole record truncation visible")
                check(messages.contains { $0.toolDetail?.contains("不保证外部操作成功") == true },"ok return never guarantees external success")
                if mode == "facts-truncated-result" { check(messages.contains { $0.toolDetail?.contains("记录已截断") == true },"individual result truncation visible") }
                check(!messages.contains { $0.text.contains("Traceback") || $0.text.contains("直接重发") },"stopped summary does not advise blind retry or show internal trace")
            }
            print("PASS " + mode);exit(0)
        }
        var snapshotBody: [String:Any] = ["status":true,"tasks":[["session_id":"A","status":"running","kind":"chat"]]]
        var snapshotCode = mode == "snapshot-unknown" ? 503 : 200
        RecoveryTransport.handler = { t in
            let p=t.request.url!.path
            if p.hasSuffix("/history") {
                let turns: [[String:Any]] = mode == "facts-history" ? [terminal] : []
                t.respond(["status":true,"session_id":"A","turns":turns,"replay_after":0])
            } else if p.hasSuffix("/turn") {
                if mode == "task-no-turn" { t.respond(["status":true,"turn":NSNull(),"last_seq":2]) }
                else { t.respond(["status":true,"turn":["turn_id":"t_receipts","state":mode == "task-queued" ? "queued" : "running","first_seq":1],"last_seq":2]) }
            } else if p.hasSuffix("/deck") { t.respond([:],status:404) }
            else if p == "/api/v1/tasks" {
                if mode == "snapshot-overlap" && RecoveryTransport.matching("/tasks").count == 1 { RecoveryTransport.hold(t) }
                else { t.respond(snapshotBody,status:snapshotCode) }
            }
            else if p.hasSuffix("/events") { RecoveryTransport.hold(t);t.deliver(Data()) }
            else if p.hasSuffix("/cancel") || p.hasSuffix("/suspend") || p.hasSuffix("/resume") {
                if mode.hasSuffix("refusal") { t.respond(["status":false,"response":"synthetic refusal"]) }
                else if mode.hasSuffix("http") { t.respond(["status":false,"response":"synthetic HTTP rejection"],status:503) }
                else { RecoveryTransport.hold(t) }
            } else { t.respond(["status":true,"sessions":[],"skills":[]]) }
        }
        let settings=AppSettings(), store=AgentStore(settings:settings)
        if mode.hasPrefix("snapshot") {
            if mode == "snapshot-overlap" {
                let old = Task { await store.refreshGlobalSnapshot() }
                await wait({ RecoveryTransport.matching("/tasks").count == 1 }, "older snapshot held")
                let delayed = RecoveryTransport.takeHeld()
                await store.refreshGlobalSnapshot()
                delayed.respond(["status":true,"tasks":[]])
                await old.value
                check(store.globalTasks.count == 1 && store.globalTasksFreshness == .fresh, "older snapshot cannot overwrite latest success")
                print("PASS " + mode);exit(0)
            }
            await store.refreshGlobalSnapshot()
            if mode == "snapshot-unknown" {
                check(store.globalTasksFreshness == .unknown && store.globalTasks.isEmpty,"initial failure stays unknown")
            } else {
                check(store.globalTasksFreshness == .fresh && store.globalTasks.count == 1,"initial successful tasks")
                snapshotBody=["status":false,"tasks":[]]; snapshotCode=mode == "snapshot-false" ? 200 : 503
                await store.refreshGlobalSnapshot()
                check(store.globalTasksFreshness == .stale && store.globalTasks.count == 1,"failure retains last successful task snapshot")
                snapshotCode=200;snapshotBody=["status":true,"tasks":[]]
                await store.refreshGlobalSnapshot()
                check(store.globalTasksFreshness == .fresh && store.globalTasks.isEmpty,"successful empty snapshot alone confirms idle")
            }
            print("PASS " + mode);exit(0)
        }
        store.select(sessionId:"A")
        await wait({ RecoveryTransport.matching("/events").count == 1 },"stream")
        let stream=RecoveryTransport.takeHeld()
        if mode == "facts-history" || mode == "facts-live" {
            if mode == "facts-live" { sse(stream,events) }
            await wait({ store.messages.contains { $0.kind == .tool } },"rendered facts")
            let expected=try mapped(terminal)
            check(store.messages.map { ($0.toolLabel ?? "") + ":" + ($0.toolDetail ?? $0.text) } == expected.map { ($0.toolLabel ?? "") + ":" + ($0.toolDetail ?? $0.text) },"history/live share complete execution facts mapping")
            print("PASS " + mode);exit(0)
        }
        sse(stream,[events[0]])
        await wait({ store.ledgerPhase == "queued" },"queued")
        check(!store.canSuspend,"queued does not offer suspend")
        if mode == "queued" {
            store.suspendTask();try? await Task.sleep(nanoseconds:60_000_000)
            check(RecoveryTransport.matching("/suspend").isEmpty,"queued does not send suspend")
            print("PASS queued");exit(0)
        }
        sse(stream,[events[1]])
        await wait({ store.canSuspend },"running")
        if mode.hasPrefix("task-") {
            store.controlTask(sessionId:"B",kind:.suspend)
            await wait({ RecoveryTransport.matching("/turn").count >= 2 },"one shot task state")
            try? await Task.sleep(nanoseconds:100_000_000)
            if mode == "task-no-turn" || mode == "task-queued" {
                check(RecoveryTransport.matching("/suspend").isEmpty,"unconfirmed/queued cross session cannot suspend")
                check(store.errorMessage != nil,"task control unavailable reason visible")
            } else {
                await wait({ RecoveryTransport.matching("/suspend").count == 1 },"cross session suspend")
                check(RecoveryTransport.matching("/suspend").first!.url!.path.contains("/B/"),"server request remains session semantic")
            }
            print("PASS " + mode);exit(0)
        }
        let resume=mode.hasPrefix("resume")
        let cancel=mode.hasPrefix("cancel")
        if resume { sse(stream,[event(3,"suspended")]);await wait({ store.isSuspended },"suspended"); store.resumeTask() }
        else if cancel { store.cancelGeneration() }
        else { store.suspendTask() }
        let suffix=resume ? "/resume" : cancel ? "/cancel" : "/suspend"
        await wait({ RecoveryTransport.matching(suffix).count == 1 },"control request")
        if mode.hasSuffix("refusal") || mode.hasSuffix("http") {
            await wait({ store.errorMessage != nil },"visible refusal")
            check(store.controlRequest == nil,"refusal clears only request state")
            check(store.ledgerPhase == (resume ? "suspended" : "running"),"refusal does not pretend suspended or stopped")
            if mode.hasSuffix("refusal") { check(store.errorMessage == "synthetic refusal","business refusal reason preserved") }
        } else {
            let delayed=RecoveryTransport.takeHeld()
            check(store.controlFeedback == "正在发送控制请求…","sending feedback before acceptance")
            if mode.contains("late-suspended") { sse(stream,[event(4,"suspended")]);await wait({ store.isSuspended },"journal confirmed suspension") }
            else if mode.contains("late-terminal") { sse(stream,[events.last!]);await wait({ store.ledgerPhase == "none" },"journal terminal") }
            else if mode.contains("old-round") {
                sse(stream,[event(4,"turn_queued",["prompt":"new round"],turn:"t_new"),event(5,"turn_started",turn:"t_new")])
                await wait({ store.ledgerPhase == "running" && store.controlRequest == nil },"new round supersedes request")
            } else if mode.contains("switch") {
                store.select(sessionId:"B")
                await wait({ RecoveryTransport.matching("/events").count == 2 },"B stream")
                let b=RecoveryTransport.takeHeld();sse(b,[events[0],events[1]])
                await wait({ store.canSuspend },"B running")
                if mode.contains("aba") {
                    store.select(sessionId:"A")
                    await wait({ RecoveryTransport.matching("/events").count == 3 },"second A stream")
                    let a=RecoveryTransport.takeHeld();sse(a,[events[0],events[1]])
                    await wait({ store.canSuspend },"second A running")
                }
            } else if mode == "client-change" {
                settings.baseURLString="https://replacement.invalid"; store.applySettings()
                await wait({ RecoveryTransport.matching("/events").count >= 2 },"replacement client stream")
            }
            let late=mode.contains("late-") || mode.contains("old-round") || mode.contains("switch") || mode == "client-change"
            delayed.respond(["status":!mode.contains("late-error"),"turn_id":"t_receipts","response":"obsolete refusal"],status:resume ? 202 : mode.contains("late-error") ? 503 : 200)
            try? await Task.sleep(nanoseconds:80_000_000)
            if late {
                check(store.controlRequest == nil,"obsolete or already confirmed ack cannot revive request")
                check(store.errorMessage == nil,"obsolete failure cannot change selected/new round errors")
            } else {
                await wait({ store.controlRequest?.accepted == true }, "accepted projected by store")
                check(store.controlRequest?.accepted == true,"accepted request remains pending journal confirmation")
                check(store.controlFeedback?.contains("已受理") == true,"accepted safe-point feedback")
                if mode == "long-tool" {
                    try? await Task.sleep(nanoseconds:18_000_000_000)
                    check(store.suspendPending && store.controlFeedback?.contains("安全点") == true,"long tool still waits for safety point beyond 15 seconds")
                }
                if resume {
                    check(store.isSuspended && !store.isGenerating,"202 resume does not confirm running")
                    sse(stream,[event(4,"resumed")]);await wait({ store.controlRequest == nil && store.isGenerating },"journal resume")
                } else if cancel {
                    check(store.isGenerating,"cancel accepted does not confirm stop")
                    sse(stream,[events.last!]);await wait({ store.controlRequest == nil && !store.isGenerating },"journal cancel")
                } else {
                    check(!store.isSuspended && store.isGenerating,"suspend accepted does not fake stop")
                    sse(stream,[event(4,"suspended")]);await wait({ store.controlRequest == nil && store.isSuspended },"journal suspend")
                }
            }
        }
        print("PASS " + mode);exit(0)
    }
}
