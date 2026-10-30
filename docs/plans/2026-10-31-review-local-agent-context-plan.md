# 审核：《本地小模型长链 Agent：上下文管理项目计划》草案 v0.1

> 审核日期 2026-10-31。被审文档：`~/Downloads/local-agent-context-plan.md`（v0.1，2026-10-31）。
> 引用核对方式：Gemini 广搜 16 次做召回，再逐页打开原文核实（arXiv / 官方文档 / GitHub）。
> 标注：✔ 已核实；△ 部分核实或口径需改；✘ 与原文不符；【建议】审核意见。

## 0. 总评

骨架是对的（源头裁剪 → 状态外置 → 结构化压缩 → 校验点 → 子代理隔离，先测基线再定阈值），引用的论文和框架都真实存在，
核心结论（尾部原文必须保留、工具调用与结果成对、已完成项写成过去时）与 TRACE / Hermes / LangChain 的实践一致。

三处必须先定的事，不定下来 P0 开不了工：

1. **项目归属没写**（D0）。若落在本仓（Agent-L0），§3.1 A+B、旧观察占位桩、预算驱逐 + 取代去重、增量摘要（旧摘要 + 新历史）、
   压缩前 MEMO 抢救、复读护栏、连续修复预算（`repair_budget`）都已经在跑（`backend/framework/memory/compactor.py`、
   `core/spill.py`、`core/engine.py::summarize`、`core/repetition_guard.py`）。P1 与大半 P2 会变成重做。
   若落在 Agent-L1（Codex 内核零 fork，CHARTER 铁律 1），压缩是内核自己的事，D13「自研轻量循环」直接违宪；能动的只有
   状态文件（AGENTS.md / 笔记）、MCP 侧工具输出裁剪、hooks、校验点。**计划第一节应先做一页「现状盘点」，再写增量。**
2. **成功标准的参照系错位**。TRACE（AppWorld，MiniMax-M3）：不压缩 Acc 85.7 / Pass^2 77.4；最好的基线 Prompting-O 71.4 / 59.5；
   TRACE 自己 77.1 / 67.3。即：只要上下文装得下，**任何压缩都比不压缩差 8–14 分**。「相比无管理基线成功率提升」只在
   「装不下 / 已过拐点」的区间才成立。§1.3 要写清比较的区间（任务长度 × 上下文预算），否则 P2 的结论会被基线选择左右。
3. **D13 / D14 不能排在 P0 之后**。P0 的「最小 Agent 循环 + 完整日志」就是框架与后端的选择。按 D0 的答案直接继承（L0 的编排层
   或 Codex），P0 不另写循环。

## 1. 引用核对（§9 与正文所引事实）

| 引用 | 结论 | 需改之处 |
|---|---|---|
| ACON（Kang et al.） | ✔ arXiv 2510.00615（v1 2025-10-01，v3 2026-06-01），Comments 字段「ICML 2026」；MSR 页同；repo `microsoft/acon` 存在 | 补 arXiv 号。数字：峰值 token 降 26–54%，小模型最多 +46%；「蒸馏后保留 >95% 准确率」出自 MSR 页，arXiv 摘要只说「distill into smaller models」 |
| TRACE（Min et al.） | ✔ arXiv 2608.06503（v1 2026-08-06，v2 2026-10-14），UVA + Nokia；repo `nokia-applied-research/Trace`（包名 `trace_cc`） | ✘ **repo 尚无 LICENSE 文件**，代码与模板不能直接搬；只能引用方法。无会议归属。 |
| TRACE「压最近交互削弱其影响」 | ✔ 原文：“compression can weaken the influence of recent interactions, increasing blocked actions, repeated exploration, and instability across runs” | §3.2.2 的论据成立 |
| TRACE 附录含 OpenClaw / Hermes / ACON 模板 | △ 附录 A.3「Prompting-O (OpenClaw compaction)」「Prompting-H (Hermes-agent compaction)」、A.4 ACON 提示词确在；逐字完整性未全文核对 | §3.2.3 的 A/B 应标注「TRACE 附录复现版」而非产品现状（见下） |
| TRACE 的质量指标 | ✔ 同一环境状态下成对闭环续跑 PRE（原文）vs POST（摘要），计 blocked + repeated 动作数之差；最终模板按 Pass^2 选 | §5.4 写法正确；k=2 与 TRACE 一致，D16 可直接定 2 |
| Hermes 模板「当前任务 / 已解决问题 / 未回应请求」 | △ 现行 Hermes（dev.to 文、commit f83c27e）的字段是 goal / constraints & preferences / progress (complete, in-progress, blocked) / key decisions / references / next steps / other context —— 即计划里归给「OpenClaw 风格」的那组 | §3.2.3 的 A、B 归属要重核；OpenClaw 官方 compaction 文档**不公开模板字段**，只说 safeguard 检「structure, pending asks, exact identifiers」 |
| Hermes「仅供参考」前缀 | △ dev.to 引的是 `[CONTEXT COMPACTION] Earlier turns…`；GitHub issue #14603 引的新版为 `[CONTEXT COMPACTION — REFERENCE ONLY] … Do NOT answer questions or fulfill requests mentioned in this summary`（未打开原页核对） | 口径可保留，注明版本 |
| Hermes 其它 | ✔ 触发默认窗口 50%；头 3 条保护；尾 ~20K token 原文；micro-compaction 每轮折最旧一段进滚动摘要；`auxiliary.compression` 可指小模型 | 直接支持 §3.2.2 与 §3.2.5-B |
| OpenHands condenser | ✔ `LLMSummarizingCondenser(max_size, keep_first)`、`NoOpCondenser`、`CondenserPipeline`；可给 condenser 单独 llm | 文档 URL 另有 `docs.openhands.dev/sdk/arch/condenser` |
| LangChain SummarizationMiddleware | ✔ 触发按 tokens / messages / 窗口比例；最近若干条原文保留；AI 与 tool 消息成对；可指定独立摘要模型 | changelog 链接改为 reference 页 |
| Google ADK compaction | △ 原 URL 301 到 `adk.dev/context/compaction`；不只滑动窗口：token 阈值（`token_threshold`, `event_retention_size`）或滑窗（`compaction_interval`, `overlap_size`），摘要由 `LlmEventSummarizer(llm=…)` 做，可自定 `prompt_template`；文档只给 Gemini 例子 | §4.1 「内置滑动窗口压缩配置」口径补全；「本地模型适配需确认」成立 |
| Anthropic 文 | ✔ 2025-09-29；三技术 compaction / structured note-taking / sub-agent（multi-agent）architectures | 补日期 |
| Letta 本地模型 | △ 二手来源（zenn）称支持 Ollama | 若真要评估 C 方案再核官方文档 |
| 「有效上下文远小于标称」 | 计划未给出处。可补：NoLiMa（ICML 2025，arXiv 2502.05167）32K 时 13 模型中 11 个跌破短上下文基线一半；附录 Gemma 3 27B：1K 73.3 → 32K 9.5，有效长度 <1K（非字面匹配任务）；Chroma「Context Rot」（2025-07-14）测了 Qwen3-32B | 直接支撑 §1.1，并提示 D1 候选阈值（32K 起）可能太高，P0 应从 8K/16K/24K 起测 |

