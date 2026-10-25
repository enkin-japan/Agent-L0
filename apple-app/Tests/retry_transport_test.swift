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
@main struct RetryTests {
    @MainActor static func wait(_ condition: () -> Bool, _ text: String) async {
        for _ in 0..<600 { if condition() { return };try? await Task.sleep(nanoseconds:5_000_000) }
        check(false,"timeout " + text)
    }
    static func sse(_ t: RecoveryTransport,_ values: [[String:Any]]) {
        let bytes=values.map { "data: "+String(data:try! JSONSerialization.data(withJSONObject:$0),encoding:.utf8)!+"\n\n" }.joined()
        t.deliver(Data(bytes.utf8))
    }
    static func event(_ seq:Int,_ type:String,_ data:[String:Any]=[:],id:String="t_retry") -> [String:Any] {
        ["seq":seq,"ts":seq,"type":type,"turn_id":id,"data":data]
    }
    static func held(_ suffix:String) -> RecoveryTransport {
        // Request observation and handler hold publication happen on URLSession's queue.
        // Wait for both rather than treating a request-count observation as the hold barrier.
        for _ in 0..<2000 {
            RecoveryTransport.lock.lock()
            if let index = RecoveryTransport.held.firstIndex(where: { $0.request.url!.path.hasSuffix(suffix) }) {
                let value = RecoveryTransport.held.remove(at:index)
                RecoveryTransport.lock.unlock()
                return value
            }
            RecoveryTransport.lock.unlock()
            Thread.sleep(forTimeInterval: 0.001)
        }
        fatalError("missing held transport " + suffix)
    }
    static func body(_ request: URLRequest) -> [String:Any] {
        let data:Data
        if let raw=request.httpBody {data=raw}
        else { let stream=request.httpBodyStream!;stream.open();defer{stream.close()};var bytes=Data();var buffer=[UInt8](repeating:0,count:4096);while stream.hasBytesAvailable {let count=stream.read(&buffer,maxLength:buffer.count);if count<=0 {break};bytes.append(buffer,count:count)};data=bytes }
        return try! JSONSerialization.jsonObject(with:data) as! [String:Any]
    }
    static func pass(_ mode: String) -> Never {
        UserDefaults.standard.synchronize()
        print("PASS "+mode);exit(0)
    }
    @MainActor static func main() async throws {
        let root=CommandLine.arguments[1], mode=CommandLine.arguments[2]
        guard let domain=ProcessInfo.processInfo.environment["CODEX_NATIVE_TEST_DOMAIN"],
              domain.hasPrefix("com.agent.codex.native-tests."), Bundle.main.bundleIdentifier==domain else {
            check(false,"retry tests require an isolated test-only application preference domain");return
        }
        if ProcessInfo.processInfo.environment["CODEX_NATIVE_TEST_FRESH"]=="1" {
            check(UserDefaults.standard.object(forKey:"macagent.outbox")==nil,"independent retry case starts with no inherited outbox")
        }
        let fixture=try JSONSerialization.jsonObject(with:Data(contentsOf:URL(fileURLWithPath:root+"/016-task-retry-inputs.history.json"))) as! [String:Any]
        let all=fixture["turns"] as! [[String:Any]]
        let prefixes=fixture["prefix_display_turn_indices"] as! [[Int]], groupPrefixes=fixture["prefix_retry_groups"] as! [[[String:Any]]]
        let source="tr_"+String(repeating:"a",count:64), nextRef="tr_"+String(repeating:"b",count:64)
        var rows=Array(all.prefix(mode.hasPrefix("prefix-") ? Int(mode.dropFirst(7))! : mode == "edit-tail" ? 2 : ["audit-readonly","branch-winner"].contains(mode) ? 5 : 1))
        if mode == "edit-tail" { rows[1]["prompt"]="latest canonical input" }
        if mode.hasPrefix("fix-coverage") {rows=Array(all.prefix(3))}
        if mode == "fix-edit-literal" {rows=Array(all.prefix(2));rows[1]["prompt"]="closed raw\n📎 user literal"}
        if mode.hasPrefix("fix-edit-first") {rows=[]}
        var latestRef=source, historyError=false
        var projectionOverride:[Int]? = nil
        func history() -> [String:Any] {
            var h:[String:Any]=["status":true,"session_id":"A","turns":rows,"replay_after":0]
            if mode != "no-ref" {h["last_turn_ref"]=latestRef}
            if mode.hasPrefix("fix-coverage") {
                h["display_turn_indices"]=[2]
                if mode == "fix-coverage-invalid" {h["retry_groups"]=[["root_index":0,"display_index":2,"turn_indices":[0,999]]]}
                if mode == "fix-coverage-partial" {h["retry_groups"]=[["root_index":0,"display_index":2,"turn_indices":[0,2]]]}
                return h
            }
            if rows.isEmpty {h["display_turn_indices"]=[];h["retry_groups"]=[];return h}
            if mode == "invalid-projection" {h["display_turn_indices"]=[999];h["retry_groups"]=[["root_index":0,"display_index":999,"turn_indices":[0,999]]]}
            else if mode == "duplicate-projection" {h["display_turn_indices"]=[0,0]}
            else if mode == "invalid-group" {h["display_turn_indices"]=[0];h["retry_groups"]=[["root_index":0,"display_index":0,"turn_indices":[0,999]]]}
            else if mode != "legacy" {
                h["display_turn_indices"]=projectionOverride ?? prefixes[min(rows.count,7)-1]
                h["retry_groups"]=projectionOverride.map { [["root_index":0,"display_index":$0[0],"turn_indices":[0,1]]] } ?? groupPrefixes[min(rows.count,7)-1]
            }
            return h
        }
        RecoveryTransport.handler={ t in
            let p=t.request.url!.path
            if p.hasSuffix("/history") {
                if historyError {t.respond([:],status:503)}
                else if ["old-new","old-before-new"].contains(mode) && RecoveryTransport.matching("/history").count == 2 {RecoveryTransport.hold(t)}
                else {t.respond(history())}
            }
            else if p.hasSuffix("/turn") { t.respond(["status":true,"turn":NSNull(),"last_seq":0]) }
            else if p.hasSuffix("/events") { RecoveryTransport.hold(t);t.deliver(Data()) }
            else if p.hasSuffix("/deck") {t.respond([:],status:404)}
            else if p.hasSuffix("/retry") || p.hasSuffix("/turns") {RecoveryTransport.hold(t)}
            else if p.hasSuffix("/turns/last") {
                if mode.hasPrefix("fix-edit-") && mode != "fix-edit-literal" {RecoveryTransport.hold(t)}
                else {rows.removeLast();t.respond(["status":true])}
            }
            else {t.respond(["status":true,"sessions":[],"skills":[]])}
        }
        let settings=AppSettings(), store=AgentStore(settings:settings);store.select(sessionId:"A")
        await wait({ RecoveryTransport.matching("/events").count == 1 },"history + stream")
        let stream=held("/events")
        if mode.hasPrefix("fix-coverage") {
            var seen=Set<Int>()
            for index in store.messages.compactMap(\.turnIndex) {seen.insert(index)}
            check(seen==Set(rows.indices) && store.retryGroups.isEmpty,"incomplete audit coverage falls back to every raw record with no residual groups")
            pass(mode)
        }
        if mode.hasPrefix("fix-edit-") {
            let first=mode.hasPrefix("fix-edit-first")
            let prompt="B raw\n📎 user literal"
            if mode != "fix-edit-literal" {
                let type=mode.hasSuffix("approval") ? "awaiting_approval" : "suspended"
                sse(stream,[event(4,"turn_queued",["prompt":prompt],id:"t_B"),event(5,"turn_started",id:"t_B"),event(6,type,["pending_tools":[["name":"write_file"]]],id:"t_B")])
                await wait({store.ledgerPhase==type},"actual B paused")
                let active=store.messages.first{$0.kind == .user && $0.turnIndex == nil}!
                check(store.canEditLastAttempt && store.canEditMessage(active,auditOnly:false) && !store.canEditMessage(active,auditOnly:true),"shared edit eligibility allows actual paused input and keeps audit readonly")
            }
            let project=URL(fileURLWithPath:root).deletingLastPathComponent().deletingLastPathComponent()
            let chat=try String(contentsOfFile:project.appendingPathComponent("apple-app/Sources/Views/ChatView.swift").path,encoding:.utf8)
            let messages=try String(contentsOfFile:project.appendingPathComponent("apple-app/Sources/Views/MessageView.swift").path,encoding:.utf8)
            check(chat.contains(".disabled(!store.canEditLastAttempt)") && messages.contains("if store.canEditMessage(message, auditOnly: auditOnly)"),"menu and message actions share actual Store edit eligibility")
            store.editLastTurn()
            await wait({RecoveryTransport.matching("/turns/last").count==1},"edit DELETE targets actual attempt")
            if mode == "fix-edit-late-round" {
                sse(stream,[event(7,"turn_ended",["status":"cancelled","discarded":true,"response":[]],id:"t_B"),event(8,"turn_queued",["prompt":"C raw"],id:"t_C"),event(9,"turn_started",id:"t_C")])
                await wait({store.ledgerPhase=="running"},"new C owns projection")
                let before=store.messages.map(\.text)
                held("/turns/last").respond(["status":true])
                try? await Task.sleep(nanoseconds:90_000_000)
                check(store.composerPrefill==nil && store.messages.map(\.text)==before && store.isGenerating,"late paused edit response cannot detach or prefill newer turn")
                pass(mode)
            }
            if mode == "fix-edit-aba" {
                store.select(sessionId:"B");await wait({RecoveryTransport.matching("/events").count==2},"edit B selection");_ = held("/events")
                store.select(sessionId:"A");await wait({RecoveryTransport.matching("/events").count==3},"edit A reincarnation");_ = held("/events")
                let before=store.messages.map(\.text)
                held("/turns/last").respond(["status":true])
                try? await Task.sleep(nanoseconds:90_000_000)
                check(store.composerPrefill==nil && store.messages.map(\.text)==before && !store.isGenerating,"ABA late edit response cannot mutate new selected projection")
                pass(mode)
            }
            if mode != "fix-edit-literal" {
                sse(stream,[event(7,"turn_ended",["status":"cancelled","discarded":true,"prompt":prompt,"response":[]],id:"t_B")])
                await wait({store.pendingTools==nil && !store.isSuspended},"owned B discard before HTTP ack")
                held("/turns/last").respond(["status":true])
            }
            await wait({store.composerPrefill != nil},"edit prefill")
            let expected=mode == "fix-edit-literal" ? "closed raw\n📎 user literal" : prompt
            check(store.composerPrefill==expected,"editing preserves actual canonical/queued raw prompt including literal attachment marker")
            if mode != "fix-edit-literal" {check(store.canonicalTurns.count==(first ? 0 : 1),"paused edit does not revoke prior archive")}
            pass(mode)
        }
        if mode.hasPrefix("prefix-") || ["legacy","invalid-projection"].contains(mode) {
            var shown:[Int]=[]
            for index in store.messages.compactMap(\.turnIndex) where !shown.contains(index) {shown.append(index)}
            let expected=["legacy","invalid-projection"].contains(mode) ? Array(0..<rows.count) : prefixes[rows.count-1]
            check(shown==expected,"016 server display indices drive native history with canonical indices")
            check(store.canonicalTurns.count==rows.count,"raw canonical history retained independently of display")
            if rows.count>=2 {check(store.retryGroups.first?.turnIndices.count==rows.count || rows.count==7,"server audit groups preserve every canonical attempt")}
            pass(mode)
        }
        if mode == "duplicate-projection" || mode == "invalid-group" {
            check(store.messages.filter{$0.kind == .user}.count==1 && store.retryGroups.isEmpty,"projection boundary validation avoids duplicated or out-of-range audit records")
            pass(mode)
        }
        if mode == "audit-readonly" {
            let winner=store.messages.first{$0.kind == .assistant}!
            check(winner.turnIndex==2 && !winner.canModifyLatestAttempt(canonicalIndex:store.lastCanonicalIndex,auditOnly:false),"older visible winner cannot edit/regenerate hidden canonical tail")
            let attempts=store.retryGroups.first!.turnIndices.flatMap {ChatMessage.from(turn:store.canonicalTurns[$0],turnIndex:$0)}
            check(attempts.allSatisfy{!$0.canModifyLatestAttempt(canonicalIndex:store.lastCanonicalIndex,auditOnly:true)},"audit messages cannot edit or regenerate even current canonical tail")
            let project=URL(fileURLWithPath:root).deletingLastPathComponent().deletingLastPathComponent()
            let view=try String(contentsOfFile:project.appendingPathComponent("apple-app/Sources/Views/MessageView.swift").path,encoding:.utf8)
            let chat=try String(contentsOfFile:project.appendingPathComponent("apple-app/Sources/Views/ChatView.swift").path,encoding:.utf8)
            check(view.components(separatedBy:"message.canModifyLatestAttempt(").count==3 && chat.contains("MessageView(message: message, auditOnly: true)"),"audit reuses existing renderer and shared readonly eligibility")
            pass(mode)
        }
        if mode == "branch-winner" {
            let index=store.messages.first{$0.kind == .assistant}!.turnIndex!
            store.branch(fromTurnIndex:index)
            await wait({RecoveryTransport.matching("/branch").count==1},"branch request")
            check(body(RecoveryTransport.matching("/branch").last!)["node_index"] as? Int==2,"branch uses true winner canonical index, not display position")
            pass(mode)
        }
        if mode == "worker-ui" {
            check(!AgentStore.hasControllableChat(kind:"prewarm") && !AgentStore.hasControllableChat(kind:"summarize") && AgentStore.hasControllableChat(kind:"chat"),"background workers have no chat controls")
            let view=try String(contentsOfFile:URL(fileURLWithPath:root).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("apple-app/Sources/Views/SessionListView.swift").path,encoding:.utf8)
            check(view.contains("if AgentStore.hasControllableChat(kind: task.kind)"),"task center uses background-worker eligibility")
            pass(mode)
        }
        if mode == "edit-tail" {
            store.editLastTurn();await wait({store.composerPrefill != nil},"canonical edit input")
            check(store.composerPrefill=="latest canonical input","editing uses raw latest canonical prompt rather than older visible winner")
            pass(mode)
        }
        if mode == "old-before-new" {
            // Real R15 window: history saw root, /turn is already null, old retry terminal is delayed.
            // Store is genuinely idle; user submits T2 BEFORE that old T1 terminal triggers recovery.
            store.send(prompt:"pending new input",uploads:[],thinking:false)
            await wait({RecoveryTransport.matching("/turns").count==1},"T2 in flight before delayed retry terminal")
            let newAck=held("/turns")
            let entry=(UserDefaults.standard.dictionary(forKey:"macagent.outbox") as! [String:[String:String]])["A"]!
            let nextKey=entry["key"]!
            var old=all[0]
            old["status"]="success";old["turn_index"]=1;old["turn_ref"]=nextRef
            old["retry_of"]=source;old["retry_root_index"]=0;old["retry_parent_index"]=0
            old["response"]=[["role":"assistant","content":"new winner"]]
            rows.append(old);latestRef=nextRef;projectionOverride=[1]
            sse(stream,[event(7,"turn_ended",old)])
            await wait({RecoveryTransport.matching("/history").count==2},"old T1 terminal recovery after T2 submit")
            check(store.isGenerating && store.messages.filter{$0.kind == .user && $0.turnIndex == nil}.count==1,"old retry restore preserves already in-flight later submit ownership")
            held("/history").respond(history())
            await wait({RecoveryTransport.matching("/events").count==2},"new attachment after preserving T2")
            let current=held("/events")
            sse(current,[event(7,"turn_ended",["status":"success","turn_index":1])])
            try? await Task.sleep(nanoseconds:70_000_000)
            let retained=(UserDefaults.standard.dictionary(forKey:"macagent.outbox") as! [String:[String:String]])["A"]
            check(retained?["key"]==nextKey && store.isGenerating && store.messages.filter{$0.kind == .user && $0.turnIndex == nil}.count==1,"old retry restore/replay preserves later submit ownership and outbox")
            newAck.respond(["status":true,"turn_id":"t_new","seq":8],status:202)
            sse(current,[event(8,"turn_queued",["prompt":"pending new input","client_turn_id":nextKey],id:"t_new"),event(9,"turn_ended",["status":"success","prompt":"pending new input","response":[["role":"assistant","content":"next answer"]],"turn_index":2],id:"t_new")])
            await wait({store.messages.contains{$0.text=="next answer"}},"T2 terminal after old retry restore")
            check(store.messages.filter{$0.kind == .user && $0.turnIndex==2}.count==1,"T2 converges once after earlier retry recovery")
            pass(mode)
        }
        store.regenerateLastTurn()
        try? await Task.sleep(nanoseconds:80_000_000)
        check(RecoveryTransport.matching("/retry").isEmpty && RecoveryTransport.matching("/turns/last").isEmpty,"confirmation never executes/deletes before user decision")
        if mode == "no-delete" {pass(mode)}
        if mode == "no-ref" {
            check(store.retryConfirmationTarget==nil && store.errorMessage != nil,"missing canonical ref explicitly unavailable")
            pass(mode)
        }
        let target=store.retryConfirmationTarget!
        if mode == "confirm-switch" || mode == "confirm-aba" {
            store.select(sessionId:"B");await wait({RecoveryTransport.matching("/events").count==2},"B ready");_ = held("/events")
            if mode == "confirm-aba" {store.select(sessionId:"A");await wait({RecoveryTransport.matching("/events").count==3},"second A ready");_ = held("/events")}
            store.confirmRetry(target)
            check(RecoveryTransport.matching("/retry").isEmpty && store.errorMessage?.contains("已失效")==true,"changed confirmation target explicitly invalid, never retargeted")
            pass(mode)
        }
        if mode == "confirm-cancel" {
            store.cancelRetryConfirmation();store.confirmRetry(target)
            try? await Task.sleep(nanoseconds:60_000_000)
            check(RecoveryTransport.matching("/retry").isEmpty && store.errorMessage != nil,"cancelled confirmation cannot later execute")
            pass(mode)
        }
        if mode == "confirm-client" {
            settings.baseURLString="https://replacement.invalid";store.applySettings()
            try? await Task.sleep(nanoseconds:100_000_000)
            store.confirmRetry(target)
            check(RecoveryTransport.matching("/retry").isEmpty && store.errorMessage != nil,"client incarnation invalidates retry confirmation")
            pass(mode)
        }
        if mode == "confirm-source" {
            latestRef=nextRef;store.refreshDisplayedHistory();await wait({store.lastTurnRef==nextRef},"source refreshed")
            store.confirmRetry(target);check(RecoveryTransport.matching("/retry").isEmpty && store.errorMessage != nil,"source change expires bound confirmation")
            pass(mode)
        }
        store.confirmRetry(target)
        await wait({RecoveryTransport.matching("/retry").count==1},"retry JSON request")
        var ack=held("/retry")
        let sent=body(RecoveryTransport.matching("/retry").last!)
        let key=sent["client_turn_id"] as! String
        check(Set(sent.keys)==Set(["expected_turn_ref","client_turn_id"]) && sent["expected_turn_ref"] as? String==source,"retry sends only canonical ref/own command key, not copied text or generation")
        check(!key.isEmpty && RecoveryTransport.matching("/turns/last").isEmpty,"retry input/history never deleted")
        let saved=UserDefaults.standard.dictionary(forKey:"macagent.outbox") as! [String:[String:String]]
        check(saved["A"]?["operation"]=="retry" && saved["A"]?["source_ref"]==source && saved["A"]?["key"]==key,"retry provenance/key persisted in existing outbox")
        if mode.hasPrefix("fix-conflict") {
            let reason=mode == "fix-conflict-source" ? "retry_source_changed" : mode == "fix-conflict-lookup" ? "retry_lookup_unavailable" : "retry_inputs_unavailable"
            ack.respond(["status":false,"reason":reason],status:409)
            await wait({!store.isGenerating && store.errorMessage != nil},"known retry refusal")
            let hint=mode == "fix-conflict-source" ? "同步" : mode == "fix-conflict-lookup" ? "核验" : "重新附上"
            check(store.errorMessage!.contains(hint) && !store.errorMessage!.contains(reason),"known retry conflict has actionable localized guidance")
            check(store.messages.contains{$0.text=="旧尝试的答案"} && store.lastTurnRef==source,"known refusal preserves original winner and canonical ref")
            let entry=(UserDefaults.standard.dictionary(forKey:"macagent.outbox") as! [String:[String:String]])["A"]!
            check(entry["key"]==key && entry["source_ref"]==source,"known refusal retains retry command provenance safely")
            if mode == "fix-conflict-lookup" {check(entry["retry_uncertain"]=="1" && store.historyProjectionNeedsRefresh,"unverifiable lookup remains unknown with same command key and resync")}
            if mode == "fix-conflict-source" {check(store.historyProjectionNeedsRefresh,"changed source exposes resync before retry")}
            check(RecoveryTransport.matching("/retry").count==1 && RecoveryTransport.matching("/turns").isEmpty,"known refusal never autoexecutes retry or plain fallback")
            pass(mode)
        }
        if mode == "404" || mode == "ambiguous" || mode == "plain-key" || mode == "restart-ambiguous" {
            ack.respond(["status":false],status:mode == "404" ? 404 : 503)
            await wait({!store.isGenerating && store.errorMessage != nil},"failure visible")
            check(store.messages.contains{$0.text=="旧尝试的答案"} && store.canonicalTurns[0].attachments?.first?.name=="report.pdf","retry failure retains old answer and attachment input")
            if mode == "404" {check(store.errorMessage!.contains("不支持"),"old retry endpoint explicitly unavailable");print("PASS 404");exit(0)}
            if mode == "plain-key" {
                store.send(prompt:all[0]["prompt"] as! String,uploads:[],thinking:false)
                await wait({RecoveryTransport.matching("/turns").count==1},"plain submit")
                let entries=UserDefaults.standard.dictionary(forKey:"macagent.outbox") as! [String:[String:String]]
                check(entries["A"]?["key"] != key && entries["A"]?["operation"]=="send","plain sameprompt must not reuse retry command key")
                pass(mode)
            }
            if mode == "restart-ambiguous" {
                let restarted=AgentStore(settings:AppSettings());restarted.select(sessionId:"A")
                await wait({RecoveryTransport.matching("/events").count==2},"restored retry outbox")
                check(restarted.messages.contains{$0.text.contains("上次任务重试")},"retry restore hint never suggests plain text resend")
                restarted.requestRetryConfirmation();restarted.confirmRetry(restarted.retryConfirmationTarget!)
                await wait({RecoveryTransport.matching("/retry").count==2},"restart explicit resend")
                check(body(RecoveryTransport.matching("/retry").last!)["client_turn_id"] as? String==key,"restart preserves operation/source ref and ambiguous command key")
                pass(mode)
            }
            store.requestRetryConfirmation();store.confirmRetry(store.retryConfirmationTarget!)
            await wait({RecoveryTransport.matching("/retry").count==2},"ambiguous command resend")
            check(body(RecoveryTransport.matching("/retry").last!)["client_turn_id"] as? String==key,"ambiguous retry resends exact command key")
            ack=held("/retry")
        }
        if mode == "late-aba" || mode == "late-error-aba" {
            store.select(sessionId:"B");await wait({RecoveryTransport.matching("/events").count==2},"B attachment");_ = held("/events")
            store.select(sessionId:"A");await wait({RecoveryTransport.matching("/events").count==3},"second A attachment");_ = held("/events")
            let before=store.messages.map(\.text)
            ack.respond(["status":true,"turn_id":"t_retry","seq":4],status:mode == "late-error-aba" ? 503 : 202)
            try? await Task.sleep(nanoseconds:70_000_000)
            check(store.messages.map(\.text)==before && store.errorMessage==nil && !store.isGenerating,"late retry receipt cannot alter ABA projection")
            pass(mode)
        }
        let queued=event(4,"turn_queued",["prompt":all[0]["prompt"]!,"retry_of":source,"client_turn_id":key,"retry_root_index":0,"retry_parent_index":0])
        var terminal=rows[0]
        terminal["status"]=mode == "terminal-fail" ? "fail" : mode == "terminal-warning" ? "warning" : mode == "terminal-cancel" ? "cancelled" : "success"
        terminal["retry_of"]=source;terminal["retry_root_index"]=0;terminal["retry_parent_index"]=0;terminal["turn_ref"]=nextRef;terminal["turn_index"]=1
        terminal["response"]=[["role":"assistant","content":mode == "terminal-fail" ? "failed retry" : "new winner"]]
        terminal["execution_receipts"]=["items":[["seq":5,"ts":5,"type":"tool_started","name":"write_file","call_id":"write1","detail":"","truncated":true],["seq":6,"ts":6,"type":"tool_result","name":"write_file","call_id":"write1","detail":"wrote report.txt","status":"ok"]],"truncated":true]
        if mode != "early-terminal" {ack.respond(["status":true,"turn_id":"t_retry","seq":4,"duplicate":mode == "duplicate"],status:202)}
        if mode == "duplicate" {
            rows.append(terminal);latestRef=nextRef;projectionOverride=[1]
            await wait({RecoveryTransport.matching("/events").count==2},"duplicate unified recovery")
            check(store.canonicalTurns.count==2,"duplicate retry reconciles history immediately")
            pass(mode)
        }
        sse(stream,[queued,event(5,"turn_started")]);await wait({store.ledgerPhase=="running"},"retry live")
        if mode == "audit-approval" {
            sse(stream,[event(6,"awaiting_approval",["pending_tools":[["name":"write_file","arguments":["path":"report.txt"]]]])])
            await wait({store.pendingApprovalTarget != nil},"retry preserves normal approval gate")
            check(store.messages.filter{$0.kind == .user}.count==1,"retry queued does not duplicate archived input")
            pass(mode)
        }
        rows.append(terminal);latestRef=nextRef
        projectionOverride=[terminal["status"] as? String == "success" ? 1 : 0]
        if mode == "history-before-terminal" {
            store.refreshDisplayedHistory()
            await wait({store.canonicalTurns.count==2 && RecoveryTransport.matching("/events").count==2},"history snapshot before terminal")
            let replay=held("/events")
            sse(replay,[queued,event(5,"turn_started"),event(7,"turn_ended",terminal)])
            await wait({!store.isGenerating},"archived retry terminal converges receipt")
            check(store.messages.contains{$0.text=="new winner"} && store.messages.filter{$0.kind == .user}.count==1,"history-before-terminal does not duplicate retry input/result")
            pass(mode)
        }
        if mode == "history-error" {historyError=true}
        sse(stream,[event(7,"turn_ended",mode == "thin" ? ["status":"success","turn_index":1] : terminal)])
        if mode == "old-new" {
            await wait({!store.isGenerating && RecoveryTransport.matching("/history").count==2},"retry ended + authoritative history held")
            store.send(prompt:"pending new input",uploads:[],thinking:false)
            await wait({RecoveryTransport.matching("/turns").count==1},"new plain submit pending old retry restore")
            let newAck=held("/turns")
            let pending=(UserDefaults.standard.dictionary(forKey:"macagent.outbox") as! [String:[String:String]])["A"]!
            let nextKey=pending["key"]!
            held("/history").respond(history())
            await wait({RecoveryTransport.matching("/events").count==2},"preserved submit recovery stream")
            let current=held("/events")
            sse(current,[queued,event(7,"turn_ended",["status":"success","turn_index":1])])
            try? await Task.sleep(nanoseconds:70_000_000)
            let retained=(UserDefaults.standard.dictionary(forKey:"macagent.outbox") as! [String:[String:String]])["A"]
            check(retained?["key"]==nextKey && store.isGenerating && store.messages.filter{$0.kind == .user && $0.turnIndex == nil}.count==1,"old retry restore/replay preserves later submit ownership and outbox")
            newAck.respond(["status":true,"turn_id":"t_new","seq":8],status:202)
            sse(current,[event(8,"turn_queued",["prompt":"pending new input","client_turn_id":nextKey],id:"t_new"),event(9,"turn_ended",["status":"success","prompt":"pending new input","response":[["role":"assistant","content":"next answer"]],"turn_index":2],id:"t_new")])
            await wait({store.messages.contains{$0.text=="next answer"}},"later submit terminal")
            check(store.messages.filter{$0.kind == .user && $0.turnIndex==2}.count==1,"later submit bubble exactly once after retry restore")
            pass(mode)
        }
        if mode == "early-terminal" {await wait({!store.isGenerating},"terminal before accepted");ack.respond(["status":true,"turn_id":"t_retry","seq":4],status:202)}
        if mode == "history-error" {
            await wait({store.errorMessage != nil},"history restore failure visible")
            check(store.messages.contains{$0.text=="旧尝试的答案"} && store.historyProjectionNeedsRefresh,"history failure retains old display and resync affordance")
            historyError=false;store.refreshDisplayedHistory()
        }
        await wait({store.canonicalTurns.count==2 && !store.historyProjectionNeedsRefresh},"authoritative retry projection")
        let expectedIndex=terminal["status"] as? String == "success" ? 1 : 0
        check(store.messages.allSatisfy{$0.turnIndex==expectedIndex},"retry result uses true server winner canonical index")
        if expectedIndex==1 {check(store.messages.contains{$0.text=="new winner"} && !store.messages.contains{$0.text=="旧尝试的答案"},"successful retry replaces original result display")}
        else {check(store.messages.contains{$0.text=="旧尝试的答案"},"failed retry keeps previous winner")}
        check(ChatMessage.from(turn:store.canonicalTurns[1],turnIndex:1).filter{$0.kind == .tool}.count==2,"audit maps actual started/result facts for hidden unsuccessful attempts")
        await wait({RecoveryTransport.matching("/events").count==2},"restored attachment visible before replay")
        let beforeCalls=RecoveryTransport.matching("/history").count
        let current=held("/events")
        sse(current,[queued,event(7,"turn_ended",["status":"success","turn_index":1])])
        try? await Task.sleep(nanoseconds:80_000_000)
        check(RecoveryTransport.matching("/history").count==beforeCalls && store.messages.allSatisfy{$0.turnIndex == expectedIndex},"hidden raw canonical replay neither appends nor reloads forever")
        if mode == "fresh-key" {
            store.requestRetryConfirmation();store.confirmRetry(store.retryConfirmationTarget!)
            await wait({RecoveryTransport.matching("/retry").count==2},"new explicit retry")
            check(body(RecoveryTransport.matching("/retry").last!)["client_turn_id"] as? String != key,"new explicit attempt uses fresh key")
        }
        pass(mode)
    }
}
