"""Build 78 Contract P: tool effect classes + the user's read/write approval policy.

Oracles (guard-type change: each assertion below is what a mutant has to break):
- the decision matrix of MCPRegistry.approval_required, including fail-closed for
  unknown tools and the "default" delegation to the tool's own requires_approval;
- the shell classifies per command with the same rule as its approval hook;
- READ_TOOLS / WRITE_TOOLS are the literal classification table: every shipped tool
  class is in exactly one of them and declares exactly that effect;
- persistence (missing / corrupt => defaults, atomic 0600 roundtrip);
- GET/PATCH /api/v1/approval-policy on the production app (auth, 422, persisted);
- the orchestrator's approval gate obeys the policy.
No command text is executed: shell calls are only classified, and the gate tests use
executors that fail the test if they run.
"""
import asyncio
import importlib
import inspect
import json
import os
import pkgutil
import stat
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from framework.core import approval_policy
from framework.core.approval_policy import ApprovalPolicy
from framework.mcp.base import BaseTool
from framework.mcp.registry import MCPRegistry
from framework.mcp.tools.shell_exec import ShellExecTool

MODES = ("auto", "default", "ask")

# ── The classification table (oracle). Changing a tool's effect means changing it here.
READ_TOOLS = {
    "list_applications", "read_ui_tree", "get_current_time", "deck_status",
    "clipboard_read", "system_status", "file_extract", "find_files", "list_dir",
    "calendar_list", "rag_list_categories", "rag_search",
    "rag_get_context", "schedule_list", "skill_invoke", "get_location", "web_read",
    "web_research",
}
WRITE_TOOLS = {
    "click_ui_element", "press_keys", "set_ui_element_value", "type_text",
    "run_applescript", "cloud_consult", "code_explore",
    "deck_critique", "deck_export_pdf", "deck_export_pptx", "deck_init",
    "deck_remove_slide", "deck_set_slide",
    "clipboard_write", "notify_user", "open_path", "write_document", "excel_research",
    "edit_file", "write_file", "copy_file", "move_file", "trash_file", "media_download",
    "memory_write", "calendar_add", "reminder_add",
    "rag_delete_category", "rag_delete_document", "rag_insert_document", "rag_mark_stale",
    "rag_reclassify_unclassified", "rag_upsert_category",
    "register_api_tool", "register_skill", "render_chart",
    "schedule_create", "schedule_delete", "schedule_update",
    "run_shell_command",
    # sandboxed compute: (allow default)(deny network*)(deny file-write*) still permits
    # subprocesses, signals, Apple Events and the clipboard -> not a pure read
    "math_research", "run_math",
}
DYNAMIC_TOOLS = {"run_shell_command"}


class _Fixture(BaseTool):
    description = "fixture tool"
    parameters = {"properties": {}, "required": []}

    def execute(self, **kwargs):
        pytest.fail("classification tests never execute a tool")


def _tool(name, effect, requires_approval):
    cls = type(f"Fixture_{name}", (_Fixture,),
               {"name": name, "effect": effect, "requires_approval": requires_approval})
    return cls()


def _registry(*tools):
    registry = object.__new__(MCPRegistry)
    registry._tools = {t.name: t for t in tools}
    return registry


# ── Decision matrix ───────────────────────────────────────────────────────────

# (effect, the tool's own requires_approval, mode for its class, expected)
MATRIX = [
    ("read", False, "auto", False), ("read", False, "default", False), ("read", False, "ask", True),
    ("read", True, "auto", False), ("read", True, "default", True), ("read", True, "ask", True),
    ("write", False, "auto", False), ("write", False, "default", False), ("write", False, "ask", True),
    ("write", True, "auto", False), ("write", True, "default", True), ("write", True, "ask", True),
]


@pytest.mark.parametrize("effect,own,mode,expected", MATRIX)
def test_policy_matrix(effect, own, mode, expected):
    other = "write" if effect == "read" else "read"
    # The other class's knob is set to the opposite answer, so deciding by the wrong
    # class can never pass by coincidence.
    policy = ApprovalPolicy(**{effect: mode, other: "auto" if expected else "ask"})
    registry = _registry(_tool("probe", effect, own))
    assert registry.approval_required("probe", {}, policy) is expected


