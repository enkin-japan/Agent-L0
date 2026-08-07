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
"""
from pathlib import Path

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
    assert cuda.get("quantization") == "fp8"
    assert cuda.get("enable_prefix_caching") is True
    reg = {e["repo"]: e for e in inf["model_registry"]}
    for repo, dialect in (("google/gemma-4-31B-it", "gemma4"),
                          ("google/gemma-4-26B-A4B-it", "gemma4"),
                          ("Qwen/Qwen3.6-35B-A3B", "qwen35"),
                          ("ornith-ai/Ornith-1.0-35B", "qwen35")):
        assert repo in reg, f"CUDA 档 {repo} 未注册（表外拒启）"
        assert reg[repo]["dialect"] == dialect
        # 画像回填后（model_eval 2026-08-08）：条目必须携真实画像且钉死 revision
        # ——profile 缺失=路由绝不优先选中，回填即解锁；数值来自 A100 实测非拍脑袋
        prof = reg[repo].get("profile")
        assert isinstance(prof, dict), f"{repo} 画像缺失（评测已回填，不应回退）"
        for k in ("tool_call", "longchain", "tps"):
            assert isinstance(prof.get(k), (int, float)), f"{repo} 画像缺 {k}"
        assert reg[repo].get("revision"), f"{repo} revision 未钉死（常驻纪律）"


# ---- ⑤ server 环境覆盖锚点 ----

def test_server_supports_infer_backend_env_override():
    src = (REPO_ROOT / "inference" / "server.py").read_text(encoding="utf-8")
    assert 'os.environ.get("INFER_BACKEND"' in src


# ---- ⑥ structural-tag 迁移（2026-08-07：vLLM 0.26 拒收 per-request logits_processors）----

def test_structural_tag_json_shape():
    """声明式约束 → vLLM structural_tag 规格：begin/schema/end + triggers 锚形状。"""
    import json
    from framework.backends.cuda.backend import structural_tag_json
    spec = {"begin": "<|tool_call>", "end": "<tool_call|>",
            "schema": {"type": "object", "properties": {"name": {"type": "string"}}}}
    tag = json.loads(structural_tag_json(spec))
    assert tag["triggers"] == ["<|tool_call>"]
    assert tag["structures"][0]["begin"] == "<|tool_call>"
    assert tag["structures"][0]["end"] == "<tool_call|>"
    assert tag["structures"][0]["schema"]["type"] == "object"


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
