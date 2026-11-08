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

  逐件声明门：full 12,036 / 12,144 不变（BUDGET 12,300）；catalog 3,735 / 3,715，新门 `BUDGET_CATALOG` 3,810。harness 四档 gemma4：mock 268 / prod 13,582 / catalog 5,281 / routed 6.3–7.6K。
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
  - D34 量法：每次主循环生成前 `engine.measure_prompt(**prompt_args)`——与 `generate_once` 共用同一份 prompt_args 与同一个 `_assemble_prompt` 判定（流有效走「已记录字节流 + 增量段」，
    否则全量渲染；量测不提交 prompt_stream——reviewer 一审 Major 1 由 `_render_full_prompt` 改来，量到的就是发出去的），执行端分词器 encode 计数（无分词器才退 `fallback_estimate` = ASCII/4 + 非 ASCII 字数，宁高估），每张图片 +1,024（占位符文本量不到视觉 token）。
    hard 尾注追加后重量，量到的就是发出去的。只守主循环；goal-verify / 沙箱 / 摘要子生成不守。
  - D35 四档处置（`agent.context_guard`，绝对 token 口径，含前缀；缺省 soft 24,576 / hard 39,322 = D1/D2，generation_reserve 12,288，未知键/非正整数/soft≥hard 拒启）：
    soft 只记事件；hard 记事件 + 尾注提醒收敛（W2 落地后在此触发步骤边界压缩）；ceiling = 上限 − reserve → 先**轮内应急机械压缩一次**
    （保护窗口 2 条之外的工具观察折成桩；先把 dict 换成拷贝断开与落盘切片/暂存轮的别名，原件登记 `session.guard_persist_originals`，落盘切片一律经
    `session.persistable_messages()` 换回原件——turn_runner 两处 + schedule/runner 一处；代价前缀缓存整段失配一次），仍 ≥ ceiling → 照复读护栏样板以 warning 终止本轮
    （add_assistant_message / set_activity done / turn_abort reason=context_guard / 轮统计），**后端收不到那次请求**。客户端 max_tokens > reserve 一律钳回 + 事件（guard 关掉也钳）。
    本轮峰值 `peak_prompt_tokens`（量值与实测取大）与最高档 `context_guard_level` 进轮统计 metrics；inspector 暴露 limit / ceiling / soft / hard / 峰值。
  - `stable_prefix_tokens_est` 纳入 system 提示估算（summary_* 阈值随之 +1,597，仍是「前缀之上」口径）。
  - harness：run_knee 每条记录 `peak_prompt_tokens = max(gens.prompt_tokens)`，summarize/analyze 加「峰值 prompt 中位」列（§2 验收「峰值 ≤ 拐点 70%」的字段）。
- **门**：reviewer 一审 0 Critical / 2 Major（增量路量法偏低→改走 _assemble_prompt；算力端重开不重绑→探活复核重算重绑）/ 8 Minor / 5 Nit，除 m5（图片常量）与 n5 外全修；verifier 变异证明 14 个变异体全红、sha256 逐字节还原；backend 全量 3941 passed / 3 skipped（原 3851，+90：test_context_guard 88 条、harness 分析 2 条）。
- **残留**：ceiling 兜底是机械压缩不是摘要折叠（W2 做保住本轮尾部的折叠变体后替换）；hard 档的压缩已由 W2 钩子接管（`6a092c8`）；子生成不守（沙箱自有熔断 max_sub_generations）；
  图文轮计数靠 +1,024/图常量；量法在事件循环线程 encode（50K 量级数十毫秒）；算力端过旧未报 max_model_len 时 ceiling 按执行端配置算，`--max-model-len` 改值感知不到（告警已发）；
  Colab 实机 ceiling 终止路径未跑过（FakeEngine 证明；P1 复审后补 FakeEngine 端到端 soft→hard→ceiling 用例）——三档复测 harness 直调 `engine.generate_once` 不经编排层，覆盖不到，须 W-K（P1 复审 M1）；MLX 本机上限只是预算，KV 实际无界（P-last）。
- **11-02 Colab 实机**（`benchmarks/data/context-knee-20261102/ANALYSIS.md` §1.5）：57K 档护栏开的两臂 20/20 run 在首次生成前 fold（量得 56,823–58,249 → 发出 26,794–32,123），超 ceiling 实发 0 次、实发最大 40,767，量法与引擎计数 3,189 次全同；context_guard 终止 0（FakeEngine 干跑预测的 abort 在真引擎上被首生成前的 fold 取代）。代价：fold 把轮内填充连同事实卡一起折掉，回忆 91% → 0%，之后 3/5 run 去调桩 rag_search 触发「检索未完成」拦截。「当前 user 轮超限时折什么、留什么」归 D40 待拍板。guard off 臂 1 个 run 第 10 次生成发 65,538 tok 被 vLLM 拒，是关护栏的预期后果。


### W2 轮内步骤边界压缩【已落地 11-01；守卫型 → 变异证明】
scout 11-01 校正前提：① 被压观察未必已 spill——只有 > observation_max_chars（4000）的才落盘，桩无 `spill:<stem>`、有的是只指中段的 file_extract 长路径，
≤4000 字的被压后模型读不回来；② complete_step 没有单点可挂——它在同批物理调用之前分派，同批观察到 `add_tool_message` 才进 history；③「被压 token × 后续迭代 >
一次全量重填」两边量纲不齐（token·次 vs 秒），W0 没有每 token·每迭代的保留代价系数；④ 轮内就地改会把桩写进 history.jsonl（当轮切片与暂存轮共享 dict）；
⑤ TaskState 的步没有 history 下标。
- **D37 触发按 W1 档位，不按代价公式**：`agent.step_compaction`（enabled / trigger_level soft / cold_refill_trigger_level hard / keep_recent 2 / digest_chars 200 / spill_full，
  未知键与非法值装配失败）。起压档按后端缓存语义分（后端可选属性 `block_prefix_cache`：远端 vLLM 与本机 CUDA vLLM True，MLX 缺省 False）：块级前缀缓存改历史只重填
  分叉点之后（≈ 该步尾段）→ soft 起每个步骤边界都压；缓存不可裁（MLX）改历史 = 整段冷预填 → 只在 hard 起压，hard 那次本就退全量、之后每个边界再各付一次冷填（`full_renders` 记账）。
- **落刀点两处**：步骤边界 = complete_step 成功落地时，范围 [轮起点, 本批 tool 消息)——模型尚未看过的同批观察归下一边界（新锚 `session.turn_step_start_idx` = 本批下标，新用户轮取
  turn_history_start_idx、每次边界前移、续跑沿用；证据门挡下/失败的 complete_step 不算，step_failed 也不算——模型可能要回看观察重试）；hard 档首次置位处对 [轮起点, 锚) 压一次
  （进行中步骤不碰），压后重量、仍 ≥ hard 才发尾注。元工具 get_tool_schema / cloud_consult 响应与结构提醒不压。
  ceiling 仍是 W1 应急压缩（整个本轮段）。范围只对 [turn_start, end) 落刀，最近 keep_recent 条观察按全 history 倒数保护。
- **落盘保全文**：复用 W1 副本 + 原件表路线（抽共用 helper），history.jsonl 与暂存轮一律原件；桩只在模型视图。
- **回读有路**：被压观察 > digest_chars 且未截断过的先补写全文 spill（复用 spill_observation：0600、预算、撞名），桩带 `file_extract(path="spill:<stem>")` 短句柄；已截断过的沿用原中缝句柄；
  写失败只告警、桩无句柄。3,000–4,000 字的全文读回带行号会再截一次，经中缝可续读。
- 账：事件 `step_compaction{trigger: step|hard, count, freed_chars, level}`；轮统计 `step_compactions` / `compaction_freed_chars` / `full_renders`（全量渲染次数，engine 计）。
- 不碰：轮首压缩（observation_keep_recent 6）、审批/挂起/定时续跑的 compact_history=False 跳过、轮间摘要（输入读落盘日志且不读 tool 轮，桩进不去）。
- 门：reviewer 一审 + 主会话二审逐行（范围与保护窗、副本路、两处钩子、spill 补写）+ verifier 变异证明；提交 `6a092c8`。
- 残留：ceiling 兜底仍是机械压缩（步骤 result 已在 TaskState 作步骤摘要，轮内摘要折叠需子生成，不做）；轮内压小后轮间摘要触发看末次 prompt_tokens 可能推迟；
  P2 验收「拐点以上题集成功率 / Pass^2 达标、全量重填有账」等 Colab 复测——但现 knee harness 不经编排层，W2 两处钩子与 full_renders 轮统计都不在其路径上，须先做 W-K（P1 复审 M1）；
  `remote/backend.py` 的 `block_prefix_cache` 写死 True：若算力端是 MLX server（D26 下不可达）会被当块级缓存、soft 起每步冷填，且与 W3 按注册表条目判缓存语义是两套判据——恢复 MLX 算力端时按 /health `inference_options is None` 判别（P1 复审 m6，记残留不改）。
- **11-02 Colab 实机**（`benchmarks/data/context-knee-20261102/ANALYSIS.md` §1.6）：w1w2 的 complete_step 17/27/26/37 次、≥1 次的 run 5/9/9/14（每档 20）、step_compaction 0/12/11/17 次——钩子触发了但不到半数 run 用上；w1w2 − w1 成功率 −5 点 [−16,+5]，持平。


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
- 门：reviewer 一审 + 主会话二审逐行（投影规则、流校验、commit 用 view）+ verifier 变异证明；提交 `2b12243`。
- 残留：K=2 是初值，验收「思考回放不降成功率且峰值 prompt 下降」要等 Colab 三档复测（`--prefix` 一并）；本机 MLX 轮内不省思考（设计取舍，P-last）；
  tool_turns 下仅 content 的生成思考会被丢（官方同形）：verify_nudge / premature_finish 推回后同轮再生成时 ps.seen 身份校验失败退全量，MLX 上即一次整段冷预填；
  注册表外模型（本地路径，options 为 {}）无处配键、走模块缺省 last_k，本机 MLX 跑表外模型每步冷填（主会话裁定不按后端形态分缺省，表外是实验路）；
  backend/scripts 的 bench_a8_multiturn / soak_longrun 镜像装配不接投影，是 all 口径（头注已注明）。
- **11-02 Colab 实机**（`benchmarks/data/context-knee-20261102/ANALYSIS.md` §1.2）：last_k K=2 对 10-31 的 all 成功率 −4 点 [−16,+5]，不降 ✅；峰值降幅在 hard 题（每 run 4–5 次生成）上量不出，推断预期只省约 55 tok（<0.5%）。收益要在长轮（≥10 次生成）或 Gemma 卡禁回放上才看得见（§3 待拍板 3）。


### W-K knee harness 编排器模式（P1 阶段复审 M1 带出；用户 11-01「先做 W-K」）【已落地 11-01；D38】
- 事实：`benchmarks/context_knee/run_knee.py` → `model_eval/executor.py` 直调 `engine.generate_once`，无 Orchestrator；W1 的 `_context_guard_pre`/钳制/终止、W2 的两处钩子、轮统计都不在路径上（knee README「harness 不经编排层」一句已声明）。
  W3 投影在引擎层，可测；W1 的 context_limit 发现与 full_renders 计数在引擎层生效但 harness 未记——P1 复审修补已补记 measure/prompt_tokens/context_limit/turn_full_renders（最小补丁）。
- scout 校正（11-01）：ceiling 应急压缩压**整段** history（轮起点之前就地改、之内走副本），只有 W2 两处钩子受轮起点约束；step 钩子读 guard 写的档位——guard 关则 W2 永不触发；
  轮首机械压缩（预算 20,000 字、保护 6 条观察）在 compact_history=True 时不分新轮/续跑都跑，会压掉 `response` 键的填充；顶档 57,344 > ceiling 53,248，首次量测即进 ceiling 路（这是要量的事实，不是故障）。
- 设计 D38（见 §6）：`--orchestrator` + `--fill-placement history|turn`（缺省 turn）+ 对照开关 `--guard/--step-compaction/--turn-compaction/--goal-verify`（goal_verify 缺省关，省一次生成）；
  工具 = 真注册表指空目录 + env 工具（read 类）+ 生产声明桩（Unknown tool JSON，与直调模式回执同口径），披露交给条目 `tool_schema_mode`；每 run 新建 Orchestrator，直调 `run_turn`（不走 TurnRunner，harness 自记录）；
  traj 从 `history[锚点:]` 映射（response→content、元工具另记 `meta_calls`、`dup_blocked`/`system_errors` 另计），`aborted_reason` ceiling→context_guard；记录新键 `turn_status/turn_metrics/guard_events/session_dir`；
  summarize/analyze 加 step_compactions / full_renders 中位与 ceiling_aborts 列；resume 口径含 mode 与开关。
- 落地要点（implementer，主会话接受的偏离）：工具按读写分类（list_files/read_file/todo_list 为 read 可并发，其余 write 串行——全标只读会让零变更守门误推、写屏障不解禁复读）；
  aborted_reason 按本 run 的 turn_abort 事件定；`--prefix` 编排器模式只支持 mock|prod（catalog/routed 的披露形态在编排器里由条目 tool_schema_mode 决定，无对应档）；
  turn 放置的填充改「轮内形态」`fill_shape=in_turn`（只留 assistant 调用 + tool 观察配对、末条 tool，避免真模型去续伪对话），last_k 下逐对代价去思考再量（修前偏低 11–13%）、整块再估一次补对；
  新增 SpillReadTool（file_extract 名，只读本会话 spill 句柄，其余路径回 Unknown tool）让 W2 折叠桩的回读句柄在编排器模式可用；goal_verify 审计生成记 audit_gens 不劫持主会话；
  事件流与审批策略在 make_engine 前装、amain finally 还原；续跑口径含 mode / 放置 / 形态 / 四开关 / 生效阈值 / step_compaction 参数 / agent 段指纹 / 披露模式。
- 对照组（README「编排器模式」节）：各臂固定同一放置只切开关——turn：无管理（guard off, step off）→ +W1 → +W1+W2；history 单列「现状参照」；turns 与 in_turn 配对数差 10–20%，不跨放置比。
- 门：reviewer 一审 0 Critical / 3 Major / 11 Minor / 5 Nit（Major：审计生成换代理 session；spill 回读句柄调不通；对照组混放置与 W2）全修或写进 README；主会话二审逐行 orch_tools / OrchKneeEngine / 注入与锚点 / 全局还原；
  verifier 变异证明见提交说明。backend/framework 零改动。
- 残留：turn 放置下首次 hard 不压填充（turn_step_start_idx=None、hard 钩子要 s0>t0），W2 只剩 complete_step 一路；mock 档系统提示不教 update_plan/complete_step，+W2 臂有效性看 meta_calls（建议 --prefix prod）；
  元工具/推回与环境调用同计 max_total_iterations，预算比直调紧；System Error / Duplicate blocked 不计 env_errors（另列）；last_k K 很大时会多铺约 K 对思考代价；
  turn 放置走续跑入口跳过轮首云端升级判定（history + turn-compaction off 同样跳过）；in_turn 形态的 token 定档只在 FakeEngine 计数下核过，Colab 真分词器下的残差随复测看。
