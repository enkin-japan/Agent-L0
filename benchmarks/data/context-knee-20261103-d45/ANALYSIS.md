# D45 验收批次：复算与结论（2026-11-03）

> 原始数据：本目录 `trace-d45-digest/`（工具段 200、turn 1 迭代 3）、`trace-d45-plan/`（工具段 0、turn 1 迭代 6）、`trace-d45-both/`（200、6），各 `transcripts.jsonl` 60 行 + `meta.json`；对照 `../context-knee-20261102-b2/trace-new-900/`（工具段字段缺键 = 旧格式、迭代 3），次参照同目录 `trace-new-640/`。三步同模型（Qwen3.8-27B-INT4）、同 seed 7、hard 10 题 × 3 rep、new 模板、900、drop_task on。`run_colab.log`：首次起机 FAIL_READY（`DONE.attempt1`、`arms_status.attempt1.txt` 三步 skipped，无产物），第二次 15:52 READY，三步 harness rc 0 / hung 0，`DONE BATCH_RC=0 SKIPPED=0`。`smoke-orch-*` 冒烟不入统计。
> 复算：`benchmarks/summary_trace/analyze_d45.py`（下文「自动部分」A–K 节原样）+ `summarize_trace.py` 四批合并（L 节原样）。CI 均为题级 bootstrap 95%（B=1000，seed 0），n = 10 题，区间宽，如实给出。

## 1. 结论

> 跨批告示：b2 对 d45 同时变了三件事——D46 修复（`_coerce`，见 §2 与结论 8）、new 模板新增的一句工具段说明（`engine.py` 工作区改动，对 plan 臂同样生效）、以及 turn 1 本身不是逐字复现（b2 对 digest 的 turn 1 env 快照 + 调用数只有 18/30 相同；批内 both 对 plan 也只有 17/30）。PRE 本不受工具段影响，但批内 PRE both − plan 仍有 −10.0 pt [−30.0, +0.0]（C 节）——这正是三者混杂产生的噪声量级的直接证据。下文「只变工具段」「只变 turn1」这类表头，只在批内对照（both − plan、both − digest）上严格成立；跨批对照（digest/plan/both 对 b2）的标题不代表真的只变了那一个因素。

