"""backends/mlx/patch/text_only_vlm.py 金测试（CPU-only，不装载任何真权重）。

覆盖：纯文本判定（白名单 × 模态键三态）、模块代理的跳塔子类、处理器垫片的
属性语义、后端装载入口确实经由本补丁（import 钉扎）。
"""
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.backends.mlx.patch import text_only_vlm as tov  # noqa: E402


def _write_cfg(tmp_path: Path, **cfg) -> Path:
    d = tmp_path / "m"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return d


# ── is_text_only_dir ────────────────────────────────────────────────────

@pytest.mark.parametrize("cfg,expected", [
    ({"model_type": "qwen3_5_moe", "vision_config": None}, True),
    ({"model_type": "qwen3_5_moe"}, True),                       # 键缺失 = 无
    ({"model_type": "qwen3_5", "vision_config": {}}, True),      # {} = 无（与上游同口径）
    ({"model_type": "qwen3_5_moe", "vision_config": {"depth": 27}}, False),
    ({"model_type": "qwen3_5_moe", "vision_config": None, "audio_config": {"x": 1}}, False),
    ({"model_type": "qwen3_5_moe", "vision_config": None, "dflash_config": {"x": 1}}, False),
    ({"model_type": "gemma4", "vision_config": None}, False),    # 白名单外不接
    ({"model_type": "Qwen3_5_MoE", "vision_config": None}, True),  # 大小写不敏感
])
def test_is_text_only_dir(tmp_path, cfg, expected):
    assert tov.is_text_only_dir(_write_cfg(tmp_path, **cfg)) is expected


def test_is_text_only_config_shared_predicate():
    assert tov.is_text_only_config({"model_type": "qwen3_5_moe"}) is True
    assert tov.is_text_only_config({"model_type": "qwen3_5_moe", "vision_config": {"d": 1}}) is False
    assert tov.is_text_only_config(None) is False
    assert tov.is_text_only_dir(None) is False


def test_is_text_only_dir_without_config_or_bad_json(tmp_path):
    assert tov.is_text_only_dir(tmp_path) is False
    d = tmp_path / "bad"
    d.mkdir()
    (d / "config.json").write_text("{not json", encoding="utf-8")
    assert tov.is_text_only_dir(d) is False


# ── _ArchShim ───────────────────────────────────────────────────────────

def _fake_arch():
    import mlx.nn as nn

    calls = []

    class VisionModel(nn.Module):
        def __init__(self, cfg):
            super().__init__()
            calls.append(("vision", cfg))

    class LanguageModel(nn.Module):
        def __init__(self, text_cfg, cfg):
            super().__init__()
            calls.append(("lm", text_cfg, cfg))

    class Model(nn.Module):
        def __init__(self, config):
            nn.Module.__init__(self)
            self.config = config
            self.vision_tower = VisionModel(config.vision_config)
            self.language_model = LanguageModel(config.text_config, config)

        def sanitize(self, weights):
            return {"sanitized": True, **weights}

    mod = types.SimpleNamespace(Model=Model, LanguageModel=LanguageModel,
                                VisionModel=VisionModel, ModelConfig="MC", TextConfig="TC")
    return mod, calls


def test_arch_shim_model_skips_vision_tower_and_keeps_rest():
    mod, calls = _fake_arch()
    shim = tov._ArchShim(mod)
    cfg = types.SimpleNamespace(vision_config="VC", text_config="TXT")
    m = shim.Model(cfg)
    assert m.vision_tower is None
    assert [c[0] for c in calls] == ["lm"]          # 视觉塔构造从未被调用
    assert calls[0][1] == "TXT" and calls[0][2] is cfg
    assert isinstance(m, mod.Model)                 # 仍是原类子类：sanitize/量化谓词沿用
    assert m.sanitize({"a": 1}) == {"sanitized": True, "a": 1}
    assert shim.Model.__name__ == "ModelTextOnly"
    # 其余属性透传原模块
    assert shim.ModelConfig == "MC" and shim.TextConfig == "TC" and shim.VisionModel is mod.VisionModel


