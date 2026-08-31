"""kv_persist D 方案金测试（2026-08-31 用户拍板：默认 false + 执行端定留存 + 立即清除）。

守的不变量：
- 决策权归执行端：INFER_KV_PERSIST/INFER_KV_TTL_DAYS 经 config_loader env 覆盖生效
  （app 设置注入 sidecar 的通道）；开关取值拼错响亮拒启，不许静默猜方向；
- 算力端只记账+清扫：persist/restore 携带的 ttl_days 逐份记到期（.retention.json，
  用到即续期）；清扫逐份到期优先、无账目按 kv_ttl_days 兜底（0=不兜）、坏账目按
  到期处理（fail-closed 向隐私侧）；
- DELETE /v1/kvstore 全清（鉴权；返回份数+字节）；会话删除清理不受开关辖制
  （见 test_kv_persist 的源锚）。
服务端按路径装载（media/m9 测试同惯例，CPU-only）。
"""
import asyncio
import importlib.util
import json
import os
import pathlib
import time

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent.parent
_SERVER = _REPO / "inference" / "server.py"


@pytest.fixture(scope="module")
def srv():
    spec = importlib.util.spec_from_file_location("inference_server_kv_under_test", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def kvdir(srv, tmp_path, monkeypatch):
    d = tmp_path / "kvstore"
    d.mkdir()
    monkeypatch.setattr(srv, "KV_DIR", d)
    monkeypatch.setattr(srv, "KV_RETENTION_FILE", d / ".retention.json")
    monkeypatch.setattr(srv, "KV_TTL_SECONDS", 14 * 86400.0)
    return d


def _mk(d, hint, age_days=0.0, size=8):
    p = d / f"{hint}.safetensors"
    p.write_bytes(b"x" * size)
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))
    return p


# ---------- 逐份留存记账 ----------

def test_note_retention_records_and_renews(srv, kvdir):
    srv._kv_note_retention("sess-1", 3)
    ent = srv._kv_retention_load()["sess-1"]
    assert abs(ent["expires"] - (time.time() + 3 * 86400)) < 60
    srv._kv_note_retention("sess-1", 7)          # 用到即续期（滚动窗口）
    ent2 = srv._kv_retention_load()["sess-1"]
    assert ent2["expires"] > ent["expires"]


def test_note_retention_bad_ttl_falls_back_to_server_default(srv, kvdir):
    for bad in (None, "abc", -5, 0.2, True, float("nan")):
        srv._kv_note_retention("sess-bad", bad)
        ent = srv._kv_retention_load()["sess-bad"]
        assert abs(ent["expires"] - (time.time() + 14 * 86400)) < 60, bad


def test_note_retention_noop_when_both_disabled(srv, kvdir, monkeypatch):
    monkeypatch.setattr(srv, "KV_TTL_SECONDS", 0.0)
    srv._kv_note_retention("sess-x", None)
    assert "sess-x" not in srv._kv_retention_load()


# ---------- 到期清扫 ----------

def test_sweep_honors_per_file_expiry_over_mtime(srv, kvdir):
    _mk(kvdir, "old-but-kept", age_days=30)      # mtime 很老但执行端给了远期到期
    srv._kv_retention_update(lambda d: d.__setitem__(
        "old-but-kept", {"expires": time.time() + 3600, "ttl_days": 30}))
    _mk(kvdir, "expired-entry", age_days=0)      # 文件很新但逐份到期已过
    srv._kv_retention_update(lambda d: d.__setitem__(
        "expired-entry", {"expires": time.time() - 1, "ttl_days": 1}))
    assert srv._sweep_kvstore() == 1
    assert (kvdir / "old-but-kept.safetensors").exists()
    assert not (kvdir / "expired-entry.safetensors").exists()
    assert "expired-entry" not in srv._kv_retention_load(), "清掉的份要一并销账"


def test_sweep_unmapped_files_use_server_default(srv, kvdir):
    _mk(kvdir, "legacy-old", age_days=30)        # 旧存量无账目：按 kv_ttl_days 兜底
    _mk(kvdir, "legacy-new", age_days=1)
    assert srv._sweep_kvstore() == 1
    assert not (kvdir / "legacy-old.safetensors").exists()
    assert (kvdir / "legacy-new.safetensors").exists()


def test_sweep_default_zero_keeps_unmapped_but_honors_entries(srv, kvdir, monkeypatch):
    monkeypatch.setattr(srv, "KV_TTL_SECONDS", 0.0)
    _mk(kvdir, "legacy-ancient", age_days=400)
    _mk(kvdir, "mapped-expired")
    srv._kv_retention_update(lambda d: d.__setitem__(
        "mapped-expired", {"expires": time.time() - 1, "ttl_days": 1}))
    assert srv._sweep_kvstore() == 1
    assert (kvdir / "legacy-ancient.safetensors").exists(), "兜底关闭时无账目文件不清"
    assert not (kvdir / "mapped-expired.safetensors").exists(), "执行端定的到期照清"


