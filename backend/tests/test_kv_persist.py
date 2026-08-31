"""T2 冻结落盘/解冻热恢复金测试。

前半用真 mlx 缓存对象钉序列化往返（KVCache + RotatingKVCache 的 state/meta_state
协议，含续写一致性）；后半用 Fake 后端钉编排语义（冻结挂钩、两级热启动优先级、
版本/模型守卫的冷路兜底）。本文件不加载任何模型权重。
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import mlx.core as mx
from mlx_vlm.models import cache as C

import pytest

import framework.backends.mlx.backend as mlx_backend_mod
import framework.memory.session as session_mod
from framework.backends.mlx.backend import MLXBackend, MLXCacheHandle
from framework.core.engine import AgentEngine
from framework.memory.session import ChatSession


import framework.core.engine as engine_mod


@pytest.fixture
def kv_on(monkeypatch):
    """执行端总开关拨开（D 方案缺省 false）：冻结落盘与解冻热恢复的既有语义
    测试都在「用户显式开启」前提下进行。两处消费点都是模块顶层绑定，各补各的名。"""
    monkeypatch.setattr(session_mod, "kv_persist_enabled", lambda: True)
    monkeypatch.setattr(engine_mod, "kv_persist_enabled", lambda: True)


@pytest.fixture
def kv_off(monkeypatch):
    """开关钉死在关（首评 S：直读真 config 会被开发者 shell 里的
    INFER_KV_PERSIST=1 污染——测试断言的是「门关时的行为」，不是「你的环境很干净」）。"""
    monkeypatch.setattr(session_mod, "kv_persist_enabled", lambda: False)
    monkeypatch.setattr(engine_mod, "kv_persist_enabled", lambda: False)


def _backend(model_key="test-model"):
    return MLXBackend(None, None, {"model_path": model_key})


def _filled_handle():
    kv = C.KVCache()
    kv.update_and_fetch(mx.random.normal((1, 2, 10, 4)), mx.random.normal((1, 2, 10, 4)))
    rot = C.RotatingKVCache(max_size=8, keep=2)
    for _ in range(3):
        rot.update_and_fetch(mx.random.normal((1, 2, 5, 4)),
                             mx.random.normal((1, 2, 5, 4)))
    h = MLXCacheHandle()
    h.state.cache = [kv, rot]
    h.state.token_ids = [1, 2, 3, 4, 5]
    return h


# ---------- 序列化往返（真 mlx 对象） ----------

def test_roundtrip_restores_offsets_tokens_and_behaviour(tmp_path):
    be = _backend()
    h = _filled_handle()
    dest = tmp_path / "kv.safetensors"
    assert be.persist_cache(h, str(dest)) is True
    assert dest.exists() and not dest.with_name("kv.tmp.safetensors").exists()

    h2 = be.restore_cache(str(dest))
    assert h2 is not None
    assert h2.state.token_ids == [1, 2, 3, 4, 5]
    kv, rot = h.state.cache
    kv2, rot2 = h2.state.cache
    assert kv2.offset == kv.offset and rot2.offset == rot.offset
    # 续写一致：恢复的缓存与原缓存喂同一 token，输出窗口完全一致
    a, b = mx.random.normal((1, 2, 1, 4)), mx.random.normal((1, 2, 1, 4))
    o1, o2 = rot.update_and_fetch(a, b), rot2.update_and_fetch(a, b)
    assert o1[0].shape == o2[0].shape
    assert bool(mx.allclose(o1[0], o2[0]).item())


def test_empty_cache_not_persisted(tmp_path):
    be = _backend()
    dest = tmp_path / "kv.safetensors"
    assert be.persist_cache(MLXCacheHandle(), str(dest)) is False
    assert not dest.exists()


def test_missing_file_restores_none(tmp_path):
    assert _backend().restore_cache(str(tmp_path / "ghost.safetensors")) is None


def test_model_mismatch_rejected(tmp_path):
    dest = tmp_path / "kv.safetensors"
    assert _backend("model-A").persist_cache(_filled_handle(), str(dest)) is True
    assert _backend("model-B").restore_cache(str(dest)) is None
    assert _backend("model-A").restore_cache(str(dest)) is not None


def test_version_mismatch_rejected(tmp_path, monkeypatch):
    dest = tmp_path / "kv.safetensors"
    be = _backend()
    assert be.persist_cache(_filled_handle(), str(dest)) is True
    monkeypatch.setattr(mlx_backend_mod, "CACHE_PERSIST_VERSION", "999")
    assert be.restore_cache(str(dest)) is None


# ---------- kv_persist 用户开关（Q9-a：由用户决定） ----------

def test_kv_persist_switch_off_blocks_both_directions(tmp_path):
    dest = tmp_path / "kv.safetensors"
    on = MLXBackend(None, None, {"model_path": "m", "kv_persist": True})
    assert on.persist_cache(_filled_handle(), str(dest)) is True

    off = MLXBackend(None, None, {"model_path": "m", "kv_persist": False})
    assert off.persist_cache(_filled_handle(), str(dest / "x")) is False   # 不写
    assert off.restore_cache(str(dest)) is None                            # 已有文件也不读
    assert on.restore_cache(str(dest)) is not None                         # 开着的照常


# ---------- RemoteBackend 远端三方法（Q9-a） ----------

class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        return self.resp

    def delete(self, url, **kw):
        self.calls.append(("delete", url, kw))
        return _FakeResp()


def _remote(resp):
    from framework.backends.remote.backend import RemoteBackend
    rb = RemoteBackend.__new__(RemoteBackend)
    rb.base_url = "http://a:1"
    rb._session = _FakeHttp(resp)
    return rb


def test_remote_hint_is_session_dir_name():
    from framework.backends.remote.backend import RemoteBackend
    assert RemoteBackend._persist_hint("/x/sessions/sid-42/kv_cache.safetensors") == "sid-42"


def test_remote_persist_restore_drop():
    rb = _remote(_FakeResp(200, {"persisted": True}))
    h = SimpleNamespace(cache_id="c1")
    assert rb.persist_cache(h, "/s/sess-1/kv_cache.safetensors") is True
    assert "/v1/cache/c1/persist" in rb._session.calls[0][1]
    body = rb._session.calls[0][2]["json"]
    assert body["hint"] == "sess-1"
    # D 方案：留存期由执行端随身携带（服务端逐份记到期），缺省 14 天
    assert body["ttl_days"] == 14.0
    assert rb.persist_cache(SimpleNamespace(cache_id=None), "/s/x/kv.safetensors") is False

    rb = _remote(_FakeResp(200, {"cache_id": "c9"}))
    restored = rb.restore_cache("/s/sess-1/kv_cache.safetensors")
    assert restored is not None and restored.cache_id == "c9"

    rb = _remote(_FakeResp(404))
    assert rb.restore_cache("/s/sess-1/kv_cache.safetensors") is None      # 无产物 → 冷路

    rb = _remote(_FakeResp())
    rb.drop_persisted("/s/sess-1/kv_cache.safetensors")
    assert rb._session.calls[-1][0] == "delete"
    assert rb._session.calls[-1][1].endswith("/v1/cache/persisted/sess-1")


# ---------- 编排语义（Fake 后端） ----------

class _FakeTok:
    special_tokens_map = {}


class _FakeProcessor:
    tokenizer = _FakeTok()


class FakeBackend:
    processor = _FakeProcessor()

    def __init__(self):
        self.persist_calls = []
        self.released = []
        self.restore_result = None

    def new_cache(self):
        return SimpleNamespace(state=SimpleNamespace(cache=None, token_ids=None))

    def clone_cache(self, src):
        return SimpleNamespace(
            state=SimpleNamespace(cache=["base-clone"], token_ids=None))

    def release_cache(self, h):
        self.released.append(h)

    def persist_cache(self, h, dest):
        self.persist_calls.append(dest)
        return True

    def restore_cache(self, src):
        return self.restore_result

    def stream_generate(self, prompt, *, cache, **kw):
        cache.state.cache = ["warmed"]
        yield SimpleNamespace(text="x")


def test_freeze_persists_before_release(tmp_path, kv_on):
    be = FakeBackend()
    s = ChatSession("t2s1", tmp_path, backend=be)
    s.freeze()
    assert be.persist_calls == [str(s.kv_persist_path)]
    assert str(s.kv_persist_path).startswith(str(s.session_dir))
    assert s.is_cold


def test_prepare_cache_prefers_restore_over_base(kv_on):
    be = FakeBackend()
    eng = AgentEngine(be)
    asyncio.run(eng.prewarm_base_cache("sys", []))
    hot = SimpleNamespace(state=SimpleNamespace(cache=["restored"], token_ids=[1]))
    be.restore_result = hot
    session = SimpleNamespace(session_id="t2s2",
                              brain_memory_state=be.new_cache(),
                              inject_env_context=True,
                              kv_persist_path=Path("/nonexistent/kv.safetensors"))
    eng._prepare_session_cache(session)
    assert session.brain_memory_state is hot          # 热恢复优先于基座克隆


def test_prepare_cache_falls_back_to_base_then_cold(kv_on):
    be = FakeBackend()
    eng = AgentEngine(be)
    asyncio.run(eng.prewarm_base_cache("sys", []))
    be.restore_result = None                           # 无落盘产物 → 退基座
    session = SimpleNamespace(session_id="t2s3",
                              brain_memory_state=be.new_cache(),
                              inject_env_context=True,
                              kv_persist_path=Path("/nonexistent/kv.safetensors"))
    eng._prepare_session_cache(session)
    assert session.brain_memory_state.state.cache == ["base-clone"]


# ---------- D 方案缺省关闭（隐私守卫：KV 实质是会话内容，缺省不许离机） ----------

def test_default_off_freeze_never_persists(tmp_path, kv_off):
    """门关时冻结绝不调 persist——拆分拓扑下这就是「会话残片流向算力机盘」的那一步。
    （「缺省即关」由 test_kv_retention 的 test_kv_gate_defaults 单独钉。）"""
    be = FakeBackend()
    s = ChatSession("t2s-off", tmp_path, backend=be)
    s.freeze()
    assert be.persist_calls == [], "默认关闭时冻结不得落盘"
    assert s.is_cold


def test_default_off_prepare_never_touches_disk(kv_off):
    """门关时解冻不问盘（restore 不被调用），直接退基座克隆。"""
    be = FakeBackend()
    eng = AgentEngine(be)
    asyncio.run(eng.prewarm_base_cache("sys", []))
    hot = SimpleNamespace(state=SimpleNamespace(cache=["restored"], token_ids=[1]))
    be.restore_result = hot        # 盘上「有」产物——但默认关闭连问都不该问
    calls = []
    orig = be.restore_cache
    be.restore_cache = lambda p: (calls.append(p), orig(p))[1]
    session = SimpleNamespace(session_id="t2s-off2",
                              brain_memory_state=be.new_cache(),
                              inject_env_context=True,
                              kv_persist_path=Path("/nonexistent/kv.safetensors"))
    eng._prepare_session_cache(session)
    assert calls == [], "默认关闭时不得尝试热恢复"
    assert session.brain_memory_state.state.cache == ["base-clone"]


def test_drop_persisted_is_not_gated_source_anchor():
    """源锚：SessionManager 删会话时的 drop_persisted 清理**不受开关辖制**——
    关掉开关后删会话，仍要把算力端旧残片带走。按**整个调用函数**扫（首评 S：
    只看前 600 字符的窗，门写在调用点下一行就漏了）。"""
    import ast
    import inspect
    import framework.memory.session as m
    tree = ast.parse(inspect.getsource(m))
    hits = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_src = ast.dump(node)
            if "drop_persisted" in body_src:
                hits += 1
                assert "kv_persist_enabled" not in body_src, \
                    f"{node.name} 里 drop_persisted 与开关判定同函数——清理必须无条件"
    assert hits >= 1, "找不到 drop_persisted 调用函数"
