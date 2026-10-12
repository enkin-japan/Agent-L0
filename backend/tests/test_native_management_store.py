"""R19 management through actual native Store/API/SSE; CPU-only fake transport."""
from pathlib import Path
import pytest
from .test_native_retry_transport import compile_retry,pytestmark
from .native_test_runtime import run_native_case
ROOT=Path(__file__).resolve().parents[2]
@pytest.fixture(scope='module')
def native_management(tmp_path_factory):
    tmp=tmp_path_factory.mktemp('native-management')
    header=(ROOT/'apple-app/Tests/recovery_transport_test.swift').read_text().split('@main struct RecoveryTests')[0]
    harness=tmp/'management.swift';harness.write_text(header+(ROOT/'apple-app/Tests/management_store_test.swift').read_text())
    return compile_retry(tmp,harness=harness)
@pytest.mark.parametrize('mode',['delete-live-terminal','delete-history-progress','schedule-action-stale','schedule-action-deleting','schedule-action-inflight','delete-false','delete-absent','delete-legacy-missing','delete-http','delete-inflight','schedule-feed','delete-partial','delete-aba','delete-other-submit','delete-client-cleanup','delete-writes','delete-retry-confirm','delete-paused','submit-inflight','submit-two-aba','submit-unknown','retry-inflight','schedule-global-false','schedule-live-feed','schedule-unknown','schedule-stale','schedule-http','schedule-late','schedule-client','runs-stale','runs-http','runs-late','doc-false','doc-http','doc-legacy','doc-zero','doc-client','ui'])
def test_native_management(native_management,mode,tmp_path):
    result=run_native_case(native_management,[mode,ROOT],tmp_path)
    (tmp_path/'case-output.txt').write_text(result.stdout+result.stderr)
    assert result.returncode==0,result.stdout+result.stderr

