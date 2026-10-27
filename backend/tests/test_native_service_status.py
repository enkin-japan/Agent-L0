"""Service reachability/authentication and capability projection use synthetic transport."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")
def test_native_service_status_and_knowledge_management(tmp_path):
    source = ROOT / "apple-app/Sources"
    view = (source / "Views/KnowledgePanelView.swift").read_text()
    reload = view[view.index("    private func reload() async {"):view.index("    private var capabilityLabel:")]
    reload = reload.replace("private func reload()", "func reload()")
    harness = '''import Foundation
    func L(_ value: String) -> String { value }
    final class SyntheticTransport: URLProtocol {
        static var healthCode = 200
        static var healthBody = "{}"
        static var indexCode = 200
        static var indexBody = #"{"status":true,"sessions":[]}"#
        static var knowledge = #"{"compute":null,"compute_error":"synthetic unavailable","rebuild":{"state":"idle"}}"#
        static var indexCalls = 0
        static var overviewCode = 200
        static var overviewBody = #"{"categories":[{"name":"kept","description":"fixture","doc_count":1,"chunk_count":1}]}"#
        static let barrier = NSLock()
        static var pauseOverview = false
        static var heldOverview: SyntheticTransport?
        static var overviewCalls = 0
        static func overviewState() -> (SyntheticTransport?, Int) {
            barrier.lock(); defer { barrier.unlock() }
            return (heldOverview, overviewCalls)
        }
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func stopLoading() {}
        override func startLoading() {
            let path = request.url!.path
            var code = 200, body = "{}"
            if path == "/health" { code = Self.healthCode; body = Self.healthBody }
            else if path == "/api/v1/sessions" {
                Self.indexCalls += 1; code = Self.indexCode; body = Self.indexBody
                precondition(request.timeoutInterval == 5)
                precondition(request.value(forHTTPHeaderField: "Authorization") == "Bearer synthetic")
            } else if path == "/api/v1/rag/retier" { body = Self.knowledge }
            else if path == "/api/v1/rag/overview" {
                Self.barrier.lock()
                Self.overviewCalls += 1
                let pause = Self.pauseOverview
                if pause { Self.heldOverview = self }
                Self.barrier.unlock()
                if pause { return }
                body = Self.overviewBody; code = Self.overviewCode
            } else { preconditionFailure("unexpected synthetic path") }
            respond(code: code, body: body)
        }
        func respond(code: Int, body: String) {
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: code, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8)); client?.urlProtocolDidFinishLoading(self)
        }
    }
    @MainActor final class Store { var client: AgentClient?; init(_ client: AgentClient) { self.client = client } }
    @MainActor final class Panel {
        let store: Store
        var categories: [RagCategory]?
        var offline = false
        var overviewStale = false
        var serviceStatus: RagRetierStatus?
        var reloadID = UUID()
        var refreshing = false
        init(_ client: AgentClient) { store = Store(client) }
    ''' + reload + '''
    }
    @main struct Check {
        @MainActor static func main() async throws {
            let config = URLSessionConfiguration.ephemeral
            config.protocolClasses = [SyntheticTransport.self]
            let api = AgentClient(baseURL: URL(string: "https://fixture.invalid")!, apiKey: "synthetic", session: URLSession(configuration: config))
            for code in [401, 403] {
                SyntheticTransport.indexCode = code
                let access = await api.backendAccess()
                precondition(access == .unauthorized, "health200 cannot prove authentication")
            }
            SyntheticTransport.indexCode = 503
            let reachable = await api.backendAccess(); precondition(reachable == .reachable)
            SyntheticTransport.indexCode = 200
            SyntheticTransport.indexBody = #"{"status":false,"sessions":[]}"#
            let rejected = await api.backendAccess(); precondition(rejected == .reachable)
            SyntheticTransport.indexBody = #"{"status":true,"sessions":[]}"#
            let authenticated = await api.backendAccess(); precondition(authenticated == .authenticated)
            for (json, expected) in [
                (#"{"compute_ready":true,"compute_state":"ready"}"#, ComputeAvailability.ready),
                (#"{"compute_ready":false,"compute_state":"starting"}"#, .starting),
                (#"{"compute_ready":false,"compute_state":"retrying"}"#, .unavailable),
                (#"{"compute_ready":false,"compute_state":"unavailable"}"#, .unavailable),
                (#"{"compute_state":"ready"}"#, .unknown),
                (#"{}"#, .unsupported),
                (#"invalid"#, .unknown),
            ] {
                SyntheticTransport.healthBody = json
                let before = SyntheticTransport.indexCalls
                let report = await api.connectionStatus()
                precondition(report.access == .authenticated && report.compute == expected)
                precondition(SyntheticTransport.indexCalls == before + 1)
            }
            SyntheticTransport.healthBody = #"{"compute_ready":true}"#
            for code in [401, 403, 503] {
                SyntheticTransport.indexCode = code
                let report = await api.connectionStatus()
                precondition(report.compute == .unknown, "public health cannot grant compute readiness")
            }
            SyntheticTransport.indexCode = 200
            SyntheticTransport.healthCode = 503
            let count = SyntheticTransport.indexCalls
            let offline = await api.backendAccess(); precondition(offline == .offline)
            precondition(count == SyntheticTransport.indexCalls)
            let report = await api.connectionStatus(); precondition(report.compute == .unknown)
            let panel = Panel(api)
            await panel.reload()
            precondition(panel.categories?.first?.name == "kept" && !panel.offline && !panel.overviewStale)
            precondition(panel.serviceStatus?.availability == .computeOffline)
            for (json, expected) in [
                (#"{"rebuild":{"state":"idle"}}"#, RagRetierStatus.Availability.unknown),
                (#"{"compute":{"tier":"fake"},"rebuild":{"state":"idle"}}"#, .reachable),
                (#"{"compute":{"tier":"fake"},"needs_rebuild":true,"rebuild":{"state":"idle"}}"#, .needsRebuild),
                (#"{"compute":{"tier":"fake"},"rebuild":{"state":"running"}}"#, .rebuilding),
            ] {
                let decoded = try JSONDecoder().decode(RagRetierStatus.self, from: Data(json.utf8))
                precondition(decoded.availability == expected)
            }
            SyntheticTransport.knowledge = "invalid response"
            await panel.reload()
            precondition(panel.categories?.first?.name == "kept" && !panel.offline)
            precondition(panel.serviceStatus == nil)
            SyntheticTransport.overviewCode = 503
            await panel.reload()
            precondition(!panel.offline && panel.categories?.first?.name == "kept" && panel.overviewStale && !panel.refreshing)
            let emptyPanel = Panel(api)
            await emptyPanel.reload()
            precondition(emptyPanel.offline && emptyPanel.categories == nil && !emptyPanel.overviewStale && !emptyPanel.refreshing)
            SyntheticTransport.overviewCode = 200
            SyntheticTransport.overviewBody = #"{"categories":[]}"#
            SyntheticTransport.knowledge = #"{"rebuild":{"state":"idle"}}"#
            await panel.reload()
            precondition(!panel.offline && panel.categories?.isEmpty == true && !panel.overviewStale && !panel.refreshing)
            precondition(panel.serviceStatus?.availability == .unknown)
            SyntheticTransport.knowledge = #"{"compute":{"tier":"fake"},"rebuild":{"state":"idle"}}"#
            await panel.reload()
            precondition(panel.serviceStatus?.availability == .reachable)
            SyntheticTransport.pauseOverview = true
            let first = Task { await panel.reload() }
            for _ in 0..<400 {
                if SyntheticTransport.overviewState().0 != nil { break }
                try? await Task.sleep(nanoseconds: 5_000_000)
            }
            let held = SyntheticTransport.overviewState()
            precondition(held.0 != nil)
            var secondFinished = false
            let second = Task { await panel.reload(); secondFinished = true }
            for _ in 0..<400 {
                if secondFinished || SyntheticTransport.overviewState().1 > held.1 { break }
                try? await Task.sleep(nanoseconds: 5_000_000)
            }
            precondition(secondFinished && SyntheticTransport.overviewState().1 == held.1, "overlapping refresh must not start duplicate requests")
            SyntheticTransport.pauseOverview = false
            held.0!.respond(code: 200, body: SyntheticTransport.overviewBody)
            await first.value; await second.value
            precondition(!panel.refreshing)
        }
    }
    '''
    test = tmp_path / "service-status.swift"
    test.write_text(harness)
    binary = tmp_path / "service-status"
    result = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(tmp_path / "modules"),
        *[str(p) for p in sorted((source / "API").glob("*.swift"))],
        *map(str,sorted((source / "Store").glob("Offline*.swift"))), str(source / "Store/TurnReducer.swift"), str(test), "-o", str(binary)],
        capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


# Contract H (build 78) projection oracles. Pure: BackendConnectionStatus is built from
# decoded /health bodies, no transport. Each line is a mutation target (see the precondition
# messages): node "unauthorized" must never read as ready, "local" defers to compute_state,
# stopping/stopped wins over a ready node, and only real startup polls fast.
CONTRACT_H_HARNESS = r'''
import Foundation
func L(_ value: String) -> String { value }
@main struct ContractH {
    static func status(_ json: String, _ access: BackendAccess = .authenticated) -> BackendConnectionStatus {
        BackendConnectionStatus(access: access, health: try! JSONDecoder().decode(BackendHealth.self, from: Data(json.utf8)))
    }
    static func main() {
        func check(_ ok: Bool, _ what: String) { if !ok { print("FAIL " + what); exit(1) } }
        check(status(#"{"compute_ready":true,"compute_state":"ready","compute_node_state":"unauthorized"}"#).compute == .unauthorized,
              "node unauthorized is never ready")
        check(status(#"{"compute_ready":true,"compute_state":"ready","compute_node_state":"unreachable"}"#).compute == .unreachable,
              "node unreachable is never ready")
        check(status(#"{"compute_ready":false,"compute_state":"starting","compute_node_state":"probing"}"#).compute == .probing,
              "node probing reads as connecting")
        check(status(#"{"compute_ready":true,"compute_state":"ready","compute_node_state":"ready"}"#).compute == .ready,
              "node ready reads as ready")
        check(status(#"{"compute_ready":false,"compute_state":"starting","compute_node_state":"local"}"#).compute == .starting,
              "local mode defers to compute_state starting")
        check(status(#"{"compute_ready":true,"compute_state":"ready","compute_node_state":"local"}"#).compute == .ready,
              "local mode defers to compute_state ready")
        check(status(#"{"compute_ready":false,"compute_state":"stopped","compute_node_state":"ready"}"#).compute == .unavailable,
              "stopped assembly is unavailable even with a ready node")
        check(status(#"{"compute_ready":true,"compute_state":"stopping","compute_node_state":"ready"}"#).compute == .unavailable,
              "stopping assembly is unavailable even with a ready node")
        check(status(#"{"compute_ready":true,"compute_state":"ready","compute_node_state":"unauthorized"}"#, .unauthorized).compute == .unknown,
              "unauthenticated access never projects compute")
        check(status(#"{"engine_ready":false}"#).engineStarting, "engine_ready false is starting")
        check(!status(#"{"compute_ready":true,"compute_state":"ready"}"#).engineStarting, "missing engine_ready is not starting")
        check(status(#"{"engine_ready":false,"rag_state":"unauthorized"}"#).ragNotice == "unauthorized", "rag unauthorized notice")
        check(status(#"{"engine_ready":false,"rag_state":"unreachable"}"#).ragNotice == "unreachable", "rag unreachable notice")
        check(status(#"{"engine_ready":false,"rag_state":"skipped"}"#).ragNotice == nil, "rag skipped has no notice")
        check(status(#"{"engine_ready":true,"compute_node_state":"probing"}"#).pollIntervalSeconds == 2, "probing polls at 2 s")
        check(status(#"{"engine_ready":false,"compute_node_state":"ready"}"#).pollIntervalSeconds == 2, "engine_ready false polls at 2 s")
        check(status(#"{"engine_ready":true,"compute_node_state":"unreachable"}"#).pollIntervalSeconds == 15, "unreachable node polls at 15 s")
        check(status(#"{"engine_ready":true,"compute_node_state":"unauthorized"}"#).pollIntervalSeconds == 15, "unauthorized node polls at 15 s")
        check(status(#"{"engine_ready":true,"compute_node_state":"ready"}"#).pollIntervalSeconds == 15, "ready polls at 15 s")
        check(status(#"{"engine_ready":false,"compute_node_state":"unreachable"}"#).pollIntervalSeconds == 15,
              "engine down with a settled unreachable node polls at 15 s")
        check(status(#"{"engine_ready":false,"compute_node_state":"unauthorized"}"#).pollIntervalSeconds == 15,
              "engine down with a settled unauthorized node polls at 15 s")
        check(status(#"{"engine_ready":false,"compute_node_state":"ready","rag_state":"unauthorized"}"#).pollIntervalSeconds == 15,
              "engine down with a settled RAG failure polls at 15 s")
        check(status(#"{"engine_ready":false,"compute_node_state":"local"}"#).pollIntervalSeconds == 2,
              "engine still installing on a local node polls at 2 s")
        check(status(#"{"engine_ready":false,"compute_node_state":"ready","rag_state":"unreachable"}"#).pollIntervalSeconds == 2,
              "local RAG not up yet is a cold start, not settled: polls at 2 s")
        check(status(#"{"compute_node_state":"probing","rag_state":"unauthorized"}"#).pollIntervalSeconds == 2,
              "a pending node probe polls at 2 s whatever the RAG state")
        check(status(#"{"compute_ready":true,"compute_state":"ready"}"#).pollIntervalSeconds == 15, "nil fields poll at 15 s")
        check(status(#"{}"#, .offline).pollIntervalSeconds == 2, "offline polls at 2 s")
        check(status(#"{"engine_ready":false}"#, .unauthorized).pollIntervalSeconds == 15, "unauthenticated never fast-polls")
        print("PASS contract-h")
    }
}
'''


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")
def test_native_contract_h_projection(tmp_path):
    source = ROOT / "apple-app/Sources"
    test = tmp_path / "contract-h.swift"
    test.write_text(CONTRACT_H_HARNESS)
    binary = tmp_path / "contract-h"
    result = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(tmp_path / "modules"),
        *[str(p) for p in sorted((source / "API").glob("*.swift"))],
        *map(str, sorted((source / "Store").glob("Offline*.swift"))), str(source / "Store/TurnReducer.swift"),
        str(test), "-o", str(binary)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
