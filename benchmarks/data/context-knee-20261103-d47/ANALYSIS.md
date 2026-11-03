# D47 验收批次：复算与结论（2026-11-03）

> 原始数据：本目录 `trace-d47-both/`（new 模板 / 900 tok / 工具段 200 / turn 1 迭代 6 / drop_task on / task_state on；`transcripts.jsonl` 60 行、`meta.json`、`guardrail_events.jsonl`、`sessions/` 90 个会话目录）；对照 `../context-knee-20261103-d45/trace-d45-both/`（meta 除 stamp 外逐键相同，A 节）。同模型 Qwen3.8-27B-INT4，同题集 lc-hard-001…010 × 3 rep。两批 meta 都记 seed 7，但后端都是 remote：`run_knee.py:602-606` 的 `_seed_sampler` 对非 mlx 后端直接返回 False，seed 不作用于采样——同题同 rep 的配对只是标签对齐，不是「同一次采样」的复现，rep 间可交换。代码差 = D47（44ca2aa）：轮首种 TaskState（goal = 用户文本，plan 空）、remember 无 plan 可用、REMEMBER_SCHEMA 描述、SUMMARY_INSTRUCTION 任务块一句、catalog 路由门（Qwen 条目走全局 `tool_schema_mode: full`，config.yaml:526，此项不作用于本批）。对照批代码身份：d45 批跑于 15:30–16:11（stamp 20261103-161140），而本节列出的 D45 改动落地的提交 38411d7 在 16:57 才提交；meta.json / 运行日志都不记 git sha，本文档无法从 d47 侧数据独立核验 d45 批当时实跑的代码版本——「代码差只是 D47」这一判断以 `../context-knee-20261103-d45/ANALYSIS.md` 自己的记载为准。
> 复算（一键）：
> ```
> PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/summary_trace/analyze_d47.py \
>     benchmarks/data/context-knee-20261103-d47 benchmarks/data/context-knee-20261103-d45
> ```
> stdout 即下文「自动部分」A–E 节原样。交叉核对：`summarize_trace.py` 分别单跑两个目录，Δ(POST−PRE) 3.60 [2.40, 4.90] / 2.83 [1.60, 4.10]、pass、首 prompt 中位、摘要字符中位与 C 节一致（中位数四舍五入差最多 0.5——`summarize_trace.py` 把中位数四舍五入到整数字符，`analyze_d47.py` 不四舍五入；两目录同时传会因分组键相同被并成一组，这是 README 规定的行为，不是错）。CI 均为题级 bootstrap 95%（B=1000，seed 0），n = 10 题，区间宽，如实给出。

## 1. 结论

> 跨批告示：d47 对 d45 不止差 D47 一个因素在起作用的位置。混淆源：① 每对的摘要器输入都多出一个只含 `[Goal]` 的 `[Task state at end of turn]` 段（30/30）；② 便签本应只在部分对出现，实际 0 对出现，所以「便签」这一因素本批没有测到；③ REMEMBER_SCHEMA 描述变了，这是工具声明的一部分，turn 1 与两支续跑的每一次生成都会带上它（不只续跑）；SUMMARY_INSTRUCTION 任务块一句只在摘要器生成时出现，只影响 POST。另外 d45 的 REMEMBER_SCHEMA 描述末句「Only meaningful during a multi-step task (create a plan first).」在 D47 里被删掉了——但 d47 的 remember 调用仍是 0/30（B 节），说明至少这一句的删除本身没有改变调用率，不能单靠它解释 0/30；④ turn 1 不是逐字复现：调用序列相同 18/30，完整响应逐字相同仅 2/30（003r0、007r0）；调用序列相同的 18 对里 005r1、007r1 的 turn1_status 还不同（success/warning 互换），跨批差含 turn 1 抽样变异；⑤ 续跑轮首 D47 也会种一个 goal = 续跑消息的无 plan 任务，PRE / POST 两支都带。

