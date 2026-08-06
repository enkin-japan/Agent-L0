#!/usr/bin/env bash
# 一次性布置：把 Agent-L0 项目 + 全部测试模型存入 Google Drive（Colab 上运行）。
#
# 用法（Colab notebook 两个 cell）：
#   ① from google.colab import drive; drive.mount('/content/drive')
#   ② !bash /content/drive/MyDrive/AgentColab/stage_to_drive.sh
#
# 前置（你在 Drive 建 AgentColab/secrets/colab.env，内容形如）：
#   HF_TOKEN=hf_xxx          # gemma 系 gated 仓库必需（先在 HF 网页接受许可）
#   GIT_TOKEN=github_pat_xxx # 可选：只读 PAT；缺省则跳过代码入库（走本地 rsync 路）
#
# 设计：
#  · 可重入/断点续传——每个模型完成后在 Drive 打 .complete 标记，重跑自动跳过；
#    会话中断（Colab 时限）后下次接着跑即可。
#  · 中转走 Colab 本地盘 /content（快、支持符号链接），逐模型 rsync 进 Drive 后
#    即删中转——绕开 Drive FUSE 直写慢与 HF 缓存符号链接两个坑。
#  · 逐模型下载前检查本地盘余量（最大单模型 ~70GB，Colab 盘 ~200GB 够用）。
#  · 密钥只经 Drive env 文件，绝不出现在本脚本/仓库/对话里。
set -uo pipefail

DRIVE="${DRIVE:-/content/drive/MyDrive}"
ROOT="$DRIVE/AgentColab"
ENV_FILE="$ROOT/secrets/colab.env"
WORK="${WORK:-/content/stage}"
REPO_URL_BASE="github.com/enkin-japan/Agent-L0.git"

MAIN_MODELS=(
  "google/gemma-4-31B-it"
  "google/gemma-4-26B-A4B-it"
  "Qwen/Qwen3.6-35B-A3B"
  "ornith-ai/Ornith-1.0-35B"
)
RAG_MODELS=(
  "Qwen/Qwen3-Embedding-0.6B"
  "Qwen/Qwen3-Embedding-4B"
  "Qwen/Qwen3-Embedding-8B"
  "Qwen/Qwen3-VL-Embedding-2B"
  "Qwen/Qwen3-VL-Embedding-8B"
  "Qwen/Qwen3-Reranker-0.6B"
  "Qwen/Qwen3-Reranker-4B"
  "Qwen/Qwen3-Reranker-8B"
  "Qwen/Qwen3-VL-Reranker-2B"
  "Qwen/Qwen3-VL-Reranker-8B"
)

fail=0
note() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }

[ -d "$DRIVE" ] || { echo "❌ Google Drive 未挂载：先跑 drive.mount('/content/drive')"; exit 1; }
mkdir -p "$ROOT"/{secrets,code,models,cache} "$WORK"

if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
  note "已读取 $ENV_FILE"
else
  warn "未见 $ENV_FILE —— gated 模型（gemma 系）与私库代码会失败，其余照常。"
fi

note "安装 huggingface_hub（含 hf_transfer 加速）"
pip -q install -U "huggingface_hub[cli,hf_transfer]" >/dev/null
export HF_HUB_ENABLE_HF_TRANSFER=1
HF=hf; command -v hf >/dev/null 2>&1 || HF=huggingface-cli

# ---- 代码入库（可选：有 GIT_TOKEN 才做；否则走本地 rsync→Drive 路） ----
note "项目代码"
if [ -n "${GIT_TOKEN:-}" ]; then
  rm -rf "$WORK/repo.git"
  if git clone --quiet --mirror "https://x-access-token:${GIT_TOKEN}@${REPO_URL_BASE}" "$WORK/repo.git"; then
    git -C "$WORK/repo.git" bundle create "$WORK/Agent-L0.bundle" --all
    mv -f "$WORK/Agent-L0.bundle" "$ROOT/code/Agent-L0.bundle"
    # 顺手出一份 HEAD 工作树 tar（Colab 引导时解包更快，也不依赖 git 版本）
    rm -rf "$WORK/src" && git clone --quiet --depth 1 "$WORK/repo.git" "$WORK/src" \
      && tar -C "$WORK/src" --exclude='.git' -czf "$ROOT/code/Agent-L0-src.tar.gz" . \
      && rm -rf "$WORK/src"
    git -C "$WORK/repo.git" log -1 --format='%h %ci %s' > "$ROOT/code/HEAD.txt" || true
    rm -rf "$WORK/repo.git"
    echo "✅ code/Agent-L0.bundle + Agent-L0-src.tar.gz（HEAD: $(cat "$ROOT/code/HEAD.txt" 2>/dev/null)）"
  else
    warn "clone 失败（token 权限？）——跳过代码，模型照常。"; fail=1
  fi
else
  warn "无 GIT_TOKEN，跳过代码入库（按原计划：本地改完 rsync 到 Drive）。"
fi

# ---- 模型入库：逐个 下载→转存→打标→清中转 ----
stage_model() {
  local repo="$1" tag="$2"
  local safe="${repo//\//__}"
  local dst="$ROOT/models/$safe" tmp="$WORK/$safe"
  if [ -f "$dst/.complete" ]; then
    echo "⏭  $repo 已在库（$(du -sh "$dst" 2>/dev/null | cut -f1)）"
    return 0
  fi
  local avail_gb
  avail_gb=$(df --output=avail -BG /content | tail -1 | tr -dc '0-9')
  if [ "${avail_gb:-0}" -lt 90 ]; then
    warn "$repo 前本地盘仅剩 ${avail_gb}GB（<90GB 安全线）——清理中转后重试"
    rm -rf "$WORK"/*; avail_gb=$(df --output=avail -BG /content | tail -1 | tr -dc '0-9')
    [ "${avail_gb:-0}" -lt 90 ] && { warn "空间仍不足，跳过 $repo"; fail=1; return 1; }
  fi
  note "下载 $repo（$tag）→ 本地中转"
  if ! $HF download "$repo" --local-dir "$tmp" --quiet; then
    warn "$repo 下载失败（gated 未接受许可？仓库名有误？）——跳过继续"
    rm -rf "$tmp"; fail=1; return 1
  fi
  note "转存 $repo → Drive"
  mkdir -p "$dst"
  if rsync -a --info=progress2 --exclude='.cache' "$tmp/" "$dst/"; then
    touch "$dst/.complete"
    echo "✅ $repo（$(du -sh "$dst" | cut -f1)）"
  else
    warn "$repo 转存 Drive 失败（Drive 配额满？）"; fail=1
  fi
  rm -rf "$tmp"
}

note "主模型 ×${#MAIN_MODELS[@]}"
for m in "${MAIN_MODELS[@]}";  do stage_model "$m" "main"; done
note "向量/重排模型 ×${#RAG_MODELS[@]}"
for m in "${RAG_MODELS[@]}";  do stage_model "$m" "rag"; done

# ---- 盘点 ----
note "盘点 $ROOT"
du -sh "$ROOT/models"/*/ 2>/dev/null | sed "s|$ROOT/models/||"
echo "----"
du -sh "$ROOT/models" "$ROOT/code" 2>/dev/null
n_done=$(find "$ROOT/models" -name .complete 2>/dev/null | wc -l | tr -d ' ')
echo "完成标记: $n_done / $(( ${#MAIN_MODELS[@]} + ${#RAG_MODELS[@]} ))"
if [ "$fail" -ne 0 ]; then
  warn "有条目失败/跳过（见上方 ⚠）。修复前置后重跑本脚本即可断点续传。"
  exit 2
fi
note "全部入库完成 ✅"