- 替代：`backend/benchmarks/attention/driver.py` 经 HTTP 打 /turns 走编排层，但要起栈、题短难到 soft，不如 W-K 可控。
- **11-02 Colab 实机带出**（`benchmarks/data/context-knee-20261102/ANALYSIS.md` §1.4、§2）：prod 前缀下 harness 桩工具吃掉约两成调用（直调 prod 臂 162/771 次来自 mock_envs；编排器四臂 181/947、174/939、228/1000、167/944 次 `Unknown tool` 来自 orch_tools StubTool——scout 11-02 纠偏：StubTool 回 JSON error 不是 System Error、不计失败，真正吃回合的是其后 rag_search 桩触发的检索未完成推回链：complete_step / write_file 被挡、REPAIR 推回各占一次生成），编排器路径 L0 基线 35%、4–5/20 run 回合耗尽，P2 规则空判（D39）；三臂 L0 处理相同却差 15 点，是本批噪声量级。口径修补（同日提交）：`peak_prompt_tokens` 只取实发、编排器量到的峰值另记 `peak_prompt_measured`、`fill.prompt_tokens_measured` 记首生成前量法值、`analyze.py` 基线 ≤ drop 报「基线在地板，规则空判」。复算脚本 `analyze_orch.py`（A–F 复用 analyze.py，G–N 新增；reviewer 一审 0C/0M/10m 全修）。
  链脚本 `run_colab.sh` 一审 1C/4M/7m 全修：臂后台跑、每 60 s 查末 3 run 连环 timeout 即 TERM harness（最坏 31 min 发现，原先整臂跑完才判、最坏 14.7 h 计费）、重起用完即停机跳余臂、`start_and_wait; rc=$?`（`if !` 后 `$?` 恒 0 的 bug）、全跳过记 rc 1、信号先杀子进程再写 INTERRUPTED、冒烟目录带时间戳、产物存在自动 --resume、ok 需 `complete` 且记 errors；模板 w3 脚本同修 `local rc`。二审 2M 再修（续跑期间旧 timeout 行再判挂死 → 只看启动后新行；stopped 先于停机成功置位 → 只在 stop rc 0 后置；DONE 只在停机后写；OUT 已有 DONE 拒起）。桩自测 `run_colab_selftest.sh` 14 场景 55 断言；verifier 变异体 9 红 1 桩下等价（stopped 提前置位）——V1「删 started=1」在中间版等价存活，暴露 fail_exit 未按 started 把关（起机前收信号会 stop 别人的 VM），补门 + 「起机前 TERM」场景后变红。
### W-K2 harness 可用桩（D39a；P2 复测前置）【新，11-02 拍板】
- 问题：knee harness 编排器模式下 prod 前缀声明生产全套工具，但 orch_tools.py 只给文件 / plan / complete_step 沙箱实现，其余回执 `Unknown tool` → System Error 并计入回合；11-02 实测 162/771 次调用（21%）白费，L0 基线 35%（`benchmarks/data/context-knee-20261102/ANALYSIS.md` §1.4、§2）。
- 落地纠偏（11-02 scout，`orch_tools.py:78/156-169`、`retrieval_evidence.py:81-111`、`orchestrator.py:1153/1248/1373/1427`）：162/771 是**直调** prod 臂（mock_envs 的 Unknown tool），与桩无关；编排器四臂的 Unknown tool 是 181/947、174/939、228/1000、167/944（18–23%），以 run_shell_command（62–89）、list_dir、edit_file、find_files、run_math、rag_search 为主——模型把 vfs 当真文件系统用生产工具操作。StubTool 回 `{"error": "Unknown tool"}` 不计失败也不直接吃回合；吃回合的是每次 `rag_search` 桩置下的检索未完成标记：complete_step / remember 被挡、write_file（DELIVERY_TOOLS）被挡（每臂 2–25 次 System Error「delivery blocked」）、纯文本回复先 REPAIR 推回再「检索未完成」收尾，每一步都多一次生成。沙箱实为 mock env 工具（vfs / todo / office）+ SpillReadTool + 元工具；编排器模式原只收 mock|prod 前缀。
- 做法按此改为四类桩：vfs 桥接（list_dir / find_files / edit_file / copy / move / trash / 非 spill 路径的 file_extract 走 env，run_shell_command 为 vfs 上的最小 shell 仿真 ls/cat/grep/find/head/tail/wc/echo/pwd）、检索（rag_search 回生产空结果原文形态使 retrieval_evidence 判 empty，rag_get_context 同）、确定性计算 / 状态（run_math 真算、get_current_time 固定值、*_list / *_status 空集）、离线不可用（网络 / 外设 / 副作用类回正常结果说明不可用，不伪装成功）；行字段 `stub_served`（按名计数）/ `stub_unknown`；编排器模式加收 catalog 前缀（D39c 对照臂）。
- 做法：为 prod 披露集里每个无实现的工具接确定性「可用桩」——与真实工具同 schema、返回形态合理的结果（rag_search 返回空命中 + 说明、run_math 真算、网络类返回「离线不可用」的正常工具结果而非错误），不触发「检索未完成」拦截；桩调用另计 `stub_calls`，入 transcripts；README「编排器模式」节记桩语义与生产的差异。
- 落地（11-02 下午；implementer → reviewer 一审 4M/11m 全修 → 主会话二审逐行 orch_tools / run_knee 接线、抽读 orch_stubs 检索桩与 run_math → verifier 变异体两轮 8/8 + 10/10 红）：新文件 `benchmarks/context_knee/orch_stubs.py`（UsableStub 四类桩；VfsBridge 读走 env.snapshot 不耗故障配额、写删走 env.handle 与 EnvTool 同锁；shell 仿真 ls/cat/grep(BRE→Python)/find/head/tail/wc/echo/pwd，长选项与 -A/-B/-C 回 exit 2；run_math ast 白名单 + 指数/位长/列表长上限；离线类桩 is_read_only=True 免得「什么都没做」被当成功变更关掉 D2 推回；写类失败与缺必填参数回 `System Error:` 由桩自记 `stub_failed`、不计 system_errors，读类失败 `Error:` 不计失败），`orch_tools.py`（StubTool 删除；SpillReadTool 非 spill 路径桥到 vfs；`keep_mock_tools_full` 把题目 mock 工具并入本 run core 使 catalog 档 mock 工具保持完整 schema，与直调 catalog 档同口径；`traj_from_history` 加 stub_served / stub_unknown（绊线，应恒 0）/ stub_failed），`run_knee.py`（ORCH_PREFIX_MODES 加 catalog、`orch_model_options` 把条目层 tool_schema_mode 强制 catalog；meta 新键 `stub_impl: usable-v1` 恒比，W-K2 前的目录不得续跑混批；timeout / 异常行同记桩字段；production_traits 批次开头预热）、`summary_trace/run_trace.py` 同步（--prefix catalog 同义、registry 计数）。
  gemma4 两条目条目层本就是 tool_schema_mode: catalog，其编排器 prod 臂即 catalog 披露；Qwen 条目 full，prod / catalog 两臂才是 full 对 catalog。
- 验收：FakeEngine 干跑 prod 前缀下 Unknown tool = 0；mock / prod / catalog 三档 L0 直调成功率不变（桩不影响直调）；Colab 第二批无管理臂 L0 成功率回到直调 mock 的量级（≥ 70%）才算基线抬离地板，否则再查元工具计熔断（D39b）。
- 守卫型部分（拦截不再触发）走变异证明；实现 implementer，一审 reviewer，二审主会话逐行 orch_tools。
- **11-02 晚第二批结果**（`benchmarks/data/context-knee-20261102-b2/ANALYSIS.md` §1.1）：四臂 `stub_unknown` = 0、Unknown tool 回执 0、delivery blocked 0、L0 迭代耗尽 1/20（上午 5/20）；无管理臂 L0 35% → 60%（配对 +25 [+0,+50]），验收「≥ 70%」**未达**——剩余失败 2/8 是 lc-hard-009：生产缺陷 qwen35 `_coerce` 把 JSON 字符串参数转成 dict、`file_edit` 落盘成 repr（Gemma 18/18 过；D46），6/8 步数超限中只计非桩调用救回 2、重标 max_steps 救回 3（可比基线 60–75%）。桩目标达成；评分口径与 `_coerce` 修后再定（ANALYSIS §1.1 / §2）。

### W1b 当前轮超限溢出外置（D40）【新，11-02 拍板】
- 问题：W1 ceiling 路径把当前 user 轮整体折进摘要，细节静默丢失（57K 回忆 91% → 0%，模型随后主动找检索）。生产对应用户一条消息贴了超限材料。
- 做法：护栏在首次生成前判当前 user 轮单独超限（或折历史后仍超 ceiling）时，把超出部分按 spill 机制落盘为会话内文件，prompt 保留头部原文 + 结构化摘要 + 回读句柄（SpillReadTool 已有），guard_events 记 `context_spill_turn`；外置后仍装不下（头部 + 摘要 + 工具声明 > ceiling）才 context_guard 中止并以用户可读的话说明「这条消息过长，请拆分」。历史轮折叠逻辑不变。
- 验收：FakeEngine 用例 soft→hard→ceiling→spill→refuse 五段全绿；变异证明（去掉 spill 分支 → 回退为整轮折叠的用例变红；去掉兜底拒绝 → 超限实发用例变红）；Colab 第二批 57K 管理臂回忆相对 11-02 的 0% 显著回升、无超限实发；MLX 路径（本机）不受影响（spill 文件落执行端）。
- 落地纠偏（11-02 scout，`orchestrator.py:548-550/576/989-995`、`spill.py:96`、`run_knee.py:1222`）：**问题栏的前提有误**——ceiling 路径从不碰 user 轮，也不折摘要：它一次性把轮内工具观察压成摘要桩（keep_recent=2），桩不带回读句柄（`_emergency_compact` 未传 `stub_handle`），且填充观察 300–3000 字符从未在入口 spill 过，所以 57K 档的事实卡随桩消失；11-02 的 57K 填充是轮内 assistant/tool 对，user 消息本身不大。生产没有 `SpillReadTool`（那是 harness 对 `file_extract(path="spill:…")` 的替身），回读路径是 `file_extract`。
  因此 W1b 落地为：ceiling 压缩复用 W2 的 spill 回调（抽成 `_spill_stub_handle` 共用，不受 `step_compaction.spill_full` 门控，`session_dir` 在即落盘），桩带 `file_extract(path="spill:<hash>")` 句柄，原件按内容哈希命名（跨轮重压不重复落盘），spill 失败即退回无句柄桩（fail-open，只告警一次）；`context_guard_fold` 事件加 `compacted` / `spilled`，中止事件同带两键；现有「上下文已达模型上限…把任务拆小分别发起」中止文案即是 D40 的拒绝兜底，不改。
  **D40 所述「当前 user 轮本身超限 → 超出部分外置」没有实测场景支撑**（护栏对 user 消息无动作，超限只会在压缩无效后中止），**改列 D40b 待拍板**：是否要为「用户一条消息贴超限材料」做外置（涉及改写 user 消息入 history.jsonl、persist 原件换回扩到 user 消息、measure_prompt 无法分部计量），不绑第二批。
- 落地（11-02 下午；implementer → reviewer 一审 0C/0M/5m（跨轮重落盘、告警一次、中止事件计数三条已修）→ 主会话二审逐行 → verifier 变异体 6/6 红：去回调 / 落盘截断 / 不计 spilled / 去哈希复用 / 按 spill_full 门控 / 去告警一次）：`orchestrator.py` `_spill_stub_handle(session, *, digest_chars, label)` 共用回调（W2 步骤边界保留 spill_full 门，ceiling 不门控）、`_emergency_compact` 返回 (compacted, spilled)、`context_guard_fold` 事件加 compacted / spilled、`turn_abort`（context_guard）同带两键；`test_context_guard.py` +7 用例。
  一审残留（记录，不绑第二批）：spill 在事件循环内同步写（入口路径用 to_thread；ceiling 一次 N 条 × 目录 stat）；句柄不带 max_chars，接近 observation_max_chars 的观察经生产 file_extract 回读会再截断再落盘一次（spill.py 已有告示的级联，harness SpillReadTool 原文返回看不到）；未校验 file_extract 是否在本会话披露集内（W2 同有）。
- 验收（随纠偏改）：FakeEngine 用例：ceiling 压缩后被折桩全部带句柄且句柄解析回原文、短观察不落盘、无 session_dir 行为不变、spill 抛错退回无句柄桩且只告警一次、同内容两次压缩只落一份；变异证明 M1 去 stub_handle / M2 落盘截断 / M3 不计 spilled / M5 去哈希复用 各有用例变红；Colab 第二批 57K 管理臂回忆相对 11-02 的 0% 显著回升、无超限实发；MLX 路径不受影响（spill 落 `session.session_dir`，即执行端）。
- 顺序：先 W-K2（复测前置），W1b 随后；两项都在第二批起机**之前**落地，否则 57K 档的管理臂回忆仍是 0。


- **11-02 晚第二批结果**（`benchmarks/data/context-knee-20261102-b2/ANALYSIS.md` §1.4）：57K 档管理三臂 20/20 run 首次生成前 fold，桩全带句柄（Σspilled 1,027 / 1,027 / 1,209），回读 43 / 107 / 75 次（各 6 run），超 ceiling 实发 0、超 context_limit 0；57K 回忆 w1 0%（0/11）、w1w2 20%（1/5）、catalog 0%（0/5）——验收「> 0」仅 w1w2 形式上过，「显著回升」**未达**：句柄在、模型不知道读哪个（摘要桩只有 digest 头）。下一步 D43。
- **11-04 D43 (a) 落地**（b3f2609）：驱逐桩在方括号内带 `; mentions: …`（ID / 路径 / 全段单例 key = value，R2，8 词 / 240 字），模型按内容挑句柄；harness 表 B 新增 `recall_src 桩/回读/原文` 与 `回读调用(run)`——线索可把卡值直接带上桩，57K recall 升高从此不再证明检索，验收看回读列与 aborted。详见 §6 D43 行；D43b（W2 再压回读内容）待拍板。

### W4 结构化摘要模板与校准【已落地 11-02；D4 定 new + 900（11-02 晚 trace 实测）；K 值仍待多轮回放】
- 落地（11-02）：`SUMMARY_INSTRUCTION` 九节固定模板（[Goal] [Constraints] [Done] [State] [Key values] [Decisions] [Issues] [Next] [User]，节名逐字、每节硬上限、空节省略、正文用对话语言、[Key values] 逐字不改写），旧指令留档 `SUMMARY_INSTRUCTION_LEGACY`；
  `summarize(mode="merge"|"rewrite", instruction=)`；摘要块头加「do not redo items listed under [Done]」；截断感知（输出估算 ≥ max_tokens×0.95 → 告警 + 事件 `summary_truncated_suspect`，成功判定不变）。
- 落地偏差 1（scout 纠前提）：代码里没有「偏好与身份」节，只有一句 "Keep: the user's long-term preferences and identity"，W4 以 [User] 节承接；「压缩后首步提醒」orchestrator 的 name=system 机制是步骤边界提醒而非压缩后提醒，W4 的前缀改放在摘要块头，W5 提醒本身未做。
- 落地偏差 2（TaskState 来源）：轮间摘要在轮次边界触发，任务完成即弃（orchestrator / turn_runner 置 None），摘要器原本只收 User/Assistant/System 文本与工具名——拿不到 plan/notes。补：`TaskState.render_for_summary()`（[Goal] 首行 → [Scratchpad] 便签原文 → [Plan] 步状态/标题/note，无系统头/Budget/Rules），
  轮次记录新键 `task_state`（PROTOCOL §2.3 additive；`append_log` 唯一漏斗盖章，任务完成即弃的由编排器置 None 前留 `last_task_state_render`；fail/cancelled 不写），`_turn_to_text` 追加 `[Task state at end of turn]` 块（≤3000 字先截 Plan；rag 沉淀同走）。「从 TaskState + 尾部原文重写」= rewrite 模式输入 = 旧摘要 + 新段（含各轮 task_state 块），输入有界。
- 校准：config.yaml `summary_max_tokens: 900`（一审 Major：640 对九节 + MEMO 块偏紧，截断先丢 [Next]/[User]/MEMO）、`summary_rewrite_every: 4`（0 = 永不）；装配期校验 fail-closed；`session.summary_folds` 随 memory.json 落盘（老存档 0）；第 K、2K… 次成功折叠走 rewrite，首次无旧摘要恒 merge。
- 评估驾驭器 `benchmarks/summary_trace/`（§4 TRACE 式）：turn 1 跑到迭代上限落盘 → 同一 env 深拷贝分 PRE（原文）/ POST（整轮折叠为摘要）续跑 ≤5 步，计 repeated（重复 turn 1 已做的 (name,args)，被拦截的不计）+ blocked、env_errors、pass、首次 prompt token、摘要长度；`--template legacy|new`（legacy 连旧摘要块头）、`--task-state on|off`、`--drop-task on|off`（模拟任务完成后被弃、只能靠摘要的目标场景）。`--dry-run` FakeEngine 跑通；真模型在 Colab 另排（D4 模板 / 900 vs 640 的结论等它）。
- 残留：D5 的 K 值用单次折叠的成对续跑量不出来（驾驭器钉 rewrite_every=0），要多轮长会话的离线回放才能比 K；驾驭器两支都保留未完成 TaskState 时量的是「摘要 + 任务块」，看 W4 本身的效果用 `--drop-task on`；
  `task_state` 键进 history.jsonl / turn_ended 全量负载（便签满载约 3K 字，`/chat` 的 ChatResponse 不含）；截断感知只估不改判；legacy 臂 MEMO 块若被模型写在节中间，锚点后非 `-` 行会被 `_split_memo` 丢弃（与 W4 前同）。
