#!/bin/bash
# 2026-08-16 通宵战役（用户拍板，睡前任务）：
#   Phase Q     全模型质量测评（model_eval 引擎直连，tool_call+longchain+vision，repeat=1，
#               与 Qwen3.8 画像同口径；Qwen3.8×2 已有 08-15 同口径画像故不重跑）
#   Phase S-off 全模型 v3 真贪心基线 × ctx {0,4k,16k,32k,64k}（bench_baseline，server 生产口径）
#   Phase S-on  MTP 档 §6.3 生产最优块 × 同 ctx（bench_baseline --spec-mode drafter）
# 纪律：单 GPU 串行（pidfile 锁保证单实例，§4.21 教训）；不改 config.yaml（投机走 INFER_SPEC_*）；
#       每步失败不阻断队列；成败判定不信 bench 进程 rc（它逐格吞异常恒 0），改从 jsonl 数 complete 格。
set -u
BASE=/Volumes/enkin/project/Agent
BACKEND="$BASE/backend"
PY="$BACKEND/.venv/bin/python"
DIR="$BASE/benchmarks/baseline/20260816-overnight"
PROG="$DIR/progress.log"
LOCK="$DIR/campaign.pid"
CTX=0,4096,16384,32768,65536
SKIP=gemma-4-31b-it-8bit:65536   # OOM 已证（§7 阶段一）；31b-8bit:32768+草稿件容许现场失败
export PYTHONUNBUFFERED=1

note() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$PROG"; }
# notify 已整体拆除（2026-08-16 推送纪律：脚本不推送；完成推送由主会话验证后发）

# ---- 单实例锁（§4.21：队列绝不允许双跑；误判进程未起就重启正是原型事故） ----
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  note "REFUSED 已有实例在跑（pid=$(cat "$LOCK")），本次启动退出"
  exit 1
fi
echo $$ > "$LOCK"

# ---- preflight：30h 队列开跑前把便宜的先决条件全验掉 ----
preflight_fail=""
[ -x "$PY" ] || preflight_fail="venv python 不存在: $PY"
[ -f "$BASE/.env.local" ] || preflight_fail="${preflight_fail:+$preflight_fail; }.env.local 缺失（bench 取 INFER_API_KEY 用）"
if lsof -nP -iTCP:14161 -sTCP:LISTEN >/dev/null 2>&1; then
  preflight_fail="${preflight_fail:+$preflight_fail; }端口 14161 已被占用（残留 server？bench 会连错对象）"
fi
if [ -n "$preflight_fail" ]; then
  note "PREFLIGHT FAIL: $preflight_fail"
  exit 1
fi

# ---- INT/TERM：转发给在跑的子进程并清掉 bench 拉起的 server，不给 GPU 留孤儿 ----
CHILD=0
cleanup() {
  note "收到 INT/TERM：转发子进程 pid=$CHILD 并清理 inference server"
  [ "$CHILD" != 0 ] && kill -TERM "$CHILD" 2>/dev/null
  sleep 2
  pkill -f "inference/server.py" 2>/dev/null
  exit 130
}
trap cleanup INT TERM

run_step() {  # 前台语义 + 可被 trap 转发：后台起子进程再 wait
  "$@" & CHILD=$!
  wait "$CHILD"; local rc=$?
  CHILD=0
  return $rc
}

# 从 bench 的 --out jsonl 里数某模型的 complete 格数（成败的唯一真相；进程 rc 恒 0）。
# 必须 json 解析：jsonl 里紧凑/带空格两种序列化并存（08-16 实案），字面 grep 会数出 0。
cells_done() {  # $1=out文件 $2=repo全名
  [ -f "$1" ] || { echo 0; return; }
  "$PY" - "$1" "$2" <<'PYCNT'
import json, sys
n = 0
for line in open(sys.argv[1]):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("type") == "cell" and r.get("complete") and r.get("model") == sys.argv[2]:
        n += 1
print(n)
PYCNT
}

