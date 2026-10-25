import Foundation

@main struct OfflineHistoryTests {
    @MainActor static func wait(_ condition: () -> Bool, _ message: String) async {
        for _ in 0..<600 { if condition() { return }; try? await Task.sleep(nanoseconds: 5_000_000) }
        check(false, "timeout " + message)
    }
    static func item(_ id: String, updated: Double = 1) -> SessionMetaItem {
        SessionMetaItem(sessionId: id, title: "会话 " + id, turnCount: 1, createdAt: 1,
            lastUpdated: updated, pinned: false, active: false)
    }
    static func history(_ id: String, answer: String? = nil) -> HistoryResponse {
        try! JSONDecoder().decode(HistoryResponse.self, from: JSONSerialization.data(withJSONObject: [
            "status": true, "session_id": id, "replay_after": 12, "generation": "synthetic-generation",
            "last_turn_ref": "ref-" + id, "display_turn_indices": [0], "retry_groups": [],
            "turns": [["status": "success", "prompt": "prompt-" + id,
                       "response": [["role": "assistant", "content": answer ?? "answer-" + id]],
                       "turn_ref": "ref-" + id, "execution_receipts": ["items": [], "truncated": false]]]
        ]))
    }
    @MainActor static func main() async throws {
        check(Bundle.main.bundleIdentifier?.hasPrefix("com.agent.codex.native-tests.") == true, "isolated test preference domain required")
        let mode = CommandLine.arguments[1]
        let root = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        let client = AgentClient(baseURL: URL(string: "https://fixture.invalid")!, apiKey: "synthetic", cacheRoot: root)
        let namespace = client.cacheNamespace, cache = OfflineHistoryStore(root: root)
        let a = item("A"), b = item("B")
        if mode == "write" {
            try await cache.saveIndex([a,b], retentionPolicy: nil, namespace: namespace)
            try await cache.saveHistory(history("A"), metadata: a, namespace: namespace)
            try await cache.saveHistory(history("B"), metadata: b, namespace: namespace)
            try await cache.saveSelection("A", namespace: namespace)
            print("PASS process write"); return
        }
        if mode == "draft-selection" {
            try await cache.saveIndex([a], retentionPolicy:nil, namespace:namespace)
            try await cache.saveSelection("B", namespace:namespace)
            try await cache.saveIndex([a], retentionPolicy:nil, namespace:namespace)
            var index = await cache.loadIndex(namespace:namespace)
            check(index.selectedSessionId == "B", "unlisted draft selection survives index refresh")
            try await cache.saveIndex([a,b], retentionPolicy:nil, namespace:namespace)
            try await cache.saveHistory(history("B"), metadata:b, namespace:namespace)
            let restarted = OfflineHistoryStore(root:root)
            index = await restarted.loadIndex(namespace:namespace)
            check(index.selectedSessionId == "B" && index.sessions.contains { $0.sessionId == "B" },
                  "newly persisted session remains selected on restart")
            let body = await restarted.loadHistory(sessionId:"B", namespace:namespace)
            check(body != nil, "newly selected body remains readable offline")
            try await cache.saveIndex([a], retentionPolicy:nil, namespace:namespace)
            index = await restarted.loadIndex(namespace:namespace)
            check(index.selectedSessionId == nil, "confirmed removal clears known selection")
            print("PASS draft selection promotion and confirmed removal"); return
        }
        if mode == "identity" {
            let one = OfflineHistoryStore.namespace(baseURL: URL(string:"https://fixture.invalid")!, apiKey:"one")
            let two = OfflineHistoryStore.namespace(baseURL: URL(string:"https://fixture.invalid")!, apiKey:"two")
            let other = OfflineHistoryStore.namespace(baseURL: URL(string:"https://other.invalid")!, apiKey:"one")
            check(one != two && one != other, "URL and key both fence namespaces")
            try await cache.saveHistory(history("A"), metadata: nil, namespace: one)
            let absent = await cache.loadHistory(sessionId:"A", namespace:two)
            check(absent == nil, "same sid on another credential is isolated")
            print("PASS identity"); return
        }
        if mode == "cleanup" {
            try await cache.saveIndex([a,b], retentionPolicy:nil, namespace:namespace)
            try await cache.saveHistory(history("A"), metadata:a, namespace:namespace)
            try await cache.saveHistory(history("B"), metadata:b, namespace:namespace)
            try await cache.saveIndex([], retentionPolicy:nil, namespace:namespace)
            let absent = await cache.loadHistory(sessionId:"A", namespace:namespace)
            check(absent == nil, "authoritative empty list removes body")
            let saved = try await cache.saveHistory(history("A"), metadata:a, namespace:namespace, requireListed:true)
            check(!saved, "late background response cannot resurrect removed history")
            print("PASS cleanup"); return
        }
        if mode == "index-actor" {
            try await cache.saveIndex([a,b], retentionPolicy:nil, namespace:namespace)
            try await cache.saveHistory(history("B"), metadata:b, namespace:namespace)
            let fence = OfflineSessionIndexFence(), old = fence.advance()
            fence.advance()
            let saved = try await cache.saveIndex([a], retentionPolicy:nil, namespace:namespace,
                isCurrent:{ fence.matches(old) })
            let retained = await cache.loadHistory(sessionId:"B", namespace:namespace)
            check(!saved && retained != nil, "obsolete actor index write cannot delete newer body")
            print("PASS actor index fence"); return
        }
        if mode.hasPrefix("cached-startup-") || mode == "uncached-startup-offline" {
            let cached = mode != "uncached-startup-offline"
            if cached {
                try await cache.saveIndex([a], retentionPolicy:nil, namespace:namespace)
                try await cache.saveHistory(history("A"), metadata:a, namespace:namespace)
                try await cache.saveSelection("A", namespace:namespace)
            }
            RecoveryTransport.handler = { t in
                if mode == "cached-startup-auth401" { t.respond(["detail":"synthetic authorization refusal"],status:401) }
                else if mode == "cached-startup-auth403" { t.respond(["detail":"synthetic forbidden"],status:403) }
                else if mode == "cached-startup-http500" { t.respond(["detail":"synthetic unknown server failure"],status:500) }
                else {
                    let code: URLError.Code = mode == "cached-startup-cancelled" ? .cancelled
                        : mode == "cached-startup-unknown" ? .badServerResponse : .cannotConnectToHost
                    t.client?.urlProtocol(t,didFailWithError:URLError(code))
                }
            }
            let store = AgentStore(settings:AppSettings(), offlineCacheRoot:root)
            await store.restoreLocalHistory()
            if cached { await wait({ store.messages.contains { $0.text == "answer-A" } }, "offline startup body") }
            await store.refreshConnection()
            await store.loadSessions()
            let quiet = mode == "cached-startup-offline" || mode == "cached-startup-cancelled"
            if quiet {
                check(store.errorMessage == nil, "cached network failure stays quiet")
                check(store.historyIsReadOnly && store.connectionOK == false && store.offlineHistoryNotice != nil, "cached startup retains offline status")
            } else {
                check(store.errorMessage != nil, "authentication and unknown errors remain visible")
                if mode.contains("auth401") { check(store.errorMessage!.contains("401"), "401 remains an authorization error") }
                if mode.contains("auth403") { check(store.errorMessage!.contains("403"), "403 remains an authorization error") }
            }
            if cached {
                let retained = await cache.loadHistory(sessionId:"A",namespace:namespace)
                check(store.messages.contains { $0.text == "answer-A" } && retained != nil, "startup failure retains saved body")
            }
            print("PASS " + mode); return
        }
        if mode == "readonly-transient" {
            // A health probe that answers while the index probe times out is a
            // transient failure. A live attached session keeps Stop / Suspend /
            // Approve; only a cache-only projection stays a reading projection.
            try await cache.saveIndex([a],retentionPolicy:nil,namespace:namespace)
            try await cache.saveHistory(history("A"),metadata:a,namespace:namespace)
            try await cache.saveSelection("A",namespace:namespace)
            var health = true, probe = true, live = true
            RecoveryTransport.handler = { t in
                let path = t.request.url!.path
                if path == "/health" {
                    if health { t.respond(["status":true]) }
                    else { t.client?.urlProtocol(t,didFailWithError:URLError(.cannotConnectToHost)) }
                    return
                }
                if path.hasSuffix("/sessions") {
                    if probe { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(SessionListResponse(status:true,sessions:[a])))) }
                    else { t.client?.urlProtocol(t,didFailWithError:URLError(.timedOut)) }
                    return
                }
                guard live else { t.client?.urlProtocol(t,didFailWithError:URLError(.cannotConnectToHost)); return }
                if path.hasSuffix("/history") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(history("A")))) }
                else if path.hasSuffix("/turn") { t.respond(["status":true,"turn":NSNull(),"last_seq":12]) }
                else if path.hasSuffix("/events") { RecoveryTransport.hold(t);t.deliver(Data()) }
                else if path.hasSuffix("/deck") { t.respond([:],status:404) }
                else if path.hasSuffix("/offline-files") { t.respond(["status":true,"session_id":"A","files":[]]) }
                else { t.respond(["status":true,"skills":[]]) }
            }
            let store = AgentStore(settings:AppSettings(),offlineCacheRoot:root)
            await store.restoreLocalHistory()
            await wait({ RecoveryTransport.matching("/chat/A/events").count == 1 && !store.historyIsReadOnly },"attached live session before the probe fails")
            probe = false
            await store.refreshConnection()
            check(store.connState == .reachable && !store.historyIsReadOnly,"a timed out index probe keeps the attached session writable")
            health = false
            await store.refreshConnection()
            check(store.connectionOK == false && store.connState == .connecting && !store.historyIsReadOnly,
                  "a single failed health probe keeps the attached turn writable")
            for _ in 0..<9 { await store.refreshConnection() }
            check(store.connState == .offline && store.historyIsReadOnly,
                  "the settled offline verdict turns the attached session into reading")
            health = true;probe = true
            await store.refreshConnection()
            check(store.connectionOK == true && !store.historyIsReadOnly,"authentication restores authority for the attached session")
            store.newSession()
            live = false
            let restarted = AgentStore(settings:AppSettings(),offlineCacheRoot:root)
            await restarted.restoreLocalHistory()
            await wait({ restarted.messages.contains { $0.text == "answer-A" } && restarted.historyIsReadOnly },"cached projection reads offline")
            await restarted.refreshConnection()
            check(restarted.connectionOK == true && restarted.historyIsReadOnly,"authentication alone cannot grant authority to a cached projection")
            restarted.newSession()
            print("PASS transient probe keeps live authority");return
        }
        if mode == "asset-mark-race" {
            // An eviction reported while A's own pass is still running must leave A
            // partial, even on a first-ever mirror that has no record yet.
            try await cache.saveIndex([a,b],retentionPolicy:nil,namespace:namespace)
            let token = await cache.beginAssetSync(sessionId:"A",namespace:namespace)
            try await cache.markAssetsEvicted(["A"],namespace:namespace)
            var partial = await cache.partialAssetSessions(namespace:namespace)
            check(partial == ["A"],"a session without a record can still lose its complete status")
            try await cache.recordAssetSync(a,namespace:namespace,complete:true,token:token)
            partial = await cache.partialAssetSessions(namespace:namespace)
            let needs = await cache.needsAssetSync(a,namespace:namespace)
            check(partial == ["A"] && !needs,"a pass cannot report complete after its bytes were evicted")
            // The next pass, started after the mark, may report complete again.
            let next = await cache.beginAssetSync(sessionId:"A",namespace:namespace)
            try await cache.recordAssetSync(a,namespace:namespace,complete:true,token:next)
            partial = await cache.partialAssetSessions(namespace:namespace)
            check(partial.isEmpty,"a later full pass clears the mark")
            print("PASS eviction mark beats a concurrent complete");return
        }
        if mode == "mirror-serial" {
            // Attachment mirroring runs one session at a time: a second manifest is
            // only requested once the previous session's pass is done.
            let items = (0..<3).map { item("M\($0)", updated: Double($0 + 1)) }
            RecoveryTransport.handler = { t in
                let path = t.request.url!.path
                let parts = path.split(separator: "/").map(String.init)
                if path.hasSuffix("/sessions") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(SessionListResponse(status:true,sessions:items)))) }
                else if path.hasSuffix("/offline-files") { RecoveryTransport.hold(t) }
                else if path.hasSuffix("/history") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(history(parts[parts.count - 2])))) }
                else if path.hasSuffix("/turn") { t.respond(["status":true,"turn":NSNull(),"last_seq":12]) }
                else if path.hasSuffix("/events") { RecoveryTransport.hold(t);t.deliver(Data()) }
                else if path.hasSuffix("/deck") { t.respond([:],status:404) }
                else { t.respond(["status":true,"skills":[]]) }
            }
            let store = AgentStore(settings:AppSettings(),offlineCacheRoot:root)
            await store.loadSessions()
            for expected in 1...3 {
                await wait({ RecoveryTransport.matching("/offline-files").count == expected },"manifest \(expected) requested")
                try? await Task.sleep(nanoseconds:120_000_000)
                check(RecoveryTransport.matching("/offline-files").count == expected,
                      "only one session mirrors at a time")
                let held = RecoveryTransport.held.first { $0.request.url!.path.hasSuffix("/offline-files") }!
                RecoveryTransport.lock.lock()
                RecoveryTransport.held.removeAll { $0 === held }
                RecoveryTransport.lock.unlock()
                let id = held.request.url!.path.split(separator:"/").dropLast().last.map(String.init)!
                held.respond(["status":true,"session_id":id,"files":[]])
            }
            await wait({ !store.offlineSyncInProgress },"serial mirroring finished")
            check(store.offlineAttachmentsComplete,"every session mirrored exactly once")
            store.newSession()
            print("PASS serial attachment mirroring");return
        }
        if mode == "mirror-resume" {
            // A failure late in the first pass must not make the next pass download
            // the files that already landed.
            var failSecond = true
            RecoveryTransport.handler = { t in
                let path = t.request.url!.path
                if path.hasSuffix("/sessions") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(SessionListResponse(status:true,sessions:[a])))) }
                else if path.hasSuffix("/offline-files") {
                    t.respond(["status":true,"session_id":"A","files":[
                        ["path":"/api/v1/files/A/assets/one.bin","name":"one.bin","kind":"assets","size":16,"revision":"1"],
                        ["path":"/api/v1/files/A/assets/two.bin","name":"two.bin","kind":"assets","size":16,"revision":"1"]]])
                }
                else if path.hasSuffix("/one.bin") { t.deliver(Data(repeating:1,count:16),status:200,type:"application/octet-stream",finish:true) }
                else if path.hasSuffix("/two.bin") {
                    if failSecond { t.respond(["detail":"synthetic transfer failure"],status:500) }
                    else { t.deliver(Data(repeating:2,count:16),status:200,type:"application/octet-stream",finish:true) }
                }
                else if path.hasSuffix("/history") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(history("A")))) }
                else if path.hasSuffix("/turn") { t.respond(["status":true,"turn":NSNull(),"last_seq":12]) }
                else if path.hasSuffix("/events") { RecoveryTransport.hold(t);t.deliver(Data()) }
                else if path.hasSuffix("/deck") { t.respond([:],status:404) }
                else { t.respond(["status":true,"skills":[]]) }
            }
            let store = AgentStore(settings:AppSettings(),offlineCacheRoot:root)
            let api = store.client!
            await store.loadSessions()
            await wait({ RecoveryTransport.matching("/two.bin").count == 1 && !store.offlineSyncInProgress },"first pass failed on the second file")
            let first = await api.offlineAssets.data(namespace:namespace,sid:"A",key:"/api/v1/files/A/assets/one.bin")
            check(RecoveryTransport.matching("/one.bin").count == 1 && first != nil,"the first file landed before the failure")
            check(!store.offlineAttachmentsComplete,"a failed pass is never reported complete")
            failSecond = false
            await store.loadSessions()
            await wait({ RecoveryTransport.matching("/two.bin").count == 2 && !store.offlineSyncInProgress },"second pass retried the failed file")
            check(RecoveryTransport.matching("/one.bin").count == 1,"an already installed file is not downloaded again")
            check(store.offlineAttachmentsComplete && store.offlineHistoryNotice == "已保存到本地","the resumed pass completes the mirror")
            store.newSession()
            print("PASS mirroring resumes after a late failure");return
        }
        if mode == "mirror-budget" {
            // One mirrored file fits at a time. Making room for B costs A its copy,
            // an over-cap file is never mirrored, and neither situation may be
            // reported as fully saved — nor re-tried every 15 seconds.
            let bytes = 4096
            var listed = [item("A", updated: 1)]
            let sizes = ["A": bytes, "B": bytes, "C": 99_999]
            RecoveryTransport.handler = { t in
                let path = t.request.url!.path
                let parts = path.split(separator: "/").map(String.init)
                if path.hasSuffix("/sessions") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(SessionListResponse(status:true,sessions:listed)))) }
                else if path.hasSuffix("/offline-files") {
                    let id = parts[parts.count - 2]
                    t.respond(["status":true,"session_id":id,"files":[["path":"/api/v1/files/\(id)/assets/report.bin",
                        "name":"report.bin","kind":"assets","size":sizes[id] ?? bytes,"revision":"1"]]])
                }
                else if path.hasSuffix("/report.bin") {
                    let id = parts[3]
                    t.deliver(Data(repeating:7,count:sizes[id] ?? bytes),status:200,type:"application/octet-stream",finish:true)
                }
                else if path.hasSuffix("/history") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(history(parts[parts.count - 2])))) }
                else if path.hasSuffix("/turn") { t.respond(["status":true,"turn":NSNull(),"last_seq":12]) }
                else if path.hasSuffix("/events") { RecoveryTransport.hold(t);t.deliver(Data()) }
                else if path.hasSuffix("/deck") { t.respond([:],status:404) }
                else { t.respond(["status":true,"skills":[]]) }
            }
            let settings = AppSettings()
            settings.mirrorLimits = .init(perFile: Int64(bytes), total: 6000)
            let store = AgentStore(settings:settings, offlineCacheRoot:root)
            let api = store.client!
            func mirrored(_ id: String) async -> Bool {
                await api.offlineAssets.data(namespace:namespace,sid:id,key:"/api/v1/files/\(id)/assets/report.bin") != nil
            }
            await store.loadSessions()
            await wait({ !store.offlineSyncInProgress && store.offlineSavedSessionIDs.contains("A") },"A mirrored")
            var partial = await cache.partialAssetSessions(namespace:namespace)
            var hasA = await mirrored("A")
            check(partial.isEmpty && hasA && store.offlineAttachmentsComplete && store.offlineHistoryNotice == "已保存到本地",
                  "a session inside the budget is saved and reported complete")
            listed = [item("A", updated: 1), item("B", updated: 2)]
            await store.loadSessions()
            await wait({ !store.offlineSyncInProgress && store.offlineSavedSessionIDs.count == 2 },"B mirrored")
            partial = await cache.partialAssetSessions(namespace:namespace)
            hasA = await mirrored("A")
            let hasB = await mirrored("B")
            check(partial == ["A"] && !hasA && hasB,"making room for B costs A its mirrored copy and its complete status")
            check(!store.offlineAttachmentsComplete && store.offlineHistoryNotice == "正文已保存 · 附件同步未完成",
                  "an evicted session is never advertised as fully saved")
            listed.append(item("C", updated: 3))
            await store.loadSessions()
            await wait({ !store.offlineSyncInProgress && store.offlineSavedSessionIDs.count == 3 },"C listed")
            partial = await cache.partialAssetSessions(namespace:namespace)
            let hasC = await mirrored("C")
            check(partial == ["A","C"] && !hasC,"a file over the per-file cap is not counted as mirrored")
            let listedC = await api.cachedSessionFiles(sessionId:"C")
            check(listedC?.count == 1,"the skipped output stays listed for on-demand download")
            let manifests = RecoveryTransport.matching("/offline-files").count
            check(manifests == 3,"each session was listed exactly once")
            await store.loadSessions()
            try? await Task.sleep(nanoseconds:250_000_000)
            check(RecoveryTransport.matching("/offline-files").count == manifests,
                  "a budget-limited mirror is not retried until the index changes")
            check(!store.offlineSyncInProgress && !store.offlineAttachmentsComplete,
                  "an idle pass keeps reporting the incomplete mirror without flapping")
            // Deleting B frees room: the mirrors the budget cut short retry.
            listed = [item("A", updated: 1), item("C", updated: 3)]
            await store.loadSessions()
            await wait({ RecoveryTransport.matching("/offline-files").count > manifests && !store.offlineSyncInProgress },"freed space retries A")
            hasA = await mirrored("A")
            partial = await cache.partialAssetSessions(namespace:namespace)
            check(hasA && partial == ["C"],"reclaimed space restores the evicted mirror")
            store.newSession()
            print("PASS mirroring budget accounting");return
        }
        if mode == "bounded-index" {
            // Removal fences cannot grow forever. The newest removals keep theirs,
            // which is what an in-flight response could still try to resurrect.
            let first = (0..<600).map { item("T\($0)") }
            let kept = Array(first.prefix(300))
            try await cache.saveIndex(first, retentionPolicy:nil, namespace:namespace)
            try await cache.saveHistory(history("T0"), metadata:first[0], namespace:namespace)
            try await cache.saveIndex(kept, retentionPolicy:nil, namespace:namespace)
            try await cache.saveIndex([], retentionPolicy:nil, namespace:namespace)
            let index = await cache.loadIndex(namespace:namespace)
            check(index.tombstones?.count == 500 && (index.epochs?.count ?? 0) <= 500,
                  "removal fences stay bounded")
            check(Set(index.tombstones ?? []).isSuperset(of: kept.map(\.sessionId)),
                  "the newest removals keep their fence")
            let resurrected = try await cache.saveHistory(history("T0"), metadata:first[0], namespace:namespace, expectedEpoch:0)
            check(!resurrected, "a retained fence still blocks a pre-deletion response")
            print("PASS bounded removal fences");return
        }
        if mode == "assets-recovery" {
            // A pass that could not mirror attachments says so, is retried on the
            // next index refresh, and a later complete pass reports complete again.
            var manifestFails = true
            RecoveryTransport.handler = { t in
                let path = t.request.url!.path
                if path.hasSuffix("/offline-files") {
                    if manifestFails { t.respond(["detail":"synthetic manifest failure"],status:500) }
                    else { t.respond(["status":true,"session_id":"A","files":[]]) }
                } else if path.hasSuffix("/sessions") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(SessionListResponse(status:true,sessions:[a])))) }
                else if path.hasSuffix("/history") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(history("A")))) }
                else if path.hasSuffix("/turn") { t.respond(["status":true,"turn":NSNull(),"last_seq":12]) }
                else if path.hasSuffix("/events") { RecoveryTransport.hold(t);t.deliver(Data()) }
                else if path.hasSuffix("/deck") { t.respond([:],status:404) }
                else { t.respond(["status":true,"skills":[]]) }
            }
            let store = AgentStore(settings:AppSettings(),offlineCacheRoot:root)
            await store.loadSessions()
            await wait({ RecoveryTransport.matching("/offline-files").count == 1 && !store.offlineSyncInProgress && store.offlineSavedSessionIDs.contains("A") },"first pass settled")
            check(!store.offlineAttachmentsComplete && store.offlineHistoryNotice == "正文已保存 · 附件同步未完成","a failed manifest reports attachments incomplete")
            manifestFails = false
            await store.loadSessions()
            await wait({ store.offlineAttachmentsComplete && !store.offlineSyncInProgress },"a later complete pass reports complete")
            check(RecoveryTransport.matching("/offline-files").count == 2,"a failed attachment pass is retried on the next refresh")
            check(RecoveryTransport.matching("/chat/A/history").count == 1,"an unchanged body is not downloaded again")
            check(store.offlineHistoryNotice == "已保存到本地","a complete pass clears the attachment notice")
            store.newSession()
            print("PASS attachment pass recovery");return
        }
        if mode == "search-version" {
            // The saved body belongs to a superseded index version: search must read
            // the server instead of quoting text that may already be deleted.
            let a2 = SessionMetaItem(sessionId:"A",title:"A2",turnCount:2,createdAt:2,lastUpdated:2,pinned:false,active:false)
            try await cache.saveIndex([a],retentionPolicy:nil,namespace:namespace)
            try await cache.saveHistory(history("A",answer:"superseded-secret"),metadata:a,namespace:namespace)
            try await cache.saveIndex([a2],retentionPolicy:nil,namespace:namespace)
            var served = 0
            RecoveryTransport.handler = { t in
                let path = t.request.url!.path
                if path.hasSuffix("/history") {
                    served += 1
                    t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(history("A",answer:"current-answer"))))
                } else if path.hasSuffix("/sessions") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(SessionListResponse(status:true,sessions:[a2])))) }
                else if path.hasSuffix("/turn") { t.respond(["status":true,"turn":NSNull(),"last_seq":12]) }
                else if path.hasSuffix("/events") { RecoveryTransport.hold(t);t.deliver(Data()) }
                else if path.hasSuffix("/deck") { t.respond([:],status:404) }
                else if path.hasSuffix("/offline-files") { t.respond(["status":true,"session_id":"A","files":[]]) }
                else { t.respond(["status":true,"skills":[]]) }
            }
            let store = AgentStore(settings:AppSettings(),offlineCacheRoot:root)
            await store.restoreLocalHistory()
            check(store.sessions.map(\.sessionId) == ["A"],"local index provides the search targets")
            store.searchQuery = "superseded-secret"
            await store.runSearch()
            check(store.searchResults?.isEmpty == true && served > 0,"search cannot quote a superseded cached body")
            store.searchQuery = "current-answer"
            await store.runSearch()
            check(store.searchResults?.count == 1,"search falls back to the network for the current version")
            store.newSession()
            print("PASS search version fence");return
        }
        if mode == "asset-path" {
            // Traversal is a path segment, not a substring: a dotted file name is
            // an ordinary name and must stay reachable.
            let dotted = await client.assetPlaybackSource(path:"/api/v1/files/A/assets/report..final.pdf")
            check(dotted?.url.absoluteString.hasSuffix("/api/v1/files/A/assets/report..final.pdf") == true,
                  "a dotted file name is not traversal")
            let escaped = await client.assetPlaybackSource(path:"/api/v1/files/A/../B/assets/report.pdf")
            let encoded = await client.assetPlaybackSource(path:"/api/v1/files/A/%2e%2e/B/assets/report.pdf")
            check(escaped?.url == nil && encoded?.url == nil,"a traversal segment is rejected")
            print("PASS asset path segments");return
        }
        if mode == "history-sync-status" {
            let a3 = SessionMetaItem(sessionId:"A",title:"v3",turnCount:2,createdAt:1,lastUpdated:3,pinned:false,active:false)
            let a4 = SessionMetaItem(sessionId:"A",title:"v4",turnCount:3,createdAt:1,lastUpdated:4,pinned:false,active:false)
            var second = history("A",answer:"verified-v3").turns[0];second.turnRef = "ref-v3"
            var third = history("A",answer:"unverified-v4").turns[0];third.turnRef = "ref-v4"
            let v3 = HistoryResponse(status:true,sessionId:"A",turns:[history("A").turns[0],second],replayAfter:16,generation:"g",lastTurnRef:"ref-v3")
            let v4 = HistoryResponse(status:true,sessionId:"A",turns:v3.turns+[third],replayAfter:17,generation:"g",lastTurnRef:"ref-v4")
            try await cache.saveIndex([a3],retentionPolicy:nil,namespace:namespace)
            try await cache.saveHistory(v3,metadata:a3,namespace:namespace)
            try await cache.saveSelection("A",namespace:namespace)
            var current = a3, historyFailure = false, offline = false
            RecoveryTransport.handler = { t in
                let path = t.request.url!.path
                if offline { t.client?.urlProtocol(t,didFailWithError:URLError(.cannotConnectToHost));return }
                if path.hasSuffix("/history") {
                    if historyFailure { t.client?.urlProtocol(t,didFailWithError:URLError(.networkConnectionLost)) }
                    else { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(current.lastUpdated == 4 ? v4 : v3))) }
                } else if path.hasSuffix("/sessions") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(SessionListResponse(status:true,sessions:[current])))) }
                else if path.hasSuffix("/turn") { t.respond(["status":true,"turn":NSNull(),"last_seq":16]) }
                else if path.hasSuffix("/events") { RecoveryTransport.hold(t);t.deliver(Data()) }
                else if path.hasSuffix("/deck") { t.respond([:],status:404) }
                else if path.hasSuffix("/offline-files") { t.respond(["status":true,"session_id":"A","files":[]]) }
                else { t.respond(["status":true,"skills":[]]) }
            }
            let store = AgentStore(settings:AppSettings(),offlineCacheRoot:root)
            await store.restoreLocalHistory()
            await wait({ RecoveryTransport.matching("/chat/A/events").count == 1 },"verified v3 attached")
            let stream = RecoveryTransport.takeHeld()
            current = a4;historyFailure = true
            await store.loadSessions()
            await wait({ !store.offlineSyncInProgress },"v4 backfill failed")
            check(!store.offlineSavedSessionIDs.contains("A"),"new target version invalidates saved status")
            var payload = try JSONSerialization.jsonObject(with:JSONEncoder().encode(third)) as! [String:Any];payload["turn_index"] = 2
            let terminal: [String:Any] = ["seq":17,"ts":17,"turn_id":"t_v4","type":"turn_ended","data":payload]
            stream.deliver(Data(("data: " + String(data:try JSONSerialization.data(withJSONObject:terminal),encoding:.utf8)! + "\n\n").utf8))
            await wait({ store.canonicalTurns.count == 3 && !store.offlineSyncInProgress },"terminal remains visible with missing v4 history")
            try? await Task.sleep(nanoseconds:80_000_000)
            let retained = await cache.loadHistory(sessionId:"A",namespace:namespace)
            check(retained?.turns.count == 2 && retained?.lastTurnRef == "ref-v3","unverified terminal cannot overwrite v3 checkpoint")
            check(!store.offlineSavedSessionIDs.contains("A") && store.offlineHistoryNotice == "历史同步未完成","failed checkpoint reports incomplete")
            offline = true;store.newSession()
            let restarted = AgentStore(settings:AppSettings(),offlineCacheRoot:root)
            restarted.select(sessionId:"A")
            await wait({ restarted.canonicalTurns.count == 2 && restarted.historyIsReadOnly },"offline restart reads verified v3")
            check(!restarted.offlineSavedSessionIDs.contains("A") && restarted.offlineHistoryNotice == "离线阅读 · 部分记录尚未同步","restart preserves version mismatch status")
            offline = false;historyFailure = false
            await restarted.loadSessions()
            await wait({ restarted.canonicalTurns.count == 3 && restarted.offlineSavedSessionIDs.contains("A") && !restarted.historyIsReadOnly && !restarted.offlineSyncInProgress },"verified v4 recovery restores saved status")
            let recovered = await cache.loadHistory(sessionId:"A",namespace:namespace,matchingVersion:OfflineHistoryStore.Version(a4))
            check(recovered?.lastTurnRef == "ref-v4" && restarted.offlineHistoryNotice == "已保存到本地","only verified current target restores complete notice")
            restarted.newSession();print("PASS history sync status");return
        }
        if mode == "history-metadata-race" || mode == "history-background-provenance" || mode == "history-prefix-terminal" || mode == "history-remote-tail-delete" {
            let a2 = SessionMetaItem(sessionId:"A",title:"new A",turnCount:1,createdAt:1,lastUpdated:2,pinned:false,active:false)
            let v1 = history("A")
            var newTurn = history("A",answer:"newest-v2").turns[0];newTurn.turnRef = "ref-v2"
            let v2 = HistoryResponse(status:true,sessionId:"A",turns:[newTurn],replayAfter:14,generation:v1.generation,lastTurnRef:"ref-v2")
            try await cache.saveIndex([a],retentionPolicy:nil,namespace:namespace)
            try await cache.saveHistory(v1,metadata:a,namespace:namespace)
            try await cache.saveSelection("A",namespace:namespace)
            var offline = false, serverHistory = v2
            var serverMetadata = a2
            RecoveryTransport.handler = { t in
                let path = t.request.url!.path
                if offline { t.client?.urlProtocol(t,didFailWithError:URLError(.cannotConnectToHost)); return }
                if path.hasSuffix("/history") {
                    if path.contains("/B/") { t.respond(["status":true,"session_id":"B","turns":[],"replay_after":0]) }
                    else if RecoveryTransport.matching("/chat/A/history").count == 1 {
                        if mode == "history-background-provenance" { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(v1))) }
                        else { RecoveryTransport.hold(t) }
                    }
                    else if mode == "history-prefix-terminal" && RecoveryTransport.matching("/chat/A/history").count >= 3 { RecoveryTransport.hold(t) }
                    else { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(serverHistory))) }
                } else if path.hasSuffix("/sessions") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(SessionListResponse(status:true,sessions:[serverMetadata])))) }
                else if path.hasSuffix("/turn") { t.respond(["status":true,"turn":NSNull(),"last_seq":14]) }
                else if path.hasSuffix("/events") { RecoveryTransport.hold(t);t.deliver(Data()) }
                else if path.hasSuffix("/deck") { t.respond([:],status:404) }
                else if path.hasSuffix("/offline-files") { t.respond(["status":true,"session_id":"A","files":[]]) }
                else { t.respond(["status":true,"skills":[]]) }
            }
            let store = AgentStore(settings:AppSettings(),offlineCacheRoot:root)
            await store.restoreLocalHistory()
            await wait({ RecoveryTransport.matching("/chat/A/history").count == 1 },"foreground v1 held")
            if mode == "history-background-provenance" { await wait({ RecoveryTransport.matching("/chat/A/events").count == 1 },"live v1 attached") }
            let delayed = RecoveryTransport.takeHeld()
            await store.loadSessions()
            await wait({ RecoveryTransport.matching("/chat/A/history").count >= 2 && !store.offlineSyncInProgress },"v2 background saved")
            let fresh = await cache.loadHistory(sessionId:"A",namespace:namespace)
            check(fresh?.turns.last?.response?.first?.content == "newest-v2","background v2 exists before old foreground returns")
            if mode == "history-background-provenance" {
                // A background snapshot with a different same-count body cannot relabel
                // the live v1 projection, even when a later terminal appends another row.
                offline = true
                var extra = history("A",answer:"later-v3").turns[0];extra.turnRef = "ref-v3"
                var payload = try JSONSerialization.jsonObject(with:JSONEncoder().encode(extra)) as! [String:Any];payload["turn_index"] = 1
                let event: [String:Any] = ["seq":15,"ts":15,"turn_id":"t_v3","type":"turn_ended","data":payload]
                delayed.deliver(Data(("data: " + String(data:try JSONSerialization.data(withJSONObject:event),encoding:.utf8)! + "\n\n").utf8))
                await wait({ store.messages.contains { $0.text == "later-v3" } && RecoveryTransport.matching("/sessions").count >= 2 },"later live terminal reached cache path")
                try? await Task.sleep(nanoseconds:80_000_000)
                let retained = await cache.loadHistory(sessionId:"A",namespace:namespace)
                check(retained?.turns.first?.response?.first?.content == "newest-v2","different background body cannot promote stale canonical provenance")
                // A shorter authoritative snapshot must never promote an old local tail.
                // Its empty prefix matches every body, but the prior canonical base remains.
                serverHistory = HistoryResponse(status:true,sessionId:"A",turns:[],replayAfter:16,generation:v1.generation,lastTurnRef:nil)
                serverMetadata = SessionMetaItem(sessionId:"A",title:"deleted tail",turnCount:0,createdAt:1,lastUpdated:3,pinned:false,active:false)
                offline = false
                await store.loadSessions()
                await wait({ RecoveryTransport.matching("/chat/A/history").count >= 3 && !store.offlineSyncInProgress },"authoritative shorter history saved")
                offline = true
                var next = history("A",answer:"after-deletion").turns[0];next.turnRef = "ref-after-deletion"
                var nextPayload = try JSONSerialization.jsonObject(with:JSONEncoder().encode(next)) as! [String:Any];nextPayload["turn_index"] = 2
                let nextEvent: [String:Any] = ["seq":16,"ts":16,"turn_id":"t_after_deletion","type":"turn_ended","data":nextPayload]
                delayed.deliver(Data(("data: " + String(data:try JSONSerialization.data(withJSONObject:nextEvent),encoding:.utf8)! + "\n\n").utf8))
                await wait({ store.canonicalTurns.count == 3 },"later terminal reached old local projection")
                try? await Task.sleep(nanoseconds:80_000_000)
                let shortened = await cache.loadHistory(sessionId:"A",namespace:namespace)
                check(shortened?.turns.isEmpty == true,"shorter remote snapshot cannot promote preexisting local tail")
                store.newSession()
                print("PASS history background provenance");return
            }
            delayed.respond(try JSONSerialization.jsonObject(with:JSONEncoder().encode(v1)))
            await wait({ RecoveryTransport.matching("/chat/A/events").count == 1 },"old foreground follows normal recovery")
            check(store.messages.contains { $0.text == "newest-v2" },"late foreground displays current saved canonical version")
            let after = await cache.loadHistory(sessionId:"A",namespace:namespace)
            let afterIndex = await cache.loadIndex(namespace:namespace)
            check(after?.turns.last?.response?.first?.content == "newest-v2" && afterIndex.versions["A"] == OfflineHistoryStore.Version(a2),"late foreground v1 cannot overwrite durable v2")
            let stream = RecoveryTransport.takeHeld()
            var data = try JSONSerialization.jsonObject(with:JSONEncoder().encode(v2.turns[0])) as! [String:Any]
            data["turn_index"] = 0
            let event: [String:Any] = ["seq":15,"ts":15,"turn_id":"t_v2","type":"turn_ended","data":data]
            stream.deliver(Data(("data: " + String(data:try JSONSerialization.data(withJSONObject:event),encoding:.utf8)! + "\n\n").utf8))
            await wait({ RecoveryTransport.matching("/sessions").count >= 2 && !store.offlineSyncInProgress },"same-index terminal replay reconciled")
            try? await Task.sleep(nanoseconds:80_000_000)
            let replayed = await cache.loadHistory(sessionId:"A",namespace:namespace)
            check(replayed?.turns.last?.response?.first?.content == "newest-v2","archived index-zero replay cannot relabel old canonical history as v2")
            // A verified background snapshot may advance provenance for an identical body.
            // The next terminal must still save immediately if connectivity then disappears.
            var third = history("A",answer:"third-v3").turns[0];third.turnRef = "ref-v3"
            serverHistory = HistoryResponse(status:true,sessionId:"A",turns:[newTurn,third],replayAfter:16,generation:v1.generation,lastTurnRef:"ref-v3")
            serverMetadata = SessionMetaItem(sessionId:"A",title:"A3",turnCount:2,createdAt:1,lastUpdated:3,pinned:false,active:false)
            var thirdData = try JSONSerialization.jsonObject(with:JSONEncoder().encode(third)) as! [String:Any];thirdData["turn_index"] = 1
            let thirdEvent: [String:Any] = ["seq":16,"ts":16,"turn_id":"t_v3","type":"turn_ended","data":thirdData]
            stream.deliver(Data(("data: " + String(data:try JSONSerialization.data(withJSONObject:thirdEvent),encoding:.utf8)! + "\n\n").utf8))
            var delayedPrefix: RecoveryTransport?
            if mode == "history-prefix-terminal" {
                await wait({ store.sessions.first?.lastUpdated == 3 && RecoveryTransport.matching("/chat/A/history").count >= 3 && store.canonicalTurns.count == 2 },"new background prefix held before terminal suffix")
                delayedPrefix = RecoveryTransport.takeHeld()
            } else {
                await wait({ store.sessions.first?.lastUpdated == 3 && RecoveryTransport.matching("/chat/A/history").count >= 3 && !store.offlineSyncInProgress },"new list version bound to identical current body")
            }
            offline = true
            var fourth = history("A",answer:"latest-v4").turns[0];fourth.turnRef = "ref-v4"
            var fourthData = try JSONSerialization.jsonObject(with:JSONEncoder().encode(fourth)) as! [String:Any];fourthData["turn_index"] = 2
            let fourthEvent: [String:Any] = ["seq":17,"ts":17,"turn_id":"t_v4","type":"turn_ended","data":fourthData]
            stream.deliver(Data(("data: " + String(data:try JSONSerialization.data(withJSONObject:fourthEvent),encoding:.utf8)! + "\n\n").utf8))
            if let delayedPrefix {
                await wait({ store.canonicalTurns.count == 3 },"terminal suffix applied before prefix comparison")
                delayedPrefix.respond(try JSONSerialization.jsonObject(with:JSONEncoder().encode(serverHistory)))
            }
            var finalSaved: HistoryResponse?
            for _ in 0..<200 {
                finalSaved = await cache.loadHistory(sessionId:"A",namespace:namespace)
                if finalSaved?.turns.last?.response?.first?.content == "latest-v4" { break }
                try? await Task.sleep(nanoseconds:5_000_000)
            }
            if mode == "history-prefix-terminal" {
                check(finalSaved?.turns.count == 2 && finalSaved?.turns.last?.response?.first?.content == "third-v3","suffix present before comparison cannot acquire background provenance")
            } else {
                check(finalSaved?.turns.last?.response?.first?.content == "latest-v4","fresh canonical provenance still saves next terminal while offline")
            }
            if mode == "history-remote-tail-delete" {
                // The third row was already saved, then deleted by another client.
                // An identical two-row prefix cannot reauthorize that preexisting suffix.
                serverMetadata = SessionMetaItem(sessionId:"A",title:"remote deleted v4",turnCount:2,createdAt:1,lastUpdated:4,pinned:false,active:false)
                offline = false
                await store.loadSessions()
                await wait({ store.sessions.first?.lastUpdated == 4 && RecoveryTransport.matching("/chat/A/history").count >= 4 && !store.offlineSyncInProgress },"remote deletion background saved")
                try? await Task.sleep(nanoseconds:80_000_000)
                finalSaved = await cache.loadHistory(sessionId:"A",namespace:namespace)
                check(finalSaved?.turns.count == 2,"background deletion cannot resurrect a terminal saved before comparison")
            }
            store.select(sessionId:"B")
            await wait({ RecoveryTransport.matching("/chat/B/history").count == 1 },"switch before restart")
            offline = true
            let restarted = AgentStore(settings:AppSettings(),offlineCacheRoot:root)
            restarted.select(sessionId:"A")
            await wait({ restarted.messages.contains { $0.text == "newest-v2" } },"offline restart keeps newest history")
            if mode == "history-prefix-terminal" || mode == "history-remote-tail-delete" {
                check(restarted.canonicalTurns.count == 2 && restarted.messages.contains { $0.text == "third-v3" } && !restarted.messages.contains { $0.text == "latest-v4" },"restart cannot resurrect unverified or remotely deleted suffix")
            } else {
                check(restarted.canonicalTurns.count == 3 && restarted.messages.contains { $0.text == "newest-v2" } && restarted.messages.contains { $0.text == "latest-v4" },"restart keeps replaced canonical row and latest terminal")
            }
            store.newSession();restarted.newSession()
            print("PASS " + mode);return
        }
        if mode.hasPrefix("history-only-") {
            try await cache.saveIndex([a],retentionPolicy:nil,namespace:namespace)
            try await cache.saveHistory(history("A"),metadata:a,namespace:namespace)
            try await cache.saveSelection("A",namespace:namespace)
            RecoveryTransport.handler = { t in
                let path = t.request.url!.path
                if path.hasSuffix("/history") {
                    if mode == "history-only-decode" { t.deliver(Data("{".utf8),type:"application/json",finish:true) }
                    else { t.respond(["detail":"synthetic history error"],status:mode == "history-only-auth" ? 401 : 500) }
                } else if path.hasSuffix("/sessions") { t.respond(try! JSONSerialization.jsonObject(with:JSONEncoder().encode(SessionListResponse(status:true,sessions:[a])))) }
                else if path.hasSuffix("/turn") { t.respond(["status":true,"turn":NSNull(),"last_seq":12]) }
                else { t.respond(["status":true,"skills":[]]) }
            }
            let store = AgentStore(settings:AppSettings(),offlineCacheRoot:root)
            await store.restoreLocalHistory()
            await wait({ store.messages.contains { $0.text == "answer-A" } && store.errorMessage != nil },"history-only failure visible")
            await store.refreshConnection();await store.loadSessions()
            check(store.connectionOK == true && store.errorMessage != nil && store.historyIsReadOnly,"history-only error remains visible despite healthy sessions")
            let retained = await cache.loadHistory(sessionId:"A",namespace:namespace)
            check(retained != nil && store.messages.contains { $0.text == "answer-A" },"history-only failure preserves saved body")
            store.newSession()
            print("PASS " + mode);return
        }
        if mode == "asset-manifest-race" || mode == "asset-fetch-race" || mode == "asset-epoch-save" || mode == "asset-membership-legacy" {
            let config = URLSessionConfiguration.ephemeral;config.protocolClasses = [RecoveryTransport.self]
            let api = AgentClient(baseURL:URL(string:"https://fixture.invalid")!,apiKey:"synthetic",session:URLSession(configuration:config),cacheRoot:root)
            if mode == "asset-membership-legacy" {
                let url = root.appendingPathComponent("assets/" + OfflineHistoryStore.digest(namespace) + "/membership.json")
                try FileManager.default.createDirectory(at:url.deletingLastPathComponent(),withIntermediateDirectories:true)
                try Data(#"{"listed":["A"],"deleted":[]}"#.utf8).write(to:url)
                let before = try await api.offlineAssets.epoch(namespace:namespace,sid:"A")
                check(before == 0,"legacy membership defaults missing epoch to zero")
                let path = "/api/v1/files/A/offline/attach/keep.txt"
                try await api.offlineAssets.save(Data("keep".utf8),namespace:namespace,sid:"A",key:path,expectedEpoch:before)
                try await api.offlineAssets.invalidateInputs(retaining:[OfflineInputReference(kind:"attach",path:"keep.txt")!],namespace:namespace,sid:"A")
                let restored = OfflineAssetStore(root:root)
                let after = try await restored.epoch(namespace:namespace,sid:"A")
                let bytes = await restored.data(namespace:namespace,sid:"A",key:path)
                check(after == 1 && bytes != nil,"legacy membership survives durable epoch update")
                print("PASS asset membership legacy");return
            }
            let keep = OfflineFileEntry(path:"/api/v1/files/A/offline/attach/keep.txt",name:"keep.txt",kind:"attach",size:4,revision:"1")
            let cut = OfflineFileEntry(path:"/api/v1/files/A/offline/attach/cut.txt",name:"cut.txt",kind:"attach",size:3,revision:"1")
            let output = OfflineFileEntry(path:"/api/v1/files/A/offline/assets/result.pdf",name:"result.pdf",kind:"assets",size:6,revision:"1")
            let old = OfflineFileManifest(status:true,session_id:"A",files:[keep,cut,output])
            try await api.offlineAssets.save(JSONEncoder().encode(old),namespace:namespace,sid:"A",key:"manifest")
            try await api.offlineAssets.save(Data("keep".utf8),namespace:namespace,sid:"A",key:keep.path)
            try await api.offlineAssets.save(Data("output".utf8),namespace:namespace,sid:"A",key:output.cachePath(sessionId:"A"))
            if mode == "asset-epoch-save" {
                let before = try await api.offlineAssets.epoch(namespace:namespace,sid:"A")
                try await api.offlineAssets.invalidateInputs(retaining:[OfflineInputReference(kind:"attach",path:"keep.txt")!],namespace:namespace,sid:"A")
                var rejected = false
                do { try await api.offlineAssets.save(JSONEncoder().encode(old),namespace:namespace,sid:"A",key:"manifest",expectedEpoch:before) } catch { rejected = true }
                check(rejected,"pre-delete manifest save is rejected")
                print("PASS asset epoch save");return
            }
            RecoveryTransport.handler = { t in RecoveryTransport.hold(t) }
            if mode == "asset-manifest-race" {
                let task = Task { await api.syncOfflineAssets(sessionId:"A") }
                await wait({ RecoveryTransport.matching("/offline-files").count == 1 },"old manifest request held")
                let delayed = RecoveryTransport.takeHeld()
                try await api.offlineAssets.invalidateInputs(retaining:[OfflineInputReference(kind:"attach",path:"keep.txt")!],namespace:namespace,sid:"A")
                delayed.respond(try JSONSerialization.jsonObject(with:JSONEncoder().encode(old)))
                let result = await task.value
                check(result.state == .failed,"old manifest response is rejected after input deletion")
            } else {
                let task = Task { try await api.fetchAsset(path:cut.path) }
                await wait({ RecoveryTransport.matching("/cut.txt").count == 1 },"old input fetch held")
                let delayed = RecoveryTransport.takeHeld()
                try await api.offlineAssets.invalidateInputs(retaining:[OfflineInputReference(kind:"attach",path:"keep.txt")!],namespace:namespace,sid:"A")
                delayed.deliver(Data("cut".utf8),type:"application/octet-stream",finish:true)
                do { _ = try await task.value;check(false,"pre-delete input fetch must not return deleted bytes") } catch {}
            }
            let restarted = OfflineAssetStore(root:root)
            let kept = await restarted.data(namespace:namespace,sid:"A",key:keep.path)
            let removed = await restarted.data(namespace:namespace,sid:"A",key:cut.path)
            let produced = await restarted.data(namespace:namespace,sid:"A",key:output.cachePath(sessionId:"A"))
            let inputs = await api.cachedInputFiles(sessionId:"A")
            check(kept != nil && removed == nil && produced != nil && inputs.map(\.name) == ["keep.txt"],"late input IO preserves only remaining originals and outputs")
            print("PASS " + mode);return
        }
        func tailTurn(_ suffix: String) -> Turn {
            try! JSONDecoder().decode(Turn.self,from:JSONSerialization.data(withJSONObject:[
                "status":"success","prompt":"input-"+suffix,
                "image":["/server/sessions/A/image/"+suffix+".png"],"audio":[],
                "attachments":[["path":"/server/sessions/A/attach/"+suffix+".txt","name":suffix+".txt","type":"text"],
                    ["path":"/server/sessions/A/audio/"+suffix+".wav","name":"录音.wav","type":"audio"],
                    ["path":"/etc/foreign/audio/cut.wav","name":"cut.wav","type":"audio"],
                    ["path":"/server/sessions/A/audio/cut.wav","name":"cut.wav","type":"text"]],
                "response":[["role":"assistant","content":suffix == "keep" ? "answer-A" : "tail-answer"]]
            ]))
        }
        let tailSnapshot = HistoryResponse(status:true,sessionId:"A",turns:[tailTurn("keep"),tailTurn("cut")],replayAfter:12,lastTurnRef:"ref-tail")
        var requestsHeld = false, failHistory = false
        RecoveryTransport.handler = { t in
            let path = t.request.url!.path
            if path.hasSuffix("/sessions"), ["index-create","index-delete","index-receipt-delete","index-receipt-submit"].contains(mode) {
                RecoveryTransport.hold(t); return
            }
            if path.hasSuffix("/turns"), mode == "index-receipt-submit" {
                t.respond(["status":true,"turn_id":"t_submit","seq":13],status:202); return
            }
            if t.request.httpMethod == "DELETE", mode == "index-receipt-delete" {
                t.respond(["status":true]); return
            }
            if path.hasSuffix("/turns/last") {
                if mode.hasSuffix("failed") { t.respond(["status":false,"response":"synthetic refusal"]) }
                else { failHistory = true; t.respond(["status":true]) }
            } else if path.hasSuffix("/history") {
                if failHistory { t.respond([:],status:503); return }
                if ["read", "select", "readonly", "late-client", "bounded"].contains(mode) { RecoveryTransport.hold(t); requestsHeld = true; return }
                let id = path.split(separator:"/").dropLast().last.map(String.init)!
                t.respond(try! JSONSerialization.jsonObject(with: JSONEncoder().encode(mode.hasPrefix("tail-") && id == "A" ? tailSnapshot : history(id))))
            } else if path.hasSuffix("/sessions") {
                if mode == "index-failure" { t.respond(["status":false,"sessions":[]]); return }
                let items = mode.hasPrefix("tail-") ? [a,b] : (0..<7).map { item("S\($0)") }
                t.respond(try! JSONSerialization.jsonObject(with: JSONEncoder().encode(SessionListResponse(status:true,sessions:items))))
            } else if path.hasSuffix("/offline-files") {
                let id = path.split(separator:"/").dropLast().last.map(String.init)!
                t.respond(["status":true,"session_id":id,"files":[]])
            } else if path.hasSuffix("/turn") { t.respond(["status":true,"turn":NSNull(),"last_seq":12]) }
            else if path.hasSuffix("/events") { RecoveryTransport.hold(t); t.deliver(Data()) }
            else if path.hasSuffix("/deck") { t.respond([:],status:404) }
            else if path.hasSuffix("/skills") { t.respond(["status":true,"skills":[]]) }
            else { t.respond(["status":true]) }
        }
        if ["index-create","index-delete","index-receipt-delete","index-receipt-submit"].contains(mode) {
            func heldIndex() -> RecoveryTransport {
                for _ in 0..<2000 {
                    RecoveryTransport.lock.lock()
                    if let index = RecoveryTransport.held.firstIndex(where: { $0.request.url!.path.hasSuffix("/sessions") }) {
                        let value = RecoveryTransport.held.remove(at:index)
                        RecoveryTransport.lock.unlock(); return value
                    }
                    RecoveryTransport.lock.unlock(); Thread.sleep(forTimeInterval:0.001)
                }
                fatalError("missing held session index")
            }
            func reply(_ request: RecoveryTransport, _ items: [SessionMetaItem]) {
                request.respond(try! JSONSerialization.jsonObject(with: JSONEncoder().encode(SessionListResponse(status:true,sessions:items))))
            }
            try await cache.saveIndex(mode == "index-create" ? [a] : [a,b], retentionPolicy:nil, namespace:namespace)
            try await cache.saveHistory(history("A"), metadata:a, namespace:namespace)
            if mode != "index-create" { try await cache.saveHistory(history("B"), metadata:b, namespace:namespace) }
            let store = AgentStore(settings:AppSettings(), offlineCacheRoot:root)
            await store.restoreLocalHistory()
            if mode != "index-create" {
                store.select(sessionId:"B")
                await wait({ RecoveryTransport.matching("/chat/B/events").count == 1 && !store.historyIsReadOnly }, "B online selection")
            }
            let old = Task { await store.loadSessions() }
            await wait({ RecoveryTransport.matching("/sessions").count == 1 }, "older index request")
            let delayed = heldIndex()
            if mode == "index-receipt-submit" {
                store.send(prompt:"new B input",uploads:[],thinking:false)
                await wait({ RecoveryTransport.matching("/turns").count == 1 && !store.hasSubmissionInFlight("B") }, "submission accepted")
                reply(delayed,[a]); await old.value
                let body = await cache.loadHistory(sessionId:"B",namespace:namespace)
                check(body != nil && store.sessions.contains { $0.sessionId == "B" }, "accepted submission invalidates earlier index")
                print("PASS index receipt submit"); return
            }
            if mode == "index-receipt-delete" {
                let deletion = Task { await store.deleteSession(b) }
                await wait({ RecoveryTransport.matching("/sessions").count == 2 }, "confirmed delete starts replacement index")
                reply(delayed,[a,b]); await old.value
                check(!store.sessions.contains { $0.sessionId == "B" }, "confirmed deletion invalidates earlier index")
                reply(heldIndex(),[a]); let result = await deletion.value
                let body = await cache.loadHistory(sessionId:"B",namespace:namespace)
                check(result.deleted == ["B"] && body == nil, "confirmed delete remains absent on disk")
                print("PASS index receipt delete"); return
            }
            let latest = Task { await store.loadSessions() }
            await wait({ RecoveryTransport.matching("/sessions").count == 2 }, "newer index request")
            reply(heldIndex(),mode == "index-create" ? [a,b] : [a])
            await latest.value
            if mode == "index-create" {
                await wait({ store.offlineSavedSessionIDs.contains("B") && !store.offlineSyncInProgress }, "new B body synchronized")
                store.select(sessionId:"B")
                await wait({ store.messages.contains { $0.text == "answer-B" } }, "new B selected")
                reply(delayed,[a]); await old.value
                let body = await cache.loadHistory(sessionId:"B",namespace:namespace)
                let index = await cache.loadIndex(namespace:namespace)
                check(body != nil && index.sessions.contains { $0.sessionId == "B" } && store.sessions.contains { $0.sessionId == "B" } && store.selectedSessionId == "B", "older index cannot discard newly synchronized B")
            } else {
                reply(delayed,[a,b]); await old.value
                let body = await cache.loadHistory(sessionId:"B",namespace:namespace)
                let index = await cache.loadIndex(namespace:namespace)
                check(body == nil && !index.sessions.contains { $0.sessionId == "B" } && !store.sessions.contains { $0.sessionId == "B" } && store.selectedSessionId == nil, "older index cannot resurrect removed B")
            }
            print("PASS " + mode); return
        }
        if mode.hasPrefix("tail-") {
            try await cache.saveIndex([a,b], retentionPolicy:nil, namespace:namespace)
            try await cache.saveHistory(tailSnapshot, metadata:a, namespace:namespace)
            try await cache.saveHistory(history("B"), metadata:b, namespace:namespace)
            try await cache.saveSelection("A", namespace:namespace)
            let store = AgentStore(settings:AppSettings(), offlineCacheRoot:root)
            let api = store.client!
            var files = [OfflineFileEntry(path:"/api/v1/files/A/offline/assets/result.pdf",name:"result.pdf",kind:"assets",size:1,revision:"1")]
            for suffix in ["keep","cut"] {
                for (kind,ext) in [("image","png"),("audio","wav"),("attach","txt")] {
                    let name = suffix + "." + ext
                    files.append(OfflineFileEntry(path:"/api/v1/files/A/offline/"+kind+"/"+name,name:name,kind:kind,size:1,revision:"1"))
                }
            }
            if mode.contains("paused") { files.append(OfflineFileEntry(path:"/api/v1/files/A/offline/attach/pending.bin",name:"pending.bin",kind:"attach",size:1,revision:"1")) }
            for file in files { try await api.offlineAssets.save(Data([42]),namespace:namespace,sid:"A",key:file.cachePath(sessionId:"A")) }
            try await api.offlineAssets.save(JSONEncoder().encode(OfflineFileManifest(status:true,session_id:"A",files:files)),namespace:namespace,sid:"A",key:"manifest")
            await store.restoreLocalHistory()
            await wait({ RecoveryTransport.matching("/chat/A/events").count == 1 && !store.historyIsReadOnly }, "online authority before tail mutation")
            if mode.contains("paused") {
                let stream = RecoveryTransport.takeHeld()
                let events: [[String:Any]] = [["seq":13,"ts":13,"turn_id":"t_paused","type":"turn_queued","data":["prompt":"pending"]],["seq":14,"ts":14,"turn_id":"t_paused","type":"suspended","data":[:]]]
                stream.deliver(Data(events.map { "data: " + String(data:try! JSONSerialization.data(withJSONObject:$0),encoding:.utf8)! + "\n\n" }.joined().utf8))
                await wait({ store.isSuspended },"paused unlogged input")
            }
            if mode.contains("edit") { store.editLastTurn() } else { store.deleteLastTurn() }
            await wait({ RecoveryTransport.matching("/turns/last").count == 1 && store.errorMessage != nil }, "tail receipt and recovery outcome")
            let restartCache = OfflineHistoryStore(root:root)
            let after = await restartCache.loadHistory(sessionId:"A", namespace:namespace)
            let other = await restartCache.loadHistory(sessionId:"B", namespace:namespace)
            let index = await restartCache.loadIndex(namespace:namespace)
            check(other != nil && index.sessions.contains { $0.sessionId == "A" }, "tail mutation preserves list and unrelated history")
            if mode.hasSuffix("failed") {
                check(after?.turns.count == 2 && store.messages.contains { $0.text == "answer-A" }, "unconfirmed tail deletion retains cached body")
            } else {
                check(after == nil, "confirmed tail deletion invalidates body even when history reload fails")
                let staleSaved = try await cache.saveHistory(history("A"), metadata:a, namespace:namespace, expectedEpoch:0)
                check(!staleSaved, "pre-deletion request cannot restore invalidated tail")
                let restarted = AgentStore(settings:AppSettings(), offlineCacheRoot:root)
                await restarted.restoreLocalHistory()
                await wait({ restarted.errorMessage != nil }, "restart remains offline after deletion")
                check(!restarted.messages.contains { $0.text == "answer-A" }, "restart never resurrects a deleted tail")
            }
            let restoredAssets = OfflineAssetStore(root:api.offlineCacheRoot)
            for entry in files {
                let bytes = await restoredAssets.data(namespace:namespace,sid:"A",key:entry.cachePath(sessionId:"A"))
                let shouldKeep = mode.hasSuffix("failed") || entry.kind == "assets" || entry.name.hasPrefix("keep") || (mode.contains("paused") && entry.name.hasPrefix("cut"))
                check((bytes != nil) == shouldKeep,"tail receipt preserves exactly the remaining originals and assets")
            }
            let inputs = await api.cachedInputFiles(sessionId:"A")
            let expectedCount = mode.hasSuffix("failed") || mode.contains("paused") ? 6 : 3
            check(inputs.count == expectedCount,"tail input manifest follows confirmed remaining turns")
            print("PASS " + mode); return
        }
        if mode == "bounded" {
            let store = AgentStore(settings:AppSettings(), offlineCacheRoot:root)
            await store.loadSessions()
            for i in 0..<7 {
                await wait({ RecoveryTransport.matching("/history").count == min(i + 2, 7) }, "two concurrent history requests")
                try? await Task.sleep(nanoseconds:20_000_000)
                check(RecoveryTransport.matching("/history").count - i <= 2, "sync concurrency stays bounded")
                let request = RecoveryTransport.takeHeld()
                let id = request.request.url!.path.split(separator:"/").dropLast().last.map(String.init)!
                request.respond(try JSONSerialization.jsonObject(with: JSONEncoder().encode(history(id))))
            }
            await wait({ store.offlineSavedSessionIDs.count == 7 && !store.offlineSyncInProgress }, "bounded sync completed")
            print("PASS bounded synchronization"); return
        }
        if mode == "migration" {
            UserDefaults.standard.set("S3", forKey:"session.lastSelected")
            UserDefaults.standard.set(try JSONSerialization.data(withJSONObject:["backendURL":"https://fixture.invalid","items":[]]), forKey:"macagent.sessions.snapshot")
            let store = AgentStore(settings:AppSettings(), offlineCacheRoot:root)
            await store.loadSessions()
            await wait({ store.selectedSessionId == "S3" && store.messages.contains { $0.text == "answer-S3" } }, "legacy selection restored after authoritative index")
            print("PASS authenticated selection migration"); return
        }
        if mode == "disk-failure" {
            try Data("cache root is a file".utf8).write(to: root)
            let store = AgentStore(settings:AppSettings(), offlineCacheRoot:root)
            store.select(sessionId:"A")
            await wait({ RecoveryTransport.matching("/chat/A/events").count == 1 }, "online projection despite disk refusal")
            check(store.messages.contains { $0.text == "answer-A" }, "disk refusal preserves online transcript")
            check(!store.offlineSavedSessionIDs.contains("A"), "failed disk write is never advertised as saved")
            print("PASS disk failure status"); return
        }
        if mode == "all" {
            let store = AgentStore(settings:AppSettings(), offlineCacheRoot:root)
            await store.loadSessions()
            await wait({ store.offlineSavedSessionIDs.count == 7 && !store.offlineSyncInProgress }, "all existing sessions cached")
            check(store.selectedSessionId == nil && store.canonicalTurns.isEmpty, "background sync retains no transcript in UI memory")
            for i in 0..<7 {
                let saved = await cache.loadHistory(sessionId:"S\(i)", namespace:namespace)
                check(saved?.turns.count == 1, "unclicked history is durable")
            }
            let count = RecoveryTransport.matching("/history").count
            let manifests = RecoveryTransport.matching("/offline-files").count
            check(manifests == 7, "the first pass lists attachments once per session")
            await store.loadSessions()
            await wait({ !store.offlineSyncInProgress }, "resume checks")
            // An unchanged index owes nothing: no manifest, no deck, and no notice
            // or Files-tab reload every 15 seconds.
            try? await Task.sleep(nanoseconds:200_000_000)
            check(RecoveryTransport.matching("/history").count == count, "completed versions skip repeated history downloads")
            check(RecoveryTransport.matching("/offline-files").count == manifests, "an unchanged index issues no second manifest request")
            check(RecoveryTransport.matching("/deck").count == manifests, "an unchanged index issues no second deck request")
            check(!store.offlineSyncInProgress && store.offlineAttachmentsComplete, "an idle pass reports a complete cache")
            print("PASS all existing histories"); return
        }
        if mode == "index-failure" {
            try await cache.saveIndex([a], retentionPolicy:nil, namespace:namespace)
            try await cache.saveHistory(history("A"), metadata:a, namespace:namespace)
            let store = AgentStore(settings:AppSettings(), offlineCacheRoot:root)
            await store.restoreLocalHistory(); await store.loadSessions()
            check(store.sessions.map(\.sessionId) == ["A"], "status false cannot erase offline index")
            let retained = await cache.loadHistory(sessionId:"A", namespace:namespace)
            check(retained != nil, "status false cannot erase offline body")
            print("PASS failed index retains cache"); return
        }
        if mode == "readonly" || mode == "select" || mode == "late-client" {
            try await cache.saveIndex([a,b], retentionPolicy:nil, namespace:namespace)
            try await cache.saveHistory(history("A"), metadata:a, namespace:namespace)
            try await cache.saveHistory(history("B"), metadata:b, namespace:namespace)
            try await cache.saveSelection("A", namespace:namespace)
        }
        let store = AgentStore(settings:AppSettings(), offlineCacheRoot:root)
        await store.restoreLocalHistory()
        await wait({ store.messages.contains { $0.text == "answer-A" } }, "cached body before network reply")
        await wait({ requestsHeld }, "network request blocked")
        check(store.historyIsReadOnly && store.canonicalTurns.count == 1 && store.lastTurnRef == "ref-A", "cached full projection is read only")
        if mode == "readonly" {
            store.send(prompt:"must not submit",uploads:[],thinking:false)
            store.branch(fromTurnIndex:0); store.deleteLastTurn(); store.regenerateLastTurn(); store.editLastTurn()
            try? await Task.sleep(nanoseconds:50_000_000)
            check(RecoveryTransport.requests.allSatisfy { $0.httpMethod == "GET" }, "cached history cannot authorize mutations")
            check(store.messages.map(\.text) == ["prompt-A","answer-A"], "read only operations retain text")
        }
        if mode == "select" {
            store.select(sessionId:"B")
            await wait({ store.messages.contains { $0.text == "answer-B" } }, "another synced session reads offline")
            let query = "answer-B"; store.searchQuery = query; await store.runSearch()
            check(store.searchResults?.contains { $0.sessionId == "B" } == true, "offline content search uses disk")
        }
        print("PASS " + mode)
    }
}
