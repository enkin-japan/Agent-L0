#!/bin/bash
# bench 专用最小引导（VM 侧，2026-08-15）：代码 + pip + 拉件 + mtp 热补丁 + GPU 保活。
# 与 daily_serve_chain.sh 的区别：无 tailscale / 无 server / 无监理循环 / 无 config 补丁
# ——bench 引擎直连不需要它们，而"起 server"正是 08-15 首场 30min 停滞的头号嫌疑段
# （crash-restart 循环在链条 peek 里与真停滞不可分辨）。步骤片段照抄链条已验证写法。
# 就绪信号：stdout 打 "BOOT_READY"。失败：打 "FATAL: ..." 退出。
set -uo pipefail
export LD_LIBRARY_PATH="/usr/lib64-nvidia:${LD_LIBRARY_PATH:-}"
export PATH="/opt/bin:$PATH"
die(){ echo "FATAL: $*"; exit 1; }
[ -f /content/colab.env ] || die "缺 /content/colab.env"
# 场参先、密钥后：bench.env（非密场参，Mac 侧生成）覆盖不了 GIT_TOKEN/HF_TOKEN。
# 旋钮：BENCH_SKIP_PULL=1 跳过 ④；BENCH_TARGET_REPO/DIR、BENCH_DRAFT_REPO/DIR
# 覆盖拉件对象（缺省 26B 场不变）。
[ -f /content/bench.env ] && { set -a; . /content/bench.env; set +a; }
set -a; . /content/colab.env; set +a
# 大件必须落大盘（/root 小盘拉一半撞满=白烧钱）；无条件钉死，连 HF_HUB_CACHE
# 一起（它能整个越过 HF_HOME——评审）
export HF_HOME=/content/hf
export HF_HUB_CACHE=/content/hf/hub
[ -n "${GIT_TOKEN:-}" ] || die "colab.env 缺 GIT_TOKEN"
[ -n "${HF_TOKEN:-}" ] || die "colab.env 缺 HF_TOKEN"
APP=/content/agent
mkdir -p /content/logs

echo "== ⓪ tailscale（控制/数据备用面。2026-08-15 v4 实证：exec 会话凭据 ~45min 内
就会 401 失效而 VM 照常计费——12h 长稳形态 exec 只管点火，之后全走 tailscale，
bench 流照此办理；本步失败仅降级为 exec 单通道，响亮警告不致命）"
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

echo "== ① 代码 + 依赖"
if [ ! -d "$APP/.git" ]; then
  [ -d "$APP" ] && mv "$APP" /content/agent-snapshot-$(date +%s)
  git clone --depth 1 \
    "https://x-access-token:${GIT_TOKEN}@github.com/enkin-japan/Agent-L0.git" \
    "$APP" || die "clone 失败"
fi
# 脱敏（评审 critical）：token URL 会被 git 原样写进 .git/config，而本流程会把 VM
# 控制台捕获提交进仓库——clone 完立即把 remote 换回干净 URL
git -C "$APP" remote set-url origin "https://github.com/enkin-japan/Agent-L0.git"
git -C "$APP" rev-parse --short HEAD
# transformers 钉 <5.15：与 bootstrap.sh/daily_serve_chain.sh 同处方（gemma 系
# 逐层配置在 5.15.0 强抛，vLLM 启动即炸——两处实撞账）
# hf_transfer 按普通包装（hub 1.27 起 extra 'hf-transfer' 已不存在，方括号写法
# 静默变 no-op——本场 boot_console 实证 WARNING）
pip -q install -U vllm huggingface_hub hf_transfer "transformers<5.15" \
    fastapi uvicorn pyyaml requests llguidance pillow soundfile || die "pip 失败"
