#!/bin/bash
# bench 场 Mac 侧编排（v5 形态落库版，2026-08-15；首版被评审毙 8 条 Critical 后重写）。
#
# 形态铁律（08-15 四连崩验尸）：**exec 只管点火与 boot 观察**——exec 会话凭据
# ~45min 必失效而 VM 照常计费；BOOT_READY 后一律走 tailscale ssh/scp。
# 重量级件（62GB 权重）一律 boot 期预拉（boot.log 可见、有独立预算），
# 绝不塞进评测格的 timeout 里（评审 Critical：格超时→下一格重下→双杀中止）。
#
# 用法：./colab/legacy/bench_orchestrate.sh <VM侧驱动名> [输出目录]
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
OUTDIR="${2:-$HERE/../../benchmarks/data/bench-fetch-$(date +%y%m%d%H%M%S)}"
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 -o ServerAliveInterval=10 -o ServerAliveCountMax=3"
SSH="ssh -n $SSH_OPTS root@colab-a100"
NO_STOP="${NO_STOP:-0}"
HARD_CAP_MIN=60
die(){ echo "❌ $*"; exit 1; }
note(){ printf '\033[0;36m[bench]\033[0m %s\n' "$*"; }
# 脱敏（08-16 复审加固）：
# - hf_ 收紧到 {16,}：真实 HF token 是 hf_+~34 位，{4} 会误伤 hf_transfer 等
#   普通词（本轮落库数据已实际咬过日志文本）；
# - tskey- 同理收紧；
# - 追加赋值形态整行值脱敏（INFER_API_KEY= 等无特征前缀的钥匙靠这条盖）。
REDACT_SED=(-E
  -e 's/(x-access-token|github_pat_)[^@[:space:]]*/\1***REDACTED***/g'
  -e 's/(hf_)[A-Za-z0-9]{16}[^@[:space:]]*/\1***REDACTED***/g'
  -e 's/(tskey-)[A-Za-z0-9-]{8}[^@[:space:]]*/\1***REDACTED***/g'
  -e 's/([A-Za-z_]*(API_KEY|TOKEN|SECRET|AUTHKEY|PASSWORD)=)[^[:space:]]+/\1***REDACTED***/g'
  -e 's/("?[A-Za-z_]*(api_key|apikey|token|secret|authkey|password)"?[[:space:]]*[:=][[:space:]]*"?)[^",[:space:]}]+/\1***REDACTED***/g'
  -e 's/([Bb]earer[[:space:]]+)[A-Za-z0-9._-]+/\1***REDACTED***/g')
redact(){ sed "${REDACT_SED[@]}"; }
REDACT_WARN=0
redact_dir(){  # 目录内产物就地脱敏。评审 Critical 返工：任何一步失败/白名单外
  # 文件都必须**响亮**——2>/dev/null||true 会让"漏脱敏"与"脱敏成功"无法区分，
  # 而 OUTDIR 缺省落在 benchmarks/data/ 下、.gitignore 已豁免 *.log，一步
  # git add 就入库。失败不打断取证流程，但置 REDACT_WARN 供收尾提醒。
  local f
  while IFS= read -r -d '' f; do
    case "$f" in
      *.json|*.log|*.txt)
        sed -i '' "${REDACT_SED[@]}" "$f" \
          || { note "⚠️ 脱敏失败: $f（勿 git add，先手查）"; REDACT_WARN=1; } ;;
      *) note "⚠️ 白名单外产物未脱敏: $f（入库前手查）"; REDACT_WARN=1 ;;
    esac
  done < <(find "$1" -type f -print0 2>/dev/null)
}

[ -f "$ENVF" ] || die "缺 $ENVF"
[ -f "$HERE/$DRIVER" ] || die "缺驱动 $HERE/$DRIVER"
command -v colab >/dev/null || die "colab CLI 不在 PATH"
mkdir -p "$OUTDIR" "$STATE/logs"

