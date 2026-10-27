"""Contract H: /health splits engine / compute node / RAG readiness; local-only retry.

CPU-only. No socket, service, real model, credentials or user data: the lifespan runs
on the synthetic `offline_app` fixture, and lib_edge runs with stubbed spawn/wait.
"""
import asyncio

import httpx
import pytest
from fastapi import FastAPI

from framework.api import compute_lifecycle as mod

from .test_edge_offline_start import run_shell
from .test_offline_startup import eventually, offline_app  # noqa: F401  (fixture)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _record_retry_sleeps(monkeypatch, clock=None, *, yield_only=False):
    """Record the sleeps of the compute-startup task only (other tasks sleep normally).

    Each entry is (delay, compute_state at the time) so assembly retries can be told
    apart from post-ready monitor ticks.
    """
    original = asyncio.sleep
    delays = []

    async def sleep(delay, *args, **kwargs):
        task = asyncio.current_task()
        if task is not None and task.get_name() == "compute-startup":
            app_state = sleep.app.state if sleep.app is not None else None
            delays.append((delay, getattr(app_state, "compute_state", None)))
            if clock is not None:
                clock.now += delay
            if yield_only:
                return await original(0)
        return await original(delay, *args, **kwargs)

    sleep.app = None
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return delays, sleep


def _owner(app, assemble, probe=None, **kwargs):
    async def cleanup(resources):
        return None
    return mod.ComputeLifecycle(app, assemble, lambda resources: None, cleanup, probe, **kwargs)


def _local_failure():
    try:
        raise ConnectionError("synthetic RAG still loading")
    except ConnectionError as exc:
        try:
            raise mod.LocalDependencyUnavailable("RAG 数据面未就绪") from exc
        except mod.LocalDependencyUnavailable as wrapped:
            return wrapped


# ---------------------------------------------------------------------------
# Retry schedule (ComputeLifecycle, production-sized intervals, fake sleep/clock)
# ---------------------------------------------------------------------------

def _run_failures(monkeypatch, failures, **kwargs):
    clock = _Clock()
    delays, sleep = _record_retry_sleeps(monkeypatch, clock, yield_only=True)

    async def scenario():
        app = FastAPI()
        sleep.app = app
        queue = list(failures)

        async def assemble():
            if queue:
                raise queue.pop(0)()
            return {"engine": object()}

        owner = _owner(app, assemble, clock=clock, **kwargs)
        owner.start()
        await asyncio.wait_for(owner.task, 5)
        assert app.state.compute_state == "ready" and app.state.engine_ready
        await owner.stop()

    asyncio.run(scenario())
    return [delay for delay, _ in delays]


def test_local_only_failure_retries_at_fixed_initial_interval(monkeypatch):
    delays = _run_failures(monkeypatch, [_local_failure] * 10)
    assert delays == [2] * 10, delays


def test_local_waiting_is_bounded_then_falls_back_to_backoff(monkeypatch):
    # 2 s steps inside the 120 s window (t = 0..118 → 60 local retries), then backoff.
    delays = _run_failures(monkeypatch, [_local_failure] * 66)
    assert delays[:60] == [2] * 60, delays[:60]
    assert delays[60:] == [2, 4, 8, 16, 30, 30], delays[60:]


def test_compute_node_failure_keeps_exponential_backoff(monkeypatch):
    def compute_down():
        return ConnectionError("synthetic compute node offline")
    delays = _run_failures(monkeypatch, [compute_down] * 6)
    assert delays == [2, 4, 8, 16, 30, 30], delays


def test_local_retries_do_not_advance_the_backoff(monkeypatch):
    def compute_down():
        return ConnectionError("synthetic compute node offline")
    delays = _run_failures(monkeypatch, [compute_down, _local_failure, _local_failure, compute_down])
    assert delays == [2, 2, 2, 4], delays


def test_local_failure_is_diagnosed_by_its_original_error(capsys):
    request = httpx.Request("GET", "http://127.0.0.1:14160/api/v1/category")
    original = httpx.HTTPStatusError("Client error '401 Unauthorized'", request=request,
                                     response=httpx.Response(401, request=request))
    try:
        raise mod.LocalDependencyUnavailable("RAG 数据面未就绪") from original
    except mod.LocalDependencyUnavailable as exc:
        diagnosis = mod.ComputeLifecycle._diagnose(exc)
    assert diagnosis.startswith("HTTPStatusError: ") and "401" in diagnosis
    assert "RAG_API_KEY" in diagnosis


