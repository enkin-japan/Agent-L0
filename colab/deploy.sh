#!/usr/bin/env bash
# Colab 计算端的唯一用户入口；租约编排只有 colab/compute_ctl.py 一份实现。
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v uv >/dev/null 2>&1; then
  echo '需要 uv（本机 Colab CLI 的安装工具）。安装后重试。' >&2
  exit 1
fi
# A detached worker must not inherit uv run's temporary --with environment:
# uv removes that environment when the foreground launcher exits, breaking the
# worker's later sys.executable calls (DNS verification and recovery).
RUNTIME="$SCRIPT_DIR/../.colab-compute/launcher-venv"
PYYAML_VERSION='6.0.3'
umask 077
mkdir -p "$(dirname "$RUNTIME")"
if [ ! -x "$RUNTIME/bin/python" ]; then
  uv venv --python 3.13 "$RUNTIME"
fi
if ! "$RUNTIME/bin/python" -c "import yaml; assert yaml.__version__ == '$PYYAML_VERSION'" >/dev/null 2>&1; then
  uv pip install --python "$RUNTIME/bin/python" "PyYAML==$PYYAML_VERSION"
fi
exec "$RUNTIME/bin/python" "$SCRIPT_DIR/compute_ctl.py" "$@"
