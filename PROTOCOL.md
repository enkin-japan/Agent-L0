# Agent 交互协议 v1(事件账本契约)

> 状态:**冻结**(Phase 0 产物,2026-07)。实现分期见 `REARCHITECTURE.md` §3。
> 本文是后端、web 前端、原生 app 三方的**单一行为标准**;任何一方的实现分歧
> 以本文为准。修改本文 = 修改协议版本,须过评审。
> **架构原则与开发铁律见 `CHARTER.md`（本文是它铁律 2/4/5 的具体契约）。**

---

## 0. 设计原则

1. **先记账,后分发**:编排循环把每个事件无条件写入会话事件账本;SSE、状态查询、
   任务中心、通知全部是账本的**读者**。不存在"某条路径忘了发事件"的可能。
2. **轮次(turn)是资源**:有 `turn_id`、有状态机、可查询、可重放。断开连接不改变
   轮次的任何状态。
3. **`turn_ended` 是唯一权威终态**:过程事件(增量/工具/计划)只作实时预览,
   最终一律以 `turn_ended` 携带的完整轮次记录对账(与旧契约的 `turn_end` 语义一致)。
4. **暂停不是结束**:等待审批(`awaiting_approval`)与挂起(`suspended`)是轮次的
   **暂停态**,轮次仍然"活着"、状态可查询、事件流不关闭语义(连接可以断,重连即续)。
5. **向前兼容**:客户端必须忽略未知事件类型与未知数据字段;新增事件类型不升协议版本,
   改变既有字段语义才升。

---

## 1. 轮次状态机

```
                    POST /turns
                        │
                        ▼
                   ┌─────────┐   scheduler 准入   ┌─────────┐
                   │ queued  │ ─────────────────▶ │ running │
                   └─────────┘                    └────┬────┘
                        ▲                              │
                        │ approval 决议 /              ├─ 命中敏感工具 ──▶ awaiting_approval ─┐
                        │ resume                       ├─ 挂起信号   ──▶ suspended ──────────┤
                        └──────────────────────────────┴──────────────◀────────(重新排队)────┘
                                                       │
                                                       ▼
                                              ┌──────────────────┐
                                              │      ended       │
                                              │ success│warning  │
                                              │ fail│cancelled   │
                                              └──────────────────┘
```

- **queued**:已受理(用户消息已入历史、inflight 已预写),等待单 worker 调度准入。
- **running**:占用推理 worker,编排循环推进中。
- **awaiting_approval**:命中敏感工具,worker 已让出,等待用户决议。
  决议(批准/拒绝)后重新排队续跑。
- **suspended**:在迭代边界安全让出 worker(KV 留内存),等待恢复。
- **ended**:终态,轮次记录已落盘 `history.jsonl`。四种收尾:
  `success` / `warning`(护栏熔断等) / `fail`(引擎故障,整轮回滚) /
  `cancelled`(用户取消,整轮回滚)。

**不变量**
- 一个会话同一时刻**至多一个未 ended 的轮次**(由会话锁与放弃规则保证)。
- 暂停态(awaiting_approval / suspended)下用户直接发新消息 = **放弃**旧轮:
  旧轮立即以 `warning` 落盘收尾(`turn_ended`),新轮随后 `turn_queued`。
- `cancel` 信号对 queued / running / 暂停后续跑中的轮次均有效,收尾一律
  `cancelled` + 整轮回滚(用户消息与中间产物退出模型记忆,存档保留)。
- 崩溃恢复:进程死亡时未 ended 的轮次,重启后由 write-ahead 标记补 `fail` 轮存档
  (沿用 `recover_inflight_turns`,账本不参与崩溃恢复)。

---

## 2. 事件模式

### 2.1 信封

