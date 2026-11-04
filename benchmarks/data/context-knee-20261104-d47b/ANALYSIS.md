# D47-next (b)+(a) 验收批次：复算与结论（2026-11-04）

> 原始数据：本目录 `trace-d47b-both/`（new 模板 / 900 tok / 工具段 200 / turn 1 迭代 6 / drop_task on / task_state on；`transcripts.jsonl` 60 行、`meta.json`、`guardrail_events.jsonl`、`sessions/` 90 个会话目录；链日志 `run_colab.log` 10:54 起机 → 11:06 READY → 11:07–11:28 本臂 → 11:28 停机 rc=0，`BATCH_RC=0 SKIPPED=0`）。对照 CTRL-1 = `../context-knee-20261103-d47/trace-d47-both/`（44ca2aa：轮首种任务、纯 [Goal] 盖章 30/30），CTRL-2 = `../context-knee-20261103-d45/trace-d45-both/`（种任务之前的代码）。三批 meta 除 stamp 外逐键相同（A 节）；同模型 Qwen3.8-27B-INT4，同题集 lc-hard-001…010 × 3 rep。后端都是 remote，meta 的 seed 7 不作用于采样：同 (题, rep) 的跨批配对只是标签对齐，rep 间可交换。
> 本批代码 = 55cff85：(b) 纯 [Goal]（无 plan 无便签）不盖 task_state；(a) REMEMBER_SCHEMA 描述加「You do not need a plan to use it …」、no_plan_nudge 文案末尾加「Either way, first save with remember any value from these results that you will need later.」。
> 交叉核对：harness 自带汇总（`trace-d47b-both.log` 末表）pass PRE 0.63 / POST 0.43、重复功 0.47 → 3.20、Δ 2.73 [1.33, 3.97]、turn1 remember 0、有便签对数 0，与 D 节一致；CTRL-1 − CTRL-2 各格与 `../context-knee-20261103-d47/ANALYSIS.md` C 节逐格相同（−10.0 pt [−20.0, +0.0]、+0.77 [+0.10, +1.43] 等）。CI 均为题级 bootstrap 95%（B=1000，seed 0），n = 10 题，区间宽，如实给出。

## 1. 结论

