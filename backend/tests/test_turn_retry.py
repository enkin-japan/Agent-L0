"""R17 explicit new attempt: owned inputs, exact source and retry-key guards; CPU only."""
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from framework.api import routes, schemas
from framework.core import turn_runner as turns
from framework.memory.session import SessionManager

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_turn_runner import GEN, history_lines, make_runner, run
from .test_turn_runner import submit


def source(session, *, media=True):
    inputs = [session.image_dir / "page.png", session.audio_dir / "memo.wav",
              session.attach_dir / "report.pdf"]
    for n, path in enumerate(inputs):
        path.write_bytes(bytes([0, 255, n]) + b"original binary")
    record = {"status": "success", "prompt": "原始完整请求", "response": [{"role": "assistant", "content": "旧答案"}],
              "image": [str(inputs[0])] if media else [], "audio": [str(inputs[1])] if media else [],
              "attachments": [{"path": str(inputs[2]), "type": "pdf", "name": "report.pdf"},
                              {"path": str(inputs[0]), "type": "image", "name": "page.png"}] if media else [],
              "ingested_text": f"PDF全文 [Original file: {inputs[2]}]" if media else "完整原始文本",
              "enable_thinking": True, "thinking_budget": 1536, "max_tokens": 2049,
              "temperature": 0.37, "min_p": 0.08}
    session.append_log(record)
    return record, inputs


def retry(runner, session, record, key="retry-key-0001"):
    return runner.accept_retry(session, session.session_id,
                               expected_turn_ref=turns.turn_reference(record), client_turn_id=key)


def test_exact_inputs_gen_independent_bytes_and_original_archive(make_session):
    session = make_session()
    original, paths = source(session)
    old_bytes = session.history_log.read_bytes()
    runner = make_runner(FakeEngine([{"text": "新答案"}]), FakeRegistry([]))
    ctx = run(retry(runner, session, original))
    assert ctx["prompt"] == original["prompt"]
    assert ctx["gen"] == {k: original[k] for k in GEN}
    assert ctx["retry_of"] == turns.turn_reference(original)
    assert ctx["retry_root_index"] == ctx["retry_parent_index"] == 0
    copied = [ctx["images"][0], ctx["audios"][0], ctx["attachments"][0]["path"]]
    for old, new in zip(paths, copied):
        assert Path(new).read_bytes() == old.read_bytes() and Path(new) != old
        assert Path(new).stat().st_ino != old.stat().st_ino
    assert ctx["attachments"][1]["path"] == ctx["images"][0]  # one copy per repeated source
    assert copied[2] in ctx["ingested_text"] and str(paths[2]) not in ctx["ingested_text"]
    assert session.history_log.read_bytes() == old_bytes
    result = run(runner.run_accepted(session, session.session_id, ctx))
    archived = history_lines(session)
    assert len(archived) == 2 and archived[0] == original
    assert result["retry_of"] == turns.turn_reference(original)
    assert result["retry_root_index"] == result["retry_parent_index"] == 0


@pytest.mark.parametrize("state", ["queued", "running", "awaiting_approval", "suspended"])
def test_active_or_paused_retry_refuses_without_changing_input(make_session, state):
    session = make_session()
    original, paths = source(session)
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    session.journal.turn_state = state
    before = session.history_log.read_bytes()
    async def scenario():
        # Lock held simulates a long generation: refusal must be a fast path.
        if state == "running":
            async with session.lock:
                with pytest.raises(turns.TurnConflict):
                    await asyncio.wait_for(retry(runner, session, original), 0.5)
        else:
            with pytest.raises(turns.TurnConflict):
                await retry(runner, session, original)
    run(scenario())
    assert session.history_log.read_bytes() == before
    assert all(p.exists() for p in paths)
    assert not list(session.session_dir.rglob("retry_*"))


def test_source_ref_mismatch_and_aba_refuse(make_session):
    session = make_session()
    original, _ = source(session, media=False)
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    changed = dict(original, prompt="changed but same history count")
    session.history_log.write_text(json.dumps(changed) + "\n")
    with pytest.raises(turns.TurnConflict, match="retry_source_changed"):
        run(retry(runner, session, original))
    assert history_lines(session) == [changed]