# torch 家族 CUDA 撕裂处置（2026-08-15 两连撞）：新 Colab 镜像 torch=cu13.0 定制
# build，而镜像预装 torchaudio 停在 cu12.8；PyPI 默认 torchaudio 同为 cu12.8 构建，
# "pip -U 对齐"无效（v3 实证）。本场纯文本推理不需要音频——直接卸 torchaudio
# （transformers 软依赖，缺席即干净降级；本机 mlx venv 无 torchaudio 常年在跑）。
pip -q uninstall -y torchaudio >/dev/null 2>&1 || true
pip show vllm 2>/dev/null | head -2
for d in /usr/local/lib/python3*/dist-packages/nvidia/*/lib; do
  [ -d "$d" ] && export LD_LIBRARY_PATH="$d:$LD_LIBRARY_PATH"
done
# torchvision 裁决必须在 LD 路径修复**之后**（评审：修复前探测会把仅缺库路径的
# 好件误判为撕裂）。能导入留（vllm 多模态可能用），真撕裂才卸，卸完 vllm 导不动才判死。
python3 -c "import torchvision" >/dev/null 2>&1 || {
  echo "torchvision 撕裂/缺失 → 卸载，交给 vllm 导入自检裁决"
  pip -q uninstall -y torchvision >/dev/null 2>&1 || true
}
# 全链导入自检：坏依赖在引导期现形，别拖到首格才崩（bootstrap.sh:107 的加强版）
python3 - <<'PY' || die "依赖全链自检失败"
import importlib.util
import torch, vllm
from transformers import AutoProcessor
tv = "已卸"
if importlib.util.find_spec("torchvision"):
    import torchvision
    tv = torchvision.__version__
print(f"vllm {vllm.__version__} | torch {torch.__version__} | tv {tv} | "
      f"cuda {torch.cuda.is_available()}", flush=True)
PY

echo "== ② 评测件就位（Mac 侧上传到 /content/ 暂存，clone 后落位）"
cp /content/vllm_cell.py "$APP/benchmarks/vllm_cell.py" || die "vllm_cell.py 未上传"
cp /content/_prompt.json "$APP/benchmarks/data/mtp-moe/_prompt.json" || die "_prompt.json 未上传"
# 上传件哈希登记（评审：上传覆盖了 clone 副本，HEAD 短哈希不再唯一标识评测器）
sha256sum /content/vllm_cell.py /content/_prompt.json /content/bench_*.sh 2>/dev/null

echo "== ③ GPU 保活 0.5G + 内存哨兵（拉件期 GPU 空闲防判零；pidfile 纪律）"
pgrep -f "gpu_keepalive[.]py" >/dev/null || {
  nohup python3 "$APP/colab/legacy/gpu_keepalive.py" 0.5 > /content/keepalive_gpu.log 2>&1 &
  sleep 1; pgrep -f "python3 .*gpu_keepalive[.]py" > /content/gpu_keepalive.pid
}
( while true; do
    avail=$(free -g | awk '/^Mem:/{print $7}')
    [ "${avail:-99}" -lt 8 ] && echo "MEM_ALARM avail=${avail}G $(date +%T)" >> /content/logs/mem_alarm.log
    sleep 30
  done ) & echo $! > /content/mem_sentinel.pid

echo "== ④ 拉件（HF 直下）"
# 盘位护栏（评审：60GB 权重拉到第 5 分钟才撞盘满＝白烧钱）：本地盘可用 <80G 即死
avail_g=$(df -BG /content | awk 'NR==2{gsub("G","",$4); print $4}')
[ "${avail_g:-0}" -ge 80 ] || die "本地盘仅 ${avail_g}G <80G，拉件必撞盘满"
pull(){
  local dst="$APP/backend/models/$2"
  [ -f "$dst/.download_complete" ] && { echo "已存在: $2"; return 0; }
  HF_TOKEN="$HF_TOKEN" python3 - "$1" "$dst" <<'PY'
import json, os, sys, time
from pathlib import Path
from huggingface_hub import snapshot_download
repo, dst = sys.argv[1], sys.argv[2]
t0 = time.time()
snapshot_download(repo, local_dir=dst, token=os.environ.get("HF_TOKEN"))
Path(dst, ".download_complete").write_text(json.dumps({"revision": None}))
print(f"OK {repo} {time.time()-t0:.0f}s", flush=True)
PY
}
if [ "${BENCH_SKIP_PULL:-0}" = "1" ]; then
  echo "跳过预拉（BENCH_SKIP_PULL=1，评测器将经 vLLM 自拉，HF_HOME=${HF_HOME}）"
else
  pull "${BENCH_TARGET_REPO:-google/gemma-4-26B-A4B-it}" \
       "${BENCH_TARGET_DIR:-gemma-4-26B-A4B-it}" || die "目标件拉取失败"
  pull "${BENCH_DRAFT_REPO:-google/gemma-4-26B-A4B-it-assistant}" \
       "${BENCH_DRAFT_DIR:-gemma-4-26B-A4B-it-assistant}" || die "草稿件拉取失败"
fi

echo "== ⑤ MTP 热补丁（嵌入共享护栏豁免）"
python3 "$APP/colab/legacy/mtp_patch.py" || die "MTP 热补丁失败"

echo "BOOT_READY"
