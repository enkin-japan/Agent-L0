# Agent 架构宪章（CHARTER）

> **这是什么**：Agent 的核心架构思想与开发铁律。它是**约束**，不是介绍。
> **何时读**：每次阶段性迭代开工前通读一遍；改动涉及交互层/编排/记忆/调度时，
> 对照第 1 节的铁律逐条自检。
> **为什么存在**：这套架构是一次"不惜拆了重做"的重整（见 `REARCHITECTURE.md`）
> 换来的。它治好了四条结构病（第 4 节）。**若在无上下文时随手改动，极易复发。**
> 本文让任何一次迭代——即使聊天记录已丢、上下文被压缩——仍受同一套原则约束。
>
> **文档生态**（四份，各司其职）：
> - `CHARTER.md`（本文）——**原则与纪律**，最持久，改动最慎重。
> - `PROTOCOL.md`——**冻结的事件协议 v1**，三端（后端/web/原生）的单一行为标准。
> - `REARCHITECTURE.md`——重整**蓝图 + 分期历史 + 踩坑记录**。
> - `ARCHITECTURE.md`——**运行时拓扑**（端口/分层/数据流图）。
>
> **改动本文 = 改动架构方向**，必须经用户明确同意。

---

## 0. 项目的目的（北极星）

用**本地开源模型**做一个帮普通用户处理**近乎所有桌面级任务**的 Agent，
**足够智能好用**且**保护好用户隐私**。前端（web + Apple 原生）只是用户与
Agent 沟通的**窗口**，不是主体。

一切取舍以此为准绳。当一个决定在"更炫的功能"与"隐私/本地/可靠"之间冲突时，
后者优先——数据不出门是这个项目存在的理由。

---

## 1. 铁律（MUST NOT break —— 违反即架构退化）

### 铁律 1：智能层是资产，一行不动（除非专门优化它本身）

**规则**：以下是几个月实测调出来的核心竞争力，**任何交互层/前端/协议的迭代都
不得触碰它们的逻辑**：
- 推理引擎与 MLX 补丁（`core/engine.py`、`backends/mlx/patch/gemma4_generate.py`）：
  KV 前缀复用、TurboQuant 量化、prompt-lookup 投机采样、思考预算锚定、
  llguidance 工具段约束采样；
- 编排护栏（`core/orchestrator.py`）：十余种**确定性**护栏（步内预算/总熔断/
  重规划上限/同参重复拦截/解析断环/截断升档续写/提前收尾推回/写后验证门控/
  失败路由菜单/结构性提醒/tail_notes 可变尾部）——参数全是实测调的；
- 三层有界记忆（`memory/`）：机械压缩→滚动摘要→RAG 沉淀；
- 单 worker 调度（`core/scheduler.py`）；
- 全部工具与物理隔离沙箱（`mcp/`、`agents/`）；
- 摄入层（`ingestion/`）。

**为什么**：推倒重来只会得到一个更差的复制品，且要重新冒一遍已经冒过的险。
**违反的后果**：丢失弱模型跑真任务的全部经验；护栏参数错乱会让 Agent 在真实
任务上退化，且不会立刻暴露。

**唯一例外**：明确以"优化智能层本身"为目标的迭代（如换模型、调护栏阈值、
加新工具）。这类改动**必须跑后端金测试**（`backend/tests/`），并在动护栏
逻辑前先读懂它护的是哪种失败形态。

---

### 铁律 2：轮次是资源，事件先记账后分发

**规则**：一个"轮次"（turn = 一次用户消息触发的完整编排闭环）是**可寻址、
可重放的持久资源**，不是"一次 HTTP 请求"。它有 `turn_id`、有状态机
（queued/running/awaiting_approval/suspended/ended）、可查询、可重放，
**断开连接不改变它的任何状态**。

编排产生的每个事件**无条件写入会话事件账本**（`core/journal.py` 的
`TurnJournal`）。SSE 附着流、`/turn` 状态查询、全局 feed、通知——**全部是账本
的读者**，没有旁路。

