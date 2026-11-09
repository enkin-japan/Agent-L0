# D48 复测批证据（`d48_evidence.py` 生成；data = `context-knee-20261109-d48`，对照 w12eval = `context-knee-20261108-w12eval`、p2b = `context-knee-20261108-p2b`）

常量：ceiling 53,248（= max_model_len 65,536 − reserve 12,288）、rev 臂 hard 51,200、soft 24,576；bootstrap 10000 次、种子 20261031，只按题重抽（P2.paired_item 同法）。

## a. 输入与去重

- DONE：`BATCH_RC=0 SKIPPED=0`；arms_status：`orch-turn-none ok / orch-turn-w1w2-rev ok`
| 臂 | 原始行 | A.load_group 有效 | missing(error) | 丢弃重复 | 顶层 error 键行 | 含 `"error"` 字样行 | 按档 run 数 | 按档 rep 集合 | sessions 目录 | seed≠1107×1000+rep 或未发送 |
|---|---|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 140 | 140 | 0 | 0 | 0 | 2 | {0: 20, 49152: 60, 57344: 60} | {0: '0–1', 49152: '0–5', 57344: '0–5'} | 140 | 0 |
| orch-turn-w1w2-rev | 140 | 140 | 0 | 0 | 0 | 2 | {0: 20, 49152: 60, 57344: 60} | {0: '0–1', 49152: '0–5', 57344: '0–5'} | 140 | 0 |

- none vs rev 同 (level,item,rep) 配对 140：fill.fingerprint 相同 140、task_fp 相同 140
- orch-turn-none 填充档 (level,item) 20 组：rep 间 fill.fingerprint 不全互异的组 0
- orch-turn-w1w2-rev 填充档 (level,item) 20 组：rep 间 fill.fingerprint 不全互异的组 0
  - orch-turn-none 第 10 行 L0 lc-hard-005 r1：顶层 error 键 False；type=error 工具调用 ['turns[1].tool_calls[3]']；score.pass True；system_errors 1
  - orch-turn-none 第 67 行 L49152 lc-hard-008 r4：顶层 error 键 False；type=error 工具调用 ['turns[1].tool_calls[2]']；score.pass False；system_errors 1
  - orch-turn-w1w2-rev 第 10 行 L0 lc-hard-005 r1：顶层 error 键 False；type=error 工具调用 ['turns[1].tool_calls[3]']；score.pass True；system_errors 1
  - orch-turn-w1w2-rev 第 67 行 L49152 lc-hard-008 r4：顶层 error 键 False；type=error 工具调用 ['turns[1].tool_calls[2]']；score.pass False；system_errors 1

### a2. 冒烟漂移（w12eval `smoke-orch-20261108-211442-A` vs 本批 `smoke-orch-20261109-083103-A`）

- 输入 L0 lc-hard-001 r0，seed 1107000 / 1107000；非输出字段差：['guard_thresholds']
- output_sig 相同 False；first_divergent_turn = 3；轮数 7 / 6；pass True / True
- gens prompt_tokens：[6191, 6408, 6534, 7242, 7621, 7830, 7795] / [6191, 6408, 6534, 7242, 7598, 7870]
- turn 3.thoughts：长度 324 / 305，首个不同字符位置 2
- turn 3.tool_calls：长度 293 / 236，首个不同字符位置 71
- 本批 orch-turn-none 同键行 vs 本批冒烟（同 VM）：output_sig 相同 True；first_divergent_turn = None
- 本批 orch-turn-w1w2-rev 同键行 vs 本批冒烟（同 VM）：output_sig 相同 True；first_divergent_turn = None

## b. L0 口径锚

| 组 | L0 rep | 成功率 k/n | Pass^2 |
|---|---|---|---|
| 本批 none | 2 | 13/20 = 65.0% | 5/10 = 50.0% |
| 本批 rev | 2 | 13/20 = 65.0% | 5/10 = 50.0% |
| w12eval none（6 rep） | 6 | 44/60 = 73.3% | 6/10 = 60.0% |
| w12eval none 只取 rep 0–1 | 2 | 12/20 | 4/10 |

