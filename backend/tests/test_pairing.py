"""iPhone 连接金测试：Mac 同意后发设备票据（framework/api/pairing.py + devices.py + auth.py）。

claim / request / poll 免 Bearer，所以这里锁的全是「什么时候绝不能发票据」：没领过码、
码过期、码用过、后端没共享密钥、ticket 不对、Mac 没点同意、请求被拒或过期；票据只发一次；
乱猜的人烧不掉别人的码；管理接口只给共享密钥（设备票据 403）；吊销 / 到期立即 401；
文件只存哈希、损坏 fail-closed；以及全部 /api/v1 路由里免 Bearer 的恰好是这三条
（白名单写死，新增免鉴权路由必须在这里留下决定）。
"""
import json
import os
import stat

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from framework.api import devices as devices_module
from framework.api import pairing
from framework.api.auth import verify_api_key

KEY = "agent-key-for-tests"
BEARER = {"Authorization": f"Bearer {KEY}"}
MAC_ID = "0b6c4c1e-77d2-4f0e-9a51-6f1e2d3c4b5a"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_INSTANCE_ID", raising=False)
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    app = FastAPI()
    app.state.api_key = KEY

    @app.get("/api/v1/probe", dependencies=[Depends(verify_api_key)])
    async def probe():
        return {"ok": True}

    app.include_router(pairing.router)
    return TestClient(app)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def start(client):
    r = client.post("/api/v1/pair/start", headers=BEARER)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["expires_in"] == pairing.TTL_SECONDS
    return r.json()["code"]


def claim(client, code, **extra):
    return client.post("/api/v1/pair/claim", json={"code": code, **extra})


def ticket(client):
    r = client.get("/api/v1/pair/ticket", headers=BEARER)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    return r.json()["ticket"]


def submit(client, value, name="Pat 的 iPhone"):
    return client.post("/api/v1/pair/request", json={"ticket": value, "name": name})


def poll(client, rid, secret):
    return client.post(f"/api/v1/pair/request/{rid}/poll", json={"poll_secret": secret})


def store_file(tmp_path):
    return tmp_path / "auth" / "devices.json"


# ---------- 扫码：打开二维码即同意 ----------

def test_code_is_exchanged_for_a_device_token_exactly_once(client):
    code = start(client)
    assert len(code) >= 24
    first = claim(client, code)
    body = first.json()
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert body["token"] and body["token"] != KEY and body["api_key"] == body["token"]
    assert body["device_id"].startswith("d_")
    assert claim(client, code).status_code == 403          # single use
    assert client.get("/api/v1/probe", headers=bearer(body["token"])).status_code == 200


def test_shared_key_never_leaves_the_mac(client):
    code = start(client)
    raw = claim(client, code).text
    assert KEY not in raw
    rid = submit(client, ticket(client)).json()
    client.post(f"/api/v1/pair/requests/{rid['request_id']}/approve", headers=BEARER)
    assert KEY not in poll(client, rid["request_id"], rid["poll_secret"]).text


def test_claim_names_the_mac_when_the_app_injected_its_id(client, monkeypatch):
    # The iPhone keeps several Macs; instance_id is how it recognises this one again.
    monkeypatch.setenv("AGENT_INSTANCE_ID", MAC_ID)
    assert claim(client, start(client)).json()["instance_id"] == MAC_ID
    for bad in ("short", "has space in it", "x" * 65, "../../etc/passwd"):
        monkeypatch.setenv("AGENT_INSTANCE_ID", bad)
        assert "instance_id" not in claim(client, start(client)).json(), bad


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
    assert claim(client, new).status_code == 200


def test_wrong_guesses_cannot_burn_someone_elses_code(client):
    # Any tailnet peer can reach /claim; burning on failures would let it block pairing.
    code = start(client)
    for _ in range(50):
        assert claim(client, "guess-guess-guess-guess!").status_code == 403
    assert claim(client, code).status_code == 200


def test_oversized_guess_is_rejected_before_comparison(client):
    start(client)
    assert claim(client, "x" * 100_000).status_code == 422


def test_non_ascii_guess_is_a_plain_refusal(client):
    start(client)
    assert claim(client, "配对码").status_code == 403


