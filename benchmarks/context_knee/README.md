# context_knee — 有效上下文拐点测量（W0）

背景见 `docs/plans/2026-10-31-context-management-plan.md` §3 W0。复用 `backend/benchmarks/model_eval`（`run_item` + longchain 终态打分，源码不改），任务前注入可控长度的真实形态历史。
```
backend/.venv/bin/python benchmarks/context_knee/run_knee.py --dry-run --levels 0,4096 --reps 2 --limit 2
INFER_REMOTE_HOST=<算力端> INFER_API_KEY=<…> backend/.venv/bin/python benchmarks/context_knee/run_knee.py \
    --model RedHatAI/Qwen3.8-27B-INT4 --backend remote --tasks hard [--sampling repetition_penalty=1.0] \
    [--prefix mock|prod|catalog|routed]
backend/.venv/bin/python benchmarks/context_knee/summarize.py <out_dir> [<out_dir> ...] [--coherent-foreign 0.01 --coherent-mixed 0.05 --coherent-dup 0.3]
```
- 缺省 `--backend remote`：本机不装载模型（单 GPU 纪律）；只拉分词器（`resolve_tokenizer_only_path`，app.py remote 同款）+ 身份护栏。mlx 走 run_eval 同款全量解析、在本进程装载。`--dry-run` = FakeEngine。
- 产物落 `results/<模型>/<时间戳>/`（gitignored）。`--out` 已有 transcripts.jsonl 时拒绝，`--append` 追加；`--resume`（隐含 --append）跳过已成功的 (level, 题, rep)、error/timeout 行重跑，已有行的 model / 已发送采样 / seed / 注册表条目采样（`entry_sampling`，W-1 起引擎并入；旧行缺该键按 {} 算）与本次不一致即拒；单纯 `--append` 只比条目采样与前缀档（`prefix_mode`，见下节）。`--run-timeout`（缺省 600 s）超时：置 cancel → 排空推理执行器（等 min(60, timeout) 秒，等不到记 `drain_timeout: true` 并警告后续可能连环超时）→ 释放缓存 → 记 `{"error":"timeout"}` 行继续。summary.json 总会写出；任何率之前先去重：同 (level, 题, rep) 取最新的非 error 行（不按 stamp 拆 Pass^2）。
- `--sampling k=v,…`：映射键（见 run_knee.SAMPLING_KEYS）记为 `sampling_overrides.sent`——**已发送、未回执核验**（服务端不回显 SamplingParams），表里标 `card(sent …)`；其余键警告后不发送、记入 `ignored`。覆盖两层：引擎入口同名形参 + 后端 `stream_generate` 的 sampling 字典（引擎按注册表条目 `sampling` 解析，W-1；覆盖层仍优先）。SIGINT/SIGTERM：置 cancel → 排空 → 写 summary → 退出码 128+signum，12 s 看门狗兜底，收尾期二次信号立即硬退。远端无采样 seed：同种子只对填充成立。

## 前缀档 `--prefix`（W-2「量法」）
方案 §W-2：拐点曲线原是 harness 短前缀口径（executor 极简 system + 题目 mock 工具，base ≈0.3–0.6K tok），生产前缀是 13.6K（full）/ 5.3K（catalog）。四档在同一填充流上对比，装配在 `prefix.py`：
- `mock`（缺省）：W-2 前口径，逐字节不变（system、tools、run_item / 引擎入参都不改；有测试钉住）。
- `prod`：system = `framework/memory/session.py` 的 `DEFAULT_SYSTEM_PROMPT`；工具 = 生产确定性工具全集完整 schema + full 档元工具（`update_plan` / `complete_step` / `remember`）+ 题目 mock 工具；`grammar_tools` 与 prompt 面同集合。
- `catalog`：system 同 prod；元工具（多 `get_tool_schema`）与 `agent.tool_routing.core` 完整，其余生产工具只留目录行（`tool_disclosure.disclose`，只有 name/description 两键），其后 mock 工具完整；`grammar_tools` = 完整 schema 全集（与生产一致：语法面恒用完整）。
- `routed`：catalog 之上，按原题面 `task.user`（不含回忆句，跨 level/rep 不变）经 `tool_disclosure.route_tools` 选 `agent.tool_routing.top_k` 件，选中者完整 schema。每题一次、题内冻结。

