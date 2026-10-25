// System dependencies only; full production Store/API/SSE compile unchanged.
@_exported import Combine
import Foundation
func L(_ value: String) -> String { value }
/// Expected Swift runtime traps (mutants that deliberately break a bound check) end with exit
/// code 128+signal instead of a crash: these binaries run inside a test-only .app bundle, so a
/// real crash files a report and pops the system "quit unexpectedly" dialog on every test run.
/// Only trap signals are taken over; abort/segfault/bus errors stay real crashes with reports.
/// Call it only from harnesses with a mutant that is expected to trap (today: thin recovery).
func exitOnFatalSignals() {
    for sig in [SIGTRAP, SIGILL] { signal(sig) { _exit(128 + $0) } }
}
let nativeOfflineCacheRoot = FileManager.default.temporaryDirectory.appendingPathComponent("agent-native-tests-" + UUID().uuidString, isDirectory: true)
@MainActor final class AppSettings {
    var setupCompleted = false
    var baseURLString = "https://fixture.invalid"
    /// Production limits by default; a case that exercises the mirroring budget
    /// sets its own before building the store.
    var mirrorLimits: OfflineAssetStore.MirrorLimits = .production
    func makeClient() -> AgentClient? {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [RecoveryTransport.self]
        return AgentClient(baseURL: URL(string: baseURLString)!, apiKey: "synthetic", session: URLSession(configuration: config), cacheRoot: nativeOfflineCacheRoot, mirrorLimits: mirrorLimits)
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
