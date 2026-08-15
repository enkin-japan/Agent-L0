#!/bin/bash
# 31B fp8 γ=4/5/7 补格（VM 侧，2026-08-15）——§6.4 补格项：补齐 8bit 档曲线，
# γ=5 是 mlx 瓦片断崖所在。口径与 benchmarks/data/mtp-31b-cross/d31_fp8_g0..3
# 一致：online fp8、VLLM_MAXLEN=1024、VLLM_UTIL=0.95、400 tok、同一份 61-token
# prompt（用 Mac 上传的 /content/_prompt.json，不依赖 clone 内路径）。vllm 版本差
# （0.27.1 vs 旧格）由 JSON 内 vllm 字段自记，落表时注明。
# 前提：bench_bootstrap.sh 已 BOOT_READY 且 31B 权重已预拉进 models/（boot 期
# 完成，绝不塞进格 timeout——评审 Critical）。
# 结构照抄 bench_mtp_recheck.sh（两轮评审形态）：RESULT-有产物即成功（rc=124
# 收尾挂死不冤杀）、产物 mv 不 rm、连续两格失败中止、逐点名收尸。
set -uo pipefail
export LD_LIBRARY_PATH="/usr/lib64-nvidia:${LD_LIBRARY_PATH:-}"
export PATH="/opt/bin:$PATH"
for d in /usr/local/lib/python3*/dist-packages/nvidia/*/lib; do
  [ -d "$d" ] && export LD_LIBRARY_PATH="$d:$LD_LIBRARY_PATH"
done
# 场参先、密钥后：bench.env 覆盖不了 GIT_TOKEN/HF_TOKEN（评审）
[ -f /content/bench.env ] && { set -a; . /content/bench.env; set +a; }
set -a; . /content/colab.env; set +a
export HF_HOME=/content/hf
export HF_HUB_CACHE=/content/hf/hub
APP=/content/agent
OUT=/content/bench_out
[ -d "$OUT" ] && [ -n "$(ls -A "$OUT" 2>/dev/null)" ] && mv "$OUT" "$OUT.prev-$(date +%s)"
mkdir -p "$OUT"
log(){ echo "[$(date +%T)] $*"; }
die(){ log "FATAL: $*"; exit 1; }

TARGET="$APP/backend/models/${BENCH_TARGET_DIR:-gemma-4-31B-it}"
DRAFT="$APP/backend/models/${BENCH_DRAFT_DIR:-gemma-4-31B-it-assistant}"
PROMPT="/content/_prompt.json"
[ -f "$TARGET/.download_complete" ] || die "目标件未预拉 ${TARGET}（boot 该做的）"
[ -f "$DRAFT/.download_complete" ] || die "草稿件未预拉 $DRAFT"
[ -f "$PROMPT" ] || die "缺 prompt 文件 ${PROMPT}（Mac 侧须先 colab upload）"

# 评测窗口开始：收掉 GPU 保活（评审 Critical：0.5G 常驻 + VLLM_UTIL=0.95 在
# 40G 上只剩 ~0.9G 余量，必挤爆）。格间空隙 <5min，用户侧 25min 判零看门无虞。
pkill -f "gpu_keepalive[.]py" 2>/dev/null && log "GPU 保活已收（评测窗口）"

reap_cells(){
  pkill -9 -f "vllm_cell[.]py" 2>/dev/null
  pkill -9 -f "VLLM::EngineCor[e]" 2>/dev/null
}
free_gpu(){
  local used
  for _ in $(seq 1 30); do
    used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
    case "$used" in (*[!0-9]*) used=99999;; ("") used=99999;; esac
    [ "$used" -lt 1200 ] && return 0
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
LAST=""
run_cell(){  # $1=γ
  local k=$1 name="d31_fp8_g$1"
  LAST="$name"
  log "CELL_START $name (γ=$k)"
  # SPEC_METHOD 显式清空：colab.env/bench.env 经 set -a 全量注入，飘着的值会
  # 让本批与 d31_fp8_g0..3 基线口径静默分叉（评审；同 INFER_SPEC_CONFIG 教训）
  ( cd "$APP/benchmarks" && \
    SPEC_METHOD="" VLLM_MAXLEN=1024 VLLM_UTIL=0.95 timeout -k 30 1500 \
      python3 vllm_cell.py "$TARGET" "$DRAFT" "$k" "$PROMPT" 400 \
      "$OUT/$name.json" graph fp8 > "$OUT/$name.log" 2>&1 )
  local rc=$?
  # 产物在手即成功（rc=124 常见于 vLLM 收尾挂死；照抄 recheck 语义）
  if [ -f "$OUT/$name.json" ]; then
    [ $rc -ne 0 ] && log "NOTE ${name} 产物完整但 rc=${rc}（收尾挂死类），按成功记"
    FAILS_IN_ROW=0
    log "RESULT $name $(python3 - "$OUT/$name.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d.pop("token_ids", None)
print(json.dumps(d, ensure_ascii=False))
PY
)"
  else
    log "CELL_FAIL $name rc=$rc tail: $(tail -3 "$OUT/$name.log" 2>/dev/null | tr '\n' '|')"
    FAILS_IN_ROW=$((FAILS_IN_ROW+1))
    [ "$FAILS_IN_ROW" -ge 2 ] && die "连续两格失败，中止保预算"
  fi
  reap_cells
}

run_cell 4
free_gpu
run_cell 5
free_gpu
run_cell 7
# 末格后不等 free_gpu（评审：白等 2min 计费），只收尸
log "BENCH_ALL_DONE"
