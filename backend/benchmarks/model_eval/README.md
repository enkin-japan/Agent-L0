# model_eval — 模型能力评测框架（多模型协作前置）

为生产机多模型常驻（HETERO_COMPUTE §10）的**路由决策**提供每模型能力画像。
评的从来不是裸模型，而是 **「模型 × 本方言层 × 本 agent 循环」的组合表现**——
执行器走引擎直连（`AgentEngine.create` + `generate_once`），prompt 由本项目方言
组装、输出由本项目 parser 还原、纠错回喂复用编排层同款信封
（`orchestrator.parse_error_feedback` + 方言 `parse_error_retry_hint`）。
社区榜（BFCL/MMMU 原样跑分）测不到这个组合，是为量身定制之因。

## 与既有基准的分工

- `benchmarks/attention/`：HTTP 顶层行为基准（编排护栏收益对比），评**架构改动**；
- `benchmarks/model_eval/`（本目录）：引擎直连能力评测，评**模型档位**，产出喂路由表。

## 目录

```
model_eval/
  itemlib.py        题库装载 + 鉴定纪律强制校验
  matchers.py       期望匹配器（参数/文本断言，纯函数）
  executor.py       引擎直连执行器（脚本化工具回喂 + 纠错闭环复刻）
  run_eval.py       CLI 入口
  scorers/          维度打分器（全部规则判定，零主观评分）
  items/<dim>/      题库（JSONL；借题批次配 VETTING.md 鉴定日志）
  results/          运行产物（gitignore；入库结论走报告）
```

## 维度

| 维度 | 采集方式 | 核心指标 |
|---|---|---|
| speed | 任何题顺带（engine metrics 白捡） | prompt_tps / generation_tps / peak_memory / KV 前缀命中率 |
| tool_call | 专用题 | 首发合格率 / 解析错误率 / 纠错成功率 / 无工具陷阱克制率 |
| cot_purity | 任何开思考题顺带 | 控制标记泄漏 / thoughts↔正文复读 / 语种混杂 / 预算纪律 |
| longchain | 专用题（mock 工具态机） | 终态断言成败 / 步数 / 回退次数 |
| vision | 专用题（带图） | 关键词/精确匹配正确率 |
| text | 专用题（纯文本，与 vision 共用 text_answer 打分） | 关键词/正则命中率。现存批：`items/text/longctx_struct.jsonl`（⑤′-2 边界探针，生成器确定性重建，见 BOUNDARY.md） |

## 题库 schema（JSONL 每行一题）

```json
{
  "id": "tc-seed-001",
  "dimension": "tool_call",
  "lang": "zh",
  "source": {"origin": "self | BFCL-v3-simple#123 | MMMU-dev#45",
             "fit": "为何适合本项目目的的鉴定结论（必填，见下）"},
  "task": {
    "system": "（可省，缺省用标准评测系统提示）",
    "user": "任务文本",
    "tools": [ …项目工具声明格式… ],
    "images": ["assets/xx.png"],
    "tool_responses": {"tool_name": "脚本化回喂内容（str 或对象）"},
    "max_turns": 3
  },
  "gen": {"enable_thinking": true, "thinking_budget": 512,
          "max_tokens": 1024, "temperature": 0.2},
  "expect": { …判分规格，见 scorers/… }
}
```

## 鉴定纪律（借题/自造一律强制）

`source.fit` 为空或敷衍（<8 字符）**装载即拒**（itemlib 响亮 ValueError）。
一道题入库前须过五问：

1. **场景贴合**：任务能映射到本项目真实使用面（自家工具形态 / App 截图 / 文档图表 /
   多步文件任务），不是为测而测的空转题；
2. **判分可程序化**：期望能用规则断言表达，不依赖主观评审或 LLM-judge；
3. **事实时效安全**：不依赖易变世界知识（训练截止差异不该影响判分）；
4. **语言面匹配**：批次内 zh/en 双语都有覆盖；
5. **来源可追溯**：借题 origin 记 `数据集-split#原id`，改写幅度写进 fit。

借题批次在 `items/<dim>/VETTING.md` 记录：候选数 / 采纳数 / 代表性拒绝理由。
拒绝日志与采纳同权重——「严格鉴定」的证据就是拒绝记录。

## 判分口径

- 全部规则判定（matchers + 各维度 scorer），可复现、可回归；主观维度
  （模型脾气）不在本框架射程（用户另有计划）。
- 生成参数用**部署同款**（temperature 0.2 / min_p 0.05 / constrained 开——gemma4
  吃约束、qwen35 按设计 fail-open），评的是"部署形态下的表现"不是最佳配置。
- 系统提示用固定极简款（executor.DEFAULT_SYSTEM_PROMPT）保证跨模型可比；
  编排全量提示下的行为归 attention 基准管。
- 随机性不钉 seed（后端不暴露），用 `--repeat N` 重复取分布；单次跑分只作冒烟。

## 用法

```bash
cd backend
.venv/bin/python benchmarks/model_eval/run_eval.py \
  --model Qwen3.5-9B-4bit --dims tool_call --repeat 3
# 产物: benchmarks/model_eval/results/<model>/<stamp>/{transcripts.jsonl, summary.json}
```