1. **(a) 工具段起作用：重复功下降、POST pass 上升，但摘要续跑仍劣于原文续跑。** 最干净的单因素是批内 both − plan（同批、同 turn 1 迭代 6，只差工具段）：POST pass +16.7 pt [+0.0, +36.7]（点估计为正，区间下沿触 0；bootstrap 均值 ≤0 的比例 3.3%），POST−PRE pass 差 +26.7 pt [+6.7, +53.3]，Δ重复功 −1.43 [−2.50, −0.57]（C 节）。跨批 digest − b2（迭代都是 3）：POST pass 16/30 对 10/30 = +20.0 pt [+0.0, +43.3]（同样点估计为正、区间下沿触 0；bootstrap 均值 ≤0 的比例 4.5%），Δ重复功 +1.50 [+0.50, +2.77] 对 +2.93 [+1.97, +4.00]，差 −1.43 [−2.63, −0.13]；去掉 lc-hard-009（D46 修复的题）后为 +14.8 pt [−3.7, +37.0] / −1.04 [−2.11, +0.07]（C 节第二表）。但 digest 臂自身 POST 仍低于 PRE（22 → 16），Δ重复功区间仍在 0 以上：差距缩小了，没有消除。摘要内容也变了：[Key values] 含文件路径 30/30（b2 23/30，H 节），抽样里 lc-hard-001 摘要逐月写出数值与合计 15330，b2 只写「sales 目录 / summary.txt」（K 节，样例只作内容证据，非统计）。
2. **(b) task_state 没有被激活，「只变 task_state」这一单因素本批没有测到。** `turn1_plan_built` 三臂 0/90，`turn1_record_has_task_state` 0/90；no-plan 提醒在 90/90 个摘要器输入里都出现了，turn 1 的 `update_plan` 调用为 0/90（D、F 节）。迭代 6 让模型把 turn 1 做完的更多（`turn1_status` success 18/30，turn1_calls 中位 9–9.5 对 6–6.5），而不是去建 plan。所以 plan 臂实际只是「turn 1 截断点 3 → 6」：POST 8/30，Δ重复功 +4.27 [+2.73, +5.67]，对 b2 +1.33 [−0.03, +2.70]（偏差）。按 plan_built 只有一层（全 False），只能按 turn1_status 分层（E 节）：turn 1 已 success 的 18 对，both 11/18、plan 7/18。
3. **交互不可估（缺批内 tool0/t3 基线臂）。** 批内干净对照只有 both − plan（工具段 @ 迭代 6，见结论 1）与 both − digest（截断点 @ digest 200：POST pass −10.0 pt [−26.7, +3.3]，Δ重复功 +1.33 [−0.10, +2.90]），后者按结论 4（跨截断点 pass 与 Δ 都不可比）口径不可读。
4. **pass 受步数上限支配这一说法只对 D45 三臂成立：b2 不计「步数超限」POST 仍只 15/30（实际 10/30），主因是「回合上限耗尽」14、「无收尾答复」10，这两者不是步数超限。** 不计「步数超限」时，POST pass 是 b2 15/30，digest 28/30、plan 27/30、both 29/30（D 节）；max_steps 把 turn 1 的调用也算进去，迭代 6 的两臂 PRE 也因此多失败（PRE 步数超限 10 / 13 对 b2 7）。b2 POST 的「回合上限 5 耗尽」14 对、「无收尾答复」10 对（集中在 003/004/007/008/009），D45 三臂只有 1–3 对。重复功 repeated 计的是续跑里落在 turn 1 调用集的调用，迭代 6 时 turn 1 已 18/30 success（PRE 续跑步数均 0.5 对 b2 2.2），Δ 的基线随截断点机械变化 → 跨截断点 pass 与 Δ 都不可比；plan − b2、both − digest 不能读成截断点效应。
5. **预算没有被触及。** summary_input_chars：digest 939 / 1,071（中位）/ 1,694（最大），both 1,352 / 1,768 / 2,491，plan（旧格式）438 / 872 / 1,385；工具段最长 1,423 字 < 2,400；输入 > 24,000 字 0；`(+N more calls` 尾标 digest 3 行、both 17 行，触发它的都是每轮 8 条的条数上限（tool_lines 最大 8），不是字符预算；plan 臂 30/30 是旧格式行（F 节）。整行丢弃和批次预算两条分支在真数据上没有走到。
6. **注入缓解的痕迹：没有误伤，但也没有被攻击过。** 行首 `User:` 恰好 1 行 / 输入（30/30 × 3 臂），非行首 `User:` 0、`[Task state` 0、行首 `MEMO:` 0、工具调用行内含 `User:` / `System:` 0；`⏎` digest 56 处（15 个输入）、both 85 处（15 个输入），都是被压行的多行文件内容（G 节）。mock env 里没有对抗文本，`spill:` 句柄在输入中出现 0 次，所以一审 M1 的短句柄路径真数据未覆盖；这一节只证明压行不破坏正常输入。
7. **error 0。** 三步 180 行 error 0，POST 摘要非空 90/90（H、I 节）；b2 new-900 / new-640 也是 0（legacy-640 的 13 对空摘要见 b2 ANALYSIS §4.7，本批没有 legacy 步）。
8. **D46 生效：lc-hard-009 PRE pass 三臂都是 3/3（state_ok 3/3），b2 两步 0/3（`files/metrics/summary.json` 正则失败）**；POST 2/3、1/3、2/3（I 节）。跨批对比因此要扣掉这一题（C 节第二表）。

## 2. 口径与可比性