def test_ask_overrides_a_tool_that_never_asks_and_auto_overrides_one_that_always_asks():
    registry = _registry(_tool("quiet_write", "write", False), _tool("loud_read", "read", True))
    assert registry.approval_required("quiet_write", {}, ApprovalPolicy(write="ask")) is True
    assert registry.approval_required("loud_read", {}, ApprovalPolicy(read="auto")) is False


@pytest.mark.parametrize("read", MODES)
@pytest.mark.parametrize("write", MODES)
def test_unknown_tool_always_requires_approval(read, write):
    registry = _registry(_tool("known", "read", False))
    assert registry.approval_required("no_such_tool", {}, ApprovalPolicy(read=read, write=write)) is True


def test_default_policy_equals_requires_approval_for_every_fixture():
    tools = [_tool(f"t{i}", eff, own) for i, (eff, own) in
             enumerate([("read", False), ("read", True), ("write", False), ("write", True)])]
    registry = _registry(*tools)
    for t in tools:
        assert registry.approval_required(t.name, {}, approval_policy.DEFAULT) \
            is registry.requires_approval(t.name, {}) is t.requires_approval


def test_effect_of_fails_safe_to_write():
    class Boom(_Fixture):
        name = "boom"
        effect = "read"

        def effect_for(self, arguments=None):
            raise RuntimeError("classifier broke")

    registry = _registry(_tool("shouty", "READ", False), _tool("typo", "reed", False), Boom())
    for name in ("shouty", "typo", "boom", "missing"):
        assert registry.effect_of(name, {}) == "write", name
    # A broken classifier under read=auto/write=ask still asks.
    assert registry.approval_required("boom", {}, ApprovalPolicy(read="auto", write="ask")) is True


def test_policy_value_rejects_unknown_class_keys_and_modes():
    with pytest.raises(KeyError):
        approval_policy.DEFAULT["execute"]
    with pytest.raises(ValueError):
        ApprovalPolicy(read="maybe")


# ── Shell: per-command class ──────────────────────────────────────────────────

def test_shell_classifies_per_command_and_policy_follows():
    shell = ShellExecTool()
    registry = _registry(shell)
    ls, rm = {"command": "ls -la"}, {"command": "rm -rf x"}
    assert ShellExecTool.effect == "write"           # static class effect stays write
    assert shell.effect_for(ls) == "read" and registry.effect_of(shell.name, ls) == "read"
    assert shell.effect_for(rm) == "write" and registry.effect_of(shell.name, rm) == "write"

    relaxed = ApprovalPolicy(read="auto", write="ask")
    assert registry.approval_required(shell.name, ls, relaxed) is False
    assert registry.approval_required(shell.name, rm, relaxed) is True
    inverted = ApprovalPolicy(read="ask", write="auto")
    assert registry.approval_required(shell.name, ls, inverted) is True
    assert registry.approval_required(shell.name, rm, inverted) is False

    for args in (ls, rm):
        assert registry.approval_required(shell.name, args, approval_policy.DEFAULT) \
            is shell.needs_approval(args)
    assert shell.needs_approval(ls) is False and shell.needs_approval(rm) is True


# ── Every shipped tool is classified, and matches the table ───────────────────

@pytest.fixture()
def hermetic_tools(monkeypatch, tmp_path):
    """No network probes (rag_admin probes at import), no user data root."""
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    with mock.patch("requests.get", side_effect=ConnectionError("hermetic")):
        yield


def _shipped_tool_classes():
    import framework.mcp.tools as tools_pkg
    from framework.mcp.tools.api_tool_factory import _ApiCore
    found = {}
    for info in pkgutil.iter_modules(tools_pkg.__path__):
        mod = importlib.import_module(f"framework.mcp.tools.{info.name}")
        for obj in vars(mod).values():
            if (inspect.isclass(obj) and issubclass(obj, BaseTool) and obj is not BaseTool
                    and not issubclass(obj, _ApiCore) and obj.__module__ == mod.__name__):
                found[obj.name] = obj
    return found


def test_table_is_disjoint():
    assert not READ_TOOLS & WRITE_TOOLS
    assert DYNAMIC_TOOLS <= WRITE_TOOLS