- 本批 none vs rev L0 同 (item,rep)：输出逐字相同 20/20；全部 gens prompt_tokens 序列相同 20/20；first_divergent_turn 分布 {None:20}（None = 全程相同）
- orch-turn-none L0 护栏事件：context_soft 0、context_hard 0、context_guard_fold 0、step_compaction 0、step_readback_exempt 0、turn_abort 0；其余类型 {'duplicate_call': 4, 'no_plan_nudge': 20, 'parse_error': 1, 'tool_error': 4, 'verify_nudge': 2}
- orch-turn-w1w2-rev L0 护栏事件：context_soft 0、context_hard 0、context_guard_fold 0、step_compaction 0、step_readback_exempt 0、turn_abort 0；其余类型 {'duplicate_call': 4, 'no_plan_nudge': 20, 'parse_error': 1, 'tool_error': 4, 'verify_nudge': 2}
- L49152 none vs rev（同 VM）：(首生成输入相同, 首轮输出逐字相同) → {(True, True): 60}；first_divergent_turn {2:1, 3:1, 4:20, 5:5, 6:2, 7:1, 8:1, 9:1, 10:1, None:27}
- L57344 none vs rev（同 VM）：(首生成输入相同, 首轮输出逐字相同) → {(False, False): 57, (False, True): 3}；first_divergent_turn {0:57, 1:3}

## c. §2 三条（none → rev，b − a；按题配对、只按题重抽 bootstrap 95%，种子 20261031）

- 本批 none 拐点（Pass^2 规则）：拐点 49152（L0 只 2 rep）；w12eval none 6 rep 定的拐点是 32768。两种口径下 49K / 57K 都在拐点以上。

### d. 零分布门槛（V2c 同法：同一臂内取两组不相交的 m 个 rep 当两条假臂，枚举全部无序拆分得 |Δ|；门槛 t = P(|Δ|≥t) ≤ 5% 的最小可取值；— = 超出支撑，括号 = 最大值:尾概率）

| 零分布 | 拆分数 m=3 | 成功率 m=3 | Pass^2 m=3 | 拆分数 m=2 | 成功率 m=2 | Pass^2 m=2 |
|---|---|---|---|---|---|---|
| 本批 L0 none+rev（各 2 rep） | 0 | —（0 拆分） | —（0 拆分） | 0 | —（0 拆分） | —（0 拆分） |
| 本批 L49152 臂内 none+rev | 20 | ≥23（5.0%） | ≥23（5.0%） | 90 | ≥30（2.2%） | ≥40（2.2%） |
| 本批 L57344 臂内 none+rev | 20 | ≥27（5.0%） | ≥20（5.0%） | 90 | —（35:6.7%） | ≥40（1.1%） |
| 本批 49K+57K 臂内合并 | 40 | ≥23（5.0%） | ≥23（2.5%） | 180 | ≥35（3.3%） | ≥40（1.7%） |
| w12eval none L0（6 rep） | 10 | —（20:10.0%） | —（23:10.0%） | 45 | —（25:8.9%） | ≥40（2.2%） |
| p2b none+full L0（委托门槛来源） | 20 | ≥23（5.0%） | ≥27（5.0%） | 90 | ≥30（2.2%） | —（40:5.6%） |

- 本批 L0 各臂只 2 rep：m=2 需 4 个 rep、m=3 需 6 个，本批 L0 拆不出零分布（拆分数 0）。本批可用的同法零分布只有填充档臂内 6 rep（每臂每档 10 种 m=3 拆分，两臂合并 20 种 / 两档合并 40 种）。p2b none+full L0 行复现委托 / w12eval 所用 23 / 27（脚本断言）。

### c1. 第一条：成功率 / Pass^2 ≥ 无管理的 90%（§2 原文两项都是 90%）

