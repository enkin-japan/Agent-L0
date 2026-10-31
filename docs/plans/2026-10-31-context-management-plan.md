# Agent-L0 上下文管理优化方案（本地小模型长链任务）

> 状态：方案 v1（2026-10-31，待用户审定）。
> 来源：用户草案《本地小模型长链 Agent：上下文管理项目计划》v0.1（`~/Downloads/local-agent-context-plan.md`）
> 按本项目重整；对草案本身的逐条审核在 `docs/plans/2026-10-31-review-local-agent-context-plan.md`。
> 现状引用来自 2026-10-31 scout 盘点（HEAD f231052；完整引文表在当日会话 scratchpad，关键行号已搬进本文）。
> 约定：【已有】本仓已实现；【改】在既有机制上改；【新】新增；【测】需实验数据定；【定】用户已拍板。

## 0. 一句话

本仓已经有了草案 §3 的大半骨架（源头裁剪 + spill、机械压缩、滚动摘要、任务状态、复读/循环护栏、沙箱子会话），
真正的缺口是**单个用户轮内部零压缩、思考全量回灌、没有上下文上限保护、摘要无结构、一切阈值未按两款生产模型实测**。
方案因此不是「从零搭」，而是：先测拐点（W0），再补轮内压缩与上限护栏（W1/W2），再做思考回放与摘要结构（W3/W4），
状态与校验只做小修（W5/W6）。

**方向修正（用户 2026-10-31 下午拍板）**：计算端目前只定 Colab（A100），本机只做执行端；核心推理模型只留 **Qwen3.8 27B** 与 **Gemma 4 31B** 两选项，Gemma 4 26B / e4b、Ornith 全部出阵容（含 colab 部署脚本缺省）；本机推理栈的部署与测试排到所有工作包之后，验收只有「能跑」，不看速度（本机 M6 基础款，带宽 170 GB/s）；一切速度数字以 Colab A100 实测为准。CUDA 侧精度只用预量化 4 bit（INT4 / QAT W4A16），不上 fp8 / NVFP4（D25）。阵容收束是 P1 第一项（W-3）。

## 1. 问题与证据

### 1.1 本项目观察到的失败（2026-10-30 取证，`benchmarks/session_forensics/2026-10-30/`）
- 失败会话 app-9daf9c9fee44（远端 Qwen3.8-27B-INT4）：单轮 6 次生成、prompt 24,981 tok（复用前缀 24,800），模型把截断提示里的
  spill 绝对路径抄错两次 → 幻觉调用 read_ui_tree → 末次生成连续解码 1,778 tok 复读，直到用户取消。
  同一提示的成功会话 app-d6e9391bd9cc 走了 plan_updated / step_completed 的任务状态路径，四步收口。
- 由此落地（build 83）：spill 短句柄、流式复读护栏、token 账修正。**没动的是上下文增长本身**：那一轮若不复读，再跑 10 步同样会撑到 4–5 万 tok。

### 1.2 现状盘点：草案模块 ↔ 本仓机制
| 草案 | 本仓现状 | 状态 |
|---|---|---|
| §3.1 工具输出裁剪 A+B | `core/spill.py`：cap 4000 字（`observation_max_chars`），头 2/3 尾 1/3，全文落 spill（0600），`spill:<stem>` 短句柄，`file_extract` 回取 | 【已有】 |
| §3.1 旧输出换占位 | `memory/compactor.py::compact_tool_observations`：keep_recent 6 / digest 200 / budget 20000 字；取代去重（同只读工具同目标，写操作为屏障）；预算驱逐大者优先。**唯一调用点在用户轮轮首**（orchestrator.py:689，while 循环之前） | 【已有】轮内不跑 |
| §3.2 结构化压缩 | `maybe_summarize_context`（compactor.py:466-529）：活跃期高阈值 8000 tok（轮次落盘后判）、空闲期低阈值 2000（120 s 扫、idle 300 s）、keep_tail 4 轮；增量 = [Existing summary]+[New conversation] → 模型重写整段（engine.py:742 `SUMMARY_INSTRUCTION`，自由要点，max_tokens 640）；MEMO 抢救 ≤3 行只增；空摘要不推进水位线 | 【已有】无结构模板、轮间才触发 |
| §3.2.6 归档回溯 | history.jsonl 全量；journal.jsonl（tool_result 截 2000 字）；spill 文件；RAG agent_memory 沉淀（零 LLM，第二水位线） | 【已有】 |
| §3.3 状态外置 | `core/task_state.py`：goal + 计划 ≤12 步 + 便签 ≤12×200 字 FIFO；`update_plan / complete_step / remember` 工具；每次迭代渲染 task_block（含 Budget 行）；task.json 仅 freeze 时落盘；长期笔记 `memory_write` + MEMORY.md 索引（≤40 行/4000 字）经环境块注入 | 【已有】框架自动渲染，模型主动写 |
| §3.4 校验点 L1/L2 | 工具调用指纹去重（orchestrator.py:1705，写后清只读指纹）、复读护栏（两级）、react_max_steps 4 / max_total_iterations 40 / max_sub_generations 80 / replan_max 3 / parse_retries 3、constrained runaway ≥512、目标校验（DEFERRED §2 已上马） | 【已有】 |
| §3.5 子 Agent 隔离 | `sandbox_runtime.py` EphemeralSession（独立 KV、不落盘）用于 research / math / excel；web_research 返回 RESEARCH DIGEST；只读工具可并行执行，生成串行持锁 | 【已有】按工具内建，非通用 |
| §4.1 框架 / §4.2 后端 | 自研编排层；本机 MLX（prompt_stream 增量喂入 + kv_persist 可选）；远端 vLLM（APC） | 【定】不换 |
| §5 评测 | `model_eval` longchain seed 6 / hard 10（mock 终态断言 + steps / redundant_calls / env_errors）、attention/driver 6 任务（需起栈）、soak_longrun 8 h、bench_a8_multiturn | 【已有】无「随上下文增长」维度 |

