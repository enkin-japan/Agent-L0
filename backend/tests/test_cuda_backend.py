"""CUDA 后端金测试（Colab 联调波·本地可测面）。

守的是什么（vllm/torch 不装在开发 Mac——测协议形状与纯逻辑，不测引擎）：
① **导入安全**——backends.cuda 包在无 CUDA 环境可 import（vllm/torch 全惰性），
  get_backend_class("cuda") 能取到类；错名报错文案含 cuda。
② **协议形状**——CudaVLLMBackend 具备 InferenceBackend 全部方法；persist/restore
  恒 False/None（server 冷路兜底语义）；缓存句柄记账语义（clone 独立、release 清账）。
③ **约束适配器纯逻辑**——_TorchGuardAdapter 的 token 簿记（增量消费、无 prompt
  边界——vLLM 二参约定只喂已生成序列）与 fail-open：allow=None 路径零 torch 依赖，
  guard 异常绝不外抛。
④ **配置面**——config.yaml inference.cuda 旋钮在位；CUDA 档 4 模型已注册（表外
  拒启的护栏靠注册表）且无 profile（未画像绝不被路由优先选中）。
⑤ **server 环境覆盖**——INFER_BACKEND 的 env 覆盖锚点在位（Colab 起 cuda 不改
  config 的通道）。
⑥ **两段式思考 ids↔text 对齐**——段一 stop 字符串只截 text，投机解码越过停符的
  token 必须从段二 prompt 与 cache 记账里截掉（否则调用头进了上下文却不进正文）。
"""
from pathlib import Path
import asyncio
import queue
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

from framework.backends import get_backend_class
from framework.backends.cuda.backend import (CudaCacheHandle, CudaVLLMBackend,
                                             _TorchGuardAdapter)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---- ① 导入安全 / 注册面 ----

def test_registry_dispatch_and_error_message():
    assert get_backend_class("cuda") is CudaVLLMBackend
    with pytest.raises(ValueError, match="cuda"):
        get_backend_class("rocm")


# ---- ② 协议形状 ----

def test_protocol_surface_present():
    for m in ("load", "new_cache", "clone_cache", "release_cache", "shutdown",
              "stream_generate", "persist_cache", "restore_cache"):
        assert callable(getattr(CudaVLLMBackend, m)), f"缺协议方法 {m}"


def test_cache_handle_bookkeeping_semantics():
    b = CudaVLLMBackend({})
    h = b.new_cache()
    assert h.token_ids is None
    h.token_ids = [1, 2, 3]
    c = b.clone_cache(h)
    assert c.token_ids == [1, 2, 3]
    c.token_ids.append(4)
    assert h.token_ids == [1, 2, 3], "clone 必须记账独立（不共享 list）"
    b.release_cache(h)
    assert h.token_ids is None
    assert b.persist_cache(h, "/tmp/x") is False      # 冷路兜底语义
    assert b.restore_cache("/tmp/x") is None


def test_unloaded_backend_refuses_generate():
    b = CudaVLLMBackend({})
    with pytest.raises(RuntimeError, match="未装配"):
        next(b.stream_generate("hi", cache=b.new_cache()))


# ---- ③ 约束适配器纯逻辑 ----

class _FakeGuard:
    def __init__(self, allow=None):
        self.failed = False
        self.active = True
        self.consumed = []
        self._allow = allow

    def consume(self, t):
        self.consumed.append(t)

    def next_allowed(self):
        return self._allow

    def fail_open(self, e):
        self.failed = True


def test_guard_adapter_incremental_consume_no_prompt_boundary():
    g = _FakeGuard(allow=None)
    ad = _TorchGuardAdapter(g)
    logits = object()                       # allow=None 路径不触碰 torch
    assert ad([10, 11], logits) is logits
    assert g.consumed == [10, 11]           # vLLM 只喂已生成序列：首调即消费
    ad([10, 11, 12], logits)
    assert g.consumed == [10, 11, 12]       # 增量消费，不重复


def test_guard_adapter_fail_open_never_raises():
    class Boom(_FakeGuard):
        def next_allowed(self):
            raise RuntimeError("grammar exploded")
    g = Boom()
    ad = _TorchGuardAdapter(g)
    logits = object()
    assert ad([1], logits) is logits        # 异常吞成 fail-open
    assert g.failed is True
    assert ad([1, 2], logits) is logits     # failed 后恒直通