name="bench-$(date +%y%m%d%H%M)"
ssh-keygen -R colab-a100 >/dev/null 2>&1 || true

FINAL_STATE="未点火"
VM_REQUESTED=0   # colab new 一经发出即置 1：CLI 返回非零也可能已把 VM 开起来
fetch_forensics(){  # 无论成败都先抢数据（评审：失败的场恰恰最需要取证）
  scp -q $SSH_OPTS "root@colab-a100:/content/bench_out/*" "$OUTDIR/" 2>/dev/null || true
  for f in boot.log drive.log; do
    $SSH "cat /content/$f 2>/dev/null" 2>/dev/null | redact \
      > "$OUTDIR/${f%.log}_console.txt" || true
  done
  $SSH "cat /content/logs/mem_alarm.log 2>/dev/null" 2>/dev/null \
    > "$OUTDIR/mem_alarm.txt" || true
  redact_dir "$OUTDIR"   # scp 直落的 bench_out 件同样过脱敏闸（08-16 复审）
  find "$OUTDIR" -type f -size 0 -delete 2>/dev/null || true
}
cleanup(){
  local rc=$?
  [ "$CLEANED" = "1" ] && exit $rc   # 重入护（INT→EXIT 双触发）
  CLEANED=1
  [ "$VM_REQUESTED" = "0" ] && { rm -f "$STATE/bench_session"; exit $rc; }  # 计费动作尚未发出，清痕即走
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
  # 未匹配、几秒后即稳定——差点误报未停）。
  # 判据按**会话名**而非全局 "No active sessions"（08-16 复审：本脚本设计成与
  # daily.sh 会话并存，别人的会话在跑不该让这里每次误报未停+退出码翻 1）。
  # 结构要点（评审 Critical 返工）：绝不能写成 `! 管道 | grep -q`——set -o
  # pipefail 下 colab ls 自身失败/sed 被 grep -q 早退 SIGPIPE 都会让管道非零、
  # 取反成"已停"，计费验证 fail-open。先落变量（ls 失败=本轮验不了，不算停），
  # 再用 case 通配匹配，不经任何管道。
  stopped=0
  for _ in 1 2 3; do
    sleep 5
    if ls_out=$(colab ls 2>&1); then
      clean=$(printf '%s' "$ls_out" | sed $'s/\x1b\\[[0-9;]*m//g') || clean=""
      case "${clean}${ls_out}" in   # 原文+去色双匹配：sed 万一失败也不误判"已停"
        *"$name"*) : ;;             # 仍在列——继续等
        *) stopped=1; break ;;
      esac
    else
      note "colab ls 失败，本轮无法验停（重试）"
    fi
  done
  if [ "$stopped" = "1" ]; then
    note "✅ 会话 ${name} 已确认停止"
  else
    note "⚠️⚠️ 会话可能未停！立即手查：colab ls / colab stop -s ${name} / daily.sh rescue"
    colab ls 2>&1 | tail -3
    rc=1
  fi
  [ "$REDACT_WARN" = "1" ] && note "⚠️ 本场有产物未过/未完成脱敏（见上方告警行），git add 前先手查 ${OUTDIR}"
  rm -f "$STATE/bench_session"
  exit $rc
}
# trap 先于计费动作装上（08-16 复审：先前 trap 装在 colab new 之后——CLI 已开机
# 却返回非零的形态会 die 出去，不停机、不留痕）。挂三个信号 + 重入护：
# INT/TERM 路径跑完 cleanup 后 exit 会再触发 EXIT trap，CLEANED 防止二次收尾。
CLEANED=0
trap cleanup EXIT INT TERM

cp "$CLI_CFG/sessions.json" "$STATE/sessions.json.bak" \
  || note "⚠️ sessions.json 备份失败——孤儿救援手段减一，继续（trap 已在岗）"
echo "$name" > "$STATE/bench_session"   # 不占 daily.sh 的 current_session 注册表

