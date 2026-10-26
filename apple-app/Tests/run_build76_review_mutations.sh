#!/bin/bash
# Guard mutations for the build 76 review fixes. Isolated: every mutant is a copy in a
# scratch directory; production files are never edited.
#   expose-key        A: expose() without the backend-key check
#   probe-ignored     B: expose() ignores the unauthenticated-probe result
#   prune-orphans     C: pruneOrphans deletes nothing
#   keepset-empty-key C: keepSet keeps going when a credential reads back empty
#   index-unreadable  C: an unreadable index reads as empty for writes
#   index-banner      A(store): an unreadable index is treated as an ordinary refresh error
#   stop-guard        D: sync writes back while a stop is requested
#   approval-refuse   I: the Live Activity falls back to the legacy connection instead of refusing
#   keepset-empty-set C: keepSet returns an empty set (would delete every partition)
#   mark-refuse       A: admitAutomatic evicts although marking the losers failed
#   readable-skip     A: the history pass runs against an unreadable index
#   handover-order    H: the new ticket is saved before the old one is forgotten
#   probe-nil         B: an unanswered auth probe still allows exposing
# Usage: bash Tests/run_build76_review_mutations.sh [mutation ...]
set -euo pipefail
cd "$(dirname "$0")"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
python3 - "$scratch" <<'PY'
from pathlib import Path
import sys,plistlib,uuid,shutil
root=Path(sys.argv[1]);sources=Path('../Sources')
shutil.copytree(sources/'API',root/'API')
for file in ('Store/OfflineAssetStore.swift','Store/OfflineCacheRoot.swift','Store/OfflineHistoryStore.swift',
             'Store/TurnReducer.swift','Store/ChatMessage.swift','Store/AgentStore.swift','App/PhoneLink.swift',
             'LiveActivity/ApprovalRouting.swift'):
    shutil.copy2(sources/file,root/Path(file).name)
for file in ('phone_link_test.swift','offline_assets_test.swift','offline_history_test.swift','recovery_support.swift'):
    shutil.copy2(file,root/file)
(root/'transport.swift').write_text(Path('recovery_transport_test.swift').read_text().split('@main struct RecoveryTests')[0])
# name -> (mutated file, [(original, mutant)])
mutations={
 'expose-key': ('PhoneLink.swift', [
    ('        if let refusal = exposeRefusal {\n            actionError = refusal\n            return\n        }\n', '')]),
 'probe-ignored': ('PhoneLink.swift', [('guard status == 401 else {', 'guard status == 401 || true else {')]),
 'prune-orphans': ('OfflineCacheRoot.swift', [
    ('if (try? manager.removeItem(at: child)) != nil { removed.append(child) }', '_ = child')]),
 'keepset-empty-key': ('OfflineHistoryStore.swift', [
    ('guard let key = keys[mac.id], !key.isEmpty else { return nil }', 'let key = keys[mac.id] ?? ""')]),
 'index-unreadable': ('OfflineHistoryStore.swift', [
    ('catch CocoaError.fileReadNoSuchFile, CocoaError.fileNoSuchFile { return Index() }\n        catch { throw IndexUnreadable(underlying: error) }',
     'catch { return Index() }')]),
 'index-banner': ('AgentStore.swift', [
    ('} catch is OfflineHistoryStore.IndexUnreadable {}', '} catch let error as OfflineHistoryStore.IndexUnreadable { throw error }')]),
 'approval-refuse': ('ApprovalRouting.swift', [('else { return .refuse }', 'else { return .legacy }')]),
 'keepset-empty-set': ('OfflineHistoryStore.swift', [('return namespaces.isEmpty ? nil : namespaces', 'return namespaces')]),
 'mark-refuse': ('OfflineAssetStore.swift', [
    ('do { try await markEvicted(losing) } catch { return Admission(accepted: false) }', 'try? await markEvicted(losing)')]),
 'readable-skip': ('AgentStore.swift', [
    ('                guard await cache.readableIndex(namespace: client.cacheNamespace) != nil else { continue }\n', '')]),
 'handover-order': ('PhoneLink.swift', [
    ('        save()\n    }\n}\n\n/// 握手轮询', '    }\n}\n\n/// 握手轮询'),
    ('        if let previous, !previous.isEmpty, let base {', '        save()\n        if let previous, !previous.isEmpty, let base {')]),
 'probe-nil': ('PhoneLink.swift', [('guard status == 401 else {', 'guard status == 401 || status == nil else {')]),
 'stop-guard': ('PhoneLink.swift', [
    ('guard let settings, settings.phoneSyncEnabled, !stopRequested else { return }',
     'guard let settings, settings.phoneSyncEnabled else { return }'),
    ('guard !stopRequested, settings.phoneSyncEnabled, case .served(url) = exposure else { return }',
     'guard settings.phoneSyncEnabled, case .served(url) = exposure else { return }')]),
}
for name,(file,edits) in mutations.items():
    folder=root/name;folder.mkdir()
    contents=folder/'Mutation.app/Contents'
    (contents/'MacOS').mkdir(parents=True)
    (contents/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'com.agent.codex.native-tests.mutation.'+uuid.uuid4().hex,'CFBundleExecutable':'test','CFBundlePackageType':'APPL'}))
    for owned in ('PhoneLink.swift','OfflineCacheRoot.swift','OfflineHistoryStore.swift','AgentStore.swift',
                  'ApprovalRouting.swift','OfflineAssetStore.swift'):
        data=(root/owned).read_text()
        if owned==file:
            for old,new in edits:
                assert data.count(old)==1,(name,old,data.count(old))
                data=data.replace(old,new)
        (folder/owned).write_text(data)