**为什么**：这是整个重整的核心手术。旧架构把轮次的过程事件只存在请求作用域的
队列里，断开即蒸发——由此长出前端十几处"补偿机制"和它们各自的 bug。
**违反的后果**：任何"某条路径忘了发事件"的可能性会立刻复活审批卡刷新丢失、
黑盒等待、补偿动物园。

**推论（同等强制）**：
- **单一写入者**：编排层/TurnRunner 持有账本句柄**必传**（不是可选 emit 回调）。
  新增任何编排路径，事件必须经账本发出。流式回调（token/thinking/tool_pending）
  集中在 `orchestrator.run_turn` 注入，不在各调用方分散接线。
- **`turn_ended` 是唯一权威终态**：过程事件（增量/工具/计划）只作实时预览，
  最终一律以 `turn_ended` 的完整轮次记录对账。cancelled/fail 时预览撤回。
- **暂停不是结束**：awaiting_approval 与 suspended 是轮次的**暂停态**，轮次仍
  "活着"、状态可查、连接可断可重连。它们的暂存态（pending_tools 等）经
  `/turn` 可查——刷新后能重建审批卡。
- **落盘策略**：里程碑事件落 `journal.jsonl`，token/thinking 增量只留活跃轮
  内存环。崩溃丢的只是增量（该轮本就被判 fail，语义自洽）。`inflight.json`
  只剩崩溃防丢（write-ahead）一个职责，**不得**再兼任前端探测。

---

### 铁律 3：TurnRunner 是轮次生命周期的唯一属主

**规则**：轮次的全部生命周期不变量集中在 `core/turn_runner.py` 一处：
前置清理（放弃暂停态旧轮）→ 追加用户消息 → inflight 预写 → 账本 turn_queued
→ 调度门闸 → 编排闭环 → 终态分流（成功/取消/故障回滚/审批暂存/挂起暂存）
→ 落盘或暂存 → 摘要触发 → 生命周期事件。`routes.py` 是**薄 HTTP 适配器**
（参数解析 + 鉴权 + 调 TurnRunner），不得含轮次逻辑。

**为什么**：旧架构 `routes.py` 有三份近拷贝各自持有这套不变量——这正是"改前端
要求改后端、改后端怕功能性破坏"的根源，谁都不敢动。
**违反的后果**：再出现第二个编排器，不变量重新漂移。

**这套不变量必须成套保持（改 TurnRunner 前逐条核对，金测试守着）**：
- `session.lock` 覆盖从清理到落盘的完整临界区；受理（accept）与执行（run）
  分两次持锁；
- **受理冲突闸门在 `session.lock` 外快路径判定**（运行中的轮次持锁数分钟，
  锁内判定会让 409 永远打不出来、断连请求还会在锁释放后复活执行），锁内再复检；
- `scheduler` 的 acquire/release/suspend **严格配对**：suspended 走 suspend
  （句柄保留可恢复），其余一律 release；
- 取消/故障**整轮回滚**到 `_rollback_idx`（含用户消息），任务态同步回滚；
- 待审批/挂起的轮次**不落盘**，暂存 `session.pending_turn`/`suspended_turn`，
  决议/恢复后合并；落盘时剥掉全部 `_` 内部账目键；
- 生命周期事件由 TurnRunner 发，过程事件由编排层发——都经账本。

---

### 铁律 4：客户端是同一协议的瘦渲染器，行为由夹具锁定

**规则**：web（`store/useChatStore.ts` + `lib/turn-reducer.ts`）与原生
（`Store/AgentStore.swift` + `Store/TurnReducer.swift`）都是**账本事件的纯
归约器投影**：`reduce(state, event) -> state` + UI 副作用。两端读**同一批**
`protocol/fixtures/` 夹具（web vitest + Swift `run_reducer_fixtures.sh`）。
（R5 补注：macOS app 自 R5 起同时充当 backend 的宿主/监护者（sidecar），
但其**渲染部分**仍是纯归约器投影——宿主身份不给渲染层任何后门，铁律不变。）

