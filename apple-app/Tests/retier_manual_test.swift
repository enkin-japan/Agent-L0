import Foundation
func L(_ value: String) -> String { value }
@MainActor final class AppSettings {
    var setupCompleted = true
    var baseURL: URL? = URL(string:"https://original.invalid")
    var apiKey = "synthetic"
    var notifEnabled = true
}
func verify(_ value: @autoclosure () -> Bool,_ message:String) {if !value(){print("FAIL: \(message)");exit(1)}}
final class RetierTransport: URLProtocol {
    static let lock=NSLock()
    static var requests:[URLRequest]=[]
    static var held:RetierTransport?
    static var handler:((RetierTransport)->Void)!
    static var reads:Int {lock.lock();defer{lock.unlock()};return requests.filter{$0.httpMethod=="GET"}.count}
    static var posts:Int {lock.lock();defer{lock.unlock()};return requests.filter{$0.httpMethod=="POST"}.count}
    override class func canInit(with request:URLRequest)->Bool {true}
    override class func canonicalRequest(for request:URLRequest)->URLRequest {request}
    override func startLoading(){Self.lock.lock();Self.requests.append(request);Self.lock.unlock();Self.handler(self)}
    override func stopLoading(){}
    func requestData() -> Data {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open(); defer { stream.close() }
        var data = Data(), buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let n = stream.read(&buffer, maxLength: buffer.count)
            if n <= 0 { break }
            data.append(buffer, count: n)
        }
        return data
    }
    func respond(_ body:Any,status:Int=200){
        client?.urlProtocol(self,didReceive:HTTPURLResponse(url:request.url!,statusCode:status,httpVersion:nil,headerFields:["Content-Type":"application/json"])!,cacheStoragePolicy:.notAllowed)
        client?.urlProtocol(self,didLoad:try! JSONSerialization.data(withJSONObject:body))
        client?.urlProtocolDidFinishLoading(self)
    }
}
@main struct RetierTests {
    @MainActor static func wait(_ ready:()->Bool) async {
        for _ in 0..<500 {if ready(){return};try? await Task.sleep(nanoseconds:5_000_000)}
        verify(false,"transport milestone timed out")
    }
    @MainActor static func main() async throws {
        let mode=CommandLine.arguments[1]
        let settings=AppSettings()
        if mode=="notifications-off" {settings.notifEnabled=false}
        let config=URLSessionConfiguration.ephemeral;config.protocolClasses=[RetierTransport.self]
        var confirmations=0,notes:[String]=[],errors:[String]=[],clock=0.0
        let monitor=RetierMonitor(session:URLSession(configuration:config),confirmation:{_ in
            confirmations += 1
            if mode=="changed-confirm" {settings.baseURL=URL(string:"https://new.invalid")}
            if mode=="changed-key" {settings.apiKey="another-synthetic"}
            return mode != "decline" && !(mode=="snoozed" && confirmations==1)
        },errorSink:{errors.append($0)},notificationSink:{notes.append($0)},pause:{_ in},now:{
            if mode=="stalled" {clock += 1801}
            return Date(timeIntervalSince1970:clock)
        })
        monitor.configure(settings:settings)
        func status(_ state:String="idle",needs:Bool?=true,started:String="old",compute:Bool=true)->[String:Any]{
            var r:[String:Any]=["rebuild":["state":state,"started_at":started,"docs_done":1,"docs_total":1],"store":["tier":"old"]]
            r["compute"]=compute ? ["tier":"new"] : NSNull()
            if let needs {r["needs_rebuild"]=needs}
            return r
        }
        RetierTransport.handler={t in
            if t.request.httpMethod=="POST" {
                let body=try! JSONSerialization.jsonObject(with:t.requestData()) as! [String:Any]
                verify(body["force"] as? Bool==false,"manual rebuild never forces healthy data")
                if mode=="changed-post" {RetierTransport.held=t;return}
                if mode.hasPrefix("post-network") {
                    t.client?.urlProtocol(t,didFailWithError:URLError(.timedOut));return
                }
                if ["post-old","post-new","changed-follow"].contains(mode) {t.respond(["detail":"synthetic unavailable"],status:503);return}
                if ["conflict-running","changed-follow-conflict"].contains(mode) {t.respond(["detail":"synthetic conflict"],status:409);return}
                t.respond(["state":"running"]);return
            }
            let reads=RetierTransport.reads,posts=RetierTransport.posts
            if ["coalesce","changed-fetch"].contains(mode) && reads==1 {RetierTransport.held=t;return}
            if ["changed-follow","changed-follow-conflict","post-network-changed-follow"].contains(mode) && posts==1 {RetierTransport.held=t;return}
            if mode=="status-failed" || (["unreachable","post-network-unreachable"].contains(mode) && reads>1) {t.respond(["detail":"synthetic unavailable"],status:503);return}
            if mode=="healthy" {t.respond(status(needs:false));return}
            if mode=="unknown" {t.respond(status(needs:nil,compute:false));return}
            if ["already-running","unreachable","stalled"].contains(mode) {
                t.respond(mode=="already-running" && reads>1 ? status("done",needs:false,started:"new") : status("running",started:"new"));return
            }
            if posts==0 {t.respond(status());return}
            if mode=="post-old" || mode=="post-network-old" {t.respond(status("done",needs:false));return}
            if mode=="conflict-running" && reads==2 {t.respond(status("running",started:"new"));return}
            t.respond(status("done",needs:false,started:"new"))
        }
        if ["coalesce","changed-fetch","changed-post","changed-follow","changed-follow-conflict","post-network-changed-follow"].contains(mode) {
            let task=Task {await monitor.requestManualRebuild()}
            await wait {RetierTransport.held != nil}
            if mode=="coalesce" {
                await monitor.requestManualRebuild()
                verify(RetierTransport.reads==1 && confirmations==0,"in-flight check coalesces before any second request")
            } else {settings.baseURL=URL(string:"https://new.invalid")}
            if mode=="changed-post" { RetierTransport.held!.respond(["state":"running"]) }
            else { RetierTransport.held!.respond(status()) }
            await task.value
        } else {
            await monitor.requestManualRebuild()
            if mode=="snoozed" {
                verify(confirmations==1 && RetierTransport.posts==0,"later defers without any rebuild")
                await monitor.requestManualRebuild()
            }
        }
        verify(!monitor.isBusy && monitor.rebuildProgress==nil,"completed driver releases busy/progress")
        if mode=="completed-switch" {
            verify(monitor.manualNotice != nil,"completion was recorded for original connection")
            settings.baseURL=URL(string:"https://new.invalid")
            verify(monitor.manualNotice==nil,"completed old connection notice cannot describe the new backend")
        }
        verify(RetierTransport.requests.allSatisfy{$0.url?.host=="original.invalid"},"confirmed operation never retargets another backend")
        switch mode {
        case "decline":verify(confirmations==1 && RetierTransport.posts==0 && notes.isEmpty,"declining confirmation cannot rebuild")
        case "snoozed":verify(confirmations==2 && RetierTransport.posts==1 && notes.count==1,"manual entry bypasses snooze with a fresh confirmation")
        case "healthy","unknown","status-failed":verify(confirmations==0 && RetierTransport.posts==0 && monitor.manualNotice != nil && notes.isEmpty,"healthy or unknown status does not invent a rebuild")
        case "changed-fetch":verify(confirmations==0 && RetierTransport.posts==0 && notes.isEmpty,"stale status cannot prompt on the new connection")
        case "changed-confirm","changed-key":verify(RetierTransport.posts==0 && notes.isEmpty,"connection change after confirmation blocks POST")
        case "changed-post","changed-follow","changed-follow-conflict","post-network-changed-follow":verify(RetierTransport.posts==1 && notes.isEmpty && errors.isEmpty && monitor.manualNotice?.contains("连接已更改")==true,"late old operation cannot overwrite connection-change notice")
        case "post-network-old","post-network-unreachable":verify(notes.isEmpty && errors.isEmpty && monitor.manualNotice?.contains("未确认")==true,"uncertain POST leaves a clear unconfirmed outcome")
        case "post-old":verify(notes.isEmpty && errors.count==1,"old done state is not proof a failed POST started rebuilding")
        case "already-running":verify(confirmations==0 && RetierTransport.posts==0 && notes.count==1,"manual entry follows existing work without a second rebuild")
        case "unreachable":verify(RetierTransport.reads==61 && monitor.manualNotice?.contains("进度未知")==true,"unreachable follow remains bounded and truthful")
        case "stalled":verify(RetierTransport.reads==3 && monitor.manualNotice?.contains("停滞")==true,"unchanged running progress stops at the existing stall budget")
        case "notifications-off":verify(notes.isEmpty && monitor.manualNotice?.contains("完成")==true,"notification opt-out is preserved with inline completion")
        default:verify(confirmations==1 && RetierTransport.posts==1 && notes.count==1 && errors.isEmpty,"accepted rebuild uses one pipeline and reports its new completion")
        }
        print("PASS \(mode) reads=\(RetierTransport.reads) posts=\(RetierTransport.posts)")
    }
}
