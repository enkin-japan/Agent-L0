#!/bin/zsh
# run_colab.sh 桩自测：用假 deploy / 假 harness 跑 14 个场景，不碰 Colab、不读密钥、不起任何服务。
# 覆盖一审（2026-11-02）指出的路径：挂死半小时内发现并 TERM 掉 harness（C1）、重起用完即停机跳余臂（M1）、
# `start_and_wait; rc=$?` 起机失败记 FAIL_START（M2）、全跳过不记全过（M3）、stop_colab 的 local rc（M4）、
# 信号先杀子进程再停机并写 INTERRUPTED（m1/m2）、同目录重跑自动 --resume（m3）、ok 需 complete 且记 errors（m4）；
# 二审：续跑期间旧 timeout 行不再判挂死（M1，桩在 resume 时延迟 3 s 才写行）、DONE 只在停机之后写、重起时起机失败仍停机、
# 等 READY 期间收信号、起机前收信号不得 stop（fail_exit 的 started 门）、OUT 已有 DONE 拒起。
# 用法：benchmarks/context_knee/run_colab_selftest.sh [场景名…]（缺省全部）；全过退出 0。
# 变异证明（守卫型）：改坏 run_colab.sh 的 started=1 / hung_tail 判据或其 base 跳过 / aborted=hung / `start_and_wait; rc=$?` /
#   kill_harness / arm_complete / skipped 计数 / stopped 只在成功后置 1 任一处，对应场景必须变红。
set -u
ROOT=${0:A:h:h:h}
CHAIN=$ROOT/benchmarks/context_knee/run_colab.sh
T=$(mktemp -d "${TMPDIR:-/tmp}/run_colab_selftest.XXXXXX")
trap 'rm -rf "$T"' EXIT
: > "$T/env"

cat > "$T/deploy_stub.sh" <<'EOF'
#!/bin/zsh
# 假 deploy：status 先打一行 usage 再打多行 JSON（模拟 colab CLI 缓冲顺序）；start 后第二次 status 变 ready。
set -u
S=$STUB_DIR; mkdir -p "$S"
print -r -- "$1" >> "$S/calls.log"
case $1 in
  status)
    [ -n "${STUB_STATUS_SLEEP:-}" ] && sleep "$STUB_STATUS_SLEEP"     # 拖慢 status，给「起机前收信号」场景留窗口
    echo "usage: colab [-h] {new,list,stop} ..."
    st=$(cat "$S/state" 2>/dev/null || echo "${STUB_INIT_STATE:-none}")
    if [ "$st" = booting ]; then
      polls=$(( $(cat "$S/polls" 2>/dev/null || echo 0) + 1 )); echo $polls > "$S/polls"
      [ $polls -ge ${STUB_READY_POLLS:-2} ] && { st=ready; echo ready > "$S/state"; }
    fi
    phase=loading-models; [ "$st" = ready ] && phase=ready
    printf '{\n  "state": "%s",\n  "phase": "%s",\n  "host": "stub-host"\n}\n' "$st" "$phase";;
  start)
    nstart=$(grep -c '^start$' "$S/calls.log")      # 本次已计入
    [ "${STUB_START_FAIL:-0}" = 1 ] && { echo "start failed (stub)"; exit 1; }
    [ -n "${STUB_START_FAIL_AFTER:-}" ] && [ $nstart -gt $STUB_START_FAIL_AFTER ] && { echo "start failed (stub, #$nstart)"; exit 1; }
    echo booting > "$S/state"; echo 0 > "$S/polls"; echo "started (stub) $*";;
  stop)
    [ "${STUB_STOP_FAIL:-0}" = 1 ] && { echo "stop failed (stub)"; exit 1; }
    echo stopped > "$S/state"; echo "stopped (stub)";;
  *) echo "stub: unknown $1" >&2; exit 2;;
esac
EOF
chmod +x "$T/deploy_stub.sh"