# ---------------------------------------------------------------------------
# engine_ready and initial states (ComputeLifecycle)
# ---------------------------------------------------------------------------

def test_initial_states_follow_contract_h():
    app = FastAPI()
    _owner(app, None)
    assert (app.state.engine_ready, app.state.compute_node_state, app.state.rag_state) == (
        False, "local", "skipped")

    async def probe():
        return None
    _owner(app, None, probe)
    assert (app.state.compute_node_state, app.state.rag_state) == ("probing", "probing")


def test_engine_ready_survives_monitor_degradation_until_stop():
    async def scenario():
        app = FastAPI()
        outcomes, entered = asyncio.Queue(), asyncio.Event()

        async def assemble():
            return {"engine": object()}

        async def probe():
            entered.set()
            if await outcomes.get() == "fail":
                raise ConnectionError("synthetic probe outage")

        owner = _owner(app, assemble, probe, retry_initial=0.001, retry_max=0.002,
                       monitor_interval=0.001)
        assert app.state.engine_ready is False
        owner.start()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert app.state.engine_ready is True
            for _ in range(mod.MONITOR_FAILURES_BEFORE_UNAVAILABLE):
                entered.clear()
                await outcomes.put("fail")
                await asyncio.wait_for(entered.wait(), 2)
            assert app.state.compute_state == "unavailable" and not app.state.compute_ready
            assert app.state.engine_ready is True, "a degraded compute node does not uninstall the engine"
        finally:
            await owner.stop()
        assert app.state.engine_ready is False and app.state.compute_state == "stopped"

    asyncio.run(scenario())


@pytest.mark.parametrize("path,status,expected", [
    ("/metrics", 401, "unauthorized"), ("/metrics", 403, "unauthorized"),
    ("/metrics", 500, "unreachable"), ("/health", 401, "unreachable")])
def test_compute_node_failure_state_from_http_status(path, status, expected):
    request = httpx.Request("GET", f"http://synthetic:14161{path}")
    exc = httpx.HTTPStatusError("synthetic", request=request,
                                response=httpx.Response(status, request=request))
    assert mod.compute_node_failure_state(exc) == expected


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("synthetic refused"), ConnectionError("synthetic"),
    RuntimeError("Inference health not confirmed")])
def test_compute_node_failure_state_other_failures_are_unreachable(exc):
    assert mod.compute_node_failure_state(exc) == "unreachable"


# ---------------------------------------------------------------------------
# Lifespan + /health (synthetic offline_app fixture: FakeEngine, stubbed probes)
# ---------------------------------------------------------------------------

async def _health(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return (await client.get("/health")).json()


def test_rag_down_compute_up_records_states_and_retries_at_fixed_interval(offline_app, monkeypatch):
    app, control, _ = offline_app
    control.infer = True
    delays, sleep = _record_retry_sleeps(monkeypatch)
    sleep.app = app

    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: len(delays) >= 3)
            health = await _health(app)
            assert health["compute_state"] == "retrying" and not health["engine_ready"]
            assert health["compute_node_state"] == "ready"
            assert health["rag_state"] == "unreachable"
            retry_delays = [d for d, state in delays if state == "retrying"]
            # offline_app: retry_initial=0.01, retry_max=0.02 → backoff would be 0.02
            assert retry_delays[:3] == [0.01] * 3, retry_delays
            control.rag = True
            await eventually(lambda: app.state.compute_ready)
            health = await _health(app)
            assert (health["engine_ready"], health["compute_node_state"], health["rag_state"]) == (
                True, "ready", "ready")
    asyncio.run(scenario())


def test_compute_down_records_unreachable_and_backs_off(offline_app, monkeypatch):
    app, control, _ = offline_app
    delays, sleep = _record_retry_sleeps(monkeypatch)
    sleep.app = app

    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: len(delays) >= 3)
            health = await _health(app)
            assert health["compute_node_state"] == "unreachable"
            assert health["rag_state"] == "probing"  # never reached behind the compute probe
            assert [d for d, _ in delays][:3] == [0.01, 0.02, 0.02], delays
    asyncio.run(scenario())


