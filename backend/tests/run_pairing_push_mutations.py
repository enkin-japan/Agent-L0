"""Mutation proof for the pairing / push ownership guards, the compute gate and log throttle.

Not a pytest module (no test_ prefix): run it directly from backend/:

    /Volumes/enkin/project/Agent/backend/.venv/bin/python tests/run_pairing_push_mutations.py

For every mutant: record the source file's sha256 -> inject the mutant -> clear every
__pycache__ (bytecode caching keys on mtime seconds + size, and a same-size mutant
written inside the same second would otherwise run the pre-mutation bytecode) -> run
the named tests (expected: red) -> restore the original bytes -> verify the sha256.
Before any mutant, the same tests must be green on the unmodified tree; otherwise a
"red" proves nothing. Exit status 0 only when every mutant went red and every file
was restored byte for byte.
"""
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PYTHON = os.environ.get("MUTATION_PYTHON", sys.executable)

T_PAIR = "tests/test_pairing.py"
T_PUSH = "tests/test_push.py"
T_GATE = "tests/test_app_compute_gate.py"
T_LIFE = "tests/test_compute_lifecycle.py"

# (id, description, file, original snippet, mutated snippet, tests expected to go red)
MUTANTS = [
    ("m1", "_push_caller_may_use refuses even when the owner ticket expired",
     "framework/api/routes.py",
     "        if store.active(owner) or store.field(caller, \"push_device_id\") != device_id:\n",
     "        if True:\n",
     [f"{T_PAIR}::test_entry_of_an_expired_ticket_is_taken_over_by_the_phone_that_owns_the_push_id"]),
    ("m2", "issue() keeps the superseded record of the same phone",
     "framework/api/devices.py",
     "            for old_id, old in superseded.items():\n"
     "                self.devices.pop(old_id)\n"
     "                self.by_hash.pop(old[\"token_sha256\"], None)\n"
     "            self.devices[device_id] = record\n",
     "            self.devices[device_id] = record\n",
     [f"{T_PAIR}::test_repairing_the_same_iphone_after_expiry_moves_its_push_entry"]),
    ("m3", "dispatcher sends to entries of expired / removed devices",
     "framework/push/dispatcher.py",
     "        if not owner or self.device_active is None:\n",
     "        if True:\n",
     [f"{T_PUSH}::test_tokens_of_expired_devices_are_not_sent",
      f"{T_PUSH}::test_push_to_start_skips_expired_devices",
      f"{T_PUSH}::test_live_activity_updates_skip_expired_devices"]),
    ("m4", "_drop_push deletes the self-reported push id unconditionally",
     "framework/api/pairing.py",
     "        if entry is not None and entry.get(\"auth_device\") in (None, device_id):\n",
     "        if entry is not None:\n",
     [f"{T_PAIR}::test_revoking_a_device_leaves_other_devices_entries_alone"]),
    ("m5", "app.py mounts the execution router without require_compute_ready",
     "app.py",
     "app.include_router(router, dependencies=[Depends(verify_api_key), Depends(require_compute_ready)])\n",
     "app.include_router(router, dependencies=[Depends(verify_api_key)])\n",
     [T_GATE]),
    ("m6", "_EXECUTION_ROUTES loses /turns/approval",
     "framework/api/routes.py",
     "    (\"POST\", \"/api/v1/chat/{session_id}/turns/approval\"),\n",
     "",
     [T_GATE]),
    ("m7", "compute log throttle removed",
     "framework/api/compute_lifecycle.py",
     "        if key == self._log_key and now - self._log_at < LOG_REPEAT_SECONDS:\n",
     "        if False:\n",
     [f"{T_LIFE}::test_unchanged_outage_logs_once_per_five_minutes",
      f"{T_LIFE}::test_outage_within_a_minute_is_one_line_and_recovery_is_logged",
      f"{T_LIFE}::test_assembly_retries_log_once_per_five_minutes",
      f"{T_LIFE}::test_retry_log_prints_each_distinct_message_once"]),
    ("m8", "re-pairing does not rebind the push entry to the new ticket",
     "framework/api/pairing.py",
     "        if registry.rebind(push_id, record[\"id\"]):\n",
     "        if False:\n",
     [f"{T_PAIR}::test_repairing_the_same_iphone_after_expiry_moves_its_push_entry"]),
    ("m9", "revocation leaves the device's Live Activity running",
     "framework/api/pairing.py",
     "        for pid in dropped:\n            forget(pid)\n",
     "        pass\n",
     [f"{T_PAIR}::test_revoking_unregisters_the_devices_push",
      f"{T_PAIR}::test_revoking_the_only_live_activity_device_ends_the_activity"]),
    ("m10", "/pair/request/{id}/poll response loses Cache-Control: no-store",
     "framework/api/pairing.py",
     "async def poll_request(request_id: str, body: PollBody, request: Request, response: Response):\n"
     "    response.headers[\"Cache-Control\"] = \"no-store\"\n",
     "async def poll_request(request_id: str, body: PollBody, request: Request, response: Response):\n",
     [f"{T_PAIR}::test_handshake_issues_a_token_only_after_the_mac_approves"]),
    ("m11", "/pair/request response loses Cache-Control: no-store",
     "framework/api/pairing.py",
     "async def submit_request(body: RequestBody, request: Request, response: Response):\n"
     "    response.headers[\"Cache-Control\"] = \"no-store\"\n",
     "async def submit_request(body: RequestBody, request: Request, response: Response):\n",
     [f"{T_PAIR}::test_handshake_issues_a_token_only_after_the_mac_approves"]),
    ("m12", "DeviceStore.active still counts a ticket at its exact expiry instant",
     "framework/api/devices.py",
     "            return expires is None or self.clock() < expires\n",
     "            return expires is None or self.clock() <= expires\n",
     [f"{T_PAIR}::test_a_ticket_stops_counting_at_its_expiry_instant"]),
    ("m13", "_push_caller_may_use takes over without rebinding the entry",
     "framework/api/routes.py",
     "    if owner != caller:\n        registry.rebind(device_id, caller)\n",
     "",
     [f"{T_PAIR}::test_entry_of_an_expired_ticket_is_taken_over_by_the_phone_that_owns_the_push_id"]),
    ("m14", "takeover of an expired owner ignores whose push id it is",
     "framework/api/routes.py",
     "        if store.active(owner) or store.field(caller, \"push_device_id\") != device_id:\n",
     "        if store.active(owner):\n",
     [f"{T_PAIR}::test_entry_of_an_expired_ticket_is_not_taken_over_by_another_phone"]),
    ("m15", "Live Activity push ignores a token forgotten after the snapshot",
     "framework/push/dispatcher.py",
     "            if st.tokens.get(device_id) != token:\n                continue\n",
     "",
     [f"{T_PUSH}::test_live_activity_frame_is_not_sent_to_a_device_forgotten_mid_push"]),
    ("m16", "issue() supersedes a still-valid record with the same push id",
     "framework/api/devices.py",
     "                          and r.get(\"expires\") is not None and now >= r[\"expires\"]}\n",
     "}\n",
     [f"{T_PAIR}::test_a_live_ticket_is_never_superseded_by_a_self_reported_push_id"]),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clear_pycache() -> None:
    subprocess.run(["find", ".", "-name", "__pycache__", "-type", "d", "-not", "-path", "*/.venv/*",
                    "-exec", "rm", "-rf", "{}", "+"], cwd=BACKEND, check=True)


def run_tests(targets):
    proc = subprocess.run(
        [PYTHON, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", *targets],
        cwd=BACKEND, capture_output=True, text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    failed = sorted({m.group(1).split("::")[-1].split(" ")[0]
                     for m in re.finditer(r"^(?:FAILED|ERROR) (\S+)", proc.stdout, re.M)})
    return proc.returncode, failed, proc.stdout


def main() -> int:
    for mid, _, rel, original, _, _ in MUTANTS:
        count = (BACKEND / rel).read_text(encoding="utf-8").count(original)
        if count != 1:
            print(f"{mid}: original snippet occurs {count} times in {rel}; refusing to run")
            return 2

    all_targets = sorted({t for m in MUTANTS for t in m[5]})
    clear_pycache()
    code, failed, out = run_tests(all_targets)
    if code != 0:
        print("baseline is not green; mutants would prove nothing:\n" + out[-3000:])
        return 2
    print(f"baseline green: {len(all_targets)} target(s)\n")

    rows, ok = [], True
    for mid, desc, rel, original, mutated, targets in MUTANTS:
        path = BACKEND / rel
        before = sha256(path)
        raw = path.read_bytes()
        try:
            path.write_bytes(raw.replace(original.encode("utf-8"), mutated.encode("utf-8"), 1))
            assert sha256(path) != before, f"{mid}: mutant did not change {rel}"
            clear_pycache()
            code, failed, out = run_tests(targets)
        finally:
            path.write_bytes(raw)
        restored = sha256(path) == before
        red = code == 1 and bool(failed)       # 1 = tests failed (not a collection / usage error)
        ok = ok and red and restored
        rows.append((mid, desc, "RED" if red else f"SURVIVED(exit {code})",
                     ", ".join(failed) or "-", "yes" if restored else "NO"))
        if not red:
            print(f"--- {mid} output tail ---\n{out[-2000:]}")
    clear_pycache()

    print("| mutant | defect | result | tests that went red | restored (sha256) |")
    print("|---|---|---|---|---|")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    print("\nALL MUTANTS KILLED, TREE RESTORED" if ok else "\nFAILURE: survivor or unrestored file")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