# ---- ④ 配置面 ----

def test_config_cuda_knobs_and_registry_entries():
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    inf = cfg["inference"]
    cuda = inf.get("cuda") or {}
    # D25（2026-10-31）：CUDA 条目一律预量化，无在线量化缺省；backend 把 None/"" 映射为
    # vLLM quantization=None（INFER_QUANT 未设时同样落到这里）
    assert cuda.get("quantization") is None
    assert cuda.get("enable_prefix_caching") is True
    reg = {e["repo"]: e for e in inf["model_registry"]}
    # D27 阵容收束（2026-10-31）：CUDA 只剩两件预量化 checkpoint（compute_ctl registered_models
    # 的全部来源）；均无 profile 属预期（未画像件不入路由优先选中），单独钉在册+方言+revision。
    cuda_entries = {r: e for r, e in reg.items() if "mlx" not in r.lower()}
    assert sorted(cuda_entries) == ["RedHatAI/Qwen3.8-27B-INT4", "google/gemma-4-31B-it-qat-w4a16-ct"]
    for repo, dialect in (("RedHatAI/Qwen3.8-27B-INT4", "qwen35"),
                          ("google/gemma-4-31B-it-qat-w4a16-ct", "gemma4")):
        e = cuda_entries[repo]
        assert e["dialect"] == dialect
        assert e["cuda_deployment"]["prequantized"] is True, f"{repo} 未声明预量化（D25）"
        assert e.get("revision"), f"{repo} revision 未钉死（常驻纪律）"
        assert "profile" not in e, f"{repo} 未评测却带画像"


# ---- ⑤ server 环境覆盖锚点 ----

def test_server_supports_infer_backend_env_override():
    src = (REPO_ROOT / "inference" / "server.py").read_text(encoding="utf-8")
    assert 'os.environ.get("INFER_BACKEND"' in src


# ---- ⑥ structural-tag 迁移（2026-08-07：vLLM 0.26 拒收 per-request logits_processors）----

def test_structural_tag_json_shape():
    """声明式约束 → vLLM structural_tag 规格：begin/schema/end + triggers 锚形状。"""
    import json
    from framework.backends.cuda.backend import structural_tag_json
    spec = {"begin": "<|tool_call>", "end": "<tool_call|>", "kind": "json_schema",
            "body": {"type": "object", "properties": {"name": {"type": "string"}}}}
    tag = json.loads(structural_tag_json(spec))
    assert tag["triggers"] == ["<|tool_call>"]
    assert tag["structures"][0]["begin"] == "<|tool_call>"
    assert tag["structures"][0]["end"] == "<tool_call|>"
    assert tag["structures"][0]["schema"]["type"] == "object"


def test_structural_tag_refuses_non_json_schema_grammar():
    """§7-8：structural-tag 的 schema 字段只能表达 JSON schema。

    gemma-4 迁到紧凑 DSL 后语法是 Lark，硬塞进去等于把模型约束成 JSON——那与提示词
    里的声明格式**正好相反**，比不约束更坏。必须抛，由调用方落到 logits processor
    后备路（llguidance 驱动方言的正确语法）。
    """
    import pytest
    from framework.backends.cuda.backend import structural_tag_json
    spec = {"begin": "<|tool_call>", "end": "<tool_call|>", "kind": "lark",
            "body": 'start: "call:x{}"'}
    with pytest.raises(ValueError, match="json_schema"):
        structural_tag_json(spec)


class _FakeTok:
    """encode 返回单 token id 的极简分词器替身（标记→固定 id）。"""
    _map = {"<|tool_call>": 7, "<tool_call|>": 9}

    def encode(self, s, add_special_tokens=False):
        return [self._map[s]] if s in self._map else [1, 2]


