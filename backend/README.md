# backend —— Agent 主体服务

> [Agent](../README.md) monorepo 的核心服务。Apple Silicon 本地多模态 AI Agent 服务，默认以 **MLX** 后端在本进程内推理。以 FastAPI 对外提供 HTTP 接口，内置 ReAct 多轮推理引擎、MCP 风格工具插件系统、KV Cache 前缀复用与会话分支能力，面向单用户、轻量并发场景。

## 特性

- **纯本地推理（后端可插拔）**：推理面收敛在 `InferenceBackend` 协议（`core/backend_protocol.py`）后，编排/记忆/工具一律不认识 MLX。`inference.mode` 决定承载——`inprocess` 用 `backends/mlx/` 在本进程加载多模态模型（默认 `mlx-community/gemma-4-e4b-it-4bit`，文本 / 图像原生输入），`remote` 用 `backends/remote/` 把生成托给另一台机器的 inference-server（同一协议两个实现，见根 [HETERO_COMPUTE.md](../HETERO_COMPUTE.md)）。无论哪种，数据都不出自己的机器
- **摄入解析层（多模态分路）**：文本 + 图像原生直喂模型；其余模态在进模型前的 worker 之外急解析——音频经 mlx-whisper 转写、PDF/办公文档折成带 provenance 标记的文本、扫描型 PDF 渲染成图走原生视觉、代码/大文件懒登记（只给路径不倾倒正文）；解析结果烘进历史日志、重放不重解析；模型可用 `file_extract` 逃生舱按 path 回源取精确内容（PDF 某页 / 音频某时段 / 代码某行区间）
- **编排循环（多步任务 Agent）**：护栏是代码、计划是数据——模型通过结构化元工具（`update_plan` / `complete_step`）制定并推进多步计划，确定性护栏管预算（单步自纠上限 / 总迭代熔断 / 重规划上限）、失败路由（重试 / 改计划 / 降级 / 问用户兜底）与死循环拦截（同名同参调用直接打回）；任务可跨轮存续并随冷冻落盘恢复
- **保持集中的推理约束**：借鉴大厂 agent loop，把「让模型专注当前步、少发散」的约束落到代码——**结构性提醒**（步骤边界 / 漂移 / 收尾验证的即时 nudge 贴到最新观察旁）、**完成门控**（计划未完成却提前吐文本收尾则有界推回，堵住「一段散文绕过整个计划」）、**输出截断恢复**（撞 `max_tokens` 先升档重试同一步、再多轮「从断点接着写」，消除半截答复→下轮道歉复述）；每处恢复分支均有界并带命名 transition，杜绝新增分支造成死循环；这些一次性护栏提醒走**可变尾部注入**（用完即弃、不进历史、不被摘要压缩污染）
- **单 worker 任务调度**：Apple Silicon 带宽受限、并行生成互抢带宽——全局单 worker 任务级门闸，一个任务独占跑完、其余排队，`GET /tasks` 全局可见；调度策略可插拔（`SchedulingPolicy`，预留自动调度插口）
- **任务挂起 / 恢复（单核多任务轮换）**：`POST /task/suspend` 在迭代边界安全让出 worker（KV 留内存待恢复），`POST /task/resume` 从 `task_state` + 已提交历史续跑，像单核 CPU 那样在任务间协作式轮换
- **上下文压缩（有界记忆）**：机械层做**预算感知 + 取代感知**的微压缩（借鉴大厂 microcompaction）——同一只读工具对同一目标的重复读取，较旧那条被后续读取取代（无夹写才取代，GUI「读树→点击→再读树」的两次树都保留）；全部观察越预算才在旧区按体量驱逐（大者先压、小者即使旧也留），未越则旧观察保持原文。再上一层：上下文越阈值把旧轮折叠进滚动摘要（方案A：`[system]+[摘要]+[尾部]`，全量日志始终保留），双阈值触发——活跃期高阈值兜底 + 空闲/冻结前低阈值优化，摘要任务走调度器低优先级不扰活跃任务
- **长期记忆沉淀**：被折叠掉的对话原文沉淀进 RAG `agent_memory` 类目（压缩的无损承重网，fail-open），日后经 `rag_search` 召回早前交互
- **联网调查（受限沙箱知识扩充）**：主体只调 `web_research` 一个工具、只拿回交叉验证过的事实摘要；搜索/抓取/原始网页全封在受限沙箱内（复用主模型权重、KV 隔离，仅有 `web_search`/`open_pages` 两个脏工具，物理上不进主体上下文）——注入即便劫持沙箱也够不到任何危险工具（无行为劫持），最坏只能返回被污染文本，故摘要仍以「不可信资料」边界交回主体。反投毒落到代码：删隐藏元素/脚本/零宽字符、标记祈使注入、域名信誉分交叉质证、SSRF 硬拒内网/环回/云元数据。搜索层用本机 SearXNG（数据不出门），离线则该工具自动不注册
- **进度可视与真取消**：`GET /task` 轮询计划推进进度；`POST /task/cancel` 在 token 级检查点停止生成并整轮回滚（非仅中断等待）
- **事件账本（协议 v1，根 `PROTOCOL.md`）**：轮次是可寻址资源——`POST /turns` 受理即返（202），编排过程"先记账后分发"（`core/journal.py`：活跃轮内存环 + journal.jsonl 里程碑落盘），`GET /events?after=seq` 断线任意重连、重放后续播，`GET /turn` 即查进行中轮次的完整可恢复状态（含待审批工具）；轮次生命周期唯一属主为 `core/turn_runner.py`，旧 SSE/阻塞对话端点全部退化为其薄适配器（旧事件契约兼容至 Phase 4，蓝图见根 `REARCHITECTURE.md`）
- **MCP 工具插件**：扫描 `framework/mcp/tools/` 自动注册 `BaseTool` 子类；注册前探活，依赖服务不可用的工具自动舍弃
- **桌面任务工具集**：三层递进——终端命令（`run_shell_command`，主力）→ AppleScript/JXA 应用级自动化（`run_applescript`）→ Accessibility GUI 操作兜底（读 UI 树 / 点击 / 键入 / 快捷键），配合按调用动态审批
- **KV Cache 前缀复用**：多轮对话仅预填充新增 token，实测续轮 prefill 提速近 10 倍
- **会话冷热管理**：闲置会话自动冻结释放内存（默认 30 分钟），再次访问时从磁盘恢复
- **会话分支**：从任意历史节点复制对话窗口，复用共同的 KV Cache 与历史文本，多模态文件硬链接零拷贝共享
- **并发安全**：引擎级推理锁 + 会话级锁，推理串行执行的同时事件循环保持响应，多会话请求可安全并发受理；单轮内连续的**只读工具**（RAG 检索 / 文件解析 / 联网调查 / 读 UI 树等，`is_concurrency_safe=True`）批量并发执行，有副作用的工具（shell / GUI 变更 / 写库）严格串行
- **音频转写 / 智能切片**：默认 mlx-whisper（large-v3-turbo）转写为文本注入；原生音频模型（声明 `modalities` 含 `audio`）则回退 Silero VAD 按语音活动切片直喂

