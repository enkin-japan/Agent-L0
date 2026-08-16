#!/bin/bash
# 战役看门狗：停滞 + 超时双报警（只报警不动手——退出即把主会话叫醒来处置）。
# 用法：watchdog.sh <campaign.sh 的 PID>
# 判据：
#   停滞 = 进度承载文件 40 分钟无更新。**排除 server_*.log**：server 卡死时仍可能
#          刷日志（重试/告警循环），把它算进活性会让停滞报警永不触发。
#          最长静默段实测参照：31b 档装载+64k 预填 ×4 run ≈ 20min 级，留一倍余量。
#   超时 = 总时长超 40h（全队列预估 ~28-30h，超 40h 必有异常）
# 不推送（2026-08-16 推送纪律：脚本不推送）；报警通道 = 退出码唤醒主会话。
set -u
BASE=/Volumes/enkin/project/Agent
DIR="$BASE/benchmarks/baseline/20260816-overnight"
RESULTS="$BASE/backend/benchmarks/model_eval/results"
PROG="$DIR/progress.log"
PID="${1:?用法: watchdog.sh <campaign pid>}"
STALL_S=2400
DEADLINE_S=$((40 * 3600))
t_start=$(date +%s)
# progress.log 跨次启动追加，陈旧的 CAMPAIGN DONE 行会把"重启后中途死亡"伪装成善终
# （stage review M5，当天实撞）。记开跑基线行数，善终判据只认**增量行**里的 DONE。
prog_base=$(wc -l < "$PROG" 2>/dev/null || echo 0)

# push 已拆除（2026-08-16 推送纪律：脚本不推送；报警=退出唤醒主会话）

# 注意不要用 find -newermt 相对日期：BSD find 解析不了（实测 Invalid timestamp），
# 且它失败时 newest_mtime 恒空、停滞报警永不触发（fail-open）。两棵树 ~百级文件，全量 stat 足够便宜。
newest_mtime() {
  find "$DIR" "$RESULTS" -type f ! -name 'server_*.log' -exec stat -f %m {} + 2>/dev/null \
    | sort -n | tail -1
}

# 起手先验 PID 真活着：错 PID/已死 PID 直接当异常报，绝不装作"正常收尾"退 0
if ! kill -0 "$PID" 2>/dev/null; then
  echo "WATCHDOG BAD PID: pid=$PID 不存在或已死，战役处于无人看管状态"
  exit 5
fi

while kill -0 "$PID" 2>/dev/null; do
  sleep 300
  now=$(date +%s)
  last=$(newest_mtime)
  if [ -z "${last:-}" ]; then
    echo "WATCHDOG DEGRADED: newest_mtime 取空（find/stat 失败？），停滞判据失效，请人工核查"
    exit 4
  fi
  if [ $((now - last)) -gt $STALL_S ]; then
    echo "WATCHDOG STALL: $(( (now - last) / 60 ))min 无进度文件更新（campaign pid=$PID 仍存活）"
    exit 2
  fi
  if [ $((now - t_start)) -gt $DEADLINE_S ]; then
    echo "WATCHDOG TIMEOUT: 战役超 40h 未收尾（campaign pid=$PID 仍存活）"
    exit 3
  fi
done

# 进程退出 ≠ 正常收尾：必须在**本次开跑之后新增的行**里见到 DONE 才算善终
# （半夜 OOM-kill 与跑完长得一样；旧 DONE 行不算数）
if tail -n +"$((prog_base + 1))" "$PROG" 2>/dev/null | grep -q 'CAMPAIGN DONE'; then
  echo "WATCHDOG: campaign 正常收尾（见 CAMPAIGN DONE），看门狗收工"
  exit 0
fi
echo "WATCHDOG PREMATURE EXIT: campaign 进程退出但 progress.log 无 CAMPAIGN DONE——中途死亡"
exit 5
