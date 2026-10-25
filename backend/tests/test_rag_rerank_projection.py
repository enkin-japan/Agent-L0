"""CPU stubs run the actual reranker, guarding last-position vocabulary projection.

No transformers/torch import, model initialization, network or user data. Fake
text/VL forward methods follow the supported Qwen3 logits_to_keep contract.
"""
import ast
import json
import logging
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = "private synthetic input that must never be logged"


class Tensor:
    def __init__(self, value):
        self.value = np.asarray(value)

    @property
    def shape(self):
        return self.value.shape

    def __getitem__(self, index):
        return Tensor(self.value[index])

    def exp(self):
        return Tensor(np.exp(self.value))

    def tolist(self):
        return self.value.tolist()


class Batch(dict):
    def to(self, device):
        assert device == "synthetic-cpu"
        return self


class Tokenizer:
    def __init__(self):
        self.calls = []
        self.padded = None

    def __call__(self, pairs, **kwargs):
        self.calls.append((pairs, kwargs))
        return {"input_ids": [[ord(c) % 17 + 1 for c in pair][:kwargs["max_length"]] for pair in pairs]}

    def pad(self, inputs, **kwargs):
        assert kwargs == {"padding": True, "return_tensors": "pt"}
        size = max(len(row) for row in inputs["input_ids"])
        self.padded = Batch(input_ids=Tensor([
            [0] * (size - len(row)) + row for row in inputs["input_ids"]]))
        return self.padded


class TextModel:
    def __init__(self):
        self.projected = []
        self.keep = []

    def __call__(self, input_ids, logits_to_keep=0):
        # Mirror native Qwen3 text/VL slicing before the linear vocabulary head.
        values = input_ids.value.astype(float)
        states = np.stack([values, np.cumsum(values, axis=1) / 100,
                           np.ones_like(values), np.arange(values.shape[1])[None, :]
                           + np.zeros_like(values)], axis=-1)
        indices = slice(-logits_to_keep, None)
        selected = states[:, indices, :]
        self.projected.append(selected.shape)
        self.keep.append(logits_to_keep)
        weights = np.array([[.3, -.2, .1, .2, -.1, .4, .1],
                            [.1, .4, -.3, .2, .2, -.1, .1],
                            [.2, .1, -.1, .3, .1, .2, -.2],
                            [.1, -.1, .2, -.1, .1, -.2, .2]])
        return SimpleNamespace(logits=Tensor(selected @ weights))


class VLModel(TextModel):
    def __call__(self, input_ids, logits_to_keep=0):
        return super().__call__(input_ids, logits_to_keep=logits_to_keep)


def log_softmax(tensor, dim):
    values = tensor.value
    shifted = values - values.max(axis=dim, keepdims=True)
    return Tensor(shifted - np.log(np.exp(shifted).sum(axis=dim, keepdims=True)))


def harness(family="text", max_length=8192):
    tokenizer = Tokenizer()
    model = TextModel() if family == "text" else VLModel()
    objects = {"rerank_tokenizer": tokenizer, "rerank_model": model,
               "rerank_prefix_tokens": [9, 4], "rerank_suffix_tokens": [3, 7, 2],
               "token_true_id": 1, "token_false_id": 4}
    ns = dict(List=List, Optional=Optional, GLOBAL_OBJECTS=objects,
              DEFAULT_RERANK_INSTRUCT="synthetic instruction", MPS_LOCK=threading.Lock(),
              MAX_LENGTH=max_length, DEVICE="synthetic-cpu", time=time, json=json,
              logger=logging.getLogger("synthetic-rerank"), torch=SimpleNamespace(
                  no_grad=nullcontext, stack=lambda values, dim: Tensor(np.stack([v.value for v in values], axis=dim)),
                  nn=SimpleNamespace(functional=SimpleNamespace(log_softmax=log_softmax)),
                  backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False))))
    source = ROOT / "rag/compute_core.py"
    tree = ast.parse(source.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "rerank_scores")
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), ns)
    return SimpleNamespace(run=ns["rerank_scores"], tokenizer=tokenizer, model=model, ns=ns)