## 环境要求

- Apple Silicon Mac（M 系列芯片）
- Python 3.11 ~ 3.13

## 快速开始

本服务是 monorepo 的一部分，统一由仓库根的启动器管理：

```bash
../devctl.sh setup            # 首次部署（建 .venv + 装依赖）
../devctl.sh start backend    # 仅启动本服务（要求 RAG 已就绪）；start 缺省启动全部
../devctl.sh model           # 从 inference.model_registry 选模型（钉死 revision）→ 写 .model-selection
```

单独调试也可直接 `.venv/bin/python app.py`。
模型解析顺序：`.model-selection` 指针 → `inference.model` → 本地绝对/相对路径 → `models/<模型名>/`
→ 从 Hugging Face Hub 自动下载（`utils/path_resolver.py`）。
`inference.mode: remote` 时本服务**不加载权重**，只拉分词器（`scripts/fetch_model.py --tokenizer-only`，
实测 31MB vs 全量 5.2GB）——分词器仍需本地，因为提示词组装与 token 计数在本进程。

### 配置（仓库根 config.yaml）

配置统一在**仓库根**的 `config.yaml`（backend 与 rag 共读一份，寻址见
`framework/utils/config_loader.py`）。本服务读取的段落如下：

```yaml
inference:
  host: "127.0.0.1"         # 仅本机回环，外部访问统一走前端的 /api 代理
  port: 14159
  mode: "inprocess"         # inprocess=本进程加载 backend 所指后端（默认）；remote=连 inference-server
  backend: "mlx"            # mlx | cuda(A1 后)：进程内所用/服务端内包的后端
  remote:                   # 仅 mode=remote 时用（密钥走 INFER_API_KEY，两侧同值）
    host: "127.0.0.1"       # inference-server 地址；跨机部署改为算力机的 Tailscale 地址
    port: 14161
    cache_ttl_minutes: 45   # 服务端缓存注册表空闲回收（略长于本侧 30min 冻结，只兜底）
  model: "mlx-community/gemma-4-e4b-it-4bit"
  model_revision: "deb1db71…"    # 钉死 HF commit：上游重传与 mlx-vlm 0.5.0 不兼容，勿改指 main
  model_registry: [...]     # `devctl model` 的候选集（各项钉死 revision + 显存/验证等级备注）
  audio_max_length: 29      # 原生音频 VAD 切片的最大时长（秒），仅原生音频能力启用时用
  modalities: ["text", "image"]  # 模型原生模态：含 "audio" 才原生直喂音频，否则一律 ASR 转写

rag:
  host: "127.0.0.1"         # rag_search 工具依赖的 RAG 服务地址（分离部署时为设备 A 的地址）
  port: 14160               # 密钥走 RAG_API_KEY，两侧同值

ingestion:                  # 摄入解析层：非原生模态进模型前的急解析（worker 之外）
  asr_model: "mlx-community/whisper-large-v3-turbo"  # 音频转写引擎（mlx-whisper，懒加载）
  asr_language: null        # null=自动检测语种；也可固定如 "ja"/"zh"/"en"
  pdf_text_min_chars_per_page: 50  # PDF 每页平均字符低于此判为扫描型→渲染成图走原生视觉
  pdf_render_max_pages: 20  # 图像型 PDF 最多渲染页数（其余用 file_extract 抽文本）
  parse_max_chars: 12000    # 单文件解析文本注入上限（超出截断，余量用 file_extract 回源）
  text_inline_max_chars: 4000  # 纯文本内联上限，超出转懒登记；代码一律懒登记不内联

web_search:                 # 联网调查（受限沙箱）：需本机 SearXNG，离线则 web_research 不注册
  searxng_url: "http://127.0.0.1:8080"  # 本机 SearXNG（须在 settings.yml 开启 json 输出）
  timeout: 15               # 单次搜索/抓取的 HTTP 超时（秒）
  max_results: 8            # web_search 返回候选上限
  max_pages_per_call: 3     # 单次 open_pages 最多抓取页数
  per_page_max_chars: 2500  # 每页正文注入沙箱上下文的字符上限
  max_steps: 6              # 沙箱 ReAct 循环步数上限
  total_timeout: 110        # web_research 整体墙钟熔断（秒）；须 < agent.tool_timeout_seconds
  engines: []               # 可选：限定 SearXNG 引擎，空=用默认
  content_farms: []         # 域名黑名单（内容农场，信誉分压到最低）
  # 另有抓取升级阶梯（impersonate_enabled / render_enabled 等），见 config.yaml 内注释

server:                     # HTTP 鉴权与跨域：真 key 经 AGENT_API_KEY 环境变量注入，不进仓库
  api_key: ""               # 留空=未启用（启动告警后放行）；设了即强制 Bearer 校验
  allowed_origins:          # CORS 白名单（仅浏览器直连 14159 时生效）
    - "http://localhost:5173"

auth:                       # 多设备票据（§7 预留）：留空 = 单密钥模式，行为与预留前逐字相同
  devices: {}               # 例：{ mac-studio: "<token>", iphone-15: "<token>" }——一台一票，
                            # 吊销一台不牵连其余；与 server.api_key 可并存（迁移期）

agent:                      # 编排循环护栏（多步任务）
  tool_schema_mode: "full"  # full=全部工具 schema 常驻；catalog=目录常驻+按名取详情（工具规模大后切换）
  react_max_steps: 4        # 单步自纠预算：连续 N 次迭代未完成即判定失败并路由
  max_total_iterations: 40  # 一轮任务的总推理次数熔断
  replan_max: 3             # 推翻已有计划的次数上限
  observation_max_chars: 4000  # 单条工具观察截断上限（保护每步延迟与上下文）
  tool_timeout_seconds: 180 # 单次工具执行熔断：超时放弃等待并反馈模型改路
  max_parse_retries: 3      # 工具格式反复解析失败的断环阈值：同一坏调用重复≥此值即优雅终止

  # 上下文压缩（机械式：预算感知 + 取代感知）：同工具同目标重复读取取代旧条（无夹写才取代），
  # 全部观察越预算才在旧区按体量驱逐，未越则保持原文。
  observation_keep_recent: 6      # 最近多少条观察为硬保护窗口（永不改动，保任务内 KV 复用）
  observation_compact_chars: 200  # 压缩后每条观察保留的首部字符数
  observation_budget_chars: 20000 # 机械层观察总预算（字符）：越过才驱逐旧大观察，保守偏大

  # 摘要式二次压缩（方案A）：越阈值把旧轮折叠进 memory.json 滚动摘要，全量日志保留，
  # 上下文重建为 [system]+[摘要]+[尾部]。高阈值=活跃期兜底、低阈值=空闲/冻结前优化。
  summary_high_threshold: 8000   # 活跃期上下文 prompt token 越过即折叠旧轮
  summary_low_threshold: 2000    # 空闲将冻结前上下文越过即折叠旧轮
  summary_keep_tail_turns: 4     # 折叠时尾部保留的最近轮次数（保持原文）
  summary_idle_seconds: 300      # 会话空闲超过此秒数即进入低阈值摘要维护（应 < 冻结超时 1800）

  # 长期记忆沉淀：折叠旧轮时把其原文沉淀进 RAG agent_memory 类目（无损承重网，可经 rag_search 召回）
  sediment_to_rag: true          # 关闭则只压缩不沉淀

schedules:                  # 定时任务：落盘一等实体，模型经 schedule_* 工具管理（创建/修改需审批）
  tick_seconds: 30          # 触发扫描周期（秒）
  run_history_keep: 50      # 每任务运行历史保留条数
  min_interval_minutes: 5   # interval 型下限（防模型排出高频轰炸）
```

