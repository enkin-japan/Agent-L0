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

DD="${DEVELOPER_DIR:-/Applications/Xcode-beta.app/Contents/Developer}"

echo "== xcodegen + Release 构建 =="
xcodegen generate
DEVELOPER_DIR="$DD" xcodebuild \
  -project MacAgentClient.xcodeproj -scheme MacAgent-macOS -configuration Release \
  -derivedDataPath build build CODE_SIGN_IDENTITY=- | tail -1

APP=build/Build/Products/Release/MacAgent.app
RES="$APP/Contents/Resources"

echo "== 装入执行层 =="
# backend：只带代码与只读内容。数据目录（models/sessions/schedules/notes/日志）是
# DATA_DIR 的事（D.16），dev 形态它们恰好躺在 backend/ 里——必须排掉，否则把
# 开发机的会话史打进包里。__pycache__ 排掉：字节码在新位置首次 import 时重生成。
rsync -a --delete \
  --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='tests' --exclude='benchmarks' --exclude='docs' \
  --exclude='models' --exclude='sessions' --exclude='schedules' --exclude='notes' \
  --exclude='*.jsonl' --exclude='logs' --exclude='api_tools' \
  "$ROOT/backend/" "$RES/backend/"
# ⚠️ backend/api_tools 是 dev 形态的数据目录（UI 添加的声明 + .secrets.env 密钥文件），
#    绝不进包——密钥红线；app 形态由 DATA_DIR（App Support）承接同名目录。
# 剧本内容（app.py: SKILLS_DIR = backend 的父目录/skills → Resources/skills）
rsync -a --delete --exclude='__pycache__' "$ROOT/skills/" "$RES/skills/"
# 手写 API 工具声明（api_tool_factory: DECL_DIR = 仓库根/api_tools → Resources/api_tools，
# 与 skills 同解析逻辑）：只读内容，UI 添加的另落数据根
rsync -a --delete "$ROOT/api_tools/" "$RES/api_tools/"
# RAG 数据面（B2c）：只随 rag_server.py——bundle 形态只支持拆分模式（计算面在算力端），
# compute_core/compute_server 依赖 torch，不进包。数据（chroma_data/storage）落数据根
# （EDGE_RAG_DATA_DIR，SidecarManager 注入），代码目录保持只读。
mkdir -p "$RES/rag"
cp "$ROOT/rag/rag_server.py" "$RES/rag/rag_server.py"
# 数据面的零重依赖伴生模块（检索增强抽层）：rag_server.py import 它，漏拷 = 包内
# 数据面启动即 ModuleNotFoundError（build 54 组装时实测抓到，S.11）
cp "$ROOT/rag/retrieval_extras.py" "$RES/rag/retrieval_extras.py"
# 运行时（已按 hardened runtime 逐 Mach-O 签名，ditto 保留符号链接与权限）
rm -rf "$RES/python"
ditto "$RUNTIME" "$RES/python"
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

echo "== 重签（资源注入破坏了 bundle 印章）=="
# 身份优先 Developer ID：ad-hoc 每次构建都是新身份，Keychain 按签名身份做 ACL——
# 每次重装 app 都读不回上次存的密钥（弹窗或静默空值，实机三轮 401 的嫌疑之一）。
# Developer ID 的 designated requirement 跨构建稳定，密钥存一次终身有效。
# Resources 里的 Mach-O 保持各自已有的签名不动（--deep 会覆盖掉它们，绝不可用）。
SIGN_ID="${SIGN_ID:-$(security find-identity -v -p codesigning 2>/dev/null \
    | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)}"
if [ -n "$SIGN_ID" ]; then
  echo "   签名身份: $SIGN_ID"
  codesign --force --timestamp -s "$SIGN_ID" "$APP"
else
  echo "   ⚠️ 未找到 Developer ID 证书，回落 ad-hoc（每次重装需重存密钥）"
  codesign --force -s - "$APP"
fi

echo "== 安装 =="
osascript -e 'quit app "MacAgent"' 2>/dev/null || true
sleep 1
rm -rf /Applications/MacAgent.app
ditto "$APP" /Applications/MacAgent.app
ver=$(plutil -extract CFBundleShortVersionString raw /Applications/MacAgent.app/Contents/Info.plist)
echo "installed → /Applications/MacAgent.app (v$ver, $(du -sh /Applications/MacAgent.app | cut -f1))"
