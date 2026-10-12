"""R14: accepted writes drain before inventory; stale survives wipe/restart.

Real temporary Chroma, synthetic configuration, fake compute; no services/models.
"""
import asyncio
import importlib.util
import json
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from test_category_boundaries import server  # existing synthetic-data fixture


def wire(srv, monkeypatch):
    ident = {"tier": "synthetic", "embed_repo": "synthetic/embed", "embed_dim": 4}
    monkeypatch.setattr(srv, "_retier_fetch_live_identity", lambda: ident)
    monkeypatch.setattr(srv.RETIER, "_fetch_live_identity", lambda: ident)
    monkeypatch.setattr(srv.COMPUTE, "ensure_ready", lambda: ident)
    monkeypatch.setattr(srv.COMPUTE, "embed", lambda texts: [[1., 2., 3., 4.] for _ in texts])
    monkeypatch.setattr(srv.COMPUTE, "token_counts", lambda texts, model_type="embedding": [len(t) for t in texts])
    return srv


def wait(mgr):
    deadline = time.monotonic() + 5
    while mgr.progress()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(.005)
    out = mgr.progress()
    assert out["state"] != "running", out
    return out


def accept(srv):
    bg = BackgroundTasks()
    asyncio.run(srv.insert_docs(srv.InsertRequest(title="accepted", document="original payload",
                                                category="unclassified_db"), bg))
    return bg


@pytest.mark.parametrize("started", [False, True])
def test_accepted_background_drains_before_inventory(server, monkeypatch, started):
    srv = wire(server, monkeypatch)
    bg = accept(srv)
    entered, release, inventory = threading.Event(), threading.Event(), threading.Event()
    # A running task may be paused before its first raw-file write.
    # The delayed case has returned from insert_docs but BackgroundTasks hasn't started.
    original = bg.tasks[0].func

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    bg.tasks[0].func = blocked
    thread = threading.Thread(target=lambda: asyncio.run(bg()))
    real_list = srv.RETIER._list_raw_files

    def inventory_probe():
        inventory.set()
        return real_list()

    monkeypatch.setattr(srv.RETIER, "_list_raw_files", inventory_probe)
    if started:
        thread.start()
        assert entered.wait(2)
    srv.RETIER.start_rebuild(force=True)
    try:
        assert not inventory.wait(.1), "snapshot ran before the accepted write completed"
    finally:
        if not started:
            thread.start()
        release.set()
        thread.join(5)
        out = wait(srv.RETIER)
    assert out["state"] == "done", out
    assert out["docs_total"] == 1
    files = srv._retier_list_raw_files()
    assert len(files) == 1 and files[0][0].read_text() == "original payload"
    paths = [m["source_path"] for c in srv.get_chroma_client().list_collections()
             for m in srv.get_chroma_collection(c.name).get()["metadatas"] if m]
    assert str(files[0][0]) in paths


def test_stale_durable_after_wipe_failure_and_module_restart(server, monkeypatch):
    srv = wire(server, monkeypatch)
    asyncio.run(accept(srv)())
    source = srv._retier_list_raw_files()[0][0]
    original = source.read_bytes()
    asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=str(source), reason="old contract")))

    def fail_adopt():
        raise RuntimeError("fault after wipe")

    monkeypatch.setattr(srv.RETIER, "_reset_and_adopt", fail_adopt)
    srv.RETIER.start_rebuild(force=True)
    failed = wait(srv.RETIER)
    assert failed["state"] == "failed"
    backup = srv.STORAGE_BASE / "stale_backup.json"
    assert json.loads(backup.read_text())[str(source)]["stale"] == "old contract"
    assert source.read_bytes() == original

    # New module/global manager, same data root: no old in-memory stale snapshot survives.
    spec = importlib.util.spec_from_file_location("restarted_rag_" + uuid.uuid4().hex, Path(srv.__file__))
    restarted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restarted)
    try:
        wire(restarted, monkeypatch)
        assert restarted.RETIER.status()["interrupted"]
        restarted.RETIER.start_rebuild()
        out = wait(restarted.RETIER)
        assert out["state"] == "done", out
        mapping, errors = restarted._retier_dump_stale()
        assert errors == 0 and mapping[str(source)]["stale"] == "old contract"
        assert source.read_bytes() == original
    finally:
        restarted.QUERY_POOL.shutdown(wait=True)


