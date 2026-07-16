#!/usr/bin/env bash
# 构建 app 内嵌的 Python 运行时（R5-3/D.15）：python-build-standalone + edge 依赖。
#
# 产出一棵**可搬**的自包含树，供 .app 的 Contents/Resources/python 使用。
# 「可搬」是这条路线的全部前提，也是它胜过另两个候选的原因（见 D.15）：
#   - 开发机 `backend/.venv` 的 bin/python 是个指向 /Library/Frameworks/Python.framework
#     的**绝对路径符号链接**，venv 里根本没有解释器本体 → 拷进 bundle 只是拷进一个断链；
#     且该 framework 的 install-name 是绝对路径（otool 可验），本身就搬不动。
#   - PyInstaller 与本仓库的「文件系统即真相源」相冲（工具/skills/notes 全靠扫目录发现），
#     冻结后 registry 扫不到 .py → **零工具且不报错**。
#
# 用法：
#   scripts/build-runtime.sh [输出目录]                  # 默认 build/runtime
#   scripts/build-runtime.sh build/runtime --sign "$ID"  # 顺带逐个签（hardened runtime）
#
# 实测（2026-07-16，arm64）：解压 65M → 装完 edge 依赖 **949M**；
# 搬到任意路径后 sys.prefix 自动跟随、212 金测试全绿。
set -euo pipefail
cd "$(dirname "$0")/.."

# 版本钉死：运行时是分发物的一部分，跟着 latest 漂会让「同一个 tag 构建出不同的 app」。
PBS_TAG="20260623"
PBS_VER="3.13.14"
# 与 backend/.venv 同为 3.13.x：金测试在哪个解释器上绿的，就分发哪个。
ARCH="$(uname -m)"   # arm64 → aarch64；本项目两台设备都是 Apple Silicon
case "$ARCH" in
  arm64) PBS_ARCH="aarch64" ;;
  x86_64) PBS_ARCH="x86_64" ;;
  *) echo "不支持的架构: $ARCH" >&2; exit 1 ;;
esac

OUT="${1:-build/runtime}"
SIGN_ID=""
if [[ "${2:-}" == "--sign" ]]; then SIGN_ID="${3:?--sign 需要一个签名身份}"; fi

ASSET="cpython-${PBS_VER}+${PBS_TAG}-${PBS_ARCH}-apple-darwin-install_only_stripped.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${ASSET}"
CACHE="build/.pbs-cache"

mkdir -p "$CACHE"
if [[ ! -f "$CACHE/$ASSET" ]]; then
  echo "▶ 下载 $ASSET"
  curl -fsSL -o "$CACHE/$ASSET" "$URL"
fi

echo "▶ 解压 → $OUT"
rm -rf "$OUT"; mkdir -p "$(dirname "$OUT")"
tar xzf "$CACHE/$ASSET" -C "$(dirname "$OUT")"
mv "$(dirname "$OUT")/python" "$OUT"

PY="$OUT/bin/python3.13"

echo "▶ 装 edge 依赖（requirements-edge.txt = 不含推理与音频，见 R5-2/D.14）"
# 直接装进 PBS 自带的 site-packages，**不建 venv**：venv 的 pyvenv.cfg 里 `home=` 又是
# 绝对路径，等于把刚获得的可搬性再丢掉一次。整棵树自成一体才是分发物。
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r backend/requirements-edge.txt

# SearXNG 随包（R5-5/D.8）：装进**同一棵树**——实测共享依赖只 +40M（第二棵树要
# +174M），pip check 干净、222 金测试在混装树上全绿。两条顺序约束都不能破：
#   ① 必须在下方签名步骤**之前**：签名后 hardened runtime 的 library validation
#     会拒载任何非同 Team ID 的 .so——签过名的树从此拒绝带原生扩展的 pip 安装
#     （实证：msgspec dlopen "different Team IDs"）。
#   ② **非 editable**（不带 -e）：editable 装的是指回 searxng-src 的引用，
#     树一搬就断，可搬性作废。非 editable 会把包数据（引擎表/模板/静态资源）
#     一并拷进 site-packages（已实测四项俱全）。
SEARX_SRC="searxng/searxng-src"
if [[ -d "$SEARX_SRC" ]]; then
  echo "▶ 装 SearXNG（同树随包，非 editable）"
  "$PY" -m pip install --quiet -U setuptools wheel   # PBS 不带 setuptools，构建 searx 必需
  "$PY" -m pip install --quiet -r "$SEARX_SRC/requirements.txt"
  "$PY" -m pip install --quiet --no-build-isolation "$SEARX_SRC"
  # 版本校验：pin 的单一真相在 lib_edge.sh（devctl setup 按它克隆）
  PIN="$(sed -n 's/^SEARX_COMMIT="\${SEARX_COMMIT:-\([0-9a-f]*\)}".*/\1/p' scripts/lib_edge.sh)"
  HEAD="$(git -C "$SEARX_SRC" rev-parse HEAD 2>/dev/null || echo unknown)"
  if [[ -n "$PIN" && "$HEAD" != "$PIN" ]]; then
    echo "⚠️ searxng-src HEAD(${HEAD:0:12}) ≠ pin(${PIN:0:12})——随包的不是实测版本" >&2
  fi