| level | 成功率 none → rev | 差 [CI]（题升/降） | 比 [CI] | 尾概率 本档臂内 m=3 / 两档合并 m=3 / p2b m=3 | Pass^2 none → rev | 差 [CI]（题升/降） | 比 [CI] | 尾概率 本档臂内 m=3 / 两档合并 m=3 / p2b m=3 |
|---|---|---|---|---|---|---|---|---|
| 49152 | 45.0% → 48.3% | +3.3 [0.0,+8.3]（2/0） | 1.07 [1.00,1.19] | 100% / 95% / 90% | 22.7% → 26.7% | +4.0 [0.0,+10.7]（2/0） | 1.18 [1.00,1.42] | 65% / 68% / 80% |
| 57344 | 43.3% → 40.0% | -3.3 [-20.0,+13.3]（4/5） | 0.92 [0.62,1.41] | 90% / 95% / 90% | 22.7% → 18.0% | -4.7 [-22.7,+12.0]（4/4） | 0.79 [0.33,2.67] | 70% / 68% / 80% |
| 49K+57K 合并 | 44.2% → 44.2% | 0.0 [-7.5,+7.5]（4/4） | 1.00 [0.86,1.21] | （合并无零分布） | 22.7% → 22.3% | -0.3 [-7.0,+6.7]（5/4） | 0.99 [0.79,1.62] | （合并无零分布） |

门槛判读（|观察差| 对 m=3 门槛；— = 超出支撑不判）：
- L49152 成功率 差 +3.3：本批 L49152 臂内 none+rev t=23 → 噪声内；本批 49K+57K 臂内合并 t=23 → 噪声内；p2b none+full L0（委托门槛来源） t=23 → 噪声内
- L49152 Pass^2 差 +4.0：本批 L49152 臂内 none+rev t=23 → 噪声内；本批 49K+57K 臂内合并 t=23 → 噪声内；p2b none+full L0（委托门槛来源） t=27 → 噪声内
- L57344 成功率 差 -3.3：本批 L57344 臂内 none+rev t=27 → 噪声内；本批 49K+57K 臂内合并 t=23 → 噪声内；p2b none+full L0（委托门槛来源） t=23 → 噪声内
- L57344 Pass^2 差 -4.7：本批 L57344 臂内 none+rev t=20 → 噪声内；本批 49K+57K 臂内合并 t=23 → 噪声内；p2b none+full L0（委托门槛来源） t=27 → 噪声内

### c2. 第二条（新口径）：管理臂每 run 实发峰值 ≤ ceiling 53,248 且 0 次 turn_abort reason=context_guard（ceiling 终止）

实发峰值 = max(gens[*].prompt_tokens)（O.sent_peak，引擎回报）。

| 臂 | level | runs | 实发峰值 中位 / 最大 | ≤ 53,248 的 run | > 53,248 的生成次数 | ≥ hard 51,200 的 run | 峰值字段 ≠ 实发峰值 | turn_abort 原因 | reason=context_guard（ceiling 终止） |
|---|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 0 | 20 | 7,471 / 11,029 | 20/20 | 0 | 0 | 0 | — | 0 |
| orch-turn-none | 49152 | 60 | 52,359 / 56,455 | 37/60 | 101 | 34 | 0 | {'max_iterations': 18, 'repetition_guard': 1} | 0 |
| orch-turn-none | 57344 | 60 | 59,345 / 63,360 | 0/60 | 556 | 60 | 0 | {'max_iterations': 19} | 0 |
| orch-turn-w1w2-rev | 0 | 20 | 7,471 / 11,029 | 20/20 | 0 | 0 | 0 | — | 0 |
| orch-turn-w1w2-rev | 49152 | 60 | 51,222 / 53,216 | 60/60 | 0 | 30 | 0 | {'max_iterations': 16, 'repetition_guard': 1} | 0 |
| orch-turn-w1w2-rev | 57344 | 60 | 43,396 / 48,770 | 60/60 | 0 | 0 | 0 | {'max_iterations': 21} | 0 |

- rev 全臂：实发峰值 ≤ 53,248 的 run 140/140，最大 53,216；全部事件里 reason=context_guard 的 turn_abort 0；事件类型全集 ['context_guard_fold', 'context_hard', 'context_soft', 'duplicate_call', 'failure_route', 'failure_swallow_nudge', 'no_plan_nudge', 'parse_error', 'premature_finish_nudge', 'repetition', 'step_compaction', 'step_readback_exempt', 'tool_error', 'turn_abort', 'upgrade_triggered', 'verify_nudge']；turn_abort reason 全集（已核对 ⊆ 已知取值） ['max_iterations', 'repetition_guard']
- none（对照，不受此条约束）：实发峰值 > 53,248 的 run 83/140，最大 63,360（max_model_len 65,536）；error 行 0

