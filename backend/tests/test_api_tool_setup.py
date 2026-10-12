"""Synthetic registration-to-setup journeys; no service or live credentials."""
import asyncio
from types import SimpleNamespace
import pytest
import yaml
from framework.mcp.base import BaseTool
from framework.mcp.registry import MCPRegistry
from framework.mcp.tools import api_tool_factory as factory
from framework.core import pending_registrations as pending
from framework.api import routes
from framework.api.schemas import ApiToolPatchRequest

@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('CODEX_SYNTHETIC_SETUP_KEY', raising=False)
    monkeypatch.setattr(factory, 'DECL_DIR', tmp_path/'repo-declarations')
    # Any accidental HTTP request in registration is a test failure.
    import requests
    monkeypatch.setattr(requests, 'request', lambda *a, **k: pytest.fail('registration must not execute HTTP'))
    reg = MCPRegistry(tmp_path/'empty-tools')
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mcp_registry=reg,
        orchestrator=SimpleNamespace(refresh_tools_declaration=lambda: None))))
    decl = {'name':'setup_fixture','description':'fixture','endpoint':'https://fixture.invalid/action',
        'method':'POST','auth':{'env':'CODEX_SYNTHETIC_SETUP_KEY'},'requires_approval':True,
        'headers':{'X-Synthetic':'keep'},'params':{'properties':{'nested':{'type':'object','additionalProperties':False}},
        'additionalProperties':False},'future_metadata':{'keep':True}}
    return reg, req, decl

def item(req): return asyncio.run(routes.list_api_tools(req))['tools'][0]

def test_confirm_missing_key_then_patch_same_saved_object(setup):
    reg, req, decl = setup
    rid = pending.submit('api_tool', decl['name'], decl)
    assert not list(factory.user_decl_dir().glob('*.yaml'))
    result = asyncio.run(routes.confirm_registration(rid, req))
    assert result['effect'] == 'unavailable'
    assert pending.get(rid) is None
    first = item(req)
    assert first['registered'] is False and first['has_secret'] is False
    assert first['runtime_declaration_current'] is None
    key = 'synthetic-key-value'
    result = asyncio.run(routes.patch_api_tool(decl['name'], req, ApiToolPatchRequest(secret=key)))
    assert result['effect'] == 'registered'
    saved = yaml.safe_load((factory.user_decl_dir()/f"{decl['name']}.yaml").read_text())
    assert saved == decl and key not in str(saved)
    assert factory.secrets_path().stat().st_mode & 0o777 == 0o600
    assert pending.list_pending() == []
    final = item(req)
    assert final['name'] == first['name'] and final['registered'] is True
    assert final['runtime_declaration_current'] is True and final['has_secret'] is True
    assert key not in str(final)
    assert len(reg.get_all_schemas()) == 1
    # A stale unavailable instance must never replace the accepted current instance.
    current = reg._tools[decl['name']]
    assert reg.rescan_unavailable() == []
    assert reg._tools[decl['name']] is current

def test_edit_registered_version_stays_old_until_restart(setup):
    reg, req, decl = setup
    factory.write_secret('CODEX_SYNTHETIC_SETUP_KEY','synthetic-key')
    asyncio.run(routes._save_api_tool_impl(req, decl))
    current = reg._tools[decl['name']]
    assert item(req)['runtime_declaration_current'] is True
    result = asyncio.run(routes.patch_api_tool(decl['name'], req, ApiToolPatchRequest(description='new saved version')))
    assert result['effect'] == 'restart_required'
    assert reg._tools[decl['name']] is current
    assert item(req)['registered'] is True
    assert item(req)['runtime_declaration_current'] is False
    assert item(req)['description'] == 'new saved version'
    assert current.description == 'fixture'

def test_latest_unavailable_declaration_is_the_one_rescanned(setup):
    reg, req, decl = setup
    asyncio.run(routes._save_api_tool_impl(req, decl))
    updated = {**decl, 'description':'latest pending settings'}
    asyncio.run(routes._save_api_tool_impl(req, updated))
    factory.write_secret('CODEX_SYNTHETIC_SETUP_KEY','synthetic-key')
    assert reg.rescan_unavailable() == [decl['name']]
    assert reg._tools[decl['name']].description == updated['description']
    assert len(reg.get_all_schemas()) == 1
    assert item(req)['runtime_declaration_current'] is True

