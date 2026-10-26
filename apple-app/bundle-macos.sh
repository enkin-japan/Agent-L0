#!/usr/bin/env bash
# 完整 bundle 组装（R5-9）：Release 构建 → 把执行层装进 .app 的 Resources → 重签 → 安装。
# 产出的 app 自带 backend + PBS 运行时（含 searx，R5-5 同树）+ lib_edge + config，
# SidecarManager 的 bundle 布局分支（resolveLayout 第一分支）由此成立。
#
# 与 install-macos.sh 的分工：那个是日用快装（纯客户端，sidecar 走仓库路径的 repo 布局）；
# 这个是交付形态验收用（990M 运行时随包，双击即全栈）。
#
# 前置：scripts/build-runtime.sh 已产出 build/runtime（PBS + 依赖 + searx，已签名）。
# 纪律：私钥（.keys/*.p8、.env*）一个字节不进包——APNs 密钥 bundle 形态放数据根
#       （~/Library/Application Support/dev.enkin.macagent/.keys/），app.py 有兜底解析。
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"

RUNTIME="$ROOT/build/runtime"
[ -x "$RUNTIME/bin/python3.13" ] || {
  echo "❌ 缺 $RUNTIME——先跑 scripts/build-runtime.sh" >&2; exit 1; }

DD="${DEVELOPER_DIR:-$(xcode-select -p)}"   # 默认跟随 xcode-select（Xcode-beta 已不在；想换版本用 DEVELOPER_DIR 覆盖）

echo "== xcodegen + Release 构建 =="
xcodegen generate
DEVELOPER_DIR="$DD" xcodebuild \
  -project MacAgentClient.xcodeproj -scheme MacAgent-macOS -configuration Release \
  -derivedDataPath build build CODE_SIGN_IDENTITY=- | tail -1

APP=build/Build/Products/Release/MacAgent.app
RES="$APP/Contents/Resources"

echo "== 装入执行层 =="
# backend：只带代码与只读内容。数据目录（models/sessions/schedules/notes/maintenance/日志）是
# DATA_DIR 的事（D.16），dev 形态它们恰好躺在 backend/ 里——必须排掉，否则把
# 开发机的会话史打进包里。__pycache__ 排掉：字节码在新位置首次 import 时重生成。
# ⚠️ '.venv*' 必须带通配：曾写 '.venv' 漏掉 .venv-mlxsrc（477MB 开发 venv + 越界软链），
#    公证 126 项全数拒收（2026-08-30 实测）。'*.log'/.DS_Store 同理是开发机残留，
#    包是要公开分发的，多带一个字节都是无审查的暴露面。
# --delete-excluded 不能省：光 --delete 会把「此前误打进包、如今已列入排除」的旧文件
# 原样留在目的地（排除项不参与删除决策）——venv 首修后第二跑仍拒收即此坑实测。
# /maintenance/ 仅匹配数据根；framework/maintenance/ 是运行代码，必须保留。
rsync -a --delete --delete-excluded \
  --exclude='.venv*' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='tests' --exclude='benchmarks' --exclude='docs' \
  --exclude='models' --exclude='sessions' --exclude='schedules' --exclude='notes' \
  --exclude='*.jsonl' --exclude='*.log' --exclude='.DS_Store' \
  --exclude='logs' --exclude='api_tools' --exclude='skills' --exclude='/maintenance/' \
  --exclude='/auth/' --exclude='/push/' --exclude='/registrations/' \
  "$ROOT/backend/" "$RES/backend/"
