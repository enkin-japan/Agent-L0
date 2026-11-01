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
  删 `gemma-4-e4b-it-8bit`、`gemma-4-26b-a4b-it-4bit/8bit`、`Qwen3.8-27B-8bit`（本机带宽下 8bit 比 4bit 慢一半，【定，用户 10-31】）、Ornith 三条、`google/gemma-4-31B-it`（bf16，fp8 路径作废）、`google/gemma-4-26B-A4B-it`、`RedHatAI/gemma-4-26B-A4B-it-NVFP4`。
  31B QAT 条目 capabilities 改 text+vision，去掉「非生产候选」注记，补 revision 钉。
- 两个缺省值：`inference.model`（现 e4b-8bit）→ `mlx-community/Qwen3.8-27B-4bit`【定，用户 10-31】；`colab/compute_vm.py` 的 `MODEL`（现 26B NVFP4）→ `RedHatAI/Qwen3.8-27B-INT4`（现行远端生产模型，【定，用户 10-31】）。
- `colab/compute_ctl.py`：删「Gemma 4 必须 fp8」规则（bf16 条目已出阵容，成死代码），改为「CUDA 条目一律预量化，带 `--quantization` 即拒绝」；`cuda_deployment.prequantized` 两条都标。`colab/legacy/` 归档不动。
- 代码与测试：Ornith 专用 `backends/mlx/patch/text_only_vlm.py` 及其测试成死代码 → 删；注册表长度钉 14 → 4 并逐条钉 revision；model_router / local_config_fixups / inference_endpoints / cuda_backend / compute_ctl 测试里引用被删模型的用例改到留档模型上。
- 顺序：先 `scout` 列出全部引用点（path:line；排除 docs/history、benchmarks/data、colab/legacy），再按清单改；护栏型改动按仓规做变异证明。config.yaml / compute_ctl / compute_vm 由主会话亲自改。
- 验收：`./devctl.sh check` 全过；`deploy.sh models` 只列两条 CUDA 条目；Colab 部署 31B QAT 一次，图文请求与纯文本各通过一次（视觉塔 vLLM 端到端）；本机「能跑」验证排到全部工作包之后（§5 P-last）。
- **执行记录（10-31 晚，代码提交随本波）**：注册表 14→4、`inference.model`/`model_revision` 换 Qwen 4bit、`cuda.quantization` fp8→null、QAT 条目 text+vision、Qwen INT4 补 `prequantized`；compute_ctl 两道 D25 门（注册表缺标即拒、`--quantization` 整个移除并在选项层拒绝）；`text_only_vlm` 补丁及测试删除；`cloud/boundary.py` 生产档改 Qwen 4bit、B1 图表 caveat 降 pending（目录全部为 26b-4bit 口径，运行期有消费方 orchestrator→upgrade.decide_at_turn_start）；`deploy.sh models` 已只列两条。
  **带出的决定**：真分词器两道门（gemma4 DSL 语法 `test_grammar_headroom`、D.26 可见文本预算 12300 `test_model_visible_text`）钉到在册 gemma4 条目 31b-4bit 的分词器，不再跟 `inference.model` 走——同一段工具文本用 Qwen 分词器量得 14,181 tok，越 D.26 线；D.26 定线须用户拍板，qwen35 口径要不要另立预算线归 W-1（见下）。
  **残留（有意不动）**：`backend/scripts/` 压测脚本缺省仍写已出册模型（M4 Max 时期测量件，bench_baseline 遇未注册臂静默跳过；本机测速已无意义，随 P-last 一并处理或归档）；`colab/legacy/` 归档脚本仍指 26B NVFP4（归档不动，重跑须检出 ≤ aa89f8c）；`backend/models/` 磁盘上 e4b/26b/8bit/bf16 权重未删（用户自行清理）；本机 `.model-selection` 已由 26b-4bit 改指 Qwen 4bit（否则启动告警后回落缺省，结果相同）；边界目录其余条目（`deep_reasoning` weak 仍会触发）待对新档重探。
  **验收通过（10-31 22:15–22:28，一体化脚本第二次起机）**：31B QAT 于 A100 READY 用时 11.6 分钟；`/health` 身份核对通过（revision 52f3f65b、capabilities text+vision）；纯文本「3 乘 7」→「3 乘 7 等于 21。」2.7 s；图文（512×256 PNG，大字 W3 OK 2026 + 三柱 10/30/20）→「大字是"W3 OK 2026"，B柱最高。」48.1 s（首个图文请求含视觉塔预热）；三步全过，Colab 已 stop 并确认服务端移除，本次耗 1.2 units。证据 `benchmarks/data/w3-vision-smoke-20261031/`（result.json、PNG、两份日志）。W-3 全部验收项闭合。
  **验收事故（10-31 晚，纪律修正随本波）**：第一次部署 18:24 起、18:38 READY，探针却等主会话被哨兵唤醒才发；哨兵把 3 分钟循环用裸 `&` 脱管后 40 秒交差，循环 18:40 写下 EXIT_READY 无人听，60 分钟租约 19:24 到期回收，A100 空转 46 分钟、验收未做。复盘本会话 26 次哨兵有 2 次同形（10-30 23:31 盯 commit 那次主会话空等 1.5 小时）。用户判断：岗位把「机制精确」交给了最便宜的模型。
  修正（用户 10-31 拍板）：盯梢改由主会话直接起 `scripts/watch_until.sh` 后台循环（退出即唤醒主会话，中间无模型）；`task-sentinel` 降为分诊员；付费算力消费动作与租约链进同一脚本 `benchmarks/w3_vision_smoke/run_colab.sh`（起机→等 READY→跑→必停机）；CLAUDE.md 新增「Long tasks」节，主会话工作预算 400k→180k（实测自动压缩线约 230k）。

