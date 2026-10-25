"""Deployment settings reach real CUDA engine args without sharing mutable config.

CPU stubs only: no vLLM, transformers download, CUDA, models or services.
"""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from framework.backends.cuda.backend import CudaVLLMBackend
from framework.backends.cuda.options import engine_options, public_options
from framework.backends.cuda.metrics import speculative_counters


def test_legacy_gemma_defaults_unchanged():
    assert engine_options({}, {}) == {"speculative_config": None, "kv_cache_dtype": "auto"}


def test_null_presets_mean_the_default_like_their_siblings():
    # Same convention as speculative_config / max_num_seqs: null keeps the default.
    assert engine_options({"kv_cache_dtype": None, "attention_backend": None}, {}) == {
        "speculative_config": None, "kv_cache_dtype": "auto"}


def test_max_num_seqs_optional_and_kept_out_of_public_receipt():
    assert "max_num_seqs" not in engine_options({"max_num_seqs": None}, {})
    options = engine_options({"max_num_seqs": 1}, {})
    assert options["max_num_seqs"] == 1
    assert "max_num_seqs" not in public_options(options)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "1", [], {}])
def test_max_num_seqs_rejects_non_positive_integers(value):
    with pytest.raises(ValueError, match="max_num_seqs"):
        engine_options({"max_num_seqs": value}, {})


def test_cuda_config_and_environment_override_are_independent():
    cfg = {"speculative_config": {"method": "mtp", "num_speculative_tokens": 3,
                                   "draft_model_config": {"x": [1]}},
           "attention_backend": "TRITON_ATTN", "kv_cache_dtype": "bfloat16"}
    original = deepcopy(cfg)
    options = engine_options(cfg, {})
    assert options["attention_config"] == {"backend": "TRITON_ATTN"}
    assert options["kv_cache_dtype"] == "bfloat16"
    options["speculative_config"]["draft_model_config"]["x"].append(2)
    assert cfg == original
    overridden = engine_options(cfg, {"INFER_SPEC_CONFIG": '{"method":"mtp","num_speculative_tokens":1}',
                                       "INFER_KV_DTYPE": "fp8"})
    assert overridden["speculative_config"] == {"method": "mtp", "num_speculative_tokens": 1}
    assert overridden["kv_cache_dtype"] == "fp8"


@pytest.mark.parametrize("value", ["off", "none", "null", " OFF "])
def test_environment_can_disable_configured_speculation(value):
    assert engine_options({"speculative_config": {"method": "mtp"}},
                          {"INFER_SPEC_CONFIG": value})["speculative_config"] is None


@pytest.mark.parametrize("value", ["fp8_e4m3", "fp8_e5m2"])
def test_legacy_environment_kv_aliases_reach_vllm(value):
    assert engine_options({"kv_cache_dtype": "bfloat16"},
                          {"INFER_KV_DTYPE": value})["kv_cache_dtype"] == value


@pytest.mark.parametrize("value", ["off", "none", "null", " OFF "])
def test_environment_can_disable_configured_kv_dtype(value):
    assert engine_options({"kv_cache_dtype": "fp8"},
                          {"INFER_KV_DTYPE": value})["kv_cache_dtype"] == "auto"


@pytest.mark.parametrize("value", ["fp8_e4m3", "fp8_e5m2"])
def test_config_kv_dtype_presets_remain_validated(value):
    with pytest.raises(ValueError, match="kv_cache_dtype"):
        engine_options({"kv_cache_dtype": value}, {})


@pytest.mark.parametrize("cfg,env", [
    ({"speculative_config": []}, {}), ({"speculative_config": {}}, {}),
    ({}, {"INFER_SPEC_CONFIG": "bad"}), ({}, {"INFER_SPEC_CONFIG": "true"}),
    ({"attention_backend": "TRITON"}, {}), ({"kv_cache_dtype": "fp4"}, {}),
])
def test_invalid_deployment_settings_fail_loudly(cfg, env):
    with pytest.raises(ValueError):
        engine_options(cfg, env)


def test_config_receipt_omits_private_draft_paths_and_is_independent():
    options = engine_options({"speculative_config": {"method": "mtp", "num_speculative_tokens": 3,
                                                    "model": "/private/model"}}, {})
    receipt = public_options(options)
    options["speculative_config"]["num_speculative_tokens"] = 9
    assert receipt == {"attention_backend": "auto", "kv_cache_dtype": "auto",
                       "speculative_config": {"method": "mtp", "num_speculative_tokens": 3}}


