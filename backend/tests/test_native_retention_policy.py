"""Production native API/session-index projection with isolated fake transport/cache."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")
def test_native_retention_and_unsaved_pin(tmp_path):
    source = ROOT / "apple-app/Sources"
    header = (ROOT / "apple-app/Tests/recovery_transport_test.swift").read_text().split("@main struct RecoveryTests")[0]
    harness = header + r'''
    @main struct Check {
        @MainActor static func main() async throws {
            var response: [String: Any] = ["status":true,"sessions":[],"retention_policy":
                ["enabled":true,"retention_days":7.5,"empty_ttl_hours":3,"scan_interval_seconds":120]]
            RecoveryTransport.handler = { t in
                if t.request.url!.path.hasSuffix("/skills") { t.respond(["status":true,"skills":[]]) }
                else { t.respond(response) }
            }
            let store = AgentStore(settings:AppSettings())
            await store.loadSessions()
            precondition(store.retentionPolicy?.retentionDays == 7.5)
            precondition(store.retentionPolicy?.emptyTTLHours == 3)
            response = ["status":true,"sessions":[]]
            await store.loadSessions()
            precondition(store.retentionPolicy == nil)
            response = ["status":false,"response":"synthetic save failure"]
            do {
                _ = try await store.client!.setPin(sessionId:"synthetic", pinned:true)
                preconditionFailure("unsaved pin accepted")
            } catch AgentError.http(let code, _) {
                precondition(code == 409)
            }
        }
    }
    '''
    mapped = tmp_path / "retention.swift"
    mapped.write_text(harness)
    binary = tmp_path / "retention"
    result = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(tmp_path / "modules"),
        *map(str, sorted((source / "API").glob("*.swift"))),
        *map(str, sorted((source / "Store").glob("Offline*.swift"))),
        str(source / "Store/TurnReducer.swift"), str(source / "Store/ChatMessage.swift"),
        str(source / "Store/AgentStore.swift"), str(ROOT / "apple-app/Tests/recovery_support.swift"),
        str(mapped), "-o", str(binary)], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