### W-1 远端推理配置修复（P0 实测带出，优先级最高）【新】
- **Colab 部署默认关 MTP**【已落地，10-31 晚】：`colab/compute_ctl.py` 原对标明 `supports_mtp` 的模型默认开投机解码；W0 显示它在 0 档就损失 25 点、16K 起崩。
  现改为**所有模型缺省 `off`**，`--speculative mtp` 显式开启才用（仍只允许 `supports_mtp` 条目）；`--draft-tokens` 不带 `--speculative mtp` 即报错；
  README「模型与 MTP」与 config.yaml `supports_mtp` 注释补实测结论（「可开」≠「默认开」）。colab 门 133 全过。
  重新验证 MTP 只能在 vLLM 升级或换 draft 配置后、用同一 harness 重测。MLX drafter（本机 Qwen 条目）是否同病：本机只做执行端（D26），随 P-last 复核。
- **CUDA 采样键透传**【已在 P0 提交 `89ae1b4` 落地】：`cuda/backend.py` 对 top_p / top_k / presence_penalty / frequency_penalty 按需透传；此前被静默丢弃。
- **按模型卡配采样**【本波】：engine.py 主路径与 oneshot 路径两处硬编码 temp 0.2 / min_p 0.05 / rep 1.1 / rep_ctx 50（vLLM 下 rep 为 HF 语义、罚整个 prompt）；
  改为注册表条目可选键 `sampling`（白名单 8 键、数值且范围校验、构造时拒启），优先级「调用方显式值 > 条目 > 旧常量」；MLX 后端补
  top_p / top_k / presence / frequency 透传（此前静默丢弃，下层 sampler 本就支持；prompt_lookup 投机路不承接 presence/frequency）；turn_runner
  审批续跑与挂起恢复两处不再自补 0.2/0.05（语义改正，但见下）。**温度与 min_p 不入条目**（reviewer 10-31 Major 1）：App 与 `/turns`、`/chat`
  的 Form 缺省每轮显式带 0.2 / 0.05，显式值优先，条目温度在聊天轮永不生效，只会在摘要/oneshot 路径冒出来造成混合口径；改协议缺省与 App 旋钮
  超出本波，留作残留。因此 config.yaml 两条 CUDA 条目只填不依赖调用方的键：Qwen3.8 top_p 0.95 / top_k 20、Gemma 4 top_p 0.95 / top_k 64，
  rep 均 1.0；MLX 两条目不设键 = 沿用旧常量（profile 即该口径）。W0 显示采样差异在噪声内，故此项是「合规与可配」而非「性能修复」。
  残留：batch_lane 只读 temp/min_p/rep（top_p/top_k 走泳道会丢，MLX 条目未设故未暴露）；OpenAI 门面非流式路未接取消入口。
- **可见文本预算线：两家族共用一条**【D28，用户 10-31 拍板；本波落门】：D.26 的 12,300 tok 线原只量 gemma4 口径（gemma4 DSL 逐件声明、gemma4 分词器）。
  决定：**两家族共用同一条 12,300 线、同口径**——各按自家方言逐件渲染工具声明（gemma4 DSL 单件 / qwen35 `<tools>` 块内单行 JSON）、自家分词器计数，
  都不含包裹与固定教学文本。实测 gemma4 12,036 / qwen35 12,144，两档都在线内，门直接生效（无需 xfail）。旧注释里的 14,181 是「gemma4 DSL 文本
  用 Qwen 分词器数」的错口径，已废。qwen35 整块含 `# Tools` 头与调用格式教学为 12,325，这段固定文本无门，归 W-2 瘦身时一并看。
