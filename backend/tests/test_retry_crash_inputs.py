"""Retry write-ahead preserves prepared inputs on crash; recovery never executes or recopies."""
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from framework.core.turn_runner import TurnConflict, display_turn_indices, turn_reference
from framework.memory.session import ChatSession, SessionManager

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_resume_budget import PausingEngine
from .test_turn_retry import source, retry
from .test_turn_runner import GEN, history_lines, make_runner, run


@pytest.mark.parametrize("phase", ["queued", "awaiting_approval", "suspended"])
def test_retry_crash_keeps_prepared_inputs_family_and_explicit_next_attempt(tmp_path, monkeypatch, phase):
    base = tmp_path / "sessions"
    manager = SessionManager(base)
    session = manager.get_or_create("crash-retry")
    original, _ = source(session)
    registry = FakeRegistry([FakeTool("danger", approval=True), FakeTool("probe")])
    engine = (PausingEngine([{"tool_calls": [call("probe")]}]) if phase == "suspended"
              else FakeEngine([{"tool_calls": [call("danger")]}] if phase == "awaiting_approval" else []))
    runner = make_runner(engine, registry)
    async def accept():
        ctx = await retry(runner, session, original)
        if phase != "queued":
            await runner.run_accepted(session, session.session_id, ctx)
        return ctx
    ctx = run(accept())
    assert session.journal.turn_state == phase
    marker = json.loads(session.inflight_path.read_text())
    assert marker["input_snapshot"]["image"] == ctx["images"]
    calls_before = list(registry.executed)
    restarted = SessionManager(base)
    with monkeypatch.context() as patch:
        patch.setattr(ChatSession, "copy_retry_inputs", lambda *_: pytest.fail("recovery cannot copy inputs"))
        entries = restarted.recover_inflight_turns()
    assert len(entries) == 1 and entries[0]["turn_id"] == ctx["turn_id"]
    archived = history_lines(session)
    failed = archived[-1]
    assert failed["status"] == "fail" and len(archived) == 2
    for key, ctx_key in [("image", "images"), ("audio", "audios"), ("attachments", "attachments"),
                         ("ingested_text", "ingested_text"), ("retry_of", "retry_of"),
                         ("retry_root_index", "retry_root_index"), ("retry_parent_index", "retry_parent_index")]:
        assert failed[key] == ctx[ctx_key]
    assert {key: failed[key] for key in ctx["gen"]} == ctx["gen"]
    assert display_turn_indices(archived) == [0] and registry.executed == calls_before
    assert not session.inflight_path.exists()
    restarted.emit_recovery_events(entries)
    restored = restarted.get_or_create(session.session_id)
    # Failed attempts stay out of model context, but their prepared input is auditable.
    assert not any(m.get("role") == "system" and "中断" in str(m.get("content")) for m in restored.history)
    next_runner = make_runner(FakeEngine([]), FakeRegistry([]))
    async def repeat():
        duplicate = await retry(next_runner, restored, original)
        assert duplicate.get("duplicate") and duplicate["turn_id"] == ctx["turn_id"]
        new = await retry(next_runner, restored, failed, key="retry-after-crash-0002")
        assert new["retry_root_index"] == 0 and new["retry_parent_index"] == 1
        assert new["gen"] == ctx["gen"] and new["prompt"] == ctx["prompt"]
        assert len(new["images"]) == len(new["audios"]) == 1 and len(new["attachments"]) == 2
    run(repeat())


