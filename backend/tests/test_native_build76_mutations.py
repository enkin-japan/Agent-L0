"""Guard proofs for the build 76 review fixes: each mutant must turn its native test red.

apple-app/Tests/run_build76_review_mutations.sh edits copies only (scratch directory);
the production Swift sources are never touched. CPU only, synthetic transport, no network.
"""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")


def test_native_build76_guard_mutations():
    result = subprocess.run(["bash", "apple-app/Tests/run_build76_review_mutations.sh"], cwd=ROOT,
                            capture_output=True, text=True, timeout=1500)
    assert result.returncode == 0, result.stdout + result.stderr
