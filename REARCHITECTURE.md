# MacAgent 重整蓝图 —— 体检报告 · 目标架构 · 迁移计划

> 2026-07 全项目体检的产物。回答三个问题:哪些是资产(别拆)、病灶到底在哪、
> 怎么重整才能不再"补丁摞补丁"。
> 结论先行:**不拆重做。拆掉重建的是"交互脊柱",智能层原地保留。**
>
> **重整工程已于 2026-07 完成（Phase 0~4）。** 本文是历史蓝图 + 踩坑记录;
> 沉淀下来的架构原则与开发铁律见 `CHARTER.md`（后续迭代以那份为约束）。

---

## 0. 结论(TL;DR)

1. **智能层是资产,负债在交互层。** 引擎(MLX 补丁/KV 复用/约束采样/投机采样)、
   编排护栏、三层有界记忆、单 worker 调度、沙箱工具生态——这些是几个月实测调出来的
   核心竞争力,推倒重来只会得到一个更差的复制品。它们一行不动。
2. **病根一句话:把「任务」建模成了「一次 HTTP 请求」。** 一个轮次跑几分钟、多步、
   可挂起、可审批、断线后还在跑——但它的全部过程事件只活在一条请求作用域的队列里,
   断开即蒸发,状态散落四处不可查询。前端为此长出的十几处"补偿机制"(以及它们各自的
   bug)全是这个建模错误的利息。
3. **手术方案:轮次升格为「可寻址、可重放的持久资源」。** 事件先记账、后分发
   (单一写入者);客户端从"三份各自为政的状态机"降级为"同一协议的瘦渲染器",
   用共享测试夹具锁死行为一致性。
4. **分四期迁移,每期独立可交付**,旧端点兼容过渡,任何一期结束系统都比之前更好,
   不存在"拆到一半不能用"的窗口期。

---

## 1. 现状盘点

### 1.1 资产负债表 —— 资产侧(动它之前三思)

| 层 | 位置 | 为什么宝贵 |
|---|---|---|
| 推理层 | `backend/framework/backends/mlx/patch/gemma4_generate.py`(824 行；2026-07-14 P0 手术自 `framework/patch/` 平移)、`core/engine.py` | KV 前缀复用(prefill 提速≈10×)、TurboQuant KV 量化、prompt-lookup 投机采样、思考预算锚定、llguidance 工具段约束采样。硬件感知的深度工程,换任何框架都要重写且大概率更差 |
| 编排护栏 | `core/orchestrator.py`(941 行) | 十余种**确定性**护栏:步内预算/总熔断/重规划上限/同参重复拦截(读写指纹+写屏障)/解析断环/截断升档续写/提前收尾推回/写后验证门控/失败路由菜单/结构性提醒/tail_notes 可变尾部。这是"弱模型跑真任务"的全部经验结晶,参数全是实测调的 |
| 记忆三层 | `memory/`(session 1164 行、compactor、rag_sink) | 机械压缩(取代去重+预算驱逐)→滚动摘要(双阈值)→RAG 沉淀(无损承重网);冷热会话 KV 冻结/恢复;分支(KV 克隆+媒体硬链接);取消/故障整轮回滚;inflight 预写防丢 |
| 任务调度 | `core/scheduler.py`(318 行) | 单 worker 协作式多任务、cancel/suspend 两种抢占原语、老化防饿死、策略可插拔。**这正是"重 task 轻会话"理念的地基**,写得很干净 |
| 工具生态 | `mcp/`(20 个工具)+ `agents/`(4 个沙箱) | 三层桌面控制(shell→AppleScript→Accessibility 兜底);物理隔离沙箱(web 反投毒、excel verify 门禁+哈希封印、math submit_answer 程序化提取);按调用动态审批;探活注册 |
| 摄入层 | `ingestion/` | 模态分路、provenance 标记、懒登记、file_extract 逃生舱、解析结果烘进日志不重解析 |
| 隐私边界 | 全局 | 回环绑定、密钥不进浏览器、SearXNG 本地聚合、沙箱 SSRF 硬拒、路径穿越白名单。故事完整自洽 |
| 运维 | `devctl.sh`、`config.yaml`、模型注册表 | 单配置文件、统一启动器、九模型钉版本+契约静态校验、`ARCHITECTURE.md` 与实际一致(难得) |

