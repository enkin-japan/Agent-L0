# grok-build（xAI Grok Build）深层架构对比

> 2026-07-18 过夜评估任务 1/5。对象：`/Volumes/enkin/project/grok-build`（xAI 开源的
> 终端 AI 编码 Agent「Grok Build」，Rust，Apache-2.0，从其 monorepo 周期性同步出来的
> 商业级架构层，约 60 个 crate）。本文两部分：**深层对比**（§2，逐维度）与
> **可借鉴清单**（§3，按建议采纳顺序排列）。结论先行见 §4。

---

## 1. 对象是什么

Grok Build 是与 Claude Code 同物种的**终端编码 Agent**：全屏 TUI + agent runtime，
可交互 / headless（CI 脚本）/ 经 ACP 协议嵌入编辑器。与本项目的定位差异先钉住，
后面所有对比都要过这层滤镜：

| | grok-build | 本项目 Agent |
|---|---|---|
| 物种 | 开发者编码 Agent（面向代码仓） | 个人全本地生活/工作 Agent（面向人） |
| 模型 | 云端 Grok API（无本地推理问题） | 本地推理（16GB 统一内存约束贯穿一切） |
| 交付 | 单二进制 CLI | 四服务 + 三端（Web/macOS/iOS） |
| 隐私 | 代码上云是前提 | 数据不出门是北极星 |
| 团队 | 商业公司体量（telemetry/更新/市场生态） | 单人 + 单开发机 |

因此「它有我没有」不自动等于欠账——凡属编码 Agent 特化（worktree 隔离、hunk 归因）
或商业生态（遥测、插件市场、自动更新）的，本文明确标记为**不借鉴**。

**crate 地图**（与本项目模块的对应关系）：

| grok-build crate | 职责 | 本项目对应物 |
|---|---|---|
| `xai-grok-agent` | Agent 对象 = 工具+系统提示+提醒策略+压缩策略+模型配置的可移植捆绑 | `framework/core/`（orchestrator + turn_runner 分散持有） |
| `xai-chat-state` | 会话状态 actor（对话数组/采样配置/token 计数，事件外发） | 事件账本（PROTOCOL.md + journal/feed） |
| `xai-sqlite-journal` | SQLite 日志模式按文件系统选择（NFS 防 SIGBUS） | history.jsonl 直写（无此问题域） |
| `xai-grok-tools` | 工具实现 + taxonomy + 注册表 + 输出截断/重试/归因 | `framework/mcp/` 46 工具 + 渲染格 |
| `xai-grok-sandbox` | OS 级沙箱（Landlock/Seatbelt，5 档 profile） | 3 个物理隔离沙箱（math/excel/research） |
| `xai-grok-memory` | markdown 记忆 + sqlite-vec 向量索引 + dream 整理 | R4 文件记忆 + RAG agent_memory/agent_notes |
| `xai-grok-hooks` | 文件发现式钩子（4 事件，PreToolUse 可拦截） | 无对应物 |
| `xai-grok-subagent-resolution` | 子 Agent 配置解析纯逻辑层 | R1 统一执行器 + D3（待扩充） |
| `xai-codebase-graph` | tree-sitter 代码图（定义/引用/增量索引） | 无（另见 evaluations/codegraph.md） |
| `xai-hunk-tracker` | 文件改动归因（Agent 改的 vs 外部改的） | 无 |
| `xai-grok-workspace` | 文件系统/VCS/权限/信任/恢复 | `framework/mcp/tools/` + 审批门 |
| `xai-grok-sampler` / `models` | 采样与模型配置 | R0 模型方言层 |
| `xai-token-estimation` / `prompt-queue` | token 估算 / 提示队列 | compactor 的字符预算 |

---

## 2. 深层对比（逐维度）

### 2.1 会话状态与持久化

**grok**：`ChatStateActor` 独立 tokio task 持有对话数组，命令进/事件出，无锁。
落盘为每会话一目录：

```
~/.grok/sessions/<encoded-cwd>/<session-id>/
  summary.json            # 索引项：标题/时间戳/模型/消息数/父会话引用
  updates.jsonl           # ACP 会话更新流（权威日志，驱动 /resume）
  chat_history.jsonl      # 发给模型的原始消息
  plan.json               # TODO 状态
  rewind_points.jsonl     # 文件快照（/rewind 撤销用）
  signals.json            # token 用量/工具/轮次计数
  compaction_checkpoints/ # 压缩前存档
  subagents/              # 子 Agent 元数据
```

