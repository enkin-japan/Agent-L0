# w12eval 证据（`w12eval_evidence.py` 生成；data = `context-knee-20261108-w12eval`，p2b = `context-knee-20261108-p2b`）

## 1a. 拷贝前缀逐字节核对（本批前 k 行 / 前 k 字节 vs p2b 同名文件全文）

| 臂 | 文件 | p2b 行数 | 本批行数 | 前缀 sha256[:16] 本批 / p2b | 逐字节相同 |
|---|---|---|---|---|---|
| orch-turn-none | transcripts.jsonl | 120 | 240 | 332a078a948c4be3 / 332a078a948c4be3 | True |
| orch-turn-none | guardrail_events.jsonl | 278 | 625 | e7b1a6d548c13d98 / e7b1a6d548c13d98 | True |
| orch-turn-none | sessions/ | 120 目录 | 240 目录 | p2b 目录缺 0、文件 360 个中不同 0 | True |
| orch-turn-w1w2 | transcripts.jsonl | 80 | 200 | ca694193839edb13 / ca694193839edb13 | True |
| orch-turn-w1w2 | guardrail_events.jsonl | 377 | 983 | ee127415f35af939 / ee127415f35af939 | True |
| orch-turn-w1w2 | sessions/ | 80 目录 | 200 目录 | p2b 目录缺 0、文件 2232 个中不同 0 | True |

## 1b. 冒烟漂移探针（p2b `smoke-orch-20261108-123017-A` vs 本批 `smoke-orch-20261108-211442-A`）

- 输入：level 0 / lc-hard-001 / r0 / seed 1107000 vs 1107000；非输出字段差：无；agent_cfg_fingerprint 5a4005e257a53646 / 5a4005e257a53646
- output_sig 相同：False；first_divergent_turn = 3；轮数 6 / 7；pass False / True
- gens prompt_tokens：[6191, 6408, 6534, 7242, 7643, 7905] / [6191, 6408, 6534, 7242, 7621, 7830, 7795]
- gens generation_tokens：[73, 34, 185, 365, 236, 124] / [73, 34, 185, 346, 168, 115, 144]
- turn 3.thoughts：长度 376 / 324，首个不同字符位置 2；p2b `"I've finished reading all 5 files. Next, I'll calcu` / 本批 `"I need to sum up the amounts. Let me use run_math t`
- turn 3.tool_calls：长度 236 / 293，首个不同字符位置 71；p2b `s": {"code": "jan = 1200+830+455\nfeb = 990+1370+615\nmar = 1450+700+8` / 本批 `s": {"code": "jan = [1200, 830, 455]\nfeb = [990, 1370, 615]\nmar = [1`
- p2b orch-turn-none 同 (level,item,rep) 行 vs p2b 冒烟：output_sig 相同 True；vs 本批冒烟 first_divergent_turn = 3
- p2b orch-turn-w1w2 同 (level,item,rep) 行 vs p2b 冒烟：output_sig 相同 True；vs 本批冒烟 first_divergent_turn = 3

## 1c. 新 rep 2–5 的输入与配置（前 k 行 = 拷贝行，其余 = 本批新行；k 见 COPIED）