def test_sweep_corrupt_entry_fails_closed(srv, kvdir):
    # 坏账目按到期处理：向隐私侧收敛（多清一份派生物无害，静默永存才是事故）
    _mk(kvdir, "corrupt")
    srv._kv_retention_update(lambda d: d.__setitem__(
        "corrupt", {"expires": "not-a-number"}))
    assert srv._sweep_kvstore() == 1
    assert not (kvdir / "corrupt.safetensors").exists()


# ---------- 立即清除端点 ----------

def test_kvstore_clear_deletes_all_and_reports(srv, kvdir):
    _mk(kvdir, "a", size=100)
    _mk(kvdir, "b", size=50)
    srv._kv_note_retention("a", 7)
    out = asyncio.run(srv.kvstore_clear())
    assert out == {"deleted": 2, "bytes": 150}
    assert not list(kvdir.glob("*.safetensors"))
    assert srv._kv_retention_load() == {}
    assert asyncio.run(srv.kvstore_clear()) == {"deleted": 0, "bytes": 0}   # 幂等


def test_kvstore_clear_route_is_authenticated(srv):
    route = next(r for r in srv.app.routes
                 if getattr(r, "path", "") == "/v1/kvstore"
                 and "DELETE" in getattr(r, "methods", set()))
    assert any(getattr(d, "dependency", None) is srv.verify_api_key
               for d in route.dependencies), "全清端点丢了 Bearer 鉴权"


# ---------- 端点接线源锚 ----------

def test_persist_and_restore_note_retention_source_anchor():
    """persist/restore 成功路都必须记/续到期——漏一处，那条路落的份就成了只受
    兜底 TTL 管的二等公民（执行端留存期设置对它失效）。"""
    import ast
    tree = ast.parse(_SERVER.read_text(encoding="utf-8"))
    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in ("cache_persist",
                                                                   "cache_restore"):
            # 直调或经 asyncio.to_thread(_kv_note_retention, ...) 引用都算接上
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == "_kv_note_retention":
                    hits.add(node.name)
    assert hits == {"cache_persist", "cache_restore"}, hits


def test_sweep_kvstore_wired_into_sweep_loop_source_anchor():
    """源锚（首评 S）：_sweep_kvstore 必须真的挂在 lifespan 的清扫循环里——六条
    清扫行为测试全是直调函数，删掉循环里那一行它们照绿，而到期清扫正是整个留存
    设计立足的行为。（能力强开 `{**_inf_cfg, "kv_persist": True}` 由
    test_bench_kv 的装载全表达式锚钉，这里不重复弱化版。）"""
    import ast
    tree = ast.parse(_SERVER.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    called = {sub.func.id for sub in ast.walk(fn)
              if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)}
    called |= {sub.id for sub in ast.walk(fn) if isinstance(sub, ast.Name)}
    assert "_sweep_kvstore" in called, "清扫循环里找不到 _sweep_kvstore"


def test_note_retention_caps_at_365_days(srv, kvdir):
    # 磁盘是本机的：authed 客户端传 1e6 天=变相永久留存，inf 会写坏账本（首评 S）
    for huge in (1e6, 400, 1e308):
        srv._kv_note_retention("sess-huge", huge)
        ent = srv._kv_retention_load()["sess-huge"]
        assert ent["expires"] <= time.time() + 365 * 86400 + 60, huge


def test_store_disable_refuses_persist_and_restore(srv, kvdir, monkeypatch):
    # 机主否决权：INFER_KV_STORE_DISABLE=1 → persist 回 false（边端冷路语义）、
    # restore 404；清理端点照常（拒收≠拒清）
    monkeypatch.setattr(srv, "KV_STORE_DISABLED", True)

    class _Req:
        async def json(self):
            return {"hint": "s1", "ttl_days": 7}
    out = asyncio.run(srv.cache_persist("cid-x", _Req()))
    assert out == {"persisted": False}
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        asyncio.run(srv.cache_restore(_Req()))
    assert ei.value.status_code == 404
    assert asyncio.run(srv.kvstore_clear())["deleted"] == 0


def test_drop_endpoint_prunes_ledger(srv, kvdir):
    _mk(kvdir, "sess-d")
    srv._kv_note_retention("sess-d", 7)
    out = asyncio.run(srv.cache_persisted_drop("sess-d"))
    assert out == {"dropped": True}
    assert "sess-d" not in srv._kv_retention_load(), "删文件必须一并销账"


