import Foundation
final class ListingResponses {
    static let lock = NSLock()
    static var held: [SetupTransport] = []
    static var holding = false
    static var suffixToHold: String?
    static var failedPath: String?
    static var name = "fresh"
    static func payload(_ path: String, _ name: String) -> String {
        if path.hasSuffix("/registrations") { return "{\"status\":true,\"registrations\":[{\"id\":\"api_tool-\(name)\",\"name\":\"\(name)\",\"kind\":\"api_tool\"}]}" }
        return "{\"status\":true,\"tools\":[{\"file\":\"\(name).yaml\",\"source\":\"user\",\"name\":\"\(name)\",\"method\":\"POST\",\"enabled\":true,\"has_secret\":false,\"requires_approval\":true,\"has_canary\":false,\"registered\":false}]}"
    }
    static func handle(_ t: SetupTransport) {
        lock.lock()
        let path=t.request.url!.path
        if holding || suffixToHold.map({path.hasSuffix($0)}) == true { held.append(t); lock.unlock(); return }
        let status=failedPath.map({path.hasSuffix($0) || $0=="all"}) == true ? 503 : 200
        let body=payload(path,name)
        lock.unlock()
        t.respond(body:body,status:status)
    }
    static var count: Int { lock.lock();defer{lock.unlock()};return held.count }
    static func release(name: String="old",failed: Bool=false) {
        lock.lock();let pending=held;held=[];lock.unlock()
        for t in pending { t.respond(body:payload(t.request.url!.path,name),status:failed ? 503 : 200) }
    }
}
@main struct ListingTests {
    @MainActor static func wait(_ condition: () -> Bool) async {
        for _ in 0..<400 {if condition(){return};try? await Task.sleep(nanoseconds:5_000_000)}
        require(false,"timed out waiting for independent listing result")
    }
    @MainActor static func main() async throws {
        let mode=CommandLine.arguments[1]
        func client(_ host: String)->AgentClient {
            let config=URLSessionConfiguration.ephemeral;config.protocolClasses=[SetupTransport.self]
            return AgentClient(baseURL:URL(string:"https://\(host).invalid")!,apiKey:"synthetic",session:URLSession(configuration:config))
        }
        let first=client("first"), second=client("second")
        var current: AgentClient?=first
        let model=RegistrationSetupState()
        SetupTransport.handler=ListingResponses.handle
        if mode=="queue-error" || mode=="tool-error" {
            ListingResponses.failedPath=mode=="queue-error" ? "/registrations" : "/api-tools"
            await model.reload(client:first,currentClient:{current})
            if mode=="tool-error" {
                require(model.items?.first?.name=="fresh" && model.queueError==nil && model.toolsError != nil,"tool failure must not hide pending queue")
            } else {
                require(model.tools.first?.name=="fresh" && model.toolsError==nil && model.queueError != nil,"queue failure must not hide saved tools")
            }
        } else if mode=="slow-tools" {
            ListingResponses.suffixToHold="/api-tools"
            let task=Task {await model.reload(client:first,currentClient:{current})}
            await wait {model.items?.first?.name=="fresh" && ListingResponses.count==1}
            require(!model.loadingQueue && model.loadingTools,"healthy queue renders before slow tool catalog")
            ListingResponses.release(failed:true);await task.value
            require(model.items?.first?.name=="fresh","later tool failure preserves ready queue")
        } else if mode=="overlap" || mode=="client-change" || mode=="closed" {
            ListingResponses.holding=true
            let old=Task {await model.reload(client:first,currentClient:{current})}
            await wait {ListingResponses.count==2}
            ListingResponses.holding=false
            if mode=="closed" {model.invalidate()}
            else {
                if mode=="client-change" {current=second}
                await model.reload(client:current,currentClient:{current})
                require(model.items?.first?.name=="fresh" && model.tools.first?.name=="fresh","latest listing has completed")
            }
            ListingResponses.release(failed:mode != "overlap");await old.value
            if mode=="closed" {
                require(model.items==nil && model.tools.isEmpty && model.queueError==nil && model.toolsError==nil,"closed panel consumes no late result")
            } else {
                require(model.items?.first?.name=="fresh" && model.tools.first?.name=="fresh" && model.queueError==nil && model.toolsError==nil,"old listing cannot overwrite newer projection or error")
            }
        } else if mode=="retain" {
            await model.reload(client:first,currentClient:{current})
            ListingResponses.failedPath="all"
            await model.reload(client:first,currentClient:{current})
            require(model.items?.first?.name=="fresh" && model.tools.first?.name=="fresh" && model.queueError != nil && model.toolsError != nil,"failed refresh keeps both last successful projections")
        } else if mode=="nil" {
            await model.reload(client:nil,currentClient:{nil})
            require(!model.loadingQueue && !model.loadingTools && model.queueError != nil,"unconfigured connection is not an endless loading state")
        } else {require(false,"unknown listing mode")}
        print("PASS \(mode)")
    }
}
