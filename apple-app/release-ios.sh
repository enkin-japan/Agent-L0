#!/usr/bin/env bash
# iOS TestFlight 发布管线：xcodegen → Release 归档 → 手动签名直传 App Store Connect。
# 鉴权走 ASC API key：标识符经仓库根 .env.local（ASC_KEY_ID / ASC_ISSUER_ID），
# 私钥 .p8 在 ~/.appstoreconnect/private_keys/AuthKey_${ASC_KEY_ID}.p8（永不入仓库）。全程无人值守。
# 签名资产（API key 无云签名权限，故自管）：
#   - 分发证书+私钥在专用钥匙串 macagent-build.keychain（密码 ~/.appstoreconnect/macagent-build-keychain-pass）
#   - 描述文件 "MacAgent iOS AppStore" 已装 ~/Library/{MobileDevice,Developer/Xcode/UserData}/Provisioning Profiles/
#   - 证书 2027 年到期；到期用 ASC API 重建（POST /certificates + /profiles，见会话记录 asc_make_cert.py）
# 上传后苹果处理 5~15 分钟，随后 TestFlight 内部测试员（iPhone 上的 TestFlight app）收到更新。
# ⚠️ entitlements / bundle id 结构变更后（如推送批次：aps-environment + widgets 扩展）须先跑
#    `../backend/.venv/bin/python ./asc-update-signing.py` 更新 capability 并重建两份描述文件，
#    否则导出报 "profile doesn't include the aps-environment entitlement"。
set -euo pipefail
cd "$(dirname "$0")"

# 凭据标识符：与 devctl.sh 同一份 .env.local、同序（.env 先、.env.local 后覆盖）
for envfile in ../.env ../.env.local; do
  [ -f "$envfile" ] && { set -a; . "$envfile"; set +a; }
done
: "${ASC_KEY_ID:?缺 ASC_KEY_ID——请在仓库根 .env.local 填写（模板见 .env.example）}"
: "${ASC_ISSUER_ID:?缺 ASC_ISSUER_ID——请在仓库根 .env.local 填写（模板见 .env.example）}"
ASC_KEY_PATH="$HOME/.appstoreconnect/private_keys/AuthKey_${ASC_KEY_ID}.p8"
[ -f "$ASC_KEY_PATH" ] || { echo "❌ 找不到私钥 $ASC_KEY_PATH" >&2; exit 1; }

DD="${DEVELOPER_DIR:-$(xcode-select -p)}"   # 默认跟随 xcode-select（Xcode-beta 已不在；想换版本用 DEVELOPER_DIR 覆盖）
# 构建钥匙串会超时自锁，跑之前解锁
security unlock-keychain -p "$(cat ~/.appstoreconnect/macagent-build-keychain-pass)" macagent-build.keychain
AUTH=(
  -authenticationKeyPath "$ASC_KEY_PATH"
  -authenticationKeyID "$ASC_KEY_ID"
  -authenticationKeyIssuerID "$ASC_ISSUER_ID"
)

echo "== xcodegen =="
xcodegen generate

echo "== archive (Release) =="
DEVELOPER_DIR="$DD" xcodebuild \
  -project MacAgentClient.xcodeproj -scheme MacAgent-iOS -configuration Release \
  -destination 'generic/platform=iOS' \
  -archivePath build/MacAgent-iOS.xcarchive \
  archive -allowProvisioningUpdates "${AUTH[@]}"

echo "== upload to App Store Connect =="
DEVELOPER_DIR="$DD" xcodebuild \
  -exportArchive -archivePath build/MacAgent-iOS.xcarchive \
  -exportOptionsPlist ExportOptions.plist -exportPath build/export \
  -allowProvisioningUpdates "${AUTH[@]}"

echo "== done：等待 App Store Connect 处理（5~15 分钟）后 TestFlight 可见 =="
