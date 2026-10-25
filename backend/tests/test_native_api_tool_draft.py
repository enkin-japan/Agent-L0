"""Run the production native form mapper with synthetic declarations."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"),
                                reason="requires Swift toolchain")


@pytest.fixture(scope="module")
def native_draft(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("api-tool-draft")
    source = ROOT / "apple-app/Sources"
    panel = (source / "Views/ApiToolsPanelView.swift").read_text()
    draft = panel[panel.index("struct ApiParamRow:"):panel.index("// MARK: - 面板")]
    client = (source / "API/AgentClient.swift").read_text()
    body = client[client.index("    struct ApiToolSaveBody:"):client.index("    func listApiTools()")]
    mapped = tmp / "Draft.swift"
    mapped.write_text("import Foundation\nenum AgentClient {\n" + body + "}\n" + draft)
    binary = tmp / "draft"
    result = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(tmp / "modules"),
        *[str(source / "API" / name) for name in ("JSONValue.swift", "ProtocolModels.swift", "Models.swift")],
        *map(str,sorted((source / "Store").glob("Offline*.swift"))), str(source / "Store/TurnReducer.swift"),
        str(mapped), str(ROOT / "apple-app/Tests/api_tool_draft_test.swift"), "-o", str(binary)],
        capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    schema = {"properties": {"filter": {"type": "object", "additionalProperties": False,
              "properties": {"tag": {"enum": ["a", "b"]}}}, "z_opaque": False}, "additionalProperties": False}
    fixture = tmp / "item.json"
    fixture.write_text(json.dumps({"file": "private_get.yaml", "source": "user", "name": "private_get",
        "description": "fixture", "endpoint": "https://fixture.invalid/data", "method": "GET",
        "enabled": True, "params": schema, "headers": {"X-Trace": "fixture"}, "timeout": 9,
        "auth_env": "FIXTURE_ONLY_API_KEY", "has_secret": True, "requires_approval": True,
        "approval_override": True, "has_canary": True, "registered": True}))
    def run(mode):
        current_fixture = fixture
        if mode in {"empty-description", "false-description"}:
            item = json.loads(fixture.read_text())
            item["params"] = {"properties": {"filter": {} if mode == "empty-description" else False}}
            current_fixture = tmp / (mode + ".json")
            current_fixture.write_text(json.dumps(item))
        result = subprocess.run([str(binary), str(current_fixture), mode], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    return run, schema


@pytest.mark.parametrize("mode, expected", [("none", {}), ("key", {"secret": "synthetic-secret"}),
    ("timeout", {"timeout": 23}), ("approval-default", {"requires_approval": None})])
def test_only_edited_fields_are_sent(native_draft, mode, expected):
    run, _ = native_draft
    assert run(mode) == expected


def test_parameter_description_preserves_hidden_schema(native_draft):
    run, schema = native_draft
    patch = run("parameter-description")
    assert set(patch) == {"params"}
    original = schema["properties"]["filter"]
    assert patch["params"]["properties"]["filter"] == {**original, "description": "updated description"}
    assert patch["params"]["additionalProperties"] is False
    assert patch["params"]["properties"]["z_opaque"] is False


def test_editing_empty_schema_description_does_not_add_type(native_draft):
    run, _ = native_draft
    assert run("empty-description")["params"]["properties"]["filter"] == {"description": "updated description"}


def test_unmodeled_schema_cannot_be_rewritten_by_form_fields(native_draft):
    run, _ = native_draft
    assert run("false-description") == {}
