"""对话式注册金测试（D.18 完整版，DEFERRED §4，2026-08-09）。

守四条：
① 红线①物理面——两个注册工具的参数 schema **没有**密钥字段；声明里形似密钥
  实值（鉴权头/常见前缀）在提交口拒绝。
② 红线②状态机——工具提交只落 pending 区，生效目录（api_tools/、skills/）
  确认前空无一物；确认走表单面同一条保存路，成功后销单；驳回即删。
③ 撞名不静默顶替（仓库手写声明 / 内置剧本）。
④ 注册单 id 即文件名——非法 id 拒绝（路径逃逸）。
"""
import asyncio
from types import SimpleNamespace

import pytest

from framework.core import pending_registrations as pending
from framework.mcp.tools.register_tools import RegisterApiToolTool, RegisterSkillTool


# ---------------- ① 红线①：密钥进不了对话式声明 ----------------

def test_tools_have_no_secret_parameter():
    for tool in (RegisterApiToolTool(), RegisterSkillTool()):
        assert "secret" not in tool.parameters["properties"]
        assert tool.requires_approval is True   # 第一道闸：调用即审


def test_inline_secret_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    t = RegisterApiToolTool()
    out = t.execute(name="leaky", description="d", endpoint="https://x.example/api",
                    headers={"Authorization": "Bearer abc"})
    assert "已拒绝" in out and "auth.env" in out
    out = t.execute(name="leaky2", description="d", endpoint="https://x.example/api",
                    headers={"X-Trace": "sk-abcdefghijklmnop"})
    assert "已拒绝" in out
    assert not (tmp_path / "registrations").exists() or \
        not list((tmp_path / "registrations").glob("*.yaml"))


def test_find_inline_secret_clean_decl_passes():
    assert pending.find_inline_secret(
        {"name": "ok", "endpoint": "https://x.example/api",
         "headers": {"Accept": "application/json"},
         "auth": {"env": "WEATHER_API_KEY"}}) is None


# ---------------- ② 红线②：pending 不生效，确认才落地 ----------------

def test_api_tool_submit_lands_pending_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from framework.mcp.tools import api_tool_factory as factory
    out = RegisterApiToolTool().execute(
        name="weather_now", description="查当前天气",
        endpoint="https://api.example.com/now",
        params={"properties": {"city": {"type": "string"}}, "required": ["city"]},
        auth_env="WEATHER_API_KEY", note="用户想要天气")
    assert "待确认" in out and "WEATHER_API_KEY" in out
    rows = pending.list_pending()
    assert [r["id"] for r in rows] == ["api_tool-weather_now"]
    assert rows[0]["payload"]["auth"] == {"env": "WEATHER_API_KEY"}
    # 生效目录确认前空无一物（红线②的可观测事实）
    assert not list(factory.user_decl_dir().glob("*.yaml"))


def _stub_request(tmp_path):
    """confirm 路径的最小 app.state 假体：注册表收下工具、编排器刷新声明。"""
    registry = SimpleNamespace(tool_names=lambda: set(),
                               discard_unavailable=lambda name: None,
                               queue_tool=lambda tool: None,
                               load_tool=lambda tool, **kwargs: True)
    orchestrator = SimpleNamespace(refresh_tools_declaration=lambda: None)
    state = SimpleNamespace(mcp_registry=registry, orchestrator=orchestrator,
                            rebuild_skills=None)
    # getattr(state, "rebuild_skills", None) → None：剧本确认走 saved_norebuild
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_confirm_api_tool_saves_and_clears(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from framework.api.routes import confirm_registration
    from framework.mcp.tools import api_tool_factory as factory
    RegisterApiToolTool().execute(
        name="weather_now", description="查当前天气",
        endpoint="https://api.example.com/now")
    result = asyncio.run(confirm_registration("api_tool-weather_now",
                                              _stub_request(tmp_path)))
    assert result["effect"] == "registered" and result["id"] == "api_tool-weather_now"
    assert (factory.user_decl_dir() / "weather_now.yaml").exists()
    assert pending.list_pending() == []          # 销单


def test_confirm_skill_saves_and_clears(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from framework.api.routes import confirm_registration
    RegisterSkillTool().execute(name="weekly-report", description="周报剧本",
                                body="1. 汇总本周笔记\n2. 产出周报文档",
                                requires=["write_document"])
    result = asyncio.run(confirm_registration("skill-weekly-report",
                                              _stub_request(tmp_path)))
    assert result["effect"] == "saved_norebuild"
    assert (tmp_path / "skills" / "weekly-report.md").exists()
    assert pending.list_pending() == []


def test_reject_removes_pending(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from framework.api.routes import reject_registration
    RegisterSkillTool().execute(name="tmp-skill", description="d", body="1. x")
    result = asyncio.run(reject_registration("skill-tmp-skill", None))
    assert result["effect"] == "rejected"
    assert pending.list_pending() == []


def test_confirm_missing_id_404(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from fastapi import HTTPException
    from framework.api.routes import confirm_registration
    with pytest.raises(HTTPException) as e:
        asyncio.run(confirm_registration("api_tool-nope", _stub_request(tmp_path)))
    assert e.value.status_code == 404


# ---------------- ③ 撞名不静默顶替 ----------------

def test_api_tool_name_conflict_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from framework.mcp.tools import api_tool_factory as factory
    d = factory.user_decl_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "weather_now.yaml").write_text(
        "name: weather_now\ndescription: 已有\nendpoint: https://a.example/x\n",
        encoding="utf-8")
    out = RegisterApiToolTool().execute(
        name="weather_now", description="d", endpoint="https://api.example.com/now")
    assert "已拒绝" in out and "已存在" in out


def test_skill_repo_name_conflict_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from framework.agents import runtime
    from framework.core.skill_registry import SkillRegistry
    (tmp_path / "demo.md").write_text(
        "---\nname: demo\ndescription: 内置\n---\nBody.", encoding="utf-8")
    old = runtime.get_skill_registry()
    runtime.set_skill_registry(SkillRegistry(tmp_path, tool_names=set()))
    try:
        out = RegisterSkillTool().execute(name="demo", description="d", body="1. x")
        assert "已拒绝" in out and "重名" in out
    finally:
        runtime.set_skill_registry(old)


# ---------------- ④ id 即文件名：非法 id 拒绝 ----------------

def test_bad_rid_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        pending.get("../escape")
    from fastapi import HTTPException
    from framework.api.routes import reject_registration
    with pytest.raises(HTTPException) as e:
        asyncio.run(reject_registration("../escape", None))
    assert e.value.status_code == 400


def test_registration_routes_registered():
    from framework.api.routes import router
    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/api/v1/registrations" in paths
    assert "/api/v1/registrations/{rid}/confirm" in paths
    assert "/api/v1/registrations/{rid}" in paths
