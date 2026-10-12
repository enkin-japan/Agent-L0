"""The actual CLI dispatch must propagate any stop failure, including restart."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("profile, services", [
    ("edge", ["stop_front", "edge_down"]),
    ("gpu", ["stop_infer", "stop_rag_compute", "stop_rag"]),
    ("", ["stop_front", "stop_backend", "stop_infer", "stop_searx", "stop_rag", "stop_rag_compute"]),
])
@pytest.mark.parametrize("action", ["stop", "restart"])
def test_every_stop_failure_is_propagated(tmp_path, profile, services, action):
    source = (ROOT / "devctl.sh").read_text()
    stop = source[source.index("    stop)\n"):source.index("    bundle-print)\n")]
    helper = ""
    if "stop_services() {" in source:
        start = source.index("stop_services() {")
        helper = source[start:source.index("\n}\n", start) + 3]
    script = tmp_path / "dispatch"
    stubs = "\n".join(f'{s}() {{ echo {s}; [ "$FAILED_SERVICE" != "{s}" ]; }}' for s in
        ["stop_front", "edge_down", "stop_infer", "stop_rag_compute", "stop_rag", "stop_backend", "stop_searx"])
    script.write_text('#!/bin/bash\nPROFILE="${PROFILE:-}"\nTARGET=all\n' + stubs + "\n" + helper +
                      '\ncase "$1" in\n' + stop + '    start) echo START_WAS_CALLED;;\nesac\n')
    script.chmod(0o700)
    for failed in [*services, "none"]:
        result = subprocess.run([str(script), action], env={"PROFILE": profile, "FAILED_SERVICE": failed},
                                capture_output=True, text=True, timeout=10)
        assert (result.returncode == 0) == (failed == "none"), result.stdout + result.stderr
        assert all(s in result.stdout for s in services)
        assert ("START_WAS_CALLED" in result.stdout) == (action == "restart" and failed == "none")