def test_backend_without_a_key_never_pairs(client):
    # A device token would switch auth on for every request and lock out the keyless Mac.
    client.app.state.api_key = ""
    assert client.post("/api/v1/pair/start").status_code == 409
    client.app.state.pairing = {"code": "c" * 24, "expires": float("inf")}
    assert claim(client, "c" * 24).status_code == 403
    assert client.get("/api/v1/pair/ticket").status_code == 409


# ---------- 握手：同一 Apple ID，Mac 点同意 ----------

def test_request_needs_the_icloud_ticket(client):
    assert submit(client, "no-ticket-was-ever-made-xxxxxxxxxxxx").status_code == 403   # none issued yet
    value = ticket(client)
    assert submit(client, value[:-1] + ("A" if value[-1] != "A" else "B")).status_code == 403
    assert client.get("/api/v1/pair/requests", headers=BEARER).json()["requests"] == []


def test_handshake_issues_a_token_only_after_the_mac_approves(client):
    r = submit(client, ticket(client)).json()
    assert len(r["code"]) == 4 and r["code"].isdigit()
    assert "token" not in r
    pending = client.get("/api/v1/pair/requests", headers=BEARER).json()["requests"]
    assert [(p["id"], p["code"], p["name"]) for p in pending] == [(r["request_id"], r["code"], "Pat 的 iPhone")]
    for _ in range(3):
        waiting = poll(client, r["request_id"], r["poll_secret"])
        assert waiting.json() == {"status": "pending"}
    assert poll(client, r["request_id"], "wrong-secret").status_code == 404
    assert client.post(f"/api/v1/pair/requests/{r['request_id']}/approve", headers=BEARER).status_code == 200
    done = poll(client, r["request_id"], r["poll_secret"]).json()
    assert done["status"] == "approved" and done["token"]
    assert poll(client, r["request_id"], r["poll_secret"]).status_code == 404   # issued once
    assert client.get("/api/v1/probe", headers=bearer(done["token"])).status_code == 200
    names = [d["name"] for d in client.get("/api/v1/pair/devices", headers=BEARER).json()["devices"]]
    assert names == ["Pat 的 iPhone"]


def test_denied_or_expired_requests_never_yield_a_token(client, monkeypatch):
    value = ticket(client)
    r = submit(client, value).json()
    client.post(f"/api/v1/pair/requests/{r['request_id']}/deny", headers=BEARER)
    assert poll(client, r["request_id"], r["poll_secret"]).json() == {"status": "denied"}
    assert poll(client, r["request_id"], r["poll_secret"]).status_code == 404
    now = [5000.0]
    monkeypatch.setattr(pairing.time, "monotonic", lambda: now[0])
    late = submit(client, value).json()
    now[0] += pairing.REQUEST_TTL_SECONDS
    assert client.post(f"/api/v1/pair/requests/{late['request_id']}/approve", headers=BEARER).status_code == 404
    assert poll(client, late["request_id"], late["poll_secret"]).status_code == 404
    assert client.get("/api/v1/pair/devices", headers=BEARER).json()["devices"] == []


