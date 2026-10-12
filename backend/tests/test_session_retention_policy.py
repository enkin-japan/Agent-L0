import asyncio
import json
import os
from types import SimpleNamespace

from framework.api.routes import list_sessions, set_session_pin
from framework.api.schemas import PinRequest
from framework.memory.session import SessionManager


def test_pin_endpoint_persists_and_cleanup_respects_existing_exemptions(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    for sid in ["ordinary", "kept", "inflight", "active", "schedule_fixture"]:
        folder = manager.sessions_dir / sid
        (folder / "assets").mkdir(parents=True)
        (folder / "assets/report.txt").write_text("synthetic artifact")
        (folder / "history.jsonl").write_text(json.dumps({"prompt": "synthetic", "response": []}) + "\n")
        os.utime(folder / "history.jsonl", (1, 1))
    (manager.sessions_dir / "inflight/inflight.json").write_text("{}")
    external = tmp_path / "external.txt"
    external.write_text("external output")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_manager=manager)))
    result = asyncio.run(set_session_pin("kept", request, PinRequest(pinned=True)))
    assert result["status"] is True
    manager = SessionManager(manager.sessions_dir)  # Reopen from disk, not in-memory state.
    manager.scheduler = SimpleNamespace(is_active=lambda sid: sid == "active", discard=lambda sid: None)
    assert next(x for x in manager.list_sessions_meta() if x["session_id"] == "kept")["pinned"]
    assert asyncio.run(manager.prune_expired_sessions(1, 1)) == ["ordinary"]
    assert not (manager.sessions_dir / "ordinary/assets/report.txt").exists()
    assert external.exists()
    request.app.state.session_manager = manager
    assert asyncio.run(set_session_pin("kept", request, PinRequest(pinned=False)))["status"]
    assert asyncio.run(manager.prune_expired_sessions(1, 1)) == ["kept"]


def test_session_index_reports_the_effective_startup_policy(tmp_path):
    from framework.memory.retention import session_cleanup_policy
    policy = session_cleanup_policy({"enabled": False, "retention_days": 7.5,
                                     "empty_ttl_hours": 3, "scan_interval_seconds": 120})
    state = SimpleNamespace(session_manager=SessionManager(tmp_path), session_cleanup_policy=policy)
    result = asyncio.run(list_sessions(SimpleNamespace(app=SimpleNamespace(state=state))))
    assert result["retention_policy"] == policy
    assert policy == {"enabled": False, "retention_days": 7.5,
                      "empty_ttl_hours": 3.0, "scan_interval_seconds": 120}