### 1.2 负债侧 —— 四条结构病

**病 1:任务被建模成 HTTP 请求(最重的一条)。**
一个轮次的过程事件只存在于 `routes.py` 里那条请求作用域的 `asyncio.Queue`
(`chat_stream.event_gen`),断开即永久丢失,无任何重新订阅途径。轮次的中间状态
散落在:内存 `pending_turn`/`suspended_turn`(**无端点可查**,审批卡刷新即永失)、
有界 40 条的 activity 快照、磁盘 inflight.json(只有 prompt)。于是前端长出补偿动物园:

- web:`scheduleInflightReconcile` 轮询、inflight provisional 气泡、
  `syncSuspendedSessions` 3s 对账、断线后 /task+/history 双补偿路径;
- native:`pollUntilIdle`→`resync` 链、全局 /tasks 6s diff 监听;
- 已实测的病例:审批中刷新=永远转圈+审批卡丢失;native 对暂存轮次进入
  **无休眠热循环**(每圈 4 个请求);web 刷新后步骤/计划面板全盲。

**病 2:一份协议,三处状态机,零共享测试。**
轮次生命周期(流式→审批→挂起→续跑→落盘→对账)在 `routes.py`、
`frontend/src/store/useChatStore.ts`(1146 行)、
`apple-app/Sources/Store/AgentStore.swift`(~830 行)各实现一遍;类型在
`agent-api.ts` 与 `Models.swift` 手工镜像。每改一次协议成本×3,且已实证漂移:
web 把 `tool_started/tool_result` 解析了又扔掉;native 判 `status=="success"`
而后端发 `"ok"`(成功工具全部渲染成失败样式);native resume 无轮询;
任务中心靠 diff 轮询模拟事件。

**病 3:routes.py 是第二个编排器。**
`_chat_turn` / `_resume_approval_turn` / `resume_task` 三份近拷贝各自持有
"回滚快照→调度门闸→终态分流→落盘或暂存→摘要触发"的完整不变量。轮次生命周期
没有单一属主——这正是"改前端要求改后端、改后端怕功能性破坏"的直接来源:
不变量活在三份复制粘贴的端点代码里,谁都不敢动。
(实证:审批续跑路径漏传 emit(`orchestrator.py:914`)、resume 路径整个无 emit,
就是三份拷贝各自演化漏掉的。)

**病 4:实时性是事后加的。**
引擎是"攒全文、末尾解析"设计,流式经 `VisibleTextStreamer` + 可选 emit 回调
后补:思考整块后置、工具 JSON 生成期全静默(本地模型几十秒)、emit 是可选参数
要靠人肉穿透每条路径。结果就是你说的"一大块吐出"和黑盒感——换更强模型也治不了,
只有事件粒度能治。

### 1.3 顺带发现的两处倒挂

- **后端功能依赖前端进程**:`deck_export_pdf` 用无头 Chrome 打印前端
  `/print/deck` 路由(`config.ppt.frontend_base_url`)。前端不跑,后端工具坏。
  渲染器应自包含(打包一份独立 HTML 随后端出货)。
- **会话元数据双头真相**:`ui_state.json`(web 专用工作区 blob,last-write-wins)
  与 `meta.json`(pinned/血缘)并存,标题在 web localStorage 又一份;
  native 刻意绕开 ui-state。跨端语义未定义。

---

## 2. 目标架构:交互脊柱 v2

### 2.1 核心抽象:Turn 是资源,事件是账本

```
                       ┌────────────────────────────────────────┐
                       │  Orchestrator / TurnRunner(唯一写入者) │
                       │  无条件把每个事件 append 到会话事件账本   │
                       └──────────────┬─────────────────────────┘
                                      ▼
            ┌──────────────────────────────────────────────┐
            │  会话事件账本(per-session,seq 单调递增)        │
            │  · 活跃轮:内存环形缓冲(含 token/thinking 增量) │
            │  · 里程碑事件:落盘 journal.jsonl(轻量)         │
            └──────┬──────────────┬──────────────┬──────────┘
                   ▼              ▼              ▼
             SSE 读者          REST 读者       全局 feed 读者
       GET /events?after=seq  GET /turn      GET /api/v1/events
       (重放→续播,断开随便重连) (当前轮完整状态) (任务中心/通知)
```

