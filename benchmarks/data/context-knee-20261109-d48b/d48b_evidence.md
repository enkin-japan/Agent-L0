# D48b 批证据（`d48b_evidence.py` 生成；data = `context-knee-20261109-d48b`，对照 d48 = `context-knee-20261109-d48`、p2b = `context-knee-20261108-p2b`）

常量：ceiling 53,248（= max_model_len 65,536 − reserve 12,288）、hard 51,200、soft 24,576；bootstrap 10000 次、种子 20261031，只按题重抽（P2.paired_item 同法）。臂简称 none / rev / w1。

## a. 输入与去重

- DONE：`BATCH_RC=0 SKIPPED=0`；arms_status：`orch-turn-none ok / orch-turn-w1w2-rev ok / orch-turn-w1 ok`
- 常量核对：本批 rev / w1 与 d48 rev 每行 guard_thresholds.hard = 51,200、soft = 24,576、context_limit − reserve = 53,248、context_limit = 65,536（脚本断言）

| 臂 | 原始行 | A.load_group 有效 | missing(error) | 丢弃重复 | 顶层 error 键行 | 含 `"error"` 字样行 | 按档 run 数 | sessions 目录数 | seed≠1107×1000+rep 或未发送 |
|---|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 140 | 140 | 0 | 0 | 0 | 1 | {0: 20, 49152: 60, 57344: 60} | 140 | 0 |
| orch-turn-w1w2-rev | 140 | 140 | 0 | 0 | 0 | 0 | {0: 20, 49152: 60, 57344: 60} | 140 | 0 |
| orch-turn-w1 | 140 | 140 | 0 | 0 | 0 | 1 | {0: 20, 49152: 60, 57344: 60} | 140 | 0 |
- none vs rev 同 (level,item,rep) 140 对：fill.fingerprint 相同 140、task_fp 相同 140
- none vs w1 同 (level,item,rep) 140 对：fill.fingerprint 相同 140、task_fp 相同 140
- 跨批 d48 none → 本批 none 同键 140 对：fill.fingerprint 相同 140
- 跨批 d48 rev → 本批 rev 同键 140 对：fill.fingerprint 相同 140
- orch-turn-none 填充档 (level,item) 20 组：rep 间 fill.fingerprint 不全互异的组 0
- orch-turn-w1w2-rev 填充档 (level,item) 20 组：rep 间 fill.fingerprint 不全互异的组 0
- orch-turn-w1 填充档 (level,item) 20 组：rep 间 fill.fingerprint 不全互异的组 0
  - orch-turn-none 第 72 行 L49152 lc-hard-009 r3：顶层 error 键 False；type=error 工具调用 ['turns[2].tool_calls[4]']；score.pass False
  - orch-turn-w1 第 10 行 L0 lc-hard-005 r1：顶层 error 键 False；type=error 工具调用 ['turns[1].tool_calls[3]']；score.pass True

## s. 冒烟漂移探针（d48 `smoke-orch-20261109-083103-A` vs 本批 `smoke-orch-20261109-190601-A`）

- 输入 L0 lc-hard-001 r0 / L0 lc-hard-001 r0，seed 1107000 / 1107000；fill.fingerprint 相同 True；task_fp 相同 True
  - 非输出字段差 `agent_cfg_fingerprint`："5a4005e257a53646" → "7b2fd89bae0acfcb"
  - 非输出字段差 `guard_thresholds`：{"fold_mode": "full", "hard": 39322, "reserve": 12288, "soft": 24576} → {"fold_mode": "minimal", "hard": 51200, "reserve": 12288, "soft": 24576}
- output_sig（全部轮输出 + 最终答复）逐字相同 False；最终答复 turn_response 相同 False；first_divergent_turn = 3；轮数 6 / 6；生成次数 6 / 6；pass True / False
- gens prompt_tokens：d48 [6191, 6408, 6534, 7242, 7598, 7870] / 本批 [6191, 6408, 6534, 7242, 7643, 7905]
- turn 3.thoughts：长度 305 / 376，首个不同字符位置 6
- 本批 none 同键行 vs 本批冒烟（同 VM）：output_sig 相同 True；first_divergent_turn = None
- 本批 rev 同键行 vs 本批冒烟（同 VM）：output_sig 相同 True；first_divergent_turn = None
- 本批 w1 同键行 vs 本批冒烟（同 VM）：output_sig 相同 False；first_divergent_turn = 0
- d48 none 同键行 vs 本批冒烟（跨 VM）：output_sig 相同 False；first_divergent_turn = 3
- d48 rev 同键行 vs 本批冒烟（跨 VM）：output_sig 相同 False；first_divergent_turn = 3

## b. L0 口径锚与同 VM 共同随机数

| 组 | L0 rep | 成功率 k/n | Pass^2 |
|---|---|---|---|
| 本批 none | 2 | 12/20 = 60.0% | 3/10 = 30.0% |
| 本批 rev | 2 | 12/20 = 60.0% | 3/10 = 30.0% |
| 本批 w1 | 2 | 11/20 = 55.0% | 3/10 = 30.0% |
| d48 none | 2 | 13/20 = 65.0% | 5/10 = 50.0% |
| d48 rev | 2 | 13/20 = 65.0% | 5/10 = 50.0% |