PY
if test "$#" -eq 0; then set -- expose-key probe-ignored probe-nil prune-orphans keepset-empty-key keepset-empty-set \
    index-unreadable index-banner readable-skip mark-refuse stop-guard approval-refuse handover-order; fi
for mutation in "$@"; do
    m="$scratch/$mutation"
    binary="$m/Mutation.app/Contents/MacOS/test"
    case "$mutation" in
        expose-key|probe-ignored|probe-nil|stop-guard|approval-refuse|handover-order) kind=phone ;;
        prune-orphans|mark-refuse) kind=assets ;;
        keepset-empty-key|keepset-empty-set|index-unreadable|index-banner|readable-skip) kind=history ;;
        *) echo "FAIL: unknown mutation $mutation" >&2; exit 1 ;;
    esac
    case "$kind" in
        phone)
            xcrun swiftc -parse-as-library -module-cache-path "$scratch/modules" \
                "$m/PhoneLink.swift" "$m/ApprovalRouting.swift" "$scratch/phone_link_test.swift" -o "$binary"
            run=("$binary") ;;
        assets)
            xcrun swiftc -parse-as-library -module-cache-path "$scratch/modules" \
                "$m/OfflineAssetStore.swift" "$m/OfflineCacheRoot.swift" \
                "$scratch/offline_assets_test.swift" -o "$binary"
            run=("$binary") ;;
        history)
            xcrun swiftc -parse-as-library -module-cache-path "$scratch/modules" \
                "$scratch"/API/*.swift "$m/OfflineAssetStore.swift" "$m/OfflineCacheRoot.swift" \
                "$m/OfflineHistoryStore.swift" "$scratch/TurnReducer.swift" "$scratch/ChatMessage.swift" \
                "$m/AgentStore.swift" "$scratch/recovery_support.swift" \
                "$scratch/transport.swift" "$scratch/offline_history_test.swift" -o "$binary"
            case "$mutation" in
                keepset-empty-key|keepset-empty-set) mode=prune-keep-set ;;
                readable-skip) mode=index-unreadable-store ;;
                index-unreadable) mode=index-unreadable ;;
                index-banner) mode=index-unreadable-store ;;
            esac
            run=("$binary" "$mode" "$m/cache") ;;
    esac
    case "$mutation" in
        expose-key) expected='FAIL: expose refuses without a backend API key' ;;
        probe-ignored) expected='FAIL: a backend that lets unauthenticated requests through is not exposed' ;;
        prune-orphans) expected='FAIL: an orphaned partition is deleted from both stores' ;;
        keepset-empty-key) expected='FAIL: a Mac whose credential reads back empty skips the whole round' ;;
        index-unreadable) expected='FAIL: an unreadable index is never replaced by an empty one' ;;
        index-banner) expected='FAIL: an unreadable index raises no session-list banner' ;;
        stop-guard) expected='FAIL: no sync writes the entry back while stopping' ;;
        probe-nil) expected='FAIL: an unanswered probe does not expose' ;;
        keepset-empty-set) expected='FAIL: no Mac and no key never prunes everything' ;;
        mark-refuse) expected='FAIL: a failed eviction mark keeps every byte and refuses the file' ;;
        readable-skip) expected='FAIL: no history storm while the index is unreadable' ;;
        approval-refuse) expected='FAIL: intent path agrees with approvalTarget: unmatched src with an identified current Mac is refused' ;;
        handover-order) expected='FAIL: forgetSelf with the old ticket happens before the new ticket is saved' ;;
    esac
    set +e
    "${run[@]}" > "$m/result" 2>&1
    result=$?
    set -e
    cat "$m/result"
    test "$result" -eq 1
    command grep -Fxq "$expected" "$m/result"
    echo "PASS intended mutation failure: $mutation"
done
