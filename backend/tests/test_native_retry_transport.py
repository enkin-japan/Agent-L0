"""R17 actual native Store/API/SSE; synthetic URLProtocol, CPU-only."""
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from .native_test_runtime import run_native_case
ROOT=Path(__file__).resolve().parents[2]
pytestmark=pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")
def compile_retry(tmp, *, store_text=None, chat_text=None, harness=None):
    source=ROOT/"apple-app/Sources"
    store=source/"Store/AgentStore.swift";chat=source/"Store/ChatMessage.swift"
    if store_text is not None: store=tmp/"AgentStore.swift";store.write_text(store_text)
    if chat_text is not None: chat=tmp/"ChatMessage.swift";chat.write_text(chat_text)
    binary=tmp/"retry"
    result=subprocess.run(["xcrun","swiftc","-parse-as-library","-module-cache-path",str(tmp/"modules"),
        *map(str,sorted((source/"API").glob("*.swift"))),str(source/"Store/TurnReducer.swift"),str(chat),str(store),str(ROOT/"apple-app/Tests/recovery_support.swift"),str(harness or ROOT/"apple-app/Tests/retry_transport_test.swift"),"-o",str(binary)],capture_output=True,text=True,timeout=90)
    assert result.returncode == 0,result.stdout+result.stderr
    return binary
@pytest.fixture(scope="module")
def native_retry(tmp_path_factory):return compile_retry(tmp_path_factory.mktemp("native-retry"))
@pytest.mark.parametrize("mode",["prefix-1","prefix-2","prefix-3","prefix-4","prefix-5","prefix-6","prefix-7","no-delete","legacy","invalid-projection","worker-ui","edit-tail","no-ref","confirm-switch","confirm-aba","confirm-source","404","ambiguous","plain-key","late-aba","late-error-aba","terminal-success","terminal-fail","early-terminal","thin","history-error","duplicate","audit-approval","fresh-key","history-before-terminal","old-new","duplicate-projection","invalid-group","audit-readonly","branch-winner","confirm-cancel","confirm-client","restart-ambiguous","terminal-warning","terminal-cancel","old-before-new","fix-edit-suspended","fix-edit-approval","fix-edit-first-suspended","fix-edit-first-approval","fix-edit-literal","fix-coverage-missing","fix-coverage-invalid","fix-coverage-partial","fix-conflict-input","fix-conflict-source","fix-conflict-lookup","fix-edit-late-round","fix-edit-aba"])
def test_native_retry_transport(native_retry,mode,tmp_path):
    result=run_native_case(native_retry,[ROOT/"protocol/fixtures",mode],tmp_path)
    assert result.returncode == 0,result.stdout+result.stderr