- L0 none vs rev：输出逐字相同 20/20；gens prompt_tokens 序列全同 20/20
- L0 none vs w1：输出逐字相同 0/20；gens prompt_tokens 序列全同 0/20
- L0 w1 vs rev：输出逐字相同 0/20；gens prompt_tokens 序列全同 0/20
- orch-turn-none L0 护栏事件：context_soft 0、context_hard 0、context_guard_fold 0、step_compaction 0、step_readback_exempt 0、turn_abort 0
- orch-turn-w1w2-rev L0 护栏事件：context_soft 0、context_hard 0、context_guard_fold 0、step_compaction 0、step_readback_exempt 0、turn_abort 0
- orch-turn-w1 L0 护栏事件：context_soft 0、context_hard 0、context_guard_fold 0、step_compaction 0、step_readback_exempt 0、turn_abort 0
- L49152 none vs rev（同 VM）：(首生成输入相同, 首轮输出相同) → {(True, False): 9, (True, True): 51}；全程逐字相同 16/60；first_divergent_turn {0:9, 2:2, 3:5, 4:22, 5:2, 6:1, 8:2, 10:1, None:16}
- L57344 none vs rev（同 VM）：(首生成输入相同, 首轮输出相同) → {(False, False): 57, (False, True): 3}；全程逐字相同 0/60；first_divergent_turn {0:57, 1:3}
- L49152 none vs w1（同 VM）：(首生成输入相同, 首轮输出相同) → {(True, False): 50, (True, True): 10}；全程逐字相同 1/60；first_divergent_turn {0:50, 1:6, 3:3, None:1}
- L57344 none vs w1（同 VM）：(首生成输入相同, 首轮输出相同) → {(False, False): 57, (False, True): 3}；全程逐字相同 0/60；first_divergent_turn {0:57, 1:3}
- L49152 w1 vs rev（同 VM）：(首生成输入相同, 首轮输出相同) → {(True, False): 41, (True, True): 19}；全程逐字相同 10/60；first_divergent_turn {0:41, 1:6, 3:1, 7:1, 9:1, None:10}
- L57344 w1 vs rev（同 VM）：(首生成输入相同, 首轮输出相同) → {(True, True): 60}；全程逐字相同 36/60；first_divergent_turn {4:2, 5:5, 6:4, 7:3, 8:6, 9:3, 12:1, None:36}

### b2. 同 VM 首轮分叉按运行日期拆（日期 = run 切片首个事件 time；无事件的 run 用 臂 start + 行序累加 wall_s 估计）

- none：切片无事件、退回估计的 run 1；按日期的 run 数 {(0, '2026-11-09'): 20, (49152, '2026-11-09'): 60, (57344, '2026-11-09'): 60}；估计末 run 结束 11-09 22:32:43 对 log harness rc 行 11-09 22:33:19（差 35 s）
- rev：切片无事件、退回估计的 run 0；按日期的 run 数 {(0, '2026-11-09'): 20, (49152, '2026-11-09'): 49, (49152, '2026-11-10'): 11, (57344, '2026-11-10'): 60}；估计末 run 结束 11-10 01:42:09 对 log harness rc 行 11-10 01:43:31（差 82 s）
- w1：切片无事件、退回估计的 run 0；按日期的 run 数 {(0, '2026-11-10'): 20, (49152, '2026-11-10'): 60, (57344, '2026-11-10'): 60}；估计末 run 结束 11-10 04:53:05 对 log harness rc 行 11-10 04:53:43（差 37 s）

- 事件日期 ≠ 累加估计日期（归属改变）：1 run
  - rev transcripts 第 70 行 L49152 lc-hard-009 r1：估计起点 11-09 23:59:53，首事件 2026-11-10 00:00:10，末事件 2026-11-10 00:01:09；首轮与 none 同键行逐字相同 True
- 切片事件跨午夜（首末事件日期不同）：1 run
  - rev transcripts 第 69 行 L49152 lc-hard-009 r0：估计起点 11-09 23:57:52，首事件 2026-11-09 23:58:08，末事件 2026-11-10 00:00:10；首轮与 none 同键行逐字相同 True

| 对 | level | (两侧同日?, 首生成 prompt_tokens 相同?, 首轮输出逐字相同?) → run 数 |
|---|---|---|
| none vs rev | 0 | {(True, True, True): 20} |
| none vs rev | 49152 | {(False, True, False): 9, (False, True, True): 2, (True, True, True): 49} |
| none vs rev | 57344 | {(False, False, False): 57, (False, False, True): 3} |
| none vs w1 | 0 | {(False, True, False): 15, (False, True, True): 5} |
| none vs w1 | 49152 | {(False, True, False): 50, (False, True, True): 10} |
| none vs w1 | 57344 | {(False, False, False): 57, (False, False, True): 3} |
| w1 vs rev | 0 | {(False, True, False): 15, (False, True, True): 5} |
| w1 vs rev | 49152 | {(False, True, False): 41, (False, True, True): 8, (True, True, True): 11} |
| w1 vs rev | 57344 | {(True, True, True): 60} |

- 读法：首生成 token 数相同、同种子，首轮分叉集中在跨日期的对 → 输入里的 `Current date: YYYY-MM-DD (Weekday)` 随本机日期变（两种日期写法 token 数相同）；同 VM 共同随机数只在同日期的 run 之间成立。首事件时间晚于首次渲染，跨午夜边界的 run 归属可能差一天（见上「归属改变」行的首轮比对）。另：analyze_p2 自动部分 W1 节报 d48 → 本批 rev 57K 的 system.json sha 60/60 相同（该数取自 analyze_p2，非本脚本计算），与日期块拼在 prompt 尾部、不在 system.json 一致。

## d. 零分布门槛（V2c 同法：同一臂内两组不相交 m 个 rep 当两条假臂，枚举全部无序拆分得 |Δ|；门槛 t = P(|Δ|≥t) ≤ 5% 的最小可取值；— = 超出支撑）

| 零分布 | 拆分数 m=3 | 成功率 m=3 | Pass^2 m=3 | 拆分数 m=2 | 成功率 m=2 | Pass^2 m=2 |
|---|---|---|---|---|---|---|
| 本批 L49152 臂内三臂 | 30 | —（30:10.0%） | ≥30（3.3%） | 135 | ≥40（2.2%） | ≥40（3.7%） |
| 本批 L57344 臂内三臂 | 30 | ≥27（3.3%） | ≥27（3.3%） | 135 | ≥30（3.0%） | ≥40（1.5%） |
| 本批 49K+57K 臂内三臂 | 60 | ≥30（5.0%） | ≥27（3.3%） | 270 | ≥35（3.3%） | ≥40（2.6%） |
| d48 L49152 臂内 none+rev | 20 | ≥23（5.0%） | ≥23（5.0%） | 90 | ≥30（2.2%） | ≥40（2.2%） |
| d48 L57344 臂内 none+rev | 20 | ≥27（5.0%） | ≥20（5.0%） | 90 | —（35:6.7%） | ≥40（1.1%） |
| d48 49K+57K 臂内 none+rev | 40 | ≥23（5.0%） | ≥23（2.5%） | 180 | ≥35（3.3%） | ≥40（1.7%） |
| p2b none+full L0 | 20 | ≥23（5.0%） | ≥27（5.0%） | 90 | ≥30（2.2%） | —（40:5.6%） |