def test_every_shipped_tool_class_declares_its_table_effect(hermetic_tools):
    classes = _shipped_tool_classes()
    assert set(classes) == READ_TOOLS | WRITE_TOOLS, (
        f"unclassified: {sorted(set(classes) - READ_TOOLS - WRITE_TOOLS)}; "
        f"stale: {sorted((READ_TOOLS | WRITE_TOOLS) - set(classes))}")
    wrong = {name: cls.effect for name, cls in classes.items()
             if cls.effect != ("read" if name in READ_TOOLS else "write")}
    assert not wrong, wrong
    dynamic = {name for name, cls in classes.items() if cls.effect_for is not BaseTool.effect_for}
    assert dynamic == DYNAMIC_TOOLS


def test_real_registry_tools_have_valid_effects(hermetic_tools):
    from framework.mcp.tools.api_tool_factory import _ApiCore
    root = Path(__file__).resolve().parent.parent
    registry = MCPRegistry(tools_dir=root / "framework" / "mcp" / "tools")
    assert registry.tool_names(), "real registry registered nothing"
    effects = registry.tool_effects()
    listed = {item["name"]: item["effect"] for item in registry.list_tools()}
    assert listed == effects
    for name, effect in effects.items():
        tool = registry._tools[name]
        assert effect in ("read", "write"), name
        if isinstance(tool, _ApiCore):
            assert effect == ("read" if tool._method in ("GET", "HEAD") else "write"), name
        else:
            assert effect == ("read" if name in READ_TOOLS else "write"), name
            assert name in READ_TOOLS | WRITE_TOOLS, name
    assert set(registry.dynamic_effect_tools()) <= DYNAMIC_TOOLS


@pytest.mark.parametrize("method,effect", [("GET", "read"), ("HEAD", "read"), ("POST", "write"),
                                           ("PUT", "write"), ("PATCH", "write"),
                                           ("DELETE", "write")])
def test_api_tool_effect_follows_method(method, effect):
    from framework.mcp.tools import api_tool_factory as factory
    decl = {"name": "fixture_api", "description": "fixture", "method": method,
            "endpoint": "https://fixture.invalid/x"}
    tool = factory.synth_class(decl, "fixture")()
    assert tool.effect == effect and tool.effect_for({}) == effect


# ── Pinned approvals: "auto" never overrides an explicit human decision ──────

def _api_tool(method, **extra):
    from framework.mcp.tools import api_tool_factory as factory
    decl = {"name": f"fixture_{method.lower()}", "description": "fixture", "method": method,
            "endpoint": "https://fixture.invalid/x", **extra}
    return factory.synth_class(decl, "fixture")()


@pytest.mark.parametrize("method,declared,policy,expected", [
    ("GET", True, ApprovalPolicy(read="auto", write="auto"), True),     # pinned: auto can't skip
    ("GET", False, ApprovalPolicy(read="ask", write="auto"), True),     # ask is stricter than a pin
    ("GET", False, ApprovalPolicy(read="auto", write="ask"), False),
    ("GET", None, ApprovalPolicy(read="auto", write="ask"), False),
    ("POST", True, ApprovalPolicy(read="ask", write="auto"), True),     # pinned
    ("POST", None, ApprovalPolicy(read="ask", write="auto"), False),    # method default, not pinned
    ("POST", None, approval_policy.DEFAULT, True),
])
def test_api_tool_explicit_requires_approval_is_pinned(method, declared, policy, expected):
    tool = _api_tool(method, **({} if declared is None else {"requires_approval": declared}))
    assert tool.approval_pinned is (declared is not None)
    assert _registry(tool).approval_required(tool.name, {}, policy) is expected


def test_pin_only_forces_when_the_tool_itself_asks():
    pinned_quiet = _tool("pinned_quiet", "write", False)
    pinned_quiet.approval_pinned = True
    pinned_loud = _tool("pinned_loud", "read", True)
    pinned_loud.approval_pinned = True
    registry = _registry(pinned_quiet, pinned_loud)
    auto = ApprovalPolicy("auto", "auto")
    assert registry.approval_required("pinned_quiet", {}, auto) is False
    assert registry.approval_required("pinned_loud", {}, auto) is True


# ── Persistence ───────────────────────────────────────────────────────────────

def _warnings(capsys):
    return [l for l in capsys.readouterr().out.splitlines() if l.startswith("[ApprovalPolicy]")]


def test_missing_file_yields_defaults(tmp_path, capsys):
    assert approval_policy.load(tmp_path) == approval_policy.DEFAULT
    assert len(_warnings(capsys)) == 1