- 原设计（保留）：
- `SUMMARY_INSTRUCTION` 改为固定节：`[压缩摘要，仅供参考；不要重做「已完成」]` 前缀 → 总目标（原文照抄）→ 约束与偏好 → 已完成（过去时事实）→ 当前状态（目录 / 已改文件 / 进程）
  → 关键值表（路径 / ID / 数字，逐字）→ 决策 → 已知问题 → 下一步 → 用户长期事实（现有「偏好与身份」保留）。每节硬上限，总长仍 ≤ 640 tok（可上调到 900【测】）。
- MEMO 抢救不变。时间锚定（绝对日期）不变。
- 校准：每 K 次增量折叠（K 初值 4【测】）做一次「从 TaskState + 尾部原文重写」而非从归档全量重写（输入有界，小模型可做）。
- 模板评估走 TRACE 式成对续跑（§4），不靠整任务成功率。
- **11-02 晚 trace 实测**（`benchmarks/data/context-knee-20261102-b2/ANALYSIS.md` §1.7、T 节）：POST pass new-900 10/30、new-640 5/30、legacy-640 5/17（另 13 对 legacy 指令下 Qwen 空摘要，判模型产物）→ 模板定 **new + 900**。但三步 POST 都比 PRE 差（pass 降 6–14 对、重复调用 +2.7…+3.4/对）：task_state 在 0/90 对起作用（turn 1 从未建 plan），摘要器输入无工具结果、无助手正文（`_turn_to_text` 设计）。根因在摘要输入，见 D45。
- **11-03 D45 验收**（`benchmarks/data/context-knee-20261103-d45/ANALYSIS.md` §1；三步各 30 对、0 error、空摘要 0）：工具段 (a) 生效——批内只差工具段的 both − plan：Δ重复功 −1.43 [−2.50, −0.57]，POST pass +16.7 pt [0, +36.7]（点估计为正、区间下沿触 0）；跨批 digest 对 b2 new-900：POST pass 16/30 对 10/30，Δ重复功 +1.50 [+0.50, +2.77] 对 +2.93 [+1.97, +4.00]（跨批同时带 D46 修复与模板新句，turn 1 只有 18/30 逐字相同，只作参照；跨截断点的 pass 与重复功 Δ 都不可比，交互不可估——缺批内 tool0/t3 基线臂）。摘要续跑仍劣于原文续跑（digest PRE 22 → POST 16），差距缩小、未消除。(b) 未激活：turn-1 迭代 6 下 `turn1_plan_built` 0/90、`update_plan` 0/90，模型直接做完 turn 1（success 18/30）不建 plan；task_state 单因素不可判 → D47。预算与注入：工具段最长 1,423 字、输入最长 2,491 字，2,400 / 24k 两道预算与 `spill:` 短句柄路径真数据未走到；lc-hard-009 PRE 3/3（b2 0/3，D46 生效）。
- **11-03 D47 验收**（`benchmarks/data/context-knee-20261103-d47/ANALYSIS.md` §1；一步 30 对、0 error、空摘要 0；对照 d45-both，参数逐键相同、只换代码）：机制通了——`turn1_record_has_task_state` 30/30（d45 0/30），摘要器输入 `[Task state at end of turn]` 30/30，但段内只有 `[Goal]`（Plan 0、Scratchpad 0，`turn1_plan_built` 仍 0/30）；便签没被用：`remember` 0/30（工具每轮都在声明里、no_plan_nudge 在 turn 1 触发 30/30，是模型不用），便签逐字率分母为 0。可见的间接效应：POST 摘要 [Goal] 逐字抄原文 22/30（d45 0/30）。效果方向不利且不可判：POST pass 10/30 对 13/30（PRE 都 17/30），d47 − d45 POST −10.0 pt [−20.0, 0]、Δ重复功之差 +0.77 [+0.10, +1.43]；turn 1 调用序列相同的 18 对（完整响应逐字相同仅 2/30；同题同 rep 配对无 seed、只是标签对齐）：−4.8 pt [−14.3, 0] / +0.69 [−0.14, +1.57]——混淆含 [Goal] 段每对多出、REMEMBER 描述（也改变 turn 1 的工具声明，且去掉了 d45 的「create a plan first」末句）与摘要指令句、turn 1 抽样、续跑轮首也种任务、d45 对照批代码身份只按其 ANALYSIS 记载（meta 无 sha），不下结论。预算未触及（输入最长 2,747 字；task_state 3000 截断 0 次；lc-hard-004 goal 被 300 字上限截 3/3）。另见摘要把助手日语漂移写成用户偏好：[User] 1/30、[Decisions] 1/30（模板缺口）。**待拍板 D47-next**：见 §6。
- **11-04 D47-next (b)+(a) 验收**（`benchmarks/data/context-knee-20261104-d47b/ANALYSIS.md`；代码 55cff85；一步 `trace-d47b-both` 30 对、0 error、空摘要 0；对照 d47-both 与 d45-both，harness 参数逐键相同）：**判据未达标**——turn 1 `remember` 0/30（d47 0/30），便签 0/30，「便签值进 [Key values]」分母 0 不可算。(a) 的提醒句 30/30 进了 history，但提醒后第一步分布（作答 15 / write_file 12 / delete_file 3 / remember 0）与 d47、d45 两批逐格相同，看不到效应；全批唯一一次 remember 出在 lc-hard-004 r1 的 POST 续跑轮（摘要已生成、该轮随后 turn_abort，便签内容无落盘产物可核）。(b) 按设计生效：摘要器输入 `[Task state at end of turn]` 0/30（d47 30/30），输入中位 1,846 字（statistics.median；d47 2,126.5 / d45 1,777.5），POST [Goal] 逐字抄原文 1/30（d47 22/30 / d45 0/30）。pass 与 d45 无可分辨差别：POST 13/30 对 13/30（PRE 19/30 对 17/30），差 +0.0 pt [−13.3, +13.3]，Δ重复功之差 −0.10 [−1.03, +0.83]；相对 d47 的 +10.0 pt [+0.0, +23.3] / −0.87 [−1.70, −0.07] 是 d47 − d45 的镜像，在调用序列相同的 14 对上归零或跨 0，不归因。混淆：(a)(b) 同步落地不可分；描述与提醒改动触及 turn 1 的声明/history；rep 无 seed（完整 turn 1 响应逐字相同 0/30）；对照批代码身份只按其 ANALYSIS 记载。reviewer 独立复算全部对上，并补两点限制：0/30 与每轮调用率 ≤ 约 10 % 相容（三分律），且 d45 的 0 是在旧描述、无 plan 即拒的条件下从 history 数出来的，三批的零条件不同；「提醒后第一步」几乎无检出力——三批 28/30 对提醒后那一步逐对相同、提醒位置 30/30 相同、基本只由题决定（8/10 题三 rep 一致），提醒后的思考里 plan / save / remember 出现 0/30（典型是「The task is complete. I just need to present the final answer.」），15/30 对提醒触发时已无东西可存：限制在提醒的位置，不只是文案。模型 turn 1 文本与思考里 remember / scratch / 便签 三批均 0 次。三批 90 次 turn 1 里模型合作写便签为 0，D47c 的前提不成立。**待拍板 D47 收口**：见 §6。

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
| P1 ✅ 代码 / ✅ Colab 出口 11-02 | **W-3 阵容收束** → **W-1 远端配置修复（MTP 默认关 + 采样可配）** → W-2 前缀瘦身 → W1 上限护栏 → W3 思考回放（含变异证明、差分测试更新）→ W2 提前并入；11-01 阶段复审 1C/1M/7m 已处置 | 注册表 4 条 ✅、check 全过 ✅、31B 图文联调过 ✅；护栏用例全红 → 全绿 ✅；**待 Colab 复测**：MTP 关后同一 harness 复测 4 档回到 W0 水平；思考回放不降成功率且峰值 prompt 下降（W3 在 harness 路径上，可测）；**11-02 Colab 复测**：direct-mock 对 W0 成功率 −4 点 [−16,+5]、Pass^2 −5 点 [−22,+10]，回忆 100%，回到 W0 水平 ✅；W3 不降成功率 ✅、峰值降幅在 hard 题上量不出（预期约 55 tok）；W-2 catalog / routed 比 prod 省 61% / 53% 前缀、成功率持平 ✅（`benchmarks/data/context-knee-20261102/ANALYSIS.md` §1.1–1.3） |
| P2 ⏳ 门 ✅ 11-08（none L0 73.3% 点估过线，用户拍板）；W1/W2 评价 ✅ 11-09 判「不优于」（成功率比 0.90 / Pass^2 比 0.75 点估不满足、峰值按设计不可过线、耗时 −7% 满足），出口未达 → 回到 W1 落刀设计；D42 续项（§2 第二条口径）待拍板 | W-K harness 编排器模式 ✅ 11-01（前置）+ W5 小修（W2 已在 P1 落地） | 拐点以上题集成功率 / Pass^2 达 §2 目标；全量重预填充次数有账——两条都只能经 W-K 的编排器路径量到。**11-02 Colab 复测**：全量重填有账 ✅（每 run `full_renders` + 逐轮 `turn_metrics`）；成功率 / Pass^2 **不可判**——编排器路径 L0 基线 35%（Pass^2 20%）在地板（harness 桩工具失败调用 + 元工具 + 推回同计回合熔断，L0 就有 4–5/20 run 耗尽），拐点规则空判；57K 读作拐点以上则 +W1 实发峰值 0.56×level、Σwall +3% 达标，成功率比 0.57、Pass^2 0/11、回忆 91%→0% 不达标，+W1+W2 更差（0.19、+23%）。须先抬基线再复测（D39 待拍板；`benchmarks/data/context-knee-20261102/ANALYSIS.md` §1.4、§3）。**11-02 晚第二批**（`benchmarks/data/context-knee-20261102-b2/ANALYSIS.md` §1.2–§1.3）：基线 60% 未过门；四臂拐点一致 32K；按 §2 预注册三项：峰值 ≤ 拐点 × 0.7 = 22.9K → **✗**（≥32K 档 0/60，W1 ceiling 固定 53,248）；成功率 / Pass^2 ≥ 无管理 90% → **不可判**（高段 −5 [−18,+8] / −7 [−20,+7]，点估计比 0.77 / 0.69，区间不支持非劣效）；Σwall ≤ 25% → ✅（−4…+15%）。阶段复审 M1 纠正了初稿的「成本 ✅ / 不劣化 ✅」；下一步见 D42 |
| P2 复测「第二批」（11-02 拍板，不省算力） | 前置 W-K2 + W1b 落地 → 一次租约打包：编排器三臂（无管理 / +W1 / +W1+W2，prod，0/32K/49K/57K）+ 编排器 catalog 对照臂 + Gemma 4 31B 直调两臂（mock + catalog）+ summary_trace 真模型（新 / 旧模板，900 vs 640）；链脚本 run_colab.sh 加臂 + Gemma 模型切换 + trace 步 | 编排器无管理臂 L0 ≥ 70%（基线抬离地板）→ 按 §2 判 P2 三项；57K 管理臂回忆 > 0 且无超限实发（W1b）；Gemma P1 出口同口径；D4 模板 / 900 定。预估约 8–9 h / 约 100 单位，起机一次。**11-02 晚跑完**：两次租约（非订阅 VM 12 h 上限，trace 首步起步时按预案切断）9.2 h / 约 49 单位，九步全 ok；结果 `benchmarks/data/context-knee-20261102-b2/ANALYSIS.md` §1；Gemma P1 出口按数字达成（§1.6）；D4 定 new + 900；新决策 D42–D45 |
| P3 ✅代码 11-02 / 评估 11-02 晚**不可判** | W4 结构化摘要 + 校准 + 成对续跑评估驾驭器 | 模板在成对续跑上优于现模板：真模型 trace（`benchmarks/data/context-knee-20261102-b2/ANALYSIS.md` T 节）new-900 对 legacy-640 在 legacy 成功的 17 对子集上 POST pass 7 对 5、配对 2 升 0 降、p=0.5——方向对、样本分不开；legacy 另 13/30 空摘要（Qwen 退役），所以模板按 D4 定 new + 900 但「优于」未判出。K 值定（需多轮回放，驾驭器量不到） |
| D45 验收（11-03） | 三步 trace-d45-digest / both / plan，对照 b2 new-900 | **工具段成立**：重复功 −1.43 [−2.50, −0.57]（批内单因素）；task_state 侧**不可判**（0/90 建 plan）；约 1.4 h 租约 | ANALYSIS d45 §1 |
| D47 验收（11-03） | 一步 trace-d47-both，对照 d45-both | **机制成立、效应未现**：task_state 段 30/30 进摘要器输入但只有 [Goal]；remember 0/30；POST pass −10 pt [−20, 0]（跨批、混淆多，不可判）；约 0.65 h 租约 | ANALYSIS d47 §1 |
| D47-next (b)+(a) 验收（11-04） | 一步 trace-d47b-both，对照 d47-both / d45-both | **判据未达标**：remember 仍 0/30；(b) 生效（task_state 段 0/30、POST [Goal] 逐字抄原文 1/30）；pass 与 d45 无差（POST 13/30 对 13/30）；约 0.6 h 租约 | ANALYSIS d47b §1 |
| D43 (a) / D43b / D44 验收（11-05） | 四编排臂 none / w1 / w1w2 / catalog，一次租约 6.1 h（MAX_VM_HOURS 首次生效未触发） | **D43 (a) ✅**：57K 回忆 0–20% → 80–92%（三臂合并 1/21 → 31/36），来源 26/31 全归桩（桩头 + 线索；61/80 个卡值只在重建线索里命中）；**D43b 机制成立、效应不可判**（免压 2 run）；**D44 预检生效**（catalog 11/11 恢复，prod 0）、catalog 对 w1w2 −1 [−14,+12]、首 prompt L0 −61%；无管理臂 L0 35%（b2 60%，−25 [−55,+5]，跨批含 D45–D47 改动与采样噪声）| ANALYSIS d44 §1 |
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
| D4 | 摘要模板 | **九节固定模板已进生产（11-02，W4）**，`summary_max_tokens` 900【测：vs 640】；旧模板留档作 A/B 对照 | 成对续跑（summary_trace，待 Colab） |
| D5 | 更新方式 | **增量 + 每 K=4 次 rewrite 已进生产（11-02，`summary_rewrite_every`）**；重写输入 = 旧摘要 + 新段（含轮末 TaskState 块）；K 值【测，需多轮回放】 | W4 |
| D6 | 压缩模型 | 主模型自己（现状）；不走外部 API（出网红线） | — |
| D7 | 截断长度 | 4000 字维持；W7 再议 | — |
| D8–D10 | 状态存储 / 写入 / 注入 | TaskState + 每迭代渲染（现状）；框架写为主 | 1.2 |
| D16 | Pass^k | k=2 | TRACE 可比 |
| D18 | 组装顺序 | 维持：不变在前、可变（task_block / tail_notes / 环境）在后 | 1.5 |
| D19（新） | 思考回放 | 跨轮丢弃 + 轮内 last_k=2【测，K 初值；落地见 D36】 | 1.3-3 |
| D36（新） | 思考回放实现 | 投影在引擎层（`core/thinking_replay.py`），view 参与 prompt_stream 指纹与 commit、加 ps.seen 身份校验，方言 builder 不变；条目级 `thinking_replay: all \| tool_turns \| last_k`（缺省 last_k、K=2，非法拒启）；本机 MLX 两条 tool_turns（缓存不可裁）、Colab 两条 last_k；纯思考消息本轮恒留/历史轮占位【定，主会话 11-01】 | W3 |
| D20（新） | 落刀位置 | 轮首 / 步骤边界 / 溢出兜底 | 1.5 |
| D37（新） | 步骤边界压缩触发与路线 | 按 W1 档位触发（块级前缀缓存后端 soft 起、缓存不可裁后端 hard 起，后端属性 `block_prefix_cache` 判别），不用「token×迭代 > 秒」公式；落刀点 = complete_step 落地后 + hard 首次（只压已完成段）；副本 + 原件表落盘保全文；被压观察补写全文 spill 带回读句柄；`agent.step_compaction` 非法拒启【定，主会话 11-01】 | W2 |
| D38（新） | 拐点 harness 编排器模式 | `--orchestrator`：每 run 新建 Orchestrator（真 MCPRegistry 指空目录；mock env 工具包成 read 类 BaseTool，`--prefix` 该档生产声明包成只回 `{"error":"Unknown tool"}` 的桩，绝不注册生产实现；审批进程内全放行；事件文件重定向结果目录）；填充放置成开关 `--fill-placement history|turn`（缺省 turn：插 user 之后、锚点置 2，借审批续跑语义 `run_turn(keep_recent_calls=True, compact_history=False)` 续跑，W1+W2 全在路径上；history = 现状臂，轮首压缩与 ceiling 整段压碰得到、W2 碰不到）；对照开关 `--guard/--step-compaction/--turn-compaction/--goal-verify`；traj 从 history 映射给 longchain 评分器，元工具不计 steps 另记；直调模式字节级不变、两种行不混 jsonl【定，主会话 11-01】 | W-K |
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
| D39（新） | P2 基线抬离地板后复测 | 11-02 复测编排器路径 L0 基线 35%，拐点规则空判。**定：a + c**——(a) harness 给 prod 声明的工具接「可用桩」（确定性合理结果；W-K2——落地纠偏：Unknown tool 本就不是 System Error，吃回合的是 rag_search 桩触发的检索未完成推回链，桩的关键是 rag_search 回生产空结果形态 + 文件类工具桥到 vfs）；(c) 编排器加一臂 catalog 前缀作对照（Qwen 生产形态仍是 full，c 不替代 a）。(b) 元工具 / 护栏推回不计 max_total_iterations 是生产编排器行为，**另开议题**，不绑进复测。复测三臂 × 4 档约 5.5 h / 约 65 单位，与 Gemma 两臂、summary_trace 同一租约（§5「第二批」） | 用户 11-02「按你的建议没有问题，不用省算力」 |
| D40（新） | W1 fold 对当前 user 轮内容的策略 | 现状：超限时整轮折进摘要，轮内事实卡丢光（57K 回忆 0%；history 放置的轮首压缩同样 62% → 13% → 0%）。**定：溢出外置为主、明确拒绝兜底**——当前 user 轮超限时把超出部分落盘（复用 spill / SpillReadTool），prompt 留摘要 + 句柄由模型按需回读；连外置都装不下才走 context_guard 中止并告知用户拆分。不采「保留尾部 N tok」（事实分布在全文时只保部分）。归 W1b。**11-02 落地纠偏**：实测丢的是轮内工具观察而非 user 轮（ceiling 路径不碰 user 消息），外置 + 句柄落在 ceiling 压缩桩上（W1b 落地纠偏条）；「user 轮本身超限外置」无实测场景，另列 D40b 待拍板。**D40b 用户 11-03 拍板：搁置——无场景不建机制，维持 context_guard 中止并告知用户拆分** | 用户 11-02 / 11-03 |
| D41（新） | Colab 批次链脚本纪律 | 臂在后台跑并由链脚本盯挂死（不等整臂）；挂死重起预算整批共用，用完即停机、余臂 skipped；全跳过不算全过；信号先杀 harness 再停机；改脚本必跑桩自测。11-04 补：首起 VM 随即 failed 的 FAIL_READY 自动重试一次（READY_RETRY，等 READY 超时不重试）；MAX_VM_HOURS=10 租约预算——每步开跑前「本租约已用 + 同类步本批最长干净用时（没跑过按 knee 2 h / trace 30 min）> 预算」则同模型 stop→start 换租约再跑，刚起的租约至少跑一步；两者都不占 RESTARTS_MAX（33c0cb0） | 主会话 11-02（一审 C1/M1/M3）；11-04 补两项 |
| D42（新） | P2 判法、编排器拐点成因、D1/D2 是否按编排器拐点回填 | 第二批按 §2 三项：成本 ✗、成功率不可判、Σwall ✅、门槛未过。候选：(a) 关闭 P2 复测，拐点成因（≥32K 迭代耗尽 6–14/20、+W2 更多）另立项；(b) 先修 D46 与评分口径再复测一次（约 7 h / 45 单位，L0 预计 70–80%，≥32K 预计仍在地板）。D1/D2：直调取 24,576 / 39,322，按编排器 32K 回填是 16,384 / 26,214（F 节），但悬崖来自迭代耗尽，压 ceiling 未必抬成功率。**建议 (a) + D1/D2 暂不回填**。**用户 11-03 拍板：(a)，D1/D2 暂不回填**；拐点成因（迭代耗尽）另立项，待 D46 修后再看。**11-09 续（w12eval 批）**：回填的反证——57K 档 fold 后 prompt 短 53% 但 Pass^2 26→13，fold 在 65K 窗内净负，按 32K 回填只会让 32K 档也进 fold 路径；§2 第二条（0.7×拐点 = 22,938）与 soft 24,576 结构性冲突，待拍板：改防溢出口径或保留。建议不回填 + 改口径 | 阶段复审 M1（反驳者确认），ANALYSIS §1.2–§1.3 |
| D43（新） | W1b 后 57K 回忆仍 0–20% | 候选：(a) 摘要桩带一行可检索线索（观察首 N 字 + 路径/ID 词）；(b) ceiling 前对工具观察建 rag_search 可达索引；(c) 接受现状（生产 ceiling 只压工具观察）。**建议 (a)**。**用户 11-03 拍板：(a)** **11-04 前置复算（b2，analyst；脚本随落地入 `benchmarks/context_knee/d43_pre/`）**：57K 档每 run 42–69 条被压观察、单条 204–3,054 字单行 JSON，生产 file_extract 一次读全（0/3,352 超 4000 上限），三管理臂各 6 run 回读 43/107/75 次、全部解析成功 0 截断、读到卡 7/12/8 次，但 18 个回读 run 里 14 个 aborted（其中迭代耗尽 11；读 12–54 个句柄），只有 w1w2 003 r1 用 12 次回读交出 3/3——瓶颈是「从约 50 个句柄里挑那 3 个」与迭代预算，不是可达性；harness 不必先改多行、file_extract 不必先加行区间/grep。线索规则 **R2**（先解码 JSON 转义；去自身路径与桩头 200 字已见词；去重；ID > 路径 > 本观察内且全历史单例的 key = value；8 词 / 240 字）在 w1 / catalog 各 10 run 真实填充上三卡覆盖 28/28、30/30，非空线索观察 319→149/504。**设计定稿（主会话）**：线索放在桩的方括号内 `_spill_hint` 之后、`]` 之前（`全文：` 句柄行仍最后，`$` 锚定测试不动），形如 `; mentions: OPS-48213, /opt/…/halyard-ab12cd.toml, queue_depth_alert_threshold = 4800`；取代桩不带线索；线索为空时桩逐字同今；harness analyze.py 加 `recall_source`（original / stub / readback）与 `readback_calls` 列——线索会把卡值原样写进桩，57K 回忆率上升不再证明检索有效，验收看来源归因与回读次数。**新决策点 D43b（待拍板）**：W2 步骤边界会把刚回读的 spill 内容再压成同一句柄的桩（keep_recent=2），回读等于白读；候选 (i) 步骤边界对 `file_extract(path="spill:…")` 的回读观察免压（ceiling 仍可压）；(ii) 不改，靠 remember/作答——但 D47 已证模型不写便签。建议 (i)，随 D43 (a) 一并落地或单列。**用户 11-04 拍板：(i) 免压，随 D44 做**（原话「(i) 免压，随 D44 做」）。**D43b 落地 3564404**：`compact_tool_observations_detailed(exempt_spill_readbacks=)` 只 W2 传 True，驱逐候选跳过 `_target` 为 `spill:` 的回读，取代阶段照旧（同句柄旧回读是重复副本，取代无损）；首次跳过打 `_rb_exempt`，> 0 时发独立事件 `step_readback_exempt`（step_compaction 事件不动）；harness §K 列 `W2免压回读`。局限：只认 `spill:` 前缀。spec：scratchpad d43/spec.md（持久化副本 docs/plans/2026-11-04-d43a-clue-spec.md）。**(a) 落地 b3f2609（11-04）**：compactor.py `observation_clue` / `assign_key_counts` + 驱逐桩 `; mentions: …`（括号内、句柄行仍最后；取代桩不带；线索为空或带线索的桩不比原文短 → 逐字同旧格式）；单例计数按整段 history 当前可见文本、spill 回读不计（一审 M2/m8；残项：第一次 W2 时计数偏薄，下一批 Colab 用 recall_source 量）；一审修补收窄：冒号只在 JSON 键 / 行首算赋值，像密钥的键名、≥20 字不透明值、URL userinfo 不收，ID 拒 SHA/ISO/RFC… 前缀，两处正则线性化。b2 重放（d43_pre/validate_r2.py）：三卡覆盖 28/28、30/30 不变，非空线索 194/504（w1）/ 220/603（catalog），多出的是填充 JSON 指标行的单例键，规则没判错、保持；每 run 线索中位 2.4K / 2.9K 字。harness：analyze.py `recall_source`（original / readback / stub / unknown，回读优先）+ `readback_calls` 两列，b2 57K 归因 none 0/0/27、w1 1/2/0、w1w2 1/3/0、catalog 0/0/0 与 analyst 一致；run_knee meta `stub_clue`（进 `_ALWAYS_COMPARED`，旧批续跑拒）。变异证明 37/37（verifier 独立复跑）；全量 4355 passed。**验收**：随 D44 的 Colab 批跑三管理臂 57K，看 recall_src 回读列与 aborted 数是否下降，不看 recall 单值。。**11-05 验收（d44 批）：57K 回忆 w1 92% / w1w2 80% / catalog 86%（b2 0 / 20 / 0%，Fisher p 9.6e-06 / 0.089 / 0.0018；ITT 11/20 / 8/20 / 12/20 全显著），31 个回忆成功 run 里 26 个三卡全归桩（桩头命中 3/4/12，其余 61/80 只在重建线索里）、回读 19/14/3 次（b2 43/107/75）命中 16/7/3；判显著回升 ✅，机制是线索不是回读。D43b 免压只发生 2 run、无一次回读被压成桩（b2 6 次），机制成立、效应不可判（`benchmarks/data/context-knee-20261105-d44/ANALYSIS.md` §1.1–1.2）** | ANALYSIS §1.4；b2 §4.3 |
| D44（新） | catalog 前缀是否作缺省 | 先补「schema 按需」（缺参 System Error 自动回灌 schema 再试 / `tool_schema` 元工具）再定；现状 −4 点 [−10,+2]、首 prompt −61%、缺参 16 次。**用户 11-03 拍板：先补 schema 按需，缺省值补完再定** **11-04 scout 纠前提**：b2 的 16 次缺参 System Error 全来自 harness 桩（orch_stubs.missing_text）；生产没有任何实参核对，缺参只在 execute() 抛 TypeError 时暴露，registry 把异常文本 + 整段 traceback 回给模型；`get_tool_schema` 元工具早已存在，80 run 只被调 3 次；catalog 已是两条 gemma4 条目的生产设置（config.yaml），「缺省」只问 Qwen。**「schema 按需」落地 3564404（11-04）**：`MCPRegistry.argument_problems` + `validate_call(schema_feedback=)`——必填缺席或 null、未知键（properties ∪ required 之外；无 properties / additionalProperties 时不查）→ 不执行、不进审批，回执 `System Error: '<名>' was not executed: missing required argument(s): …`，catalog 模式附该工具完整声明（与 get_tool_schema 同一渲染；前缀里已有完整声明的 core / 路由件只给提示句）。打回的调用计失败（进 unresolved_failure / 批次失败 / 去重指纹，带 `_safe`）。模型面不再回 traceback（registry、rag_search）。edit_file.new_string / write_file.content 改可选（execute 缺省空串）。harness：System Error 三类、`missing_arg_recovered`（同工具其后真成功，一配一）、`schema_fed`（只认事件）。**可比性**：本批起编排器桩拒未知键（b2 桩静默接受），成功率 / 迭代 / system_errors 与 b2 不直接可比，未知参数类单列。**待定**：catalog 是否作 Qwen 缺省——下一批 catalog 对 full 看首 prompt、成功率、缺参→恢复率。。**11-05 验收（d44 批）：预检只在 catalog 触发 11 条 / 11 run，全为「缺必填 + 未知键」（run_math 10 用 `command` 代 `code`、move_file 1），附声明 11、恢复 11/11、打回计失败计步 11/11（1 run 只因此超限）；prod 三臂 0；catalog − w1w2 成功率 −1 [−14,+12]（b2 −4 [−10,+2]）、Pass^2 +0 [−15,+15]、首 prompt L0 ×0.394。缺省待拍板：建议 (iii) catalog 作缺省 + run_math 等高频工具进 core 完整声明（ANALYSIS d44 §1.3 / §3）**。**用户 11-05 拍板 (iii)**（原话「按建议没问题」）：Qwen 条目缺省 catalog，run_math 等高频工具进前缀完整声明的 core 列表；打回的调用照计评分步数、口径不改。**落地 c76492f（11-06）**：两条 Qwen 条目 `tool_schema_mode: "catalog"`（MLX 条目为外推）；core 7→9 件 +run_math +rag_search（move_file / copy_file 各 1 次不进）；BUDGET_CATALOG 3810→4590（九件实测 gemma4 4494 / qwen35 4457，两件 +710/+693）；core 件打回只给 PRECHECK_FULL_MODE_HINT（新用例 file_extract 守非 core 附声明路径）；harness `keep_mock_tools_full` 改按生效档位（`--prefix prod` 随条目=catalog 并入 mock 工具）+ 续跑口径键 `mock_merge_rule` 恒比拒旧目录 + 编排器 `--prefix full` 供 Qwen full 对照臂；summary_trace 分组键纳入披露模式。变异 V1–V5 全红。**可比性**：core 九件 / MLX 条目未经实机，×0.394 只是上界；下一批 none/w1/w1w2 臂的 `--prefix prod` 生效为 catalog，与 b2/d44 的 full 口径不可比，full 对照须显式 `--prefix full` | ANALYSIS §1.5 |
| D46（新） | qwen35 `_coerce` 写坏 JSON 文件（生产缺陷，阶段复审 M2） | 建议下一工作项首位：按 schema 只对 object / array / number / boolean 型参数还原，string 原样；改 `test_qwen35_dialect.py:124` 预期、加保留用例；变异证明。退路：file_edit / edit_file 对非 str 内容 json.dumps。**用户 11-03 拍板：首位开工** | 13 个 Qwen 臂 lc-hard-009 0 过、Gemma 18/18；反驳者 CPU 复现（数组 / true / null / 1e3 同样写坏） |
| D45（新） | 摘要续跑丢进度 | 候选：(a) `_turn_to_text` 加助手正文与工具结果 digest（走 W2 压缩预算）；(b) turn 1 先落实 task_state；(c) 两者后再跑 trace 三步（约 1 h / 7 单位）。模板定 new + 900。**用户 11-03 拍板：(a)+(b) 都做，(c) 作验收** | ANALYSIS §1.7；POST 对 PRE pass 减半 **11-03 落地**：(a) 已落地（提交 38411d7）；(b) 走 harness 迭代 6 实测无效 → D47；(c) 验收已跑（ANALYSIS d45）。 |
| D47（新） | turn 1 从不建 plan，task_state 进不了摘要 | harness 加迭代（3→6）无效：模型宁可直接做完也不调 update_plan。候选：(a) 生产侧 no_plan_nudge 提前到第 1–2 次物理调用后 / 改措辞为强制；(b) 轮首自动种 TaskState（goal=user 原话，plan 空）并让 `render_for_summary` 在无 plan 时也输出 [Goal]+[Scratchpad]（remember 不再被拒）；(c) 接受：摘要靠工具段，不靠 task_state。**建议 (b)**：不改模型行为，摘要与便签立即可用；(a) 会给所有短任务加一次 plan 开销 。**用户 11-03 拍板：(b)**（原话「先进度落盘，等我 /compact 之后再按建议开工」）；/compact 后开工，顺序 D47 → D43 (a) → D44 schema 按需 → 阶段复审（覆盖 d60bd50..，含 D46/D45/D47）| ANALYSIS d45 §1；D45 (b) 的实测否定 **落地 44ca2aa（11-03 晚）**：种点在 `run_turn` 的 `_fresh_turn` 块（无任务或 plan 空才种，未完成 plan 跨轮沿用，续跑不重种）；catalog 路由改「有 plan 才路由」且只剩 `_tools_decl_for` 懒路由一处；remember 只拒无任务；REMEMBER_SCHEMA / SUMMARY_INSTRUCTION 任务块句改措辞；harness 加 turn1_remember_calls / has_scratchpad / chars，`cont_live_task` 口径=带 plan 的任务。验收：Colab `trace-d47-both`（见 §9 11-03 晚）。 **验收（11-03 晚）**：机制成立、便签效应未触发（remember 0/30）、[Goal] 段只是复述用户文本且 POST pass 方向不利。**待拍板 D47-next**：(a) 保留种任务，改让模型用便签——REMEMBER 描述首句去掉「During a planned multi-step task」前提、no_plan_nudge 文案点名 remember，再跑一步验收（约 3 单位）；(b) 保留种任务但轮记录只在 notes 非空时盖 task_state（去掉只有 [Goal] 的段，摘要器输入回到 d45 形态、便签有了自然进）；(c) 回滚种任务（revert 44ca2aa 的生产部分，保留 harness 字段与路由简化），D47 判「无效」。建议 (b)+(a) 合一步：先去掉纯 [Goal] 段（它只带来重复与 −10 pt 的嫌疑），同时改描述与 nudge 文案，一步 Colab 看 remember 调用率；若仍 0，走 (c) 或改编排器自动抽值（D47c 的替代）。 **用户 11-04 拍板「按建议 (b)+(a) 再验」→ 落地 55cff85**：`TaskState.is_bare_goal()`（无 plan 且无便签）在 `session._stamp_task_state` 活任务分支与编排层弃前两处把闸（scout 纠偏：熔断/挂起/审批暂停出口不弃任务，活任务分支照旧盖，只在弃前过滤达不到 (b)）；REMEMBER 第二句改为「无 plan 也能用；工具原文会被缩短/丢弃，便签在折叠时随摘要带走」（一审 Minor：无 plan 的便签不进 prompt 任务块，不能许诺 later turn 看得见），nudge 末加「first save with remember …」；变异 8/8 红。**验收（11-04，ANALYSIS d47b §1）：判据未达标**——remember 0/30（三批 90 次 turn 1 共 0），(b) 生效且无副作用，pass 与 d45 无差。**待拍板 D47 收口**：(A) 保留现状——种任务惰性无害（prompt 任务块与路由都 plan 门控、纯 [Goal] 不盖章），(b) 闸与 (a) 文案保留，D47 判「便签靠模型合作不成立」收口，D47c / 编排器自动抽值记 DEFERRED，进 D43 (a)；(B) 回滚种任务（revert 44ca2aa 生产部分），保留 (b) 闸、文案、harness 字段与路由简化——多一轮门，收益只是少三行；(C) 立项编排器自动抽值（轮末从工具结果抽 url/id/路径进便签）——需新方案与新验收；(D) 若仍想测模型合作：把 no_plan_nudge 挪到第 1–2 次物理调用后再验一步——reviewer 指出现行提醒 15/30 对在任务已完成时才触发，本次对 (a) 几乎无检出力。**建议 (A)**：两步验收已花约 1.3 h 租约，模型在 90 次 turn 1 与所有提醒后的思考里从未提到便签，继续改文案或位置性价比低。 **用户 11-04 拍板：(A)**（原话「(A) 保留现状进 D43」）——D47 收口：种任务与 (b) 闸、(a) 文案保留；D47c / 自动抽值 / 挪提醒位置记 DEFERRED，不再为便签花租约。 |

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
| 编排器路径基线被 harness 桩工具压到地板，P2 目标无法判（11-02 实测） | D39：桩工具接可用实现 / 编排器臂改 catalog 前缀，再复测（需 Colab 授权）；拐点规则已改为基线在地板时明示空判 |


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
- 11-01 傍晚（W1 推送后直接开 W3）：scout 推翻前提（只改渲染在增量路无效——prompt_stream 存含思考原始字节、只比 history 指纹；MLX 缓存不可裁；官方两模板口径）→ 主会话定 D36、亲改 config.yaml
  （四条目 thinking_replay：MLX tool_turns / Colab last_k）→ implementer（core/thinking_replay.py 纯函数、engine 一次投影 + view 走 stream_valid/增量/全量/commit + ps.seen 身份校验、
  prewarm、轮统计、list 型 thoughts 改 str、gemma4 官方 4bit 模板差分、run_knee 口径开关与续跑校验）→ reviewer 一审 0 Critical / 2 Major（预热按本轮语义投影致前缀必分叉；knee 续跑不拦口径）
  / 5 Minor / 5 Nit → 同一 implementer 修 9 项（Minor 3 表外模型缺省记残留）→ 主会话二审逐行（投影规则/流校验/commit 用 view/预热/续跑校验/差分样本）→ verifier 13 变异体全红、sha256 还原一致。
  新增 73 条测试。提交 `2b12243`（全量 check 经 pre-commit）。下一步 W2 步骤边界压缩。
