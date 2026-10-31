"""W-1/C：推理服务单流路取消即止损（客户端断连 / POST cancel）。

守的不变量（W0 观察：断连后 /metrics active_generations 持续为 1 不释放）：
- 取消一发生，服务端经 features["cancel_scope"] 触发后端登记的中止钩子（CUDA = 唤醒
  首包 get + engine.abort），不等下一个 chunk；预填期（首 chunk 前阻塞）同样生效；
- 推理线程退出后才注销 S.gens（真实占用语义不变），但「取消→注销」在限定时间内完成；
- 后端生成器在推理线程上被关闭（finally 执行）；正常完成路径照旧发 done。
另守 CUDA 后端一侧：scope.cancel() 让阻塞在首包 get 的同步迭代器立即抛出并走 abort。
全部 CPU、假后端，不起服务、不监听端口。
"""
import asyncio
import importlib.util
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from framework.core.backend_protocol import CancelScope, GenChunk

_REPO = pathlib.Path(__file__).resolve().parent.parent.parent
_SERVER = _REPO / "inference" / "server.py"


@pytest.fixture(scope="module")
def srv():
    spec = importlib.util.spec_from_file_location("inference_server_cancel_under_test", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _chunk(text):
    return GenChunk(text=text, delta=None, prompt_tokens=3, generation_tokens=1)


class _Backend:
    """假后端。mode：
    - "prefill"：首 chunk 前阻塞于 Event（模拟 CUDA 首包 q.get / 远端预填）；
    - "stream"：先出一个 chunk，再在第二个 chunk 前阻塞（模拟 token 间隔很长的解码）；
    - "finish"：出两个 chunk 正常结束。
    登记 cancel_scope 钩子（CUDA 后端同款契约）：钩子 = 记 abort + 放行阻塞点。"""

    def __init__(self, mode, abort_msg="aborted by cancel_scope"):
        self.mode = mode
        self.abort_msg = abort_msg            # 取消后从阻塞点抛出的 RuntimeError 文案
        self.entered = threading.Event()      # 已到阻塞点
        self.release = threading.Event()
        self.aborted = threading.Event()
        self.closed = threading.Event()
        self.close_thread = None
        self.gen_thread = None

    def new_cache(self):
        return SimpleNamespace(token_ids=None)

    def release_cache(self, h):
        pass

    def _hook(self):
        self.aborted.set()
        self.release.set()

    def stream_generate(self, prompt, *, cache, features=None, **kw):
        self.gen_thread = threading.get_ident()
        scope = (features or {}).get("cancel_scope")
        unhook = scope.add_hook(self._hook) if hasattr(scope, "add_hook") else None
        try:
            if self.mode == "finish":
                yield _chunk("a")
                yield _chunk("b")
                return
            if self.mode == "stream":
                yield _chunk("a")
            self.entered.set()
            # 上限 8 s：改动前（无钩子）这里一直等到超时，测试据此判红而不挂死
            self.release.wait(timeout=8)
            if self.aborted.is_set():
                raise RuntimeError(self.abort_msg)
            yield _chunk("late")
        finally:
            if unhook is not None:
                unhook()
            self.close_thread = threading.get_ident()
            self.closed.set()


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def armed(srv, monkeypatch):
    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inference-test")
    monkeypatch.setattr(srv.S, "executor", ex)
    monkeypatch.setattr(srv.S, "gens", {})
    monkeypatch.setattr(srv.S, "batch_lane", None)
    monkeypatch.setattr(srv.S, "single_busy", 0)
    monkeypatch.setattr(srv.S, "constraint_disabled", True)
    monkeypatch.setattr(srv.S, "metrics", dict(srv.S.metrics))
    holder = {}

    def install(mode, **kw):
        be = _Backend(mode, **kw)
        holder["be"] = be
        monkeypatch.setattr(srv.S, "backend", be)
        monkeypatch.setattr(srv.S, "registry", srv.CacheRegistry(be, 3600))
        return be

    yield install
    be = holder.get("be")
    if be is not None:
        be.release.set()                   # 红测时放行残留线程
    ex.shutdown(wait=False)


async def _start(srv, be):
    """进入 /v1/generate 的 SSE 体，消费到阻塞点；返回 (消费任务, 收到的事件, gen_id)。"""
    srv.S.gen_sem = asyncio.Semaphore(1)
    resp = await srv.generate(_Req({"prompt": "p", "features": {"cancel_scope": True}}))
    events = []

    async def consume():
        async for part in resp.body_iterator:
            events.append(part)

    task = asyncio.create_task(consume())
    ok = await asyncio.to_thread(be.entered.wait, 5)
    assert ok, "假后端没走到阻塞点"
    gen_id = next(iter(srv.S.gens))
    return task, events, gen_id


async def _gens_drained(srv, limit):
    t0 = time.monotonic()
    while srv.S.gens and time.monotonic() - t0 < limit:
        await asyncio.sleep(0.01)
    return time.monotonic() - t0


def _cancel(srv, how, task, gen_id):
    if how == "disconnect":
        task.cancel()          # Starlette 对断连的做法：取消跑响应体的任务
        return asyncio.sleep(0)
    return srv.generate_cancel(gen_id)


@pytest.mark.parametrize("how", ["disconnect", "cancel_endpoint"])
def test_cancel_mid_stream_aborts_backend_and_frees_gens(srv, armed, how):
    """(a) 解码中途断连 / POST cancel：后端中止钩子被调、生成器在推理线程上关闭、
    S.gens 在 2 s 内回 0（改动前：钩子从不被调，线程卡到阻塞点超时）。"""
    be = armed("stream")

    async def run():
        task, events, gen_id = await _start(srv, be)
        assert len(srv.S.gens) == 1
        await _cancel(srv, how, task, gen_id)
        took = await _gens_drained(srv, 2.0)
        assert not srv.S.gens, f"取消 {took:.2f}s 后 S.gens 仍未释放"
        assert be.aborted.is_set(), "取消没有触发后端中止钩子"
        assert be.closed.is_set() and be.close_thread == be.gen_thread, \
            "后端生成器须在拥有它的推理线程上关闭"
        if how == "cancel_endpoint":
            await asyncio.wait_for(task, 2)
            assert not any("event: done" in e for e in events)
        else:
            assert task.cancelled() or task.done()
        assert srv.S.single_busy == 0
        assert srv.S.gen_sem._value == 1, "生成闸未释放"

    asyncio.run(run())


@pytest.mark.parametrize("how", ["disconnect", "cancel_endpoint"])
def test_cancel_during_prefill_exits_thread_quickly(srv, armed, how):
    """(b) 首 chunk 之前（预填期，后端阻塞在 Event 上）取消：< 2 s 内推理线程退出、
    S.gens 回 0（改动前：取消标志只在 chunk 边界被看见，线程要等后端自己醒）。"""
    be = armed("prefill")

    async def run():
        task, events, gen_id = await _start(srv, be)
        t0 = time.monotonic()
        await _cancel(srv, how, task, gen_id)
        await _gens_drained(srv, 2.0)
        took = time.monotonic() - t0
        assert not srv.S.gens and took < 2.0, f"预填期取消 {took:.2f}s 未退出"
        assert be.aborted.is_set() and be.closed.is_set()
        assert srv.S.metrics["errors_total"] == 0, "中止属取消，不计 error"
        if how == "cancel_endpoint":
            await asyncio.wait_for(task, 2)
            assert not any("event: error" in e or "event: done" in e for e in events)

    asyncio.run(run())


_CANCEL_LOG = "[Server] 取消后后端异常（按取消处理）: "


@pytest.mark.parametrize("abort_msg,logged", [
    ("CUDA 生成 600s 无产出（引擎挂死？）", True),    # 非取消应答：按取消计，但留诊断
    ("CUDA 生成已被调用方取消", False),              # 后端对取消的正常应答：不刷日志
])
def test_backend_error_after_cancel_is_cancelled_but_logged(srv, armed, capsys,
                                                             abort_msg, logged):
    """(b2) 取消标志已置后后端抛 RuntimeError：仍记 cancelled（不计 error、不发 error 事件），
    但非「已被调用方取消」类的异常打一行日志，「引擎挂死」这类诊断不被取消吞掉。"""
    be = armed("prefill", abort_msg=abort_msg)

    async def run():
        task, events, gen_id = await _start(srv, be)
        await srv.generate_cancel(gen_id)
        await _gens_drained(srv, 2.0)
        await asyncio.wait_for(task, 2)
        assert not srv.S.gens and srv.S.metrics["errors_total"] == 0
        assert not any("event: error" in e or "event: done" in e for e in events)

    asyncio.run(run())
    out = capsys.readouterr().out
    line = f"{_CANCEL_LOG}RuntimeError: {abort_msg}"
    assert (line in out) is logged, out


def test_note_cancel_exc_classification(srv, capsys):
    """门面路与 /v1/generate 共用的分类：取消类静默，其余一行日志。"""
    from concurrent.futures import CancelledError as FCancelled
    srv._note_cancel_exc(asyncio.CancelledError())
    srv._note_cancel_exc(FCancelled())
    srv._note_cancel_exc(RuntimeError("CUDA 生成已被调用方取消"))
    assert capsys.readouterr().out == ""
    srv._note_cancel_exc(ValueError("boom"))
    assert capsys.readouterr().out.strip() == f"{_CANCEL_LOG}ValueError: boom"


def test_normal_completion_unaffected(srv, armed):
    """(c) 正常完成：发 done、钩子已注销（收尾的 cancel 不打到已完成请求）、gens 回 0。"""
    be = armed("finish")
    be.entered.set()

    async def run():
        srv.S.gen_sem = asyncio.Semaphore(1)
        resp = await srv.generate(_Req({"prompt": "p"}))
        events = [part async for part in resp.body_iterator]
        assert sum("event: chunk" in e for e in events) == 2
        assert "event: done" in events[-1]
        await _gens_drained(srv, 2.0)
        assert not srv.S.gens and not be.aborted.is_set() and be.closed.is_set()

    asyncio.run(run())


def test_client_cannot_spoof_cancel_scope(srv, armed):
    """客户端 JSON 里的同名键被服务端无条件覆盖为进程内 CancelScope。"""
    be = armed("finish")
    seen = {}
    orig = be.stream_generate

    def spy(prompt, *, cache, features=None, **kw):
        seen["scope"] = (features or {}).get("cancel_scope")
        return orig(prompt, cache=cache, features=features, **kw)

    be.stream_generate = spy

    async def run():
        srv.S.gen_sem = asyncio.Semaphore(1)
        resp = await srv.generate(_Req({"prompt": "p", "features": {"cancel_scope": "x"}}))
        _ = [part async for part in resp.body_iterator]

    asyncio.run(run())
    assert isinstance(seen["scope"], CancelScope)


def test_cancel_scope_semantics():
    s = CancelScope()
    calls = []
    remove = s.add_hook(lambda: calls.append("a"))
    s.add_hook(lambda: (_ for _ in ()).throw(ValueError("boom")))   # 钩子异常不外抛
    remove()
    s.cancel()
    s.cancel()                                                      # 幂等
    assert calls == [] and s.cancelled
    s.add_hook(lambda: calls.append("late"))                        # 取消后登记：当场执行
    assert calls == ["late"]


# ---- CUDA 后端一侧：钩子让首包前阻塞的同步迭代器立即结束并走 engine.abort ----

def test_cuda_scope_cancel_unblocks_prefill_and_aborts(monkeypatch):
    from framework.backends.cuda.backend import CudaVLLMBackend

    class _PrefillEngine:
        def __init__(self):
            self.aborted = threading.Event()
            self.started = threading.Event()

        async def generate(self, prompt, params, rid):
            self.started.set()
            await asyncio.Event().wait()            # 预填永不出首包
            yield None

        async def abort(self, rid):
            self.aborted.set()

    monkeypatch.setitem(sys.modules, "vllm", SimpleNamespace(
        SamplingParams=lambda **kw: SimpleNamespace(kwargs=kw)))
    b = CudaVLLMBackend()
    b._tokenizer = SimpleNamespace(encode=lambda text, **kw: [100, 101])
    b._peak_gb = lambda: 0.0
    b._loop = asyncio.new_event_loop()
    th = threading.Thread(target=b._loop.run_forever, daemon=True)
    th.start()
    eng = _PrefillEngine()
    b._engine = eng
    scope = CancelScope()
    out = {}

    def consume():
        stream = b.stream_generate("synthetic", cache=b.new_cache(),
                                   features={"cancel_scope": scope})
        try:
            next(stream)
            out["result"] = "chunk"
        except BaseException as e:      # noqa: BLE001
            out["result"] = e
        finally:
            stream.close()

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    try:
        assert eng.started.wait(5)
        t0 = time.monotonic()
        scope.cancel()
        worker.join(timeout=2)
        assert not worker.is_alive(), "scope.cancel 后首包 get 未被唤醒"
        assert time.monotonic() - t0 < 2
        assert isinstance(out["result"], RuntimeError) and "取消" in str(out["result"])
        assert eng.aborted.wait(2), "取消没有走到 engine.abort"
        deadline = time.monotonic() + 2
        while b._producers and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not b._producers and not b._rid_map
    finally:
        b._loop.call_soon_threadsafe(b._loop.stop)
        th.join(timeout=2)
