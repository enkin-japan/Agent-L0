#!/bin/bash
# Isolated guard mutations; production files are never edited.
set -euo pipefail
cd "$(dirname "$0")"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
python3 - "$scratch" <<'PY'
from pathlib import Path
import sys,plistlib,uuid,shutil
root=Path(sys.argv[1]);source=Path('../Sources/Store')
# Compile one fixed dependency snapshot even while another owner integrates API work.
shutil.copytree('../Sources/API',root/'API')
for file in ('OfflineAssetStore.swift','OfflineCacheRoot.swift','TurnReducer.swift','ChatMessage.swift'):
    shutil.copy2(source/file,root/file)
for file in ('recovery_support.swift','offline_history_test.swift'):
    shutil.copy2(file,root/file)
(root/'transport.swift').write_text(Path('recovery_transport_test.swift').read_text().split('@main struct RecoveryTests')[0])
# name -> (test mode, mutated file, original, mutant)
mutations={
 'draft-selection': ('draft-selection','OfflineHistoryStore.swift', 'removed.contains(selected)', '!valid.contains(selected)'),
 'readonly': ('readonly','AgentStore.swift', 'guard historyIsReadOnly else { return false }', 'guard false else { return false }'),
 'readonly-transient': ('readonly-transient','AgentStore.swift', 'case .reachable: connFailures = 0; connState = .reachable', 'case .reachable: connFailures = 0; connState = .reachable; markHistoryReadOnly()'),
 'readonly-cached-auth': ('readonly-transient','AgentStore.swift', 'if historyIsReadOnly, selectedSessionIsAttached { historyIsReadOnly = false }', 'if historyIsReadOnly, selectedSessionIsAttached || true { historyIsReadOnly = false }'),
 'readonly-debounce': ('readonly-transient','AgentStore.swift', 'if connState == .offline || !selectedSessionIsAttached { markHistoryReadOnly() }', 'markHistoryReadOnly()'),
 'mirror-evicted': ('mirror-budget','AgentStore.swift', 'markEvicted: { ids in try await cache.markAssetsEvicted(ids, namespace: namespace) })', 'markEvicted: { _ in })'),
 'mirror-partial': ('mirror-budget','AgentStore.swift', 'complete: result.state == .complete, token: token)', 'complete: true, token: token)'),
 'asset-evicted-unknown': ('asset-mark-race','OfflineHistoryStore.swift', 'index.assetPartial = (index.assetPartial ?? []).union(ids)', 'index.assetPartial = (index.assetPartial ?? []).union(ids.filter { index.assetVersions?[$0] != nil })'),
 'asset-mark-token': ('asset-mark-race','OfflineHistoryStore.swift', 'if complete, !evictedDuringPass {', 'if complete {'),
 'search-version': ('search-version','AgentStore.swift', 'matchingVersion: OfflineHistoryStore.Version(item))', 'matchingVersion: nil)'),
 'asset-resync': ('all','AgentStore.swift', 'if history || assets { work.append((item, history, assets)) }', 'work.append((item, history, true))'),
 'identity': ('identity','OfflineHistoryStore.swift', '+ "\\n" + digest(apiKey)', '+ "\\n"'),
 'cleanup': ('cleanup','OfflineHistoryStore.swift', 'if requireListed {', 'if false {'),
 'tail-delete-success': ('tail-delete-success','OfflineHistoryStore.swift', 'if let expectedEpoch, expectedEpoch != epoch(sessionId: history.sessionId, namespace: namespace) { return false }', 'if false { return false }'),
 'index-create': ('index-create','AgentStore.swift', 'sessionIndexFence.matches(request)', 'true'),
 'index-actor': ('index-actor','OfflineHistoryStore.swift', 'isCurrent()', 'true'),
 'cached-startup-offline': ('cached-startup-offline','AgentStore.swift', 'if sessions.isEmpty || !Self.isOfflineTransportFailure(error) {', 'if true {'),
 'cached-startup-auth401': ('cached-startup-auth401','OfflineHistoryStore.swift', 'guard let transport = error as? URLError else { return false }', 'guard let transport = error as? URLError else { return true }')
}
for name,(mode,file,old,new) in mutations.items():
    folder=root/name;folder.mkdir()
    (folder/'mode').write_text(mode)
    contents=folder/'Mutation.app/Contents'
    (contents/'MacOS').mkdir(parents=True)
    (contents/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'com.agent.codex.native-tests.mutation.'+uuid.uuid4().hex,'CFBundleExecutable':'test','CFBundlePackageType':'APPL'}))
    for owned in ('AgentStore.swift','OfflineHistoryStore.swift'):
        data=(source/owned).read_text()
        if file==owned:
            assert data.count(old)==(3 if name=="index-actor" else 1),(name,old,data.count(old))
            data=data.replace(old,new)
        (folder/owned).write_text(data)