@pytest.mark.parametrize("terminal", ["success", "fail", "cancelled"])
def test_retry_key_never_reexecutes_inflight_or_terminal(make_session, terminal):
    session = make_session()
    original, _ = source(session, media=False)
    class Engine(FakeEngine):
        async def generate_once(self, *a, **kw):
            if terminal == "fail":
                raise RuntimeError("synthetic retry failure")
            return await super().generate_once(*a, **kw)
    engine = Engine([{"cancelled": True}] if terminal == "cancelled" else [{"text": "new"}])
    runner = make_runner(engine, FakeRegistry([]))
    async def scenario():
        accepted = await retry(runner, session, original)
        dup1 = await retry(runner, session, original)
        assert dup1["duplicate"] and dup1["turn_id"] == accepted["turn_id"]
        await runner.run_accepted(session, session.session_id, accepted)
        # Source is now stale; same retry key must win BEFORE source check.
        dup2 = await retry(runner, session, original)
        assert dup2["duplicate"] and dup2["turn_id"] == accepted["turn_id"]
        assert len(history_lines(session)) == 2
        fresh = await retry(runner, session, history_lines(session)[-1], key="retry-key-0002")
        assert not fresh.get("duplicate")
    run(scenario())


@pytest.mark.parametrize("forged", ["missing", "outside", "other_session", "symlink", "assets"])
def test_retry_media_ownership_refuses(make_session, tmp_path, forged):
    session = make_session()
    original, paths = source(session)
    if forged == "missing":
        bad = session.image_dir / "absent.png"
    elif forged == "outside":
        bad = tmp_path / "outside.png"
        bad.write_bytes(b"outside")
    elif forged == "other_session":
        bad = make_session("other").image_dir / "other.png"
        bad.write_bytes(b"other")
    elif forged == "assets":
        bad = session.session_dir / "assets/output.png"
        bad.parent.mkdir()
        bad.write_bytes(b"asset")
    else:
        bad = session.image_dir / "link.png"
        bad.symlink_to(paths[0])
    original["image"] = [str(bad)]
    session.history_log.write_text(json.dumps(original) + "\n")
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    before = session.history_log.read_bytes()
    with pytest.raises(turns.TurnConflict, match="retry_inputs_unavailable"):
        run(retry(runner, session, original))
    assert session.history_log.read_bytes() == before and all(p.exists() for p in paths)
    assert not list(session.session_dir.rglob("retry_*"))


def test_partial_copy_failure_cleans_only_new_files(make_session, monkeypatch):
    session = make_session()
    original, paths = source(session)
    before = list(session.history)
    real = session._copy_retry_file
    calls = []
    def broken(src, dst):
        calls.append(dst)
        real(src, dst)
        if len(calls) == 2:
            raise OSError("synthetic partial copy failure")
    monkeypatch.setattr(session, "_copy_retry_file", broken)
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    with pytest.raises(turns.TurnConflict, match="retry_inputs_unavailable"):
        run(retry(runner, session, original))
    assert all(p.exists() for p in paths) and all(not p.exists() for p in calls)
    assert len(history_lines(session)) == 1 and session.history == before


def test_accept_failure_rolls_back_and_cleans_retry_copies(make_session, monkeypatch):
    session = make_session()
    original, paths = source(session)
    before = list(session.history)
    def fail(*a, **k):
        raise OSError("synthetic inflight write failure")
    monkeypatch.setattr(session, "write_inflight", fail)
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    with pytest.raises(OSError):
        run(retry(runner, session, original))
    assert all(p.exists() for p in paths) and not list(session.session_dir.rglob("retry_*"))
    assert session.history == before and session.images == [] and session.audios == []
    assert session.journal.turn_state == "none" and len(history_lines(session)) == 1


