"""Synthetic HTTP/lifespan regression: local history survives remote cold starts.

No socket, service, real model, credentials, or user data is used. The real session
manager, recovery journal, router, scheduler, and TurnRunner remain in the fixture.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI

from framework.api import compute_lifecycle as lifecycle_module
from framework.api.auth import verify_api_key
from framework.api.routes import require_compute_ready, router
from .fake_engine import FakeEngine, FakeRegistry


async def eventually(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 2)


@pytest.fixture
def offline_app(monkeypatch, tmp_path):
    import app as entry
    from framework.memory import rag_sink
    from framework.utils import model_selection
    from framework.api.offline_files import router as offline_files_router

    cfg = {"inference": {"mode": "remote", "remote": {"host": "synthetic", "port": 1},
                         "modalities": ["text"]},
           "agent": {"sediment_to_rag": False}, "push": {"enabled": False},
           "session_cleanup": {"enabled": False}}
    for name, value in {"config": cfg, "DATA_DIR": tmp_path,
                        "SESSIONS_DIR": tmp_path / "sessions", "SCHEDULES_DIR": tmp_path / "schedules",
                        "NOTES_DIR": tmp_path / "notes", "MODEL": "org/test", "MODEL_REVISION": "r1",
                        "MODEL_OPTIONS": {}, "_model_source": "selection", "API_KEY": "synthetic-key"}.items():
        monkeypatch.setattr(entry, name, value)
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INFER_API_KEY", "synthetic-infer-key")
    monkeypatch.setenv("RAG_API_KEY", "synthetic-rag-key")
    monkeypatch.setattr(entry, "resolve_tokenizer_only_path", lambda *a, **k: tmp_path)
    monkeypatch.setattr(rag_sink, "ensure_notes_category", lambda: None)
    monkeypatch.setattr(model_selection, "resolve_endpoints", lambda: [{"name": "main"}])
    monkeypatch.setattr(entry, "data_dir", lambda: tmp_path)

    class Registry(FakeRegistry):
        def __init__(self, **kwargs):
            super().__init__([])
        def load_tool(self, tool):
            pass
        def rescan_unavailable(self):
            return []

    class Skills:
        def __init__(self, *args, **kwargs):
            pass
        def names(self):
            return []

    monkeypatch.setattr(entry, "MCPRegistry", Registry)
    monkeypatch.setattr(entry, "SkillRegistry", Skills)
    control = SimpleNamespace(infer=False, rag=False, engines=[], probes=0, rag_probes=0, closed=0)

    async def probe(*args):
        control.probes += 1
        if not control.infer:
            raise ConnectionError("synthetic offline compute")
        return {"status": "ok", "service": "macagent-inference", "model_repo": "org/test"}

    async def probe_rag(*args):
        control.rag_probes += 1
        if not control.rag:
            raise ConnectionError("synthetic offline RAG")

    monkeypatch.setattr(lifecycle_module, "probe_remote_compute", probe)
    monkeypatch.setattr(lifecycle_module, "probe_rag_data", probe_rag)
    original = lifecycle_module.ComputeLifecycle

    def fast_lifecycle(*args, **kwargs):
        return original(*args, **kwargs, retry_initial=0.01, retry_max=0.02, monitor_interval=0.01)

    monkeypatch.setattr(lifecycle_module, "ComputeLifecycle", fast_lifecycle)

    async def create(*args, **kwargs):
        assert kwargs["backend"] == "remote"
        assert kwargs["kv_config"]["expected_model"] == {"repo": "org/test", "revision": "r1"}
        engine = FakeEngine([{"text": "recovered answer"}])
        engine.model, engine.processor = None, object()
        async def prewarm(*args):
            return False
        engine.prewarm_base_cache = prewarm
        def shutdown():
            control.closed += 1
        engine.backend.shutdown = shutdown
        control.engines.append(engine)
        return engine

    monkeypatch.setattr(entry.AgentEngine, "create", create)
    app = FastAPI(lifespan=entry.lifespan)
    app.state.api_key = "synthetic-key"
    app.state.auth_devices = {}
    app.include_router(router, dependencies=[Depends(verify_api_key), Depends(require_compute_ready)])
    app.include_router(offline_files_router,
                       dependencies=[Depends(verify_api_key), Depends(require_compute_ready)])
    app.add_api_route("/health", entry.health)
    monkeypatch.setattr(entry, "app", app)
    return app, control, tmp_path


def test_offline_cold_start_history_recovery_then_execution(offline_app):
    app, control, root = offline_app
    sdir = root / "sessions" / "s1"
    (sdir / "assets").mkdir(parents=True)
    archive = {"status": "success", "prompt": "original", "response": [{"role": "assistant", "content": "answer"}]}
    (sdir / "history.jsonl").write_text(json.dumps(archive) + "\n")
    (sdir / "inflight.json").write_text(json.dumps({"turn_id": "interrupted", "prompt": "preserved input"}))
    (sdir / "assets" / "chart.png").write_bytes(b"synthetic PNG")
    (sdir / "deck").mkdir()
    (sdir / "deck" / "deck.json").write_text('{"slides": []}')

    async def scenario():
        async with app.router.lifespan_context(app):
            manager = app.state.session_manager
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                health = (await client.get("/health")).json()
                assert health["status"] == "ok" and health["service"] == "macagent-backend"
                assert "key_fp" in health and "layout" in health
                assert health["data_ready"] and not health["compute_ready"]
                assert (await client.get("/api/v1/sessions")).status_code == 401
                assert (await client.post("/api/v1/chat/s1/turns", data={"prompt": "unauthorized"})).status_code == 401
                client.headers["Authorization"] = "Bearer synthetic-key"
                history = (await client.get("/api/v1/chat/s1/history")).json()
                assert [row["prompt"] for row in history["turns"]] == ["original", "preserved input"]
                assert history["turns"][-1]["status"] == "fail"
                assert history["generation"] and history["replay_after"] > 0
                for path in ("/api/v1/sessions", "/api/v1/chat/s1/system", "/api/v1/chat/s1/task",
                             "/api/v1/chat/s1/turn", "/api/v1/schedules", "/api/v1/files/s1",
                             "/api/v1/files/s1/assets/chart.png", "/api/v1/files/s1/deck",
                             "/api/v1/chat/s1/offline-files"):
                    assert (await client.get(path)).status_code == 200, path
                assert not manager._all_sessions  # All offline reads are data-only.
                assert not (sdir / "inflight.json").exists()
                terminal = manager.feed.replay(0)[-1]
                assert terminal["type"] == "turn_ended" and terminal["turn_id"] == "interrupted"
                assert terminal["data"]["status"] == "fail"
                response = await client.get("/api/v1/chat/s1/events")
                assert response.status_code == 503  # No backendless hot session for SSE.
                before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                mutations = [("POST", "/api/v1/chat/new/turns", {"data": {"prompt": "blocked"}}),
                             ("POST", "/api/v1/chat/s1/turns/approval", {"json": {"approve": True}}),
                             ("POST", "/api/v1/chat/s1/turns/resume", {}),
                             ("DELETE", "/api/v1/chat/s1", {}),
                             ("PATCH", "/api/v1/chat/s1/pin", {"json": {"pinned": True}})]
                for method, path, kwargs in mutations:
                    response = await client.request(method, path, **kwargs)
                    assert response.status_code == 503, response.text
                    assert response.json()["detail"]["reason"] == "compute_not_ready"
                after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                assert after == before and not manager._all_sessions
                assert not control.engines
                control.infer = True
                await eventually(lambda: control.rag_probes > 0)
                assert not app.state.compute_ready and not control.engines
                control.rag = True
                await eventually(lambda: app.state.compute_ready)
                assert app.state.session_manager is manager and len(control.engines) == 1
                response = await client.post("/api/v1/chat/s1/turns", data={"prompt": "new task"})
                assert response.status_code == 202, response.text
                await eventually(lambda: not app.state.turn_runner._bg)
                assert [r["prompt"] for r in manager.get_history("s1")] == ["original", "preserved input", "new task"]
                assert manager.get_history("s1")[-1]["status"] == "success"
                control.infer = False
                await eventually(lambda: not app.state.compute_ready)
                assert app.state.compute_state == "unavailable"
                assert (await client.get("/api/v1/chat/s1/history")).status_code == 200
                assert (await client.post("/api/v1/chat/s1/task/cancel")).status_code == 200
                assert (await client.post("/api/v1/chat/s1/turns", data={"prompt": "outage"})).status_code == 503
                control.infer = True
                await eventually(lambda: app.state.compute_ready)
                assert len(control.engines) == 1  # Reconnect probes the installed engine.
        assert control.closed == 1 and not app.state.data_ready
        assert app.state.compute_lifecycle.task.done()
        assert app.state.compute_state == "stopped"

    asyncio.run(scenario())


def test_lifespan_stop_while_offline_has_no_engine(offline_app):
    app, control, _ = offline_app
    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: control.probes > 0)
            assert app.state.data_ready and not app.state.compute_ready
        assert app.state.compute_lifecycle.task.done()
        assert not control.engines and not control.closed
    asyncio.run(scenario())


def test_failed_post_engine_assembly_closes_before_retry(offline_app, monkeypatch):
    import app as entry
    app, control, _ = offline_app
    control.infer = control.rag = True
    original = entry.Ingestion
    attempts = []
    def ingestion(*args, **kwargs):
        attempts.append(control.closed)
        if len(attempts) == 1:
            raise RuntimeError("synthetic failure after engine")
        return original(*args, **kwargs)
    monkeypatch.setattr(entry, "Ingestion", ingestion)
    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: app.state.compute_ready)
            assert attempts == [0, 1]
            assert len(control.engines) == 2 and control.closed == 1
        assert control.closed == 2
    asyncio.run(scenario())


def test_missing_compute_host_keeps_local_data_service(offline_app):
    import app as entry
    app, control, _ = offline_app
    entry.config["inference"]["remote"]["host"] = ""
    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: app.state.compute_state == "retrying")
            assert app.state.data_ready and not app.state.compute_ready
            assert control.probes == 0 and not control.engines
    asyncio.run(scenario())


def test_unready_execution_guard_rejects_before_adapter_side_effects():
    from fastapi import HTTPException, Request
    app = FastAPI()
    app.state.compute_ready = False
    app.state.compute_installed = False
    app.state.compute_state = "retrying"
    request = Request({"type": "http", "method": "POST", "app": app,
                       "route": SimpleNamespace(path="/api/v1/chat/{session_id}/turns")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_compute_ready(request))
    assert exc.value.status_code == 503
    assert exc.value.detail["reason"] == "compute_not_ready"


def test_execution_gate_keys_on_method_and_path(offline_app):
    """Same path, two operations: deleting stored data is not executing a turn."""
    app, control, root = offline_app
    control.infer = control.rag = True
    sdir = root / "sessions" / "s1"
    sdir.mkdir(parents=True)
    (sdir / "history.jsonl").write_text(
        json.dumps({"status": "success", "prompt": "kept", "response": []}) + "\n")

    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: app.state.compute_ready)
            control.infer = False
            await eventually(lambda: not app.state.compute_ready)
            assert app.state.compute_installed
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                         headers={"Authorization": "Bearer synthetic-key"}) as client:
                for method, path, kwargs in (
                        ("POST", "/api/v1/chat/s1", {"data": {"prompt": "blocked"}}),
                        ("POST", "/api/v1/chat/s1/turns", {"data": {"prompt": "blocked"}})):
                    gated = await client.request(method, path, **kwargs)
                    assert gated.status_code == 503, gated.text
                    assert gated.json()["detail"]["reason"] == "compute_not_ready"
                removed = await client.delete("/api/v1/chat/s1")
                assert removed.status_code == 200, removed.text
                assert removed.json()["status"] is True
                assert not sdir.exists()
    asyncio.run(scenario())


def test_turn_state_without_scheduler_reports_queued_snapshot(tmp_path):
    """A queued snapshot outlives the scheduler (pre-install, or after teardown)."""
    from fastapi.testclient import TestClient
    from framework.memory.session import SessionManager
    app = FastAPI()
    app.state.api_key = ""
    app.state.auth_devices = {}
    app.state.compute_ready = False
    app.state.compute_installed = False
    app.state.compute_state = "retrying"
    app.state.session_manager = SessionManager(tmp_path / "sessions")
    app.include_router(router, dependencies=[Depends(verify_api_key),
                                             Depends(require_compute_ready)])
    session = app.state.session_manager.get_or_create("s1")
    session.journal.emit("turn_queued", {"prompt": "queued by a previous generation"},
                         turn_id="t_queued")
    assert not hasattr(app.state, "scheduler")
    with TestClient(app) as client:
        response = client.get("/api/v1/chat/s1/turn")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["turn"]["state"] == "queued" and body["turn"]["turn_id"] == "t_queued"
    assert "queue_position" not in body["turn"]  # unknown without a scheduler, not 0 by luck


def test_rag_outage_after_install_does_not_revoke_execution(offline_app):
    """RAG is a startup prerequisite only; afterwards just rag_search degrades."""
    app, control, _ = offline_app
    control.infer = control.rag = True

    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: app.state.compute_ready)
            assert app.state.rag_ready
            control.rag = False
            baseline = control.rag_probes
            # More failed RAG cycles than the compute hysteresis window allows.
            await eventually(lambda: control.rag_probes >= baseline + 5)
            assert app.state.compute_ready and app.state.compute_state == "ready"
            assert app.state.rag_ready is False
            control.rag = True
            await eventually(lambda: app.state.rag_ready)
    asyncio.run(scenario())


def test_failing_extra_endpoint_still_releases_the_main_engine(offline_app):
    app, control, _ = offline_app
    control.infer = control.rag = True

    def explode():
        raise RuntimeError("synthetic extra endpoint failure")

    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: app.state.compute_ready)
            app.state.engine_pool._engines["aux"] = SimpleNamespace(
                _inference_executor=None, backend=SimpleNamespace(shutdown=explode))
        assert control.closed == 1, "main engine release must not depend on extra endpoints"
        assert app.state.compute_state == "stopped"
        assert not hasattr(app.state, "engine")
    asyncio.run(scenario())


def test_assembly_failure_log_keeps_the_original_error(offline_app, monkeypatch, capsys):
    """Cleanup noise must not replace the diagnosis the operator needs."""
    import app as entry
    from framework.core import engine_pool as pool_module
    app, control, _ = offline_app
    control.infer = control.rag = True

    class Hostile(dict):
        def items(self):
            raise RuntimeError("synthetic cleanup failure")

    class HostilePool(pool_module.EnginePool):
        def __init__(self, engine):
            super().__init__(engine)
            self._engines = Hostile(self._engines)

    monkeypatch.setattr(pool_module, "EnginePool", HostilePool)
    original = entry.Ingestion
    attempts = []

    def ingestion(*args, **kwargs):
        attempts.append(control.closed)
        if len(attempts) == 1:
            raise RuntimeError("synthetic failure after engine")
        return original(*args, **kwargs)

    monkeypatch.setattr(entry, "Ingestion", ingestion)

    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: app.state.compute_ready)
            assert attempts == [0, 1]  # first engine released despite the pool error
    asyncio.run(scenario())
    out = capsys.readouterr().out
    assert "synthetic failure after engine" in out, out
    assert "计算装配未就绪" in out, out


def test_shutdown_is_bounded_when_a_turn_never_settles(offline_app, monkeypatch):
    import app as entry
    app, control, _ = offline_app
    control.infer = control.rag = True
    monkeypatch.setattr(entry, "SHUTDOWN_JOIN_SECONDS", 0.05)

    async def scenario():
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        await eventually(lambda: app.state.compute_ready)
        stuck = asyncio.create_task(asyncio.Event().wait())
        app.state.turn_runner._bg["stuck"] = stuck

        async def never_stops():
            await asyncio.Event().wait()

        app.state.schedule_runner.stop = never_stops
        exit_task = asyncio.create_task(context.__aexit__(None, None, None))
        # asyncio.wait never cancels on our behalf: an unbounded join leaves
        # exit_task pending and this assertion goes red instead of hanging.
        done, _ = await asyncio.wait({exit_task}, timeout=5)
        try:
            assert done, "shutdown waited forever on a turn that never settles"
            await exit_task
        finally:
            exit_task.cancel()
            await asyncio.gather(exit_task, stuck, return_exceptions=True)
        assert stuck.cancelled()
        assert control.closed == 1, "GPU/backend release must survive a stuck turn"
        assert app.state.compute_state == "stopped"
        assert not app.state.data_ready
    asyncio.run(scenario())


def test_stuck_turn_cannot_starve_the_schedule_archive(offline_app, monkeypatch):
    # Each phase has its own budget: a turn that eats all of its time must not leave
    # ScheduleRunner.stop() a zero timeout (it would be cancelled before archiving).
    import app as entry
    app, control, _ = offline_app
    control.infer = control.rag = True
    monkeypatch.setattr(entry, "SHUTDOWN_JOIN_SECONDS", 0.2)
    archived = []

    async def scenario():
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        await eventually(lambda: app.state.compute_ready)
        stuck = asyncio.create_task(asyncio.Event().wait())
        app.state.turn_runner._bg["stuck"] = stuck

        async def slow_but_finite_stop():
            await asyncio.sleep(0.05)
            archived.append(True)

        app.state.schedule_runner.stop = slow_but_finite_stop
        exit_task = asyncio.create_task(context.__aexit__(None, None, None))
        done, _ = await asyncio.wait({exit_task}, timeout=5)
        assert done, "shutdown hung"
        await exit_task
        await asyncio.gather(stuck, return_exceptions=True)
    asyncio.run(scenario())
    assert archived == [True], "schedule stop was cancelled before it could archive"


def test_inference_thread_join_is_bounded(offline_app, monkeypatch):
    # cancel_futures only drops queued work; a generation already running keeps its
    # thread. Shutdown must stop waiting for it and still release the backend.
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import app as entry
    app, control, _ = offline_app
    control.infer = control.rag = True
    monkeypatch.setattr(entry, "ENGINE_RELEASE_SECONDS", 0.1)
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)

    async def scenario():
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        await eventually(lambda: app.state.compute_ready)
        app.state.engine._inference_executor = executor
        executor.submit(release.wait, 30)          # a generation that is still running
        # Not wait_for: teardown deliberately absorbs cancellation, so a timeout there
        # would just wait the thread out. asyncio.wait observes without cancelling.
        exit_task = asyncio.create_task(context.__aexit__(None, None, None))
        done, _ = await asyncio.wait({exit_task}, timeout=5)
        release.set()
        await exit_task
        assert done, "shutdown waited for a generating inference thread"
        assert control.closed == 1, "backend release must not wait for the generating thread"
        assert app.state.compute_state == "stopped"
    asyncio.run(scenario())
    executor.shutdown(wait=True)


def test_default_discovery_retries_and_adopts_before_tokenizer(offline_app, monkeypatch):
    import app as entry
    from framework.utils import model_selection as ms
    app, control, root = offline_app
    monkeypatch.setattr(entry, "_model_source", "default")
    order = []
    def discover(base):
        if not control.infer:
            raise ConnectionError("synthetic compute offline")
        order.append("discover")
        return "org/test", "r1", {}
    monkeypatch.setattr(ms, "discover_remote_model", discover)
    monkeypatch.setattr(ms, "adopt_model", lambda *args: order.append("adopt"))
    monkeypatch.setattr(entry, "resolve_selected_model",
                        lambda: (order.append("resolve") or ("org/test", "r1", "adopted")))
    monkeypatch.setattr(entry, "model_options", lambda repo: {})
    monkeypatch.setattr(entry, "resolve_tokenizer_only_path",
                        lambda *args, **kwargs: (order.append("tokenizer") or root))
    async def scenario():
        async with app.router.lifespan_context(app):
            await eventually(lambda: app.state.compute_state == "retrying")
            assert app.state.data_ready and not control.engines and not order
            control.infer = control.rag = True
            await eventually(lambda: app.state.compute_ready)
            assert order == ["discover", "adopt", "resolve", "tokenizer"]
    asyncio.run(scenario())


def test_shutdown_cancels_post_engine_probe_and_closes_unpublished_engine(offline_app, monkeypatch):
    app, control, _ = offline_app
    control.infer = control.rag = True
    entered = None
    calls = 0
    async def probe(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await asyncio.Event().wait()
        return {"status": "ok", "service": "macagent-inference", "model_repo": "org/test"}
    monkeypatch.setattr(lifecycle_module, "probe_remote_compute", probe)
    async def scenario():
        nonlocal entered
        entered = asyncio.Event()
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(entered.wait(), 2)
            assert len(control.engines) == 1 and control.closed == 0
            assert not app.state.compute_ready and not hasattr(app.state, "engine")
        assert control.closed == 1 and app.state.compute_lifecycle.task.done()
        assert not hasattr(app.state, "engine")
    asyncio.run(scenario())
