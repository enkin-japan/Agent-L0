"""条目级 sampling（W-1/A）：注册表条目 `sampling` → 引擎传给后端的采样字典。

解析顺序：调用方显式值 > 条目 sampling > 旧常量（主路径 0.2/0.05、oneshot 0.3/0.05、
rep 1.1、rep_ctx 50）。条目未声明的可选键（top_p/top_k/presence/frequency）不得出现
在字典里——后端缺省行为与改动前一致。非法条目在构造时 ValueError（失败关闭）。
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from framework.core.engine import AgentEngine, _validate_sampling_entry


class _Tok:
    special_tokens_map = {}

    def encode(self, s, add_special_tokens=False):
        return list(range(len(s)))


class _RecordingBackend:
    """记录每次 stream_generate 的 sampling；吐一段纯文本即收尾。"""

    def __init__(self):
        self.processor = SimpleNamespace(tokenizer=_Tok())
        self.samplings = []

    def stream_generate(self, prompt, **kwargs):
        self.samplings.append(dict(kwargs.get("sampling") or {}))
        yield SimpleNamespace(text="ok", delta=None, prompt_tokens=3,
                              generation_tokens=1, prompt_tps=1.0,
                              generation_tps=1.0, peak_memory=0.0, extra={})

    def new_cache(self):
        return object()

    def release_cache(self, cache):
        pass


def _engine(backend, sampling=None):
    opts = {"dialect": "gemma4"}
    if sampling is not None:
        opts["sampling"] = sampling
    return AgentEngine(backend, kv_config={}, model_options=opts)


def _session():
    return SimpleNamespace(history=[{"role": "system", "content": "s"},
                                    {"role": "user", "content": "hi"}],
                           images=[], audios=[], brain_memory_state=None,
                           cancel_requested=False, inject_env_context=False,
                           prompt_stream=None, session_id="samp-s",
                           journal=SimpleNamespace(current_turn_id="turn-1"))


QWEN_LIKE = {"temperature": 1.0, "top_p": 0.95, "top_k": 20,
             "repetition_penalty": 1.0, "min_p": 0.0}


def _main(be, eng, **kw):
    asyncio.run(eng.generate_once(_session(), tools=None, max_tokens=77, **kw))
    return be.samplings[-1]


def _oneshot(be, eng, **kw):
    asyncio.run(eng.oneshot_instruction("instr", "content", max_tokens=55, **kw))
    return be.samplings[-1]


# (a) 无条目 sampling：与旧硬编码逐键相等，且无可选键
def test_no_entry_sampling_main_path_matches_legacy_dict():
    be = _RecordingBackend()
    s = _main(be, _engine(be))
    assert s == {"temperature": 0.2, "min_p": 0.05, "max_tokens": 77,
                 "repetition_penalty": 1.1, "repetition_context_size": 50}
    for k in ("top_p", "top_k", "presence_penalty", "frequency_penalty"):
        assert k not in s


def test_no_entry_sampling_oneshot_and_summarize_match_legacy_dict():
    be = _RecordingBackend()
    eng = _engine(be)
    legacy = {"temperature": 0.3, "min_p": 0.05, "max_tokens": 55,
              "repetition_penalty": 1.1, "repetition_context_size": 50}
    assert _oneshot(be, eng) == legacy
    asyncio.run(eng.summarize("hello", max_tokens=55))
    assert be.samplings[-1] == legacy


# (b) 条目全部键体现在传给后端的 sampling
def test_entry_sampling_reaches_backend_main_path():
    be = _RecordingBackend()
    s = _main(be, _engine(be, QWEN_LIKE))
    assert s == {"temperature": 1.0, "min_p": 0.0, "top_p": 0.95, "top_k": 20,
                 "repetition_penalty": 1.0, "repetition_context_size": 50,
                 "max_tokens": 77}


def test_entry_sampling_passes_presence_and_frequency():
    be = _RecordingBackend()
    s = _main(be, _engine(be, {"presence_penalty": 1.5, "frequency_penalty": 0.25}))
    assert s["presence_penalty"] == 1.5 and s["frequency_penalty"] == 0.25
    assert s["temperature"] == 0.2 and s["repetition_penalty"] == 1.1  # 未声明键走旧常量


# (c) 调用方显式值盖过条目；None 视为未指定
def test_explicit_caller_values_override_entry():
    be = _RecordingBackend()
    eng = _engine(be, QWEN_LIKE)
    s = _main(be, eng, temperature=0.7, min_p=0.1)
    assert s["temperature"] == 0.7 and s["min_p"] == 0.1
    assert s["top_p"] == 0.95 and s["top_k"] == 20      # 其余仍取条目
    s = _main(be, eng, temperature=None, min_p=None)
    assert s["temperature"] == 1.0 and s["min_p"] == 0.0
    s = _oneshot(be, eng, temperature=0.0)
    assert s["temperature"] == 0.0                       # 0.0 是显式值，不是缺省


# (d) 非法条目：构造即 ValueError
@pytest.mark.parametrize("bad", [
    {"temprature": 0.5},                 # 拼错的键
    {"seed": 1},                         # 白名单外的键
    {"temperature": "0.7"},              # 字符串
    {"top_k": True},                     # bool 不算数值
    {"repetition_penalty": None},        # None 不算数值
    ["temperature", 0.7],                # 非映射
])
def test_invalid_entry_sampling_rejected_at_construction(bad):
    with pytest.raises(ValueError):
        _engine(_RecordingBackend(), bad)


# (d2) 取值范围：每个边界一红一绿（构造时校验，失败关闭）
_INF, _NAN = float("inf"), float("nan")


@pytest.mark.parametrize("key,bad", [
    ("temperature", -0.01),
    ("min_p", -0.01), ("min_p", 1.01),
    ("top_p", 0.0), ("top_p", 1.01),
    ("top_k", -1), ("top_k", 20.5), ("top_k", 20.0),
    ("repetition_penalty", 0.0), ("repetition_penalty", -1.0),
    ("repetition_context_size", 0), ("repetition_context_size", 50.0),
    ("presence_penalty", -2.01), ("presence_penalty", 2.01),
    ("frequency_penalty", -2.01), ("frequency_penalty", 2.01),
    ("temperature", _INF), ("temperature", _NAN), ("min_p", _NAN),
    ("top_p", _NAN), ("repetition_penalty", _INF), ("presence_penalty", -_INF),
    ("frequency_penalty", _NAN),
])
def test_out_of_range_entry_sampling_rejected(key, bad):
    with pytest.raises(ValueError):
        _validate_sampling_entry({key: bad})
    with pytest.raises(ValueError):
        _engine(_RecordingBackend(), {key: bad})


@pytest.mark.parametrize("key,good", [
    ("temperature", 0), ("temperature", 0.0), ("temperature", 2.0),
    ("min_p", 0.0), ("min_p", 1.0), ("min_p", 1),
    ("top_p", 1e-6), ("top_p", 1.0), ("top_p", 1),
    ("top_k", 0), ("top_k", 20),
    ("repetition_penalty", 1e-6), ("repetition_penalty", 1.0),
    ("repetition_context_size", 1), ("repetition_context_size", 50),
    ("presence_penalty", -2), ("presence_penalty", 2.0),
    ("frequency_penalty", -2.0), ("frequency_penalty", 2),
])
def test_in_range_entry_sampling_accepted(key, good):
    assert _validate_sampling_entry({key: good}) == {key: good}
    _engine(_RecordingBackend(), {key: good})


# (d3) 仓库 config.yaml：每条注册表条目的 sampling 都过校验；CUDA 两条不带
# temperature/min_p（App/routes 每轮显式带 0.2/0.05，条目温度在聊天轮永不生效，
# 留着会造成「条目写 1.0、实际跑 0.2」的混合口径）。
_CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"


def _registry():
    cfg = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    return (cfg.get("inference") or {}).get("model_registry") or []


def test_repo_config_registry_sampling_entries_validate():
    entries = _registry()
    assert entries, "注册表为空？config.yaml 结构变了"
    for e in entries:
        try:
            _validate_sampling_entry(e.get("sampling"))
        except ValueError as exc:
            pytest.fail(f"{e.get('repo')}: {exc}")


@pytest.mark.parametrize("repo_frag", ["gemma-4-31B-it-qat-w4a16-ct", "Qwen3.8-27B-INT4"])
def test_cuda_entries_do_not_declare_temperature_or_min_p(repo_frag):
    hits = [e for e in _registry() if repo_frag in e.get("repo", "")]
    assert len(hits) == 1, f"{repo_frag}: 期望恰一条，得到 {[e.get('repo') for e in hits]}"
    sampling = hits[0].get("sampling") or {}
    assert "temperature" not in sampling and "min_p" not in sampling, \
        f"{hits[0]['repo']} sampling 不应声明 temperature/min_p：{sampling}"


def test_empty_or_null_entry_sampling_is_legacy():
    for empty in ({}, None):
        be = _RecordingBackend()
        assert _main(be, _engine(be, empty))["repetition_penalty"] == 1.1


# (e) oneshot 路径同样吃条目
def test_oneshot_path_uses_entry_sampling():
    be = _RecordingBackend()
    eng = _engine(be, QWEN_LIKE)
    s = _oneshot(be, eng)
    assert s == {"temperature": 1.0, "min_p": 0.0, "top_p": 0.95, "top_k": 20,
                 "repetition_penalty": 1.0, "repetition_context_size": 50,
                 "max_tokens": 55}
    asyncio.run(eng.summarize("hello", max_tokens=55))
    assert be.samplings[-1]["temperature"] == 1.0
    asyncio.run(eng.summarize("hello", max_tokens=55, temperature=0.3))
    assert be.samplings[-1]["temperature"] == 0.3


def test_entry_sampling_not_shared_between_calls():
    """解析结果是新字典：调用方/后端改它不得回写条目默认。"""
    be = _RecordingBackend()
    eng = _engine(be, QWEN_LIKE)
    _main(be, eng)
    be.samplings[-1]["top_k"] = 999
    assert _main(be, eng)["top_k"] == 20


# MLX 后端：条目可选键透传到补丁层 stream_generate（→ make_sampler / 处理器）
def _mlx_capture(monkeypatch, sampling):
    import framework.backends.mlx.backend as mlx_backend_mod
    from framework.backends.mlx.backend import MLXBackend
    captured = {}

    def fake_stream_generate(**kw):
        captured.update(kw)
        return iter(())

    monkeypatch.setattr(mlx_backend_mod, "_patched_stream_generate", fake_stream_generate)
    be = MLXBackend(None, None, {"model_path": "m"})
    cache = SimpleNamespace(state=SimpleNamespace())
    list(be.stream_generate("p", cache=cache, sampling=sampling, features={}))
    return captured


def test_mlx_backend_forwards_optional_sampling_keys(monkeypatch):
    kw = _mlx_capture(monkeypatch, {"temperature": 1.0, "min_p": 0.0, "max_tokens": 9,
                                    "top_p": 0.95, "top_k": 20,
                                    "presence_penalty": 1.5, "frequency_penalty": 0.1,
                                    "repetition_penalty": 1.0,
                                    "repetition_context_size": 50})
    assert kw["top_p"] == 0.95 and kw["top_k"] == 20
    assert kw["presence_penalty"] == 1.5 and kw["frequency_penalty"] == 0.1
    assert kw["temperature"] == 1.0 and kw["min_p"] == 0.0
    assert kw["repetition_penalty"] == 1.0


def test_mlx_backend_omits_absent_optional_keys(monkeypatch):
    kw = _mlx_capture(monkeypatch, {"temperature": 0.2, "min_p": 0.05, "max_tokens": 9,
                                    "repetition_penalty": 1.1,
                                    "repetition_context_size": 50})
    for k in ("top_p", "top_k", "presence_penalty", "frequency_penalty"):
        assert k not in kw


def test_decode_steps_accept_top_p_top_k_by_name():
    """下层两条 decode 生成器都按名收 top_p/top_k（签名锚：上游改名会让键静默落进
    **kwargs 被丢）。MTP 路走同一个上游 generate_step。"""
    import inspect
    from mlx_vlm.generate import generate_step
    from framework.backends.mlx.patch.gemma4_generate import lookup_generate_step
    for fn in (generate_step, lookup_generate_step):
        params = inspect.signature(fn).parameters
        assert "top_p" in params and "top_k" in params, fn.__name__
    assert "presence_penalty" in inspect.signature(generate_step).parameters
    assert "frequency_penalty" in inspect.signature(generate_step).parameters