@pytest.mark.parametrize("content", ["{not json", "[]", '"auto"', '{"read": "maybe"}',
                                     '{"read": "auto", "extra": "ask"}', '{"write": null}',
                                     b"\xff\xfe not utf-8"])
def test_corrupt_or_invalid_file_fails_closed_to_ask(tmp_path, capsys, content):
    path = tmp_path / approval_policy.FILENAME
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    assert approval_policy.load(tmp_path) == approval_policy.ASK_ALL == ApprovalPolicy("ask", "ask")
    assert len(_warnings(capsys)) == 1


def test_unreadable_file_fails_closed_to_ask(tmp_path, capsys):
    path = tmp_path / approval_policy.FILENAME
    path.write_text('{"read": "auto", "write": "auto"}')
    os.chmod(path, 0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running with privileges that ignore file modes")
        assert approval_policy.load(tmp_path) == approval_policy.ASK_ALL
    finally:
        os.chmod(path, 0o600)
    (tmp_path / "dir-root").mkdir()
    (tmp_path / "dir-root" / approval_policy.FILENAME).mkdir()      # a directory in its place
    assert approval_policy.load(tmp_path / "dir-root") == approval_policy.ASK_ALL
    assert len(_warnings(capsys)) == 2


def test_save_load_roundtrip_atomic_0600(tmp_path):
    path = tmp_path / approval_policy.FILENAME
    path.write_text('{"read": "ask", "write": "ask"}')
    os.chmod(path, 0o644)
    policy = ApprovalPolicy(read="auto", write="ask")
    assert approval_policy.save(policy, tmp_path) == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"read": "auto", "write": "ask"}
    assert approval_policy.load(tmp_path) == policy
    assert sorted(p.name for p in tmp_path.iterdir()) == [approval_policy.FILENAME]


def test_current_lazily_loads_the_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    approval_policy.save(ApprovalPolicy(read="auto", write="default"), tmp_path)
    approval_policy.reset()
    assert approval_policy.current() == ApprovalPolicy(read="auto", write="default")


def test_validate_merges_and_rejects():
    base = ApprovalPolicy(read="ask", write="auto")
    assert approval_policy.validate({"write": "default"}, base) == ApprovalPolicy("ask", "default")
    assert approval_policy.validate({}, base) == base
    for bad in ({"read": "maybe"}, {"foo": "auto"}, {"read": None}, {"write": 1}, [], "auto"):
        with pytest.raises(ValueError):
            approval_policy.validate(bad, base)


# ── HTTP endpoints on the production app ──────────────────────────────────────

KEY = "policy-key-for-tests"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import app as entry          # lazy, as test_app_compute_gate: no lifespan, no assembly
    from framework.api.devices import DeviceStore
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    approval_policy.reset()
    registry = _registry(_tool("reader", "read", False), _tool("writer", "write", True),
                         ShellExecTool())
    for name, value in (("api_key", KEY), ("auth_devices", None),
                        ("device_store", DeviceStore(tmp_path / "auth" / "devices.json")),
                        ("compute_ready", True), ("compute_installed", True),
                        ("compute_state", "ready"), ("mcp_registry", registry)):
        monkeypatch.setattr(entry.app.state, name, value, raising=False)
    return TestClient(entry.app, raise_server_exceptions=False), tmp_path / approval_policy.FILENAME


EXPECTED_TOOLS = {"reader": "read", "writer": "write", "run_shell_command": "write"}


def test_get_policy_shape(client):
    http, _ = client
    r = http.get("/api/v1/approval-policy", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"read": "default", "write": "default", "tools": EXPECTED_TOOLS,
                        "dynamic": ["run_shell_command"]}