- **服务端断连止损**【本波】：W0 观察 active_generations 持续为 1。核实：server.py 断连只置 `cancel` 标志，生成线程仅在每个 chunk 入队时检查，
  不 close 后端流生成器、不调引擎 abort；预填期无 chunk → 最长等 CUDA 首包 600 s。修法：取消时触发后端 abort 链（CUDA `_produce` finally 已有
  `engine.abort`）。harness `run_knee.py` 无信号处理、推理线程阻塞在 `iter_lines` 且 executor 非守护 → SIGTERM 不死；补 SIGINT/SIGTERM 处理 + 兜底退出。
- 顺带查（dialect 层归一）：方言层无字面 `\n` 清洗钩子（qwen35 `_coerce` 只还原 JSON 字面量；gemma4 `_scalar`/`parse_arguments`）。
  两模型各一个习惯（Qwen 把 dict repr 写进 .json、31B 写字面 `\n`）是否值得在 dialect 层归一，留待 W-2/W1 有同口径复测数据再定，本波不做。

### W-2 静态前缀瘦身（用户 10-31 采纳；11-01 按 scout 摸底修订，用户「按这六条开工」）【已落地 11-01】
- **前提修正（scout 11-01）**：旧数 9,424 已过期——同口径（system + 声明，include_env=False）gemma4 full **13,565** / qwen35 13,708，RAG 在线 +rag_admin 14.8K，
  system 本身 1.4K。原 ③「skill 按需」已是现状（skill 正文从不进 system prompt，`skill_invoke` 即 load_skill，索引 325 tok），本波无事。
  原 ①「本机 Gemma catalog / 远端 Qwen full」与 D26/D27 冲突：prompt 一律在执行端装配、原样上行 Colab，只能按模型家族/条目分档。
  原 ②「每轮 embedding 路由」会击穿前缀缓存（工具声明紧跟 system，换集合 = 指纹失配整段重预填），且融合模式 rag_server 无 /embed、RAG 不保证在线。
  原 catalog 实现只省 43%（62/64 描述单行，取首行 = 全文，只省掉参数）。
- **落地（D29–D32）**：
  - D29 分档：注册表条目 `tool_schema_mode` > `agent.tool_schema_mode` > full，值限 full|catalog 否则拒启；gemma4 两条目 catalog（稠密全注意力，prefill 随前缀线性涨），
    qwen35 两条目 full；模式跟配置主模型条目，model_router 专科切换不改披露。接线 `app._make_orchestrator`（有钉接线的测试）。
  - D30 目录行：`BaseTool.summary`（不进 get_schema，full 字节与 W-1 一致）或描述首句，硬截 80 字；60 件工具补 summary。
  - D31 路由只在 catalog：任务建立时按「goal + 建任务的用户请求」词法选 top_k=6（候选 = 全部 − 元工具 − core 七件 memory_write/edit_file/write_file/find_files/list_dir/skill_invoke/run_shell_command），
    存 `TaskState.routed_tools` 进快照，任务期冻结（replan 不重算）；任务结束后会话级 `last_routed_tools`（routing.json）保留到下一任务建立，分支继承、轮级/定时轮回滚恢复
    → 每任务一次前缀失配（建任务处）。首个任务前纯聊天轮无路由件。语法面恒用完整 schema。
  - D32 词法 BM25（k1 1.2 / b 0.75）确定性：小写 ASCII 词（含下划线切片与整词）+ CJK 双字 bigram；查询词去英文停用词/泛用请求词、df>N/2 高频词（N≥4）、直呼常驻工具名的切片；
    得分 0 不入、不足不补、同分按名。embedding 打分归 **W-2b**（需 rag_server 融合模式加 /embed）。
  - 量法：harness `--prefix mock|prod|catalog|routed`（`benchmarks/context_knee/prefix.py` 纯装配，复用 tool_disclosure，生产工具只声明不执行，mock 工具撞名优先；
    meta 记 prefix_mode / prefix_base_tokens / 按题路由名单，续跑按 prefix_mode 校验；mock 档 16 题字节不变）。
