#!/bin/bash
# 日用算力端一键起停（Mac 侧）。白天 start、晚上 stop，中间不用管。
#
#   ./colab/daily.sh start [l4|a100]   开 VM → 全链引导 → 26B 起服 → 挂哨兵/隧道保活
#   ./colab/daily.sh stop              停哨兵 → 停会话（孤儿自动还原自救）
#   ./colab/daily.sh status            会话/健康/时长/预估 unit
#   ./colab/daily.sh rescue            孤儿会话手动自救（还原 sessions.json 备份再 stop）
#
# 前置（一次性）：
#   mkdir -p ~/.config/agent-colab && chmod 700 ~/.config/agent-colab
#   从 Drive AgentColab/secrets/ 拷 colab.env 到该目录，chmod 600
# 崩溃防线：sessions.json 开机即备份（孤儿可救）／VM 侧监理循环崩了自动重启 ≤5 次
# ／GPU 保活 0.5G 防 25min 判零／Mac 侧隧道保活替身（CLI keep_alive 被 404 连坐的
# 长稳站教训）／哨兵健康轮询+macOS 通知告警。费率：L4 ≈1.54u/h、A100 40G ≈5.3u/h。
set -uo pipefail
export PATH="$HOME/Library/Python/3.13/bin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HOME/.config/agent-colab"
ENVF="$STATE/colab.env"
CLI_CFG="$HOME/.config/colab-cli"
die(){ echo "❌ $*"; exit 1; }
note(){ printf '\033[0;32m[daily]\033[0m %s\n' "$*"; }

cmd="${1:-}"; tier="${2:-l4}"
if [ "$cmd" = "start" ]; then
  case "$tier" in l4) GPU=L4;; a100) GPU=A100;; *) die "档位只认 l4|a100";; esac
fi
rate_of(){ [ "$1" = "a100" ] && echo 5.3 || echo 1.54; }

session_name(){ cat "$STATE/current_session" 2>/dev/null; }
session_start_ts(){ cat "$STATE/session_start_ts" 2>/dev/null; }
session_tier(){ cat "$STATE/current_tier" 2>/dev/null || echo "$tier"; }

