# docs/history/ —— 开发历史档案（2026-08-29 冻结）

> **这是什么**：项目开发期（2026-07 ~ 2026-08）的全部过程文档，开发主线收官后
> 整体归档（用户 2026-08-29 拍板）。**只读档案，不再更新**；低频查证用。
> 逐文件的 git 历史随迁移保留：`git log --follow docs/history/<文件>`。
>
> 仍在服役的文档留在原位：根 `README.md` / `ARCHITECTURE.md` / `PROTOCOL.md`
> （协议 v1 活规格）/ `CHARTER.md` / `CLAUDE.md`（维护期纪律照旧）/
> `DEFERRED.md` / `IDEAS.md`（未来账本），以及各子项目 README。

## 档案清单

| 文件 | 是什么 | 原路径 |
|---|---|---|
| `EXECUTION_LEDGER.md` | 执行台账主卷（§2 规模表 / §D 决策表 / §S 坑表 / 各波交付记录）。**头部有冻结注记与复役步骤** | 仓库根 |
| `EXECUTION_LEDGER_ARCHIVE.md` | 台账早期卷（R0–R5 / D 阶段 / B2c / 七月各波，2026-08-08 瘦身迁出） | 仓库根 |
| `ROADMAP.md` | 项目方向与排序理由（基于 COMPARE_CC_HAHA 诊断） | 仓库根 |
| `REARCHITECTURE.md` | 交互层重整蓝图（体检 / 目标架构 / 四期迁移 + 踩坑） | 仓库根 |
| `COMPARE_CC_HAHA.md` | 与 cc-haha 的全量对比诊断（ROADMAP 的输入） | 仓库根 |
| `HETERO_COMPUTE.md` | 异构算力执行账本（CUDA 兼容层 / 算力分离 / RAG 档位） | 仓库根 |
| `MTP_PROJECT.md` | MTP（草稿件投机采样）专项工作面 | 仓库根 |
| `AGENT-L0-OSS-SCAN-20260817.md` | 对六个开源 Agent 项目的全量对比扫描与增量裁决（波 A–F） | 仓库根 |
| `RETROSPECTIVE.md` | 全项目复盘（B2c 收官时点提炼；重入首选读物） | 仓库根 |
| `inference-speedup-plan.md` | 推理提速计划（MTP 专项的前身文档） | backend/docs/ |
| `ppt-production-todo.md` | deck/PPT 工具生产化待办 | backend/docs/ |
| `tools/doc_numbers.py` | 文档规模数字防漂移校验器（曾是 `devctl check` 第四道门 docs；已去可执行位） | scripts/ |
| `tools/test_doc_numbers.py.frozen` | 上者的纯函数金测试（改名冻结，防仓库根裸 `pytest` 误收集必红） | backend/tests/ |

## doc-numbers 门为何随档退役

该门守的是**活跃开发期**「文档里的规模数字 vs 仓库实际」的漂移；打点宿主是台账
§2 的规模表。台账冻结后没有漂移可防，留着门等于每次提交都校验一份已死文档。
2026-08-29 与台账同日退役：`devctl.sh` 摘除 `docs` 门与 `doc-numbers` 子命令，
pre-commit 的文档-only 快路径由「只跑 docs 门」改为放行（`skills/*.md` 除外——
剧本是运行时内容，同日显式踢出白名单走全门）。
**复役步骤**见 `EXECUTION_LEDGER.md` 头部冻结注记。

已知且**有意接受**的覆盖损失（首审 #8）：doc_numbers 曾统计
`protocol/fixtures/*.events.jsonl` 数量，新夹具漏登记 README 表会以「计数漂移」
形态被拦；退役后该登记纪律只剩 `protocol/fixtures/README.md` 的流程文字约束。