1. **task_state 段进了摘要器输入，但只有 `[Goal]`。** `turn1_record_has_task_state` 30/30（d45 0/30），摘要器输入含 `[Task state at end of turn]` 30/30，段内节头构成 `Goal` 30/30，`[Plan]` 0、`[Scratchpad]` 0；`turn1_plan_built` 仍 0/30，`cont_live_task` 60/60 False（B 节）。段内 [Goal] 与 turn 1 用户原文逐字相同 27/30，余下 3 对是 lc-hard-004 × 3：原文 341 字，被 `task_state.SUMMARY_GOAL_CHARS = 300`（task_state.py:260）截成 300 字 + `…`。
2. **便签没有被模型使用：`remember` 调用 0/30 对（总数 0），`turn1_task_state_has_scratchpad` 0/30。** remember 每轮都在声明里（orchestrator.py:695 `_meta_schemas` 无门控），no_plan_nudge 在 turn 1 触发 30/30（A 节事件表）。「便签值逐字进 [Key values]」的命中率**不可算**：分母为 0，不是命中率 0。
3. **D47 的可观测间接效应是摘要的 [Goal] 改成逐字抄目标：** POST 摘要 [Goal] 与用户原文逐字相同 22/30（d45 0/30），含原文为子串 24/30（d45 4/30）；[Goal] 字符中位 113 对 86，摘要字符中位 625 对 563.5（B、C 节）。
4. **对 POST pass / 重复功：方向不利，强度不足以下结论。** PRE pass 17/30 对 17/30；POST pass 10/30 对 13/30。d47 − d45（按题）：POST pass −10.0 pt [−20.0, +0.0]，POST−PRE pass gap 之差 −10.0 pt [−30.0, +13.3]，Δ重复功之差 +0.77 [+0.10, +1.43]（批内 Δ重复功 +3.60 [+2.40, +4.90] 对 +2.83 [+1.60, +4.10]）。只看调用序列相同的 18 对 / 7 题：POST pass 差 −4.8 pt [−14.3, +0.0]，Δ重复功之差 +0.69 [−0.14, +1.57]——区间跨 0；去掉其中 turn1_status 不同的 005r1、007r1（剩 16 对 / 7 题）：Δ重复功之差 +0.83 [−0.10, +1.74]，仍跨 0。全量的 Δ重复功区间下沿 +0.10 只勉强离开 0，而它同时含混淆源 ①③④⑤；按 [Goal] 是否逐字分层（探索性）看不到「逐字目标 → 重做更多」的迹象（逐字层 +3.00、非逐字层 +5.25，但层与题目混在一起）。POST 失败原因里「步数超限」20 对 17、「回合上限耗尽」3 对 1、「无收尾答复」3 对 1；POST 会话 no_plan_nudge 9 对 5、turn_abort 3 对 1。
5. **预算未触及。** summary_input_chars 中位 2,123 / 最大 2,747（d45 1,768 / 2,491）；task_state 段 73–308 字，3,000 字截断 0 次触发；输入 > 24,000 字 0（D 节）。
6. **完整性：** 60 行、error 0、配对 30、空摘要 0；turn1_status success 18 / warning 12（与 d45 计数相同，但 2/30 对不同：005r1、007r1 的 success/warning 互换）（A 节）。
7. **顺带发现（非 D47 目标）：** 摘要 [User] 节提到日语 2/30（d47，都是 lc-hard-001 zh 题，r0 / r1）对 0/30（d45），但两条性质不同：r0 写「用户偏好使用日语进行回复」——把模型自己的语言漂移误归因成用户偏好；r1 写「用户以中文下达任务，但助手以日语回复」——这条是准确描述，不是误归因。所以 [User] 节的日语误归因实际只有 1/30（r0）。另外 lc-hard-001 r2 的 [Decisions] 节也有一处误归因：「summary.txt 采用日文格式……以匹配用户语言」，把模型单方面切换到日语写成了「匹配用户语言」的决策——[Decisions] 节日语误归因 1/30（E 节新列；d45 该列 0/30）。两处合计日语误归因 2/30，都是 lc-hard-001（zh 题）。题面是中文，来源是 turn 1 模型自己用日语写了 summary.txt / 回复（摘要器输入含假名：d47 3/30、d45 2/30）。这些误归因会被带进后续轮次（E 节；逐条核对见 transcripts 该三行 `summary`：001 r0/r1/r2）。

## 2. 下一步建议（草稿，供方案 §3 W4 / §6 D47）

- D47c（无 plan 时轮内注入便签块）**本批数据不支持立即做**：它放大的是便签的可见性，而便签的前提——模型调 remember——是 0/30。先解决调用率：REMEMBER_SCHEMA 描述第二句仍是「During a planned multi-step task …」，在无 plan 的常态下可能被读成不适用；候选是改描述 / 在 no_plan_nudge 文本里点名 remember，或者不靠模型合作、由编排器从工具结果自动抽关键值进便签。任一候选都要先有 remember 调用率 > 0 的证据，再谈 D47c。
- `[Goal]` 段本身对 POST 无可见正收益（结论 4），保留它的理由只剩「结构对齐、成本小」——但「成本小」只就 POST 摘要本身而言（约 +60 摘要字符，B 节）：摘要器输入的中位字符数涨了 +355（summary_input_chars 中位 2123 对 1768，C 节），这是 task_state 段本身（73–308 字，D 节）加上它带来的其余差异；lc-hard-004 的 300 字截断说明长题面会被截。
- 摘要器把助手的语言漂移写成 `[User]` 偏好（结论 7，1/30）是一条独立的模板缺口，样本只有一例，记档即可。

## 自动部分（`benchmarks/summary_trace/analyze_d47.py`，seed 0，B=1000）

