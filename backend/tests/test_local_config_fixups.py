"""local_config_fixups 金测试（Ornith 案，2026-07-29）：注册表声明式修正落盘 config.json。

守的是什么：`_apply_local_config_fixups` 按注册表条目的点分路径修正 model_dir/config.json——
声明命中即改、幂等（已是目标值零写盘）、路径不存在只警告不崩、无声明零动作；
以及 config.yaml 里 Ornith/Qwen3.5 两条注册表事实本身没有漂移。
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


# ---- 5. 真 config 钉：Ornith 声明 fixups，Qwen3.5 不声明 ----

def test_registry_fixups_declaration_matches_ornith_case():
    entries = {e.get("repo"): e for e in _registry()}
    ornith = entries.get("mlx-community/Ornith-1.0-9B-4bit")
    qwen35 = entries.get("mlx-community/Qwen3.5-9B-4bit")
    assert ornith is not None, "Ornith-1.0-9B-4bit 注册表条目缺失"
    assert qwen35 is not None, "Qwen3.5-9B-4bit 注册表条目缺失"

    assert ornith.get("local_config_fixups") == {"vision_config.model_type": "qwen3_5"}
    assert "local_config_fixups" not in qwen35