cat > "$T/knee_stub.py" <<'EOF'
"""假 harness：按产物目录名查 STUB_BEHAVIOR（JSON）决定行为；smoke-* 目录按 STUB_SMOKE。
ok / errors / fail / incomplete / hang / hang_then_ok / slow。像真 run_knee 一样：产物已存在且无 --resume 则拒跑。
hang / slow 的 sleep 秒数取 STUB_HANG_SLEEP（缺省 600）：变异证明里判据被改坏时不必等满。"""
import json, os, sys, time, pathlib
args = sys.argv[1:]
HANG_SLEEP = float(os.environ.get("STUB_HANG_SLEEP", "600"))
out = pathlib.Path(args[args.index("--out") + 1])
resume = "--resume" in args
name = out.name
beh = os.environ.get("STUB_SMOKE", "ok") if name.startswith("smoke-") else json.loads(os.environ.get("STUB_BEHAVIOR", "{}")).get(name, "ok")
out.mkdir(parents=True, exist_ok=True)
tr = out / "transcripts.jsonl"
if tr.exists() and not resume:
    print(f"❌ {tr} 已存在：换 --out，或 --resume"); sys.exit(1)
with (out / "stub.pids").open("a") as f: f.write(f"{os.getpid()}\n")   # 每次启动追加（resume 的健康进程不覆盖挂死进程的 pid）
def row(**kw):
    with tr.open("a") as f: f.write(json.dumps({"level": 0, "item_id": "x", "rep": 0, **kw}) + "\n")
def summary(n_errors=0, complete=True):
    (out / "summary.json").write_text(json.dumps({"complete": complete, "n_errors": n_errors}))
if beh == "hang_then_ok":
    # 真实时序：--resume 续跑要先跑完第一个 run（分钟级）才追加新行，期间 transcripts 末尾仍是旧的 timeout 行；
    # 链脚本只看启动后的新行，否则会把健康的续跑再判成挂死（二审 M1）。这里用 3 s 复现。
    if resume:
        time.sleep(3); beh = "ok"
    else:
        beh = "hang"
if beh == "ok":
    row(); row(); summary(); sys.exit(0)
if beh == "errors":
    row(); row(error="boom"); summary(n_errors=1); sys.exit(0)
if beh == "fail":
    sys.exit(2)
if beh == "incomplete":
    row(); summary(complete=False); sys.exit(0)
if beh == "hang":
    for _ in range(3): row(error="timeout")
    time.sleep(HANG_SLEEP); sys.exit(0)
if beh == "slow":
    row(); time.sleep(HANG_SLEEP); summary(); sys.exit(0)
print("stub: unknown behaviour", beh); sys.exit(3)
EOF

typeset -a ENVBASE
ENVBASE=(DEPLOY="$T/deploy_stub.sh" PY=python3 KNEE="$T/knee_stub.py" ENV_FILE="$T/env" MON_INTERVAL=1 POLL=1 READY_MINUTES=15 MODEL=stub/model STUB_HANG_SLEEP=30)
pass=0; fail=0
check(){ if eval "$2"; then pass=$((pass + 1)); else fail=$((fail + 1)); print -r -- "FAIL [$1] $3"; fi; }
run_chain(){ # $1 场景名，其余 = 额外 env；前台跑，rc 存 $T/$1.rc
  local n=$1; shift
  env "${ENVBASE[@]}" OUT="$T/$n" STUB_DIR="$T/$n.stub" "$@" zsh "$CHAIN" > "$T/$n.stdout" 2>&1; echo $? > "$T/$n.rc"
}
rc_of(){ cat "$T/$1.rc"; }
done_of(){ tail -n1 "$T/$1/DONE" 2>/dev/null; }
calls(){ local n; n=$(grep -c "^$2\$" "$T/$1.stub/calls.log" 2>/dev/null); print -r -- "${n:-0}"; }   # grep -c 无匹配退出 1 但已打印 0
status_of(){ cat "$T/$1/arms_status.txt" 2>/dev/null | tr '\n' ';'; }
pids_dead(){ # $1 场景 $2 臂：stub.pids 存在且其中每个 pid 都已退出
  local f="$T/$1/$2/stub.pids" p
  [ -s "$f" ] || return 1
  while read -r p; do kill -0 $p 2>/dev/null && return 1; done < "$f"
  return 0
}
stop_before_done(){ # 时间线里「stop rc=」须先于「DONE」：DONE 只在停机之后写
  local s d
  s=$(grep -n 'stop rc=' "$T/$1/run_colab.log" | head -n1 | cut -d: -f1); d=$(grep -n '^.* DONE ' "$T/$1/run_colab.log" | head -n1 | cut -d: -f1)
  [ -n "$s" ] && [ -n "$d" ] && [ "$s" -lt "$d" ]
}
start_bg(){ # $1 场景名，其余 = 额外 env；后台起链脚本，pid 存全局 cpid
  local n=$1; shift
  env "${ENVBASE[@]}" OUT="$T/$n" STUB_DIR="$T/$n.stub" "$@" zsh "$CHAIN" > "$T/$n.stdout" 2>&1 &
  cpid=$!
}
wait_log(){ # $1 场景 $2 正则：最多 30 s 等时间线出现该行
  local i; for i in {1..30}; do grep -qE "$2" "$T/$1/run_colab.log" 2>/dev/null && return 0; sleep 1; done; return 1
}