- 跨批（digest / plan / both 对 b2）不只差一个因素：D45 批同时带 D46 修复（`d60bd50`，13:24 提交、15:30 起机）以及 new 模板新增的一句工具段说明（`engine.py` 工作区改动，对 plan 臂同样生效）；b2 的 meta / 行里没有 `tool_digest_chars` 键（L 节归 `None` 组）。turn 1 跨批也不是逐字复现：同 (题, rep) 的 env 快照 + 调用数，b2 对 digest 只有 18/30 相同（b2 内部 900 对 640 是 28/30），所以跨批差里含 turn 1 的抽样变异。只差一个因素的对比只有批内 both − plan（工具段）和 both − digest（截断点）。
- D46 不止 lc-hard-009：lc-hard-006 的 `count.txt` 在 b2 读到 `'7'`、在 d45 读到 `'7\n'`（旧 `_coerce` 先 strip 再 int；3/3 rep 都如此）。b2 对 digest 的 turn 1 不一致对（18/30 相同之外的 12 个）里，6 个是 D46 结构性差异（lc-hard-006 ×3：`count.txt` 多了换行；lc-hard-009 ×3：`metrics/summary.json` 旧 `_coerce` 写出 Python dict repr、新写法写出合法 JSON），其余 6 个（lc-hard-001 ×3、lc-hard-002 ×2、lc-hard-010 ×1）是模型输出本身的差异。这一结构性差异是否影响分数未核实。
- 委托里写的对照数「+2.7 [1.6, 4.3]」是 b2 三步合起来的范围（§1.7 原文）；new-900 单步是 +2.93 [+1.97, +4.00]（b2 ANALYSIS §4.6，本次 L 节逐位复现）。
- E 节 `turn1_plan_built` 只有一层（全 False）：判据没有失败，只是不可判。

## 自动部分（`benchmarks/summary_trace/analyze_d45.py`，seed 0，B=1000）

### A. 输入与配置（meta.json / 行字段）

| 臂 | 目录 | 行 | error 行 | 配对 | tool_digest_chars | turn1_iterations | no_plan_nudge_iterations | stamp |
|---|---|---|---|---|---|---|---|---|
| b2-new-900 | `benchmarks/data/context-knee-20261102-b2/trace-new-900` | 60 | 0 | 30 | 缺键 | 3 | 缺键 | 20261102-202216 |
| b2-new-640 | `benchmarks/data/context-knee-20261102-b2/trace-new-640` | 60 | 0 | 30 | 缺键 | 3 | 缺键 | 20261102-205618 |
| d45-digest | `benchmarks/data/context-knee-20261103-d45/trace-d45-digest` | 60 | 0 | 30 | 200 | 3 | 3 | 20261103-155340 |
| d45-plan | `benchmarks/data/context-knee-20261103-d45/trace-d45-plan` | 60 | 0 | 30 | 0 | 6 | 3 | 20261103-163341 |
| d45-both | `benchmarks/data/context-knee-20261103-d45/trace-d45-both` | 60 | 0 | 30 | 200 | 6 | 3 | 20261103-161140 |

### B. 每臂汇总（字段：score、repeated、blocked、cont_redundant_calls、first_prompt_tokens、summary_chars、summary_tokens_est）

| 臂 | pass PRE | pass POST | POST−PRE pass（题级 95%） | 重复功 PRE→POST 均 | Δ重复功（题级 95%） | cont_redundant 均 PRE / POST | 首 prompt 中位 PRE / POST | 摘要字符中位 | 摘要 token 中位 |
|---|---|---|---|---|---|---|---|---|---|
| b2-new-900 | 20/30 | 10/30 | -33.3 pt [-60.0, -13.3] | 0.97 → 3.90 | +2.93 [+1.97, +4.00] | 0.07 / 0.50 | 1818.5 / 1422.5 | 472.5 | 171.5 |
| b2-new-640 | 19/30 | 5/30 | -46.7 pt [-73.3, -20.0] | 0.97 → 4.33 | +3.37 [+2.43, +4.30] | 0.10 / 0.57 | 1818.5 / 1434.5 | 465.5 | 195.0 |
| d45-digest | 22/30 | 16/30 | -20.0 pt [-46.7, +6.7] | 0.97 → 2.47 | +1.50 [+0.50, +2.77] | 0.10 / 0.17 | 1799.0 / 1477.0 | 613.5 | 215.5 |
| d45-plan | 20/30 | 8/30 | -40.0 pt [-63.3, -16.7] | 0.37 → 4.63 | +4.27 [+2.73, +5.67] | 0.00 / 0.13 | 2301.5 / 1430.5 | 430.5 | 189.5 |
| d45-both | 17/30 | 13/30 | -13.3 pt [-30.0, +0.0] | 0.67 → 3.50 | +2.83 [+1.60, +4.10] | 0.00 / 0.07 | 2338.0 / 1469.5 | 563.5 | 202.0 |