> 以上仅列常用段。完整配置还包括推理加速（`inference.kv_bits` KV 量化 /
> `constrained_tool_calls` 工具段约束采样 / `speculative` prompt-lookup 投机采样）、
> 数学攻坚沙箱（`math_research`）、PPT 制作（`ppt`）等，各项含义见 `config.yaml` 内注释。

## API

服务启动后可访问 `http://localhost:14159/docs` 查看交互式文档。

### 对话 `POST /api/v1/chat/{session_id}`

`multipart/form-data`，会话不存在时自动创建：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `prompt` | str | 必填 | 用户输入 |
| `system` | str | `""` | 系统提示词（仅会话首次创建时生效） |
| `files` | file[] | - | 图片 / 音频 / PDF / 办公文档 / 代码文本，可多个（按模态自动分路解析） |
| `enable_thinking` | bool | `false` | 开启思考模式 |
| `thinking_budget` | int | `2048` | 思考 token 预算 |
| `max_tokens` | int | `4096` | 生成上限 |
| `temperature` | float | `0.2` | 采样温度 |
| `min_p` | float | `0.05` | min-p 采样 |

```bash
curl -X POST http://localhost:14159/api/v1/chat/my_session \
  -F 'prompt=这张图片是什么内容？' \
  -F 'files=@photo.png'
```

### 真流式对话 `POST /api/v1/chat/{session_id}/stream`

参数与非流式对话完全一致，响应为 **SSE 事件流**：`turn_start` → `token`（可见正文逐段）/
`thinking`（思考整块）/ `tool_started` / `tool_result` / `task_progress` → `turn_end`
（权威终态，含完整 response）。契约：token 只是临时预览，**前端一律以 `turn_end` 对账**
（终态可能是 success/warning/cancelled/fail/pending_approval/suspended）。客户端中途断开
不丢工作——轮次在后台照常跑完并落盘。前端默认走本接口，非流式接口为回退路径。