### c3. 第三条：总耗时增量 ≤ +25%（Σwall = 去重行 wall_s 之和；增量 = rev ÷ none − 1）

| level | Σwall none min | Σwall rev min | 增量 | wall 中位 none / rev s |
|---|---|---|---|---|
| 0 | 8.1 | 8.2 | +0.3% | 25.3 / 25.7 |
| 49152 | 96.0 | 97.0 | +1.1% | 100.1 / 99.7 |
| 57344 | 98.7 | 86.2 | -12.6% | 86.3 / 87.1 |
| 49K+57K | 194.6 | 183.2 | -5.9% | — |
| 三档合计（含 L0） | 202.8 | 191.4 | -5.6% | — |

run_colab.log 臂墙钟：none 204.2 min、rev 192.2 min（-5.9%）；批首行 → DONE 417.0 min。

## e. 机制（guardrail_events.jsonl 按行 guard_events_lines 切片；iter1 = detail.iteration == 1 即首次生成前）

| 批 / 臂 | level | runs | soft 事件（run） | hard iter1 / 后（run） | fold iter1 / 后（run） | step_compaction（按 detail.level） | step_readback_exempt | 切片 vs session 字段 |
|---|---|---|---|---|---|---|---|---|
| d48 none | 0 | 20 | 0（0） | 0 / 0（0） | 0 / 0（0） | 0（0） | 0 | 31 / 31，不符 0 |
| d48 none | 49152 | 60 | 0（0） | 0 / 0（0） | 0 / 0（0） | 0（0） | 0 | 185 / 185，不符 0 |
| d48 none | 57344 | 60 | 0（0） | 0 / 0（0） | 0 / 0（0） | 0（0） | 0 | 181 / 181，不符 0 |
| d48 w1w2-rev | 0 | 20 | 0（0） | 0 / 0（0） | 0 / 0（0） | 0（0） | 0 | 31 / 31，不符 0 |
| d48 w1w2-rev | 49152 | 60 | 60（60） | 0 / 30（30） | 0 / 8（8） | 44（29）{'ceiling': 10, 'hard': 34} | 0 | 300 / 300，不符 0 |
| d48 w1w2-rev | 57344 | 60 | 60（60） | 0 / 0（0） | 60 / 0（60） | 38（28）{'ceiling': 38} | 1 | 322 / 322，不符 0 |
| w12eval w1w2 | 49152 | 60 | 22（22） | 60 / 0（60） | 0 / 8（8） | 28（21）{'ceiling': 6, 'hard': 22} | 1 | 287 / 287，不符 0 |
| w12eval w1w2 | 57344 | 60 | 60（60） | 0 / 1（1） | 60 / 0（60） | 45（30）{'ceiling': 45} | 5 | 357 / 357，不符 0 |

### e2. rev 臂 fold（context_guard_fold）细节

| level | fold 事件 | mode | passes 分布 | 每 run fold 次数分布（含 0） | before | after | gap = before − hard + 1 | 实省 = before − after | 实省 ÷ gap 中位（范围） | after < hard | hard − after | compacted | spilled |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 0 | — | — | {0:20} | — | — | — | — | — | — | — | — | — |
| 49152 | 8 | {'minimal': 8} | {1:8} | {0:52, 1:8} | 53,437（53,261–53,591） | 47,558（45,362–49,485） | 2,238（2,062–2,392） | 5,904（3,862–8,151） | 2.6（1.8–3.6） | 8/8 | 3,642（1,715–5,838） | 14（4–16） | 14（4–16） |
| 57344 | 60 | {'minimal': 60} | {1:60} | {1:60} | 57,554（56,693–59,097） | 41,104（35,044–46,354） | 6,356（5,494–7,898） | 16,568（11,215–24,006） | 2.6（1.8–3.4） | 60/60 | 10,096（4,846–16,156） | 35（24–50） | 34（22–49） |

首生成 prompt（gens[0].prompt_tokens）中位（范围）与首生成前 fold 的 run：