def test_patch_persists_and_applies_without_restart(client):
    http, path = client
    r = http.patch("/api/v1/approval-policy", json={"write": "ask"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"read": "default", "write": "ask", "tools": EXPECTED_TOOLS,
                        "dynamic": ["run_shell_command"]}
    assert json.loads(path.read_text()) == {"read": "default", "write": "ask"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert approval_policy.current() == ApprovalPolicy(read="default", write="ask")
    r = http.patch("/api/v1/approval-policy", json={"read": "auto"}, headers=AUTH)
    assert r.status_code == 200 and (r.json()["read"], r.json()["write"]) == ("auto", "ask")
    assert http.get("/api/v1/approval-policy", headers=AUTH).json()["read"] == "auto"
    approval_policy.reset()                     # a restart re-reads the persisted value
    assert approval_policy.current() == ApprovalPolicy(read="auto", write="ask")


@pytest.mark.parametrize("body", [{"read": "maybe"}, {"foo": "auto"}, {"read": None},
                                  {"write": "ask", "foo": "auto"}, ["auto"], "auto"])
def test_patch_rejects_invalid_body_with_422_and_changes_nothing(client, body):
    http, path = client
    r = http.patch("/api/v1/approval-policy", json=body, headers=AUTH)
    assert r.status_code == 422, (r.status_code, r.text)
    assert not path.exists()
    assert approval_policy.current() == approval_policy.DEFAULT


def test_patch_after_a_corrupt_file_merges_onto_ask_ask(client):
    http, path = client
    path.write_text("{corrupt")
    approval_policy.reset()
    assert http.get("/api/v1/approval-policy", headers=AUTH).json()["write"] == "ask"
    r = http.patch("/api/v1/approval-policy", json={"read": "auto"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert json.loads(path.read_text()) == {"read": "auto", "write": "ask"}


def test_patch_save_failure_is_503_and_changes_nothing(client, monkeypatch):
    http, path = client
    approval_policy.replace(ApprovalPolicy(read="ask", write="default"))

    def unwritable(policy, data_root=None):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(approval_policy, "save", unwritable)
    r = http.patch("/api/v1/approval-policy", json={"write": "auto"}, headers=AUTH)
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == {"reason": "policy_save_failed"}
    assert approval_policy.current() == ApprovalPolicy(read="ask", write="default")
    assert not path.exists()


def test_policy_routes_require_auth(client):
    http, path = client
    assert http.get("/api/v1/approval-policy").status_code == 401
    r = http.patch("/api/v1/approval-policy", json={"write": "auto"})
    assert r.status_code == 401
    r = http.patch("/api/v1/approval-policy", json={"write": "auto"},
                   headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert not path.exists() and approval_policy.current() == approval_policy.DEFAULT


def test_tools_listing_carries_effect(client):
    http, _ = client
    r = http.get("/api/v1/tools", headers=AUTH)
    assert r.status_code == 200, r.text
    assert {t["name"]: t["effect"] for t in r.json()["tools"]} == EXPECTED_TOOLS


# ── Orchestrator gate obeys the policy ────────────────────────────────────────

def _run(make_session, tools, script, policy):
    from framework.core.orchestrator import Orchestrator
    from .fake_engine import FakeEngine, FakeRegistry
    from .test_orchestrator_lifecycles import GEN, begin_turn
    approval_policy.replace(policy)
    registry = FakeRegistry(tools)
    orch = Orchestrator(FakeEngine(script), registry,
                        {"react_max_steps": 4, "max_total_iterations": 12})
    session = make_session()
    session.add_user_message("go")
    begin_turn(session)
    return asyncio.run(orch.run_turn(session, journal=session.journal, **GEN)), registry


@pytest.mark.parametrize("approval,policy,expect_pause", [
    (False, ApprovalPolicy(write="ask"), True),       # tool never asks; user says ask
    (False, approval_policy.DEFAULT, False),          # default = the tool's own answer
    (True, approval_policy.DEFAULT, True),
    (True, ApprovalPolicy(write="auto"), False),      # tool always asks; user says auto
    (True, ApprovalPolicy(read="ask", write="auto"), False),
    (False, ApprovalPolicy(read="auto", write="ask"), True),
])
def test_gate_follows_policy_for_a_write_tool(make_session, approval, policy, expect_pause):
    from .fake_engine import FakeTool, call
    tools = [FakeTool("danger_op", approval=approval, read_only=False, concurrency_safe=False)]
    result, registry = _run(make_session, tools,
                            [{"tool_calls": [call("danger_op", target="A")]}, {"text": "done"}],
                            policy)
    if expect_pause:
        assert result["status"] == "pending_approval"
        assert [t["name"] for t in result["pending_tools"]] == ["danger_op"]
        assert registry.executed == []
    else:
        assert result["status"] != "pending_approval"
        assert registry.executed == [{"name": "danger_op", "args": {"target": "A"}}]


def test_gate_asks_for_a_read_only_shell_command_when_reads_are_ask(make_session, tmp_path):
    from framework.core.orchestrator import Orchestrator
    from .fake_engine import FakeEngine, call
    from .test_orchestrator_lifecycles import GEN, begin_turn

    class NeverExecute(ShellExecTool):
        def execute(self, **kwargs):
            pytest.fail("approval must precede shell execution")

    approval_policy.replace(ApprovalPolicy(read="ask", write="default"))
    registry = MCPRegistry(tmp_path / "empty-tools")
    registry.register(NeverExecute())
    engine = FakeEngine([{"tool_calls": [call(ShellExecTool.name, command="ls -la")]},
                         {"text": "ok"}])
    orch = Orchestrator(engine, registry, {"react_max_steps": 4, "max_total_iterations": 12})
    session = make_session()
    session.add_user_message("list")
    begin_turn(session)
    pending = asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))
    assert pending["status"] == "pending_approval"
    assert pending["pending_tools"][0]["arguments"] == {"command": "ls -la"}
    assert not any(e["type"] == "tool_started" for e in session.journal.ring)


# ── Unattended turns never get "auto" ─────────────────────────────────────────

SCHEDULE_TASK = {"id": "test", "name": "test", "prompt": "write and report"}


@pytest.mark.parametrize("stored", [ApprovalPolicy("auto", "auto"), ApprovalPolicy("ask", "auto"),
                                    ApprovalPolicy("ask", "ask"), ApprovalPolicy("auto", "ask")])
@pytest.mark.parametrize("origin", ["chat", "schedule", "anything-else"])
def test_effective_policy_is_pure_default_for_unattended_turns(origin, stored):
    from types import SimpleNamespace
    from framework.core.orchestrator import Orchestrator
    approval_policy.replace(stored)
    expected = stored if origin == "chat" else approval_policy.DEFAULT
    assert Orchestrator._approval_policy_for(SimpleNamespace(turn_origin=origin)) == expected


def _schedule_run(make_session, registry, engine):
    from types import SimpleNamespace
    from framework.core.scheduler import TaskScheduler
    from framework.schedule.runner import ScheduleRunner
    from .test_orchestrator_lifecycles import make_orch, run
    session = make_session("schedule_test")
    runner = ScheduleRunner(None, SimpleNamespace(get_or_create=lambda sid: session),
                            make_orch(engine, registry), TaskScheduler())
    record = {"status": "fail"}
    run(runner._execute_inner(SCHEDULE_TASK, record))
    return record, [e["type"] for e in session.journal.ring]


@pytest.mark.parametrize("stored,own_approval", [
    (ApprovalPolicy(write="auto"), True),               # stored auto is ignored unattended
    (ApprovalPolicy(read="auto", write="auto"), True),
    (ApprovalPolicy(read="ask", write="auto"), True),
])
def test_scheduled_turn_is_auto_denied_whatever_the_stored_auto(make_session, tmp_path,
                                                                stored, own_approval):
    from .fake_engine import FakeEngine, call
    from .test_execution_receipts import WritingRegistry
    approval_policy.replace(stored)
    registry = WritingRegistry(tmp_path / "must-not-exist.txt", approval=own_approval)
    engine = FakeEngine([{"tool_calls": [call("write_probe")]}, {"text": "acknowledged denial"}])
    record, types = _schedule_run(make_session, registry, engine)
    assert record["status"] == "denied_approval"
    assert not registry.path.exists() and registry.executed == []
    assert "awaiting_approval" in types and "tool_started" not in types


@pytest.mark.parametrize("stored", [ApprovalPolicy(write="ask"), ApprovalPolicy("ask", "ask")])
def test_scheduled_turn_ignores_stored_ask_for_writes(make_session, tmp_path, stored):
    from .fake_engine import FakeEngine, call
    from .test_execution_receipts import WritingRegistry
    approval_policy.replace(stored)
    registry = WritingRegistry(tmp_path / "effect.txt", approval=False)
    engine = FakeEngine([{"tool_calls": [call("write_probe")]}, {"text": "done"}])
    record, types = _schedule_run(make_session, registry, engine)
    assert record["status"] == "success"
    assert registry.path.read_text() == "effect remains"
    assert "awaiting_approval" not in types


def test_scheduled_turn_with_read_ask_still_runs_read_tools(make_session):
    from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
    approval_policy.replace(ApprovalPolicy(read="ask", write="default"))
    registry = FakeRegistry([FakeTool("lookup", result="42", read_only=True)])
    engine = FakeEngine([{"tool_calls": [call("lookup", q="x")]}, {"text": "the answer is 42"}])
    record, types = _schedule_run(make_session, registry, engine)
    assert record["status"] == "success"
    assert registry.executed == [{"name": "lookup", "args": {"q": "x"}}]
    assert "awaiting_approval" not in types


def test_interactive_turn_with_the_same_tool_and_stored_auto_executes(make_session, tmp_path):
    from .fake_engine import FakeEngine, call
    from .test_execution_receipts import WritingRegistry
    from .test_turn_runner import make_runner, submit
    approval_policy.replace(ApprovalPolicy(write="auto"))
    registry = WritingRegistry(tmp_path / "effect.txt", approval=True)
    runner = make_runner(FakeEngine([{"tool_calls": [call("write_probe")]}, {"text": "done"}]),
                         registry)
    session = make_session()
    asyncio.run(submit(runner, session, "fixture-session"))
    assert registry.executed == [{"name": "write_probe", "args": {}}]
    assert registry.path.read_text() == "effect remains"
    assert "awaiting_approval" not in [e["type"] for e in session.journal.replay(0)]


# ── Orchestrator-initiated cloud consults obey the policy ─────────────────────

class _Flaky(_Fixture):
    name = "flaky"
    effect = "read"

    def execute(self, **kwargs):
        return "System Error: boom"


def _cloud_turn(make_session, monkeypatch, tmp_path, path, policy, config_require_approval):
    """path=explicit: turn-start consult_auto (orchestrator.py ~765);
    path=failures: consult_auto after repeated failures (~1178).
    The consult tool is the real CloudConsultTool with only its egress stubbed, so its
    needs_approval reads the real config knob (cloud.require_approval, patched here)."""
    from framework.cloud.degrade import CLOUD_CONSULT_TOOL
    from framework.mcp.tools.cloud_consult import CloudConsultTool
    from framework.utils import config_loader
    from .fake_engine import FakeEngine, call
    from .test_cloud_upgrade import cloud, make_orch
    from .test_orchestrator_lifecycles import GEN, begin_turn

    executed = []

    class StubbedConsult(CloudConsultTool):
        def __init__(self):
            pass                                # no provider/key lookup

        def execute(self, **kwargs):
            executed.append(CLOUD_CONSULT_TOOL)
            return "[cloud advisor · oa/m1] advice text"

    real_load_section = config_loader.load_section
    monkeypatch.setattr(config_loader, "load_section", lambda name: (
        {"require_approval": config_require_approval} if name == "cloud" else real_load_section(name)))
    monkeypatch.setenv("T_UP_KEY", "k")
    approval_policy.replace(policy)
    registry = MCPRegistry(tmp_path / "empty-tools")
    registry.register(StubbedConsult())
    registry.register(_Flaky())
    if path == "explicit":
        prompt, script = "问问云端导师：这个方案可行吗", [{"text": "ok"}] * 4
    else:
        prompt = "做这件事"
        script = [{"tool_calls": [call("flaky", n=1)]}, {"tool_calls": [call("flaky", n=2)]}] \
            + [{"text": "ok"}] * 4
    orch = make_orch(FakeEngine(script), registry, cloud_cfg=cloud("auto"))
    session = make_session()
    session.add_user_message(prompt)
    begin_turn(session)
    result = asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))
    upgrades = [e["data"]["action"] for e in session.journal.ring if e["type"] == "upgrade_triggered"]
    assert upgrades and upgrades[0] == "consult_auto", upgrades     # the path under test fired
    paused = result["status"] == "pending_approval"
    if paused:
        assert result["pending_tools"][0]["name"] == CLOUD_CONSULT_TOOL
    return paused, executed


@pytest.mark.parametrize("path", ["explicit", "failures"])
@pytest.mark.parametrize("policy,config,expect_pause", [
    (ApprovalPolicy(write="ask"), False, True),          # ask forces asking over the config
    (ApprovalPolicy(write="auto"), True, True),          # config require_approval=true is pinned
    (ApprovalPolicy(write="auto"), False, False),        # config off: auto applies
    (approval_policy.DEFAULT, True, True),               # default follows the config knob
    (approval_policy.DEFAULT, False, False),
])
def test_orchestrator_initiated_consult_follows_policy(make_session, monkeypatch, tmp_path, path,
                                                       policy, config, expect_pause):
    paused, executed = _cloud_turn(make_session, monkeypatch, tmp_path, path, policy, config)
    assert paused is expect_pause
    assert executed == ([] if expect_pause else ["cloud_consult"])


def test_orchestrator_initiated_consult_is_classified_as_write(make_session, monkeypatch, tmp_path):
    # read=ask must not catch the consult (it is a write; the failures path is not used
    # here because its flaky fixture is itself a read tool and would ask first).
    paused, executed = _cloud_turn(make_session, monkeypatch, tmp_path, "explicit",
                                   ApprovalPolicy(read="ask", write="default"), False)
    assert paused is False and executed == ["cloud_consult"]


# ── Unknown tool names: build-77 behaviour (no card, same error) ──────────────
# Literal captured by running the same scenario on a pristine 5162fc6 export
# (.b78-evidence/unknown_tool_baseline_5162fc6.json).
BASELINE_UNKNOWN_RESPONSES = [[{
    "name": "no_such_tool",
    "response": "System Error: No tool named 'no_such_tool' was found in the MCP registry.",
    "_failed": True}]]
BASELINE_UNKNOWN_TYPES = ["turn_queued", "turn_started", "iteration_started", "tool_call_pending",
                          "tool_started", "tool_result", "iteration_started", "token_delta",
                          "iteration_started", "token_delta"]


@pytest.mark.parametrize("read", MODES)
@pytest.mark.parametrize("write", MODES)
def test_hallucinated_tool_name_fails_as_before_under_every_mode(make_session, tmp_path, read, write):
    from framework.core.orchestrator import Orchestrator
    from .fake_engine import FakeEngine, call
    from .test_orchestrator_lifecycles import GEN, begin_turn
    approval_policy.replace(ApprovalPolicy(read=read, write=write))
    registry = MCPRegistry(tmp_path / "empty-tools")
    engine = FakeEngine([{"tool_calls": [call("no_such_tool", x=1)]}] + [{"text": "done"}] * 4)
    orch = Orchestrator(engine, registry, {"react_max_steps": 4, "max_total_iterations": 12})
    session = make_session()
    session.add_user_message("go")
    begin_turn(session)
    result = asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))
    assert result["status"] != "pending_approval"
    assert [m["tool_responses"] for m in session.history if m.get("role") == "tool"] \
        == BASELINE_UNKNOWN_RESPONSES
    types = [e["type"] for e in session.journal.ring]
    assert "awaiting_approval" not in types
    assert types == BASELINE_UNKNOWN_TYPES