- **实测（`backend/scripts/measure_prefix.py`，离线真分词器）**：

  | 整段前缀 | gemma4 | +rag | qwen35 | +rag |
  |---|---|---|---|---|
  | full | 13,565 | 14,773 | 13,708 | 14,900 |
  | catalog（元+core） | **5,266** | 5,417 | 5,280 | 5,414 |
  | catalog + 路由（三例） | 5,771–6,703 | | 5,773–6,782 | |

  逐件声明门：full 12,036 / 12,144 不变（BUDGET 12,300）；catalog 3,735 / 3,713，新门 `BUDGET_CATALOG` 3,810。harness 四档 gemma4：mock 268 / prod 13,582 / catalog 5,281 / routed 6.3–7.6K。
  原定 catalog ≤ 5,000 因 run_shell_command 入 core（+~460）未达，主会话接受（描述自称 PRIMARY、system 点名，目录行+往返不值）。
- **残留**：词法路由对英文/口语请求召回有限（W-2b embedding）；`stable_prefix_tokens_est` 不含路由件（+0.5–1.4K，W1 定阈值按真实声明算）；
  prod/catalog/routed 三档 Colab 复测另排时段（低于前缀 base 的档位 filler 报 error 行是旧行为）；qwen35 固定教学文本 122 tok 与 skill_invoke 325 tok 不动（事故驱动措辞）；
  catalog 下的 31B 质量（目录行 → get_tool_schema 逃生口是否被用）尚无实机数据，随三档复测看；gemma4 profile 数字仍是 full 口径。
### W1 上下文上限护栏【已落地 11-01；守卫型 → 变异证明】
- **前提修正（scout 11-01）**：① 远端执行端打的是自家 inference/server.py `/v1/generate`，**没有 vLLM `/v1/models`** 可查，上限只能由算力端 `/health inference_options` 暴露；
  ② MLX `max_kv_size` 对 gemma4 / qwen3_5 **无效**（二者自带 make_cache，mlx_vlm 直接忽略），本机上限只能是编排层预算；③ 执行端在 remote 模式**也有分词器**
  （remote/backend.py `_ProcessorShim(AutoTokenizer)`），不必字符/4；④ `maybe_summarize_context` 只能在轮边界调（rebuild_from_log 会清掉进行中的本轮），
  轮内「强制摘要折叠」归 W2；⑤ 轮内 history 只增不减，末次 prompt_tokens ≈ 峰值，真缺口是**轮中不判定**而非基准错位；⑥ `stable_prefix_tokens_est` 还漏了 system 提示；
  ⑦ 客户端 max_tokens 从未钳制（ceiling = 上限 − 12,288 的前提不成立）；⑧ 总熔断与 runaway 的 warning 都没 add_assistant_message，用户看不到提示文字——样板只能是复读护栏。
- **落地（D1/D2 + D33–D35）**：
  - D33 上限来源：后端可选属性 `context_limit`（engine getattr 探测，与 runtime_options 同手法）：remote = 算力端 `/health inference_options.max_model_len`
    （cuda `public_options` 新增；复用模型身份护栏那一次 /health），缺则回落执行端 `inference.cuda.max_model_len` 并告警 + 事件 `context_limit_fallback`，再缺 65536；
    inprocess mlx = 新键 `inference.mlx.context_limit`（65536）；inprocess cuda = `max_model_len`。app lifespan 引擎装载后 `bind_context_limit()`；阈值与上限不相容（hard ≥ ceiling）→ ValueError **拒启**。
  - D34 量法：每次主循环生成前 `engine.measure_prompt(**prompt_args)`——与 `generate_once` 共用同一份 prompt_args 与同一个 `_render_full_prompt`（全量渲染，纯函数，
    不碰 prompt_stream），执行端分词器 encode 计数（无分词器才退 `fallback_estimate` = ASCII/4 + 非 ASCII 字数，宁高估），每张图片 +1,024（占位符文本量不到视觉 token）。
    hard 尾注追加后重量，量到的就是发出去的。只守主循环；goal-verify / 沙箱 / 摘要子生成不守。
  - D35 四档处置（`agent.context_guard`，绝对 token 口径，含前缀；缺省 soft 24,576 / hard 39,322 = D1/D2，generation_reserve 12,288，未知键/非正整数/soft≥hard 拒启）：
    soft 只记事件；hard 记事件 + 尾注提醒收敛（W2 落地后在此触发步骤边界压缩）；ceiling = 上限 − reserve → 先**轮内应急机械压缩一次**
    （保护窗口 2 条之外的工具观察折成桩；先把 dict 换成拷贝断开与落盘切片/暂存轮的别名，原件登记 `session.guard_persist_originals`，落盘切片一律经
    `session.persistable_messages()` 换回原件——turn_runner 两处 + schedule/runner 一处；代价前缀缓存整段失配一次），仍 ≥ ceiling → 照复读护栏样板以 warning 终止本轮
    （add_assistant_message / set_activity done / turn_abort reason=context_guard / 轮统计），**后端收不到那次请求**。客户端 max_tokens > reserve 一律钳回 + 事件（guard 关掉也钳）。
    本轮峰值 `peak_prompt_tokens`（量值与实测取大）与最高档 `context_guard_level` 进轮统计 metrics；inspector 暴露 limit / ceiling / soft / hard / 峰值。
  - `stable_prefix_tokens_est` 纳入 system 提示估算（summary_* 阈值随之 +~1.4K，仍是「前缀之上」口径）。
  - harness：run_knee 每条记录 `peak_prompt_tokens = max(gens.prompt_tokens)`，summarize/analyze 加「峰值 prompt 中位」列（§2 验收「峰值 ≤ 拐点 70%」的字段）。