### A. 完整性（transcripts.jsonl / meta.json / guardrail_events.jsonl / t1 会话 history.jsonl）

| 批 | 目录 | 行 | error 行 | 配对 | 空摘要（POST） | turn1_status | turn1_plan_built | turn1_record_has_task_state | cont_status PRE / POST | stamp | meta 除 stamp 外与对照批相同 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| d47-both | `benchmarks/data/context-knee-20261103-d47/trace-d47-both` | 60 | 0 | 30 | 0 | {'success': 18, 'warning': 12} | {'False': 30} | {'True': 30} | {'success': 30} / {'success': 24, 'warning': 6} | 20261103-212444 | True |
| d45-both | `benchmarks/data/context-knee-20261103-d45/trace-d45-both` | 60 | 0 | 30 | 0 | {'success': 18, 'warning': 12} | {'False': 30} | {'False': 30} | {'success': 26, 'warning': 4} / {'success': 28, 'warning': 2} | 20261103-161140 | True |

guardrail_events.jsonl 事件类型计数（turn 1 会话 `-t1` / 续跑会话 `-pre` `-post`）：

| 批 | 事件总数 | 按 (type, 会话后缀) |
|---|---|---|
| d47-both | 104 | action_claim_nudge@post=5, action_claim_nudge@pre=11, duplicate_call@t1=4, no_plan_nudge@post=9, no_plan_nudge@pre=1, no_plan_nudge@t1=30, turn_abort@post=3, turn_abort@t1=12, verify_nudge@post=3, verify_nudge@pre=1, verify_nudge@t1=25 |
| d45-both | 97 | action_claim_nudge@post=8, action_claim_nudge@pre=10, duplicate_call@t1=5, no_plan_nudge@post=5, no_plan_nudge@t1=30, turn_abort@post=1, turn_abort@t1=12, verify_nudge@post=1, verify_nudge@t1=25 |

### B. D47 直接效应（本批内；turn 1 每对一次，取 PRE 行）

| 批 | remember 调用总数 | remember ≥1 的对 | 有便签对（turn1_task_state_has_scratchpad） | turn1_task_state_chars min/p25/中位/p75/max | 摘要器输入含 `[Task state at end of turn]` | 其中含 `[Scratchpad]` | 其中含 `[Plan]` | cont_live_task PRE / POST |
|---|---|---|---|---|---|---|---|---|
| d47-both | 0 | 0 | 0 | 73 / 96 / 120 / 283 / 308 | 30/30 | 0 | 0 | {'False': 30} / {'False': 30} |
| d45-both | 缺键 | 缺键 | 缺键 | 缺键 | 0/30 | 0 | 0 | {'False': 30} / {'False': 30} |

- 便签值逐字进 POST 摘要 [Key values]：**不可算**——本批没有一对的摘要器输入带 `[Scratchpad]`（分母为 0，不是命中率 0）。
- d47 摘要器输入 task_state 段的节头构成（按对）：{'Goal': 30}
- d47 task_state 段 [Goal] 与 turn 1 用户原文（t1 history.jsonl `prompt`）逐字相同：{True: 27, False: 3}

POST 摘要 [Goal] 节（D47 的间接效应：摘要器是否逐字抄目标）：

| 批 | 摘要以 `[Goal]` 开头 | [Goal] 非空 | [Goal] == turn 1 用户原文（逐字） | [Goal] 含用户原文（子串） | [Goal] 字符中位 |
|---|---|---|---|---|---|
| d47-both | 29/30 | 29 | 22 | 24 | 113.0 |
| d45-both | 30/30 | 30 | 0 | 4 | 86.0 |

分层（探索性，非预注册）：POST 摘要 [Goal] 是否逐字等于用户原文 → POST pass / Δ重复功均：

| 批 | 层 | 对 | POST pass | PRE pass | Δ重复功均 |
|---|---|---|---|---|---|
| d47-both | goal_verbatim=True | 22 | 8 | 12 | +3.00 |
| d47-both | goal_verbatim=False | 8 | 2 | 5 | +5.25 |
| d45-both | goal_verbatim=False | 30 | 13 | 17 | +2.83 |

### C. 每批汇总与跨批差（d47 − d45，按题；正 = d47 更高；pass 单位百分点）

| 批 | pass PRE | pass POST | POST−PRE pass（题级 95%） | 重复功 PRE→POST 均 | Δ重复功（题级 95%） | 首 prompt 中位 PRE / POST | 摘要字符中位 | turn1_calls 中位 |
|---|---|---|---|---|---|---|---|---|
| d47-both | 17/30 | 10/30 | -23.3 pt [-43.3, -6.7] | 0.73 → 4.33 | +3.60 [+2.40, +4.90] | 2307.5 / 1485.0 | 625.0 | 9.0 |
| d45-both | 17/30 | 13/30 | -13.3 pt [-30.0, +0.0] | 0.67 → 3.50 | +2.83 [+1.60, +4.10] | 2338.0 / 1469.5 | 563.5 | 9.5 |