### 1.3 结构性缺口（本方案要补的）
1. **轮内零压缩**：机械压缩只在轮首、摘要只在轮间；长链轮内唯一限幅是单条 4000 字 + spill。40 次迭代 × 几千字观察 + 全量思考 = 无界。
2. **没有上下文上限保护**：代码没有任何地方把 prompt 规模与模型上限比较（vLLM `max_model_len` 65536，MLX `max_kv_size` 无配置）；超出即后端报错或静默质量崩。
3. **思考全量回灌**：history 保留每步 thoughts 并渲染进 prompt（gemma4 prompt_builder.py:186-188；qwen35 prompt_builder.py 头注「思考恒回放」是刻意选择）。
   Gemma 4 模型卡：「Thoughts from previous model turns must not be added before the next user turn begins, with the exception of tool call turns」；
   Qwen3.8 卡：`preserve_thinking` 默认保留全部历史思考，可关为只留末问之后。本仓跨用户轮也回放，既违 Gemma 规范，也是最大的 token 消耗项。
4. **摘要无结构**：自由要点 + 640 tok 上限，没有「已完成（过去时）/ 关键值 / 下一步 / 仅供参考前缀」，增量重写无校准（TRACE 指出的执行状态错位风险）。
5. **阈值基准错位**：摘要判定用 `last_prompt_tokens`（末次生成）而非本轮峰值；三档阈值（8000/2000/20000 字）全是经验值，未按 Qwen3.8-27B / Gemma 4 26B 实测。
6. **压缩与 KV 的耦合未量化**：机械压缩原地改写旧观察 → history 指纹变 → prompt_stream 失效 → 下一轮全量重预填充（prompt_stream.py:92）；远端 vLLM 按块哈希链，改中段同样使其后全部失效。该代价多大、值不值，没有数。

### 1.4 两款生产模型的事实（2026-10-31 逐页核实，模型卡为准）
| | Qwen3.8-27B（远端 INT4） | Gemma 4 26B A4B（本机 4bit） |
|---|---|---|
| 规模 / 架构 | 27B 稠密；**混合 Gated DeltaNet 3 : 1 Gated Attention**（线性注意力层有固定大小状态） | 25.2B 总 / 3.8B 激活，128 专家取 8 + 1 共享，30 层；滑窗 1024 局部 + 全局交错 |
| 标称上下文 | 262,144 原生（YaRN 到 1M） | 256K |
| 长上下文公开数字 | 模型卡无 RULER/MRCR/LongBench；第三方（arXiv 2610.33025）只有 RULER 4k 0.87、LongBench-v2 0.44 | 模型卡：**MRCR v2 8-needle 128K 平均 44.1%**（31B 66.4%，Gemma 3 27B 13.5%） |
| 思考 | 默认开；`reasoning_effort` xhigh/medium/low；`preserve_thinking` 默认 True | `<\|think\|>` 开关；跨用户轮须丢弃思考，工具轮保留 |
| 复读相关 | 卡上只说 presence_penalty 0–2 抑制「endless repetition」；非思考推荐 presence 1.5；本仓用 repetition_penalty 1.1（engine.py:426） | 开放 issue gemma#610 / #622：长生成与 JSON 约束下 token 复读塌缩，26B 5/10 复现，repeat_penalty 无效，无官方修复 |
| INT4 回收率 | RedHatAI W4A16（AWQ+GPTQ，FP8 KV）：IFEval 99.7%、MMLU-Pro 98.8%、GPQA 98.5% | — |
结论：**两款模型都没有可用的「随上下文长度下滑」曲线**，拐点只能自己测（W0）。Gemma 4 的 MRCR 44.1% 说明 128K 处多针检索已只剩一半；
Qwen3.8 的线性注意力层意味着它的退化形态可能不是「注意力稀释」而是「状态遗忘」，探针要同时测精确回忆（路径/ID 逐字）与执行决策。
不用 Gemma 3 / NoLiMa 的数字（用户 10-31 指出：过旧）。