| 组 | level | 首生成前 fold 的 run | 首生成 prompt | fill.prompt_tokens_est 中位 |
|---|---|---|---|---|
| d48 none | 49152 | 0/60 | 49,382（48,775–50,586） | 49,382 |
| d48 none | 57344 | 0/60 | 57,554（56,693–59,097） | 57,554 |
| d48 rev | 49152 | 0/60 | 49,382（48,775–50,586） | 49,382 |
| d48 rev | 57344 | 60/60 | 41,104（35,044–46,354） | 57,554 |
| w12eval none | 49152 | 0/60 | 49,382（48,775–50,586） | 49,382 |
| w12eval none | 57344 | 0/60 | 57,554（56,693–59,097） | 57,554 |
| w12eval w1w2 | 49152 | 0/60 | 49,428（48,820–50,631） | 49,382 |
| w12eval w1w2 | 57344 | 60/60 | 27,158（22,804–30,696） | 57,554 |

- rev context_hard 事件 30：detail.prompt_tokens 最小 51,279 / 最大 53,120；< 51,200 的 0；limit 取值 {65536: 30}；ceiling 取值 {53248: 30}；按档 {'hard': 30}
- rev context_soft 事件 120：iteration 分布 {1:120}；prompt_tokens 最小 35,044
- rev 一 run 内 ≥ 2 次 fold（折后再涨回 ≥ ceiling 又折）：0 run
- rev 有 fold 的 run 里发出过 ≥ ceiling 生成的 run：0
- rev step_compaction (档, detail.level, trigger) 计数：{(49152, 'ceiling', 'step'): 10, (49152, 'hard', 'step'): 34, (57344, 'ceiling', 'step'): 38}

### e3. 相邻两次生成 prompt 下降 > 2,000 tok 的位置（gens[i−1] → gens[i]，i ≥ 1；fold 在首生成前的不在此列）

| 批 / 臂 | level | 下降次数（run） | 下降前 prompt | 下降后 prompt | 下降前 < hard 51,200 的次数 | run 内首次下降后的最小 prompt 中位 |
|---|---|---|---|---|---|---|
| d48 none | 49152 | 0（0） | — | — | 0 | — |
| d48 none | 57344 | 0（0） | — | — | 0 | — |
| d48 rev | 49152 | 36（31） | 52,180（45,452–53,216） | 27,112（22,720–49,485） | 9 | 26,585 |
| d48 rev | 57344 | 27（27） | 44,143（39,067–48,127） | 29,834（25,684–34,558） | 27 | 29,834 |
| w12eval w1w2 | 49152 | 24（24） | 52,946（50,014–53,244） | 26,887（22,969–30,108） | 1 | 26,887 |
| w12eval w1w2 | 57344 | 3（3） | 32,422（31,095–32,679） | 29,776（28,827–29,879） | 3 | 29,776 |

- reused_prefix 可信度：rev 臂 iteration > 1 的 fold 之后紧接的那次生成（gens[iteration−1]，其 prompt_tokens = fold 的 after）8 次，reused_prefix ÷ prompt_tokens 中位 0.989、最小 0.968。fold 从最旧观察（系统前缀之后第一条）起改写，真前缀缓存在此处应大面积失配；报 ≥ 0.9 说明远端 reused_prefix 不反映真实命中（推断：与 README「首生成 num_cached_tokens 不可信」同源）。

## f. 成本（每 run；gens[1:] 计缓存未命中：miss = max(0, prompt_tokens − reused_prefix)；首生成不计（README：远端首生成 num_cached_tokens 不可信）；「近全量重填」= gens[1:] 中 reused_prefix < 0.5 × prompt_tokens 的生成。⚠️ e3 末条：fold 后 reused_prefix 仍 ≈ prompt，miss / 近全量重填两列**不能**读作真实前缀缓存失配；full_renders = 编排层全量渲染次数，按档 full_renders÷生成（两臂合并，两臂同档取值相同）：L0 202/280=0.72、L49152 1143/1143=1.00、L57344 1160/1160=1.00，也不是缓存信号。可用的成本证据只有 wall_s）