# ---------- backend 薄代理（app「立即清除」按钮的落点） ----------

class _Resp:
    def __init__(self, code=200, payload=None, text=""):
        self.status_code = code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _patch_proxy(monkeypatch, host="10.0.0.9", handler=None):
    import framework.utils.config_loader as cl
    from framework.api import routes as _r
    monkeypatch.setattr(cl, "load_section",
                        lambda name: {"remote": {"host": host, "port": 14161}})
    import requests
    if handler:
        monkeypatch.setattr(requests, "delete", handler)
    return _r


def test_kvstore_proxy_success_passthrough(monkeypatch):
    seen = {}

    def ok(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers)
        return _Resp(200, {"deleted": 3, "bytes": 999})
    monkeypatch.setenv("INFER_API_KEY", "k-123")
    routes = _patch_proxy(monkeypatch, handler=ok)
    out = asyncio.run(routes.compute_kvstore_clear())
    assert out == {"deleted": 3, "bytes": 999}
    assert seen["url"] == "http://10.0.0.9:14161/v1/kvstore"
    assert seen["headers"] == {"Authorization": "Bearer k-123"}


def test_kvstore_proxy_upstream_error_maps_502(monkeypatch):
    # 上游 401 原样透传会让 app 误以为自己的 backend 钥匙错（首评 S）：一律 502 +
    # 提出来的 detail，不嵌套 JSON
    from fastapi import HTTPException
    routes = _patch_proxy(monkeypatch, handler=lambda url, headers=None, timeout=None:
                          _Resp(401, {"detail": "API 密钥无效"}))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes.compute_kvstore_clear())
    assert ei.value.status_code == 502
    assert "API 密钥无效" in ei.value.detail and "{" not in ei.value.detail


def test_kvstore_proxy_no_host_503(monkeypatch):
    from fastapi import HTTPException
    routes = _patch_proxy(monkeypatch, host="")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes.compute_kvstore_clear())
    assert ei.value.status_code == 503 and "算力端地址" in ei.value.detail


def test_kvstore_proxy_unreachable_503(monkeypatch):
    from fastapi import HTTPException

    def boom(url, headers=None, timeout=None):
        raise ConnectionError("refused")
    routes = _patch_proxy(monkeypatch, handler=boom)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes.compute_kvstore_clear())
    assert ei.value.status_code == 503


# ---------- 执行端 env 覆盖（config_loader） ----------

def test_env_override_kv_persist_parsing(monkeypatch):
    from framework.utils.config_loader import _apply_host_overrides
    monkeypatch.setenv("INFER_KV_PERSIST", "1")
    assert _apply_host_overrides({})["inference"]["kv_persist"] is True
    monkeypatch.setenv("INFER_KV_PERSIST", "false")
    assert _apply_host_overrides({"inference": {"kv_persist": True}}
                                 )["inference"]["kv_persist"] is False
    monkeypatch.setenv("INFER_KV_PERSIST", "ture")   # 隐私开关拼错不许静默猜
    with pytest.raises(SystemExit, match="INFER_KV_PERSIST"):
        _apply_host_overrides({})


def test_env_override_kv_ttl_parsing(monkeypatch):
    from framework.utils.config_loader import _apply_host_overrides
    monkeypatch.setenv("INFER_KV_TTL_DAYS", "30")
    assert _apply_host_overrides({})["inference"]["kv_ttl_days"] == 30.0
    monkeypatch.setenv("INFER_KV_TTL_DAYS", "一个月")
    with pytest.raises(SystemExit, match="INFER_KV_TTL_DAYS"):
        _apply_host_overrides({})


def test_kv_gate_defaults(monkeypatch):
    import framework.utils.config_loader as cl
    monkeypatch.setattr(cl, "load_section", lambda name: {})
    assert cl.kv_persist_enabled() is False, "缺省必须 false（隐私门）"
    assert cl.kv_ttl_days() == 14.0
    monkeypatch.setattr(cl, "load_section",
                        lambda name: {"kv_persist": True, "kv_ttl_days": 3})
    assert cl.kv_persist_enabled() is True
    assert cl.kv_ttl_days() == 3.0
    # 便利参数坏值回落不拒启；开关非布尔真值不算开（"true" 字符串在 config 里
    # 是 YAML 会解析好的，这里防的是手写 JSON/env 注入错型）
    monkeypatch.setattr(cl, "load_section",
                        lambda name: {"kv_persist": "true", "kv_ttl_days": -2})
    assert cl.kv_persist_enabled() is False
    assert cl.kv_ttl_days() == 14.0
