#!/bin/bash
# GPU wired limit 管理（EXECUTION_LEDGER §7-0 ⑥，2026-08-13 生产机 M4 Max 64G）
#
# 背景：macOS 默认 GPU 可 wire 内存上限 ≈75% 物理内存（64G 机 ≈48G，
# iogpu.wired_limit_mb=0 表示默认）。31B-8bit 全栈足迹 ≈53-54G@64k(kv8)
# 会撞默认上限 → 换页悬崖。目标值 57344 MB (56G)：留 8G 给 macOS 与非 GPU 进程。
#
# 用法：
#   ./scripts/wired-limit.sh status     # 自检（免 sudo）：当前值 vs 目标值
#   sudo ./scripts/wired-limit.sh set       # 立即生效（重启后失效）
#   sudo ./scripts/wired-limit.sh install   # set + 装 LaunchDaemon 开机持久化
#   sudo ./scripts/wired-limit.sh uninstall # 卸 LaunchDaemon 并恢复默认(0)
set -euo pipefail

TARGET_MB=57344
PLIST=/Library/LaunchDaemons/dev.enkin.macagent.wiredlimit.plist
LABEL=dev.enkin.macagent.wiredlimit

current() { sysctl -n iogpu.wired_limit_mb; }

status() {
  local cur; cur=$(current)
  local total_mb=$(( $(sysctl -n hw.memsize) / 1024 / 1024 ))
  local eff=$cur
  [ "$cur" -eq 0 ] && eff=$(( total_mb * 3 / 4 ))
  echo "iogpu.wired_limit_mb = ${cur}（实效上限 ≈${eff} MB / 物理 ${total_mb} MB）"
  [ -f "$PLIST" ] && echo "持久化：已装 $PLIST" || echo "持久化：未装"
  if [ "$cur" -ge "$TARGET_MB" ]; then
    echo "✅ 达标（目标 ${TARGET_MB} MB）"
  else
    echo "⚠️ 未达标（目标 ${TARGET_MB} MB）——大档长上下文会撞 GPU wire 上限，跑："
    echo "   sudo $0 install"
    return 1
  fi
}

set_now() {
  sysctl -w iogpu.wired_limit_mb=$TARGET_MB
  echo "✅ 已设 ${TARGET_MB} MB（56G）。重启后失效；持久化用 install。"
}

install() {
  set_now
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/sbin/sysctl</string>
    <string>-w</string>
    <string>iogpu.wired_limit_mb=${TARGET_MB}</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
EOF
  chmod 644 "$PLIST"; chown root:wheel "$PLIST"
  launchctl bootstrap system "$PLIST" 2>/dev/null || launchctl load "$PLIST" 2>/dev/null || true
  echo "✅ LaunchDaemon 已装（开机自动重设）。自检：$0 status"
}

uninstall() {
  launchctl bootout system "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  sysctl -w iogpu.wired_limit_mb=0
  echo "✅ 已卸载并恢复默认(0)"
}

case "${1:-status}" in
  status) status ;;
  set) set_now ;;
  install) install ;;
  uninstall) uninstall ;;
  *) echo "用法: $0 {status|set|install|uninstall}"; exit 64 ;;
esac