细则：
- 生产工具集与 `backend/scripts/measure_prefix.py` 同源（`tool_token_audit.collect_schemas`：密封构造、不探活 RAG、不含 `rag_admin`、skill_invoke 取生产形）；声明顺序与生产 `MCPRegistry` 一致（sorted 模块 × `inspect.getmembers` 类名序，skill_invoke 因 app.py 补注册排末尾；有测试钉住）；元工具是 orchestrator 的模块级常量（`Orchestrator._meta_schemas` 同一份对象）。不构造 Orchestrator / MCPRegistry，不起服务。
- 撞名：题目 mock 工具优先（它承载评分），同名生产工具剔除，记 `prefix_shadowed`（vfs/office 题的 `write_file`）；被剔的 core 名记 `prefix_core_skipped`。
- **生产工具只声明、不执行**：模型调用它们（含元工具、`get_tool_schema`）时走 executor 既有的未知工具回执（mock env 回 `Unknown tool: <名>`，计入 env_errors），不新造执行路径。所以非 mock 档测的是「大前缀下还能不能做对题」，不是生产编排的行为。
- 填充流按题目 mock 工具取名，不随档变：非 mock 档的伪轮次是同一 (seed, rep, 题) mock 档的前缀，只因前缀更大而更少。level 仍是 prompt 总量目标——低于前缀 base 的档（prod 下 4096/8192，fake 计数下连 16384）只有最少 3 个伪轮次、打 `fill.warning`，填充里放不下事实卡时记 error 行；非 mock 档请选高于 `prefix_base_tokens` 的档。
- 披露模式由 `--prefix` 显式决定，**不读**注册表条目的 `tool_schema_mode`（D29）：同一模型可分别量 full 与 catalog（D29 的选择正需要这组对比）。`prefix_base_tokens` 是整段前缀（含包裹与教学文本）的数，与 D28 门（逐件声明之和 12,300 线、两家族同口径）不是同一口径，不可直接对比。
- `summarize.py` 不按 `prefix_mode` 分组：不同档放不同 `--out` 目录，并排给 summarize 比较（同目录混档被续跑校验拒绝）。

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
- `peak_prompt_tokens`（W1）：本 run 各次生成实测 prompt_tokens 的最大值（gens 空或全缺 = null）；summarize / analyze 汇总为「峰值prompt中位」`peak_prompt_tokens_median`（W1 前的旧行缺键，不进中位数）。harness 不经编排层，W1 护栏动作（钳制/压缩/终止）不在此计数。
- 其余：`final_text_quality`、`sampling_overrides`、`turns`。
- 前缀（meta，每行与 summary.json 都有）：`prefix_mode`（W-2 前的旧行缺键 = mock）；`prefix_base_tokens` = 生产稳定前缀 token（system + 生产声明，未剔撞名、不含 mock 工具与路由件、`include_env=False`，与 `measure_prefix.py` 的 full / catalog(meta+core) 行同口径；mock 档 = 极简 system 无工具）。真后端用引擎分词器；fake 用 `backend/models/<repo 末段>` 离线分词器（`--model` 给了且目录在时），不可得记 null 并告警一次（mock + fake 不载分词器、不告警）。非 mock 档另有 `prefix_shadowed` / `prefix_core_skipped` / `prefix_n_tools`（按题：prompt 面件数、其中完整件数、语法面件数）；routed 档另有 `prefix_top_k`、`prefix_routed`（选中名单）。这四个按题的键在 meta / summary.json 是 `{题: 值}` 全批字典，transcript 每行只放本题的值（`run_knee.PER_ITEM_META_KEYS`）。

summarize 口径：
- **prefill 秒(未命中)** = 首次生成 prompt_tokens / prompt_tps，不按 reused_prefix 过滤：远端首生成报的 num_cached_tokens 实测不可信。对照列「后续 prompt_tps 中位」：真命中缓存时高一到两个数量级。
- **连贯率**：抓「漂进任务语言之外的文字系统」。coherent = 外文字母占比 `foreign_script_ratio` < 0.01（预期文字按题 lang：zh = 拉丁 + 汉字，en = 拉丁，ja 另加假名；数字、标点、符号不计）且 16-gram 重复占比 < 0.3，en 题另要求词内拉丁与 CJK 混写的 token 占比 `mixed_token_ratio` < 0.05（zh 题只记录）。summarize 按记录的 lang 从末轮正文现算；script_mix、longest_run 等旧指标照记，不进判据。局限：不含外文字的语义乱码抓不到。