```json
{"seq": 42, "turn_id": "t_9f3c2a", "ts": 1783600000.123, "type": "tool_started", "data": {...}}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `seq` | int | **会话内**单调递增(从 1 起),账本追加时分配。重连游标就是它 |
| `turn_id` | string | 轮次唯一 ID(服务端生成,`t_` + 12 hex) |
| `ts` | float | Unix 秒 |
| `type` | string | 事件类型(下表) |
| `data` | object | 事件负载 |

协议版本不进信封:SSE 流的第一帧固定为 `stream_attached` 事件(见 §2.4),
其中携带 `v: 1`;账本落盘文件首行为 `{"kind": "journal_header", "v": 1}`。

### 2.2 事件类型总表

**生命周期(★=里程碑,持久化进 journal.jsonl)**

| type | data | 说明 |
|---|---|---|
| ★ `turn_queued` | `{prompt, attachments: [{name,kind}], gen: {enable_thinking, thinking_budget, max_tokens, temperature, min_p}, origin}` | 轮次受理。`origin`: `chat`(用户消息) / `schedule`(定时任务) / `intent`(快捷指令等)。用户气泡据此渲染 |
| ★ `turn_started` | `{}` | 调度准入,开始占用 worker(含审批/恢复后的再次准入) |
| ★ `awaiting_approval` | `{pending_tools: [{name, arguments}]}` | 转入等待审批(worker 已让出) |
| ★ `suspended` | `{}` | 转入挂起(worker 已让出,KV 留内存) |
| ★ `resumed` | `{trigger: "approve" \| "deny" \| "resume"}` | 暂停态被决议/恢复,重新排队(随后有新的 `turn_started`) |
| ★ `turn_ended` | 完整轮次记录(见 §2.3);journal 持久化时瘦身为 `{status, turn_index}` | **唯一权威终态**。status ∈ success/warning/fail/cancelled |

**过程(实时预览;增量类不持久化,仅活跃轮内存环可重放)**

| type | data | 持久化 | 说明 |
|---|---|---|---|
| `iteration_started` | `{n, thinking: bool}` | 否 | 编排循环第 n 次生成开始(n 从 1 起)。客户端据此显示"思考中/生成中" |
| `thinking_delta` | `{text}` | 否 | 思考文本增量,append-only。Phase 1 阶段整块一次发出,Phase 3 起小段流式——客户端实现**不得**依赖块边界 |
| `token_delta` | `{text}` | 否 | 可见正文增量,append-only(临时预览,以 `turn_ended` 对账) |
| `tool_call_pending` | `{name}` | 否 | (Phase 3)约束采样状态机检测到模型正在生成对该工具的调用——执行前预告 |
| ★ `tool_started` | `{call_id, name, args}` | 截断 2000 字符 | 工具开始执行。`call_id` 配对 started/result |
| `tool_progress` | `{call_id?, label, detail?}` | 否 | 长耗时工具的执行中进度(web_research"正在阅读…"等);`call_id` 可缺(旧进度钩子) |
| ★ `tool_result` | `{call_id, name, response, status: "ok" \| "timeout"}` | 截断 2000 字符 | 工具执行完毕。`response` 已按观察上限截断;工具内部错误以 `status:"ok"` + response 文本表达(与模型看到的一致) |
| ★ `plan_updated` | `{task: TaskSnapshot \| null}` | 是 | 计划创建/修改/清除后的完整快照。`null` = 计划被清除 |
| ★ `step_completed` | `{title, result}` | 是 | 计划推进一步 |
| ★ `step_failed` | `{title, reason}` | 是 | 步骤失败并进入失败路由 |
| `queue_moved` | `{position}` | 否 | (Phase 3)排队位次变化(0 = 下一个) |

`TaskSnapshot` 沿用现状:`{goal, plan: [{id, title, status, result?}], replan_count, ...}`
(`task_state.snapshot()` 的现有形状,不改)。

**规则**
- 同一轮内事件按 `seq` 全序;`tool_started` 必先于同 `call_id` 的 `tool_result`;
  并发只读批的多组 started/result 可交错。
- 增量事件(`thinking_delta`/`token_delta`)只保证**活跃轮**重放可得;轮次 ended 或
  进程重启后不可重放——已 ended 的轮次以 `turn_ended`(或 `/history`)为准渲染。
- 里程碑事件对整个会话生命周期可重放(journal),随会话清理一并回收。

### 2.3 `turn_ended` 负载(完整轮次记录)

与现行 `history.jsonl` 行 / 旧 `turn_end` 事件同构(不新造形状):

```json
{
  "status": "success",
  "prompt": "…", "image": [], "audio": [], "ingested_text": "", "attachments": [],
  "enable_thinking": false, "thinking_budget": 2048, "max_tokens": 4096,
  "temperature": 0.2, "min_p": 0.05,
  "response": [ {"role": "assistant", "tool_calls": […], "thoughts": […]},
                {"role": "tool", "tool_responses": […]},
                {"role": "assistant", "content": "最终答复"} ],
  "prompt_tokens": 1234, "generation_tokens": 567, "generation_tps": 21.3,
  "token_breakdown": {…}, "generations": 3,
  "turn_index": 7,
  "timestamp": 1752710400.5
}
```

新增字段仅 `turn_index`(落盘后该轮在 `history.jsonl` 中的下标,分支定位用;
暂停态放弃收尾的轮同样落盘并占下标)。SSE 实时下发**全量**;journal 持久化瘦身为
`{status, turn_index}`(重启后重放旧轮靠 `/history` 补全,客户端本就如此加载)。

`timestamp`(additive,2026-07-17;不升版本):轮次完结的 epoch 秒,落盘时由
`append_log` 盖章——落盘是唯一漏斗,故 `/chat` 响应、`turn_ended` 事件与
`history.jsonl` 三面同值。**可选**:旧历史行没有,客户端缺省不显示时间(§0.5)。

#### 2.3.1 工具观察的渲染格 `render`(additive,2026-07-16;不升版本)

`response` 里 `role:"tool"` 的 `tool_responses[]` 条目**可选**携带 `render`:

```json
{"name": "rag_search", "response": "[Search results …] --- Result 1 (Match Score: 0.91, …",
 "render": {"hint": "citations",
            "payload": {"query": "报销流程", "items": [
              {"source": "员工手册.md", "path": "/kb/员工手册.md",
               "score": 0.9123, "snippet": "报销需在事发后 30 日内提交。"}]}}}
