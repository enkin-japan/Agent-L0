"""local_config_fixups 金测试（Ornith 案，2026-07-29）：注册表声明式修正落盘 config.json。

守的是什么：`_apply_local_config_fixups` 按注册表条目的点分路径修正 model_dir/config.json——
声明命中即改、幂等（已是目标值零写盘）、路径不存在只警告不崩、无声明零动作；
以及 config.yaml 注册表阵容与 D.20（2026-08-16）/ D27（2026-10-31）收束决策没有漂移。
（机制的原始消费方 Ornith 已随 D.20 舍弃；机制保留，现无在册声明方。）
"""
import json
from pathlib import Path

import yaml

import framework.utils.model_selection as model_selection_mod
from framework.utils.path_resolver import _apply_local_config_fixups

_CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"


def _registry():
    cfg = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    return (cfg.get("inference") or {}).get("model_registry") or []


def _patch_options(monkeypatch, options: dict):
    monkeypatch.setattr(model_selection_mod, "model_options", lambda repo: options)


def _write_config(tmp_path: Path, data: dict) -> Path:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cfg_path


# ---- 1. 声明命中 ----

def test_fixup_applies_declared_path(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path, {"vision_config": {"model_type": "qwen3_5_vision"}})
    _patch_options(monkeypatch, {"local_config_fixups": {"vision_config.model_type": "qwen3_5"}})

    _apply_local_config_fixups("fake/repo", tmp_path)

    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["vision_config"]["model_type"] == "qwen3_5"


# ---- 2. 幂等 ----

def test_fixup_is_idempotent(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path, {"vision_config": {"model_type": "qwen3_5_vision"}})
    _patch_options(monkeypatch, {"local_config_fixups": {"vision_config.model_type": "qwen3_5"}})

    _apply_local_config_fixups("fake/repo", tmp_path)
    before = cfg_path.read_text(encoding="utf-8")
    _apply_local_config_fixups("fake/repo", tmp_path)
    after = cfg_path.read_text(encoding="utf-8")

    assert before == after


# ---- 3. 路径不存在：不崩、不改 ----

def test_fixup_missing_path_skips_without_crash(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path, {"vision_config": {"model_type": "qwen3_5_vision"}})
    before = cfg_path.read_text(encoding="utf-8")
    _patch_options(monkeypatch, {"local_config_fixups": {"no.such.path": "x"}})

    _apply_local_config_fixups("fake/repo", tmp_path)

    after = cfg_path.read_text(encoding="utf-8")
    assert before == after


# ---- 4. 无声明：零动作 ----

def test_fixup_no_declaration_is_noop(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path, {"vision_config": {"model_type": "qwen3_5_vision"}})
    before = cfg_path.read_text(encoding="utf-8")
    _patch_options(monkeypatch, {})

    _apply_local_config_fixups("fake/repo", tmp_path)

    after = cfg_path.read_text(encoding="utf-8")
    assert before == after


# ---- 5. 真 config 钉：D27 阵容收束（2026-10-31）不漂移 ----

