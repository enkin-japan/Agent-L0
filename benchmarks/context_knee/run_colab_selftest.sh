#!/bin/zsh
# run_colab.sh 桩自测：用假 deploy / 假 harness 跑 32 个场景，不碰 Colab、不读密钥、不起任何服务。
# 覆盖一审（2026-11-02）指出的路径：挂死半小时内发现并 TERM 掉 harness（C1）、重起用完即停机跳余臂（M1）、
# `start_and_wait; rc=$?` 起机失败记 FAIL_START（M2）、全跳过不记全过（M3）、stop_colab 的 local rc（M4）、
# 信号先杀子进程再停机并写 INTERRUPTED（m1/m2）、同目录重跑自动 --resume（m3）、ok 需 complete 且记 errors（m4）；
# 二审：续跑期间旧 timeout 行不再判挂死（M1，桩在 resume 时延迟 3 s 才写行）、DONE 只在停机之后写、重起时起机失败仍停机、
# 等 READY 期间收信号、起机前收信号不得 stop（fail_exit 的 started 门）、OUT 已有 DONE 拒起。
# 用法：benchmarks/context_knee/run_colab_selftest.sh [场景名…]（缺省全部）；全过退出 0。
# 变异证明（守卫型）：改坏 run_colab.sh 的 started=1 / hung_tail 判据或其 base 跳过 / aborted=hung / `start_and_wait; rc=$?` /
#   kill_harness / arm_complete / skipped 计数 / stopped 只在成功后置 1 任一处，对应场景必须变红。
# 第二批（11-02 下午）：A/B 两段换模型（phases / order / b_only / switch_budget / smoke_reset / switch_start_fail）、trace 步
#   （trace_hang：TimeoutError 行同判挂死、半截目录挪走后从头重跑、不得 --resume；trace_done_rerun：完成的步不重跑）、
#   B 段冒烟门（phase_b_smoke_fail）。变异证明：换模型不改 MODEL / 不分段 / hung_tail 不认 TimeoutError / 不挪半截目录 /
#   B 段不冒烟 / 换模型不先停机 任一处，对应场景必须变红（11-02 verifier 6/6 红）。
# 11-04：首起 FAIL_READY（VM 随即 failed）自动重试一次（ready_retry：重试后再挂死一次仍能重起，证明不占 RESTARTS_MAX；
#   ready_retry_exhaust：只重试一次；ready_retry_off：READY_RETRY=0 同旧版；ready_retry_timeout：等 READY 超时不重试；ready_retry_stop_fail：停机失败不重试、仍写 skipped）、MAX_VM_HOURS
#   租约预算换机（lease_rollover：已用未超但「已用 + 同类步最长用时」超预算 → 同模型 stop+start 后再跑，之后挂死仍能重起；
#   lease_rollover_floor：刚起的租约至少跑一步，预算小于单步时每步换一次机、有界；lease_unlimited：MAX_VM_HOURS=0 不限，1.5 非法 64）。
#   变异证明：去掉重试 / 重试不先停机 / 预算判据去掉 est / 去掉 lease_steps 门 / 换租约改模型 / 不记 lease_steps 任一处，对应场景必须变红。
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
    print -r -- "$*" >> "$S/starts.log"          # 每次 start 的完整参数（换模型场景查 --model）
    # 前 N 次 start 命令成功但 VM 随即 failed（11-02 的瞬时 FAIL_READY）：status 报 state=failed，链脚本等 READY 时见之返回 4
    [ -n "${STUB_FAIL_READY_TIMES:-}" ] && [ $nstart -le $STUB_FAIL_READY_TIMES ] && { echo failed > "$S/state"; echo 0 > "$S/polls"; echo "started (stub, #$nstart → failed) $*"; exit 0; }
    echo booting > "$S/state"; echo 0 > "$S/polls"; echo "started (stub) $*";;
  stop)
    [ "${STUB_STOP_FAIL:-0}" = 1 ] && { echo "stop failed (stub)"; exit 1; }
    echo stopped > "$S/state"; echo "stopped (stub)";;
  *) echo "stub: unknown $1" >&2; exit 2;;
esac
EOF
chmod +x "$T/deploy_stub.sh"

