#!/bin/bash
# CPU-only production Store/API/SSE with synthetic URLProtocol and isolated disk cache.
set -euo pipefail
cd "$(dirname "$0")"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
python3 - "$scratch/transport.swift" <<'PY'
import pathlib,sys,plistlib,uuid
header=pathlib.Path('recovery_transport_test.swift').read_text().split('@main struct RecoveryTests')[0]
pathlib.Path(sys.argv[1]).write_text(header)
contents=pathlib.Path(sys.argv[1]).parent/'OfflineHistoryTests.app/Contents'
(contents/'MacOS').mkdir(parents=True)
(contents/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'com.agent.codex.native-tests.offline.'+uuid.uuid4().hex,'CFBundleExecutable':'offline-history','CFBundlePackageType':'APPL'}))
PY
binary="$scratch/OfflineHistoryTests.app/Contents/MacOS/offline-history"
xcrun swiftc -parse-as-library -module-cache-path "$scratch/modules" \
    ../Sources/API/*.swift ../Sources/Store/Offline*.swift \
    ../Sources/Store/TurnReducer.swift ../Sources/Store/ChatMessage.swift \
    ../Sources/Store/AgentStore.swift recovery_support.swift \
    "$scratch/transport.swift" offline_history_test.swift -o "$binary"
if test "$#" -gt 0; then
    for mode in "$@"; do "$binary" "$mode" "$scratch/$mode"; done
    exit 0
fi
"$binary" write "$scratch/restart"
"$binary" read "$scratch/restart"
for mode in draft-selection identity cleanup all bounded readonly readonly-transient assets-recovery mirror-budget mirror-serial mirror-resume asset-mark-race bounded-index search-version asset-path select index-failure disk-failure migration tail-delete-success tail-edit-success tail-delete-failed tail-edit-failed index-create index-delete index-actor index-receipt-delete index-receipt-submit cached-startup-offline cached-startup-cancelled cached-startup-auth401 cached-startup-auth403 cached-startup-http500 cached-startup-unknown uncached-startup-offline history-metadata-race history-background-provenance history-prefix-terminal history-remote-tail-delete history-sync-status history-only-auth history-only-http500 history-only-decode tail-edit-paused-success asset-manifest-race asset-fetch-race asset-epoch-save asset-membership-legacy; do
    "$binary" "$mode" "$scratch/$mode"
done
