"""Production native API and session-index projection, with local fake transport."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")
def test_native_retention_and_unsaved_pin(tmp_path):
    source = ROOT / "apple-app/Sources"
    store = (source / "Store/AgentStore.swift").read_text()
    method = store[store.index("    func loadSessions()") : store.index("    /// 读上次的列表快照")]
    harness = '''import Foundation
    func L(_ s: String) -> String { s }
    final class RetentionTransport: URLProtocol {
        static var response = ""
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func stopLoading() {}
        override func startLoading() {
            let body = request.url!.path.hasSuffix("/skills") ? "{\\"status\\":true,\\"skills\\":[]}" : Self.response
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        }
    }
    @MainActor final class Harness {
        var client: AgentClient? = AgentClient(baseURL: URL(string: "https://fixture.invalid")!, apiKey: "synthetic")
        var sessions: [SessionMetaItem] = []
        var retentionPolicy: SessionRetentionPolicy?
        var errorMessage: String?
        var skills: [SkillItem] = []
        func saveSessionsSnapshot() {}
    ''' + method + '''
    }
    @main struct Check {
        @MainActor static func main() async throws {
            RetentionTransport.response = #"{"status":true,"sessions":[],"retention_policy":{"enabled":true,"retention_days":7.5,"empty_ttl_hours":3,"scan_interval_seconds":120}}"#
            let store = Harness()
            await store.loadSessions()
            precondition(store.retentionPolicy?.retentionDays == 7.5)
            precondition(store.retentionPolicy?.emptyTTLHours == 3)
            RetentionTransport.response = #"{"status":true,"sessions":[]}"#
            await store.loadSessions()
            precondition(store.retentionPolicy == nil)
            RetentionTransport.response = #"{"status":false,"response":"synthetic save failure"}"#
            do {
                _ = try await store.client!.setPin(sessionId: "synthetic", pinned: true)
                preconditionFailure("unsaved pin accepted")
            } catch AgentError.http(let code, _) {
                precondition(code == 409)
            }
        }
    }
    '''
    mapped = tmp_path / "retention.swift"
    mapped.write_text(harness)
    client = tmp_path / "AgentClient.swift"
    client.write_text((source / "API/AgentClient.swift").read_text().replace(
        "let config = URLSessionConfiguration.default",
        "let config = URLSessionConfiguration.ephemeral\n        config.protocolClasses = [RetentionTransport.self]"))
    binary = tmp_path / "retention"
    result = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(tmp_path / "modules"),
        *[str(p) for p in sorted((source / "API").glob("*.swift")) if p.name != "AgentClient.swift"],
        str(client), str(source / "Store/TurnReducer.swift"), str(mapped), "-o", str(binary)],
        capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
