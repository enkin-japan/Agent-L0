"""CUDA startup options, independent of vLLM/GPU imports.

The engine owns its input dictionaries and may modify them during validation.
Never hand it configuration objects owned by the caller/config loader.
"""
from copy import deepcopy
import json
from typing import Mapping


def engine_options(cfg: Mapping, env: Mapping) -> dict:
    spec = deepcopy(cfg.get("speculative_config"))
    raw = env.get("INFER_SPEC_CONFIG", "").strip()
    if raw:
        if raw.lower() in ("off", "none", "null"):
            spec = None
        else:
            try:
                spec = json.loads(raw)
            except ValueError as exc:
                raise ValueError("INFER_SPEC_CONFIG must be a JSON object or off/null") from exc
    if spec is not None and not isinstance(spec, dict):
        raise ValueError("inference.cuda.speculative_config must be an object or null")
    # An empty object is ambiguous to vLLM; use null/off to explicitly disable it.
    if spec == {}:
        raise ValueError("empty speculative_config: use null/off to disable speculation")

    kv_env = env.get("INFER_KV_DTYPE", "").strip()
    # null 与同组的 speculative_config / max_num_seqs 一致：沿用缺省（auto）
    kv = kv_env or cfg.get("kv_cache_dtype") or "auto"
    if isinstance(kv, str) and kv.lower() in ("off", "none", "null"):
        kv = "auto"
    # The config surface lists supported deployment presets. Preserve the legacy
    # env escape hatch for vLLM aliases (e.g. fp8_e4m3/fp8_e5m2) and let the
    # installed vLLM version validate them, as it did before these presets existed.
    if not kv_env and kv not in ("auto", "bfloat16", "fp8"):
        raise ValueError("inference.cuda.kv_cache_dtype must be auto, bfloat16 or fp8")
    attention = cfg.get("attention_backend") or "auto"
    if attention not in ("auto", "TRITON_ATTN", "FLASHINFER"):
        raise ValueError("inference.cuda.attention_backend must be auto, TRITON_ATTN or FLASHINFER")
    out = {"speculative_config": spec, "kv_cache_dtype": kv}
    # Omitting auto preserves older vLLM/Gemma auto-selection behavior.
    if attention != "auto":
        out["attention_config"] = {"backend": attention}
    max_num_seqs = cfg.get("max_num_seqs")
    if max_num_seqs is not None:
        if isinstance(max_num_seqs, bool) or not isinstance(max_num_seqs, int) or max_num_seqs <= 0:
            raise ValueError("inference.cuda.max_num_seqs must be a positive integer or null")
        out["max_num_seqs"] = max_num_seqs
    return out


def public_options(options: Mapping) -> dict:
    """A startup configuration receipt, not evidence of actual speculative work."""
    spec = options.get("speculative_config")
    public_spec = None if spec is None else {
        k: deepcopy(spec[k]) for k in ("method", "num_speculative_tokens") if k in spec}
    return {"attention_backend": (options.get("attention_config") or {}).get("backend", "auto"),
            "kv_cache_dtype": options.get("kv_cache_dtype", "auto"),
            "speculative_config": public_spec}