def test_boot_passes_vllm_024_args_and_survives_vllm_mutation(monkeypatch):
    seen = {}

    def args(**kw):
        seen.update(deepcopy(kw))
        return SimpleNamespace(**kw)

    def make_engine(args):
        args.speculative_config["model"] = "vllm-internal-added-model"
        args.speculative_config["nested"]["values"].append(2)
        return SimpleNamespace(shutdown=lambda: None)

    tok = SimpleNamespace()
    loader = SimpleNamespace(from_pretrained=lambda *a, **kw: tok)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoProcessor=loader, AutoTokenizer=loader))
    monkeypatch.setitem(sys.modules, "vllm", SimpleNamespace(AsyncEngineArgs=args))
    monkeypatch.setitem(sys.modules, "vllm.engine.async_llm_engine", SimpleNamespace(
        AsyncLLMEngine=SimpleNamespace(from_engine_args=make_engine)))
    for key in ("INFER_SPEC_CONFIG", "INFER_KV_DTYPE"):
        monkeypatch.delenv(key, raising=False)
    cfg = {"cuda": {"speculative_config": {"method": "mtp", "num_speculative_tokens": 3,
                                            "nested": {"values": [1]}},
                     "attention_backend": "TRITON_ATTN", "kv_cache_dtype": "bfloat16",
                     "max_num_seqs": 1}}
    original = deepcopy(cfg)
    backend = CudaVLLMBackend(cfg)
    try:
        backend._boot("/synthetic/model")
        assert seen["attention_config"] == {"backend": "TRITON_ATTN"}
        assert "attention_backend" not in seen
        assert seen["speculative_config"]["num_speculative_tokens"] == 3
        assert seen["kv_cache_dtype"] == "bfloat16"
        assert seen["max_num_seqs"] == 1
        assert cfg == original
        assert backend._cfg == original["cuda"]
        assert backend.runtime_options()["speculative_config"] == {"method": "mtp", "num_speculative_tokens": 3}
    finally:
        backend.shutdown()
        backend._thread.join(timeout=3)
        assert not backend._thread.is_alive()


@pytest.mark.parametrize("env_value,config_spec,expect_min_p", [
    ("off", {"method": "mtp", "num_speculative_tokens": 3}, True),
    ("null", {"method": "mtp", "num_speculative_tokens": 3}, True),
    (None, {"method": "mtp", "num_speculative_tokens": 3}, False),
    ('{"method":"mtp","num_speculative_tokens":3}', None, False),
    (None, None, True),
])
def test_min_p_uses_resolved_startup_speculation(monkeypatch, env_value, config_spec, expect_min_p):
    samples = []
    engine_args = []

    def args(**kw):
        engine_args.append(kw)
        return SimpleNamespace(**kw)

    def sample(**kw):
        samples.append(kw)
        return SimpleNamespace(**kw)

    async def generate(*args):
        yield SimpleNamespace(outputs=[SimpleNamespace(text="ok", token_ids=[1], finish_reason="stop")])

    engine = SimpleNamespace(generate=generate, shutdown=lambda: None)
    tok = SimpleNamespace(encode=lambda *a, **kw: [1, 2])
    loader = SimpleNamespace(from_pretrained=lambda *a, **kw: tok)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoProcessor=loader, AutoTokenizer=loader))
    monkeypatch.setitem(sys.modules, "vllm", SimpleNamespace(AsyncEngineArgs=args, SamplingParams=sample))
    monkeypatch.setitem(sys.modules, "vllm.engine.async_llm_engine", SimpleNamespace(
        AsyncLLMEngine=SimpleNamespace(from_engine_args=lambda args: engine)))
    if env_value is None:
        monkeypatch.delenv("INFER_SPEC_CONFIG", raising=False)
    else:
        monkeypatch.setenv("INFER_SPEC_CONFIG", env_value)
    backend = CudaVLLMBackend({"cuda": {"speculative_config": config_spec}})
    backend._peak_gb = lambda: 0.0
    try:
        backend._boot("/synthetic/model")
        assert (engine_args[0]["speculative_config"] is None) == expect_min_p
        # A later env edit must not change sampling for an already booted engine.
        monkeypatch.setenv("INFER_SPEC_CONFIG", "mtp-after-boot" if expect_min_p else "")
        assert list(backend.stream_generate("synthetic", cache=backend.new_cache(), sampling={"min_p": 0.17}))
        assert ("min_p" in samples[0]) == expect_min_p
        if expect_min_p:
            assert samples[0]["min_p"] == 0.17
    finally:
        backend.shutdown()
        backend._thread.join(timeout=3)
        assert not backend._thread.is_alive()


