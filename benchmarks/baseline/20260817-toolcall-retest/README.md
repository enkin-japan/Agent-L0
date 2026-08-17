# gemma tool_call 架构锅修复重测（2026-08-17）

带点工具名解析修复（commit a61e27e）后，D.20 保留四档 × tool_call 维度重测。
结论：解析错四档归零；pass e4b-8bit 0.64→0.88、26b-4bit/8bit 0.72→0.92、
31b-4bit 0.72→0.96。详见 PROFILES.md「08-17 重测节」与台账第一梯队条。

## 调用记录（阶段复审 m6 补）

```bash
# backend/ 下，逐档顺序执行（scratchpad 驱动脚本 retest_gemma_toolcall.sh，
# 会话级临时件未入库；等价命令如下）
PYTHONUNBUFFERED=1 .venv/bin/python benchmarks/model_eval/run_eval.py \
    --model <短名> --dims tool_call
# 短名依次：gemma-4-e4b-it-8bit / gemma-4-26b-a4b-it-4bit /
#           gemma-4-26b-a4b-it-8bit / gemma-4-31b-it-4bit
```

全程 00:47:04 → 00:52:31（5 分 27 秒，含逐档模型装载）；引擎直连（inprocess
mlx），无 server；工作树 = commit a61e27e。

## 件清单

- `<模型>.<run戳>.summary.json` / `.transcripts.jsonl` —— **修复后**四档重测
  （runs 20260817-004704 / 004908 / 005007 / 005105）。
- `<模型>.<run戳>.pre-fix.transcripts.jsonl` —— **修复前**（08-16 全库横评）
  同四档的逐题原始件（阶段复审 M3 补档：头号结论「12 例解析失败全数为带点
  工具名」的可复算依据；runs 20260816-034903 / 022318 / 022711 / 025147）。
  聚合值另见 `../20260816-overnight/q_summaries/`。
