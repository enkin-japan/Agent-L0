"""Compile the actual native configuration reader against a synthetic launcher environment."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")
def test_native_effective_configuration(tmp_path):
    main = tmp_path / "main.swift"
    main.write_text('''import Foundation
    @main struct Check {
        static func main() async throws {
            let args = CommandLine.arguments
            let config = URL(fileURLWithPath: args[2])
            var env = ["PATH": "/usr/bin:/bin", "EDGE_CONFIG": config.path,
                       "EDGE_BACKEND_PY": args[3], "AGENT_LAYOUT": "bundle",
                       "AGENT_API_KEY": "synthetic-never-report"]
            for key in ["EDGE_BACKEND_DIR", "EDGE_SEARX_DIR", "EDGE_SEARX_PY",
                        "EDGE_SEARX_SETTINGS", "EDGE_LOG_DIR"] { env[key] = args[4] + "/" + key }
            let lib = URL(fileURLWithPath: args[1])
            try "inference: {mode: remote}\\nrag: {host: 127.0.0.1}\\n".write(to: config, atomically: true, encoding: .utf8)
            let missing = try await EdgeConfiguration.read(lib: lib, environment: env)
            precondition(missing.incomplete && Set(missing.missing) == ["inference_host", "rag_compute_host"])
            env["INFER_REMOTE_HOST"] = "fixture.invalid"
            env["RAG_COMPUTE_HOST"] = "fixture.invalid"
            let configured = try await EdgeConfiguration.read(lib: lib, environment: env)
            precondition(configured.status == "configured" && !configured.incomplete)
            try "broken: [".write(to: config, atomically: true, encoding: .utf8)
            let unknown = try await EdgeConfiguration.read(lib: lib, environment: env)
            precondition(unknown.status == "unknown")
            precondition(!FileManager.default.fileExists(atPath: env["EDGE_LOG_DIR"]!))
        }
    }
    ''')
    binary = tmp_path / "configuration"
    result = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(tmp_path / "modules"),
        str(ROOT / "apple-app/Sources/App/EdgeConfiguration.swift"), str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary), str(ROOT / "scripts/lib_edge.sh"), str(tmp_path / "config.yaml"),
                             sys.executable, str(tmp_path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "synthetic-never-report" not in result.stdout + result.stderr
