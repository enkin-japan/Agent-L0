"""基线格序架 v3 金测试（scripts/bench_baseline.py，§7 阶段一）。

守的是什么：这支序架产出的数字会直接进 EXECUTION_LEDGER §7 当基线，之后 MTP/KV
量化各格都拿它作分母。它自身的静默失败（流被腰斩当正常收尾、KV id 随异常丢掉、
野 data 行挂错事件名）不会报错，只会让数据**看上去正常**——所以 SSE 消费与口径
校验必须有回归网。纯 CPU：无网络、无模型、无 server 子进程，HTTP 连接全用替身。
"""
import importlib.util
import json
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "bench_baseline_v3", BACKEND_ROOT / "scripts" / "bench_baseline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bb = _load_harness()


# ---- HTTP 替身 ----

class FakeSock:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, t):
        self.timeouts.append(t)


class FakeResponse:
    """HTTPResponse 形状的替身：readline() 吐预置 SSE 字节行，吐完即 EOF。"""

    def __init__(self, lines, status=200):
        self.status = status
        self._lines = list(lines)
        self.fp = None          # 若序架回退到 r.fp.readline() 会立刻 AttributeError

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def read(self, *_a):
        return b"".join(self._lines)


class FakeConn:
    def __init__(self, resp):
        self._resp = resp
        self.sock = FakeSock()
        self.closed = False
        self.requests = []

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self._resp

    def close(self):
        self.closed = True


def _sse(*events):
    """(事件名, payload) 序列 → SSE 字节行；payload 传 str 即原样发（用于坏 JSON）。"""
    lines = []
    for name, payload in events:
        if name:
            lines.append(f"event: {name}\n".encode())
        body = payload if isinstance(payload, str) else json.dumps(payload)
        lines.append(f"data: {body}\n".encode())
        lines.append(b"\n")
    return lines


def _client(lines, status=200):
    cli = bb.Client(14161, "k")
    conn = FakeConn(FakeResponse(lines, status))
    cli._conn = lambda timeout: conn          # 替身注入：不碰真 socket
    return cli, conn


CHUNK1 = {"delta": "你好", "generation_tokens": 1}
CHUNK2 = {"delta": "，世界", "generation_tokens": 2,
          "prompt_tokens": 4096, "prompt_tps": 900.0, "generation_tps": 42.0,
          "peak_memory": 21.5}


# ---- SSE 消费 ----

def test_generate_normal_stream_returns_text_and_cache_id():
    """正常流：delta 累计成全文，末 chunk 原样回，cache_id 从 meta 取。"""
    cli, conn = _client(_sse(("meta", {"cache_id": "c-1"}),
                             ("chunk", CHUNK1), ("chunk", CHUNK2),
                             ("done", {})))
    holder = {}
    last, text, ttft, cid, wall = cli.generate("p", cache_out=holder)
    assert text == "你好，世界"
    assert last == CHUNK2
    assert cid == "c-1" and holder == {"cache_id": "c-1"}
    assert ttft is not None and wall >= 0
    assert conn.sock.timeouts == [bb.INTER_CHUNK_TIMEOUT]   # 首 chunk 后切逐 chunk 看门狗
    assert conn.closed


def test_generate_eof_before_done_is_an_error():
    """socket EOF 早于 done = 生成被腰斩：必须报错，不能当正常收尾把半截数据入账。"""
    cli, _ = _client(_sse(("meta", {"cache_id": "c-1"}), ("chunk", CHUNK1),
                          ("chunk", CHUNK2)))
    with pytest.raises(RuntimeError) as ei:
        cli.generate("p")
    msg = str(ei.value)
    assert "done" in msg and "2 chunk" in msg and "2 token" in msg


def test_generate_done_event_is_required_even_with_zero_chunks():
    """空流（连 chunk 都没有）同样按错误处理，报出 0 chunk。"""
    cli, _ = _client([])
    with pytest.raises(RuntimeError) as ei:
        cli.generate("p")
    assert "0 chunk" in str(ei.value)


def test_generate_error_event_raises():
    cli, _ = _client(_sse(("meta", {"cache_id": "c-1"}), ("error", {"msg": "boom"})))
    with pytest.raises(RuntimeError, match="engine error"):
        cli.generate("p")


def test_cache_out_filled_before_later_chunk_blows_up():
    """坏 JSON（或任何中途异常）之后，cache_id 已经落进 holder——删缓存才有得删。"""
    cli, conn = _client(_sse(("meta", {"cache_id": "c-9"}), ("chunk", CHUNK1),
                             ("chunk", "{坏掉的 json")))
    holder = {}
    with pytest.raises(json.JSONDecodeError):
        cli.generate("p", cache_out=holder)
    assert holder == {"cache_id": "c-9"}
    assert conn.closed


def test_cache_out_filled_on_error_event():
    cli, _ = _client(_sse(("meta", {"cache_id": "c-7"}), ("error", {"msg": "boom"})))
    holder = {}
    with pytest.raises(RuntimeError):
        cli.generate("p", cache_out=holder)
    assert holder == {"cache_id": "c-7"}