def test_text_only_arch_context_patches_only_supported_text_only_and_restores(monkeypatch):
    U = pytest.importorskip("mlx_vlm.utils")
    orig = U.get_model_and_args
    mod, _ = _fake_arch()

    def fake_get(config):
        return mod, config["model_type"]

    monkeypatch.setattr(U, "get_model_and_args", fake_get)
    with tov._text_only_arch():
        assert U.get_model_and_args is not fake_get
        arch, mt = U.get_model_and_args({"model_type": "qwen3_5_moe", "vision_config": None})
        assert isinstance(arch, tov._ArchShim) and mt == "qwen3_5_moe"
        arch2, _ = U.get_model_and_args({"model_type": "qwen3_5_moe", "vision_config": {"d": 1}})
        assert arch2 is mod                          # 真 VLM 件：原样
        arch3, _ = U.get_model_and_args({"model_type": "gemma4", "vision_config": None})
        assert arch3 is mod                          # 白名单外：原样
    assert U.get_model_and_args is fake_get          # 退出即复原


def test_text_only_arch_is_serialized_by_lock():
    """重入/并发装载不会把补丁永久留在模块里：第二个上下文必须等第一个退出。"""
    import threading
    U = pytest.importorskip("mlx_vlm.utils")
    before = U.get_model_and_args
    entered = threading.Event()
    release = threading.Event()
    seen = {}

    def holder():
        with tov._text_only_arch():
            entered.set()
            release.wait(5)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert entered.wait(5)
    assert tov._PATCH_LOCK.locked()
    assert not tov._PATCH_LOCK.acquire(timeout=0.2)   # 第二个进入者被挡
    release.set()
    t.join(5)
    assert U.get_model_and_args is before


def test_text_only_arch_restores_on_exception(monkeypatch):
    U = pytest.importorskip("mlx_vlm.utils")
    before = U.get_model_and_args
    with pytest.raises(RuntimeError):
        with tov._text_only_arch():
            raise RuntimeError("boom")
    assert U.get_model_and_args is before


# ── TextOnlyProcessor ───────────────────────────────────────────────────

class _Tok:
    eos_token = "<eos>"
    eos_token_id = 7
    pad_token = None

    def __call__(self, prompts, **kw):
        return ("encoded", prompts, kw)

    def encode(self, s, add_special_tokens=False):
        return list(s)

    def __len__(self):          # 真 HF 分词器定义 __len__（词表大小）
        return 99


def test_processor_shim_semantics():
    tok = _Tok()
    p = tov.TextOnlyProcessor(tok)
    assert p.tokenizer is tok                        # 引擎 processor.tokenizer.encode 路
    assert p.image_processor is None                 # 显式占位，不透传到分词器
    assert p.detokenizer is None
    assert p("hi", padding=True) == ("encoded", "hi", {"padding": True})
    assert p.eos_token == "<eos>"                    # 透传
    assert p.tokenizer.encode("ab") == ["a", "b"]
    with pytest.raises(AttributeError):
        _ = p.no_such_attr
    # dunder 不透传：分词器有 __len__，垫片上 len() 必须不可用（去掉守卫这条就绿不了）
    with pytest.raises(TypeError):
        len(p)
    assert not hasattr(p, "__len__")
    # 可赋值覆盖（load_text_only 给 detokenizer 赋值即走这条）
    p.detokenizer = "D"
    assert p.detokenizer == "D"


def test_processor_shim_survives_half_initialized_instance_and_deepcopy():
    import copy
    p = tov.TextOnlyProcessor(_Tok())
    q = copy.deepcopy(p)                 # batch_lane 对 backend.processor 做 deepcopy
    assert isinstance(q.tokenizer, _Tok) and q.image_processor is None
    bare = tov.TextOnlyProcessor.__new__(tov.TextOnlyProcessor)   # __init__ 未跑
    with pytest.raises(AttributeError):  # 不得无限递归
        _ = bare.eos_token


# ── load_text_only 本体（假 load_model + 桩分词器，不碰权重） ────────────────

class _StubDetok:
    def __init__(self, tok):
        self.tok = tok


def _fake_load_text_only(monkeypatch, tmp_path, *, model_eos, tok_eos_ids=None, tok_eos_id=None):
    import mlx_vlm.utils as U
    import mlx_vlm.tokenizer_utils as TU
    import transformers
    d = _write_cfg(tmp_path, model_type="qwen3_5_moe")
    calls = {}

    class _Model:
        config = types.SimpleNamespace(eos_token_id=model_eos)

    def fake_load_model(path, **kw):
        calls["load_model"] = (path, kw)
        calls["patched_during_load"] = U.get_model_and_args is not calls["orig"]
        return _Model()

    class _HfTok(_Tok):
        eos_token_ids = tok_eos_ids
        eos_token_id = tok_eos_id

    calls["orig"] = U.get_model_and_args
    monkeypatch.setattr(U, "load_model", fake_load_model)
    def fake_sc(eos, tok):
        calls["eos"] = eos
        return ("SC", eos)

    monkeypatch.setattr(U, "StoppingCriteria", fake_sc)
    monkeypatch.setattr(TU, "load_tokenizer", lambda path, return_tokenizer=True: _StubDetok)
    monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained",
                        classmethod(lambda cls, path, **kw: _HfTok()))
    return d, calls


