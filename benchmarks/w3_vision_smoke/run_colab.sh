#!/bin/zsh
# W-3 验收一体化：起 Colab(31B QAT) → 等 READY → 跑图文+纯文本冒烟 → 无论结果都 stop。
# 付费算力铁则（CLAUDE.md「Long tasks」）：消费动作与租约链在同一个后台脚本里，
# 全程不依赖主会话被唤醒；密钥只经 `set -a; . .env.local` 注入子进程，不打印。
# 付费算力入口，主会话亲自改（CLAUDE.md「Never delegate」）。
#
# 用法（仓库根目录，主会话 Bash run_in_background）：
#   benchmarks/w3_vision_smoke/run_colab.sh
# 环境变量：MODEL（默认 google/gemma-4-31B-it-qat-w4a16-ct；改了别的模型则探针以 --any-model 跳过身份核对）、
#   MINUTES（租约，默认 40，最少 20：READY 最晚在到期前 10 分钟，否则放弃，给冒烟留足时间）、
#   OUT（结果目录，默认 benchmarks/data/w3-vision-smoke-<YYYYMMDD>/；同日重跑会覆盖时间线日志与 DONE）。
# 产物：$OUT/run_colab.log（时间线）、$OUT/smoke.log（探针输出）、$OUT/result.json + PNG（探针写）、
#   $OUT/DONE（最后一行 SMOKE_RC=<rc> / FAIL_START / FAIL_READY / STOP_FAILED）。
# 退出码 = 探针退出码（0 全过）；3 起机失败；4 超时未 READY；5 停机失败（VM 可能仍在计费，最长到租约到期）。
# 起机成功后，任何路径（探针失败/崩溃、INT/TERM/HUP、超时）都经 EXIT trap 停机一次。
set -u
ROOT=${0:A:h:h:h}
cd "$ROOT" || exit 2
DEFAULT_MODEL=google/gemma-4-31B-it-qat-w4a16-ct
MODEL=${MODEL:-$DEFAULT_MODEL}
MINUTES=${MINUTES:-40}
OUT=${OUT:-benchmarks/data/w3-vision-smoke-$(date +%Y%m%d)}
[[ "$MINUTES" == <20-> ]] || { echo "MINUTES 须为 ≥20 的整数（现 $MINUTES）" >&2; exit 64; }
mkdir -p "$OUT"
LOG=$OUT/run_colab.log
: > "$LOG"; rm -f "$OUT/DONE"
log(){ print -r -- "$(date '+%F %T') $*" >> "$LOG"; }
finish(){ print -r -- "$1" > "$OUT/DONE"; }

started=0; stopped=0; final_rc=""
stop_colab(){
  [ $stopped -eq 1 ] && return 0
  stopped=1
  log "STOP colab"
  ./colab/deploy.sh stop >> "$LOG" 2>&1; rc=$?
  log "stop rc=$rc"
  return $rc
}
on_exit(){
  [ $started -eq 1 ] || return 0
  if ! stop_colab; then
    log "STOP FAILED: VM 可能仍在计费（最长到租约到期），需人工 ./colab/deploy.sh status / stop"
    finish STOP_FAILED
    exit 5
  fi
  [ -n "$final_rc" ] && exit $final_rc
}
trap on_exit EXIT
trap 'log "interrupted (signal)"; exit 130' INT TERM HUP

log "START deploy $MODEL lease ${MINUTES}min"
./colab/deploy.sh start --model "$MODEL" --minutes "$MINUTES" >> "$LOG" 2>&1; start_rc=$?
if [ $start_rc -ne 0 ]; then
  log "deploy start FAILED rc=$start_rc; defensive stop"
  started=1; stop_colab || true; stopped=1; started=0
  finish FAIL_START; exit 3
fi
started=1

host=""
deadline=$(( $(date +%s) + (MINUTES - 10) * 60 ))
while [ $(date +%s) -lt $deadline ]; do
  st=$(./colab/deploy.sh status 2>/dev/null)
  phase=$(print -r -- "$st" | grep -m1 -E '^\s*"phase":' | sed -E 's/.*: *"([^"]*)".*/\1/')
  state=$(print -r -- "$st" | grep -m1 -E '^\s*"state":' | sed -E 's/.*: *"([^"]*)".*/\1/')
  if [ "$phase" = "ready" ]; then
    host=$(print -r -- "$st" | grep -m1 -E '^\s*"host":' | sed -E 's/.*: *"([^"]*)".*/\1/')
    [ -n "$host" ] && break
  fi
  case "$state" in stopped|stopping|failed|release-unconfirmed) log "deploy state=$state before READY"; break;; esac
  sleep 30
done

if [ -z "$host" ]; then
  log "no READY host within deadline"
  finish FAIL_READY; final_rc=4; exit 4
fi
log "READY host=$host; running smoke"
extra=(); [ "$MODEL" != "$DEFAULT_MODEL" ] && extra=(--any-model)
( set -a; . ./.env.local; set +a
  INFER_REMOTE_HOST="$host" backend/.venv/bin/python benchmarks/w3_vision_smoke/smoke.py --out "$OUT" "${extra[@]}" ) \
  > "$OUT/smoke.log" 2>&1
rc=$?
log "smoke rc=$rc"
stop_colab || { finish STOP_FAILED; final_rc=5; exit 5; }
finish "SMOKE_RC=$rc"
final_rc=$rc
exit $rc
