#!/usr/bin/env bash
# macOS 日用安装：Release 构建 → 替换 /Applications/MacAgent.app（本机免签名，ad-hoc）。
# 每批迭代收尾自动跑；正在运行的实例会被先退出。
set -euo pipefail
cd "$(dirname "$0")"

DD="${DEVELOPER_DIR:-$(xcode-select -p)}"   # 默认跟随 xcode-select（Xcode-beta 已不在；想换版本用 DEVELOPER_DIR 覆盖）

xcodegen generate
DEVELOPER_DIR="$DD" xcodebuild \
  -project MacAgentClient.xcodeproj -scheme MacAgent-macOS -configuration Release \
  -derivedDataPath build build CODE_SIGN_IDENTITY=- | tail -1

APP=build/Build/Products/Release/MacAgent.app
osascript -e 'quit app "MacAgent"' 2>/dev/null || true
sleep 1
rm -rf /Applications/MacAgent.app
ditto "$APP" /Applications/MacAgent.app
echo "installed → /Applications/MacAgent.app ($(plutil -extract CFBundleShortVersionString raw /Applications/MacAgent.app/Contents/Info.plist))"
