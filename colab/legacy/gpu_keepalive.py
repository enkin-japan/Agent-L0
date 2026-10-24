#!/usr/bin/env python3
"""Colab GPU 保活（2026-08-08 纪律版）：防用户侧看门 cell 的「25min 利用率为零
即注销」误伤正在进行的无 GPU 阶段（权重 rsync/pip/盘操作）。

用法：python3 gpu_keepalive.py [hold_gb]
  hold_gb 缺省 5.0 —— 无引擎在卡时用（5GB 驻留 + 占空比算力心跳）；
  引擎常驻期间用 0.5 —— 只保利用率心跳，别挤 vLLM 0.92 的启动/运行余量。

纪律（与持久记忆 colab-gpu-keepalive 同步）：
  · 工作进行中且 GPU 利用率会归零的窗口 → 挂本程序；
  · 阶段结束向用户汇报时 → 停掉本程序（清空占用）：用户未响应时其看门 cell
    25min 后自动注销运行时，杜绝空烧 unit；
  · 起停一律 pidfile（pkill -f 自匹配已五案）：
    起：nohup python3 gpu_keepalive.py [gb] > /content/keepalive.log 2>&1 &
        （随后独立 ssh 写 pidfile：pgrep -f "python3 .*gpu_keepalive[.]py" > pid）
    停：kill "$(cat /content/gpu_keepalive.pid)"
心跳形态：每 5s 做 ~0.3s 连续 matmul（4096²），nvidia-smi 采样窗内利用率非零。
"""
import sys
import time

import torch

assert torch.cuda.is_available(), "无 CUDA 设备——保活无意义"
hold_gb = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
n_hold = max(1, int(hold_gb * (1 << 30) / 4))
hold = torch.empty(n_hold, dtype=torch.float32, device="cuda")  # 驻留占位
a = torch.rand(4096, 4096, device="cuda")
print(f"gpu_keepalive 上岗：驻留 {hold_gb:.1f}GB + 每 5s 约 0.3s matmul 心跳",
      flush=True)
n = 0
while True:
    t0 = time.time()
    while time.time() - t0 < 0.3:
        a = (a @ a)
        a = a / a.norm().clamp_min(1e-6)    # 防溢出，保持数值有界
    torch.cuda.synchronize()
    n += 1
    if n % 720 == 0:                        # 每小时一行痕迹
        print(f"心跳 {n} 轮", flush=True)
    time.sleep(5)
