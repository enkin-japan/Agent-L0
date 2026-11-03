#!/bin/zsh
# 拐点 Colab 复测一体化：起 Colab(--retain) → 等 READY → 冒烟 → 顺序跑 A 段各步（边跑边盯挂死）→ [有 B 段步：stop → 等租约释放
#   → 换 MODEL_B 起机 → 冒烟 → 跑 B 段步] → 无论结果都 stop。
# 付费算力铁则（CLAUDE.md「Long tasks」）：消费动作与租约链在同一个后台脚本里，全程不依赖主会话被唤醒；
# 密钥只经 `set -a; . .env.local` 注入子进程，不打印。付费算力入口，主会话亲自改（CLAUDE.md「Never delegate」）。
# 桩自测：benchmarks/context_knee/run_colab_selftest.sh（不碰 Colab；改本脚本后必跑）。
#
# 用法（仓库根目录，主会话 Bash run_in_background）：
#   benchmarks/context_knee/run_colab.sh
# 环境变量：MODEL（A 段模型，默认 RedHatAI/Qwen3.8-27B-INT4；deploy 缺省 --speculative off，D21）、
#   MODEL_B（B 段模型，默认 google/gemma-4-31B-it-qat-w4a16-ct；gemma-* 步在 B 段跑：A 段步全部跑完后 stop → 换模型 start
#       → 冒烟 → 跑 B 段步；同一租约链，整批一次 DONE）、
#   OUT（结果目录，默认 benchmarks/data/context-knee-<YYYYMMDD>/；同目录重跑：时间线日志追加、arms_status 重写、
#       已有产物的拐点臂自动 --resume 续跑，trace 步不支持续跑（半截目录挪到 <步>.partial-<stamp> 后重跑），
#       冒烟目录带时间戳不复用；目录里已有 DONE（完成过的批次）则拒绝起机，
#       要续跑须显式 FORCE_OUT=1——防止默认 OUT 撞上当天已收官的档案目录白起付费机）、
#   ARMS（逗号分隔的步名，缺省为下表「第二批」九步；给了别的顺序也按 A 段先 B 段后分段，段内保持给定顺序）、
#   TRACE_REPS（trace 步每题重复数，默认 3）、READY_MINUTES（起机到 READY 的等待上限，默认 40）、
#   RESTARTS_MAX（服务端挂死时 stop+start 重起的次数上限，整批共用，默认 2；换模型的那次 stop+start 不计）、
#   HUNG_RUNS（连环 timeout 判挂死的 run 数，默认 3）、MON_INTERVAL（盯挂死的轮询秒，默认 60）、
#   SMOKE（1=每段起机后先各跑一个 run 冒烟，缺省 1；冒烟失败则跳过本段同模式全部步，不白烧）。
#   测试接缝（只为桩自测，缺省即生产路径）：DEPLOY、PY、KNEE、TRACE、ENV_FILE、POLL（等 READY 轮询秒，默认 30）。
# 步（口径见 benchmarks/context_knee/README.md「编排器模式」：各臂固定同一放置只切开关，不跨放置比）：
#   第二批缺省九步（11-02 拍板，方案 §5「第二批」）——A 段（MODEL）：
#   orch-turn-none    编排器 turn 放置 无管理（guard off / step off）        prod 前缀     0/32K/49K/57K
#   orch-turn-w1      编排器 turn 放置 +W1（guard on / step off）           prod 前缀     同上
#   orch-turn-w1w2    编排器 turn 放置 +W1+W2（guard on / step on）         prod 前缀     同上
#   orch-turn-catalog 编排器 turn 放置 +W1+W2 catalog 前缀对照（D39c）       catalog 前缀  同上
#   trace-new-900     summary_trace 成对续跑：新模板 / task_state on / 900 tok（现行生产）     --drop-task on
#   trace-legacy-640  同上：旧模板 / task_state off / 640 tok（W4 前的生产）
#   trace-new-640     同上：新模板 / task_state on / 640 tok（把模板与预算拆开看）
#   trace-d45-digest  D45 验收：新模板 / 900 tok / 工具 digest 200 字 / turn-1 迭代 3（只变 digest，对照 trace-new-900）
#   trace-d45-both    同上，digest 200 + turn-1 迭代 6（nudge 阈值 3 之后仍有迭代可建 plan）
#   trace-d45-plan    同上，digest 0 + turn-1 迭代 6（只变 task_state 一侧）
#   B 段（MODEL_B）：
#   gemma-direct-mock    直调 mock 前缀（W3 条目口径）                                   0/16K/32K/49K
#   gemma-direct-catalog 直调 catalog 前缀（gemma4 家族生产口径 D29）                     0/32K/49K
#   仍可用 ARMS 点名的第一批臂（A 段）：direct-mock、direct-prod、direct-catalog、direct-routed、orch-history-ref。
#   57,344 > ceiling 53,248（65,536 − 12,288）：guard on 的臂在该档走 W1 实机 ceiling 路径。
# 产物：$OUT/run_colab.log（时间线，追加）、$OUT/<臂>/（transcripts.jsonl + summary.json；trace 步为 transcripts.jsonl + meta.json）、
#   $OUT/<步>.log（harness 输出）、
#   $OUT/smoke-{orch,direct}-<stamp>-<段 A/B>/（每段只冒本段要跑的模式）、$OUT/arms_status.txt（每步一行：<步> ok[ errors=N]|fail|skipped）、
#   $OUT/DONE（单行，停机之后才写：BATCH_RC=<失败臂数> SKIPPED=<跳过臂数>[ ABORTED=hung] / FAIL_START / FAIL_READY / INTERRUPTED；
#   停机失败时改写为 STOP_FAILED。看护以 DONE 判完成即可确认 VM 已停或已记停机失败）。
# 退出码：0 全臂 ok；1 有臂 fail 或 skipped；2 cd 仓库失败；3 起机失败或租约被占拒起（DONE 都是 FAIL_START，看日志区分）；
#   4 超时未 READY；5 停机失败（VM 可能仍在计费：--retain 下不会自动到期，须人工 stop）；64 参数非法（未起机）；
#   129/130/143 收到 HUP/INT/TERM（先 TERM 掉 harness 子进程，再停机，再写 DONE）。
# 挂死判据：步（含冒烟）在后台跑，每 MON_INTERVAL 秒看 transcripts **本次启动后新追加的**末 HUNG_RUNS 行是否全是 error=timeout
#   （trace 步的行形如 error="TimeoutError: …"，同判）
#   （并发 1 的服务端一条生成挂死会连环超时，10-31 实测；只看新行——被 TERM 取消的 run 不写行，--resume 续跑要等第一个 run 跑完
#   才有新行，旧的 timeout 尾巴不能再算一次，二审 M1）→ TERM 掉 harness（它取消当前 run、写 summary 退出）→ stop+start 新 VM
#   → 同臂 --resume 续跑（trace 步无 --resume：半截目录挪成 <步>.partial-<stamp>-rN 后从头重跑；run_trace 无 TERM 处理，被杀即
#   无 meta.json）；整批最多 RESTARTS_MAX 次，用完即记该步 fail、余步 skipped、停机退出（挂死的 VM 上不再跑任何步）。
#   最坏发现时间 ≈ HUNG_RUNS × (run-timeout + 排空 ≤60 s) + MON_INTERVAL（缺省约 34 分钟），不是整臂跑完。
#   已知盲区：末 HUNG_RUNS 个 run 恰好都是正当超时（拐点区大档）也会判挂死并重起一次。stop 后立刻 start（重起、换模型都是）
#   会撞 compute_ctl 的「旧 VM 仍分配 / 旧节点仍可见」拒起 → 链脚本停机后先等租约 state 回到 stopped（最多 SWITCH_WAIT_POLLS×POLL，
#   默认 10 min），起机失败再试一次，仍失败才 FAIL_START/FAIL_READY 停机（安全，但 B 段不一定起得来；余步记 skipped）。
# 起机命令发出后，任何路径（起机失败、臂崩溃、HUP/INT/TERM、超时）都停机一次再写 DONE；EXIT trap 兜底再停一次（stopped 只在
#   stop 成功后置 1，停机被打断会重试，二审 M2）；停机失败记 STOP_FAILED / rc 5。
#   zsh 的 trap 在前台子进程退出后才执行：信号最多延后 MON_INTERVAL 秒（盯挂死的 sleep）或一次 status 轮询；进程组整体收到
#   TERM 时 harness 已先拿到一次，链脚本等 3 s 让它自己收尾，仍在才补 TERM。
set -u
ROOT=${0:A:h:h:h}
cd "$ROOT" || exit 2
MODEL=${MODEL:-RedHatAI/Qwen3.8-27B-INT4}
MODEL_B=${MODEL_B:-google/gemma-4-31B-it-qat-w4a16-ct}
TRACE_REPS=${TRACE_REPS:-3}
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
TRACE=${TRACE:-benchmarks/summary_trace/run_trace.py}
ENV_FILE=${ENV_FILE:-./.env.local}
[[ "$TRACE_REPS" == <1-> ]]     || { echo "TRACE_REPS 须为 ≥1 的整数（现 $TRACE_REPS）" >&2; exit 64; }
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
ARM_ARGS[orch-turn-catalog]="--orchestrator --fill-placement turn --guard on --step-compaction on --prefix catalog --levels $ORCH_LV"
# trace-* 步：summary_trace 成对续跑（run_trace.py），--drop-task on = 任务完成后被弃、只能靠摘要的目标场景（W4 残留项）；
#   前缀用 run_trace 缺省 --prefix mock（summary_trace/README 模板口径：量摘要模板，不量工具披露）
ARM_ARGS[trace-new-900]="--template new --task-state on --summary-max-tokens 900"
ARM_ARGS[trace-legacy-640]="--template legacy --task-state off --summary-max-tokens 640"
ARM_ARGS[trace-new-640]="--template new --task-state on --summary-max-tokens 640"
# D45 验收三步（11-03）：digest / turn-1 迭代上限两因素各单独一步 + 合并一步；对照组是上一批的 trace-new-900（digest 0 / 迭代 3）
ARM_ARGS[trace-d45-digest]="--template new --task-state on --summary-max-tokens 900 --tool-digest-chars 200 --turn1-iterations 3"
ARM_ARGS[trace-d45-both]="--template new --task-state on --summary-max-tokens 900 --tool-digest-chars 200 --turn1-iterations 6"
ARM_ARGS[trace-d45-plan]="--template new --task-state on --summary-max-tokens 900 --tool-digest-chars 0 --turn1-iterations 6"
# gemma-* 步：B 段（MODEL_B）
ARM_ARGS[gemma-direct-mock]="--levels $DIRECT_LV"
ARM_ARGS[gemma-direct-catalog]="--prefix catalog --levels 0,32768,49152"
DEFAULT_ORDER=(orch-turn-none orch-turn-w1 orch-turn-w1w2 orch-turn-catalog trace-new-900 trace-legacy-640 trace-new-640 gemma-direct-mock gemma-direct-catalog)
if [ -n "${ARMS:-}" ]; then ORDER=(${(s:,:)ARMS}); else ORDER=($DEFAULT_ORDER); fi
for a in $ORDER; do [ -n "${ARM_ARGS[$a]:-}" ] || { echo "未知步：$a" >&2; exit 64; }; done
# 步的段 / 驾驭器 / 模式（按名字前缀）：A 段 = MODEL，B 段 = MODEL_B；trace-* 走 run_trace.py（编排器模式，无 --resume）
step_phase(){ case $1 in gemma-*) print B;; *) print A;; esac; }
step_kind(){  case $1 in trace-*) print trace;; *) print knee;; esac; }
step_mode(){  case $1 in orch-*|trace-*) print orch;; *) print direct;; esac; }   # 冒烟门：orch 冒烟管编排器模式的步
# 分段排序：A 段步先、B 段步后，段内保持给定顺序（B 段只起一次机）
ORDER_A=(); ORDER_B=()
for a in $ORDER; do if [ "$(step_phase $a)" = B ]; then ORDER_B+=($a); else ORDER_A+=($a); fi; done
ORDER=($ORDER_A $ORDER_B)