def test_compute_unauthorized_is_reported(offline_app, monkeypatch):
    app, control, _ = offline_app

    async def probe(*args):
        request = httpx.Request("GET", "http://synthetic:1/metrics")
        raise httpx.HTTPStatusError("Client error '401 Unauthorized'", request=request,
                                    response=httpx.Response(401, request=request))
    monkeypatch.setattr(mod, "probe_remote_compute", probe)

    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: app.state.compute_node_state != "probing")
            assert (await _health(app))["compute_node_state"] == "unauthorized"
    asyncio.run(scenario())


def test_discovery_failure_reports_unreachable(offline_app, monkeypatch):
    import app as entry
    from framework.utils import model_selection as ms
    app, control, _ = offline_app
    monkeypatch.setattr(entry, "_model_source", "default")

    def discover(base):
        raise ConnectionError("synthetic compute offline")
    monkeypatch.setattr(ms, "discover_remote_model", discover)

    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: app.state.compute_state == "retrying")
            assert app.state.compute_node_state == "unreachable"
            assert control.probes == 0  # failed before probe_remote_compute
    asyncio.run(scenario())


def test_monitor_refreshes_node_and_rag_state_and_engine_stays_ready(offline_app, monkeypatch):
    app, control, _ = offline_app
    control.infer = control.rag = True
    inner = mod.probe_remote_compute
    seen = []

    async def probe(*args):
        # Snapshot at each monitor entry = the state the previous tick published.
        if getattr(app.state, "compute_installed", False):
            seen.append((app.state.compute_state, app.state.compute_node_state))
        return await inner(*args)
    monkeypatch.setattr(mod, "probe_remote_compute", probe)

    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: app.state.compute_ready)
            control.rag = False
            await eventually(lambda: app.state.rag_state == "unreachable")
            assert app.state.compute_state == "ready"
            control.rag = True
            await eventually(lambda: app.state.rag_state == "ready")
            control.infer = False
            await eventually(lambda: app.state.compute_state == "unavailable")
            health = await _health(app)
            assert health["engine_ready"] is True
            assert health["compute_node_state"] == "unreachable"
            control.infer = True
            await eventually(lambda: app.state.compute_node_state == "ready")
            await eventually(lambda: len(seen) >= 2 and seen[-1] == ("ready", "ready"))
        assert app.state.engine_ready is False
    asyncio.run(scenario())
    # Hysteresis: the node state flips with compute_state, never ahead of it.
    assert all((state == "ready") == (node == "ready") for state, node in seen), seen
    assert ("ready", "unreachable") not in seen


def test_health_defaults_before_lifespan(offline_app, monkeypatch):
    import app as entry
    app, _, _ = offline_app

    async def scenario(expected_node, expected_rag):
        bare = FastAPI()
        bare.add_api_route("/health", entry.health)
        monkeypatch.setattr(entry, "app", bare)
        health = await _health(bare)
        assert (health["engine_ready"], health["compute_node_state"], health["rag_state"]) == (
            False, expected_node, expected_rag)
        for field in ("status", "service", "layout", "key_fp", "data_ready",
                      "compute_ready", "compute_state"):
            assert field in health

    asyncio.run(scenario("probing", "probing"))   # fixture config: inference.mode=remote
    monkeypatch.setattr(entry, "config", {"inference": {"mode": "inprocess"}})
    asyncio.run(scenario("local", "skipped"))


# ---------------------------------------------------------------------------
# lib_edge: remote edge_up spawns backend and RAG before waiting for either
# ---------------------------------------------------------------------------

_EDGE_STUBS = r'''
is_ready() { return 1; }
guard_port_before_spawn() { return 0; }
rag_is_local() { return 0; }
EDGE_RAG_PY=/usr/bin/true
EDGE_RAG_DIR="$EDGE_LOG_DIR"
spawn_daemon() {
    local name; name="$(basename "$3" .pid)"
    echo "SPAWN $name"
    [ "$name" = "${FAIL_SPAWN:-}" ] && return 1
    echo $$ > "$3"
}
wait_ready() {
    echo "WAIT $1"
    [ "$1" = "${FAIL_WAIT:-}" ] && return 1
    return 0
}
start_searx() { echo SEARCH; return 0; }
stop_rag_local() { echo "STOP rag"; return 0; }
INFER_MODE=remote
'''