@pytest.mark.parametrize('mutation,mode,expected',[
    ('receipt-status','delete-false','unconfirmed deletion preserves active session and displayed winner'),
    ('not-found','delete-absent','explicit shared not_found receipt satisfies local deletion target'),
    ('delete-fence','delete-writes','deleting target blocks all task writes through shared predicate'),
    ('submission-admission','submit-inflight','active submission HTTP prohibits racing session deletion'),
    ('submission-settle','submit-unknown','timeout HTTP returned but receipt unknown'),
    ('delete-epoch','delete-aba','late delete receipt cannot detach new selection/ABA'),
    ('delete-cleanup','delete-client-cleanup','old delete cleanup cannot clear newer client deletion fence'),
    ('schedule-request','schedule-late','old schedule reply cannot overwrite newer authoritative snapshot'),
    ('runs-request','runs-late','old schedule reply cannot overwrite newer authoritative snapshot'),
    ('schedule-retain','schedule-stale','schedule failure preserves last successful rows and marks stale'),
    ('api-schedule-status','schedule-unknown','initial schedule failure remains unknown, never successful empty'),
    ('api-rag-status','doc-false','document delete requires receipt while legacy/zero-chunk success remains compatible'),
])
def test_native_management_mutation(tmp_path,mutation,mode,expected):
    source=ROOT/'apple-app/Sources'
    store=(source/'Store/AgentStore.swift').read_text()
    client=(source/'API/AgentClient.swift').read_text()
    if mutation=='receipt-status':
        old='if accepted { result.deleted.insert(id) }';assert store.count(old)==1
        store=store.replace(old,'if true { result.deleted.insert(id) }')
    elif mutation=='not-found':
        old='response.status || response.reason == "not_found"';assert store.count(old)==1
        store=store.replace(old,'response.status')
    elif mutation=='delete-fence':
        old='func isSessionDeleting(_ sid: String) -> Bool { deletingSessionIDs.contains(sid) }';assert store.count(old)==1
        store=store.replace(old,'func isSessionDeleting(_ sid: String) -> Bool { false }')
    elif mutation=='submission-admission':
        old='!isSessionDeleting($0) && !hasSubmissionInFlight($0)';assert store.count(old)==1
        store=store.replace(old,'!isSessionDeleting($0)')
        old='isSessionDeleting($0) || hasSubmissionInFlight($0)';assert store.count(old)==1
        store=store.replace(old,'isSessionDeleting($0)')
    elif mutation=='submission-settle':
        old='self?.submissionHTTPRequests[submittedReceipt] = nil';assert store.count(old)==4
        store=store.replace(old,'_ = self')
    elif mutation=='delete-epoch':
        old='result.deleted.contains(sid), isCurrent(sid, epoch, client),';assert store.count(old)==1
        store=store.replace(old,'result.deleted.contains(sid), true,')
    elif mutation=='delete-cleanup':
        old='for id in available where sessionDeleteRequests[id] == operation';assert store.count(old)==1
        store=store.replace(old,'for id in available where true')
    elif mutation=='schedule-request':
        old='schedulesRequest == request';assert store.count(old)>=3
        store=store.replace(old,'true')
    elif mutation=='runs-request':
        old='scheduleRunsRequests[id] == request';assert store.count(old)==2
        store=store.replace(old,'true')
    elif mutation=='schedule-retain':
        old='schedulesFreshness = schedulesFreshness == .unknown ? .unknown : .stale';assert store.count(old)==1
        store=store.replace(old,'schedules = []; '+old)
    elif mutation=='api-schedule-status':
        old='guard res.status else { throw AgentError.http(200, L("定时任务读取未被受理，请刷新重试。")) }';assert client.count(old)==1
        client=client.replace(old,'// removed status acceptance guard')
    elif mutation=='api-rag-status':
        start=client.index('    func ragDeleteDocument(');end=client.index('    @discardableResult',start)
        old='guard result.status != false else { throw AgentError.http(200, result.msg ?? L("删除未被受理。")) }'
        assert client[start:end].count(old)==1
        client=client[:start]+client[start:end].replace(old,'// removed explicit refusal guard')+client[end:]
    # All production files copied as a stable input snapshot; only fault is in temporary copy.
    import shutil,subprocess
    api=tmp_path/'API';shutil.copytree(source/'API',api)
    (api/'AgentClient.swift').write_text(client)
    mapped=tmp_path/'AgentStore.swift';mapped.write_text(store)
    header=(ROOT/'apple-app/Tests/recovery_transport_test.swift').read_text().split('@main struct RecoveryTests')[0]
    harness=tmp_path/'management.swift';harness.write_text(header+(ROOT/'apple-app/Tests/management_store_test.swift').read_text())
    binary=tmp_path/'management'
    compiled=subprocess.run(['xcrun','swiftc','-parse-as-library','-module-cache-path',str(tmp_path/'modules'),
        *map(str,sorted(api.glob('*.swift'))),str(source/'Store/TurnReducer.swift'),str(source/'Store/ChatMessage.swift'),str(mapped),
        str(ROOT/'apple-app/Tests/recovery_support.swift'),str(harness),'-o',str(binary)],capture_output=True,text=True,timeout=90)
    assert compiled.returncode==0,compiled.stdout+compiled.stderr
    result=run_native_case(binary,[mode,ROOT],tmp_path/'case')
    (tmp_path/'mutation-output.txt').write_text(result.stdout+result.stderr)
    assert result.returncode==1 and 'FAIL: '+expected in result.stdout,result.stdout+result.stderr
    print(f'Mutation {mutation}: {result.stdout.strip()}')


@pytest.mark.parametrize('fault',[False,True])
def test_native_management_post_fence(tmp_path,fault):
    # Admission and egress independently protect the same window. Disable only
    # admission as a deliberate test prerequisite, then expose queued task timing.
    # Production retains both guards, and no Store method is replaced.
    source=ROOT/'apple-app/Sources'
    store=(source/'Store/AgentStore.swift').read_text()
    store=store.replace('!isSessionDeleting($0) && !hasSubmissionInFlight($0)','!isSessionDeleting($0)')
    store=store.replace('isSessionDeleting($0) || hasSubmissionInFlight($0)','isSessionDeleting($0)')
    old='guard self?.shouldBlockSubmission(sid, client: client, receipt: submittedReceipt, epoch: epoch) != true else { return }'
    assert store.count(old)==1
    store=store.replace(old,'await ManagementSubmissionGate.wait()\n            '+('/* deliberate loss of egress protection */' if fault else old))
    header=(ROOT/'apple-app/Tests/recovery_transport_test.swift').read_text().split('@main struct RecoveryTests')[0]
    harness=tmp_path/'management.swift';harness.write_text(header+(ROOT/'apple-app/Tests/management_store_test.swift').read_text())
    binary=compile_retry(tmp_path,store_text=store,harness=harness)
    result=run_native_case(binary,['egress-fence',ROOT],tmp_path/'case')
    (tmp_path/'proof-output.txt').write_text(result.stdout+result.stderr)
    if fault:
        assert result.returncode==1 and 'FAIL: queued submit rechecks deletion before real POST' in result.stdout,result.stdout+result.stderr
    else:assert result.returncode==0,result.stdout+result.stderr
    print(f'Post-fence admission prerequisite disabled, egress fault={fault}: {result.stdout.strip()}')