**原则:先记账,后分发。** 编排循环不再接受"可选 emit 回调",而是持有账本句柄
无条件写入;SSE、轮询、通知全部降级为**读者**。"忘了传 emit"这一类 bug 从结构上
不可能再发生(审批续跑、挂起续跑、定时任务自动获得完整事件流)。

**事件模式 v1(带 `v` 字段,客户端忽略未知类型):**

| 类别 | 事件 | 说明 |
|---|---|---|
| 生命周期 | `turn_queued`(排队位次) / `turn_started` / `turn_ended`(权威终态,含完整 response) | 提交即有回执,排队可见 |
| 生命周期 | `awaiting_approval`(含 pending_tools 全量) / `suspended` / `resumed` | **暂存态入账本** → 刷新后可重建审批卡/挂起横幅 |
| 过程 | `iteration_started`(第几迭代/是否思考) / `thinking_delta` / `token_delta` | 思考改逐段;增量只进内存环,不落盘 |
| 过程 | `tool_call_pending`(name,生成中即发) / `tool_started` / `tool_progress` / `tool_result` | pending 来自约束采样状态机(ToolCallGuard 掐得准进入工具段的时刻)→ 工具名提前几十秒亮出 |
| 过程 | `plan_updated` / `step_completed` / `step_failed` | 替代现在的整包 task_progress 快照 |
| 全局 | 各会话的生命周期事件聚合 | 任务中心/后台通知直接消费,不再 diff 轮询 |

**落盘策略:** 里程碑事件(生命周期+工具+计划)写 `sessions/{sid}/journal.jsonl`;
token/thinking 增量只留在活跃轮的内存环形缓冲(重附着时完整重放)。进程崩溃丢的
只是增量——而崩溃后该轮本来就由 `recover_inflight_turns` 判 fail,语义自洽。
磁盘不膨胀,inflight.json 退役为纯崩溃防丢(不再兼任前端探测)。

**端点变化(旧端点兼容期保留):**

```
POST /api/v1/chat/{sid}/turns            提交轮次,摄入完成即返 {turn_id, seq}(不再长连)
GET  /api/v1/chat/{sid}/events?after=seq SSE:重放 seq 之后的事件→续播;断开任意重连
GET  /api/v1/chat/{sid}/turn             当前轮完整可恢复状态:status/partial_text/
                                         partial_thinking/pending_tools/plan/steps
POST /api/v1/chat/{sid}/stream           (兼容)= turns + 内联 events
GET  /api/v1/events                      全局任务 feed(Phase 3)
```

刷新/杀进程/切后台后的恢复流程统一为:`GET /turn`(拿状态)→ `GET /events?after`
(续直播)。补偿动物园全部退役。

### 2.2 后端内部:TurnRunner —— 轮次生命周期的唯一属主

把 routes.py 三份拷贝收敛为一个 `core/turn_runner.py`:

```
TurnRunner.submit(session, user_msg)     前置清理(残留审批/挂起)→ 追加用户消息 →
                                         记账 turn_queued → 调度排队
TurnRunner.run(...)                      scheduler.acquire → orchestrator.run_turn(journal)
                                         → 终态分流(成功/取消/故障回滚/审批暂存/挂起暂存)
                                         → 落盘 append_log + save_task → 摘要触发
TurnRunner.resolve_approval(approve)     同一收尾路径
TurnRunner.resume()                      同一收尾路径
```

session.lock 顺序、scheduler acquire/release/suspend 配对、回滚快照、
落盘剥内部键——所有不变量集中一处。routes.py 变成薄 HTTP 适配器(参数解析+鉴权)。
**这是唯一有风险的后端手术,用金测试保护(见 §4)。**

### 2.3 客户端:同一协议的瘦渲染器

```
store = reducer(事件流)     两端同构:attach → replay → live tail → UI 状态
```

- **web/native 的 store 各重写为纯 reducer**(这部分实质是重写,约 2000 行合计,
  但换来的是删掉全部补偿逻辑后反而更短);