def test_delete_retry_keeps_original_attachment_bytes(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("retry")
    original, paths = source(session)
    runner = make_runner(FakeEngine([{"text": "new answer"}]), FakeRegistry([]))
    async def scenario():
        ctx = await retry(runner, session, original)
        await runner.run_accepted(session, session.session_id, ctx)
        await runner.delete_last_turn(manager, session.session_id)
    run(scenario())
    assert all(p.exists() for p in paths) and history_lines(session) == [original]


def test_retry_uses_normal_approval_gate(make_session):
    session = make_session()
    original, _ = source(session, media=False)
    registry = FakeRegistry([FakeTool("danger", approval=True, read_only=False)])
    runner = make_runner(FakeEngine([{"tool_calls": [call("danger")]}]), registry)
    async def scenario():
        ctx = await retry(runner, session, original)
        return await runner.run_accepted(session, session.session_id, ctx)
    result = run(scenario())
    assert result["status"] == "pending_approval" and registry.executed == []


def test_public_ref_matches_actual_logged_private_meta_and_history_snapshot(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("retry")
    original, _ = source(session, media=False)
    original["response"] = [{"role": "tool", "tool_responses": [{"name": "probe", "response": "data", "_safe": True,
                                                                 "render": {"_private": 1, "value": 2}}]}]
    session.history_log.unlink()
    session.append_log(original)
    terminal = turns.public_turn({**original, "turn_index": 0})
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_manager=manager)))
    history = run(routes.get_history(session.session_id, req))
    assert terminal["turn_ref"] == history["last_turn_ref"]
    assert terminal["turn_ref"] == turns.turn_reference(history["turns"][-1])
    assert "turn_ref" not in history["turns"][-1]
    # Old records without timestamps still obtain a usable reference.
    old = {"status": "success", "prompt": "legacy", "response": []}
    session.history_log.write_text(json.dumps(old) + "\n")
    assert run(routes.get_history(session.session_id, req))["last_turn_ref"] == turns.turn_reference(old)


def test_retry_route_is_thin_authenticated_router_path_and_missing_refusal(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("retry")
    original, _ = source(session, media=False)
    runner = make_runner(FakeEngine([{"text": "new answer"}]), FakeRegistry([]))
    spawned = []
    runner.spawn = lambda sid, coro: (spawned.append(sid), coro.close())
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_manager=manager, turn_runner=runner)))
    body = schemas.RetryRequest(expected_turn_ref=turns.turn_reference(original), client_turn_id="retry-key-0001")
    response = run(routes.submit_retry("retry", req, body))
    assert response.status_code == 202 and spawned == ["retry"]
    duplicate = run(routes.submit_retry("retry", req, body))
    assert duplicate.status_code == 202 and json.loads(duplicate.body)["duplicate"]
    assert spawned == ["retry"]
    missing = run(routes.submit_retry("missing", req, body))
    assert missing.status_code == 404 and not (manager.sessions_dir / "missing").exists()
    assert any(r.path.endswith("/turns/retry") for r in routes.router.routes)


def _family_rows():
    root = {"status": "success", "prompt": "root", "response": []}
    relation = {"retry_root_index": 0, "retry_of": "tr_" + "0" * 64}
    return [root,
            {**root, **relation, "status": "fail", "retry_parent_index": 0},
            {**root, **relation, "retry_parent_index": 1},
            {**root, **relation, "status": "warning", "retry_parent_index": 2},
            {**root, **relation, "status": "cancelled", "retry_parent_index": 3},
            {**root, **relation, "retry_parent_index": 4},
            {"status": "success", "prompt": "ordinary", "response": []}]


def test_family_success_projection_only_replaces_display_and_undo_rewinds():
    rows = _family_rows()
    assert turns.display_turn_indices(rows) == [5, 6]
    assert turns.display_turn_indices(rows[:6]) == [5]
    assert turns.display_turn_indices(rows[:5]) == [2]
    assert turns.display_turn_indices(rows[:4]) == [2]
    assert turns.display_turn_indices(rows[:3]) == [2]
    assert turns.display_turn_indices(rows[:2]) == [0]
    assert turns.display_turn_indices(rows[:1]) == [0]
    assert turns.retry_history_projection(rows) == {
        "display_turn_indices": [5, 6],
        "retry_groups": [{"root_index": 0, "display_index": 5, "turn_indices": list(range(6))}]}
    for prefix, winner in [(6, 5), (5, 2), (4, 2), (3, 2), (2, 0)]:
        group = turns.retry_history_projection(rows[:prefix])["retry_groups"]
        assert group == [{"root_index": 0, "display_index": winner, "turn_indices": list(range(prefix))}]
    assert turns.retry_history_projection(rows[:1])["retry_groups"] == []


@pytest.mark.parametrize("bad", [
    {"retry_root_index": True, "retry_parent_index": 0},
    {"retry_root_index": -1, "retry_parent_index": 0},
    {"retry_root_index": 99, "retry_parent_index": 0},
    {"retry_root_index": 0, "retry_parent_index": 99},
    {"retry_root_index": 0, "retry_parent_index": 0},  # parent is not the last attempt
    {"retry_root_index": 0, "retry_parent_index": 1},  # parent belongs to another root
    {"retry_root_index": "0", "retry_parent_index": 1},
])
def test_malformed_family_metadata_cannot_hide_unrelated_history(bad):
    rows = [{"status": "success", "prompt": "A", "response": []},
            {"status": "success", "prompt": "B", "response": []},
            {"status": "success", "prompt": "forged", "response": [],
             "retry_of": "tr_" + "0" * 64, **bad}]
    assert turns.display_turn_indices(rows) == [0, 1, 2]
    assert turns.retry_history_projection(rows)["retry_groups"] == []


