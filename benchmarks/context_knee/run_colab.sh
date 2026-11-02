#!/bin/zsh
# 拐点 Colab 复测一体化：起 Colab(--retain) → 等 READY → 冒烟 → 顺序跑各臂（边跑边盯挂死）→ 无论结果都 stop。
# 付费算力铁则（CLAUDE.md「Long tasks」）：消费动作与租约链在同一个后台脚本里，全程不依赖主会话被唤醒；
# 密钥只经 `set -a; . .env.local` 注入子进程，不打印。付费算力入口，主会话亲自改（CLAUDE.md「Never delegate」）。
# 桩自测：benchmarks/context_knee/run_colab_selftest.sh（不碰 Colab；改本脚本后必跑）。
#
# 用法（仓库根目录，主会话 Bash run_in_background）：
#   benchmarks/context_knee/run_colab.sh
# 环境变量：MODEL（默认 RedHatAI/Qwen3.8-27B-INT4；deploy 缺省 --speculative off，D21）、
#   OUT（结果目录，默认 benchmarks/data/context-knee-<YYYYMMDD>/；同目录重跑：时间线日志追加、arms_status 重写、
#       已有产物的臂自动 --resume 续跑，冒烟目录带时间戳不复用；目录里已有 DONE（完成过的批次）则拒绝起机，
#       要续跑须显式 FORCE_OUT=1——防止默认 OUT 撞上当天已收官的档案目录白起付费机）、
#   ARMS（逗号分隔的臂名，缺省全部，按下表顺序）、READY_MINUTES（起机到 READY 的等待上限，默认 40）、
#   RESTARTS_MAX（服务端挂死时 stop+start 重起的次数上限，整批共用，默认 2）、
#   HUNG_RUNS（连环 timeout 判挂死的 run 数，默认 3）、MON_INTERVAL（盯挂死的轮询秒，默认 60）、
#   SMOKE（1=先各跑一个 run 冒烟，缺省 1；冒烟失败则跳过同模式全部臂，不白烧）。
#   测试接缝（只为桩自测，缺省即生产路径）：DEPLOY、PY、KNEE、ENV_FILE、POLL（等 READY 轮询秒，默认 30）。
# 臂（口径见 benchmarks/context_knee/README.md「编排器模式」：各臂固定同一放置只切开关，不跨放置比）：
#   orch-turn-none   编排器 turn 放置 无管理（guard off / step off）        prod 前缀  0/32K/49K/57K
#   orch-turn-w1     编排器 turn 放置 +W1（guard on / step off）           prod 前缀  同上
#   orch-turn-w1w2   编排器 turn 放置 +W1+W2（guard on / step on）         prod 前缀  同上
#   direct-mock      直调 mock 前缀（W3 条目口径 last_k K=2；对 10-31 qwen-current=all 口径）  0/16K/32K/49K
#   direct-prod      直调 prod 前缀（W-2 三档复测之一）                              0/16K/32K/49K
#   orch-history-ref 编排器 history 放置 现状参照（guard on / step on / turn-compaction on） prod 前缀 0/32K/49K/57K
#   direct-catalog   直调 catalog 前缀（W-2）                                        0/32K/49K
#   direct-routed    直调 routed 前缀（W-2）                                         0/32K/49K
#   57,344 > ceiling 53,248（65,536 − 12,288）：guard on 的臂在该档走 W1 实机 ceiling 路径。
# 产物：$OUT/run_colab.log（时间线，追加）、$OUT/<臂>/（transcripts.jsonl + summary.json）、$OUT/<臂>.log（harness 输出）、
#   $OUT/smoke-{orch,direct}-<stamp>/、$OUT/arms_status.txt（每臂一行：<臂> ok[ errors=N]|fail|skipped）、
#   $OUT/DONE（单行，停机之后才写：BATCH_RC=<失败臂数> SKIPPED=<跳过臂数>[ ABORTED=hung] / FAIL_START / FAIL_READY / INTERRUPTED；
#   停机失败时改写为 STOP_FAILED。看护以 DONE 判完成即可确认 VM 已停或已记停机失败）。
# 退出码：0 全臂 ok；1 有臂 fail 或 skipped；2 cd 仓库失败；3 起机失败或租约被占拒起（DONE 都是 FAIL_START，看日志区分）；
#   4 超时未 READY；5 停机失败（VM 可能仍在计费：--retain 下不会自动到期，须人工 stop）；64 参数非法（未起机）；
#   129/130/143 收到 HUP/INT/TERM（先 TERM 掉 harness 子进程，再停机，再写 DONE）。
# 挂死判据：臂（含冒烟）在后台跑，每 MON_INTERVAL 秒看 transcripts **本次启动后新追加的**末 HUNG_RUNS 行是否全是 error=timeout
#   （并发 1 的服务端一条生成挂死会连环超时，10-31 实测；只看新行——被 TERM 取消的 run 不写行，--resume 续跑要等第一个 run 跑完
#   才有新行，旧的 timeout 尾巴不能再算一次，二审 M1）→ TERM 掉 harness（它取消当前 run、写 summary 退出）→ stop+start 新 VM
#   → 同臂 --resume 续跑；整批最多 RESTARTS_MAX 次，用完即记该臂 fail、余臂 skipped、停机退出（挂死的 VM 上不再跑任何臂）。
#   最坏发现时间 ≈ HUNG_RUNS × (run-timeout + 排空 ≤60 s) + MON_INTERVAL（缺省约 34 分钟），不是整臂跑完。
#   已知盲区：末 HUNG_RUNS 个 run 恰好都是正当超时（拐点区大档）也会判挂死并重起一次；重起时旧节点若仍可见，start 会被
#   compute_ctl 拒绝 → FAIL_READY 停机（安全，但重起不一定起得来）。
# 起机命令发出后，任何路径（起机失败、臂崩溃、HUP/INT/TERM、超时）都停机一次再写 DONE；EXIT trap 兜底再停一次（stopped 只在
#   stop 成功后置 1，停机被打断会重试，二审 M2）；停机失败记 STOP_FAILED / rc 5。
#   zsh 的 trap 在前台子进程退出后才执行：信号最多延后 MON_INTERVAL 秒（盯挂死的 sleep）或一次 status 轮询；进程组整体收到
#   TERM 时 harness 已先拿到一次，链脚本等 3 s 让它自己收尾，仍在才补 TERM。
set -u
ROOT=${0:A:h:h:h}
cd "$ROOT" || exit 2
MODEL=${MODEL:-RedHatAI/Qwen3.8-27B-INT4}
OUT=${OUT:-benchmarks/data/context-knee-$(date +%Y%m%d)}
READY_MINUTES=${READY_MINUTES:-40}
RESTARTS_MAX=${RESTARTS_MAX:-2}
HUNG_RUNS=${HUNG_RUNS:-3}
MON_INTERVAL=${MON_INTERVAL:-60}
POLL=${POLL:-30}
SMOKE=${SMOKE:-1}
DEPLOY=${DEPLOY:-./colab/deploy.sh}
PY=${PY:-backend/.venv/bin/python}
KNEE=${KNEE:-benchmarks/context_knee/run_knee.py}
ENV_FILE=${ENV_FILE:-./.env.local}
[[ "$READY_MINUTES" == <15-> ]] || { echo "READY_MINUTES 须为 ≥15 的整数（现 $READY_MINUTES）" >&2; exit 64; }
[[ "$RESTARTS_MAX" == <0-> ]]   || { echo "RESTARTS_MAX 须为 ≥0 的整数（现 $RESTARTS_MAX）" >&2; exit 64; }
[[ "$HUNG_RUNS" == <2-> ]]      || { echo "HUNG_RUNS 须为 ≥2 的整数（现 $HUNG_RUNS）" >&2; exit 64; }
[[ "$MON_INTERVAL" == <1-> ]]   || { echo "MON_INTERVAL 须为 ≥1 的整数（现 $MON_INTERVAL）" >&2; exit 64; }
[[ "$POLL" == <1-> ]]           || { echo "POLL 须为 ≥1 的整数（现 $POLL）" >&2; exit 64; }
[ -f "$ENV_FILE" ] || { echo "ENV_FILE 不存在：$ENV_FILE" >&2; exit 64; }
if [ -f "$OUT/DONE" ] && [ "${FORCE_OUT:-0}" != 1 ]; then
  echo "OUT=$OUT 里已有 DONE（$(cat "$OUT/DONE")）：这是收官过的批次目录；换 OUT，或确实要续跑则 FORCE_OUT=1" >&2; exit 64