def test_unknown_nonfactory_runtime_is_not_claimed_current(setup):
    reg, req, decl = setup
    asyncio.run(routes._save_api_tool_impl(req, decl))
    reg._tools[decl['name']] = SimpleNamespace(name=decl['name'])
    listed = item(req)
    assert listed['registered'] is True
    assert listed['runtime_declaration_current'] is None

def test_disabled_saved_declaration_reports_pending_runtime_change(setup):
    reg, req, decl = setup
    factory.write_secret('CODEX_SYNTHETIC_SETUP_KEY','synthetic-key')
    asyncio.run(routes._save_api_tool_impl(req, decl))
    asyncio.run(routes.patch_api_tool(decl['name'], req, ApiToolPatchRequest(enabled=False)))
    listed = item(req)
    assert listed['enabled'] is False and listed['registered'] is True
    assert listed['runtime_declaration_current'] is False


def test_invalid_auth_declaration_remains_a_diagnostic_row(setup):
    _, req, decl = setup
    directory = factory.user_decl_dir()
    directory.mkdir(parents=True)
    (directory/'bad.yaml').write_text(yaml.safe_dump({**decl, 'auth': 'bad'}))
    result = item(req)
    assert result['problem'] and result['name'] == decl['name']
    assert result['registered'] is False and result['has_secret'] is False
    assert result['runtime_declaration_current'] is None


def test_invalid_metadata_guard_mutation(setup, tmp_path):
    import ast
    import inspect
    _, req, decl = setup
    directory = factory.user_decl_dir()
    directory.mkdir(parents=True)
    (directory/'bad.yaml').write_text(yaml.safe_dump({**decl, 'auth': 'bad'}))
    source = inspect.getsource(routes.list_api_tools)
    start = source.index('        if item["problem"]:')
    end = source.index('        name = decl.get("name")', source.index('            decl = ', start))
    copy = tmp_path/'list_mutant.py'
    copy.write_text(source[:start] + source[end:])
    tree = ast.parse(copy.read_text())
    tree.body[0].decorator_list = []  # Do not register a new route in the live router.
    namespace = dict(vars(routes))
    exec(compile(tree, str(copy), 'exec'), namespace)
    with pytest.raises(AttributeError, match="has no attribute 'get'") as failure:
        asyncio.run(namespace['list_api_tools'](req))
    (tmp_path/'mutation-output.txt').write_text(str(failure.value))
    print('Mutation invalid-metadata: intended failure:', failure.value)


class _Candidate(BaseTool):
    name = 'synthetic_candidate'
    description = 'fixture'
    parameters = {}
    available = False

    def is_available(self):
        return self.available

    def execute(self, **arguments):
        raise AssertionError('setup does not execute tools')


def _exercise_candidates(registry, mode):
    old = _Candidate()
    registry.load_tool(old)
    current = _Candidate()
    if mode == 'registered':
        # Registration from another existing loading path must also survive rescan.
        registry.register(current)
    else:
        registry.load_tool(current)
    old.available = current.available = True
    registry.rescan_unavailable()
    assert registry._tools[current.name] is current, 'rescan must retain the current candidate'
    assert len(registry.get_all_schemas()) == 1


@pytest.mark.parametrize('mode', ['registered', 'latest'])
def test_rescan_keeps_current_candidate(tmp_path, mode):
    _exercise_candidates(MCPRegistry(tmp_path/'tools'), mode)


@pytest.mark.parametrize('mode', ['registered', 'latest'])
def test_rescan_guard_mutation(tmp_path, mode):
    from pathlib import Path
    import framework.mcp.registry as module
    source = Path(module.__file__).read_text()
    target = ('                if tool.name in self._tools:\n'
              '                    self._discarded = [old for old in self._discarded if old is not tool]\n'
              '                    continue  # Rescan only adds; running instances keep their identity.\n') if mode == 'registered' else (
              '            self.discard_unavailable(tool.name)\n')
    assert source.count(target) == 1
    mutant = tmp_path/'registry.py'
    mutant.write_text(source.replace(target, ''))
    namespace = {'__name__': 'framework.mcp.synthetic_registry', '__package__': 'framework.mcp'}
    exec(compile(mutant.read_text(), str(mutant), 'exec'), namespace)
    with pytest.raises(AssertionError, match='rescan must retain the current candidate') as failure:
        _exercise_candidates(namespace['MCPRegistry'](tmp_path/'tools'), mode)
    (tmp_path/'mutation-output.txt').write_text(str(failure.value))
    print('Mutation rescan-'+mode+': intended failure:', failure.value)


