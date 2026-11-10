"""搜索 API 提供方 HTTP 端点金测试（build 85 批 C，设置页「搜索」）。

五个端点在生产 app 上真跑（TestClient，不走 lifespan / 装配）：正常与错误码（422 / 404）、
写端点鉴权（require_owner_or_paired，无权 401）、算力就绪前可用（_LOCAL_READ_ROUTES）、
响应 / 日志 / 422 错误体里没有密钥（FastAPI 缺省 422 会把请求体当 input 回显）。
桩点：网络出口 `web_tools._session`（仅 test 端点用）、临时数据根（AGENT_DATA_DIR）。
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from framework.agents import search_providers, web_tools
from framework.mcp.tools import api_tool_factory

KEY = "routes-key-for-tests"
AUTH = {"Authorization": f"Bearer {KEY}"}
SECRET = "test-key-0009-ROUTE"
SECRET2 = "test-key-0010-ROUTE"
BASE = "/api/v1/search-providers"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import app as entry
    from framework.api.devices import DeviceStore
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(search_providers, "_root_override", None)
    for name in list(os.environ):
        if name.startswith(search_providers.SECRET_PREFIX):
            monkeypatch.delenv(name)
    for name, value in (("api_key", KEY), ("auth_devices", None),
                        ("device_store", DeviceStore(tmp_path / "auth" / "devices.json")),
                        ("compute_ready", True), ("compute_installed", True),
                        ("compute_state", "ready")):
        monkeypatch.setattr(entry.app.state, name, value, raising=False)
    return TestClient(entry.app, raise_server_exceptions=False)


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.headers = status, data, {}

    def json(self):
        return self._data


class _Egress:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append(("get", url, dict(params or {}), dict(headers or {})))
        return self.reply

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls.append(("post", url, json, dict(headers or {})))
        return self.reply


def _no_secret(*texts):
    for t in texts:
        assert SECRET not in str(t) and SECRET2 not in str(t), "secret leaked"


def _add(http, ptype="brave", label="home", secret=SECRET, headers=AUTH):
    return http.post(BASE, json={"type": ptype, "label": label, "secret": secret}, headers=headers)


def test_get_shape_and_create(client, capsys):
    http = client
    r = http.get(BASE, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"types": [{"type": "brave", "label": "Brave Search"},
                                  {"type": "tavily", "label": "Tavily"}],
                        "providers": [], "fallback": "searxng"}
    r = _add(http)
    assert r.status_code == 201, r.text
    item = r.json()
    assert set(item) == {"id", "type", "label", "enabled", "has_secret", "status", "status_at"}
    assert (item["type"], item["label"], item["enabled"], item["has_secret"], item["status"],
            item["status_at"]) == ("brave", "home", True, True, "unused", None)
    assert http.get(BASE, headers=AUTH).json()["providers"] == [item]
    r2 = _add(http, ptype="tavily", label=None)
    assert r2.status_code == 201 and r2.json()["label"] == ""
    out = capsys.readouterr().out
    assert f"[SearchProviders] added id={item['id']} type=brave by device=default" in out
    _no_secret(r.text, r2.text, http.get(BASE, headers=AUTH).text, out)
    assert str(len(SECRET)) not in out.split("[SearchProviders] added")[1].split("\n")[0]


@pytest.mark.parametrize("body", [
    {"type": "google", "secret": SECRET}, {"type": "brave", "secret": ""},
    {"type": "brave", "secret": "   "}, {"type": "brave", "secret": SECRET + "x" * 600},
    {"type": "brave", "label": "l" * 65, "secret": SECRET}, {"secret": SECRET},
    {"type": "brave", "secret": SECRET, "extra": SECRET}, {"type": "brave"},
    {"type": "brave", "secret": SECRET + "\nX=1"}, {"type": 1, "secret": SECRET}, [SECRET], SECRET,
    {"type": "brave", "secret": SECRET + "\u2028X=1"}, {"type": "brave", "secret": SECRET + "\x85X=1"},
    {"type": "brave", "label": "a\u2029b", "secret": SECRET},
])
def test_create_rejects_with_422_without_echoing_the_body(client, body):
    r = client.post(BASE, json=body, headers=AUTH)
    assert r.status_code == 422, r.text
    _no_secret(r.text)
    assert client.get(BASE, headers=AUTH).json()["providers"] == []


def test_patch_enable_label_secret_and_errors(client, capsys):
    http = client
    pid = _add(http).json()["id"]
    r = http.patch(f"{BASE}/{pid}", json={"enabled": False, "label": "work"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert (r.json()["enabled"], r.json()["label"]) == (False, "work")
    r = http.patch(f"{BASE}/{pid}", json={"secret": SECRET2}, headers=AUTH)
    assert r.status_code == 200 and r.json()["has_secret"] is True
    assert api_tool_factory.read_secret(search_providers.env_name_for(pid)) == SECRET2
    _no_secret(r.text, capsys.readouterr().out)

    assert http.patch(f"{BASE}/brave-ffffff", json={"enabled": True}, headers=AUTH).status_code == 404
    assert http.patch(f"{BASE}/BAD..id", json={"enabled": True}, headers=AUTH).status_code == 404
    for body in ({"color": "red"}, {"enabled": "maybe"}, {"secret": ""},
                 {"secret": SECRET + "\u2028X=1"},
                 {"secret": SECRET, "type": "tavily"}, {"label": "l" * 65}):
        r = http.patch(f"{BASE}/{pid}", json=body, headers=AUTH)
        assert r.status_code == 422, (body, r.text)
        _no_secret(r.text)
    assert http.get(BASE, headers=AUTH).json()["providers"][0]["label"] == "work"


def test_delete_removes_entry_and_key(client, capsys):
    http = client
    pid = _add(http).json()["id"]
    r = http.delete(f"{BASE}/{pid}", headers=AUTH)
    assert r.status_code == 204 and r.content == b""
    assert http.get(BASE, headers=AUTH).json()["providers"] == []
    assert api_tool_factory.read_secret(search_providers.env_name_for(pid)) == ""
    assert http.delete(f"{BASE}/{pid}", headers=AUTH).status_code == 404
    assert f"[SearchProviders] deleted id={pid} type=brave" in capsys.readouterr().out


@pytest.mark.parametrize("reply,expected,status", [
    (_Resp(200, {"type": "search", "web": {"results": [
        {"title": "Python", "url": "https://www.python.org/", "description": "d"}]}}),
     {"ok": True, "kind": "ok", "results": 1}, "ok"),
    (_Resp(401, {"error": SECRET}), {"ok": False, "kind": "auth", "results": 0}, "auth_error"),
    (_Resp(429, {}), {"ok": False, "kind": "rate_limit", "results": 0}, "rate_limit"),
])
def test_test_endpoint_uses_the_evergreen_query(client, monkeypatch, reply, expected, status):
    http = client
    pid = _add(http).json()["id"]
    eg = _Egress(reply)
    monkeypatch.setattr(web_tools, "_session", lambda: eg)
    r = http.post(f"{BASE}/{pid}/test", headers=AUTH)
    assert r.status_code == 200 and r.json() == expected
    (method, url, params, headers) = eg.calls[0]
    assert params["q"] == web_tools.SEARCH_CONFIRM_QUERY and headers["X-Subscription-Token"] == SECRET
    listed = http.get(BASE, headers=AUTH).json()["providers"][0]
    assert listed["status"] == status and listed["status_at"] is not None
    _no_secret(r.text, json.dumps(listed))
    assert http.post(f"{BASE}/brave-ffffff/test", headers=AUTH).status_code == 404


def test_test_endpoint_tavily_quota(client, monkeypatch):
    pid = _add(client, ptype="tavily").json()["id"]
    eg = _Egress(_Resp(432, {}))
    monkeypatch.setattr(web_tools, "_session", lambda: eg)
    r = client.post(f"{BASE}/{pid}/test", headers=AUTH)
    assert r.json() == {"ok": False, "kind": "quota", "results": 0}
    assert eg.calls[0][0] == "post" and SECRET not in json.dumps(eg.calls[0][2])
    assert client.get(BASE, headers=AUTH).json()["providers"][0]["status"] == "quota"


def test_write_routes_require_auth_and_accept_paired_devices(client):
    import app as entry
    http = client
    pid = _add(http).json()["id"]
    assert http.get(BASE).status_code == 401
    for method, path, body in (("post", BASE, {"type": "brave", "secret": SECRET2}),
                               ("patch", f"{BASE}/{pid}", {"enabled": False}),
                               ("delete", f"{BASE}/{pid}", None),
                               ("post", f"{BASE}/{pid}/test", None)):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            kw = {"headers": headers} if body is None else {"headers": headers, "json": body}
            r = getattr(http, method)(path, **kw)
            assert r.status_code == 401, (method, path, r.status_code)
    assert len(http.get(BASE, headers=AUTH).json()["providers"]) == 1

    store = entry.app.state.device_store
    token, record, _ = store.issue(name="iPhone", via="qr")
    device = {"Authorization": f"Bearer {token}"}
    r = _add(http, ptype="tavily", headers=device)
    assert r.status_code == 201, r.text
    store.revoke(record["id"])
    assert _add(http, headers=device).status_code == 401


def test_write_routes_mount_require_owner_or_paired():
    import app as entry
    from framework.api.auth import require_owner, require_owner_or_paired
    deps = {}
    for r in entry.app.routes:
        if getattr(r, "path", "").startswith(BASE):
            for m in r.methods:
                deps[(m, r.path)] = {d.call for d in r.dependant.dependencies}
    writes = [("POST", BASE), ("PATCH", BASE + "/{provider_id}"), ("DELETE", BASE + "/{provider_id}"),
              ("POST", BASE + "/{provider_id}/test")]
    assert set(deps) == set(writes) | {("GET", BASE)}
    for op in writes:
        assert require_owner_or_paired in deps[op] and require_owner not in deps[op]
        assert entry.verify_api_key in deps[op]
    assert require_owner_or_paired not in deps[("GET", BASE)]
    assert entry.verify_api_key in deps[("GET", BASE)]


def test_routes_are_local_and_work_before_the_first_assembly(client, monkeypatch):
    import app as entry
    from framework.api import routes
    for op in (("GET", BASE), ("POST", BASE), ("PATCH", BASE + "/{provider_id}"),
               ("DELETE", BASE + "/{provider_id}"), ("POST", BASE + "/{provider_id}/test")):
        assert op in routes._LOCAL_READ_ROUTES
        assert op not in routes._EXECUTION_ROUTES
    for name, value in (("compute_ready", False), ("compute_installed", False),
                        ("compute_state", "retrying")):
        monkeypatch.setattr(entry.app.state, name, value, raising=False)
    assert client.get(BASE, headers=AUTH).status_code == 200
    assert _add(client).status_code == 201
    assert client.get("/api/v1/tools", headers=AUTH).status_code == 503