def test_stale_snapshot_error_fails_before_wipe(server, monkeypatch):
    srv = wire(server, monkeypatch)
    asyncio.run(accept(srv)())
    monkeypatch.setattr(srv.RETIER, "_dump_stale", lambda: ({}, 1))
    wiped = []
    monkeypatch.setattr(srv.RETIER, "_wipe_store", lambda: wiped.append(True))
    srv.RETIER.start_rebuild(force=True)
    assert wait(srv.RETIER)["state"] == "failed"
    assert not wiped


def test_stale_restore_failure_keeps_recovery_and_cannot_report_done(server, monkeypatch):
    srv = wire(server, monkeypatch)
    asyncio.run(accept(srv)())
    source = str(srv._retier_list_raw_files()[0][0])
    asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=source, reason="old")))
    restore = srv.RETIER._restore_stale
    monkeypatch.setattr(srv.RETIER, "_restore_stale", lambda mapping: list(mapping))
    srv.RETIER.start_rebuild(force=True)
    out = wait(srv.RETIER)
    assert out["state"] == "failed", out
    assert srv.RETIER.status()["interrupted"]
    monkeypatch.setattr(srv.RETIER, "_restore_stale", restore)
    srv.RETIER.start_rebuild()
    assert wait(srv.RETIER)["state"] == "done"
    assert srv._retier_dump_stale()[0][source]["stale"] == "old"


def test_completed_snapshot_does_not_resurrect_cancelled_stale(server, monkeypatch):
    srv = wire(server, monkeypatch)
    asyncio.run(accept(srv)())
    source = str(srv._retier_list_raw_files()[0][0])
    asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=source, reason="old")))
    srv.RETIER.start_rebuild(force=True)
    assert wait(srv.RETIER)["state"] == "done"
    asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=source, reason="")))
    srv.RETIER.start_rebuild(force=True)
    out = wait(srv.RETIER)
    assert out["state"] == "done" and out["stale_total"] == 0
    assert srv._retier_dump_stale()[0] == {}


def failed_with_stale(srv, monkeypatch, restored=False):
    asyncio.run(accept(srv)())
    source = str(srv._retier_list_raw_files()[0][0])
    asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=source, reason="old")))
    restore = srv.RETIER._restore_stale
    def fail(mapping):
        if restored:
            restore(mapping)
        return list(mapping)
    monkeypatch.setattr(srv.RETIER, "_restore_stale", fail)
    srv.RETIER.start_rebuild(force=True)
    assert wait(srv.RETIER)["state"] == "failed"
    return source, restore


@pytest.mark.parametrize("operation", ["delete", "clear", "update", "purge"])
def test_failed_rebuild_governance_survives_retry_and_restart(server, monkeypatch, operation):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch, restored=True)
    if operation == "delete":
        asyncio.run(srv.delete_document_by_path(source))
    elif operation == "purge":
        asyncio.run(srv.purge_category("unclassified_db"))
    else:
        reason = "" if operation == "clear" else "new reason"
        asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=source, reason=reason)))
    # Discard every old global/in-memory object; only the existing data files survive.
    spec = importlib.util.spec_from_file_location("governance_restart_" + uuid.uuid4().hex, Path(srv.__file__))
    restarted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restarted)
    try:
        wire(restarted, monkeypatch)
        restarted.RETIER.start_rebuild()
        result = wait(restarted.RETIER)
        assert result["state"] == "done", result
        mapping = restarted._retier_dump_stale()[0]
        if operation == "update":
            assert mapping[source]["stale"] == "new reason"
        else:
            assert mapping == {}
    finally:
        restarted.QUERY_POOL.shutdown(wait=True)


@pytest.mark.parametrize("reason", ["", "new pending"])
def test_failed_rebuild_clear_and_update_pending_stale_without_live_chunks(server, monkeypatch, reason):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch)
    # Remove derived chunks only, as if wipe succeeded but reinsertion failed.
    srv.get_chroma_collection("unclassified_db").delete(where={"source_path": source})
    result = asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=source, reason=reason)))
    assert result.get("recovery_updated") is True
    monkeypatch.setattr(srv.RETIER, "_restore_stale", restore)
    srv.RETIER.start_rebuild()
    assert wait(srv.RETIER)["state"] == "done"
    mapping = srv._retier_dump_stale()[0]
    if reason:
        assert mapping[source]["stale"] == reason
    else:
        assert mapping == {}