def test_retry_legacy_source_defaults_match_during_crash_validation(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("legacy-source")
    original = {"status": "success", "prompt": "legacy source", "response": []}
    session.append_log(original)
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    ctx = run(retry(runner, session, original))
    SessionManager(manager.sessions_dir).recover_inflight_turns()
    failed = history_lines(session)[-1]
    assert failed["retry_of"] == turn_reference(original)
    assert failed["retry_root_index"] == failed["retry_parent_index"] == 0
    assert {key: failed[key] for key in ctx["gen"]} == ctx["gen"]


@pytest.mark.parametrize("fault", ["root", "parent", "source_ref", "source_changed", "bad_shape"])
def test_bad_prepared_marker_cannot_claim_an_unrelated_family(tmp_path, fault):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("bad-marker")
    original, _ = source(session)
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    ctx = run(retry(runner, session, original))
    data = json.loads(session.inflight_path.read_text())
    prepared = data["input_snapshot"]
    if fault == "root":
        prepared["retry_root_index"] = True
    elif fault == "parent":
        prepared["retry_parent_index"] = 99
    elif fault == "source_ref":
        prepared["retry_of"] = "tr_" + "0" * 64
    elif fault == "source_changed":
        session.append_log({"status": "success", "prompt": "unrelated new tail", "response": []})
    else:
        prepared["image"] = "not a list"
    session.inflight_path.write_text(json.dumps(data))
    recovered = SessionManager(manager.sessions_dir).recover_inflight_turns()
    assert len(recovered) == 1
    records = history_lines(session)
    failed = records[-1]
    assert failed["status"] == "fail"
    assert all(key not in failed for key in ("retry_of", "retry_root_index", "retry_parent_index"))
    assert "校验" in failed["response"][0]["content"]
    if fault != "bad_shape":
        assert failed["image"] == ctx["images"]  # valid-shaped input facts remain, never accessed by recovery
    assert display_turn_indices(records) == list(range(len(records)))


def test_bad_marker_does_not_block_other_recovery_and_old_marker_stays_compatible(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    bad = manager.get_or_create("bad")
    good = manager.get_or_create("good")
    bad.inflight_path.write_text(json.dumps({"prompt": "bad gen", "gen": {"max_tokens": "bad"},
                                           "input_snapshot": []}))
    good.write_inflight("old marker", {"max_tokens": 512}, "t_legacy")
    assert "input_snapshot" not in json.loads(good.inflight_path.read_text())
    recovered = manager.recover_inflight_turns()
    assert {record["session_id"] for record in recovered} == {"bad", "good"}
    old = history_lines(good)[-1]
    assert old["prompt"] == "old marker" and old["max_tokens"] == 512
    assert old["image"] == old["audio"] == old["attachments"] == []
    assert "retry_of" not in old
    assert old["retry_input_unavailable"] is True


def test_first_ordinary_multimodal_crash_retains_inputs_for_explicit_retry(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("ordinary-crash")
    original, _ = source(session)
    session.history_log.unlink()  # simulate the first input, with no prior archived turn
    gen = {key: original[key] for key in GEN}
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    ctx = run(runner.accept_turn(session, session.session_id, prompt=original["prompt"],
                                 images=original["image"], audios=original["audio"],
                                 ingested_text=original["ingested_text"], attachments=original["attachments"],
                                 gen=gen, client_turn_id="ordinary-crash-key"))
    restarted = SessionManager(manager.sessions_dir)
    entries = restarted.recover_inflight_turns()
    assert len(entries) == 1
    failed = history_lines(session)[0]
    assert failed["image"] == ctx["images"] and failed["audio"] == ctx["audios"]
    assert failed["attachments"] == ctx["attachments"] and failed["ingested_text"] == ctx["ingested_text"]
    assert not failed.get("retry_input_unavailable") and "retry_of" not in failed
    fresh = restarted.get_or_create(session.session_id)
    retry_ctx = run(retry(make_runner(FakeEngine([]), FakeRegistry([])), fresh, failed))
    assert retry_ctx["gen"] == gen and len(retry_ctx["images"]) == len(retry_ctx["audios"]) == 1
    assert len(retry_ctx["attachments"]) == 2


def test_old_marker_recovery_refuses_unknown_input_retry_without_copy(tmp_path, monkeypatch):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("old-marker")
    session.write_inflight("legacy input", {}, "t_legacy")
    entries = manager.recover_inflight_turns()
    manager.emit_recovery_events(entries)
    failed = history_lines(session)[0]
    assert failed["retry_input_unavailable"] is True
    assert "重新附" in failed["response"][0]["content"]
    monkeypatch.setattr(session, "copy_retry_inputs", lambda *_: pytest.fail("unknown inputs cannot be recopied"))
    with pytest.raises(TurnConflict, match="retry_inputs_unavailable"):
        run(retry(make_runner(FakeEngine([]), FakeRegistry([])), session, failed))


def test_partial_snapshot_marks_unavailable_but_preserves_valid_fields(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("partial")
    session.write_inflight("input", {}, "t_partial")
    data = json.loads(session.inflight_path.read_text())
    data["input_snapshot"] = {"image": ["/synthetic/owned/image.png"], "audio": [], "attachments": []}
    session.inflight_path.write_text(json.dumps(data))
    manager.recover_inflight_turns()
    failed = history_lines(session)[0]
    assert failed["retry_input_unavailable"] is True
    assert failed["image"] == ["/synthetic/owned/image.png"]


def test_actual_ingestion_route_input_serializes_and_recovers_without_reingestion(tmp_path, monkeypatch):
    from fastapi import UploadFile
    from framework.api import routes
    from framework.ingestion.router import Ingestion
    from framework.ingestion.parsers import asr, pdf
    manager = SessionManager(tmp_path / "sessions")
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    monkeypatch.setattr(asr, "transcribe_audio", lambda *a, **k: "synthetic voice transcript")
    monkeypatch.setattr(pdf, "parse_pdf", lambda *a, **k: {"mode": "text", "pages": 1, "text": "synthetic pdf text"})
    ingestion = Ingestion(inference_cfg={"modalities": ["text", "image"]})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        orchestrator=runner.orchestrator, scheduler=runner.scheduler, session_manager=manager, ingestion=ingestion)))
    uploads = [UploadFile(filename=name, file=BytesIO(b"synthetic upload bytes"))
               for name in ("page.png", "memo.wav", "report.pdf")]
    async def scenario():
        _, _, session, io = await routes._prepare_turn(request, "ingested", "", uploads)
        assert all(isinstance(a, dict) and isinstance(a["path"], str) for a in io["attachments"])
        ctx = await runner.accept_turn(session, "ingested", prompt="original complete input", gen=dict(GEN), **io)
        marker = json.loads(session.inflight_path.read_text())
        assert marker["input_snapshot"]["attachments"] == io["attachments"]
        restarted = SessionManager(manager.sessions_dir)
        restarted.recover_inflight_turns()
        failed = history_lines(session)[-1]
        assert failed["attachments"] == io["attachments"]
        assert failed["image"] == io["images"] and failed["ingested_text"] == ctx["ingested_text"]
        assert not failed.get("retry_input_unavailable")
    run(scenario())


def test_retry_duplicate_precedes_recovered_unknown_input_refusal(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("unknown-accepted")
    original, _ = source(session)
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    ctx = run(retry(runner, session, original))
    data = json.loads(session.inflight_path.read_text())
    data.pop("input_snapshot")
    session.inflight_path.write_text(json.dumps(data))
    restarted = SessionManager(manager.sessions_dir)
    entries = restarted.recover_inflight_turns()
    restarted.emit_recovery_events(entries)
    fresh = restarted.get_or_create(session.session_id)
    duplicate = run(retry(make_runner(FakeEngine([]), FakeRegistry([])), fresh, original))
    assert duplicate.get("duplicate") and duplicate["turn_id"] == ctx["turn_id"]


def test_snapshot_uses_the_same_json_safe_path_representation_as_archive(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("path-metadata")
    uploaded = session.attach_dir / "uploaded.bin"
    uploaded.write_bytes(b"synthetic input")
    attachments = [{"path": Path(uploaded), "name": "uploaded.bin", "type": "attach"}]
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    run(runner.accept_turn(session, session.session_id, prompt="input", images=[], audios=[],
                           ingested_text="", attachments=attachments, gen=dict(GEN)))
    marker = json.loads(session.inflight_path.read_text())
    assert marker["input_snapshot"]["attachments"][0]["path"] == str(uploaded)
    SessionManager(manager.sessions_dir).recover_inflight_turns()
    assert history_lines(session)[-1]["attachments"] == marker["input_snapshot"]["attachments"]


def test_atomic_input_marker_failure_preserves_previous_marker(tmp_path, monkeypatch):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("atomic")
    previous = b'{"prompt":"previous marker","gen":{},"turn_id":"t_old"}'
    session.inflight_path.write_bytes(previous)
    real_replace = Path.replace
    def failed_replace(path, destination):
        if destination == session.inflight_path:
            raise OSError("synthetic atomic replace failure")
        return real_replace(path, destination)
    monkeypatch.setattr(Path, "replace", failed_replace)
    with pytest.raises(OSError):
        session.write_inflight("new input", dict(GEN), "t_new",
                               input_snapshot={"image": [], "audio": [], "attachments": [], "ingested_text": "input"})
    assert session.inflight_path.read_bytes() == previous


def test_ordinary_queued_bad_key_is_unknown_even_without_retry_tag(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("ordinary-bad-key")
    original, _ = source(session, media=False)
    session.journal.path.write_text(json.dumps({"seq": 1, "turn_id": "old", "type": "turn_queued",
                                              "data": {"client_turn_id": ["not a string key"]}}) + "\n")
    with pytest.raises(TurnConflict, match="retry_lookup_unavailable"):
        run(retry(make_runner(FakeEngine([]), FakeRegistry([])), session, original))
    assert session.journal.turn_state == "none"

@pytest.mark.parametrize("fault", ["missing", "null", "object", "empty"])
def test_recovery_requires_known_prompt_but_accepts_empty_multimodal_prompt(tmp_path, fault):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("prompt-integrity")
    original, _ = source(session)
    session.history_log.unlink()
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    run(runner.accept_turn(session, session.session_id, prompt="" if fault == "empty" else original["prompt"],
        images=original["image"], audios=original["audio"], attachments=original["attachments"],
        ingested_text=original["ingested_text"], gen={key: original[key] for key in GEN}))
    marker = json.loads(session.inflight_path.read_text())
    if fault == "missing": marker.pop("prompt")
    elif fault == "null": marker["prompt"] = None
    elif fault == "object": marker["prompt"] = {"not": "the original user text"}
    session.inflight_path.write_text(json.dumps(marker))
    restarted = SessionManager(manager.sessions_dir)
    entries = restarted.recover_inflight_turns()
    failed = history_lines(session)[0]
    assert failed["image"] == original["image"] and failed["attachments"] == original["attachments"]
    assert failed["prompt"] == ""  # unknown values must not become invented user text
    restarted.emit_recovery_events(entries)
    current = restarted.get_or_create(session.session_id)
    next_runner = make_runner(FakeEngine([]), FakeRegistry([]))
    if fault == "empty":
        assert not failed.get("retry_input_unavailable")
        ctx = run(retry(next_runner, current, failed))
        assert ctx["prompt"] == "" and len(ctx["images"]) == 1
    else:
        assert failed.get("retry_input_unavailable") is True
        with pytest.raises(TurnConflict, match="retry_inputs_unavailable"):
            run(retry(next_runner, current, failed))
