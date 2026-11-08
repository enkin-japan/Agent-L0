# lc-hard-004 / lc-hard-008 L0 逐 run：p2b（写后验证门控改文案 09c084f1 后）对 p2（改前）

_脚本：`benchmarks/data/context-knee-20261108-p2b/item004_008_evidence.py`（口径见脚本头）；同 KNEE_SEED=1107、同 (item, rep) 种子。_

直调参照 qwen-current L0：lc-hard-004 r0 步数 8 过 冗余 0；lc-hard-004 r1 步数 8 过 冗余 0；lc-hard-008 r0 步数 7 过 冗余 0；lc-hard-008 r1 步数 7 过 冗余 0；预算 lc-hard-004 11、lc-hard-008 10。

## 汇总（每格 6 run）

| 题 | 臂 | 批 | 过 | 步数众数 | 步数中位 | ≤预算 run | 纯步数败 | state_ok & final_text_ok | 写后回读 合计 / 众数 | 有写后回读的 run | shell 回读 合计 | redundant_calls 众数 | verify_nudge 合计 | 收尾含 re-read |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lc-hard-004 | none | p2 | 0/6 | 13 | 13 | 1 | 5 | 6 | 20 / 4 | 5 | 0 | 3 | 3 | 4 |
| lc-hard-004 | none | p2b | 6/6 | 8 | 8 | 6 | 0 | 6 | 0 / 0 | 0 | 0 | 0 | 0 | 0 |
| lc-hard-004 | full | p2 | 0/6 | 13 | 13 | 1 | 4 | 5 | 20 / 4 | 5 | 0 | 3 | 3 | 3 |
| lc-hard-004 | full | p2b | 3/6 | 9 | 10 | 4 | 1 | 4 | 4 / 0 | 1 | 0 | 0 | 0 | 0 |
| lc-hard-008 | none | p2 | 0/6 | 12 | 12 | 0 | 6 | 6 | 24 / 4 | 6 | 0 | 2 | 5 | 0 |
| lc-hard-008 | none | p2b | 4/6 | 8 | 8 | 4 | 2 | 6 | 7 / 0 | 2 | 0 | 0 | 0 | 0 |
| lc-hard-008 | full | p2 | 0/6 | 12 | 12 | 0 | 6 | 6 | 27 / 4 | 6 | 0 | 2 | 5 | 1 |
| lc-hard-008 | full | p2b | 3/6 | 11 | 11 | 3 | 2 | 5 | 7 / 0 | 2 | 1 | 0 | 0 | 0 |

## 附：全 10 题、两批同名臂 × 档（verify_nudge 事件 = guard_events_lines 切片；写后回读口径同上）

| 臂 | level | 批 | run | verify_nudge 事件 | 有 verify_nudge 的 run | 写后回读 合计 | 有写后回读的 run |
|---|---|---|---|---|---|---|---|
| none | 0 | p2 | 60 | 39 | 39 | 97 | 53 |
| none | 0 | p2b | 60 | 6 | 6 | 17 | 9 |
| none | 32768 | p2 | 20 | 10 | 10 | 25 | 14 |
| none | 32768 | p2b | 20 | 2 | 2 | 3 | 1 |
| none | 49152 | p2 | 20 | 11 | 11 | 25 | 11 |
| none | 49152 | p2b | 20 | 2 | 2 | 0 | 0 |
| none | 57344 | p2 | 20 | 8 | 8 | 19 | 10 |
| none | 57344 | p2b | 20 | 2 | 2 | 3 | 1 |
| w1 | 0 | p2 | 60 | 39 | 39 | 97 | 53 |
| w1 | 0 | p2b | 20 | 2 | 2 | 8 | 4 |
| w1 | 32768 | p2 | 20 | 10 | 10 | 25 | 14 |
| w1 | 32768 | p2b | 20 | 2 | 2 | 3 | 1 |
| w1 | 49152 | p2 | 20 | 8 | 8 | 24 | 12 |
| w1 | 49152 | p2b | 20 | 2 | 2 | 9 | 3 |
| w1 | 57344 | p2 | 20 | 5 | 5 | 20 | 10 |
| w1 | 57344 | p2b | 20 | 2 | 2 | 12 | 6 |
| w1w2 | 0 | p2 | 60 | 39 | 39 | 97 | 53 |
| w1w2 | 0 | p2b | 20 | 2 | 2 | 8 | 4 |
| w1w2 | 32768 | p2 | 20 | 10 | 10 | 25 | 14 |
| w1w2 | 32768 | p2b | 20 | 2 | 2 | 3 | 1 |
| w1w2 | 49152 | p2 | 20 | 9 | 9 | 28 | 13 |
| w1w2 | 49152 | p2b | 20 | 2 | 2 | 5 | 2 |
| w1w2 | 57344 | p2 | 20 | 5 | 5 | 24 | 11 |
| w1w2 | 57344 | p2b | 20 | 2 | 2 | 12 | 6 |
| full | 0 | p2 | 60 | 35 | 35 | 102 | 53 |
| full | 0 | p2b | 60 | 6 | 6 | 25 | 11 |
| full | 32768 | p2 | 20 | 11 | 11 | 33 | 16 |
| full | 32768 | p2b | 20 | 1 | 1 | 10 | 4 |
| full | 49152 | p2 | 20 | 7 | 7 | 32 | 11 |
| full | 49152 | p2b | 20 | 2 | 2 | 0 | 0 |
| full | 57344 | p2 | 20 | 9 | 9 | 39 | 10 |
| full | 57344 | p2b | 20 | 1 | 1 | 6 | 3 |

