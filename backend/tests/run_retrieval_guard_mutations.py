"""CPU-only mutation proof in an isolated copy; never rewrites the shared worktree.

Run with backend/.venv/bin/python backend/tests/run_retrieval_guard_mutations.py.
Only copies runtime Python sources and synthetic tests, never config or secrets.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

BACKEND = Path(__file__).resolve().parents[1]
TEST_FILES = ["__init__.py", "conftest.py", "fake_engine.py",
              "test_retrieval_evidence_guard.py", "test_fabrication_guardrails.py",
              "test_turn_runner.py"]
MUTATIONS = [
    # (label, file, needle, replacement, test or tuple of tests that must each go red)
    ("stream_gate_removed", "orchestrator.py",
     'None if provisional_generation else journal.emit("token_delta", {"text": t})',
     'journal.emit("token_delta", {"text": t})',
     "test_failure_unrelated_success_cannot_release_fabricated_answer"),
    ("evidence_accounting_removed", "orchestrator.py",
     'retrieval.observe([c for (_, c, _) in safe_batch], results)', "pass # mutation: no accounting",
     "test_failure_unrelated_success_cannot_release_fabricated_answer"),
    ("pause_checkpoint_removed", "orchestrator.py",
     '"retrieval_evidence": retrieval.checkpoint(),',
     '"retrieval_evidence": {},',
     "test_approval_pause_keeps_missing_evidence_after_unrelated_success"),
    ("delivery_before_approval_guard_removed", "orchestrator.py",
     'if (not retrieval_before\n'
     '                                and is_delivery(self.registry, c)):',
     'if False and (not retrieval_before\n'
     '                                and is_delivery(self.registry, c)):',
     "test_failed_retrieval_blocks_delivery_without_execution_or_approval"),
    ("same_batch_retrieval_barrier_removed", "orchestrator.py",
     'for (_, c, _) in safe_batch):\n'
     '                    await flush_safe_batch()',
     'for (_, c, _) in safe_batch):\n'
     '                    pass  # mutation: no flush',
     "test_pending_failure_same_batch_retry_is_flushed_before_chart"),
    ("step_completion_guard_removed", "orchestrator.py",
     'if mname in {"complete_step", "remember"} and retrieval.unresolved:',
     'if False and mname in {"complete_step", "remember"} and retrieval.unresolved:',
     "test_failed_retrieval_cannot_publish_fabricated_complete_step_result"),
    ("later_retrieval_barrier_removed", "orchestrator.py",
     'for c in calls[idx + 1:] if c.get("type") != "error"):',
     'for c in [] if c.get("type") != "error"):',
     "test_pending_failure_chart_cannot_run_ahead_of_later_retrieval"),
    ("remember_fact_guard_removed", "orchestrator.py",
     'if mname in {"complete_step", "remember"} and retrieval.unresolved:',
     'if mname == "complete_step" and retrieval.unresolved:',
     "test_failed_retrieval_cannot_remember_fabricated_values_as_facts"),
    # M-A: delivery scope widened back to "every non-read-only tool"
    ("M-A_delivery_is_any_mutation", "retrieval_evidence.py",
     '    if name == SHELL_TOOL:\n',
     '    return not registry.is_read_only(name, args)\n    if name == SHELL_TOOL:\n',
     ("test_shell_is_judged_by_its_own_read_only_logic",
      "test_non_delivery_mutation_runs_while_chart_is_blocked")),
    # M-B: an empty result is treated like a failure
    ("M-B_empty_is_pending", "retrieval_evidence.py",
     'if fact.get("kind") in {"evidence", "empty"}:',
     'if fact.get("kind") == "evidence":',
     "test_empty_retrieval_completes_and_chart_executes"),
    # M-C: a completed retrieval only releases its own target
    ("M-C_per_target_release", "retrieval_evidence.py",
     'self.pending.clear()',
     'self.pending.pop(target, None)',
     "test_context_evidence_for_another_target_releases_delivery"),
    # M-D: the later-retrieval barrier is applied with nothing pending
    ("M-D_barrier_without_pending", "orchestrator.py",
     'if retrieval.unresolved and any(\n'
     '                        retrieval_target(c.get("name"), c.get("arguments") or {})\n'
     '                        for c in calls[idx + 1:]',
     'if any(\n'
     '                        retrieval_target(c.get("name"), c.get("arguments") or {})\n'
     '                        for c in calls[idx + 1:]',
     "test_no_pending_batch_runs_without_barrier"),
    # M-G: the pre-delivery flush is gated back on a pending failure
    ("M-G_flush_only_while_pending", "orchestrator.py",
     'if any(\n'
     '                        retrieval_target(c.get("name"), c.get("arguments") or {})\n'
     '                        for (_, c, _) in safe_batch):',
     'if retrieval.unresolved and any(\n'
     '                        retrieval_target(c.get("name"), c.get("arguments") or {})\n'
     '                        for (_, c, _) in safe_batch):',
     "test_fresh_batch_failed_retrieval_is_serialized_before_chart"),
    # M-H: rag_get_context "not found" msg misclassified as failed again
    ("M-H_context_not_found_is_failed", "retrieval_evidence.py",
     '        else:\n'
     '            # rag_get_context reports "not found"',
     '        elif text.startswith("No chunks found") or not text:\n'
     '            # rag_get_context reports "not found"',
     "test_context_not_found_message_is_a_completed_lookup"),
    # M-I: a C4 block counts as a failed batch for the cloud-consult escalation
    ("M-I_block_counts_as_upgrade_failure", "orchestrator.py",
     '                        if not r.get("_blocked"):\n'
     '                            _batch_failed = True',
     '                        if True:\n'
     '                            _batch_failed = True',
     "test_c4_block_does_not_count_as_upgrade_failure"),
    # M-J: the serial execution path no longer records retrieval facts
    ("M-J_serial_observe_removed", "orchestrator.py",
     '                if retrieval is not None:\n'
     '                    retrieval.observe([call], [by_idx[idx]])',
     '                pass  # mutation: serial observe removed',
     "test_serial_retrieval_failure_is_accounted"),
    # M-L: a C4-blocked approved write still seeds the resumed unverified-write flag
    ("M-L_resume_blocked_write_unverified", "orchestrator.py",
     'c.get("name") in _FILE_WRITE_TOOLS and not r.get("_blocked")',
     'c.get("name") in _FILE_WRITE_TOOLS',
     "test_resumed_c4_blocked_file_write_is_not_an_unverified_write"),
    # M-E: the iteration-budget exit replaces its real reason with the retrieval text
    ("M-E_exit_reason_replaced", "orchestrator.py",
     '        _exit_audit()\n'
     '        return {\n'
     '            "status": "warning",\n'
     '            "response": _with_retrieval_note(\n',
     '        _exit_audit()\n'
     '        if retrieval.unresolved:\n'
     '            return _retrieval_incomplete()\n'
     '        return {\n'
     '            "status": "warning",\n'
     '            "response": _with_retrieval_note(\n',
     "test_iteration_budget_exit_keeps_real_reason_and_names_missing_retrieval"),
    # M-F: the shell is never a delivery, even for a non-read-only command
    ("M-F_shell_never_delivery", "retrieval_evidence.py",
     '        return bool(registry.requires_approval(name, args))',
     '        return False',
     "test_shell_is_judged_by_its_own_read_only_logic"),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    protected = [BACKEND / "framework/core" / name
                 for name in ("orchestrator.py", "retrieval_evidence.py")]
    before = {str(path): digest(path) for path in protected}
    results = []
    with tempfile.TemporaryDirectory(prefix="retrieval-guard-proof-") as raw:
        root = Path(raw)
        framework = root / "backend/framework"
        for source in (BACKEND / "framework").rglob("*.py"):
            target = framework / source.relative_to(BACKEND / "framework")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        tests = root / "backend/tests"
        tests.mkdir(parents=True)
        for name in TEST_FILES:
            shutil.copyfile(BACKEND / "tests" / name, tests / name)
        (root / "config.yaml").write_text("{}\n")

        def run_test(name):
            return subprocess.run([sys.executable, "-m", "pytest", "-q",
                                   f"backend/tests/test_retrieval_evidence_guard.py::{name}"],
                                  cwd=root, capture_output=True, text=True)

        for label, filename, needle, replacement, tests in MUTATIONS:
            tests = (tests,) if isinstance(tests, str) else tuple(tests)
            for test in tests:
                baseline = run_test(test)
                if baseline.returncode:
                    raise RuntimeError(f"baseline failed for {label}/{test}: "
                                       f"{baseline.stdout}{baseline.stderr}")
            path = framework / "core" / filename
            original = path.read_bytes()
            source = original.decode()
            assert source.count(needle) == 1, (label, source.count(needle))
            try:
                path.write_text(source.replace(needle, replacement))
                # Only invalidate affected copied bytecode, not repository caches.
                for cache in (path.parent / "__pycache__").glob(path.stem + ".*.pyc"):
                    cache.unlink()
                for test in tests:
                    mutated = run_test(test)
                    if mutated.returncode != 1 or "FAILED" not in mutated.stdout:
                        raise RuntimeError(f"mutation did not fail assertions: {label}/{test}: "
                                           f"{mutated.stdout}{mutated.stderr}")
                    results.append({"mutation": label, "baseline": "passed",
                                    "mutation_result": "test failed as intended", "test": test})
            finally:
                path.write_bytes(original)
                for cache in (path.parent / "__pycache__").glob(path.stem + ".*.pyc"):
                    cache.unlink()
                assert path.read_bytes() == original
    assert before == {str(path): digest(path) for path in protected}
    print(json.dumps({"results": results, "shared_worktree_unchanged": True}, indent=2))


if __name__ == "__main__":
    main()