- 11-01 晚（W3 推送后用户「直接开 W2」）：scout 校正五前提（被压观察未必已 spill 且桩无句柄；complete_step 无单点；代价公式量纲不齐；轮内就地改污染落盘；步无 history 下标）
  → 主会话定 D37（按 W1 档位触发、后端缓存语义判别、两处落刀点、副本 + 原件表、补写 spill）、亲改 config.yaml（agent.step_compaction）→ implementer（step_compaction.py、
  压缩器范围/回调/豁免、_compact_turn_range 共用、步骤边界与 hard 钩子、engine full_renders、spill 补写、66 例测试）→ reviewer 一审 0 Critical / 2 Major（范围压到未看过的同批观察；
  hard 断言为空）/ 8 Minor / 4 Nit → 同一 implementer 修 13 项（属性改名 block_prefix_cache、键改 cold_refill_trigger_level、hard 压后重量再决定尾注、豁免元工具与提醒）
  → 主会话二审逐行 → verifier 15 变异体全红、sha256 还原一致。提交 `6a092c8`（全量 check 经 pre-commit）。P1 代码序列 W1 → W3 → W2 完成（W2 提前并入，W5 留 P2）；P1 两条 Colab 出口待复测。
- 11-01 晚 P1 阶段复审（stage-reviewer，范围 `80a1b3b..a6be3ba`，19 变异体）：**1 Critical**（C1 `colab/compute_vm.py` verify_inference 期望三键严格相等，W1 后 /health 回执多带 `max_model_len`
  → 下一次 deploy start/switch 装载 11 分钟后必 mismatch、start 回收 VM 白烧；colab 门手写三键回执测不到）、**1 Major**（M1 三档复测 harness 不经编排层，W1/W2 实机验收不可达）、
  7 Minor（run_colab.sh 起机期间信号不停机 + 防御性 stop 失败被吞；README switch 示例 32768 低于护栏 hard；watch_until pidfile 缺失关掉存活检查；W2 范围下界无测试钉住；
  四项声明行为无测试能红；remote block_prefix_cache 写死；六处文档口径）。两条 C/M 各派 skeptic 驳斥均失败。处置：C1 主会话亲改（期望补 `max_model_len` 仍严格相等，
  测试改真 `public_options` 造回执 + 不一致必抛用例；变异体「删期望键」→ 两条 ready 用例红）；m1/m2/m3/m7 主会话改；m4/m5/端到端 ceiling 用例/harness 量测记录 → implementer；
  M1 立 W-K 工作包待拍板；m6 记残留。修补一次提交 `e435f28`（reviewer 一审 0C/0M/6m/4n 全修主要项；pre-commit 全量 check：backend 4091 passed / 3 skipped，colab 135，web 252）。全量 pytest 两条失败（call_id 含 "1250" 子串偶发、Swift 变异体 nil 解包）在本阶段 diff 之外，见 precommit-under-load 记忆。
  已核无问题面：W-3 冒烟数字、measure_prefix 前缀表、P0 拐点 CSV 0 差异、守卫链构造期失败关闭、persistable_messages 三处覆盖、W3 指纹与身份校验、计费出口、密扫 0 命中、旧测试期望零改动。