**W0 实测（2026-10-31，远端 A100 vLLM；复算与数表 `benchmarks/data/context-knee-20261031/ANALYSIS.md`，harness `benchmarks/context_knee/`）**
longchain hard 10 题 × 2 rep，首次生成 prompt 0 / 4K / 8K / 16K / 32K / 49K / 57K 七档，填充为按题独立的确定性伪历史 + 三张逐字回忆事实卡。
- **Qwen3.8-27B-INT4（MTP 关）：49K 内无拐点，57K 未定**。模型卡采样各档 Pass^2 50–90% 随题抖动、不随长度单调降（低段→高段合并差 +2 点 [−10,+15]）；
  当前生产采样（temp 0.2 / rep 1.1）同样 70–90%；三槽精确回忆 ≥95%，复读 0。
- **Qwen3.8-27B-INT4（MTP 开，即 Colab 部署默认）：0 档就掉到 60–65%，16K 起成功率 20–35%、Pass^2 ≤20%、回忆 15–61%，复读命中重现**；两种采样同样糟。
  MTP 关→开按题配对：合并成功率 −49 点 [−66,−31]，Pass^2 −62 点 [−80,−45]，10 题 0 升 9 降（p=0.004）；回忆 100%→42–52%，复读 0→4–5；
  失败主因：终态断言 25、超步数 6–12、无收尾 7–11、复读 4–5、把填充里的旧项目当成当前任务作答（16K/32K 各 6 次）。
  → 远端路径长上下文退化与 10-30 复读事故的首要因素是 **vLLM 投机解码（原生 MTP 头，draft 3）**，不是上下文长度，采样参数差异在噪声内（推断：事故当日部署为 MTP 开）。
- **Gemma 4 26B A4B（fp8，无 MTP）：拐点 8K（变体 8K–16K）**，低段→高段合并 Pass^2 −38 点 [−60,−15]（6 题降 0 升，p=0.03）；
  形态是执行漂移（冗余调用 0.2→1.7、终态断言失败），不是记忆丢失（回忆 ≥89%，漏召全是「无收尾答复」）。与 MRCR 44.1% 方向一致。
- **Gemma 4 31B（官方 QAT W4A16 `gemma-4-31B-it-qat-w4a16-ct`，vLLM Marlin，MTP 关，模型卡采样）：49K 内无拐点**（0/8K/16K/32K/49K 五档 × 10 题 × 2 rep，100 run 0 错）。
  Pass^2 90/90/70/90/80%，16K 的 70% 随题抖动、32K 回到 90%，不触发拐点规则；三槽逐字回忆 8K–49K **全部 100%**，连贯 100%，复读 0，冗余调用 0.10–0.15 不随长度增长。
  → 26B A4B 的 8K 早退化是该 MoE（3.8B 激活）自身上限，不是 Gemma 4 家族上限（推断）；31B 质量与 Qwen3.8-27B（MTP 关）同档。
  代价在速度：首次 prefill 49K 在 A100 已 42 s（1.17K tok/s），解码 49→35 tok/s（稠密全注意力，随长度降；Qwen 线性注意力占 3/4，解码 50–55 不随长度变）。本机不再测速（10-31 方向修正），W-2 瘦前缀与 W2 落刀对 31B 比对 Qwen 更值钱。
  视觉塔核实（10-31，HF config.json）：`gemma-4-31B-it-qat-w4a16-ct` 为 `Gemma4ForConditionalGeneration`，27 层 `gemma4_vision` 整塔列在量化 `ignore` 清单（保持 bf16），带 processor_config 与 audio_config；注册表 capabilities 应改为 text+vision，vLLM 图文端到端待 W-3 在 Colab 联调一次。
- 未覆盖：本机 MLX 4bit + 生产前缀 9.4K 的 Gemma（测的是 A100 fp8、前缀 ~0.5K）；attention 6 题；真实脱敏历史形态（填充为合成模板）。
  采样键（top_p/top_k）只确认「已发送」，远端无回执证明进了 SamplingParams（qwen-card 两批的部署日志已轮转，无法核实是否带透传补丁）；采样对照结论因此只写「噪声内」。

### 1.5 推理栈前缀缓存事实（决定压缩「落刀位置」）
- 本机 MLX：严格前缀复用（`--prompt-cache-file` 「treated as a prefix」；server 旧行为「任何差异即重置」，issue #137 后改为取公共前缀），
  prefill 分块 `--prefill-step-size` 默认 2048；`--max-kv-size` 旋转缓存、`--kv-bits` KV 量化（本仓 gemma4_generate.py 透传 `max_kv_size`，无配置）。
  本仓 prompt_stream 失效条件：history 指纹变、工具集变、带 tail_notes 或 error 调用的那次不提交流（engine.py:631-638）。
