# qwen38 双档生产口径重画像——蒸馏聚合件（2026-08-23，③′）

阶段复审 J4 补档：config profile 与 PROFILES.md「08-23 生产口径节」的可复算
依据（对齐 08-16 横评 q_summaries 先例：只入分数聚合件，不入生成正文；全量
transcripts 在产机 `backend/benchmarks/model_eval/results/`，gitignored）。

- `8bit-133216-summary.json` ← results/Qwen3.8-27B-8bit/20260823-133216/
- `4bit-140448-summary.json` ← results/Qwen3.8-27B-4bit/20260823-140448/

口径：思考开 + 预算 1024（executor 缺省）+ 条目级 drafter（b5/b3），
`run_eval.py --dims tool_call,longchain,vision`，--spec-mode 留空。