- **共享测试夹具**:录制真实 journal(JSONL)入库 `protocol/fixtures/`,
  vitest(web)与 XCTest(native)回放同一批夹具断言最终 UI 状态一致——
  TS 和 Swift 不能共享代码,但可以共享行为标准;
- **类型单源**:事件 schema 用 JSON Schema 定义,quicktype 生成 TS + Swift 类型
  (或维持手写,但以夹具锁死);
- **outbox**:发送前本地暂存,收到 `turn_queued` 才清——multipart 没传完就退出的
  真丢失窗口由客户端兜住;
- **错误语义反转**:断开是常态不是异常——静默重附着,不弹红字。

### 2.4 "重 task 轻会话"的落位

会话仍是**记忆容器**(上下文/KV/历史);**任务成为交互单元**:
提交即得 turn_id、排队位次可见、跨会话任务中心吃全局 feed、通知由生命周期事件
直接驱动。后端调度器本来就是任务为中心的,这次是把 API 和前端拉齐到同一世界观。

### 2.5 明确不动的东西

引擎、编排护栏、记忆三层、调度器、全部工具与沙箱、摄入层、RAG、模型注册表、
devctl——一行不动。`PromptBuilder`/`ResponseParser`/`patch/` 仍是仅有的
gemma 方言层,协议改造完全不触碰。**不换语言、不换框架、不引入新第三方编排库。**

---

## 3. 迁移计划(四期,每期独立可交付)

### Phase 0 —— 契约冻结(1 个工作批次) ✅ 2026-07-11 完成
- `PROTOCOL.md`:事件 schema v1、状态机图、端点定义、兼容策略——已冻结;
- `protocol/fixtures/`:001 成功轮 / 002 审批中态 / 003 未知事件容忍;
- `backend/tests/`:FakeEngine + 15 项编排层生命周期金测试(全绿)。

### Phase 1 —— 后端脊柱(2~3 个批次) ✅ 2026-07-11 完成
- 事件账本 `core/journal.py`(内存环+journal.jsonl+seq+状态跟踪+订阅分发);
- `core/turn_runner.py` 收敛三份端点拷贝;orchestrator emit → 账本句柄(必传),
  **顺带修复**:审批批准的敏感调用漏发 tool_started/result(orchestrator 旧 914 行)、
  schedule 运行无事件(现与交互轮同契约,origin=schedule);
- 新端点 `/turns`(202) `/turns/approval` `/turns/resume` `/turn` `/events`(重放+续播);
  旧 `/stream` `/approval/stream` `/chat` `/approval` `/task/resume` 全部改为
  TurnRunner 薄适配器 + 旧事件名翻译层(无双路逻辑);
- 金测试 32 项全绿(编排 15 + 账本 6 + TurnRunner 11);
- 实机验证(e4b-4bit 真模型):202 受理→生成中附着 /events 完整生命周期→落盘对账;
  完结后重放+游标续播;旧 /stream 事件契约逐帧核对;真取消;409 冲突即拒。
- **实机发现并修复**:受理冲突闸门原在 session.lock 内→运行中的轮次持锁数分钟,
  409 永远打不出、断连请求还会复活执行;改为锁外快路径+锁内复检(回归测试钉住)。
- **实机发现(未修,记入 Phase 3+)**:MLX prefill 是整块同步计算,期间事件循环
  完全饿死——新会话完整 prompt 预填充 ~11s 内所有 API 无响应(心跳也发不出)。
  这是"刚发送时无响应感"的另一半根因;方向:prefill 分块让出 / 推理下沉专用线程。

### Phase 2 —— 客户端换脊柱(web 2 批次,native 2 批次)
- **web ✅ 2026-07-11 完成**:`lib/turn-reducer.ts` 纯归约器(夹具回放 4/4 绿,
  vitest 落地为 `npm run test`);useChatStore 换脊柱——附着流(单附着策略:
  活跃会话一条 /events 长连,断线静默指数退避重连,切走即断、切回经
  /history + /turn + 重放对账)+ outbox(202 受理即清,刷新致死后回收提示);
  补偿动物园整体退役(inflight provisional / scheduleInflightReconcile /
  taskPollers / 双断线补偿路径全删);审批/恢复改 202 受理 + 事件流收结果。
  实机验收(真模型+Playwright):生成中刷新 0s 恢复直播;**审批中刷新 1.8s
  恢复审批卡(旧架构的死局)**;turn_ended 在线对账无幽灵重复;拒绝→续跑→落地。
  注:/history 响应与 turn_ended 事件到达顺序不定,两侧各有 turnIndex 去重护栏。