def _withdraw(req, name, operation):
    if operation == "disable":
        result = asyncio.run(routes.patch_api_tool(name, req, ApiToolPatchRequest(enabled=False)))
        assert result["effect"] == "saved_disabled"
    else:
        result = asyncio.run(routes.delete_api_tool(name, req))
        assert result["effect"] == "deleted"


def _withdrawn_journey(setup, monkeypatch, operation):
    reg, req, decl = setup
    assert asyncio.run(routes._save_api_tool_impl(req, decl))["effect"] == "unavailable"
    _withdraw(req, decl["name"], operation)
    monkeypatch.setenv("CODEX_SYNTHETIC_SETUP_KEY", "synthetic-only")
    assert reg.rescan_unavailable() == [], "withdrawn candidate revived"
    assert decl["name"] not in reg.tool_names()


@pytest.mark.parametrize("operation", ["disable", "delete"])
def test_withdrawn_unavailable_candidate_cannot_register_later(setup, monkeypatch, operation):
    _withdrawn_journey(setup, monkeypatch, operation)


@pytest.mark.parametrize("operation", ["disable", "delete"])
def test_withdrawal_cancels_probe_queued_before_worker_starts(setup, monkeypatch, operation):
    reg, req, decl = setup
    async def journey():
        queued, release = asyncio.Event(), asyncio.Event()
        async def held_thread(fn, *args, **kwargs):
            queued.set()
            await release.wait()
            return fn(*args, **kwargs)
        monkeypatch.setattr(routes.asyncio, "to_thread", held_thread)
        saving = asyncio.create_task(routes._save_api_tool_impl(req, decl))
        await asyncio.wait_for(queued.wait(), 2)
        try:
            if operation == "disable":
                result = await routes.patch_api_tool(decl["name"], req, ApiToolPatchRequest(enabled=False))
                assert result["effect"] == "saved_disabled"
            else:
                assert (await routes.delete_api_tool(decl["name"], req))["effect"] == "deleted"
            monkeypatch.setenv("CODEX_SYNTHETIC_SETUP_KEY", "synthetic-only")
        finally:
            release.set()
            await saving
        assert reg.rescan_unavailable() == [], "queued stale candidate revived"
        assert decl["name"] not in reg.tool_names(), "queued stale candidate registered"
    asyncio.run(journey())


def _withdraw_during_rescan(setup, operation, available):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    reg, req, decl = setup
    asyncio.run(routes._save_api_tool_impl(req, decl))
    candidate = reg._discarded[0]
    entered, release = Event(), Event()
    def held_probe():
        entered.set()
        assert release.wait(3), "test failed to release probe"
        return available
    candidate.is_available = held_probe
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(reg.rescan_unavailable)
        try:
            assert entered.wait(3), "probe did not start"
            _withdraw(req, decl["name"], operation)
        finally:
            release.set()
        assert work.result(timeout=3) == [], "in-flight withdrawn candidate revived"
    assert not reg._discarded, "withdrawn candidate reinserted"
    assert decl["name"] not in reg.tool_names()


@pytest.mark.parametrize("operation", ["disable", "delete"])
@pytest.mark.parametrize("available", [False, True])
def test_withdrawal_during_rescan_cannot_register_or_reinsert(setup, operation, available):
    _withdraw_during_rescan(setup, operation, available)


def test_revocation_keeps_unrelated_candidate_and_registered_instance(setup, monkeypatch):
    reg, req, decl = setup
    asyncio.run(routes._save_api_tool_impl(req, decl))
    other = {**decl, "name": "other_fixture"}
    asyncio.run(routes._save_api_tool_impl(req, other))
    _withdraw(req, decl["name"], "delete")
    monkeypatch.setenv("CODEX_SYNTHETIC_SETUP_KEY", "synthetic-only")
    assert reg.rescan_unavailable() == [other["name"]]
    current = reg._tools[other["name"]]
    assert asyncio.run(routes.delete_api_tool(other["name"], req))["effect"] == "restart_required"
    assert reg._tools[other["name"]] is current


