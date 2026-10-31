#!/bin/bash
# 长任务后台监视循环——主会话用 Bash `run_in_background: true` 直接起它，
# 它退出时 harness 唤醒的就是主会话本身，中间没有任何模型（用户 2026-10-31 拍板，
# 取代「挂 task-sentinel 盯梢」：两次哨兵把循环用裸 & 脱管后弃岗，主会话空等数小时）。
#
# 用法：
#   scripts/watch_until.sh [--interval SEC] [--deadline SEC] [--stall SEC]
#       [--pid N | --pidfile F]        进程验活（消失 → EXIT_DEAD；pidfile 优先于 --pid）
#       [--log F]                      只读增量行，匹配 --done / --fail
#       [--cmd 'CMD']                  每轮执行，整段输出匹配 --done / --fail
#       [--file F]...                  进度承载文件；全部超过 --stall 秒无更新 → EXIT_STALL
#       [--done REGEX] [--fail REGEX]  grep -E 语法
#   SEC 必须是非负整数；--interval ≥ 1（默认 180）；--deadline 默认 21600（6 h），0 = 不设。
#
# 输出：恰好一行 `EXIT_<STATE> <证据>`，然后退出。
# 退出码：DONE=0  FAIL=1  DEAD=2  STALL=3  TIMEOUT=4  用法错误=64
# 判定顺序：done → fail → dead → stall → timeout；进程消失时再读一次增量，收尾标志已出现则按 DONE。
# 第一轮不睡，立即检查；首轮把日志现有内容整段当增量（已 READY 的任务当场返回；
# 同一日志重跑前请清空或换文件，否则旧收尾行会被当场命中）。

set -u

interval=180; deadline=21600; stall=0; pid=""; pidfile=""; log=""; cmd=""; done_re=""; fail_re=""
files=()
usage() { sed -n '2,/^$/p' "$0" >&2; exit "${1:-64}"; }
need() { [ $# -ge 2 ] || { echo "$1 needs a value" >&2; usage; }; }
num() { [[ "$2" =~ ^[0-9]+$ ]] || { echo "$1 must be a non-negative integer, got '$2'" >&2; usage; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --interval) need "$@"; num "$1" "$2"; interval=$2; shift 2;;
    --deadline) need "$@"; num "$1" "$2"; deadline=$2; shift 2;;
    --stall) need "$@"; num "$1" "$2"; stall=$2; shift 2;;
    --pid) need "$@"; num "$1" "$2"; pid=$2; shift 2;;
    --pidfile) need "$@"; pidfile=$2; shift 2;;
    --log) need "$@"; log=$2; shift 2;;
    --cmd) need "$@"; cmd=$2; shift 2;;
    --file) need "$@"; files+=("$2"); shift 2;;
    --done) need "$@"; done_re=$2; shift 2;;
    --fail) need "$@"; fail_re=$2; shift 2;;
    -h|--help) usage 0;;
    *) echo "unknown arg: $1" >&2; usage;;
  esac
done
[ "$interval" -ge 1 ] || { echo "--interval must be >= 1" >&2; usage; }
[ -z "$log$cmd$pid$pidfile" ] && [ ${#files[@]} -eq 0 ] && { echo "nothing to watch" >&2; usage; }
[ -n "$log" ] && files+=("$log")

if stat -c %Y . >/dev/null 2>&1; then mtime() { stat -c %Y "$1" 2>/dev/null || echo 0; }
else mtime() { stat -f %m "$1" 2>/dev/null || echo 0; }; fi
alive() { ps -p "$1" >/dev/null 2>&1; }   # 不用 kill -0：对不属于自己的 pid 会 EPERM 误判为死
trim() { tr '\n' ' ' | cut -c1-200; }

start=$(date +%s); last_lines=0
read_probe() {   # 日志增量（按 awk 行计，末行无换行也算）+ 命令输出 → $probe
  local incr="" out="" total
  if [ -n "$log" ] && [ -f "$log" ]; then
    total=$(awk 'END{print NR}' "$log")
    [ "$total" -gt "$last_lines" ] && incr=$(tail -n +"$((last_lines + 1))" "$log")
    last_lines=$total
  fi
  [ -n "$cmd" ] && out=$(bash -c "$cmd" 2>&1 || true)
  probe=$(printf '%s\n%s' "$incr" "$out")
}
check_patterns() {   # 命中 done → exit 0；命中 fail → exit 1
  local hit
  if [ -n "$done_re" ] && hit=$(printf '%s' "$probe" | grep -E -m1 -- "$done_re"); then
    echo "EXIT_DONE $(printf '%s' "$hit" | trim)"; exit 0; fi
  if [ -n "$fail_re" ] && hit=$(printf '%s' "$probe" | grep -E -m1 -- "$fail_re"); then
    echo "EXIT_FAIL $(printf '%s' "$hit" | trim)"; exit 1; fi
}

while :; do
  now=$(date +%s)
  read_probe; check_patterns

  p=$pid; [ -n "$pidfile" ] && p=$(cat "$pidfile" 2>/dev/null || true)
  if [ -n "$p" ] && ! alive "$p"; then
    read_probe; check_patterns   # 进程在上一次读取后正常收尾的窗口
    echo "EXIT_DEAD pid=$p gone after $((now - start))s"; exit 2; fi

  if [ "$stall" -gt 0 ] && [ ${#files[@]} -gt 0 ]; then
    newest=0
    for f in "${files[@]}"; do m=$(mtime "$f"); [ "$m" -gt "$newest" ] && newest=$m; done
    if [ "$newest" -gt 0 ] && [ $((now - newest)) -ge "$stall" ]; then
      echo "EXIT_STALL no file updated for $((now - newest))s (threshold ${stall}s)"; exit 3; fi
  fi

  if [ "$deadline" -gt 0 ] && [ $((now - start)) -ge "$deadline" ]; then
    echo "EXIT_TIMEOUT ${deadline}s elapsed"; exit 4; fi
  sleep "$interval"
done
