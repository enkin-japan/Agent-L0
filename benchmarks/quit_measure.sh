#!/bin/bash
# 2026-10-29 ⌘Q 退出耗时实测（计划 §17.7）：启动已装的 /Applications/MacAgent.app，按模式等就绪，
# 再用 osascript quit 计时到进程消失。原始输出见 benchmarks/reports/2026-10-29-quit-timing.md。
# 用法：benchmarks/quit_measure.sh [full|early|searx-spawning]
L="$HOME/Library/Application Support/dev.enkin.macagent/logs"
pgrep -x MacAgent >/dev/null && { echo "app already running; abort"; exit 1; }
MODE="${1:-full}"   # full: 等三个服务全就绪 + 8 s（sidecar 已 running）；early: backend 一就绪就退（启动中）
open -a /Applications/MacAgent.app; t0=$(date +%s); ready=0
for i in $(seq 1 200); do
  if [ "$MODE" = early ]; then
    lsof -nP -iTCP:14159 -sTCP:LISTEN >/dev/null 2>&1 && { ready=1; break; }
  elif [ "$MODE" = searx-spawning ]; then
    [ -f "$L/searx.pid" ] && ! lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1 && { ready=1; break; }
  else
    n=$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -cE ':14159|:14160|:8080'); [ "$n" -ge 3 ] && { ready=1; break; }
  fi
  sleep 0.1
done
echo "mode=$MODE ready=$ready after $(( $(date +%s) - t0 )) s"; [ "$MODE" = full ] && sleep 8 || sleep 0.3
python3 - <<'PY'
import subprocess, time
t0=time.monotonic()
subprocess.run(["osascript","-e",'tell application "MacAgent" to quit'],check=False)
while subprocess.run(["pgrep","-x","MacAgent"],capture_output=True).returncode==0:
    time.sleep(0.05)
print(f"QUIT_ELAPSED={time.monotonic()-t0:.2f}s")
PY
sleep 1; echo "listeners after quit: $(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -cE ':14159|:14160|:8080')"
grep -nE "Shutting|Waiting for connections|Cancel|shutdown complete|Finished" "$L/backend.log" | head -8; echo "--- sidecar-down.log tail"; tail -12 "$L/sidecar-down.log" 2>/dev/null | cut -c1-180; echo "--- pidfiles: $(ls "$L"/*.pid 2>/dev/null | xargs -n1 basename 2>/dev/null | tr "\n" " ")"