# 当前步的命令行（--out 与步参数由 run_arm 追加）；--model 取当前段模型 $MODEL
CMD=()
step_cmd(){
  case $(step_kind $1) in
    trace) CMD=("$PY" "$TRACE" --model "$MODEL" --backend remote --tasks hard --reps "$TRACE_REPS" --run-timeout 600 --drop-task on);;
    *)     CMD=("$PY" "$KNEE"  --model "$MODEL" --backend remote --tasks hard --run-timeout 600);;
  esac
}

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

# 跑一步 / 冒烟（后台 + 盯挂死）：$1 产物目录 $2 日志 其余 = 额外参数；命令行取全局 CMD（调用前 step_cmd 置好）；
# 密钥只在子进程（exec 后 $! 即 python 进程）；返回 harness rc；挂死被 TERM 时置全局 hung=1。
# 挂死只看本次启动后新追加的行（启动时记下已有行数）。
run_arm(){
  local outdir=$1 logf=$2 rc base=0; shift 2
  hung=0
  [ -f "$outdir/transcripts.jsonl" ] && base=$(wc -l < "$outdir/transcripts.jsonl" | tr -d ' ')
  ( set -a; . "$ENV_FILE"; set +a
    INFER_REMOTE_HOST="$host" exec "${CMD[@]}" --out "$outdir" "$@" ) >> "$logf" 2>&1 &
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
def is_timeout(e):   # 拐点 harness 写 error="timeout"；run_trace 写 error="TimeoutError: run_turn 超时（600s）"；
    return "timeout" in str(e or "").lower()   # 远端后端读超时（ReadTimeout…）同判（一审 Minor 7）
sys.exit(0 if len(tail) == n and all(is_timeout(r.get("error")) for r in tail) else 1)
PY
}
# trace 步完成判定：meta.json 存在（run_trace 收尾才写）；stdout 给 " errors=N"（transcripts 里带 error 键的行数，N>0 时）
trace_complete(){
  python3 - "$1" <<'PY'
import json, sys, pathlib
out = pathlib.Path(sys.argv[1])
if not (out / "meta.json").is_file(): sys.exit(1)
try:
    rows = [json.loads(l) for l in open(out / "transcripts.jsonl") if l.strip()]
except (FileNotFoundError, ValueError):
    sys.exit(1)
n = sum(1 for r in rows if r.get("error"))
if n: print(f" errors={n}", end="")
sys.exit(0 if rows and n < len(rows) else 1)   # 全部成对都报错 = 没量到东西，记 fail（一审 Minor 6）
PY
}
# $1 之后的下一步名（没有则空）：重起失败时当前步记 fail、其后全记 skipped
next_step_after(){
  local a hit=0
  for a in $ORDER; do
    [ $hit -eq 1 ] && { print -r -- "$a"; return 0; }
    [ "$a" = "$1" ] && hit=1
  done
  return 0
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

# 余下的步全记 skipped（$1 起，含 $1）：起机失败退出前写，arms_status 仍是每步一行（一审 Minor 4）
skip_from(){
  local a seen=0
  for a in $ORDER; do
    [ "$a" = "$1" ] && seen=1
    [ $seen -eq 1 ] && { arm_status $a skipped; skipped=$((skipped + 1)); }
  done
}
# 起机（含等 READY）失败的统一出口：$1 = start_and_wait 的 rc（3/4），$2 = 当前步（含它起全记 skipped）
start_fail_exit(){
  [ -n "${2:-}" ] && skip_from "$2"
  [ "$1" -eq 3 ] && fail_exit FAIL_START 3
  fail_exit FAIL_READY 4
}
# 换模型 / 重起共用：停机 → 等租约真正 stopped（compute_ctl 对「旧 VM 仍分配 / 旧节点仍可见」拒起，
# 一审 Major 3）→ 起机；起机失败（3）或未 READY（4）再试一次（4 先防御性停机）。返回 start_and_wait 的 rc。
SWITCH_WAIT_POLLS=${SWITCH_WAIT_POLLS:-20}
restart_vm(){
  local rc i st
  stop_colab || { finish STOP_FAILED; final_rc=5; exit 5; }
  for i in $(seq 1 $SWITCH_WAIT_POLLS); do
    st=$(lease_state)
    case $st in none|stopped) break;; esac
    log "wait lease release: state=$st ($i/$SWITCH_WAIT_POLLS)"
    sleep $POLL
  done
  start_and_wait; rc=$?
  if [ $rc -ne 0 ]; then
    log "start after stop FAILED rc=$rc（旧节点未释放？）：${POLL}s 后再试一次"
    [ $rc -eq 4 ] && { stop_colab || { finish STOP_FAILED; final_rc=5; exit 5; }; }
    sleep $POLL
    start_and_wait; rc=$?
  fi
  return $rc
}