- 11-01 深夜 W-K（用户「先做 W-K」）：scout 摸底（校正「ceiling 只压本轮」前提）→ 主会话定 D38 → implementer（run_knee `--orchestrator`、orch_tools.py、filler in_turn 形态、34 条测试）
  → 主会话收口三处（轮内填充形态、构造顺序、续跑口径含生效 cfg）→ reviewer 一审 0C/3M/11m/5n → 同一 implementer 修（审计生成不劫持主会话、SpillReadTool、对照组各臂同放置、事件/审批还原、游标、指纹）
  → 主会话二审逐行 → verifier 7 变异体 6 红 1 等价存活（mode 比较冗余）。提交 `c21a042`（全量 check：backend 4125 passed / 3 skipped，colab 135，web 252）。backend/framework 零改动。
- 11-02 凌晨（用户「先 colab 复测…测完直接开 W4」）：Colab 复测链式脚本 `benchmarks/context_knee/run_colab.sh`（起机 --retain → 冒烟 → 8 臂 → 必停；挂死判据 stop+start 续跑）跑 Qwen3.8-27B-INT4，首次起机被 colab CLI 60 s 超时打断（瞬时），重跑成功，批次进行中（结果另记）。
  并行开 W4：scout 摸底（纠三前提：无「偏好与身份」节、提醒机制是步骤边界提醒、TaskState 任务完成即弃）→ 主会话定模板/TaskState 入折叠/K 重写/截断感知、亲改 config.yaml 两键与 PROTOCOL §2.3
  → implementer → reviewer 一审 0C/3M/8m（任务块截断先砍便签 → render_for_summary；配对键缺维度 → 分组 + 碰撞报错；640 截断无感知 → 900 + 事件）→ 同一 implementer 全修 → 主会话二审逐行（守卫、折叠计数、盖章与弃前渲染、透传、模板文本）
  → verifier 11 变异体 10 红 1 等价存活（max_tokens 的 bool 判断）。提交 `5e2626e`（全量 check：backend 4180 passed / 3 skipped，colab 135，web 252；首轮被 test_native_task_control Swift nil 解包偶发拦下，单跑通过后重跑全过）。
- 11-02 上午（批次收官 + W4 已落地）：Colab 八臂批次 00:09–10:21 全 ok、0 超时、`DONE BATCH_RC=0`、VM 已停（约 10 h / 约 120 单位；首次起机 FAIL_READY 未产生数据）。analyst 用 `benchmarks/context_knee/analyze_orch.py` 复算 → `benchmarks/data/context-knee-20261102/ANALYSIS.md`（结论 §1–§3 主会话手写；reviewer 一审 0C/0M/10m，抽查 6 组数字全同，全修）。结论：P1 出口 a ✅（−4 点 [−16,+5]）、W3 不降 ✅ 峰值量不出、W-2 省 53–61% 持平 ✅、W1 57K 20/20 首生成前 fold 无超限实发（代价回忆 0%）、W2 触发 12/11/17 次持平、P2 因基线在地板不可判（D39/D40 待拍板）。
  链脚本 `run_colab.sh` reviewer 一审 1C/4M/7m → 主会话亲修（红线路径）→ 二审 2M（旧 timeout 行再判挂死、stopped 提前置位）再修 → 桩自测 14 场景 55 断言 → verifier 变异体 9 红 1 等价（started=1 / hung_tail 判据 / 不跳过旧行 / aborted=hung / `$?` 取反 / kill_harness / fail_exit 的 started 门 / arm_complete / skipped 计数；stopped 提前置位桩下等价）。harness 口径三修（chore-worker；主会话逐行：峰值拆键、量法值另记、knee 地板短路、旧断言改对）。提交 `f1a16cc`（全量 check：backend 4183 passed / 3 skipped，colab 135，web 252；含 11-02 数据档案 21 MB）。