- 本批 L0 各臂 2 rep，拆不出零分布；本批同法零分布 = 填充档臂内 6 rep（每臂每档 10 种 m=3 拆分，三臂 30 种 / 两档合并 60 种）。d48 行复现 d48 证据 d 节 23/23、27/20、23/23，p2b 行复现 23 / 27（均脚本断言）。

## c. §2 三条（按题配对、只按题重抽 bootstrap 95%，种子 20261031；b − a）

- 本批各臂拐点（Pass^2 规则，L0 只 2 rep）：{'orch-turn-none': '到 57344 未见拐点', 'orch-turn-w1w2-rev': '到 57344 未见拐点', 'orch-turn-w1': '到 57344 未见拐点'}

### c1a. 第一条：rev 对 none（门：比 ≥ 0.90，§2 原文两项都是 90%）

| level | 成功率 none → rev | 差 [CI]（题升/降） | 比 [CI] | 尾概率 本档三臂 / 合并三臂 m=3 | Pass^2 none → rev | 差 [CI]（题升/降） | 比 [CI] | 尾概率 本档三臂 / 合并三臂 m=3 |
|---|---|---|---|---|---|---|---|---|
| 49152 | 53.3% → 48.3% | -5.0 [-16.7,+3.3]（1/2） | 0.906 [0.71,1.07] | 80% / 77% | 30.0% → 24.7% | -5.3 [-19.3,+4.7]（1/2） | 0.822 [0.42,1.20] | 70% / 70% |
| 57344 | 50.0% → 43.3% | -6.7 [-15.0,+1.7]（1/4） | 0.867 [0.64,1.03] | 73% / 77% | 28.0% → 25.3% | -2.7 [-6.0,+0.7]（1/4） | 0.905 [0.65,1.02] | 93% / 93% |
| 49K+57K 合并 | 51.7% → 45.8% | -5.8 [-11.7,0.0]（2/6） | 0.887 [0.78,1.00] | 77% | 29.0% → 25.0% | -4.0 [-10.7,+1.3]（2/6） | 0.862 [0.66,1.05] | 70% |

门槛判读（|观察差| 对 m=3 门槛）：
- 49152：成功率 -5.0 对 t=— → 不判；Pass^2 -5.3 对 t=30 → 噪声内
- 57344：成功率 -6.7 对 t=27 → 噪声内；Pass^2 -2.7 对 t=27 → 噪声内
- 49K+57K 合并：成功率 -5.8 对 t=30 → 噪声内；Pass^2 -4.0 对 t=27 → 噪声内

### c1b. 第一条：w1 对 none（门：比 ≥ 0.90）

| level | 成功率 none → w1 | 差 [CI]（题升/降） | 比 [CI] | 尾概率 本档三臂 / 合并三臂 m=3 | Pass^2 none → w1 | 差 [CI]（题升/降） | 比 [CI] | 尾概率 本档三臂 / 合并三臂 m=3 |
|---|---|---|---|---|---|---|---|---|
| 49152 | 53.3% → 58.3% | +5.0 [-8.3,+20.0]（3/4） | 1.094 [0.86,1.46] | 80% / 77% | 30.0% → 34.7% | +4.7 [-11.3,+22.7]（3/4） | 1.156 [0.70,2.28] | 70% / 70% |
| 57344 | 50.0% → 46.7% | -3.3 [-13.3,+5.0]（2/3） | 0.933 [0.70,1.10] | 73% / 85% | 28.0% → 29.3% | +1.3 [-4.7,+9.3]（2/3） | 1.048 [0.73,1.29] | 93% / 93% |
| 49K+57K 合并 | 51.7% → 52.5% | +0.8 [-8.3,+10.0]（4/5） | 1.016 [0.84,1.22] | 85% | 29.0% → 32.0% | +3.0 [-6.7,+14.3]（4/5） | 1.103 [0.78,1.68] | 93% |

门槛判读（|观察差| 对 m=3 门槛）：
- 49152：成功率 +5.0 对 t=— → 不判；Pass^2 +4.7 对 t=30 → 噪声内
- 57344：成功率 -3.3 对 t=27 → 噪声内；Pass^2 +1.3 对 t=27 → 噪声内
- 49K+57K 合并：成功率 +0.8 对 t=30 → 噪声内；Pass^2 +3.0 对 t=27 → 噪声内

### c2. 第二条：管理臂每 run 实发峰值 ≤ ceiling 53,248 且 0 次 turn_abort reason=context_guard

| 臂 | level | runs | 实发峰值 中位 / 最大 | ≤ 53,248 的 run | > 53,248 的生成次数 | ≥ hard 的 run | 峰值字段 ≠ 实发峰值 | turn_abort 原因 | reason=context_guard |
|---|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 0 | 20 | 7,398 / 11,037 | 20/20 | 0 | 0 | 0 | — | 0 |
| orch-turn-none | 49152 | 60 | 52,662 / 55,788 | 40/60 | 84 | 37 | 0 | {'max_iterations': 19} | 0 |
| orch-turn-none | 57344 | 60 | 59,680 / 63,582 | 0/60 | 569 | 60 | 0 | {'max_iterations': 20} | 0 |
| orch-turn-w1w2-rev | 0 | 20 | 7,398 / 11,037 | 20/20 | 0 | 0 | 0 | — | 0 |
| orch-turn-w1w2-rev | 49152 | 60 | 51,752 / 53,213 | 60/60 | 0 | 36 | 0 | {'max_iterations': 22} | 0 |
| orch-turn-w1w2-rev | 57344 | 60 | 48,562 / 52,702 | 60/60 | 0 | 7 | 0 | {'max_iterations': 19} | 0 |
| orch-turn-w1 | 0 | 20 | 7,586 / 11,123 | 20/20 | 0 | 0 | 0 | — | 0 |
| orch-turn-w1 | 49152 | 60 | 51,368 / 53,244 | 60/60 | 0 | 31 | 0 | {'max_iterations': 20} | 0 |
| orch-turn-w1 | 57344 | 60 | 49,234 / 53,194 | 60/60 | 0 | 15 | 0 | {'max_iterations': 19} | 0 |