def test_failed_rebuild_reclassify_retains_stale_and_updates_recovery_path(server, monkeypatch):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch)
    asyncio.run(srv.upsert_category(srv.CategoryItem(db_name="faq", description="routing")))
    bg = BackgroundTasks()
    asyncio.run(srv.trigger_reclassification(bg))
    asyncio.run(bg())
    moved = srv.RAW_FILES_DIR / "faq" / Path(source).name
    assert not Path(source).exists() and moved.read_text() == "original payload"
    # Mutating at the new path must cancel the original path's recovery intent too.
    asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=str(moved), reason="")))
    monkeypatch.setattr(srv.RETIER, "_restore_stale", restore)
    srv.RETIER.start_rebuild()
    assert wait(srv.RETIER)["state"] == "done"
    assert srv._retier_dump_stale()[0] == {}
    saved = json.loads((srv.STORAGE_BASE / "stale_backup.json").read_text())
    assert saved[source]["_moved_to"] == str(moved)


@pytest.mark.parametrize("operation", ["delete", "clear", "purge", "move"])
def test_failed_governance_backup_write_failure_preserves_raw_and_snapshot(server, monkeypatch, operation):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch, restored=True)
    snapshot = srv.STORAGE_BASE / "stale_backup.json"
    previous = snapshot.read_bytes()
    original = Path(source).read_bytes()
    if operation == "move":
        asyncio.run(srv.upsert_category(srv.CategoryItem(db_name="faq", description="routing")))
    atomic = srv.RETIER._atomic_json
    def fail(path, data):
        if path == snapshot:
            raise OSError("snapshot write fault")
        atomic(path, data)
    monkeypatch.setattr(srv.RETIER, "_atomic_json", fail)
    with pytest.raises(OSError, match="snapshot write fault"):
        if operation == "delete":
            asyncio.run(srv.delete_document_by_path(source))
        elif operation == "purge":
            asyncio.run(srv.purge_category("unclassified_db"))
        elif operation == "clear":
            asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=source, reason="")))
        else:
            srv.reclassify_background_task()
    assert Path(source).read_bytes() == original
    assert snapshot.read_bytes() == previous
    assert srv._retier_dump_stale()[0][source]["stale"] == "old"


@pytest.mark.parametrize("operation", ["delete", "move", "purge"])
def test_raw_operation_failure_retains_original_recovery(server, monkeypatch, operation):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch)
    def fail(*args, **kwargs):
        raise OSError("raw move fault")
    with monkeypatch.context() as faults:
        if operation == "purge":
            real_rename = Path.rename
            def fail_directory(path, target):
                if path == srv.UNCLASSIFIED_DIR:
                    fail()
                return real_rename(path, target)
            faults.setattr(Path, "rename", fail_directory)
            with pytest.raises(OSError, match="raw move fault"):
                asyncio.run(srv.purge_category("unclassified_db"))
        else:
            faults.setattr(srv, "_move_raw_file_no_replace", fail)
            if operation == "move":
                asyncio.run(srv.upsert_category(srv.CategoryItem(db_name="faq", description="routing")))
            with pytest.raises(OSError, match="raw move fault"):
                if operation == "delete":
                    asyncio.run(srv.delete_document_by_path(source))
                else:
                    srv.reclassify_background_task()
    assert Path(source).read_text() == "original payload"
    monkeypatch.setattr(srv.RETIER, "_restore_stale", restore)
    srv.RETIER.start_rebuild()
    out = wait(srv.RETIER)
    assert out["state"] == "done", out
    assert list(srv._retier_dump_stale()[0].values())[0]["stale"] == "old"


def test_deleted_recovery_cannot_be_revived_by_marking_missing_path(server, monkeypatch):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch)
    asyncio.run(srv.delete_document_by_path(source))
    result = asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=source, reason="new")))
    assert result["updated_chunks"] == 0 and not result.get("recovery_updated")
    monkeypatch.setattr(srv.RETIER, "_restore_stale", restore)
    srv.RETIER.start_rebuild()
    assert wait(srv.RETIER)["state"] == "done"


