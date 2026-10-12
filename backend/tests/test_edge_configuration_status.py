"""Read-only effective configuration report; never start a service or load a model."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def report(tmp_path, text, **overrides):
    config = tmp_path / "config.yaml"
    config.write_text(text)
    env = {k: v for k, v in os.environ.items()
           if k not in {"INFER_REMOTE_HOST", "RAG_COMPUTE_HOST", "AGENT_LAYOUT"}}
    env.update({key: str(tmp_path / key) for key in (
        "EDGE_BACKEND_DIR", "EDGE_SEARX_DIR", "EDGE_SEARX_PY",
        "EDGE_SEARX_SETTINGS", "EDGE_LOG_DIR")})
    env.update(EDGE_BACKEND_PY=sys.executable, EDGE_CONFIG=str(config),
               AGENT_API_KEY="synthetic-secret-never-report")
    env.update(overrides)
    result = subprocess.run(["bash", str(ROOT / "scripts/lib_edge.sh"), "configuration"],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "synthetic-secret" not in result.stdout + result.stderr
    assert not Path(env["EDGE_LOG_DIR"]).exists(), "configuration query must not start services"
    return json.loads(result.stdout)


@pytest.mark.parametrize("override,default,expected", [
    ("", "", "incomplete"), ("", "fixture.invalid", "configured"),
    ("override.invalid", "", "configured"), ("  ", "fixture.invalid", "configured"),
])
def test_remote_host_uses_same_effective_configuration(tmp_path, override, default, expected):
    data = report(tmp_path, f"inference:\n  mode: remote\n  remote:\n    host: '{default}'\n",
                  INFER_REMOTE_HOST=override)
    assert data["status"] == expected
    assert data["missing"] == (["inference_host"] if expected == "incomplete" else [])


def test_blank_bundle_requires_local_rag_compute_address(tmp_path):
    data = report(tmp_path, "inference: {mode: remote, remote: {host: fixture.invalid}}\nrag: {host: 127.0.0.1}\n",
                  AGENT_LAYOUT="bundle")
    assert data["status"] == "incomplete"
    assert data["missing"] == ["rag_compute_host"]


@pytest.mark.parametrize("layout,rag_host,compute", [
    ("repo", "127.0.0.1", ""), ("bundle", "remote.invalid", ""),
    ("bundle", "127.0.0.1", "compute.invalid"),
])
def test_supported_fused_external_and_split_configurations(tmp_path, layout, rag_host, compute):
    data = report(tmp_path, f"inference: {{mode: inprocess}}\nrag: {{host: '{rag_host}'}}\n",
                  AGENT_LAYOUT=layout, RAG_COMPUTE_HOST=compute)
    assert data["status"] == "configured"
    assert data["rag_compute_remote"] == bool(compute)


def test_unreadable_configuration_is_unknown_not_configured(tmp_path):
    assert report(tmp_path, "inference: [broken")["status"] == "unknown"
