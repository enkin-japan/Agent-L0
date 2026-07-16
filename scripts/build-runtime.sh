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
print('  ✅ 搬迁后 sys.prefix 自动跟随，重依赖可导入')
"
rm -rf "$RELOC"

if [[ -n "$SIGN_ID" ]]; then
  echo "▶ 逐个签名（hardened runtime）"
  # 必须**由内向外**逐个签：Resources 里的嵌套 Mach-O 不会被外层 .app 的签名覆盖。
  # 不用 `--deep`（Apple 已不建议，且会漏签/错签嵌套物）。实测 320 个 Mach-O / 3.6s。
  # hardened runtime 会启用 library validation → 所有 .so 必须与 .app 同一 Team ID 签，
  # 故这里的身份要和 app 构建用的是同一个。实测签完 212 金测试仍全绿。
  find "$OUT" -type f \( -name "*.so" -o -name "*.dylib" -o -perm +111 \) \
    -exec sh -c 'file -b "$1" | grep -q Mach-O && echo "$1"' _ {} \; \
  | xargs -P 8 -I{} codesign --force --options runtime --timestamp -s "$SIGN_ID" {} 2>/dev/null
  codesign -v --strict "$PY"
  # ${PY} 的花括号不能省：后面紧跟的是**全角** `）`，bash 会把它当作变量名的一部分
  # 一起吃掉（→ `PY）: unbound variable`）。本文件注释里全角括号很多，凡是紧跟变量的
  # 都要显式界定。
  echo "  ✅ 已签（含 ${PY}）"
fi

echo "✅ 运行时就绪：${OUT}（$(du -sh "$OUT" | cut -f1)）"