### 记忆状态 `GET /api/v1/chat/{session_id}/memory`

会话上下文压缩状态快照（只读，不触发会话唤醒）：当前 model-facing 上下文规模
（`last_prompt_tokens`）vs 折叠阈值、已折叠进滚动摘要的水位线与摘要体量、被折叠原文
是否沉淀进 RAG。供前端「记忆」面板可视化有界记忆。

### 定时任务 `GET/POST/DELETE /api/v1/schedules*`

创建/编辑走对话（模型的 `schedule_*` 工具 + 审批卡片）；这里只提供**不经过模型**的用户
直接控制：`GET /schedules` 清单、`POST /schedules/{id}/toggle` 启停、`POST /schedules/{id}/run`
立即运行、`POST /schedules/{id}/cancel` 中止本次运行、`DELETE /schedules/{id}` 删除、
`GET /schedules/{id}/runs` 运行历史（每条含专属会话 `session_id`，可跳转查看全文）。

### 会话资产 / Deck `GET /api/v1/files/{session_id}/{assets/{filename} | deck}`

出货 `render_chart` 生成的图表 PNG、`deck_export_pdf` 导出的 PDF（`assets/`，长缓存），
以及会话的 Deck Spec（`deck.json`，前端 SlideRenderer 的数据源）。路径双重白名单校验。

### UI 状态 `GET/PUT /api/v1/ui-state`

前端工作区/会话元数据的服务端化存储（不透明 JSON blob，last-write-wins，原子落盘），
换设备打开即恢复工作区结构。

### 任务进度 `GET /api/v1/chat/{session_id}/task`

多步任务进度快照（只读，不触发会话创建）：`active` 是否有进行中的计划、
`generating` 是否有轮次在推理中、`task` 为完整 TaskState（goal / plan 步骤状态 / 计数器）、
`activity` 为实时步骤流（`phase` 阶段 / `current` 当前动作 / `steps[]` 已完成步骤）。
前端在等待对话响应期间轮询本接口，把"黑盒等待"变成看得见的动作流（当前在思考/调哪个
工具/拿到什么结果），单工具任务（无多步计划）同样可见。活动状态仅存内存、按轮重置、不落盘。

### 取消生成 `POST /api/v1/chat/{session_id}/task/cancel`

真取消：置位取消标志，引擎在 token 段粒度、编排循环在迭代边界响应。
被取消的轮次**整轮回滚出内存记忆**（与引擎故障同语义，用户消息也不保留），
以 `status: "cancelled"` 落盘存档；对话接口会以 cancelled 状态正常返回。

### 全局任务视图 `GET /api/v1/tasks`

跨会话的单 worker 调度快照（只读）：`running` 当前占用 worker 的会话、`queued` / `suspended`
计数、`tasks[]` 每项含 `session_id` / `status`（running/queued/suspended）/ `kind` / 等待或运行时长。
供前端"任务切换器"与后续自动调度观测使用。

### 挂起 / 恢复任务 `POST /api/v1/chat/{session_id}/task/{suspend|resume}`

- `task/suspend`：挂起**正在占用 worker** 的任务（区别于取消）。编排循环在当前步骤完成后的
  安全点让出 worker，KV 与 `task_state` 依设计**常驻内存**，其余排队任务上位；仅对运行中任务有效。
- `task/resume`：恢复一个已挂起的任务，重新排队申请 worker 后从挂起处续跑（`run_turn` 可重入）。
  响应仅含本次续跑新增的消息；续跑再次挂起 / 触发审批则相应转入挂起 / 审批暂存。

挂起期间用户直接发新消息 = 改变方向、放弃该挂起任务（局部产出留在历史，以 `warning` 收尾落盘）。

### 工具清单 `GET /api/v1/tools`

返回已注册工具的完整明细（`name` / `description` / `parameters` / `requires_approval`），
供前端上下文面板展示模型当前可用的工具。

### 系统提示词 `GET /api/v1/chat/{session_id}/system`

只读查询会话的系统提示词，不触发会话创建。`source` 为 `session`（该会话在用/已落盘）
或 `default`（会话尚未创建，返回默认提示词）。

### 历史节点 `GET /api/v1/chat/{session_id}/history`

返回该会话的历史轮次列表，下标即节点位置，供前端展示与选取分支点。

### 删除最后一轮 `DELETE /api/v1/chat/{session_id}/turns/last`

删除会话的最后一轮问答，三方同步防止冷启动后不同步：

- **落盘**：截断 `history.jsonl` 最后一行，物理删除该轮的图片/音频文件
- **内存**：热会话按截断后的日志整体重建 history 与多模态引用
- **KV Cache**：保留不动，下次推理经前缀匹配自动裁剪对齐（与分支复用同一机制）
- 若该轮正等待敏感操作审批（尚未落盘），则撤销挂起状态并回滚内存

前端「更新最后一次发言」= 本接口 + 按新内容重新发送。

### 会话分支 `POST /api/v1/chat/{session_id}/branch`

从任意历史节点复制出新的对话窗口：

```bash
curl -X POST http://localhost:14159/api/v1/chat/my_session/branch \
  -H 'Content-Type: application/json' \
  -d '{"node_index": 2, "new_session_id": "my_fork"}'
```

- `node_index` 支持负索引（`-1` 表示最新节点）
- `new_session_id` 缺省时自动生成
- 父会话处于热态时克隆其 KV Cache（响应中 `kv_cache_reused: true`），分支首轮推理经前缀匹配自动对齐分支点；冷态则回退为冷启动预填充，正确性不受影响
- 分支自成目录并记录血缘（`meta.json`），删除父会话不影响分支