def test_stray_data_line_is_not_attributed_to_previous_event():
    """事件名消费即清：紧跟 meta 的野 data 行不得被当成第二个 meta 改写 cache_id，
    紧跟 chunk 的野 data 行也不得混进全文。"""
    lines = [b"event: meta\n", b'data: {"cache_id": "c-1"}\n', b"\n",
             b'data: {"cache_id": "WRONG"}\n', b"\n",            # 野行①
             b"event: chunk\n", b'data: {"delta": "\\u597d", "generation_tokens": 1}\n',
             b"\n",
             b'data: {"delta": "\\u810f", "generation_tokens": 99}\n', b"\n",  # 野行②
             b"event: done\n", b"data: {}\n", b"\n"]
    cli, _ = _client(lines)
    holder = {}
    last, text, ttft, cid, _w = cli.generate("p", cache_out=holder)
    assert cid == "c-1" and holder == {"cache_id": "c-1"}
    assert text == "好"
    assert last["generation_tokens"] == 1


def test_generate_http_error_status_raises():
    cli, _ = _client([], status=500)
    with pytest.raises(RuntimeError, match="/v1/generate -> 500"):
        cli.generate("p")


def test_generate_pins_bare_greedy_sampling_when_caller_passes_it():
    """v3 口径：真裸贪心显式钉死，不吃 backend 缺省（min_p=0.05 / rep_penalty=1.1）。
    rep_penalty 必须是 None（非 1.0）：make_logits_processors 只在 None/0 跳过处理器。"""
    assert bb.SAMPLING_BASE == {"temperature": 0.0, "min_p": 0.0,
                                "repetition_penalty": None}
    cli, conn = _client(_sse(("meta", {"cache_id": "c-1"}), ("chunk", CHUNK1),
                             ("done", {})))
    cli.generate("p", sampling={**bb.SAMPLING_BASE, "max_tokens": bb.MAX_TOKENS})
    body = json.loads(conn.requests[0][2])
    assert body["sampling"] == {"temperature": 0.0, "min_p": 0.0,
                                "repetition_penalty": None, "max_tokens": bb.MAX_TOKENS}
    assert body["features"]["want_delta"] is True


# ---- 中位与样本数 ----

def test_med_reports_sample_count_alongside_value():
    """med 只丢 None（缺测）；0.0 是合法测量值（亚毫秒 TTFT 圆整）必须保留。
    「3 次中位」与「1 次单点」必须能在产物里区分。"""
    rows = [{"gen_tps": 40.0}, {"gen_tps": None}, {"gen_tps": 44.0}, {"gen_tps": 42.0}]
    assert bb.med(rows, "gen_tps") == (42.0, 3)
    assert bb.med(rows, "prompt_tps") == (None, 0)
    assert bb.med([{"ttft_s": 0.0}, {"ttft_s": 1.5}], "ttft_s") == (0.75, 2)


# ---- 命令行口径护栏 ----

def test_parse_skips_accepts_wellformed_pairs():
    assert bb.parse_skips("gemma-4-31b-it-8bit:65536, e4b:0") == {
        ("gemma-4-31b-it-8bit", 65536), ("e4b", 0)}
    assert bb.parse_skips("") == set()
    assert bb.parse_skips(" , ") == set()


@pytest.mark.parametrize("bad", ["gemma-4-31b-it-8bit", "31b:", ":65536", "31b:64k"])
def test_parse_skips_rejects_malformed(bad):
    """形式不合就报错：静默忽略 = 「以为跳了其实在跑」，长上下文格一跑就是几十分钟。"""
    with pytest.raises(ValueError):
        bb.parse_skips(bad)


def test_check_runs_rejects_below_two_and_warns_at_two(capsys):
    with pytest.raises(ValueError):
        bb.check_runs(1)
    bb.check_runs(2)
    assert "稳态样本仅 1 个" in capsys.readouterr().out
    bb.check_runs(3)
    assert capsys.readouterr().out == ""


# ---- 缓存回收 ----

def test_drop_cache_deletes_and_logs_failure(capsys):
    calls = []

    class Cli:
        def json_call(self, method, path, **kw):
            calls.append((method, path))

    bb.drop_cache(Cli(), {"cache_id": "c-3"})
    assert calls == [("DELETE", "/v1/cache/c-3")]
    bb.drop_cache(Cli(), {})          # 无 id：静默跳过
    bb.drop_cache(Cli(), None)
    assert calls == [("DELETE", "/v1/cache/c-3")]

    class Boom:
        def json_call(self, *a, **kw):
            raise RuntimeError("connection refused")

    bb.drop_cache(Boom(), {"cache_id": "c-4"})   # 不得外抛，但要留证
    assert "c-4" in capsys.readouterr().out


# ---- 环境头与子进程环境 ----

def test_sysctl_missing_key_degrades_to_none(capsys):
    assert bb.sysctl("no.such.sysctl.key.for.test") is None
    capsys.readouterr()


def test_start_server_forwards_api_key(tmp_path, monkeypatch):
    """子进程钥匙 = read_env_key() 那把：两侧各读各的必然有对不上的一天。"""
    seen = {}

    class FakePopen:
        def __init__(self, argv, cwd=None, env=None, stdout=None, stderr=None):
            seen.update({"argv": argv, "env": env, "stdout": stdout})

    monkeypatch.setattr(bb.subprocess, "Popen", FakePopen)
    proc, lf = bb.start_server("org/model-x", 14161, tmp_path / "s.log", "K3Y")
    try:
        assert isinstance(proc, FakePopen)
        assert seen["env"]["INFER_API_KEY"] == "K3Y"
        assert seen["env"]["INFER_MODEL_REPO"] == "org/model-x"
        assert seen["env"]["INFER_PORT"] == "14161"
        assert seen["stdout"] is lf
    finally:
        lf.close()
