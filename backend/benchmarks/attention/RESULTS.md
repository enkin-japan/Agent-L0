# 注意力涣散基准 — attention-guardrails 分支 vs main 基线

日期：2026-07-08 ｜ 模型：gemma-4-e4b（开发机）｜ 每任务 3 重复，共 18 runs/侧
main 基线 commit：e7a0eba ｜ 分支：attention-guardrails（护栏事件回流 + 澄清引导 + 终止文案选项化）

## 指标定义

- **drift_events**（过程涣散度）= parse_errors + dup_blocks + invalid_ops + failure_routes + tool_errors
  （全部来自护栏/工具的确定性反馈痕迹，无主观判定）
- **success**：文件系统断言判定最终产物（执行≠成功）
- **clarify**：模糊任务先提问而非动手改数据；清晰任务此值应为 0（过度提问回归探针）

## 结果

| 任务 | success main→branch | drift main→branch | clarify main→branch |
|---|---|---|---|
| A 单点编辑 | 3/3 → 2/3 | 5 → 3 | 0 → 0 |
| B 多步改造（原事故任务） | 2/3 → 2/3 | 5 → 4 | 0 → 0 |
| C 多文件统计 | 2/3 → 2/3 | 0 → 2 | 0 → 0 |
| D CSV 汇总报告 | 1/3 → 0/3 | 1 → 2 | 0 → 0 |
| E 模糊目标（整理目录） | 3/3 → 3/3 | 0 → 0 | **3 → 3** |
| F 批量改配置 | 3/3 → 3/3 | 0 → 0 | 0 → 0 |
| **合计** | **14/18 → 12/18** | **11 → 11** | — |

## 解读

1. **两项改动均无可测的行为副作用**：成功率差（14 vs 12）在 n=3 的 e4b 随机波动内
   （失败样本全部是模型层问题：D 组编造类别/求和算错、A r2 文中插空行、C 组格式跑偏，
   与分支改动无因果路径）；drift 总量持平；**清晰任务澄清率两侧均为 0**——澄清引导
   没有引入过度提问回归。
2. **E 组基线即 3/3 先澄清**：e4b 在"整理目录"这类明显模糊的任务上本来就会提问，
   澄清引导在此探针无提升空间；其价值定位改为“把既有好行为固化成显式规则 +
   终止文案给用户可选项”，属零成本保险。
3. **事件回流是本批真正的收益**：分支跑出的 6 个 parse_error 全部实时入流
   （main 侧只能事后扫 history 文本）；套件跑完后补上 tool_error 钩子并实机烟测通过，
   至此 drift_events 的全部五个组分都有对应事件类型，覆盖闭合。
4. **基准本身的价值**：单次通过≠稳定（B 任务此前单测通过，基准跑出 2/3 与 1/3），
   n=3 只能看趋势；31b 上线后同一套件复跑即模型层搁置项的自动验收。

## 已知边界

- e4b 方差大，n=3 不支持显著性结论；若要硬结论需 n≥10/侧（约 6 小时实机）。
- D 任务（读→算→写长链条）对 e4b 接近能力上限，成功率本身就在 0–1/3 间波动。
- 事件流为 fire-and-forget 仪表，不参与任何控制流；文件在 .gitignore 中。

## 复跑方法

```bash
python3 benchmarks/attention/driver.py --work /tmp/bench_work --out results.jsonl --reps 3
# 按会话聚合护栏事件：guardrail_events.jsonl 的 session 字段 == 驱动打印的 sid
```

> 2026-08-09 driver 现代化：审批环从已移除的同步 `/approval` 端点迁至协议 v1
> 事件账本制（POST /turns 202 → SSE /events 观察 → awaiting_approval 即决 →
> turn_ended 收轮；断连带 after 游标续播）。鉴权经 BENCH_KEY 或 .env.local 自动
> 解析。E2E 已验（e4b inprocess，A_small_edit 双审批打穿全通）。历史数据可比性
> 不受影响——痕迹抓取与断言逻辑未动。

## 31B 编排层验收（2026-08-09 #77，n=10×6 任务，Colab-hetero：Mac edge + 80G 31B fp8+MTP）

| 任务 | success | drift_sum | clarify | avg_wall |
|---|---|---|---|---|
| A_small_edit | 10/10 | 0 | — | 20s |
| B_multi_edit | 10/10 | 2 | — | 83s |
| C_line_count | 10/10 | 0 | — | 15s |
| D_csv_report | 10/10 | 0 | — | 21s |
| E_ambiguous | 9/10 | 0 | 9/10 | 10s |
| F_ini_ports | 10/10 | **336** | — | 142s |

- **产物成功率 59/60**；E 的 1"失败"实为澄清措辞用冒号未用问号（判分启发式
  `?` 检测误判，行为上 10/10 先澄清不动手）。
- **F 复读吸引子**：6/10 轮 edit_file 重发风暴（dup 拦截 30-61 次/轮、100+ 调用、
  turn 终态 fail），但护栏把复读全拦在门外——**文件产物 10/10 正确**。同款模式在
  skill 口测里复现（29 连 excel_research + 41 连 render_chart）。模型层毛病记档
  （D1 纪律），护栏收益实证：drift 全被拦截器吸收、零产物损伤。
- 新 driver（事件账本制）全程 60 轮零 driver_error，审批 1-5 次/轮全部经
  awaiting_approval→决议→续跑闭环。
- 原始行级数据：`model_eval/results/attention-31b-n10.jsonl`（gitignored）。