```

- `hint`:`chart | table | slides | code | diff | citations | text` 之一。
  **客户端遇到不认识的 hint 一律降级为灰框**(与未知字段同一条向前兼容规矩,§0.5)。
- `payload`:该 hint 的投影数据,形状由 hint 决定。
- **不声明 = 现状灰框**。故这是零破坏的增量迁移,工具可逐个接入。

**hint 是账本里的数据,不是渲染代码**:后端只声明"这是什么",三端各自决定"怎么画"
(铁律 4:客户端是薄渲染器)。工具直接返回 UI 组件在本项目物理不可能——
后端 Python、web TS、原生 Swift。

**`render` 不进模型上下文**:`tool_responses[]` 条目是"模型上下文 + 渲染源"两用面,
模型可见键走白名单 `{name, response, content}`(见 `prompt_builder._MODEL_VISIBLE_KEYS`)。
`render` 只朝客户端与落盘,不朝模型——否则整份引文列表会白烧 token 并打掉 KV 前缀。

**载荷在结构还活着的地方产出**:后端工具返回 `ToolOutput(text, payload)`
(`mcp/base.py`),`text` 给模型、`payload` 给客户端。故载荷**不吃观察截断**
(截断只护模型上下文),也不必让谁从格式化文本里再解析回结构。

**为什么挂在观察条目上而非另开 `turn_ended.renders[]` 平行面**:`tool_responses[]`
条目不带 `call_id`,平行面只能靠下标 join,错位即"图配错工具结果";挂在条目上天然对齐。

夹具:`007-render-hint`(该字段穿过两端归约器不被吞——两端 transcript 均为不透明子树)。

### 2.4 传输:SSE 附着流

```
GET /api/v1/chat/{sid}/events?after={seq}        (Accept: text/event-stream)
```

- 首帧固定:`stream_attached`,data = `{"v": 1, "last_seq": <当前账本游标>}`
  (信封 `seq`/`turn_id` 为 0/null——它是流控制帧,不入账本)。
  ⚠️ 「游标」不等于「账本里最后一条记录的 seq」:重启直后(以及冷会话的只读实例)
  它是**号段水位** = 已落盘最大 seq + `SEQ_RESTART_RESERVE`,账本里并不存在这个号的
  记录(见 §3)。作为 `after` 游标语义完全成立(下一条新事件必然大于它),但不要把它
  当成「最后一条事件的编号」去做算术。
- 随后按序重放 `seq > after` 的全部**可重放**事件(journal 里程碑 + 活跃轮内存环,
  按 seq 归并去重),然后转入实时续播。
- `after` 省略或 0 = 从账本起点重放;`after = last_seq` = 只要新事件。
- 心跳:静默期每 15s 一条 SSE 注释帧(`: ping`),与现行一致。
- 断开重连:客户端记住已消费的最大 seq,带 `after` 重连即可,**任何时刻、任意次**。
- 服务端不保留断线客户端的任何状态(账本本身就是状态)。
- **会话不存在 → 404**(本流唯一的非 2xx 终局)。会话是懒创建的:sid 由客户端生成,
  服务端要到首条消息(§2.6 `POST /turns`)受理才落地。故 §2.5 的标准恢复流程用在
  **尚未发过消息的新会话**上时,`/turn` 返 200(`turn: null`, `last_seq: 0`)而本流必然
  404——这是常态,不是错误。`/turn` 与 `/history` 容忍缺席会话、本流不容忍,是刻意的:
  前两者是快照(空快照有意义),本流是长连(无账本可附着)。
- 客户端契约:收到 404 即**停止附着且不重连**(退避重连无意义——会话不会自己出现),
  待 `POST /turns` 受理后再附着。404 亦可能是会话已被清扫/删除,处置相同。
  判定必须读 HTTP 状态码,**不得匹配错误文本**:附着循环的 catch 通常连帧解析与
  归约器的异常一起罩着,而那些异常文本里同样可能出现 `404`(如 JSON 解析报的
  `at position 404`),匹配字符串会把可重连的故障误判成不可重连,静默断流。

SSE 帧格式沿用现行:`event: <type>\ndata: <信封整体 JSON>\n\n`。

### 2.5 状态查询(即时恢复,免重放)

```
GET /api/v1/chat/{sid}/turn
```

```json
{
  "status": true,
  "turn": {                          // 无活跃轮时为 null
    "turn_id": "t_9f3c2a",
    "state": "running",              // queued|running|awaiting_approval|suspended
    "since": 1783600000.1,           // 进入当前 state 的时刻
    "queue_position": 0,             // 仅 queued 时有意义
    "prompt": "…",                   // turn_queued 的回显(用户气泡)
    "partial_text": "…",             // 已流出的可见正文(token_delta 累计)
    "partial_thinking": "…",         // 已流出的思考(thinking_delta 累计)
    "pending_tools": [...],          // 仅 awaiting_approval 时非空
    "task": {TaskSnapshot 或 null},
    "first_seq": 118, "last_seq": 143 // 本轮在账本中的 seq 区间(重放定位)
  },
  "last_seq": 143                    // 账本游标(attach 用);重启直后是号段水位,见 §2.4 与 §3
}
```

**客户端标准恢复流程**(刷新/杀进程/切后台回来,一律同一条路):
`GET /turn` 立即渲染当前状态(含审批卡/挂起横幅/进行中预览)→
`GET /events?after=turn.first_seq-1`(要完整时间线)或 `?after=last_seq`(只要后续)。

### 2.6 提交与控制

```
POST /api/v1/chat/{sid}/turns              multipart,字段与现行 /stream 相同
  → 202 {"status": true, "turn_id": "t_…", "seq": 118}     受理即返,不长连
  → 409 {"status": false, "reason": "turn_active"}          已有未 ended 轮(队不进同会话)