cd "$BACKEND" || { note "FATAL cd backend 失败"; exit 1; }
note "==== CAMPAIGN START pid=$$ ===="

# ---------- Phase Q：质量测评（争议档优先：26b 三档 → 31b 三档 → 12b → e4b → 9B 双件） ----------
Q_MODELS=(
  gemma-4-26b-a4b-it-4bit
  gemma-4-26b-a4b-it-8bit
  gemma-4-26b-a4b-it-qat-4bit
  gemma-4-31b-it-8bit
  gemma-4-31b-it-4bit
  gemma-4-31b-it-qat-4bit
  gemma-4-12b-it-8bit
  gemma-4-12b-it-4bit
  gemma-4-e4b-it-4bit
  gemma-4-e4b-it-8bit
  Qwen3.5-9B-4bit
  Ornith-1.0-9B-4bit
)
note "PHASE Q START（${#Q_MODELS[@]} 模型，dims=tool_call,longchain,vision）"
q_fail=0
for m in "${Q_MODELS[@]}"; do
  if [ -f "$DIR/q_done_${m}" ]; then    # 断点续跑：本战役已跑完的模型不重烧
    note "Q $m 已完成（marker 存在），跳过"
    continue
  fi
  note "Q $m start"
  t0=$SECONDS
  run_step "$PY" benchmarks/model_eval/run_eval.py --model "$m" \
      --dims tool_call,longchain,vision >> "$DIR/q_${m}.log" 2>&1
  rc=$?
  # 取**最后一条** summary：q 日志跨次追加，-m1 会拿到上一轮的旧行、让本轮失败
  # 被旧成功掩护（stage review M7，与 cells_done 同病根）
  summ=$(grep '^== summary:' "$DIR/q_${m}.log" | tail -1)
  summ=${summ:0:300}
  if [ $rc -eq 0 ] && [ -n "$summ" ]; then
    touch "$DIR/q_done_${m}"
  else
    q_fail=$((q_fail+1))
  fi
  note "Q $m rc=$rc dur=$((SECONDS-t0))s ${summ:-（无 summary 行）}"
done