- orch-turn-none：新行 120，(level, rep) 计数 {(32768, 2): 10, (32768, 3): 10, (32768, 4): 10, (32768, 5): 10, (49152, 2): 10, (49152, 3): 10, (49152, 4): 10, (49152, 5): 10, (57344, 2): 10, (57344, 3): 10, (57344, 4): 10, (57344, 5): 10}；新旧行配置键取值集合不同的键：无；seed ≠ 1107×1000+rep 或未发送的行 0；system.json sha 旧 ['3e99a6a89bd79373'] / 新 ['3e99a6a89bd79373']
- orch-turn-w1w2：新行 120，(level, rep) 计数 {(32768, 2): 10, (32768, 3): 10, (32768, 4): 10, (32768, 5): 10, (49152, 2): 10, (49152, 3): 10, (49152, 4): 10, (49152, 5): 10, (57344, 2): 10, (57344, 3): 10, (57344, 4): 10, (57344, 5): 10}；新旧行配置键取值集合不同的键：无；seed ≠ 1107×1000+rep 或未发送的行 0；system.json sha 旧 ['3e99a6a89bd79373'] / 新 ['3e99a6a89bd79373']
- none vs w1w2 同 (level,item,rep)：配对 200，fill.fingerprint 相同 200，task_fp 相同 200，fill.prompt_tokens_measured 相同 80
- orch-turn-none 填充档 (level,item) 30 组：6 个 rep 的 fill.fingerprint 不全互异的组 0；task_fp 跨 rep 不唯一的组 0
- orch-turn-w1w2 填充档 (level,item) 30 组：6 个 rep 的 fill.fingerprint 不全互异的组 0；task_fp 跨 rep 不唯一的组 0
- 1d. L32768 none vs w1w2 rep 0–1（p2b VM）：(首生成输入相同, 首轮输出逐字相同) → {(True, True): 20}；first_divergent_turn 分布 {2: 1, 5: 1, 6: 3, 7: 2, 8: 2, None: 11}（None = 全程逐字相同）
- 1d. L32768 none vs w1w2 rep 2–5（本批 VM）：(首生成输入相同, 首轮输出逐字相同) → {(True, False): 33, (True, True): 7}；first_divergent_turn 分布 {0: 33, 1: 3, 2: 1, 3: 1, 4: 1, 5: 1}（None = 全程逐字相同）
- 抽样 none L49152 lc-hard-001：rep→(fingerprint, prompt_tokens_measured, gens[0].prompt_tokens) r0 225deec2b4d7c5d8/49479/49479；r1 34e1fd66cbb89f17/49698/49698；r2 7ac5bc4d9788951d/48967/48967；r3 e95b37fd31dc3c07/49120/49120；r4 9433bc3ca6239017/49355/49355；r5 43a32a3f9c83566b/49491/49491

## 2. 含 `"error"` 字样的行

- orch-turn-none 第 194 行：level 49152 / lc-hard-009 / r3；顶层 `error` 键 False；turn_status warning；system_errors 1；type=error 的工具调用 turns[2].tool_calls[4] `Malformed tool call: expected an inner <function=NAME>...</function> block inside <tool_ca` raw `'<function=read_file>\n<parameter=path>\nmetrics/gpu\n</paramete'`；score.pass False why ['步数超限: 14 > 10', '回合上限 12 耗尽仍在发调用', '无收尾答复']；事件 parse_error 1；进 A.load_group 有效行（计入分母）True；该组 missing（error 行）0

## 3. §2 验收（none → w1w2，b − a；只按题聚类 bootstrap 95%，种子 20261031、10000 次）

- 拐点（none）：Pass^2 规则 拐点 32768（基线 L0 60.0%）；成功率规则 拐点 32768；成功率带拐点（L0 按题 CI 下界 55.0%）→ 32768

零分布门槛（P(|Δ|≥t) ≤ 5% 的最小 t；— = 超出支撑，括号 = 最大值:尾概率）：

| 零分布 | 拆分数 m=3 | 成功率 m=3 | Pass^2 m=3 | 成功率 m=2 | Pass^2 m=2 |
|---|---|---|---|---|---|
| 本批 none L0 | 10 | —（20:10.0%） | —（23:10.0%） | —（25:8.9%） | ≥40（2.2%） |
| p2b none+full L0 | 20 | ≥23（5.0%） | ≥27（5.0%） | ≥30（2.2%） | —（40:5.6%） |
| 本批 L32768 臂内（none+w1w2） | 20 | ≥33（5.0%） | ≥30（5.0%） | ≥40（3.3%） | ≥40（3.3%） |
| 本批 L49152 臂内（none+w1w2） | 20 | ≥20（5.0%） | ≥27（5.0%） | ≥25（4.4%） | ≥40（2.2%） |
| 本批 L57344 臂内（none+w1w2） | 20 | ≥23（5.0%） | ≥20（5.0%） | —（25:6.7%） | ≥30（3.3%） |