**本项目**：事件账本（轮次可寻址可重放，PROTOCOL.md 冻结 + 双端夹具），
`sessions/{id}/` 下 history.jsonl（全量）+ memory.json（滚动摘要+水位线）+ assets/。

**评**：设计同构——都是「权威 jsonl 日志 + 旁挂索引/状态」。方向性差异有二：
① grok 把**每类状态拆成独立文件**（plan/signals/rewind 各归各），本项目集中在
memory.json/history.jsonl；拆分的好处是单类状态损坏不连坐、可独立演进 schema。
② grok 有 `summary.json` 作**会话索引项**（标题自动生成、父会话引用支持 fork/restore），
本项目冷启快照（v0.16.0 43）已解决列表加载，但**会话 fork/restore 谱系**没有概念。
差距不大，本项目的账本还多一层「协议冻结 + 三端共享夹具」的纪律，这是 grok
（单 TUI 端）不需要而本项目必须有的。

### 2.2 上下文压缩（本项目最值得照镜子的一面）

**grok** `CompactionPolicy`：
- 阈值触发：上下文用量 85% 自动压缩（可配）；
- **压缩前记忆冲洗（memory flush）**：压缩前先跑一个「把重要信息存进记忆」的
  模型回合，然后才折叠——先抢救，再丢弃；
- **两遍预燃压缩（two-pass prefire）**：接近阈值时**后台推测性**摘要历史前缀
  （pass 1），真到压缩点只需摘要「NOTE₁ + 近期尾部」（pass 2）——把大头延迟藏进后台;
- **墙钟预算**：单次压缩生成超时即砍并重试（防 reasoning 跑飞，token 上限接不住的
  那类）；
- 压缩前状态存 `compaction_checkpoints/`，可回看被折叠的内容；
- 可指定专用小模型做压缩（`compact_model`）。

**本项目**：机械压缩（T3-a/b：预算感知+取代感知+写屏障，零模型调用）→ 摘要式二次
压缩（活跃高阈值 + 空闲低阈值双触发）→ rag_sink 原文沉淀（无损承重网）。

**评**：各有一半先进。本项目的**机械层**（取代去重/写屏障/预算驱逐）grok 没有——
它一上来就是模型摘要；本项目的 **rag_sink 无损沉淀**（折叠原文全进 agent_memory
类目，可语义召回）也比 grok 的 checkpoint（只能人工回看）强。但 grok 有三件本项目
没有的：**memory flush**（压缩即将吞掉上下文前给模型一次主动抢救机会——本项目
R4 笔记本基础设施是现成的，缺的只是这个触发时机）；**prefire 两遍压缩**（对本地
推理特别有价值：16GB 机上摘要一次以十秒计，藏进后台 = 用户无感）；**墙钟预算**
（本项目摘要压缩若遇模型跑飞，目前靠全局超时兜底，粒度粗）。

### 2.3 记忆系统

**grok**（实验特性，默认关）：
- 布局：`~/.grok/memory/MEMORY.md`（全局）+ `{workspace_hash}/MEMORY.md`（项目级）
  + `sessions/*.md`（会话日志）——markdown 为体，**sqlite-vec 为索引**；
- 检索：向量召回 + **MMR 多样性重排** + **查询扩展**（query_expansion）；
- **dream 整理**（autoDream）：三重闸（开关/距上次≥min_hours/新会话数≥min_sessions）
  全过才触发一次「梦境整理」——把攒下的会话日志折叠提炼进 MEMORY.md，带文件锁防
  并发，锁记录上次整理时间戳；
- 工具面：`memory_search` / `memory_get` 两个只读工具 + 压缩前 flush 写入；
- 文件 watcher 增量重索引。

**本项目**：R4 文件记忆（notes.py：agent 主动撰写，MEMORY.md 索引行随环境块注入
主编排轮「知道自己知道」，正文走 RAG agent_notes 类目语义召回）+ rag_sink
（agent_memory 类目自动沉淀）+ 分类路由式 RAG 本体。

