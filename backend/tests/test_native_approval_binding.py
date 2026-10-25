"""CPU-only native approval tests: complete production Store/API/SSE, no network."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from .test_native_retry_transport import compile_retry
from .native_test_runtime import run_native_case

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"),
                                reason="requires Swift toolchain")


@pytest.fixture(scope="module")
def native_approval(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("native-approval")
    # Shared URLProtocol transport only. All Store/API/SSE lifecycle code is
    # compiled whole, without slicing or copying any production Store method.
    transport = (ROOT / "apple-app/Tests/recovery_transport_test.swift").read_text().split("@main struct RecoveryTests")[0]
    card = (ROOT / "apple-app/Sources/Views/ApprovalCard.swift").read_text()
    presentation = card[card.index("struct ApprovalArguments {"):card.index("/// 敏感工具审批卡片")]
    mapped = tmp / "ApprovalHarness.swift"
    mapped.write_text(transport + presentation + (ROOT / "apple-app/Tests/approval_binding_test.swift").read_text())
    return compile_retry(tmp, harness=mapped)


@pytest.mark.parametrize("mode", ["capture", "body", "stale-card", "stale-batch", "late-session-202", "late-session-409", "late-session-500", "late-turn-202", "late-turn-409", "late-turn-500", "retry", "arguments"])
def test_native_approval(native_approval, mode, tmp_path):
    result = run_native_case(native_approval, [mode,
        ROOT / "protocol/fixtures/010-approval-replaced-long-arguments.events.jsonl"], tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_full_argument_disclosure_uses_same_presentation():
    card = (ROOT / "apple-app/Sources/Views/ApprovalCard.swift").read_text()
    assert 'DisclosureGroup(L("完整参数"))' in card
    # Same presentation, and both serialized texts pass the invisible-character
    # filter: a bidi override must not reorder what the approver reads.
    assert "Text(ApprovalArguments.visibleFormatting(presentation.summary))" in card
    assert "Text(ApprovalArguments.visibleFormatting(presentation.fullText))" in card
    assert "Text(presentation.summary)" not in card
    assert "Text(presentation.fullText)" not in card
    # The short per-key rows go through the same filter.
    assert "Text(ApprovalArguments.displayKey(key))" in card
    assert "args[key].map(ApprovalArguments.displayValue)" in card