@pytest.mark.parametrize("operation", ["clear", "delete", "purge"])
def test_governance_preserves_other_unrestored_stale(server, monkeypatch, operation):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch)
    srv.auto_classify_and_insert("other original", "other", forced_category="faq")
    other = srv.RAW_FILES_DIR / "faq"
    second = str(next(other.glob("*.txt")))
    asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=second, reason="other old")))
    if operation == "clear":
        asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=source, reason="")))
    elif operation == "delete":
        asyncio.run(srv.delete_document_by_path(source))
    else:
        asyncio.run(srv.purge_category("unclassified_db"))
    monkeypatch.setattr(srv.RETIER, "_restore_stale", restore)
    srv.RETIER.start_rebuild()
    assert wait(srv.RETIER)["state"] == "done"
    assert srv._retier_dump_stale()[0] == {second: {"stale": "other old", "stale_at": time.strftime("%Y-%m-%d")}}


def test_failed_live_mark_update_keeps_durable_override(server, monkeypatch):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch, restored=True)
    col_type = type(srv.get_chroma_collection("unclassified_db"))
    with monkeypatch.context() as faults:
        def fail(*args, **kwargs):
            raise RuntimeError("live metadata update fault")
        faults.setattr(col_type, "update", fail)
        result = asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=source, reason="new durable")))
    assert result["updated_chunks"] == 0 and result["recovery_updated"]
    assert srv._retier_dump_stale()[0][source]["stale"] == "old"
    monkeypatch.setattr(srv.RETIER, "_restore_stale", restore)
    srv.RETIER.start_rebuild()
    assert wait(srv.RETIER)["state"] == "done"
    assert srv._retier_dump_stale()[0][source]["stale"] == "new durable"


@pytest.mark.parametrize("reason", ["", "changed during archive"])
def test_governance_during_pending_archive_retains_operation_identity(server, monkeypatch, reason):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch, restored=True)
    real = srv.RETIER.record_raw_removal
    entered, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        real(*args, **kwargs)
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(srv.RETIER, "record_raw_removal", blocked)
    errors = []
    def replace():
        try:
            srv.auto_classify_and_insert("replacement payload", "accepted", forced_category="unclassified_db")
        except BaseException as err:
            errors.append(err)
    thread = threading.Thread(target=replace)
    thread.start()
    try:
        assert entered.wait(2)
        asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=source, reason=reason)))
    finally:
        release.set()
        thread.join(5)
    assert not errors and not thread.is_alive()
    monkeypatch.setattr(srv.RETIER, "_restore_stale", restore)
    srv.RETIER.start_rebuild()
    result = wait(srv.RETIER)
    assert result["state"] == "done", result
    assert srv._retier_dump_stale()[0] == {}


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_reused_deleted_destination_preserves_migrating_document_identity(server, monkeypatch, reverse, restart):
    srv = wire(server, monkeypatch)
    a = srv.UNCLASSIFIED_DIR / "same_file.txt"
    b = srv.RAW_FILES_DIR / "faq" / "same_file.txt"
    b.parent.mkdir()
    a.write_text("document A")
    b.write_text("document B")
    srv._retier_upsert_category("faq", "routing")
    for p, title, category, reason in [(a, "A", "unclassified_db", "stale A"), (b, "B", "faq", "stale B")]:
        srv.auto_classify_and_insert(p.read_text(), title, forced_category=category, existing_source_path=str(p))
        asyncio.run(srv.mark_document_stale(srv.StaleRequest(source_path=str(p), reason=reason)))
    wipe = srv.RETIER._wipe_store
    def fail():
        raise RuntimeError("failure before wipe")
    monkeypatch.setattr(srv.RETIER, "_wipe_store", fail)
    srv.RETIER.start_rebuild(force=True)
    assert wait(srv.RETIER)["state"] == "failed"
    backup = srv.STORAGE_BASE / "stale_backup.json"
    saved = json.loads(backup.read_text())
    keys = [str(b), str(a)] if reverse else [str(a), str(b)]
    srv.RETIER._atomic_json(backup, {key: saved[key] for key in keys})
    asyncio.run(srv.delete_document_by_path(str(b)))
    srv.reclassify_background_task()
    assert b.read_text() == "document A"
    target = srv
    if restart:
        spec = importlib.util.spec_from_file_location("identity_restart_" + uuid.uuid4().hex, Path(srv.__file__))
        target = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(target)
        wire(target, monkeypatch)
    else:
        monkeypatch.setattr(srv.RETIER, "_wipe_store", wipe)
    try:
        target.RETIER.start_rebuild()
        result = wait(target.RETIER)
        assert result["state"] == "done", result
        assert target._retier_dump_stale()[0][str(b)]["stale"] == "stale A"
        assert b.read_text() == "document A"
    finally:
        if restart:
            target.QUERY_POOL.shutdown(wait=True)


