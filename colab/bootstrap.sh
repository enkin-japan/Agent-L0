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
[ -n "${TS_AUTHKEY:-}" ] || die "colab.env 缺 TS_AUTHKEY"
[ -n "${INFER_API_KEY:-}" ] || die "colab.env 缺 INFER_API_KEY（≥16 位，护栏拒短钥）"
[ "${#INFER_API_KEY}" -ge 16 ] || die "INFER_API_KEY 太短（<16，非回环护栏口径）"

# ── ① Tailscale（userspace：无 TUN 也能入网；入站转发到 localhost 同端口）──
note "① Tailscale 入网"
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
TS_IP=$($TS ip -4 2>/dev/null | head -1)
note "tailnet 就绪: $HOSTNAME_TS ($TS_IP)"

# ── ② 代码 ──
note "② 代码解包"
if [ ! -d "$APP/backend" ]; then
  mkdir -p "$APP"
  if [ -f "$ROOT/code/Agent-L0-src.tar.gz" ]; then
    tar -C "$APP" -xzf "$ROOT/code/Agent-L0-src.tar.gz"
  else
    git clone --depth 1 https://github.com/enkin-japan/Agent-L0.git "$APP" \
      || die "无 Drive 代码包且 clone 失败"
  fi
fi
echo "代码就绪: $APP（$(cat "$ROOT/code/HEAD.txt" 2>/dev/null || echo 'HEAD 未知')）"

# ── ③ 依赖（Drive 缓存加速二跑；不用 backend/requirements.txt——那是 MLX 家的）──
note "③ pip 依赖（vLLM 等，首跑 ~5 分钟）"
export PIP_CACHE_DIR="$ROOT/cache/pip"
mkdir -p "$PIP_CACHE_DIR"
pip -q install -U vllm "huggingface_hub[hf_transfer]" transformers \
    fastapi uvicorn pyyaml requests llguidance pillow soundfile \
  || die "pip 安装失败"
python -c "import vllm, torch; print(f'vllm {vllm.__version__} | torch {torch.__version__} | cuda {torch.cuda.is_available()}')" \
  || die "vllm/torch 导入失败"

# ── ④ 权重：Drive → 本地盘 + 下载完整性标记（path_resolver 口径）──
note "④ 权重落盘: $MODEL_REPO"
SAFE="${MODEL_REPO//\//__}"
SRC="$ROOT/models/$SAFE"
MODELS_ROOT="$APP/backend/models"
DST="$MODELS_ROOT/$(basename "$MODEL_REPO")"
[ -f "$SRC/.complete" ] || die "Drive 里没有 $MODEL_REPO（先跑 stage_to_drive.sh）"
mkdir -p "$MODELS_ROOT"
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
pkill -f "inference/server.py" 2>/dev/null; sleep 1
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
