#!/usr/bin/env bash
# macOS 发布管线：bundle-macos.sh（构建+装机）→ app 公证+钉票 → DMG → DMG 公证+钉票
#   → Sparkle appcast（自动含 delta 补丁）→ 推送公共发布仓的滚动 release（tag=updates）。
#
# 分发模型（用户 2026-08-30 拍板）：
#   - 新设备装机 = 从发布仓下最新 MacAgent-<ver>-<build>.dmg，双击拖装（app 与 DMG
#     双层公证钉票，离线首启也过 Gatekeeper）；
#   - 老设备更新 = app 内 Sparkle 自动提示，只下 delta（内嵌运行时不变则远小于全量）。
# ⚠️ 公开面：发布仓是 PUBLIC，DMG 内是完整 app（含内嵌 backend 源码）——等于公开这些
#   内容（用户已知情接受）。密钥红线不变：.p8/.env*/EdDSA 私钥一个字节不进包不进仓；
#   本脚本任何输出不得回显 .env 值（失败提示只给 submission id，凭据自己去 .env.local 看）。
#
# 鉴权三件套：
#   - 公证：复用 iOS 的 ASC API key（.env.local: ASC_KEY_ID/ASC_ISSUER_ID；
#     私钥 ~/.appstoreconnect/private_keys/AuthKey_<ID>.p8，永不入仓库）；
#   - appcast 签名：EdDSA 私钥在登录钥匙串（build/sparkle-tools/bin/generate_keys 管理；
#     换构建机先 generate_keys -x 导出迁走，公钥在 project.yml SUPublicEDKey）；
#   - 上传：gh CLI 的已登录账号（写权限对 RELEASE_REPO）。
# 前置：build/runtime 就绪（同 bundle-macos.sh）；Sparkle CLI 在 build/sparkle-tools/bin
#   （缺了按下面报错里的指引取，版本与 project.yml 钉的 Sparkle 一致）。
# 本地存档：releases/（gitignored）。generate_appcast 默认只保 3 个版本做 delta 基线，
#   更老的自动挪进 releases/old_updates/——那是工具自己的归档动作，不是病；old_updates
#   可手动清理换磁盘。GitHub 滚动 release 每版累积约 1G，攒多了手动删旧 DMG 资产即可
#   （appcast 里已不引用的版本删了无害）。
set -euo pipefail
cd "$(dirname "$0")"

RELEASE_REPO="enkin-japan/Agent-L0-macOS-app"
FEED_TAG="updates"
DL_PREFIX="https://github.com/${RELEASE_REPO}/releases/download/${FEED_TAG}/"
SPARKLE_BIN="build/sparkle-tools/bin"
ARCHIVE_DIR="releases"

for envfile in ../.env ../.env.local; do
  [ -f "$envfile" ] && { set -a; . "$envfile"; set +a; }
done
: "${ASC_KEY_ID:?缺 ASC_KEY_ID——仓库根 .env.local（模板见 .env.example）}"
: "${ASC_ISSUER_ID:?缺 ASC_ISSUER_ID——仓库根 .env.local}"
ASC_KEY_PATH="$HOME/.appstoreconnect/private_keys/AuthKey_${ASC_KEY_ID}.p8"
[ -f "$ASC_KEY_PATH" ] || { echo "❌ 找不到公证私钥（~/.appstoreconnect/private_keys/ 下按 ASC_KEY_ID 命名的 .p8）" >&2; exit 1; }
[ -x "$SPARKLE_BIN/generate_appcast" ] || { echo "❌ 缺 Sparkle CLI 工具：去 sparkle-project/Sparkle 的 GitHub Releases 下载与 project.yml 钉版一致的 Sparkle-<ver>.tar.xz，解压到 build/sparkle-tools/" >&2; exit 1; }
# gh：查的是登录态不是命令在不在——token 过期要在公证花完十分钟之前发现（首审 S8）
gh auth status >/dev/null 2>&1 || { echo "❌ gh 未登录或 token 失效（gh auth login）" >&2; exit 1; }
# EdDSA 公私钥配对自检：Info.plist 里烤错公钥 = 所有已发设备永远验签失败且无法自救（首审 nit）
PUB_KEYCHAIN="$("$SPARKLE_BIN/generate_keys" -p 2>/dev/null | tail -1 | tr -d '[:space:]')"
PUB_PLIST="$(awk '/^[[:space:]]*SUPublicEDKey:/{print $2}' project.yml | tr -d '[:space:]')"
if [ -n "$PUB_KEYCHAIN" ] && [ "$PUB_KEYCHAIN" != "$PUB_PLIST" ]; then
  echo "❌ project.yml 的 SUPublicEDKey 与钥匙串私钥不配对——发出去的包将拒收一切后续更新签名" >&2
  exit 1
fi

# 公证提交：凭据一律走参数不落日志；失败只报 submission id（凭据同 release-ios.sh，
# 自己去 .env.local 看——首审 C3：失败分支曾把 key-id/issuer 回显进持久日志）。
notarize() {  # $1 = 待公证文件
  local target="$1" json=build/notary-result.json status subid
  xcrun notarytool submit "$target" \
    --key "$ASC_KEY_PATH" --key-id "$ASC_KEY_ID" --issuer "$ASC_ISSUER_ID" \
    --wait --output-format json > "$json" || true   # 非零也要走诊断路（首审 S2）
  status=$(plutil -extract status raw -o - "$json" 2>/dev/null || echo unknown)
  subid=$(plutil -extract id raw -o - "$json" 2>/dev/null || echo unknown)
  if [ "$status" != "Accepted" ]; then
    echo "❌ 公证未通过（status=${status}，submission=${subid}）" >&2
    echo "   取明细：xcrun notarytool log ${subid} <凭据参数同 release-ios.sh，见 .env.local>" >&2
    return 1
  fi
  echo "   公证通过（${subid}）"
}