POST /api/v1/chat/{sid}/turns/approval     {"approve": true|false}
  → 202 {"status": true, "turn_id": "t_…"}                  决议受理;过程走事件流
  → 409 无等待审批的轮次

POST /api/v1/chat/{sid}/turns/resume       (空体)
  → 202 {"status": true, "turn_id": "t_…"}                  恢复受理;过程走事件流
  → 409 无挂起的轮次

POST /api/v1/chat/{sid}/task/cancel        (不变)信号语义,结果以 turn_ended(cancelled) 入流
POST /api/v1/chat/{sid}/task/suspend       (不变)信号语义,结果以 suspended 事件入流
```

注:`POST /turns` 对暂停态旧轮的"发新消息=放弃"语义**由服务端执行**
(先收尾旧轮再受理新轮,事件序:旧轮 `turn_ended{warning}` → 新轮 `turn_queued`),
客户端不做本地模拟。

### 2.7 全局任务 feed(Phase 3)

```
GET /api/v1/events?after={global_seq}
```

跨会话聚合**生命周期事件**(turn_queued/started/awaiting_approval/suspended/
resumed/turn_ended,信封加 `session_id` 字段)+ 定时任务运行事件。任务中心与
后台通知的唯一数据源,替代 /tasks 差分轮询。`global_seq` 独立于会话 seq。
(`GET /api/v1/tasks` 快照保留,作为低频对账与调试接口。)

⚠️ **已知缺陷(未修,2026-08-17 记)**:`feed_seq` 纯内存(`core/feed.py` 的
`GlobalFeed.seq`),**每次进程启动归零**,而两端客户端游标跨重连保留且不读首帧的
`last_feed_seq`——于是后端一重启,新条目全部撞上 `feed_seq <= 游标` 被静默丢弃。
它比 §3 已修的会话 seq 回退**更狠**:那个要攒够未落盘增量才回退,这个 100% 重启即
回退。feed 无持久层,「加水位余量」的修法不适用,正解是客户端按首帧重锚(代际围栏),
与 §3 末尾那半留待同一个后续项一并解决。
当前缓解**只覆盖一半**:两端都另有 30s 级慢对账定时器(Swift `startGlobalMonitor` 的
fallback 任务、前端 `schedulerTimer`),故任务中心的**状态**最终会收敛;但被丢弃的
那些事件本身不会补发,依赖 feed 逐条触发的**通知**(轮次完结、待审批)是真丢的——
注意 dispatch 循环里 `feed_seq <= 游标` 的 `continue` 发生在刷新与通知**之前**,
不要误以为每条事件的刷新能兜住它。

### 2.8 推送投影(additive,2026-07-12;不改既有事件语义,不升版本)

通知不是新事件源——后端 `framework/push/` 的 dispatcher 是 feed/journal 的**读者**
(铁律 2),把生命周期事件映射为 APNs 推送(iOS);macOS 客户端自行订阅 feed 发本地
通知。推送丢失不丢状态,恢复仍走 §2.4/§2.5 统一路径("推送即门铃,真相在账本")。

```
POST   /api/v1/push/register                    设备注册/更新(upsert;device_id 为 app 生成的稳定主键)
       {device_id, platform, token?, push_to_start_token?, prefs?, app_build?}