def test_count_constrained_tokens_instrument():
    from framework.backends.cuda.backend import _count_constrained_tokens
    spec = {"begin": "<|tool_call>", "end": "<tool_call|>", "schema": {}}
    # 自由 2 tok → stc → 段内 3 tok → etc（算1步）→ 自由 1 tok = 4 约束步
    ids = [11, 12, 7, 21, 22, 23, 9, 13]
    assert _count_constrained_tokens(ids, _FakeTok(), spec) == 4
    # 无调用段 → 0；标记非单 token（schema begin 未注册）→ 0 不猜测
    assert _count_constrained_tokens([11, 12], _FakeTok(), spec) == 0
    bad = {"begin": "nope", "end": "<tool_call|>", "schema": {}}
    assert _count_constrained_tokens(ids, _FakeTok(), bad) == 0


# ---- ⑦ CPU 假引擎：同步消费桥的背压、结束与取消 ----

class _FakeSamplingParams:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _completion(text, ids=None, finish=None):
    return SimpleNamespace(outputs=[SimpleNamespace(
        text=text, token_ids=ids or list(range(len(text))), finish_reason=finish)],
        num_cached_tokens=0)


@pytest.fixture
def cuda_stream(monkeypatch):
    """仅启动假引擎的私有循环；失败也排空队列，避免旧死锁遗留线程。"""
    from framework.backends.cuda import backend as mod
    queues = []
    blocked = threading.Event()
    original_queue = queue.Queue

    class ObservedQueue(original_queue):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            queues.append(self)

        def put(self, item, *args, **kwargs):
            if self.full():
                blocked.set()
            return super().put(item, *args, **kwargs)

    monkeypatch.setattr(mod._queue, "Queue", ObservedQueue)
    monkeypatch.setitem(sys.modules, "vllm", SimpleNamespace(
        SamplingParams=_FakeSamplingParams))
    b = CudaVLLMBackend()
    b._tokenizer = SimpleNamespace(encode=lambda text, **kw: [100, 101])
    b._peak_gb = lambda: 0.0  # 不导入 torch，不探测 GPU
    b._loop = asyncio.new_event_loop()
    b._thread = threading.Thread(target=b._loop.run_forever, daemon=True)
    b._thread.start()
    streams = []

    def start(engine, **kwargs):
        b._engine = engine
        stream = b.stream_generate("synthetic", cache=b.new_cache(), **kwargs)
        streams.append(stream)
        return stream

    yield SimpleNamespace(backend=b, start=start, queues=queues, blocked=blocked)
    for stream in streams:
        stream.close()
    # The original regression blocks the loop in Queue.put: release it before
    # asking the loop to cancel pending fake tasks and stop.
    deadline = time.monotonic() + 2
    while b._thread.is_alive() and time.monotonic() < deadline:
        for q in queues:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
        if not b._rid_map:
            break
        time.sleep(0.001)
    if b._thread.is_alive():
        async def cleanup():
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(cleanup(), b._loop).result(timeout=2)
        b._loop.call_soon_threadsafe(b._loop.stop)
    b._thread.join(timeout=2)
    assert not b._thread.is_alive(), "fake CUDA loop cleanup failed"
    for q in queues:
        while not q.empty():
            q.get_nowait()
    b._loop.close()


class _BurstEngine:
    def __init__(self, count=300, error=False):
        self.count = count
        self.error = error
        self.aborted = threading.Event()
        self.stopped = threading.Event()
        self.produced = 0
        self.requests = []

    async def generate(self, prompt, params, rid):
        self.requests.append(rid)
        for i in range(1, self.count + 1):
            self.produced += 1
            yield _completion("x" * i)
            await asyncio.sleep(0)
        if self.error:
            raise ValueError("synthetic engine failure")

    async def abort(self, rid):
        self.abort_rid = rid
        self.aborted.set()

    def shutdown(self):
        self.stopped.set()


@pytest.mark.parametrize("count,error", [(300, False), (257, False), (257, True)])
def test_cuda_full_queue_close_releases_loop_abort_and_next_request(cuda_stream, count, error):
    """正文、结束哨兵和异常投递满队列都必须能取消；队列保持256上界。"""
    engine = _BurstEngine(count, error)
    stream = cuda_stream.start(engine)
    assert next(stream).text == "x"
    assert cuda_stream.blocked.wait(2), "producer never reached full queue"
    q = cuda_stream.queues[0]
    assert q.maxsize == q.qsize() == 256
    assert engine.produced <= 258  # 一条已消费 + 256已排队 + 一条待投递
    heartbeat = threading.Event()
    cuda_stream.backend._loop.call_soon_threadsafe(heartbeat.set)
    assert heartbeat.wait(0.3), "full queue starved private event loop"
    stream.close()
    assert engine.aborted.wait(1), "consumer close did not abort active request"
    assert engine.abort_rid == engine.requests[-1]
    next_engine = _BurstEngine(2)
    next_stream = cuda_stream.start(next_engine)
    assert [chunk.text for chunk in next_stream] == ["x", "x"]
    assert not next_engine.aborted.is_set()