def test_restore_never_claims_another_document_by_basename(server, monkeypatch):
    srv = wire(server, monkeypatch)
    a = srv.UNCLASSIFIED_DIR / "same.txt"
    b = srv.RAW_FILES_DIR / "faq" / "same.txt"
    a.write_text("original A")
    b.parent.mkdir()
    b.write_text("original B")
    srv.auto_classify_and_insert(b.read_text(), "B", forced_category="faq", existing_source_path=str(b))
    lost = srv._retier_restore_stale({str(a): {"stale": "belongs to A", "stale_at": "date"}})
    assert lost == [str(a)]
    assert srv._retier_dump_stale()[0] == {}
    assert a.read_text() == "original A" and b.read_text() == "original B"


def test_restore_rejects_same_path_with_different_raw_identity(server, monkeypatch):
    srv = wire(server, monkeypatch)
    asyncio.run(accept(srv)())
    p = srv._retier_list_raw_files()[0][0]
    identity = srv.RETIER.raw_identity(p)
    archived = srv.STORAGE_BASE / "old-generation.txt"
    p.rename(archived)
    p.write_text("new generation")
    lost = srv._retier_restore_stale({str(p): {"stale": "old generation", "_raw_id": identity}})
    assert lost == [str(p)] and srv._retier_dump_stale()[0] == {}
    assert archived.read_text() == "original payload" and p.read_text() == "new generation"


def test_changed_raw_content_aborts_before_wipe_and_preserves_recovery(server, monkeypatch):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch)
    snapshot = srv.STORAGE_BASE / "stale_backup.json"
    old = snapshot.read_bytes()
    Path(source).write_text("externally reloaded and changed original")
    wipes = []
    monkeypatch.setattr(srv.RETIER, "_wipe_store", lambda: wipes.append(True))
    monkeypatch.setattr(srv.RETIER, "_restore_stale", restore)
    srv.RETIER.start_rebuild()
    result = wait(srv.RETIER)
    assert result["state"] == "failed" and "身份" in result["error"]
    assert not wipes and snapshot.read_bytes() == old
    assert Path(source).read_text() == "externally reloaded and changed original"


def test_legacy_plain_snapshot_adopts_existing_raw_identity(server, monkeypatch):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch)
    snapshot = srv.STORAGE_BASE / "stale_backup.json"
    srv.RETIER._atomic_json(snapshot, {source: {"stale": "legacy old", "stale_at": "date"}})
    monkeypatch.setattr(srv.RETIER, "_restore_stale", restore)
    srv.RETIER.start_rebuild()
    assert wait(srv.RETIER)["state"] == "done"
    assert srv._retier_dump_stale()[0][source]["stale"] == "legacy old"
    assert json.loads(snapshot.read_text())[source]["_raw_id"] == srv.RETIER.raw_identity(Path(source))


def test_legacy_unproven_operation_fails_without_wipe(server, monkeypatch):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch)
    snapshot = srv.STORAGE_BASE / "stale_backup.json"
    saved = {source: {"stale": "legacy old", "_moved_to": str(srv.RAW_FILES_DIR / "faq/new.txt")}}
    srv.RETIER._atomic_json(snapshot, saved)
    original = snapshot.read_bytes()
    wipes = []
    monkeypatch.setattr(srv.RETIER, "_wipe_store", lambda: wipes.append(True))
    srv.RETIER.start_rebuild()
    result = wait(srv.RETIER)
    assert result["state"] == "failed" and "缺少身份" in result["error"]
    assert not wipes and snapshot.read_bytes() == original


