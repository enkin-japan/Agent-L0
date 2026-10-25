// Actual complete production Store/API/SSE; synthetic URLProtocol only.
import Foundation
// Scheduler instrumentation is referenced only by isolated full-source proof builds.
@MainActor enum ManagementSubmissionGate {
    static var pending: CheckedContinuation<Void,Never>?
    static func wait() async { await withCheckedContinuation { pending = $0 } }
    static func release() { pending?.resume();pending = nil }
}
@main struct ManagementStoreTests {
    @MainActor static func wait(_ condition:()->Bool,_ name:String) async {
        for _ in 0..<400 {if condition(){return};try? await Task.sleep(nanoseconds:5_000_000)}
        check(false,"timeout "+name)
    }
    static func held(_ suffix:String)->RecoveryTransport {
        RecoveryTransport.lock.lock();defer{RecoveryTransport.lock.unlock()}
        return RecoveryTransport.held.remove(at:RecoveryTransport.held.firstIndex{$0.request.url!.path.hasSuffix(suffix)}!)
    }
    static func body(_ req:URLRequest)->[String:Any] {
        if let data=req.httpBody {return try! JSONSerialization.jsonObject(with:data) as! [String:Any]}
        let stream=req.httpBodyStream!;stream.open();defer{stream.close()};var bytes=Data();var buffer=[UInt8](repeating:0,count:4096)
        while stream.hasBytesAvailable {let n=stream.read(&buffer,maxLength:buffer.count);if n<=0 {break};bytes.append(buffer,count:n)}
        return try! JSONSerialization.jsonObject(with:bytes) as! [String:Any]
    }
    static func sse(_ stream:RecoveryTransport,_ event:[String:Any]) {
        stream.deliver(Data(("data: "+String(data:try! JSONSerialization.data(withJSONObject:event),encoding:.utf8)!+"\n\n").utf8))
    }
    @MainActor static func main() async throws {
        let mode=CommandLine.arguments[1],root=CommandLine.arguments[2]
        guard let domain=ProcessInfo.processInfo.environment["CODEX_NATIVE_TEST_DOMAIN"],Bundle.main.bundleIdentifier==domain,domain.hasPrefix("com.agent.codex.native-tests.") else {check(false,"isolated synthetic preferences");return}
        let absence=try JSONSerialization.jsonObject(with:Data(contentsOf:URL(fileURLWithPath:root+"/protocol/fixtures/017-delete-absent.responses.json"))) as! [String:[String:Any]]
        let rows=("ABC").map {["session_id":String($0),"title":String($0)]}
        let items=try JSONDecoder().decode([SessionMetaItem].self,from:JSONSerialization.data(withJSONObject:rows))
        let item=items[0]
        var refused=true,scheduleFails=false,completed=false,listHold=false,runHold=false,deleted=Set<String>(),histories=0
        var deleteCalls:[String]=[]
        func history(_ sid:String)->[String:Any] {
            histories+=1
            return ["status":true,"session_id":sid,"turns":[["status":"success","prompt":"keep input","response":[["role":"assistant","content":"keep winner \(sid)"]]]],"last_turn_ref":"ref-\(sid)","replay_after":0]
        }
        RecoveryTransport.handler={t in
            let p=t.request.url!.path
            if p.hasSuffix("/rag/document") && t.request.httpMethod=="DELETE" {
                let path=body(t.request)["source_path"] as! String
                deleteCalls.append(path)
                if mode=="doc-client" {RecoveryTransport.hold(t);return}
                if path=="/fake/b" && refused {
                    t.respond(["status":false,"msg":"synthetic doc refusal"],status:mode=="doc-http" ? 503 : 200)
                } else if mode=="doc-zero" || mode=="doc-legacy" {t.respond(["msg":"legacy confirmed","deleted_chunks":mode=="doc-zero" ? 0 : 1])}
                else {t.respond(["status":true,"msg":"confirmed"])}
            } else if t.request.httpMethod=="DELETE" {
                let sid=String(p.split(separator:"/").last!)
                deleteCalls.append(sid)
                if ["delete-inflight","delete-aba","delete-other-submit","delete-client-cleanup","delete-writes","delete-retry-confirm","delete-paused","egress-fence","delete-live-terminal","delete-history-progress","schedule-action-deleting","delete-new-intent"].contains(mode) {RecoveryTransport.hold(t)}
                else if mode=="delete-absent" {t.respond(absence["absent"]!)}
                else if mode=="delete-legacy-missing" {t.respond(["status":false,"response":"Target is already absent","turn_count":0])}
                else if mode=="delete-false" || mode=="delete-http" || (sid=="B" && refused && mode=="delete-partial") {t.respond(["status":false,"response":"synthetic refusal"],status:mode=="delete-http" ? 503 : 200)}
                else {deleted.insert(sid);t.respond(["status":true])}
            } else if p.hasSuffix("/history") {
                if mode=="delete-history-progress" && histories>0 {histories+=1;RecoveryTransport.hold(t)}
                else {t.respond(history(String(p.split(separator:"/").dropLast().last!)))}
            }
            else if p.hasSuffix("/turn") {t.respond(["status":true,"turn":NSNull(),"last_seq":0])}
            else if p.hasSuffix("/events") {RecoveryTransport.hold(t);t.deliver(Data())}
            else if p.hasSuffix("/toggle") && mode=="schedule-action-inflight" {RecoveryTransport.hold(t)}
            else if p.hasSuffix("/tasks") {t.respond(["status":mode != "schedule-global-false","tasks":[],"running":[]])}
            else if p.hasSuffix("/runs") {
                if runHold {RecoveryTransport.hold(t)}
                else {t.respond(["status":!scheduleFails,"runs":[["run_id":"run","status":completed ? "success" : "running","session_id":"schedule_job"]]],status:mode=="runs-http" && scheduleFails ? 503 : 200)}
            } else if p.hasSuffix("/schedules") {
                if listHold {RecoveryTransport.hold(t)}
                else {t.respond(["status":!scheduleFails,"schedules":[["id":"job","name":"kept schedule","running":!completed,"last_run":["run_id":"run","status":completed ? "success" : "running"]]]],status:mode=="schedule-http" && scheduleFails ? 503 : 200)}
            } else if p.hasSuffix("/deck") {t.respond([:],status:404)}
            else if p.hasSuffix("/turns") || p.hasSuffix("/retry") || p.hasSuffix("/resume") || p.hasSuffix("/approval") || p.hasSuffix("/branch") {RecoveryTransport.hold(t)}
            else if p=="/health" {t.respond(["status":"ok"])}
            else {t.respond(["status":true,"sessions":rows.filter{!deleted.contains($0["session_id"]!)},"skills":[]])}
        }
        let settings=AppSettings(),store=AgentStore(settings:settings)
        if mode.hasPrefix("schedule-action") {
            await store.refreshSchedules()
            if mode=="schedule-action-stale" {scheduleFails=true;await store.refreshSchedules()}
            var deleting:Task<AgentStore.ManagementDeleteResult,Never>?
            if mode=="schedule-action-deleting" {
                let target=try JSONDecoder().decode(SessionMetaItem.self,from:Data(#"{"session_id":"schedule_job"}"#.utf8))
                deleting=Task{await store.deleteSession(target)}
                await wait({deleteCalls.count==1},"scheduled parent deletion")
            }
            if mode=="schedule-action-inflight" {
                let action=Task{try await store.mutateSchedule("job",mutation:.enabled(false))}
                await wait({RecoveryTransport.matching("/toggle").count==1},"scheduled actual HTTP")
                let target=try JSONDecoder().decode(SessionMetaItem.self,from:Data(#"{"session_id":"schedule_job"}"#.utf8))
                let result=await store.deleteSession(target)
                check(result.failedIDs==["schedule_job"] && deleteCalls.isEmpty,"schedule action HTTP participates in parent-session deletion admission")
                held("/toggle").respond(["status":true]);_ = try await action.value
            } else {
                _ = try? await store.mutateSchedule("job",mutation:.enabled(false))
                check(RecoveryTransport.matching("/toggle").isEmpty,"schedule write checks authoritative freshness and parent deletion at actual egress")
            }
            if let deleting {held("/chat/schedule_job").respond(["status":false]);_ = await deleting.value}
        } else if mode.hasPrefix("schedule") || mode.hasPrefix("runs") {
            if mode=="schedule-feed" || mode=="schedule-global-false" {
                await store.refreshGlobalSnapshot()
                check(RecoveryTransport.matching("/schedules").count==1 && store.schedulesFreshness == .fresh,"existing global snapshot refreshes authoritative schedules independently")
            } else if mode=="schedule-live-feed" {
                settings.baseURLString="https://replacement.invalid";store.applySettings()
                await wait({store.schedulesFreshness == .fresh && RecoveryTransport.matching("/api/v1/events").count==1},"existing global feed")
                completed=true
                sse(held("/api/v1/events"),["feed_seq":1,"session_id":"schedule_job","turn_id":"t_job","type":"turn_ended","data":["status":"success"]])
                await wait({store.schedules.first?.lastRun?.status=="success"},"authoritative finished schedule")
                check(store.schedules.first?.running==false,"feed doorbell refreshes terminal snapshot rather than guessing completion")
            } else if mode=="schedule-late" || mode=="runs-late" {
                if mode=="runs-late" {runHold=true} else {listHold=true}
                let old=Task {if mode=="runs-late" {await store.refreshScheduleRuns("job")} else {await store.refreshSchedules()}}
                await wait({!RecoveryTransport.held.isEmpty},"old request")
                let delayed=held(mode=="runs-late" ? "/runs" : "/schedules")
                completed=true;listHold=false;runHold=false
                if mode=="runs-late" {await store.refreshScheduleRuns("job")} else {await store.refreshSchedules()}
                delayed.respond(mode=="runs-late" ? ["status":true,"runs":[["run_id":"old","status":"running"]]] : ["status":true,"schedules":[["id":"old","running":true]]])
                await old.value
                check(mode=="runs-late" ? store.scheduleRunHistory["job"]?.first?.status=="success" : store.schedules.first?.id=="job" && store.schedules.first?.running==false,"old schedule reply cannot overwrite newer authoritative snapshot")
            } else if mode=="schedule-client" {
                listHold=true;let old=Task{await store.refreshSchedules()}
                await wait({RecoveryTransport.matching("/schedules").count==1},"old client list")
                let delayed=held("/schedules");listHold=false;completed=true
                settings.baseURLString="https://replacement.invalid";store.applySettings()
                await wait({store.schedules.first?.running==false},"new client schedules")
                delayed.respond(["status":true,"schedules":[["id":"old"]]]);await old.value
                check(store.schedules.first?.id=="job" && store.schedulesFreshness == .fresh,"old client cannot restore schedule cache")
            } else {
                let runs=mode.hasPrefix("runs")
                if mode=="schedule-unknown" {scheduleFails=true}
                if runs {await store.refreshScheduleRuns("job")} else {await store.refreshSchedules()}
                if mode=="schedule-unknown" {check(store.schedulesFreshness == .unknown && store.schedulesError != nil,"initial schedule failure remains unknown, never successful empty")} else {
                    scheduleFails=true
                    if runs {await store.refreshScheduleRuns("job")} else {await store.refreshSchedules()}
                    check(runs ? store.scheduleRunsFreshness["job"] == .stale && store.scheduleRunHistory["job"]?.first?.status=="running" : store.schedulesFreshness == .stale && store.schedules.first?.id=="job","schedule failure preserves last successful rows and marks stale")
                }
            }
        } else if mode.hasPrefix("doc") {
            let paths=["/fake/a","/fake/b","/fake/c"]
            if mode=="doc-legacy" || mode=="doc-zero" {refused=false}
            if mode=="doc-client" {
                let old=Task{await store.deleteKnowledgeDocuments(paths)}
                await wait({deleteCalls.count==1},"first document request")
                let delayed=held("/rag/document")
                settings.baseURLString="https://replacement.invalid";store.applySettings()
                delayed.respond(["status":true]);let result=await old.value
                check(result.obsolete && deleteCalls.count==1,"client replacement stops later document deletes and invalidates projection")
            } else {
                let result=await store.deleteKnowledgeDocuments(paths)
                let failed=(mode=="doc-false" || mode=="doc-http")
                check(result.failedIDs == (failed ? ["/fake/b"] : []) && result.deleted.count == (failed ? 2 : 3),"document delete requires receipt while legacy/zero-chunk success remains compatible")
                if failed {refused=false;let retry=await store.deleteKnowledgeDocuments(Array(result.failedIDs));check(retry.deleted==["/fake/b"] && deleteCalls==paths+["/fake/b"],"document retry only issues failed selected item")}
            }
        } else {
            store.select(sessionId:"A")
            await wait({RecoveryTransport.matching("/chat/A/events").count==1},"A actual attachment")
            if mode=="delete-new-intent" {
                // Isolated proof disables the ordinary deletion fence to exercise
                // the cleanup owner independently of its admission protection.
                let deletion=Task{await store.deleteSession(item)}
                await wait({deleteCalls.count==1},"delete captures original intent")
                store.send(prompt:"new local T2",uploads:[],thinking:false)
                await wait({RecoveryTransport.matching("/turns").count==1},"new actual local submission")
                let entries=UserDefaults.standard.dictionary(forKey:"macagent.outbox") as! [String:[String:String]]
                let key=entries["A"]!["key"]!
                held("/turns").respond(["status":true,"turn_id":"t_new","seq":5],status:202)
                await wait({!store.hasSubmissionInFlight("A")},"T2 HTTP acknowledged")
                let stream=held("/chat/A/events")
                sse(stream,["seq":5,"turn_id":"t_new","type":"turn_queued","data":["prompt":"new local T2","client_turn_id":key]])
                sse(stream,["seq":6,"turn_id":"t_new","type":"turn_ended","data":["status":"success","prompt":"new local T2","response":[["role":"assistant","content":"T2 winner"]],"turn_index":1,"turn_ref":"T2-ref"]])
                await wait({store.messages.contains{$0.text=="T2 winner"} && !store.isGenerating},"T2 settled without live receipt")
                held("/chat/A").respond(["status":true]);_ = await deletion.value
                check(store.selectedSessionId=="A" && store.messages.contains{$0.text=="T2 winner"},"new settled local submission intent keeps ownership after receipts disappear")
            } else if mode=="delete-live-terminal" {
                let stream=held("/chat/A/events")
                sse(stream,["seq":1,"turn_id":"t_live","type":"turn_queued","data":["prompt":"running input"]])
                sse(stream,["seq":2,"turn_id":"t_live","type":"turn_started","data":[:]])
                await wait({store.ledgerPhase=="running"},"original running T1")
                let deletion=Task{await store.deleteSession(item)}
                await wait({deleteCalls.count==1},"T1 delete held")
                sse(stream,["seq":3,"turn_id":"t_live","type":"turn_ended","data":["status":"cancelled","prompt":"running input","response":[],"turn_index":1,"turn_ref":"cancelled-ref"]])
                await wait({store.canonicalTurns.count==2 && store.ledgerPhase=="none"},"same original terminal")
                held("/chat/A").respond(["status":true]);_ = await deletion.value
                check(store.selectedSessionId==nil && store.messages.isEmpty && store.canonicalTurns.isEmpty,"confirmed delete after original cancelled terminal clears selected session")
            } else if mode=="delete-history-progress" {
                store.refreshDisplayedHistory()
                await wait({histories==2},"ordinary history read held")
                let historyReply=held("/history")
                let deletion=Task{await store.deleteSession(item)}
                await wait({deleteCalls.count==1},"delete before ordinary projection")
                historyReply.respond(["status":true,"session_id":"A","turns":[["status":"success","prompt":"new history","response":[]],["status":"cancelled","prompt":"settled history","response":[]]],"last_turn_ref":"changed-history-ref","replay_after":0])
                await wait({store.canonicalTurns.count==2},"ordinary authoritative projection advanced")
                held("/chat/A").respond(["status":true]);_ = await deletion.value
                check(store.selectedSessionId==nil && store.messages.isEmpty,"ordinary history projection does not become new deletion owner")
            } else if mode=="egress-fence" {
                store.send(prompt:"queued before deletion",uploads:[],thinking:false)
                await wait({ManagementSubmissionGate.pending != nil},"queued actual submit")
                let deletion=Task{await store.deleteSession(item)}
                await wait({deleteCalls.count==1},"deliberately disabled admission exposes egress layer")
                ManagementSubmissionGate.release()
                await wait({!store.hasSubmissionInFlight("A") || !RecoveryTransport.matching("/turns").isEmpty},"resumed actual submit outcome")
                check(RecoveryTransport.matching("/turns").isEmpty,"queued submit rechecks deletion before real POST")
                held("/chat/A").respond(["status":false]);_ = await deletion.value
            } else if mode=="submit-two-aba" {
                store.send(prompt:"first pending",uploads:[],thinking:false)
                await wait({RecoveryTransport.matching("/turns").count==1},"first HTTP")
                let first=held("/turns")
                store.select(sessionId:"B");await wait({RecoveryTransport.matching("/chat/B/events").count==1},"B window")
                store.select(sessionId:"A");await wait({RecoveryTransport.matching("/chat/A/events").count==2},"A new window")
                store.send(prompt:"second pending",uploads:[],thinking:false)
                await wait({RecoveryTransport.matching("/turns").count==2},"second HTTP")
                held("/turns").respond(["reason":"synthetic refusal"],status:500)
                await wait({!store.isGenerating},"second HTTP returned")
                let denied=await store.deleteSession(item)
                check(denied.failedIDs==["A"] && deleteCalls.isEmpty && store.hasSubmissionInFlight("A"),"one settled HTTP cannot hide another in-flight receipt after ABA")
                first.respond(["reason":"synthetic refusal"],status:500)
                await wait({!store.hasSubmissionInFlight("A")},"all actual HTTP completed")
            } else if mode=="delete-absent" {
                let result=await store.deleteSession(item)
                check(result.deleted==["A"] && result.failedIDs.isEmpty && store.selectedSessionId==nil,"explicit shared not_found receipt satisfies local deletion target")
            } else if mode=="delete-partial" {
                let result=await store.deleteSessions(items)
                check(result.deleted==["A","C"] && result.failedIDs==["B"] && result.failures["B"]=="synthetic refusal","batch confirms each item and retains exact failed subset")
                refused=false;let retry=await store.deleteSessions(items.filter{result.failedIDs.contains($0.sessionId)})
                check(retry.deleted==["B"] && deleteCalls==["A","B","C","B"],"batch retry sends only failed selected session")
            } else if mode=="submit-inflight" || mode=="submit-unknown" || mode=="retry-inflight" {
                if mode=="retry-inflight" {store.requestRetryConfirmation();store.confirmRetry(store.retryConfirmationTarget!)} else {store.send(prompt:"pending",uploads:[],thinking:false)}
                let suffix=mode=="retry-inflight" ? "/retry" : "/turns"
                await wait({RecoveryTransport.matching(suffix).count==1},"actual submission HTTP")
                if mode=="submit-unknown" {
                    held(suffix).respond(["reason":"synthetic timeout"],status:500)
                    await wait({!store.hasSubmissionInFlight("A") && !store.isGenerating},"HTTP returned but receipt unknown")
                    let result=await store.deleteSession(item)
                    check(result.deleted==["A"] && deleteCalls==["A"],"persisted unknown receipt is not an in-flight HTTP and cannot permanently prohibit deletion")
                } else {
                    let result=await store.deleteSession(item)
                    check(result.deleted.isEmpty && result.failedIDs==["A"] && deleteCalls.isEmpty,"active submission HTTP prohibits racing session deletion")
                    check(store.hasSubmissionInFlight("A"),"deletion refusal does not clear actual HTTP ownership")
                }
            } else if ["delete-inflight","delete-aba","delete-other-submit","delete-client-cleanup","delete-writes","delete-retry-confirm","delete-paused"].contains(mode) {
                var retryTarget:RetryConfirmationTarget?
                if mode=="delete-retry-confirm" {store.requestRetryConfirmation();retryTarget=store.retryConfirmationTarget}
                if mode=="delete-paused" {
                    let stream=held("/chat/A/events")
                    sse(stream,["seq":1,"turn_id":"paused","type":"turn_queued","data":["prompt":"paused raw"]])
                    sse(stream,["seq":2,"turn_id":"paused","type":"awaiting_approval","data":["pending_tools":[["name":"write_file","arguments":[:]]]]])
                    await wait({store.pendingApprovalTarget != nil},"paused approval")
                }
                let old=Task{await store.deleteSession(item)}
                await wait({deleteCalls.count==1},"delete at transport")
                let delayed=held("/chat/A")
                if mode=="delete-aba" || mode=="delete-other-submit" {
                    store.select(sessionId:"B");await wait({RecoveryTransport.matching("/chat/B/events").count==1},"B actual attachment")
                    if mode=="delete-aba" {store.select(sessionId:"A");await wait({RecoveryTransport.matching("/chat/A/events").count==2},"A new incarnation")}
                    else {store.send(prompt:"B pending",uploads:[],thinking:false);await wait({RecoveryTransport.matching("/turns").count==1},"other session submission")}
                    let texts=store.messages.map(\.text),outbox=UserDefaults.standard.dictionary(forKey:"macagent.outbox")
                    delayed.respond(["status":true]);_ = await old.value
                    check(store.selectedSessionId == (mode=="delete-aba" ? "A" : "B") && store.messages.map(\.text)==texts,"late delete receipt cannot detach new selection/ABA")
                    check((outbox as NSDictionary?)?.isEqual(to:UserDefaults.standard.dictionary(forKey:"macagent.outbox") ?? [:]) ?? (outbox==nil),"late delete of another session preserves new outbox receipt")
                } else if mode=="delete-client-cleanup" {
                    // The replacement backend's own cache never listed A, so it starts
                    // with nothing selected; the new incarnation is opened explicitly.
                    settings.baseURLString="https://replacement.invalid";store.applySettings();store.select(sessionId:"A")
                    await wait({store.messages.contains{$0.text=="keep winner A"}},"replacement client")
                    let newer=Task{await store.deleteSession(item)}
                    await wait({deleteCalls.count==2},"new deletion ownership")
                    delayed.respond(["status":true]);let oldResult=await old.value
                    check(oldResult.obsolete && store.isSessionDeleting("A"),"old delete cleanup cannot clear newer client deletion fence")
                    held("/chat/A").respond(["status":false]);_ = await newer.value
                } else {
                    store.send(prompt:"must block",uploads:[],thinking:false)
                    store.requestRetryConfirmation();store.editLastTurn();store.deleteLastTurn();store.branch(fromTurnIndex:0);store.resumeTask()
                    if let retryTarget {store.confirmRetry(retryTarget)}
                    if let target=store.pendingApprovalTarget {store.decideApproval(approve:true,target:target)}
                    try? await Task.sleep(nanoseconds:80_000_000)
                    check(RecoveryTransport.matching("/turns").isEmpty && RecoveryTransport.matching("/retry").isEmpty && RecoveryTransport.matching("/turns/last").isEmpty && RecoveryTransport.matching("/branch").isEmpty && RecoveryTransport.matching("/resume").isEmpty && RecoveryTransport.matching("/approval").isEmpty,"deleting target blocks all task writes through shared predicate")
                    delayed.respond(["status":false]);_ = await old.value
                }
            } else if mode != "ui" {
                _ = await store.deleteSession(item)
                try? await Task.sleep(nanoseconds:80_000_000)
                check(store.selectedSessionId=="A" && store.messages.contains{$0.text=="keep winner A"},"unconfirmed deletion preserves active session and displayed winner")
            }
        }
        if mode=="ui" {
            let src=URL(fileURLWithPath:root).appendingPathComponent("apple-app/Sources")
            let sessions=try String(contentsOf:src.appendingPathComponent("Views/SessionListView.swift"),encoding:.utf8)
            let knowledge=try String(contentsOf:src.appendingPathComponent("Views/KnowledgePanelView.swift"),encoding:.utf8)
            check(sessions.contains("multiSelection = remaining") && sessions.contains("multiSelection.subtracting(result.deleted)") && !sessions.contains("items.forEach"),"session UI awaits confirmed selection projection")
            let schedules=try String(contentsOf:src.appendingPathComponent("Views/SchedulesView.swift"),encoding:.utf8)
            check(schedules.components(separatedBy:"!store.canMutateSchedule(task.id)").count==5 && schedules.contains("try await store.mutateSchedule(id, mutation: mutation)"),"all schedule write UI controls and actual egress use the same Store policy")
            check(knowledge.contains("multiSelection.subtract(result.deleted)") && knowledge.contains("await store.deleteKnowledgeDocuments(paths)"),"knowledge UI retains failed selections via actual common receipt result")
            for filename in ["ChatView.swift","ComposerView.swift","MessageView.swift"] {
                let text=try String(contentsOf:src.appendingPathComponent("Views/"+filename),encoding:.utf8)
                check(text.contains("selectedSessionIsDeleting"),"task UI uses actual shared deletion eligibility")
            }
        }
        print("PASS "+mode);exit(0)
    }
}
