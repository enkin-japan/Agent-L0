"""扫码配对金测试：一次性配对码换密钥（framework/api/pairing.py）。

claim 免鉴权，所以这里锁的全是「什么时候绝不能给出密钥」：没领过码、码过期、码用过、
后端没密钥；乱猜的人烧不掉别人的码；start 必须带对的 Bearer；以及全部 /api/v1 路由里
只有 claim 不带鉴权依赖（白名单写死，新增免鉴权路由必须在这里留下决定）。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from framework.api import pairing

KEY = "agent-key-for-tests"
BEARER = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture()
def client():
    app = FastAPI()
    app.state.api_key = KEY
    app.include_router(pairing.router)
    return TestClient(app)


def start(client):
    r = client.post("/api/v1/pair/start", headers=BEARER)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["expires_in"] == pairing.TTL_SECONDS
    return r.json()["code"]


def claim(client, code):
    return client.post("/api/v1/pair/claim", json={"code": code})


def test_code_is_exchanged_for_the_key_exactly_once(client):
    code = start(client)
    assert len(code) >= 24
    first = claim(client, code)
    assert first.status_code == 200 and first.json() == {"api_key": KEY}
    assert first.headers["cache-control"] == "no-store"
    assert claim(client, code).status_code == 403          # single use


def test_start_requires_the_agent_key(client):
    assert client.post("/api/v1/pair/start").status_code == 401
    assert client.post("/api/v1/pair/start",
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert claim(client, "anything").status_code == 403    # no code was ever issued


def test_expired_code_is_refused(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(pairing.time, "monotonic", lambda: now[0])
    code = start(client)
    now[0] += pairing.TTL_SECONDS - 1
    later = start(client)                                   # restarts the clock
    now[0] += pairing.TTL_SECONDS
    assert claim(client, later).status_code == 403
    assert claim(client, code).status_code == 403


def test_new_code_replaces_the_previous_one(client):
    old = start(client)
    new = start(client)
    assert old != new
    assert claim(client, old).status_code == 403
    assert claim(client, new).json() == {"api_key": KEY}


def test_wrong_guesses_cannot_burn_someone_elses_code(client):
    # Any tailnet peer can reach /claim; burning on failures would let it block pairing.
    code = start(client)
    for _ in range(50):
        assert claim(client, "guess-guess-guess-guess!").status_code == 403
    assert claim(client, code).json() == {"api_key": KEY}


def test_oversized_guess_is_rejected_before_comparison(client):
    start(client)
    assert claim(client, "x" * 100_000).status_code == 422


def test_non_ascii_guess_is_a_plain_refusal(client):
    start(client)
    assert claim(client, "配对码").status_code == 403


def test_backend_without_a_key_never_pairs(client):
    client.app.state.api_key = ""
    assert client.post("/api/v1/pair/start").status_code == 409
    client.app.state.pairing = {"code": "c" * 24, "expires": float("inf")}
    assert claim(client, "c" * 24).status_code == 403      # never hands out an empty key


def test_app_mounts_pairing_outside_the_compute_gate():
    # Pairing has nothing to do with compute: no readiness gate, and claim has no auth
    # dependency at all (the one-time code is its only credential).
    import app as app_module
    routes = {r.path: r for r in app_module.app.routes if hasattr(r, "path")}
    deps = {path: {d.call for d in routes[path].dependant.dependencies}
            for path in ("/api/v1/pair/start", "/api/v1/pair/claim")}
    assert deps["/api/v1/pair/start"] == {app_module.verify_api_key}
    assert deps["/api/v1/pair/claim"] == set()


def test_every_api_route_requires_the_key_except_claim():
    import app as app_module
    unauthenticated = sorted(
        r.path for r in app_module.app.routes
        if getattr(r, "path", "").startswith("/api/v1/")
        and app_module.verify_api_key not in {d.call for d in r.dependant.dependencies})
    assert unauthenticated == ["/api/v1/pair/claim"]