- rev 全臂：实发峰值 ≤ 53,248 的 run 140/140，最大 53,213；guardrail_events 全文件 turn_abort reason 分布 {'max_iterations': 41}，reason=context_guard 0

- w1 全臂：实发峰值 ≤ 53,248 的 run 140/140，最大 53,244；guardrail_events 全文件 turn_abort reason 分布 {'max_iterations': 39}，reason=context_guard 0
- none（对照）：实发峰值 > 53,248 的 run 80/140，最大 63,582；turn_abort {'max_iterations': 39}；reason 全集（⊆ 已知取值，已核）['max_iterations']

### c3. 第三条：总耗时增量 ≤ +25%（Σwall = 去重行 wall_s 之和）

| level | Σwall none | Σwall rev | Σwall w1 | rev/none | w1/none | wall 中位 none / rev / w1 s |
|---|---|---|---|---|---|---|
| 0 | 8.0 | 8.0 | 8.4 | +0.1% | +6.0% | 19.1 / 19.1 / 24.3 |
| 49152 | 88.9 | 92.7 | 95.7 | +4.3% | +7.6% | 96.8 / 98.8 / 90.2 |
| 57344 | 94.7 | 88.2 | 85.4 | -6.9% | -9.8% | 83.4 / 82.2 / 82.1 |
| 49K+57K | 183.6 | 180.9 | 181.1 | -1.5% | -1.4% | — |
| 三档合计（含 L0） | 191.6 | 188.8 | 189.6 | -1.4% | -1.0% | — |

run_colab.log 臂墙钟：none 192.2、rev 190.2（-1.0%）、w1 190.2 min（-1.0%）。

## e1. D48b 专项：minimal fold（context_guard_fold；gap = before − hard + 1，实省 = before − after，目标 实省÷gap ≈ 1.1–1.3）

| 批 / 臂 | level | fold 事件 | 有 fold 的 run | iter1 / 后 | passes | mode / method | before | after | after < hard | hard − after | 实省 | gap | 实省 ÷ gap 中位（Q1–Q3；范围） | compacted | density | density_raw；= density 的次数 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| d48 rev | 0 | 0 | 0/20 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| d48 rev | 49152 | 8 | 8/60 | 0 / 8 | {1:8} | {'minimal': 8} / {'（无键）': 8} | 53,437（53,261–53,591） | 47,558（45,362–49,485） | 8/8 | 3,642（1,715–5,838） | 5,904（3,862–8,151） | 2,238（2,062–2,392） | 2.62（2.38–3.08；1.80–3.57） | 14（4–16） | （无键） | （无键）；0 |
| d48 rev | 57344 | 60 | 60/60 | 60 / 0 | {1:60} | {'minimal': 60} / {'（无键）': 60} | 57,554（56,693–59,097） | 41,104（35,044–46,354） | 60/60 | 10,096（4,846–16,156） | 16,568（11,215–24,006） | 6,356（5,494–7,898） | 2.57（2.42–2.81；1.76–3.40） | 35（24–50） | （无键） | （无键）；0 |
| 本批 rev | 0 | 0 | 0/20 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| 本批 rev | 49152 | 5 | 5/60 | 0 / 5 | {1:5} | {'minimal': 5} / {'tokenizer': 5} | 53,388（53,274–53,752） | 49,484（48,703–50,043） | 5/5 | 1,716（1,157–2,497） | 3,801（3,402–5,049） | 2,189（2,075–2,553） | 1.82（1.58–1.92；1.51–1.98） | 10（4–11） | 1.43（1.38–1.44；1.37–1.55） | 1.43（1.38–1.44；1.37–1.55）；5 |
| 本批 rev | 57344 | 60 | 60/60 | 60 / 0 | {1:60} | {'minimal': 60} / {'tokenizer': 60} | 57,554（56,693–59,097） | 46,508（42,307–48,892） | 60/60 | 4,692（2,308–8,893） | 11,096（8,035–16,743） | 6,356（5,494–7,898） | 1.74（1.62–1.83；1.33–2.14） | 24（12–40） | 1.52（1.47–1.56；1.40–1.69） | 1.52（1.47–1.56；1.40–1.69）；60 |
| 本批 w1 | 0 | 0 | 0/20 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| 本批 w1 | 49152 | 22 | 22/60 | 0 / 22 | {1:22} | {'minimal': 22} / {'tokenizer': 22} | 53,394（53,276–62,044） | 49,252（41,539–50,087） | 22/22 | 1,948（1,113–9,661） | 4,111（3,225–20,505） | 2,196（2,077–10,845） | 1.89（1.67–2.02；1.49–2.45） | 10（4–52） | 1.43（1.40–1.46；1.25–1.56） | 1.43（1.40–1.46；1.25–1.56）；22 |
| 本批 w1 | 57344 | 62 | 60/60 | 60 / 2 | {1:62} | {'minimal': 62} / {'tokenizer': 62} | 57,554（53,291–59,097） | 46,523（42,307–48,892） | 62/62 | 4,677（2,308–8,893） | 11,022（4,803–16,743） | 6,355（2,092–7,898） | 1.74（1.63–1.84；1.33–2.36） | 24（4–40） | 1.52（1.47–1.55；1.40–1.69） | 1.52（1.47–1.55；1.40–1.69）；62 |
- 系数确已生效（检验）本批 rev L49152：d48 rev 同档 实省÷gap 中位 2.62 ÷ 本批 fold density 中位 1.43 = 1.83，实测 1.82（只说明整段混合密度系数确实乘上去了；剩余超折的来源拆分见 e1c）
- 系数确已生效（检验）本批 rev L57344：d48 rev 同档 实省÷gap 中位 2.57 ÷ 本批 fold density 中位 1.52 = 1.69，实测 1.74（只说明整段混合密度系数确实乘上去了；剩余超折的来源拆分见 e1c）
- 系数确已生效（检验）本批 w1 L49152：d48 rev 同档 实省÷gap 中位 2.62 ÷ 本批 fold density 中位 1.43 = 1.83，实测 1.89（只说明整段混合密度系数确实乘上去了；剩余超折的来源拆分见 e1c）
- 系数确已生效（检验）本批 w1 L57344：d48 rev 同档 实省÷gap 中位 2.57 ÷ 本批 fold density 中位 1.52 = 1.70，实测 1.74（只说明整段混合密度系数确实乘上去了；剩余超折的来源拆分见 e1c）