- 11-02 午后（用户拍板，原话「按你的建议没有问题，不用省算力。确定方案，落实文档后，等我 /compact」）：D39 定 a + c（可用桩 + catalog 对照臂；元工具计熔断另议）、D40 定溢出外置兜底拒绝；W3 接受「方向对、量不出」；Gemma 31B 两臂与 summary_trace 真模型搭第二批同一租约。新增工作包 W-K2、W1b（§3），§5 加「第二批」行。只落文档不动代码；下一步 /compact 后按 W-K2 → W1b → 链脚本加臂 → 第二批起机的顺序开工。
- 11-02 下午（开工：W-K2 → W1b → 链脚本加臂 → 第二批）：两名 scout 先纠前提——(1) 162/771 Unknown tool 是直调 prod 臂，编排器四臂 18–23%，吃回合的是 rag_search 桩触发的检索未完成推回链；(2) ceiling 路径不碰 user 轮、只把轮内观察压成无句柄的桩，57K 回忆 0% 的真因在此（D40 的「user 轮超限外置」无实测场景，改列 D40b 待拍板）。两名 implementer 并行（文件集互斥，同一工作树）：W-K2 可用桩（reviewer 一审 4M/11m 全修；verifier 变异体两轮 8/8、10/10 红）、W1b ceiling 压缩带回读句柄（一审 0C/0M/5m 修 3；变异体 6/6 红）。链脚本 `run_colab.sh` 主会话亲改为第二批九步两段（A 段 Qwen：编排器三臂 + catalog 对照臂 + trace 三步；B 段 Gemma 31B：直调 mock / catalog；换模型 stop → 等租约释放 → start 失败再试一次；trace 步无 --resume、半截目录挪 .partial 重跑、完成的不重跑；只给 B 段步直接起 B 模型），reviewer 一审 0C/3M 全修，桩自测 23 场景 88 断言，verifier 变异体 6/6 红 + 主会话亲跑 2 个（换模型不先停机 → phases 红；B 段独跑仍先起 A → b_only 红）。全部合成一次提交 `4a35baa`（全量 check 首轮即过：backend 4204 passed / 3 skipped，colab 135，web 252）。偏离纪律一处：两名 implementer 并行未用独立 worktree（backend/.venv 不在 git、worktree 里跑不了测试），以文件集互斥 + 各只跑本组测试文件代替，两者改的文件零交集。
- 11-02 晚（第二批收官）：第一租约 13:36 起（首次 13:33 FAIL_READY 瞬时超时，人工重起）跑完编排器四臂（89 / 91 / 91 / 102 min）；用户告知非订阅 VM 12 h 上限 → 主会话估 A 段超限，挂哨位在 trace 首步起步时给链 TERM（20:05 停机 rc=0）；第二租约 20:07 续跑 trace 三步（16–19 min/步，主会话事前估 3 h/步偏高 10×）+ 换 Gemma（起机失败一次、原因未证实、重试即过）两臂，22:49 `DONE BATCH_RC=0`，合计 9.2 h / 约 49 单位（余额 860.5 → 811.4；此前各处「约 100 / 约 120 单位」按约 12 u/h 估，实测约 5–7 u/h，上午批实耗无余额记录不可回核）。analyst 复算（analyze_orch 识别第二批布局 + O–T 节、`fold_forensics.py`、新测试 6 例；上午目录逐字节不变）→ reviewer 一审 → 主会话二审 → 结论手写 ANALYSIS §1–§3：W-K2 桩生效（Unknown 0、基线 35→60%，70% 未达）；编排器路径拐点首次量出 32K 四臂一致；P2 初判「成本 ✅ / 不劣化 ✅」；W1b 机制成立回忆未回（D43）；catalog −4 点缺参 16 次（D44）；Gemma P1 出口达成；trace 模板定 new + 900、legacy 13/30 空摘要、摘要续跑劣于原文续跑（D45）。提交 `472b1b2`（全量 check 经 pre-commit 首轮即过）。
- 11-03 凌晨（阶段复审 `f1a16cc..58e8ba6`）：stage-reviewer 0C/2M/9m，两条 Major 各经反驳者（reviewer）核查后成立，主会话亲核代码行：**M1** P2 初判换了口径（§2 原文峰值 ≤ 拐点 × 0.7 = 22.9K，≥32K 档 0/60；「区间含 0」证不了非劣效）→ §5 P2 行 / D42 / ANALYSIS §1.3 改为「成本 ✗、成功率不可判、Σwall ✅、门槛未过」，D1/D2 是否按编排器拐点回填并入 D42；**M2** lc-hard-009 不是题目问题：qwen35 `_coerce` 不看 schema 把 JSON 字符串参数转成 Python 对象、file_edit `str()` 落盘成 repr，生产静默写坏 JSON 文件（Gemma 18/18 过；反驳者复现数组 / true / null / 1e3 同样写坏）→ 新 D46，§1.1 / §2 / W-K2 结果改写，「6 个步数超限是评分口径」改为救回 2–3 个。Minor 9 条：直调臂 Unknown 比例（20–21% / 71%）、w1 耗时 91、单位费率按实测（约 5–7 u/h，此前估 12）、「3 h/步」注明主会话估、换机失败原因未证实、测试数 6 例 / 4210、P2 / W4 / P3 标签更新（P3 评估不可判：17 对子集 7 对 5，p=0.5）、「驱逐桩」措辞 + P 节表头 ≥、桩自测缺 FAIL_READY 场景（记 ANALYSIS §2 待办）；存活变异体 A2 补缺参判别用例（7 passed）。修补提交 `777ce14`（全量 check 经 pre-commit 首轮即过）（代码只有表头字符串 + 一个测试，主会话自读；docs 为主）。
- 11-03 上午（D46 修复，用户拍板原话「按建议，开 D46」，六项均按建议定）：scout 摸底 → schema 在 `engine.py` 调 `parse_response` 处（`gtools`）可达，生产无统一类型校验，gemma4 DSL 由语法定类型不受影响。修法：`Qwen35ResponseParser.parse(raw, tools=None)` 按 schema 还原（string 原文、boolean 收 True/False、integer 容 5.0、object/array 需 json 成功且类型吻合、联合/anyOf/oneOf 按序逐型、无 schema 原文），旧 `_coerce` 删除；协议 + 两方言签名加 `tools`，engine 传 `gtools`。implementer 全量 4225 passed（+14）；reviewer 一审 0C/0M/4m/6n → chore-worker 全修（re.ASCII、5.0、anyOf/oneOf、四类用例、协议 docstring）；verifier 变异 11/11 红（string 走 json.loads / 删联合含 string 判断 / 无 schema 走 json.loads / boolean 不收 True / 去 re.ASCII / integer int(float) / object 去 isinstance / 索引非 dict 不退路 / 删 null→None / engine 不传 tools / engine 传 tools 非 gtools），sha256 逐次还原一致。file_edit 退路未启用。提交 d60bd50（pre-commit 全量 check 首轮即过）。
- 11-03 下午（D45 开工，用户原话「直接开 D45」）：scout 纠三处前提——摘要器读落盘原件轮记录（W2/W1b 桩不进）、`_turn_to_text` 本就取助手正文（工具调用迭代不存正文、熔断出口不写助手消息）、role=tool 整条丢弃；nudge 阈值 3 = harness turn-1 迭代 3 → task_state 0/90。落地 (a)：`_turn_to_text(tool_digest_chars=0→生产 200)` 工具段（参数 ≤80 / digest ≤200 / ≤8 条 / 段 ≤2400 整行丢弃 / 批次 ≤24k 从新到旧 / 外来文本压行 ⏎ / 固定格式短句柄）；沉淀路径缺省 0 零改变；new 模板加一句。(b) 走 harness：turn-1 迭代 6 让 nudge 有迭代可响应（生产 nudge 阈值不改），加 `turn1_plan_built` 与 `summary_input` 落盘。reviewer 一审 0C/2M/7m：**M1** 长句柄原文绕过压行（`_SPILL_CALL_RE` 跨行 + 全文首匹配 → 可伪造独立 `User:` 行）→ 只追加严格 hex 短句柄取最后匹配；M2 summarize_trace 分组键。verifier 变异 18 变体 17 红 1 等价（C16 入口夹紧被整行丢弃预算覆盖，纵深层），sha256 逐次还原一致。链脚本加 trace-d45-digest / both / plan 三步 + 桩自测 d45_steps（24 场景 92 断言）。提交 38411d7（pre-commit 第三次过：前两次各一条原生测试在 Colab 链并跑负载下假红，单跑即过）。(c) Colab 验收三步 OUT=context-knee-20261103-d45 起机 11-03 15:30，对照上一批 trace-new-900（POST 10/30、重复 +2.7）。 验收收官提交 798d140（数据档案 + analyze_d45.py + ANALYSIS + §3/§5/§6 D47）。待办：编排层回执带 call 下标（同名调用配对张冠李戴）；W2/W1b `_spill_hint` 长句柄同源问题（内存副本，不进摘要）；摘要器输入 token 上限（既有）。
  - 待办：编排层回执带 call 下标（D45 m1）：拒发 / DELIVERY_BLOCKED 回执先于物理回执 append，同名多次调用时摘要器工具段按名先进先出会张冠李戴。
  - 待办：W2 / W1b 压缩桩的 `_spill_hint` 同样追加长句柄原文（D45 M1 同源，既有问题，只在内存副本、不进滚动摘要）：改为短句柄固定格式。
  - 待办：摘要器输入无 token 上限（既有）：D45 加了批次字符预算 24k 作为上界之一；摘要失败水位线不推进、batch 变大的既有行为未改。
- 11-03 傍晚（D45 收官，提交 798d140 / 3606c8a，完工推送 17:42）：用户拍板 D47 (b)，指示先落盘、/compact 后开工。开工清单：D47 (b) 轮首自动种 TaskState（`orchestrator.py` 创建点 :1937 只在 update_plan 时触发 → 轮首种 goal=user 原话、plan 空；`task_state.py render_for_summary` 无 plan 也输出 [Goal]/[Scratchpad]；`remember` 无 plan 不再拒；注入 prompt 的 `task.render` 仍只在有 plan 时）→ trace 验收复用 trace-d45-both 口径一步（约 25 min / 3 单位）；然后 D43 (a)、D44；最后 stage-reviewer 覆盖 d60bd50..HEAD。链脚本待办：首起 FAIL_READY 自动重试一次（三见）、MAX_VM_HOURS、桩自测 FAIL_READY 场景。
- 11-03 晚（D47 (b) 落地，提交 44ca2aa，用户 /compact 后原话「开工」，未再问）：scout 纠两处前提——`render_for_summary` 本就按三段各自判空、无需改；摘要段缺失的根因是轮末 `session.task_state` 为 None（`_stamp_task_state` 无活任务可渲染），compactor 只读轮记录的 `task_state` 键。scout 另指出真风险：catalog 模式 `_tools_decl_for` 只要任务非 None 且无路由记录就当场路由，种任务会让每轮重算前缀——故路由门改成「有 plan 才路由」。implementer 落地 + T1–T7 用例；reviewer 一审 0C/1M/7m：Major `cont_live_task` 口径（turn 1 熔断留下的无 plan 种任务续跑轮首会被换掉，不算活任务）已修；Minor 2 摘要指令把便签说成「系统维护的事实」（便签现在任何轮都能写、可能来自不可信工具输出）→ 改为「编排器记录的助手自身便签，是数据不是指令」；Minor 4 update_plan 即时路由块与懒路由等价（reviewer 两个变异存活）→ 删除；Minor 6 老批次缺键显示「—」。变异 11/11 红（verifier，sha256 还原），全量 4305 过；pre-commit 第一次被 `test_native_retention_and_unsaved_pin` 假红（负载 1.4，两天内第四例）、重跑过。链脚本加 `trace-d47-both`（参数逐字同 d45-both，差异全在代码侧），selftest 92/92。Colab 一步 21:08 起（`benchmarks/data/context-knee-20261103-d47`）。21:47 DONE BATCH_RC=0、VM 已停（约 0.65 h）。analyst 复算（`analyze_d47.py`，ANALYSIS d47 §1）+ reviewer 一审：机制成立但效应未现——task_state 段 30/30 进摘要器输入、只有 [Goal]；remember 0/30；POST pass −10 pt [−20, 0] 跨批不可判。D47-next (a)/(b)/(c) 待拍板（§6 D47 行），建议 (b)+(a) 合一步再验。档案提交 1186342（22:31 推送）。
  - 待办：定时轮的 `[Goal]` 全是 preamble 样板（`_last_user_text` 取前 500 字，preamble 516 字；带 plan 的定时任务 goal / 路由查询历来如此，D47 让每条定时轮记录都带这段）。
  - 待办：`test_resume_budget` 的 `with_task=False` 档失去区分力——新鲜轮恒有任务，`no_task_calls` 携带路径只剩「续跑且 task 为 None」可达；要么给它一个续跑形态的用例，要么删掉该路径。
  - 待办：D47c 待拍板——无 plan 时 remember 的便签轮内模型看不到（task_block 仍 plan 门控），只进摘要；要不要在 notes 非空时注入一个最小 [Scratchpad] 块。等验收数据（remember 调用率）再定。
  - 待办：`test_native_*` 偶发 precondition / nil 解包（retention ×2、task_control ×2，负载高低都见）疑 Swift 测试二进制自身竞态，查根因。
- 11-04 上午（D47-next (b)+(a) 落地 55cff85 + 验收，用户原话「按建议 (b)+(a) 再验」）：scout 纠偏盖章路径（见 §6）；implementer 落地 + T_b1–T_b3 / T_a1–T_a2；reviewer 一审 0C/0M/2 Minor（描述「later turn」许诺过头 → 改写并把「Notes also survive context folding」并入；续跑/旧会话无任务时 remember 仍拒，与「不需要 plan」不矛盾，记录）+ nit：selftest 子串 grep 挡不住多出旗标 → 改成 d47b 参数（除 --out）与 d47 逐字相等（93/93）；verifier 首次按硬规则停工——看到两个 14 h 前主会话自己起的 `until ! pgrep -f "pytest tests -q"` 等待循环（pgrep 模式匹配自身命令行永不退出），主会话核实父进程为本会话 harness 后只杀这两个 PID、重派；变异 8/8 红、sha 还原、96 过。pre-commit 一次过（负载 1.25）。链脚本 `trace-d47b-both`（参数逐字同 d45-both）；Colab 10:54 起 11:28 停（≈0.6 h），BATCH_RC=0。analyst `analyze_d47b.py`（只 import analyze_d45/analyze_d47）+ reviewer 审：remember 0/30，判据未达标，详见 §3 11-04 条。代码提交 55cff85（推送 10:5x），档案 + analyze_d47b.py + 方案提交 2406ab0（约 12:2x 推送）。
- 11-04 中午：用户拍板 D47 收口 (A)（原话「(A) 保留现状进 D43」）。接着开 D43 (a)。
- 11-04 下午（D43 (a) 设计）：scout 定位三处桩生成点都在 compactor.py:171-275（编排层不拼桩文本），桩头已是前 200 字，(a) 的新东西是抽取的词；analyst 复算 b2 的回读实况 / file_extract 能力 / 线索噪声（见 §6 D43 行）。设计定稿 + implementer spec 已写（scratchpad d43/），本窗口到阶段边界即停、请 /compact 后起 implementer。D43b（W2 再压回读内容）记为新决策点。
  - 待办：harness transcripts 的 feed 与 history 共用对象、事后被原地压缩改写（3 条回读在 feed 里只剩桩）——用 feed 做内容分析有偏差，run_knee 落盘前应深拷贝。
  - 待办：主会话后台 `until/while pgrep -f` 等待循环会匹配自身命令行永不退出——以后用 `--pidfile`/watch_until.sh，不再写裸 pgrep 循环。
  - 待办：`cont_turns` 与 journal 都不记元工具调用（remember/update_plan 的参数），004r1 那次便签内容无从核验；harness 若要核便签值，需在 run_trace 落 meta 调用参数。
- 11-04 傍晚（D43 (a) 落地，提交 b3f2609）：用户「已 /compact，开工」→ implementer（spec + preflight）→ reviewer 一审 0C/2M/8m/7n（M1 旧批续跑比较键、M2 单例计数只按本次候选）→ 同一 implementer 修补 11 项（F2 改 history 全局计数并排除回读、F3 桩不短于原文则去线索、F4 密钥/URL、F5 线性化、F6 冒号收窄并重验 28/28、30/30）→ verifier 独立复跑两套变异 37/37 红、sha 恢复 → 主会话逐行二审 → 一次提交。教训：第一轮变异脚本的目标串被修补改掉，脚本在 M1 断言即崩、M2–M18 没跑——变异脚本须逐变异 try/except，「没注入」与「存活」分开报。
  - 待办：analyze.py 不自动展开批次目录（b2 根目录直接传会报缺 transcripts.jsonl），要逐臂传。
  - 待办：recall_source 盲区两条（中缝截断后再压的观察记 original；journal 2000 字截断整份 spill 记回读，生产 file_extract 会多记）写在 docstring，b2 未碰到。
  - 残项：单例计数在第一次 W2 步骤边界偏薄（history 里只有 1–3 条观察），模板键会漏进线索；下一批看 recall_src 与线索噪声再定要不要会话级计数。
- 11-04 傍晚：用户拍板 D43b (i)——W2 步骤边界对 `file_extract(path="spill:…")` 回读观察免压（ceiling 仍可压），与 D44 同一波落地。
- 11-04 晚（D44 + D43b 落地，提交 3564404）：scout 纠前提（缺参报错是桩造的、get_tool_schema 已在、catalog 已是 gemma4 生产设置）→ 主会话定设计写 spec（docs/plans/2026-11-04-d44-d43b-spec.md）→ implementer → reviewer 一审 0C/1M/9m/5n（Major：预检打回的调用对吞失败提醒 / 升级护栏失明，b2 里最常见的失败类 16/24 全在此）→ 同一 implementer 修补 F1–F15（打回计失败 + 去重指纹 + `_safe`；免压计数首次标记 + 独立事件；recovered 一配一；异常类型名；rag_search traceback；required ∉ properties 死锁；两参数改可选 + 快照红后重生成；前缀已含声明不重复附；schema_fed 只认事件）→ 变异脚本目标串过期两处先干跑再放 verifier → 二审逐行 → 一次提交。
  - 残项：批次里有打回又因审批暂停时，续跑后那些打回不再计失败（审批分支在失败扫描之前）。
  - 残项：recall_source 回读配对按 feed 名，一轮两次 file_extract 且一次被打回时仍可能错配。
  - 待办：D44 验收批（catalog 对 full，Qwen）与 D43 (a)/D43b 验收（三管理臂 57K recall_src 回读列、aborted、W2免压回读）合成一次租约；跑前先补链脚本待办（FAIL_READY 自动重试一次、MAX_VM_HOURS、selftest 场景）。
