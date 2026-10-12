"""R10: cleanup candidates are provisional until the final deletion boundary."""
import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from framework.api.routes import set_session_pin
from framework.api.schemas import PinRequest
from framework.memory import compactor
from framework.memory.session import SessionManager


def expired(manager, sid="candidate", *, empty=False):
    folder = manager.sessions_dir / sid
    (folder / "assets").mkdir(parents=True)
    (folder / "assets/synthetic.txt").write_text("synthetic data")
    (folder / "meta.json").write_text(json.dumps({"session_id": sid, "created_at": 1}))
    if not empty:
        (folder / "history.jsonl").write_text(json.dumps({"status": "success", "prompt": "old", "response": []}) + "\n")
        os.utime(folder / "history.jsonl", (1, 1))
    return folder


def sediment_barrier(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def sediment(*_args):
        entered.set()
        assert release.wait(3), "synthetic sediment barrier was not released"
        return True
    monkeypatch.setattr(compactor, "last_chance_sediment", sediment)
    return entered, release


@pytest.mark.parametrize("change", ["pin", "history", "inflight", "scheduler", "queued", "running", "suspended", "accept-lock", "empty-clock"])
def test_candidate_changed_during_sediment_is_retained(tmp_path, monkeypatch, change):
    manager = SessionManager(tmp_path / "sessions")
    folder = expired(manager, empty=change == "empty-clock")
    active = set()
    manager.scheduler = SimpleNamespace(is_active=lambda sid: sid in active, discard=lambda sid: active.discard(sid))
    entered, release = sediment_barrier(monkeypatch)
    async def scenario():
        task = asyncio.create_task(manager.prune_expired_sessions(1, 1, sediment_on_expire=True))
        assert await asyncio.to_thread(entered.wait, 1)
        session = None
        try:
            if change == "pin":
                request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_manager=manager)))
                result = await set_session_pin("candidate", request, PinRequest(pinned=True))
                assert result["status"] is True
            elif change == "history":
                os.utime(folder / "history.jsonl", None)
            elif change == "empty-clock":
                (folder / "meta.json").write_text(json.dumps({"created_at": time.time()}))
            elif change == "scheduler":
                active.add("candidate")
            else:
                session = manager.get_or_create("candidate")
                if change == "inflight":
                    session.write_inflight("new input", {})
                elif change == "accept-lock":
                    await session.lock.acquire()
                else:
                    session.journal.emit("turn_queued", {"prompt": "new input"}, turn_id="t_new")
                    if change == "running":
                        session.journal.emit("turn_started", {})
                    elif change == "suspended":
                        session.journal.emit("suspended", {})
            release.set()
            assert await asyncio.wait_for(task, 0.5) == []
            assert (folder / "assets/synthetic.txt").exists()
            if session is not None:
                assert not session.cancel_requested
                assert manager._all_sessions["candidate"] is session
                assert not session.journal._closed
            if change == "pin":
                restarted = SessionManager(manager.sessions_dir)
                assert restarted.list_sessions_meta()[0]["pinned"] is True
        finally:
            release.set()
            if session is not None and session.lock.locked():
                session.lock.release()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("hot", [False, True])
def test_expired_idle_sessions_are_still_reaped_and_external_files_survive(tmp_path, hot):
    manager = SessionManager(tmp_path / "sessions")
    folder = expired(manager)
    external = tmp_path / "external.txt"
    external.write_text("external data")
    (folder / "outside-link").symlink_to(external)
    if hot:
        manager.get_or_create("candidate")
    assert asyncio.run(manager.prune_expired_sessions(1, 1)) == ["candidate"]
    assert not folder.exists() and external.read_text() == "external data"
    assert "candidate" not in manager._all_sessions


def test_manual_delete_retains_explicit_user_semantics(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    folder = expired(manager)
    assert manager.set_meta("candidate", pinned=True)
    assert asyncio.run(manager.delete_session("candidate"))["status"] is True
    assert not folder.exists()


def test_pin_during_freeze_keeps_the_cold_session_and_files(tmp_path, monkeypatch):
    """freeze may perform backend I/O; metadata writes must remain possible then."""
    manager = SessionManager(tmp_path / "sessions")
    folder = expired(manager)
    session = manager.get_or_create("candidate")
    original = session.freeze
    results = []
    writers = []
    def freeze_with_pin():
        writer = threading.Thread(target=lambda: results.append(manager.set_meta("candidate", pinned=True)))
        writers.append(writer)
        writer.start()
        writer.join(0.5)
        assert not writer.is_alive(), "metadata mutex held across freeze/backend I/O"
        assert results == [True]
        original()
    monkeypatch.setattr(session, "freeze", freeze_with_pin)
    try:
        assert asyncio.run(manager.prune_expired_sessions(1, 1)) == []
    finally:
        for writer in writers:
            writer.join(1)
    assert (folder / "assets/synthetic.txt").exists()
    assert manager._all_sessions["candidate"] is session and session.is_cold
    assert not session.journal._closed
    assert SessionManager(manager.sessions_dir).list_sessions_meta()[0]["pinned"]


def test_pin_cannot_succeed_inside_committed_local_deletion(tmp_path, monkeypatch):
    """If deletion wins the mutex, a later pin reports failure rather than false success."""
    manager = SessionManager(tmp_path / "sessions")
    folder = expired(manager)
    deleting, release, pin_started, pin_done = (threading.Event() for _ in range(4))
    original = __import__("shutil").rmtree
    def held_rmtree(path, *args, **kwargs):
        if path == folder:
            deleting.set()
            assert release.wait(2), "synthetic local deletion was not released"
        return original(path, *args, **kwargs)
    monkeypatch.setattr("framework.memory.session.shutil.rmtree", held_rmtree)
    reaped, pinned, errors = [], [], []
    def prune():
        try:
            reaped.extend(asyncio.run(manager.prune_expired_sessions(1, 1)))
        except Exception as error:
            errors.append(error)
    def pin():
        pin_started.set()
        pinned.append(manager.set_meta("candidate", pinned=True))
        pin_done.set()
    cleaner = threading.Thread(target=prune)
    writer = threading.Thread(target=pin)
    cleaner.start()
    try:
        assert deleting.wait(1)
        writer.start()
        assert pin_started.wait(1)
        assert not pin_done.wait(0.05), "pin reported success while rmtree still owned the directory"
    finally:
        release.set()
        cleaner.join(2)
        if writer.ident is not None:
            writer.join(2)
    assert not cleaner.is_alive() and not writer.is_alive() and not errors
    assert reaped == ["candidate"] and pinned == [False]
    assert not folder.exists()
