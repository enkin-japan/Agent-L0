"""Offline copies include inputs, but cannot escape the session directory."""
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from framework.api.auth import verify_api_key
from framework.api.offline_files import router


@pytest.fixture
def archive(tmp_path):
    app = FastAPI()
    app.state.api_key = "fixture-only"
    app.state.auth_devices = {}
    app.state.session_manager = SimpleNamespace(sessions_dir=tmp_path / "sessions")
    app.include_router(router, dependencies=[Depends(verify_api_key)])
    session = tmp_path / "sessions" / "sample"
    for kind in ("assets", "image", "audio", "attach"):
        (session / kind).mkdir(parents=True)
        (session / kind / "sample.txt").write_text(kind)
    return TestClient(app), session


def test_manifest_includes_input_files_and_requires_auth(archive):
    client, session = archive
    assert client.get("/api/v1/chat/sample/offline-files").status_code == 401
    client.headers["Authorization"] = "Bearer fixture-only"
    data = client.get("/api/v1/chat/sample/offline-files").json()
    assert {f["kind"] for f in data["files"]} == {"assets", "image", "audio", "attach"}
    for item in data["files"]:
        response = client.get(item["path"])
        assert response.status_code == 200
        assert response.text == item["kind"]
        assert len(response.content) == item["size"]
    old_revision = data["files"][0]["revision"]
    (session / "assets" / "sample.txt").write_text("updated output")
    assert client.get("/api/v1/chat/sample/offline-files").json()["files"][0]["revision"] != old_revision


def test_manifest_rejects_symlinks_and_private_files(archive, tmp_path):
    client, session = archive
    client.headers["Authorization"] = "Bearer fixture-only"
    external = tmp_path / "outside.txt"
    external.write_text("not exported")
    (session / "assets" / "escape.txt").symlink_to(external)
    (session / "assets" / ".private").write_text("not exported")
    (session / "assets" / "subdirectory").mkdir()
    (session.parent / "alias").symlink_to(session, target_is_directory=True)
    items = client.get("/api/v1/chat/sample/offline-files").json()["files"]
    assert len(items) == 4
    assert client.get("/api/v1/files/sample/offline/assets/escape.txt").status_code == 404
    assert client.get("/api/v1/chat/alias/offline-files").status_code == 404
    assert client.get("/api/v1/files/sample/offline/secrets/sample.txt").status_code == 400


def test_unicode_uploaded_filename_is_portable(archive):
    client, session = archive
    client.headers["Authorization"] = "Bearer fixture-only"
    (session / "attach" / "住宿确认 1.pdf").write_bytes(b"synthetic PDF")
    items = client.get("/api/v1/chat/sample/offline-files").json()["files"]
    item = next(item for item in items if item["name"] == "住宿确认 1.pdf")
    response = client.get(item["path"])
    assert response.content == b"synthetic PDF"
    # RFC 5987: the non-ASCII upload name survives percent-encoded, so the saved
    # copy keeps the name the manifest advertises.
    assert response.headers["Content-Disposition"] == (
        "attachment; filename*=UTF-8''%E4%BD%8F%E5%AE%BF%E7%A1%AE%E8%AE%A4%201.pdf")


def test_uploaded_markup_can_never_render_as_same_origin_script(archive):
    """An uploaded .html/.svg is user content served from the API's own origin."""
    client, session = archive
    client.headers["Authorization"] = "Bearer fixture-only"
    (session / "attach" / "note.html").write_text("<script>fetch('/api/v1/sessions')</script>")
    (session / "image" / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    for path in ("/api/v1/files/sample/offline/attach/note.html",
                 "/api/v1/files/sample/offline/image/logo.svg"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["X-Content-Type-Options"] == "nosniff", path
        disposition = response.headers["Content-Disposition"]
        assert disposition.startswith("attachment"), path
        assert "inline" not in disposition, path
        assert response.headers["Cache-Control"] == "private, no-cache", path