def _edge(tmp_path, tail):
    result = run_shell(tmp_path, _EDGE_STUBS + tail)
    events = [line for line in result.stdout.splitlines()
              if line.split(" ")[0] in ("SPAWN", "WAIT", "SEARCH", "STOP")]
    return result, events


def test_remote_edge_up_spawns_both_before_waiting(tmp_path):
    result, events = _edge(tmp_path, "edge_up\n")
    assert result.returncode == 0, result.stderr
    assert events == ["SPAWN backend", "SPAWN rag", "WAIT backend", "WAIT RAG", "SEARCH"], events


def test_remote_edge_up_rag_wait_failure_only_warns(tmp_path):
    result, events = _edge(tmp_path, "FAIL_WAIT=RAG\nedge_up\n")
    assert result.returncode == 0, result.stderr
    assert events[-1] == "SEARCH" and "RAG 未就绪" in result.stderr


def test_remote_edge_up_rag_spawn_failure_only_warns_and_is_not_waited(tmp_path):
    result, events = _edge(tmp_path, "FAIL_SPAWN=rag\nedge_up\n")
    assert result.returncode == 0, result.stderr
    assert "WAIT RAG" not in events and "RAG 未就绪" in result.stderr


def test_remote_edge_up_backend_wait_failure_is_fatal_and_stops_spawned_rag(tmp_path):
    result, events = _edge(tmp_path, "FAIL_WAIT=backend\nedge_up\necho \"PENDING=$_EDGE_PENDING_RAG\"\n")
    assert result.returncode == 0  # the trailing echo; edge_up's own status is checked below
    assert events == ["SPAWN backend", "SPAWN rag", "WAIT backend", "STOP rag"], events
    assert "PENDING=0" in result.stdout


def test_remote_edge_up_backend_wait_failure_returns_nonzero(tmp_path):
    result, _ = _edge(tmp_path, "FAIL_WAIT=backend\nedge_up\n")
    assert result.returncode != 0


def test_backend_wait_failure_leaves_a_preexisting_rag_alone(tmp_path):
    # RAG already running (not spawned by this edge_up): no pending wait, nothing to stop.
    result, events = _edge(tmp_path, r'''
FAIL_WAIT=backend
start_rag_local() { echo "SKIP rag"; _EDGE_PENDING_RAG=0; return 0; }
edge_up
''')
    assert result.returncode != 0
    assert "STOP rag" not in events, events


def test_remote_edge_up_backend_spawn_failure_does_not_spawn_rag(tmp_path):
    result, events = _edge(tmp_path, "FAIL_SPAWN=backend\nedge_up\n")
    assert result.returncode != 0
    assert events == ["SPAWN backend"], events


def test_start_functions_without_flag_still_wait(tmp_path):
    result, events = _edge(tmp_path, "start_backend && start_rag_local\n")
    assert result.returncode == 0, result.stderr
    assert events == ["SPAWN backend", "WAIT backend", "SPAWN rag", "WAIT RAG"], events


# ---------------------------------------------------------------------------
# Follow-ups: monitor hysteresis, RAG classification, diagnosis chain, log switch
# ---------------------------------------------------------------------------

def _metrics_401():
    request = httpx.Request("GET", "http://synthetic:14161/metrics")
    return httpx.HTTPStatusError("Client error '401 Unauthorized'", request=request,
                                 response=httpx.Response(401, request=request))


def test_monitor_two_failures_then_success_keeps_node_ready():
    async def scenario():
        app = FastAPI()
        outcomes, entered = asyncio.Queue(), asyncio.Event()

        async def assemble():
            return {"engine": object()}

        async def probe():
            entered.set()
            outcome = await outcomes.get()
            if outcome == "fail":
                raise ConnectionError("synthetic dropped tick")
            if outcome == "401":
                raise _metrics_401()

        owner = _owner(app, assemble, probe, retry_initial=0.001, retry_max=0.002,
                       monitor_interval=0.001)
        owner.start()
        try:
            await asyncio.wait_for(entered.wait(), 2)

            async def cycle(outcome):
                entered.clear()
                await outcomes.put(outcome)
                await asyncio.wait_for(entered.wait(), 2)

            await cycle("ok")
            assert app.state.compute_node_state == "ready"
            await cycle("fail")
            await cycle("fail")
            assert app.state.compute_node_state == "ready", "below the threshold the node stays ready"
            await cycle("ok")
            assert (app.state.compute_state, app.state.compute_node_state) == ("ready", "ready")
            for _ in range(mod.MONITOR_FAILURES_BEFORE_UNAVAILABLE - 1):
                await cycle("fail")
            assert app.state.compute_node_state == "ready"
            await cycle("401")
            assert (app.state.compute_state, app.state.compute_node_state) == (
                "unavailable", "unauthorized")
            await cycle("ok")
            assert (app.state.compute_state, app.state.compute_node_state) == ("ready", "ready")
        finally:
            await owner.stop()
    asyncio.run(scenario())