@pytest.mark.parametrize("family", ["text", "vl"])
@pytest.mark.parametrize("batch_size", [1, 7, 33])
def test_only_final_position_is_projected_with_original_score_order(family, batch_size):
    h = harness(family)
    docs = [f"record {i}: " + "x" * (i * 23) for i in range(batch_size)]
    scores = h.run(PRIVATE, docs)
    assert len(scores) == batch_size
    assert h.model.keep == [1], "reranker must request only the last token's vocabulary logits"
    assert h.model.projected[0] == (batch_size, 1, 4), "full-sequence vocabulary projection returned"

    # Run the old full-sequence projection on identical already-tokenized input.
    full = h.model(**h.tokenizer.padded).logits[:, -1, :]
    expected = log_softmax(Tensor(np.stack([full.value[:, 4], full.value[:, 1]], axis=1)), 1)[:, 1].exp().tolist()
    np.testing.assert_allclose(scores, expected, rtol=1e-12, atol=1e-12)
    assert h.model.projected[1][1] > 1


def test_start_record_is_emitted_before_the_model_lock_is_acquired(caplog):
    # 锁等待/分词卡死是真实故障形态：start 记录落在锁内就等于「卡死即无记录」。
    caplog.set_level(logging.INFO)
    h = harness()
    h.ns["MPS_LOCK"].acquire()          # 模拟另一批正占着模型锁
    worker = threading.Thread(target=lambda: h.run(PRIVATE, ["doc"]), daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    def rows():
        return [json.loads(r.getMessage()) for r in caplog.records
                if r.name == "synthetic-rerank"]
    while time.monotonic() < deadline and not rows():
        time.sleep(.01)
    assert [r["state"] for r in rows()] == ["start"], "锁等待期必须已有 start 记录"
    h.ns["MPS_LOCK"].release()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert [r["state"] for r in rows()] == ["start", "end"]
    assert rows()[1]["lock_wait_ms"] > 0
    assert PRIVATE not in caplog.text


def test_empty_documents_do_not_call_tokenizer_or_model():
    h = harness()
    assert h.run(PRIVATE, []) == []
    assert h.tokenizer.calls == h.model.projected == []


@pytest.mark.parametrize("family", ["text", "vl"])
def test_long_unequal_batch_keeps_truncation_padding_and_override(family, caplog):
    caplog.set_level(logging.INFO)
    h = harness(family, max_length=8192)
    docs = ["x" * 20000, "tiny", "middle" * 300]
    scores = h.run(PRIVATE, docs, instruct=PRIVATE)
    assert len(scores) == 3
    assert h.model.projected == [(3, 1, 4)]
    pairs, options = h.tokenizer.calls[0]
    assert all(pair.startswith("<Instruct>: " + PRIVATE) for pair in pairs)
    assert options == {"padding": False, "truncation": "longest_first",
                       "return_attention_mask": False, "max_length": 8187}
    assert h.tokenizer.padded["input_ids"].shape == (3, 8192)
    rows = [json.loads(r.getMessage()) for r in caplog.records if r.name == "synthetic-rerank"]
    assert [r["state"] for r in rows] == ["start", "end"]
    # start 落在取锁/分词之前（卡死时现场也留一条），故只带批形状，不带 token 数
    assert rows[0]["batch_size"] == 3 and rows[0]["logits_to_keep"] == 1
    assert "padded_tokens" not in rows[0] and "lock_wait_ms" not in rows[0]
    assert rows[1]["padded_tokens"] == 8192 and rows[1]["batch_size"] == 3
    assert rows[1]["token_count"] < 3 * 8192
    assert rows[1]["lock_wait_ms"] >= 0 and rows[1]["tokenize_ms"] >= 0
    assert rows[1]["forward_ms"] >= 0 and rows[1]["result_count"] == 3
    assert PRIVATE not in caplog.text