else
  echo "▶ 跳过 SearXNG（${SEARX_SRC} 不存在；如需随包先 ./devctl.sh setup 拉源码）"
fi

# 装完立刻验可搬：搬走再 import。这一步不是仪式——它挡的正是「某个包在安装时把
# 绝对路径烤进了 .pth/console_script」这类只有换路径才暴露的问题。
echo "▶ 验可搬性"
RELOC="$(dirname "$OUT")/.reloc-probe"
rm -rf "$RELOC"; cp -R "$OUT" "$RELOC"
"$RELOC/bin/python3.13" -c "
import sys, pathlib
assert pathlib.Path(sys.prefix).resolve() == pathlib.Path('$RELOC').resolve(), \
    f'sys.prefix 没跟着搬: {sys.prefix}'
import fastapi, transformers, playwright, sympy, matplotlib, fitz, trafilatura
try:
    import searx   # 随包时一并验：editable 安装会在这里暴露（指回树外源码）
    assert str(pathlib.Path(searx.__file__)).startswith(str(pathlib.Path('$RELOC').resolve())), \
        f'searx 指回树外: {searx.__file__}'
except ImportError:
    pass           # 未随包（searxng-src 缺席）时不苛求
print('  ✅ 搬迁后 sys.prefix 自动跟随，重依赖可导入')
"
rm -rf "$RELOC"

if [[ -n "$SIGN_ID" ]]; then
  echo "▶ 逐个签名（hardened runtime + 安全时间戳）"
  # 必须**由内向外**逐个签：Resources 里的嵌套 Mach-O 不会被外层 .app 的签名覆盖。
  # 不用 `--deep`（Apple 已不建议，且会漏签/错签嵌套物）。
  # hardened runtime 会启用 library validation → 所有 .so 必须与 .app 同一 Team ID 签，
  # 故这里的身份要和 app 构建用的是同一个。实测签完 212 金测试仍全绿。
  #
  # `--timestamp` 不能省：**公证强制要求安全时间戳**（`--timestamp=none` 签出来的
  # 提交必被拒）。它对每个二进制都要向 Apple 的 TSA 发一次网络往返，但 8 路并行下
  # 开销不显著——实测本脚本全程（解压→装依赖→验可搬→签名→核对）48s。
  #
  # 🔴 **前置：私钥的 partition list 必须先设好**，否则 codesign 会为每次用私钥弹一次
  # 钥匙串授权框；在非交互 shell（CI、本脚本）里那个框弹不出来，进程就**无限挂起**，
  # 且不报任何错。手工 `security import` 进来的私钥都有这个问题（`-T` 只设旧 ACL，
  # 不设 partition list；Xcode 自己装的身份则已被设好，所以对比起来像是「这把钥匙坏了」）：
  #     security set-key-partition-list -S apple-tool:,apple:,codesign: -s ~/Library/Keychains/login.keychain-db
  find "$OUT" -type f \( -name "*.so" -o -name "*.dylib" -o -perm +111 \) \
    -exec sh -c 'file -b "$1" | grep -q Mach-O && echo "$1"' _ {} \; \
  | xargs -P 8 -I{} codesign --force --options runtime --timestamp -s "$SIGN_ID" {} 2>&1 \
  | grep -v "replacing existing signature" || true
  # 上面只滤掉 `replacing existing signature` 这一句噪音，**不吞其余 stderr**——
  # 曾经这里是个裸的 `2>/dev/null`，签名静默失败时脚本照样报成功（教训见 §S.5）。
  codesign -v --strict "$PY"

  # 全量核对，不是抽验：数「带安全时间戳且由 Developer ID 签」的文件数，必须等于
  # Mach-O 总数。只验 $PY 挡不住「部分文件漏签」——而漏签的后果要到公证被拒才出现，
  # 那时离现场已经很远。
  echo "▶ 核对签名覆盖"
  MACHO_ALL="$(find "$OUT" -type f \( -name "*.so" -o -name "*.dylib" -o -perm +111 \) \
    -exec sh -c 'file -b "$1" | grep -q Mach-O && echo "$1"' _ {} \;)"
  TOTAL="$(echo "$MACHO_ALL" | grep -c . || true)"
  OK="$(echo "$MACHO_ALL" | xargs -P 12 -I{} sh -c \
    'codesign -dv --verbose=2 "$1" 2>&1 | grep -q "^Timestamp=" && echo ok' _ {} | grep -c . || true)"
  echo "  带安全时间戳: ${OK}/${TOTAL}"
  if [[ "$OK" != "$TOTAL" ]]; then
    echo "  ❌ 有 $((TOTAL-OK)) 个二进制没签上（公证必拒）。若是挂起，先查 partition list（见上）" >&2
    exit 1
  fi
  # ${PY} 的花括号不能省：后面紧跟的是**全角** `）`，bash 会把它当作变量名的一部分
  # 一起吃掉（→ `PY）: unbound variable`）。本文件注释里全角括号很多，凡是紧跟变量的
  # 都要显式界定。
  echo "  ✅ 全部已签（含 ${PY}）"
fi

echo "✅ 运行时就绪：${OUT}（$(du -sh "$OUT" | cut -f1)）"
