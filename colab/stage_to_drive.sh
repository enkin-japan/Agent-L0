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
#  · 模型一律**直下 Drive 路径**（hf --local-dir 指向 Drive）。2026-08-06 实测教训：
#    「本地中转 + rsync 进 Drive」在 Colab 上是 2× 本地盘占用——DriveFS 的**写缓存
#    也落在本地盘**，62GB 大件 = 62G 中转 + 62G 缓存，108G 免费档盘必死（31B 首跑
#    即 rsync 中途盘满，当时误判为 Drive 配额）。直下=只占缓存一份（1×）。
#  · 逐模型前置**缓存排空闸门**：DriveFS 上传排空前本地盘持续被占，大件等
#    ≥80G、小件等 ≥20G 可用再动手（上限 2h，超时跳过该件如实上报）。
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
export HF_XET_HIGH_PERFORMANCE=1   # hf_transfer 已废弃，Xet 高速通道是现名
HF=hf; command -v hf >/dev/null 2>&1 || HF=huggingface-cli

# ---- 代码入库（仓库现为公开=匿名拉取；GIT_TOKEN 有则用，转回私库时无缝） ----
note "项目代码"
if [ -n "${GIT_TOKEN:-}" ]; then
  CLONE_URL="https://x-access-token:${GIT_TOKEN}@${REPO_URL_BASE}"
else
  CLONE_URL="https://${REPO_URL_BASE}"
fi
{
  rm -rf "$WORK/repo.git"
  if git clone --quiet --mirror "$CLONE_URL" "$WORK/repo.git"; then
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
    warn "clone 失败（私库无 token？网络？）——跳过代码，模型照常。"; fail=1
  fi
}

# ---- 模型入库：逐个 下载→转存→打标→清中转 ----
stage_model() {
  local repo="$1" tag="$2"
  local safe="${repo//\//__}"
  local dst="$ROOT/models/$safe"
  if [ -f "$dst/.complete" ]; then
    echo "⏭  $repo 已在库（$(du -sh "$dst" 2>/dev/null | cut -f1)）"
    return 0
  fi
  # 缓存排空闸门：DriveFS 写缓存占本地盘，排空前动手会把盘挤死（2026-08-06 案）。
  # 大件（main 档 bf16 50-70G）等 ≥80G，小件等 ≥20G；上限 2h，超时跳过如实上报。
  local need=20 avail_gb waited=0
  [ "$tag" = "main" ] && need=80
  while :; do
    avail_gb=$(df --output=avail -BG /content | tail -1 | tr -dc '0-9')
    [ "${avail_gb:-0}" -ge "$need" ] && break
    if [ "$waited" -ge 7200 ]; then
      warn "$repo 等 DriveFS 缓存排空超 2h（仅 ${avail_gb}G，需 ${need}G）——跳过"
      fail=1; return 1
    fi
    [ "$waited" -eq 0 ] && echo "⏳ $repo 等缓存排空（${avail_gb}G/${need}G）…"
    sleep 30; waited=$((waited + 30))
  done
  note "直下 $repo（$tag）→ Drive"
  if $HF download "$repo" --local-dir "$dst" --quiet; then
    rm -rf "$dst/.cache"; touch "$dst/.complete"
    echo "✅ $repo（$(du -sh "$dst" | cut -f1)）"
  else
    # 半成品留在 dst：hf --local-dir 自身可断点续传，重跑本脚本即续
    warn "$repo 下载失败（gated 未接受许可？网络抖动？重跑即续传）——跳过继续"
    fail=1; return 1
  fi
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