echo "== ① 构建 + 本机装机（bundle-macos.sh）=="
./bundle-macos.sh

APP=build/Build/Products/Release/MacAgent.app
VER=$(plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist")
BUILD=$(plutil -extract CFBundleVersion raw "$APP/Contents/Info.plist")
DMG_NAME="MacAgent-${VER}-${BUILD}.dmg"
DMG="$ARCHIVE_DIR/$DMG_NAME"

# 防覆盖已发布版本（首审 S4）：同名资产已在滚动 release 上 = 这个构建号发过了，
# 老设备的 delta 基线会被悄悄换掉——必须提版本号，不许原地重发。
if gh release view "$FEED_TAG" -R "$RELEASE_REPO" --json assets \
     --jq '.assets[].name' 2>/dev/null | grep -qxF "$DMG_NAME"; then
  echo "❌ ${DMG_NAME} 已经发布过——提 project.yml 的 CURRENT_PROJECT_VERSION 再来" >&2
  exit 1
fi

# 签名门（首审 S1/S3）：尊重环境 SIGN_ID（与 bundle-macos.sh 同语义），
# 先本地 strict 校验——一秒钟就能拦下的病不要花十分钟去苹果那里拒。
SIGN_ID="${SIGN_ID:-$(security find-identity -v -p codesigning 2>/dev/null \
    | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)}"
[ -n "$SIGN_ID" ] || { echo "❌ 找不到 Developer ID Application 证书" >&2; exit 1; }
SIGN_INFO="$(codesign -dvv "$APP" 2>&1)"   # 落变量再匹配：pipefail 下 grep -q 会 SIGPIPE 误报
case "$SIGN_INFO" in
  *"Authority=Developer ID Application"*) : ;;
  *) echo "❌ app 不是 Developer ID 签名（bundle-macos.sh 回落了 ad-hoc？）——公证必拒，终止" >&2; exit 1 ;;
esac
case "$SIGN_INFO" in
  *"(runtime)"*) : ;;
  *) echo "❌ app 未开 hardened runtime——公证必拒，终止" >&2; exit 1 ;;
esac
VERIFY_OUT="$(codesign --verify --strict "$APP" 2>&1)" || {
  echo "❌ codesign --verify --strict 不过（包内有未签/越界内容？）——公证必拒，终止" >&2
  printf '%s\n' "$VERIFY_OUT" | head -10 >&2; exit 1; }

echo "== ② app 公证 + 钉票（苹果侧通常 2~10 分钟）=="
# 先公证 app 本体并把票钉进 .app，再装 DMG：这样 Sparkle 更新解出的 app 和用户拖进
# /Applications 的 app 都自带票据，新设备离线首启不依赖在线查验（首审 S7）。
APP_ZIP=build/MacAgent-notarize.zip
rm -f "$APP_ZIP"
ditto -c -k --keepParent "$APP" "$APP_ZIP"
notarize "$APP_ZIP"
rm -f "$APP_ZIP"
xcrun stapler staple "$APP" >/dev/null
xcrun stapler validate "$APP" >/dev/null || { echo "❌ app 钉票校验失败" >&2; exit 1; }

echo "== ③ 制 DMG（${DMG_NAME}）=="
STAGE=build/dmg-stage
rm -rf "$STAGE"; mkdir -p "$STAGE" "$ARCHIVE_DIR"
ditto "$APP" "$STAGE/MacAgent.app"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "MacAgent ${VER}" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"
codesign --force --timestamp -s "$SIGN_ID" "$DMG"

echo "== ④ DMG 公证 + 钉票 =="
notarize "$DMG"
xcrun stapler staple "$DMG" >/dev/null
xcrun stapler validate "$DMG" >/dev/null || { echo "❌ DMG 钉票校验失败" >&2; exit 1; }

echo "== ⑤ 生成 appcast（EdDSA 签名 + delta 补丁）=="
# generate_appcast 扫 releases/ 里的历史 DMG：为新版本对既有版本自动产 .delta，并用
# 钥匙串私钥给每个条目签名（客户端用 SUPublicEDKey 验签）。超过保留数的老版本会被它
# 自动挪进 releases/old_updates/。
"$SPARKLE_BIN/generate_appcast" --download-url-prefix "$DL_PREFIX" "$ARCHIVE_DIR"

echo "== ⑥ 推送公共发布仓（${RELEASE_REPO} · tag=${FEED_TAG}）=="
gh release view "$FEED_TAG" -R "$RELEASE_REPO" >/dev/null 2>&1 || \
  gh release create "$FEED_TAG" -R "$RELEASE_REPO" \
    --title "MacAgent 更新通道" \
    --notes "滚动更新通道：appcast.xml（Sparkle 订阅源）+ 各版本 DMG + delta 补丁。新设备装机请下最新的 MacAgent-<版本>-<构建号>.dmg。"
UPLOADS=("$DMG" "$ARCHIVE_DIR/appcast.xml")
for d in "$ARCHIVE_DIR"/*.delta; do [ -e "$d" ] && UPLOADS+=("$d"); done
gh release upload "$FEED_TAG" -R "$RELEASE_REPO" --clobber "${UPLOADS[@]}"

echo "== done：v${VER}（${BUILD}）已发布 =="
echo "   新装机：${DL_PREFIX}${DMG_NAME}"
echo "   应用内更新：装有本版及以后版本的设备将经 appcast 自动收到提示（delta 优先）"
