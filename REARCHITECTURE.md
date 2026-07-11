# MacAgent 重整蓝图 —— 体检报告 · 目标架构 · 迁移计划

> 2026-07 全项目体检的产物。回答三个问题:哪些是资产(别拆)、病灶到底在哪、
> 怎么重整才能不再"补丁摞补丁"。
> 结论先行:**不拆重做。拆掉重建的是"交互脊柱",智能层原地保留。**

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
| 推理层 | `backend/framework/patch/gemma4_generate.py`(824 行)、`core/engine.py` | KV 前缀复用(prefill 提速≈10×)、TurboQuant KV 量化、prompt-lookup 投机采样、思考预算锚定、llguidance 工具段约束采样。硬件感知的深度工程,换任何框架都要重写且大概率更差 |
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

### Phase 0 —— 契约冻结(1 个工作批次)
- 写 `PROTOCOL.md`:事件 schema v1、状态机图、端点定义、兼容策略;
- 建 `protocol/fixtures/` 目录与夹具格式;
- 后端建 `FakeEngine`(脚本化生成序列,不加载模型)测试骨架。
- **验收:文档评审通过,夹具能手工构造。** 不改任何运行代码。

### Phase 1 —— 后端脊柱(2~3 个批次)
- 事件账本(内存环+journal.jsonl)+ seq;
- TurnRunner 收敛三份端点拷贝;orchestrator 的 emit 参数改为账本句柄(必传);
- 新端点 `/turns` `/events` `/turn`;旧 `/stream` `/approval/stream` 内部走新路径、
  对外行为不变;
- 金测试:FakeEngine 驱动 8 条生命周期(成功/取消/故障/审批批准/审批拒绝/挂起/
  恢复/再挂起),断言账本内容+磁盘状态。
- **验收:现有 web/native 零改动照常工作;curl 可重放任意会话事件;
  审批/挂起状态可 GET。** 出问题可整体切回旧路径(兼容层在)。

### Phase 2 —— 客户端换脊柱(web 2 批次,native 2 批次)
- web 先行(迭代快):useChatStore 重写为 reducer + attach 流程,
  删 reconciler/inflight-provisional/双补偿路径;outbox;断开静默重连;
- native 跟进:AgentStore 同构改造,删 pollUntilIdle/resync 链与热循环,
  顺修 "ok"≠"success"、resume 无轮询、tool_result 详情丢弃;
- 夹具双端回放测试全绿。
- **验收:在轮次任意阶段(生成中/工具中/审批中/挂起中)刷新或杀进程,
  回来 ≤1 秒恢复完整直播视图(含审批卡);两端行为一致。**

### Phase 3 —— 粒度与任务中心(1~2 个批次)
- `tool_call_pending`(约束采样状态机触发)、thinking 逐段、排队位次事件;
- 全局 feed `GET /api/v1/events`;两端任务中心/通知改吃 feed;
- 顺带:web 步骤时间线补上 tool 级卡片(现在扔掉的信息用起来)。
- **验收:发一个多工具任务,从提交到收尾无超过"一次生成时长"的信息真空;
  工具名在执行前即可见。**

### Phase 4 —— 清账(1 个批次)
- 退役旧端点与 inflight 前端探测;
- 会话元数据服务端一等化(workspace/title 入 meta.json,`ui_state.json` blob 退役,
  native/web 同源);
- deck 打印页自包含化(独立 HTML 随后端出货),解除后端→前端倒挂;
- 文档同步(`ARCHITECTURE.md` 重绘)。

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