def test_cuda_shutdown_releases_full_producer(cuda_stream):
    engine = _BurstEngine()
    stream = cuda_stream.start(engine)
    next(stream)
    assert cuda_stream.blocked.wait(2)
    cuda_stream.backend.shutdown()
    assert engine.stopped.wait(1), "shutdown queued behind blocking producer"
    assert engine.aborted.wait(1), "shutdown did not abort in-flight request"
    cuda_stream.backend._thread.join(timeout=1)
    assert not cuda_stream.backend._thread.is_alive()


def test_cuda_shutdown_wakes_consumer_waiting_for_first_chunk(cuda_stream):
    class WaitingEngine(_BurstEngine):
        async def generate(self, prompt, params, rid):
            self.requests.append(rid)
            self.started.set()
            await asyncio.Event().wait()
            yield _completion("unreachable")

        async def abort(self, rid):
            await asyncio.sleep(0.02)  # consumer wake/重复shutdown不应再取消abort
            await super().abort(rid)

    engine = WaitingEngine()
    engine.started = threading.Event()
    stream = cuda_stream.start(engine)
    outcome = []
    done = threading.Event()

    def consume():
        try:
            next(stream)
        except RuntimeError as error:
            outcome.append(str(error))
        finally:
            done.set()

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    assert engine.started.wait(1)
    cuda_stream.backend.shutdown()
    cuda_stream.backend.shutdown()
    assert done.wait(1), "shutdown left consumer in 600s Queue.get"
    consumer.join(timeout=1)
    assert outcome and "shutdown" in outcome[0]
    assert engine.aborted.wait(1)
    assert engine.stopped.wait(1)


@pytest.mark.parametrize("count", [0, 1, 255, 256, 257, 300])
def test_cuda_stream_capacity_boundaries_complete_in_order(cuda_stream, count):
    engine = _BurstEngine(count)
    chunks = list(cuda_stream.start(engine))
    assert [c.text for c in chunks] == ["x"] * count
    assert [c.generation_tokens for c in chunks] == list(range(1, count + 1))
    assert not engine.aborted.is_set()
    assert not cuda_stream.backend._rid_map


def test_cuda_close_while_forced_thinking_end_waits_for_space(cuda_stream):
    class ThinkingBurst(_BurstEngine):
        async def generate(self, prompt, params, rid):
            self.requests.append(rid)
            for i in range(1, 258):
                yield _completion("x" * i, list(range(i)),
                                  "length" if i == 257 else None)
                await asyncio.sleep(0)

    engine = ThinkingBurst()
    stream = cuda_stream.start(engine, sampling={"max_tokens": 1000},
        features={"enable_thinking": True, "thinking_budget": 257,
                  "thinking_end_token": "</think>"})
    next(stream)
    assert cuda_stream.blocked.wait(2)
    stream.close()
    assert engine.aborted.wait(1)
    assert len(engine.requests) == 1, "cancelled thinking phase started phase two"


def test_cuda_stream_order_exception_and_cache(cuda_stream):
    engine = _BurstEngine(3, error=True)
    stream = cuda_stream.start(engine, features={"want_delta": True})
    chunks = [next(stream) for _ in range(3)]
    assert [c.text for c in chunks] == ["x"] * 3
    assert [c.delta for c in chunks] == ["x"] * 3
    assert [c.generation_tokens for c in chunks] == [1, 2, 3]
    assert all(c.extra == {"reused_prefix": 0} for c in chunks)
    with pytest.raises(ValueError, match="synthetic engine failure"):
        next(stream)