# 只跑 B 段步时直接起 B 模型，不白起一次 A（一审 Major 1）
failed=0; skipped=0; aborted=""; phase=A
[ ${#ORDER_A} -eq 0 ] && { phase=B; MODEL=$MODEL_B; }
start_and_wait; rc=$?
if [ $rc -ne 0 ]; then
  # 起机失败 / 未 READY：防御性停机一次（started=1 已置）再写 DONE；停机失败记 STOP_FAILED / 5
  start_fail_exit $rc "${ORDER[1]}"
fi

# 本段冒烟：只冒本段还要跑的模式（orch / direct），拐点 harness 一个 run；失败则置 0，本段同模式的步全跳过
orch_ok=1; direct_ok=1
run_smokes(){   # $1 段名 其余 = 本段步名
  local ph=$1 need_orch=0 need_direct=0 s tag; shift
  for s in "$@"; do [ "$(step_mode $s)" = orch ] && need_orch=1 || need_direct=1; done
  orch_ok=1; direct_ok=1
  [ "$SMOKE" = 1 ] || return 0
  tag="$STAMP-$ph"
  step_cmd orch-turn-none
  if [ $need_orch -eq 1 ]; then
    log "smoke orch [$ph $MODEL] (prod, turn, L0, 1 题 1 rep) → smoke-orch-$tag"
    run_arm "$OUT/smoke-orch-$tag" "$OUT/smoke-orch-$tag.log" --orchestrator --fill-placement turn --prefix prod --levels 0 --reps 1 --limit 1
    if smoke_ok "$OUT/smoke-orch-$tag"; then log "smoke orch OK"; else orch_ok=0; log "smoke orch FAILED → 跳过本段全部编排器模式的步（见 smoke-orch-$tag.log）"; fi
  fi
  if [ $need_direct -eq 1 ]; then
    log "smoke direct [$ph $MODEL] (prod, L0, 1 题 1 rep) → smoke-direct-$tag"
    run_arm "$OUT/smoke-direct-$tag" "$OUT/smoke-direct-$tag.log" --prefix prod --levels 0 --reps 1 --limit 1
    if smoke_ok "$OUT/smoke-direct-$tag"; then log "smoke direct OK"; else direct_ok=0; log "smoke direct FAILED → 跳过本段全部直调步（见 smoke-direct-$tag.log）"; fi
  fi
}
if [ $phase = A ]; then run_smokes A $ORDER_A; else run_smokes B $ORDER_B; fi

for arm in $ORDER; do
  if [ -n "$aborted" ]; then arm_status $arm skipped; skipped=$((skipped + 1)); continue; fi
  # 段切换（A → B）：停机 → 等租约释放 → 换模型起机（失败再试一次）→ 本段冒烟；换模型的 stop+start 不占重起预算
  if [ "$(step_phase $arm)" != "$phase" ]; then
    phase=$(step_phase $arm); MODEL=$MODEL_B
    log "PHASE $phase: 换模型 $MODEL（stop → start）"
    restart_vm; rc2=$?
    [ $rc2 -ne 0 ] && start_fail_exit $rc2 "$arm"
    run_smokes $phase $ORDER_B
  fi
  extra=(${=ARM_ARGS[$arm]})
  case $(step_mode $arm) in
    orch)   [ $orch_ok -eq 1 ]   || { arm_status $arm skipped; skipped=$((skipped + 1)); continue; };;
    direct) [ $direct_ok -eq 1 ] || { arm_status $arm skipped; skipped=$((skipped + 1)); continue; };;
  esac
  kind=$(step_kind $arm)
  step_cmd $arm
  resume=()
  if [ -f "$OUT/$arm/transcripts.jsonl" ]; then
    if [ "$kind" = trace ]; then
      # 已完成的 trace 步（meta.json 在）不重跑（一审 Major 2：FORCE_OUT 续跑别把完成的步当半截再付一次）
      if errs=$(trace_complete "$OUT/$arm"); then
        log "ARM $arm: 已完成（meta.json 在），不重跑"; arm_status $arm "ok$errs"; continue
      fi
      mv "$OUT/$arm" "$OUT/$arm.partial-$STAMP" || log "⚠️ mv $arm 失败（rc=$?），run_trace 会拒跑该目录"
      log "ARM $arm: trace 不支持续跑，半截产物挪到 $arm.partial-$STAMP 后重跑"
    else
      resume=(--resume); log "ARM $arm: 产物已存在，--resume 续跑"
    fi
  fi
  log "ARM $arm [$phase $MODEL] start: ${extra[*]} ${resume[*]}"
  while :; do
    run_arm "$OUT/$arm" "$OUT/$arm.log" "${extra[@]}" "${resume[@]}"; rc=$?
    log "ARM $arm harness rc=$rc hung=$hung"
    if [ $hung -eq 1 ]; then
      if [ $restarts -lt $RESTARTS_MAX ]; then
        restarts=$((restarts + 1))
        log "ARM $arm: stop+start 重起（$restarts/$RESTARTS_MAX）后续跑"
        restart_vm; rc2=$?
        if [ $rc2 -ne 0 ]; then
          arm_status $arm fail; failed=$((failed + 1))
          start_fail_exit $rc2 "$(next_step_after $arm)"
        fi
        if [ "$kind" = trace ]; then
          if [ -d "$OUT/$arm" ]; then
            mv "$OUT/$arm" "$OUT/$arm.partial-$STAMP-r$restarts" || log "⚠️ mv $arm 失败（rc=$?）"
          fi
          log "ARM $arm: trace 半截产物挪到 $arm.partial-$STAMP-r$restarts，从头重跑"
        else
          resume=(--resume)
        fi
        continue
      fi
      log "ARM $arm: 连环 timeout 且重起次数已用完（$RESTARTS_MAX）：记 fail，余步 skipped，停机退出"
      arm_status $arm fail; failed=$((failed + 1)); aborted=hung
      break
    fi
    errs=""
    if [ "$kind" = trace ]; then
      if [ $rc -eq 0 ] && errs=$(trace_complete "$OUT/$arm"); then arm_status $arm "ok$errs"; else arm_status $arm fail; failed=$((failed + 1)); fi
    else
      if [ $rc -eq 0 ] && errs=$(arm_complete "$OUT/$arm"); then arm_status $arm "ok$errs"; else arm_status $arm fail; failed=$((failed + 1)); fi
    fi
    break
  done
done

stop_colab || { finish STOP_FAILED; final_rc=5; exit 5; }
rc=0; [ $((failed + skipped)) -gt 0 ] && rc=1
finish "BATCH_RC=$failed SKIPPED=$skipped${aborted:+ ABORTED=$aborted}"
final_rc=$rc
exit $rc
