#!/bin/bash
# bench 场 Mac 侧编排（v5 形态落库版，2026-08-15；首版被评审毙 8 条 Critical 后重写）。
#
# 形态铁律（08-15 四连崩验尸）：**exec 只管点火与 boot 观察**——exec 会话凭据
# ~45min 必失效而 VM 照常计费；BOOT_READY 后一律走 tailscale ssh/scp。
# 重量级件（62GB 权重）一律 boot 期预拉（boot.log 可见、有独立预算），
# 绝不塞进评测格的 timeout 里（评审 Critical：格超时→下一格重下→双杀中止）。
#
# 用法：./colab/bench_orchestrate.sh <VM侧驱动名> [输出目录]
# 防线：trap 在 colab new 一成功就装（计费窗先于一切）；清尾顺序=取证→tailscale
# logout（防下一场主机名漂移）→stop→sleep3+ls 验证真停；ssh 全程 ServerAlive
# 有界；逐轮增量取回；总时长 60min 硬顶；控制台落地一律脱敏 token。
set -uo pipefail
export PATH="$HOME/Library/Python/3.13/bin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HOME/.config/agent-colab"
ENVF="$STATE/colab.env"
CLI_CFG="$HOME/.config/colab-cli"
DRIVER="${1:?用法: bench_orchestrate.sh <驱动脚本名> [输出目录]}"
OUTDIR="${2:-$HERE/../benchmarks/data/bench-fetch-$(date +%y%m%d%H%M%S)}"
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 -o ServerAliveInterval=10 -o ServerAliveCountMax=3"
SSH="ssh -n $SSH_OPTS root@colab-a100"
NO_STOP="${NO_STOP:-0}"
HARD_CAP_MIN=60
die(){ echo "❌ $*"; exit 1; }
note(){ printf '\033[0;36m[bench]\033[0m %s\n' "$*"; }
redact(){ sed -E 's/(x-access-token|hf_[A-Za-z0-9]{4})[^@[:space:]]*/\1***REDACTED***/g'; }

[ -f "$ENVF" ] || die "缺 $ENVF"
[ -f "$HERE/$DRIVER" ] || die "缺驱动 $HERE/$DRIVER"
command -v colab >/dev/null || die "colab CLI 不在 PATH"
mkdir -p "$OUTDIR" "$STATE/logs"

name="bench-$(date +%y%m%d%H%M)"
ssh-keygen -R colab-a100 >/dev/null 2>&1 || true

note "开 A100 会话 ${name}（~5.3u/h，总时长硬顶 ${HARD_CAP_MIN}min）…"
colab new --gpu A100 -s "$name" || die "colab new 失败"
T0=$(date +%s)

FINAL_STATE="启动中"
fetch_forensics(){  # 无论成败都先抢数据（评审：失败的场恰恰最需要取证）
  scp -q $SSH_OPTS "root@colab-a100:/content/bench_out/*" "$OUTDIR/" 2>/dev/null || true
  for f in boot.log drive.log; do
    $SSH "cat /content/$f 2>/dev/null" 2>/dev/null | redact \
      > "$OUTDIR/${f%.log}_console.txt" || true
  done
  $SSH "cat /content/logs/mem_alarm.log 2>/dev/null" 2>/dev/null \
    > "$OUTDIR/mem_alarm.txt" || true
  find "$OUTDIR" -type f -size 0 -delete 2>/dev/null || true
}
cleanup(){
  local rc=$?
  [ "$NO_STOP" = "1" ] && { note "NO_STOP=1：按约保留会话 ${name}（自行收尾！）"; exit $rc; }
  note "收尾（状态: ${FINAL_STATE}）：取证 → tailnet 登出 → 停会话…"
  fetch_forensics
  $SSH "tailscale --socket=/content/ts.sock logout" >/dev/null 2>&1 || true
  local out srv_rc
  out=$(colab stop -s "$name" 2>&1); srv_rc=$?
  echo "$out" | tail -1
  if [ $srv_rc -ne 0 ]; then
    note "直接 stop 失败 → rescue（备份互换重试）"
    cp "$CLI_CFG/sessions.json" "$STATE/sessions.json.orphaned-$(date +%s)" 2>/dev/null || true
    [ -f "$STATE/sessions.json.bak" ] && cp "$STATE/sessions.json.bak" "$CLI_CFG/sessions.json"
    colab stop -s "$name" 2>&1 | tail -1
  fi
  # 验停带重试：stop 刚返回时 ls 输出形态不稳（08-15 首跑实撞：3 秒后首查
  # 未匹配、几秒后即 "No active sessions"——差点误报未停）
  stopped=0
  for _ in 1 2 3; do
    sleep 5
    if colab ls 2>&1 | sed $'s/\x1b\\[[0-9;]*m//g' | grep -q "No active sessions"; then
      stopped=1; break
    fi
  done
  if [ "$stopped" = "1" ]; then
    note "✅ 会话已确认停止"
  else
    note "⚠️⚠️ 会话可能未停！立即手查：colab ls / colab stop -s ${name} / daily.sh rescue"
    colab ls 2>&1 | tail -3
    rc=1
  fi
  rm -f "$STATE/bench_session"
  exit $rc
}
trap cleanup EXIT

cp "$CLI_CFG/sessions.json" "$STATE/sessions.json.bak" \
  || note "⚠️ sessions.json 备份失败——孤儿救援手段减一，继续（trap 已在岗）"
echo "$name" > "$STATE/bench_session"   # 不占 daily.sh 的 current_session 注册表