def test_requests_are_bounded(client, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(pairing.time, "monotonic", lambda: now[0])
    value = ticket(client)
    for _ in range(pairing.MAX_REQUESTS_PER_MINUTE):
        assert submit(client, value).status_code == 200
    assert submit(client, value).status_code == 429          # per minute
    now[0] += 61
    assert submit(client, value).status_code == 429          # still 3 pending
    for p in client.get("/api/v1/pair/requests", headers=BEARER).json()["requests"]:
        client.post(f"/api/v1/pair/requests/{p['id']}/deny", headers=BEARER)
    # denied requests no longer occupy a slot the Mac UI cannot show
    assert submit(client, value).status_code == 200
    now[0] += pairing.REQUEST_TTL_SECONDS
    assert submit(client, value).status_code == 200


def test_pending_codes_are_distinct(client, monkeypatch):
    draws = iter([1234, 1234, 1234, 5678, 1234, 5678, 9012])
    monkeypatch.setattr(pairing.secrets, "randbelow", lambda n: next(draws))
    value = ticket(client)
    codes = [submit(client, value).json()["code"] for _ in range(3)]
    assert codes == ["1234", "5678", "9012"]


def test_rotating_the_ticket_voids_it_and_pending_requests(client):
    old = ticket(client)
    r = submit(client, old).json()
    new = client.post("/api/v1/pair/ticket/rotate", headers=BEARER).json()["ticket"]
    assert new != old and ticket(client) == new
    assert submit(client, old).status_code == 403
    assert poll(client, r["request_id"], r["poll_secret"]).status_code == 404


# ---------- 只给 Mac：设备票据管不了别的设备 ----------

def test_device_tokens_cannot_manage_pairing(client):
    token = claim(client, start(client)).json()["token"]
    for method, path in [("post", "/api/v1/pair/start"), ("get", "/api/v1/pair/ticket"),
                         ("post", "/api/v1/pair/ticket/rotate"), ("get", "/api/v1/pair/requests"),
                         ("get", "/api/v1/pair/devices"), ("post", "/api/v1/pair/upgrade"),
                         ("put", "/api/v1/pair/settings")]:
        kwargs = {"json": {}} if method in ("post", "put") else {}
        assert getattr(client, method)(path, headers=bearer(token), **kwargs).status_code == 403, path


def test_upgrade_swaps_the_shared_key_for_a_device_token(client):
    r = client.post("/api/v1/pair/upgrade", headers=BEARER, json={"name": "旧 iPhone"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["token"] != KEY
    devices = client.get("/api/v1/pair/devices", headers=BEARER).json()["devices"]
    assert [(d["name"], d["via"]) for d in devices] == [("旧 iPhone", "upgrade")]


# ---------- 吊销、有效期、改名 ----------

def test_revoked_device_is_refused_immediately(client):
    token = claim(client, start(client)).json()["token"]
    device = client.get("/api/v1/pair/devices", headers=BEARER).json()["devices"][0]["id"]
    assert client.delete(f"/api/v1/pair/devices/{device}", headers=BEARER).json()["revoked"] is True
    assert client.get("/api/v1/probe", headers=bearer(token)).status_code == 401
    assert client.get("/api/v1/probe", headers=BEARER).status_code == 200     # Mac unaffected


def test_revoking_an_unknown_device_is_404(client):
    assert client.delete("/api/v1/pair/devices/d_nope", headers=BEARER).status_code == 404


def test_a_device_can_disconnect_itself(client):
    token = claim(client, start(client)).json()["token"]
    assert client.delete("/api/v1/pair/self", headers=bearer(token)).status_code == 200
    assert client.get("/api/v1/probe", headers=bearer(token)).status_code == 401
    assert client.get("/api/v1/pair/devices", headers=BEARER).json()["devices"] == []
    assert client.delete("/api/v1/pair/self", headers=BEARER).status_code == 400


def test_revoking_unregisters_the_devices_push(client):
    class Registry:
        removed = []

        def remove(self, device_id):
            self.removed.append(device_id)
        bound = []

        def remove_bound(self, auth_device):
            self.bound.append(auth_device)
    client.app.state.push_registry = Registry()
    claim(client, start(client), push_device_id="push-123")
    device = client.get("/api/v1/pair/devices", headers=BEARER).json()["devices"][0]["id"]
    client.delete(f"/api/v1/pair/devices/{device}", headers=BEARER)
    assert Registry.removed == ["push-123"] and Registry.bound == [device]


def test_expiry_defaults_to_90_days_and_can_be_made_permanent(client, monkeypatch):
    now = [1_000_000.0]
    client.app.state.device_store = devices_module.DeviceStore(
        devices_module.Path(os.environ["AGENT_DATA_DIR"]) / "auth" / "devices.json",
        clock=lambda: now[0])
    token = claim(client, start(client)).json()["token"]
    device = client.get("/api/v1/pair/devices", headers=BEARER).json()["devices"][0]
    assert device["expires"] == now[0] + 90 * 86400
    now[0] += 90 * 86400 - 1
    assert client.get("/api/v1/probe", headers=bearer(token)).status_code == 200
    now[0] += 1
    expired = client.get("/api/v1/probe", headers=bearer(token))
    assert expired.status_code == 401 and expired.json()["detail"] == "device_expired"
    assert client.patch(f"/api/v1/pair/devices/{device['id']}", headers=BEARER,
                        json={"permanent": True}).json()["expires"] is None
    now[0] += 10 * 365 * 86400
    assert client.get("/api/v1/probe", headers=bearer(token)).status_code == 200
    back = client.patch(f"/api/v1/pair/devices/{device['id']}", headers=BEARER, json={"permanent": False})
    assert back.json()["expires"] == now[0] + 90 * 86400


def test_settings_require_an_explicit_value(client):
    # An empty body must not silently make every new device permanent.
    assert client.put("/api/v1/pair/settings", headers=BEARER, json={}).status_code == 422
    assert client.get("/api/v1/pair/devices", headers=BEARER).json()["default_ttl_days"] == 90


def test_device_names_cannot_impersonate_another_row(client):
    claim(client, start(client))
    device = client.get("/api/v1/pair/devices", headers=BEARER).json()["devices"][0]
    renamed = client.patch(f"/api/v1/pair/devices/{device['id']}", headers=BEARER,
                           json={"name": "Pat\u202e的 iPhone\x7f"}).json()
    assert renamed["name"] == "Pat的 iPhone"


def test_default_expiry_switch(client):
    assert client.put("/api/v1/pair/settings", headers=BEARER,
                      json={"default_ttl_days": None}).json() == {"default_ttl_days": None}
    claim(client, start(client))
    device = client.get("/api/v1/pair/devices", headers=BEARER).json()
    assert device["default_ttl_days"] is None and device["devices"][0]["expires"] is None


def test_mac_can_rename_a_device(client):
    claim(client, start(client))
    device = client.get("/api/v1/pair/devices", headers=BEARER).json()["devices"][0]
    assert device["name"].startswith("iPhone（")
    renamed = client.patch(f"/api/v1/pair/devices/{device['id']}", headers=BEARER, json={"name": "  Pat\n的 iPhone "})
    assert renamed.json()["name"] == "Pat 的 iPhone"
    assert client.patch("/api/v1/pair/devices/d_nope", headers=BEARER, json={"name": "x"}).status_code == 404


# ---------- 存储：只存哈希、0600、损坏 fail-closed ----------

def test_store_holds_only_hashes_with_private_permissions(client, tmp_path):
    token = claim(client, start(client)).json()["token"]
    path = store_file(tmp_path)
    text = path.read_text()
    assert token not in text and KEY not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    ticket_file = tmp_path / "auth" / "pair_ticket"
    ticket(client)
    assert stat.S_IMODE(ticket_file.stat().st_mode) == 0o600


@pytest.mark.parametrize("damage", [
    "not json",
    json.dumps({"v": 2, "devices": []}),
    json.dumps({"v": 1, "devices": {}}),
    json.dumps({"v": 1, "devices": [{"id": "d_x", "token_sha256": "zz"}]}),
    "TYPES",
    "NO_EXPIRES",
])
def test_corrupt_store_fails_closed(client, tmp_path, damage):
    token = claim(client, start(client)).json()["token"]
    path = store_file(tmp_path)
    if damage in ("TYPES", "NO_EXPIRES"):
        data = json.loads(path.read_text())
        if damage == "TYPES":
            data["devices"][0]["created"] = "yesterday"
        else:
            del data["devices"][0]["expires"]       # missing ≠ permanent
        damage = json.dumps(data)
    path.write_text(damage)
    client.app.state.device_store = None                    # next request reloads from disk
    assert client.get("/api/v1/probe", headers=bearer(token)).status_code == 401
    assert client.get("/api/v1/probe", headers=BEARER).status_code == 200
    listing = client.get("/api/v1/pair/devices", headers=BEARER).json()
    assert listing["devices"] == [] and listing["notice"]
    assert list((tmp_path / "auth").glob("devices.json.corrupt-*"))


def test_losing_the_store_never_switches_auth_off(client, tmp_path):
    token = claim(client, start(client)).json()["token"]
    store_file(tmp_path).unlink()
    client.app.state.device_store = None
    assert client.get("/api/v1/probe").status_code == 401
    assert client.get("/api/v1/probe", headers=bearer(token)).status_code == 401


def test_whether_auth_is_on_depends_only_on_static_config(client, tmp_path):
    # The store must never switch auth on or off: otherwise deleting a damaged
    # devices.json on a keyless backend would flip it from closed to wide open.
    claim(client, start(client))
    client.app.state.api_key = ""
    before = client.get("/api/v1/probe").status_code
    store_file(tmp_path).unlink()
    client.app.state.device_store = None
    after = client.get("/api/v1/probe").status_code
    assert before == after == 200          # keyless = the configured no-auth dev mode, both times


def test_push_entries_belong_to_the_device_that_registered_them(tmp_path, monkeypatch):
    # Device B must not take over (or silence) device A's notifications.
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from framework.api.routes import router as api_router
    from framework.push.registry import PushRegistry
    app = FastAPI()
    app.state.api_key = KEY
    app.state.push_registry = PushRegistry(tmp_path / "push")
    app.state.push_dispatcher = type("D", (), {"enabled": True})()
    app.include_router(pairing.router)
    app.include_router(api_router, dependencies=[Depends(verify_api_key)])
    c = TestClient(app)
    a = claim(c, c.post("/api/v1/pair/start", headers=BEARER).json()["code"]).json()["token"]
    b = claim(c, c.post("/api/v1/pair/start", headers=BEARER).json()["code"]).json()["token"]
    body = {"device_id": "push-A", "platform": "ios", "token": "apns-A"}
    assert c.post("/api/v1/push/register", headers=bearer(a), json=body).status_code == 200
    stolen = {**body, "token": "apns-B"}
    assert c.post("/api/v1/push/register", headers=bearer(b), json=stolen).status_code == 403
    assert c.delete("/api/v1/push/devices/push-A", headers=bearer(b)).status_code == 403
    assert app.state.push_registry.devices["push-A"]["token"] == "apns-A"
    device_a = app.state.push_registry.devices["push-A"]["auth_device"]
    assert c.delete(f"/api/v1/pair/devices/{device_a}", headers=BEARER).status_code == 200
    assert "push-A" not in app.state.push_registry.devices        # revocation drops its pushes


def test_store_survives_a_restart(client, tmp_path):
    token = claim(client, start(client)).json()["token"]
    client.app.state.device_store = None
    assert client.get("/api/v1/probe", headers=bearer(token)).status_code == 200


# ---------- 挂载：配对不挂计算闸；免 Bearer 的恰好三条 ----------

def test_app_mounts_pairing_outside_the_compute_gate():
    import app as app_module
    from framework.api.auth import require_owner
    routes = {r.path: r for r in app_module.app.routes if hasattr(r, "path")}
    deps = {path: {d.call for d in routes[path].dependant.dependencies}
            for path in ("/api/v1/pair/start", "/api/v1/pair/claim")}
    assert deps["/api/v1/pair/start"] == {app_module.verify_api_key, require_owner}
    assert deps["/api/v1/pair/claim"] == set()


def test_every_api_route_requires_the_key_except_the_pairing_entrances():
    import app as app_module
    unauthenticated = sorted(
        r.path for r in app_module.app.routes
        if getattr(r, "path", "").startswith("/api/v1/")
        and app_module.verify_api_key not in {d.call for d in r.dependant.dependencies})
    assert unauthenticated == ["/api/v1/pair/claim", "/api/v1/pair/request",
                               "/api/v1/pair/request/{request_id}/poll"]


def test_management_routes_are_owner_only():
    import app as app_module
    from framework.api.auth import require_owner
    owner_only = {"/api/v1/pair/start", "/api/v1/pair/requests", "/api/v1/pair/requests/{request_id}/approve",
                  "/api/v1/pair/requests/{request_id}/deny", "/api/v1/pair/ticket", "/api/v1/pair/ticket/rotate",
                  "/api/v1/pair/upgrade", "/api/v1/pair/devices", "/api/v1/pair/devices/{device_id}",
                  "/api/v1/pair/settings"}
    seen = set()
    for r in app_module.app.routes:
        if getattr(r, "path", "") in owner_only:
            assert require_owner in {d.call for d in r.dependant.dependencies}, r.path
            seen.add(r.path)
    assert seen == owner_only