@pytest.mark.parametrize('mutation,mode,expected',[
    ('old-projection-owner','delete-live-terminal','confirmed delete after original cancelled terminal clears selected session'),
    ('schedule-authority','schedule-action-stale','schedule write checks authoritative freshness and parent deletion at actual egress'),
    ('schedule-delete-owner','schedule-action-deleting','schedule write checks authoritative freshness and parent deletion at actual egress'),
])
def test_native_management_review_mutation(tmp_path,mutation,mode,expected):
    store=(ROOT/'apple-app/Sources/Store/AgentStore.swift').read_text()
    if mutation=='old-projection-owner':
        anchor='        let receipt = optimisticReceipt, intent = submissionIntentEpoch'
        assert store.count(anchor)==1
        store=store.replace(anchor,anchor+'\n        let capturedRawCount = canonicalTurns.count')
        old='submissionIntentEpoch == intent,'
        assert store.count(old)==1
        store=store.replace(old,old+' capturedRawCount == canonicalTurns.count,')
    elif mutation=='schedule-authority':
        old='schedulesFreshness == .fresh && schedules.contains { $0.id == id } &&'
        assert store.count(old)==1
        store=store.replace(old,'true &&')
    else:
        old='!isSessionDeleting("schedule_" + id) && !hasSubmissionInFlight("schedule_" + id)'
        assert store.count(old)==1
        store=store.replace(old,'!hasSubmissionInFlight("schedule_" + id)')
    header=(ROOT/'apple-app/Tests/recovery_transport_test.swift').read_text().split('@main struct RecoveryTests')[0]
    harness=tmp_path/'management.swift';harness.write_text(header+(ROOT/'apple-app/Tests/management_store_test.swift').read_text())
    binary=compile_retry(tmp_path,store_text=store,harness=harness)
    result=run_native_case(binary,[mode,ROOT],tmp_path/'case')
    (tmp_path/'mutation-output.txt').write_text(result.stdout+result.stderr)
    assert result.returncode==1 and 'FAIL: '+expected in result.stdout,result.stdout+result.stderr
    print(f'Review mutation {mutation}: {result.stdout.strip()}')

@pytest.mark.parametrize('fault',[False,True])
def test_native_management_local_intent(tmp_path,fault):
    store=(ROOT/'apple-app/Sources/Store/AgentStore.swift').read_text()
    old='func isSessionDeleting(_ sid: String) -> Bool { deletingSessionIDs.contains(sid) }'
    assert store.count(old)==1
    store=store.replace(old,'func isSessionDeleting(_ sid: String) -> Bool { false }')
    if fault:
        old='submissionIntentEpoch == intent,'
        assert store.count(old)==1
        store=store.replace(old,'true,')
    header=(ROOT/'apple-app/Tests/recovery_transport_test.swift').read_text().split('@main struct RecoveryTests')[0]
    harness=tmp_path/'management.swift';harness.write_text(header+(ROOT/'apple-app/Tests/management_store_test.swift').read_text())
    binary=compile_retry(tmp_path,store_text=store,harness=harness)
    result=run_native_case(binary,['delete-new-intent',ROOT],tmp_path/'case')
    (tmp_path/'proof-output.txt').write_text(result.stdout+result.stderr)
    if fault:
        assert result.returncode==1 and 'FAIL: new settled local submission intent keeps ownership after receipts disappear' in result.stdout,result.stdout+result.stderr
    else:assert result.returncode==0,result.stdout+result.stderr
    print(f'Local-intent fence prerequisite disabled, nonce fault={fault}: {result.stdout.strip()}')
