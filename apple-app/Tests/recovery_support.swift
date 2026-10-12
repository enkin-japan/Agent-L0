// System dependencies only; full production Store/API/SSE compile unchanged.
@_exported import Combine
import Foundation
func L(_ value: String) -> String { value }
@MainActor final class AppSettings {
    var setupCompleted = false
    var baseURLString = "https://fixture.invalid"
    func makeClient() -> AgentClient? {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [RecoveryTransport.self]
        return AgentClient(baseURL: URL(string: baseURLString)!, apiKey: "synthetic", session: URLSession(configuration: config))
    }
}
@MainActor final class PushManager {
    enum Kind { case scheduleStart, approvalAutodenied, approval(tools: [String]), result(status: String) }
    func postLocal(kind: Kind, sessionId: String, turnId: String, sessionTitle: String, fullBody: String? = nil) { fatalError("notifications forbidden") }
}
final class LocationProvider {
    static let shared = LocationProvider()
    enum Failure: Error { case noFix(String) }
    struct Fix { let latitude: Double; let longitude: Double; let place: String; let accuracyM: Double }
    func currentFix() async throws -> Fix { throw Failure.noFix("synthetic only") }
}

// Test scheduler gate: used only by the separately instrumented full-source build.
// No store/API methods are replaced; this delays a task's deferred cleanup callback.
@MainActor enum RecoveryCleanupGate {
    static var enabled = false
    static var pending: CheckedContinuation<Void, Never>?
    static var heldOnce = false
    static func wait() async {
        guard enabled && !heldOnce else { return }
        heldOnce = true
        await withCheckedContinuation { pending = $0 }
    }
    static func release() { pending?.resume(); pending = nil }
}