### 敏感操作审批 `POST /api/v1/chat/{session_id}/approval`

当模型调用了 `requires_approval=True` 的敏感工具（如删除知识库文档），对话接口会以
`status: "pending_approval"` 返回并挂起该轮（`pending_tools` 列出待授权的调用）。
前端经用户确认后调用本接口作出决议：

```bash
curl -X POST http://localhost:14159/api/v1/chat/my_session/approval \
  -H 'Content-Type: application/json' \
  -d '{"approve": true}'
```

- `approve: true` 执行挂起的工具调用并继续推理收尾；`false` 则工具不执行，由模型向用户解释
- 响应格式与对话接口一致，`response` 仅含决议后新增的消息
- 挂起期间用户直接发新消息视为放弃，该轮以 `warning` 收尾落盘；会话冻结/服务关闭时以 `fail` 作废

### 销毁会话 `DELETE /api/v1/chat/{session_id}`

释放内存并物理删除该会话目录。

## 架构

```
app.py                        # 装配入口：生命周期管理 + 依赖注入 + 绑定护栏
framework/
├── api/                      # 表现层：路由与响应模型
│   ├── routes.py
│   ├── schemas.py
│   └── auth.py               # Bearer 校验面（唯一比对点：共享密钥 + 多设备票据，常量时间）
├── core/                     # 领域层：编排循环 + 推理引擎 + 任务调度 + 事件账本
│   ├── backend_protocol.py   # ★ InferenceBackend 协议：推理面的唯一契约（MLX/远端/日后 CUDA 各自实现）
│   ├── orchestrator.py       # 外层编排循环：计划推进、失败路由、预算护栏、结构性提醒/完成门控/截断恢复、只读并发调度、元工具拦截、挂起检查点、上下文压缩钩子
│   ├── turn_runner.py        # 轮次生命周期的唯一属主（旧 SSE/阻塞端点退化为其薄适配器）
│   ├── journal.py            # 事件账本：活跃轮内存环 + journal.jsonl 里程碑落盘
│   ├── feed.py               # 事件分发：订阅者扇出 + 断线重连的 after=seq 重放
│   ├── events.py             # 事件类型定义（协议 v1，见根 PROTOCOL.md）
│   ├── scheduler.py          # 单 worker 任务调度器：任务级门闸、挂起/恢复、可插拔调度策略（自动调度插口）
│   ├── task_state.py         # 任务状态对象：计划(数据)、结构化操作校验、序列化
│   ├── engine.py             # 推理引擎：单次生成、取消检查点、推理互斥锁、上下文摘要器
│   ├── constrained.py        # 工具段约束采样
│   ├── stream_filter.py      # 流式输出过滤（思考/正文/工具段分流）
│   ├── prompt_builder.py     # Prompt 组装（前缀分层：稳定前缀→追加历史→可变任务块）
│   └── response_parser.py    # 模型输出解析（思考 / 文本 / 工具调用）
├── backends/                 # 推理后端实现（协议的两个实现，编排层不认识其中任何一个）
│   ├── mlx/                  # 进程内 MLX 后端
│   │   ├── backend.py        # 协议实现：生成、缓存句柄、取消
│   │   ├── asr.py            # mlx-whisper 转写
│   │   └── patch/            # 防腐层：隔离 MLX 底层补丁（gemma4 流式生成 + KV 前缀复用）
│   └── remote/backend.py     # 远端后端：连 inference-server（SSE 生成、媒体上传、缓存克隆）
├── mcp/                      # 插件层：MCP 风格工具系统
│   ├── base.py               # BaseTool 基类（含 is_available 探活契约、is_concurrency_safe 并发标志）
│   ├── registry.py           # 扫描、探活、注册与分发
│   └── tools/
│       ├── shell_exec.py     # 终端命令执行（动态审批：只读放行/变更弹窗）
│       ├── apple_script.py   # AppleScript/JXA 应用级自动化（恒审批）
│       ├── accessibility.py  # Accessibility GUI 工具（列应用/读UI树/点击/键入/快捷键）
│       ├── rag_search.py     # RAG 知识库检索工具
│       ├── rag_admin.py      # RAG 入库/分类治理/删除工具
│       ├── file_extract.py   # 逃生舱：按 path 回源重解析已摄入文件（PDF页/音频时段/代码行区间）
│       └── web_research.py    # 联网调查包壳（主体唯一可见的联网工具，内部跑受限沙箱、只回摘要）
├── agents/                   # 受限子沙箱（方案 b：物理隔离，脏工具/子工具不进插件目录）
│   ├── runtime.py            # 进程级引擎/事件循环句柄（供各沙箱复用主模型生成）
│   ├── web_tools.py          # 脏工具：SearXNG 检索 + 抓取 + scrub/反投毒 + SSRF + 信任边界
│   ├── research_sandbox.py   # 受限 ReAct 循环（仅 web_search/open_pages，KV 隔离），折成事实摘要
│   ├── math_sandbox.py       # 数学攻坚沙箱（离线 Python/SymPy，多步写码-执行-验证）
│   ├── math_tools.py         # 数学沙箱的子工具
│   ├── excel_sandbox.py      # 表格攻坚沙箱
│   ├── excel_tools.py        # 表格沙箱的子工具
│   └── chart_tools.py        # 图表渲染子工具
├── ingestion/                # 摄入解析层：非原生模态进模型前的急解析（worker 之外）
│   ├── router.py             # Ingestion：按 mime/扩展名分路，产出 images/ingested_text/attachments
│   └── parsers/
│       ├── asr.py            # 音频转写（mlx-whisper，懒加载、fail-open）
│       ├── pdf.py            # PDF：文本型抽文本 / 扫描型渲染成图走原生视觉（pymupdf）
│       ├── document.py       # 办公文档→markdown（markitdown）
│       └── text.py           # 纯文本/代码：内联 vs 懒登记判定、按行区间读取
├── memory/                   # 记忆层：会话管理 + 上下文压缩 + 长期记忆
│   ├── session.py            # 冷热切换、KV Cache 状态、分支派生、挂起暂存、摘要旁挂与"摘要+尾部"重建
│   ├── compactor.py          # 上下文压缩：机械式（预算驱逐 + 取代去重）+ 摘要式二次压缩（方案A）
│   └── rag_sink.py           # 长期记忆沉淀汇：折叠内容原文写入 RAG agent_memory 类目（fail-open）
├── ppt/                      # Deck（PPT）：Spec 模型、增删改、视觉路由、无头 Chrome 导出 PDF
├── excel/                    # 表格：重算、渲染、校验、工作簿 diff / inspect
├── schedule/                 # 定时任务：落盘存储（store）+ 触发扫描与无人值守执行（runner）
├── push/                     # 推送（§7 预留 #3，已实现）：ES256 JWT 直发 APNs + 设备注册 + 分发
└── utils/                    # 基础设施层
    ├── file_handler.py       # 图像落盘、原生音频 VAD 切片（供摄入层复用）
    ├── config_loader.py      # 仓库根 config.yaml 寻址与加载
    ├── model_selection.py    # .model-selection 指针解析（devctl model 的落点）
    └── path_resolver.py      # 模型路径解析 / HF 下载（含 remote 模式的 tokenizer-only 拉取）
```