**评**：结构惊人地趋同（markdown 记忆体 + 向量索引 + 索引注入提示词），互为印证。
本项目占优处：真 RAG 服务（分类路由/两阶段检索/治理面）比 sqlite-vec 强一个量级；
「索引进提示词 + 正文走 RAG」的双召回路 grok 没有（它只靠工具搜索，模型不知道
自己知道什么——正是 notes.py 头注点名的缺陷）。grok 占优处：**dream 整理**——
本项目的笔记只增不整理，写多了会碎片化冗余；grok 的三重闸 + 锁 + 时间戳记账
是一个完整的「何时整理」答案（本项目已有空闲维护回合，是现成的挂载点）。
MMR 与查询扩展是 RAG 检索侧的通用增强，可在数据面 query 管线加（重排后做多样性
过滤，纯计算无新依赖）。

### 2.4 工具体系

**grok**：
- **taxonomy 层**：`ToolKind` 规范词汇表（Read/Edit/Execute/…语义身份）+ 规范字段名
  （path/offset/command/…）+ `x.ai/tool` `_meta` 信封（版本化 v1）——同一语义工具在
  不同 harness（codex 移植/opencode 移植/自家）间归一；展示名与模型可见名分离；
- 输出上限：工具结果 40KB（≈1 万 token）、shell 20K 字符，MCP 结果单独上限（env
  可调）；
- 重试（retry.rs）、401 归因（attribution.rs：Bearer 前缀截取定位哪个消费者的钥匙
  坏了）、工具版本化（versions.rs）；
- 值得注意：`implementations/` 里直接**源码移植了 openai/codex 与 sst/opencode 的
  工具实现**（License 声明齐全）——商业产品也在「抄同行作业」，且抄得光明正大。

**本项目**：MCPRegistry 46 工具 23 文件，注册表热重扫，探活门控，D4 目录阈值
（>12k tokens 切 catalog），渲染格（R3），纠错回环（400 附目录自纠）。

**评**：本项目工具面的**纠错回环**（分类不存在→400附目录、参数预检、三调三返）
是 grok 没有的——它的模型足够聪明不需要；这正是「架构补模型智力」路线的产物，
保留。grok 的 taxonomy 层对本项目是**过度设计**（只有一个 harness），但其中
**规范字段名纪律**值得记住（本项目已踩过 yt-dlp 键名坑 S.10，同病）。
**工具输出硬上限**本项目值得补：目前靠压缩器事后削，缺「进模型前」的第一道闸
（详见 §3.4）。

### 2.5 权限与安全

**grok** 四层管道，顺序固定：
1. PreToolUse 钩子（可先斩）；
2. 规则（deny > ask > allow，deny 恒胜）；
3. **已记住的批准**（remembered grants）：按命令前缀记住、**按项目作用域**、
   危险命令列表上的永远重新问；
4. 内建自动放行：只读工具 + **词边界匹配的只读 shell 命令白名单**（`ls` 不匹配
   `lsof`；链式命令按 `&&`/`;`/管道拆段后逐段判）；
5. 权限模式兜底（问/自动批/自动拒）。
另有 OS 级沙箱（nono 库，Linux Landlock / macOS Seatbelt）5 档 profile
（workspace/devbox/read-only/strict/off + TOML 自定义），子进程网络用 seccomp 断。

**本项目**：审批门（写/强力工具挂起待批，审批续跑重入 run_turn）+ 参数预检 +
3 个物理隔离沙箱（数学/Excel/研究，进程级）+ 出网口收敛（SearXNG 恒回环）。

**评**：本项目每次审批都是一次性的——**没有「记住这次批准」**。个人 Agent 高频
场景（每天都批同一条 `brew install` 类命令）下这是真实摩擦。grok 的方案要点值得
整体搬：按命令前缀 + 按会话/项目作用域 + 危险命令表除外。词边界白名单本项目
部分有（只读工具不审批），shell 命令粒度的拆段判只读没有——但本项目 shell 工具
本来就全审批，属于更保守而非缺失。OS 级沙箱（Seatbelt）比进程隔离强，但本项目
沙箱是「工具作者自己写的受限执行器」而非「放模型任意跑 shell」，威胁模型不同，
不必跟进；唯 shell 工具若未来放宽审批，Seatbelt profile 是该补的前提。

### 2.6 子 Agent

