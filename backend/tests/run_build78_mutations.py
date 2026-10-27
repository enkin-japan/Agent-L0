"""Mutation proof for the build 78 guards (readiness split, approval policy, Swift projections).

Not a pytest module (no test_ prefix). Canonical command, from the repo root:

    python3 backend/tests/run_build78_mutations.py            # all 41 mutants
    python3 backend/tests/run_build78_mutations.py --list
    python3 backend/tests/run_build78_mutations.py --only P-M1,S-a,H-M4

What it proves: each guard added in build 78 is held by a test that goes red when the
guard is broken. 24 backend mutants (H-M1..H-M7 incl. H-M3b: compute readiness split in
compute_lifecycle.py / app.py / scripts/lib_edge.sh; P-M1..P-M16: approval policy in
registry.py / approval_policy.py / orchestrator.py / routes.py / shell_exec.py /
schedule_manage.py) and 17 in-tree Swift mutants (S-a..S-g, S-m..S-o ServiceStatus.swift;
S-j, S-l, S-l2, S-l3 AgentStore.swift; S-k PhoneLink.swift; S-h/S-i ChatMessage.swift) whose
pytest harnesses compile the Swift sources with swiftc (S-k / S-l / S-l3 are source pins:
PhoneLink's iOS half is not compiled by any harness). The iOS unpair mutants are copy-only and live in apple-app/Tests/run_unpair_mutations.py.

Per mutant: refuse a dirty target file (unless --allow-dirty) -> record sha256 -> inject
-> clear backend __pycache__ (bytecode caching keys on mtime seconds + size; a same-size
mutant written within the same second would otherwise run the old bytecode) -> run the
named test file with backend/.venv/bin/python -> restore the original bytes -> verify the
sha256 -> clear __pycache__ again. The named test files must be green on the unmodified
tree first, otherwise a red proves nothing. A mutant counts as killed only when pytest
fails AND every expected test name appears among the failures. Exit 0 only when every
selected mutant is killed and every file is restored byte for byte. Never needs a
service, a port or a model; Swift mutants need xcrun swiftc (macOS).
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
PYTHON = REPO / "backend/.venv/bin/python"

CL = "backend/framework/api/compute_lifecycle.py"
APP = "backend/app.py"
LIBEDGE = "scripts/lib_edge.sh"
REG = "backend/framework/mcp/registry.py"
POL = "backend/framework/core/approval_policy.py"
ORCH = "backend/framework/core/orchestrator.py"
ROUTES = "backend/framework/api/routes.py"
SHELL = "backend/framework/mcp/tools/shell_exec.py"
STATUS = "apple-app/Sources/API/ServiceStatus.swift"
STORE = "apple-app/Sources/Store/AgentStore.swift"
CHAT = "apple-app/Sources/Store/ChatMessage.swift"
PHONE = "apple-app/Sources/App/PhoneLink.swift"

T_HEALTH = "tests/test_compute_readiness_split.py"
T_POLICY = "tests/test_approval_policy.py"
T_STATUS = "tests/test_native_service_status.py"
T_RECOVERY = "tests/test_native_recovery_transport.py"
T_CONTROL = "tests/test_native_task_control.py"
T_PHONE = "tests/test_phone_link.py"

# (id, file, old_str, new_str, test_file, expected_red_substrings[, pytest -k selector])
# Paths: file is repo-root relative; test_file is relative to backend/ (pytest's cwd).
MUTANTS = [
    # ---------------- Health: compute readiness split ----------------
    # H-M1: the catalogue named the fixed-interval tests; the test that actually kills
    # this mutant is test_local_retries_do_not_advance_the_backoff.
    ("H-M1", CL,
     "                wait = self.retry_initial if local else delay\n",
     "                wait = delay\n",
     T_HEALTH, ["test_local_retries_do_not_advance_the_backoff"]),
    ("H-M2", CL,
     '                if failures >= MONITOR_FAILURES_BEFORE_UNAVAILABLE:\n'
     '                    self.app.state.compute_ready = False\n'
     '                    self.app.state.compute_state = "unavailable"\n',
     '                if failures >= MONITOR_FAILURES_BEFORE_UNAVAILABLE:\n'
     '                    self.app.state.compute_ready = False\n'
     '                    self.app.state.compute_state = "unavailable"\n'
     '                    self.app.state.engine_ready = False\n',
     T_HEALTH, ["test_engine_ready_survives_monitor_degradation_until_stop",
                "test_monitor_refreshes_node_and_rag_state_and_engine_stays_ready"]),
    ("H-M3", APP,
     '        except Exception as exc:\n'
     '            if not installed:\n'
     '                app.state.compute_node_state = compute_node_failure_state(exc)\n'
     '            raise\n',
     '        except Exception as exc:\n'
     '            if not installed:\n'
     '                pass\n'
     '            raise\n',
     T_HEALTH, ["test_compute_down_records_unreachable_and_backs_off",
                "test_compute_unauthorized_is_reported"]),
    ("H-M3b", CL,
     '                failures += 1\n'
     '                diagnosis = self._diagnose(exc)\n'
     '                # Hysteresis: only consecutive failures mean the compute side is\n'
     '                # gone. Flipping on the first one would 503 a user mid-conversation\n'
     '                # for a single dropped packet.\n'
     '                if failures >= MONITOR_FAILURES_BEFORE_UNAVAILABLE:\n'
     '                    self.app.state.compute_ready = False\n'
     '                    self.app.state.compute_state = "unavailable"\n'
     '                    # Same threshold as compute_state: the two never disagree in steady\n'
     '                    # state. The monitor probe is the compute-node probe (RAG failures\n'
     '                    # after install do not raise).\n'
     '                    self.app.state.compute_node_state = compute_node_failure_state(exc)\n',
     '                failures += 1\n'
     '                diagnosis = self._diagnose(exc)\n'
     '                self.app.state.compute_node_state = compute_node_failure_state(exc)\n'
     '                # Hysteresis: only consecutive failures mean the compute side is\n'
     '                # gone. Flipping on the first one would 503 a user mid-conversation\n'
     '                # for a single dropped packet.\n'
     '                if failures >= MONITOR_FAILURES_BEFORE_UNAVAILABLE:\n'
     '                    self.app.state.compute_ready = False\n'
     '                    self.app.state.compute_state = "unavailable"\n'
     '                    # Same threshold as compute_state: the two never disagree in steady\n'
     '                    # state. The monitor probe is the compute-node probe (RAG failures\n'
     '                    # after install do not raise).\n',
     T_HEALTH, ["test_monitor_two_failures_then_success_keeps_node_ready"]),
    ("H-M4", LIBEDGE,
     '    local rag_ok=1\n'
     '    start_backend --no-wait || return 1\n'
     '    if rag_is_local; then\n'
     '        start_rag_local --no-wait || rag_ok=0\n'
     '    fi\n'
     '    if ! wait_backend_spawned; then\n'
     '        if [ "${_EDGE_PENDING_RAG:-0}" = 1 ]; then\n'
     '            _EDGE_PENDING_RAG=0\n'
     '            stop_rag_local || warn "撤掉本次拉起的 RAG 失败（${EDGE_LOG_DIR}/rag.pid），请 down 收摊"\n'
     '        fi\n'
     '        return 1\n'
     '    fi\n',
     '    local rag_ok=1\n'
     '    start_backend --no-wait || return 1\n'
     '    if ! wait_backend_spawned; then\n'
     '        if [ "${_EDGE_PENDING_RAG:-0}" = 1 ]; then\n'
     '            _EDGE_PENDING_RAG=0\n'
     '            stop_rag_local || warn "撤掉本次拉起的 RAG 失败（${EDGE_LOG_DIR}/rag.pid），请 down 收摊"\n'
     '        fi\n'
     '        return 1\n'
     '    fi\n'
     '    if rag_is_local; then\n'
     '        start_rag_local --no-wait || rag_ok=0\n'
     '    fi\n',
     T_HEALTH, ["test_remote_edge_up_spawns_both_before_waiting"]),
    ("H-M5", APP,
     '                    raise LocalDependencyUnavailable("RAG 数据面未就绪") from exc\n',
     '                    raise LocalDependencyUnavailable("RAG 数据面未就绪")\n',
     T_HEALTH, ["test_local_rag_failure_log_keeps_the_original_error"]),
    ("H-M6", LIBEDGE,
     '        if [ "${_EDGE_PENDING_RAG:-0}" = 1 ]; then\n'
     '            _EDGE_PENDING_RAG=0\n'
     '            stop_rag_local || warn "撤掉本次拉起的 RAG 失败（${EDGE_LOG_DIR}/rag.pid），请 down 收摊"\n'
     '        fi\n'
     '        return 1\n'
     '    fi\n'
     '    if rag_is_local; then\n',
     '        if [ "${_EDGE_PENDING_RAG:-0}" = 1 ]; then\n'
     '            _EDGE_PENDING_RAG=0\n'
     '        fi\n'
     '        return 1\n'
     '    fi\n'
     '    if rag_is_local; then\n',
     T_HEALTH, ["test_remote_edge_up_backend_wait_failure_is_fatal_and_stops_spawned_rag"]),
    ("H-M7", APP,
     "                if is_loopback_bind(rag_probe_host(rag_cfg)) and rag_failure_is_transient(exc):\n",
     "                if is_loopback_bind(rag_probe_host(rag_cfg)):\n",
     T_HEALTH, ["test_rag_failure_classification_drives_retry_schedule"]),

    # ---------------- Policy: approval policy classes ----------------
    ("P-M1", REG,
     "        tool = self._tools.get(name)\n"
     "        if tool is None:\n"
     "            return True\n"
     "        try:\n"
     "            pinned = getattr(tool, \"approval_pinned\", False) is True\n",
     "        tool = self._tools.get(name)\n"
     "        if tool is None:\n"
     "            return False\n"
     "        try:\n"
     "            pinned = getattr(tool, \"approval_pinned\", False) is True\n",
     T_POLICY, ["test_unknown_tool_always_requires_approval"]),
    ("P-M2", REG,
     '        if mode == "auto":\n'
     '            return False\n'
     '        if mode == "ask":\n'
     '            return True\n',
     '        if mode == "auto":\n'
     '            return True\n'
     '        if mode == "ask":\n'
     '            return False\n',
     T_POLICY, ["test_policy_matrix", "test_gate_follows_policy_for_a_write_tool",
                "test_orchestrator_initiated_consult_follows_policy"]),
    ("P-M3", SHELL,
     '    def effect_for(self, arguments: Dict[str, Any] = None) -> str:\n'
     '        # 与 needs_approval 同一把尺：只读命令 = read，其余（含判不了的）= write。\n'
     '        # 类属性 effect 保持 "write"（静态清单里 shell 按最坏情形展示）。\n'
     '        arguments = arguments or {}\n'
     '        return "read" if _is_readonly(str(arguments.get("command", "")),\n'
     '                                      cwd=str(arguments.get("cwd") or "")) else "write"\n',
     '    def effect_for(self, arguments: Dict[str, Any] = None) -> str:\n'
     '        # 与 needs_approval 同一把尺：只读命令 = read，其余（含判不了的）= write。\n'
     '        # 类属性 effect 保持 "write"（静态清单里 shell 按最坏情形展示）。\n'
     '        arguments = arguments or {}\n'
     '        return "read"\n',
     T_POLICY, ["test_shell_classifies_per_command_and_policy_follows"]),
    ("P-M4", POL,
     '    if not isinstance(patch, dict):\n'
     '        raise ValueError("approval policy patch must be an object")\n'
     '    unknown = sorted(str(k) for k in patch if k not in CLASSES)\n'
     '    if unknown:\n'
     '        raise ValueError(f"unknown approval policy key(s): {unknown} (allowed: {list(CLASSES)})")\n'
     '    for cls, mode in patch.items():\n'
     '        if not isinstance(mode, str) or mode not in MODES:\n'
     '            raise ValueError(f"approval policy {cls!r}: invalid mode {mode!r} (use {\'/\'.join(MODES)})")\n',
     '    if not isinstance(patch, dict):\n'
     '        pass\n'
     '    unknown = sorted(str(k) for k in patch if k not in CLASSES)\n'
     '    if unknown:\n'
     '        pass\n'
     '    for cls, mode in patch.items():\n'
     '        if not isinstance(mode, str) or mode not in MODES:\n'
     '            pass\n',
     T_POLICY, ["test_patch_rejects_invalid_body_with_422", "test_validate_merges_and_rejects"]),
    ("P-M5", REG,
     '        if mode == "default":\n'
     '            return self.requires_approval(name, arguments)\n',
     '        if mode == "default":\n'
     '            return False\n',
     T_POLICY, ["test_policy_matrix[read-True-default", "test_default_policy_equals_"]),
    ("P-M6", ORCH,
     '                _policy = self._approval_policy_for(journal)\n'
     '                approval_needed = [\n'
     '                    c for c in phys_calls\n'
     '                    if c.get("type") != "error"\n'
     '                    and self._gate_requires_approval(c.get("name"), c.get("arguments", {}), _policy)\n'
     '                ]\n',
     '                _policy = self._approval_policy_for(journal)\n'
     '                approval_needed = [\n'
     '                    c for c in phys_calls\n'
     '                    if c.get("type") != "error"\n'
     '                    and self.registry.requires_approval(c.get("name"), c.get("arguments", {}))\n'
     '                ]\n',
     T_POLICY, ["test_gate_follows_policy_for_a_write_tool",
                "test_gate_asks_for_a_read_only_shell_command_"]),
    # P-M7: unattended turns map "auto" -> "default" but keep "ask" (the old without_auto).
    ("P-M7", ORCH,
     '        if getattr(journal, "turn_origin", "chat") != "chat":\n'
     '            return approval_policy.DEFAULT\n'
     '        return approval_policy.current()\n',
     '        if getattr(journal, "turn_origin", "chat") != "chat":\n'
     '            from dataclasses import replace as _replace\n'
     '            _cur = approval_policy.current()\n'
     '            _fields = {_cls: ("default" if _cur[_cls] == "auto" else _cur[_cls])\n'
     '                       for _cls in approval_policy.CLASSES}\n'
     '            return _replace(_cur, **_fields)\n'
     '        return approval_policy.current()\n',
     T_POLICY, ["test_effective_policy_is_pure_default_for_unattended_turns[schedule",
                "test_scheduled_turn_ignores_stored_ask_for_writes",
                "test_scheduled_turn_with_read_ask_still_runs_read_tools"]),
    ("P-M8", ORCH,
     '        names = getattr(self.registry, "tool_names", None)\n'
     '        if callable(names) and name not in names():\n'
     '            return False\n'
     '        return self.registry.approval_required(name, arguments or {}, policy)\n',
     '        return self.registry.approval_required(name, arguments or {}, policy)\n',
     T_POLICY, ["test_hallucinated_tool_name_fails_as_before_under_every_mode"]),
    ("P-M9", ORCH,
     '                    if self._gate_requires_approval(_calls[0]["name"], _calls[0]["arguments"],\n'
     '                                                    self._approval_policy_for(journal)):\n'
     '                        return _suspend_for_approval(_calls, [], _calls)\n',
     '                    if self.registry.requires_approval(_calls[0]["name"], _calls[0]["arguments"]):\n'
     '                        return _suspend_for_approval(_calls, [], _calls)\n',
     T_POLICY, ["test_orchestrator_initiated_consult_follows_policy"]),
    ("P-M10", ROUTES,
     '@router.patch("/api/v1/approval-policy", response_model=ApprovalPolicyResponse,\n'
     '              dependencies=[Depends(require_owner)])\n',
     '@router.patch("/api/v1/approval-policy", response_model=ApprovalPolicyResponse)\n',
     T_POLICY, ["test_patch_with_a_device_ticket_is_forbidden_", "test_patch_route_mounts_require_owner"]),
    ("P-M11", ROUTES,
     '    ("GET", "/api/v1/approval-policy"), ("PATCH", "/api/v1/approval-policy"),\n',
     '',
     T_POLICY, ["test_policy_routes_work_before_the_first_assembly"]),
    ("P-M12", REG,
     '        if pinned and self.requires_approval(name, arguments):\n'
     '            return True\n'
     '        try:\n',
     '        try:\n',
     T_POLICY, ["test_api_tool_explicit_requires_approval_is_pinned",
                "test_pin_only_forces_when_the_tool_itself_asks"]),
    ("P-M13", ORCH,
     '        if getattr(journal, "turn_origin", "chat") != "chat":\n'
     '            return approval_policy.DEFAULT\n'
     '        return approval_policy.current()\n',
     '        return approval_policy.current()\n',
     T_POLICY, ["test_effective_policy_is_pure_default_for_unattended_turns[schedule",
                "test_scheduled_turn_is_auto_denied_whatever_the_stored_auto"]),
    ("P-M14", POL,
     '        print(f"[ApprovalPolicy] ⚠️ {path} 无法使用（{type(e).__name__}: {e}），"\n'
     '              f"按全审批处理 {ASK_ALL.as_dict()}")\n'
     '        return ASK_ALL\n',
     '        print(f"[ApprovalPolicy] ⚠️ {path} 无法使用（{type(e).__name__}: {e}），"\n'
     '              f"按全审批处理 {ASK_ALL.as_dict()}")\n'
     '        return DEFAULT\n',
     T_POLICY, ["test_corrupt_or_invalid_file_fails_closed_to_ask",
                "test_unreadable_file_fails_closed_to_ask",
                "test_patch_after_a_corrupt_file_merges_onto_ask_ask"]),
    # P-M16: schedule_create's approval card is the documented gate against injected scheduling.
    ("P-M16", "backend/framework/mcp/tools/schedule_manage.py",
     '    requires_approval = True     # 审批卡片=任务细节确认；防无人值守/注入内容排任务\n'
     '    # 钉死（build 78）：这张卡是文档化的安全闸（见模块 docstring），写档 "auto" 不得放行\n'
     '    approval_pinned = True\n',
     '    requires_approval = True     # 审批卡片=任务细节确认；防无人值守/注入内容排任务\n',
     T_POLICY, ["test_every_shipped_tool_class_declares_its_table_effect",
                "test_security_gate_tools_ask_under_every_policy[schedule_create-auto"]),
    # P-M15 (M1): the shell is pinned as a whole; without the pin write="auto" runs a
    # mutating command unapproved and outside the env rule (免审批执行 ⇔ 只读).
    ("P-M15", SHELL,
     '    approval_pinned = True\n',
     '',
     T_POLICY, ["test_shell_classifies_per_command_and_policy_follows",
                "test_shell_pin_keeps_non_readonly_commands_asking_under_write_auto",
                "test_gate_asks_for_a_mutating_shell_command_even_when_writes_are_auto"]),

    # ---------------- Swift: macOS status mapping (Contract H) ----------------
    ("S-a", STATUS,
     '        case "unauthorized": return .unauthorized\n',
     '        case "unauthorized": return .ready\n',
     T_STATUS, ["test_native_contract_h_projection"]),
    ("S-b", STATUS,
     '        case "ready": return .ready\n',
     '        case "local", "ready": return .ready\n',
     T_STATUS, ["test_native_contract_h_projection"]),
    ("S-c", STATUS,
     '        if health.computeState == "stopping" || health.computeState == "stopped" { return .unavailable }\n',
     '',
     T_STATUS, ["test_native_contract_h_projection"]),
    ("S-d", STATUS,
     '(health?.computeNodeState == "probing"\n'
     '            || (health?.engineReady == false\n',
     '((health?.engineReady == false\n',
     T_STATUS, ["test_native_contract_h_projection"]),
    ("S-e", STATUS,
     '            || (health?.engineReady == false\n',
     '            || (health?.engineReady != true\n',
     T_STATUS, ["test_native_contract_h_projection"]),
    # S-m / S-n (M3): a settled node or RAG failure with engine_ready=false is not "starting".
    ("S-m", STATUS,
     '                && !["unreachable", "unauthorized"].contains(health?.computeNodeState ?? "")\n',
     '',
     T_STATUS, ["test_native_contract_h_projection"]),
    ("S-n", STATUS,
     '                && health?.ragState != "unauthorized"))\n',
     '                && true))\n',
     T_STATUS, ["test_native_contract_h_projection"]),
    # S-o: only a permanent RAG failure settles; a transient local "unreachable" is cold start.
    ("S-o", STATUS,
     '                && health?.ragState != "unauthorized"))\n',
     '                && ragNotice == nil))\n',
     T_STATUS, ["test_native_contract_h_projection"]),
    ("S-f", STATUS,
     '        if access == .offline || stillStarting { return 2 }\n'
     '        return 15\n',
     '        if access == .offline || stillStarting { return 2 }\n'
     '        return 2\n',
     T_STATUS, ["test_native_contract_h_projection"]),
    ("S-g", STATUS,
     '        case "unauthorized": return "unauthorized"\n',
     '        case "unauthorized": return "unauthorized"\n'
     '        case "skipped": return "unreachable"\n',
     T_STATUS, ["test_native_contract_h_projection"]),
    # ---------------- Swift: AgentStore connState ----------------
    ("S-j", STORE,
     'setConn(engineStarting ? .engineStarting : .connected)',
     'setConn(.connected)',
     T_RECOVERY, ["test_native_recovery_transport[auth-engine-starting]"]),
    # S-l / S-l2 (M2): the per-tick emission; S-l2 is the behavioural oracle (3 failing
    # probes => 3 probeTick emissions while $connState stays silent).
    ("S-l", STORE,
     '        probeTick.send(connState)   // unconditionally: one emission per probe, see probeTick\n',
     '',
     T_PHONE, ["test_wiring"]),
    ("S-l3", STORE,
     '            probeTick.send(connState)   // no client is a probe outcome too: PhoneLink heals on it\n',
     '',
     T_PHONE, ["test_wiring"]),
    ("S-l2", STORE,
     '        probeTick.send(connState)   // unconditionally: one emission per probe, see probeTick\n',
     '',
     T_RECOVERY, ["test_native_recovery_transport[auth-offline-ticks]"]),
    # ---------------- Swift: PhoneLink heal / upgrade retry (M2) ----------------
    ("S-k", PHONE,
     '        store.probeTick\n',
     '        store.$connState\n',
     T_PHONE, ["test_wiring"]),
    # ---------------- Swift: receipt matching / step count ----------------
    ("S-h", CHAT,
     'guard let matched = receiptGroups[k], matched.count == indices.count else { continue }',
     'guard let matched = receiptGroups[k], matched.count >= 1 else { continue }',
     T_CONTROL, ["steps-denied-same-name"], "same-name"),
    ("S-i", CHAT,
     '                if let n = open[name], n > 0 { open[name] = n - 1 } else { count += 1 }\n',
     '                count += 1\n',
     T_CONTROL, ["steps-paired-same-name"], "same-name"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clear_pycache() -> None:
    subprocess.run(["find", ".", "-name", "__pycache__", "-type", "d", "-not", "-path", "./.venv/*",
                    "-exec", "rm", "-rf", "{}", "+"], cwd=BACKEND, check=True)


def run_tests(test_file, select=None):
    cmd = [str(PYTHON), "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", test_file]
    if select:
        cmd += ["-k", select]
    proc = subprocess.run(cmd, cwd=BACKEND, capture_output=True, text=True,
                          env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    failed = sorted({m.group(1).split("::", 1)[-1]
                     for m in re.finditer(r"^(?:FAILED|ERROR) (\S+)", proc.stdout, re.M)})
    return proc.returncode, failed, proc.stdout + proc.stderr


def dirty(rel: str) -> bool:
    out = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    return bool(out.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", help="comma-separated mutant ids (see --list)")
    ap.add_argument("--list", action="store_true", help="list mutants and exit")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="run even when a target file has uncommitted changes")
    args = ap.parse_args()

    by_id = {m[0]: m for m in MUTANTS}
    if args.list:
        for m in MUTANTS:
            print(f"{m[0]:6} {m[1]:46} {m[4]:42} expect red: {', '.join(m[5])}")
        return 0
    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [w for w in wanted if w not in by_id]
        if unknown:
            print(f"unknown mutant id(s): {unknown}; see --list")
            return 2
        selected = [by_id[w] for w in wanted]
    else:
        selected = list(MUTANTS)

    if not PYTHON.exists():
        print(f"venv python not found: {PYTHON}")
        return 2
    for m in selected:
        rel, original = m[1], m[2]
        if not args.allow_dirty and dirty(rel):
            print(f"{m[0]}: {rel} has uncommitted changes; refusing (use --allow-dirty)")
            return 2
        count = (REPO / rel).read_text(encoding="utf-8").count(original)
        if count != 1:
            print(f"{m[0]}: original snippet occurs {count} times in {rel}; refusing to run")
            return 2

    for test_file, select in sorted({(m[4], m[6] if len(m) > 6 else None) for m in selected},
                                    key=lambda t: (t[0], t[1] or "")):
        clear_pycache()
        code, failed, out = run_tests(test_file, select)
        if code != 0:
            print(f"baseline not green for {test_file}; mutants would prove nothing:\n" + out[-3000:])
            return 2
        print(f"baseline green: {test_file}" + (f" -k {select}" if select else ""))
    print()

    rows, ok = [], True
    for m in selected:
        mid, rel, original, mutated, test_file, expected = m[:6]
        select = m[6] if len(m) > 6 else None
        path = REPO / rel
        before = sha256(path)
        raw = path.read_bytes()
        try:
            path.write_bytes(raw.replace(original.encode("utf-8"), mutated.encode("utf-8"), 1))
            assert sha256(path) != before, f"{mid}: mutant did not change {rel}"
            clear_pycache()
            code, failed, out = run_tests(test_file, select)
        finally:
            path.write_bytes(raw)
            clear_pycache()
        restored = sha256(path) == before
        missing = [e for e in expected if not any(e in f for f in failed)]
        killed = code == 1 and bool(failed) and not missing   # 1 = tests failed, not a usage error
        ok = ok and killed and restored
        if killed:
            result = "killed"
        elif code == 1 and failed:
            result = "RED-BUT-NOT-EXPECTED (missing: " + ", ".join(missing) + ")"
        else:
            result = f"SURVIVED(exit {code})"
        shown = ", ".join(failed[:6]) + (f", ... (+{len(failed) - 6})" if len(failed) > 6 else "")
        rows.append((mid, result, shown or "-", "yes" if restored else "NO"))
        print(f"{mid}: {result}; restored={'yes' if restored else 'NO'}", flush=True)
        if not killed:
            print(f"--- {mid} output tail ---\n{out[-2000:]}")
        if not restored:
            print(f"FATAL: {rel} not restored byte for byte (sha256 {before} expected); stopping")
            break

    print("\n| mutant | result | tests that went red | restored (sha256) |")
    print("|---|---|---|---|")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    print("\nALL MUTANTS KILLED, TREE RESTORED" if ok else "\nFAILURE: survivor or unrestored file")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