- 远端 vLLM：块级哈希链 hash(parent, tokens, extra)，只缓存整块（默认 16 tok/块，V1 默认开 APC —— 这两点是经验值，文档页未直接写）；
  中段任何 token 变化使其后所有块失效；`reused_prefix` 来自 `num_cached_tokens`。
→ 压缩只在三个位置落刀：**用户轮轮首（已有）、步骤边界（新）、溢出兜底（新）**；可变块（task_block、tail_notes、环境日期）继续放末尾（现状已如此，保持）。

## 2. 目标与成功标准
- 比较区间：只在「历史超过软阈值」的任务上比；装得下时任何压缩都比不压差（TRACE：AppWorld 不压 85.7 vs 最优压缩 77.1 Acc），不在那个区间立目标。
- 指标（沿用 longchain 评分器并加三项）：成功率、**Pass^2**、redundant_calls、env_errors、复读命中、峰值 prompt tok、prefill 秒（prompt_tokens / prompt_tps）、全量重预填充次数。
- 验收【测】：P0 测出拐点后回填具体数；方向性要求：在拐点以上的任务集上成功率与 Pass^2 不低于「无管理」的 90%（因为无管理在该区间本来就会崩），
  峰值 prompt 不超过拐点的 70%，总耗时增量 ≤ 25%。

## 3. 工作包

### W0 测量先行（P0）【新】
- `benchmarks/context_knee/`：复用 model_eval longchain hard 10 题 + attention 6 题，在题前注入**可控长度的真实形态填充**（历史工具观察 + 思考，取自脱敏 fixture，非随机文本），
  档位 8K / 16K / 24K / 32K / 48K / 64K。每档每题 2 次（Pass^2）。两个探针族：执行型（终态断言）与精确回忆型（填充里埋 3 个路径/ID，终局要逐字用到）。
- 跑两端：Qwen3.8-27B-INT4 于 Colab（`colab/deploy.sh`，用完 `stop`，用户 10-31 授权随用随关）；Gemma 4 26B A4B 于本机（单 GPU 纪律：期间不起 :14161 以外的真模型，挂 task-sentinel）。
- 同时量：各档 prefill 秒与 tps（给 W2 的 KV 代价算账）；现网 prompt 规模分布 —— 从 history.jsonl 只抽 metrics 字段（不读正文），**需用户放行 sessions 目录**。
- 出口：两条拐点曲线、D1–D3 初值、W2 的落刀代价表。

### W-3 模型阵容收束（用户 10-31 方向修正，P1 第一项）【新】
- 注册表 14 → 4：留 `mlx-community/Qwen3.8-27B-4bit`、`mlx-community/gemma-4-31b-it-4bit`（本机，只需能跑）、`RedHatAI/Qwen3.8-27B-INT4`、`google/gemma-4-31B-it-qat-w4a16-ct`（Colab）。
  删 `gemma-4-e4b-it-8bit`、`gemma-4-26b-a4b-it-4bit/8bit`、`Qwen3.8-27B-8bit`（本机带宽下 8bit 无意义，【待拍板】）、Ornith 三条、`google/gemma-4-31B-it`（bf16，fp8 路径作废）、`google/gemma-4-26B-A4B-it`、`RedHatAI/gemma-4-26B-A4B-it-NVFP4`。
  31B QAT 条目 capabilities 改 text+vision，去掉「非生产候选」注记，补 revision 钉。
- 两个缺省值：`inference.model`（现 e4b-8bit）→ `mlx-community/Qwen3.8-27B-4bit`【待拍板，或 gemma-4-31b-it-4bit】；`colab/compute_vm.py` 的 `MODEL`（现 26B NVFP4）→ `RedHatAI/Qwen3.8-27B-INT4`（现行远端生产模型）。
- `colab/compute_ctl.py`：删「Gemma 4 必须 fp8」规则（bf16 条目已出阵容，成死代码），改为「CUDA 条目一律预量化，带 `--quantization` 即拒绝」；`cuda_deployment.prequantized` 两条都标。`colab/legacy/` 归档不动。
- 代码与测试：Ornith 专用 `backends/mlx/patch/text_only_vlm.py` 及其测试成死代码 → 删；注册表长度钉 14 → 4 并逐条钉 revision；model_router / local_config_fixups / inference_endpoints / cuda_backend / compute_ctl 测试里引用被删模型的用例改到留档模型上。
- 顺序：先 `scout` 列出全部引用点（path:line；排除 docs/history、benchmarks/data、colab/legacy），再按清单改；护栏型改动按仓规做变异证明。config.yaml / compute_ctl / compute_vm 由主会话亲自改。
- 验收：`./devctl.sh check` 全过；`deploy.sh models` 只列两条 CUDA 条目；Colab 部署 31B QAT 一次，图文请求与纯文本各通过一次（视觉塔 vLLM 端到端）；本机「能跑」验证排到全部工作包之后（§5 P-last）。