### C. 相对 b2-new-900 的差（臂 − b2，按题；正 = 臂更高；pass 单位百分点）

| 对照 | POST pass | PRE pass | POST−PRE pass gap 之差 | Δ重复功之差 |
|---|---|---|---|---|
| digest − b2（只变工具段；同 turn1=3） | +20.0 pt [+0.0, +43.3] | +6.7 pt [-20.0, +30.0] | +13.3 pt [-13.3, +43.3] | -1.43 [-2.63, -0.13] |
| plan − b2（只变 turn1 3→6；工具段 0） | -6.7 pt [-26.7, +10.0] | +0.0 pt [-30.0, +20.0] | -6.7 pt [-30.0, +23.3] | +1.33 [-0.03, +2.70] |
| both − b2（两者） | +10.0 pt [-10.0, +30.0] | -10.0 pt [-50.0, +20.0] | +20.0 pt [-3.3, +50.0] | -0.10 [-1.50, +1.23] |
| 交互 − 批次效应（不可解读） | -3.3 pt [-30.0, +26.7] | -16.7 pt [-43.3, +10.0] | +13.3 pt [-26.7, +53.3] | -0.00 [-1.67, +1.50] |
| 批内：both − plan（只变工具段；turn1=6） | +16.7 pt [+0.0, +36.7] | -10.0 pt [-30.0, +0.0] | +26.7 pt [+6.7, +53.3] | -1.43 [-2.50, -0.57] |
| 批内：both − digest（只变 turn1；工具段 200） | -10.0 pt [-26.7, +3.3] | -16.7 pt [-40.0, -3.3] | +6.7 pt [-16.7, +33.3] | +1.33 [-0.10, +2.90] |
| 次参照：b2-new-640 − b2-new-900（同批两步） | -16.7 pt [-30.0, -3.3] | -3.3 pt [-20.0, +10.0] | -13.3 pt [-36.7, +10.0] | +0.43 [-0.43, +1.27] |

同表去掉 lc-hard-009（b2 两步该题因 D46 前 qwen35 `_coerce` 写坏 JSON，PRE/POST 全 0；D45 批已含 D46 修复，该题 9 题外单独看 I 节）：

| 对照（9 题） | POST pass | PRE pass | POST−PRE pass gap 之差 | Δ重复功之差 |
|---|---|---|---|---|
| digest − b2（只变工具段；同 turn1=3） | +14.8 pt [-3.7, +37.0] | -3.7 pt [-22.2, +11.1] | +18.5 pt [-11.1, +48.1] | -1.04 [-2.11, +0.07] |
| plan − b2（只变 turn1 3→6；工具段 0） | -11.1 pt [-29.6, +3.7] | -11.1 pt [-33.3, +0.0] | -0.0 pt [-25.9, +33.3] | +1.67 [+0.22, +2.96] |
| both − b2（两者） | +3.7 pt [-14.8, +25.9] | -22.2 pt [-55.6, +0.0] | +25.9 pt [+0.0, +55.6] | +0.37 [-0.81, +1.52] |
| 交互 − 批次效应（不可解读） | +0.0 pt [-25.9, +25.9] | -7.4 pt [-37.0, +18.5] | +7.4 pt [-37.0, +55.6] | -0.26 [-1.89, +1.22] |
| 批内：both − plan（只变工具段；turn1=6） | +14.8 pt [+0.0, +37.0] | -11.1 pt [-33.3, +0.0] | +25.9 pt [+0.0, +55.6] | -1.30 [-2.41, -0.41] |
| 批内：both − digest（只变 turn1；工具段 200） | -11.1 pt [-29.6, +3.7] | -18.5 pt [-44.4, -3.7] | +7.4 pt [-14.8, +37.0] | +1.41 [-0.22, +3.19] |
| 次参照：b2-new-640 − b2-new-900（同批两步） | -18.5 pt [-33.3, -3.7] | -3.7 pt [-22.2, +11.1] | -14.8 pt [-40.7, +14.8] | +0.70 [-0.00, +1.44] |