| level | 成功率 none → w1w2 | 差 [CI]（升/降） | 比 [CI]（≥0.9） | 尾概率 本批L0 m=3 / p2b m=3 / 本档臂内 m=3 | Pass^2 none → w1w2 | 差 [CI]（升/降） | 比 [CI]（≥0.9） | 尾概率 本批L0 m=3 / p2b m=3 / 本档臂内 m=3 |
|---|---|---|---|---|---|---|---|---|
| 32768 | 38.3% → 43.3% | +5.0 [-6.7,+16.7]（5/2） | 1.13 [0.85,1.58] | 80% / 70% / 75% | 20.0% → 23.3% | +3.3 [-10.0,+14.7]（4/2） | 1.17 [0.60,2.15]（1 次分母 0 丢弃） | 100% / 95% / 100% |
| 49152 | 50.0% → 38.3% | -11.7 [-25.0,+3.3]（2/7） | 0.77 [0.50,1.07] | 50% / 40% / 30% | 30.0% → 20.0% | -10.0 [-18.0,-2.0]（1/6） | 0.67 [0.33,0.96] | 100% / 75% / 45% |
| 57344 | 45.0% → 38.3% | -6.7 [-21.7,+8.3]（3/5） | 0.85 [0.64,1.25] | 80% / 70% / 60% | 26.0% → 13.3% | -12.7 [-30.7,+2.7]（3/5） | 0.51 [0.28,1.25] | 80% / 60% / 25% |
| 三填充档合并 | 44.4% → 40.0% | -4.4 [-11.7,+3.3]（3/7） | 0.90 [0.77,1.10] | （合并无零分布） | 25.3% → 18.9% | -6.4 [-14.0,+0.7]（2/6） | 0.75 [0.56,1.04] | （合并无零分布） |

rep 段拆分（成功率 k/n；差 = w1w2 − none 按题配对，只按题 bootstrap）：

| level | rep 段 | none | w1w2 | 差 [CI]（升/降） |
|---|---|---|---|---|
| 32768 | rep 0–1 | 10/20 = 50.0% | 9/20 = 45.0% | -5.0 [-15.0,+0.0]（0/1） |
| 32768 | rep 2–5 | 13/40 = 32.5% | 17/40 = 42.5% | +10.0 [-7.5,+25.0]（6/2） |
| 49152 | rep 0–1 | 11/20 = 55.0% | 5/20 = 25.0% | -30.0 [-55.0,-5.0]（1/6） |
| 49152 | rep 2–5 | 19/40 = 47.5% | 18/40 = 45.0% | -2.5 [-17.5,+10.0]（3/3） |
| 57344 | rep 0–1 | 10/20 = 50.0% | 7/20 = 35.0% | -15.0 [-40.0,+15.0]（3/6） |
| 57344 | rep 2–5 | 17/40 = 42.5% | 16/40 = 40.0% | -2.5 [-25.0,+22.5]（3/4） |
| 三填充档合并 | rep 0–1 | 31/60 = 51.7% | 21/60 = 35.0% | -16.7 [-31.7,0.0]（2/7） |
| 三填充档合并 | rep 2–5 | 49/120 = 40.8% | 51/120 = 42.5% | +1.7 [-10.0,+14.2]（5/5） |

峰值 prompt（实发峰值 = max(gens.prompt_tokens)，O.sent_peak；拐点 = none Pass^2 拐点档 32768 → 0.7× = 22,938 tok）：

