# context_knee — 有效上下文拐点测量（W0）

背景见 `docs/plans/2026-10-31-context-management-plan.md` §3 W0。复用 `backend/benchmarks/model_eval`（`run_item` + longchain 终态打分，源码不改），任务前注入可控长度的真实形态历史。
```
backend/.venv/bin/python benchmarks/context_knee/run_knee.py --dry-run --levels 0,4096 --reps 2 --limit 2
INFER_REMOTE_HOST=<算力端> INFER_API_KEY=<…> backend/.venv/bin/python benchmarks/context_knee/run_knee.py \
    --model RedHatAI/Qwen3.8-27B-INT4 --backend remote --tasks hard [--sampling repetition_penalty=1.0]
backend/.venv/bin/python benchmarks/context_knee/summarize.py <out_dir> [<out_dir> ...] [--coherent-foreign 0.01 --coherent-mixed 0.05 --coherent-dup 0.3]
```
- 缺省 `--backend remote`：本机不装载模型（单 GPU 纪律）；只拉分词器（`resolve_tokenizer_only_path`，app.py remote 同款）+ 身份护栏。mlx 走 run_eval 同款全量解析、在本进程装载。`--dry-run` = FakeEngine。
- 产物落 `results/<模型>/<时间戳>/`（gitignored）。`--out` 已有 transcripts.jsonl 时拒绝，`--append` 追加；`--resume`（隐含 --append）跳过已成功的 (level, 题, rep)、error/timeout 行重跑，已有行的 model / 已发送采样 / seed 与本次不一致即拒。`--run-timeout`（缺省 600 s）超时：置 cancel → 排空推理执行器（等 min(60, timeout) 秒，等不到记 `drain_timeout: true` 并警告后续可能连环超时）→ 释放缓存 → 记 `{"error":"timeout"}` 行继续。summary.json 总会写出；任何率之前先去重：同 (level, 题, rep) 取最新的非 error 行（不按 stamp 拆 Pass^2）。
- `--sampling k=v,…`：映射键（见 run_knee.SAMPLING_KEYS）记为 `sampling_overrides.sent`——**已发送、未回执核验**（服务端不回显 SamplingParams），表里标 `card(sent …)`；其余键警告后不发送、记入 `ignored`。覆盖两层：引擎入口同名形参 + 后端 `stream_generate` 的 sampling 字典（引擎硬编码的 repetition_penalty 1.1 只能在后一层改）。远端无采样 seed：同种子只对填充成立。

## level 语义
`--levels` 是**首次生成的 prompt 总 token 目标**（system + 工具 + 填充 + 任务），缺省 `0,4096,8192,16384,32768,49152,57344`。
0 = 自然基线（无填充、无回忆句）；4096 = 带回忆句的最小填充、与高档同口径的基线；57344 = 顶档，给远端 max_model_len 65536 留生成余量。level 低于「基础 prompt + 3 个伪轮次」时打警告并记入 `fill.warning`。

计数按引擎同款组装渲染，再用引擎分词器计（fake 模式按 chars/3.5）；首次生成后用 `metrics.prompt_tokens` 回填。
填充是伪轮次流的前 n 轮：user 短问 → assistant 思考加一次调用 → tool 观察（`response` 键）→ 收尾。观察是 mock 环境同款 JSON 回执，读型观察长度 300–3000 字、对数均匀。
- 流由 (seed, rep, 题面指纹, 工具集) 决定：每题独立；同一题内，高档的伪轮次是低档的超集。
- 键名差异：填充用 orchestrator 的 `response` 键（compactor 只看这个键；qwen35 / gemma4 都能渲染）。执行器自己回喂的观察仍是 `content` 键，所以 compactor 看不见任务本身的观察。

**与 mock 环境的隔离**：伪历史的路径全部落在 `/srv/legacy-2025q3/…`，与 longchain 全部题面的路径和目录不相交（有测试钉住）。待办名带 `legacy-2025q3` 前缀，每条伪 user 消息都点明这是「上一个已归档、与当前无关的项目」。
残余：模型仍可能回读伪文件，mock 环境回 File not found，计入 env_errors。

## 事实卡（精确回忆探针）
- 每个 level>0 的填充埋 3 张卡，各是工具观察里的一行：halyard 配置路径、OPS- 工单号、`queue_depth_alert_threshold` 数值。
- 卡落在填充 token 轴的 10% / 50% / 90% 处；卡种按 (题序 + rep) % 3 在槽位间轮换（`cards[].kind`）；卡值在填充里查重，撞车就重抽（最多 20 次）。
- 任务 prompt 末尾追加一句，要求逐字写出三值；终态断言不变。
- 判命中前答复先 NFKC 归一、U+2010–2015 换成 "-"；值须整体出现（两侧不紧贴字母数字、后面不跟 ".数字"）；aborted run 不进 recall 分母。

## 输出与口径
transcripts.jsonl 每 run 一行：
- `fill`：目标 / 估算 / 实际 token、fill_error、warning、cards、fingerprint；`score`：longchain 原字段 + recall_* + repetition_hits。
- `gens`：每次生成的 prompt_tokens、reused_prefix、prompt_tps、generation_tps、repetition_guard。
- 其余：`final_text_quality`、`sampling_overrides`、`turns`。

summarize 口径：
- **prefill 秒(未命中)** = 首次生成 prompt_tokens / prompt_tps，不按 reused_prefix 过滤：远端首生成报的 num_cached_tokens 实测不可信。对照列「后续 prompt_tps 中位」：真命中缓存时高一到两个数量级。
- **连贯率**：抓「漂进任务语言之外的文字系统」。coherent = 外文字母占比 `foreign_script_ratio` < 0.01（预期文字按题 lang：zh = 拉丁 + 汉字，en = 拉丁，ja 另加假名；数字、标点、符号不计）且 16-gram 重复占比 < 0.3，en 题另要求词内拉丁与 CJK 混写的 token 占比 `mixed_token_ratio` < 0.05（zh 题只记录）。summarize 按记录的 lang 从末轮正文现算；script_mix、longest_run 等旧指标照记，不进判据。局限：不含外文字的语义乱码抓不到。
