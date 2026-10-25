"""Durable native reading projection; synthetic transport, isolated disk, CPU only."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")


def test_native_offline_history():
    result = subprocess.run(["bash", "apple-app/Tests/run_offline_history.sh"], cwd=ROOT,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_offline_history_guard_mutations():
    result = subprocess.run(["bash", "apple-app/Tests/run_offline_history_mutations.sh"], cwd=ROOT,
                            capture_output=True, text=True, timeout=900)
    assert result.returncode == 0, result.stdout + result.stderr
