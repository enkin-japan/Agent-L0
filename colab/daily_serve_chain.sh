#!/bin/bash
# 日用算力端全链（VM 侧，由 daily.sh 上传并点火；幂等可重跑）。
# 引导（tailscale 入网 colab-a100 + GIT_TOKEN clone + pip）→ 拉件 → 注册表/上限
# 补丁（VM 本地 config，不入 git）→ GPU 保活 0.5G → 服务监理循环（崩了自动重启，
# 上限 5 次/天）。就绪信号 /content/SERVE_READY；日志 /content/logs/。
#
# 档位由 daily.sh 经 env 注入：
#   SERVE_TIER=l4|a100    l4 = 26B NVFP4（免在线量化、无 MTP）
#                         a100 = 26B fp8+MTP k=4 生产形态（拉草稿件+热补丁）
#   SERVE_MAXLEN          可选：覆盖 cuda.max_model_len（L4 显存紧时下调）
#   SERVE_KV_DTYPE        可选：L4 档缺省 fp8（KV 减半 +20% 提速，tool_call 零漂移
#                         实测 2026-08-09；传 auto 可退回）。A100=SM80 会被拒，缺省 auto。
set -uo pipefail
export LD_LIBRARY_PATH="/usr/lib64-nvidia:${LD_LIBRARY_PATH:-}"
export PATH="/opt/bin:$PATH"
die(){ echo "FATAL: $*"; exit 1; }
[ -f /content/colab.env ] || die "缺 /content/colab.env"
set -a; . /content/colab.env; set +a
[ -n "${GIT_TOKEN:-}" ] || die "colab.env 缺 GIT_TOKEN"
[ -n "${INFER_API_KEY:-}" ] || die "colab.env 缺 INFER_API_KEY"
TIER="${SERVE_TIER:-l4}"
if [ "$TIER" = "l4" ] && [ -z "${SERVE_KV_DTYPE:-}" ]; then
  SERVE_KV_DTYPE="fp8"
fi
# 缺省上下文 65536（8 页 deck 单轮 ~33k 撞 32768 顶的实案）：L4 fp8 KV 池实测
# 114,963 token、A100 档 KV bf16 65536≈3.7GiB 均有余量。L4 上手动退 auto KV 时
# 池只有 57k——须同时显式给 SERVE_MAXLEN（如 49152），此处不猜。
if [ -z "${SERVE_MAXLEN:-}" ] && { [ "$TIER" = "a100" ] || [ "${SERVE_KV_DTYPE:-}" = "fp8" ]; }; then
  SERVE_MAXLEN=65536
fi
APP=/content/agent
mkdir -p /content/logs

echo "== ① tailscale（主机名 colab-a100，与工作端 .env.local 拨号地址对齐）"
command -v tailscale >/dev/null 2>&1 \
  || curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1 || echo "WARN tailscale 安装失败"
if command -v tailscale >/dev/null 2>&1; then
  pgrep -x tailscaled >/dev/null || {
    nohup tailscaled --tun=userspace-networking --state=/content/ts.state \
      --socket=/content/ts.sock >/content/tsd.log 2>&1 &
    sleep 3
  }
  tailscale --socket=/content/ts.sock up --authkey="${TS_AUTHKEY:-}" \
    --hostname=colab-a100 --ssh --accept-dns=false || echo "WARN tailscale 入网失败"
fi

echo "== ② 代码 + 依赖"
if [ ! -d "$APP/.git" ]; then
  [ -d "$APP" ] && mv "$APP" /content/agent-snapshot-$(date +%s)
  git clone --depth 1 \
    "https://x-access-token:${GIT_TOKEN}@github.com/enkin-japan/Agent-L0.git" \
    "$APP" || die "clone 失败"
fi
git -C "$APP" rev-parse --short HEAD
pip -q install -U vllm "huggingface_hub[hf_transfer]" transformers \
    fastapi uvicorn pyyaml requests llguidance pillow soundfile || die "pip 失败"