start(){
  [ -f "$ENVF" ] || die "缺 $ENVF —— 从 Drive AgentColab/secrets/colab.env 拷来并 chmod 600"
  command -v colab >/dev/null || die "colab CLI 不在 PATH"
  if [ -n "$(session_name)" ] && curl -s -m 3 http://colab-a100:14161/health >/dev/null 2>&1; then
    note "会话 $(session_name) 已在服务中，无需重复 start"; return 0
  fi
  mkdir -p "$STATE/logs"
  local name="agentd-$(date +%m%d)"
  ssh-keygen -R colab-a100 >/dev/null 2>&1 || true   # 主机名跨天复用的旧钥（账本坑账）

  # 注意：变量后紧跟全角字符必须 ${} 包裹，macOS bash 3.2 会把多字节字符吞进变量名
  note "开 $GPU 会话 ${name}（费率 ~$(rate_of "$tier")u/h）…"
  colab new --gpu "$GPU" -s "$name" || die "colab new 失败（配额/档位不可用？）"
  cp "$CLI_CFG/sessions.json" "$STATE/sessions.json.bak" || die "sessions.json 备份失败"
  echo "$name" > "$STATE/current_session"
  echo "$tier" > "$STATE/current_tier"
  date +%s > "$STATE/session_start_ts"

  note "上传密钥与链条脚本…"
  colab upload -s "$name" "$ENVF" /content/colab.env >/dev/null || die "colab.env 上传失败"
  colab upload -s "$name" "$HERE/daily_serve_chain.sh" /content/daily_serve_chain.sh >/dev/null
  cat > "$STATE/kick.py" <<EOF
import subprocess
subprocess.run("mkdir -p /content/logs && SERVE_TIER=$tier SERVE_MAXLEN=${SERVE_MAXLEN:-} "
               "SERVE_KV_DTYPE=${SERVE_KV_DTYPE:-} nohup bash /content/daily_serve_chain.sh "
               "> /content/logs/chain.log 2>&1 & echo KICKED", shell=True)
EOF
  colab exec -s "$name" -f "$STATE/kick.py" --timeout 30 >/dev/null || die "点火失败"

  note "等服务就绪（引导+拉件+装载，L4 首次 ~12-15min / A100 ~16min；停滞 30min 判死）…"
  cat > "$STATE/peek.py" <<'EOF'
import subprocess
r = subprocess.run("ls /content/SERVE_READY 2>/dev/null && echo READY; "
                   "grep -m1 CONTRACT /content/logs/supervisor.log 2>/dev/null; "
                   "tail -3 /content/logs/chain.log 2>/dev/null",
                   shell=True, capture_output=True, text=True)
print(r.stdout[-800:])
EOF
  local last="" stall=0
  for i in $(seq 1 40); do
    sleep 60
    out=$(colab exec -s "$name" -f "$STATE/peek.py" --timeout 45 2>&1)
    echo "$out" | grep -q "FATAL" && { echo "$out" | tail -5; die "链条失败（见上）"; }
    if echo "$out" | grep -q "READY"; then
      echo "$out" | grep -q "CONTRACT_OK" && note "契约测试 ✅" || note "⚠️ 契约测试未过（看 /content/logs/contract.log）"
      break
    fi
    sig=$(echo "$out" | md5); [ "$sig" = "$last" ] && stall=$((stall+1)) || stall=0; last="$sig"
    [ $stall -ge 30 ] && die "链条停滞 30min（colab exec -s $name 手查 /content/logs/chain.log）"
    printf '.'
  done
  curl -s -m 5 http://colab-a100:14161/health >/dev/null || die "tailnet 拨号不通（tailscale status 查 colab-a100）"
  note "隧道保活 + 哨兵上岗…"
  nohup python3 "$HERE/tunnel_keepalive.py" "$name" > "$STATE/logs/tunnel.log" 2>&1 &
  echo $! > "$STATE/tunnel.pid"
  sentinel_start
  note "✅ 就绪：本机 ./devctl.sh start all 即可（.env.local 已拨 colab-a100）。晚上 ./colab/daily.sh stop"
}

# 哨兵独立子命令：评测/维护要主动停服的窗口先 sentinel stop 静默，完事 sentinel start
# 复岗，免得停服被当失联误报（VM 监理自愈与此无关，只是通知静默）。
sentinel_start(){
  sentinel_stop_quiet
  nohup bash -c '
    fails=0
    while true; do
      sleep 120
      if curl -s -m 5 http://colab-a100:14161/health >/dev/null 2>&1; then fails=0
      else
        fails=$((fails+1))
        if [ $fails -eq 5 ]; then
          echo "$(date +%T) 服务失联 10min（VM 监理若在会自愈；持续失联=会话已死）" >> "'"$STATE"'/logs/sentinel.log"
          osascript -e "display notification \"colab-a100 服务失联 10min\" with title \"Agent 算力端\"" 2>/dev/null
        fi
      fi
      # 20min 一次外部指标回拉：给平台一个「有人在用」的活跃信号（长稳站纪律）
      if [ $(( $(date +%s) / 60 % 20 )) -eq 0 ]; then
        curl -s -m 5 http://colab-a100:14161/metrics >/dev/null 2>&1
      fi
    done' > /dev/null 2>&1 &
  echo $! > "$STATE/sentinel.pid"
  note "哨兵上岗（pid $(cat "$STATE/sentinel.pid")）"
}
sentinel_stop_quiet(){
  [ -f "$STATE/sentinel.pid" ] && kill "$(cat "$STATE/sentinel.pid")" 2>/dev/null
  rm -f "$STATE/sentinel.pid"
}
sentinel_stop(){ sentinel_stop_quiet; note "哨兵已撤（评测窗口静默）"; }

