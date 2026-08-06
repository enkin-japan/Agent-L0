#!/usr/bin/env python3
"""Colab GPU 保活：长时间不用 GPU 时挂着，防运行时被判空闲回收。

原理：每 30s 做一次小矩阵乘（16MB 张量，占用与算力都可忽略），让 Colab 的
GPU 利用率检测始终见到心跳。与 vLLM 常驻共存无碍（显存占用 <100MB，
gpu_memory_utilization 0.92 的余量足够）；重活（引擎装载/评测）前建议先停。

用法（bootstrap 之外手工管理）：
  起：nohup python3 /content/agent/colab/gpu_keepalive.py > /content/keepalive.log 2>&1 &
      echo $! > /content/gpu_keepalive.pid
  停：kill "$(cat /content/gpu_keepalive.pid)" && rm -f /content/gpu_keepalive.pid
（用 pidfile 而非 pkill —— pkill -f 模式字面量自匹配已有四案教训。）
"""
import time

import torch

assert torch.cuda.is_available(), "无 CUDA 设备——保活无意义"
a = torch.rand(2048, 2048, device="cuda")
print("gpu_keepalive 上岗：每 30s 一次 16MB matmul 心跳", flush=True)
n = 0
while True:
    (a @ a).sum().item()
    n += 1
    if n % 120 == 0:  # 每小时留一行痕迹，日志不涨肚
        print(f"心跳 {n} 次", flush=True)
    time.sleep(30)