**为什么**：旧架构"一份协议三处状态机零共享测试"，每改协议成本×3 且实证漂移
（web 把工具事件解析了又扔、native 判 `"success"` 而后端发 `"ok"`）。
**违反的后果**：两端行为漂移从"上真机才发现"退回不可测。

**推论**：
- **恢复只有一条路**：刷新/杀进程/切后台/断线回来，一律
  `GET /turn`（拿可恢复状态）→ `GET /events?after`（续播）。不得为某个场景
  另造补偿路径。
- **断开是常态不是异常**：静默指数退避重连，**不弹"连接中断"红字**。
- **单附着策略**：同一时刻只对活跃会话保持一条 `/events` 长连（HTTP/1.1 每源
  连接数有限）。切走即断、切回经历史+重放对账。
- **outbox**：发送前本地暂存，202 受理即清——兜住 multipart 没传完就退出的
  真丢失窗口。
- TS 与 Swift 不能共享代码，但**必须共享行为标准**（夹具）。

---

### 铁律 5：改协议 = 先改夹具，再改实现

**规则**：任何新增/改变协议行为，**先在 `protocol/fixtures/` 落夹具**
（事件流 + 期望归约终态），再改三端实现，以夹具回放全绿为验收。
未知事件类型**必须忽略**（向前兼容，`003-unknown-event-ignored` 永久保留）。

**为什么**：夹具是三端行为的可执行契约。先写夹具强制先想清楚语义。
**违反的后果**：协议悄悄漂移，三端各解释各的。

`PROTOCOL.md` 是冻结的 v1。**改它 = 升协议版本，须过评审**。新增事件类型不升
版本（客户端忽略未知即可）；改变既有字段语义才升。

---

## 2. 架构分层与职责（地图）

```
用户 ──窗口── web / Apple 原生（瘦渲染器：reduce 账本事件 + UI 副作用）
              │  提交 202 受理即返 / 附着 GET /events 观察 / GET /turn 恢复
              ▼
        ┌─────────────────────────── backend :14159（仅回环）───────────────────┐
        │ 表现层 api/    routes.py（薄适配器）+ auth（Bearer 常量时间比对）        │
        │ 属主层 core/turn_runner.py     轮次生命周期唯一属主                     │
        │ 账本   core/journal.py（每会话）+ core/feed.py（全局）  ← 唯一事件出口   │
        │ 调度   core/scheduler.py       单 worker + ClassPolicy                 │
        │ 编排   core/orchestrator.py    ReAct 循环，护栏全是确定性代码           │
        │ 引擎   core/engine.py          单次生成（专用推理线程，事件循环不饿死） │
        │ 方言   dialects/（R0）         prompt 模板 / 输出反序列化 / 标记 / schema │
        │ 承载   backends/（P0/B1）      mlx（进程内）/ remote（inference-server） │
        │ 记忆   memory/                 冷热会话 / 三层压缩 / inflight 防丢       │
        │ 工具   mcp/（26 文件 53 类）+ agents/（3 沙箱，物理隔离）               │
        │ 剧本   skills/*.md（主轮次提示词，经 skill_invoke 取用）              │
        └──────────────────────────────────────────────────────────────────────┘
              │ rag_search        │ web_research（受限沙箱）   │ deck PDF 导出
              ▼                    ▼                           ▼
        rag 数据面 :14160      SearXNG :8080              /print/deck（前端在跑打前端，
        （分类路由向量库，     （本地聚合，数据不出门）      否则打后端自托管包）
         本机；嵌入/重排经
         :14162 计算面，B2c）
```