| 臂 | level | runs | wall 中位 s | Σwall min | 生成次数 Σ | full_renders Σ | miss tok Σ（gens[1:]） | miss tok 中位 / run | 近全量重填 生成数（run） |
|---|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 0 | 20 | 25.3 | 8.1 | 140 | 101 | 77,554 | 3,045 | 0（0） |
| orch-turn-none | 49152 | 60 | 100.1 | 96.0 | 576 | 576 | 377,556 | 5,767 | 0（0） |
| orch-turn-none | 57344 | 60 | 86.3 | 98.7 | 556 | 556 | 313,293 | 5,036 | 0（0） |
| orch-turn-w1w2-rev | 0 | 20 | 25.7 | 8.2 | 140 | 101 | 77,554 | 3,045 | 0（0） |
| orch-turn-w1w2-rev | 49152 | 60 | 99.7 | 97.0 | 567 | 567 | 371,945 | 6,026 | 0（0） |
| orch-turn-w1w2-rev | 57344 | 60 | 87.1 | 86.2 | 604 | 604 | 382,840 | 6,576 | 0（0） |

## g. 口径核对

- orch-turn-none summary.json：guard="off"；guard_thresholds={"fold_mode": "full", "hard": 39322, "reserve": 12288, "soft": 24576}；step_compaction="off"；step_compaction_cfg={"cold_refill_trigger_level": "hard", "digest_chars": 200, "keep_recent": 2, "trigger_level": "soft"}；agent_cfg_fingerprint="bd006ebc49cf403b"；level_reps={"0": 2, "49152": 6, "57344": 6}；seed=1107；prefix_mode="prod"；tool_schema_mode_effective="catalog"；context_limit=65536
- orch-turn-w1w2-rev summary.json：guard="on"；guard_thresholds={"fold_mode": "minimal", "hard": 51200, "reserve": 12288, "soft": 24576}；step_compaction="on"；step_compaction_cfg={"cold_refill_trigger_level": "hard", "digest_chars": 200, "keep_recent": 2, "trigger_level": "soft"}；agent_cfg_fingerprint="7b2fd89bae0acfcb"；level_reps={"0": 2, "49152": 6, "57344": 6}；seed=1107；prefix_mode="prod"；tool_schema_mode_effective="catalog"；context_limit=65536
- orch-turn-none 行内：guard_thresholds 取值 ['{"fold_mode": "full", "hard": 39322, "reserve": 12288, "soft": 24576}']；取值不唯一的配置键 {'prefix_n_tools': ['{"grammar": 61, "prompt": 61, "prompt_full": 17}', '{"grammar": 64, "prompt": 64, "prompt_full": 20}'], 'max_total_iterations': ['11', '12', '13', '14', '16']}
- orch-turn-w1w2-rev 行内：guard_thresholds 取值 ['{"fold_mode": "minimal", "hard": 51200, "reserve": 12288, "soft": 24576}']；取值不唯一的配置键 {'prefix_n_tools': ['{"grammar": 61, "prompt": 61, "prompt_full": 17}', '{"grammar": 64, "prompt": 64, "prompt_full": 20}'], 'max_total_iterations': ['11', '12', '13', '14', '16']}
- none 与 rev 行内取值不同的配置键：['guard', 'step_compaction', 'guard_thresholds', 'agent_cfg_fingerprint', 'orchestrator_cfg_source']
- harness_mode_of（run_knee，guard_thresholds 缺 fold_mode 按 full 归一）每臂取值数：d48 none 1、rev 1、w12eval none 1、w1w2 1
- d48 none vs w12eval none harness_mode 不同的键：无；run_knee.harness_mismatch(w12eval none 行, d48 none 口径) = False
- d48 rev vs w12eval w1w2 harness_mode 不同的键：guard_thresholds: {'fold_mode': 'full', 'hard': 39322, 'reserve': 12288, 'soft': 24576} → {'fold_mode': 'minimal', 'hard': 51200, 'reserve': 12288, 'soft': 24576}；agent_cfg_fingerprint: 5a4005e257a53646 → 7b2fd89bae0acfcb
- run_colab.log：`START deploy` 2 次、READY ['2026-11-09 08:50:32']、stop rc=0 行 2、tailnet_cleanup ['"logout-unconfirmed"', '"ephemeral-logout-confirmed"']；余额行（unit）无（费用数不能从本目录复算）