DELETE /api/v1/push/devices/{device_id}         注销(幂等)
PATCH  /api/v1/push/devices/{device_id}/prefs   通知偏好 {results?, schedule_start?, approval?, content_level?}
POST   /api/v1/push/live-activity               Live Activity 活动 token 上报
       {device_id, session_id, turn_id, token} → {active: bool}   active=false ⇒ app 本地收尾孤儿活动
```

`POST /turns/approval` 请求体新增**可选** `expected_turn_id`(向后兼容):远程决议
(通知按钮/灵动岛)带上,当前等待审批的轮次不是它时 409 `approval_target_mismatch`
——迟到点击绝不错递给后来的审批请求。不带该字段的旧客户端行为不变。

APNs payload 自定义段 `ma: {v, kind, session_id, turn_id}`,kind ∈ result /
approval / approval_autodenied / schedule_start;Live Activity content-state 契约
见 `apple-app/Sources/LiveActivity/AgentActivityAttributes.swift`(与
`framework/push/dispatcher.py` 字段一一对应,改动须两边同步)。

---

## 3. 持久化与账本实现约束

- 账本 = **内存环 + journal.jsonl** 两层:
  - 内存环:活跃轮的**全部**事件(含增量),新轮 `turn_queued` 时清空上一轮;
  - `sessions/{sid}/journal.jsonl`:仅 ★ 里程碑事件追加落盘,首行 journal_header;
    `tool_started.args` / `tool_result.response` 持久化副本截断至 2000 字符并加
    `"truncated": true` 标记(SSE 实时帧不截断——以观察上限 4000 为界)。
- `seq` 恢复:进程重启后取 journal 中**已落盘记录的最大 seq**(取 max 而非读末行——
  坏行会让末行不是最大)**加一段水位余量**(1e6,`journal.SEQ_RESTART_RESERVE`)续增;
  内存环不恢复(增量丢失可接受,因为崩溃时轮次本身会被补 fail)。
  由此 **`seq` 单调但不连续**:每次重启留下一个百万级空洞。客户端只可把 `seq` 当
  排序键与游标,**不得据空洞判定「我漏收了事件」**并触发重同步。
  **余量不可省**,它正是 §2.4「任何时刻、任意次」那句成立的前提:增量事件
  (`token_delta`/`thinking_delta`)同样消耗 seq 却不落盘,直接接末行会让 seq
  **就地回退**到已用号段,而客户端游标是内存态、跨重连保留——重启后的新事件会全部
  撞上服务端直播循环的 `seq <= 已发送最大号` 判定被当成重放丢弃,会话对用户表现为
  **永久静音**。(此前本行写作「取末行 seq 续增」,与 §2.4 直接矛盾,两端实现都照
  §2.4 办;OSS 扫描 §6.3 复现,2026-08-17 修。)
  ⚠️ 余量只保证 seq 不回退。进程死时那一轮没有 `turn_ended`(恢复流程只补写
  history、不发账本事件),客户端仍会停在 generating;这一半要代际围栏(后续项,
  与 §2.7 全局 feed 的 `feed_seq` 进程内归零同源)。
- journal 随会话删除/清理(`session_cleanup`)一并回收;不设独立轮转(v1)。
- `inflight.json` 语义收窄为**纯崩溃防丢**(write-ahead);客户端不再用它探测
  进行中轮次(用 `/turn`)。
- 磁盘写入均走 `asyncio.to_thread`,与现行落盘纪律一致。

---

## 4. 兼容与退役计划

**Phase 4a(2026-07-11)已执行退役**:下表旧端点除注明保留者外均已移除(404),
翻译层随之删除;`GET /task` 的 activity 快照保留(调试价值)。历史对照:

| 旧 | 新语义下的实现 | 退役 |
|---|---|---|
| `POST /chat/{sid}/stream` | 已退役(原:提交+内联附着的翻译层) | ✅ 4a |
| `POST /chat/{sid}/approval/stream` | 已退役 | ✅ 4a |
| `POST /chat/{sid}`(阻塞版) | **保留**:App Intents/脚本便捷入口(走 TurnRunner) | 保留 |
| `POST /approval`(阻塞版) | 已退役 | ✅ 4a |
| `POST /task/resume`(阻塞版) | 已退役 | ✅ 4a |
| `GET /history` 的 `inflight` 字段 | 已退役(客户端经 /turn + /events 恢复) | ✅ 4a |
| `GET /task` 的 `activity` 快照 | 照旧(session.activity 继续维护) | 保留(调试价值) |

**旧事件名翻译表**(适配层内固定映射):

| 新(账本) | 旧(/stream SSE) |
|---|---|
| `token_delta {text}` | `token {delta}` |
| `thinking_delta {text}` | `thinking {text}`(累计后整块) |
| `tool_started` / `tool_result` | 同名同形 |
| `iteration_started` / `plan_updated` / `step_*` / `tool_progress` | `task_progress {task, activity}`(由 session.activity 快照合成) |
| `awaiting_approval` | `turn_end {status: "pending_approval", pending_tools, …}` + 闭流 |
| `suspended` | `turn_end {status: "suspended", …}` + 闭流 |
| `turn_ended` | `turn_end`(全量) + 闭流 |
| `turn_queued`+`turn_started` | `turn_start {turn_id, session_id}` |

---

## 5. 客户端归约(reducer)契约与测试夹具

两端 store 必须实现同一纯函数语义:

```
reduce(NormalizedState, Event) -> NormalizedState
```

**NormalizedState(夹具断言用的规范形,与内部实现解耦)**:

```json
{
  "turn_state": "none|queued|running|awaiting_approval|suspended",
  "transcript": [ …已 ended 轮次的消息(与 turn_ended.response 同构,前置 user 消息) ],
  "preview": {
    "prompt": "…",              // 活跃轮用户消息
    "text": "…",                // token_delta 累计
    "thinking": "…",            // thinking_delta 累计
    "steps": [ {"kind": "tool|plan", "label": "…", "detail": "…", "status": "pending|running|ok|timeout|failed"} ],
    "task": {TaskSnapshot 或 null}
  },
  "pending_tools": [ … ] 
}
```

归约规则要点:
- `turn_queued` → preview.prompt 置入,turn_state=queued;
- `turn_ended` → preview 整体清空、轮次消息(prompt + response)并入 transcript,
  turn_state=none;**以 turn_ended 为准丢弃预览**(cancelled/fail 时预览撤回);
- `awaiting_approval` → pending_tools 置入,turn_state 相应;`resumed` → 清 pending_tools;
- `tool_call_pending` → steps 追加 pending 项(生成中预告,无 call_id);
  `tool_started` → 若存在**同名 pending** 步则收编它(置 running + 配 call_id),
  否则追加 running 项;`tool_result` → 按 call_id 收尾该项;
- 未知 type → 原样忽略(必须有此用例)。

**夹具格式**(`protocol/fixtures/`):

```
<name>.events.jsonl      一行一个事件信封(客户端从 attach 收到的序列,含增量)
<name>.expected.json     终态 NormalizedState
<name>.md                (可选)场景说明
```

两端测试(web: vitest / native: XCTest)读取同一批夹具:逐行 reduce,断言终态
deep-equal `expected.json`。归约器可携带 `_` 前缀的内部簿记字段(如工具步的
`_callId` 配对键),比对前由测试运行器递归剥除——夹具只锁公共形状。
**新增协议行为必须先落夹具再改实现。**

---

## 6. 服务端事件发射点(实现对照表,Phase 1 验收依据)

| 编排位置 | 事件 |
|---|---|
| TurnRunner 受理(用户消息入历史 + inflight 预写后) | `turn_queued` |
| scheduler.acquire 返回 | `turn_started` |
| 每次 generate_once 前 | `iteration_started` |
| VisibleTextStreamer 可见增量 | `token_delta` |
| 生成后思考块(P1 整块 / P3 分段) | `thinking_delta` |
| `_handle_meta_call` 后(update_plan/complete_step) | `plan_updated` / `step_completed` |
| `_route_step_failure` | `step_failed` + `plan_updated` |
| `_invoke_tool` 进/出 | `tool_started` / `tool_result` |
| 进度钩子(runtime.set_progress_hook) | `tool_progress` |
| 审批闸门命中 | `awaiting_approval` |
| 审批决议 / resume 受理 | `resumed`(随后 `turn_started`) |
| **审批批准后的挂起工具执行** | `tool_started` / `tool_result`(修复现行漏发) |
| 挂起检查点让出 | `suspended` |
| 终态落盘 append_log 后 | `turn_ended` |

任何新增编排路径**必须**经账本句柄发射(句柄必传,无 None 分支)。
