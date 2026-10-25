"""CPU-only AST stubs verify device-specific precision and truthful identity.

No torch/transformers import or device/model operation. Both real loader dispatch
paths receive the selected runtime dtype through actual load_models code.
"""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]


def harness(device="cpu", configured="bf16", compute_cfg=None, modality="vl"):
    fake_torch = SimpleNamespace(float16=object(), bfloat16=object(), float32=object())
    calls = []
    class Tokenizer:
        def convert_tokens_to_ids(self, token):
            return {"no": 0, "yes": 1}[token]

        def encode(self, text, **kw):
            return [1, 2]

    def loader(family):
        def load(embed, rerank, dtype):
            calls.append((family, embed, rerank, dtype))
            return {"embed_model": SimpleNamespace(dtype=dtype),
                    "rerank_model": SimpleNamespace(dtype=dtype),
                    "rerank_tokenizer": Tokenizer()}
        return load

    ns = dict(Optional=Optional, torch=fake_torch, DEVICE=device,
              ACTIVE_TIER={"dtype": configured, "id": "synthetic-tier", "thresholds": {"min": .3}},
              RAG_CFG={"compute": compute_cfg}, MODALITY=modality,
              EMBED_MODEL="synthetic/embed", RERANK_MODEL="synthetic/rerank", GLOBAL_OBJECTS={},
              _load_text_models=loader("text"), _load_vl_models=loader("vl"),
              resolve_model_path=lambda repo: "synthetic-path:" + repo,
              embed_texts=lambda texts: [[.1, .2] for _ in texts],
              TIER_EMBED_DIM_HINT=2, logger=logging.getLogger("synthetic-dtype"))
    tree = ast.parse((ROOT / "rag/compute_core.py").read_text())
    names = {"_DTYPE_MAP", "TIER_DTYPE", "RUNTIME_DTYPE_NAME", "RUNTIME_DTYPE"}
    functions = {"_runtime_dtype_name", "load_models", "identity"}
    nodes = [n for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name in functions)
             or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets))]
    assert {n.name for n in nodes if isinstance(n, ast.FunctionDef)} == functions
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "rag/compute_core.py", "exec"), ns)
    return SimpleNamespace(ns=ns, calls=calls, torch=fake_torch)


@pytest.mark.parametrize("configured", ["fp16", "bf16", "fp32", "float16", "bfloat16", "float32"])
def test_cpu_defaults_to_fp32_without_changing_tier(configured):
    h = harness(configured=configured)
    assert h.ns["RUNTIME_DTYPE"] is h.torch.float32
    assert h.ns["RUNTIME_DTYPE_NAME"] == "fp32"
    assert h.ns["ACTIVE_TIER"]["dtype"] == configured


@pytest.mark.parametrize("override,expected", [
    ("fp32", "fp32"), ("bf16", "bf16"), ("fp16", "fp16"),
    ("float32", "fp32"), ("bfloat16", "bf16"), ("float16", "fp16"),
    ("BF16", "bf16"), (None, "fp32"), ("", "fp32"),
])
def test_explicit_cpu_dtype_is_resolved_without_changing_repo(override, expected):
    h = harness(compute_cfg={"cpu_dtype": override})
    assert h.ns["RUNTIME_DTYPE_NAME"] == expected
    assert h.ns["RUNTIME_DTYPE"] is h.ns["_DTYPE_MAP"][expected]
    ident = h.ns["identity"]()
    assert ident["dtype"] == expected and ident["configured_dtype"] == "bf16"
    assert ident["embed_repo"] == "synthetic/embed" and ident["rerank_repo"] == "synthetic/rerank"


@pytest.mark.parametrize("device", ["cuda", "mps"])
@pytest.mark.parametrize("configured,expected", [
    ("bf16", "bf16"), ("fp16", "fp16"), ("fp32", "fp32"),
    ("float16", "fp16"), ("bfloat16", "bf16"), ("float32", "fp32"),
])
def test_accelerator_keeps_tier_dtype_and_ignores_irrelevant_cpu_config(device, configured, expected, capsys):
    h = harness(device=device, configured=configured, compute_cfg={"cpu_dtype": "fp32"})
    assert h.ns["RUNTIME_DTYPE"] is h.ns["TIER_DTYPE"]
    assert h.ns["RUNTIME_DTYPE_NAME"] == expected
    assert h.ns["RUNTIME_DTYPE"] is h.ns["_DTYPE_MAP"][expected]
    assert h.ns["identity"]()["configured_dtype"] == configured
    assert capsys.readouterr().out == ""   # 合法取值不该有警告


@pytest.mark.parametrize("override", ["auto", "float64", "invalid", 16])
def test_invalid_explicit_cpu_dtype_warns_and_falls_back_to_fp32(override, capsys):
    # 便利参数不拒启（同 rag.query_timeout_seconds）：拼错的精度不该让计算面起不来。
    h = harness(compute_cfg={"cpu_dtype": override})
    assert h.ns["RUNTIME_DTYPE_NAME"] == "fp32"
    assert h.ns["RUNTIME_DTYPE"] is h.torch.float32
    assert h.ns["identity"]()["dtype"] == "fp32"
    warning = capsys.readouterr().out
    assert warning.count("\n") == 1 and "rag.compute.cpu_dtype" in warning


@pytest.mark.parametrize("device", ["cuda", "mps"])
def test_invalid_cpu_dtype_is_still_reported_on_accelerators(device, capsys):
    # 拼错的 cpu_dtype 在 GPU/MPS 机上也要看得见（只是不生效），否则这台机器
    # 改不出的错要等搬到纯 CPU 机才首次暴露。
    h = harness(device=device, configured="bf16", compute_cfg={"cpu_dtype": "flot32"})
    assert h.ns["RUNTIME_DTYPE"] is h.ns["TIER_DTYPE"]
    assert h.ns["RUNTIME_DTYPE_NAME"] == "bf16"
    warning = capsys.readouterr().out
    assert "rag.compute.cpu_dtype" in warning and device in warning


@pytest.mark.parametrize("modality", ["text", "vl"])
@pytest.mark.parametrize("device", ["cpu", "cuda", "mps"])
def test_actual_load_models_dispatch_uses_runtime_dtype_and_identity(modality, device):
    h = harness(device=device, modality=modality)
    h.ns["load_models"]()
    assert len(h.calls) == 1
    family, embed, rerank, dtype = h.calls[0]
    assert family == modality
    assert embed == "synthetic-path:synthetic/embed" and rerank == "synthetic-path:synthetic/rerank"
    assert dtype is (h.torch.float32 if device == "cpu" else h.torch.bfloat16)
    assert h.ns["GLOBAL_OBJECTS"]["embed_model"].dtype is dtype
    assert h.ns["GLOBAL_OBJECTS"]["rerank_model"].dtype is dtype
    assert h.ns["identity"]() == {
        "tier": "synthetic-tier", "modality": modality,
        "embed_repo": "synthetic/embed", "rerank_repo": "synthetic/rerank", "embed_dim": 2,
        "dtype": "fp32" if device == "cpu" else "bf16", "configured_dtype": "bf16",
        "device": device, "thresholds": {"min": .3}}
