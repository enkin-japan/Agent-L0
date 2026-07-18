"""T3 基座前缀缓存金测试（全离线，FakeBackend）。

钉住的行为：
1. prewarm 幂等：同款 system+tools 只预填一次（空闲回合反复调是纯哈希比较）；
2. 工具集变化 → key 变 → 自动重建并释放旧基座；
3. 处女缓存判定：MLX 形（state.cache）与远程形（懒注册 cache_id）都认，
   未知形状一律按非处女（绝不动别人的缓存）；
4. 采用：处女缓存换成克隆、旧句柄释放；非处女不动；克隆失败静默冷启动。
"""
import asyncio
from types import SimpleNamespace

from framework.core.engine import AgentEngine


class _FakeTok:
    special_tokens_map = {}


class _FakeProcessor:
    tokenizer = _FakeTok()


class FakeBackend:
    processor = _FakeProcessor()

    def __init__(self, clone_ok=True):
        self.gen_calls = 0
        self.released = []
        self.clone_ok = clone_ok

    def new_cache(self):
        return SimpleNamespace(state=SimpleNamespace(cache=None, token_ids=None))

    def clone_cache(self, src):
        if not self.clone_ok:
            return None
        return SimpleNamespace(
            state=SimpleNamespace(cache=list(src.state.cache or []), token_ids=None),
            cloned_from=src)

    def release_cache(self, h):
        self.released.append(h)

    def stream_generate(self, prompt, *, cache, **kw):
        self.gen_calls += 1
        cache.state.cache = ["warmed"]
        yield SimpleNamespace(text="x")


def _engine(backend=None):
    return AgentEngine(backend or FakeBackend())


TOOLS_A = [{"name": "t1", "description": "d", "parameters": {"properties": {}}}]
TOOLS_B = TOOLS_A + [{"name": "t2", "description": "d2", "parameters": {"properties": {}}}]


def test_prewarm_idempotent_and_rebuilds_on_change():
    be = FakeBackend()
    eng = _engine(be)

    async def _run():
        assert await eng.prewarm_base_cache("sys", TOOLS_A) is True
        assert await eng.prewarm_base_cache("sys", TOOLS_A) is False   # 幂等 no-op
        assert be.gen_calls == 1
        old = eng._base_cache
        assert await eng.prewarm_base_cache("sys", TOOLS_B) is True    # 工具集变 → 重建
        assert be.gen_calls == 2
        assert old in be.released                                       # 旧基座已释放

    asyncio.run(_run())


def test_virgin_detection_shapes():
    v = AgentEngine._cache_is_virgin
    assert v(SimpleNamespace(state=SimpleNamespace(cache=None))) is True     # MLX 新建
    assert v(SimpleNamespace(state=SimpleNamespace(cache=["k"]))) is False   # MLX 已预填
    assert v(SimpleNamespace(cache_id=None)) is True                         # 远程懒注册
    assert v(SimpleNamespace(cache_id="abc")) is False                       # 远程已注册
    assert v(SimpleNamespace()) is False                                     # 未知形状不动


def test_adopt_replaces_virgin_and_releases_old():
    be = FakeBackend()
    eng = _engine(be)
    asyncio.run(eng.prewarm_base_cache("sys", TOOLS_A))
    virgin = be.new_cache()
    session = SimpleNamespace(session_id="s1", brain_memory_state=virgin,
                              inject_env_context=True)
    eng._adopt_base_cache(session)
    assert session.brain_memory_state is not virgin          # 已换成克隆
    assert session.brain_memory_state.cloned_from is eng._base_cache
    assert virgin in be.released


def test_adopt_leaves_warm_cache_alone():
    be = FakeBackend()
    eng = _engine(be)
    asyncio.run(eng.prewarm_base_cache("sys", TOOLS_A))
    warm = be.new_cache()
    warm.state.cache = ["history"]
    session = SimpleNamespace(session_id="s2", brain_memory_state=warm,
                              inject_env_context=True)
    eng._adopt_base_cache(session)
    assert session.brain_memory_state is warm                # 非处女：一个字不动


def test_adopt_clone_failure_falls_back_cold():
    be = FakeBackend(clone_ok=False)
    eng = _engine(be)
    asyncio.run(eng.prewarm_base_cache("sys", TOOLS_A))
    virgin = be.new_cache()
    session = SimpleNamespace(session_id="s3", brain_memory_state=virgin,
                              inject_env_context=True)
    eng._adopt_base_cache(session)
    assert session.brain_memory_state is virgin              # 冷启动兜底
    assert virgin not in be.released


def test_adopt_noop_without_base():
    be = FakeBackend()
    eng = _engine(be)                                        # 未 prewarm
    virgin = be.new_cache()
    session = SimpleNamespace(session_id="s4", brain_memory_state=virgin,
                              inject_env_context=True)
    eng._adopt_base_cache(session)
    assert session.brain_memory_state is virgin