**智能层（sacred，铁律 1）**：engine / orchestrator / memory / scheduler /
mcp / agents / ingestion / patch。
**交互脊柱（本次重整建立）**：turn_runner / journal / feed + 协议 + 双端归约器。
**方言层（R0 收编，2026-07-16）**：`dialects/gemma4/`（`PromptBuilder` /
`ResponseParser` / `tool_schema`），经 `core/dialect_protocol.py` 的 `ModelDialect`
协议注入引擎——**引擎不认任何模型家族的字面量**。换目标模型（30–70B 稠密 /
100–300B MoE，必然离开 gemma-4 家族）= 加一个 `dialects/<name>/` + 注册表一行，
不动承重代码。方言名跟着 `config.yaml` 的 `model_registry` 条目走（同
`suppress_ghost_thinking` 那条路）。协议改造不触碰。
> `backends/mlx/patch/` **不属方言层**：它是 `backend=mlx × dialect=gemma4` 的交叉
> 产物，归属已由 `backends/` 的目录名表达（R0 刻意不收编它——强抽象会让手术范围失控）。

---

## 3. 核心设计原则（带理由，指导日常取舍）

**重 task 轻会话**：会话是**记忆容器**（上下文/KV/历史）；任务是**交互单元**。
提交即得 turn_id、排队位次可见、跨会话任务中心吃全局 feed、通知由生命周期事件
直接驱动。后端调度器本就是任务为中心的——API 与前端要对齐到同一世界观，不要
把 UI 退化成"纯会话列表"。

**护栏是代码，计划是数据，模型在菜单里选**：不做"外层决策推理"（会让每步推理
翻倍）。所有护栏是确定性代码；模型只能通过元工具（update_plan/complete_step）
改计划。新增编排行为时，优先做成确定性护栏而非依赖模型自觉。

**单 worker 串行**：Apple Silicon 统一内存"高带宽弱算力"，并行生成互抢带宽、
各占一份 KV——串行才是吞吐最优。全进程同一时刻只一个任务占推理 worker。
**不要**为了"并发感"引入多路并行生成。

**推理下沉专用线程**：MLX 生成是同步计算，放事件循环线程会把整个 API 饿死
（prefill 数秒内连 /health 都无响应）。生成整段在专用推理线程，增量经
`call_soon_threadsafe` 回主循环。**MLX 流与惰性张量绑定创建线程**——模型必须
**在推理线程内加载**（`AgentEngine.create`），否则跨线程首次前向必崩。

**有界记忆**：机械压缩（观察去重/驱逐）→ 摘要折叠（滚动摘要+水位线）→
RAG 沉淀（原文可召回）。全量日志永远保留在 `history.jsonl`。model-facing
上下文必须有界。

**隐私边界（不可退让）**：后端仅回环绑定（外部访问走前端代理或 Tailscale）；
密钥不进浏览器 bundle（代理注入）；SearXNG 本地聚合；沙箱 SSRF 硬拒内网/环回/
云元数据；路径穿越白名单。任何新功能不得开这些口子。

**物理隔离沙箱**：联网调查/数学/Excel 子 agent 的脏工具与原始数据**物理上不进
主体上下文**（方案 b 隔离）。注入即便劫持沙箱也够不到危险工具。新增子 agent
遵循同一边界。

三条隔离不变量——工具集物理受限 / KV 隔离（一次性会话，全新缓存）/ 隐私边界
（个人环境事实不进子生成）——由 `agents/sandbox_runtime.py` **单点实现**并由
`tests/test_sandbox_invariants.py` 钉死。新增子 agent = 写一份 `SandboxSpec`
（声明 + hook），循环骨架不重写；照抄一份沙箱去改，等于把这三条不变量再复制一份
注释——R1 收编前正是如此（会话替身有 5 份拷贝）。

**审批闸门只有一处**：`orchestrator.py:551`（经 `registry.requires_approval`）。
沙箱执行器**没有**审批通路，也不需要——不变量 1 保证沙箱工具由构造即无害。推论
（R2a/D.11）：**skill 是主轮次的提示词剧本，不得自带工具集**。剧本能决定子会话拿
哪些工具 + 沙箱无审批 = 一个 md 文件即可绕过闸门执行 `run_shell_command` /
`write_file`。要给 skill 加 `tools:`，**先**解决「审批从沙箱透出到前端」，不能反过来。
由 `tests/test_skill_registry.py::test_skill_grants_no_tool_authority` 钉死。

---

