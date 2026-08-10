#!/usr/bin/env bash
# Colab 算力端引导：一条命令从裸会话到「可被 edge 拨号的 CUDA 推理服务」。
#
# 用法（notebook cell，先 drive.mount）：
#   !bash /content/drive/MyDrive/AgentColab/bootstrap.sh [模型repo]
#   例：!bash .../bootstrap.sh google/gemma-4-31B-it
#
# 前置：AgentColab/secrets/colab.env 追加两行（stage 阶段已有 HF_TOKEN）：
#   TS_AUTHKEY=tskey-auth-xxx   # Tailscale ephemeral+reusable auth key
#   INFER_API_KEY=<≥16位>       # 推理服务鉴权钥（backend .env.local 同值）
#
# 步骤（幂等，可整段重跑）：
#   ① tailscale userspace 入网（无 TUN；入站自动转发到本机端口——服务绑回环即可
#      被 tailnet 访问，非回环护栏无需触碰）
#   ② 解包代码（Drive 的 src tar；本地已有则跳过）
#   ③ pip 依赖（缓存指向 Drive，二跑起 ~1 分钟）
#   ④ 权重 Drive→本地盘（FUSE 上 mmap 推理不可用；拷贝后写 .download_complete
#      标记喂 path_resolver）
#   ⑤ 拉起 inference-server（backend: cuda）→ 健康探活 → 契约测试
#
# 收尾（edge 侧）：./devctl.sh start -p edge -h <本节点 MagicDNS 名或 100.x IP>
set -uo pipefail

# Colab 的 GPU 库路径只注入 notebook 壳；tailscale SSH 进来的裸环境必须自补，
# 否则 nvidia-smi/torch 全部找不到 libnvidia-ml（A100 首会话实测案）
export LD_LIBRARY_PATH="/usr/lib64-nvidia:${LD_LIBRARY_PATH:-}"
export PATH="/opt/bin:$PATH"

MODEL_REPO="${1:-google/gemma-4-31B-it}"
DRIVE="${DRIVE:-/content/drive/MyDrive}"
ROOT="$DRIVE/AgentColab"
ENV_FILE="$ROOT/secrets/colab.env"
APP=/content/agent
PORT="${INFER_PORT:-14161}"
HOSTNAME_TS="${TS_HOSTNAME:-colab-a100}"

note() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m❌ %s\033[0m\n' "$*"; exit 1; }

[ -d "$DRIVE" ] || die "Drive 未挂载：先 drive.mount('/content/drive')"
[ -f "$ENV_FILE" ] || die "缺 $ENV_FILE（TS_AUTHKEY / INFER_API_KEY / HF_TOKEN）"
set -a; . "$ENV_FILE"; set +a
[ -n "${INFER_API_KEY:-}" ] || die "colab.env 缺 INFER_API_KEY（≥16 位，护栏拒短钥）"
[ "${#INFER_API_KEY}" -ge 16 ] || die "INFER_API_KEY 太短（<16，非回环护栏口径）"

# ── ① Tailscale：已入网（用户手工 cell 打通）则直接复用，否则 authkey 自入 ──
note "① Tailscale 入网"
TS=""
for sock in /var/run/tailscale/tailscaled.sock /content/ts.sock /content/tailscaled.sock; do
  if tailscale --socket="$sock" status >/dev/null 2>&1; then
    TS="tailscale --socket=$sock"; break
  fi
done
if [ -z "$TS" ]; then
  [ -n "${TS_AUTHKEY:-}" ] || die "未入网且 colab.env 缺 TS_AUTHKEY（或先在 notebook 手工入网）"
  if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1 || die "tailscale 安装失败"
  fi
  if ! pgrep -x tailscaled >/dev/null 2>&1; then
    nohup tailscaled --tun=userspace-networking \
        --state=/content/tailscaled.state \
        --socket=/content/tailscaled.sock >/content/tailscaled.log 2>&1 &
    sleep 3
  fi
  TS="tailscale --socket=/content/tailscaled.sock"
  $TS up --authkey="$TS_AUTHKEY" --hostname="$HOSTNAME_TS" --ssh --accept-dns=false \
    || die "tailscale up 失败（authkey 过期/用尽？）"
fi
TS_IP=$($TS ip -4 2>/dev/null | head -1)
note "tailnet 就绪: $TS_IP"

# ── ② 代码 ──
note "② 代码解包"
if [ ! -d "$APP/backend" ]; then
  mkdir -p "$APP"
  if [ -f "$ROOT/code/Agent-L0-src.tar.gz" ]; then
    tar -C "$APP" -xzf "$ROOT/code/Agent-L0-src.tar.gz"
  else
    # 仓库为私密（2026-08-08 可见性已复归 PRIVATE）：匿名 https 必挂，须带
    # colab.env 的 GIT_TOKEN；无 token 时仍试匿名（万一临时公开）
    CLONE_URL="https://github.com/enkin-japan/Agent-L0.git"
    [ -n "${GIT_TOKEN:-}" ] && CLONE_URL="https://x-access-token:${GIT_TOKEN}@github.com/enkin-japan/Agent-L0.git"
    git clone --depth 1 "$CLONE_URL" "$APP" \
      || die "无 Drive 代码包且 clone 失败"
  fi
fi
echo "代码就绪: $APP（$(cat "$ROOT/code/HEAD.txt" 2>/dev/null || echo 'HEAD 未知')）"

# ── ③ 依赖（Drive 缓存加速二跑；不用 backend/requirements.txt——那是 MLX 家的）──
note "③ pip 依赖（vLLM 等，首跑 ~5 分钟）"
export PIP_CACHE_DIR="$ROOT/cache/pip"
mkdir -p "$PIP_CACHE_DIR"
# faster-whisper = ASR 的 CUDA 腿（CTranslate2；A3）——/v1/transcribe 在这台机器上
# 靠它才不 503（mlx-whisper 是 mac 专属件，装不上也用不了）。pip -U 天然幂等。
# transformers 钉 <5.15：5.15.0 对 gemma 系逐层配置强抛 AmbiguousGlobalPerLayerAttribute，
# vLLM get_head_size 全局读 head_dim 即崩（2026-08-10 实案：新 VM 漂到 5.15.0，
# server 六连崩 supervisor 放弃）。等 vLLM 适配后再解钉。
pip -q install -U vllm "huggingface_hub[hf_transfer]" "transformers<5.15" \
    fastapi uvicorn pyyaml requests llguidance pillow soundfile faster-whisper \
  || die "pip 安装失败"
# vLLM 新轮子的原生扩展可能链更高大版的 CUDA 运行时（实测 cu13）而 Colab 自带
# torch 是 cu12x——pip 的 nvidia/* 库目录全部前置进 LD 路径，两代 cudart soname
# 不同可共存（A100 首会话实测修法）
for d in /usr/local/lib/python3*/dist-packages/nvidia/*/lib; do
  [ -d "$d" ] && export LD_LIBRARY_PATH="$d:$LD_LIBRARY_PATH"
done
python -c "import vllm, torch; print(f'vllm {vllm.__version__} | torch {torch.__version__} | cuda {torch.cuda.is_available()}')" \
  || die "vllm/torch 导入失败"

# ── ④ 权重：Drive → 本地盘 + 下载完整性标记（path_resolver 口径）──
note "④ 权重落盘: $MODEL_REPO"
SAFE="${MODEL_REPO//\//__}"
SRC="$ROOT/models/$SAFE"
MODELS_ROOT="$APP/backend/models"
DST="$MODELS_ROOT/$(basename "$MODEL_REPO")"
[ -f "$SRC/.complete" ] || die "Drive 里没有 $MODEL_REPO（先跑 stage_to_drive.sh）"
# 权重根优先落独立 NVMe scratch（A100 档实测 /mnt/local-scratch 369G 全空，
# 2026-08-07 用户发现）：backend/models 改为指向 scratch 的符号链接——四主模型
# 241G 可共存，连测换模型免删免重拉；overlay 主盘只留代码与缓存。既有权重搬家
# 后原目录让位（scratch 与 /content 同属会话生命周期，回收即失，Drive 仍是真身）。
NVME_SCRATCH=/mnt/local-scratch
if [ -d "$NVME_SCRATCH" ] && [ -w "$NVME_SCRATCH" ] && [ ! -L "$MODELS_ROOT" ]; then
  mkdir -p "$NVME_SCRATCH/agent-models"
  if [ -d "$MODELS_ROOT" ]; then
    for d in "$MODELS_ROOT"/*/; do
      [ -d "$d" ] && { echo "权重搬家 → scratch: $(basename "$d")"; mv "$d" "$NVME_SCRATCH/agent-models/"; }
    done
    rm -rf "$MODELS_ROOT"
  fi
  ln -sfn "$NVME_SCRATCH/agent-models" "$MODELS_ROOT"
fi
mkdir -p "$MODELS_ROOT/"
# 盘面自清兜底：权重根所在盘不足 80G 时清掉非目标模型目录（Drive 真身随删随取；
# 已装载引擎的权重在显存，不需要盘上文件）。scratch 在位时正常不会触发；无 scratch
# 的机型（如免费 T4）沿用 2026-08-07 33G 剩量惊险案的教训。
avail_gb=$(df --output=avail -BG "$MODELS_ROOT/" | tail -1 | tr -dc '0-9')
if [ "${avail_gb:-0}" -lt 80 ]; then
  for d in "$MODELS_ROOT"/*/; do
    [ "${d%/}" = "$DST" ] && continue
    echo "盘面自清：删 ${d}（免费仅 ${avail_gb}G）"; rm -rf "${d}"
  done
fi
if [ ! -f "$DST/.download_complete" ]; then
  rsync -a --info=progress2 --exclude='.complete' "$SRC/" "$DST/"
  python - "$DST" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
(p / ".download_complete").write_text(json.dumps({"revision": None}), encoding="utf-8")
print("标记写入:", p / ".download_complete")
PY
fi
du -sh "$DST"

# ── ⑤ 拉起推理服务 → 探活 → 契约测试 ──
note "⑤ inference-server (backend=cuda, :$PORT)"
# 换代必须把整棵进程树清干净：vLLM EngineCore 是独立子进程（cmdline 不含
# server.py 模式），只杀父进程会留下抱着全部显存的孤儿——26B 起测时 free 仅
# 4.65G 即此案。杀完等 nvidia-smi 计算进程清零，残留者 -9 兜底。
pkill -f "inference/server.py" 2>/dev/null
pkill -f "VLLM::EngineCore" 2>/dev/null
for i in $(seq 1 30); do
  busy=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c '[0-9]')
  [ "${busy:-0}" -eq 0 ] && break
  sleep 2
done
nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | xargs -r kill -9 2>/dev/null
sleep 2
cd "$APP/backend"
# 绑回环：userspace tailscale 会把 tailnet 入站转发到本机端口，护栏口径最稳
INFER_REMOTE_HOST=127.0.0.1 INFER_PORT="$PORT" INFER_MODEL_REPO="$MODEL_REPO" \
INFER_BACKEND=cuda PYTHONUNBUFFERED=1 \
nohup python ../inference/server.py > /content/infer.log 2>&1 &
echo "加载中（bf16→fp8 在线量化，31B 档首启约 5-10 分钟）… tail -f /content/infer.log 可观察"
for i in $(seq 1 300); do
  sleep 4
  if curl -s -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "server 就绪 ✅"; UP=1; break
  fi
  pgrep -f "inference/server.py" >/dev/null || { tail -30 /content/infer.log; die "server 进程死亡（见上方日志尾）"; }
done
[ "${UP:-}" = 1 ] || { tail -30 /content/infer.log; die "20 分钟未就绪"; }

note "契约测试（CUDA 端验收门禁）"
python scripts/check_infer_contract.py --base-url "http://127.0.0.1:$PORT" --key "$INFER_API_KEY" \
  || die "契约未过——CUDA 后端还有账要还（看输出逐项修）"

note "全部就绪 🎉  edge 侧拨号："
echo "  ./devctl.sh start -p edge -h $HOSTNAME_TS   # 或 -h $TS_IP"
echo "  （edge 的 .env.local 需 INFER_API_KEY 同值）"