首生成 prompt（gens[0].prompt_tokens；57K 管理臂 = 首生成前 fold 的折后值）：

| 组 | level | 首生成前 fold 的 run | 首生成 prompt 中位（范围） | hard − 首生成 中位 | fill.prompt_tokens_est 中位 |
|---|---|---|---|---|---|
| d48 none | 49152 | 0/60 | 49,382（48,775–50,586） | 1,818 | 49,382 |
| d48 none | 57344 | 0/60 | 57,554（56,693–59,097） | -6,354 | 57,554 |
| d48 rev | 49152 | 0/60 | 49,382（48,775–50,586） | 1,818 | 49,382 |
| d48 rev | 57344 | 60/60 | 41,104（35,044–46,354） | 10,096 | 57,554 |
| 本批 none | 49152 | 0/60 | 49,382（48,775–50,586） | 1,818 | 49,382 |
| 本批 none | 57344 | 0/60 | 57,554（56,693–59,097） | -6,354 | 57,554 |
| 本批 rev | 49152 | 0/60 | 49,382（48,775–50,586） | 1,818 | 49,382 |
| 本批 rev | 57344 | 60/60 | 46,508（42,307–48,892） | 4,692 | 57,554 |
| 本批 w1 | 49152 | 0/60 | 49,382（48,775–50,586） | 1,818 | 49,382 |
| 本批 w1 | 57344 | 60/60 | 46,508（42,307–48,892） | 4,692 | 57,554 |

### e1b. 逐代实测密度 measure_density（gens[*]，= 分词器 token ÷ fallback_estimate；本批新字段，d48 无）与 fold density 对照

| 臂 | level | 生成数 | 有 measure_density | measure_method | measure_source | measure_tokens = prompt_tokens | measure_density 中位（Q1–Q3；范围） | gens[0] 的 measure_density |
|---|---|---|---|---|---|---|---|---|
| none | 0 | 141 | 141 | {'tokenizer': 141} | {'proxy': 141} | 141 | 0.921（0.917–0.935；0.910–0.991） | 0.912（0.910–0.913；0.910–0.913） |
| none | 49152 | 601 | 601 | {'tokenizer': 601} | {'proxy': 601} | 601 | 1.466（1.427–1.529；1.352–1.709） | 1.488（1.453–1.545；1.398–1.709） |
| none | 57344 | 569 | 569 | {'tokenizer': 569} | {'proxy': 569} | 569 | 1.496（1.445–1.542；1.363–1.689） | 1.518（1.473–1.557；1.402–1.689） |
| rev | 0 | 141 | 141 | {'tokenizer': 141} | {'guard': 141} | 141 | 0.921（0.917–0.935；0.910–0.991） | 0.912（0.910–0.913；0.910–0.913） |
| rev | 49152 | 607 | 607 | {'tokenizer': 607} | {'guard': 607} | 607 | 1.446（1.165–1.520；1.112–1.709） | 1.488（1.453–1.545；1.398–1.709） |
| rev | 57344 | 576 | 576 | {'tokenizer': 576} | {'guard': 576} | 576 | 1.396（1.316–1.450；1.118–1.586） | 1.432（1.387–1.496；1.290–1.586） |
| w1 | 0 | 158 | 158 | {'tokenizer': 158} | {'guard': 158} | 158 | 0.921（0.917–0.934；0.910–0.986） | 0.912（0.910–0.913；0.910–0.913） |
| w1 | 49152 | 573 | 573 | {'tokenizer': 573} | {'guard': 573} | 573 | 1.465（1.418–1.529；1.068–1.709） | 1.488（1.453–1.545；1.398–1.709） |
| w1 | 57344 | 571 | 571 | {'tokenizer': 571} | {'guard': 571} | 571 | 1.414（1.373–1.458；1.276–1.586） | 1.432（1.387–1.496；1.290–1.586） |

fold 事件 density_raw（折前那次量值）对其后紧接发出的生成 gens[iteration−1].measure_density（折后 prompt），以及折前上一次发出的生成 gens[iteration−2]：

| 臂 | level | fold 数 | density_raw 中位 | 折后生成 density 中位 | 折后 − raw 中位 | 折前上一生成 density 中位 | 折后生成 prompt = fold.after 的次数 |
|---|---|---|---|---|---|---|---|
| rev | 49152 | 5 | 1.429 | 1.388 | -0.028 | 1.445 | 5/5 |
| rev | 57344 | 60 | 1.518 | 1.432 | -0.084 | — | 60/60 |
| w1 | 49152 | 22 | 1.427 | 1.394 | -0.033 | 1.435 | 22/22 |
| w1 | 57344 | 62 | 1.517 | 1.431 | -0.084 | 1.463 | 62/62 |

### e1c. 被折内容的边际密度与剩余超折拆分