- p2 有 verify_nudge 的 run 按题（四臂全档合计）：lc-hard-001 41、lc-hard-003 16、lc-hard-004 12、lc-hard-005 37、lc-hard-006 31、lc-hard-007 7、lc-hard-008 33、lc-hard-009 38、lc-hard-010 40；合计 255 run、9 题
- p2b 有 verify_nudge 的 run 按题（四臂全档合计）：lc-hard-009 38；合计 38 run、1 题

## 逐 run

| 题 | 臂 | 批 | rep | 步数 / 预算 | 判 | state_ok / final_text_ok | 写后回读 | shell 回读 | redundant_calls | verify_nudge | 判败理由 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| lc-hard-004 | none | p2 | r0 | 13 / 11 | 败 | True / True | 4 | 0 | 3 | 1 | 步数超限: 13 > 11 |
| lc-hard-004 | none | p2 | r1 | 13 / 11 | 败 | True / True | 4 | 0 | 3 | 0 | 步数超限: 13 > 11 |
| lc-hard-004 | none | p2 | r2 | 9 / 11 | 败 | True / True | 0 | 0 | 0 | 1 | 回合上限 13 耗尽仍在发调用 |
| lc-hard-004 | none | p2 | r3 | 13 / 11 | 败 | True / True | 4 | 0 | 3 | 0 | 步数超限: 13 > 11 |
| lc-hard-004 | none | p2 | r4 | 13 / 11 | 败 | True / True | 4 | 0 | 3 | 0 | 步数超限: 13 > 11 |
| lc-hard-004 | none | p2 | r5 | 13 / 11 | 败 | True / True | 4 | 0 | 3 | 1 | 步数超限: 13 > 11 |
| lc-hard-004 | none | p2b | r0 | 9 / 11 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-004 | none | p2b | r1 | 8 / 11 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-004 | none | p2b | r2 | 8 / 11 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-004 | none | p2b | r3 | 8 / 11 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-004 | none | p2b | r4 | 8 / 11 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-004 | none | p2b | r5 | 8 / 11 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-004 | full | p2 | r0 | 13 / 11 | 败 | True / True | 4 | 0 | 3 | 1 | 步数超限: 13 > 11 |
| lc-hard-004 | full | p2 | r1 | 13 / 11 | 败 | True / True | 4 | 0 | 3 | 0 | 步数超限: 13 > 11 |
| lc-hard-004 | full | p2 | r2 | 9 / 11 | 败 | True / True | 0 | 0 | 0 | 1 | 回合上限 13 耗尽仍在发调用 |
| lc-hard-004 | full | p2 | r3 | 13 / 11 | 败 | True / True | 4 | 0 | 3 | 1 | 步数超限: 13 > 11 |
| lc-hard-004 | full | p2 | r4 | 13 / 11 | 败 | True / False | 4 | 0 | 3 | 0 | 步数超限: 13 > 11；回合上限 13 耗尽仍在发调用；无收尾答复 |
| lc-hard-004 | full | p2 | r5 | 13 / 11 | 败 | True / True | 4 | 0 | 3 | 0 | 步数超限: 13 > 11 |
| lc-hard-004 | full | p2b | r0 | 13 / 11 | 败 | True / False | 0 | 0 | 0 | 0 | 步数超限: 13 > 11；回合上限 13 耗尽仍在发调用；无收尾答复 |
| lc-hard-004 | full | p2b | r1 | 13 / 11 | 败 | True / True | 4 | 0 | 3 | 0 | 步数超限: 13 > 11 |
| lc-hard-004 | full | p2b | r2 | 9 / 11 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-004 | full | p2b | r3 | 10 / 11 | 败 | True / False | 0 | 0 | 1 | 0 | 回合上限 13 耗尽仍在发调用；无收尾答复 |
| lc-hard-004 | full | p2b | r4 | 9 / 11 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-004 | full | p2b | r5 | 9 / 11 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-008 | none | p2 | r0 | 12 / 10 | 败 | True / True | 4 | 0 | 2 | 1 | 步数超限: 12 > 10 |
| lc-hard-008 | none | p2 | r1 | 12 / 10 | 败 | True / True | 4 | 0 | 2 | 0 | 步数超限: 12 > 10 |
| lc-hard-008 | none | p2 | r2 | 12 / 10 | 败 | True / True | 4 | 0 | 2 | 1 | 步数超限: 12 > 10 |
| lc-hard-008 | none | p2 | r3 | 12 / 10 | 败 | True / True | 4 | 0 | 2 | 1 | 步数超限: 12 > 10 |
| lc-hard-008 | none | p2 | r4 | 12 / 10 | 败 | True / True | 4 | 0 | 2 | 1 | 步数超限: 12 > 10 |
| lc-hard-008 | none | p2 | r5 | 12 / 10 | 败 | True / True | 4 | 0 | 2 | 1 | 步数超限: 12 > 10 |
| lc-hard-008 | none | p2b | r0 | 12 / 10 | 败 | True / True | 4 | 0 | 2 | 0 | 步数超限: 12 > 10 |
| lc-hard-008 | none | p2b | r1 | 8 / 10 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-008 | none | p2b | r2 | 8 / 10 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-008 | none | p2b | r3 | 8 / 10 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-008 | none | p2b | r4 | 11 / 10 | 败 | True / True | 3 | 0 | 2 | 0 | 步数超限: 11 > 10 |
| lc-hard-008 | none | p2b | r5 | 8 / 10 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-008 | full | p2 | r0 | 17 / 10 | 败 | True / True | 4 | 0 | 2 | 0 | 步数超限: 17 > 10 |
| lc-hard-008 | full | p2 | r1 | 12 / 10 | 败 | True / True | 4 | 0 | 2 | 1 | 步数超限: 12 > 10 |
| lc-hard-008 | full | p2 | r2 | 12 / 10 | 败 | True / True | 4 | 0 | 2 | 1 | 步数超限: 12 > 10 |
| lc-hard-008 | full | p2 | r3 | 12 / 10 | 败 | True / True | 4 | 0 | 2 | 1 | 步数超限: 12 > 10 |
| lc-hard-008 | full | p2 | r4 | 15 / 10 | 败 | True / True | 7 | 0 | 5 | 1 | 步数超限: 15 > 10 |
| lc-hard-008 | full | p2 | r5 | 12 / 10 | 败 | True / True | 4 | 0 | 2 | 1 | 步数超限: 12 > 10 |
| lc-hard-008 | full | p2b | r0 | 11 / 10 | 败 | True / False | 0 | 0 | 0 | 0 | 步数超限: 11 > 10；回合上限 12 耗尽仍在发调用；无收尾答复 |
| lc-hard-008 | full | p2b | r1 | 8 / 10 | 过 | True / True | 0 | 0 | 0 | 0 | — |
| lc-hard-008 | full | p2b | r2 | 12 / 10 | 败 | True / True | 4 | 0 | 2 | 0 | 步数超限: 12 > 10 |
| lc-hard-008 | full | p2b | r3 | 9 / 10 | 过 | True / True | 0 | 1 | 0 | 0 | — |
| lc-hard-008 | full | p2b | r4 | 11 / 10 | 败 | True / True | 3 | 0 | 2 | 0 | 步数超限: 11 > 10 |
| lc-hard-008 | full | p2b | r5 | 7 / 10 | 过 | True / True | 0 | 0 | 0 | 0 | — |
