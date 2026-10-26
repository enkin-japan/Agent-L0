"""生产装配的就绪闸（app.py 的 include_router + routes._EXECUTION_ROUTES）。

test_offline_startup 用的是自己拼的 app：删掉 app.py 里的 Depends(require_compute_ready)
或从 _EXECUTION_ROUTES 漏掉一条执行路由，那里都不会变红。这里直接用 app.py 导出的 app
（不进 lifespan：不装配、不起任何服务、不碰真模型），算力未就绪时逐条打执行类路由。

执行类路由的清单写死在本文件（EXPECTED_EXECUTION）：它是独立的判据，常量少一条、多一条
都要在这里留下决定，而不是让常量自己证明自己。
"""
import pytest
from fastapi.testclient import TestClient

from framework.api import routes
from framework.api.devices import DeviceStore

KEY = "gate-key-for-tests"

# 会让算力端干活的操作（跑一轮、重试、批准后继续、恢复、手动触发定时任务、清 KV 存储）
EXPECTED_EXECUTION = {
    ("POST", "/api/v1/chat/{session_id}"),
    ("POST", "/api/v1/chat/{session_id}/turns"),
    ("POST", "/api/v1/chat/{session_id}/turns/retry"),
    ("POST", "/api/v1/chat/{session_id}/turns/approval"),
    ("POST", "/api/v1/chat/{session_id}/turns/resume"),
    ("POST", "/api/v1/schedules/{schedule_id}/run"),
    ("DELETE", "/api/v1/compute/kvstore"),
}


def _production_app():
    import app as entry          # 与 test_pairing 一样惰性导入：不在收集期触发 app.py 的装配副作用
    return entry.app


@pytest.fixture()
def gated(monkeypatch, tmp_path):
    app = _production_app()
    state = app.state

    def configure(installed):
        for name, value in (("api_key", KEY), ("auth_devices", None),
                            ("device_store", DeviceStore(tmp_path / "auth" / "devices.json")),
                            ("compute_ready", False), ("compute_installed", installed),
                            ("compute_state", "unavailable" if installed else "retrying")):
            monkeypatch.setattr(state, name, value, raising=False)
        return TestClient(app, raise_server_exceptions=False)
    return configure


def _url(path):
    return path.replace("{session_id}", "s-gate").replace("{schedule_id}", "sch-gate")


def _production_route(method, path):
    for route in _production_app().routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", ()):
            return route
    return None


def test_execution_routes_constant_matches_the_independent_list():
    assert routes._EXECUTION_ROUTES == EXPECTED_EXECUTION


@pytest.mark.parametrize("method,path", sorted(EXPECTED_EXECUTION))
def test_every_execution_route_is_mounted_behind_the_gate(method, path):
    route = _production_route(method, path)
    assert route is not None, f"{method} {path} is not mounted on app.app"
    assert routes.require_compute_ready in {d.call for d in route.dependant.dependencies}


@pytest.mark.parametrize("installed", [False, True], ids=["before_first_assembly", "after_outage"])
@pytest.mark.parametrize("method,path", sorted(EXPECTED_EXECUTION))
def test_execution_route_is_refused_while_compute_is_not_ready(gated, method, path, installed):
    client = gated(installed)
    response = client.request(method, _url(path), json={},
                              headers={"Authorization": f"Bearer {KEY}"})
    assert response.status_code == 503, (method, path, response.status_code, response.text[:200])
    assert response.json()["detail"]["reason"] == "compute_not_ready"
    assert response.headers["retry-after"] == "5"