@pytest.mark.parametrize("mutation,mode,expected",[
    ("display_projection","prefix-5","016 server display indices drive native history with canonical indices"),
    ("duplicate_projection","duplicate-projection","projection boundary validation avoids duplicated or out-of-range audit records"),
    ("combined-group-visibility","invalid-group","projection boundary validation avoids duplicated or out-of-range audit records"),
    ("no_delete","no-delete","confirmation never executes/deletes before user decision"),
    ("missing_ref","no-ref","missing canonical ref explicitly unavailable"),
    ("confirmation_source","confirm-source","source change expires bound confirmation"),
    ("confirmation_owner","confirm-aba","changed confirmation target explicitly invalid, never retargeted"),
    ("operation_key","plain-key","plain sameprompt must not reuse retry command key"),
    ("ambiguous_key","ambiguous","ambiguous retry resends exact command key"),
    ("raw_coverage","terminal-fail","hidden raw canonical replay neither appends nor reloads forever"),
    ("pending_submit","old-before-new","old retry restore preserves already in-flight later submit ownership"),
    ("audit_readonly","audit-readonly","audit messages cannot edit or regenerate even current canonical tail"),
    ("worker_kind","worker-ui","background workers have no chat controls"),
    ("paused_edit_prompt","fix-edit-suspended","editing preserves actual canonical/queued raw prompt including literal attachment marker"),
    ("first_paused_edit","fix-edit-first-approval","shared edit eligibility allows actual paused input and keeps audit readonly"),
    ("literal_edit_prompt","fix-edit-literal","editing preserves actual canonical/queued raw prompt including literal attachment marker"),
    ("edit_reply_owner","fix-edit-late-round","late paused edit response cannot detach or prefill newer turn"),
    ("edit_reply_aba","fix-edit-aba","ABA late edit response cannot mutate new selected projection"),
    ("audit_coverage","fix-coverage-missing","incomplete audit coverage falls back to every raw record with no residual groups"),
    ("audit_fallback_groups","fix-coverage-partial","incomplete audit coverage falls back to every raw record with no residual groups"),
    ("lookup_unknown","fix-conflict-lookup","unverifiable lookup remains unknown with same command key and resync"),
    ("input_guidance","fix-conflict-input","known retry conflict has actionable localized guidance"),
])
def test_native_retry_mutation(tmp_path,mutation,mode,expected):
    source=ROOT/"apple-app/Sources"
    store=(source/"Store/AgentStore.swift").read_text()
    chat=(source/"Store/ChatMessage.swift").read_text()
    if mutation=="display_projection":
        old='let displayed = HistoryProjection.indices(history)'
        assert store.count(old)==1
        store=store.replace(old,'let displayed = Array(history.turns.indices)')
    elif mutation=="duplicate_projection":
        old='Set(requested).count == requested.count,'
        assert chat.count(old)==1
        chat=chat.replace(old,'true,')
    elif mutation=="combined-group-visibility":
        old='indices.allSatisfy({ $0 >= 0 && $0 < history.turns.count }),'
        assert chat.count(old)==1
        chat=chat.replace(old,'true,')
        # Both production layers stay intact: this isolated fault removes the
        # complete user-visible protection, not the redundant member-only guard.
        for old,new in [('guard covered == Set(0..<count) else { return Array(0..<count) }','guard true else { return Array(0..<count) }'),('guard covered == Set(history.turns.indices) else { return [] }','guard true else { return [] }')]:
            assert chat.count(old)==1
            chat=chat.replace(old,new)
    elif mutation=="no_delete":
        old='func regenerateLastTurn() { requestRetryConfirmation() }'
        assert store.count(old)==1
        store=store.replace(old,'func regenerateLastTurn() { if let client, let sid = selectedSessionId { Task { _ = try? await client.deleteLastTurn(sessionId: sid) } }; requestRetryConfirmation() }')
    elif mutation=="missing_ref":
        old='guard let canonicalRef = lastTurnRef, !canonicalRef.isEmpty, !canonicalTurns.isEmpty else {'
        assert store.count(old)==1
        store=store.replace(old,'let canonicalRef = lastTurnRef ?? ""; guard !canonicalTurns.isEmpty else {')
    elif mutation=="confirmation_source":
        old='lastTurnRef == target.canonicalRef,'
        assert store.count(old)==1
        store=store.replace(old,'true,')
    elif mutation=="confirmation_owner":
        old="""guard let client, retryConfirmationId == target.id,
              isCurrent(target.sessionId, target.selectionEpoch, client),
              ObjectIdentifier(client) == target.clientIdentity,"""
        assert store.count(old)==1
        store=store.replace(old,'guard let client, true,')
    elif mutation=="operation_key":
        old='if outboxAll()[sessionId]?["operation"] != "retry",\n           let pending = outboxRead'
        assert store.count(old)==1
        store=store.replace(old,'if let pending = outboxRead')
    elif mutation=="ambiguous_key":
        old='let key = uncertain ? (pending?["key"] ?? UUID().uuidString.lowercased()) : UUID().uuidString.lowercased()'
        assert store.count(old)==1
        store=store.replace(old,'let key = UUID().uuidString.lowercased()')
    elif mutation=="raw_coverage":
        old='let archived = index.map { $0 >= 0 && $0 < knownTurnCount } ?? false'
        assert store.count(old)==1
        store=store.replace(old,'let archived = index.map { idx in messages.contains { $0.turnIndex == idx } } ?? false')
        old='index == nil || index! < 0 || index! >= knownTurnCount'
        assert store.count(old)==1
        store=store.replace(old,'index == nil || !messages.contains(where: { $0.turnIndex == index })')
    elif mutation=="pending_submit":
        old="""historyProjectionNeedsRefresh = true
        attachmentId = nil"""
        assert store.count(old)==1
        store=store.replace(old,'historyProjectionNeedsRefresh = true\n        detach()\n        attachmentId = nil')
    elif mutation=="audit_readonly":
        old='!auditOnly && turnIndex != nil && turnIndex == canonicalIndex'
        assert chat.count(old)==1
        chat=chat.replace(old,'turnIndex != nil && turnIndex == canonicalIndex')
    elif mutation=="paused_edit_prompt":
        old='EditableAttemptTarget(prompt: prompt, pausedTurnId: id,'
        assert store.count(old)==1
        store=store.replace(old,'EditableAttemptTarget(prompt: canonicalTurns.last?.prompt ?? prompt, pausedTurnId: id,')
    elif mutation=="first_paused_edit":
        old='guard let id = liveTurnId, let prompt = liveRawPrompt else { return nil }'
        assert store.count(old)==1
        store=store.replace(old,'guard !canonicalTurns.isEmpty, let id = liveTurnId, let prompt = liveRawPrompt else { return nil }')
    elif mutation=="literal_edit_prompt":
        old='composerPrefill = source.prompt'
        assert store.count(old)==1
        store=store.replace(old,'composerPrefill = source.prompt.components(separatedBy: "\\n📎").first!')
    elif mutation=="edit_reply_owner":
        old='guard isCurrent(sid, epoch, client), ownsEditReply(source) else { return }'
        assert store.count(old)==1
        store=store.replace(old,'guard isCurrent(sid, epoch, client) else { return }')
    elif mutation=="edit_reply_aba":
        old='guard isCurrent(sid, epoch, client), ownsEditReply(source) else { return }'
        assert store.count(old)==1
        store=store.replace(old,'guard true else { return }')
    elif mutation=="audit_coverage":
        old='guard covered == Set(0..<count) else { return Array(0..<count) }'
        assert chat.count(old)==1
        chat=chat.replace(old,'guard true else { return Array(0..<count) }')
    elif mutation=="audit_fallback_groups":
        old='guard displayed == history.displayTurnIndices else { return [] }'
        assert chat.count(old)==1
        chat=chat.replace(old,'guard true else { return [] }')
    elif mutation=="lookup_unknown":
        old='if conflict.reason != "retry_lookup_unavailable" { self.markRetryRejected(sessionId: sid, receipt: receipt) }'
        assert store.count(old)==1
        store=store.replace(old,'if true { self.markRetryRejected(sessionId: sid, receipt: receipt) }')
    elif mutation=="input_guidance":
        old='self.errorMessage = L("重试所需输入或附件不完整。请检查尝试记录，并重新附上所需输入。")'
        assert store.count(old)==1
        store=store.replace(old,'self.errorMessage = conflict.reason')
    else:
        old='kind != "summarize" && kind != "prewarm"'
        assert store.count(old)==1
        store=store.replace(old,'kind != "summarize"')
    # Complete production Store/API/SSE compile in temporary copies, never live mutations.
    binary=compile_retry(tmp_path,store_text=store,chat_text=chat)
    result=run_native_case(binary,[ROOT/"protocol/fixtures",mode],tmp_path/"case")
    assert result.returncode==1 and "FAIL: "+expected in result.stdout,result.stdout+result.stderr
    print(f"Mutation {mutation}: intended failure: {result.stdout.strip()}")


def test_native_retry_case_domain_isolation(native_retry,tmp_path):
    # fresh-key intentionally leaves an unacknowledged command for nextRef.
    # Later independent cases must retain their exact source_ref assertions.
    first=run_native_case(native_retry,[ROOT/"protocol/fixtures","fresh-key"],tmp_path/"first")
    assert first.returncode==0,first.stdout+first.stderr
    for index,mode in enumerate(["history-before-terminal","old-new","restart-ambiguous","terminal-warning","terminal-cancel"]):
        result=run_native_case(native_retry,[ROOT/"protocol/fixtures",mode],tmp_path/str(index))
        assert result.returncode==0,result.stdout+result.stderr