- 11-04 深夜（链脚本待办落地，提交 33c0cb0；红线文件，主会话亲改）：`run_colab.sh` 加 READY_RETRY（首起 start 成功但 VM 随即 stopped/failed → 防御性停机 → 等租约释放 → 自动再起一次；等 READY 超时 READY_MINUTES 的不重试；停机失败不重试、各步仍写 skipped）与 MAX_VM_HOURS（缺省 10 h；时钟从 start 命令算；每步开跑前、续跑判定之后算「已用 + 同类步本批最长干净用时（挂死重起过的步不计；没跑过的类按 EST_DEFAULT knee 2 h / trace 30 min）> 预算」→ restart_vm 同模型换租约，不冒烟；刚起的租约至少跑一步、每步至多换一次，有界；DONE 加 ` ROLLOVERS=N`；时间线首行记有效配置）；测试接缝 LEASE_BUDGET_SEC / READY_DEADLINE_SEC。自测加 8 场景（ready_retry 含重试后挂死仍能重起 = 不占 RESTARTS_MAX / ready_retry_exhaust / ready_retry_off / ready_retry_timeout / ready_retry_stop_fail / lease_rollover 含换租约后挂死仍能重起 / lease_rollover_floor / lease_unlimited 含 MAX_VM_HOURS=1.5 → 64）→ 32 场景 / 115 断言全过。reviewer 一审 0C/0M/8m/7n：预算块原在续跑判定之前（FORCE_OUT 下会为已完成的 trace 步白换机）、换机次数上限 `if` 下永远打不中（删）、「无限换机」措辞错（无门时每次起机多换一次、有界）、首类步 est=0 的洞（缺省顺序下不可达，仍补 EST_DEFAULT）、重试前停机失败丢 arms_status 行、超时也重试、变异时序脆弱（预算 20 s / 步 12 s）、MAX_VM_HOURS 换算无场景；全修，Nit 14/15（换租约不冒烟、超长整数）接受。变异 V1–V8 见 commit message。
  - 残项：换租约后起机失败走 FAIL_START/FAIL_READY，DONE 不带 ROLLOVERS / BATCH_RC（与换模型失败同形，看时间线）。
  - 残项：EST_DEFAULT 只是常数，自测不可观测（预算小于单步的场景不区分）。
- 11-05（D43 (a) / D43b / D44 验收批，提交 41cfc9b）：用户 06:5x「起，跑四臂」→ 主会话 nohup 起 `run_colab.sh`（ARMS 四编排臂，OUT `benchmarks/data/context-knee-20261105-d44`）+ `watch_until.sh` 后台循环 → 06:54 起机、07:06 READY、四臂 07:07–13:01 各 86–91 min 全 ok、13:01 停机确认，无重起无换租约（READY_RETRY / MAX_VM_HOURS 首次上生产未触发）→ analyst 复算（新脚本 `benchmarks/context_knee/analyze_d44.py`，A–Q 复用 + U 节；写 ANALYSIS.md 被权限拦下，主会话亲跑落档）→ reviewer 一审脚本 → 主会话定稿 §1–§3。结论见 ANALYSIS d44 §1：D43 (a) 显著回升 ✅（机制是 R2 线索，回读少读多中）；D43b 机制成立、效应不可判；D44 预检 11/11 恢复、catalog 无证据更差；无管理臂 L0 35% 对 b2 60%（跨批混淆，只报）。
  - 待拍板（ANALYSIS d44 §3）：D44 缺省（建议 (iii) catalog + run_math 等进 core 完整声明）；打回的调用计评分步数（建议照计）；P2 门槛 L0 ≥ 70% 三批未达（建议下一租约顺带种 seed 加 rep 量噪声）；D43b 单独证明（建议接受机制成立）。
  - 残项：b2 → d44 之间还有 D45 / D46 / D47 改动，跨批成功率差不可归因；seed 未生效（`sampling_seed_applied` False 两批皆然）。
  - 残项：analyze_d44.py 批次布局硬编码（第三个批次专用入口），下一批前抽成参数。
- 11-05 下午（阶段复审 d60bd50^..e549f21 + 修补，提交 4dae900）：stage-reviewer 0C/0M/7m/3n——委托点名的数字全部重算对上（57K 回忆、回读、预检 11 条、none L0、d47b 0/30 与 13/30）、四档案可复算且引用文件全入库、20 余变异点名测试全红、生产预检 / 死锁 / 不重附声明 / D46 覆盖 / D47 路由逐链核过、付费算力 fail-closed、7,907 新增文件密扫无命中。Minor：M1 D47 后 `no_task_calls` 携带路径无用例（两处变异存活、注释称不缩小覆盖）；M2 R2 线索密钥过滤只是键名子串黑名单（`"key": sk-…` / `OPENAI_KEY=` / `"pass":` / `license_key =` / `xoxb-` / 查询串 `user=alice` 都进 mentions）；M3 D43 (a) 只按 recall 判、未按预注册判据（回读列与 aborted）下判决——补算 57K 三臂 aborted 39/60 → 24/60 p=0.010 也判过；M4 方案「全桩线索」超出数据；M5 validate_r2 头注 NRUNS 20 应为 10；M6 勘误 14/11 未同步方案；M7 用户 11-04 拍板 (A) 的 D47c / 自动抽值 / 挪提醒位置未记 DEFERRED。Nit：49K「无 fold」应为「无首迭代 fold」（3 个已 aborted run 有后期 fold）；场景数 7/31 应 8/32；d44 单位估算按 b2 实耗 5.3 u/h ≈ 33。修补：M2 → R2.1（末词黑名单含驼峰缩写与尾修饰剥离、凭据前缀、按字母数字段量熵且三类俱全、密钥值从 ID / 路径通道剔除、`?k=v` / `#k=v` 整段遮罩；reviewer 一审 0C/0M/6m/7n 指出首版熵规则把 UUID / 主机名 / 路径全丢的回归与 ID 通道旁路，二轮全修）；M1 补机制用例并把注释改为「当前无调用方可达（生产 / scheduler / harness 都不到）」；变异 M1 4/4、M2 49/49 红（verifier 独立复跑，首次因主会话给错脚本路径跑了旧版、5 条没注入，换 round2 版重跑全红）；b2 + d44 9,407 条观察重放 R2 对 R2.1 差异 0，存档数字不受影响；M3–M7 与 Nit 文档全改，DEFERRED.md 新增 §9。
  - 带出（ANALYSIS d44 §2）：knee harness turn 放置不走 D47 种任务，本批四臂 task 全程 None、remember 被拒，与生产 TurnRunner 不同；要不要让 harness 种任务待定。
  - 阶段关闭。待用户拍板四条（ANALYSIS d44 §3）：D44 缺省 (iii)；打回调用计评分步数照计；P2 门槛 L0 ≥ 70% 三批未达——下一租约顺带种 seed 加 rep；D43b 单证接受。
- 11-05 傍晚：用户拍板四条全按建议（原话「按建议没问题，等我 /compact 够继续开工」）：① D44 (iii)——Qwen 缺省 catalog + run_math 等高频工具进前缀完整声明的 core 列表（按 d44 批打回工具频次定名单：run_math 10、move_file 1，再看 b2 的 run_math 9 / rag_search 6 / copy_file 1）；② 被打回的调用照计评分步数，口径不改；③ P2 门槛 L0 ≥ 70% 不改判法，下一次租约顺带种 seed、L0 加 rep 量基线噪声，不单独起机；④ D43b 判「机制成立」收口，不再单测。/compact 后开工清单：(1) D44 (iii) 落地——`config.yaml` Qwen 条目 prompt 档改 catalog（红线文件，主会话亲改；bundle_id 不动）+ 预检 / 前缀的 core 完整声明名单加 run_math 等 + 测试 + knee harness 缺省前缀随生产（reviewer 一审 + 二审；非守卫型无变异义务，core 名单若有「必须附声明」的断言则算守卫）；(2) harness 种 seed 与 L0 rep 参数（为 ③ 备，随下一次租约）；(3) ANALYSIS d44 §2 带出的「harness turn 放置不种任务」要不要跟生产对齐——先 scout 评估影响面再定。
- 11-06 晚（D44 (iii) 落地，提交 c76492f）：scout 纠四处前提（档位字段是 `tool_schema_mode` full|catalog，w1/w1w2 是臂名不是档；编排器在 `backend/framework/core/`；core 名单在 config.yaml `agent.tool_routing.core` 不是代码常量；run_math 进 core 后预检按设计不再附声明只给 hint）→ 主会话亲改 config.yaml（两条 Qwen 条目 catalog、core +run_math +rag_search、bundle_id 未动）→ implementer 落地（预算线按 measure_prefix 实测重定、三处断言随新口径、harness 并入判据改按生效档位、drill 演练工具换 file_extract）→ 主会话定「加续跑口径键拒混」（`mock_merge_rule` 进 `_ALWAYS_COMPARED`）→ reviewer 一审 0C/1M/4m/4n（M1 summary_trace 合批分组键不含披露模式→已修；m3 编排器加 `--prefix full`；`--prefix` 缺省 mock 不改，直调模式共用且测试钉死）→ verifier 串行变异 V1–V5 全红、全量 4415 passed → 二审逐行（config / orch_tools / prefix / run_knee / 两条预检 oracle）→ 一次提交。**开工清单余项**：(2) harness 种 seed 与 L0 rep 参数（随下一次租约）；(3) harness turn 放置不种任务的对齐——scout 影响面：走生产路径会重置 `turn_history_start_idx`（harness :1294 设 2）、首次生成前机械压缩填充、无计划 remember 由拒变接受、预检 tracker 换 task.recent_calls；L0 前缀字节与计步口径推断不变，变化集中在 L>0 区间；HARNESS_MODE_KEYS 无「是否种任务」键，新旧混批不会被拒——待用户定。下一次租约注意：Qwen 条目已是 catalog，none/w1/w1w2 的 `--prefix prod` 生效为 catalog；要与 b2/d44 的 full 口径对照须显式 `--prefix full` 臂（run_colab.sh 未改）。
- 11-07：用户拍板 harness turn 放置**暂不对齐**生产种任务（原话「按建议没问题」）：下一次租约（P2 噪声基线 seed/rep、Qwen full 对照臂 `--prefix full`）保持与 b2/d44 同口径；对齐作为之后单独一波做，届时同时给 HARNESS_MODE_KEYS 加「是否种任务」键拒混。D47 种任务对路由 / remember / 预检去重的影响在此之前只有 golden test 守。
- 11-07（下次租约备件落地，提交 f0d967c）：knee harness 采样种子透传远端（scout 核实 --seed 原只对进程内 MLX 生效，远端请求体从不带种子；现 cuda/backend.py 可选键 +seed → SamplingParams(seed=)，harness 逐 run 覆盖层 seed*1000+rep，行记 sampling_seed_sent，续跑恒比拒混）；`--level-reps 0=6`（档 rep 数，summary 记 level_reps、complete 按档）；analyze Pass^2 / Pass@2 对 n≥2 改无偏式（n=2 逐项同旧值，d44 重算逐字节相同）；analyze_orch 臂列表自动发现 orch-turn-*；run_colab.sh +`orch-turn-full`（guard on / step on / --prefix full，不进缺省批）、LEVEL_REPS / KNEE_SEED 环境变量（起机前校验档位在每个 knee 步自己的 --levels；冒烟不带 LEVEL_REPS——reviewer C1）。一审 1C/2M/6m/3n 全处理；M2 拍板：臂间 / 档间同种子（共同随机数），**噪声基线只取臂内 L0 各 rep 方差，臂间 L0 差不再当噪声量级**（§3 旧「三臂 L0 差 15 点」的用法作废），L0 跨臂逐字相同率作 vLLM 复现性 sanity check。变异 V1–V10 全红，全量 4429 passed。
  - **下次租约起机单**（用户说起机时照此）：`ARMS=orch-turn-none,orch-turn-w1,orch-turn-w1w2,orch-turn-catalog,orch-turn-full LEVEL_REPS=0=6 KNEE_SEED=<整数>`；五臂约 90 min × 5 + L0 加 rep 约 21 min × 5 ≈ 9.3 h，贴近 MAX_VM_HOURS=10，必要时拆两租约或减臂；起机前确认 deploy 拉到的 commit ≥ f0d967c（否则算力端静默丢 seed 而行仍记 sent=True）；冒烟后看服务端日志有无 per-request generator / 慢采样告警；分析前先报 L0 跨臂逐字相同率。none/w1/w1w2 的 prod 前缀对 Qwen 生效为 catalog，与 b2/d44 的 full 口径对照用 orch-turn-full。
- 11-07 晚（P2 噪声基线批跑完并复算，用户「按建议。先试试单次跑完，实在不行再分两次。开工」）：12:56 nohup `run_colab.sh`（`ARMS` 五编排臂 + `LEVEL_REPS=0=6 KNEE_SEED=1107`，OUT `benchmarks/data/context-knee-20261107-p2`）+ watch 循环；首起 FAIL_READY 由 READY_RETRY 自动重试成功，13:10 READY，五臂 13:11–22:15 各 104–113 min 全 ok、单租约无换租约、22:15 停机确认，本批 62.4 units。analyst 复算（新脚本 `benchmarks/context_knee/analyze_p2.py` V 节 + `test_context_knee_analyze_p2.py`）→ reviewer 一审 2 Major（「≥20 点」门槛按最近秩分位实为尾概率 8.9% / 26.7%；§1.5 引用的两段重抽 CI 偏宽）+ 7 Minor 全修 → 主会话二审逐行读统计辅助函数。结论见 ANALYSIS p2 §1：**seed 生效且推理确定**（同输入臂 L0 两两 60/60 逐字相同、同臂不同 rep 0/150），但共同随机数 + L0 无填充 + prod 生效 catalog ⇒ L0 只有 {none,w1,w1w2,catalog} 与 {full} 两份独立样本，catalog 臂整臂是 w1w2 的复制（白跑 109 min）；噪声门槛改为拆分零分布尾概率口径：同档 10 题 × 2 rep 成功率 ≥ 25 点、Pass^2 ≥ 30 点（P = 2.2%）才超噪声；catalog 对 full 全档噪声内（D44 (iii) 维持）；P2 门槛 none L0 53%（6 rep，按题 CI [28,77]）点估计未达，**根因是 lc-hard-004 / 008 在编排器路径多走约 5 步（3 步写后回读验证）撞步数预算**，直调参照 8 / 7 步通过——不是题坏（§1.9）。待拍板三条与建议见 ANALYSIS §3（维持 70% 门槛 + 修冗余回读；去 catalog 臂、L0 加 rep 只在 none + full）。残项：harness prod 前缀元数据按 full 计（`run_knee.build_prefixes`）、`analyze.paired_units` 成功率区间未按题配对、run_colab.sh 停机前未抓 VM 日志（本批服务端 seed 告警无法核）。
- 11-07 深夜：用户拍板 ANALYSIS p2 §3 三条全按建议（原话「按建议」）：① P2 门槛维持「无管理臂 L0 ≥ 70%」不改相对判法，修编排器写后冗余回读（会话 orch-reread 已开，分支 claude/friendly-feistel-8d8e3e）；② D44 (iii) 维持 catalog 缺省，L0 判清随下一租约带 12 rep；③ 下一批去掉 catalog 臂、L0 加 rep 只在 none + full。为 ③ 给 run_colab.sh 加 `LEVEL_REPS_ARMS`（点名的 knee 步才带 --level-reps，点名须在本批 knee 步里否则 64 不起机；selftest +level_reps_arms）。
  - **下次租约起机单（修订）**：等 orch-reread 修复合入后起：`ARMS=orch-turn-none,orch-turn-w1,orch-turn-w1w2,orch-turn-full LEVEL_REPS=0=6 LEVEL_REPS_ARMS=orch-turn-none,orch-turn-full KNEE_SEED=1107`（四臂 ≈ 90 min × 4 + L0 加 rep ≈ 20 min × 2 ≈ 6.7 h）；要判清 catalog−full 的 L0 则 `LEVEL_REPS=0=12`（再 +40 min）。分析用 analyze_p2.py 口径（尾概率门槛、按题配对区间）；先报 L0 跨臂逐字相同率。