**grok**：`spawn_subagent` 工具 + 内建三型（general-purpose/explore/plan）+
`.grok/agents/*.md` 文件定义可覆盖/新增 + **persona 叠加层**（行为覆盖：语气/
输出契约，`.grok/personas/*.toml`）+ 能力模式过滤工具集 + worktree 隔离可选 +
配置解析抽成无依赖纯逻辑 crate（显式覆盖 > 角色 > persona > 父级的优先级链）+
子会话入正常会话树可恢复。

**本项目**：R1 统一执行器（子 Agent 与主轮同一执行器，沙箱工具集），D3 扩充
挂起等真实需求。

**评**：这是 D3 的现成参考设计。三点最值得记：① **agent 定义 = .md 文件**
（名字/描述/工具集/提示词 frontmatter），与本项目 skill 剧本同构——D3 落地时
子 Agent 类型可以直接复用 SkillRegistry 的文件发现模式，不必发明新机制；
② **explore/plan 只读双型**是被 Claude Code 与 grok 双重验证过的最小够用集，
D3 首批就照这个抄；③ persona 层对单人项目是过度设计，跳过。

### 2.7 Skills / Hooks / 后台任务 / 计划模式

- **skills**：与本项目 R2a 同构（目录发现 + 注入），grok 是 Anthropic Agent Skills
  规格的目录制渐进披露；本项目是扁平单 md 全文注入（已在 remotion 评估里定过：
  等 31B 再考虑目录制）。无新信息。
- **hooks**（4 事件 JSON 文件定义，PreToolUse 可拦，fail-open）：本项目无。个人
  Agent 场景下 hooks 的真实用途（公司合规、审计上报）不存在，**不借鉴**；若将来
  要「某类工具调用前自动跑检查」，本项目的正确形态是往编排层加确定性预检
  （参数预检已是此类），不是外挂子进程。
- **后台任务**：`background:true` → task_id → `get/wait/kill` 三工具 + TUI Ctrl+G
  把前台命令送后台 + monitor 工具 + 调度器。本项目有 schedules（定时任务）但
  **工具执行是同步阻塞的**——长命令占住整轮。这是真实差距（详见 §3.6）。
- **计划模式**：plan mode + `plan.json` 持久化。本项目 update_plan 元工具 +
  task_state 已覆盖核心；「计划先批准后执行」的模式门控没有，对话式个人 Agent
  暂不需要，跳过。

### 2.8 可靠性工程（regard 但不搬）

NFS 上 SQLite WAL 会 SIGBUS → 按文件系统选日志模式 + per-host DB 文件；崩溃
处理器；系统电源事件监听；PTY 兼容层；自动更新。这些是「装机量 × 环境多样性」
逼出来的工程,单人单机没有这些问题域。**价值在于姿势**：每个都是「检测环境 →
选实现」的分派模式，与本项目「同一契约两个实现」同一信仰，互为印证。

---

## 3. 可借鉴清单（按建议采纳顺序）

### 3.1 压缩前记忆冲洗（memory flush）★ 强烈建议，成本低

压缩即将折叠上下文前，先给模型一个「把值得跨会话记住的东西写进笔记」的回合，
再折叠。本项目全部基础设施现成：R4 notes（写入面）+ 摘要式二次压缩（触发点在
compactor，两处触发共用）。改动 = 在摘要压缩前插一个可选模型回合（或更轻：在
摘要 prompt 里加「同时输出应存笔记的条目」一段，单次调用双产出——16GB 机上
省一次推理，更契合本地约束）。风险：e4b 智力能否判断「什么值得记」待实测，
属观察清单维度；但机械上先通，31B 到位即受益。

### 3.2 dream 式笔记整理（挂进既有空闲维护回合）★ 建议，成本低-中

R4 笔记只增不理，长期必碎片化。grok 的三重闸直接搬：开关 + 距上次整理 ≥N 小时 +
新增会话/笔记数 ≥M 才触发；整理动作 = 模型读全部笔记 → 合并去重改写 → 回写。
挂载点现成：app 的空闲维护回合（现在只做低阈值折叠）。需要一个锁文件记上次
整理时间戳（grok 的 dream_lock 模式）。注意本项目红线：整理回合只在主编排环境
跑（笔记是个人事实，不进沙箱）。

### 3.3 记住的批准（remembered grants）★ 建议，成本中