for d in /usr/local/lib/python3*/dist-packages/nvidia/*/lib; do
  [ -d "$d" ] && export LD_LIBRARY_PATH="$d:$LD_LIBRARY_PATH"
done

echo "== ③ 内存哨兵 + 拉件（tier=${TIER}）"
( while true; do
    avail=$(free -g | awk '/^Mem:/{print $7}')
    [ "${avail:-99}" -lt 8 ] && echo "MEM_ALARM avail=${avail}G $(date +%T)" >> /content/logs/mem_alarm.log
    sleep 30
  done ) & echo $! > /content/mem_sentinel.pid
pull(){
  local dst="$APP/backend/models/$2"
  [ -f "$dst/.download_complete" ] && { echo "已存在: $2"; return 0; }
  HF_TOKEN="$HF_TOKEN" python - "$1" "$dst" <<'PY'
import json, os, sys, time
from pathlib import Path
from huggingface_hub import snapshot_download
repo, dst = sys.argv[1], sys.argv[2]
t0 = time.time()
snapshot_download(repo, local_dir=dst, token=os.environ.get("HF_TOKEN"))
Path(dst, ".download_complete").write_text(json.dumps({"revision": None}))
print(f"OK {repo} {time.time()-t0:.0f}s")
PY
}
if [ "$TIER" = "l4" ]; then
  MODEL_REPO="RedHatAI/gemma-4-26B-A4B-it-NVFP4"
  pull "$MODEL_REPO" gemma-4-26B-A4B-it-NVFP4 || die "NVFP4 拉取失败"
else
  MODEL_REPO="google/gemma-4-26B-A4B-it"
  pull "$MODEL_REPO" gemma-4-26B-A4B-it || die "26B 拉取失败"
  pull google/gemma-4-26B-A4B-it-assistant gemma-4-26B-A4B-it-assistant || die "草稿件拉取失败"
fi

echo "== ④ VM 本地 config 补丁（注册表 NVFP4 件 / max_model_len 覆盖）"
python - <<PY || die "config 补丁失败"
import re
p = "$APP/config.yaml"
s = open(p, encoding="utf-8").read()
if "NVFP4" not in s:
    anchor = '    - repo: "google/gemma-4-26B-A4B-it"'
    entry = ('    - repo: "RedHatAI/gemma-4-26B-A4B-it-NVFP4"\n'
             '      dialect: "gemma4"\n'
             '      capabilities: ["text", "vision"]\n'
             '      profile: { }\n'
             '      note: "日用 L4 件（第 6 站已验带内）"\n')
    s = s.replace(anchor, entry + anchor, 1)
maxlen = "${SERVE_MAXLEN:-}"
if maxlen:
    s = re.sub(r"(max_model_len:\s*)\d+", r"\g<1>" + maxlen, s, count=1) \
        if "max_model_len" in s else s.replace(
        '    gpu_memory_utilization:', f'    max_model_len: {maxlen}\n    gpu_memory_utilization:', 1)
open(p, "w", encoding="utf-8").write(s)
print("config ok")
PY

echo "== ⑤ GPU 保活 0.5G（防用户侧 25min 判零看门在空闲窗误杀；pidfile 纪律）"
pgrep -f "gpu_keepalive[.]py" >/dev/null || {
  nohup python3 "$APP/colab/gpu_keepalive.py" 0.5 > /content/keepalive_gpu.log 2>&1 &
  sleep 1; pgrep -f "python3 .*gpu_keepalive[.]py" > /content/gpu_keepalive.pid
}

echo "== ⑥ 服务监理循环（崩溃自动重启 ≤5 次；A100 档先打 MTP 热补丁）"
[ "$TIER" = "a100" ] && { python "$APP/colab/mtp_patch.py" || die "MTP 热补丁失败"; }
cat > /content/serve_supervisor.sh <<SUP
#!/bin/bash
set -u
export LD_LIBRARY_PATH="$LD_LIBRARY_PATH"
export PATH="$PATH"
set -a; . /content/colab.env; set +a
cd "$APP/backend"
RESTARTS=0
while [ \$RESTARTS -le 5 ]; do
  pkill -f "inference/server[.]py" 2>/dev/null; pkill -f "VLLM::EngineCor[e]" 2>/dev/null; sleep 3
  if [ "$TIER" = "l4" ]; then
    INFER_REMOTE_HOST=127.0.0.1 INFER_PORT=14161 INFER_MODEL_REPO="$MODEL_REPO" \
    INFER_BACKEND=cuda INFER_QUANT=none INFER_KV_DTYPE="${SERVE_KV_DTYPE:-}" PYTHONUNBUFFERED=1 \
      python ../inference/server.py > /content/logs/infer.log 2>&1 &
  else
    DRAFT="$APP/backend/models/gemma-4-26B-A4B-it-assistant"
    INFER_REMOTE_HOST=127.0.0.1 INFER_PORT=14161 INFER_MODEL_REPO="$MODEL_REPO" \
    INFER_BACKEND=cuda PYTHONUNBUFFERED=1 \
    INFER_SPEC_CONFIG="{\"method\":\"mtp\",\"model\":\"\$DRAFT\",\"num_speculative_tokens\":4}" \
      python ../inference/server.py > /content/logs/infer.log 2>&1 &
  fi
  SRV=\$!
  for i in \$(seq 1 300); do
    sleep 4
    curl -s -m 2 http://127.0.0.1:14161/health >/dev/null 2>&1 && break
    kill -0 \$SRV 2>/dev/null || break
  done
  if curl -s -m 2 http://127.0.0.1:14161/health >/dev/null 2>&1; then
    [ -f /content/SERVE_READY ] || {
      python scripts/check_infer_contract.py --base-url http://127.0.0.1:14161 \
        --key "\$INFER_API_KEY" > /content/logs/contract.log 2>&1 \
        && echo CONTRACT_OK >> /content/logs/supervisor.log \
        || echo CONTRACT_FAIL >> /content/logs/supervisor.log
      date > /content/SERVE_READY
    }
    echo "\$(date +%T) server up (restarts=\$RESTARTS)" >> /content/logs/supervisor.log
    wait \$SRV
  fi
  RESTARTS=\$((RESTARTS+1))
  echo "\$(date +%T) server died → 重启 #\$RESTARTS" >> /content/logs/supervisor.log
done
echo "\$(date +%T) SUPERVISOR_GAVE_UP" >> /content/logs/supervisor.log
SUP
chmod +x /content/serve_supervisor.sh
pgrep -f "serve_supervisor[.]sh" >/dev/null \
  || nohup bash /content/serve_supervisor.sh > /dev/null 2>&1 &
echo CHAIN_LAUNCHED