def _rag_http(status):
    def build():
        request = httpx.Request("GET", "http://127.0.0.1:14160/api/v1/category")
        return httpx.HTTPStatusError(f"synthetic {status}", request=request,
                                     response=httpx.Response(status, request=request))
    return build


@pytest.mark.parametrize("host,failure,state,local", [
    (None, lambda: ConnectionError("synthetic refused"), "unreachable", True),
    ("localhost", lambda: httpx.ConnectError("synthetic refused"), "unreachable", True),
    ("127.0.0.1", lambda: httpx.ReadTimeout("synthetic timeout"), "unreachable", True),
    ("127.0.0.1", _rag_http(503), "unreachable", True),
    ("127.0.0.1", _rag_http(401), "unauthorized", False),
    ("127.0.0.1", _rag_http(403), "unauthorized", False),
    ("127.0.0.1", lambda: RuntimeError("RAG data authentication/readiness not confirmed"),
     "unreachable", False),
    ("10.0.0.5", lambda: ConnectionError("synthetic refused"), "unreachable", False),
    ("10.0.0.5", _rag_http(503), "unreachable", False),
])
def test_rag_failure_classification_drives_retry_schedule(offline_app, monkeypatch,
                                                          host, failure, state, local):
    import app as entry
    app, control, _ = offline_app
    control.infer = True
    if host is not None:
        monkeypatch.setitem(entry.config, "rag", {"host": host, "port": 14160})

    async def probe_rag(*args):
        control.rag_probes += 1
        raise failure()
    monkeypatch.setattr(mod, "probe_rag_data", probe_rag)
    delays, sleep = _record_retry_sleeps(monkeypatch)
    sleep.app = app

    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: len(delays) >= 3)
            assert app.state.rag_state == state
            assert app.state.compute_node_state == "ready"
            # offline_app: retry_initial=0.01, retry_max=0.02
            expected = [0.01] * 3 if local else [0.01, 0.02, 0.02]
            assert [d for d, _ in delays][:3] == expected, delays
    asyncio.run(scenario())


def test_local_rag_failure_log_keeps_the_original_error(offline_app, capsys):
    """The real `raise LocalDependencyUnavailable(...) from exc` chain: the log names the cause."""
    app, control, _ = offline_app
    control.infer = True

    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: control.rag_probes >= 2)
    asyncio.run(scenario())
    out = capsys.readouterr().out
    assert "计算装配未就绪（ConnectionError: synthetic offline RAG）" in out, out
    assert "本机依赖启动中" in out, out
    assert "LocalDependencyUnavailable" not in out, out


def test_local_rag_unauthorized_diagnosis_carries_rag_key_guidance(offline_app, monkeypatch, capsys):
    """A wrong RAG key takes the backoff path and still prints the RAG_API_KEY guidance."""
    app, control, _ = offline_app
    control.infer = True

    async def probe_rag(*args):
        control.rag_probes += 1
        raise _rag_http(401)()
    monkeypatch.setattr(mod, "probe_rag_data", probe_rag)

    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: control.rag_probes >= 1 and app.state.compute_state == "retrying")
    asyncio.run(scenario())
    out = capsys.readouterr().out
    assert "RAG_API_KEY" in out and "本机依赖启动中" not in out, out


def test_switch_from_fixed_interval_to_backoff_is_logged(monkeypatch, capsys):
    delays = _run_failures(monkeypatch, [_local_failure] * 62)
    assert delays[59:] == [2, 2, 4], delays[59:]
    lines = [line for line in capsys.readouterr().out.splitlines() if "计算装配未就绪" in line]
    assert len(lines) == 2, lines
    assert "本机依赖启动中" in lines[0] and "本机依赖启动中" not in lines[1], lines