@pytest.mark.parametrize("operation", ["disable", "delete"])
def test_withdrawal_guard_mutation(setup, monkeypatch, tmp_path, operation):
    import ast
    import inspect
    name = "_save_api_tool_impl" if operation == "disable" else "delete_api_tool"
    source = inspect.getsource(getattr(routes, name))
    target = '    registry.discard_unavailable(decl["name"])\n' if operation == "disable" else '    registry.discard_unavailable(name)\n'
    assert source.count(target) == 1
    mutated = source.replace(target, "")
    path = tmp_path/"withdrawal_mutant.py"
    path.write_text(mutated)
    tree = ast.parse(mutated); tree.body[0].decorator_list = []
    namespace = dict(vars(routes))
    exec(compile(tree, str(path), "exec"), namespace)
    monkeypatch.setattr(routes, name, namespace[name])
    with pytest.raises(AssertionError, match="withdrawn candidate revived") as failure:
        _withdrawn_journey(setup, monkeypatch, operation)
    (tmp_path/"mutation-output.txt").write_text(str(failure.value))
    print("Mutation withdrawal-" + operation + ": intended failure:", failure.value)


def test_inflight_rescan_revocation_guard_mutation(setup, monkeypatch, tmp_path):
    import framework.mcp.registry as module
    from pathlib import Path
    reg, req, decl = setup
    source = Path(module.__file__).read_text()
    target = '                if not any(old is tool for old in self._discarded):\n                    continue\n'
    assert source.count(target) == 1
    path = tmp_path/"registry_mutant.py"; path.write_text(source.replace(target, ""))
    namespace = {"__name__": "framework.mcp.synthetic_registry", "__package__": "framework.mcp"}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    mutant = namespace["MCPRegistry"](tmp_path/"tools")
    req.app.state.mcp_registry = mutant
    with pytest.raises(AssertionError, match="in-flight withdrawn candidate revived") as failure:
        _withdraw_during_rescan((mutant, req, decl), "delete", True)
    (tmp_path/"mutation-output.txt").write_text(str(failure.value))
    print("Mutation rescan-withdrawal: intended failure:", failure.value)

@pytest.mark.parametrize("scanning", [False, True])
def test_new_candidate_survives_old_probe_completion(tmp_path, scanning):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    reg = MCPRegistry(tmp_path/"tools")
    old, current = _Candidate(), _Candidate()
    entered, release = Event(), Event()
    if scanning:
        reg.load_tool(old)
    def held_probe():
        entered.set()
        assert release.wait(3)
        return True
    old.is_available = held_probe
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(reg.rescan_unavailable if scanning else lambda: reg.load_tool(old))
        try:
            assert entered.wait(3)
            assert reg.load_tool(current) is False
        finally:
            release.set()
        result = work.result(timeout=3)
        assert result == ([] if scanning else False)
    assert reg._discarded == [current]
    assert not reg.tool_names()
    current.available = True
    assert reg.rescan_unavailable() == [current.name]
    assert reg._tools[current.name] is current


def test_queued_probe_revocation_guard_mutation(setup, monkeypatch, tmp_path):
    import framework.mcp.registry as module
    from pathlib import Path
    _, req, decl = setup
    source = Path(module.__file__).read_text()
    target = ('            if not any(old is tool for old in self._discarded):\n'
              '                return self._tools.get(tool.name) is tool\n')
    assert source.count(target) == 1
    path = tmp_path/"registry_mutant.py"; path.write_text(source.replace(target, ""))
    namespace = {"__name__": "framework.mcp.synthetic_registry", "__package__": "framework.mcp"}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    reg = namespace["MCPRegistry"](tmp_path/"tools")
    req.app.state.mcp_registry = reg
    with pytest.raises(AssertionError, match="queued stale candidate registered") as failure:
        test_withdrawal_cancels_probe_queued_before_worker_starts((reg, req, decl), monkeypatch, "delete")
    (tmp_path/"mutation-output.txt").write_text(str(failure.value))
    print("Mutation queued-withdrawal: intended failure:", failure.value)