### W-1 远端推理配置修复（P0 实测带出，优先级最高）【新】
- **Colab 部署默认关 MTP**：`colab/compute_ctl.py` 对标明 `supports_mtp` 的模型默认开投机解码；W0 显示它在 0 档就损失 25 点、16K 起崩。
  改为默认 `off`，`--speculative mtp` 显式开启才用；config.yaml 的 `cuda_deployment.supports_mtp` 注释补上实测结论。
  （付费算力脚本与 config.yaml 属主会话亲自改的范围。）重新验证 MTP 只能在 vLLM 升级或换 draft 配置后、用同一 harness 重测。
- **CUDA 采样键透传**（已随 P0 落地，待随本波提交）：`cuda/backend.py` 对 top_p / top_k / presence_penalty / frequency_penalty 按需透传；此前被静默丢弃。
- **按模型卡配采样**：engine.py:440-444 硬编码 temp 0.2 / rep 1.1（vLLM 下 rep 为 HF 语义、罚整个 prompt）；改为按模型注册表条目配 `sampling`
  （Qwen3.8 思考档 temp 1.0 / top_p 0.95 / top_k 20 / rep 1.0；Gemma 4 temp 1.0 / top_p 0.95 / top_k 64），缺省沿用现值。W0 显示采样差异在噪声内，
  故此项是「合规与可配」而非「性能修复」，排在 MTP 之后。
- 服务端：客户端断连后挂死生成不释放（W0 观察到 active_generations 持续为 1）→ 核查 inference server 的断连止损路径；harness 客户端 SIGTERM 不死（推理线程阻塞读）→ 可中断。

### W-2 本机路径静态前缀瘦身（用户 10-31 采纳）【新】
- 事实：system + 全量工具 schema 稳定前缀 9,424 tok（engine.py:140），30 个工具模块；`tool_schema_mode: full | catalog` 已实现未启用
  （catalog = 目录常驻 + `get_tool_schema` 按名取详情，约束采样语法仍建在完整 schema 上）。
- ① 本机 Gemma 路径默认 `catalog`，远端 Qwen 维持 `full`（无拐点，无需省）；量前缀缩到多少【测】。
- ② 工具路由：每个用户轮用 rag_server 的 embedding 对「用户请求 + 任务目标」选 top-k 相关工具进目录，核心工具（文件 / 计划 / 记忆）常驻，未入选者仍可 `get_tool_schema` 取回。
- ③ skill 渐进披露：skill 正文不进 system prompt，只留一行索引，加 `load_skill` 工具按需读入。
- MCP 只是接线协议本身不省 token；省的是「目录常驻、详情按需」的披露策略，MCP 服务器的工具列表走同一路由。
- 量法：harness 加 `--prefix prod|catalog|routed` 三档，同一历史档位对比。

### W1 上下文上限护栏【新，守卫型 → 变异证明】
- 发现上限：远端取 vLLM `/v1/models` 的 `max_model_len`（或配置 65536）；本机从模型 config 读 `max_position_embeddings`，并以 `max_kv_size`（新增配置）封顶。
- 三档预算随上限派生：soft = 拐点×0.5【测】，hard = 拐点×0.8【测】，ceiling = 上限 − 生成预留（max_tokens 升档最大 12288）。
- 每次生成前估 prompt 规模（已有 token 账 + 字符/4 兜底）；≥ hard 触发 W2 压缩；≥ ceiling 强制摘要折叠后重试一次，再超 → 以 warning 终止本轮（像复读护栏那样可解释地失败，不让后端报 context overflow）。
- 阈值比较基准改为**本轮峰值**（修 1.3-5）。

### W2 轮内步骤边界压缩【改】
- 在 `complete_step` 落地时（已有 step_completed 事件）对**该步的工具观察**调用 compact_tool_observations（同一预算器，keep_recent 保护窗照旧），
  被压观察的全文已在 spill，占位桩带 `spill:<stem>` 句柄 → 模型回读有路。该步的 result（已由模型写进 TaskState）就是「步骤摘要」，不另调模型。
- 预算越过 hard 而无步骤边界时，退化为「按体量驱逐」（现有逻辑）；仍越过 → W1 的强制摘要。
- KV 代价：按 W0 的 prefill 曲线定「值不值」规则 —— 当被压掉的 token 数 × 后续预计迭代数 > 一次全量重预填充成本时才落刀；否则等到轮首。远端 vLLM 块粒度小，可放宽。
- 不碰的：审批 / 挂起续跑路径（compact_history=False）继续跳过；最近 keep_recent 条永不改。

### W3 思考回放策略【改】
- 跨用户轮：落盘轮次的 thoughts 不再渲染进 prompt（Gemma 卡要求；Qwen 等价于 `preserve_thinking=False`），history.jsonl 照旧全量。
- 轮内：保留最近 K 步思考（K 初值 2【测】），更早步只留一行「(思考已省略)」或并入步骤 result。
- 按方言可配（`thinking_replay: all | tool_turns | last_k`），qwen35 头注的「思考恒回放」改为可配项并记录当年选它的理由（差分测试锚点），差分测试样本随之更新。
- 这是最便宜的大头：10-30 失败会话 token_breakdown visible 6200 / thinking 0 是计数缺口，修正后思考占比才看得见（W0 顺带量）。