- 11-07（P2 批 ANALYSIS §2 两缺陷修复，同一提交，不推送）：(1) `--prefix prod` 行内前缀口径随生效披露：新增 `prefix.decl_mode(mode, tool_schema_mode)`，`run_knee.build_prefixes` 接 orch_meta 的 `tool_schema_mode_effective`（直调 None）——生效 catalog 时 prod 的装配与 prefix_base_tokens / prefix_n_tools / prefix_core_skipped / prefix_shadowed 按 catalog 算；prefix_mode 仍记字面值 prod（续跑比对不变）；送入编排器的字节不变（桩集取 grammar_tools 的 physical 集，full / catalog 同集；披露仍由 Orchestrator 按生效值做）；full / catalog / routed / mock 与直调 prod 不变。护栏测试 `test_build_prefixes_prod_follows_effective_tool_schema_mode`（假方言按字符计：prod@catalog 四项逐项 = catalog、≠ full；prod@full = full；None = full）+ 端到端 `test_orchestrator_prod_dry_run_prefix_meta_matches_catalog_arm`（真 config Qwen 条目，--dry-run --orchestrator 的 prod 与 catalog 行 prefix_n_tools / core_skipped / shadowed 相等、行 tool_schema_mode_effective=catalog）；变异（decl_mode 写死回 `mode`）记 commit message。 一审（reviewer，0C/1M/4m）补：行与 summary 新增 `prefix_decl`（实际装配档），续跑比对同 --prefix 字面值下装配档不同即拒（旧编排器 prod 行缺键 = 按 full 计；`test_resume_rejects_prefix_decl_mismatch`），免得生效 catalog 条目的 prod 目录混进 full 与 catalog 两种 prefix_base_tokens（analyze F 节「不唯一取较大值」会取旧 full 值）；e2e 另加 `test_build_prefixes_prod_meta_matches_orchestrator_declaration`：prod 的 prefix_n_tools 件数 = 编排器首次生成实发声明件数（不只 harness 两档互比）；起步行打印「（装配 catalog）」。P2 批档案里 prod 行的 13757 / 60 不改写（档案按生成时代码留档，ANALYSIS §2 已注明实发为 catalog；下批起行值自动正确）。
  (2) `analyze.paired_units` 成功率区间改为只按题重抽（题内 rep 原样，与 analyze_p2.paired_item 同口径；Pass^2 路径的 RNG 消耗序与浮点算式逐项不变）。重算核验（脚本 scratch，按表头逐格比对）：d44（`analyze_d44 --prev b2`）与 b2（`analyze_orch --prev 11-02 上午批 + 两 extra-ref`）的 summary_by_level.csv 变前变后**逐字节相同**；ANALYSIS 自动段变动行 d44 80 / b2 96，其中 1 行复跑命令、1 行表头文案（「两段」→「题级 bootstrap」），其余全部落在「成功率配对差 / 成功率差 / U3 成功率 差」列或散文「成功率 ±x [a,b]」，**Pass^2 配对差列与 Pass^2 散文区间 0 变动**；点估计不变，区间普遍收窄 0–10 个百分点。含 0 判定翻转 5 处（d44：w1w2 L32768 成功率差 [-65,+0]→[-65,-5]、catalog L32768 [-70,+0]→[-65,-5]、E 节 none→w1 L0 [+0,+50]→[+5,+45]；b2：E 节 none@am→none L0 [+0,+50]→[+5,+45]、w1@am→w1 合并 [-2,+14]→[+1,+9]），都是原本贴着 0 的边界，不改两批已写的结论（b2 / d44 结论按 Pass^2 与「成功率不可判」口径下的）。档案内 ANALYSIS.md 不重写（留档按生成时代码；csv 本就相同）；留档里嵌的「_复跑」命令现在给出的成功率区间比留档文本窄，已在 paired_units 文档说明（一审 m3）。题级聚类 bootstrap 对种子透传之前的批同样是正确单位，种子透传只是让重复计入暴露出来（一审纠正理由表述）。变前 → 变后各 3 行（C 节「成功率配对差」列，点差不变）：
  - d44 qwen-current L16384：-10 [-30,+0] → -10 [-25,+0]；d44 orch-turn-none L32768：-5 [-35,+25] → -5 [-25,+15]；d44 orch-turn-w1 L32768：-20 [-60,+15] → -20 [-55,+15]
  - b2 qwen-current L16384：-10 [-30,+0] → -10 [-25,+0]；b2 orch-turn-none L32768：-30 [-65,+5] → -30 [-60,+0]；b2 orch-turn-none L49152：-40 [-70,-10] → -40 [-70,-15]
  - 测试：`test_success_rate_bootstrap_item_level_only`（相同数据零宽、一题 0→1 区间恰 [0,1]、多档题内 rep 不重抽）；旧 `test_pass_hat_bootstrap_item_level_centered` 不动。P2 批（`analyze_p2.py`，ac78ab28）已自带 paired_item，下批分析脚本可改回直接用 analyze.paired_units。
- 11-07 深夜（P2 批 lc-hard-004/008 恒败根因修复，side session；规格 docs/plans/2026-11-07-verify-nudge-reread-spec.md）：scout 逐 run 核转录（none 臂两题 L0 各 6 rep）→ 回读每 run 是 **4** 次不是 3（已写文件逐个 + pending-changes.md / count.txt），另每 run 第 1 步固定一次失败的 run_shell_command（exit 127，系统提示 "prefer the terminal" 诱导，沙箱无 shell）；004 = 8+1+4 = 13、008 = 7+1+4 = 12，只去回读即 9 ≤ 11、8 ≤ 10 两题可过。**根因**：7/12 run 回读紧跟编排器 T1-4 写后验证门控推回，推回文本逐字要求 "re-read what you changed"（`orchestrator.py` 推回块；回读前思考原文 "The guardrail is asking me to verify… Let me re-read"，同 run 更早曾判 "The tool results already confirm success" 被门控推翻）；4/12 run 模型自建计划含 "Verify final contents" 步（系统提示 `session.py:134` "After editing, verify: re-read the changed section"；直调用的极简提示无此句从不回读）；1/12 自发。回执信息量**不是**原因（两模式同一 mock env 回执逐字相同；生产 edit_file/write_file 回执更详细）；题面不要求回读。**改法**：(D1) 门控武装判据收窄为「成功写到非纯内容文件」——`.md/.txt/.log/.csv/.tsv/.rst` 的成功回执即验证、失败写与纯内容写中性（既不武装也不清标记）、`requirements*/constraints*.txt`/`CMakeLists.txt` 例外仍按代码；抽成 `_arms_verify_gate` 单一入口，主循环与审批续跑共用（此前续跑是独立判据）；(D2) 推回文本改写：明说回执已证明落盘、别回读、先问有无真检查可跑、验证的是效果；(D3) 系统提示那句同向改写（快照按流程重生成，只变 1 行）；(D4) 计划完成提醒同向改写；harness 行加口径键 `verify_gate_rule="prose-neutral-v1"` 恒比（旧批续跑 / 追加即拒）。一审 0C/1M/8m/5n：Major 是 M-L 变异退化为等价变异（`_blocked` 观察都带 `_failed`）改到续跑调用点注入；m2/m3/m5/m7/m8 各补一测、m4 加 .txt 例外、m6 记档（harness mock 写失败回 `{"error":…}` JSON，`_obs_failed` 判不出，失败写代码类文件在 harness 仍武装，004/008 不受影响）。**golden 证据**：knee `test_prose_writes_do_not_trigger_verify_nudge_reread`（lc-hard-004 原题，EACCES + 2 edit + 1 write 后直接收尾被接受，steps 8 / gens 9，回读分支未被消费；集合置空即红）+ lifecycles 6 测（/a.py 仍推回且文案禁回读、notes.md 不推回、失败写只吃失败吞没、app.py→README.md 仍推回、requirements-dev.txt 仍推回、批准续跑 notes.md 不推回）。变异 m1–m8 全红（verifier 串行、逐字节恢复核对）；项目自带的检索护栏变异 harness `run_retrieval_guard_mutations.py` 的 M-L 首跑**存活**——先疑 fixture：`test_retrieval_evidence_guard.py` 的 `VERIFY_NOTE` 是旧推回文案 "never re-checked the result"，D2 改写后断言空置；改成新文案稳定前缀 "You modified state this turn" 后 M-L 被杀、harness 20/20。全量 4369 passed / 71 skipped（18m35s，负载下）；提交前另跑 `./devctl.sh check` 四门（钩子 GIT_DIR 泄漏期间手动跑）。
  - **真模型复测待下一租约**：`ARMS=orch-turn-none,orch-turn-full LEVEL_REPS=0=6 KNEE_SEED=1107`，看 004/008 L0 步数众数是否降到 ≤ 9 / ≤ 8、none 臂 L0 是否 ≥ 70%（P2 门槛 (a)）；跑前确认 deploy 拉到的 commit 含本修复，且 OUT 用新目录（旧批行缺 `verify_gate_rule`，`--resume` 会拒）。
  - 残项：每 run 首步的 run_shell_command exit 127 是 harness 沙箱形态问题（系统提示 "prefer the terminal" vs 无 shell），本波不动。
  - 残项（一审 n9）：续跑路径 `wrote_unverified` 用 `any()` 不看顺序，主循环是顺序状态机；既有问题。
- 11-08（合并，a3b8ecb4，已推送）：两条 side-session 分支 09c084f1（回读措辞修复，verify_gate_rule="prose-neutral-v1"）与 2d3fd99c（prefix_decl + paired_units 只按题重抽）并入 main；冲突两处（test_context_knee_orchestrator.py 尾部追加 / 本节两条 11-07 条目）均两边全留；run_knee.py 8 个自动合并 hunk 主会话逐个核读；合并树上 knee 三组 golden 186 过、钩子全量 check 过。**下一租约**：新 OUT 目录（verify_gate_rule 续跑键），`ARMS=orch-turn-none,orch-turn-w1,orch-turn-w1w2,orch-turn-full LEVEL_REPS=0=6 LEVEL_REPS_ARMS=orch-turn-none,orch-turn-full KNEE_SEED=1107`（≈6.7 h；要解 catalog−full L0 用 LEVEL_REPS=0=12 +≈40 min），等用户起机令。
- 11-08 晚（P2b 复测批 `benchmarks/data/context-knee-20261108-p2b/`，12:30 起机 → 19:12 停机，6.71 h，44.7 unit，四臂全 ok）：**004/008 修复生效**（none L0 步数众数 13/12 → 8/8，过 0/6 → 6/6、4/6；verify_nudge 全批 255 run → 6 run）；**P2 门 none L0 44/60 = 73.3%（题聚类 CI [55,90]）对 p2 53%**，同种子配对 +20 点、分叉对 15:3 p=0.01；full 对 none 全档噪声内、w1w2−full 三填充档 −11.7 [−23.3,0] 维持 D44 (iii)；none Pass^2 拐点 32768（L0 抬高所致，填充档绝对值高于 p2），w1/w1w2 49152、full 32768 与 p2 同。analyze_p2.py 泛化（catalog 臂可缺、`--p2` 同种子对照 W 节；重跑 p2 V 节数字逐项同）。建议与残项见该目录 ANALYSIS.md §3（P2 门按点估过线记、自发回读 9/60 不再修、57344 档首生成前 fold 记 DEFERRED、W1/W2 评价批若做把 rep 加在填充档）。**待用户拍板**：接受 P2 出口条件满足并进入 W1/W2 评价阶段，还是再起 `LEVEL_REPS=0=12` 收区间。
- 11-08 晚（用户拍板）：**P2 门按 73.3% 点估过线，进入 W1/W2 评价阶段**。同时改正 p2b ANALYSIS §3 第 4 条：按 §2 验收口径看，p2b 与 p2 两批管理臂（w1/w1w2）在填充档都没赢过无管理（none → w1w2 成功率 49K −30 [−55,−5]，合并 −12 [−24,0]，Pass^2 合并 −12 [−22,−2]），49K 疑似有害，单档 2 rep 门槛 30 点尚不能判死。W1/W2 评价批设计：在 p2b 的 none/w1w2 产物上同种子续跑，32K/49K/57K 各补到 6 rep（只跑 rep 2–5，240 run，估 5–6 h），判据 §2 三条；判「不优于」则回到 W1 落刀 / W2 触发点 / 57K pre-fold 设计。**起机单（scout 核实 run_colab.sh:504/513 只看 transcripts.jsonl 决定 --resume；run_knee.py:899-903 L0 缺省 2 rep 全在 done 集不会多跑；check_resume_compat 全部键与 p2b 行一致，seed 须给 1107；guardrail_events.jsonl 行号游标与 sessions/ 由分析引用，故整臂目录拷贝）**：`N=benchmarks/data/context-knee-20261108-w12eval; mkdir -p $N; cp -R benchmarks/data/context-knee-20261108-p2b/orch-turn-none benchmarks/data/context-knee-20261108-p2b/orch-turn-w1w2 $N/; OUT=$N ARMS=orch-turn-none,orch-turn-w1w2 LEVEL_REPS=32768=6,49152=6,57344=6 LEVEL_REPS_ARMS=orch-turn-none,orch-turn-w1w2 KNEE_SEED=1107 benchmarks/context_knee/run_colab.sh`；240 run，按 p2b V5 中位估 5.3 h + READY/冒烟 ≈ 5.7 h，单租约（MAX_VM_HOURS=10 不触发换租约）。冒烟与 p2b 的 smoke 同输入（L0 lc-hard-001 r0 种子 1107000），逐字比对即算力端漂移探针。不带 orch-turn-w1（再加 2.8 h 贴近 10 h 预算；w1 与 w1w2 在 p2b 填充档 49K/57K 同为 25%/35%，先判整套管理栈，W1/W2 归因留第二步）。分析：analyze_p2.py --data $N --p2 p2b 目录（W 节同种子对 p2b 自身：rep 0–1 应逐字相同，等于一次零成本一致性检查）；判据 §2 三条按 m=3 零分布门槛。summary.json 的 none level_reps["0"]=2 与实际 6 rep 外观不一致，分析不读它。起机等用户令。
- 11-09 凌晨（W1/W2 评价批 `benchmarks/data/context-knee-20261108-w12eval/`，p2b 的 none / w1w2 整臂拷贝同种子续跑补 32K/49K/57K rep 2–5；21:10 首次 A100 申请 2 min TimeoutExpired 未分配未计费、`stop --confirm-unallocated` 清租约后 21:14 重起，21:30 READY，03:15 停机，6.02 h，40.2 unit，两臂 ok）：**W1/W2 管理栈（现行 D1/D2）在拐点以上「不优于」无管理，P2 出口未达。** none 拐点 32768（Pass^2 60% → 20%）。w1w2 − none 按题配对：成功率三档合并 −4.4 [−11.7,+3.3]（比 0.90 [0.77,1.10]），Pass^2 −6.4 [−14.0,+0.7]（比 0.75 [0.56,1.04]）；49K −11.7 / −10.0 [−18,−2]，57K −6.7 / −12.7；全部在 m=3 零分布门槛内（尾概率 ≥25%）。p2b 的 49K −30 在新 rep 2–5 只剩 −2.5——主要是 2 rep 噪声（两段 rep 分属两台 VM，不能排除 VM 差异），「49K 疑似有害」撤回。§2 三条：第一条点估不满足、区间不排除；第二条按设计不可满足（soft 24,576 > 0.7×32,768，0/180 过线）= D42 另一半；第三条满足（Σwall −7.1%）。机制证据：57K 档 w1w2 60/60 首生成前 fold 到中位 27,158 tok（none 57,554）而 Pass^2 26.0 → 13.3，49K hard 首生成前触发 60/60；32K 只有 soft 级 step_compaction，+5.0 / +3.3 中性偏正——fold 在 65K 窗内净负，价值只剩防溢出。测量口径：跨 VM 逐字复现不再成立（冒烟 turn 3 分叉；同 VM 32K 两臂首轮 33/40 turn 0 分叉），同种子续跑不能当漂移探针。分辨力：10 题 × 6 rep 比值区间宽 ±0.17，判不出 10% 非劣效线。analyze_p2.py 加 `--two-arm`（缺 w1/full 只要求 none + w1w2；缺省路径 p2b 重跑逐字节相同）；证据脚本 `w12eval_evidence.py`。一审 0C/0M/8m（Q 节判序 KeyError、V5 拷贝续跑口径、W 节写死 p2b/p2 标签改目录名派生、V4 门槛超支撑格误写噪声内、--two-arm 缺测试、证据脚本 rep 切分 / 拐点档集合 / knee 缺失、措辞）全部修补，p2b 四臂路径回归逐字节不变。**待拍板**：D42 续项（§2 第二条改防溢出口径 / D1/D2 不回填）→ 设计修订后只复测 49K/57K（hard 提到 ceiling 附近或关 hard 级 step_compaction；240 run ≈ 5.7 h ≈ 40 unit）；加 16K/24K 定拐点不建议。详见 ANALYSIS.md §1 / §2 / §3。Tailscale 过期节点 agent-compute 11-09 已自动消失。
