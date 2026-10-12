import Foundation
func L(_ value: String) -> String { value }
final class SetupTransport: URLProtocol {
    static let lock = NSLock()
    static var seen = 0
    static var held: SetupTransport?
    static var hold = false
    static var fail = false
    static var handler: ((SetupTransport) -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock(); Self.seen += 1; let hold = Self.hold
        if hold { Self.held = self }; Self.lock.unlock()
        if let handler = Self.handler { handler(self) }
        else if !hold { respond() }
    }
    override func stopLoading() {}
    func respond(body: String = "{\"status\":true,\"effect\":\"registered\"}", status: Int? = nil) {
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status ?? (Self.fail ? 503 : 200), httpVersion: nil, headerFields: [:])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
}
func require(_ value: @autoclosure () -> Bool, _ text: String) { if !value() { print("FAIL: \(text)"); exit(1) } }
@main struct SetupTests {
    @MainActor static func main() async throws {
        let mode = CommandLine.arguments[1]
        if mode == "mapping" {
            let item = try JSONDecoder().decode(ApiToolItem.self, from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2])))
            print(item.needsSetup ? "pending" : "complete")
            return
        }
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [SetupTransport.self]
        let client = AgentClient(baseURL: URL(string: "https://fixture.invalid")!, apiKey: "synthetic", session: URLSession(configuration: config))
        var current = mode != "before"
        let opened = UUID()
        var presentation: UUID? = opened
        let operation = ApiToolOperation.editor(client: mode == "nil" ? nil : client,
            connectionIsCurrent: { current }, presentationIsCurrent: { presentation == opened })
        let body = AgentClient.ApiToolSaveBody(name: "fixture", description: "fixture", endpoint: "https://fixture.invalid/action", method: "POST", enabled: true)
        let delayed = ["late", "late-error", "closed", "reopened"].contains(mode)
        SetupTransport.hold = delayed
        SetupTransport.fail = mode.hasSuffix("error")
        var succeeded = false
        var consumedError = false
        let task = Task { @MainActor in
            do { _ = try await operation.perform { try await $0.saveApiTool(body) }; succeeded = true }
            catch is CancellationError { }
            catch { consumedError = true }
        }
        if delayed {
            for _ in 0..<400 {
                if SetupTransport.held != nil { break }
                try? await Task.sleep(nanoseconds: 5_000_000)
            }
            require(SetupTransport.held != nil, "actual request was held")
            if mode == "closed" { presentation = nil }
            else if mode == "reopened" { presentation = UUID() }
            else { current = false }
            SetupTransport.held!.respond()
        }
        await task.value
        if mode == "current" {
            require(succeeded && SetupTransport.seen == 1, "current form saves exactly once")
        } else if mode == "current-error" {
            require(consumedError && !succeeded, "current request error stays visible")
        } else if delayed {
            require(!succeeded && !consumedError && SetupTransport.seen == 1, "late result cannot advance another connection's form")
        } else {
            require(!succeeded && SetupTransport.seen == 0, "stale or missing form cannot write to a backend")
        }
        print("PASS \(mode)")
    }
}