# ── PATCH is owner-only; both routes work before the first assembly ───────────

def test_patch_with_a_device_ticket_is_forbidden_get_is_allowed(client):
    import app as entry
    http, path = client
    token, _, _ = entry.app.state.device_store.issue(name="iPhone", via="qr")
    device = {"Authorization": f"Bearer {token}"}
    r = http.patch("/api/v1/approval-policy", json={"write": "auto"}, headers=device)
    assert r.status_code == 403, r.text
    assert not path.exists() and approval_policy.current() == approval_policy.DEFAULT
    assert http.get("/api/v1/approval-policy", headers=device).status_code == 200


def test_patch_route_mounts_require_owner():
    import app as entry
    from framework.api.auth import require_owner
    routes = [r for r in entry.app.routes if getattr(r, "path", "") == "/api/v1/approval-policy"]
    deps = {method: {d.call for d in r.dependant.dependencies}
            for r in routes for method in r.methods}
    assert require_owner in deps["PATCH"] and require_owner not in deps["GET"]
    assert entry.verify_api_key in deps["PATCH"] and entry.verify_api_key in deps["GET"]


def test_policy_routes_work_before_the_first_assembly(client, monkeypatch):
    import app as entry
    http, path = client
    for name, value in (("compute_ready", False), ("compute_installed", False),
                        ("compute_state", "retrying")):
        monkeypatch.setattr(entry.app.state, name, value, raising=False)
    monkeypatch.delattr(entry.app.state, "mcp_registry", raising=False)
    r = http.get("/api/v1/approval-policy", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"read": "default", "write": "default", "tools": {}, "dynamic": []}
    r = http.patch("/api/v1/approval-policy", json={"read": "ask"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert json.loads(path.read_text()) == {"read": "ask", "write": "default"}
    # control: an ordinary non-local route is still gated before assembly
    assert http.get("/api/v1/tools", headers=AUTH).status_code == 503