1. **判据不达标：turn 1 的 remember 调用 0 次 / 0 of 30 对（CTRL-1 也是 0/30）。**（rule of three：n=30 全 0 在 95% 置信水平下只能排除真实逐轮 remember 率高于 ≈10% 的假设，不能排除更低的非零率。）`turn1_task_state_has_scratchpad` 0/30；`turn1_plan_built` 0/30；t1 history 里 remember / update_plan 调用都是 0（B 节）。(a) 的提醒文案确实落到了数据里：30/30 个 t1 会话各有 1 条 no_plan_nudge，文案 30/30 含「first save with remember」（CTRL-1 / CTRL-2 为 0，A 节）。提醒之后的第一步：直接作答 15、write_file 12、delete_file 3、remember 0——**三批分布完全相同**（B 节），即新增的那句对提醒后的下一步没有可见作用。模型也没有在别处把 remember「说出来」：三批全部的 turn 1 assistant 文本/思考与全部续跑轮 assistant 思考里，"remember" / "scratch" / "便签" 均出现 0 次（grep，534 + 1068 条 assistant 消息）。REMEMBER_SCHEMA 描述的变化不在任何落盘产物里，无法从数据核验，只能以代码为准。
2. **续跑轮里出现 1 次 remember（CTRL-1 / CTRL-2 均为 0）：** lc-hard-004 r1 的 POST 续跑轮，该轮触发了 no_plan_nudge 并被 turn_abort（回合上限 5 耗尽仍在发调用），score False。它发生在 POST 摘要生成之后，对摘要无影响；便签内容不在任何落盘产物里（`cont_turns` 不记元工具调用；journal 几乎也不记——三批 270 个会话 `journal.jsonl` 里唯一一条 `plan_updated` 事件在 `lc-hard-004-r1-8a66d2-post/journal.jsonl` seq 382（`{"task": null}`，轮末），与这次 remember 同轮但不记便签内容；第 5 次生成的非元工具调用数为 0，与「最后一次生成就是这次 remember」相符，但这是推断）。三批续跑轮里共有 3 + 10 + 5 行触发 no_plan_nudge，其后跟了 remember 的只有这 1 行。
3. **问题 2（便签进摘要器输入 / [Key values]）不可算：** 摘要器输入含 `[Scratchpad]` 0/30，分母为 0，不是命中率 0（C 节）。
4. **(b) 按设计生效：** 摘要器输入含 `[Task state at end of turn]` 0/30（CTRL-1 30/30，CTRL-2 0/30），`turn1_record_has_task_state` 0/30。summary_input_chars 中位（statistics.median）1,846（CTRL-1 2,126.5 / CTRL-2 1,777.5）；去掉 task_state 段后的中位（同一定义）1,846 / 1,817 / 1,777.5（E 节）——本批与 CTRL-2 两支全程无段，全量中位与去段后中位相等；CTRL-1 的全量中位高出去段后中位 309.5 字，就是那一段本身的大小——形状回到 CTRL-2。POST 摘要 [Goal] 逐字等于用户原文（exact）1/30（CTRL-1 22/30，CTRL-2 0/30），含用户原文子串（quote-stripped / substring）3/30（CTRL-1 24/30，CTRL-2 4/30），[Goal] 字符中位 92（113 / 86）：D47 的「逐字抄目标」间接效应随段一起消失。
5. **pass / 重复功：本批与 CTRL-2 无可分辨差别；相对 CTRL-1 的差是 CTRL-1 − CTRL-2 的镜像，方向有利但不能归因。** PRE / POST pass 19/30 / 13/30（CTRL-1 17/10，CTRL-2 17/13）。NEW − CTRL-1：POST pass +10.0 pt [+0.0, +23.3]，gap 之差 +3.3 pt [−13.3, +26.7]，Δ重复功之差 −0.87 [−1.70, −0.07]；调用序列相同的 14 对 / 6 题上 POST pass +0.0 pt [+0.0, +0.0]，Δ重复功之差 −0.44 [−1.89, +1.11]（跨 0）。NEW − CTRL-2：POST pass +0.0 pt [−13.3, +13.3]，gap 之差 −6.7 pt [−26.7, +16.7]，Δ重复功之差 −0.10 [−1.03, +0.83]；调用序列相同的 16 对 / 8 题：POST pass −6.2 pt [−18.8, +0.0]，Δ重复功之差 +0.19 [−0.62, +1.23]。完整 turn 1 响应逐字相同 0/30（两组对照都是），D 节。
6. **完整性与预算：** 60 行、error 0、配对 30、空摘要 0；turn1_status success 18 / warning 12（三批计数相同）；task_state 段不存在，3,000 字截断与 [Goal] 300 字截断都无从触发（不是「触发 0 次」）；输入 > 24,000 字 0（A、E 节）。
7. **顺带（非本批目标）：** 日语漂移照旧出现在 zh 题——turn 1 最终答复含假名 3/30、cont_response 含假名 PRE 3 / POST 3（CTRL-1 3 / 2 / 2，CTRL-2 2 / 2 / 2），POST 摘要含假名 0（F 节）。

**供拍板的输入（不是决定）：** 靠模型合作写便签这条路在三批 90 次 turn 1 里为 0 次（三个 0 不是同一口径：d47b-both / d47-both 的 0 同时由 `turn1_remember_calls` 计数器与 t1 history 直数两条路径给出，CTRL-2（d45-both）跑的是改描述之前的 remember 文案且无 plan 时会拒绝 remember，其 0 只来自 t1 history 直数——`turn1_remember_calls` 等字段在该批缺键，三批的 0 不是同一条件下的 0），改描述 + 提醒点名 remember 也没有让它动；D47c（无 plan 时轮内注入便签块）的前提仍不成立。(b) 无可测代价、把摘要器输入还原成 CTRL-2 形状，可单独保留；若还要便签，剩下的候选是 D47 ANALYSIS §2 记的「编排器从工具结果自动抽关键值」，它不依赖模型调用 remember。

