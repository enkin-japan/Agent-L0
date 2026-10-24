#!/bin/bash
# 26B fp8 MTP 开/关复核场（VM 侧，2026-08-15）——验证台账 batch6 的 1.92×。
#
# 前提：daily_serve_chain.sh 已跑完（代码/依赖/权重/mtp 热补丁在位，SERVE_READY）。
# 本脚本开评测窗口：杀监理循环与 server 腾出 GPU，然后逐格跑（GPU 串行）。
# 跑序按交付价值排（评审 #8：时间预算若被截断，损失的必须是次要格）：
#   c0    受控 γ=0 基线 —— 快速管线健全性（对 B5 144.1 tps）
#   e_off / e_on4 / e_on8 —— chat 负载 A/B（batch6 同款 tool_call 30 题）＝本场交付
#   c4 / c4m —— 受控 γ=4 的 method 键 A/B（对 B5 202.8）
#   c8m   受控 γ=8（A9 悬案；**预期可能合法失败**，不计失败连击）
# 产物统一落 /content/bench_out/；每格完成在 stdout 打一行 "RESULT <名> <json>"
# 供 Mac 侧看护即时取回（§8.3 教训：会话丢时最多损失一格）。
# 失败策略：单格失败记 CELL_FAIL 继续；连续两格失败（c8m 除外）=系统性问题，中止保预算。
set -uo pipefail
export LD_LIBRARY_PATH="/usr/lib64-nvidia:${LD_LIBRARY_PATH:-}"
export PATH="/opt/bin:$PATH"
for d in /usr/local/lib/python3*/dist-packages/nvidia/*/lib; do
  [ -d "$d" ] && export LD_LIBRARY_PATH="$d:$LD_LIBRARY_PATH"
done
set -a; . /content/colab.env; set +a
APP=/content/agent
OUT=/content/bench_out
# 手动重跑时旧产物搬迁而非删除（评审：上一场未取回的付费数据不能因重跑消失）
[ -d "$OUT" ] && [ -n "$(ls -A "$OUT" 2>/dev/null)" ] && mv "$OUT" "$OUT.prev-$(date +%s)"
mkdir -p "$OUT"
log(){ echo "[$(date +%T)] $*"; }
die(){ log "FATAL: $*"; exit 1; }

TARGET="$APP/backend/models/gemma-4-26B-A4B-it"
DRAFT="$APP/backend/models/gemma-4-26B-A4B-it-assistant"
PROMPT="$APP/benchmarks/data/mtp-moe/_prompt.json"
[ -f "$TARGET/.download_complete" ] || die "目标件未就绪 $TARGET"
[ -f "$DRAFT/.download_complete" ] || die "草稿件未就绪 $DRAFT"
[ -f "$PROMPT" ] || die "缺 prompt 文件 $PROMPT（Mac 侧须先 colab upload）"

log "== 评测窗口：停监理循环与 server，腾 GPU"
pkill -f "serve_supervisor" 2>/dev/null
sleep 1
pkill -f "inference/server[.]py" 2>/dev/null
pkill -f "VLLM::EngineCor[e]" 2>/dev/null
reap_cells(){  # timeout SIGTERM 未必收走 vLLM（关停挂死常见）；本场进程逐一点名
  pkill -9 -f "vllm_cell[.]py" 2>/dev/null
  pkill -9 -f "run_eval[.]py" 2>/dev/null
  pkill -9 -f "VLLM::EngineCor[e]" 2>/dev/null
}
free_gpu(){  # 等 GPU 显存降到基本干净（保活 0.5G 常驻，阈值放 2000MB）
  local used
  for i in $(seq 1 30); do
    used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
    case "$used" in (*[!0-9]*) used=99999;; ("") used=99999;; esac
    [ "$used" -lt 2000 ] && return 0
    sleep 4
  done
  log "WARN GPU 显存 2min 未清（used=${used}MB），强杀残余后继续"
  reap_cells; sleep 5
}
free_gpu

pip show vllm 2>/dev/null | head -2 > "$OUT/env.txt"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader >> "$OUT/env.txt"
log "ENV $(tr '\n' ' ' < "$OUT/env.txt")"

FAILS_IN_ROW=0
after_cell(){  # $1=名 $2=rc $3=日志文件 $4=计不计失败连击(默认计)
  if [ "$2" -eq 0 ]; then
    FAILS_IN_ROW=0
  else
    log "CELL_FAIL $1 rc=$2 tail: $(tail -3 "$3" 2>/dev/null | tr '\n' ' | ')"
    if [ "${4:-count}" = "count" ]; then
      FAILS_IN_ROW=$((FAILS_IN_ROW+1))
      [ "$FAILS_IN_ROW" -ge 2 ] && die "连续两格失败，中止保预算"
    fi
  fi
  reap_cells
  free_gpu
}

run_cell(){  # $1=名 $2=k $3=method(空=不带) $4=计不计连击
  local name=$1 k=$2 method=$3 streak=${4:-count} draft_arg=none
  [ "$k" -gt 0 ] && draft_arg="$DRAFT"
  log "CELL_START $name (γ=$k method=${method:-无})"
  ( cd "$APP/benchmarks" && \
    SPEC_METHOD="$method" timeout -k 30 1500 python3 vllm_cell.py \
      "$TARGET" "$draft_arg" "$k" "$PROMPT" 400 "$OUT/$name.json" graph fp8 \
      > "$OUT/$name.log" 2>&1 )
  local rc=$?
  [ $rc -eq 0 ] && [ ! -f "$OUT/$name.json" ] && rc=97   # 零退出但无产物＝失败（评审 #14）
  if [ $rc -eq 0 ]; then
    log "RESULT $name $(python3 - "$OUT/$name.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d.pop("token_ids", None)
print(json.dumps(d, ensure_ascii=False))
PY
)"
  fi
  after_cell "$name" $rc "$OUT/$name.log" "$streak"
}

run_eval_arm(){  # $1=名 $2=spec_config json(空=off)
  local name=$1 spec=$2
  local resdir="$APP/backend/benchmarks/model_eval/results/gemma-4-26B-A4B-it"
  local before
  before=$(ls -1dt "$resdir"/*/ 2>/dev/null | head -1)
  log "EVAL_START $name"
  ( cd "$APP/backend"
    if [ -n "$spec" ]; then
      export INFER_SPEC_CONFIG="$spec"
    else
      # off 臂强制净空（评审：colab.env 被 set -a 全量注入，env 里若飘着
      # INFER_SPEC_CONFIG，对照臂会静默带投机跑，A/B 全错且无症状）
      unset INFER_SPEC_CONFIG
    fi
    INFER_QUANT=fp8 PYTHONUNBUFFERED=1 timeout -k 30 2700 python3 \
      benchmarks/model_eval/run_eval.py --model google/gemma-4-26B-A4B-it \
      --dims tool_call --backend cuda > "$OUT/$name.log" 2>&1 )
  local rc=$?
  local latest
  latest=$(ls -1dt "$resdir"/*/ 2>/dev/null | head -1)
  # 本臂没产出新目录时 latest 会指到上一臂的产物——绝不能冒名顶替
  [ -n "$latest" ] && [ "$latest" = "$before" ] && latest=""
  if [ -n "$latest" ] && [ -f "$latest/summary.json" ]; then
    cp "$latest/summary.json" "$OUT/${name}_summary.json"
    cp "$latest/transcripts.jsonl" "$OUT/${name}_transcripts.jsonl" 2>/dev/null
    log "RESULT $name $(python3 - "$OUT/${name}_summary.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
keep = {k: d[k] for k in ("speed", "dimensions") if k in d}
print(json.dumps(keep, ensure_ascii=False))
PY
)"
    # 产物在手即判本臂成功（评审：评测写完 summary 后 vLLM 收尾挂死 rc=124 的
    # 常见形态，不该既收数又记失败连击——与 run_cell 的"rc==0 才 RESULT"语义对齐）
    [ $rc -ne 0 ] && log "NOTE $name 产物完整但进程 rc=$rc（收尾挂死类），按成功记" && rc=0
  else
    [ $rc -eq 0 ] && rc=97   # 零退出但无产物＝失败
  fi
  after_cell "$name" $rc "$OUT/$name.log"
}

run_cell c0 0 ""

run_eval_arm e_off ""
run_eval_arm e_on4 "{\"method\":\"mtp\",\"model\":\"$DRAFT\",\"num_speculative_tokens\":4}"
run_eval_arm e_on8 "{\"method\":\"mtp\",\"model\":\"$DRAFT\",\"num_speculative_tokens\":8}"

run_cell c4 4 ""
run_cell c4m 4 mtp
run_cell c8m 8 mtp nostreak

log "BENCH_ALL_DONE"