PY
if test "$#" -eq 0; then set -- draft-selection readonly readonly-transient readonly-cached-auth readonly-debounce mirror-evicted mirror-partial asset-evicted-unknown asset-mark-token search-version asset-resync identity cleanup tail-delete-success index-create index-actor cached-startup-offline cached-startup-auth401; fi
for mutation in "$@"; do
    xcrun swiftc -parse-as-library -module-cache-path "$scratch/modules" \
        "$scratch"/API/*.swift "$scratch/OfflineAssetStore.swift" "$scratch/OfflineCacheRoot.swift" \
        "$scratch/$mutation/OfflineHistoryStore.swift" \
        "$scratch/TurnReducer.swift" "$scratch/ChatMessage.swift" \
        "$scratch/$mutation/AgentStore.swift" "$scratch/recovery_support.swift" \
        "$scratch/transport.swift" "$scratch/offline_history_test.swift" -o "$scratch/$mutation/Mutation.app/Contents/MacOS/test"
    set +e
    "$scratch/$mutation/Mutation.app/Contents/MacOS/test" "$(cat "$scratch/$mutation/mode")" "$scratch/$mutation/cache" > "$scratch/$mutation/result" 2>&1
    result=$?
    set -e
    cat "$scratch/$mutation/result"
    test "$result" -eq 1
    case "$mutation" in
        draft-selection) command grep -Fxq 'FAIL: unlisted draft selection survives index refresh' "$scratch/$mutation/result" ;;
        readonly) command grep -Fxq 'FAIL: cached history cannot authorize mutations' "$scratch/$mutation/result" ;;
        readonly-transient) command grep -Fxq 'FAIL: a timed out index probe keeps the attached session writable' "$scratch/$mutation/result" ;;
        readonly-cached-auth) command grep -Fxq 'FAIL: authentication alone cannot grant authority to a cached projection' "$scratch/$mutation/result" ;;
        readonly-debounce) command grep -Fxq 'FAIL: a single failed health probe keeps the attached turn writable' "$scratch/$mutation/result" ;;
        mirror-evicted) command grep -Fxq 'FAIL: making room for B costs A its mirrored copy and its complete status' "$scratch/$mutation/result" ;;
        mirror-partial) command grep -Fxq 'FAIL: a file over the per-file cap is not counted as mirrored' "$scratch/$mutation/result" ;;
        asset-evicted-unknown) command grep -Fxq 'FAIL: a session without a record can still lose its complete status' "$scratch/$mutation/result" ;;
        asset-mark-token) command grep -Fxq 'FAIL: a pass cannot report complete after its bytes were evicted' "$scratch/$mutation/result" ;;
        search-version) command grep -Fxq 'FAIL: search cannot quote a superseded cached body' "$scratch/$mutation/result" ;;
        asset-resync) command grep -Fxq 'FAIL: an unchanged index issues no second manifest request' "$scratch/$mutation/result" ;;
        identity) command grep -Fxq 'FAIL: URL and key both fence namespaces' "$scratch/$mutation/result" ;;
        cleanup) command grep -Fxq 'FAIL: late background response cannot resurrect removed history' "$scratch/$mutation/result" ;;
        tail-delete-success) command grep -Fxq 'FAIL: pre-deletion request cannot restore invalidated tail' "$scratch/$mutation/result" ;;
        index-create) command grep -Fxq 'FAIL: older index cannot discard newly synchronized B' "$scratch/$mutation/result" ;;
        index-actor) command grep -Fxq 'FAIL: obsolete actor index write cannot delete newer body' "$scratch/$mutation/result" ;;
        cached-startup-offline) command grep -Fxq 'FAIL: cached network failure stays quiet' "$scratch/$mutation/result" ;;
        cached-startup-auth401) command grep -Fxq 'FAIL: authentication and unknown errors remain visible' "$scratch/$mutation/result" ;;
        *) echo "FAIL: no expected message registered for mutation $mutation" >&2; exit 1 ;;
    esac
    echo "PASS intended mutation failure: $mutation"
done
