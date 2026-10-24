#!/usr/bin/env bash
# 本地 → Google Drive 的代码同步（开发 Mac 上跑；免 Colab 碰 GitHub 的日常通道）。
#
# 用法：./colab/legacy/sync_to_colab.sh
# 产出：Drive AgentColab/code/Agent-L0-src.tar.gz（+ HEAD.txt 指纹）——bootstrap
# 解包的就是它。权重不经此路（stage_to_drive.sh 在 Colab 侧拉 HF）。
#
# Drive 根自动探测 macOS 桌面版挂载点；也可 GDRIVE_ROOT=... 显式指定
# （rclone 用户可改为 rclone copy，同一产物路径即可）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
if [ -z "${GDRIVE_ROOT:-}" ]; then
  for d in "$HOME"/Library/CloudStorage/GoogleDrive-*/"My Drive" \
           "$HOME/Google Drive/My Drive" "$HOME/Google Drive"; do
    [ -d "$d" ] && GDRIVE_ROOT="$d" && break
  done
fi
[ -n "${GDRIVE_ROOT:-}" ] && [ -d "$GDRIVE_ROOT" ] \
  || { echo "❌ 找不到 Google Drive 挂载（装桌面版或 GDRIVE_ROOT=... 指定）"; exit 1; }

DEST="$GDRIVE_ROOT/AgentColab/code"
mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 打包 HEAD 工作树（含未提交改动的工作区版本；排除重资产与运行时产物）
tar -C "$REPO_ROOT" \
    --exclude='.git' --exclude='backend/.venv' --exclude='backend/models' \
    --exclude='backend/sessions' --exclude='backend/static' --exclude='logs' \
    --exclude='rag' --exclude='node_modules' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='frontend/dist' \
    -czf "$TMP/Agent-L0-src.tar.gz" .
mv -f "$TMP/Agent-L0-src.tar.gz" "$DEST/Agent-L0-src.tar.gz"
{ git -C "$REPO_ROOT" log -1 --format='%h %ci %s' 2>/dev/null || echo "no-git"; \
  git -C "$REPO_ROOT" status --porcelain 2>/dev/null | head -20; } > "$DEST/HEAD.txt"
echo "✅ 已同步 → $DEST/Agent-L0-src.tar.gz ($(du -h "$DEST/Agent-L0-src.tar.gz" | cut -f1))"
echo "   指纹: $(head -1 "$DEST/HEAD.txt")"
echo "   （Drive 桌面版随后自动上云；Colab 侧重跑 bootstrap 前先 rm -rf /content/agent 取新包）"
