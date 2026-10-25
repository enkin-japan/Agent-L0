"""Exercise real shell startup functions without starting services or models."""
import os
from pathlib import Path
import subprocess


LIB = Path(__file__).resolve().parents[2] / "scripts/lib_edge.sh"


def run_shell(tmp_path, body, lib=LIB):
    env = {**os.environ, "EDGE_BACKEND_DIR": str(tmp_path), "EDGE_BACKEND_PY": "/usr/bin/true",
           "EDGE_SEARX_DIR": str(tmp_path), "EDGE_SEARX_PY": "/usr/bin/true",
           "EDGE_SEARX_SETTINGS": str(tmp_path / "none"), "EDGE_LOG_DIR": str(tmp_path),
           "EDGE_CONFIG": str(tmp_path / "config.yaml")}
    return subprocess.run(["bash", "-c", 'source "$1"\n' + body, "_", str(lib)],
                          env=env, text=True, capture_output=True)


def test_backend_data_service_does_not_wait_for_compute(tmp_path):
    result = run_shell(tmp_path, r'''
is_ready() { return 1; }
guard_port_before_spawn() { return 0; }
probe_auth() { echo UNEXPECTED_COMPUTE_PROBE; return 1; }
spawn_daemon() { echo DATA_SPAWN; echo 1 > "$EDGE_LOG_DIR/backend.pid"; }
wait_ready() { echo DATA_READY; return 0; }
INFER_MODE=remote
INFER_HOST=""
start_backend
''')
    assert result.returncode == 0, result.stderr
    assert "DATA_SPAWN" in result.stdout and "DATA_READY" in result.stdout
    assert "UNEXPECTED_COMPUTE_PROBE" not in result.stdout


def test_optional_service_failure_preserves_data_service(tmp_path):
    result = run_shell(tmp_path, r'''
INFER_MODE=remote
start_backend() { echo DATA_FIRST; return 0; }
rag_is_local() { return 0; }
start_rag_local() { echo RAG_LATER; return 1; }
start_searx() { echo SEARCH_LATER; return 1; }
edge_up
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.index("DATA_FIRST") < result.stdout.index("RAG_LATER")
    assert result.stdout.index("DATA_FIRST") < result.stdout.index("SEARCH_LATER")


def test_backend_failure_does_not_start_extra_services(tmp_path):
    result = run_shell(tmp_path, r'''
INFER_MODE=remote
start_backend() { return 1; }
start_rag_local() { echo UNEXPECTED; }
start_searx() { echo UNEXPECTED; }
edge_up
''')
    assert result.returncode != 0
    assert "UNEXPECTED" not in result.stdout


def test_inprocess_preserves_dependency_registration_order(tmp_path):
    result = run_shell(tmp_path, r'''
INFER_MODE=inprocess
start_backend() { echo BACKEND; }
rag_is_local() { return 0; }
start_rag_local() { echo RAG; }
start_searx() { echo SEARCH; }
edge_up
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["RAG", "SEARCH", "BACKEND"]
