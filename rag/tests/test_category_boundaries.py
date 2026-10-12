"""分类与原文落盘边界：真实临时 Chroma，计算和网络均为替身。"""
import asyncio
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
import requests
from fastapi import BackgroundTasks, HTTPException

RAG_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def server(tmp_path, monkeypatch):
    cfg = tmp_path / "synthetic.yaml"
    cfg.write_text("rag:\n  host: 127.0.0.1\n  port: 14160\n  compute:\n    host: 127.0.0.1\n")
    monkeypatch.setenv("RAG_CONFIG", str(cfg))
    monkeypatch.setenv("RAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RAG_API_KEY", "synthetic")
    monkeypatch.setenv("RAG_COMPUTE_API_KEY", "synthetic")
    monkeypatch.setenv("RAG_COMPUTE_HOST", "127.0.0.1")
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "False")
    monkeypatch.syspath_prepend(str(RAG_ROOT))

    def no_network(*args, **kwargs):
        raise AssertionError("boundary tests forbid network")

    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    name = "synthetic_rag_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, RAG_ROOT / "rag_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    ident = {"tier": "synthetic", "embed_repo": "synthetic/embed", "embed_dim": 4}
    monkeypatch.setattr(srv.COMPUTE, "ensure_ready", lambda: ident)
    monkeypatch.setattr(srv.COMPUTE, "embed", lambda texts: [[1., 2., 3., 4.] for _ in texts])
    monkeypatch.setattr(srv.COMPUTE, "token_counts", lambda texts, model_type="embedding": [len(t) for t in texts])
    yield srv
    srv.QUERY_POOL.shutdown(wait=True)


def add_route(srv, name):
    # 直接写 meta 能模拟旧安装的非法条目；Chroma 的 id 本身没有 collection-name 校验。
    srv.get_chroma_collection("meta_classification_db").upsert(
        ids=[name], documents=["synthetic routing"], embeddings=[[1., 2., 3., 4.]])


def assert_refused(call):
    with pytest.raises(HTTPException) as err:
        call()
    assert err.value.status_code == 400


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", "a/b", "a\\b", "..", "", "bad%2fname", "meta_classification_db"])
def test_create_category_rejects_before_meta_write(server, name):
    assert_refused(lambda: asyncio.run(server.upsert_category(
        server.CategoryItem(db_name=name, description="synthetic"))))
    assert server.get_chroma_collection("meta_classification_db").count() == 0
    assert list(server.RAW_FILES_DIR.iterdir()) == []


@pytest.mark.parametrize("relative", [False, True])
def test_forced_insert_rejects_legacy_bad_meta_before_acceptance(server, tmp_path, relative):
    outside = tmp_path / "outside"
    name = "../../../outside" if relative else str(outside)
    add_route(server, name)
    bg = BackgroundTasks()
    assert_refused(lambda: asyncio.run(server.insert_docs(
        server.InsertRequest(title="sample", document="payload", category=name), bg)))
    assert not bg.tasks
    assert not outside.exists()
    assert server.get_chroma_collection("meta_classification_db").get()["ids"] == [name]


@pytest.mark.parametrize("name", ["Legacy.Docs-2026", "python_programming", "faq", "unclassified_db"])
def test_legal_existing_categories_keep_raw_and_index(server, name):
    if name != "unclassified_db":
        asyncio.run(server.upsert_category(server.CategoryItem(db_name=name, description="synthetic")))
    bg = BackgroundTasks()
    asyncio.run(server.insert_docs(server.InsertRequest(title="sample", document="payload", category=name), bg))
    asyncio.run(bg())
    col = server.get_chroma_collection(name)
    assert col.count() == 1
    path = Path(col.get()["metadatas"][0]["source_path"])
    assert path.read_text() == "payload"
    assert path.resolve().is_relative_to(server.STORAGE_BASE.resolve())


def test_auto_route_skips_illegal_meta_and_keeps_originals(server, tmp_path, caplog):
    outside = tmp_path / "outside"
    outside.mkdir()
    original = outside / "user.txt"
    original.write_text("user original")
    add_route(server, str(outside))
    server.auto_classify_and_insert("payload", "sample")
    assert list(outside.iterdir()) == [original]
    assert original.read_text() == "user original"
    assert server.get_chroma_collection("unclassified_db").count() == 1
    assert "跳过" in caplog.text and str(outside) in caplog.text
    categories = asyncio.run(server.list_categories())
    assert categories == []
    assert server.get_chroma_collection("meta_classification_db").get()["ids"] == [str(outside)]