### 会话存储

每个会话在 `sessions/<session_id>/` 下自成目录：

```
sessions/my_session/
├── system.json       # 系统提示词
├── history.jsonl     # 历史日志（每行一轮完整交互；fail/cancelled 轮次冷恢复时跳过）
├── meta.json         # 血缘元数据（parent_session_id / branch_point / created_at）
├── task.json         # 跨轮多步任务快照（任务进行中才存在，完成即删）
├── memory.json       # 摘要式压缩旁挂（方案A）：滚动摘要 + 已摘要行数水位线（有摘要才存在）
├── inflight.json     # 轮次预写标记（write-ahead，进行中才存在；进程死亡后启动恢复补 fail 轮）
├── image/            # 本会话上传的图片（含扫描型 PDF 渲染出的页面）
├── audio/            # 上传音频（默认转写用；原生音频能力下为切片后的分段）
├── attach/           # 原始非原生文件（音频/PDF/文档/代码），供 file_extract 回源与前端
├── assets/           # 工具产物（render_chart 图表 PNG、deck_export_pdf 导出的 PDF），经 /files 出货
└── deck/deck.json    # PPT Deck Spec（单一真相，前端实时投影；有 deck 才存在）
```

> **多模态摄入**：图像与文本型 PDF 渲染页走**原生**视觉；音频/文档/PDF文本等在进模型前于
> worker 之外**急解析**成带 provenance 标记的文本（`ingested_text` 字段，随轮次烘进
> `history.jsonl`，冷恢复原样回放、不重解析）；代码/大文件**懒登记**只给路径。模型需要精确
> 内容时用 `file_extract(path=…)` 逃生舱回源。原生音频仅在 `inference.modalities` 含
> `audio` 时启用（回退 VAD 切片直喂）。

> **上下文压缩与长期记忆**：`history.jsonl` 始终保留全量（前端展示 / 审计）；喂给模型的
> model-facing 上下文则受压缩约束——任务沉降后机械压缩旧工具观察，越阈值把旧轮折叠进
> `memory.json` 的滚动摘要并重建为 `[system]+[摘要]+[尾部]`（仅保 system 前缀 KV 复用）。
> 被折叠的原文同时沉淀进 RAG `agent_memory` 类目，作为无损承重网供 `rag_search` 召回。

## 内置工具

### 桌面任务工具

| 工具 | 功能 | 审批 |
|------|------|------|
| `run_shell_command` | 在本机执行终端命令（桌面任务主力）。只读命令（ls/cat/grep/git status…）自动放行，有副作用的命令（写文件/安装/sudo/管道重定向/未知命令）**按调用**触发弹窗确认 | 动态 |
| `run_applescript` | 执行 AppleScript / JXA 驱动 macOS 原生应用（邮件、日历、访达、Safari…），应用级操作优先于图形界面点击 | ✅ 恒审批 |
| `list_applications` | 列出运行中的 GUI 应用（名称/pid/前台标记），免 AX 权限 | - |
| `read_ui_tree` | 读取应用聚焦窗口的 Accessibility 树（剪枝的缩进文本，可交互元素带 `[编号]`），操作前后各读一次以感知/验证 | - |
| `click_ui_element` | 按 `[编号]` 点击（AXPress）UI 元素 | - |
| `set_ui_element_value` | 按 `[编号]` 给文本框等元素设值 | - |
| `type_text` | 向当前聚焦元素键入文本（Unicode 键盘合成） | - |
| `press_keys` | 发送组合键（`cmd+s`、`cmd+shift+t`、`return`…） | - |

工具优先级（也写入系统提示词）：**能终端就终端 → 应用有脚本接口就 AppleScript → 图形界面（AX）兜底**。
GUI 感知以 AX 树的文本序列化为主（原生 App 足够）；截图作为验证手段属后置项，需多模态注入管线，暂未接入。
审批是「按调用动态判定」：`BaseTool.needs_approval(arguments)` 按实参决定是否弹窗，
判定异常时 fail-closed（宁可多弹一次）。

