// Complete production Store/API/SSE; no replacement lifecycle methods.
import Foundation
@main struct ThinRecoveryTests {
    @MainActor static func wait(_ condition: () -> Bool, _ message: String) async {
        for _ in 0..<200 {if condition(){return};try? await Task.sleep(nanoseconds:5_000_000)}
        check(false,"timeout "+message)
    }
    static func held(_ suffix:String) -> RecoveryTransport {
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
    static func sse(_ stream:RecoveryTransport,_ event:[String:Any]) {
        stream.deliver(Data(("data: "+String(data:try! JSONSerialization.data(withJSONObject:event),encoding:.utf8)!+"\n\n").utf8))
    }
    @MainActor static func main() async throws {
        exitOnFatalSignals()
        let mode=CommandLine.arguments[1],root=CommandLine.arguments[2]
        #if THIN_ACTUAL_L10N
        L10n.current = .zh
        #endif
        guard let domain=ProcessInfo.processInfo.environment["CODEX_NATIVE_TEST_DOMAIN"],
              domain.hasPrefix("com.agent.codex.native-tests."),Bundle.main.bundleIdentifier==domain else {check(false,"test-only preference domain required");return}
        check(UserDefaults.standard.object(forKey:"macagent.outbox")==nil,"no inherited submit")
        let fixture=try JSONSerialization.jsonObject(with:Data(contentsOf:URL(fileURLWithPath:root+"/014-history-replay-boundary.restore.json"))) as! [String:Any]
        var thin=fixture["thin_terminal"] as! [String:Any]
        var data=thin["data"] as! [String:Any]
        let preserve=mode=="preserve-t2" || mode=="manual" || mode=="locale"
        let invalid=mode.hasSuffix("index")
        if preserve {data["turn_index"]=1}
        switch mode {
        case "nil-index": data.removeValue(forKey:"turn_index")
        case "negative-index": data["turn_index"] = -1
        case "fraction-index": data["turn_index"] = 0.5
        case "huge-index": data["turn_index"] = 1e300
        case "string-index": data["turn_index"] = "0"
        default:break
        }
        thin["data"]=data
        let old:[String:Any]=["status":"success","prompt":"old input","response":[["role":"assistant","content":"old winner"]]]
        let filled=fixture["history_including"] as! [String:Any]
        var complete=mode=="normal",aHistory=0
        let late=mode.hasPrefix("late-")
        RecoveryTransport.handler={t in
            let path=t.request.url!.path
            if path.hasSuffix("/history") {
                let isA=path.hasSuffix("/chat/A/history")
                if isA {aHistory+=1}
                if late && isA && aHistory==2 && t.request.url!.host=="fixture.invalid" {RecoveryTransport.hold(t);return}
                if !isA || (late && aHistory>2) || t.request.url!.host=="replacement.invalid" {
                    t.respond(["status":true,"session_id":isA ? "A" : "B","turns":[["status":"success","prompt":"new selection","response":[["role":"assistant","content":"current selection"]]]],"replay_after":4]);return
                }
                var h:[String:Any]=["status":true,"session_id":"A","count":0,"turns":[]]
                if mode.contains("zero") {h["replay_after"]=0}
                if aHistory==1 && (preserve || invalid) {h["turns"]=[old];h["last_turn_ref"]="retained-ref"}
                if (mode=="normal" && aHistory>=2) || (complete && mode != "normal") {h=filled;if preserve {h["turns"]=[old]+(filled["turns"] as! [[String:Any]])}}
                t.respond(h)
            } else if path.hasSuffix("/turn") {
                let snapshot:Any=mode.contains("ended") ? ["state":"ended","turn_id":"t_restore","first_seq":1,"last_seq":4] : NSNull()
                t.respond(["status":true,"turn":snapshot,"last_seq":4])
            } else if path.hasSuffix("/events") {RecoveryTransport.hold(t);t.deliver(Data())}
            else if path.hasSuffix("/turns") {RecoveryTransport.hold(t)}
            else if path.hasSuffix("/deck") {t.respond([:],status:404)}
            else if path=="/health" {t.respond(["status":"ok"])}
            else {t.respond(["status":true,"sessions":[],"skills":[],"tasks":[],"running":[]])}
        }
        let settings=AppSettings(),store=AgentStore(settings:settings)
        store.select(sessionId:"A")
        await wait({RecoveryTransport.matching("/chat/A/events").count==1},"initial attachment")
        let initial=held("/chat/A/events")
        if mode=="preserve-t2" {
            store.send(prompt:"T2 input",uploads:[],thinking:false)
            await wait({RecoveryTransport.matching("/turns").count==1},"real later submit")
        }
        let before=UserDefaults.standard.dictionary(forKey:"macagent.outbox")
        sse(initial,thin)
        await wait({aHistory>=2},"same recovery entrance")
        if late {
            let delayed=held("/chat/A/history")
            // A replaced backend never inherits the previous selection (its own cache
            // does not know it), so the new incarnation is opened explicitly here.
            if mode=="late-client" {settings.baseURLString="https://replacement.invalid";store.applySettings();store.select(sessionId:"A")}
            else {store.select(sessionId:"B");await wait({store.messages.contains{$0.text=="current selection"}},"B selection");if mode=="late-aba" {store.select(sessionId:"A")}}
            await wait({store.messages.contains{$0.text=="current selection"}},"new incarnation")
            delayed.respond(["status":true,"session_id":"A","turns":[]])
            try? await Task.sleep(nanoseconds:80_000_000)
            check(store.errorMessage==nil && store.messages.contains{$0.text=="current selection"},"old missing-record target cannot pollute session/client/ABA")
            print("PASS "+mode);exit(0)
        }
        if mode=="normal" {
            await wait({RecoveryTransport.matching("/chat/A/events").count==2},"normal thin recovery continues")
            sse(held("/chat/A/events"),thin)
            try? await Task.sleep(nanoseconds:80_000_000)
            check(aHistory==2 && store.errorMessage==nil && store.messages.map(\.text)==["restore prompt","restore answer"],"normal covered thin recovers once without duplicate response")
            print("PASS "+mode);exit(0)
        }
        await wait({store.errorMessage != nil},"missing thin record stops bounded recovery")
        try? await Task.sleep(nanoseconds:80_000_000)
        print("OBSERVED \(mode) history=\(aHistory) events=\(RecoveryTransport.matching("/chat/A/events").count) error=\(store.errorMessage ?? "nil")")
        check(aHistory==2 && RecoveryTransport.matching("/chat/A/events").count==1 && store.historyProjectionNeedsRefresh,"missing thin record stops bounded recovery")
        check(store.errorMessage!.contains("手动同步"),"incomplete synchronization has actionable stable error")
        if preserve || invalid {check(store.messages.contains{$0.text=="old winner"} && store.lastTurnRef=="retained-ref" && store.canonicalTurns.count==1,"incomplete reload preserves original winner and canonical metadata")}
        else {check(store.messages.isEmpty && store.canonicalTurns.isEmpty,"thin does not fabricate archived content")}
        if mode=="preserve-t2" {
            check(store.messages.filter{$0.kind == .user && $0.turnIndex==nil}.map(\.text)==["T2 input"] && store.isGenerating,"incomplete reload preserves later optimistic submit")
            check((before as NSDictionary?)?.isEqual(to:UserDefaults.standard.dictionary(forKey:"macagent.outbox") ?? [:])==true,"old thin does not consume later outbox receipt")
        }
        #if THIN_ACTUAL_L10N
        if mode=="locale" {
            L10n.current = .en
            complete=true;store.refreshDisplayedHistory()
            await wait({RecoveryTransport.matching("/chat/A/events").count==2},"locale manual completed history")
            sse(held("/chat/A/events"),thin)
            try? await Task.sleep(nanoseconds:80_000_000)
            print("LOCALE resolved=\(L10n.resolved) refreshed=\(!store.historyProjectionNeedsRefresh) winner=\(store.messages.contains{$0.text=="restore answer"}) error=\(store.errorMessage ?? "nil")")
            check(!store.historyProjectionNeedsRefresh && store.messages.contains{$0.text=="restore answer"},"valid history completes after locale switch")
            check(store.errorMessage==nil,"completed sync clears prior incomplete error across language changes")
        }
        #endif
        if mode=="stable-error" {
            // Ordinary errors auto-clear after 6.0 s; wait well past that so a loaded machine
            // cannot delay a (mutant) dismissal beyond the check and let it pass unseen.
            // Upper bounds: run_native_case times out at 20 s, and the connection monitor
            // re-polls sessions at ~15 s -- keep this wait below both.
            try? await Task.sleep(nanoseconds:9_000_000_000)
            check(store.errorMessage != nil && aHistory==2,"incomplete synchronization error stays visible until explicit recovery")
        }
        if mode=="manual" {
            store.refreshDisplayedHistory()
            await wait({aHistory==3 && (store.errorMessage != nil || RecoveryTransport.matching("/chat/A/events").count==2)},"manual sync one retry")
            try? await Task.sleep(nanoseconds:50_000_000)
            check(aHistory==3 && RecoveryTransport.matching("/chat/A/events").count==1 && store.messages.contains{$0.text=="old winner"} && store.canonicalTurns.count==1 && store.lastTurnRef=="retained-ref" && store.historyProjectionNeedsRefresh && store.errorMessage != nil,"manual incomplete reload preserves winner without false success or reattachment")
            complete=true;store.refreshDisplayedHistory()
            await wait({RecoveryTransport.matching("/chat/A/events").count==2},"manual sync repaired history")
            sse(held("/chat/A/events"),thin)
            try? await Task.sleep(nanoseconds:80_000_000)
            check(aHistory==4 && store.errorMessage==nil && !store.historyProjectionNeedsRefresh && store.messages.contains{$0.text=="restore answer"},"manual repaired history resumes same entrance")
        }
        print("PASS "+mode);exit(0)
    }
}