def test_history_projection_uses_raw_snapshot_and_preserves_indices(tmp_path, monkeypatch):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("family")
    rows = _family_rows()
    session.history_log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_manager=manager)))
    checked = []
    original_validation = turns.retry_family_root
    def validate_once(record, index, previous_root):
        checked.append(index)
        return original_validation(record, index, previous_root)
    monkeypatch.setattr(turns, "retry_family_root", validate_once)
    history = run(routes.get_history("family", req))
    assert checked == list(range(7))  # one validation traversal produces both projections
    assert history["turns"] == rows and history["count"] == 7
    assert history["display_turn_indices"] == [5, 6]
    assert history["retry_groups"] == [{"root_index": 0, "display_index": 5, "turn_indices": list(range(6))}]
    assert schemas.HistoryResponse.model_validate(history).model_dump()["retry_groups"] == history["retry_groups"]
    assert history["last_turn_ref"] == turns.turn_reference(rows[-1])


def test_latest_failed_retry_inherits_root_and_keeps_complete_inputs(make_session):
    session = make_session()
    original, _ = source(session)
    runner = make_runner(FakeEngine([{"cancelled": True}, {"text": "success"}]), FakeRegistry([]))
    async def scenario():
        first = await retry(runner, session, original)
        await runner.run_accepted(session, session.session_id, first)
        cancelled = history_lines(session)[-1]
        second = await retry(runner, session, cancelled, key="retry-key-0002")
        assert second["retry_root_index"] == 0 and second["retry_parent_index"] == 1
        assert second["prompt"] == original["prompt"] and second["gen"] == first["gen"]
        await runner.run_accepted(session, session.session_id, second)
    run(scenario())
    assert turns.display_turn_indices(history_lines(session)) == [2]


@pytest.mark.parametrize("operation", ["copy", "inflight"])
def test_cancelled_accept_waits_for_worker_and_cleans_new_inputs(make_session, monkeypatch, operation):
    session = make_session()
    original, paths = source(session)
    before = list(session.history)
    release = threading.Event()
    method = "copy_retry_inputs" if operation == "copy" else "write_inflight"
    real = getattr(session, method)
    loop_holder = {}
    def gated(*args, **kwargs):
        loop_holder["loop"].call_soon_threadsafe(loop_holder["entered"].set)
        assert release.wait(3)
        return real(*args, **kwargs)
    monkeypatch.setattr(session, method, gated)
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    async def scenario():
        loop_holder.update(loop=asyncio.get_running_loop(), entered=asyncio.Event())
        attempt = asyncio.create_task(retry(runner, session, original))
        await loop_holder["entered"].wait()
        attempt.cancel()
        await asyncio.sleep(0)
        assert not attempt.done()  # worker still owns its pending filesystem writes
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await attempt
    run(asyncio.wait_for(scenario(), 5))
    assert all(p.exists() for p in paths) and not list(session.session_dir.rglob("retry_*"))
    assert not session.inflight_path.exists() and session.history == before
    assert session.journal.turn_state == "none"


@pytest.mark.parametrize("operation", ["copy", "inflight"])
def test_repeated_cancel_keeps_worker_result_until_files_are_cleaned(make_session, monkeypatch, operation):
    session = make_session()
    original, paths = source(session)
    before = list(session.history)
    release = threading.Event()
    method = "copy_retry_inputs" if operation == "copy" else "write_inflight"
    real = getattr(session, method)
    state = {}
    def gated(*args, **kwargs):
        state["loop"].call_soon_threadsafe(state["entered"].set)
        assert release.wait(3)
        return real(*args, **kwargs)
    monkeypatch.setattr(session, method, gated)
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    async def scenario():
        state.update(loop=asyncio.get_running_loop(), entered=asyncio.Event())
        async def dispatch_barrier():
            # FIFO callback fence lets the cancellation handler run; no elapsed-time sleep.
            fence = state["loop"].create_future()
            state["loop"].call_soon(fence.set_result, None)
            await fence
        attempt = asyncio.create_task(retry(runner, session, original))
        try:
            await state["entered"].wait()
            attempt.cancel()
            await dispatch_barrier()
            attempt.cancel()
            await dispatch_barrier()
            assert not attempt.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await attempt
        finally:
            release.set()
    run(asyncio.wait_for(scenario(), 2))
    assert all(p.exists() for p in paths) and not list(session.session_dir.rglob("retry_*"))
    assert not session.inflight_path.exists() and session.history == before
    assert session.journal.turn_state == "none"