### D. 失败原因（score_why 分类计数；一行可多因；计数 = 原因出现次数）与 turn 1 形态

| 臂 | 支 | 步数超限 | 回合上限耗尽 | 无收尾答复 | env 判错 | state_ok=False | turn1_status | turn1_calls 中位 | turn1_plan_built | turn1_record_has_task_state |
|---|---|---|---|---|---|---|---|---|---|---|
| b2-new-900 | pre | 7 | 0 | 0 | 3 | 3 | {'warning': 30} | 6.5 | {'None': 30} | {'False': 30} |
| b2-new-900 | post | 20 | 14 | 10 | 3 | 3 | {'warning': 30} | 6.5 | {'None': 30} | {'False': 30} |
| b2-new-640 | pre | 8 | 0 | 0 | 3 | 3 | {'warning': 30} | 6.5 | {'None': 30} | {'False': 30} |
| b2-new-640 | post | 24 | 14 | 10 | 3 | 3 | {'warning': 30} | 6.5 | {'None': 30} | {'False': 30} |
| d45-digest | pre | 8 | 0 | 0 | 0 | 0 | {'warning': 30} | 6.0 | {'False': 30} | {'False': 30} |
| d45-digest | post | 14 | 2 | 2 | 0 | 0 | {'warning': 30} | 6.0 | {'False': 30} | {'False': 30} |
| d45-plan | pre | 10 | 0 | 0 | 0 | 0 | {'success': 18, 'warning': 12} | 9.0 | {'False': 30} | {'False': 30} |
| d45-plan | post | 22 | 3 | 3 | 1 | 1 | {'success': 18, 'warning': 12} | 9.0 | {'False': 30} | {'False': 30} |
| d45-both | pre | 13 | 0 | 0 | 0 | 0 | {'success': 18, 'warning': 12} | 9.5 | {'False': 30} | {'False': 30} |
| d45-both | post | 17 | 1 | 1 | 0 | 0 | {'success': 18, 'warning': 12} | 9.5 | {'False': 30} | {'False': 30} |

pass 若不计「步数超限」（其余原因为空即算过；评分器 max_steps 计 turn 1 + 续跑全部调用）：

| 臂 | PRE | POST |
|---|---|---|
| b2-new-900 | 27/30 | 15/30 |
| b2-new-640 | 27/30 | 15/30 |
| d45-digest | 30/30 | 28/30 |
| d45-plan | 30/30 | 27/30 |
| d45-both | 30/30 | 29/30 |

### E. 分层：turn1_plan_built 与 turn1_status（POST pass / 对数；Δ重复功均）

| 臂 | 层 | 对 | PRE pass | POST pass | Δ重复功均 |
|---|---|---|---|---|---|
| b2-new-900 | turn1_plan_built=None | 30 | 20 | 10 | 2.93 |
| b2-new-900 | turn1_status=warning | 30 | 20 | 10 | 2.93 |
| b2-new-640 | turn1_plan_built=None | 30 | 19 | 5 | 3.37 |
| b2-new-640 | turn1_status=warning | 30 | 19 | 5 | 3.37 |
| d45-digest | turn1_plan_built=False | 30 | 22 | 16 | 1.50 |
| d45-digest | turn1_status=warning | 30 | 22 | 16 | 1.50 |
| d45-plan | turn1_plan_built=False | 30 | 20 | 8 | 4.27 |
| d45-plan | turn1_status=success | 18 | 17 | 7 | 3.61 |
| d45-plan | turn1_status=warning | 12 | 3 | 1 | 5.25 |
| d45-both | turn1_plan_built=False | 30 | 17 | 13 | 2.83 |
| d45-both | turn1_status=success | 18 | 14 | 11 | 1.89 |
| d45-both | turn1_status=warning | 12 | 3 | 2 | 4.25 |