- **native ✅ 2026-07-11 完成(v0.10.0,macOS 已装 /Applications,iOS build 10
  已推 TestFlight)**:`TurnReducer.swift` 归约器(读同一批 protocol/fixtures,
  `Tests/run_reducer_fixtures.sh` 3/3 绿)+ AgentStore 换脊柱(202 受理+附着流,
  静默退避重连);退役 pollUntilIdle/resync 链与暂存轮热循环;顺修三个存量 bug
  ("ok"≠"success" 致成功工具显示 ✗、resume 全程无反馈、tool_result 详情丢弃);
  暂停态保留直播预览;别端发起的轮次经附着流实时可见。旧契约 E2E 19 项照跑全绿
  (兼容层未破)。
- 夹具双端回放测试全绿:web vitest 4/4 + Swift 3/3,同一批夹具。
- **验收:在轮次任意阶段(生成中/工具中/审批中/挂起中)刷新或杀进程,
  回来 ≤1 秒恢复完整直播视图(含审批卡);两端行为一致。**
  (web 已由 Playwright 实机验收;native 的杀 app 恢复待用户真机查验。)

### Phase 3 —— 粒度与任务中心(1~2 个批次) 🟡 3a 完成(2026-07-11)
**3a ✅**:粒度事件全链路——
- `tool_call_pending`:挂在 VisibleTextStreamer(它看得到全部原始流,与约束采样
  开关无关)——工具块内一解析出 name 即预告,实测**执行前即可见**;
- thinking 逐段:同一过滤器直播 thought 频道(实测单轮 152 段 vs 原整块 1 段);
  流式回调收敛到编排层集中注入(单一写入者,任何调用方自动获得完整直播);
- `queue_moved`:调度器位次广播 → 会话账本;/turn 附 queue_position——
  单 worker 忙碌时排队不再是黑盒(实测 B 会话 queued·位次 0 + 2 条位次事件);
- 全局 feed `GET /api/v1/events`:各会话账本生命周期事件镜像聚合(环形缓冲+
  feed_seq 游标,turn_ended 瘦身),实测跨 3 会话聚合正常;
- 双端消费:归约器 pending 步语义(夹具 004 双端绿),活动行显示"准备调用 X…"/
  排队位次;后端金测试 48 项全绿。
**3b ✅ 2026-07-11**:两端任务中心/通知改吃全局 feed——feed 作"该刷新了"的门铃
(生命周期事件到达即刷 /tasks 快照,快照仍是秒数等数据的权威),固定轮询降级为
30s 慢对账兜底(原 web 3s / native 6s);native 后台通知从"消失即完结"的差分猜测
改为 turn_ended / awaiting_approval 事件直达(审批等待也能通知了)。
web 步骤时间线的 tool 级卡片已由 Phase 2 归约器步骤流覆盖(含详情),不再单列;
- ~~prefill 事件循环饿死~~ **✅ 提前完成(2026-07-11,用户实测反馈"任务期间像掉线"
  后拉前)**:整个推理宇宙下沉专用线程——`AgentEngine.create()` 在推理线程内加载
  模型并生成,增量经 call_soon_threadsafe 回主循环。实测生成全程 API 平均延迟
  6ms(改造前 prefill 窗口 ~11s 全冻结),取消 0.1s 生效。
  **踩坑记录(勿回退)**:MLX 流与惰性张量都绑定创建线程——补丁的 generation_stream
  须用 `mx.new_thread_local_stream`(对齐上游);模型 __init__ 里的非参数惰性常量
  (rope 频率等,不在 parameters() 树)跨线程首次物化必报 "There is no Stream(gpu,N)
  in current thread",唯一稳妥解法是 load 本身进推理线程(engine.create)。