按命令前缀 + 按作用域记住用户批过的审批，危险命令表除外。本项目审批体系
（挂起/续跑/前端审批卡）成熟，缺的是记忆层：审批卡加「本会话不再问 / 一直允许」
选项，落 `sessions/{id}/`（会话作用域）或全局配置（永久作用域），orchestrator
审批前查表。**危险前缀表**（rm/sudo/kill/…）恒问，照抄 grok 的纪律。这是纯 UX
减摩擦项，用得越多收益越大。

### 3.4 工具输出进模型前的硬上限 ★ 建议，成本低

grok：工具结果 40KB、shell 20K 字符，超限截断（保首尾）。
**〔落地时修正（2026-07-18）〕**本节评估时的论断有误：当轮闸本项目**早已存在**
（orchestrator `observation_max_chars` 默认 4000，每条观察入流前截断）。真实
差距只有两点并已落地：旧实现只保头部（shell/日志的错误行多在尾部，截后模型
误判成功）→ 改保头 2/3 + 尾 1/3；截断标记不报原始体积、不给取回出路 → 中缝
标注 total 与「收窄参数重取」提示。

### 3.5 prefire 两遍压缩（本地推理特化收益）☆ 值得，成本中

接近阈值时后台先摘要前缀，真压缩时只摘尾部。本地慢推理下用户体感收益比云端
更大。但要处理好与 MPS 串行锁/正在进行的推理的资源竞争（16GB 只有一个推理位，
后台摘要会抢），建议等生产 GPU 设备后做——挂 DEFERRED，与 8B 档同批。

### 3.6 后台任务执行（长命令不占轮）☆ 值得，成本中-高

`background:true` + task_id + get/wait/kill 三工具。本项目已有 schedule/runner
的进程管理经验可复用。真实需求场景：media_download 长下载、批量文件处理。
建议等 D2 第三批需求清单一起拍（用户拍板项——工具面扩张属需求驱动）。

### 3.7 检索侧两个小增强：MMR 多样性 + 查询扩展 ☆ 顺手，成本低

RAG 数据面 query 管线在重排后加 MMR 多样性过滤（防 top-k 全是同一文档的近邻
块）；查询扩展（同义改写后多路召回取并）成本高些（多次嵌入调用），先只做 MMR。
纯数据面改动，不动计算面契约。

### 3.8 会话谱系（fork/restore 父引用）与 rewind 快照 —— 记下不做

`summary.json` 的 parent 引用 + `rewind_points.jsonl` 文件快照。编码 Agent 刚需
（改坏了要回滚），对话 Agent 弱需求。本项目文件类工具走审批 + git 仓库自身可回滚,
暂无真实场景。记录在案，等需求出现。

### 明确不借鉴

| 项 | 理由 |
|---|---|
| TUI/ACP/编辑器嵌入 | 端形态不同（本项目三端已定型） |
| telemetry/mixpanel/自动更新/插件市场 | 商业生态问题域，单人项目不存在 |
| OS 级沙箱（Seatbelt/Landlock） | 威胁模型不同；唯 shell 审批放宽时再议 |
| persona 叠加层 | 单人项目过度设计；D3 用 agent 定义文件即够 |
| hooks 子进程钩子 | 正确形态是编排层确定性预检（已有） |
| NFS/崩溃/电源等可靠性件 | 问题域不存在；记住「检测环境→选实现」的姿势即可 |
| codex/opencode 工具移植 | 本项目工具面已按自身需求成形 |

---

## 4. 结论

grok-build 与本项目在**架构信仰上高度同源**（权威 jsonl 日志、markdown 记忆体 +
向量索引、配置分派选实现、文件发现式扩展、分层权限），这本身是对本项目路线的
商业级印证——没有发现任何「方向错了」级别的信号。真实差距集中在三处：
**压缩前抢救（memory flush）、笔记整理（dream）、审批记忆（remembered grants）**，
全部是低-中成本、基础设施现成的增量，且前两者恰好补在本项目记忆体系唯一的
短板（只写不理）上。建议采纳顺序即 §3 编号顺序：3.1/3.2/3.4 三件低成本先行,
3.3 随后，3.5-3.7 挂条件，其余记录不做。

*评估依据：crate 源码模块头注（agent/memory/chat-state/sandbox/tools/hooks/
subagent-resolution/sqlite-journal/hunk-tracker/codebase-graph）+ 随包用户指南
（13-memory/16-subagents/17-sessions/20-background-tasks/22-permissions）。
未跑其二进制（无需运行时行为验证的评估）。*
