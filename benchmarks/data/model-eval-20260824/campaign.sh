#!/bin/bash
# 2026-08-24 睡前大型任务（用户拍板）：题库扩容后的六档同口径能力测评 + Ornith-1.5 速度基线
#   Phase Q   model_eval 引擎直连（部署同款参数），dims=tool_call,longchain,text,vision，repeat=1
#             档位：Ornith-1.5-35B-A3B 4bit/8bit（纯文本件，vision 自动跳过）、gemma-4-26b 4bit/8bit、
#             Qwen3.8-27B 4bit/8bit（条目级 MTP 生产口径，与 08-23 画像同）
#   Phase 6   6bit 判定（用户规则）：Ornith 4bit vs 8bit 在 tool_call/longchain/text 任一维
#             pass_rate 差 ≥ 0.03 → 追加 6bit 的 Q + S；否则不跑
#   Phase S   bench_baseline v3 真贪心（server 生产口径）Ornith 各档 × ctx {0,4k,16k,32k,64k}，runs=3；
#             四档老模型的同口径数据复用 benchmarks/baseline/20260816-overnight/speed_off/baseline_v3.jsonl
# 纪律：单 GPU 串行（pidfile 锁）；不改 config.yaml；每步失败不阻断队列；脚本不推送（完成推送由主会话验证后发）。
set -u
BASE=/Volumes/enkin/project/Agent
BACKEND="$BASE/backend"
PY="$BACKEND/.venv/bin/python"
DIR="$BASE/benchmarks/data/model-eval-20260824"
PROG="$DIR/progress.log"
LOCK="$DIR/campaign.pid"
DIMS=tool_call,longchain,text,vision
CTX=0,4096,16384,32768,65536
export PYTHONUNBUFFERED=1

note() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$PROG"; }

if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  note "REFUSED 已有实例在跑（pid=$(cat "$LOCK")），本次启动退出"; exit 1
fi
echo $$ > "$LOCK"

preflight_fail=""
[ -x "$PY" ] || preflight_fail="venv python 不存在: $PY"
[ -f "$BASE/.env.local" ] || preflight_fail="${preflight_fail:+$preflight_fail; }.env.local 缺失（bench 取 INFER_API_KEY 用）"
if lsof -nP -iTCP:14161 -sTCP:LISTEN >/dev/null 2>&1; then
  preflight_fail="${preflight_fail:+$preflight_fail; }端口 14161 已被占用"
fi
if pgrep -f "run_eval.py|bench_baseline.py|inference/server.py" >/dev/null 2>&1; then
  preflight_fail="${preflight_fail:+$preflight_fail; }已有真模型进程在跑（run_eval/bench/server）"
fi
if [ -n "$preflight_fail" ]; then note "PREFLIGHT FAIL: $preflight_fail"; exit 1; fi

CHILD=0
cleanup() {
  note "收到 INT/TERM：转发子进程 pid=$CHILD 并清理 inference server"
  [ "$CHILD" != 0 ] && kill -TERM "$CHILD" 2>/dev/null
  sleep 2; pkill -f "inference/server.py" 2>/dev/null; exit 130
}
trap cleanup INT TERM
run_step() { "$@" & CHILD=$!; wait "$CHILD"; local rc=$?; CHILD=0; return $rc; }

cells_done() {
  [ -f "$1" ] || { echo 0; return; }
  "$PY" - "$1" "$2" <<'PYCNT'
import json, sys
n = 0
for line in open(sys.argv[1]):
    try: r = json.loads(line)
    except ValueError: continue
    if r.get("type") == "cell" and r.get("complete") and r.get("model") == sys.argv[2]: n += 1
print(n)
PYCNT
}

run_q() {  # $1=短名
  local m=$1
  if [ -f "$DIR/q/${m}/summary.json" ]; then note "Q $m 已完成（summary 存在），跳过"; return 0; fi
  note "Q $m start"
  local t0=$SECONDS
  run_step "$PY" benchmarks/model_eval/run_eval.py --model "$m" --dims "$DIMS" \
      --out "$DIR/q/${m}" >> "$DIR/q_${m}.log" 2>&1
  local rc=$?
  local summ; summ=$(grep '^== summary:' "$DIR/q_${m}.log" | tail -1); summ=${summ:0:400}
  [ $rc -eq 0 ] && [ -f "$DIR/q/${m}/summary.json" ] || q_fail=$((q_fail+1))
  note "Q $m rc=$rc dur=$((SECONDS-t0))s ${summ:-（无 summary 行）}"
}

run_s() {  # $1=repo 全名
  local repo=$1 m=${1##*/}
  local out="$DIR/speed/baseline_v3_ornith.jsonl"
  if [ "$(cells_done "$out" "$repo")" -ge 5 ]; then note "S $m 已有 5 格 complete，跳过"; return 0; fi
  note "S $m start"
  local t0=$SECONDS
  run_step "$PY" scripts/bench_baseline.py --models "$repo" --ctx "$CTX" --runs 3 \
      --out "$out" >> "$DIR/s_${m}.log" 2>&1
  local rc=$?
  local got; got=$(cells_done "$out" "$repo")
  [ "$got" -lt 5 ] && s_fail=$((s_fail+1))
  note "S $m rc=$rc cells=$got/5 dur=$((SECONDS-t0))s"
}

cd "$BACKEND" || { note "FATAL cd backend 失败"; exit 1; }
note "==== CAMPAIGN START pid=$$ ===="
q_fail=0; s_fail=0

# ---------- Phase Q ----------
for m in Ornith-1.5-35B-A3B-MLX-4bit Ornith-1.5-35B-A3B-MLX-8bit; do run_q "$m"; done

# ---------- Phase 6：6bit 判定 ----------
need6=$("$PY" - "$DIR/q" <<'PY6'
import json, sys
from pathlib import Path
q = Path(sys.argv[1])
try:
    a = json.loads((q / "Ornith-1.5-35B-A3B-MLX-4bit/summary.json").read_text())["dimensions"]
    b = json.loads((q / "Ornith-1.5-35B-A3B-MLX-8bit/summary.json").read_text())["dimensions"]
except Exception as e:
    print("undecided:" + str(e)); sys.exit(0)
diffs = {d: round(abs(a.get(d, {}).get("pass_rate", 0) - b.get(d, {}).get("pass_rate", 0)), 3)
         for d in ("tool_call", "longchain", "text")}
print(("yes" if any(v >= 0.03 for v in diffs.values()) else "no") + ":" + json.dumps(diffs))
PY6
)
note "6bit 判定：$need6"

if [[ "$need6" == yes:* ]]; then
  run_q Ornith-1.5-35B-A3B-MLX-6bit
else
  note "6bit Q 不跑（差距 <0.03 或无法判定）"
fi

for m in gemma-4-26b-a4b-it-4bit gemma-4-26b-a4b-it-8bit Qwen3.8-27B-4bit Qwen3.8-27B-8bit; do run_q "$m"; done

# ---------- Phase S ----------
run_s ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit
run_s ornith-ai/Ornith-1.5-35B-A3B-MLX-8bit
if [[ "$need6" == yes:* ]]; then run_s ornith-ai/Ornith-1.5-35B-A3B-MLX-6bit; fi

note "==== CAMPAIGN DONE q_fail=$q_fail s_fail=$s_fail ===="
rm -f "$LOCK"