cat > "$T/knee_stub.py" <<'EOF'
"""假 harness：按产物目录名查 STUB_BEHAVIOR（JSON）决定行为；smoke-* 目录按 STUB_SMOKE（B 段模型的冒烟按 STUB_SMOKE_B，
缺省同 STUB_SMOKE；B 段模型 = 环境 MODEL_B）。ok / errors / fail / incomplete / hang / hang_then_ok / slow / ok_slow。
像真 run_knee 一样：产物已存在且无 --resume 则拒跑。trace-* 目录按 run_trace 口径：收尾写 meta.json 而非 summary.json、
超时行 error="TimeoutError: …"、带 --resume 即拒跑；hang_then_ok 在 trace 下看旁边有没有 <步>.partial-* 目录（链脚本挪走的半截）。
hang / slow 的 sleep 秒数取 STUB_HANG_SLEEP（缺省 600）：变异证明里判据被改坏时不必等满。
ok_slow = 跑 STUB_OK_SLEEP 秒（缺省 3）后正常完成：给租约预算场景一个有用时的步。"""
import json, os, sys, time, pathlib
args = sys.argv[1:]
HANG_SLEEP = float(os.environ.get("STUB_HANG_SLEEP", "600"))
out = pathlib.Path(args[args.index("--out") + 1])
resume = "--resume" in args
model = args[args.index("--model") + 1] if "--model" in args else ""
name = out.name
trace = name.startswith("trace-")
if name.startswith("smoke-"):
    beh = os.environ.get("STUB_SMOKE", "ok")
    if model and model == os.environ.get("MODEL_B", ""):
        beh = os.environ.get("STUB_SMOKE_B", beh)
else:
    beh = json.loads(os.environ.get("STUB_BEHAVIOR", "{}")).get(name, "ok")
out.mkdir(parents=True, exist_ok=True)
tr = out / "transcripts.jsonl"
if trace and resume:
    print("❌ trace 不支持 --resume（成对评估不支持追加混批）"); sys.exit(1)
if tr.exists() and not resume:
    print(f"❌ {tr} 已存在：换 --out，或 --resume"); sys.exit(1)
with (out / "stub.pids").open("a") as f: f.write(f"{os.getpid()}\n")   # 每次启动追加（resume 的健康进程不覆盖挂死进程的 pid）
(out / "stub.args").write_text(" ".join(args) + "\n")                   # 本次启动的完整参数（查 --model / --reps 等）
TIMEOUT_ERR = "TimeoutError: run_turn 超时（600s）" if trace else "timeout"
def row(**kw):
    with tr.open("a") as f: f.write(json.dumps({"level": 0, "item_id": "x", "rep": 0, **kw}) + "\n")
def summary(n_errors=0, complete=True):
    if trace:
        (out / "meta.json").write_text(json.dumps({"model": model, "complete": complete})); return
    (out / "summary.json").write_text(json.dumps({"complete": complete, "n_errors": n_errors}))
if beh == "hang_then_ok":
    # 真实时序：--resume 续跑要先跑完第一个 run（分钟级）才追加新行，期间 transcripts 末尾仍是旧的 timeout 行；
    # 链脚本只看启动后的新行，否则会把健康的续跑再判成挂死（二审 M1）。这里用 3 s 复现。
    # trace 步没有 --resume：链脚本把半截目录挪成 <步>.partial-* 后从头重跑，桩据此判「第二次」。
    second = resume or (trace and any(out.parent.glob(name + ".partial-*")))
    if second:
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
    for _ in range(3): row(error=TIMEOUT_ERR)
    time.sleep(HANG_SLEEP); sys.exit(0)
if beh == "slow":
    row(); time.sleep(HANG_SLEEP); summary(); sys.exit(0)
if beh == "ok_slow":
    row(); time.sleep(float(os.environ.get("STUB_OK_SLEEP", "3"))); row(); summary(); sys.exit(0)
print("stub: unknown behaviour", beh); sys.exit(3)
EOF

typeset -a ENVBASE
ENVBASE=(DEPLOY="$T/deploy_stub.sh" PY=python3 KNEE="$T/knee_stub.py" TRACE="$T/knee_stub.py" ENV_FILE="$T/env" MON_INTERVAL=1 POLL=1 READY_MINUTES=15 MODEL=stub/model MODEL_B=stub/model-b STUB_HANG_SLEEP=30)
pass=0; fail=0
check(){ if eval "$2"; then pass=$((pass + 1)); else fail=$((fail + 1)); print -r -- "FAIL [$1] $3"; fi; }
run_chain(){ # $1 场景名，其余 = 额外 env；前台跑，rc 存 $T/$1.rc
  local n=$1; shift
  env "${ENVBASE[@]}" OUT="$T/$n" STUB_DIR="$T/$n.stub" "$@" zsh "$CHAIN" > "$T/$n.stdout" 2>&1; echo $? > "$T/$n.rc"
}
rc_of(){ cat "$T/$1.rc"; }
done_of(){ tail -n1 "$T/$1/DONE" 2>/dev/null; }
calls(){ local n; n=$(grep -c "^$2\$" "$T/$1.stub/calls.log" 2>/dev/null); print -r -- "${n:-0}"; }   # grep -c 无匹配退出 1 但已打印 0
start_models(){ sed -nE 's/.*--model ([^ ]+).*/\1/p' "$T/$1.stub/starts.log" 2>/dev/null | tr '\n' ';'; }   # 每次 start 的 --model，按序
args_model(){ sed -nE 's/.*--model ([^ ]+).*/\1/p' "$T/$1/$2/stub.args" 2>/dev/null; }                   # 某步 harness 收到的 --model
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