## 2. 表（自动部分）

### 自动部分（`benchmarks/summary_trace/analyze_d47b.py`，seed 0，B=1000）

### A. 完整性（transcripts.jsonl / meta.json / guardrail_events.jsonl / t1 会话 history.jsonl）

| 批 | 目录 | 行 | error 行 | 配对 | 空摘要（POST） | turn1_status | cont_status PRE / POST | stamp | meta 除 stamp 外与 d47b 相同 |
|---|---|---|---|---|---|---|---|---|---|
| d47b-both | `benchmarks/data/context-knee-20261104-d47b/trace-d47b-both` | 60 | 0 | 30 | 0 | {'success': 18, 'warning': 12} | {'success': 26, 'warning': 4} / {'success': 26, 'warning': 4} | 20261104-110741 | True |
| d47-both | `benchmarks/data/context-knee-20261103-d47/trace-d47-both` | 60 | 0 | 30 | 0 | {'success': 18, 'warning': 12} | {'success': 30} / {'success': 24, 'warning': 6} | 20261103-212444 | True |
| d45-both | `benchmarks/data/context-knee-20261103-d45/trace-d45-both` | 60 | 0 | 30 | 0 | {'success': 18, 'warning': 12} | {'success': 26, 'warning': 4} / {'success': 28, 'warning': 2} | 20261103-161140 | True |

guardrail_events.jsonl 事件计数（会话后缀 `t1` / `pre` / `post`）：

| 批 | 事件总数 | 按 (type, 会话后缀) |
|---|---|---|
| d47b-both | 89 | action_claim_nudge@post=5, action_claim_nudge@pre=8, duplicate_call@t1=5, no_plan_nudge@post=2, no_plan_nudge@pre=1, no_plan_nudge@t1=30, turn_abort@post=1, turn_abort@t1=12, verify_nudge@t1=25 |
| d47-both | 104 | action_claim_nudge@post=5, action_claim_nudge@pre=11, duplicate_call@t1=4, no_plan_nudge@post=9, no_plan_nudge@pre=1, no_plan_nudge@t1=30, turn_abort@post=3, turn_abort@t1=12, verify_nudge@post=3, verify_nudge@pre=1, verify_nudge@t1=25 |
| d45-both | 97 | action_claim_nudge@post=8, action_claim_nudge@pre=10, duplicate_call@t1=5, no_plan_nudge@post=5, no_plan_nudge@t1=30, turn_abort@post=1, turn_abort@t1=12, verify_nudge@post=1, verify_nudge@t1=25 |

turn 1 no_plan_nudge 文案（t1 history.jsonl 里含 `update_plan NOW` 的 system 消息；(a) 是否落到数据里）：

| 批 | 含提醒的 t1 会话 | 提醒条数 | 不同文案数 | 含「first save with remember」 |
|---|---|---|---|---|
| d47b-both | 30/30 | 30 | 1 | 30 |
| d47-both | 30/30 | 30 | 1 | 0 |
| d45-both | 30/30 | 30 | 1 | 0 |

- REMEMBER_SCHEMA 描述（(a) 的另一半）不在任何落盘产物里（t1 history.jsonl / journal.jsonl / transcripts 都不记工具声明），本脚本无法从数据核验它；只能以代码（HEAD 的 `model_visible_text.snap`）为准。

### B. 判据：turn 1 的 remember 调用率（turn 1 每对一次，取 PRE 行；字段来自 run_trace.py:324-337）

| 批 | turn1_remember_calls 总数 | remember ≥1 的对 | turn1_task_state_has_scratchpad | turn1_record_has_task_state | turn1_task_state_chars min/p25/中位/p75/max | turn1_plan_built | t1 history 里元工具调用（remember / update_plan） |
|---|---|---|---|---|---|---|---|
| d47b-both | 0 | 0 | 0 | {'False': 30} | 0 / 0 / 0 / 0 / 0 | {'False': 30} | remember=0, update_plan=0 |
| d47-both | 0 | 0 | 0 | {'True': 30} | 73 / 96 / 120 / 283 / 308 | {'False': 30} | remember=0, update_plan=0 |
| d45-both | 缺键 | 缺键 | 缺键 | {'False': 30} | 缺键 | {'False': 30} | remember=0, update_plan=0 |