fi
mkdir -p "$OUT"
LOG=$OUT/run_colab.log
STAMP=$(date +%Y%m%d-%H%M%S)
touch "$LOG"; rm -f "$OUT/DONE"
: > "$OUT/arms_status.txt"
log(){ print -r -- "$(date '+%F %T') $*" >> "$LOG"; }
finish(){ print -r -- "$1" > "$OUT/DONE"; log "DONE $1"; }
arm_status(){ print -r -- "$1 $2" >> "$OUT/arms_status.txt"; log "arm $1: $2"; }

ORCH_LV=0,32768,49152,57344
DIRECT_LV=0,16384,32768,49152
typeset -A ARM_ARGS
ARM_ARGS[orch-turn-none]="--orchestrator --fill-placement turn --guard off --step-compaction off --prefix prod --levels $ORCH_LV"
ARM_ARGS[orch-turn-w1]="--orchestrator --fill-placement turn --guard on --step-compaction off --prefix prod --levels $ORCH_LV"
ARM_ARGS[orch-turn-w1w2]="--orchestrator --fill-placement turn --guard on --step-compaction on --prefix prod --levels $ORCH_LV"
ARM_ARGS[direct-mock]="--levels $DIRECT_LV"
ARM_ARGS[direct-prod]="--prefix prod --levels $DIRECT_LV"
ARM_ARGS[orch-history-ref]="--orchestrator --fill-placement history --guard on --step-compaction on --turn-compaction on --prefix prod --levels $ORCH_LV"
ARM_ARGS[direct-catalog]="--prefix catalog --levels 0,32768,49152"
ARM_ARGS[direct-routed]="--prefix routed --levels 0,32768,49152"
DEFAULT_ORDER=(orch-turn-none orch-turn-w1 orch-turn-w1w2 direct-mock direct-prod orch-history-ref direct-catalog direct-routed)
if [ -n "${ARMS:-}" ]; then ORDER=(${(s:,:)ARMS}); else ORDER=($DEFAULT_ORDER); fi
for a in $ORDER; do [ -n "${ARM_ARGS[$a]:-}" ] || { echo "未知臂：$a" >&2; exit 64; }; done