| 臂 | level | 实发峰值 中位 / 最小 / 最大 | 中位 ÷ 拐点 | ≤ 0.7×拐点 的 run | ≤ 0.7×49152（= 34,406）的 run（对照） | 首生成 prompt 中位 / 最小 / 最大 | fill.prompt_tokens_est 中位 |
|---|---|---|---|---|---|---|---|
| orch-turn-none | 32768 | 36,078 / 33,732 / 39,888 | 1.10 | 0/60 | 14/60 | 32,928 / 32,398 / 34,017 | 32,928 |
| orch-turn-none | 49152 | 51,721 / 49,957 / 55,511 | 1.58 | 0/60 | 0/60 | 49,382 / 48,775 / 50,586 | 49,382 |
| orch-turn-none | 57344 | 59,458 / 57,833 / 63,612 | 1.81 | 0/60 | 0/60 | 57,554 / 56,693 / 59,097 | 57,554 |
| orch-turn-w1w2 | 32768 | 35,723 / 33,481 / 38,784 | 1.09 | 0/60 | 15/60 | 32,928 / 32,398 / 34,017 | 32,928 |
| orch-turn-w1w2 | 49152 | 51,688 / 49,686 / 53,244 | 1.58 | 0/60 | 0/60 | 49,428 / 48,820 / 50,631 | 49,382 |
| orch-turn-w1w2 | 57344 | 30,144 / 25,215 / 46,753 | 0.92 | 0/60 | 57/60 | 27,158 / 22,804 / 30,696 | 57,554 |

用时（Σwall = 去重行 wall_s 之和，含 rep 0–1 拷贝行（p2b VM）；增量 = w1w2 ÷ none − 1）：

| level | Σwall none min | Σwall w1w2 min | 增量（≤ +25%） | wall 中位 none / w1w2 s |
|---|---|---|---|---|
| 32768 | 73.9 | 75.5 | +2.2% | 76.0 / 81.1 |
| 49152 | 90.9 | 93.7 | +3.1% | 96.1 / 99.7 |
| 57344 | 97.0 | 73.9 | -23.8% | 88.3 / 76.8 |
| 三填充档合并 | 261.8 | 243.1 | -7.1% | — |
| 三填充档 rep 0–1 | 81.0 | 81.9 | +1.1% | — |
| 三填充档 rep 2–5 | 180.8 | 161.2 | -10.8% | — |

本批 run_colab.log 臂墙钟（只含新 rep 2–5 的 120 run）：none 182.2 min、w1w2 162.2 min（-11.0%）

## 4. W1 / W2 触发（guardrail_events.jsonl，按行 guard_events_lines 切片；括号 = 有该事件的 run 数；fold/hard 的 iter1 = detail.iteration == 1，即首次生成前）

| 臂 | level | runs | context_soft | context_hard（iter1 / 后） | context_guard_fold（iter1 / 后） | step_compaction（按 detail.level） | step_readback_exempt | 事件 session 字段与切片一致 |
|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 32768 | 60 | 0（0） | 0 / 0（0） | 0 / 0（0） | 0（0） | 0（0） | 切片 224 / session 224，不符 0 |
| orch-turn-none | 49152 | 60 | 0（0） | 0 / 0（0） | 0 / 0（0） | 0（0） | 0（0） | 切片 148 / session 148，不符 0 |
| orch-turn-none | 57344 | 60 | 0（0） | 0 / 0（0） | 0 / 0（0） | 0（0） | 0（0） | 切片 163 / session 163，不符 0 |
| orch-turn-w1w2 | 32768 | 60 | 60（60） | 0 / 0（0） | 0 / 0（0） | 41（33）{'soft': 41} | 0（0） | 切片 307 / session 307，不符 0 |
| orch-turn-w1w2 | 49152 | 60 | 22（22） | 60 / 0（60） | 0 / 8（8） | 28（21）{'ceiling': 6, 'hard': 22} | 1（1） | 切片 287 / session 287，不符 0 |
| orch-turn-w1w2 | 57344 | 60 | 60（60） | 0 / 1（1） | 60 / 0（60） | 45（30）{'ceiling': 45} | 5（5） | 切片 357 / session 357，不符 0 |

- orch-turn-w1w2 L49152 rep 0–1：首生成前 fold 的 run 0/20；首生成 prompt 中位 49,435（49,068–49,927）
- orch-turn-w1w2 L49152 rep 2–5：首生成前 fold 的 run 0/40；首生成 prompt 中位 49,385（48,820–50,631）
- orch-turn-w1w2 L57344 rep 0–1：首生成前 fold 的 run 20/20；首生成 prompt 中位 27,089（24,161–29,644）
- orch-turn-w1w2 L57344 rep 2–5：首生成前 fold 的 run 40/40；首生成 prompt 中位 27,302（22,804–30,696）

