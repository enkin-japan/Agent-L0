#!/bin/bash
# 协议夹具回放(Swift 侧):与 web 的 `npm run test` 消费同一批 protocol/fixtures。
set -euo pipefail
cd "$(dirname "$0")"
BIN=$(mktemp -d)/reducer_fixtures
xcrun swiftc -parse-as-library -o "$BIN" \
    ../Sources/API/JSONValue.swift \
    ../Sources/API/ProtocolModels.swift \
    ../Sources/API/Models.swift \
    ../Sources/Store/TurnReducer.swift \
    reducer_fixtures_test.swift
"$BIN" "${1:-../../protocol/fixtures}"