# D27（用户 10-31 拍板）：计算端只 Colab、模型只留 Qwen3.8 27B + Gemma 4 31B，各一件 MLX
# （本机）+ 一件 CUDA 预量化件（Colab）。顺序 = 注册表声明序；revision 逐条钉死。
_ROSTER = (
    ("mlx-community/gemma-4-31b-it-4bit", "696d436c404745a59f30e4939a658162b0a9e57f"),
    ("mlx-community/Qwen3.8-27B-4bit", "3e6447f082e89cc7f0bc6e5441afd38dfce760ff"),
    ("google/gemma-4-31B-it-qat-w4a16-ct", "52f3f65bc7a02d555763bc923bd1d9094898219d"),
    ("RedHatAI/Qwen3.8-27B-INT4", "91bd022d5b49442a868bc35008f6c21e1860edfa"),
)
_DROPPED = (
    # D.20（2026-08-16）
    "mlx-community/gemma-4-e4b-it-4bit",
    "mlx-community/gemma-4-e4b-it-nvfp4",
    "mlx-community/gemma-4-12b-it-4bit",
    "mlx-community/gemma-4-12b-it-8bit",
    "mlx-community/gemma-4-26b-a4b-it-qat-4bit",
    "mlx-community/gemma-4-26b-a4b-it-nvfp4",
    "mlx-community/gemma-4-31b-it-8bit",
    "mlx-community/gemma-4-31b-it-qat-4bit",
    "mlx-community/gemma-4-31b-it-nvfp4",
    "mlx-community/Qwen3.5-9B-4bit",
    "mlx-community/Ornith-1.0-9B-4bit",
    "Qwen/Qwen3.6-35B-A3B",
    "ornith-ai/Ornith-1.0-35B",
    "sakamakismile/Ornith-1.0-35B-NVFP4",
    # D27（2026-10-31）
    "mlx-community/gemma-4-e4b-it-8bit",
    "mlx-community/gemma-4-26b-a4b-it-4bit",
    "mlx-community/gemma-4-26b-a4b-it-8bit",
    "mlx-community/Qwen3.8-27B-8bit",
    "ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit",
    "ornith-ai/Ornith-1.5-35B-A3B-MLX-6bit",
    "ornith-ai/Ornith-1.5-35B-A3B-MLX-8bit",
    "google/gemma-4-31B-it",
    "google/gemma-4-26B-A4B-it",
    "RedHatAI/gemma-4-26B-A4B-it-NVFP4",
)
_DROPPED_FRAGMENTS = ("e4b", "26b", "26B", "ornith", "NVFP4", "27B-8bit")


def _config():
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def test_registry_matches_d27_roster():
    reg = _registry()
    # 长度钉防"静默加档不触钉"
    assert len(reg) == 4, f"注册表应为 4 条（D27），实际 {len(reg)}: {[e.get('repo') for e in reg]}"
    assert [e.get("repo") for e in reg] == [repo for repo, _ in _ROSTER]
    entries = {e["repo"]: e for e in reg}
    for repo, revision in _ROSTER:
        assert entries[repo].get("revision") == revision, f"{repo} revision 漂移"
    for repo in _DROPPED:
        assert repo not in entries, f"{repo} 应已随 D.20 / D27 阵容收束删除"
    for repo in entries:
        for frag in _DROPPED_FRAGMENTS:
            assert frag.lower() not in repo.lower(), f"{repo} 含已出册片段 {frag!r}"
    # fixups 机制保留但现无在册声明方（原消费方 Ornith 已舍弃）
    declared = [r for r, e in entries.items() if e.get("local_config_fixups")]
    assert not declared, f"意外的 local_config_fixups 声明方: {declared}"


def test_d27_local_default_is_qwen_4bit_with_registry_revision():
    inf = _config()["inference"]
    assert inf["model"] == "mlx-community/Qwen3.8-27B-4bit"
    entry = next(e for e in inf["model_registry"] if e["repo"] == inf["model"])
    assert inf["model_revision"] == entry["revision"]


def test_d27_cuda_entries_are_prequantized():
    inf = _config()["inference"]
    entries = {e["repo"]: e for e in inf["model_registry"]}
    qat31 = entries["google/gemma-4-31B-it-qat-w4a16-ct"]
    cuda_qwen = entries["RedHatAI/Qwen3.8-27B-INT4"]
    for e in (qat31, cuda_qwen):
        assert e["cuda_deployment"]["prequantized"] is True   # D25：CUDA 条目一律预量化
        assert "profile" not in e                            # 未画像：路由绝不选中
        assert "speculative" not in e                        # CUDA 原生 MTP 不加载 MLX drafter
    assert cuda_qwen["cuda_deployment"]["supports_mtp"] is True
    assert cuda_qwen["dialect"] == "qwen35"
    assert qat31["dialect"] == "gemma4"
    assert qat31["capabilities"] == ["text", "vision"]
    assert inf["cuda"]["quantization"] is None               # D25：无在线量化缺省
