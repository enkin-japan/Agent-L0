"""model_registry capabilities 字段金测试（多模型协作接口准备②）。

守两件事：
  1. config.yaml 声明合法：每条注册表条目都有 capabilities，值域 ⊆ {text,vision,audio}
     且含 text；e4b 档含 audio、26b/31b 档不含——这是按各仓 config.json 的
     vision_config/audio_config 实测钉下的事实（e4b 独有音频编码器），不是家族推断。
     谁往 26b 条目里写 audio（或给新条目漏 capabilities），这里当场红。
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


def test_audio_is_e4b_only_within_gemma_family():
    # 实测事实钉死：gemma-4 家族里音频编码器是 e4b 独有（audio_config: gemma4_audio；
    # 26b/31b 的 audio_config 为 null）。
    for e in _registry():
        repo = e.get("repo", "")
        if "gemma-4" not in repo:
            continue
        has_audio = "audio" in (e.get("capabilities") or [])
        assert has_audio == ("-e4b-" in repo), \
            f"{repo} 的 audio 声明与实测事实不符（e4b 独有音频编码器）"


def test_capabilities_accessor_known_repo():
    entries = _registry()
    repo = entries[0]["repo"]
    caps = model_capabilities(repo)
    assert caps == set(entries[0]["capabilities"])
    assert "text" in caps


def test_capabilities_accessor_fallback_conservative():
    assert model_capabilities("nonexistent/repo") == {"text"}
    assert model_capabilities(None) == {"text"}