# 场参可用环境变量覆盖（08-16 复审：先前硬编码 31B——换驱动跑别的模型会先
# 白拉 62GB 错权重）。缺省仍是 31B 场；非 31B 驱动务必同时传四个 BENCH_* 与
# BENCH_PROMPT，否则引导期就会拉错模型。值加引号：VM 侧 set -a source，含
# 空格/元字符的值不引号会碎行甚至执行（评审建议）。
# 全部预检在 colab new **之前**——一个 typo 不该先开一台计费 VM（评审建议）。
cat > "$STATE/bench.env" <<EOF
BENCH_TARGET_REPO="${BENCH_TARGET_REPO:-google/gemma-4-31B-it}"
BENCH_TARGET_DIR="${BENCH_TARGET_DIR:-gemma-4-31B-it}"
BENCH_DRAFT_REPO="${BENCH_DRAFT_REPO:-google/gemma-4-31B-it-assistant}"
BENCH_DRAFT_DIR="${BENCH_DRAFT_DIR:-gemma-4-31B-it-assistant}"
BENCH_SKIP_PULL="${BENCH_SKIP_PULL:-}"
HF_HUB_ENABLE_HF_TRANSFER=1
EOF
PROMPT_SRC="${BENCH_PROMPT:-$HERE/../../benchmarks/data/mtp-31b-cross/_prompt.json}"
[ -f "$PROMPT_SRC" ] || die "缺 prompt 件 $PROMPT_SRC"

note "开 A100 会话 ${name}（~5.3u/h，总时长硬顶 ${HARD_CAP_MIN}min）…"
VM_REQUESTED=1
colab new --gpu A100 -s "$name" \
  || { FINAL_STATE="colab new 返回非零（可能已开机）"; die "colab new 失败（trap 在岗：按已开机形态走停机验证）"; }
T0=$(date +%s)
FINAL_STATE="启动中"

note "上传密钥/场参/引导/驱动/评测件…"
colab upload -s "$name" "$ENVF" /content/colab.env >/dev/null || die "colab.env 上传失败"
colab upload -s "$name" "$STATE/bench.env" /content/bench.env >/dev/null || die "bench.env 上传失败"
colab upload -s "$name" "$HERE/bench_bootstrap.sh" /content/bench_bootstrap.sh >/dev/null || die "引导上传失败"
colab upload -s "$name" "$HERE/$DRIVER" "/content/$DRIVER" >/dev/null || die "驱动上传失败"
colab upload -s "$name" "$HERE/../../benchmarks/vllm_cell.py" /content/vllm_cell.py >/dev/null || die "vllm_cell 上传失败"
colab upload -s "$name" "$PROMPT_SRC" /content/_prompt.json >/dev/null || die "prompt 上传失败"

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
  # 失联不 continue：固定串参与签名，连续失联照常累积成停滞（08-16 复审：
  # 先前 continue 跳过 stall 累加，VM 掉线会白烧到 60min 硬顶——与 boot 循环
  # 第 125 行注释声明的设计相反）
  if ! out=$($SSH "tail -6 /content/drive.log 2>/dev/null; ls -l /content/bench_out/ 2>/dev/null | tail -4; wc -c /content/bench_out/*.log 2>/dev/null | tail -2" 2>&1); then
    out="(ssh 失联)"
    note "ssh 失联（计入停滞累积，连续 15 轮判死）"
  fi
  echo "$out" | grep -q "FATAL" && { RUN_FAIL="驱动 FATAL"; break; }
  cnt=$($SSH "ls /content/bench_out/*.json 2>/dev/null | wc -l" 2>/dev/null | tr -d ' ')
  case "$cnt" in (*[!0-9]*|"") cnt=$seen;; esac
  if [ "$cnt" -gt "$seen" ]; then
    if scp -q $SSH_OPTS "root@colab-a100:/content/bench_out/*" "$OUTDIR/" 2>/dev/null; then
      redact_dir "$OUTDIR"
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
