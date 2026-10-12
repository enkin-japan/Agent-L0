// Full production Store/API/SSE; only system services and URLProtocol are synthetic.
import Foundation
@main struct ApprovalBindingTest {
    @MainActor static func wait(_ condition: () -> Bool, _ message: String) async {
        for _ in 0..<400 { if condition() { return }; try? await Task.sleep(nanoseconds:5_000_000) }
        check(false,"timeout "+message)
    }
    static func body(_ request: URLRequest) -> [String:Any] {
        let data:Data
        if let raw=request.httpBody {data=raw}
        else {
            let stream=request.httpBodyStream!;stream.open();defer{stream.close()}
            var bytes=Data();var buffer=[UInt8](repeating:0,count:4096)
            while stream.hasBytesAvailable {let count=stream.read(&buffer,maxLength:buffer.count);if count<=0 {break};bytes.append(buffer,count:count)}
            data=bytes
        }
        return try! JSONSerialization.jsonObject(with:data) as! [String:Any]
    }
    static func sse(_ stream: RecoveryTransport, _ events: [[String:Any]]) {
        let bytes=events.map{"data: "+String(data:try! JSONSerialization.data(withJSONObject:$0),encoding:.utf8)!+"\n\n"}.joined()
        stream.deliver(Data(bytes.utf8))
    }
    @MainActor static func main() async throws {
        let mode=CommandLine.arguments[1]
        guard let domain=ProcessInfo.processInfo.environment["CODEX_NATIVE_TEST_DOMAIN"],
              domain.hasPrefix("com.agent.codex.native-tests."), Bundle.main.bundleIdentifier==domain else {
            check(false,"approval tests require an isolated test-only application preference domain");return
        }
        check(UserDefaults.standard.object(forKey:"macagent.outbox")==nil,"approval case starts with no inherited outbox")
        let lines=try String(contentsOfFile:CommandLine.arguments[2],encoding:.utf8).split(separator:"\n")
        let events=try lines.map{try JSONSerialization.jsonObject(with:Data($0.utf8)) as! [String:Any]}
        let env=try JSONDecoder().decode(EventEnvelope.self,from:Data(lines.last!.utf8))
        if mode=="arguments" {
            let tools=env.decode([PendingTool].self,at:"pending_tools")!
            let presentation=ApprovalArguments(tools[0].arguments!)
            check(presentation.fullText.contains("APPROVAL_TAIL_VISIBLE"),"complete parameter tail is missing")
            let encoded=try JSONSerialization.jsonObject(with:Data(presentation.fullText.utf8)) as! [String:String]
            check(encoded["content"]?.hasSuffix("APPROVAL_TAIL_VISIBLE")==true,"complete text differs from executable arguments")
            check(presentation.summary==String(presentation.fullText.prefix(500))+"…","summary must be a prefix of the same complete serialization")
            print("PASS "+mode);exit(0)
        }
        RecoveryTransport.handler={t in
            let path=t.request.url!.path
            if path.hasSuffix("/history") {t.respond(["status":true,"session_id":String(path.split(separator:"/").dropLast().last!),"turns":[],"replay_after":0])}
            else if path.hasSuffix("/turn") {t.respond(["status":true,"turn":NSNull(),"last_seq":0])}
            else if path.hasSuffix("/deck") {t.respond([:],status:404)}
            else if path.hasSuffix("/events") {RecoveryTransport.hold(t);t.deliver(Data())}
            else if path.hasSuffix("/approval") {RecoveryTransport.hold(t)}
            else {t.respond(["status":true,"sessions":[],"skills":[]])}
        }
        let store=AgentStore(settings:AppSettings())
        store.select(sessionId:mode=="capture" ? "s2" : "s1")
        await wait({RecoveryTransport.matching("/events").count==1},"first actual attachment")
        let stream=RecoveryTransport.takeHeld()
        sse(stream,mode=="capture" ? events : Array(events.prefix(3)))
        await wait({store.pendingApprovalTarget != nil},"journal approval capture")
        if mode=="capture" {
            await wait({store.pendingApprovalTarget?.seq==7},"B approval")
            check(store.pendingApprovalTarget==PendingApprovalTarget(sessionId:"s2",turnId:"t_card_b",seq:7),"card did not capture the approval envelope identity")
            check(store.pendingTools?.first?.arguments?["content"]?.compact(max:Int.max).contains("APPROVAL_TAIL_VISIBLE")==true,"event projection lost full arguments")
            sse(stream,[["seq":8,"turn_id":"t_card_b","type":"resumed","data":[:]]])
            await wait({store.pendingApprovalTarget==nil},"resumed invalidation")
            check(store.pendingTools==nil,"resumed did not invalidate pending tools")
            print("PASS "+mode);exit(0)
        }
        let target=store.pendingApprovalTarget!
        check(target==PendingApprovalTarget(sessionId:"s1",turnId:"t_card_a",seq:3),"initial A identity captured through production SSE")
        if mode=="stale-card" || mode=="stale-batch" {
            if mode=="stale-card" {sse(stream,Array(events.dropFirst(3)))}
            else {var next=events.last!;next["turn_id"]="t_card_a";sse(stream,[next])}
            await wait({store.pendingApprovalTarget?.seq==7},"replaced approval batch")
            store.decideApproval(approve:true,target:target)
            try? await Task.sleep(nanoseconds:100_000_000)
            check(RecoveryTransport.matching("/approval").isEmpty,"stale A card sent a request for B")
            print("PASS "+mode);exit(0)
        }
        store.decideApproval(approve:true,target:target)
        await wait({RecoveryTransport.matching("/approval").count==1},"approval POST")
        let pending=RecoveryTransport.takeHeld(),request=RecoveryTransport.matching("/approval").first!,json=body(request)
        check(request.url?.path=="/api/v1/chat/s1/turns/approval","approval session changed")
        check(json["approve"] as? Bool==true,"approve decision differs from displayed action")
        check(json["expected_turn_id"] as? String=="t_card_a","displayed A identity missing from request")
        check(json["expected_approval_seq"] as? Int==3,"displayed A approval batch missing from request")
        if mode=="body" {
            pending.respond(["status":true,"turn_id":"t_card_a","seq":4],status:202)
            let legacy=Task {try await store.client!.submitApproval(sessionId:"legacy",approve:false)}
            await wait({RecoveryTransport.matching("/approval").count==2},"legacy compatible POST")
            let old=body(RecoveryTransport.matching("/approval").last!)
            check(old["approve"] as? Bool==false && old["expected_turn_id"]==nil && old["expected_approval_seq"]==nil,"legacy approval caller remains compatible")
            RecoveryTransport.takeHeld().respond(["status":true],status:202)
            _=try await legacy.value
            print("PASS "+mode);exit(0)
        }
        if mode.hasPrefix("late-") {
            var replacement=Array(events.dropFirst(3))
            replacement[replacement.count-1]["data"]=["pending_tools":[["name":"write_file","arguments":["content":"B"]]]]
            if mode.hasPrefix("late-session") {
                store.select(sessionId:"s2")
                await wait({RecoveryTransport.matching("/events").count==2},"actual session replacement")
                sse(RecoveryTransport.takeHeld(),Array(replacement.dropFirst()))
            } else {sse(stream,replacement)}
            await wait({store.pendingApprovalTarget?.turnId=="t_card_b" && !store.isGenerating},"B journal approval")
        }
        let historyCalls=RecoveryTransport.matching("/history").count,eventCalls=RecoveryTransport.matching("/events").count
        let reply: [String:Any] = mode.hasSuffix("202") ? ["status":true,"turn_id":"t_card_a","seq":4] : ["reason":"approval_target_mismatch"]
        pending.respond(reply,status:mode.hasSuffix("409") ? 409 : mode.hasSuffix("202") ? 202 : 500)
        try? await Task.sleep(nanoseconds:100_000_000)
        if mode=="retry" {
            check(store.pendingApprovalTarget==target && store.pendingTools != nil && !store.isGenerating,"failed current approval cannot be retried")
            store.decideApproval(approve:true,target:target)
            await wait({RecoveryTransport.matching("/approval").count==2},"actual approval retry")
            let retried=body(RecoveryTransport.matching("/approval").last!)
            check(retried["expected_turn_id"] as? String=="t_card_a" && retried["expected_approval_seq"] as? Int==3,"retry retains original displayed approval target")
            RecoveryTransport.takeHeld().respond(["status":true],status:202)
        } else {
            check(RecoveryTransport.matching("/history").count==historyCalls && RecoveryTransport.matching("/events").count==eventCalls,"late approval detached current session")
            check(store.pendingApprovalTarget==PendingApprovalTarget(sessionId:mode.hasPrefix("late-session") ? "s2" : "s1",turnId:"t_card_b",seq:7),"late approval replaced B target")
            check(store.pendingTools?.first?.arguments?["content"]?.compact()=="B","late approval restored A tools")
            check(store.errorMessage==nil,"late approval leaked an error into B")
            check(!store.isGenerating,"late approval changed B paused state")
        }
        print("PASS "+mode);exit(0)
    }
}