def test_cuda_two_phase_forced_close_order_and_cache(cuda_stream):
    class TwoPhase(_BurstEngine):
        async def generate(self, prompt, params, rid):
            self.requests.append((prompt, params.kwargs, rid))
            if rid.endswith("-p2"):
                yield _completion("answer", [20, 21], "stop")
            else:
                yield _completion("thought", [10, 11], "length")

    engine = TwoPhase()
    b = cuda_stream.backend
    b._engine = engine
    cache = b.new_cache()
    chunks = list(b.stream_generate("synthetic", cache=cache,
        sampling={"max_tokens": 8}, features={"want_delta": True,
            "enable_thinking": True, "thinking_budget": 2,
            "thinking_end_token": "</think>"}))
    assert [c.text for c in chunks] == ["thought", "\n</think>", "answer"]
    assert [c.generation_tokens for c in chunks] == [2, 4, 6]
    assert chunks[1].extra == {"thinking_budget_forced_close": True}
    assert engine.requests[0][1]["stop"] == ["</think>"]
    assert engine.requests[1][1]["max_tokens"] == 4
    assert engine.requests[1][0]["prompt_token_ids"] == [100, 101, 10, 11, 100, 101]
    assert cache.token_ids == [100, 101, 10, 11, 100, 101, 20, 21]


class _OvershootEngine(_BurstEngine):
    """段一 text 已被 stop 截在 </think>；ids 多出「\\n\\n」=50、「<tool_call>」=51。"""
    def __init__(self, phase1_ids=(10, 11, 99, 50, 51)):
        super().__init__()
        self.phase1_ids = list(phase1_ids)

    async def generate(self, prompt, params, rid):
        self.requests.append((prompt, params.kwargs, rid))
        if rid.endswith("-p2"):
            yield _completion("<tool_call>…</tool_call>", [51, 60, 61], "stop")
        else:
            yield _completion("thought</think>", self.phase1_ids, "stop")


def _run_overshoot(b, engine, encode):
    b._engine = engine
    b._tokenizer = SimpleNamespace(encode=encode)
    cache = b.new_cache()
    chunks = list(b.stream_generate("synthetic", cache=cache,
        sampling={"max_tokens": 8}, features={"want_delta": True,
            "enable_thinking": True, "thinking_budget": 4,
            "thinking_end_token": "</think>"}))
    return chunks, cache


def _encode_close_99(text, **kw):
    return [99] if text == "</think>" else [100, 101]


def test_cuda_two_phase_trims_speculative_overshoot_past_think_close(cuda_stream, capsys):
    """MTP 一步接受多 token：停符后的 ids 不得混进段二 prompt（2026-10-24 事故）。"""
    engine = _OvershootEngine()
    chunks, cache = _run_overshoot(cuda_stream.backend, engine, _encode_close_99)
    assert [c.text for c in chunks] == ["thought</think>", "<tool_call>…</tool_call>"]
    assert engine.requests[1][0]["prompt_token_ids"] == [100, 101, 10, 11, 99]
    assert engine.requests[1][1]["max_tokens"] == 5
    # 口径＝保留的 token：段一 chunk 按引擎原值（5），段二起回落到对齐值 3+3
    assert [c.generation_tokens for c in chunks] == [5, 6]
    assert cache.token_ids == [100, 101, 10, 11, 99, 51, 60, 61]
    # 越界告警只打一次（MTP 下每轮都会发生，不得刷屏）
    _run_overshoot(cuda_stream.backend, _OvershootEngine(), _encode_close_99)
    assert capsys.readouterr().out.count("投机解码越界") == 1


def test_cuda_two_phase_unmatched_close_ids_left_untouched_and_reported(cuda_stream, capsys):
    """分词口径不一致（闭符 ids 在段一里找不到）：不猜测截断，但必须响亮告警。"""
    engine = _OvershootEngine()
    chunks, cache = _run_overshoot(cuda_stream.backend, engine,
                                   lambda text, **kw: [98])
    assert engine.requests[1][0]["prompt_token_ids"] == [98, 10, 11, 99, 50, 51]
    assert "分词口径不一致" in capsys.readouterr().out