note "上传密钥/场参/引导/驱动/评测件…"
colab upload -s "$name" "$ENVF" /content/colab.env >/dev/null || die "colab.env 上传失败"
cat > "$STATE/bench.env" <<'EOF'
BENCH_TARGET_REPO=google/gemma-4-31B-it
BENCH_TARGET_DIR=gemma-4-31B-it
BENCH_DRAFT_REPO=google/gemma-4-31B-it-assistant
BENCH_DRAFT_DIR=gemma-4-31B-it-assistant
HF_HUB_ENABLE_HF_TRANSFER=1
EOF
colab upload -s "$name" "$STATE/bench.env" /content/bench.env >/dev/null || die "bench.env 上传失败"
colab upload -s "$name" "$HERE/bench_bootstrap.sh" /content/bench_bootstrap.sh >/dev/null || die "引导上传失败"
colab upload -s "$name" "$HERE/$DRIVER" "/content/$DRIVER" >/dev/null || die "驱动上传失败"
colab upload -s "$name" "$HERE/../benchmarks/vllm_cell.py" /content/vllm_cell.py >/dev/null || die "vllm_cell 上传失败"
colab upload -s "$name" "$HERE/../benchmarks/data/mtp-31b-cross/_prompt.json" /content/_prompt.json >/dev/null || die "prompt 上传失败"

cat > "$STATE/kick.py" <<'EOF'
import subprocess
subprocess.run("nohup bash /content/bench_bootstrap.sh > /content/boot.log 2>&1 & echo KICKED",
               shell=True)
EOF
colab exec -s "$name" -f "$STATE/kick.py" --timeout 30 >/dev/null || die "点火失败"

note "等 BOOT_READY（pip + 62GB 预拉，预期 ~10-15min；exec 观察，停滞 12min 判死）…"
cat > "$STATE/peek.py" <<'EOF'
import subprocess
r = subprocess.run(
    "tail -4 /content/boot.log 2>/dev/null; du -s /content/agent/backend/models 2>/dev/null",
    shell=True, capture_output=True, text=True)
print(r.stdout[-700:])
EOF
last=""; stall=0; booted=0
for i in $(seq 1 28); do
  sleep 60
  out=$(colab exec -s "$name" -f "$STATE/peek.py" --timeout 45 2>&1) || out="(exec 失联)"
  # 失联字样不带轮数：连续失联必须能累积成停滞（评审：带 $i 则永不停滞）
  echo "$out" | grep -q "FATAL" && { echo "$out" | redact; die "引导失败（见上）"; }
  echo "$out" | grep -q "BOOT_READY" && { booted=1; break; }
  sig=$(echo "$out" | md5); [ "$sig" = "$last" ] && stall=$((stall+1)) || stall=0; last="$sig"
  [ $stall -ge 12 ] && die "引导停滞 12min"
  note "boot 第 ${i}min：$(echo "$out" | tail -1 | redact | cut -c1-90)"
done
[ "$booted" = "1" ] || die "28min 未见 BOOT_READY"

note "BOOT_READY ✅ → 切 ssh 数据面"
$SSH "echo SSH_OK" >/dev/null 2>&1 || die "ssh 不通（tailscale status 查 colab-a100）"

note "ssh 起驱动 ${DRIVER}…"
$SSH "nohup bash /content/$DRIVER > /content/drive.log 2>&1 & echo DRIVER_KICKED" \
  || die "驱动点火失败"
FINAL_STATE="驱动运行中"

note "逐分钟盯驱动（活动信号=日志字节+产物清单；硬顶 ${HARD_CAP_MIN}min）…"
seen=0; last=""; stall=0; RUN_FAIL=""
while true; do
  sleep 60
  now=$(date +%s)
  [ $(( (now - T0) / 60 )) -ge $HARD_CAP_MIN ] && { RUN_FAIL="总时长 ${HARD_CAP_MIN}min 硬顶"; break; }
  # 活动签名必须含"会动的东西"：格内 vLLM 只写自己的 cell log，drive.log 在
  # CELL_START→RESULT 之间静止（评审 Critical：只看 tail 必误杀健康长格）
  out=$($SSH "tail -6 /content/drive.log 2>/dev/null; ls -l /content/bench_out/ 2>/dev/null | tail -4; wc -c /content/bench_out/*.log 2>/dev/null | tail -2" 2>&1) || { note "ssh 失联，下一轮重试"; continue; }
  echo "$out" | grep -q "FATAL" && { RUN_FAIL="驱动 FATAL"; break; }
  cnt=$($SSH "ls /content/bench_out/*.json 2>/dev/null | wc -l" 2>/dev/null | tr -d ' ')
  case "$cnt" in (*[!0-9]*|"") cnt=$seen;; esac
  if [ "$cnt" -gt "$seen" ]; then
    if scp -q $SSH_OPTS "root@colab-a100:/content/bench_out/*" "$OUTDIR/" 2>/dev/null; then
      seen=$cnt; note "已取回 ${cnt} 格产物 → ${OUTDIR}"
    fi
  fi
  echo "$out" | grep -q "BENCH_ALL_DONE" && { note "全部格完成"; break; }
  sig=$(echo "$out" | md5); [ "$sig" = "$last" ] && stall=$((stall+1)) || stall=0; last="$sig"
  [ $stall -ge 15 ] && { RUN_FAIL="驱动停滞 15min"; break; }
  note "第 $(( (now - T0) / 60 ))min：$(echo "$out" | grep -E '^\[' | tail -1 | cut -c1-90)"
done

FINAL_STATE="${RUN_FAIL:-完成}"
njson=$(ls "$OUTDIR"/*.json 2>/dev/null | wc -l | tr -d ' ')
note "结果 JSON 已在手 ${njson} 格（终态取证由 cleanup 统一做）"
[ -n "$RUN_FAIL" ] && die "${RUN_FAIL}（已取证可用产物 ${njson} 格）"
note "完成；trap 将停会话结账。"