def test_copy_collision_never_deletes_a_preexisting_file(make_session, monkeypatch):
    from framework.memory import session as storage
    session = make_session()
    original, _ = source(session)
    collision = session.image_dir / ("retry_" + "f" * 32 + ".png")
    collision.write_bytes(b"preexisting unrelated file")
    monkeypatch.setattr(storage.uuid, "uuid4", lambda: SimpleNamespace(hex="f" * 32))
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    with pytest.raises(turns.TurnConflict, match="retry_inputs_unavailable"):
        run(retry(runner, session, original))
    assert collision.read_bytes() == b"preexisting unrelated file"


def test_branch_media_hash_changes_but_family_and_retry_inputs_stay_local(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("parent")
    original, paths = source(session)
    runner = make_runner(FakeEngine([{"text": "new answer"}, {"text": "branch answer"}]), FakeRegistry([]))
    async def scenario():
        ctx = await retry(runner, session, original)
        await runner.run_accepted(session, session.session_id, ctx)
        await manager.branch_session("parent", node_index=1, new_session_id="child")
        child = manager.get_or_create("child")
        raw = history_lines(child)
        assert turns.turn_reference(raw[0]) != turns.turn_reference(original)
        assert turns.display_turn_indices(raw) == [1]
        assert turns.retry_history_projection(raw)["retry_groups"] == [
            {"root_index": 0, "display_index": 1, "turn_indices": [0, 1]}]
        for path in paths:
            path.unlink()
        assert "parent/attach" not in raw[-1]["ingested_text"]
        new = await retry(runner, child, raw[-1], key="retry-branch-0001")
        assert new["retry_root_index"] == 0 and new["retry_parent_index"] == 1
        await runner.run_accepted(child, "child", new)
        return child
    child = run(scenario())
    assert turns.display_turn_indices(history_lines(child)) == [2]
    assert turns.retry_history_projection(history_lines(child))["retry_groups"] == [
        {"root_index": 0, "display_index": 2, "turn_indices": [0, 1, 2]}]


@pytest.mark.parametrize("kind", ["file", "audio"])
def test_unknown_provenance_input_is_rejected_not_reopened(make_session, tmp_path, kind):
    session = make_session()
    original, _ = source(session)
    original["ingested_text"] = f"[Original {kind}: {tmp_path / 'external.bin'}]"
    session.history_log.write_text(json.dumps(original) + "\n")
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    with pytest.raises(turns.TurnConflict, match="retry_inputs_unavailable"):
        run(retry(runner, session, original))


def test_concurrent_same_retry_key_accepts_one_copy_and_one_execution(make_session):
    session = make_session()
    original, _ = source(session)
    engine = FakeEngine([{"text": "one attempt"}])
    runner = make_runner(engine, FakeRegistry([]))
    async def scenario():
        both = await asyncio.gather(retry(runner, session, original), retry(runner, session, original))
        accepted = next(ctx for ctx in both if not ctx.get("duplicate"))
        assert sum(bool(ctx.get("duplicate")) for ctx in both) == 1
        assert both[0]["turn_id"] == both[1]["turn_id"]
        assert len(list(session.session_dir.rglob("retry_*"))) == 3
        await runner.run_accepted(session, session.session_id, accepted)
    run(scenario())
    assert len(engine.calls) == 1 and len(history_lines(session)) == 2


def test_source_ref_is_rechecked_after_waiting_for_accept_lock(make_session):
    session = make_session()
    original, _ = source(session, media=False)
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    async def scenario():
        async with session.lock:
            pending = asyncio.create_task(retry(runner, session, original))
            await asyncio.sleep(0)
            changed = dict(original, prompt="new source behind the same count")
            session.history_log.write_text(json.dumps(changed) + "\n")
        with pytest.raises(turns.TurnConflict, match="retry_source_changed"):
            await pending
    run(scenario())
    assert len(history_lines(session)) == 1 and not list(session.session_dir.rglob("retry_*"))


def test_projection_fixture_is_shared_and_prefix_stable():
    root = Path(__file__).resolve().parents[2]
    fixture = json.loads((root / "protocol/fixtures/016-task-retry-inputs.history.json").read_text())
    assert turns.display_turn_indices(fixture["turns"]) == fixture["display_turn_indices"]
    assert turns.retry_history_projection(fixture["turns"])["retry_groups"] == fixture["retry_groups"]
    assert [turns.display_turn_indices(fixture["turns"][:n]) for n in range(1, len(fixture["turns"]) + 1)] \
        == fixture["prefix_display_turn_indices"]
    assert [turns.retry_history_projection(fixture["turns"][:n])["retry_groups"]
            for n in range(1, len(fixture["turns"]) + 1)] == fixture["prefix_retry_groups"]
    events = [json.loads(line) for line in
              (root / "protocol/fixtures/016-task-retry-inputs.events.jsonl").read_text().splitlines()]
    archived = events[2]["data"]
    queued = events[3]["data"]
    assert queued["retry_of"] == archived["turn_ref"] == turns.turn_reference(archived)
    assert queued["retry_root_index"] == queued["retry_parent_index"] == 0


def test_delete_multiple_retry_attempts_rewinds_winner_keeps_original(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("undo")
    original, paths = source(session)
    runner = make_runner(FakeEngine([{"text": "first success"}, {"cancelled": True},
                                     {"text": "latest success"}]), FakeRegistry([]))
    async def scenario():
        parent = original
        for n in range(3):
            ctx = await retry(runner, session, parent, key=f"retry-delete-{n:04}")
            await runner.run_accepted(session, "undo", ctx)
            parent = history_lines(session)[-1]
        assert turns.display_turn_indices(history_lines(session)) == [3]
        for expected in ([1], [1], [0]):
            await runner.delete_last_turn(manager, "undo")
            assert turns.display_turn_indices(history_lines(session)) == expected
            remaining = history_lines(session)
            groups = turns.retry_history_projection(remaining)["retry_groups"]
            assert groups == ([{"root_index": 0, "display_index": expected[0],
                                "turn_indices": list(range(len(remaining)))}] if len(remaining) > 1 else [])
            assert all(path.exists() for path in paths)
    run(scenario())
    assert history_lines(session) == [original]


def test_retry_journal_lookup_failure_cannot_mean_no_duplicate(make_session, monkeypatch):
    from framework.core import jsonl
    session = make_session()
    original, _ = source(session)
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    def unreadable(*a, **kw):
        raise OSError("synthetic unreadable journal")
    monkeypatch.setattr(jsonl, "iter_records_reversed", unreadable)
    with pytest.raises(turns.TurnConflict, match="retry_lookup_unavailable"):
        run(retry(runner, session, original))
    assert len(history_lines(session)) == 1 and not list(session.session_dir.rglob("retry_*"))


def test_prepared_retry_never_dispatches_or_rescans_a_stored_slash_prompt(make_session, monkeypatch):
    from framework.agents import runtime
    session = make_session()
    original, _ = source(session, media=False)
    original.update(prompt="/stored-skill complete input", ingested_text="already prepared skill and input text")
    session.history_log.write_text(json.dumps(original) + "\n")
    monkeypatch.setattr(runtime, "get_skill_registry", lambda: pytest.fail("retry must not process inputs again"))
    runner = make_runner(FakeEngine([]), FakeRegistry([]))
    ctx = run(retry(runner, session, original))
    assert ctx["prompt"] == original["prompt"] and ctx["ingested_text"] == original["ingested_text"]


def test_retry_key_stays_consumed_after_ordinary_window_and_tail_undo(tmp_path):
    manager = SessionManager(tmp_path / "sessions")
    session = manager.get_or_create("durable-retry")
    original, _ = source(session, media=False)
    engine = FakeEngine([{"text": "one retry"}] + [{"text": "ordinary"} for _ in range(65)])
    runner = make_runner(engine, FakeRegistry([]))
    async def scenario():
        first = await retry(runner, session, original)
        await runner.run_accepted(session, session.session_id, first)
        for _ in range(65):
            await submit(runner, session, session.session_id, "ordinary")
        for _ in range(66):
            await runner.delete_last_turn(manager, session.session_id)
        assert history_lines(session) == [original]
        same_request = await retry(runner, session, original)
        assert same_request.get("duplicate") is True
        assert same_request["turn_id"] == first["turn_id"]
    run(scenario())