RUN=("$PY" "$KNEE" --model "$MODEL" --backend remote --tasks hard --run-timeout 600)

started=0; stopped=0; final_rc=""; host=""; restarts=0; hpid=""; hung=0
stop_colab(){
  local rc
  [ $stopped -eq 1 ] && return 0
  log "STOP colab"
  "$DEPLOY" stop >> "$LOG" 2>&1; rc=$?
  log "stop rc=$rc"
  [ $rc -eq 0 ] && stopped=1        # 只在成功后置 1：停机被信号打断或失败，下次（含 EXIT trap）会重试
  return $rc
}
# （已起机则）停机 → 写 DONE → 退出：$1 DONE 内容，$2 退出码；停机失败改记 STOP_FAILED / 5。
# started 门不可去：起机前（租约检查期间）收到信号也走这里，此时绝不能 stop——那会停掉用户已有的 VM。
fail_exit(){
  if [ $started -eq 1 ] && ! stop_colab; then
    log "STOP FAILED: VM 可能仍在计费（--retain 不自动到期），需人工 $DEPLOY status / stop"
    finish STOP_FAILED; final_rc=5; exit 5
  fi
  finish "$1"; final_rc=$2; exit $2
}
on_exit(){
  [ $started -eq 1 ] || return 0
  if ! stop_colab; then
    log "STOP FAILED: VM 可能仍在计费（--retain 不自动到期），需人工 $DEPLOY status / stop"
    finish STOP_FAILED
    exit 5
  fi
  [ -n "$final_rc" ] && exit $final_rc
}
trap on_exit EXIT
# 等子进程退出，最多 $2 秒；退出返回 0，仍在返回 1
wait_child(){
  local pid=$1 limit=$2 i=0
  while kill -0 $pid 2>/dev/null; do
    [ $i -ge $limit ] && return 1
    sleep 1; i=$((i + 1))
  done
  return 0
}
kill_harness(){
  [ -n "$hpid" ] || return 0
  kill -0 $hpid 2>/dev/null || return 0
  wait_child $hpid 3 && return 0     # 进程组整体收到信号时 harness 已在自行收尾；再补 TERM 会被它当第二次信号立即硬退
  kill -TERM $hpid 2>/dev/null
  wait_child $hpid 120 || { log "harness 未在 120 s 内退出，KILL"; kill -KILL $hpid 2>/dev/null; wait_child $hpid 10; }
}
on_signal(){
  trap '' HUP INT TERM               # 收尾期间再来的信号不重入；EXIT trap 兜底仍在
  log "interrupted (SIG$1)"
  kill_harness
  fail_exit INTERRUPTED $2
}
trap 'on_signal HUP 129' HUP
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM

