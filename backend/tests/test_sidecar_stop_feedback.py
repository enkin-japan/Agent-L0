"""Exercise production stop/restart control with a failing synthetic library run."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")
def test_stop_failure_is_visible_and_prevents_restart(tmp_path):
    source = (ROOT / "apple-app/Sources/App/SidecarManager.swift").read_text()
    def method(start, end):
        return source[source.index(start):source.index(end, source.index(start))]
    methods = method("    func startStop()", "    /// 设置里改了服务密钥")
    methods += method("    func restart()", "    /// app 退出路径")
    methods += method("    private func down()", "    /// kqueue")
    harness = '''import Foundation
    struct Run {
        var code: Int
        func waitAsync() async -> Int { code }
        func outputTail(lines: Int) -> String { "synthetic ownership refusal" }
    }
    @MainActor final class Harness {
        enum State: Equatable { case running, starting, disabled, external, failed(String) }
        var state = State.running
        var layout: Int? = 1
        var code = 1
        var starts = 0
        func cancelWatchers() {}
        func runLib(_ layout: Int, command: String) throws -> Run { Run(code: code) }
        func autostart(force: Bool = false) async { starts += 1 }
    ''' + methods + '''
    }
    @main struct Check {
        @MainActor static func main() async {
            let stopped = Harness()
            await stopped.startStop()
            precondition(stopped.state == .failed("synthetic ownership refusal"))
            let restart = Harness()
            await restart.restart()
            precondition(restart.starts == 0)
            precondition(restart.state == .failed("synthetic ownership refusal"))
            let success = Harness(); success.code = 0
            await success.startStop()
            precondition(success.state == .disabled)
            let successRestart = Harness(); successRestart.code = 0
            await successRestart.restart()
            precondition(successRestart.starts == 1)
        }
    }
    '''
    test = tmp_path / "stop.swift"
    test.write_text(harness)
    binary = tmp_path / "stop"
    compiled = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path",
        str(tmp_path / "modules"), str(test), "-o", str(binary)], capture_output=True, text=True, timeout=90)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