### F. summary_input 分布与预算（POST 行 summary_input / summary_input_chars / summary_input_tool_lines）

| 臂 | 有 summary_input | chars min/p25/中位/p75/max | tool_lines min/p25/中位/p75/max | 工具段字符 max（预算 2400） | 工具段 ≥ 2000 字的数 | `(+N more calls` 行数 | 旧格式 `(tools called this turn:` 行数 | 输入 > 24000 字 | 含 update_plan 调用的输入 | 含 no-plan 提醒（`update_plan NOW`）的输入 |
|---|---|---|---|---|---|---|---|---|---|---|
| b2-new-900 | 0/30 | — | — | — | — | — | — | — | — | — |
| b2-new-640 | 0/30 | — | — | — | — | — | — | — | — | — |
| d45-digest | 30/30 | 939 / 1038 / 1071 / 1400 / 1694 | 5 / 6 / 6 / 7 / 8 | 1162 | 0 | 3 | 0 | 0 | 0 | 30 |
| d45-plan | 30/30 | 438 / 744 / 872 / 1043 / 1385 | 0 / 0 / 0 / 0 / 0 | — | 0 | 0 | 30 | 0 | 0 | 30 |
| d45-both | 30/30 | 1352 / 1462 / 1768 / 2292 / 2491 | 7 / 8 / 8 / 8 / 8 | 1423 | 0 | 17 | 0 | 0 | 0 | 30 |

### G. 注入缓解痕迹（summary_input 逐行）

| 臂 | 输入数 | 行首 `User:` 行（期望 = 轮数 1/输入） | 非行首 `User:` 片段 | 行首 `[Task state` | 非行首 `[Task state` | 行首 `MEMO:` | `⏎` 字形数（含它的输入数） | 工具段内含 `User:` / `System:` 的行 |
|---|---|---|---|---|---|---|---|---|
| b2-new-900 | 0 | — | — | — | — | — | — (—) | — |
| b2-new-640 | 0 | — | — | — | — | — | — (—) | — |
| d45-digest | 30 | 30 | 0 | 0 | 0 | 0 | 56 (15) | 0 |
| d45-plan | 30 | 30 | 0 | 0 | 0 | 0 | 0 (0) | 0 |
| d45-both | 30 | 30 | 0 | 0 | 0 | 0 | 85 (15) | 0 |

### H. 摘要里的工具证据（POST summary 的 [Done] / [Key values] 节）

| 臂 | 摘要非空 | [Done] 非空 | [Key values] 非空 | [Key values] 含文件路径（含裸文件名） | [Done] 含文件路径（含裸文件名） | 摘要含 `spill:` 句柄 | 输入含 `spill:` |
|---|---|---|---|---|---|---|---|
| b2-new-900 | 30/30 | 30 | 26 | 23 | 28 | 0 | 0 |
| b2-new-640 | 30/30 | 30 | 24 | 21 | 27 | 0 | 0 |
| d45-digest | 30/30 | 27 | 30 | 30 | 27 | 0 | 0 |
| d45-plan | 30/30 | 30 | 28 | 28 | 30 | 0 | 0 |
| d45-both | 30/30 | 29 | 30 | 30 | 28 | 0 | 0 |

### I. error 行与 lc-hard-009（字段 error / score / state_ok / score_why）

- b2-new-900: error 行 0
- b2-new-640: error 行 0
- d45-digest: error 行 0
- d45-plan: error 行 0
- d45-both: error 行 0