stop(){
  for p in sentinel tunnel; do
    [ -f "$STATE/$p.pid" ] && { kill "$(cat "$STATE/$p.pid")" 2>/dev/null; rm -f "$STATE/$p.pid"; }
  done
  local name; name=$(session_name)
  [ -n "$name" ] || { note "无在册会话；colab ls 核对如下"; colab ls 2>&1 | tail -3; return 0; }
  # 释名（2026-08-10 复发根修）：ephemeral 节点停机后要等平台 GC 才腾出机器名，
  # 期间新 VM 只能领 colab-a100-1 后缀名——拨号侧（daily 探活/backend remote 闸/app
  # 设置）全部脱靶且机器名注册后不随 hostname 自改，只能进管理台手工正名。
  # 终止前主动 logout = 节点即刻删除、名字即刻回收，下台 VM 恒拿正名。best-effort：
  # VM 已死则 ssh 不通，退回平台 GC 慢路（-1 后缀风险回归，见账本 8-10 夜横幅）。
  local h
  for h in colab-a100 colab-a100-1; do
    ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "root@$h" \
        'tailscale --socket=/content/ts.sock logout' >/dev/null 2>&1 \
      && { note "tailnet 已释名（经 $h）"; break; }
  done
  note "停会话 ${name}…"
  local out
  if ! out=$(colab stop -s "$name" 2>&1); then   # 管道会吃退出码，必须分开取
    echo "$out" | tail -2
    note "直接 stop 失败 → 孤儿自救（还原 sessions.json 备份重试）"
    rescue; return
  fi
  echo "$out" | tail -1
  sleep 3   # 平台侧注销有延迟，停完即查会误报「仍有会话」
  colab ls 2>&1 | grep -q "No active sessions" && note "✅ 无残留会话" \
    || { note "⚠️ 仍有会话在册："; colab ls 2>&1 | tail -3; }
  # awk 一律 -v 传参：嵌套引号内插在 macOS bash 3.2 下会被劈碎（stop 首跑实案）
  local rate hours cost; rate=$(rate_of "$(session_tier)")
  hours=$(awk -v a="$(date +%s)" -v b="$(session_start_ts)" 'BEGIN{printf "%.1f",(a-b)/3600}')
  cost=$(awk -v h="$hours" -v r="$rate" 'BEGIN{printf "%.1f", h*r}')
  note "本次时长 ${hours}h ≈ ${cost}u"
  rm -f "$STATE/current_session" "$STATE/current_tier" "$STATE/session_start_ts"
}

rescue(){
  [ -f "$STATE/sessions.json.bak" ] || die "无 sessions.json 备份可还原"
  cp "$CLI_CFG/sessions.json" "$STATE/sessions.json.orphaned-$(date +%s)" 2>/dev/null
  cp "$STATE/sessions.json.bak" "$CLI_CFG/sessions.json"
  local name; name=$(session_name)
  note "已还原备份，重试 stop ${name}…"
  colab stop -s "$name" 2>&1 | tail -1
  colab ls 2>&1 | tail -3
  rm -f "$STATE/current_session" "$STATE/session_start_ts"
}

status(){
  local name; name=$(session_name)
  [ -n "$name" ] || { note "无在册会话"; colab ls 2>&1 | tail -3; return 0; }
  echo "会话: $name"
  curl -s -m 5 http://colab-a100:14161/health >/dev/null 2>&1 && echo "服务: ✅ colab-a100:14161" || echo "服务: ❌ 不可达"
  for p in sentinel tunnel; do
    [ -f "$STATE/$p.pid" ] && kill -0 "$(cat "$STATE/$p.pid")" 2>/dev/null \
      && echo "$p: 在岗" || echo "$p: 不在（stop 后属正常）"
  done
  local ts rate; ts=$(session_start_ts); rate=$(rate_of "$(session_tier)")
  [ -n "$ts" ] && awk -v n="$(date +%s)" -v t="$ts" -v r="$rate" -v g="$(session_tier)" \
    'BEGIN{h=(n-t)/3600; printf "时长: %.1fh ≈ %.1fu（%s）\n", h, h*r, g}'
}

case "$cmd" in
  start) start;;
  stop) stop;;
  status) status;;
  rescue) rescue;;
  sentinel) case "${2:-}" in start) sentinel_start;; stop) sentinel_stop;;
            *) die "用法: sentinel start|stop";; esac;;
  *) sed -n 2,10p "$0"; exit 1;;
esac