### W4 结构化摘要模板与校准【改】
- `SUMMARY_INSTRUCTION` 改为固定节：`[压缩摘要，仅供参考；不要重做「已完成」]` 前缀 → 总目标（原文照抄）→ 约束与偏好 → 已完成（过去时事实）→ 当前状态（目录 / 已改文件 / 进程）
  → 关键值表（路径 / ID / 数字，逐字）→ 决策 → 已知问题 → 下一步 → 用户长期事实（现有「偏好与身份」保留）。每节硬上限，总长仍 ≤ 640 tok（可上调到 900【测】）。
- MEMO 抢救不变。时间锚定（绝对日期）不变。
- 校准：每 K 次增量折叠（K 初值 4【测】）做一次「从 TaskState + 尾部原文重写」而非从归档全量重写（输入有界，小模型可做）。
- 模板评估走 TRACE 式成对续跑（§4），不靠整任务成功率。

### W5 状态外置小修【改】
- TaskState 加「关键值」渲染区：`remember` 已有，渲染时把形如路径 / URL / ID 的便签单列成表，便于逐字抄。
- 压缩后首步提醒：任何 W1/W2/W4 落刀后的下一次生成，以现有 system 工具响应形式贴一行「上下文已压缩，计划与关键值见上；不要重做已完成步骤」（orchestrator.py:1141 机制）。
- 不做：模型主动写笔记的新工具（现有 remember / memory_write 足够，27B 级不会更主动）；Letta / LangGraph Store。

### W6 校验点补强【改，小】
- L2 已齐（指纹去重、复读、迭代预算）。补：W1 的 ceiling 终止事件写 `guardrail_events.jsonl`；压缩后首步若出现「重做已完成步骤」的工具指纹，直接按重复调用拦。
- L3 模型自检不做（草案已判不可靠）；L4 外部复核 = 现有云端咨询升格路径，不新增。

### W7（可选，P3 后）大输出工具子模块化
- 把 `file_extract` 大范围读取、GUI `read_ui_tree` 这类大观察工具改成 EphemeralSession 内「读 + 提炼」返回摘要（web_research 已是此形态）。等 W0–W4 数据再决定。

## 4. 评估方案
- 题集：longchain seed 6 + hard 10 + attention 6；W0 新增「长上下文填充」变体与「精确回忆」探针；留 20% 题只做最终验证。
- 对照：无管理 / 只有现状（轮首机械 + 轮间摘要）/ +W3 / +W2 / +W4 / 全开；每组 Pass^2。
- 压缩质量单测（TRACE 式）：从同一执行状态出发，PRE（原文）与 POST（压缩后）各续跑 ≤5 步，计 blocked + repeated 动作数之差；复用 mock 环境可离线跑；用于比模板与 K 值。
- 算力：Qwen 在 Colab（随用随关），Gemma 本机；两端同题同种子；长跑挂 task-sentinel，证据入 `benchmarks/context_knee/`。

## 5. 实施阶段与验收
| 阶段 | 内容 | 出口 |
|---|---|---|
| P0 ✅ 2026-10-31 | W0 harness + 两端（Colab）拐点测量 | 曲线与结论见 §1.4「W0 实测」与 `ANALYSIS.md`；D1–D3 初值已回填 §6；prompt 分布未做（需 sessions 放行） |
| P0.5 ✅ 2026-10-31 | Gemma 4 31B QAT-W4A16 Colab 拐点补测（用户讨论后加测） | 49K 无拐点、回忆 100%、比 26B +30 点（§1.4、ANALYSIS.md §6）；31B 定为 Gemma 侧唯一选项（D23）；本机复核取消（方向修正） |
| P1 | **W-3 阵容收束** → **W-1 远端配置修复（MTP 默认关 + 采样可配）** → W-2 前缀瘦身 → W1 上限护栏 → W3 思考回放（含变异证明、差分测试更新） | 注册表 4 条、check 全过、31B 图文联调过；MTP 关后同一 harness 复测 4 档回到 W0 水平；护栏用例全红 → 全绿；思考回放不降成功率且峰值 prompt 下降 |
| P2 | W2 步骤边界压缩 + W5 小修 | 拐点以上题集成功率 / Pass^2 达 §2 目标；全量重预填充次数有账 |
| P3 | W4 结构化摘要 + 校准 + 成对续跑评估 | 模板在成对续跑上优于现模板；K 值定 |
| P4（可选） | W7、模板自动优化（ACON/TRACE 思路，用失败案例让云端模型改模板，仅合成数据） | 视 P3 结果 |
| P-last | 本机执行端推理栈：两条 MLX 4bit 条目装载 + 一次 longchain 冒烟 | 能跑即过，不计速度（用户 10-31） |
每阶段：reviewer 一审 + 主会话逐行（守卫型改动变异证明）；收官跑 stage-reviewer；文档与 harness 当天入库。

