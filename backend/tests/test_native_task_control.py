"""Production native Store/API/SSE; synthetic URLProtocol only, no services."""
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
ROOT=Path(__file__).resolve().parents[2]
pytestmark=pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")
def compile_control(tmp, *, store_text=None, chat_text=None, harness=None):
    source=ROOT/"apple-app/Sources"
    binary=tmp/"control"
    store=source/"Store/AgentStore.swift"
    chat=source/"Store/ChatMessage.swift"
    if store_text is not None:
        store=tmp/"AgentStore.swift";store.write_text(store_text)
    if chat_text is not None:
        chat=tmp/"ChatMessage.swift";chat.write_text(chat_text)
    harness=harness or ROOT/"apple-app/Tests/task_control_test.swift"
    result=subprocess.run(["xcrun","swiftc","-parse-as-library","-module-cache-path",str(tmp/"modules"),
        *map(str,sorted((source/"API").glob("*.swift"))),*map(str,sorted((source / "Store").glob("Offline*.swift"))), str(source/"Store/TurnReducer.swift"),str(chat),str(store),
        str(ROOT/"apple-app/Tests/recovery_support.swift"),str(harness),"-o",str(binary)],capture_output=True,text=True,timeout=90)
    assert result.returncode == 0, result.stdout+result.stderr
    return binary
@pytest.fixture(scope="module")
def native_control(tmp_path_factory):
    return compile_control(tmp_path_factory.mktemp("native-control"))
@pytest.mark.parametrize("mode",["facts","facts-missing","facts-empty","facts-fail","facts-truncated-result","facts-abandoned-suspended","facts-abandoned-approval","facts-abandoned-existing","facts-warning-generic","facts-success","facts-live","facts-history","queued","cancel-refusal","cancel-http","cancel-accepted","suspend-refusal","suspend-http","suspend-accepted","suspend-late-suspended","suspend-late-suspended-late-error","suspend-late-terminal","suspend-old-round","suspend-old-round-late-error","suspend-switch","suspend-switch-aba","suspend-switch-aba-late-error","resume-accepted","resume-http","resume-late-terminal","resume-switch-aba","long-tool","snapshot-unknown","snapshot-stale","snapshot-false","snapshot-overlap","task-no-turn","task-queued","task-running","client-change","review-paused-cancel-suspended","review-paused-cancel-approval","review-paused-ui","review-resume-floor","review-turn-floor","review-old-round","review-old-round-ack","review-resume-action","review-cancel-action","review-resume-late-http","review-resume-aba-late-http","review-new-round","review-attach-advanced"])
def test_native_task_control(native_control,mode):
    result=subprocess.run([str(native_control),str(ROOT/"protocol/fixtures"),mode],capture_output=True,text=True,timeout=28)
    assert result.returncode == 0,result.stdout+result.stderr