- 注：d47b 的 `turn1_record_has_task_state` 只在有 plan 或有便签时为 True（纯 [Goal] 不盖章），与 d47 的 30/30（每轮都盖 [Goal]）按构造不可比。

turn 1 每条 no_plan_nudge 之后第一条 assistant 消息的形态（text = 无工具调用直接作答）：

| 批 | 提醒后第一步 | 其中含 remember |
|---|---|---|
| d47b-both | {'calls:delete_file': 3, 'calls:write_file': 12, 'text': 15} | 0 |
| d47-both | {'calls:delete_file': 3, 'calls:write_file': 12, 'text': 15} | 0 |
| d45-both | {'calls:delete_file': 3, 'calls:write_file': 12, 'text': 15} | 0 |

续跑支（PRE / POST 续跑轮）的元工具调用（行字段 meta_calls）与该轮 no_plan_nudge（行字段 guard_events）：

| 批 | 支 | meta_calls 合计 | 有 no_plan_nudge 的行 | 这些行里 meta_calls 含 remember | 有 remember 的行（题 rep: guard_events / score） |
|---|---|---|---|---|---|
| d47b-both | pre | {} | 1 | 0 | — |
| d47b-both | post | {'remember': 1} | 2 | 1 | 004r1: {'no_plan_nudge': 1, 'turn_abort': 1} / score=False |
| d47-both | pre | {} | 1 | 0 | — |
| d47-both | post | {} | 9 | 0 | — |
| d45-both | pre | {} | 0 | 0 | — |
| d45-both | post | {} | 5 | 0 | — |

### C. 便签是否进摘要器输入与 POST [Key values]（POST 行 summary_input / summary）

| 批 | 摘要器输入含 `[Task state at end of turn]` | 其中含 `[Scratchpad]` | 其中含 `[Plan]` | 段节头构成（按对） | 便签值逐字进 [Key values] |
|---|---|---|---|---|---|
| d47b-both | 0/30 | 0 | 0 | {'无段': 30} | **不可算**（0 对带 `[Scratchpad]`，分母为 0） |
| d47-both | 30/30 | 0 | 0 | {'Goal': 30} | **不可算**（0 对带 `[Scratchpad]`，分母为 0） |
| d45-both | 0/30 | 0 | 0 | {'无段': 30} | **不可算**（0 对带 `[Scratchpad]`，分母为 0） |

### D. pass 与重复功：每批汇总与跨批差（按题；正 = 前者更高；pass 单位百分点；同 (题, rep) 只是标签对齐）

| 批 | pass PRE | pass POST | POST−PRE pass（题级 95%） | 重复功 PRE→POST 均 | Δ重复功（题级 95%） | 首 prompt 中位 PRE / POST | 摘要字符中位 | turn1_calls 中位 |
|---|---|---|---|---|---|---|---|---|
| d47b-both | 19/30 | 13/30 | -20.0 pt [-40.0, -3.3] | 0.47 → 3.20 | +2.73 [+1.33, +3.97] | 2327.0 / 1537.0 | 603.5 | 9.0 |
| d47-both | 17/30 | 10/30 | -23.3 pt [-43.3, -6.7] | 0.73 → 4.33 | +3.60 [+2.40, +4.90] | 2307.5 / 1485.0 | 625.0 | 9.0 |
| d45-both | 17/30 | 13/30 | -13.3 pt [-30.0, +0.0] | 0.67 → 3.50 | +2.83 [+1.60, +4.10] | 2338.0 / 1469.5 | 563.5 | 9.5 |