lease_state(){
  "$DEPLOY" status 2>>"$LOG" | python3 -c '
import sys, json, re
s = sys.stdin.read(); m = re.search(r"^\{", s, re.M)
d, _ = json.JSONDecoder().raw_decode(s[m.start():])
print(d.get("state") or "none")' 2>>"$LOG" || echo error
}

# 起机 + 等 READY；成功置全局 host，返回 0；失败返回 3（start 命令失败）/ 4（未 READY）。
# 起机命令发出前就置 started=1：deploy start 预约租约并派出 worker 后即返回，这期间收到信号也必须停机。
start_and_wait(){
  local start_rc deadline st phase state
  stopped=0; host=""
  started=1
  log "START deploy $MODEL --retain"
  "$DEPLOY" start --retain --model "$MODEL" >> "$LOG" 2>&1; start_rc=$?
  if [ $start_rc -ne 0 ]; then log "deploy start FAILED rc=$start_rc"; return 3; fi
  deadline=$(( $(date +%s) + READY_MINUTES * 60 ))
  while [ $(date +%s) -lt $deadline ]; do
    st=$("$DEPLOY" status 2>/dev/null)
    phase=$(print -r -- "$st" | grep -m1 -E '^\s*"phase":' | sed -E 's/.*: *"([^"]*)".*/\1/')
    state=$(print -r -- "$st" | grep -m1 -E '^\s*"state":' | sed -E 's/.*: *"([^"]*)".*/\1/')
    log "wait READY: state=$state phase=$phase"
    if [ "$phase" = "ready" ]; then
      host=$(print -r -- "$st" | grep -m1 -E '^\s*"host":' | sed -E 's/.*: *"([^"]*)".*/\1/')
      [ -n "$host" ] && break
    fi
    case "$state" in stopped|stopping|failed|release-unconfirmed) log "deploy state=$state before READY"; return 4;; esac
    sleep $POLL
  done
  [ -n "$host" ] || { log "no READY host within ${READY_MINUTES}min"; return 4; }
  log "READY host=$host"
  return 0
}

# 跑一臂 / 冒烟（后台 + 盯挂死）：$1 产物目录 $2 日志 其余 = 额外参数；密钥只在子进程（exec 后 $! 即 python 进程）；
# 返回 harness rc；挂死被 TERM 时置全局 hung=1。挂死只看本次启动后新追加的行（启动时记下已有行数）。
run_arm(){
  local outdir=$1 logf=$2 rc base=0; shift 2
  hung=0
  [ -f "$outdir/transcripts.jsonl" ] && base=$(wc -l < "$outdir/transcripts.jsonl" | tr -d ' ')
  ( set -a; . "$ENV_FILE"; set +a
    INFER_REMOTE_HOST="$host" exec "${RUN[@]}" --out "$outdir" "$@" ) >> "$logf" 2>&1 &
  hpid=$!
  while kill -0 $hpid 2>/dev/null; do
    sleep $MON_INTERVAL
    if hung_tail "$outdir" "$base"; then
      hung=1
      log "末 $HUNG_RUNS run 连环 timeout，疑服务端挂死：TERM harness"
      kill_harness
      break
    fi
  done
  wait $hpid 2>/dev/null; rc=$?
  hpid=""
  return $rc
}
# 冒烟判定：transcripts 至少一行且有非 error 行
smoke_ok(){
  python3 - "$1/transcripts.jsonl" <<'PY'
import json, sys
try:
    rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
except FileNotFoundError:
    sys.exit(1)
sys.exit(0 if any(not r.get("error") for r in rows) else 1)
PY
}
# 挂死判定：第 $2 行之后新追加的末 HUNG_RUNS 行全是 error=timeout；文件缺失 / 正在写的半行（JSON 坏）都按「未挂死」
hung_tail(){
  python3 - "$1/transcripts.jsonl" "$HUNG_RUNS" "${2:-0}" <<'PY'
import json, sys
n, base = int(sys.argv[2]), int(sys.argv[3])
try:
    with open(sys.argv[1]) as f:
        lines = [l for l in f if l.strip()]
    rows = [json.loads(l) for l in lines[base:]]
except (FileNotFoundError, ValueError):
    sys.exit(1)
tail = rows[-n:]
sys.exit(0 if len(tail) == n and all(r.get("error") == "timeout" for r in tail) else 1)
PY
}
# 臂完成判定：summary.json 存在且 complete 为真；stdout 给 " errors=N"（N>0 时）
arm_complete(){
  python3 - "$1/summary.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except (FileNotFoundError, ValueError):
    sys.exit(1)
n = int(d.get("n_errors") or 0)
if n: print(f" errors={n}", end="")
sys.exit(0 if d.get("complete") is True else 1)
PY
}

