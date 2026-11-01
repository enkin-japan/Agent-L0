"""app.py → Orchestrator 接线（W-2 / D29，首审 8）：工具披露模式必须跟所选主模型条目走。

不起服务：只调 app 的编排器工厂 `_make_orchestrator`（计算面装配期就是调它），把
MODEL_OPTIONS 换成真 config.yaml 的注册表条目，断言模式。删掉工厂里的
`model_options=MODEL_OPTIONS` → gemma4 条目落回 agent 段全局 full → 本测试红。
"""
import inspect

import pytest

from .fake_engine import FakeEngine, FakeRegistry, FakeTool


def _entry(dialect):
    from framework.utils.config_loader import load_section
    for e in load_section("inference").get("model_registry") or []:
        if e.get("dialect") == dialect:
            return e
    pytest.fail(f"注册表里没有 {dialect} 条目")


@pytest.mark.parametrize("dialect, expected", [("gemma4", "catalog"), ("qwen35", "full")])
def test_app_passes_selected_entry_mode_to_orchestrator(monkeypatch, dialect, expected):
    import app as entry          # 惰性导入：不在收集期触发 app.py 的装配副作用
    monkeypatch.setattr(entry, "MODEL_OPTIONS", _entry(dialect))
    orch = entry._make_orchestrator(FakeEngine([]), FakeRegistry([FakeTool("edit_file")]))
    assert orch.tool_schema_mode == expected


def test_compute_assembly_builds_orchestrator_via_factory():
    """装配路径确实走工厂（防止调用处回退成不带 model_options 的裸构造）。"""
    import app as entry
    src = inspect.getsource(entry)
    assert src.count("Orchestrator(") == 1                 # 唯一构造点在工厂里
    assert "orchestrator = _make_orchestrator(engine, mcp_registry)" in src
