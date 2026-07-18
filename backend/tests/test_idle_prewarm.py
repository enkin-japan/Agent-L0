"""T1 压缩后空闲预热金测试。

钉住的行为：
1. 压缩成功折叠后 session.context_dirty 置位（预热信号的唯一来源）；
2. prewarm_session_cache 用会话当前上下文跑 1-token 生成、成功后清脏标志；
3. 拿锁后复检：会话已冻结/缓存句柄被换（等待期间有用户轮进来）→ 放弃不生成；
4. 无缓存句柄（FakeEngine 环境）→ 直接 False，不炸。
"""
import asyncio
import json
from types import SimpleNamespace

from framework.core.engine import AgentEngine
from framework.memory.compactor import maybe_summarize_context


class _FakeTok:
    special_tokens_map = {}


class _FakeProcessor:
    tokenizer = _FakeTok()


class FakeBackend:
    processor = _FakeProcessor()

    def __init__(self):
        self.gen_prompts = []
        self.gen_sampling = []

    def new_cache(self):
        return SimpleNamespace(state=SimpleNamespace(cache=None, token_ids=None))

    def release_cache(self, h):
        pass

    def stream_generate(self, prompt, *, cache, sampling=None, **kw):
        self.gen_prompts.append(prompt)
        self.gen_sampling.append(sampling or {})
        cache.state.cache = ["warmed"]
        yield SimpleNamespace(text="x")


class _SummEngine:
    """只为 compactor 服务的替身：summarize 返回固定摘要。"""

    async def summarize(self, batch_text, existing, cancel_check=None):
        return "- 折叠摘要"


def _write_turns(session, n):
    for i in range(n):
        session.append_log({"user_input": f"问题{i}",
                            "response": [{"role": "assistant", "content": f"回答{i}"}],
                            "status": "success", "timestamp": 1752800000 + i})


def test_compaction_sets_context_dirty(make_session, monkeypatch):
    import framework.memory.rag_sink as sink
    monkeypatch.setattr(sink, "sediment", lambda *a, **k: False)
    session = make_session("t1-dirty")
    _write_turns(session, 6)
    session.last_prompt_tokens = 10_000
    assert session.context_dirty is False

    n = asyncio.run(maybe_summarize_context(
        session, _SummEngine(), threshold_tokens=1,
        keep_tail_turns=2, last_prompt_tokens=10_000, sediment=False))
    assert n > 0
    assert session.context_dirty is True


def test_prewarm_generates_and_clears_dirty():
    be = FakeBackend()
    eng = AgentEngine(be)
    session = SimpleNamespace(
        session_id="t1-s1", is_cold=False, context_dirty=True,
        inject_env_context=True,
        history=[{"role": "system", "content": "sys"},
                 {"role": "user", "content": "折叠后的摘要上下文"}],
        brain_memory_state=be.new_cache())

    assert asyncio.run(eng.prewarm_session_cache(session, tools=[])) is True
    assert session.context_dirty is False
    assert len(be.gen_prompts) == 1
    assert "折叠后的摘要上下文" in be.gen_prompts[0]
    assert be.gen_sampling[0]["max_tokens"] == 1


def test_prewarm_aborts_if_frozen_after_lock():
    be = FakeBackend()
    eng = AgentEngine(be)
    session = SimpleNamespace(
        session_id="t1-s2", is_cold=True, context_dirty=True,
        inject_env_context=True,
        history=[{"role": "system", "content": "sys"}],
        brain_memory_state=be.new_cache())
    assert asyncio.run(eng.prewarm_session_cache(session, tools=[])) is False
    assert session.context_dirty is True                 # 脏标志保留，下次再试
    assert be.gen_prompts == []


def test_prewarm_without_cache_is_noop():
    be = FakeBackend()
    eng = AgentEngine(be)
    session = SimpleNamespace(session_id="t1-s3", is_cold=False,
                              context_dirty=True, inject_env_context=True,
                              history=[], brain_memory_state=None)
    assert asyncio.run(eng.prewarm_session_cache(session, tools=[])) is False