### 文件解析工具

| 工具 | 功能 | 审批 |
|------|------|------|
| `file_extract` | 逃生舱：按 `path`（取自上下文 provenance 标记如 `[原文件：/abs/path]`）回源重解析已摄入文件——PDF 指定页（`page="N"`）、音频时间区间（`start`/`end` 秒）、代码/文本行区间（`start`/`end` 行）、文档全文。只读 | - |

摄入层默认只把文件「净荷」急解析成带路标的文本注入上下文（代码/大文件只登记路径），
模型需要被省略的精确/完整内容时用本工具按 path 回源。工具无状态，路径即 handle。

### 文件写入 / 编辑工具

| 工具 | 功能 | 审批 |
|------|------|------|
| `edit_file` | 精确文本替换（`old_string` 唯一命中才执行）——修改已有文件的**主通道**，小步多次、转义面小、审批卡片可读；自动剥离误拷入的行号前缀 | ✅ |
| `write_file` | 新建文件 / 彻底重写；覆盖已有文件前自动备份 `.bak`；原子写（tmp + replace） | ✅ |

> 把「改文件」从 shell heredoc 通道里解放出来：长内容经 JSON + shell 双层转义必崩，
> 且两者一律走审批闸门——定时任务无人值守时被自动拒绝，不新增免审批写盘面。

### 数学 / 图表 / PPT 工具

| 工具 | 功能 | 审批 |
|------|------|------|
| `run_math` | 单次 SymPy 计算（一条高层调用优先于手搓公式），结果程序化提取 | - |
| `math_research` | 数学攻坚子沙箱：离线 Python/SymPy 内多步「写码-执行-验证」，最终答案经 `submit_answer` 程序化提取（不经模型转写），一次交一个自足问题 | - |
| `excel_research` | 表格工程子沙箱：Python 内建/改 `.xlsx`/`.xlsm`，出货前过硬交付闸门（全公式真重算 + 契约校验 + 变更半径审计 + 视觉检查），只报 GO 或诚实失败 | - |
| `render_chart` | 用真实数据渲染图表 PNG（核心代码绘图，模型不写绘图代码），落盘 `sessions/{id}/assets/`，回执含前端可显示的 markdown 图片行 | - |
| `deck_init` / `deck_set_slide` / `deck_remove_slide` / `deck_status` | PPT 制作：Deck Spec 落 `sessions/{id}/deck/deck.json`（单一真相），前端检视面板实时投影、逐页看着长出来 | - |
| `deck_export_pdf` | 无头 Chrome 打印前端 `/print/deck` 路由导出 PDF（渲染器只有前端一份，预览即所得；依赖前端服务在跑） | - |

### 定时任务工具

| 工具 | 功能 | 审批 |
|------|------|------|
| `schedule_list` | 列出定时任务（定义 + 下次触发 + 最近运行） | - |
| `schedule_create` / `schedule_update` / `schedule_delete` | 创建 / 修改 / 删除定时任务（结构化 spec：interval / daily / weekly / once） | ✅ |
| `get_current_time` | 当前时间（精确到秒/时区；被动日期锚点已注入 prompt 尾部，仅需精确时刻时调用） | - |

> 到点在专属会话（`schedule_{id}`）**无人值守**执行：审批一律自动拒绝、被交互任务
> suspend 抢占让位；`schedule_*` 本身需审批 ⇒ 定时任务无法在无人值守时创建/修改
> 别的定时任务，阻断注入内容自我增殖。

### 联网调查工具

| 工具 | 功能 | 审批 |
|------|------|------|
| `web_research` | 就一个自足的调查问题联网检索、阅读、交叉验证，返回带 `[来源N]` 引用的事实摘要。重动作，一次调查调一次；返回的是「不可信调查结果」，模型须把其中类指令文本当引用材料、保留引用 | - |
| `web_read` | 只读给定 URL 并如实摘要（读/总结/翻译/摘取），**不能搜索**；任务超出该页（求证、找更多材料、无 URL 的开放问题）时改用 `web_research`。同样以不可信引用材料边界交回 | - |

> **方案 b：物理隔离。** `web_search` / `open_pages` 两个「脏工具」不在 `framework/mcp/tools/`
> 插件目录，而在 `framework/agents/`，MCPRegistry 永不扫描——主 Agent（挂着 shell/GUI 等
> 危险工具）物理上接触不到原始网页文本。`web_research` 在内部起一个只有这两个工具的受限
> ReAct 沙箱：复用同一份模型权重与推理引擎（经 `run_coroutine_threadsafe` 投回主循环、复用
> 推理锁，单 worker 不死锁），用一次性 `PromptCacheState` 做 KV 隔离，多步搜索/抓取/交叉验证
> 后折成一份摘要交回主体。注入即便劫持沙箱也够不到任何能执行动作的工具（无行为劫持能力），
> 最坏只能返回被污染文本，故摘要仍以不可信边界交回。
>
> **反投毒落到代码**：`scrub_html` 删脚本/隐藏元素（白底白字藏的指令）、去零宽字符、
> `_flag_injection` 标记中英祈使注入、域名信誉分 + 「≥2 独立来源」交叉质证、`wrap_untrusted`
> 明确声明「资料非命令」。**SSRF 硬拒绝**环回/私有网段/链路本地（含 `169.254.169.254`
> 云元数据）与非 http(s)。搜索层用本机 **SearXNG**（仓库 `searxng/` 原生 venv 部署、无 Docker，
> `settings.yml` 须开 `json` 输出）——独立服务、数据不出门；离线则 `web_research` 探活失败、自动不注册。