- **门**：reviewer 一审 0 Critical / 2 Major（增量路量法偏低→改走 _assemble_prompt；算力端重开不重绑→探活复核重算重绑）/ 8 Minor / 5 Nit，除 m5（图片常量）与 n5 外全修；verifier 变异证明 14 个变异体全红、sha256 逐字节还原；backend 全量 3941 passed / 3 skipped（原 3851，+90：test_context_guard 88 条、harness 分析 2 条）。
- **残留**：ceiling 兜底是机械压缩不是摘要折叠（W2 做保住本轮尾部的折叠变体后替换）；hard 档只提醒不压缩（W2 接）；子生成不守（沙箱自有熔断 max_sub_generations）；
  图文轮计数靠 +1,024/图常量；量法在事件循环线程 encode（50K 量级数十毫秒）；算力端过旧未报 max_model_len 时 ceiling 按执行端配置算，`--max-model-len` 改值感知不到（告警已发）；
  Colab 实机 ceiling 终止路径未跑过（FakeEngine 证明），随三档复测看；MLX 本机上限只是预算，KV 实际无界（P-last）。

### W2 轮内步骤边界压缩【改】
- 在 `complete_step` 落地时（已有 step_completed 事件）对**该步的工具观察**调用 compact_tool_observations（同一预算器，keep_recent 保护窗照旧），
  被压观察的全文已在 spill，占位桩带 `spill:<stem>` 句柄 → 模型回读有路。该步的 result（已由模型写进 TaskState）就是「步骤摘要」，不另调模型。
- 预算越过 hard 而无步骤边界时，退化为「按体量驱逐」（现有逻辑）；仍越过 → W1 的强制摘要。
- KV 代价：按 W0 的 prefill 曲线定「值不值」规则 —— 当被压掉的 token 数 × 后续预计迭代数 > 一次全量重预填充成本时才落刀；否则等到轮首。远端 vLLM 块粒度小，可放宽。
- 不碰的：审批 / 挂起续跑路径（compact_history=False）继续跳过；最近 keep_recent 条永不改。

### W3 思考回放策略【已落地 11-01；守卫型 → 变异证明】
scout 11-01 校正前提：history 里思考只有 `thoughts` 一个字段（解析器产 str），落盘每轮全量；两方言 builder 对每条带 thoughts 的消息都回放、无轮边界概念；
**只改渲染在增量路上无效**——`prompt_stream` 存的是含思考的原始生成字节，有效性只比 `messages_fingerprint(history[:seen])`（内容哈希）与工具集，
流有效时旧思考照原样回放。KV 代价：MLX 两款缓存不可裁（递归/线性状态，excess>0 拒复用），历史消息渲染一变即整段冷预填；Colab vLLM 前缀缓存按块、只重填分叉点之后。
官方口径：Gemma 4 QAT 模板回放「末条 user 之后」全部思考，Gemma 4 MLX 4bit 模板只回放末条 user 之后**带 tool_calls** 的；Qwen3.8 preserve_thinking 缺省 true（= 旧恒回放）。
- **D36 投影在引擎层、方言不变**：`core/thinking_replay.py` 纯函数 `project_thoughts(history, turn_start, policy)` 产等长 view（未改消息同对象、改动消息浅拷贝），
  `engine._assemble_prompt` 一次装配只投影一次，`stream_valid` / 增量 delta / 全量渲染 / `prompt_stream.commit` 全走 view——投影一变指纹即变、自然退全量；
  另加 `view[ps.seen] is history[ps.seen]` 身份校验（占位消息的思考被投影掉时流里原始字节与视图不符）。W1 的 measure_prompt 经同一 `_assemble_prompt`，量到的即发出的。
