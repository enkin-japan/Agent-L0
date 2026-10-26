"""CPU-only startup ownership, guard, retry and cancellation proofs."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from framework.api import compute_lifecycle as mod
from framework.backends.remote.backend import RemoteBackend
from framework.core.engine import AgentEngine


@pytest.mark.parametrize("failure", ["identity", "auth", "health", "rag_auth", "rag_error"])
def test_dependency_probe_requires_positive_identity_and_authenticated_200(monkeypatch, failure):
    seen = []
    def wire(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "error" if failure == "health" else "ok",
                "service": "macagent-inference", "model_repo": "wrong" if failure == "identity" else "org/model",
                "model_revision": "r1"})
        if request.url.path == "/metrics":
            return httpx.Response(401 if failure == "auth" else 200, json={})
        return httpx.Response(401 if failure == "rag_auth" else 500 if failure == "rag_error" else 200, json=[])
    original = mod.httpx.AsyncClient
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(wire)))
    async def scenario():
        if failure.startswith("rag"):
            await mod.probe_remote_compute("http://synthetic", {"repo": "org/model", "revision": "r1"}, "infer-key")
            await mod.probe_rag_data({"host": "synthetic"}, "rag-key")
        else:
            await mod.probe_remote_compute("http://synthetic", {"repo": "org/model", "revision": "r1"}, "infer-key")
    with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
        asyncio.run(scenario())
    assert seen[0] == ("/health", None)
    if any(path == "/metrics" for path, _ in seen):
        assert ("/metrics", "Bearer infer-key") in seen
    if failure.startswith("rag"):
        assert ("/api/v1/category", "Bearer rag-key") in seen


def test_unchanged_identity_still_reauthenticates(monkeypatch):
    identity = {"status": "ok", "service": "macagent-inference", "model_repo": "org/model"}
    calls = []
    def wire(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=identity if request.url.path == "/health" else {})
    original = mod.httpx.AsyncClient
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(wire)))
    async def scenario():
        first = await mod.probe_remote_compute("http://synthetic", {"repo": "org/model"}, "key")
        await mod.probe_remote_compute("http://synthetic", {"repo": "org/model"}, "key", first)
    asyncio.run(scenario())
    assert calls == ["/health", "/metrics", "/health", "/metrics"]


async def _settle(predicate, timeout=2):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), timeout)


def _trivial_owner(app, probe=None, cleanup=None, assemble=None, **kwargs):
    async def default_assemble():
        return {"engine": object()}

    async def default_cleanup(resources):
        return None

    return mod.ComputeLifecycle(app, assemble or default_assemble, lambda resources: None,
                                cleanup or default_cleanup, probe,
                                retry_initial=0.001, retry_max=0.002,
                                monitor_interval=0.001, **kwargs)


def test_monitor_requires_three_consecutive_failures_before_unavailable():
    """A single lost probe cycle must not 503 a user mid-conversation."""
    async def scenario():
        app = FastAPI()
        outcomes, entered = asyncio.Queue(), asyncio.Event()

        async def probe():
            entered.set()
            if await outcomes.get() == "fail":
                raise ConnectionError("synthetic probe outage")

        owner = _trivial_owner(app, probe=probe)
        owner.start()
        try:
            await asyncio.wait_for(entered.wait(), 2)

            async def cycle(outcome):
                # Deterministic stepping: the next probe entry proves the previous
                # outcome was fully handled by the monitor loop. No sleep races.
                entered.clear()
                await outcomes.put(outcome)
                await asyncio.wait_for(entered.wait(), 2)

            await cycle("fail")
            assert app.state.compute_ready and app.state.compute_state == "ready"
            await cycle("fail")
            assert app.state.compute_ready, "two failed cycles must not revoke execution"
            assert app.state.compute_state == "ready"
            await cycle("fail")
            assert not app.state.compute_ready
            assert app.state.compute_state == "unavailable"
            await cycle("ok")
            assert app.state.compute_ready and app.state.compute_state == "ready"
            await cycle("fail")
            assert app.state.compute_ready, "a success must reset the failure counter"
            await cycle("fail")
            assert app.state.compute_ready
        finally:
            await owner.stop()
    asyncio.run(scenario())


def test_failure_detail_is_one_sanitized_line_with_actionable_guidance():
    host_failure = RuntimeError(
        "❌ [backend] 拒绝启动：inference.mode=remote 但算力端地址为空。\n"
        "   请填写算力端地址——app 形态：…\n")
    detail = mod.describe_failure(host_failure)
    assert "\n" not in detail
    assert "设置 → 执行层 → 算力端地址" in detail and "INFER_REMOTE_HOST" in detail

    request = httpx.Request("GET", "http://synthetic:14161/metrics",
                            headers={"Authorization": "Bearer synthetic-secret-token"})
    unauthorized = httpx.HTTPStatusError(
        "Client error '401 Unauthorized' for url 'http://synthetic:14161/metrics'\n"
        "For more information check: https://example.invalid/401",
        request=request, response=httpx.Response(401, request=request))
    detail = mod.describe_failure(unauthorized)
    assert "\n" not in detail and "401" in detail
    assert ".env.local" in detail and "INFER_API_KEY" in detail
    # The key and the request headers must never reach a log line.
    assert "synthetic-secret-token" not in detail and "Authorization" not in detail

    rag_request = httpx.Request("GET", "http://synthetic:14160/api/v1/category")
    forbidden = httpx.HTTPStatusError(
        "Client error '403 Forbidden' for url 'http://synthetic:14160/api/v1/category'",
        request=rag_request, response=httpx.Response(403, request=rag_request))
    assert "RAG_API_KEY" in mod.describe_failure(forbidden)

    assert len(mod.describe_failure(RuntimeError("x" * 900))) <= mod.MESSAGE_MAX_CHARS


def test_retry_log_prints_each_distinct_message_once(capsys):
    async def scenario():
        app = FastAPI()
        failures = ["first synthetic outage", "first synthetic outage",
                    "first synthetic outage", "second synthetic outage"]

        async def assemble():
            if failures:
                raise ConnectionError(failures.pop(0))
            return {"engine": object()}

        owner = _trivial_owner(app, assemble=assemble)
        owner.start()
        await _settle(lambda: owner.resources is not None)
        await owner.stop()
    asyncio.run(scenario())
    out = capsys.readouterr().out
    # a changed message is a state change and prints at once; the repeats are throttled
    assert out.count("ConnectionError") == 2, out
    assert out.count("first synthetic outage") == 1, out
    assert out.count("second synthetic outage") == 1, out


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_unchanged_outage_logs_once_per_five_minutes(capsys):
    """5 s probe cycle: a 60 s outage is one line, not twelve; then one line per 5 min."""
    clock = _Clock()
    done = asyncio.Event()

    async def scenario():
        app = FastAPI()
        calls = []

        async def probe():
            calls.append(clock.now)
            if len(calls) > 3 + 300 // 5 + 1:
                done.set()
                await asyncio.Event().wait()
            clock.now += 5
            raise ConnectionError("synthetic probe outage")

        owner = _trivial_owner(app, probe=probe, clock=clock)
        owner.start()
        try:
            await asyncio.wait_for(done.wait(), 5)
        finally:
            await owner.stop()
    asyncio.run(scenario())
    lines = capsys.readouterr().out.splitlines()
    unavailable = [line for line in lines if "计算连接未就绪" in line]
    # failures 1/3 and 2/3 are distinct states (each printed once); the outage spans
    # 300+ s of 5 s cycles: printed when it starts and once more five minutes later
    assert sum("探活失败" in line for line in lines) == 2, lines
    assert len(unavailable) == 2, lines
    assert "省略" in unavailable[1], unavailable


def test_outage_within_a_minute_is_one_line_and_recovery_is_logged(capsys):
    clock = _Clock()
    outcomes = ["fail"] * (3 + 60 // 5) + ["ok", "fail", "fail", "fail"]
    done = asyncio.Event()

    async def scenario():
        app = FastAPI()

        async def probe():
            if not outcomes:
                done.set()
                await asyncio.Event().wait()
            clock.now += 5
            if outcomes.pop(0) == "fail":
                raise ConnectionError("synthetic probe outage")

        owner = _trivial_owner(app, probe=probe, clock=clock)
        owner.start()
        try:
            await asyncio.wait_for(done.wait(), 5)
        finally:
            await owner.stop()
    asyncio.run(scenario())
    out = capsys.readouterr().out
    # 60 s of the same outage = 1 line; recovering is a change; the next outage is news
    assert out.count("计算连接未就绪") == 2, out
    assert out.count("计算连接已恢复") == 1, out


def test_assembly_retries_log_once_per_five_minutes(capsys):
    clock = _Clock()

    async def scenario():
        app = FastAPI()
        attempts = []

        async def assemble():
            attempts.append(1)
            if len(attempts) <= 13:              # 13 attempts, 5 s apart = 60 s
                clock.now += 5
                raise ConnectionError("synthetic assembly outage")
            return {"engine": object()}

        owner = _trivial_owner(app, assemble=assemble, clock=clock)
        owner.start()
        await _settle(lambda: owner.resources is not None)
        await owner.stop()
    asyncio.run(scenario())
    out = capsys.readouterr().out
    assert out.count("计算装配未就绪") == 1, out


def test_stop_clears_installed_before_cleanup_runs():
    """Teardown must present 503 to non-execution routes, not a 500 from half-released deps."""
    async def scenario():
        app, observed = FastAPI(), []

        async def cleanup(resources):
            observed.append((app.state.compute_installed, app.state.compute_ready,
                             app.state.compute_state))

        owner = _trivial_owner(app, cleanup=cleanup)
        owner.start()
        await _settle(lambda: app.state.compute_installed)
        await owner.stop()
        assert observed == [(False, False, "stopping")]
        assert app.state.compute_state == "stopped"
        assert not app.state.compute_installed and not app.state.compute_ready
    asyncio.run(scenario())


def test_publish_failure_cleans_resources_before_retry():
    async def scenario():
        app, events = FastAPI(), []
        async def assemble():
            events.append("assemble")
            return {"engine": object()}
        def publish(resources):
            assert not app.state.compute_ready
            events.append("publish")
            raise RuntimeError("synthetic publish failure")
        async def cleanup(resources):
            events.append("cleanup")
        owner = mod.ComputeLifecycle(app, assemble, publish, cleanup,
                                     retry_initial=0.01, retry_max=0.02)
        owner.start()
        while len(events) < 6:
            await asyncio.sleep(0.001)
        await owner.stop()
        assert events[:6] == ["assemble", "publish", "cleanup"] * 2
        assert owner.resources is None and owner.task.done()
    asyncio.run(scenario())


def test_stop_during_failed_publish_cleanup_finishes_cleanup_without_retry():
    async def scenario():
        app, events = FastAPI(), []
        cleanup_started, cleanup_release = asyncio.Event(), asyncio.Event()

        async def assemble():
            events.append("assemble")
            return {"engine": object()}

        def publish(resources):
            events.append("publish")
            raise RuntimeError("synthetic publish failure")

        async def cleanup(resources):
            events.append("cleanup-start")
            cleanup_started.set()
            await cleanup_release.wait()
            events.append("cleanup-end")

        owner = mod.ComputeLifecycle(app, assemble, publish, cleanup,
                                     retry_initial=0.01, retry_max=0.02)
        owner.start()
        stop_task = None
        try:
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            stop_task = asyncio.create_task(owner.stop())
            # Explicitly reach cancellation inside finish_task while cleanup is
            # still blocked. No sleeps/polling race determines this ordering.
            for _ in range(100):
                await asyncio.sleep(0)
                if owner.task.cancelling():
                    break
            assert owner.task.cancelling()
            assert not stop_task.done()
            assert events == ["assemble", "publish", "cleanup-start"]
            cleanup_release.set()
            # Bounded observation rather than wait_for(stop_task): that would
            # cancel stop itself and hang under the exact swallowed-cancel bug.
            for _ in range(100):
                await asyncio.sleep(0)
                if stop_task.done():
                    break
            assert stop_task.done(), "stop lost cancellation during publish cleanup and retried"
            await stop_task
            assert owner.task.cancelled()
            assert events == ["assemble", "publish", "cleanup-start", "cleanup-end"]
            assert owner.resources is None
            assert app.state.compute_state == "stopped"
        finally:
            cleanup_release.set()
            owner.task.cancel()
            await asyncio.gather(owner.task, return_exceptions=True)
            if stop_task is not None:
                await asyncio.gather(stop_task, return_exceptions=True)

    asyncio.run(scenario())


def test_blocking_startup_io_cancellation_joins_worker():
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    def worker():
        started.set()
        release.wait(2)
        done.set()
    async def scenario():
        task = asyncio.create_task(mod.blocking_call(worker))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not done.is_set()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert done.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["load", "construct", "cancel"])
def test_engine_factory_releases_failed_or_cancelled_load(monkeypatch, failure):
    import framework.backends as backends
    from framework.utils import model_selection
    monkeypatch.setattr(model_selection, "merge_entry_speculative", lambda cfg, options: cfg)
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    seen = SimpleNamespace(executor=None, closes=0)
    backend = SimpleNamespace(shutdown=lambda: setattr(seen, "closes", seen.closes + 1))
    def load_worker():
        started.set()
        release.wait(2)
        done.set()
        if failure == "load":
            raise RuntimeError("synthetic failed load")
        return backend
    class Backend:
        @classmethod
        async def load(cls, path, executor, **kwargs):
            seen.executor = executor
            return await asyncio.get_running_loop().run_in_executor(executor, load_worker)
    monkeypatch.setattr(backends, "get_backend_class", lambda name: Backend)
    monkeypatch.setattr("framework.dialects.get_dialect_class",
                        lambda name: (_ for _ in ()).throw(RuntimeError("synthetic constructor failure")))
    async def scenario():
        task = asyncio.create_task(AgentEngine.create("synthetic"))
        await asyncio.to_thread(started.wait, 1)
        if failure == "cancel":
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await task
        assert done.is_set() and seen.executor._shutdown
        assert all(not thread.is_alive() for thread in seen.executor._threads)
        assert seen.closes == (0 if failure == "load" else 1)
    asyncio.run(scenario())


def test_remote_loader_closes_session_on_identity_failure(monkeypatch):
    from framework.utils import model_selection
    monkeypatch.setattr(model_selection, "resolve_endpoints", lambda: [])
    monkeypatch.setattr("transformers.AutoTokenizer.from_pretrained", lambda path: object())
    sessions = []
    class Session:
        headers = {}
        def __init__(self):
            self.closed = False
            sessions.append(self)
        def get(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None,
                json=lambda: {"status": "ok", "service": "macagent-inference", "model_repo": "wrong"})
        def close(self):
            self.closed = True
    monkeypatch.setattr("framework.backends.remote.backend.requests.Session", Session)
    with pytest.raises(RuntimeError, match="档位错开"):
        asyncio.run(RemoteBackend.load("synthetic", expected_model={"repo": "expected"}))
    assert len(sessions) == 1 and sessions[0].closed


def test_remote_loader_does_not_block_http_loop_and_joins_cancel(monkeypatch):
    started, release = threading.Event(), threading.Event()
    closed = []
    def load(cls, path, **kwargs):
        started.set()
        release.wait(2)
        return SimpleNamespace(shutdown=lambda: closed.append(True))
    monkeypatch.setattr(RemoteBackend, "_load_sync", classmethod(load))
    async def scenario():
        task = asyncio.create_task(RemoteBackend.load("synthetic"))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]
    asyncio.run(scenario())


def test_failure_detail_never_carries_a_bearer_value():
    # A key with an illegal character makes h11 echo the whole header value.
    exc = RuntimeError("Illegal header value b'Bearer sk-live-SECRET123'")
    line = mod.describe_failure(exc)
    assert "SECRET123" not in line and "Bearer <redacted>" in line