# ---------- Phase S-off：v3 基线全矩阵（逐模型单独调用，隔离崩溃面） ----------
S_MODELS=(
  mlx-community/gemma-4-26b-a4b-it-4bit
  mlx-community/gemma-4-26b-a4b-it-8bit
  mlx-community/gemma-4-26b-a4b-it-qat-4bit
  mlx-community/gemma-4-31b-it-8bit
  mlx-community/gemma-4-31b-it-4bit
  mlx-community/gemma-4-31b-it-qat-4bit
  mlx-community/gemma-4-12b-it-8bit
  mlx-community/gemma-4-12b-it-4bit
  mlx-community/gemma-4-e4b-it-4bit
  mlx-community/gemma-4-e4b-it-8bit
  mlx-community/Qwen3.8-27B-8bit
  mlx-community/Qwen3.8-27B-4bit
  mlx-community/Qwen3.5-9B-4bit
  mlx-community/Ornith-1.0-9B-4bit
)
expected_cells() {  # 31b-8bit 跳 64k 格
  [ "$1" = gemma-4-31b-it-8bit ] && echo 4 || echo 5
}
note "PHASE S-off START（${#S_MODELS[@]} 模型 × ctx ${CTX}，runs=3）"
soff_fail=0
SOFF_OUT="$DIR/speed_off/baseline_v3.jsonl"
for repo in "${S_MODELS[@]}"; do
  m=${repo##*/}
  exp=$(expected_cells "$m")
  if [ "$(cells_done "$SOFF_OUT" "$repo")" -ge "$exp" ]; then
    note "S-off $m 已有 $exp 格 complete，跳过"
    continue
  fi
  note "S-off $m start"
  t0=$SECONDS
  run_step "$PY" scripts/bench_baseline.py --models "$repo" --ctx "$CTX" --runs 3 \
      --skip "$SKIP" --out "$SOFF_OUT" >> "$DIR/soff_${m}.log" 2>&1
  rc=$?
  got=$(cells_done "$SOFF_OUT" "$repo")
  [ "$got" -lt "$exp" ] && soff_fail=$((soff_fail+1))
  note "S-off $m rc=$rc cells=$got/$exp dur=$((SECONDS-t0))s"
done

# ---------- Phase S-on：MTP 臂（§6.3 生产最优块；QAT 档暂配 PTQ 草稿件） ----------
# 26b-4bit 冷启动已判死（§6.3 ⛔）——仍入列跑 ctx 维度，是为把"各 ctx 下同样净亏"存档，非翻案。
# 记法 repo|草稿件目录名|draft_block_size
S_ON=(
  "mlx-community/gemma-4-26b-a4b-it-8bit|gemma-4-26b-a4b-it-assistant-4bit|4"
  "mlx-community/gemma-4-26b-a4b-it-4bit|gemma-4-26b-a4b-it-assistant-4bit|3"
  "mlx-community/gemma-4-26b-a4b-it-qat-4bit|gemma-4-26b-a4b-it-assistant-4bit|3"
  "mlx-community/gemma-4-31b-it-8bit|gemma-4-31b-it-assistant-8bit|4"
  "mlx-community/gemma-4-31b-it-4bit|gemma-4-31b-it-assistant-8bit|3"
  "mlx-community/gemma-4-31b-it-qat-4bit|gemma-4-31b-it-assistant-8bit|3"
  "mlx-community/gemma-4-12b-it-8bit|gemma-4-12b-it-assistant-bf16|5"
  "mlx-community/gemma-4-12b-it-4bit|gemma-4-12b-it-assistant-bf16|3"
  "mlx-community/gemma-4-e4b-it-8bit|gemma-4-e4b-it-assistant-bf16|5"
  "mlx-community/gemma-4-e4b-it-4bit|gemma-4-e4b-it-assistant-bf16|4"
  "mlx-community/Qwen3.8-27B-8bit|Qwen3.8-27B-MTP-8bit|5"
  "mlx-community/Qwen3.8-27B-4bit|Qwen3.8-27B-MTP-4bit|3"
)
note "PHASE S-on START（${#S_ON[@]} 档 × ctx ${CTX}，runs=3）"
son_fail=0
SON_OUT="$DIR/speed_mtp/mtp_v3.jsonl"
for entry in "${S_ON[@]}"; do
  IFS='|' read -r repo draft blk <<< "$entry"
  m=${repo##*/}
  exp=$(expected_cells "$m")
  if [ "$(cells_done "$SON_OUT" "$repo")" -ge "$exp" ]; then
    note "S-on $m 已有 $exp 格 complete，跳过"
    continue
  fi
  note "S-on $m draft=$draft block=$blk start"
  t0=$SECONDS
  run_step "$PY" scripts/bench_baseline.py --models "$repo" --ctx "$CTX" --runs 3 \
      --skip "$SKIP" --spec-mode drafter \
      --draft-model "$BACKEND/models/$draft" --draft-block-size "$blk" \
      --out "$SON_OUT" >> "$DIR/son_${m}.log" 2>&1
  rc=$?
  got=$(cells_done "$SON_OUT" "$repo")
  [ "$got" -lt "$exp" ] && son_fail=$((son_fail+1))
  note "S-on $m rc=$rc cells=$got/$exp dur=$((SECONDS-t0))s"
done

note "==== CAMPAIGN DONE q_fail=$q_fail soff_fail=$soff_fail son_fail=$son_fail ===="
# 失败计数必须进退出码：全 0 才 rc=0，否则外层按 rc 判成败会 fail-open
# （stage review M6：son_fail=12 的整臂全灭曾以 rc=0 收场）
[ $((q_fail + soff_fail + son_fail)) -eq 0 ] || exit 2
exit 0