@pytest.fixture(scope="module")
def server():
    path = Path(__file__).resolve().parents[2] / "inference/server.py"
    spec = importlib.util.spec_from_file_location("inference_server_cuda_options_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_receipt_is_additive_and_old_backends_remain_supported(server, monkeypatch):
    monkeypatch.setattr(server.S, "backend", object())
    health = asyncio.run(server.health())
    assert health["service"] == "macagent-inference"
    assert health["inference_options"] is None
    backend = CudaVLLMBackend()
    backend._runtime_options = public_options(engine_options({}, {}))
    monkeypatch.setattr(server.S, "backend", backend)
    assert asyncio.run(server.health())["inference_options"] == backend.runtime_options()


def _counter(name, value, **labels):
    return SimpleNamespace(name="vllm:spec_decode_" + name, value=value, labels=labels)


def test_true_speculative_counters_sum_engines_without_exposing_labels():
    samples = [_counter("num_draft_tokens", 12, model_name="private", engine="0"),
               _counter("num_draft_tokens", 4, model_name="private", engine="1"),
               _counter("num_accepted_tokens", 7), _counter("num_drafts", 6),
               SimpleNamespace(name="unrelated", value=888)]
    assert speculative_counters(samples) == {
        "status": "observed", "source": "vllm", "draft_tokens": 16,
        "accepted_tokens": 7, "num_drafts": 6}


@pytest.mark.parametrize("samples", [[], [_counter("num_draft_tokens", 0)],
    [_counter("num_draft_tokens", float("nan"))], [_counter("num_draft_tokens", -1)]])
def test_missing_or_bad_counters_are_unknown_not_zero(samples):
    result = speculative_counters(samples)
    assert result["status"] == "unknown"
    assert result["draft_tokens"] is None


def test_async_metrics_flushes_only_on_engine_loop_and_uses_real_reader(monkeypatch):
    backend = CudaVLLMBackend()
    backend._loop = asyncio.new_event_loop()
    import threading
    backend._thread = threading.Thread(target=backend._loop.run_forever, daemon=True)
    backend._thread.start()
    calls = []

    async def flush():
        assert asyncio.get_running_loop() is backend._loop
        calls.append("flushed")

    def snapshot():
        assert asyncio.get_running_loop() is backend._loop
        assert calls == ["flushed"]
        return [_counter("num_draft_tokens", 9), _counter("num_accepted_tokens", 4),
                _counter("num_drafts", 3)]

    monkeypatch.setitem(sys.modules, "vllm.v1.metrics.reader", SimpleNamespace(get_metrics_snapshot=snapshot))
    backend._engine = SimpleNamespace(do_log_stats=flush, shutdown=lambda: None)
    try:
        result = asyncio.run(backend.speculative_metrics())
        assert result["status"] == "observed"
        assert result["draft_tokens"] == 9
        assert result["accepted_tokens"] == 4
    finally:
        backend.shutdown()
        backend._thread.join(timeout=3)
        assert not backend._thread.is_alive()


def test_unloaded_backend_metrics_is_unknown():
    assert asyncio.run(CudaVLLMBackend().speculative_metrics())["status"] == "unknown"


def test_server_metrics_adds_backend_execution_counters(server, monkeypatch):
    async def read():
        return {"status": "observed", "draft_tokens": 9, "accepted_tokens": 4, "num_drafts": 3}
    monkeypatch.setattr(server.S, "registry", {})
    monkeypatch.setattr(server.S, "backend", SimpleNamespace(speculative_metrics=read))
    result = asyncio.run(server.metrics())
    assert result["speculative_decode"]["accepted_tokens"] == 4
    assert "generations_total" in result


@pytest.mark.parametrize('facade,batch', [(0, 0), (1, 0), (0, 2), (1, 2)])
def test_active_gpu_work_includes_facade_and_batch(server, monkeypatch, facade, batch):
    monkeypatch.setattr(server.S, 'registry', {})
    monkeypatch.setattr(server.S, 'gens', {})
    monkeypatch.setattr(server.S, 'backend', None)
    monkeypatch.setattr(server.S, 'single_busy', facade)
    monkeypatch.setattr(server.S, 'batch_lane', SimpleNamespace(
        active_count=batch, decoding_count=batch, paused=False, steps_total=0,
        dead=False, max_num_seqs=4) if batch else None)
    result = asyncio.run(server.metrics())
    assert result['active_generations'] == 0  # Preserve the original field's semantics.
    assert result['active_gpu_work'] == facade + batch


def test_active_gpu_work_is_sampled_after_metrics_await(server, monkeypatch):
    async def read():
        server.S.single_busy = 1
        return {"status": "unknown"}
    monkeypatch.setattr(server.S, 'registry', {})
    monkeypatch.setattr(server.S, 'single_busy', 0)
    monkeypatch.setattr(server.S, 'batch_lane', None)
    monkeypatch.setattr(server.S, 'backend', SimpleNamespace(speculative_metrics=read))
    assert asyncio.run(server.metrics())['active_gpu_work'] == 1