@pytest.mark.parametrize("entry", ["create", "accept", "background", "auto", "move", "purge"])
def test_symlink_category_cannot_write_or_move_outside(server, tmp_path, entry):
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "user.txt"
    keep.write_text("untouched")
    (server.RAW_FILES_DIR / "faq").symlink_to(outside, target_is_directory=True)
    add_route(server, "faq")
    source = server.UNCLASSIFIED_DIR / "sample.txt"
    source.write_text("payload")
    calls = {
        "create": lambda: asyncio.run(server.upsert_category(server.CategoryItem(db_name="faq", description="synthetic"))),
        "accept": lambda: asyncio.run(server.insert_docs(server.InsertRequest(title="sample", document="payload", category="faq"), BackgroundTasks())),
        "background": lambda: server.auto_classify_and_insert("payload", "sample", forced_category="faq"),
        "auto": lambda: server.auto_classify_and_insert("payload", "sample"),
        "move": lambda: server.auto_classify_and_insert("payload", "sample", forced_category="faq", existing_source_path=str(source)),
        "purge": lambda: asyncio.run(server.purge_category("faq")),
    }
    if entry == "auto":
        calls[entry]()  # 非法旧路由跳过，回到未分类池
    else:
        assert_refused(calls[entry])
    assert list(outside.iterdir()) == [keep]
    assert keep.read_text() == "untouched"
    assert source.read_text() == "payload"


def test_background_revalidates_after_acceptance(server, tmp_path):
    add_route(server, "faq")
    bg = BackgroundTasks()
    asyncio.run(server.insert_docs(server.InsertRequest(title="sample", document="payload", category="faq"), bg))
    outside = tmp_path / "outside"
    outside.mkdir()
    (server.RAW_FILES_DIR / "faq").symlink_to(outside, target_is_directory=True)
    assert_refused(lambda: asyncio.run(bg()))
    assert list(outside.iterdir()) == []


