"""CPU-only source contract for the macOS quit path (2026-10-29).

Pins the order that makes ⌘Q fast: the app drops its own SSE long connections
(AgentStore.prepareForTermination → AgentClient.cancelInFlightRequests) *before*
SidecarManager.shutdownSync blocks the main thread waiting for `lib_edge.sh down`.
Without that order uvicorn waits the full timeout_graceful_shutdown (5 s) for the
app's /api/v1/events stream; measured 7.0 s → fixed build on the same machine.
Also pins the 0.2 s poll granularity of stop_one. Mutations run on in-memory copies.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def _strip(text):
    return re.sub(r"//[^\n]*", "", text)


def _app():
    return _strip((ROOT / "apple-app/Sources/App/MacAgentApp.swift").read_text())


def assert_quit_contract(app, store, client, lib):
    body = re.search(r"func applicationWillTerminate\(.*?\n    \}\n", app, re.S)
    assert body, "applicationWillTerminate missing"
    body = body.group(0)
    i = body.find("Self.store?.prepareForTermination()")
    j = body.find("Self.sidecar?.shutdownSync()")
    assert i >= 0, "prepareForTermination not called on quit"
    assert j >= 0, "shutdownSync not called on quit"
    assert i < j, "long connections must be dropped before sidecar down blocks the main thread"
    assert "MacAppDelegate.store = store" in app, "delegate has no store reference"
    assert "func prepareForTermination()" in store
    assert re.search(r"func prepareForTermination\(\)\s*\{[^}]*client\?\.cancelInFlightRequests\(\)", store, re.S), \
        "prepareForTermination must cancel the client's in-flight requests (the SSE socket stays open otherwise)"
    assert re.search(r"func cancelInFlightRequests\(\)\s*\{\s*session\.getAllTasks \{ tasks in tasks\.forEach \{ \$0\.cancel\(\) \} \}", client), \
        "cancelInFlightRequests must cancel tasks without invalidating the session"
    assert "invalidateAndCancel" not in client, \
        "never invalidate the shared session: a request issued during quit would throw NSGenericException"
    # stop_one: 0.2 s poll, grace measured on the wall clock (not tick counts), 5 s post-KILL window
    assert '[ $((SECONDS - started)) -lt "$grace" ]' in lib, "TERM grace must be a wall-clock deadline in seconds"
    assert lib.count("        sleep 0.2\n") == 2, "stop_one must poll at 0.2 s granularity"
    assert "if [ $((SECONDS - started)) -ge 5 ]; then" in lib, "post-KILL observation window must stay 5 s"


def _sources():
    return dict(
        app=_app(),
        store=_strip((ROOT / "apple-app/Sources/Store/AgentStore.swift").read_text()),
        client=_strip((ROOT / "apple-app/Sources/API/AgentClient.swift").read_text()),
        lib=(ROOT / "scripts/lib_edge.sh").read_text(),
    )


def test_quit_path_contract():
    assert_quit_contract(**_sources())


def test_mutation_order_swapped_fails():
    s = _sources()
    app = s["app"].replace("Self.store?.prepareForTermination()\n            Self.sidecar?.shutdownSync()",
                           "Self.sidecar?.shutdownSync()\n            Self.store?.prepareForTermination()")
    assert app != s["app"]
    try:
        assert_quit_contract(app, s["store"], s["client"], s["lib"])
    except AssertionError as e:
        assert "before sidecar down" in str(e)
    else:
        raise AssertionError("swapped order survived")


def test_mutation_no_cancel_fails():
    s = _sources()
    store = s["store"].replace("client?.cancelInFlightRequests()", "")
    assert store != s["store"]
    try:
        assert_quit_contract(s["app"], store, s["client"], s["lib"])
    except AssertionError as e:
        assert "in-flight" in str(e)
    else:
        raise AssertionError("missing cancel survived")


def test_mutation_invalidate_fails():
    s = _sources()
    client = s["client"].replace("session.getAllTasks { tasks in tasks.forEach { $0.cancel() } }",
                                 "session.invalidateAndCancel()")
    assert client != s["client"]
    try:
        assert_quit_contract(s["app"], s["store"], client, s["lib"])
    except AssertionError as e:
        assert "invalidat" in str(e)
    else:
        raise AssertionError("invalidate survived")


def test_mutation_coarse_poll_fails():
    s = _sources()
    lib = s["lib"].replace("        sleep 0.2\n", "        sleep 1\n", 1)
    assert lib != s["lib"]
    try:
        assert_quit_contract(s["app"], s["store"], s["client"], lib)
    except AssertionError as e:
        assert "0.2 s" in str(e)
    else:
        raise AssertionError("coarse poll survived")


def test_mutation_tick_grace_fails():
    s = _sources()
    lib = s["lib"].replace('[ $((SECONDS - started)) -lt "$grace" ]', '[ "$waited" -lt "$grace" ]')
    assert lib != s["lib"]
    try:
        assert_quit_contract(s["app"], s["store"], s["client"], lib)
    except AssertionError as e:
        assert "wall-clock" in str(e)
    else:
        raise AssertionError("tick grace survived")