- 模式（注册表条目级 `thinking_replay`，缺省 `last_k`；`thinking_replay_k` 缺省 2；非法装配期拒启）：`all` = 全部回放（旧行为，差分锚点）；
  `tool_turns` = 历史轮一律丢（Gemma 卡禁回放上轮思考），本轮只回放带 tool_calls 的生成；`last_k` = 历史轮丢、本轮只留最近 K 次带思考的生成。
  纯思考消息（无正文无调用，全思考 nudge 留档）本轮恒保留不占名额、历史轮改一行占位 `(earlier thinking omitted)`，避免空 assistant 轮。
  轮边界取 `session.turn_history_start_idx`（W1 已在审批/挂起续跑与定时任务保留），缺则回落「末条 user 之后」（沙箱/门面路 = 官方模板语义）。
- 按部署形态配（config.yaml 四条目）：本机 MLX 两条 `tool_turns`（轮内 ≈ 全留，只在换用户轮时冷填一次；last_k 在 MLX 上每步冷填不可接受），Colab 两条 `last_k`（K=2 待测）。
- 顺手修：orchestrator 两处合成 `[System upgrade]` 思考是 list（以 Python repr 进 prompt、Swift 端解码 nil）改 str；轮统计加 `thinking_replay`（mode/kept/dropped/dropped_chars），思考占 prompt 的比例首次可见。
- 不做：非纯思考消息丢思考后不留占位（步骤 result 已在 task_block 逐步渲染，占位是假思考；占位只给历史轮的纯思考消息，免空 assistant 轮）；并入 result（已是现状）；history 落盘不改。
- 门：reviewer 一审 + 主会话二审逐行（投影规则、流校验、commit 用 view）+ verifier 变异证明；提交 `__W3_HASH__`。
- 残留：K=2 是初值，验收「思考回放不降成功率且峰值 prompt 下降」要等 Colab 三档复测（`--prefix` 一并）；本机 MLX 轮内不省思考（设计取舍，P-last）；
  tool_turns 下仅 content 的生成思考会被丢（官方同形）：verify_nudge / premature_finish 推回后同轮再生成时 ps.seen 身份校验失败退全量，MLX 上即一次整段冷预填；
  注册表外模型（本地路径，options 为 {}）无处配键、走模块缺省 last_k，本机 MLX 跑表外模型每步冷填（主会话裁定不按后端形态分缺省，表外是实验路）；
  backend/scripts 的 bench_a8_multiturn / soak_longrun 镜像装配不接投影，是 all 口径（头注已注明）。

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
| D19（新） | 思考回放 | 跨轮丢弃 + 轮内 last_k=2【测，K 初值；落地见 D36】 | 1.3-3 |
| D36（新） | 思考回放实现 | 投影在引擎层（`core/thinking_replay.py`），view 参与 prompt_stream 指纹与 commit、加 ps.seen 身份校验，方言 builder 不变；条目级 `thinking_replay: all \| tool_turns \| last_k`（缺省 last_k、K=2，非法拒启）；本机 MLX 两条 tool_turns（缓存不可裁）、Colab 两条 last_k；纯思考消息本轮恒留/历史轮占位【定，主会话 11-01】 | W3 |
| D20（新） | 落刀位置 | 轮首 / 步骤边界 / 溢出兜底 | 1.5 |
| D21（新） | 远端投机解码 | **Colab 部署默认关 MTP**【已落地 W-1，10-31 晚：compute_ctl 所有模型缺省 off】；重开须经同一 harness 复测 | W0 F9 |
| D23 | 本机/Gemma 侧模型 | **Gemma 4 26B A4B 出阵容**（用户 10-31：上限在那里；实测 8K 拐点）。**Gemma 4 31B 定为 Gemma 侧唯一选项**：Colab 用 `google/gemma-4-31B-it-qat-w4a16-ct`（49K 无拐点、回忆 100%、含 bf16 视觉塔），本机用 `mlx-community/gemma-4-31b-it-4bit` 只需能跑 | 用户决定 + P0.5 |
| D24（改） | 前缀披露策略 | ~~本机 catalog / 远端 full~~ → **按模型家族/条目分档**（prompt 在执行端装配、原样上行，D26）；skill 按需已是现状 | W-2 scout 11-01 |
| D29（新） | 披露模式解析 | 注册表条目 `tool_schema_mode` > `agent.tool_schema_mode` > full，值限 full\|catalog 否则拒启；gemma4 catalog、qwen35 full；专科切换不改披露【定，用户 11-01】 | W-2 |
| D30（新） | 目录行文本 | `BaseTool.summary`（不进 full schema）或描述首句，硬截 80 字【定，用户 11-01】 | W-2 |
| D31（新） | 工具路由时点 | 只在 catalog；任务建立时算一次、任务期冻结、任务结束后保留到下一任务；core 七件常驻；每任务一次前缀失配【定，用户 11-01；退出不失配为主会话修订】 | W-2 |
| D32（新） | 路由打分器 | 本波纯本地词法 BM25（确定性、离线可用）；embedding 版归 W-2b（rag_server 融合模式需 /embed）【定，用户 11-01】 | W-2 |
| D33（新） | 上下文上限来源 | 后端可选属性 `context_limit`：remote 取算力端 `/health inference_options.max_model_len`（缺则回落执行端 `inference.cuda.max_model_len` 并告警），inprocess mlx 取新键 `inference.mlx.context_limit`（`max_kv_size` 对两款模型无效，只是编排层预算），cuda 取 `max_model_len`；装配后绑定，探活复核处重算重绑；hard ≥ ceiling 装配失败【定，主会话 11-01】 | W1 |
| D34（新） | prompt 量法 | 每次主循环生成前 `engine.measure_prompt`，与 generate_once 同一份入参、同一个 `_assemble_prompt` 判定，执行端分词器 encode；无分词器/encode 异常退 ASCII/4+非 ASCII 字数；每图 +1024；只守主循环【定，主会话 11-01】 | W1 |
| D35（新） | 四档处置 | `agent.context_guard` 绝对 token 口径：soft 24,576 记事件、hard 39,322 记事件+尾注收敛（W2 接压缩）、ceiling = 上限 − 12,288 → 轮内应急机械压缩一次（落盘经 persistable_messages 保全文）仍超则照复读护栏样板 warning 终止；max_tokens 钳到 reserve；峰值与最高档入轮统计【定，主会话 11-01；数值 D1/D2 用户 10-31】 | W1 |
| D22（新） | 采样参数来源 | 按模型注册表条目配（`sampling` 键，W-1；温度/min_p 不入条目，仍是 App 旋钮），缺省沿用现值；CUDA 透传 top_p/top_k/presence/frequency（`89ae1b4`）；MLX 同四键透传（W-1） | W0 F1/F5 |
| D25（新） | CUDA 精度 | **只用预量化 4 bit**（Qwen INT4、Gemma QAT W4A16），不上 fp8 / NVFP4：A100 1.55 TB/s 单流解码受权重读取限制，INT4 实测 50–55 tok/s 已只有理论值一半，fp8 再腰斩；Ampere 无 FP8 张量核，fp8 实为 W8A16 无 prefill 收益；NVFP4 只在 Blackwell 有硬件收益。质量侧 QAT W4A16 49K 无拐点，无证据需要更高精度。Colab 出 Blackwell 档或 Google 出 QAT-NVFP4 再议 | 用户 10-31 + §1.4 |
| D26（新） | 部署形态 | **计算端只定 Colab，本机只做执行端**；本机推理栈排 P-last，能跑即可 | 用户 10-31 |
| D27（新） | 模型阵容 | 注册表收束到 4 条（W-3）；本机缺省 `mlx-community/Qwen3.8-27B-4bit`，compute_vm 缺省 `RedHatAI/Qwen3.8-27B-INT4`，Qwen 8bit MLX 删【定，用户 10-31】 | 用户 10-31 |
| D28（新） | 可见文本预算线口径 | **两家族共用一条 12,300 线、同口径**（用户 10-31）：各按自家方言逐件渲染声明、自家分词器计数，不含包裹与固定教学文本；实测 gemma4 12,036 / qwen35 12,144，门已生效 | W-3 带出 / W-1 |

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
| 测量形态 ≠ 生产形态：模型与精度已对齐（Colab INT4 / QAT W4A16 即生产），仍差前缀 0.3K vs 13.6K（full）/ 5.3K（catalog） | harness `--prefix mock\|prod\|catalog\|routed` 四档已落地（W-2）；Colab 复测另排时段；阈值按生产形态定 |
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
- 10-31 晚：W-3 阵容收束代码落地（scout → 主会话改 config/compute_ctl/compute_vm → implementer 删补丁改测试 → reviewer 一审无 Critical/Major → verifier 变异证明 → 提交）；执行记录与残留见 W-3 节。Colab 31B QAT 图文联调待做。
- 11-01 凌晨（/compact 后开 W-1；用户 10-31 拍板预算线「瘦身后两家族共用一条」）：scout 一次摸清采样链路 / 预算门渲染 / 服务端断连路径；
  主会话亲改 compute_ctl 缺省 off 与 config.yaml；两个 implementer 并行各自 worktree（A 条目级采样 + D28 预算门；C CancelScope 断连止损 + harness 信号），
  diff 合回主树后 reviewer 一审 0 Critical / 5 Major / 8 Minor（Major 1 条目温度在聊天轮永不生效 → 改为温度/min_p 不入条目；Major 2 harness 口径续跑不报
  → meta entry_sampling + 兼容校验；Major 3/4 看门狗与二次信号 → 12 s + 硬退处理器；Major 5 方案与门不一致 → 改方案），修补后 138 项定向测试绿；
  verifier 变异证明 8 个变异体（采样校验 bool/NaN/top_p 边界/条目生效、compute_ctl 缺省、MLX 透传、server scope.cancel、CUDA 钩子 q.close）全红、源文件逐字节恢复。
  W-1 实测数：gemma4 预算 12,036 / qwen35 12,144（同口径，线 12,300）；qwen35 整块 12,325 含固定教学文本归 W-2。
  残留（不阻断 W-1 收口）：聊天轮温度/min_p 仍是 App 与 Form 缺省 0.2/0.05 的旋钮（改协议归后续）；batch_lane 不透传 top_p/top_k；门面非流式路未接取消入口；
  CUDA 取消异常靶文案匹配而非专用异常类；MLX 预填期不可中断（无等待点）；MLX drafter 是否同 MTP 病归 P-last。提交 `4cb0286`（全量 check 通过）。