## 6. 决策记录
| 编号 | 决策项 | 决定 / 候选 | 依据 |
|---|---|---|---|
| D0 | 项目归属 | **Agent-L0 自研编排层**【定，用户 10-31】 | — |
| D13 / D14 | 框架 / 后端 | 不换：编排层 + MLX / vLLM【定】 | 1.2 |
| D1 / D2 | soft / hard 阈值 | **Qwen（远端，前提 MTP 关）soft 24,576 / hard 39,322【定，用户 10-31】**（49K 内无拐点，取 49K × 0.5 / × 0.8；57K 若成立则 28,672 / 45,875；ceiling 53,248）；MTP 开时无可用区间。**Gemma 待 P0.5**：26B fp8 拐点 8K–16K 按公式得 soft 4–8K / hard 6.5–13K，低于生产前缀 9.4K，不能直接执行；31B W4A16 在 A100 49K 无拐点，远端同口径 soft 24,576 / hard 39,322（与 Qwen 同值）；本机不再单独定值（方向修正） | W0 / ANALYSIS.md §4 |
| D3 | 尾部保留 | keep_recent 6 / keep_tail 4 维持（W0 无压缩臂、三槽回忆无位置差，无改的依据）【测·无信号】 | W0 |
| D4 | 摘要模板 | W4 固定节【测】 vs 现自由要点 | 成对续跑 |
| D5 | 更新方式 | 增量 + 每 K=4 次从 TaskState 重写【测】 | W4 |
| D6 | 压缩模型 | 主模型自己（现状）；不走外部 API（出网红线） | — |
| D7 | 截断长度 | 4000 字维持；W7 再议 | — |
| D8–D10 | 状态存储 / 写入 / 注入 | TaskState + 每迭代渲染（现状）；框架写为主 | 1.2 |
| D16 | Pass^k | k=2 | TRACE 可比 |
| D18 | 组装顺序 | 维持：不变在前、可变（task_block / tail_notes / 环境）在后 | 1.5 |
| D19（新） | 思考回放 | 跨轮丢弃 + 轮内 last_k=2【测】 | 1.3-3 |
| D20（新） | 落刀位置 | 轮首 / 步骤边界 / 溢出兜底 | 1.5 |
| D21（新） | 远端投机解码 | **Colab 部署默认关 MTP**；重开须经同一 harness 复测 | W0 F9 |
| D23 | 本机/Gemma 侧模型 | **Gemma 4 26B A4B 出阵容**（用户 10-31：上限在那里；实测 8K 拐点）。**Gemma 4 31B 定为 Gemma 侧唯一选项**：Colab 用 `google/gemma-4-31B-it-qat-w4a16-ct`（49K 无拐点、回忆 100%、含 bf16 视觉塔），本机用 `mlx-community/gemma-4-31b-it-4bit` 只需能跑 | 用户决定 + P0.5 |
| D24（新） | 前缀披露策略 | 本机 catalog + 工具路由 + skill 按需；远端 full | W-2 |
| D22（新） | 采样参数来源 | 按模型注册表条目配，缺省沿用现值；CUDA 透传 top_p/top_k/presence | W0 F1/F5 |
| D25（新） | CUDA 精度 | **只用预量化 4 bit**（Qwen INT4、Gemma QAT W4A16），不上 fp8 / NVFP4：A100 1.55 TB/s 单流解码受权重读取限制，INT4 实测 50–55 tok/s 已只有理论值一半，fp8 再腰斩；Ampere 无 FP8 张量核，fp8 实为 W8A16 无 prefill 收益；NVFP4 只在 Blackwell 有硬件收益。质量侧 QAT W4A16 49K 无拐点，无证据需要更高精度。Colab 出 Blackwell 档或 Google 出 QAT-NVFP4 再议 | 用户 10-31 + §1.4 |
| D26（新） | 部署形态 | **计算端只定 Colab，本机只做执行端**；本机推理栈排 P-last，能跑即可 | 用户 10-31 |
| D27（新） | 模型阵容 | 注册表收束到 4 条（W-3）；缺省本机模型与 compute_vm 缺省【待拍板】 | 用户 10-31 |