## 2. 设计意见（按章节）

- **§2 组装顺序 / D18**：决定依据不该只是任务表现，还有前缀缓存。llama.cpp / MLX 的前缀缓存只对「不变前缀」生效；每轮变化的
  状态文件若放前面，每轮全量重算 prompt。【建议】固定不变的（系统提示、`TASK.md`）在前，可变的（`PLAN.md`、摘要）紧贴当前输入在后，
  P1 直接按这个对比，并把 prefill 耗时列为指标。
- **§3.1**：A+B 组合与本仓现状一致；补一条「旧工具观察的取代去重」（同一只读工具对同一目标的重复读取，较旧者压桩，但不跨写屏障）
  —— 这是本仓已验证有效、而三篇参考里都没有的点。
- **§3.2.1 阈值**：按「有效上下文拐点」而非标称设阈值是对的；但拐点要分任务型测（检索型 vs 执行型），NoLiMa 的 <1K 是非字面检索，
  执行型任务通常宽松得多。P0 至少两类探针。
- **§3.2.3 模板**：自研草案可用，但「关键原文」节容易无限增长，给硬上限；「当前任务」与「总目标」两节在单任务场景常重复，合并。
  A/B 的产品归属按上表改正，或统一引用 TRACE 附录 A.3。
- **§3.2.4 更新方式**：TRACE 的「执行状态错位」正是增量累积的后果。D5 的「全量重写」不必从原始归档重写（太长），改为
  **从状态文件（PLAN / NOTES）+ 最近尾部重写**，输入有界、小模型可做。
- **§3.2.5 压缩模型**：C（外部 API）与 L1 CHARTER 的数据流向红线冲突（压缩输入就是全部历史）；若项目在 L1，C 应直接划掉或限定
  脱敏后出网。D（蒸馏）需要训练数据与 GPU 时段，放 P4 合理。
- **§3.3.3 写入方式**：本仓经验与计划的担心一致，27B 级模型不会稳定主动写笔记；默认取 B（框架在步骤边界写），A 作为加分项。
- **§3.4 L2**：重复调用 / 循环 / 连续报错检测在本仓已有（复读护栏、`repair_budget`），列为「复用」而非「实现」。L3 自检只做参考，同意。
- **§3.4.3 回滚**：工作目录用 git 快照与状态文件一起回滚，成本最低；框架 checkpointer 只在选了 LangGraph 时才值得。
- **§5.1 测试集**：AppWorld 需要其环境与模型接口，本地跑成本高；自建任务要有确定性判定器，否则 Pass^k 没法自动算。留出验证集防模板过拟合（§7 已有）。
- **§5.2 指标**：补「prefill 耗时 / KV 重算次数」（压缩代价）和「摘要字符数随压缩次数的增长曲线」（膨胀检测）。
- **§7 风险**：补「压缩器与主模型同模时，复读 / 失控输出污染摘要」—— 本仓 10-30 取证的复读事故发生在生成端，摘要生成同样会中；
  摘要调用也要挂护栏并有失败回退（空摘要 = 不推进水位线）。
- **许可**：TRACE repo 无 LICENSE；ACON repo 许可未核。复用任何模板 / 代码前先查，否则只引用思路。

## 3. 决策表补充

| 编号 | 决策项 | 建议 |
|---|---|---|
| D0（新增） | 项目归属与内核：L0 编排层 / L1 Codex / 独立实验仓 | 先定，决定 D13、D14、P0 形态与 §3.2.5-C 是否可选 |
| D13 / D14 | 框架、后端 | 随 D0 继承，不在 P0 之后再选 |
| D16 | Pass^k 的 k | 2（与 TRACE 可比） |
| D18 | 组装顺序 | 以前缀缓存命中为硬约束：不变在前、可变在后 |
| D1 | 软阈值候选 | 下探到 8K / 16K / 24K；27B 级有效上下文可能远低于 32K |