def test_existing_source_escape_is_rejected_without_moving_original(server, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("user original")
    assert_refused(lambda: server.auto_classify_and_insert("payload", "sample", forced_category="faq", existing_source_path=str(outside)))
    assert outside.read_text() == "user original"
    assert list(server.RAW_FILES_DIR.iterdir()) == []


def test_overview_reports_invalid_legacy_meta_without_touching_raw(server, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "user.txt").write_text("original")
    add_route(server, str(outside))
    result = asyncio.run(server.list_documents())
    assert str(outside) not in [cat["name"] for cat in result["categories"]]
    assert result["skipped_categories"][0]["name"] == str(outside)
    assert "reason" in result["skipped_categories"][0]
    assert (outside / "user.txt").read_text() == "original"


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.syspath_prepend(str(RAG_ROOT.parent / "backend"))
    monkeypatch.setattr(requests, "get", lambda *a, **k: type("Resp", (), {"status_code": 200})())
    from framework.mcp.tools import rag_admin
    monkeypatch.setattr(rag_admin, "_request", lambda *a, **k: pytest.fail("invalid category reached HTTP write"))
    monkeypatch.setattr(rag_admin, "_fetch_categories", lambda: (True, {"../outside": "legacy"}))
    return rag_admin


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", "a/b", "a\\b", "meta_classification_db"])
def test_admin_rejects_category_before_http(admin, name):
    assert "REJECTED" in admin.RAGUpsertCategoryTool().execute(name, "synthetic")
    assert "REJECTED" in admin.RAGInsertDocumentTool().execute("sample", "payload", name)
    assert "REJECTED" in admin.RAGDeleteCategoryTool().execute(name)


def test_retier_category_restore_rejects_illegal_id(server):
    assert_refused(lambda: server._retier_upsert_category("../outside", "synthetic"))
    assert server.get_chroma_collection("meta_classification_db").count() == 0


def test_reclassify_illegal_route_preserves_same_original(server, tmp_path, caplog):
    original = server.UNCLASSIFIED_DIR / "sample_abcd.txt"
    original.write_text("user original")
    add_route(server, str(tmp_path / "outside"))
    server.reclassify_background_task()
    assert original.read_text() == "user original"
    assert list(server.UNCLASSIFIED_DIR.iterdir()) == [original]
    assert "跳过" in caplog.text
    assert server.get_chroma_collection("unclassified_db").count() == 1


def test_reclassify_valid_route_moves_once_and_clears_old_index(server):
    original = server.UNCLASSIFIED_DIR / "sample_abcd.txt"
    original.write_text("user original")
    server.auto_classify_and_insert("user original", "sample", forced_category="unclassified_db", existing_source_path=str(original))
    assert [hit["source_path"] for hit in server.FTS.search("original")] == [str(original)]
    add_route(server, "faq")
    server.reclassify_background_task()
    moved = server.RAW_FILES_DIR / "faq" / original.name
    assert not original.exists()
    assert moved.read_text() == "user original"
    assert server.get_chroma_collection("unclassified_db").count() == 0
    assert server.get_chroma_collection("faq").count() == 1
    hits = server.FTS.search("original")
    assert [(hit["db"], hit["source_path"]) for hit in hits] == [("faq", str(moved))]


def test_reindex_destination_file_symlink_is_rejected(server, tmp_path):
    outside = tmp_path / "user.txt"
    outside.write_text("original")
    original = server.UNCLASSIFIED_DIR / "sample_abcd.txt"
    original.write_text("payload")
    target = server.RAW_FILES_DIR / "faq"
    target.mkdir()
    (target / original.name).symlink_to(outside)
    assert_refused(lambda: server.auto_classify_and_insert("payload", "sample", forced_category="faq", existing_source_path=str(original)))
    assert original.read_text() == "payload"
    assert outside.read_text() == "original"


def test_version_archive_symlink_never_moves_original_outside(server, tmp_path):
    server.auto_classify_and_insert("old", "sample", forced_category="faq")
    original = Path(server.get_chroma_collection("faq").get()["metadatas"][0]["source_path"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (original.parent / ".versions").symlink_to(outside, target_is_directory=True)
    server.auto_classify_and_insert("new", "sample", forced_category="faq")
    assert original.read_text() == "old"
    assert list(outside.iterdir()) == []


def test_raw_root_symlink_escape_is_rejected_before_creation(server, tmp_path):
    server.RAW_FILES_DIR.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    server.RAW_FILES_DIR.symlink_to(outside, target_is_directory=True)
    assert_refused(lambda: asyncio.run(server.upsert_category(server.CategoryItem(db_name="faq", description="synthetic"))))
    assert list(outside.iterdir()) == []


def test_storage_root_symlink_escape_is_rejected(server, tmp_path):
    server.RAW_FILES_DIR.rmdir()
    server.UNCLASSIFIED_DIR.rmdir()
    server.STORAGE_BASE.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    server.STORAGE_BASE.symlink_to(outside, target_is_directory=True)
    assert_refused(lambda: asyncio.run(server.upsert_category(server.CategoryItem(db_name="faq", description="synthetic"))))
    assert list(outside.iterdir()) == []


def test_import_rejects_preexisting_storage_escape_before_mkdir(server, tmp_path, monkeypatch):
    data = tmp_path / "new-data"
    data.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (data / "storage").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("RAG_DATA_DIR", str(data))
    spec = importlib.util.spec_from_file_location("rejected_rag", RAG_ROOT / "rag_server.py")
    module = importlib.util.module_from_spec(spec)
    assert_refused(lambda: spec.loader.exec_module(module))
    assert list(outside.iterdir()) == []
    assert not (data / "chroma_data").exists()


def test_admin_legacy_name_passes_to_http(admin, monkeypatch):
    seen = []
    monkeypatch.setattr(admin, "_request", lambda *a, **k: seen.append((a, k)) or "ok")
    monkeypatch.setattr(admin, "_fetch_categories", lambda: (True, {"Legacy.Docs-2026": "existing"}))
    assert admin.RAGUpsertCategoryTool().execute("Legacy.Docs-2026", "synthetic") == "ok"
    assert "ok" in admin.RAGInsertDocumentTool().execute("sample", "payload", "Legacy.Docs-2026")
    assert admin.RAGDeleteCategoryTool().execute("Legacy.Docs-2026") == "ok"
    assert len(seen) == 3


def test_reclassify_collision_preserves_both_originals_and_indexes(server):
    source = server.UNCLASSIFIED_DIR / "sample_abcd.txt"
    source.write_text("unclassified original")
    server.auto_classify_and_insert(source.read_text(), "sample", forced_category="unclassified_db", existing_source_path=str(source))
    add_route(server, "faq")
    destination = server.RAW_FILES_DIR / "faq" / source.name
    destination.parent.mkdir()
    destination.write_text("existing original")
    server.get_chroma_collection("faq").add(ids=["existing"], documents=["existing original"],
        embeddings=[[1., 2., 3., 4.]], metadatas=[{"source_path": str(destination), "title": "other"}])
    with pytest.raises(HTTPException) as err:
        server.reclassify_background_task()
    assert err.value.status_code == 409
    assert "已存在" in err.value.detail
    assert source.read_text() == "unclassified original"
    assert destination.read_text() == "existing original"
    assert server.get_chroma_collection("unclassified_db").count() == 1
    assert server.get_chroma_collection("faq").count() == 1


@pytest.mark.parametrize("escape", ["trash", "destination", "source"])
def test_document_delete_rejects_escape_before_index_or_raw_changes(server, tmp_path, monkeypatch, escape):
    server.auto_classify_and_insert("original", "sample", forced_category="faq")
    source = Path(server.get_chroma_collection("faq").get()["metadatas"][0]["source_path"])
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("outside original")
    if escape == "trash":
        server.TRASH_DIR.symlink_to(outside, target_is_directory=True)
    elif escape == "destination":
        monkeypatch.setattr(server.time, "strftime", lambda *a, **k: "fixed")
        server.TRASH_DIR.mkdir()
        (server.TRASH_DIR / ("fixed_" + source.name)).symlink_to(keep)
    else:
        source.unlink()
        source.symlink_to(keep)
    assert_refused(lambda: asyncio.run(server.delete_document_by_path(str(source))))
    assert server.get_chroma_collection("faq").count() == 1
    assert source.exists()
    assert source.read_text() == ("outside original" if escape == "source" else "original")
    assert list(outside.iterdir()) == [keep]
    assert keep.read_text() == "outside original"


@pytest.mark.parametrize("name", ["a..b", "127.0.0.1"])
def test_chroma_rejected_names_refused_before_writes(server, admin, name):
    assert_refused(lambda: asyncio.run(server.upsert_category(server.CategoryItem(db_name=name, description="synthetic"))))
    add_route(server, name)
    bg = BackgroundTasks()
    assert_refused(lambda: asyncio.run(server.insert_docs(server.InsertRequest(title="sample", document="payload", category=name), bg)))
    assert not bg.tasks
    assert list(server.RAW_FILES_DIR.iterdir()) == []
    assert "REJECTED" in admin.RAGUpsertCategoryTool().execute(name, "synthetic")


@pytest.mark.parametrize("length", [64, 512])
def test_real_chroma_long_name_acceptance_is_retained(server, admin, length):
    name = "x" * length
    assert server._validate_category_id(name) == name
    assert admin._valid_category_id(name)
    assert server.get_chroma_collection(name).name == name
    if length == 512:
        assert_refused(lambda: asyncio.run(server.upsert_category(server.CategoryItem(db_name=name, description="synthetic"))))
        assert server.get_chroma_collection("meta_classification_db").count() == 0
    else:
        assert server._category_raw_dir(name).name == name


@pytest.mark.parametrize("name", ["999.999.999.999", "01.2.3.4"])
def test_non_ipv4_numeric_names_keep_real_chroma_compatibility(server, admin, name):
    assert server._validate_category_id(name) == name
    assert admin._valid_category_id(name)
    assert server.get_chroma_collection(name).name == name
    asyncio.run(server.upsert_category(server.CategoryItem(db_name=name, description="synthetic")))


def test_raw_move_concurrent_destination_creation_preserves_both(server, monkeypatch):
    source = server.UNCLASSIFIED_DIR / "sample_abcd.txt"
    source.write_text("source original")
    destination = server.RAW_FILES_DIR / "faq" / source.name
    destination.parent.mkdir()
    original_link = server.os.link

    def racing_link(src, dst, **kwargs):
        Path(dst).write_text("concurrent original")
        return original_link(src, dst, **kwargs)

    monkeypatch.setattr(server.os, "link", racing_link)
    with pytest.raises(HTTPException) as err:
        server.auto_classify_and_insert("source original", "sample", forced_category="faq", existing_source_path=str(source))
    assert err.value.status_code == 409
    assert source.read_text() == "source original"
    assert destination.read_text() == "concurrent original"


def test_document_delete_success_archives_original_then_removes_indexes(server):
    server.auto_classify_and_insert("original", "sample", forced_category="faq")
    source = Path(server.get_chroma_collection("faq").get()["metadatas"][0]["source_path"])
    result = asyncio.run(server.delete_document_by_path(str(source)))
    assert result["deleted_chunks"] == 1
    assert server.get_chroma_collection("faq").count() == 0
    assert not source.exists()
    archived = list(server.TRASH_DIR.iterdir())
    assert len(archived) == 1 and archived[0].read_text() == "original"


def test_document_delete_trash_collision_preserves_originals_and_index(server, monkeypatch):
    server.auto_classify_and_insert("original", "sample", forced_category="faq")
    source = Path(server.get_chroma_collection("faq").get()["metadatas"][0]["source_path"])
    monkeypatch.setattr(server.time, "strftime", lambda *a, **k: "fixed")
    server.TRASH_DIR.mkdir()
    destination = server.TRASH_DIR / ("fixed_" + source.name)
    destination.write_text("archived original")
    with pytest.raises(HTTPException) as err:
        asyncio.run(server.delete_document_by_path(str(source)))
    assert err.value.status_code == 409
    assert server.get_chroma_collection("faq").count() == 1
    assert source.read_text() == "original"
    assert destination.read_text() == "archived original"