def test_trim_after_stop_boundaries():
    from framework.backends.cuda.backend import _trim_after_stop
    assert _trim_after_stop([1, 7, 8, 2, 3], [7, 8]) == [1, 7, 8], "多 token 停符"
    assert _trim_after_stop([1, 7, 2, 7, 3], [7]) == [1, 7], "取首个停符"
    assert _trim_after_stop([1, 2, 7], [7]) == [1, 2, 7], "无越界则不动"
    assert _trim_after_stop([1, 2, 3], [7]) == [1, 2, 3], "找不到停符不猜测"
    assert _trim_after_stop([1], [1, 2]) == [1], "停符比 ids 长"
    assert _trim_after_stop([1, 2], []) == [1, 2]
    src = [1, 2]
    assert _trim_after_stop(src, [7]) is not src, "不与入参别名"


def test_cuda_two_phase_close_aborts_second_request(cuda_stream):
    class TwoPhaseBurst(_BurstEngine):
        async def generate(self, prompt, params, rid):
            if not rid.endswith("-p2"):
                self.requests.append(rid)
                yield _completion("</think>", [10], "stop")
            else:
                async for chunk in super().generate(prompt, params, rid):
                    yield chunk

    engine = TwoPhaseBurst()
    stream = cuda_stream.start(engine, sampling={"max_tokens": 1000},
        features={"enable_thinking": True, "thinking_budget": 2,
                  "thinking_end_token": "</think>"})
    assert next(stream).text == "</think>"
    assert cuda_stream.blocked.wait(2)
    stream.close()
    assert engine.aborted.wait(1)
    assert engine.abort_rid == engine.requests[-1]
    assert engine.abort_rid.endswith("-p2")


def test_cuda_running_producer_waits_for_abort_despite_queued_cancel(cuda_stream, monkeypatch):
    from framework.backends.cuda.backend import _StreamBridge
    entered, release, abort_started = threading.Event(), threading.Event(), threading.Event()
    finish_abort = asyncio.Event()

    class Engine(_BurstEngine):
        async def abort(self, rid):
            abort_started.set()
            await finish_abort.wait()
            await super().abort(rid)

    engine = Engine()
    original = _StreamBridge.put
    calls = 0

    async def put(bridge, item):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(2)
        return await original(bridge, item)

    monkeypatch.setattr(_StreamBridge, "put", put)
    stream = cuda_stream.start(engine)
    try:
        next(stream)
        assert entered.wait(1)
        stream.close()
        release.set()
        assert abort_started.wait(1)
        # Drain the already queued cancel callback before inspecting ownership.
        async def heartbeat():
            await asyncio.sleep(0)
        asyncio.run_coroutine_threadsafe(heartbeat(), cuda_stream.backend._loop).result(timeout=1)
        assert cuda_stream.backend._producers, "cleanup lost ownership before abort finished"
        assert cuda_stream.backend._rid_map
        cuda_stream.backend._loop.call_soon_threadsafe(finish_abort.set)
        assert engine.aborted.wait(1)
    finally:
        release.set()
        cuda_stream.backend._loop.call_soon_threadsafe(finish_abort.set)


def test_cuda_close_does_not_interrupt_abort_cleanup(cuda_stream, monkeypatch):
    from framework.backends.cuda.backend import _StreamBridge
    started = threading.Event()
    release = asyncio.Event()

    class Engine(_BurstEngine):
        async def abort(self, rid):
            started.set()
            await release.wait()
            await super().abort(rid)

    engine = Engine()
    stream = cuda_stream.start(engine)
    next(stream)
    assert cuda_stream.blocked.wait(2)
    original_close = _StreamBridge.close

    def close(self):
        original_close(self)
        assert started.wait(1), "producer did not enter abort cleanup"

    monkeypatch.setattr(_StreamBridge, "close", close)
    stream.close()
    b = cuda_stream.backend

    async def tracked():
        return bool(b._producers), bool(b._rid_map)

    try:
        assert asyncio.run_coroutine_threadsafe(tracked(), b._loop).result(1) == (True, True)
        b.shutdown()
        assert not engine.stopped.wait(0.05), "shutdown skipped unfinished abort"
    finally:
        b._loop.call_soon_threadsafe(release.set)
    assert engine.aborted.wait(1), "abort cancelled midway through consumer close"
    assert engine.stopped.wait(1)