## 4. 反模式（治好的四条病，别复发）

这四条是重整前的实际病灶。**任何改动若开始像其中之一，立即停手**：

1. **把任务建模成 HTTP 请求**——过程事件只活在请求作用域、断开即失、状态不可查。
   ➜ 现在：轮次是账本资源（铁律 2）。
2. **一份协议三处状态机零共享测试**——每改×3、实证漂移。
   ➜ 现在：瘦渲染器 + 共享夹具（铁律 4/5）。
3. **routes.py 是第二个编排器**——三份拷贝各持不变量，谁都不敢动。
   ➜ 现在：TurnRunner 唯一属主（铁律 3）。
4. **实时性事后加**——攒全文末尾解析、emit 可选靠人肉穿透每条路径。
   ➜ 现在：账本单一写入者 + 流式回调集中注入（铁律 2 推论）。

**衍生反模式**："补偿动物园"——为某个断线/刷新场景造一条专用轮询/对账/热循环。
一旦想加这种东西，说明恢复没走"统一一条路"（铁律 4），回去改那条路。

---

## 5. 开发纪律（流程约束）

**加一个功能**：
1. 若涉及新协议行为 → 先落 `protocol/fixtures/` 夹具（铁律 5）；
2. 后端改动 → 跑 `backend/tests/`（524 项金测试，FakeEngine 无模型毫秒级）
   必须全绿；动 TurnRunner/编排/账本前**先读金测试**知道它守着哪些不变量；
3. 两端归约器改动 → 跑夹具回放（web `npm run test` + Swift
   `Tests/run_reducer_fixtures.sh`）双绿；
4. 涉及真实行为的改动 → 起本地后端跑 E2E（原生 `Tests/run_e2e.sh` 20 项）
   或 Playwright；**实机测试前先 `lsof -ti :14159 | xargs kill`**
   （旧进程占端口会静默连到旧后端，新代码全不生效——曾误判 3 个功能坏）；
5. 起后端调试用 `PYTHONUNBUFFERED=1`（print 块缓冲会吞日志）；真环境在
   `backend/.venv`（仓库根有个指向旧路径的死 .venv，别用）。

**改协议**：先改夹具 → 更新 `PROTOCOL.md` → 三端实现 → 夹具全绿。改既有字段
语义要升版本。

**部署纪律**（多机拓扑）：后端可能跑在另一台机器。**改后端后要提示用户重新
同步**（"跟我说句，我重新同步一下"）。**开发场景下不要**在开发机上替用户
启动常驻后端（Xcode 资源紧张，用户特意分开）——此纪律约束的是**开发流程**；
macOS app 内建执行层（sidecar，R5-4）按设计自启/监护 backend，不受此条限制
（2026-07-16 随 R5 收官重述适用域）。测试性质的临时拉起要用完即收。

**提交纪律**：一般阶段做完 commit；需要用户实机查验的阶段做完 commit+push。
**`git add` 一律用显式路径，不要 `git add -A`**——曾因 `-A` 误扫进 `.env.example`。
模型权重收敛在项目内，绝不下载到 `~/.cache`。

**`apple-app/` 源码入库**（D.6，2026-07-16 改自「全程 gitignored」）：R5 要把执行层
收编进 macOS app，它从此是本仓库的一等公民——「记录遗失也能续工」的前提是它在库里。
只忽略派生物（`build/` `Generated/` `*.xcodeproj/`）与本机签名资产（`*.p8` `*.p12`
`*.mobileprovision`）。**签名凭据的标识符走 `.env.local`**（`ASC_KEY_ID` /
`ASC_ISSUER_ID`），**私钥永不入库**。

**原生发布**：每批 `./install-macos.sh`（Release→/Applications）+
`./release-ios.sh`（TestFlight）+ `wait-testflight.py <build>`；发布前
`project.yml` 的 `CURRENT_PROJECT_VERSION` 必须自增。

---

## 6. 关键文件地图（承重代码在哪）

