#!/usr/bin/env bash
# 协议层 E2E：用 app 同一份 API 源码直连本机后端（须先 ./devctl.sh start backend）。
# 用法: ./run_e2e.sh [e2e_client_test|e2e_batch2_test]（缺省全跑）
set -euo pipefail
cd "$(dirname "$0")"
API=../Sources/API

# 种资产夹具（batch2 第 5 项）：尊重 AGENT_DATA_DIR（bundle 烟测指临时根），
# 缺省 = 仓库 backend/（D.16 的 dev 默认）。空壳会话会被 TTL 回收，每次跑前重种。
DATA_ROOT="${AGENT_DATA_DIR:-$(cd ../../backend && pwd)}"
mkdir -p "$DATA_ROOT/sessions/e2efixture-chart/assets"
cp fixtures/chart-fixture.png "$DATA_ROOT/sessions/e2efixture-chart/assets/"
TESTS=${1:-"e2e_client_test e2e_batch2_test"}
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
export AGENT_E2E_CACHE_ROOT="${AGENT_E2E_CACHE_ROOT:-$scratch/cache}"
for t in $TESTS; do
  echo "== 编译 $t =="
  swiftc -parse-as-library -module-cache-path "$scratch/modules" -o "$scratch/$t" \
    "$API"/*.swift ../Sources/Store/Offline*.swift "$t.swift"
  "$scratch/$t"
done