| 臂 | lc-hard-009 PRE pass | PRE state_ok | POST pass | POST state_ok | PRE score_why（去重） |
|---|---|---|---|---|---|
| b2-new-900 | 0/3 | 0/3 | 0/3 | 0/3 | files/metrics/summary.json: regex 失败: '(?s)\\{\\s* |
| b2-new-640 | 0/3 | 0/3 | 0/3 | 0/3 | files/metrics/summary.json: regex 失败: '(?s)\\{\\s* |
| d45-digest | 3/3 | 3/3 | 2/3 | 3/3 | — |
| d45-plan | 3/3 | 3/3 | 1/3 | 3/3 | — |
| d45-both | 3/3 | 3/3 | 2/3 | 3/3 | — |

### J. 按题 POST pass（通过 rep / 3）与 turn 1 一致率（只比 env 快照与调用数，非完整文本）

| 臂 | 001 | 002 | 003 | 004 | 005 | 006 | 007 | 008 | 009 | 010 |
|---|---|---|---|---|---|---|---|---|---|---|
| b2-new-900 | 1 | 3 | 0 | 0 | 3 | 1 | 0 | 0 | 0 | 2 |
| b2-new-640 | 0 | 2 | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 0 |
| d45-digest | 0 | 3 | 2 | 2 | 3 | 1 | 1 | 0 | 2 | 2 |
| d45-plan | 0 | 3 | 1 | 0 | 3 | 0 | 0 | 0 | 1 | 0 |
| d45-both | 0 | 3 | 2 | 0 | 3 | 0 | 0 | 0 | 2 | 3 |

- turn 1 env 快照 + 调用数逐字相同（同 (题, rep)）：b2-new-900 vs d45-digest = 18/30
- turn 1 env 快照 + 调用数逐字相同（同 (题, rep)）：d45-plan vs d45-both = 17/30
- turn 1 env 快照 + 调用数逐字相同（同 (题, rep)）：b2-new-900 vs b2-new-640 = 28/30

### K. 抽样：d45-digest 与 b2-new-900 同 (题, rep=0) 的 POST 摘要 [Done] / [Key values]（各截 220 字；前 5 题）