def test_load_text_only_wires_detokenizer_and_eos_from_model_config(monkeypatch, tmp_path):
    import mlx_vlm.utils as U
    d, calls = _fake_load_text_only(monkeypatch, tmp_path, model_eos=[248046, 248044])
    model, proc = tov.load_text_only(d)
    assert calls["patched_during_load"] is True          # 替换只在装载窗口内生效
    assert U.get_model_and_args is calls["orig"]         # 窗口外已复原
    assert calls["load_model"][0] == Path(d) and calls["load_model"][1] == {}
    assert isinstance(proc, tov.TextOnlyProcessor)
    assert isinstance(proc.detokenizer, _StubDetok) and proc.detokenizer.tok is proc.tokenizer
    assert calls["eos"] == [248046, 248044]              # 列表原样透传，不退化成单值
    assert proc.tokenizer.stopping_criteria == ("SC", [248046, 248044])


def test_load_text_only_eos_gradient_and_failure(monkeypatch, tmp_path):
    d, calls = _fake_load_text_only(monkeypatch, tmp_path, model_eos=[], tok_eos_ids=[5, 6], tok_eos_id=5)
    tov.load_text_only(d)
    assert calls["eos"] == [5, 6]                        # 空列表跳过 → eos_token_ids 梯
    d2, calls2 = _fake_load_text_only(monkeypatch, tmp_path / "b", model_eos=None, tok_eos_id=9)
    tov.load_text_only(d2)
    assert calls2["eos"] == 9
    d3, _ = _fake_load_text_only(monkeypatch, tmp_path / "c", model_eos=None)
    with pytest.raises(ValueError, match="eos_token_id"):
        tov.load_text_only(d3)


def test_load_text_only_rejects_unsupported_kwargs(monkeypatch, tmp_path):
    d, calls = _fake_load_text_only(monkeypatch, tmp_path, model_eos=1)
    with pytest.raises(ValueError, match="adapter_path"):
        tov.load_text_only(d, adapter_path="lora")
    assert "load_model" not in calls                     # 拒在装载之前
    tov.load_text_only(d, adapter_path=None, revision=None, lazy=True)
    assert calls["load_model"][1] == {"lazy": True}      # None 剥掉、其余透传


# ── 后端接线钉扎 ────────────────────────────────────────────────────────

def test_backend_load_goes_through_patch():
    src = (Path(__file__).resolve().parents[1]
           / "framework/backends/mlx/backend.py").read_text(encoding="utf-8")
    assert "from .patch.text_only_vlm import load_model_and_processor as _vlm_load" in src
    assert "from mlx_vlm.utils import load as _vlm_load" not in src


def test_load_model_and_processor_dispatch(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(tov, "load_text_only", lambda p, **kw: seen.append(("text", p)) or ("m", "p"))
    import mlx_vlm.utils as U
    monkeypatch.setattr(U, "load", lambda p, **kw: seen.append(("vlm", p)) or ("M", "P"))
    d_text = _write_cfg(tmp_path, model_type="qwen3_5_moe", vision_config=None)
    assert tov.load_model_and_processor(d_text) == ("m", "p")
    d_vlm = tmp_path / "v"
    d_vlm.mkdir()
    (d_vlm / "config.json").write_text(json.dumps({"model_type": "qwen3_5_moe",
                                                   "vision_config": {"depth": 27}}))
    assert tov.load_model_and_processor(d_vlm) == ("M", "P")
    assert [s[0] for s in seen] == ["text", "vlm"]


# ── 注册表 ──────────────────────────────────────────────────────────────

def test_registry_ornith15_entries_are_text_only_qwen35():
    from framework.utils.config_loader import load_section
    reg = {e["repo"]: e for e in load_section("inference")["model_registry"]}
    for q in ("4bit", "6bit", "8bit"):
        e = reg[f"ornith-ai/Ornith-1.5-35B-A3B-MLX-{q}"]
        assert e["dialect"] == "qwen35"
        assert e["capabilities"] == ["text"], "纯文本件不得声明 vision——路由会派图过去"
        assert len(e["revision"]) == 40 and all(c in "0123456789abcdef" for c in e["revision"])
        assert "profile" not in e, "未画像前不得带 profile（路由优先选中纪律）"