边际密度 ρ = (before − after) ÷ (before ÷ density_raw − after ÷ d_after)，d_after = 折后首个生成 gens[iteration−1].measure_density（只取该生成 prompt_tokens = fold.after 的事件）；即被折掉部分的 实测 token ÷ fallback_estimate。密度错配 = ρ ÷ density_raw（候选正文比整段混合密度高多少）；折叠粒度 = (实省 ÷ gap) ÷ 密度错配（按校准后估值本应刚好够用、实际多折的倍数：minimal 按整条消息落刀，同一消息里其余观察一并折，orchestrator.py:655-656）。逐事件算后取中位（Q1–Q3；范围）。

| 臂 | level | 事件 | ρ 边际密度 | density_raw（混合） | 密度错配 ρ ÷ raw | 折叠粒度 | 实省 ÷ gap | 错配 × 粒度 = 实省÷gap 的事件 |
|---|---|---|---|---|---|---|---|---|
| rev | 49152 | 5 | 1.96（1.57–2.10；1.57–2.18） | 1.43（1.38–1.44；1.37–1.55） | 1.36（1.15–1.42；1.10–1.51） | 1.35（1.31–1.38；1.16–1.59） | 1.82（1.58–1.92；1.51–1.98） | 5/5 |
| rev | 57344 | 60 | 1.99（1.83–2.13；1.57–2.65） | 1.52（1.47–1.56；1.40–1.69） | 1.31（1.24–1.38；1.04–1.61） | 1.32（1.26–1.35；1.21–1.49） | 1.74（1.62–1.83；1.33–2.14） | 60/60 |
| w1 | 49152 | 22 | 1.91（1.74–2.11；1.40–2.70） | 1.43（1.40–1.46；1.25–1.56） | 1.35（1.18–1.48；0.97–1.83） | 1.37（1.30–1.51；1.19–1.62） | 1.89（1.67–2.02；1.49–2.45） | 22/22 |
| w1 | 57344 | 62 | 2.00（1.83–2.13；1.57–2.65） | 1.52（1.47–1.55；1.40–1.69） | 1.32（1.24–1.40；1.04–1.67） | 1.32（1.26–1.35；1.21–1.70） | 1.74（1.63–1.84；1.33–2.36） | 62/62 |

- 读法：剩余超折 = 密度错配 × 折叠粒度。若改用被折候选的边际密度校准（消掉错配项），实省 ÷ gap 预期 ≈ 折叠粒度中位，落在目标带 1.1–1.3 的上沿附近（推断，未测）。

- d48 rev：一 run 内 ≥ 2 次 fold 的 run 0；有 fold 的 run 里发出过 ≥ ceiling 生成的 run 0；context_hard 30 次（prompt 51,279–53,120，< hard 0；iteration=1 0）
- 本批 rev：一 run 内 ≥ 2 次 fold 的 run 0；有 fold 的 run 里发出过 ≥ ceiling 生成的 run 0；context_hard 43 次（prompt 51,267–53,130，< hard 0；iteration=1 0）
- 本批 w1：一 run 内 ≥ 2 次 fold 的 run 2；有 fold 的 run 里发出过 ≥ ceiling 生成的 run 0；context_hard 47 次（prompt 51,207–53,240，< hard 0；iteration=1 0）

## e2. W1 / W2 归因（rev = W1 + W2，w1 = 只 W1；两臂 guard_thresholds 相同，见 g）

### e2a. rev − w1（正 = 加 W2 更好）

| level | 成功率 w1 → rev | 差 [CI]（题升/降） | 比 [CI] | 尾概率 本档三臂 / 合并三臂 m=3 | Pass^2 w1 → rev | 差 [CI]（题升/降） | 比 [CI] | 尾概率 本档三臂 / 合并三臂 m=3 |
|---|---|---|---|---|---|---|---|---|
| 49152 | 58.3% → 48.3% | -10.0 [-26.7,+5.0]（3/4） | 0.829 [0.59,1.09] | 67% / 50% | 34.7% → 24.7% | -10.0 [-29.3,+8.0]（3/4） | 0.712 [0.30,1.30] | 60% / 58% |
| 57344 | 46.7% → 43.3% | -3.3 [-8.3,0.0]（0/2） | 0.929 [0.83,1.00] | 73% / 85% | 29.3% → 25.3% | -4.0 [-10.7,0.0]（0/2） | 0.864 [0.69,1.00] | 70% / 70% |
| 49K+57K 合并 | 52.5% → 45.8% | -6.7 [-14.2,+0.8]（2/4） | 0.873 [0.74,1.02] | 77% | 32.0% → 25.0% | -7.0 [-17.7,+2.7]（3/4） | 0.781 [0.52,1.11] | 58% |

门槛判读（|观察差| 对 m=3 门槛）：
- 49152：成功率 -10.0 对 t=— → 不判；Pass^2 -10.0 对 t=30 → 噪声内
- 57344：成功率 -3.3 对 t=27 → 噪声内；Pass^2 -4.0 对 t=27 → 噪声内
- 49K+57K 合并：成功率 -6.7 对 t=30 → 噪声内；Pass^2 -7.0 对 t=27 → 噪声内

### e2c. 分叉对的通过翻转（配对键 (level,item,rep)；只数 output_sig 不同的对；按 b2 日期（首事件 time）拆同日 / 跨日；同题多 rep 相关未校正）

| 对 a → b | level | 同日：分叉对 / a 过 b 不过 / a 不过 b 过 | 跨日：分叉对 / a 过 b 不过 / a 不过 b 过 |
|---|---|---|---|
| none → rev | 49152 | 33 / 3 / 0 | 11 / 2 / 2 |
| none → rev | 57344 | 0 / 0 / 0 | 60 / 12 / 8 |
| none → w1 | 49152 | 0 / 0 / 0 | 59 / 7 / 10 |
| none → w1 | 57344 | 0 / 0 / 0 | 60 / 11 / 9 |
| w1 → rev | 49152 | 2 / 0 / 0 | 48 / 11 / 5 |
| w1 → rev | 57344 | 24 / 2 / 0 | 0 / 0 / 0 |