def test_conflicting_records_for_one_raw_identity_fail_without_wipe(server, monkeypatch):
    srv = wire(server, monkeypatch)
    source, restore = failed_with_stale(srv, monkeypatch)
    snapshot = srv.STORAGE_BASE / "stale_backup.json"
    saved = json.loads(snapshot.read_text())
    saved[source + "#conflict"] = {**saved[source], "stale": "ambiguous second owner"}
    srv.RETIER._atomic_json(snapshot, saved)
    original = snapshot.read_bytes()
    wipes = []
    monkeypatch.setattr(srv.RETIER, "_wipe_store", lambda: wipes.append(True))
    srv.RETIER.start_rebuild()
    result = wait(srv.RETIER)
    assert result["state"] == "failed" and "多条恢复记录" in result["error"]
    assert not wipes and snapshot.read_bytes() == original


@pytest.mark.parametrize("legacy", [False, True])
def test_missing_unidentified_source_cannot_claim_later_same_named_migration(server, monkeypatch, legacy):
    srv = wire(server, monkeypatch)
    missing = srv.RAW_FILES_DIR / "faq/same.txt"
    missing.parent.mkdir()
    other = srv.UNCLASSIFIED_DIR / "same.txt"
    other.write_text("different document B")
    srv._retier_upsert_category("faq", "routing")
    srv.auto_classify_and_insert(other.read_text(), "B", forced_category="unclassified_db",
                                 existing_source_path=str(other))
    snapshot = srv.STORAGE_BASE / "stale_backup.json"
    old = {str(missing): {"stale": "only missing A", "stale_at": "2000-01-01"}}
    if legacy:
        srv.RETIER._atomic_json(snapshot, old)
        srv.RETIER._rebuild_marker.write_text('{}')
    else:
        # A real legacy Chroma slice whose original is absent, not a fake dump.
        srv.get_chroma_collection("faq").add(ids=["missing-A"], documents=["old A"],
            embeddings=[[1., 2., 3., 4.]], metadatas=[{"source_path": str(missing), "stale": "only missing A"}])
        srv.RETIER._atomic_json(snapshot, {"previous backup": {"stale": "retained"}})
    backup_bytes = snapshot.read_bytes()
    marker_bytes = srv.RETIER._rebuild_marker.read_bytes() if legacy else None
    before = {c.name: srv.get_chroma_collection(c.name).get() for c in srv.get_chroma_client().list_collections()}
    srv.RETIER.start_rebuild(force=True)
    result = wait(srv.RETIER)
    assert result["state"] == "failed", result
    assert result["phase"] == "dump_categories"
    assert not missing.exists() and other.read_text() == "different document B"
    assert snapshot.read_bytes() == backup_bytes
    if legacy:
        assert srv.RETIER._rebuild_marker.read_bytes() == marker_bytes
    after = {c.name: srv.get_chroma_collection(c.name).get() for c in srv.get_chroma_client().list_collections()}
    assert after == before, "missing identity must fail before any derived store wipe"


def test_pending_legacy_missing_source_cannot_claim_governance_move_before_rebuild(server, monkeypatch):
    srv = wire(server, monkeypatch)
    missing = srv.RAW_FILES_DIR / "faq/same.txt"
    missing.parent.mkdir()
    other = srv.UNCLASSIFIED_DIR / "same.txt"
    other.write_text("different B")
    srv._retier_upsert_category("faq", "routing")
    srv.auto_classify_and_insert(other.read_text(), "B", forced_category="unclassified_db",
                                 existing_source_path=str(other))
    snapshot = srv.STORAGE_BASE / "stale_backup.json"
    srv.RETIER._atomic_json(snapshot, {str(missing): {"stale": "only old A"}})
    srv.RETIER._rebuild_marker.write_text('{}')
    before = snapshot.read_bytes()
    with pytest.raises(RuntimeError, match="原文缺失"):
        srv.reclassify_background_task()
    assert not missing.exists() and other.read_text() == "different B"
    assert snapshot.read_bytes() == before
    assert srv.get_chroma_collection("unclassified_db").count() == 1