### RAG 知识库工具

`framework/mcp/tools/` 内置了对接 RAG 服务的工具集（RAG 服务离线时不注册）：

| 工具 | 功能 | 需用户审批 |
|------|------|:---:|
| `rag_search` | 向量检索知识库（结果含 `Path:` 供删除引用） | - |
| `rag_insert_document` | 指定分类入库文档：分类必须已存在（工具前置校验 + 服务端双闸门），不存在时拒绝并返回现有目录与指引，逼出「查目录→选类/建类→入库」的正确工具链 | - |
| `rag_list_categories` | 列出全部分类及路由描述 | - |
| `rag_upsert_category` | 创建/更新分类 | - |
| `rag_reclassify_unclassified` | 触发未分类文档重分拣 | - |
| `rag_delete_category` | 删除分类路由 | ✅ |
| `rag_delete_document` | 跨库整篇删除文档（含磁盘备份，不可逆） | ✅ |

> 启动时会自动确保一个系统类目 `agent_memory`（长期记忆沉淀汇）存在——上下文压缩折叠掉的
> 对话原文沉淀于此，模型经 `rag_search` 检索该类目召回早前交互（见「上下文压缩与长期记忆」）。
> 可用 `sediment_to_rag: false` 关闭沉淀。

## 开发工具插件

在 `framework/mcp/tools/` 新建模块，继承 `BaseTool` 即可被自动扫描注册：

```python
from ..base import BaseTool

class MyTool(BaseTool):
    name = "my_tool"
    description = "告诉 LLM 什么情况下调用这个工具"
    parameters = {
        "properties": {
            "query": {"type": "string", "description": "参数说明"}
        },
        "required": ["query"]
    }

    # 可选：标记为敏感操作后，引擎会挂起调用等待前端用户授权（见审批接口）
    requires_approval = False

    # 可选：只读/无副作用工具设 True，同批连续只读调用会被编排层并发执行（默认 False 串行）
    is_concurrency_safe = False

    def needs_approval(self, arguments: dict) -> bool:
        # 可选：按调用动态审批。默认返回 requires_approval（静态）；
        # 风险随参数变化的工具（如 shell 只读放行、写操作弹窗）覆写本方法
        return self.requires_approval

    def is_available(self) -> bool:
        # 可选：依赖外部服务时覆写探活逻辑，返回 False 则该工具不会注册
        return True

    def execute(self, query: str) -> str:
        return "执行结果（返回字符串，供 LLM 阅读）"
```

## 注意事项

- 工具探活只发生在服务启动时；若依赖的外部服务（如 RAG）在 Agent 运行期间才上线，需重启服务才能注册对应工具。同理，`rag_search` 的分类目录提示烘在启动时的 schema 里，运行期间新建的分类需经 `rag_list_categories` 现查（工具描述已注明）
- 请勿以 `reload=True` 方式启动，MLX 模型反复重载会导致严重内存泄漏
- **鉴权**：`/api/v1/*` 全量套 Bearer 校验（`AGENT_API_KEY` 环境变量优先，回落 `config.yaml` 的 `server.api_key`）；密钥为空视为未启用（启动告警后放行，便于首启/dev），生产务必设置。`/health` 免鉴权供探活。浏览器不持有密钥——dev 由 Vite 代理注入 `Authorization`，生产由 Nginx 注入。比对面收敛在 `api/auth.py` 的 `verify_token` 一个函数里（换设备票据/JWT/mTLS 只动它），另支持 `auth.devices` 多设备票据——留空即单密钥模式，与预留前行为逐字相同（`tests/test_auth.py` 锁死这一点）
- **绑定护栏**：绑定到非回环地址却没设密钥时**拒绝启动**（不是告警），在模型加载前失败。inference-server 与 rag 服务同此纪律。理由：改 host 与设密钥是两个独立动作，先改 host 后忘密钥，服务就对整个可达网络裸奔
- **远程推理**：`inference.mode: remote` 时本服务不加载权重，生成经 `backends/remote/` 转发给设备 A 的 inference-server（`INFER_API_KEY` 两侧同值）。会话数据仍全部落在本机——服务端只见 prompt 与媒体、不持久化会话。部署见根 [README](../README.md#部署形态)
- **CORS**：白名单收敛（`server.allowed_origins`，默认仅 `http://localhost:5173`）；走代理时为同源，CORS 仅在浏览器直连 14159 时触发，属纵深防御
- **AppleScript 权限**：`run_applescript` 首次驱动某个应用时，macOS 会弹出「自动化」授权请求；部分操作还需在「系统设置 → 隐私与安全性 → 辅助功能 / 自动化」中授予运行 Agent 的进程（终端 / Python）权限，否则脚本会因权限不足报错
- **Accessibility 权限**：GUI 工具（`read_ui_tree` / `click_ui_element` / `type_text` / `press_keys` / `set_ui_element_value`）需在「系统设置 → 隐私与安全性 → 辅助功能」中授予运行 Agent 的进程权限。未授权时工具会返回明确引导而非报错，运行期授予后**无需重启**即可生效（`list_applications` 免此权限）
- **GUI 动作默认不弹窗审批**：点击/键入/快捷键默认免审批（AX 权限已一次性授予、用户可见、可随时用取消按钮终止本轮）。若偏好更保守，可将 `accessibility.py` 中动作类工具的 `requires_approval` 置为 `True` 改为每次操作弹窗
- **GUI 依赖非 macOS 环境**：`pyobjc-*` 缺失或非 macOS 时，本组 GUI 工具整体不注册（`is_available=False`），不影响其余功能