### e2d. 同日配对的通过翻转与符号检验（只用两侧同日的 (level,item,rep) 对 = 同 VM 共同随机数配对；同题多 rep 相关未校正）

| 对 a → b | level | 同日对 | 其中分叉 | a 过 b 不过 | a 不过 b 过 |
|---|---|---|---|---|---|
| none → rev | 0 | 20 | 0 | 0 | 0 |
| none → rev | 49152 | 49 | 33 | 3 | 0 |
| none → rev | 57344 | 0 | 0 | 0 | 0 |
| w1 → rev | 0 | 0 | 0 | 0 | 0 |
| w1 → rev | 49152 | 11 | 2 | 0 | 0 |
| w1 → rev | 57344 | 60 | 24 | 2 | 0 |
| none → w1 | 0 | 0 | 0 | 0 | 0 |
| none → w1 | 49152 | 0 | 0 | 0 | 0 |
| none → w1 | 57344 | 0 | 0 | 0 | 0 |

- rev 作 b 侧的同日配对合计（none → rev 与 w1 → rev，全部档）：对侧过 rev 不过 5、rev 过对侧不过 0；精确双侧符号检验 p = 0.0625（n = 5）。none → w1 无同日对。

### e2b. W2 事件（step_compaction / step_readback_exempt；按 (level, detail.level, trigger)）

- orch-turn-none：step_compaction 全文件 0（切片内 run 0）；trigger 分布 —（trigger=hard 0）；按 (档, level, trigger) —；step_readback_exempt 0
- orch-turn-w1w2-rev：step_compaction 全文件 84（切片内 run 54）；trigger 分布 {'step': 84}（trigger=hard 0）；按 (档, level, trigger) {(49152, 'ceiling', 'step'): 9, (49152, 'hard', 'step'): 39, (57344, 'ceiling', 'step'): 36}；step_readback_exempt 0
- orch-turn-w1：step_compaction 全文件 0（切片内 run 0）；trigger 分布 —（trigger=hard 0）；按 (档, level, trigger) —；step_readback_exempt 0
- 核对：w1 臂 W2 事件 0 次 → ✓ 为 0

### e3. 相邻两次生成 prompt 下降 > 2,000 tok（gens[i−1] → gens[i]，i ≥ 1）

| 批 / 臂 | level | 下降次数（run） | 下降前 prompt | 下降后 prompt | 下降前 < hard 的次数 | run 内首次下降后最小 prompt 中位 |
|---|---|---|---|---|---|---|
| d48 rev | 49152 | 36（31） | 52,180（45,452–53,216） | 27,112（22,720–49,485） | 9 | 26,585 |
| d48 rev | 57344 | 27（27） | 44,143（39,067–48,127） | 29,834（25,684–34,558） | 27 | 29,834 |
| 本批 none | 49152 | 0（0） | — | — | 0 | — |
| 本批 none | 57344 | 0（0） | — | — | 0 | — |
| 本批 rev | 49152 | 32（31） | 52,341（48,703–53,213） | 26,730（22,839–49,926） | 4 | 26,646 |
| 本批 rev | 57344 | 24（24） | 49,919（47,732–52,702） | 29,970（25,454–33,861） | 19 | 29,970 |
| 本批 w1 | 49152 | 21（21） | 53,123（49,760–53,244） | 49,220（41,539–50,087） | 2 | 49,220 |
| 本批 w1 | 57344 | 2（2） | 53,029（52,971–53,087） | 48,549（48,352–48,746） | 0 | 48,549 |

### e4. 护栏事件按档（切片 guard_events_lines）

| 批 / 臂 | level | runs | soft（run） | hard iter1 / 后（run） | fold iter1 / 后（run） | step_compaction（run） |
|---|---|---|---|---|---|---|
| d48 rev | 0 | 20 | 0（0） | 0 / 0（0） | 0 / 0（0） | 0（0） |
| d48 rev | 49152 | 60 | 60（60） | 0 / 30（30） | 0 / 8（8） | 44（29） |
| d48 rev | 57344 | 60 | 60（60） | 0 / 0（0） | 60 / 0（60） | 38（28） |
| 本批 rev | 0 | 20 | 0（0） | 0 / 0（0） | 0 / 0（0） | 0（0） |
| 本批 rev | 49152 | 60 | 60（60） | 0 / 36（36） | 0 / 5（5） | 48（30） |
| 本批 rev | 57344 | 60 | 60（60） | 0 / 7（7） | 60 / 0（60） | 36（24） |
| 本批 w1 | 0 | 20 | 0（0） | 0 / 0（0） | 0 / 0（0） | 0（0） |
| 本批 w1 | 49152 | 60 | 60（60） | 0 / 32（32） | 0 / 22（22） | 0（0） |
| 本批 w1 | 57344 | 60 | 60（60） | 0 / 15（15） | 60 / 2（60） | 0（0） |

## x. 跨批同臂（d48 → 本批；跨 VM、同种子同输入，不是同 VM 配对）

| 臂 | level | 同键 | 输出逐字相同 | 首生成 prompt 相同 | 成功率 d48 → 本批 | Pass^2 d48 → 本批 | 差（成功率 / Pass^2）[CI] |
|---|---|---|---|---|---|---|---|
| none | 0 | 20 | 2 | 20 | 65.0% → 60.0% | 50.0% → 30.0% | -5.0 [-25.0,+15.0] / -20.0 [-50.0,0.0] |
| none | 49152 | 60 | 1 | 60 | 45.0% → 53.3% | 22.7% → 30.0% | +8.3 [-8.3,+25.0] / +7.3 [-14.0,+28.0] |
| none | 57344 | 60 | 0 | 60 | 43.3% → 50.0% | 22.7% → 28.0% | +6.7 [-3.3,+18.3] / +5.3 [-7.3,+20.0] |
| rev | 0 | 20 | 2 | 20 | 65.0% → 60.0% | 50.0% → 30.0% | -5.0 [-25.0,+15.0] / -20.0 [-50.0,0.0] |
| rev | 49152 | 60 | 1 | 60 | 48.3% → 48.3% | 26.7% → 24.7% | 0.0 [-16.7,+15.0] / -2.0 [-23.3,+18.0] |
| rev | 57344 | 60 | 0 | 0 | 40.0% → 43.3% | 18.0% → 25.3% | +3.3 [-16.7,+20.0] / +7.3 [-8.7,+23.3] |