- **验收:发一个多工具任务,从提交到收尾无超过"一次生成时长"的信息真空;
  工具名在执行前即可见;~~新会话首轮期间 /turn、/events 心跳保持响应~~ ✅。**

### Phase 4 —— 清账
**4a ✅ 2026-07-11(退役旧契约)**:
- 退役端点:`POST /chat/{sid}/stream`、`/approval`、`/approval/stream`、
  `/task/resume`(阻塞)及整个旧事件名翻译层(实测均 404);
  `POST /chat/{sid}`(阻塞)保留为 App Intents/脚本的便捷入口(仍走 TurnRunner);
- `/history` 撤下 inflight 字段(客户端统一经 /turn + /events 恢复;
  inflight.json 只剩崩溃防丢一个职责);
- 两端删净旧契约客户端代码(web sendChat*/sendApproval*/阻塞 resume;
  native chatEvents/approvalEvents/AgentEvent 解码层);
- 原生 E2E 全部迁移到账本协议(20 项全绿:202→/turn→附着→对账→分支/回退/资产)。
**4b ✅ 2026-07-11(用户决策后)**:
- 会话元数据服务端一等:PATCH /meta 改名(双端同源),SessionManager.set_meta
  落 meta.json;**工作区(分组)砍掉**(用户拍板)——web 独有+ui_state blob 双头
  真相病灶清除,会话回归纯"最近"列表;ui_state.json 端点退役,元数据改
  localStorage 缓存 + /sessions 对账;
- deck 打印自托管(用户拍板"打包进后端"):devctl bundle-print → backend/static/
  webapp;pdf_export 两级选宿主(前端可达优先,否则后端自托管+Bearer 注入),
  实测 self-hosted 路径渲染器加载执行成功——**后端→前端倒挂解除**;
- 顺带:/memory total_turns 统一落盘轮数(冷热一致);ARCHITECTURE.md 交互面重绘。

**重整工程全部完成(Phase 0~4)。** 交付脊柱:事件账本(先记账后分发)+
可寻址轮次 + 双端瘦渲染器 + 协议夹具锁行为 + 48 后端金测试 + 20 原生 E2E。

**总量估计:9~12 个工作批次**(按现在"一批实现+真机验证"的协作节奏)。
期间**冻结两端的新功能批次**(v0.9 式的功能迭代先停),否则全是返工。

---

## 4. 测试与验证策略(本项目当前最缺的一环)

1. **后端金测试(新增,Phase 1 起)**:FakeEngine 脚本化驱动,无模型、毫秒级、
   确定性;覆盖全部轮次生命周期与回滚/落盘/调度配对不变量。这是让"改后端不怕
   功能性破坏"成立的东西。
2. **协议夹具双端回放(新增,Phase 2 起)**:录制 journal 入库,web/native
   各自回放断言 UI 状态——两端漂移从"上真机才发现"变成"CI 就红"。
3. **保留** apple-app `Tests/run_e2e.sh` 式的真后端冒烟。
4. **手工验收清单**:六个阶段×刷新/杀进程/切后台、审批决议、挂起恢复、
   双端同会话并用。

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| TurnRunner 收敛动到不变量密集区 | 金测试先行(Phase 1 内先写测试再动代码);兼容层保留可整体回切 |
| journal 磁盘增长 | 只落里程碑事件;随会话清理一起回收;增量不落盘 |
| 兼容期双路径漂移 | 旧端点内部走新路径(只是包装),不存在两套逻辑;Phase 4 时间盒收口 |
| 两端 reducer 重写引入新 bug | 夹具回放测试 + 分端分批上线(web 先) |
| 迁移期间想加新功能的诱惑 | 明确冻结;新需求记 `PROTOCOL.md` 附录,Phase 3 后统一消化 |

## 6. 判决书:为什么不拆了重做

推倒重来要重新冒险的恰恰是最难复制的部分——MLX 补丁层、按真实使用调出来的护栏
参数、记忆/调度语义;要付出数周零产出窗口;而终点大概率就是本文的目标架构减去
成熟度。**分期换脊柱能以可验证的小步到达同一终点,且每一步系统都在变好。**
真正接近"重写"的只有两端的 store(它们本来就该死)和 routes 的对话路径
(它本来就是三份拷贝)。