- lc-hard-001 r0 b2-new-900（POST pass=False, Δ重复功=6）：[Done] - 读取了 sales 目录下 2026 年 1–5 月的五份流水文件 - 汇总五个月金额并写入 summary.txt，注明为 1–5 月合计 ‖ [Key values] - sales 目录 - 2026 年 1–5 月流水文件 - summary.txt - 每行末尾数字为金额
- lc-hard-001 r0 d45-digest（POST pass=False, Δ重复功=6）：[Done] - 读取了 sales/2026-01.csv 至 sales/2026-05.csv 全部五个月文件。 - 计算各月金额：2026-01: 2485, 2026-02: 2975, 2026-03: 3030, 2026-04: 3540, 2026-05: 3300，合计 15330。 - 将合计 15330 及逐月内訳写入 summary.txt。 ‖ [Key values] - sales/2026-01.csv - sales/2026-02.csv - sales/2026-03.csv - sales/2026-04.csv - sales/2026-05.csv - summary.txt - 2026-01: 2485 - 2026-02: 2975 - 2026-03: 3030 - 2026-04: 3540 - 2026-05: 3300 - 合计: 15330
- lc-hard-002 r0 b2-new-900（POST pass=True, Δ重复功=1）：[Done] - Deleted all report files in the reports folder containing the word "DRAFT" in their text. - Renamed all remaining report files by inserting "-final" before the ".md" extension, copying content to the new filenames and  ‖ [Key values] - reports folder - DRAFT - -final - .md
- lc-hard-002 r0 d45-digest（POST pass=True, Δ重复功=2）：[Done] - Deleted reports/alpha.md and reports/gamma.md (both contained "DRAFT"). - Created reports/beta-final.md with the exact content of reports/beta.md. ‖ [Key values] reports/beta.md reports/beta-final.md reports/alpha.md reports/gamma.md
- lc-hard-003 r0 b2-new-900（POST pass=False, Δ重复功=5）：[Done] - 读取了 team 目录下四个人的文件，提取了各自的值日日子。 - 生成了 duty-summary.txt，内容为四人及其值日日子，一人一行。 ‖ [Key values] - team - duty-summary.txt
- lc-hard-003 r0 d45-digest（POST pass=True, Δ重复功=0）：[Done] （空） ‖ [Key values] - team/lina.txt - team/wangfang.txt - team/zhangwei.txt - team/zhaomin.txt - duty-summary.txt - 张伟 周一 - 李娜 周三 - 王芳 周四 - 赵敏 周五
- lc-hard-004 r0 b2-new-900（POST pass=False, Δ重复功=2）：[Done] - Listed files under policy/ and read the relevant files. - Wrote updated content (with "30 days") to the writable files under policy/. - Created policy/pending-changes.md naming the read-only file that still needs a man ‖ [Key values] policy/ policy/pending-changes.md "90 days" "30 days"
- lc-hard-004 r0 d45-digest（POST pass=False, Δ重复功=0）：[Done] - Replaced "90 days" with "30 days" in policy/access.md and policy/backup.md. - Attempted to update policy/security.md but the write was refused (EACCES: read-only file). ‖ [Key values] - policy/access.md - policy/backup.md - policy/security.md - policy/pending-changes.md - EACCES: read-only file: policy/security.md
- lc-hard-005 r0 b2-new-900（POST pass=True, Δ重复功=0）：[Done] - 读取了 `notes/todo-notes.txt` 文件内容。 - 将文件中的 5 个事项添加到了待办清单中。 - 将其中 2 个已标记为 `[x]` 的事项在清单中设为完成状态。 - 将剩余的 3 个未完成事项写入 `remaining.txt`。 ‖ [Key values] - notes/todo-notes.txt - remaining.txt
- lc-hard-005 r0 d45-digest（POST pass=True, Δ重复功=0）：[Done] 读取 notes/todo-notes.txt，将 5 条事项全部加入待办清单（提交周报、预约牙医、交房租、买咖啡豆、修打印机）。 - 将已完成项 提交周报、交房租 标记为完成。 - 将未完成项 预约牙医、买咖啡豆、修打印机 写入 remaining.txt（一行一件）。 ‖ [Key values] notes/todo-notes.txt remaining.txt 提交周报 预约牙医 交房租 买咖啡豆 修打印机

### L. summarize_trace.report（四批合并，原样；`summarize_trace.py b2/trace-new-900 d45/trace-d45-digest d45/trace-d45-plan d45/trace-d45-both`）

| model | max_tokens | template | task_state | drop_task | tool_digest | turn1_iter | 对数 | 题数 | 重复功 PRE | 重复功 POST | Δ(POST−PRE) | 95% CI | pass PRE | pass POST | 首 prompt 中位 PRE | 首 prompt 中位 POST | 摘要字符中位 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RedHatAI/Qwen3.8-27B-INT4 | 900 | new | on | on | 0 | 6 | 30 | 10 | 0.37 | 4.63 | 4.27 | [2.73, 5.67] | 0.67 | 0.27 | 2302 | 1430 | 430 |
| RedHatAI/Qwen3.8-27B-INT4 | 900 | new | on | on | 200 | 3 | 30 | 10 | 0.97 | 2.47 | 1.50 | [0.50, 2.77] | 0.73 | 0.53 | 1799 | 1477 | 614 |
| RedHatAI/Qwen3.8-27B-INT4 | 900 | new | on | on | 200 | 6 | 30 | 10 | 0.67 | 3.50 | 2.83 | [1.60, 4.10] | 0.57 | 0.43 | 2338 | 1470 | 564 |
| RedHatAI/Qwen3.8-27B-INT4 | 900 | new | on | on | None | 3 | 30 | 10 | 0.97 | 3.90 | 2.93 | [1.97, 4.00] | 0.67 | 0.33 | 1818 | 1422 | 472 |
（重复功 = repeated + blocked；不成对 / error：0；CI = 题级 bootstrap 1000 次，<2 题不给）