# /auth/ /push/ /registrations/ 同样只匹配数据根（设备票据哈希、APNs token、待确认注册队列），
# framework/push/ 是运行代码，必须保留；下方泄漏断言一并核对这三个目录。
# ⚠️ backend/api_tools 是 dev 形态的数据目录（UI 添加的声明 + .secrets.env 密钥文件），
#    绝不进包——密钥红线；app 形态由 DATA_DIR（App Support）承接同名目录。
#    backend/skills 同理（UI 添加的用户剧本，dev 数据目录）——开发机私有内容不进交付包。
# 剧本内容（app.py: SKILLS_DIR = backend 的父目录/skills → Resources/skills）
rsync -a --delete --exclude='__pycache__' "$ROOT/skills/" "$RES/skills/"
# 手写 API 工具声明（api_tool_factory: DECL_DIR = 仓库根/api_tools → Resources/api_tools，
# 与 skills 同解析逻辑）：只读内容，UI 添加的另落数据根
rsync -a --delete "$ROOT/api_tools/" "$RES/api_tools/"
# RAG 数据面（B2c）：只随 rag_server.py——bundle 形态只支持拆分模式（计算面在算力端），
# compute_core/compute_server 依赖 torch，不进包。数据（chroma_data/storage）落数据根
# （EDGE_RAG_DATA_DIR，SidecarManager 注入），代码目录保持只读。
mkdir -p "$RES/rag"
# 数据面模块**全量自动入包**，只点名排除 compute 两件（torch 依赖）。此前逐文件 cp
# 漏拷过两回（retrieval_extras S.11 / fts_index 2026-08-10），每漏一件 = 包内数据面
# 启动即 ModuleNotFoundError；多带一个纯 py 无害，漏一个致命——方向选宁多勿漏。
for f in "$ROOT"/rag/*.py; do
  case "$(basename "$f")" in compute_core.py|compute_server.py) continue ;; esac
  cp "$f" "$RES/rag/"
done
# 运行时（已按 hardened runtime 逐 Mach-O 签名，ditto 保留符号链接与权限）
rm -rf "$RES/python"
ditto "$RUNTIME" "$RES/python"
# 剪掉调试残渣。两个理由，各对应一个模式：
# ① `*.dSYM`（含 PyObjCTest 内嵌套的）带签名扩展属性，Sparkle generate_appcast
#   拒绝差分——71 版实测「Diffing code signed extended attributes are not
#   supported」，delta 做不出来，更新退全量下载。注意脏基线（≤71 的 DMG）做
#   before-tree 时照旧报这个错：首份 delta 预期在两侧都是净树的版本对（72→73），
#   届时再见此报错才是修法失效；
# ② `PyObjCTest` 是三方包自带的测试目录（16MB 纯死重），本就不该进交付包。
# 放在 compileall/签名之前：删目录不碰其余 Mach-O 的既有签名（71 版实测这三处
# 是全包唯一的 com.apple.cs.* 扩展属性载体）。零命中要说话不许静默（上游哪天
# 自己剥了 dSYM，这行提示就是「可以删掉本段」的信号，而不是无声空转）。
_pruned=$(find "$RES/python" \( -name '*.dSYM' -o -name 'PyObjCTest' \) -type d -prune \
  -print -exec rm -rf {} + | wc -l | tr -d ' ')
if [ "${_pruned}" -gt 0 ]; then
  echo "   已剪除 ${_pruned} 个调试残渣目录（dSYM/PyObjCTest，保 delta 可差分）"
else
  echo "   ⚠️ 未发现 dSYM/PyObjCTest（上游布局变了？核对 delta 是否仍可差分）"
fi
# 编队真相 + 配置（config_loader: CONFIG_PATH = backend 父目录/config.yaml → Resources/）
mkdir -p "$RES/scripts"
cp "$ROOT/scripts/lib_edge.sh" "$RES/scripts/lib_edge.sh"
cp "$ROOT/config.yaml" "$RES/config.yaml"

# 预编译字节码（R5-9 实证）：不做这步，首启 import 会把 __pycache__ 写满包内、
# 当场破坏 codesign 印章（rsync 排除了 backend 的字节码；python/bin 也有缺口）。
# 预编译后字节码随包进印章；运行时再配 PYTHONDONTWRITEBYTECODE=1（SidecarManager
# bundle 分支）兜住漏网的。个别三方包自带坏语法测试文件，编不过就跳过（|| true）。
"$RES/python/bin/python3.13" -m compileall -qq -j0 "$RES/backend" "$RES/python" 2>/dev/null || true

# 私钥零泄漏断言：包里绝不能有 .p8 / .env*（S 级纪律，宁可组装失败）
leaked=$(find "$APP" \( -name '*.p8' -o -name '.env' -o -name '.env.*' \) | head -5)
[ -z "$leaked" ] || { echo "❌ 包内发现私钥/环境文件：" >&2; echo "$leaked" >&2; exit 1; }
for d in auth push registrations api_tools; do
  [ ! -e "$RES/backend/$d" ] || { echo "❌ 包内发现数据目录 backend/$d（票据 / 推送 token / 密钥文件）" >&2; exit 1; }
done

# 执行层完整性断言（2026-08-10 401 事故）：resolveLayout 的 bundle 判据就是这些文件，
# 缺任何一件装出去的 app 会静默退 repo 布局（密钥换源 → 构造性 401）。宁可组装失败。
for f in backend/app.py python/bin/python3.13 scripts/lib_edge.sh config.yaml; do
  [ -e "$RES/$f" ] || { echo "❌ 包内执行层不完整：缺 Resources/$f" >&2; exit 1; }
done

echo "== 重签（资源注入破坏了 bundle 印章）=="
# 身份优先 Developer ID：ad-hoc 每次构建都是新身份，Keychain 按签名身份做 ACL——
# 每次重装 app 都读不回上次存的密钥（弹窗或静默空值，实机三轮 401 的嫌疑之一）。
# Developer ID 的 designated requirement 跨构建稳定，密钥存一次终身有效。
# Resources 里的 Mach-O 保持各自已有的签名不动（--deep 会覆盖掉它们，绝不可用）。
SIGN_ID="${SIGN_ID:-$(security find-identity -v -p codesigning 2>/dev/null \
    | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)}"
if [ -n "$SIGN_ID" ]; then
  echo "   签名身份: $SIGN_ID"
  # Sparkle 框架的嵌套件（XPC 服务/Autoupdate/Updater.app）由 xcodebuild 按 ad-hoc
  # 签出，而公证要求**全部嵌套代码**都是 Developer ID + hardened runtime——由深到浅
  # 定点重签（绝不 --deep，理由同上：Resources 里的运行时签名不能被覆盖）。
  SPARKLE_FW="$APP/Contents/Frameworks/Sparkle.framework"
  if [ -d "$SPARKLE_FW" ]; then
    for nested in \
      "$SPARKLE_FW/Versions/B/XPCServices/Downloader.xpc" \
      "$SPARKLE_FW/Versions/B/XPCServices/Installer.xpc" \
      "$SPARKLE_FW/Versions/B/Autoupdate" \
      "$SPARKLE_FW/Versions/B/Updater.app"; do
      # 硬断言而非跳过：Sparkle 换版挪了路径时要当场红，不许静默漏签
      # 让 ad-hoc 嵌套件走到苹果那边才被拒（首审 S5）。
      [ -e "$nested" ] || { echo "❌ Sparkle 嵌套件不在预期路径：$nested（升级了 Sparkle？对照新版布局更新此清单）" >&2; exit 1; }
      # 不继承 entitlements：Autoupdate 自带 org.sparkle-project 的
      # application-identifier，换队签名后语义已错，Sparkle 官方重签指引也不保留。
      codesign --force --timestamp --options runtime -s "$SIGN_ID" "$nested"
    done
    codesign --force --timestamp --options runtime -s "$SIGN_ID" "$SPARKLE_FW"
  fi
  # iCloud 钥匙串同步（Sources/App/PhoneLink.swift）：共享钥匙串组是受限 entitlement，
  # 须由嵌入的 Developer ID 描述文件授权——没有描述文件却带它签名 = app 起不来（AMFI 拒）。
  # 所以它不进 project.yml（开发构建照常能跑），只在这里按描述文件追加。描述文件由
  # asc-macos-profile.py 建并装到 ~/Library/MobileDevice/Provisioning Profiles/。
  # 钥匙串组本 app 自己的排第一（未指定组的新条目默认落这里），共享组第二。
  # 找不到 → 不带同步权限签（iPhone 只剩二维码配对）；找到但 30 天内过期、证书不是本次
  # 签名身份、或 App ID 对不上 → 当场红：带着它发出去的 app 到期/不匹配即无法启动。
  ENTITLEMENTS=Generated/MacAgent-macOS.entitlements
  BUNDLE_ID=$(plutil -extract CFBundleIdentifier raw "$APP/Contents/Info.plist")
  # SIGN_ID 可由调用方覆盖成 40 位 SHA-1（多身份同名时的消歧惯例）：那就直接用它比对
  if [[ "$SIGN_ID" =~ ^[0-9A-Fa-f]{40}$ ]]; then
    SIGN_HASHES=$(echo "$SIGN_ID" | tr '[:lower:]' '[:upper:]')
  else
    SIGN_HASHES=$(security find-identity -v -p codesigning 2>/dev/null \
        | awk -v id="$SIGN_ID" 'index($0, "\"" id "\"") {print $2}' | sort -u | tr '\n' ' ')
  fi
  PROFILE=$(/usr/bin/python3 - "$BUNDLE_ID" "$SIGN_HASHES" <<'PY'
import datetime, glob, hashlib, os, plistlib, subprocess, sys
bundle, hashes = sys.argv[1], set(sys.argv[2].split())
found = []
for f in glob.glob(os.path.expanduser("~/Library/MobileDevice/Provisioning Profiles/*.provisionprofile")):
    raw = subprocess.run(["security", "cms", "-D", "-i", f], capture_output=True).stdout
    try:
        p = plistlib.loads(raw)
    except Exception:
        continue
    if p.get("Name") == "MacAgent macOS DeveloperID":
        found.append((p["ExpirationDate"], f, p))
if not found:
    sys.exit(0)
expires, path, p = max(found, key=lambda x: x[0])
team = p["TeamIdentifier"][0]
# 有效期取描述文件与其证书的较早者（ASC 报的就是证书到期日，描述文件里写的更晚）
for cert in p["DeveloperCertificates"]:
    end = subprocess.run(["openssl", "x509", "-inform", "DER", "-noout", "-enddate"],
                         input=cert, capture_output=True).stdout.decode().strip()
    if end.startswith("notAfter="):
        expires = min(expires, datetime.datetime.strptime(end[9:], "%b %d %H:%M:%S %Y %Z"))
problems = []
if expires - datetime.datetime.utcnow() < datetime.timedelta(days=30):  # plist 日期是 UTC
    problems.append(f"{expires:%Y-%m-%d} 到期（不足 30 天）——重跑 asc-macos-profile.py 续建")
if not hashes & {hashlib.sha1(c).hexdigest().upper() for c in p["DeveloperCertificates"]}:
    problems.append("描述文件里的证书不是本次签名身份")
if p["Entitlements"].get("com.apple.application-identifier") != f"{team}.{bundle}":
    problems.append(f"App ID 不是 {team}.{bundle}")
if f"{team}.*" not in p["Entitlements"].get("keychain-access-groups", []):
    problems.append("未授权 Team 钥匙串组")
if problems:
    sys.exit("❌ 描述文件 " + path + "：" + "；".join(problems))
print(f"{path}\t{team}\t{expires:%Y-%m-%d}")
PY
  )
  if [ -n "$PROFILE" ]; then
    IFS=$'\t' read -r PROFILE_PATH TEAM_ID PROFILE_EXPIRES <<<"$PROFILE"
    cp "$PROFILE_PATH" "$APP/Contents/embedded.provisionprofile"
    ENTITLEMENTS=build/MacAgent-macOS-signing.entitlements
    cp Generated/MacAgent-macOS.entitlements "$ENTITLEMENTS"
    /usr/libexec/PlistBuddy \
      -c "Add :com.apple.application-identifier string $TEAM_ID.$BUNDLE_ID" \
      -c "Add :com.apple.developer.team-identifier string $TEAM_ID" \
      -c "Add :keychain-access-groups array" \
      -c "Add :keychain-access-groups:0 string $TEAM_ID.$BUNDLE_ID" \
      -c "Add :keychain-access-groups:1 string $TEAM_ID.dev.enkin.macagent.shared" \
      "$ENTITLEMENTS"
    echo "   iCloud 钥匙串同步：已嵌入描述文件（$PROFILE_EXPIRES 到期）"
  else
    rm -f "$APP/Contents/embedded.provisionprofile"
    echo "   ⚠️ 未找到描述文件「MacAgent macOS DeveloperID」：本包不带 iCloud 钥匙串同步" \
         "（iPhone 只能扫码配对；建档跑 asc-macos-profile.py）"
  fi
  # 主 app：hardened runtime 是公证硬性要求；entitlements **显式指定**（xcodegen 产出的
  # 麦克风/定位 + 上面按描述文件追加的钥匙串组），绝不用 --preserve-metadata=entitlements
  # 继承——ad-hoc 构建签名会注入 get-task-allow（调试键），继承下来公证必拒（首跑实测踩中）。
  codesign --force --timestamp --options runtime \
    --entitlements "$ENTITLEMENTS" -s "$SIGN_ID" "$APP"
else
  echo "   ⚠️ 未找到 Developer ID 证书，回落 ad-hoc（每次重装需重存密钥）"
  codesign --force -s - "$APP"
fi

# SKIP_INSTALL=1（release-macos.sh 专用）：发布管线里本段的产物是**钉票前**副本，
# 装了也会被⑦的钉票版覆盖，还会在公证失败的分支里把未公证构建留在 /Applications
# ——发布路一律跳过，安装只由 release 的⑦做一次（复审 S4；独立跑本脚本行为不变）。
if [ "${SKIP_INSTALL:-}" = "1" ]; then
  echo "== 安装（SKIP_INSTALL=1，跳过——由 release 管线装钉票版）=="
else
echo "== 安装 =="
osascript -e 'quit app "MacAgent"' 2>/dev/null || true
sleep 1
rm -rf /Applications/MacAgent.app
ditto "$APP" /Applications/MacAgent.app
ver=$(plutil -extract CFBundleShortVersionString raw /Applications/MacAgent.app/Contents/Info.plist)
echo "installed → /Applications/MacAgent.app (v$ver, $(du -sh /Applications/MacAgent.app | cut -f1))"
fi
