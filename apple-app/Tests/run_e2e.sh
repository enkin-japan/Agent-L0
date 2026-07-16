#!/usr/bin/env bash
# 协议层 E2E：用 app 同一份 API 源码直连本机后端（须先 ./devctl.sh start backend）。
# 用法: ./run_e2e.sh [e2e_client_test|e2e_batch2_test]（缺省全跑）
set -euo pipefail
cd "$(dirname "$0")"
API=../Sources/API
TESTS=${1:-"e2e_client_test e2e_batch2_test"}
for t in $TESTS; do
  echo "== 编译 $t =="
  swiftc -parse-as-library -o "/tmp/$t" \
    "$API/JSONValue.swift" "$API/Models.swift" "$API/ProtocolModels.swift" \
    "$API/DeckModels.swift" "$API/SSEClient.swift" \
    "$API/Multipart.swift" "$API/AgentClient.swift" "$t.swift"
  "/tmp/$t"
done