| 对照 | 子集 | 对 / 题 | POST pass 差 | PRE pass 差 | POST−PRE pass gap 之差 | Δ重复功之差 | 子集内 POST pass 前 / 后 | 子集内 Δ重复功均 前 / 后 |
|---|---|---|---|---|---|---|---|---|
| d47b-both − d47-both | 全部 | 30 / 10 | +10.0 pt [+0.0, +23.3] | +6.7 pt [-6.7, +20.0] | +3.3 pt [-13.3, +26.7] | -0.87 [-1.70, -0.07] | 13/30 / 10/30 | +2.73 / +3.60 |
| d47b-both − d47-both | turn 1 宽口径一致 | 15 / 7 | +14.3 pt [+0.0, +42.9] | -9.5 pt [-42.9, +14.3] | +23.8 pt [-14.3, +85.7] | -0.38 [-1.62, +0.86] | 5/15 / 4/15 | +2.67 / +3.40 |
| d47b-both − d47-both | 调用序列相同 | 14 / 6 | +0.0 pt [+0.0, +0.0] | +5.6 pt [+0.0, +16.7] | -5.6 pt [-16.7, +0.0] | -0.44 [-1.89, +1.11] | 4/14 / 4/14 | +2.79 / +3.57 |
| d47b-both − d47-both | 调用序列相同且 turn1_status 相同 | 12 / 6 | +0.0 pt [+0.0, +0.0] | +5.6 pt [+0.0, +16.7] | -5.6 pt [-16.7, +0.0] | -0.47 [-1.92, +1.08] | 3/12 / 3/12 | +2.92 / +3.92 |
| d47b-both − d45-both | 全部 | 30 / 10 | +0.0 pt [-13.3, +13.3] | +6.7 pt [-13.3, +33.3] | -6.7 pt [-26.7, +16.7] | -0.10 [-1.03, +0.83] | 13/30 / 13/30 | +2.73 / +2.83 |
| d47b-both − d45-both | turn 1 宽口径一致 | 16 / 8 | -6.2 pt [-18.8, +0.0] | -16.7 pt [-41.7, +0.0] | +10.4 pt [-14.6, +41.7] | +0.19 [-0.62, +1.23] | 7/16 / 8/16 | +2.25 / +2.19 |
| d47b-both − d45-both | 调用序列相同 | 16 / 8 | -6.2 pt [-18.8, +0.0] | -16.7 pt [-41.7, +0.0] | +10.4 pt [-14.6, +41.7] | +0.19 [-0.62, +1.23] | 7/16 / 8/16 | +2.25 / +2.19 |
| d47b-both − d45-both | 调用序列相同且 turn1_status 相同 | 16 / 8 | -6.2 pt [-18.8, +0.0] | -16.7 pt [-41.7, +0.0] | +10.4 pt [-14.6, +41.7] | +0.19 [-0.62, +1.23] | 7/16 / 8/16 | +2.25 / +2.19 |
| d47-both − d45-both | 全部 | 30 / 10 | -10.0 pt [-20.0, +0.0] | +0.0 pt [-20.0, +20.0] | -10.0 pt [-30.0, +13.3] | +0.77 [+0.10, +1.43] | 10/30 / 13/30 | +3.60 / +2.83 |
| d47-both − d45-both | turn 1 宽口径一致 | 22 / 8 | -8.3 pt [-20.8, +0.0] | -8.3 pt [-25.0, +0.0] | +0.0 pt [-16.7, +20.8] | +0.65 [-0.04, +1.50] | 9/22 / 11/22 | +2.91 / +2.18 |
| d47-both − d45-both | 调用序列相同 | 18 / 7 | -4.8 pt [-14.3, +0.0] | -9.5 pt [-28.6, +0.0] | +4.8 pt [-14.3, +28.6] | +0.69 [-0.14, +1.57] | 7/18 / 8/18 | +3.28 / +2.33 |
| d47-both − d45-both | 调用序列相同且 turn1_status 相同 | 16 / 7 | -4.8 pt [-14.3, +0.0] | -9.5 pt [-28.6, +0.0] | +4.8 pt [-14.3, +28.6] | +0.83 [-0.10, +1.74] | 6/16 / 7/16 | +3.50 / +2.38 |

