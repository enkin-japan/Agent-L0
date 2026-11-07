# lc-hard-004 / lc-hard-008 恒败根因（主会话 2026-11-07 23:0x 自查，p2 五臂 × 12 run/题 + qwen-current 参照）
- 直调参照 qwen-current L0：004 通过 8 步（2/2）、008 通过 7 步（2/2），redundant_calls 0。
- 编排器臂（none/w1/w1w2/full，各 12 run）：004 步数众数 13（预算 11）、008 众数 12（预算 10）；redundant_calls 众数 3 / 2；
  判败理由主体是「步数超限」（004：none 6/12、w1 8/12、w1w2 8/12、full 5/12 为纯步数超限；008：none 8、w1 9、w1w2 9、full 6），
  这些 run 的 state_ok=True、final_text_ok=True（收尾答复里逐文件「verified by re-reading」）。
- 其余判败：回合上限耗尽仍在发调用 / 无收尾答复（每臂 1–3 run）、full 臂 1 run context_guard（39 步）；真正环境态错的只 none/w1/w1w2 各 1 run（004 三文件未改）。
- 结论：两题不是题坏、不是门槛定错，而是编排器路径比直调多约 5 步（3 次回读验证 + 其余），恰好撞步数预算。修掉冗余回读验证后 none 类 L0 可望 +2 题 → ≈73%，P2 门槛 (a) 70% 可达。