want=("$@"); [ ${#want} -eq 0 ] && want=(ok hang_recover hang_exhaust start_fail smoke_fail term stop_fail lease_busy arm_fail arm_errors restart_start_fail term_ready term_prestart done_guard)
has(){ [[ " ${want[*]} " == *" $1 "* ]]; }

if has ok; then
  run_chain ok ARMS=direct-mock,direct-prod
  check ok '[ "$(rc_of ok)" = 0 ]' "rc=$(rc_of ok) (want 0)"
  check ok '[ "$(done_of ok)" = "BATCH_RC=0 SKIPPED=0" ]' "DONE=$(done_of ok)"
  check ok '[ "$(calls ok stop)" = 1 ] && [ "$(calls ok start)" = 1 ]' "stop=$(calls ok stop) start=$(calls ok start)"
  check ok '[ "$(status_of ok)" = "direct-mock ok;direct-prod ok;" ]' "status=$(status_of ok)"
  check ok 'ls -d "$T"/ok/smoke-orch-* "$T"/ok/smoke-direct-* >/dev/null 2>&1' "冒烟目录带时间戳"
fi
if has hang_recover; then
  run_chain hang_recover ARMS=orch-turn-none,direct-mock STUB_BEHAVIOR='{"orch-turn-none":"hang_then_ok"}'
  check hang_recover '[ "$(rc_of hang_recover)" = 0 ]' "rc=$(rc_of hang_recover) (want 0)"
  check hang_recover '[ "$(done_of hang_recover)" = "BATCH_RC=0 SKIPPED=0" ]' "DONE=$(done_of hang_recover)"
  check hang_recover '[ "$(calls hang_recover stop)" = 2 ] && [ "$(calls hang_recover start)" = 2 ]' "stop=$(calls hang_recover stop) start=$(calls hang_recover start) (want 2/2)"
  check hang_recover 'grep -q "重起（1/2）后 --resume" "$T/hang_recover/run_colab.log"' "日志无重起记录"
  check hang_recover '[ "$(status_of hang_recover)" = "orch-turn-none ok;direct-mock ok;" ]' "status=$(status_of hang_recover)"
  check hang_recover 'pids_dead hang_recover orch-turn-none' "挂死的 harness 子进程仍活着（或 stub.pids 缺失）"
  check hang_recover '[ "$(grep -c "hung=1" "$T/hang_recover/run_colab.log")" = 1 ]' "挂死判定次数=$(grep -c 'hung=1' "$T/hang_recover/run_colab.log")（want 1：续跑期间旧 timeout 行不得再判挂死）"
fi
if has hang_exhaust; then
  t0=$(date +%s)
  run_chain hang_exhaust ARMS=orch-turn-none,direct-mock RESTARTS_MAX=0 STUB_BEHAVIOR='{"orch-turn-none":"hang"}'
  dt=$(( $(date +%s) - t0 ))
  check hang_exhaust '[ "$(rc_of hang_exhaust)" = 1 ]' "rc=$(rc_of hang_exhaust) (want 1)"
  check hang_exhaust '[ "$(done_of hang_exhaust)" = "BATCH_RC=1 SKIPPED=1 ABORTED=hung" ]' "DONE=$(done_of hang_exhaust)"
  check hang_exhaust '[ "$(calls hang_exhaust stop)" = 1 ]' "stop=$(calls hang_exhaust stop) (want 1)"
  check hang_exhaust '[ "$(status_of hang_exhaust)" = "orch-turn-none fail;direct-mock skipped;" ]' "status=$(status_of hang_exhaust)"
  check hang_exhaust '[ $dt -lt 15 ]' "挂死 ${dt}s 才发现（harness 本应 MON_INTERVAL 内被 TERM，不等它 sleep 完）"
fi
if has start_fail; then
  run_chain start_fail ARMS=direct-mock STUB_START_FAIL=1
  check start_fail '[ "$(rc_of start_fail)" = 3 ]' "rc=$(rc_of start_fail) (want 3)"
  check start_fail '[ "$(done_of start_fail)" = FAIL_START ]' "DONE=$(done_of start_fail) (want FAIL_START)"
  check start_fail '[ "$(calls start_fail stop)" = 1 ]' "起机失败后防御性 stop=$(calls start_fail stop) (want 1)"
  check start_fail 'stop_before_done start_fail' "DONE 写在停机之前"
fi
if has smoke_fail; then
  run_chain smoke_fail ARMS=orch-turn-none,direct-mock STUB_SMOKE=fail
  check smoke_fail '[ "$(rc_of smoke_fail)" = 1 ]' "rc=$(rc_of smoke_fail) (want 1：全跳过不算全过)"
  check smoke_fail '[ "$(done_of smoke_fail)" = "BATCH_RC=0 SKIPPED=2" ]' "DONE=$(done_of smoke_fail)"
  check smoke_fail '[ "$(calls smoke_fail stop)" = 1 ]' "stop=$(calls smoke_fail stop)"
  check smoke_fail '[ "$(status_of smoke_fail)" = "orch-turn-none skipped;direct-mock skipped;" ]' "status=$(status_of smoke_fail)"
fi
if has term; then
  start_bg term ARMS=direct-mock STUB_BEHAVIOR='{"direct-mock":"slow"}'
  wait_log term "ARM direct-mock start"; for i in {1..10}; do [ -s "$T/term/direct-mock/stub.pids" ] && break; sleep 1; done
  sleep 1; kill -TERM $cpid; wait $cpid; echo $? > "$T/term.rc"
  check term '[ "$(rc_of term)" = 143 ]' "rc=$(rc_of term) (want 143)"
  check term '[ "$(done_of term)" = INTERRUPTED ]' "DONE=$(done_of term) (want INTERRUPTED)"
  check term '[ "$(calls term stop)" = 1 ]' "stop=$(calls term stop) (want 1)"
  check term 'pids_dead term direct-mock' "TERM 后 harness 子进程仍活着（或 stub.pids 缺失）"
  check term 'stop_before_done term' "DONE 写在停机之前"
fi
if has restart_start_fail; then
  # 挂死 → stop → 重起时 start 失败 → 防御性停机 → DONE FAIL_START / rc 3
  run_chain restart_start_fail ARMS=orch-turn-none,direct-mock STUB_BEHAVIOR='{"orch-turn-none":"hang_then_ok"}' STUB_START_FAIL_AFTER=1
  check restart_start_fail '[ "$(rc_of restart_start_fail)" = 3 ]' "rc=$(rc_of restart_start_fail) (want 3)"
  check restart_start_fail '[ "$(done_of restart_start_fail)" = FAIL_START ]' "DONE=$(done_of restart_start_fail)"
  check restart_start_fail '[ "$(calls restart_start_fail start)" = 2 ] && [ "$(calls restart_start_fail stop)" = 2 ]' "start=$(calls restart_start_fail start) stop=$(calls restart_start_fail stop) (want 2/2：挂死停机一次 + 起机失败防御性停机一次)"
  check restart_start_fail 'pids_dead restart_start_fail orch-turn-none' "挂死的 harness 子进程仍活着"
fi
if has term_ready; then
  # 等 READY 期间收到 TERM：无 harness 子进程，直接停机、DONE INTERRUPTED、rc 143
  start_bg term_ready ARMS=direct-mock STUB_READY_POLLS=1000
  wait_log term_ready "wait READY:"; sleep 1; kill -TERM $cpid; wait $cpid; echo $? > "$T/term_ready.rc"
  check term_ready '[ "$(rc_of term_ready)" = 143 ]' "rc=$(rc_of term_ready) (want 143)"
  check term_ready '[ "$(done_of term_ready)" = INTERRUPTED ]' "DONE=$(done_of term_ready)"
  check term_ready '[ "$(calls term_ready stop)" = 1 ]' "stop=$(calls term_ready stop) (want 1)"
  check term_ready 'stop_before_done term_ready' "DONE 写在停机之前"
fi
if has term_prestart; then
  # 起机前（租约检查的 status 还在跑）收到 TERM：没起过机就绝不能 stop（会停掉用户已有 VM）；DONE INTERRUPTED、rc 143
  start_bg term_prestart ARMS=direct-mock STUB_STATUS_SLEEP=5
  sleep 2; kill -TERM $cpid; wait $cpid; echo $? > "$T/term_prestart.rc"
  check term_prestart '[ "$(rc_of term_prestart)" = 143 ]' "rc=$(rc_of term_prestart) (want 143)"
  check term_prestart '[ "$(done_of term_prestart)" = INTERRUPTED ]' "DONE=$(done_of term_prestart)"
  check term_prestart '[ "$(calls term_prestart start)" = 0 ] && [ "$(calls term_prestart stop)" = 0 ]' "起机前收信号却动了 deploy：start=$(calls term_prestart start) stop=$(calls term_prestart stop)（want 0/0）"
fi
if has done_guard; then
  # OUT 里已有 DONE（收官过的批次目录）→ 拒绝起机 rc 64，不碰 deploy
  mkdir -p "$T/done_guard"; echo "BATCH_RC=0 SKIPPED=0" > "$T/done_guard/DONE"
  run_chain done_guard ARMS=direct-mock
  check done_guard '[ "$(rc_of done_guard)" = 64 ]' "rc=$(rc_of done_guard) (want 64)"
  check done_guard '[ "$(calls done_guard start)" = 0 ] && [ "$(calls done_guard status)" = 0 ]' "已有 DONE 仍动了 deploy：start=$(calls done_guard start) status=$(calls done_guard status)"
  check done_guard '[ "$(done_of done_guard)" = "BATCH_RC=0 SKIPPED=0" ]' "旧 DONE 被改动：$(done_of done_guard)"
fi
if has stop_fail; then
  run_chain stop_fail ARMS=direct-mock STUB_STOP_FAIL=1
  check stop_fail '[ "$(rc_of stop_fail)" = 5 ]' "rc=$(rc_of stop_fail) (want 5)"
  check stop_fail '[ "$(done_of stop_fail)" = STOP_FAILED ]' "DONE=$(done_of stop_fail)"
fi
if has lease_busy; then
  run_chain lease_busy ARMS=direct-mock STUB_INIT_STATE=running
  check lease_busy '[ "$(rc_of lease_busy)" = 3 ]' "rc=$(rc_of lease_busy) (want 3)"
  check lease_busy '[ "$(done_of lease_busy)" = FAIL_START ]' "DONE=$(done_of lease_busy)"
  check lease_busy '[ "$(calls lease_busy stop)" = 0 ] && [ "$(calls lease_busy start)" = 0 ]' "别人的租约被动了：stop=$(calls lease_busy stop) start=$(calls lease_busy start)"
fi
if has arm_fail; then
  run_chain arm_fail ARMS=direct-mock,direct-prod,direct-catalog STUB_BEHAVIOR='{"direct-mock":"fail","direct-catalog":"incomplete"}'
  check arm_fail '[ "$(rc_of arm_fail)" = 1 ]' "rc=$(rc_of arm_fail) (want 1)"
  check arm_fail '[ "$(done_of arm_fail)" = "BATCH_RC=2 SKIPPED=0" ]' "DONE=$(done_of arm_fail)"
  check arm_fail '[ "$(status_of arm_fail)" = "direct-mock fail;direct-prod ok;direct-catalog fail;" ]' "status=$(status_of arm_fail)（rc 0 但 complete=false 须记 fail）"
  check arm_fail '[ "$(calls arm_fail stop)" = 1 ]' "stop=$(calls arm_fail stop)"
fi
if has arm_errors; then
  run_chain arm_errors ARMS=direct-mock STUB_BEHAVIOR='{"direct-mock":"errors"}'
  check arm_errors '[ "$(status_of arm_errors)" = "direct-mock ok errors=1;" ]' "status=$(status_of arm_errors)"
  # 同目录重跑：已有产物 → 自动 --resume（假 harness 无 --resume 会拒跑）
  env "${ENVBASE[@]}" OUT="$T/arm_errors" STUB_DIR="$T/arm_errors.stub2" ARMS=direct-mock STUB_BEHAVIOR='{"direct-mock":"ok"}' FORCE_OUT=1 zsh "$CHAIN" > "$T/arm_errors.stdout2" 2>&1; echo $? > "$T/arm_errors.rc"
  check arm_errors '[ "$(rc_of arm_errors)" = 0 ] && grep -q "产物已存在，--resume 续跑" "$T/arm_errors/run_colab.log"' "重跑未自动 --resume：rc=$(rc_of arm_errors)"
fi

print -r -- "run_colab selftest: pass=$pass fail=$fail (scenarios: ${want[*]})"
[ $fail -eq 0 ]