- d47b-both vs d47-both：调用序列相同 14/30（004r2, 005r0, 005r1, 005r2, 006r0, 006r1, 006r2, 007r0, 007r1, 008r0, 008r1, 008r2, 009r1, 009r2）；完整 turn 1 响应逐字相同 0/30（无）。
- d47b-both vs d45-both：调用序列相同 16/30（002r1, 003r1, 005r0, 005r1, 005r2, 006r0, 006r1, 006r2, 007r0, 007r1, 008r0, 008r1, 008r2, 009r1, 009r2, 010r0）；完整 turn 1 响应逐字相同 0/30（无）。

按题 POST / PRE pass（通过 rep / 3）：

| 批 | 支 | 001 | 002 | 003 | 004 | 005 | 006 | 007 | 008 | 009 | 010 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| d47b-both | post | 1 | 3 | 3 | 0 | 3 | 0 | 0 | 0 | 1 | 2 |
| d47b-both | pre | 3 | 3 | 2 | 0 | 3 | 1 | 1 | 0 | 3 | 3 |
| d47-both | post | 0 | 3 | 1 | 0 | 3 | 0 | 0 | 0 | 1 | 2 |
| d47-both | pre | 2 | 3 | 3 | 0 | 3 | 0 | 0 | 0 | 3 | 3 |
| d45-both | post | 0 | 3 | 2 | 0 | 3 | 0 | 0 | 0 | 2 | 3 |
| d45-both | pre | 0 | 3 | 3 | 0 | 3 | 2 | 0 | 0 | 3 | 3 |

失败原因（score_why 分类计数，一行可多因）：

| 批 | 支 | 步数超限 | 回合上限耗尽 | 无收尾答复 | env 判错 | 不计「步数超限」的 pass |
|---|---|---|---|---|---|---|
| d47b-both | pre | 11 | 0 | 0 | 0 | 30/30 |
| d47b-both | post | 17 | 1 | 1 | 0 | 29/30 |
| d47-both | pre | 13 | 0 | 0 | 0 | 30/30 |
| d47-both | post | 20 | 3 | 3 | 0 | 27/30 |
| d45-both | pre | 13 | 0 | 0 | 0 | 30/30 |
| d45-both | post | 17 | 1 | 1 | 0 | 29/30 |

### E. 摘要器输入形态与预算（POST summary_input_chars；task_state 段截断上限 3000 字；[Goal] 截断 300 字 + `…`）

| 批 | summary_input_chars min/p25/中位（statistics.median）/p75/max | 含 task_state 段 | 段字符 min/p25/中位/p75/max | 段 > 3000 | 段内 [Goal] 以 `…` 结尾 | 去掉 task_state 段后 chars 中位（statistics.median） | 输入 > 24000 | summary_chars 中位 | summary_tokens_est 中位 |
|---|---|---|---|---|---|---|---|---|---|
| d47b-both | 1429 / 1511 / 1846.0 / 2232 / 2504 | 0/30 | —（无段） | — | — | 1846.0 | 0 | 603.5 | 241.5 |
| d47-both | 1355 / 1530 / 2126.5 / 2433 / 2747 | 30/30 | 73 / 96 / 120 / 283 / 308 | 0 | 3 | 1817.0 | 0 | 625.0 | 213.5 |
| d45-both | 1352 / 1462 / 1777.5 / 2292 / 2491 | 0/30 | —（无段） | — | — | 1777.5 | 0 | 563.5 | 202.0 |

- `summary_input_chars` 与 `len(summary_input)` 是否处处相等：d47b-both 30/30；d47-both 30/30；d45-both 30/30。

POST 摘要 [Goal] 节（D47 的间接效应在去掉 [Goal] 段后是否回落）：

| 批 | 摘要以 `[Goal]` 开头 | [Goal] 非空 | [Goal] == turn 1 用户原文（逐字） | [Goal] 含用户原文（子串） | [Goal] 字符中位 |
|---|---|---|---|---|---|
| d47b-both | 30/30 | 30 | 1 | 3 | 92.0 |
| d47-both | 29/30 | 29 | 22 | 24 | 113.0 |
| d45-both | 30/30 | 30 | 0 | 4 | 86.0 |

### F. 异常与漂移（error 行、空摘要、假名 U+3040–30FF）