want=("$@"); [ ${#want} -eq 0 ] && want=(ok hang_recover hang_exhaust start_fail smoke_fail term stop_fail lease_busy arm_fail arm_errors restart_start_fail term_ready term_prestart done_guard phases trace_hang phase_b_smoke_fail order b_only switch_budget smoke_reset trace_done_rerun switch_start_fail d45_steps ready_retry ready_retry_exhaust ready_retry_off ready_retry_timeout ready_retry_stop_fail lease_rollover lease_rollover_floor lease_unlimited)
has(){ [[ " ${want[*]} " == *" $1 "* ]]; }

if has ok; then
  run_chain ok ARMS=direct-mock,direct-prod
  check ok '[ "$(rc_of ok)" = 0 ]' "rc=$(rc_of ok) (want 0)"
  check ok '[ "$(done_of ok)" = "BATCH_RC=0 SKIPPED=0" ]' "DONE=$(done_of ok)"
  check ok '[ "$(calls ok stop)" = 1 ] && [ "$(calls ok start)" = 1 ]' "stop=$(calls ok stop) start=$(calls ok start)"
  check ok '[ "$(status_of ok)" = "direct-mock ok;direct-prod ok;" ]' "status=$(status_of ok)"
  check ok 'ls -d "$T"/ok/smoke-direct-*-A >/dev/null 2>&1 && [ -z "$(print -l "$T"/ok/smoke-orch-*(N))" ]' "只冒本段要跑的模式：直调臂批次不该跑 orch 冒烟"
fi
if has phases; then
  # 第二批形态：A 段（拐点臂 + trace 步）→ stop → 换 MODEL_B 起机 → B 段冒烟 → gemma 步 → stop；一次 DONE
  run_chain phases ARMS=orch-turn-none,trace-new-900,gemma-direct-mock
  check phases '[ "$(rc_of phases)" = 0 ]' "rc=$(rc_of phases) (want 0)"
  check phases '[ "$(done_of phases)" = "BATCH_RC=0 SKIPPED=0" ]' "DONE=$(done_of phases)"
  check phases '[ "$(calls phases start)" = 2 ] && [ "$(calls phases stop)" = 2 ]' "换模型须 stop+start 各一次：start=$(calls phases start) stop=$(calls phases stop)"
  check phases '[ "$(start_models phases)" = "stub/model;stub/model-b;" ]' "起机模型顺序=$(start_models phases)"
  check phases '[ "$(args_model phases gemma-direct-mock)" = stub/model-b ] && [ "$(args_model phases orch-turn-none)" = stub/model ]' "步收到的 --model：gemma=$(args_model phases gemma-direct-mock) orch=$(args_model phases orch-turn-none)"
  check phases 'grep -q -- "--reps 3 --run-timeout 600 --drop-task on" "$T/phases/trace-new-900/stub.args" && grep -q -- "--template new --task-state on --summary-max-tokens 900" "$T/phases/trace-new-900/stub.args"' "trace 步参数：$(cat "$T/phases/trace-new-900/stub.args" 2>/dev/null)"
  check phases '[ -f "$T/phases/trace-new-900/meta.json" ] && [ "$(status_of phases)" = "orch-turn-none ok;trace-new-900 ok;gemma-direct-mock ok;" ]' "status=$(status_of phases)"
  check phases 'ls -d "$T"/phases/smoke-orch-*-A "$T"/phases/smoke-direct-*-B >/dev/null 2>&1 && [ -z "$(print -l "$T"/phases/smoke-direct-*-A(N))" ]' "A 段只冒 orch、B 段只冒 direct"
  check phases 'grep -c "stop rc=0" "$T/phases/run_colab.log" | grep -qx 2 && stop_before_done phases' "两次停机且 DONE 在停机后"
  check phases '[ "$(args_model phases trace-new-900)" = stub/model ]' "切换前最后一步 trace 须仍是 A 模型：$(args_model phases trace-new-900)"
fi
if has d45_steps; then
  # D45 验收三步 + D47 一步 + D47b 一步只是 trace 步的参数变体：A 段跑、kind=trace、参数逐字到位、不碰 B 段
  run_chain d45_steps ARMS=trace-d45-digest,trace-d45-both,trace-d45-plan,trace-d47-both,trace-d47b-both
  check d45_steps '[ "$(rc_of d45_steps)" = 0 ] && [ "$(done_of d45_steps)" = "BATCH_RC=0 SKIPPED=0" ]' "rc=$(rc_of d45_steps) DONE=$(done_of d45_steps)"
  check d45_steps '[ "$(status_of d45_steps)" = "trace-d45-digest ok;trace-d45-both ok;trace-d45-plan ok;trace-d47-both ok;trace-d47b-both ok;" ]' "status=$(status_of d45_steps)"
  check d45_steps 'grep -q -- "--tool-digest-chars 200 --turn1-iterations 3" "$T/d45_steps/trace-d45-digest/stub.args" && grep -q -- "--tool-digest-chars 200 --turn1-iterations 6" "$T/d45_steps/trace-d45-both/stub.args" && grep -q -- "--tool-digest-chars 0 --turn1-iterations 6" "$T/d45_steps/trace-d45-plan/stub.args" && grep -q -- "--tool-digest-chars 200 --turn1-iterations 6" "$T/d45_steps/trace-d47-both/stub.args" && grep -q -- "--tool-digest-chars 200 --turn1-iterations 6" "$T/d45_steps/trace-d47b-both/stub.args"' "D45/D47 步参数：both=$(cat "$T/d45_steps/trace-d45-both/stub.args" 2>/dev/null) d47=$(cat "$T/d45_steps/trace-d47-both/stub.args" 2>/dev/null) d47b=$(cat "$T/d45_steps/trace-d47b-both/stub.args" 2>/dev/null)"
  check d45_steps '[ -s "$T/d45_steps/trace-d47b-both/stub.args" ] && [ "$(sed -E "s# --out [^ ]+##" "$T/d45_steps/trace-d47b-both/stub.args")" = "$(sed -E "s# --out [^ ]+##" "$T/d45_steps/trace-d47-both/stub.args")" ]' "d47b 步参数（除 --out）须与 d47 步逐字相同：d47b=$(cat "$T/d45_steps/trace-d47b-both/stub.args" 2>/dev/null)"
  check d45_steps 'grep -q -- "--drop-task on" "$T/d45_steps/trace-d45-plan/stub.args" && [ "$(calls d45_steps start)" = 1 ] && [ "$(start_models d45_steps)" = "stub/model;" ]' "五步都在 A 段：start=$(calls d45_steps start) models=$(start_models d45_steps)"
fi
if has trace_hang; then
  # trace 步挂死：TimeoutError 行同判挂死 → 重起 → 半截目录挪成 .partial-* → 从头重跑（不得 --resume）
  run_chain trace_hang ARMS=trace-new-900,orch-turn-none STUB_BEHAVIOR='{"trace-new-900":"hang_then_ok"}'
  check trace_hang '[ "$(rc_of trace_hang)" = 0 ]' "rc=$(rc_of trace_hang) (want 0)"
  check trace_hang '[ "$(status_of trace_hang)" = "trace-new-900 ok;orch-turn-none ok;" ]' "status=$(status_of trace_hang)"
  check trace_hang 'ls -d "$T"/trace_hang/trace-new-900.partial-*-r1 >/dev/null 2>&1 && [ -f "$T/trace_hang/trace-new-900/meta.json" ]' "半截目录未挪走或重跑未完成"
  check trace_hang '! grep -q -- "--resume" "$T/trace_hang/trace-new-900/stub.args" && [ "$(calls trace_hang start)" = 2 ]' "trace 重跑不得 --resume；start=$(calls trace_hang start)"
  partial=$(find "$T/trace_hang" -maxdepth 1 -name 'trace-new-900.partial-*-r1' -exec basename {} \; | head -n1)
  check trace_hang '[ -n "$partial" ] && pids_dead trace_hang "$partial"' "挂死的 trace 进程未被杀（partial=$partial）"
fi
if has phase_b_smoke_fail; then
  # B 段冒烟失败：只跳过 B 段的步，A 段结果不受影响，仍正常停机
  run_chain phase_b_smoke_fail ARMS=direct-mock,gemma-direct-mock,gemma-direct-catalog STUB_SMOKE_B=fail
  check phase_b_smoke_fail '[ "$(rc_of phase_b_smoke_fail)" = 1 ]' "rc=$(rc_of phase_b_smoke_fail) (want 1)"
  check phase_b_smoke_fail '[ "$(done_of phase_b_smoke_fail)" = "BATCH_RC=0 SKIPPED=2" ]' "DONE=$(done_of phase_b_smoke_fail)"
  check phase_b_smoke_fail '[ "$(status_of phase_b_smoke_fail)" = "direct-mock ok;gemma-direct-mock skipped;gemma-direct-catalog skipped;" ]' "status=$(status_of phase_b_smoke_fail)"
  check phase_b_smoke_fail '[ "$(calls phase_b_smoke_fail stop)" = 2 ] && [ "$(calls phase_b_smoke_fail start)" = 2 ]' "stop=$(calls phase_b_smoke_fail stop) start=$(calls phase_b_smoke_fail start)"
fi
if has order; then
  # ARMS 里 B 段步写在前面也要排到 A 段之后（B 段只起一次机）
  run_chain order ARMS=gemma-direct-mock,direct-mock,gemma-direct-catalog
  check order '[ "$(status_of order)" = "direct-mock ok;gemma-direct-mock ok;gemma-direct-catalog ok;" ]' "status=$(status_of order)"
  check order '[ "$(start_models order)" = "stub/model;stub/model-b;" ]' "起机模型顺序=$(start_models order)"
fi
if has b_only; then
  # 只给 B 段步：直接起 MODEL_B，不白起一次 A（一审 Major 1）
  run_chain b_only ARMS=gemma-direct-mock
  check b_only '[ "$(rc_of b_only)" = 0 ] && [ "$(status_of b_only)" = "gemma-direct-mock ok;" ]' "rc=$(rc_of b_only) status=$(status_of b_only)"
  check b_only '[ "$(calls b_only start)" = 1 ] && [ "$(start_models b_only)" = "stub/model-b;" ] && [ "$(calls b_only stop)" = 1 ]' "start=$(calls b_only start) models=$(start_models b_only) stop=$(calls b_only stop)"
  check b_only 'ls -d "$T"/b_only/smoke-direct-*-B >/dev/null 2>&1' "B 段独跑也要冒烟"
fi
if has switch_budget; then
  # 换模型的 stop+start 不占重起预算：RESTARTS_MAX=1 下 B 段步挂死一次仍能重起续跑
  run_chain switch_budget ARMS=direct-mock,gemma-direct-mock RESTARTS_MAX=1 STUB_BEHAVIOR='{"gemma-direct-mock":"hang_then_ok"}'
  check switch_budget '[ "$(rc_of switch_budget)" = 0 ] && [ "$(status_of switch_budget)" = "direct-mock ok;gemma-direct-mock ok;" ]' "rc=$(rc_of switch_budget) status=$(status_of switch_budget)"
  check switch_budget 'grep -q "重起（1/1）后续跑" "$T/switch_budget/run_colab.log" && [ "$(calls switch_budget start)" = 3 ] && [ "$(start_models switch_budget)" = "stub/model;stub/model-b;stub/model-b;" ]' "start=$(calls switch_budget start) models=$(start_models switch_budget)"
fi
if has smoke_reset; then
  # 冒烟门按段重置：A 段冒烟失败只跳 A 段步，B 段冒烟通过照跑
  run_chain smoke_reset ARMS=direct-mock,gemma-direct-mock STUB_SMOKE=fail STUB_SMOKE_B=ok
  check smoke_reset '[ "$(done_of smoke_reset)" = "BATCH_RC=0 SKIPPED=1" ] && [ "$(status_of smoke_reset)" = "direct-mock skipped;gemma-direct-mock ok;" ]' "DONE=$(done_of smoke_reset) status=$(status_of smoke_reset)"
fi
if has trace_done_rerun; then
  # 已完成的 trace 步在 FORCE_OUT 续跑时不重跑、不挪目录（一审 Major 2）
  run_chain trace_done_rerun ARMS=trace-new-900,orch-turn-none STUB_BEHAVIOR='{"orch-turn-none":"fail"}'
  check trace_done_rerun '[ "$(status_of trace_done_rerun)" = "trace-new-900 ok;orch-turn-none fail;" ]' "第一遍 status=$(status_of trace_done_rerun)"
  cp "$T/trace_done_rerun/trace-new-900/stub.args" "$T/trace_done_rerun.args1"
  env "${ENVBASE[@]}" OUT="$T/trace_done_rerun" STUB_DIR="$T/trace_done_rerun.stub2" ARMS=trace-new-900,orch-turn-none FORCE_OUT=1 zsh "$CHAIN" > "$T/trace_done_rerun.stdout2" 2>&1; echo $? > "$T/trace_done_rerun.rc"
  check trace_done_rerun '[ "$(rc_of trace_done_rerun)" = 0 ] && [ "$(status_of trace_done_rerun)" = "trace-new-900 ok;orch-turn-none ok;" ]' "第二遍 rc=$(rc_of trace_done_rerun) status=$(status_of trace_done_rerun)"
  check trace_done_rerun '[ -z "$(print -l "$T"/trace_done_rerun/trace-new-900.partial-*(N))" ] && cmp -s "$T/trace_done_rerun/trace-new-900/stub.args" "$T/trace_done_rerun.args1" && grep -q "已完成（meta.json 在），不重跑" "$T/trace_done_rerun/run_colab.log"' "完成的 trace 步被重跑或被挪走"
fi
if has switch_start_fail; then
  # 换模型起机失败（两次都失败）：A 段结果保留、B 段步记 skipped、DONE FAIL_START、停机
  run_chain switch_start_fail ARMS=direct-mock,gemma-direct-mock,gemma-direct-catalog STUB_START_FAIL_AFTER=1
  check switch_start_fail '[ "$(rc_of switch_start_fail)" = 3 ] && [ "$(done_of switch_start_fail)" = FAIL_START ]' "rc=$(rc_of switch_start_fail) DONE=$(done_of switch_start_fail)"
  check switch_start_fail '[ "$(status_of switch_start_fail)" = "direct-mock ok;gemma-direct-mock skipped;gemma-direct-catalog skipped;" ]' "status=$(status_of switch_start_fail)（B 段步须记 skipped）"
  check switch_start_fail '[ "$(calls switch_start_fail start)" = 3 ] && [ "$(calls switch_start_fail stop)" = 2 ] && stop_before_done switch_start_fail' "start=$(calls switch_start_fail start)（A + B 两试） stop=$(calls switch_start_fail stop)"
fi
if has hang_recover; then
  run_chain hang_recover ARMS=orch-turn-none,direct-mock STUB_BEHAVIOR='{"orch-turn-none":"hang_then_ok"}'
  check hang_recover '[ "$(rc_of hang_recover)" = 0 ]' "rc=$(rc_of hang_recover) (want 0)"
  check hang_recover '[ "$(done_of hang_recover)" = "BATCH_RC=0 SKIPPED=0" ]' "DONE=$(done_of hang_recover)"
  check hang_recover '[ "$(calls hang_recover stop)" = 2 ] && [ "$(calls hang_recover start)" = 2 ]' "stop=$(calls hang_recover stop) start=$(calls hang_recover start) (want 2/2)"
  check hang_recover 'grep -q "重起（1/2）后续跑" "$T/hang_recover/run_colab.log"' "日志无重起记录"
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
  check restart_start_fail '[ "$(calls restart_start_fail start)" = 3 ] && [ "$(calls restart_start_fail stop)" = 2 ]' "start=$(calls restart_start_fail start) stop=$(calls restart_start_fail stop) (want 3/2：首起 + 重起两试；挂死停机一次 + 起机失败防御性停机一次)"
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
if has ready_retry; then
  # 首起 start 成功但 VM 随即 failed（瞬时 FAIL_READY）→ 防御性停机 → 等释放 → 自动再起一次 → 跑步；步随后挂死一次，RESTARTS_MAX=1
  # 下仍能重起续跑 = 重试没占重起预算
  run_chain ready_retry ARMS=direct-mock STUB_FAIL_READY_TIMES=1 RESTARTS_MAX=1 STUB_BEHAVIOR='{"direct-mock":"hang_then_ok"}'
  check ready_retry '[ "$(rc_of ready_retry)" = 0 ] && [ "$(done_of ready_retry)" = "BATCH_RC=0 SKIPPED=0" ]' "rc=$(rc_of ready_retry) DONE=$(done_of ready_retry)"
  check ready_retry '[ "$(calls ready_retry start)" = 3 ] && [ "$(calls ready_retry stop)" = 3 ]' "start=$(calls ready_retry start) stop=$(calls ready_retry stop)（want 3/3：首起失败防御性停机 + 重试 + 挂死重起 + 收官停机）"
  check ready_retry 'grep -q "首次起机未 READY（rc=4，VM 随即 state）" "$T/ready_retry/run_colab.log" && grep -q "重起（1/1）后续跑" "$T/ready_retry/run_colab.log" && [ "$(status_of ready_retry)" = "direct-mock ok;" ]' "重试/重起日志缺失或 status=$(status_of ready_retry)"
  check ready_retry '[ "$(grep -n "stop rc=0" "$T/ready_retry/run_colab.log" | head -n1 | cut -d: -f1)" -lt "$(grep -n "START deploy" "$T/ready_retry/run_colab.log" | sed -n 2p | cut -d: -f1)" ]' "重试没有先停机再起机"
fi
if has ready_retry_exhaust; then
  # 重试那次也未 READY → DONE FAIL_READY / rc 4、步记 skipped、停机两次、DONE 在停机后
  run_chain ready_retry_exhaust ARMS=direct-mock,direct-prod STUB_FAIL_READY_TIMES=2
  check ready_retry_exhaust '[ "$(rc_of ready_retry_exhaust)" = 4 ] && [ "$(done_of ready_retry_exhaust)" = FAIL_READY ]' "rc=$(rc_of ready_retry_exhaust) DONE=$(done_of ready_retry_exhaust)"
  check ready_retry_exhaust '[ "$(calls ready_retry_exhaust start)" = 2 ] && [ "$(calls ready_retry_exhaust stop)" = 2 ] && stop_before_done ready_retry_exhaust' "start=$(calls ready_retry_exhaust start) stop=$(calls ready_retry_exhaust stop)（want 2/2，只重试一次）"
  check ready_retry_exhaust '[ "$(status_of ready_retry_exhaust)" = "direct-mock skipped;direct-prod skipped;" ]' "status=$(status_of ready_retry_exhaust)"
fi
if has ready_retry_off; then
  # READY_RETRY=0：行为同旧版，首起未 READY 直接 FAIL_READY，起机一次停机一次
  run_chain ready_retry_off ARMS=direct-mock STUB_FAIL_READY_TIMES=1 READY_RETRY=0
  check ready_retry_off '[ "$(rc_of ready_retry_off)" = 4 ] && [ "$(done_of ready_retry_off)" = FAIL_READY ] && [ "$(calls ready_retry_off start)" = 1 ] && [ "$(calls ready_retry_off stop)" = 1 ]' "rc=$(rc_of ready_retry_off) DONE=$(done_of ready_retry_off) start=$(calls ready_retry_off start) stop=$(calls ready_retry_off stop)"
fi
if has ready_retry_timeout; then
  # 等 READY 超时（READY_MINUTES 等满）不重试：FAIL_READY / rc 4、起机一次停机一次。桩 status 永不 ready，测试接缝 READY_DEADLINE_SEC=2
  run_chain ready_retry_timeout ARMS=direct-mock STUB_READY_POLLS=1000 READY_DEADLINE_SEC=2
  check ready_retry_timeout '[ "$(rc_of ready_retry_timeout)" = 4 ] && [ "$(done_of ready_retry_timeout)" = FAIL_READY ] && [ "$(calls ready_retry_timeout start)" = 1 ] && [ "$(calls ready_retry_timeout stop)" = 1 ]' "rc=$(rc_of ready_retry_timeout) DONE=$(done_of ready_retry_timeout) start=$(calls ready_retry_timeout start) stop=$(calls ready_retry_timeout stop)（等 READY 超时不得重试）"
  check ready_retry_timeout '! grep -q "自动重试一次" "$T/ready_retry_timeout/run_colab.log" && grep -q "no READY host within" "$T/ready_retry_timeout/run_colab.log"' "超时路径日志不对"
fi
if has ready_retry_stop_fail; then
  # 首起 VM 随即 failed、防御性停机失败 → 不重试、起机只一次；各步仍写 skipped；再次停机失败记 STOP_FAILED / rc 5
  run_chain ready_retry_stop_fail ARMS=direct-mock,direct-prod STUB_FAIL_READY_TIMES=1 STUB_STOP_FAIL=1
  check ready_retry_stop_fail '[ "$(rc_of ready_retry_stop_fail)" = 5 ] && [ "$(done_of ready_retry_stop_fail)" = STOP_FAILED ] && [ "$(calls ready_retry_stop_fail start)" = 1 ]' "rc=$(rc_of ready_retry_stop_fail) DONE=$(done_of ready_retry_stop_fail) start=$(calls ready_retry_stop_fail start)（停机失败不得重试起机）"
  check ready_retry_stop_fail '[ "$(status_of ready_retry_stop_fail)" = "direct-mock skipped;direct-prod skipped;" ] && grep -q "防御性停机失败：不重试" "$T/ready_retry_stop_fail/run_colab.log"' "status=$(status_of ready_retry_stop_fail)（每步须有一行 skipped）"
fi
if has lease_rollover; then
  # 租约预算 20 s：第一步跑 12 s 后已用约 15 s（未超预算），但「已用 + 同类最长约 13 s > 20」→ 第二步开跑前同模型 stop+start 换租约；
  # 只看已用不加预计用时的判据在这里不会换机（变异证明用）。第二步随后挂死一次，RESTARTS_MAX=1 下仍能重起 = 换租约不占重起预算
  run_chain lease_rollover ARMS=direct-mock,direct-prod LEASE_BUDGET_SEC=20 RESTARTS_MAX=1 STUB_BEHAVIOR='{"direct-mock":"ok_slow","direct-prod":"hang_then_ok"}' STUB_OK_SLEEP=12
  check lease_rollover '[ "$(rc_of lease_rollover)" = 0 ] && [ "$(done_of lease_rollover)" = "BATCH_RC=0 SKIPPED=0 ROLLOVERS=1" ]' "rc=$(rc_of lease_rollover) DONE=$(done_of lease_rollover)"
  check lease_rollover '[ "$(calls lease_rollover start)" = 3 ] && [ "$(calls lease_rollover stop)" = 3 ] && [ "$(start_models lease_rollover)" = "stub/model;stub/model;stub/model;" ]' "start=$(calls lease_rollover start) stop=$(calls lease_rollover stop) models=$(start_models lease_rollover)（want 3/3：换租约 + 挂死重起，同模型）"
  check lease_rollover '[ "$(status_of lease_rollover)" = "direct-mock ok;direct-prod ok;" ] && grep -q "LEASE direct-prod: 本租约已用" "$T/lease_rollover/run_colab.log" && grep -q "重起（1/1）后续跑" "$T/lease_rollover/run_colab.log"' "status=$(status_of lease_rollover)；换租约 / 重起日志缺失"
  check lease_rollover '[ "$(grep -n "LEASE direct-prod" "$T/lease_rollover/run_colab.log" | cut -d: -f1)" -lt "$(grep -n "ARM direct-prod .* start:" "$T/lease_rollover/run_colab.log" | cut -d: -f1)" ] && [ "$(grep -c "stop rc=0" "$T/lease_rollover/run_colab.log")" = 3 ]' "换租约须在第二步开跑之前，且先停机"
  check lease_rollover '[ "$(print -l "$T"/lease_rollover/smoke-direct-*(N/) | wc -l | tr -d " ")" = 1 ]' "换租约后不该再冒烟（冒烟目录数 $(print -l "$T"/lease_rollover/smoke-direct-*(N/) | wc -l | tr -d " ")）"
fi
if has lease_rollover_floor; then
  # 预算 1 s 小于任何一步：刚起的租约仍至少跑一步（否则刚起就再换一次，白烧起机）；三步 → 起机三次（每步一次），全 ok
  run_chain lease_rollover_floor ARMS=direct-mock,direct-prod,direct-catalog LEASE_BUDGET_SEC=1
  check lease_rollover_floor '[ "$(rc_of lease_rollover_floor)" = 0 ] && [ "$(done_of lease_rollover_floor)" = "BATCH_RC=0 SKIPPED=0 ROLLOVERS=2" ]' "rc=$(rc_of lease_rollover_floor) DONE=$(done_of lease_rollover_floor)"
  check lease_rollover_floor '[ "$(calls lease_rollover_floor start)" = 3 ] && [ "$(calls lease_rollover_floor stop)" = 3 ] && [ "$(status_of lease_rollover_floor)" = "direct-mock ok;direct-prod ok;direct-catalog ok;" ]' "start=$(calls lease_rollover_floor start) stop=$(calls lease_rollover_floor stop) status=$(status_of lease_rollover_floor)"
fi
if has lease_unlimited; then
  # MAX_VM_HOURS=0 走生产换算（不给 LEASE_BUDGET_SEC）：不限，同样的两步只起一次机；时间线首行记有效配置；MAX_VM_HOURS=1.5 非法 → 64 不起机
  run_chain lease_unlimited ARMS=direct-mock,direct-prod MAX_VM_HOURS=0 STUB_BEHAVIOR='{"direct-mock":"ok_slow"}'
  check lease_unlimited '[ "$(rc_of lease_unlimited)" = 0 ] && [ "$(done_of lease_unlimited)" = "BATCH_RC=0 SKIPPED=0" ] && [ "$(calls lease_unlimited start)" = 1 ]' "rc=$(rc_of lease_unlimited) DONE=$(done_of lease_unlimited) start=$(calls lease_unlimited start)"
  check lease_unlimited 'head -n1 "$T/lease_unlimited/run_colab.log" | grep -q "config: .*MAX_VM_HOURS=0 LEASE_BUDGET_SEC=0 "' "首行有效配置缺失：$(head -n1 "$T/lease_unlimited/run_colab.log")"
  run_chain lease_unlimited_bad ARMS=direct-mock MAX_VM_HOURS=1.5
  check lease_unlimited '[ "$(rc_of lease_unlimited_bad)" = 64 ] && [ "$(calls lease_unlimited_bad start)" = 0 ]' "MAX_VM_HOURS=1.5 应 64 不起机：rc=$(rc_of lease_unlimited_bad) start=$(calls lease_unlimited_bad start)"
fi

print -r -- "run_colab selftest: pass=$pass fail=$fail (scenarios: ${want[*]})"
[ $fail -eq 0 ]
