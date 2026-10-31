"""model_registry capabilities 字段金测试（多模型协作接口准备②）。

守两件事：
  1. config.yaml 声明合法：每条注册表条目都有 capabilities，值域 ⊆ {text,vision,audio}
     且含 text；gemma-4 条目一律不含 audio——这是按各仓 config.json 的
     vision_config/audio_config 实测钉下的事实（家族里唯一带音频编码器的小档已随 D27
     2026-10-31 出册），不是家族推断。谁往 31b 条目里写 audio（或给新条目漏
     capabilities），这里当场红。
  2. 消费面 model_capabilities()：注册表内条目返回声明集合；表外 repo / None /
     畸形值一律回落 {"text"}（路由代码永远拿到非空、词汇受控的集合）。
"""
from pathlib import Path

import yaml

from framework.utils.model_selection import KNOWN_CAPABILITIES, model_capabilities

_CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"


def _registry():
    cfg = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    return (cfg.get("inference") or {}).get("model_registry") or []


def test_every_entry_declares_valid_capabilities():
    entries = _registry()
    assert entries, "注册表为空？config.yaml 结构变了"
    for e in entries:
        caps = e.get("capabilities")
        assert isinstance(caps, list) and caps, f"{e.get('repo')} 缺 capabilities 声明"
        assert set(caps) <= set(KNOWN_CAPABILITIES), \
            f"{e.get('repo')} capabilities 含词汇表外值: {caps}"
        assert "text" in caps, f"{e.get('repo')} capabilities 必含 text"


def test_no_registered_gemma_entry_declares_audio():
    # 实测事实钉死：在册 gemma-4 档（31b MLX 4bit / 31B QAT W4A16）的 audio_config 为 null；
    # 唯一带音频编码器的小档已随 D27 出册（见 git 历史）。
    gemma = [e for e in _registry() if "gemma-4" in e.get("repo", "")]
    assert gemma, "注册表里没有 gemma-4 条目？"
    for e in gemma:
        assert "audio" not in (e.get("capabilities") or []), \
            f"{e['repo']} 的 audio 声明与实测事实不符（在册 gemma-4 档无音频编码器）"


def test_capabilities_accessor_known_repo():
    entries = _registry()
    repo = entries[0]["repo"]
    caps = model_capabilities(repo)
    assert caps == set(entries[0]["capabilities"])
    assert "text" in caps


def test_capabilities_accessor_fallback_conservative():
    assert model_capabilities("nonexistent/repo") == {"text"}
    assert model_capabilities(None) == {"text"}