| 批 | error 行 | 空摘要（POST） | 题面 lang | 摘要器输入含假名（POST） | POST 摘要含假名 | turn 1 最终答复含假名（t1 history 末条 assistant 文本） | cont_response 含假名 PRE / POST |
|---|---|---|---|---|---|---|---|
| d47b-both | 0 | 0 | {'zh': 18, 'en': 12} | 3 | 0 | 3 | 3 / 3 |
| d47-both | 0 | 0 | {'zh': 18, 'en': 12} | 3 | 0 | 3 | 2 / 2 |
| d45-both | 0 | 0 | {'zh': 18, 'en': 12} | 2 | 0 | 2 | 2 / 2 |

## 3. 混淆源

- (a) 与 (b) 同批落地，效应不可分离：结论 5 的差无法分给「去掉 [Goal] 段」或「描述 / 提醒文案」。
- REMEMBER_SCHEMA 描述是工具声明的一部分，turn 1 与两支续跑的每一次生成都带着它；新的 no_plan_nudge 文案进的是 turn 1 history（第 3 次无计划迭代后），因此也进了 POST 支的摘要器输入与 PRE 支续跑的上下文。两处改动都作用于 turn 1，所以 turn 1 本身就在批间不同（完整响应逐字相同 0/30），跨批差含 turn 1 抽样变异。
- 三批都是独立采样（remote 后端，seed 不生效），同 (题, rep) 只是标签对齐；10 题、每题 3 rep，题级 bootstrap 区间宽。
- 对照批的代码身份只以它们各自的 ANALYSIS 记载为准：meta.json 与运行日志都不记 git sha（CTRL-1 = 44ca2aa、CTRL-2 = D45 改动之后、D47 之前，见 `../context-knee-20261103-d47/ANALYSIS.md` 头注里关于 d45 批代码版本无法独立核验的说明）。本批（NEW）的「55cff85」同样只是记录，不是核验：meta.json 没有 git sha 字段，`revision` 是 HF 模型 revision，不是代码版本。数据本身能确认的只是 (a) 的提醒半边（history 与 POST summary_input 里新 nudge 文案 30/30）与 (b) 的行为（task_state 段消失），确认不了 REMEMBER_SCHEMA 描述的文案，也确认不了跑这批时工作树是干净的。
- 提醒位置：提醒之后的下一步几乎只由题面决定，不是由文案决定——三批间「提醒后第一步」完全一致的对 28/30（nudge 在 t1 response 里的索引三批全同 30/30；差的 2 对都出在 d47-both 单批内的 lc-hard-005 r1、lc-hard-007 r1，其余 28 对该步只取决于题面，与批次 / 文案无关）。d47b-both 的 30 条提醒后第一步思考文本里 "plan" / "save" / "remember" 出现 0 次（典型：「The task is complete. I just need to present the final answer.」）；其中 15/30 对，提醒触发时已经没有东西可存（即将直接作答）。因此「提醒后第一步」这个判据几乎没有能力检测 (a)：限制在提醒出现的时机上，不只是文案本身。
- 计数器的灵敏度：`turn1_remember_calls`（run_trace.py:81）读内存 history 的 assistant tool_calls，与 `meta_calls`（`benchmarks/context_knee/orch_tools.py:304-312`）同源。turn 1 内的灵敏度有直接正例：`backend/tests/test_summary_trace.py:80` 断言一个会调 remember 的干跑 turn 1（脚本见 `run_trace.py:168-178`）下 `turn1_remember_calls == 1`，走的是同一计数器代码路径。真实模型路径下的正例仍只有续跑轮的 `meta_calls`（004r1）：这证明计数器能看见元工具调用，但不是 turn 1 本身在真实模型下的正例。

## 4. 复算

```
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/summary_trace/analyze_d47b.py benchmarks/data/context-knee-20261104-d47b benchmarks/data/context-knee-20261103-d47 benchmarks/data/context-knee-20261103-d45
```

stdout 即 §2 原样（仅首行标题 `##` 降为 `###`；确定性：只读文件，bootstrap seed 0）。脚本复用 `analyze_d45.py` / `analyze_d47.py` 的函数（只 import，不改）。
