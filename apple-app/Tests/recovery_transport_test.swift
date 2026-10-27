import Foundation
final class RecoveryTransport: URLProtocol {
    static let lock = NSLock()
    static var handler: (RecoveryTransport) -> Void = { _ in fatalError("unconfigured") }
    static var requests: [URLRequest] = [], held: [RecoveryTransport] = []
    static func matching(_ suffix: String) -> [URLRequest] { lock.lock(); defer { lock.unlock() }; return requests.filter { $0.url!.path.hasSuffix(suffix) } }
    static func hold(_ t: RecoveryTransport) { lock.lock(); held.append(t); lock.unlock() }
    static func takeHeld() -> RecoveryTransport {
        for _ in 0..<2000 {
            lock.lock()
            if !held.isEmpty { let value = held.removeFirst(); lock.unlock(); return value }
            lock.unlock()
            Thread.sleep(forTimeInterval: 0.001)
        }
        fatalError("missing held transport")
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { Self.lock.lock(); Self.requests.append(request); Self.lock.unlock(); Self.handler(self) }
    override func stopLoading() {}
    private var headersSent = false
    func respond(_ body: Any, status: Int = 200) {
        // Shared history fixtures are replayed for A/B ownership races. Bind their
        // resource envelope to the requested session, as a real backend does.
        var value = body
        if request.url!.path.hasSuffix("/history"), var history = body as? [String: Any],
           history["session_id"] != nil {
            history["session_id"] = request.url!.path.split(separator: "/").dropLast().last.map(String.init)
            value = history
        }
        deliver(try! JSONSerialization.data(withJSONObject: value), status: status, type: "application/json", finish: true)
    }
    func deliver(_ data: Data, status: Int = 200, type: String = "text/event-stream", finish: Bool = false) {
        if !headersSent { headersSent = true; client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type":type])!, cacheStoragePolicy: .notAllowed) }
        if !data.isEmpty { client?.urlProtocol(self, didLoad: data) }
        if finish { client?.urlProtocolDidFinishLoading(self) }
    }
}
func check(_ condition: @autoclosure () -> Bool, _ message: String) { if !condition() { print("FAIL: \(message)"); exit(1) } }
@main struct RecoveryTests {
    @MainActor static func wait(_ condition: () -> Bool, _ message: String) async {
        for _ in 0..<400 { if condition() { return }; try? await Task.sleep(nanoseconds: 5_000_000) }
        check(false, "timeout \(message)")
    }
    @MainActor static func main() async throws {
        let root = CommandLine.arguments[1], mode = CommandLine.arguments[2]
        let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: root + "/014-history-replay-boundary.restore.json"))) as! [String: Any]
        let raw = try String(contentsOfFile: root + "/014-history-replay-boundary.events.jsonl", encoding: .utf8)
        let events = try raw.split(separator: "\n").map { try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String: Any] }
        let before = fixture["history_before"]!, included = fixture["history_including"]!, turn = fixture["turn_after"]!
        func sse(_ t: RecoveryTransport, _ values: [[String: Any]]) {
            let after = Int(URLComponents(url: t.request.url!, resolvingAgainstBaseURL: false)!.queryItems!.first!.value!)!
            let bytes = values.filter { ($0["seq"] as! Int) > after }.map { "data: " + String(data: try! JSONSerialization.data(withJSONObject: $0), encoding: .utf8)! + "\n\n" }.joined()
            t.deliver(Data(bytes.utf8))
        }
        var historyCalls: Int { RecoveryTransport.matching("/history").count }
        var eventCalls: Int { RecoveryTransport.matching("/events").count }
        RecoveryTransport.handler = { t in
            let path = t.request.url!.path
            if path == "/health" {
                var body: [String:Any] = ["status":"ok", "compute_ready":true, "compute_state":"ready"]
                // Contract H engine_ready: absent for every other mode (older backend shape).
                if mode == "auth-engine-starting" { body["engine_ready"] = false }
                if mode == "auth-engine-ready" { body["engine_ready"] = true }
                t.respond(body, status: mode == "auth-offline" ? 503 : 200); return
            }
            if path.hasSuffix("/turns/last") && mode == "delete-failed" { t.respond(["status":false,"response":"synthetic refusal"]); return }
            if path.hasSuffix("/history") { if mode.hasPrefix("overlap") { RecoveryTransport.hold(t) } else if mode == "fresh-completed" { var h = included as! [String:Any]; h["replay_after"] = 4; t.respond(h) } else if mode == "old-count" {
                    var value = included as! [String:Any]
                    value["count"] = 2
                    value["turns"] = (value["turns"] as! [[String:Any]]) + [["status":"success","prompt":"second","response":[["role":"assistant","content":"second answer"]]]]
                    t.respond(value)
                } else { t.respond((mode == "included" || (mode == "thin" && historyCalls > 1)) ? included : before) } }
            else if path.hasSuffix("/turn") { t.respond(turn) }
            else if path.hasSuffix("/deck") { t.respond([:], status:404) }
            else if path.hasSuffix("/sessions") {
                if mode == "auth-late" && RecoveryTransport.matching("/sessions").count == 1 { RecoveryTransport.hold(t) }
                else { t.respond(["status":true,"sessions":[]], status: ["auth-failed","auth-late"].contains(mode) ? 401 : mode == "auth-unknown" ? 503 : 200) }
            }
            else if path.hasSuffix("/skills") { t.respond(["status":true,"skills":[]]) }
            else if path.hasSuffix("/events") {
                if mode == "404" && eventCalls == 1 { t.deliver(Data(), status:404, finish:true) }
                else if mode == "thin" && eventCalls == 1 { sse(t, [events[0], fixture["thin_terminal"] as! [String:Any]]) }
                else if ["gap","included","no-queued","thin","old-count","fresh-completed"].contains(mode) { sse(t, mode == "no-queued" ? [events.last!] : events); if mode == "old-count" { RecoveryTransport.hold(t) } }
                else { RecoveryTransport.hold(t); t.deliver(Data()) }
            } else if path.hasSuffix("/turns") || path.hasSuffix("/resume") { if mode == "fresh-completed" { t.respond(["status":true,"turn_id":"t_restore","seq":2],status:202) } else { RecoveryTransport.hold(t) } }
            else { t.respond(["status":true]) }
        }
        let settings = AppSettings(), store = AgentStore(settings: AppSettings())
        if mode == "delete-failed" {
            store.selectedSessionId = "A"
            store.messages = [.user("keep input"), .assistant("keep answer")]
            store.deleteLastTurn()
            await wait({ RecoveryTransport.matching("/turns/last").count == 1 }, "delete rejected")
            try? await Task.sleep(nanoseconds:80_000_000)
            check(store.errorMessage == "synthetic refusal", "business refusal remains visible")
            check(store.messages.map(\.text) == ["keep input","keep answer"], "refusal preserves displayed history")
            check(historyCalls == 0 && eventCalls == 0, "refusal does not detach and reload")
        } else if mode == "auth-late" {
            let old = Task { await store.refreshConnection() }
            await wait({ RecoveryTransport.matching("/sessions").count == 1 }, "older access probe")
            let delayed = RecoveryTransport.takeHeld()
            await store.refreshConnection()
            check(String(describing:store.connState) == "unauthorized", "latest probe rejected authentication")
            delayed.respond(["status":true,"sessions":[]])
            await old.value
            check(store.connectionOK == false && String(describing:store.connState) == "unauthorized", "older probe cannot overwrite latest access state")
            check(store.computeState == .unknown, "late ready probe cannot overwrite latest unauthenticated compute state")
        } else if mode.hasPrefix("auth-") {
            await store.refreshConnection()
            let authenticated = ["auth-ok", "auth-engine-starting", "auth-engine-ready"].contains(mode)
            check(store.computeState == (authenticated ? .ready : .unknown), "compute readiness requires current authenticated backend")
            switch mode {
            case "auth-failed": check(store.connectionOK == false && String(describing:store.connState) == "unauthorized", "health200 is not authenticated")
            case "auth-unknown": check(store.connectionOK == false && String(describing:store.connState) == "reachable", "business status unavailable stays unknown")
            case "auth-ok": check(store.connectionOK == true && String(describing:store.connState) == "connected", "authenticated backend stays usable")
            case "auth-engine-starting": check(store.connectionOK == true && String(describing:store.connState) == "engineStarting", "engine_ready false shows backend starting")
            case "auth-engine-ready": check(store.connectionOK == true && String(describing:store.connState) == "connected", "engine_ready true is connected")
            default: check(store.connectionOK == false, "health failure is not connected")
            }
        } else if ["gap","included","no-queued","thin","old-count"].contains(mode) {
            store.select(sessionId:"A")
            await wait({ eventCalls >= (mode == "thin" ? 2 : 1) }, "attachment")
            await wait({ store.messages.contains { $0.text == "restore answer" } }, "terminal answer")
            if mode == "old-count" {
                check(store.messages.count == 4, "older terminal did not duplicate archived turn")
                // No explicit turn_index: this next terminal uses the store's knownTurnCount.
                var next = events.last!
                next["seq"] = 5
                next["data"] = ["status":"success","prompt":"third","response":[["role":"assistant","content":"third answer"]]]
                // This new request is an actual accepted submit on the existing attachment.
                // Obtain held stream below: old-count stores the active transport in handler.
                sse(RecoveryTransport.takeHeld(), [next])
                await wait({ store.messages.count == 6 }, "new terminal after older replay")
                check(store.messages.suffix(2).allSatisfy { $0.turnIndex == 2 }, "known turn count never regresses")
            } else { check(store.messages.map(\.text) == ["restore prompt","restore answer"], "terminal once with prompt") }
            check(RecoveryTransport.matching("/events").first!.url!.query == "after=1", "history lower bound retained")
        } else if mode == "fresh-completed" {
            store.selectedSessionId = "A"
            store.send(prompt:"restore prompt", uploads:[], thinking:false)
            await wait({ eventCalls == 1 }, "first submit attachment")
            await wait({ !store.isGenerating }, "accepted archived terminal")
            check(store.messages.map(\.text) == ["restore prompt","restore answer"], "first attachment keeps one prompt")
        } else if mode == "404" {
            store.select(sessionId:"A"); await wait({ eventCalls == 1 }, "initial 404")
            try? await Task.sleep(nanoseconds:1_200_000_000)
            check(eventCalls == 1, "SSE404 must terminate retries")
            store.send(prompt:"new", uploads:[], thinking:false)
            await wait({ RecoveryTransport.matching("/turns").count == 1 }, "submit")
            RecoveryTransport.takeHeld().respond(["status":true,"turn_id":"t_new","seq":5],status:202)
            await wait({ eventCalls == 2 }, "valid submit reattaches")
        } else if mode.hasPrefix("late-") {
            if mode.contains("resume") {
                // Resume requires a journal-confirmed turn, not a hand-set UI flag.
                store.select(sessionId:"A")
                await wait({ eventCalls == 1 }, "A suspended attachment")
                let initial = RecoveryTransport.takeHeld()
                sse(initial, [events[0], ["seq":3,"turn_id":"t_restore","ts":3,"type":"suspended","data":[:]]])
                await wait({ store.isSuspended }, "A journal suspension")
                store.resumeTask()
            } else {
                store.selectedSessionId = "A"
                store.send(prompt:"late A", uploads:[], thinking:false)
            }
            let initialAttachments = mode.contains("resume") ? 1 : 0
            await wait({ RecoveryTransport.matching(mode.contains("resume") ? "/resume" : "/turns").count == 1 }, "A submit")
            let submission = RecoveryTransport.takeHeld()
            store.select(sessionId:"B"); await wait({ eventCalls == initialAttachments + 1 }, "B attach")
            let b = RecoveryTransport.takeHeld(); sse(b,events)
            await wait({ store.messages.contains { $0.text == "restore answer" } }, "B terminal")
            if mode.contains("aba") {
                store.select(sessionId:"A"); await wait({ eventCalls == initialAttachments + 2 }, "A second attach")
                let a = RecoveryTransport.takeHeld(); sse(a,events)
                await wait({ store.messages.contains { $0.text == "restore answer" } }, "A terminal")
            }
            let texts = store.messages.map(\.text)
            submission.respond(["status":true,"turn_id":"t_late","duplicate":mode.contains("duplicate"),"reason":"synthetic"],status: mode.contains("error") ? 500 : mode.contains("conflict") ? 409 : 202)
            try? await Task.sleep(nanoseconds:80_000_000)
            check(store.messages.map(\.text) == texts, "late A did not mutate selected projection")
            check(store.errorMessage == nil && !store.isGenerating, "late A did not alter selected state")
            check(eventCalls == initialAttachments + (mode.contains("aba") ? 2 : 1), "late A did not replace attachment")
        } else if mode.hasPrefix("overlap") {
            store.select(sessionId:"A")
            await wait({ historyCalls == 1 }, "history held")
            let historyRequest = RecoveryTransport.takeHeld()
            let prompt = mode.contains("same") ? "restore prompt" : "new prompt"
            store.send(prompt:prompt, uploads:[], thinking:false)
            await wait({ RecoveryTransport.matching("/turns").count == 1 }, "pending new submit")
            let submission = RecoveryTransport.takeHeld()
            let entries = UserDefaults.standard.dictionary(forKey:"macagent.outbox") as! [String:[String:String]]
            let key = entries["A"]!["key"]!
            historyRequest.respond(included)
            await wait({ eventCalls == 1 }, "overlap attachment")
            let stream = RecoveryTransport.takeHeld()
            var oldQueued = events[0]
            oldQueued["data"] = ["prompt":"restore prompt","client_turn_id":"old-key"]
            sse(stream,[oldQueued,events.last!])
            try? await Task.sleep(nanoseconds:30_000_000)
            let retained = UserDefaults.standard.dictionary(forKey:"macagent.outbox") as? [String:[String:String]]
            check(retained?["A"]?["key"] == key, "old terminal preserved new outbox")
            check(store.isGenerating, "old terminal preserved submitting state")
            check(store.messages.filter { $0.kind == .user && $0.turnIndex == nil }.count == 1, "one pending bubble preserved")
            let queued: [String:Any] = ["seq":5,"turn_id":"t_new","ts":5,"type":"turn_queued","data":["prompt":prompt,"client_turn_id":key]]
            let ended: [String:Any] = ["seq":6,"turn_id":"t_new","ts":6,"type":"turn_ended","data":["status":"success","prompt":prompt,"response":[["role":"assistant","content":"new answer"]],"turn_index":1]]
            if mode.contains("early") { sse(stream,[queued,ended]); await wait({ store.messages.contains { $0.text == "new answer" } }, "early terminal") }
            submission.respond(["status":true,"turn_id":"t_new","seq":5],status:mode.contains("error") ? 500 : 202)
            try? await Task.sleep(nanoseconds:30_000_000)
            if mode.contains("error") {
                let retained = UserDefaults.standard.dictionary(forKey:"macagent.outbox") as? [String:[String:String]]
                check(retained?["A"]?["key"] == key, "failed request retained retry key")
            } else {
                if !mode.contains("early") { sse(stream,[queued,ended]); await wait({ store.messages.contains { $0.text == "new answer" } }, "terminal") }
                check(store.messages.filter { $0.kind == .user && $0.turnIndex == 1 }.count == 1, "one authoritative new user")
            }
            check(store.messages.filter { $0.kind == .user && $0.turnIndex == nil }.isEmpty, "no orphan optimistic bubble")
        } else if mode == "old-cleanup" || mode == "deferred-cleanup" {
            RecoveryCleanupGate.enabled = mode == "deferred-cleanup"
            store.select(sessionId:"A"); await wait({ eventCalls == 1 }, "first A attach")
            let old = RecoveryTransport.takeHeld()
            store.select(sessionId:"B")
            store.select(sessionId:"A")
            await wait({ eventCalls == 2 }, "new A attach")
            let current = RecoveryTransport.takeHeld(); sse(current,events)
            await wait({ !store.isGenerating }, "new A terminal")
            old.client?.urlProtocolDidFinishLoading(old)
            if mode == "deferred-cleanup" {
                await wait({ RecoveryCleanupGate.pending != nil }, "old deferred cleanup held")
                RecoveryCleanupGate.release()
            }
            try? await Task.sleep(nanoseconds:20_000_000)
            store.send(prompt:"valid", uploads:[], thinking:false)
            await wait({ RecoveryTransport.matching("/turns").count == 1 }, "valid submit")
            RecoveryTransport.takeHeld().respond(["status":true,"turn_id":"t_next","seq":5],status:202)
            try? await Task.sleep(nanoseconds:30_000_000)
            check(eventCalls == 2, "old cleanup did not clear newer A handle")
        } else if mode == "client-change" {
            RecoveryTransport.handler = { t in
                let p = t.request.url!.path
                if t.request.url!.host == "fixture.invalid" && p.hasSuffix("/history") { RecoveryTransport.hold(t) }
                else if p.hasSuffix("/history") { t.respond(included) }
                else if p.hasSuffix("/turn") { t.respond(turn) }
                else if p == "/api/v1/events" { t.deliver(Data(),finish:true) }
                else if p.hasSuffix("/events") { sse(t,events) }
                else if p.hasSuffix("/deck") { t.respond([:],status:404) }
                else if p.hasSuffix("/sessions") { t.respond(["status":true,"sessions":[]]) }
                else if p.hasSuffix("/skills") { t.respond(["status":true,"skills":[]]) }
                else { t.respond(["status":true,"tasks":[]]) }
            }
            let actual = AgentStore(settings:settings)
            actual.select(sessionId:"A"); await wait({ !RecoveryTransport.matching("/history").isEmpty }, "old history")
            // The replacement backend does not inherit the previous selection: its own
            // cache never listed A, so the new incarnation is opened explicitly.
            settings.baseURLString = "https://other.invalid"; actual.applySettings()
            check(actual.selectedSessionId == nil, "a replaced backend starts with nothing selected")
            actual.select(sessionId: "A")
            await wait({ actual.messages.count == 2 }, "new client history")
            RecoveryTransport.takeHeld().respond(before)
            try? await Task.sleep(nanoseconds:80_000_000)
            check(actual.messages.map(\.text) == ["restore prompt","restore answer"], "old backend history ignored")
        }
        print("PASS native \(mode), history=\(historyCalls), events=\(eventCalls)"); exit(0)
    }
}
