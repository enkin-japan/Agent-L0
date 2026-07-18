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

import framework.backends.mlx.backend as mlx_backend_mod
from framework.backends.mlx.backend import MLXBackend, MLXCacheHandle
from framework.core.engine import AgentEngine
from framework.memory.session import ChatSession


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


def test_freeze_persists_before_release(tmp_path):
    be = FakeBackend()
    s = ChatSession("t2s1", tmp_path, backend=be)
    s.freeze()
    assert be.persist_calls == [str(s.kv_persist_path)]
    assert str(s.kv_persist_path).startswith(str(s.session_dir))
    assert s.is_cold


def test_prepare_cache_prefers_restore_over_base():
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


def test_prepare_cache_falls_back_to_base_then_cold():
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
