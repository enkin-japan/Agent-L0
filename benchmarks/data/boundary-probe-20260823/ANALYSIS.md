# boundary-probe-20260823 · ⑤′-2 边界目录探针档案

结论与逐格数字见 `backend/benchmarks/model_eval/BOUNDARY.md`（§B1–B3），本文件只记**跑法与复算入口**。

## 跑法（两段，单 GPU 手核独占，各挂 task-sentinel）

- 第一段 `probe_run.sh`：`run_eval.py --dims text --spec-mode off` 依次 26b-4bit、26b-8bit（当时题库 8 题：
  4 尺寸 × JSON/表格）。产物 `gemma-4-26b-a4b-it-4bit/`、`gemma-4-26b-a4b-it-8bit/`。
- 第二段 `probe_run2.sh`：生成器加了 `-table-think` 变体后，26b-4bit 只跑 4 题 think 变体
  （`--ids … --out …-4bit-think`），qwen38-4bit 跑 table + table-think 8 题（其注册表条目级 drafter 生效，
  即生产口径）。产物 `gemma-4-26b-a4b-it-4bit-think/`、`Qwen3.8-27B-4bit/`。
- 题库 `backend/benchmarks/model_eval/items/text/longctx_struct.jsonl` 由 `gen_longctx_struct.py`（seed
  20260823）确定性重建；评审后任务 B 判分收紧（regex_all 钉条数+合计），文档与 prompt 逐字未变——
  `rescore.py` 对四个 run 离线重判，结论不变（`rescore.json`）。

- 第三段（08-23 晚，用户拍板"先跑再修"）：生成器加 `-table-list` 变体（关思考 + "先逐条列出再分组
  计算"指令），26b-4bit 只跑 4 题（`probe_run3.sh`，`--ids` 为逐个逗号分隔的题 id——run_eval 只认精确
  id，不支持通配）。结果：列表 12/12 全对、表格 0/4——算术这一步错。产物
  `gemma-4-26b-a4b-it-4bit-listfirst/`；`rescore.py` 已含此 run。

## 复算入口

| 文件 | 作用 | 输入是否在库 |
|---|---|---|
| `rescore.py` → `rescore.json` | 按当前题库规格重判五个 run | 是（同目录 transcripts + 题库） |
| `vision_diff.py` → `vision_diff.json` | B1 逐题差分 | **否**（`results/` gitignored 本地件；换机只能核对清单） |
| `mutants.log` | ⑤′-2 变异证明 | — |

## 环境

Mac Studio M4 Max 64G；26b-4bit 峰值 20.9G、26b-8bit 33.5G、qwen38-4bit 28.1G（含 MTP 草稿件）；
prompt tps 26b ≈1300 vs qwen38 ≈239（稠密 27B）。