def test_cuda_shutdown_during_stream_admission_refuses_stopped_loop(cuda_stream, monkeypatch):
    from framework.backends.cuda import backend as mod
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    bridges = []
    original_init = mod._StreamBridge.__init__

    def paused_init(self, loop):
        original_init(self, loop)
        bridges.append(self)
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(mod._StreamBridge, "__init__", paused_init)
    engine = _BurstEngine(1)
    stream = cuda_stream.start(engine)
    outcome = []

    def consume():
        try:
            outcome.append(next(stream))
        except RuntimeError as error:
            outcome.append(str(error))
        finally:
            done.set()

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    assert entered.wait(1)
    b = cuda_stream.backend
    try:
        b.shutdown()
        assert engine.stopped.wait(1)
        b._thread.join(1)
        assert not b._thread.is_alive()
        release.set()
        assert done.wait(0.2), "stream submitted after shutdown and waited 600s"
        assert len(outcome) == 1 and isinstance(outcome[0], str)
        assert "shutdown" in outcome[0]
        assert not engine.requests
    finally:
        release.set()
        for bridge in bridges:
            bridge.close()  # 唤醒失败实现中已进入get的消费者，不遗留线程
        consumer.join(1)


def test_cuda_admitted_stream_submits_before_shutdown_can_stop_loop(cuda_stream, monkeypatch):
    from framework.backends.cuda import backend as mod
    entered = threading.Event()
    release = threading.Event()
    consumed = threading.Event()
    shutdown_returned = threading.Event()
    bridges = []
    original_submit = asyncio.run_coroutine_threadsafe

    def paused_submit(coro, loop):
        if coro.cr_code.co_name == "_produce":
            bridges.append(coro.cr_frame.f_locals["q"])
            entered.set()
            assert release.wait(2)
        return original_submit(coro, loop)

    monkeypatch.setattr(mod.asyncio, "run_coroutine_threadsafe", paused_submit)
    engine = _BurstEngine(1)
    stream = cuda_stream.start(engine)
    b = cuda_stream.backend

    def consume():
        try:
            next(stream)
        except RuntimeError:
            pass  # shutdown允许唤醒已受理消费者
        finally:
            consumed.set()

    def stop():
        b.shutdown()
        shutdown_returned.set()

    consumer = threading.Thread(target=consume, daemon=True)
    stopper = threading.Thread(target=stop, daemon=True)
    consumer.start()
    assert entered.wait(1)
    stopper.start()
    try:
        assert not engine.stopped.wait(0.05), "shutdown overtook an admitted submission"
        release.set()
        assert shutdown_returned.wait(1)
        assert consumed.wait(1), "admitted consumer left waiting on stopped loop"
        assert engine.stopped.wait(1)
    finally:
        release.set()
        for bridge in bridges:
            bridge.close()
        consumer.join(1)
        stopper.join(1)


def test_cuda_optional_sampling_keys_pass_through_only_when_given(cuda_stream):
    """top_p / top_k / presence / frequency：调用方给了才进 SamplingParams，没给沿用 vLLM 缺省
    （2026-10-31 P0 拐点测量加入；此前这些键被静默丢弃，模型卡推荐的截断采样到不了 vLLM）。"""
    class OneShot(_BurstEngine):
        async def generate(self, prompt, params, rid):
            self.requests.append((prompt, params.kwargs, rid))
            yield _completion("ok", [1, 2], "stop")

    engine = OneShot()
    b = cuda_stream.backend
    b._engine = engine
    list(b.stream_generate("synthetic", cache=b.new_cache(), sampling={"max_tokens": 8},
                           features={"want_delta": True}))
    kw = engine.requests[0][1]
    assert not ({"top_p", "top_k", "presence_penalty", "frequency_penalty"} & set(kw))
    engine.requests.clear()
    list(b.stream_generate("synthetic", cache=b.new_cache(),
                           sampling={"max_tokens": 8, "top_p": 0.95, "top_k": "20",
                                     "presence_penalty": 0, "repetition_penalty": 1.0},
                           features={"want_delta": True}))
    kw = engine.requests[0][1]
    assert (kw["top_p"], kw["top_k"], kw["presence_penalty"]) == (0.95, 20, 0.0)
    assert kw["repetition_penalty"] == 1.0 and "frequency_penalty" not in kw