## 7. 风险
| 风险 | 应对 |
|---|---|
| 步骤边界压缩使 prompt_stream 失效、本机每步重预填充 | W2 的「值不值」规则 + W0 代价表；远端放宽 |
| 思考丢弃让 Qwen 多步推理变差 | W3 按方言可配、P1 用 P0 题集对照；保留 last_k |
| 摘要被复读 / 失控输出污染 | 摘要生成同样挂复读护栏；空摘要不推进水位线（已有） |
| Gemma 4 JSON 约束下复读塌缩（gemma#622） | 复读护栏已覆盖工具块；本机 BPE 去 token 器延迟（DEFERRED §7）仍是盲区，W0 记录命中数 |
| 探针填充不像真实历史 → 拐点失真 | 填充取自脱敏真实观察形态；与现网 prompt 分布对照 |
| 模板对题集过拟合 | 20% 题只做最终验证 |
| 投机解码 / 量化件在长上下文的隐性退化（W0：MTP 开 16K 起崩） | 部署默认关；任何推理栈变更（vLLM 版本、draft 配置、量化件）都用 context_knee harness 复测 4 档 |
| 测量形态 ≠ 生产形态：模型与精度已对齐（Colab INT4 / QAT W4A16 即生产），仍差前缀 0.4K vs 9.4K | W-2 的 `--prefix prod|catalog|routed` 三档补测；阈值按生产形态定 |
| 31B QAT 的视觉塔在 vLLM 上未端到端验过 | W-3 验收项：Colab 部署后图文请求通过一次 |

## 8. 参考（已逐页核实）
- ACON，Kang et al.，ICML 2026，arXiv 2510.00615，`microsoft/acon`（峰值 token −26–54%，蒸馏压缩器保留 >95%，MSR 页）。
- TRACE，Min et al.，arXiv 2608.06503（v2 2026-10-14），`nokia-applied-research/Trace`（**无 LICENSE**，只引方法）；成对闭环续跑指标；附录 A.3/A.4 含 OpenClaw / Hermes / ACON 模板。
- Hermes Agent 压缩（dev.to「How compaction works in Hermes Agent」；官方 micro-compaction 文档）：阈值 50% 窗口、尾 ~20K 原文、可用小模型做摘要。
- OpenHands `LLMSummarizingCondenser(max_size, keep_first)`；LangChain `SummarizationMiddleware`；Google ADK `EventsCompactionConfig`（adk.dev/context/compaction）。
- Anthropic《Effective context engineering for AI agents》2025-09-29：compaction / structured note-taking / sub-agent architectures。
- 模型卡：`Qwen/Qwen3.8-27B`、`RedHatAI/Qwen3.8-27B-INT4`、`google/gemma-4-26B-A4B-it`、deepmind.google/models/gemma/gemma-4；gemma issues #610 / #622。
- MLX-LM README（Long Prompts and Generations、Prompt caching）、issue #137；vLLM design/prefix_caching（2026-06-23 版）。

## 9. P0 执行记录（2026-10-31）
- harness：`benchmarks/context_knee/`（run_knee / filler / quality / summarize / analyze；README）+ `backend/tests/test_context_knee_harness.py`；
  reviewer 一审 0 Critical / 3 Major（填充按题独立、伪历史命名空间与题面不相交、同口径低档基线）全部修复，另加 run 级超时与 `--resume`。
- 产物：`benchmarks/data/context-knee-20261031/`：qwen-card（7 档）、qwen-current（4 档）、qwen-current-mtp、qwen-card-mtp（4 档，MTP 开）、
  gemma-card（7 档）、gemma-current（4 档）、qwen-probeC、**gemma31b-w4a16-card（5 档，用户讨论后加测）**；`ANALYSIS.md` + `summary_by_level.csv`。
- 算力：Colab A100 五次租约（Qwen MTP 开 → Qwen MTP 关 → Gemma 26B fp8 → Qwen MTP 开对照 → Gemma 31B W4A16，约 69 分钟），已全部 stop。
- 事故与数据缺陷：① 第 118 个 run 服务端生成挂死 15+ 分钟，客户端 SIGTERM 不死，续跑进程与旧进程并行写入 → 57344 档 6 行重复（取最新 stamp）、
  该档与 qwen-current 0 档 wall 不可用；② 4096 档 lc-hard-005 r1 / lc-hard-010 r0 填充装不下事实卡，确定性缺测；③ Qwen 对 zh 题偶有日文作答（与长度、MTP 无关）。
- 代码附带改动：`cuda/backend.py` 可选采样键透传 + 单测；`config.yaml` 注册表加 31B QAT 评测件条目（`cuda_deployment.prequantized`，无 profile 不会被路由选中）；`colab/compute_ctl.py` 对 prequantized 条目免 Gemma fp8 强制规则、拒绝再叠 `--quantization`。
- 用户决定：P0 结果与修正落盘后停下等 /compact，P1 不自动开。
- 10-31 下午：31B QAT-W4A16 补测提交 `6885f18`；讨论 Qwen vs 31B 对比（逐题差在固定题：Qwen 把 dict repr 写进 .json / 只说不写文件 / 超步；31B 把换行写成字面 `\\n` / 心算错）→ 两者各有一个可在 dialect 层归一的习惯，W-1 顺带查；用户修正方向（见 §0），核实 31B QAT 含视觉塔；方案按新方向改（W-3、D25–D27、P-last），纯文档提交。等用户 /compact 后从 W-3 开工。