- turn 1 一致（同 (题, rep)）：宽口径（env 快照 + 调用数）22/30；调用序列相同 18/30；完整 turn 1 响应逐字相同（t1 history.jsonl 的 response 列表相等）2/30。
- 调用序列相同的对：003r0, 005r0, 005r1, 005r2, 006r0, 006r1, 006r2, 007r0, 007r1, 007r2, 008r0, 008r1, 008r2, 009r0, 009r1, 009r2, 010r1, 010r2；其中 turn1_status 不同：005r1, 007r1。
- 完整响应逐字相同的对：003r0, 007r0。

| 子集 | 对 / 题 | POST pass 差 | PRE pass 差 | POST−PRE pass gap 之差 | Δ重复功之差 | 子集内 POST pass d47 / d45 | 子集内 Δ重复功均 d47 / d45 |
|---|---|---|---|---|---|---|---|
| 全部 | 30 / 10 | -10.0 pt [-20.0, +0.0] | +0.0 pt [-20.0, +20.0] | -10.0 pt [-30.0, +13.3] | +0.77 [+0.10, +1.43] | 10/30 / 13/30 | +3.60 / +2.83 |
| turn 1 宽口径一致 | 22 / 8 | -8.3 pt [-20.8, +0.0] | -8.3 pt [-25.0, +0.0] | +0.0 pt [-16.7, +20.8] | +0.65 [-0.04, +1.50] | 9/22 / 11/22 | +2.91 / +2.18 |
| 调用序列相同 | 18 / 7 | -4.8 pt [-14.3, +0.0] | -9.5 pt [-28.6, +0.0] | +4.8 pt [-14.3, +28.6] | +0.69 [-0.14, +1.57] | 7/18 / 8/18 | +3.28 / +2.33 |
| 调用序列相同且 turn1_status 相同 | 16 / 7 | -4.8 pt [-14.3, +0.0] | -9.5 pt [-28.6, +0.0] | +4.8 pt [-14.3, +28.6] | +0.83 [-0.10, +1.74] | 6/16 / 7/16 | +3.50 / +2.38 |

按题 POST pass（通过 rep / 3）与 PRE pass：

| 批 | 支 | 001 | 002 | 003 | 004 | 005 | 006 | 007 | 008 | 009 | 010 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| d47-both | post | 0 | 3 | 1 | 0 | 3 | 0 | 0 | 0 | 1 | 2 |
| d47-both | pre | 2 | 3 | 3 | 0 | 3 | 0 | 0 | 0 | 3 | 3 |
| d45-both | post | 0 | 3 | 2 | 0 | 3 | 0 | 0 | 0 | 2 | 3 |
| d45-both | pre | 0 | 3 | 3 | 0 | 3 | 2 | 0 | 0 | 3 | 3 |

失败原因（score_why 分类计数，一行可多因）：

| 批 | 支 | 步数超限 | 回合上限耗尽 | 无收尾答复 | env 判错 | 不计「步数超限」的 pass |
|---|---|---|---|---|---|---|
| d47-both | pre | 13 | 0 | 0 | 0 | 30/30 |
| d47-both | post | 20 | 3 | 3 | 0 | 27/30 |
| d45-both | pre | 13 | 0 | 0 | 0 | 30/30 |
| d45-both | post | 17 | 1 | 1 | 0 | 29/30 |

### D. 预算（POST summary_input_chars；task_state 段长度，截断上限 3000 字 + `…`）

| 批 | summary_input_chars min/p25/中位/p75/max | task_state 段字符 min/p25/中位/p75/max | 段 > 3000（compactor 截断触发） | [Goal] 以 `…` 结尾（task_state.SUMMARY_GOAL_CHARS=300 截断） | 输入 > 24000 字 |
|---|---|---|---|---|---|
| d47-both | 1355 / 1530 / 2123 / 2433 / 2747 | 73 / 96 / 120 / 283 / 308 | 0 | 3 | 0 |
| d45-both | 1352 / 1462 / 1768 / 2292 / 2491 | —（无段） | — | — | 0 |

### E. 语言漂移（假名 U+3040–30FF 出现；题面 lang 均为 zh/en）

| 批 | 题面 lang | 摘要器输入含假名（POST） | POST 摘要含假名 | 摘要 [User] 节提到日语 | 摘要 [Decisions] 节提到日语误归因 | cont_response 含假名 PRE / POST |
|---|---|---|---|---|---|---|
| d47-both | {'zh': 18, 'en': 12} | 3 | 0 | 2 | 1 | 2 / 2 |
| d45-both | {'zh': 18, 'en': 12} | 2 | 0 | 0 | 0 | 2 / 2 |