| 关注点 | 后端 | web | 原生 app |
|---|---|---|---|
| 事件账本 | `core/journal.py` `core/feed.py` | — | — |
| 轮次属主 | `core/turn_runner.py` | — | — |
| 归约器 | — | `lib/turn-reducer.ts` | `Store/TurnReducer.swift` |
| 主状态机 | — | `store/useChatStore.ts` | `Store/AgentStore.swift` |
| API 封装 | `api/routes.py` | `api/agent.ts` | `API/AgentClient.swift` |
| 协议类型 | `api/schemas.py` | `types/protocol.ts` | `API/ProtocolModels.swift` |
| 智能层 | `core/{engine,orchestrator,scheduler}.py` `memory/` `mcp/` `agents/` `patch/` | — | — |
| 模型方言 | `core/dialect_protocol.py` + `dialects/<name>/`（R0） | — | — |
| 推理承载 | `core/backend_protocol.py` + `backends/<name>/`（P0/B1） | — | — |
| 流程剧本 | `core/skill_registry.py` + `skills/*.md`（R2a） | — | — |
| 渲染格 | `mcp/base.py` 的 `render_hint` + `ToolOutput`（R3） | `chat-message.tsx` 的 `renderByHint` | 随 R5 |
| 长期记忆 | `memory/notes.py` + `backend/notes/`（R4，gitignore） | — | — |
| 契约 | `PROTOCOL.md` + `protocol/fixtures/`（三端共享） | | |
| 回归网 | `backend/tests/`（**524** 金测试） | `lib/turn-reducer.test.ts` | `Tests/run_reducer_fixtures.sh` + `run_e2e.sh` |

单配置：仓库根 `config.yaml`。单启动器：仓库根 `devctl.sh`。

---

## 7. 已知边界与刻意搁置（非目标，别当 bug 修）

> 本节是**非目标**（刻意不做）。另有**缓做**（先轻量交付、完整版择机再做）的架构记在
> `DEFERRED.md`——那些不是边界，是有明确完整设计、等触发条件的待办，别混为一谈。

- ~~**iOS 后台推送**~~（2026-07-12 用户拍板解除搁置并实装）：iOS 推送经 APNs **直发**
  （`framework/push/`，铁律 2 预留的"通知读者"——dispatcher 订阅 feed/journal，无旁路），
  不用任何第三方中转。与"纯本地"的张力以**内容分级**缓解：per-device 三档
  （full / category / placeholder，默认 category 只推类别不推内容），
  "推送即门铃，真相永远在账本"——丢通知不丢状态，回 app 走统一 /turn+/events 对账。
  macOS 客户端**不经 APNs**（app 常驻订阅 feed 发本地通知，零出网）。
  残留边界：iOS 推送 payload 经 Apple 服务器，出门内容档位由用户在设置里选择。
- **deck 的 lucide 图标**：以 SF Symbol 近似映射，未收录降级圆点（web 端同有
  降级）。
- **gemma 生成偶发孤立 `*`**：模型侧格式紊乱，属提示词/输出清洗层，非交互架构。
- **`/memory` 面板数值**：已统一为落盘轮数口径。
- **deck PDF 导出**：优先打前端，前端不跑则打后端自托管包
  （`devctl bundle-print` 生成 `backend/static/webapp`）。渲染器仍只有一份
  React 代码——不得在后端复刻第二个渲染器（两个必然漂移）。

---

## 附：一句话自检清单（开工前默念）

1. 我要动的是智能层还是交互层？动智能层→跑金测试且读懂护栏；动交互层→往下看。
2. 我有没有在账本之外发事件、或把 emit 变回可选？（违反铁律 2）
3. 我有没有在 TurnRunner 之外新写轮次生命周期逻辑？（违反铁律 3）
4. 我有没有为某个场景造补偿路径而非走"/turn + /events 统一恢复"？（违反铁律 4）
5. 协议行为变了，我有没有先落夹具？（违反铁律 5）
6. 这个改动会不会开隐私口子、或引入并行生成？（违反第 3 节原则）

任何一条答"是/没有"且指向违反——停手，回到本文对应条款。
