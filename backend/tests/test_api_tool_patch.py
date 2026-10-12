"""Editing one field must not silently reset a private tool's authority."""
import asyncio
from types import SimpleNamespace

import pytest
import yaml


def test_edit_secret_or_timeout_preserves_complete_declaration(tmp_path, monkeypatch):
    from framework.api.routes import patch_api_tool
    from framework.api.schemas import ApiToolPatchRequest
    from framework.mcp.tools import api_tool_factory as factory
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    decl = {
        "name": "private_get", "description": "fixture", "endpoint": "https://fixture.invalid/data",
        "method": "GET", "requires_approval": True, "headers": {"X-Trace": "fixture"},
        "auth": {"env": "FIXTURE_ONLY_API_KEY", "format": "Token {}"},
        "params": {"properties": {"filter": {"type": "object", "additionalProperties": False,
                   "properties": {"tag": {"enum": ["a", "b"]}}}}, "additionalProperties": False},
        "canary": {"arguments": {"filter": {"tag": "a"}}}, "future_metadata": {"keep": True},
    }
    user_dir = factory.user_decl_dir()
    user_dir.mkdir(parents=True)
    path = user_dir / "private_get.yaml"
    path.write_text(yaml.safe_dump(decl))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        mcp_registry=SimpleNamespace(tool_names=lambda: {"private_get"}, discard_unavailable=lambda name: None))))
    secrets = []
    monkeypatch.setattr(factory, "write_secret", lambda name, value: secrets.append((name, value)))
    result = asyncio.run(patch_api_tool("private_get", request,
                         ApiToolPatchRequest(secret="synthetic-secret", timeout=23)))
    assert result["effect"] == "restart_required"
    saved = yaml.safe_load(path.read_text())
    assert saved == {**decl, "timeout": 23}
    assert secrets == [("FIXTURE_ONLY_API_KEY", "synthetic-secret")]
    assert factory.synth_class(saved, "synthetic")().requires_approval is True
    asyncio.run(patch_api_tool("private_get", request, ApiToolPatchRequest(requires_approval=None)))
    cleared = yaml.safe_load(path.read_text())
    assert "requires_approval" not in cleared
    assert cleared["headers"] == decl["headers"]
    assert factory.synth_class(cleared, "synthetic")().requires_approval is False


def test_patch_missing_tool_never_creates_or_writes_secret(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from framework.api.routes import patch_api_tool
    from framework.api.schemas import ApiToolPatchRequest
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(patch_api_tool("missing", None, ApiToolPatchRequest(secret="synthetic")))
    assert exc.value.status_code == 404
    assert not list(tmp_path.rglob("*.yaml"))


def test_patch_cannot_rename_or_accept_unknown_fields():
    from pydantic import ValidationError
    from framework.api.schemas import ApiToolPatchRequest
    for values in ({"name": "other"}, {"unknown": True}):
        with pytest.raises(ValidationError):
            ApiToolPatchRequest(**values)


@pytest.mark.parametrize("raw", [None, True, False, 1, 0, "false", "", {}, {"legacy": True}])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_list_normalizes_legacy_approval_override(raw, method, monkeypatch):
    from framework.api import routes
    from framework.mcp.tools import api_tool_factory as factory
    decl = {"name": "synthetic_tool", "description": "fixture", "endpoint": "https://fixture.invalid",
            "method": method, "requires_approval": raw}
    assert factory._validate(decl) is None
    entry = {"file": "fixture.yaml", "source": "user", "problem": None, "decl": decl}
    monkeypatch.setattr(routes, "_atf", lambda: SimpleNamespace(
        DECL_DIR="repo", user_decl_dir=lambda: "user", _MUTATING=factory._MUTATING,
        load_declarations=lambda directory, source: [entry] if source == "user" else []))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        mcp_registry=SimpleNamespace(tool_names=lambda: []))))
    item = asyncio.run(routes.list_api_tools(request))["tools"][0]
    assert item["approval_override"] is (None if raw is None else bool(raw))
    assert item["requires_approval"] is factory.synth_class(decl, "synthetic")().requires_approval