## t. 时长与租约（run_colab.log）

- 本批：none 19:21→22:33（192.2 min）；rev 22:33→01:43（190.2 min）；w1 01:43→04:53（190.2 min）
  - START deploy 1 次（19:06:02）；READY 19:20:08；末次 START deploy → stop rc 行 9.80 h（MAX_VM_HOURS=10）；首行 → DONE 587.8 min（9.80 h，首行 19:06:01）；非 config 行里 lease/租约/切段/rotate 字样 0 行；tailnet_cleanup ['"ephemeral-logout-confirmed"']
- d48：none 08:51→12:15（204.2 min）；rev 12:15→15:27（192.2 min）
  - START deploy 2 次（08:31:04, 08:32:44）；READY 08:50:32；末次 START deploy → stop rc 行 6.92 h（MAX_VM_HOURS=10）；首行 → DONE 417.0 min（6.95 h，首行 08:31:03）；非 config 行里 lease/租约/切段/rotate 字样 1 行；tailnet_cleanup ['"logout-unconfirmed"', '"ephemeral-logout-confirmed"']

## g. 口径核对

- orch-turn-none summary.json：guard="off"；guard_thresholds={"fold_mode": "minimal", "hard": 51200, "reserve": 12288, "soft": 24576}；step_compaction="off"；step_compaction_cfg={"cold_refill_trigger_level": "hard", "digest_chars": 200, "keep_recent": 2, "trigger_level": "soft"}；agent_cfg_fingerprint="dab98cc8420c493b"；level_reps={"0": 2, "49152": 6, "57344": 6}；seed=1107；prefix_mode="prod"；tool_schema_mode_effective="catalog"；context_limit=65536
- orch-turn-w1w2-rev summary.json：guard="on"；guard_thresholds={"fold_mode": "minimal", "hard": 51200, "reserve": 12288, "soft": 24576}；step_compaction="on"；step_compaction_cfg={"cold_refill_trigger_level": "hard", "digest_chars": 200, "keep_recent": 2, "trigger_level": "soft"}；agent_cfg_fingerprint="7b2fd89bae0acfcb"；level_reps={"0": 2, "49152": 6, "57344": 6}；seed=1107；prefix_mode="prod"；tool_schema_mode_effective="catalog"；context_limit=65536
- orch-turn-w1 summary.json：guard="on"；guard_thresholds={"fold_mode": "minimal", "hard": 51200, "reserve": 12288, "soft": 24576}；step_compaction="off"；step_compaction_cfg={"cold_refill_trigger_level": "hard", "digest_chars": 200, "keep_recent": 2, "trigger_level": "soft"}；agent_cfg_fingerprint="d3a8d307004f3cb2"；level_reps={"0": 2, "49152": 6, "57344": 6}；seed=1107；prefix_mode="prod"；tool_schema_mode_effective="catalog"；context_limit=65536
- orch-turn-none 行内取值不唯一的配置键：{'prefix_n_tools': ['{"grammar": 61, "prompt": 61, "prompt_full": 17}', '{"grammar": 64, "prompt": 64, "prompt_full": 20}'], 'max_total_iterations': ['11', '12', '13', '14', '16']}
- orch-turn-w1w2-rev 行内取值不唯一的配置键：{'prefix_n_tools': ['{"grammar": 61, "prompt": 61, "prompt_full": 17}', '{"grammar": 64, "prompt": 64, "prompt_full": 20}'], 'max_total_iterations': ['11', '12', '13', '14', '16']}
- orch-turn-w1 行内取值不唯一的配置键：{'prefix_n_tools': ['{"grammar": 61, "prompt": 61, "prompt_full": 17}', '{"grammar": 64, "prompt": 64, "prompt_full": 20}'], 'max_total_iterations': ['11', '12', '13', '14', '16']}
- 行内配置键 none ≠ rev：['guard', 'step_compaction', 'agent_cfg_fingerprint', 'orchestrator_cfg_source']
- 行内配置键 none ≠ w1：['guard', 'agent_cfg_fingerprint', 'orchestrator_cfg_source']
- 行内配置键 w1 ≠ rev：['step_compaction', 'agent_cfg_fingerprint', 'orchestrator_cfg_source']
- 行内配置键 d48 none ≠ 本批 none：guard_thresholds: ['{"fold_mode": "full", "hard": 39322, "reserve": 12288, "soft": 24576}'] → ['{"fold_mode": "minimal", "hard": 51200, "reserve": 12288, "soft": 24576}']；agent_cfg_fingerprint: ['"bd006ebc49cf403b"'] → ['"dab98cc8420c493b"']
- 行内配置键 d48 rev ≠ 本批 rev：
- harness_mode_of 每臂取值数：{'none': 1, 'rev': 1, 'w1': 1}
- harness_mode w1 → rev 不同的键：step_compaction: off → on；agent_cfg_fingerprint: d3a8d307004f3cb2 → 7b2fd89bae0acfcb
- run_colab.log：余额行（unit）无（费用数不能从本目录复算）；臂命令行：orch-turn-none `--orchestrator --fill-placement turn --guard off --step-compaction off --prefix prod --levels 0,49152,57344`；orch-turn-w1w2-rev `--orchestrator --fill-placement turn --guard on --step-compaction on --guard-hard-tokens 51200 --guard-fold-mode minimal --prefix prod --levels 0,49152,57344`；orch-turn-w1 `--orchestrator --fill-placement turn --guard on --step-compaction off --prefix prod --levels 0,49152,57344`

