"""local_config_fixups 金测试（Ornith 案，2026-07-29）：注册表声明式修正落盘 config.json。

守的是什么：`_apply_local_config_fixups` 按注册表条目的点分路径修正 model_dir/config.json——
声明命中即改、幂等（已是目标值零写盘）、路径不存在只警告不崩、无声明零动作；
以及 config.yaml 注册表阵容与 D.20 收束决策（2026-08-16）没有漂移。
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


# ---- 5. 真 config 钉：D.20 阵容收束（2026-08-16）不漂移 ----

_KEPT_MLX = (
    "mlx-community/gemma-4-e4b-it-8bit",
    "mlx-community/gemma-4-26b-a4b-it-4bit",
    "mlx-community/gemma-4-26b-a4b-it-8bit",
    "mlx-community/gemma-4-31b-it-4bit",
    "mlx-community/Qwen3.8-27B-8bit",
    "mlx-community/Qwen3.8-27B-4bit",
)
_DROPPED = (
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
)


# 2026-08-24 入册评测的 Ornith-1.5-35B-A3B 三件 MLX 量化（纯文本件，dialect qwen35）。
# 与 D.20 舍弃的 Ornith-1.0 无关：1.0 条目当年靠 local_config_fixups 修 vision
# model_type，1.5 的 MLX 件没有视觉塔，走 backends/mlx/patch/text_only_vlm.py。
_ADDED_0824 = (
    "ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit",
    "ornith-ai/Ornith-1.5-35B-A3B-MLX-6bit",
    "ornith-ai/Ornith-1.5-35B-A3B-MLX-8bit",
)


def test_registry_matches_d20_roster():
    entries = {e.get("repo"): e for e in _registry()}
    # 6 MLX 保留档 + 3 CUDA 部署形态 = 9（D.20）+ 3 Ornith-1.5（08-24）= 12；
    # 长度钉防"静默加档不触钉"
    assert len(entries) == 12, f"注册表应为 12 条（D.20 9 + 08-24 3），实际 {len(entries)}: {sorted(entries)}"
    for repo in _KEPT_MLX + _ADDED_0824:
        assert repo in entries, f"{repo} 注册表条目缺失（D.20 保留档 / 08-24 入册）"
    for repo in _DROPPED:
        assert repo not in entries, f"{repo} 应已随 D.20 阵容收束删除"
    # fixups 机制保留但现无在册声明方（原消费方 Ornith 已舍弃）
    declared = [r for r, e in entries.items() if e.get("local_config_fixups")]
    assert not declared, f"意外的 local_config_fixups 声明方: {declared}"
