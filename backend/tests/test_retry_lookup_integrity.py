"""Explicit retry requires complete, usable journal evidence; ordinary readers stay tolerant."""
import json

import pytest

from framework.core import jsonl
from framework.core.turn_runner import TurnConflict
from framework.memory.session import SessionManager

from .fake_engine import FakeEngine, FakeRegistry
from .test_turn_retry import source, retry
from .test_turn_runner import make_runner, run, submit


def test_retry_key_beyond_reverse_byte_window_is_still_duplicate(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("byte-window")
    original, _ = source(session, media=False)
    runner = make_runner(FakeEngine([{"text": "retry"}, {"text": "padding answer"}]), FakeRegistry([]))
    async def scenario():
        first = await retry(runner, session, original)
        await runner.run_accepted(session, session.session_id, first)
        await submit(runner, session, session.session_id, "x" * (4 * 1024 * 1024 + 1024))
        await runner.delete_last_turn(manager, session.session_id)
        await runner.delete_last_turn(manager, session.session_id)
        assert session.journal.path.stat().st_size > 4 * 1024 * 1024
        assert runner.lookup_client_turn(session, "retry-key-0001") is None  # ordinary byte window preserved
        duplicate = await retry(runner, session, original)
        assert duplicate.get("duplicate") is True and duplicate["turn_id"] == first["turn_id"]
    run(scenario())


@pytest.mark.parametrize("corruption", ["json", "non_object", "utf8", "null_data", "empty_list_data",
                                          "list_data", "false_data", "bad_key", "missing_type",
                                          "bad_seq", "bad_turn_id", "missing_retry_key", "null_retry_key", "empty_retry_key", "invalid_retry_key"])
def test_retry_unknown_journal_evidence_refuses_without_copy(tmp_path, monkeypatch, corruption):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("corrupt")
    original, paths = source(session)
    runner = make_runner(FakeEngine([{"text": "retry"}]), FakeRegistry([]))
    async def setup():
        first = await retry(runner, session, original)
        await runner.run_accepted(session, session.session_id, first)
        await runner.delete_last_turn(manager, session.session_id)
    run(setup())
    lines = session.journal.path.read_bytes().split(b"\n")
    for index, line in enumerate(lines):
        if not line:
            continue
        record = json.loads(line)
        if record.get("type") != "turn_queued":
            continue
        if corruption == "json":
            lines[index] = line[:-1]
        elif corruption == "non_object":
            lines[index] = b'["lost accepted-key object"]'
        elif corruption == "utf8":
            lines[index] = b'\xffinvalid encoding'
        else:
            if corruption == "missing_type":
                record.pop("type")
            elif corruption == "bad_key":
                record["data"]["client_turn_id"] = ["retry-key-0001"]
            elif corruption == "missing_retry_key":
                record["data"].pop("client_turn_id")
            elif corruption in {"null_retry_key", "empty_retry_key", "invalid_retry_key"}:
                record["data"]["client_turn_id"] = {"null_retry_key": None, "empty_retry_key": "", "invalid_retry_key": "bad"}[corruption]
            elif corruption == "bad_seq":
                record["seq"] = True
            elif corruption == "bad_turn_id":
                record["turn_id"] = None
            else:
                record["data"] = {"null_data": None, "empty_list_data": [],
                                  "list_data": [1], "false_data": False}[corruption]
            lines[index] = json.dumps(record).encode()
    session.journal.path.write_bytes(b"\n".join(lines))
    before = session.history_log.read_bytes()
    monkeypatch.setattr(session, "copy_retry_inputs", lambda *_: pytest.fail("unknown evidence must not copy inputs"))
    with pytest.raises(TurnConflict, match="retry_lookup_unavailable"):
        run(retry(runner, session, original))
    assert session.journal.turn_state == "none" and session.history_log.read_bytes() == before
    assert all(path.exists() for path in paths) and not list(session.session_dir.rglob("retry_*"))


@pytest.mark.parametrize("bad", [b'{bad json}\n', b'[1,2]\n', b'\xff\n'])
def test_reverse_corruption_mode_is_explicit_and_defaults_stay_tolerant(tmp_path, bad):
    path = tmp_path / "records.jsonl"
    path.write_bytes(b'{"old":1}\n' + bad + b'{"new":2}\n')
    jsonl.reset_stats()
    assert list(jsonl.iter_records_reversed(path)) == [{"new": 2}, {"old": 1}]
    assert jsonl.bad_line_stats == {}
    with pytest.raises(ValueError):
        list(jsonl.iter_records_reversed(path, complete=True, reject_corruption=True))


def test_reverse_complete_mode_ignores_byte_ceiling_only_when_requested(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_bytes(b'{"old":1}\n' + json.dumps({"padding": "x" * (4 * 1024 * 1024 + 1024)}).encode() + b'\n{"new":2}\n')
    assert not any(record.get("old") for record in jsonl.iter_records_reversed(path))
    assert any(record.get("old") for record in jsonl.iter_records_reversed(path, complete=True, reject_corruption=True))


def test_complete_reverse_ascii_blanks_and_absent_legacy_journal(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_bytes(b' \t\r\n{"one":1}\n\n{"two":2}')
    assert list(jsonl.iter_records_reversed(path, complete=True, reject_corruption=True)) == [{"two": 2}, {"one": 1}]
    assert list(jsonl.iter_records_reversed(tmp_path / "absent", complete=True, reject_corruption=True)) == []


@pytest.mark.parametrize("byte_ceiling", [0, -1, 3])
def test_complete_mode_overrides_zero_negative_or_partial_window(tmp_path, byte_ceiling):
    path = tmp_path / "records.jsonl"
    path.write_bytes(b'{"first":1}\n{"last":2}\n')
    assert list(jsonl.iter_records_reversed(path, max_bytes=byte_ceiling, complete=True,
                                            reject_corruption=True)) == [{"last": 2}, {"first": 1}]


def test_unknown_type_shape_remains_tolerated_by_ordinary_consumer(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_bytes(b'{"type":"not-a-known-event","data":{}}\n')
    assert list(jsonl.iter_records_reversed(path)) == [{"type": "not-a-known-event", "data": {}}]

@pytest.mark.parametrize("key", [None, ""])
def test_ordinary_queued_without_key_is_valid_retry_lookup_evidence(tmp_path, key):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("ordinary")
    original, _ = source(session, media=False)
    session.journal.path.write_text(json.dumps({"seq": 1, "turn_id": "old", "type": "turn_queued", "data": {"client_turn_id": key}}) + "\n")
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    result = run(retry(runner, session, original))
    assert not result.get("duplicate") and session.journal.turn_state == "queued"
