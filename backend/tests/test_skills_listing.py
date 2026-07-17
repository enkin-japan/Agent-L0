"""GET /api/v1/skills 金测试（斜杠候选提示的数据源，2026-07-17）。

守两条：①listing 含被探活舍弃的剧本（灰显 + missing 缺什么），description 不丢——
「凭空消失」不可解释，用户装完依赖后点名还能触发前置重扫；②端点已注册且在
剧本表未装配时返回空清单而非 500。
"""
import asyncio

from framework.core.skill_registry import SkillRegistry


_OK = """---
name: demo
description: 一份示范剧本
---
Body line one.
"""

_NEEDY = """---
name: needy
description: 依赖 LibreOffice 的剧本
requires: [deck_export_pdf]
---
Do the export.
"""


def _registry(tmp_path, tool_names=None):
    (tmp_path / "demo.md").write_text(_OK, encoding="utf-8")
    (tmp_path / "needy.md").write_text(_NEEDY, encoding="utf-8")
    return SkillRegistry(tmp_path, tool_names=tool_names or set())


def test_listing_includes_unavailable_with_desc_and_missing(tmp_path):
    reg = _registry(tmp_path)
    listing = reg.listing()
    by_name = {s["name"]: s for s in listing}
    assert by_name["demo"] == {"name": "demo", "description": "一份示范剧本",
                               "available": True, "missing": []}
    assert by_name["needy"]["available"] is False
    assert by_name["needy"]["missing"] == ["deck_export_pdf"]
    # 描述留档：灰显行也得让用户知道这剧本是干嘛的
    assert by_name["needy"]["description"] == "依赖 LibreOffice 的剧本"


def test_listing_all_available_when_deps_met(tmp_path):
    reg = _registry(tmp_path, tool_names={"deck_export_pdf"})
    assert all(s["available"] for s in reg.listing())
    # catalog（R2a 错误提示用）形状不变，只列可用者
    assert {s["name"] for s in reg.catalog()} == {"demo", "needy"}


def test_skills_endpoint_registered_and_safe_without_registry():
    from framework.agents import runtime
    from framework.api.routes import router, list_skills

    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/api/v1/skills" in paths

    old = runtime.get_skill_registry()
    try:
        runtime.set_skill_registry(None)
        res = asyncio.run(list_skills(request=None))
        assert res == {"status": True, "skills": []}
    finally:
        runtime.set_skill_registry(old)


def test_skills_endpoint_serves_listing(tmp_path):
    from framework.agents import runtime
    from framework.api.routes import list_skills

    old = runtime.get_skill_registry()
    try:
        runtime.set_skill_registry(_registry(tmp_path))
        res = asyncio.run(list_skills(request=None))
        assert res["status"] is True
        assert {s["name"] for s in res["skills"]} == {"demo", "needy"}
    finally:
        runtime.set_skill_registry(old)