@pytest.mark.parametrize("mutation,mode", [
    ("phase", "queued"), ("request_identity", "suspend-late-suspended-late-error"),
    ("journal_confirmation", "suspend-late-suspended"), ("snapshot_retention", "snapshot-stale"),
    ("snapshot_order", "snapshot-overlap"), ("execution_facts", "facts"),
    ("fake_timeout", "long-tool"), ("receipt_reuse", "facts-abandoned-existing"),
    ("warning_identity", "facts-warning-generic"),
    ("cancel_phase", "review-paused-cancel-suspended"), ("control_floor", "review-resume-floor"),
    ("cancel_action", "review-cancel-action"), ("resume_action", "review-resume-action"),
    ("new_round_floor", "review-old-round"), ("ack_during_replay", "review-old-round-ack"),
    ("fixed_floor", "review-attach-advanced"), ("paused_stop_ui", "review-paused-ui"),
])
def test_native_task_control_mutation(tmp_path, mutation, mode):
    source=ROOT/"apple-app/Sources"
    store=(source/"Store/AgentStore.swift").read_text()
    chat=(source/"Store/ChatMessage.swift").read_text()
    extra_args=[]
    if mutation == "phase":
        old='(kind == .suspend ? phase == "running" : kind == .resume ? phase == "suspended" : Self.canCancelTask(phase: phase))'
        assert store.count(old) == 1
        store=store.replace(old,'true')
    elif mutation == "request_identity":
        old='self.controlRequest?.id == id,'
        assert store.count(old) == 2
        store=store.replace(old,'true,')
    elif mutation == "journal_confirmation":
        old='if newRound || confirmed { controlRequest = nil }'
        assert store.count(old) == 1
        store=store.replace(old,'if newRound || confirmed { /* protection removed */ }')
    elif mutation == "snapshot_retention":
        old='globalTasksFreshness = globalTasksFreshness == .unknown ? .unknown : .stale'
        assert store.count(old) == 2
        store=store.replace(old,'globalTasks = []; '+old)
    elif mutation == "snapshot_order":
        old='self.client === client, globalSnapshotRequest == request'
        assert store.count(old) == 2
        store=store.replace(old,'self.client === client')
    elif mutation == "execution_facts":
        old='let facts = turn.executionReceipts'
        assert chat.count(old) == 1
        chat=chat.replace(old,'let facts = Optional<ExecutionReceipts>.none')
    elif mutation == "receipt_reuse":
        old='if !started, let existing = out.firstIndex(where:'
        assert chat.count(old) == 1
        chat=chat.replace(old,'if !started && false, let existing = out.firstIndex(where:')
    elif mutation == "warning_identity":
        old='let abandoned = turn.status == "warning" &&'
        assert chat.count(old) == 1
        chat=chat.replace(old,'let abandoned = turn.status == "warning" ||')
    elif mutation == "cancel_phase":
        old='["queued", "running"].contains(phase)'
        assert store.count(old) == 1
        store=store.replace(old,'["queued", "running", "suspended", "awaiting_approval"].contains(phase)')
    elif mutation == "control_floor":
        old='let newerThanRequest = env.seq > request.observedSeq'
        assert store.count(old) == 1
        store=store.replace(old,'let newerThanRequest = true')
    elif mutation == "cancel_action":
        old='case .cancel: matchesAction = env.type == "turn_ended"'
        assert store.count(old) == 1
        store=store.replace(old,'case .cancel: matchesAction = ["suspended", "resumed", "turn_ended"].contains(env.type)')
    elif mutation == "resume_action":
        old='case .resume: matchesAction = ["resumed", "turn_started", "turn_ended"].contains(env.type)'
        assert store.count(old) == 1
        store=store.replace(old,'case .resume: matchesAction = ["suspended", "resumed", "turn_started", "turn_ended"].contains(env.type)')
    elif mutation == "new_round_floor":
        old='let newRound = newerThanRequest && env.type == "turn_queued"'
        assert store.count(old) == 1
        store=store.replace(old,'let newRound = env.type == "turn_queued"')
    elif mutation == "ack_during_replay":
        old=' || self.cursor <= observedSeq'
        assert store.count(old) == 2
        store=store.replace(old,'')
    elif mutation == "fixed_floor":
        old='let observedSeq: Int'
        assert store.count(old) == 1
        store=store.replace(old,'var observedSeq: Int')
        old='let ts = try await client.turnState(sessionId: sessionId)'
        assert store.count(old) == 1
        store=store.replace(old,old+'\n                    if var pending = controlRequest { pending.observedSeq = max(pending.observedSeq, ts.lastSeq ?? 0); controlRequest = pending }')
    elif mutation == "paused_stop_ui":
        view=(source/"Views/SessionListView.swift").read_text()
        old='if AgentStore.canCancelTask(phase: task.status) {'
        assert view.count(old) == 1
        modified=tmp_path/"SessionListView.swift"
        modified.write_text(view.replace(old,'if true {'))
        extra_args=[str(modified)]
    else:
        old='self.controlRequest?.accepted = true'
        assert store.count(old) == 1
        store=store.replace(old,old+'\n                    Task { try? await Task.sleep(nanoseconds: 15_000_000_000); self.controlRequest = nil }')
    # Compile complete production sources in a temporary copy, never mutate live files.
    binary=compile_control(tmp_path,store_text=store,chat_text=chat)
    result=subprocess.run([str(binary),str(ROOT/"protocol/fixtures"),mode,*extra_args],capture_output=True,text=True,timeout=28)
    expected={
        "phase":"queued does not send suspend",
        "request_identity":"obsolete failure cannot change selected/new round errors",
        "journal_confirmation":"obsolete or already confirmed ack cannot revive request",
        "snapshot_retention":"failure retains last successful task snapshot",
        "snapshot_order":"older snapshot cannot overwrite latest success",
        "execution_facts":"cancel retains started and returned execution facts",
        "fake_timeout":"long tool still waits for safety point beyond 15 seconds",
        "receipt_reuse":"retained exact tool result is reused without whole-payload duplication",
        "warning_identity":"generic warning is never inferred to be cancelled or abandoned",
        "cancel_phase":"paused state never sends /task/cancel",
        "control_floor":"old suspended/resumed replay cannot confirm newer control request",
        "cancel_action":"unrelated suspension/resumption never confirms cancel",
        "resume_action":"new suspension never confirms resume",
        "new_round_floor":"older queued round cannot supersede snapshot-owned request",
        "ack_during_replay":"timeout ack while older projection is replaying",
        "fixed_floor":"timeout new action-matched journal confirmation",
        "paused_stop_ui":"paused task center UI omits Stop via shared phase eligibility",
    }[mutation]
    assert result.returncode == 1 and "FAIL: "+expected in result.stdout, result.stdout+result.stderr
    print(f"Mutation {mutation}: intended failure: {result.stdout.strip()}")