# 起机前先看租约：已有未停租约（state 非 stopped）就拒绝起机、也绝不停机（防御性 stop 会停掉用户已有 VM）。
ls_state=$(lease_state)
case $ls_state in
  none|stopped) ;;
  *) log "refuse to start: existing lease state=$ls_state (not ours; no stop)"; finish FAIL_START; exit 3;;
esac

start_and_wait; rc=$?
if [ $rc -ne 0 ]; then
  # 起机失败 / 未 READY：防御性停机一次（started=1 已置）再写 DONE；停机失败记 STOP_FAILED / 5
  [ $rc -eq 3 ] && fail_exit FAIL_START 3
  fail_exit FAIL_READY 4
fi

orch_ok=1; direct_ok=1
if [ "$SMOKE" = 1 ]; then
  log "smoke orch (prod, turn, L0, 1 题 1 rep) → smoke-orch-$STAMP"
  run_arm "$OUT/smoke-orch-$STAMP" "$OUT/smoke-orch-$STAMP.log" --orchestrator --fill-placement turn --prefix prod --levels 0 --reps 1 --limit 1
  if smoke_ok "$OUT/smoke-orch-$STAMP"; then log "smoke orch OK"; else orch_ok=0; log "smoke orch FAILED → 跳过全部编排器臂（见 smoke-orch-$STAMP.log）"; fi
  log "smoke direct (prod, L0, 1 题 1 rep) → smoke-direct-$STAMP"
  run_arm "$OUT/smoke-direct-$STAMP" "$OUT/smoke-direct-$STAMP.log" --prefix prod --levels 0 --reps 1 --limit 1
  if smoke_ok "$OUT/smoke-direct-$STAMP"; then log "smoke direct OK"; else direct_ok=0; log "smoke direct FAILED → 跳过全部直调臂（见 smoke-direct-$STAMP.log）"; fi
fi

failed=0; skipped=0; aborted=""
for arm in $ORDER; do
  if [ -n "$aborted" ]; then arm_status $arm skipped; skipped=$((skipped + 1)); continue; fi
  extra=(${=ARM_ARGS[$arm]})
  case $arm in
    orch-*)   [ $orch_ok -eq 1 ]   || { arm_status $arm skipped; skipped=$((skipped + 1)); continue; };;
    direct-*) [ $direct_ok -eq 1 ] || { arm_status $arm skipped; skipped=$((skipped + 1)); continue; };;
  esac
  resume=()
  [ -f "$OUT/$arm/transcripts.jsonl" ] && { resume=(--resume); log "ARM $arm: 产物已存在，--resume 续跑"; }
  log "ARM $arm start: ${extra[*]} ${resume[*]}"
  while :; do
    run_arm "$OUT/$arm" "$OUT/$arm.log" "${extra[@]}" "${resume[@]}"; rc=$?
    log "ARM $arm harness rc=$rc hung=$hung"
    if [ $hung -eq 1 ]; then
      if [ $restarts -lt $RESTARTS_MAX ]; then
        restarts=$((restarts + 1))
        log "ARM $arm: stop+start 重起（$restarts/$RESTARTS_MAX）后 --resume"
        stop_colab || { finish STOP_FAILED; final_rc=5; exit 5; }
        start_and_wait; rc2=$?
        if [ $rc2 -ne 0 ]; then
          [ $rc2 -eq 3 ] && fail_exit FAIL_START 3
          fail_exit FAIL_READY 4
        fi
        resume=(--resume)
        continue
      fi
      log "ARM $arm: 连环 timeout 且重起次数已用完（$RESTARTS_MAX）：记 fail，余臂 skipped，停机退出"
      arm_status $arm fail; failed=$((failed + 1)); aborted=hung
      break
    fi
    errs=""
    if [ $rc -eq 0 ] && errs=$(arm_complete "$OUT/$arm"); then arm_status $arm "ok$errs"; else arm_status $arm fail; failed=$((failed + 1)); fi
    break
  done
done

stop_colab || { finish STOP_FAILED; final_rc=5; exit 5; }
rc=0; [ $((failed + skipped)) -gt 0 ] && rc=1
finish "BATCH_RC=$failed SKIPPED=$skipped${aborted:+ ABORTED=$aborted}"
final_rc=$rc
exit $rc