- 11-01 上午（/compact 后开 W-2；用户「按这六条开工」）：scout 摸底推翻三前提（9,424 过期 → 13,565；skill 按需已是现状；本机/远端分档与 D26 冲突；每轮路由击穿缓存且融合模式无 /embed；
  catalog 只省 43%）→ 主会话提六条修订、用户拍板（D29–D32）→ 主会话亲改 config.yaml（条目级 tool_schema_mode、agent.tool_routing）→ implementer A 后端
  （tool_disclosure 纯函数模块、编排器接线、60 件 summary、measure_prefix）‖ implementer B harness 四档（prefix.py）→ reviewer 一审 0 Critical / 1 Major（core 件数断言过期）/ 9 Minor，
  复审 0/0/5 Minor/4 Nit（分支继承路由集合、轮级与定时轮回滚恢复、英文停用词补全、audit 声明顺序对齐生产注册序、transcript 行体积）全部修 → verifier 变异证明 12 个变异体全红、
  sha256 逐字节还原。实测与残留见 W-2 节。主会话复审读法：tool_disclosure / 编排器·会话·快照·app 接线 / config.yaml / prefix.py / run_knee 增删行 / 路由冻结测试断言逐行，summary 行与文档按摘要。
- 11-01 下午（/compact 后开 W1）：scout 推翻四前提（无 /v1/models、MLX max_kv_size 无效、执行端有分词器、摘要折叠只能轮边界）→ 主会话定 D33–D35、亲改 config.yaml
  （inference.mlx.context_limit、agent.context_guard）→ implementer（context_guard 纯函数模块、三后端 context_limit、engine.measure_prompt、编排层四档护栏与应急压缩、
  persistable_messages 落盘保全文、inspector、harness 峰值）→ reviewer 一审 0 Critical / 2 Major（增量路量法偏低；算力端重开不重绑）/ 8 Minor / 5 Nit → 同一 implementer 修补
  （量法改走 _assemble_prompt；compute_lifecycle.refresh_context_limit 探活重绑含回滚；encode 异常退估算；旗标/还原表按轮挂 session；缺键 max_tokens 受钳；复合路径与落盘端到端测试补齐）
  → 主会话二审逐行（护栏段/量法/重绑/钳制/落盘换回）→ verifier 变异证明 14 个变异体全红（边界等号、soft≥hard、hard<ceiling、钳制、终止 return、尾注重量、三遍循环、
  拷贝断别名、persistable_messages、remote 取值、增量路量法、重绑赋值、turn_runner/schedule 落盘切片）、sha256 逐字节还原。backend 3941 passed。提交 `87fd7d3`（全量 check 过，已推送）。残留见 W1 节。
