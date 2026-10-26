# Agent 交互协议 v1(事件账本契约)

> 状态:**冻结**(Phase 0 产物,2026-07)。实现分期见 `docs/history/REARCHITECTURE.md` §3（已归档）。
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
| `upgrade_triggered` | `{trigger: "explicit" \| "catalog" \| "failures", action: "consult_auto" \| "consult_suggest" \| "tier_suggest" \| "tool_hint" \| "caveat" \| "unavailable", boundaries: [id…], tier_target, reason}` | 否 | (⑤′-3)编排层升级策略触发(一轮可多条:consult 与 tier 可并存)。`consult_auto` 时紧随其后是编排层代模型发起的 `cloud_consult` 调用(照常走 `awaiting_approval`,审批卡即将出网原文);`tier_suggest`/`caveat` 只在最终答复末尾追加一行提示;`tool_hint` 只给模型一条尾注、用户面无提示(此时 `tier_target` 字段承载的是**工具名**,如 `run_math`);用户显式要求而云端不可用时 action 为 `unavailable`。consult 类/tier/tool_hint/caveat 各至多一条,可并存。`turn_ended` 顶层 `cloud_upgrade: [同形…]`(仅触发轮出现,见 §2.3) |
| `cloud_degraded` | `{call_id, name, kind}` | 否 | (⑤′-1)云端导师(`cloud_consult`)本次调用降级(`kind` ∈ cloud/degrade.KINDS:no_key/budget_exhausted/timeout/…)。紧随对应 `tool_result` 之后(同 `call_id`)。收尾:`turn_ended` 顶层 `cloud_degraded: [kind…]` 列出本轮原因序列、`cloud_consult_ok: bool` 标明同轮是否另有成功咨询(仅降级轮出现,见 §2.3);最终答复末尾由编排层确定性追加「本地作答」标注——仅当 `cloud_consult_ok` 为 false(首次超时重试成功 = 真用了云端建议,不挂) |
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

基础定位字段 `turn_index`(落盘后该轮在 `history.jsonl` 中的下标,分支定位用;
暂停态放弃收尾的轮同样落盘并占下标)。SSE 实时下发**全量**;journal 持久化瘦身为
`{status, turn_index}`(重启后重放旧轮靠 `/history` 补全,客户端本就如此加载)。

`discarded: true`（可选扩展，2026-10-11）：删除尚未落盘的 awaiting_approval /
suspended 轮时，TurnRunner 发 `turn_ended`，status 为 cancelled，response 为空，
**不带 turn_index**，不新增 history 记录。客户端仍清空预览、审批卡与挂起状态，
但不追加 transcript / 历史消息、不增加轮数；标记在 journal 的瘦身记录中保留，
重启重放同样适用。turn_index 始终只指已落盘历史下标，不用哨兵或下一条可复用
下标代替。既有终态无此标记时沿用原对账语义。旧客户端可忽略新增字段并收起卡片，
但需要更新客户端才能避免临时显示取消存档；重新加载 `/history` 后以实际历史为准。

`timestamp`(additive,2026-07-17;不升版本):轮次完结的 epoch 秒,落盘时由
`append_log` 盖章——落盘是唯一漏斗,故 `/chat` 响应、`turn_ended` 事件与
`history.jsonl` 三面同值。**可选**:旧历史行没有,客户端缺省不显示时间(§0.5)。

`sub_generations` / `sub_generation_tokens` / `goal_verify_generations` /
`goal_verify_generation_tokens`(additive,2026-08-22 F6;不升版本):子沙箱
(web/math/excel 调研)与收尾目标校验在各自一次性会话里的生成次数与 token 数,
按桶归因回父轮。**可选,只在非零时出现**;客户端按未知字段规矩忽略即可。
注意 `sub_generation_tokens` 是跨模型合计(excel 的视觉 QA 可能路由到专职视觉端点),
是算力量级仪表,不是单一 tokenizer 口径。

`execution_receipts`（可选 additive，2026-10-11 R16）：TurnRunner在正常最终落盘点
从同轮内存journal投影已有tool_started/tool_result，随history与turn_ended保存。
这份数据只作执行事实凭据，不进模型上下文，不参与任务恢复，不新增账本/端点。
取消和失败仍回滚模型上下文；磁盘、网络或其他外部副作用不因此自动撤销。

形状：
{"items":[{"seq":3,"ts":1003.0,"type":"tool_started","name":"write_file",
"call_id":"id","detail":"","truncated":true},
{"seq":4,"ts":1004.0,"type":"tool_result","name":"write_file","call_id":"id",
"detail":"工具返回文本","status":"ok"}],"truncated":true}

- items仅包含同轮最近最多64条真实开始/返回事件，按原seq顺序；ts为原事件时间。
- name/call_id最多128字符，detail最多2000字符，status最多32字符；不把完整args复制
  进凭据。开始事件detail为空，省略非空args时该item及整体truncated=true。
- 超64条、字段截断、源事件已标截断：整体truncated=true；字段截断或参数省略也在
  对应item标truncated=true。数量截断表示只保留尾部，不能宣称整轮完整操作清单。
- tool_started只证明调用开始，tool_result只证明工具返回；status=ok沿用现有返回
  状态，不保证外部操作成功。未配对的started也保留，不能推导操作没产生副作用。
- 完整可确认的本轮ring无工具事件时items=[]，仅表示未观察到工具事件；不能推导
  所有副作用为零。旧历史/崩溃恢复无该字段，或无法确认轮首身份，保持未知。
- journal.jsonl的turn_ended继续瘦身；重启后经既有history入口读取完整凭据。
- R11显式删除暂停轮仍discarded，不新增历史记录；此改动不改变其删除语义。
- 015-cancelled-execution-receipts为新增夹具；顶层未知字段可由旧归约器忽略。

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

**`cloud_upgrade`(additive,2026-08-23,⑤′-3)**:本轮升级策略触发过时出现的顶层字段,值为 `upgrade_triggered` 负载的列表(consult 与 tier 决策可并存,各至多一条;审批/挂起续跑后仍随本轮带出)。无触发不出现。

**`cloud_degraded` / `cloud_consult_ok`(additive,2026-08-23,⑤′-1)**:本轮云端导师
(`cloud_consult`)至少降级一次时出现的两个顶层字段——`cloud_degraded: ["budget_exhausted", …]`
为原因序列(cloud/degrade.KINDS),`cloud_consult_ok` 为同轮是否另有成功咨询。无降级的轮次
两字段都不出现;所有收尾出口(含迭代/子生成保险丝)都记,但「本地作答」标注只追加在正常
完成出口且 `cloud_consult_ok=false` 时。客户端按 §0.5 忽略即可,不要求渲染。


### 2.4 传输:SSE 附着流

```
GET /api/v1/chat/{sid}/events?after={seq}        (Accept: text/event-stream)
```

- 首帧固定:`stream_attached`,data = `{"v": 1, "last_seq": <当前账本游标>,
  "generation": <进程代际>}`(信封 `seq`/`turn_id` 为 0/null——它是流控制帧,不入账本)。
  `generation`(2026-08-22 起,additive):后端进程每次启动的随机代际串,与 `/turn`
  同源同值。**会话流的游标不因代际变化作废**——seq 因号段水位跨代际单调(见 §3),
  旧游标照用;本字段供客户端感知重启(诊断/对账)。全局 feed 是另一回事,见 §2.7。
  进程死时的崩溃轮由启动恢复**补发** `turn_ended`(status=fail,seq 必然大于任何
  重启前游标),重附着经重放即送达——纯事件流消费者不再停在 generating。
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
  "last_seq": 143,                   // 账本游标(attach 用);重启直后是号段水位,见 §2.4 与 §3
  "generation": "a1b2c3d4e5f60708"   // 进程代际(2026-08-22 起):与 stream_attached 同源
}
```

**客户端标准恢复流程**(刷新/杀进程/切后台回来,一律同一条路):
`GET /turn` 立即渲染当前状态(含审批卡/挂起横幅/进行中预览)→
`GET /events` 的初次 after 按下述历史读取边界取下界；已有消费游标的断线仍续播。

#### 历史与重放的读取边界（additive，2026-10-11，不升版本）

`GET /api/v1/chat/{sid}/history` 新增可选 `replay_after`（int）和 `generation`
（与 `/turn` 同源）。`replay_after` 是**读取历史之前**的账本游标下界，不能声称
历史恰好截止于它；两者可能重叠。缺会话为 0，冷会话沿用 `/turn` 的只读账本水位。
读取不等待正在生成的轮次锁，不改变既有 `turns` / `count` / `last_seq` 语义。

初次恢复按 `history → /turn → /events` 串行：`after = min(replay_after,
turn.first_seq - 1)`（有活轮），否则 `min(replay_after, last_seq)`；`turn=null`
不可把历史下界推进到后来 `last_seq`。旧服务端无新字段时下界取 0，再按既有
`turn_index` 去重。已经消费事件的临时断线仍带消费游标续播。历史失败不得标记
加载成功；切会话或换客户端使旧恢复/提交回包失去当前投影写权（包括 A→B→A）。

持久化瘦身的 `turn_ended` 若对应历史已存在，收掉该轮预览后跳过重复渲染；若
对应历史缺失，断开本次附着，合并返回**同一** history→turn→events 恢复入口。
不得把 thin 终态渲染成空答复或增加已知轮数，不增设终态恢复端点/独立轮询。
历史已经含轮次而 journal 尚未 emit terminal 的窗口也按 turn_index 收敛。
单附着句柄属于一次选中/客户端 incarnation；旧 task 的退出不能清掉新句柄。
历史重放也不得认领新的在途提交：乐观气泡按既有 `turn_queued.client_turn_id`
或 202 的 `turn_id` 绑定，**相同 prompt 不是身份凭据**。未绑定终态保留新提交的
outbox / 气泡 / 提交中标志；终态比 202 先到时，后来的回执据轮次身份收敛。
outbox 在 202 或其绑定终态清仓，历史文字相同不推断已受理；未知身份回同一
恢复入口或保留凭据供确认重发，不猜测归属。当前提交的 202/queued seq 也是
已知下界；首附着在受理后才读取历史时，after 还须不大于该 seq - 1，以回放身份
收编已完成提交的乐观气泡。`client_turn_id` 是已有 additive
提交幂等键与 journal 字段，本文仅明确其客户端使用，不改变字段语义。

共享 `014-history-replay-boundary.restore.json` 与事件夹具分别由两端真实 store
合成网络时序测试及 reducer 回放消费，覆盖缺轮/重叠/瘦身重放恢复。

### 2.6 提交与控制

```
POST /api/v1/chat/{sid}/turns              multipart,字段与现行 /stream 相同
  → 202 {"status": true, "turn_id": "t_…", "seq": 118}     受理即返,不长连
  → 409 {"status": false, "reason": "turn_active"}          已有未 ended 轮(队不进同会话)
  可选字段 client_turn_id(2026-08-22 起,additive;提交幂等键,此处为普通提交策略——
  阻塞式 /chat/{sid} 不受理该字段):蜂窝网下请求完整抵达而 202 回程丢失是常态,
  用户手动重发若被当成新提交就是一次货真价实的重复执行(可能带审批副作用)。
  键形状:`[A-Za-z0-9._:-]{8,128}`(不合形状按未带键受理)。带同键的重发 →
  202 {"turn_id", "seq", "duplicate": true} 直接返回**原轮**(不重复执行、不重复
  摄入——判重在 multipart 摄入之前短路);客户端收到 duplicate 必须**重新对账**
  (/history + /turn),原轮可能早已完结,干等事件流会永远转圈。
  判重语义:键随 turn_queued 落 journal(已完结轮的唯一持久真相,文件尾反向回查,
  近 **64 轮**内有效——幂等键防分钟级手动重发,不做无限期查重);同键撞上仍活跃的
  原轮回 202 而非 409(同一次提交的重试应引导附着,不是冲突;真并发双提交由受理
  冲突分支二次判重兜住);**fail/cancelled 的原轮不算命中**（保留普通提交的既有兼容策略）。
  这只表示模型上下文已回滚，不代表工具没有外部副作用；界面不得据此承诺安全重做。
  客户端键纪律:键随 outbox 持久,**同会话同文的纯文本重发复用旧键**;带附件的提交
  **不复用也不存键**(键只比对得了文本,同文换附件被判重会静默丢附件);网络类失败
  不清 outbox(真 409 才清),对账提示不清键(只标记已提示);换了内容即换键。

POST /api/v1/chat/{sid}/turns/approval     {"approve": true|false}
  → 202 {"status": true, "turn_id": "t_…"}                  决议受理;过程走事件流
  → 409 无等待审批的轮次

POST /api/v1/chat/{sid}/turns/resume       (空体)
  → 202 {"status": true, "turn_id": "t_…"}                  恢复受理;过程走事件流
  → 409 无挂起的轮次

POST /api/v1/chat/{sid}/task/cancel        (不变)信号语义,结果以 turn_ended(cancelled) 入流
POST /api/v1/chat/{sid}/task/suspend       (不变)信号语义,结果以 suspended 事件入流
```

控制请求的界面反馈不新增轮次状态：发送中、已受理等待安全点与拒绝原因属于
当前请求的投影。HTTP 成功不等于已暂停/已停止，状态仍由后续账本事件确认；
不得用固定超时清掉等待提示来模拟完成。挂起仅对 running 提供，停止仅对
queued/running 提供；paused 继续使用已有恢复/放弃入口。请求绑定客户端、选中
会话代次、轮次和发送前已观察的 seq 下界，旧重放不能确认新请求。现有 cancel /
suspend 端点仍按会话控制，这些客户端绑定不构成服务端 expected_turn_id 保证。
停止只阻止后续执行，已完成的磁盘、网络等副作用不会自动撤销。

注:`POST /turns` 对暂停态旧轮的"发新消息=放弃"语义**由服务端执行**
(先收尾旧轮再受理新轮,事件序:旧轮 `turn_ended{warning}` → 新轮 `turn_queued`),
客户端不做本地模拟。

`POST /turns/approval` 的追加可选字段 `expected_approval_seq`（2026-10-11，
additive，不升版本）：取卡片所展示的 `awaiting_approval` **信封 seq**，与可选
`expected_turn_id` 一起绑定决议对象。同一轮可反复等待审批；当前审批批次的 seq
与请求不符时也返回 409 `approval_target_mismatch`，不执行该批工具。该身份由
账本 `emit` 单点记录，后续进度/增量事件不会改变它；不能拿 `/turn.last_seq`
代替。恢复仍从 `GET /turn` 返回的 `turn.first_seq` 之前附着重放，取得真实审批事件的 seq。
字段为正整数或 null；省略/null 保留旧客户端行为，未知请求字段沿用忽略策略。
现有 `expected_turn_id` 的字段语义、事件负载和 v1 信封均保持不变。

#### 显式任务重试（additive，2026-10-11，不升版本）

```
POST /api/v1/chat/{sid}/turns/retry
JSON {expected_turn_ref: opaque string, client_turn_id: fresh command key}
```

返回原202 {status,turn_id,seq[,duplicate:true]}。每次明确重试为同会话新轮；完整输入
由后端从末条canonical存档读取并独立复制，原轮/输出/全部工具过程保持可审查。
不重放旧tool_calls；仍走普通编排/预算/审批/sandbox。相同retry key判重先于源引用，
fail/cancelled也不重复执行；新点击使用新key，不能复用原提交键。

缺会话404；无存档/过期source/活跃或暂停轮/缺失或不属于本会话输入的媒体拒绝，
不删除原轮、不触发模型/工具。不支持此能力的旧服务/缺token必须显式失败，禁止先删
历史或纯文本fallback。ASR/slash已固化的prepared输入不会再摄入/重复注入。

可选 additive 字段：

- full public terminal.turn_ref：完整公开record确定性hash；排除内部键及顶层
  turn_ref/turn_index派生字段，包含timestamp/input/gen/response/receipt等公开内容。
- HistoryResponse.last_turn_ref：由实际返回raw turns末行同一快照派生；旧行无timestamp
  或原turn_id也可使用。raw turns不为该字段被重写。
- retry_of：即时parent的内容ref（审计关联，分支复制路径变化后不能当永久root）。
- retry_root_index/retry_parent_index：真实canonical有效行下标，随retry queued/终态/
  raw history持久；只从锁内原源记录推导，客户端不能提供索引作为许可。
- HistoryResponse.display_turn_indices：同一raw快照纯投影；root占原槽，只有最新
  status=success尝试替换该族显示winner。fail/cancelled/warning保留此前答案；
  全部尝试仍在raw turns/count/journal，供开发者和Agent审查。旧行各自显示。
  root/parent类型、范围、紧邻parent、同族继承或retry_of形状不合法时该行独立显示，
  不能吞其它记录。原count/turn_index/下标语义不改。

Branch必须用display winner真实canonical下标，而不是display数组位置；前缀分支
保持root/parent关系，media重挂导致内容hash改变不损坏族展示。只受控改写明确
input old→new的ingested provenance，不改工具receipt的历史陈述或扩张assets复制。
未知/外部/失效provenance不能静默重新读取。

无未落盘暂停轮时，DELETE last仍只撤销canonical尾尝试，去掉其独立媒体并重算剩余
winner，不隐式批删root或审计行。有 awaiting_approval/suspended 活轮时仍沿 §2.3
的 discarded 规则撤销该活轮；编辑取该活轮的原始 prompt，不能错用前一条存档。
客户端应标“撤销/编辑最近一次尝试”；原D1编辑并重发语义不变。

整会话 `DELETE /api/v1/chat/{session_id}` 的回执可附加 `reason: "not_found"`：
仅在服务端确认目标既无热实例、也无目录时返回。原 `status: false` 保持不变；
客户端可凭此明确缺席回执移除本地空会话或失效缓存。其它失败（包括无 reason 的
旧端失败）仍保留条目，不能根据显示文案或本地轮数猜测缺席。该回执不保证未来或
其它客户端不能用同一 ID 发起新请求。共享 HTTP 夹具见 `017-delete-absent.responses.json`。

016-task-retry-inputs.events.jsonl + expected.json守护原审批与additive兼容；
016-task-retry-inputs.history.json守root success→fail→success→warning→cancel→success
→ordinary及每个prefix投影。字节复制、失败清理、source guards、branch输入独立性
由CPU真实TurnRunner/session测试守护。

明确retry幂等为严格全journal流式key查找（额外内存常量，不累计终态dict）；普通submit
的近64轮窗口与failed/cancelled可重发策略保持。磁盘查重失败、坏 JSON/UTF-8/非对象行或畸形受理见证时明确拒绝，而非
当成查无。显式重试扫描不受普通读取的 4 MiB 尾窗限制；缺失整个 journal 的旧会话
保持兼容，不承诺外部删除账本后的永久恰好一次。很旧/全新retry key会扫描更长journal，代价为后台线程磁盘时间。
受理在复制或write-ahead阶段被重复取消时先等实际worker完成，再清理本次新文件/
恢复模型内存与旧marker，不允许已退出请求的线程在cleanup之后再落盘。

`retry_groups`（HistoryResponse可选additive）为 [{root_index,display_index,turn_indices}]，
仅返回包含>=2条真实canonical记录的合法族。与display_turn_indices共用同一合法性
验证遍历和同一raw快照；只传真实下标，不复制完整record，不是第二账本。客户端
可在winner旁显示“尝试记录”折叠入口，按turn_indices用既有renderer呈现每次输入、
结果及execution_receipts（包括fail/cancel/warning）。不得各自重写root合法性算法，
不得因展开记录自动重执行。若可选 groups 缺失、畸形或不能让所有隐藏 raw 记录
通过审计入口访问，客户端退回全部 raw 展示，不能静默隐藏记录；这里只检查可见
覆盖，不重写服务端族算法。旧客户端忽略该字段兼容。


新受理轮的崩溃防丢标记保留完整输入及生成设置；重试另保留已验证的尝试关系。
恢复仅补 fail 存档，不重新摄入、复制或执行工具。遗留标记未保存完整输入时，
新恢复行用可选 `retry_input_unavailable: true` 如实标注；新显式 retry 返回
`retry_inputs_unavailable`，请用户检查记录并重新附上所需输入，不静默降为纯文本。
此约束不按字段缺席泛拦普通旧历史，同 key 已受理回执仍先于源校验判重。

### 2.7 全局任务 feed(Phase 3)

```
GET /api/v1/events?after={global_seq}
```

跨会话聚合**生命周期事件**(turn_queued/started/awaiting_approval/suspended/
resumed/turn_ended,信封加 `session_id` 字段)+ 定时任务运行事件。任务中心与
后台通知的唯一数据源,替代 /tasks 差分轮询。`global_seq` 独立于会话 seq。
(`GET /api/v1/tasks` 快照保留,作为低频对账与调试接口。)

首帧 `stream_attached`,data = `{"v": 1, "last_feed_seq": <当前 feed 游标>,
"generation": <进程代际>}`。**代际围栏(2026-08-22 修)**:`feed_seq` 纯内存
(`core/feed.py`),每次进程启动归零,而客户端游标跨重连保留——不重锚的话,重启后的
新条目全部撞上 `feed_seq <= 游标`(服务端直播循环与客户端 dispatch **双侧**都有这个
判定,且发生在刷新与通知之前)被静默丢弃,依赖 feed 逐条触发的**通知**(轮次完结、
待审批)真丢。feed 无持久层,§3 的「加水位余量」修法不适用,故约定:
**`generation` 变化 = feed 游标作废,客户端清零重连(`after=0`)**。重启后环里只有
新代事件,清零重放不会重复通知,反而恰好把启动恢复补发的崩溃轮 `turn_ended`(§2.4)
送到。首帧无 `generation` 字段(旧后端)时维持既有行为。两端 30s 级慢对账定时器
(Swift `startGlobalMonitor` fallback、前端 `schedulerTimer`)保留,作为深度断档的
状态收敛兜底——围栏管事件,慢对账管状态,两层各管一半。

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

APNs payload 自定义段 `ma: {v, kind, session_id, turn_id, src?}`,kind ∈ result /
approval / approval_autodenied / schedule_start。可选 `src`(2026-10-25 追加)= 后端所在
Mac 的 macId(Mac app 经 `AGENT_INSTANCE_ID` 注入内建执行层,`framework/utils/instance_id.py`
校验格式;未注入时不带):保存了多台 Mac 的 iPhone 据此把点开、审批按钮回到发出它的那台;
push-to-start 的 `attributes` 同样可带 `src`。Live Activity content-state 契约
见 `apple-app/Sources/LiveActivity/AgentActivityAttributes.swift`(与
`framework/push/dispatcher.py` 字段一一对应,改动须两边同步)。

### 2.9 iPhone 连接:Mac 同意后按设备发票据(additive,2026-10-25;不改既有事件语义,不升版本)

每部 iPhone 都要经 Mac **明确同意**才能连上,并拿到它自己的设备票据;共享密钥不离开
Mac。实现 `framework/api/pairing.py`(入口)+ `devices.py`(票据存储)+ `auth.py`
(`verify_api_key` 认设备票据、`require_owner`),不挂计算端就绪闸。设计见
`docs/plans/2026-10-25-device-tokens-design.md`。

```
# 扫码(不同 Apple ID):打开二维码即同意
POST /api/v1/pair/start                (owner)         → {code, expires_in}
POST /api/v1/pair/claim                {code, name?, platform?, push_device_id?}
                                                        → {token, api_key(=token), device_id, instance_id?}
# 握手(同一 Apple ID):iCloud 条目里的 ticket 只能提交请求,Mac 点允许才发票据
POST /api/v1/pair/request              {ticket, name?, platform?, push_device_id?}
                                                        → {request_id, poll_secret, code(4 位核对码), expires_in}
POST /api/v1/pair/request/{id}/poll    {poll_secret}    → {status: pending|denied}
                                                          | {status: approved, token, api_key, device_id, instance_id?}
GET  /api/v1/pair/requests             (owner)          → {requests: [{id, code, name, platform, created}]}
POST /api/v1/pair/requests/{id}/approve | /deny  (owner)
GET  /api/v1/pair/ticket · POST /api/v1/pair/ticket/rotate   (owner) → {ticket}
# 旧连接(手里是共享密钥)换票据;设备管理
POST /api/v1/pair/upgrade              (owner)          → 同 claim
GET  /api/v1/pair/devices              (owner)          → {devices: [{id, name, via, platform, created, last_used, expires}],
                                                           default_ttl_days, notice}
PATCH /api/v1/pair/devices/{id}        (owner) {name?, permanent?, ttl_days?}
DELETE /api/v1/pair/devices/{id}       (owner)          吊销,连带删它名下的推送登记,其进行中的 Live Activity 不再收到任何帧
DELETE /api/v1/pair/self               (设备票据)       iPhone 自行断开:吊销自己的票据,同上连带清理;共享密钥 400
PUT  /api/v1/pair/settings             (owner) {default_ttl_days: int|null}   新设备默认有效期(缺省 90,null=永久)
```

(owner) = `verify_api_key` 通过且是共享密钥(`device_id == "default"`),设备票据一律 403。
**全 /api/v1 免 Bearer 的恰好三条**:`claim`、`request`、`request/{id}/poll`(各凭一次性
码 / ticket / poll_secret),由 `test_pairing.py` 的白名单测试守。

约束:配对码 144 bit、5 分钟、单次,错码不作废(防乱猜把码烧光);请求 5 分钟过期,同时
≤3 条待处理、每分钟 ≤3 次提交,未经同意的查询永不含票据,被拒 / 过期即作废,票据只发一次;
后端没有共享密钥时一律不发票据(start / ticket 409,claim / request 403)。票据 256 bit,
只在发放那一次出现;数据根 `auth/devices.json`(0700/0600,原子写)只存 sha256,损坏即
fail-closed(设备票据全 401,共享密钥不受影响,`notice` 提示重连);到期的票据 401
`detail: device_expired`。凡发出票据 / ticket / poll_secret 的响应(claim、request、poll、upgrade、
ticket、rotate)带 `Cache-Control: no-store`;其余管理响应不带。配对码与待处理请求在进程
内存,后端重启即作废;ticket 与设备记录持久。`instance_id` 与 §2.8 的 `src` 同值(可选),
iPhone 的 Mac 列表据此认出同一台 Mac。二维码载荷 `macagent://pair?u=<*.ts.net 地址>&c=<code>`;
iCloud 条目 `{url, mac, macId, ticket}`(不含共享密钥),app 端见
`apple-app/Sources/App/PhoneLink.swift`。

同一部手机的身份靠 `push_device_id`(app 生成、UserDefaults 持久)衔接:claim / poll / upgrade
发新票据时,若已有**另一条已到期**的设备记录带同一个 `push_device_id`,旧记录视为被取代而删除,
它名下的推送条目改绑新票据(2026-10-26 修正;此前到期后重新配对的新票据会被自己的旧登记 403
挡住)。仍有效的旧记录不会被自报的 `push_device_id` 顶掉:iPhone 在旧票据仍有效时重新配对,
先用旧票据调 `DELETE /pair/self` 注销,再换上新票据。
Mac 端 `expose`(tailscale serve)在后端没有共享密钥时拒绝执行:无密钥的后端对回环放行一切请求,
反代到 tailnet 等于免鉴权。

推送条目归属(§2.8 四条路由):条目记首次登记它的设备票据 `auth_device`;设备票据可以动
`auth_device` 为空或等于自己的条目;`auth_device` 指向的记录已到期 / 已删(吊销)时,只有自己
记录里的 `push_device_id` 与该条目一致的调用方才能接管并改绑,否则 403;共享密钥不受限。发送侧
跳过归属设备已失效(到期 / 已删,状态读不出也按失效)的条目,只跳过、不删;吊销只删该设备
名下的条目,不按自报的 `push_device_id` 删别人的。

### 2.10 离线优先:算力就绪闸(additive,2026-10-25;不改既有事件语义,不升版本)

后端先于算力端起来,`/health` 多报三项:`data_ready`(本地会话 / 文件可读)、`compute_ready`
(推理活体 + 鉴权通过)、`compute_state` ∈ starting / ready / retrying / unavailable /
stopping / stopped。鉴权之后、受理之前,`require_compute_ready`(`framework/api/routes.py`)
对执行类路由在 `compute_ready=false` 时回 503 `{reason: compute_not_ready, compute_state}` +
`Retry-After: 5`;执行类路由固定为 `_EXECUTION_ROUTES`:

```
POST   /api/v1/chat/{session_id}                 POST /api/v1/chat/{session_id}/turns
POST   /api/v1/chat/{session_id}/turns/retry     POST /api/v1/chat/{session_id}/turns/approval
POST   /api/v1/chat/{session_id}/turns/resume    POST /api/v1/schedules/{schedule_id}/run
DELETE /api/v1/compute/kvstore
```

首次装配完成前(`compute_installed=false`)除本地读取白名单 `_LOCAL_READ_ROUTES` 外一律 503;
装配过一次后,断线期间只有上表受限,本地控制(含取消)与读取照常。§2.9 配对面不挂此闸。
离线文件(只要 `data_ready`,不看算力):

```
GET /api/v1/chat/{session_id}/offline-files            → {status, session_id, files: [{path, name, kind, size, revision}]}
GET /api/v1/files/{session_id}/offline/{kind}/{filename}   Cache-Control: private, no-cache;
                                                           nosniff + attachment(用户上传的 .html/.svg 绝不同源执行)
```

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
  余量只保证 seq 不回退;另一半——进程死时那一轮没有 `turn_ended`,客户端停在
  generating——由**启动恢复补发**收敛(2026-08-22 修):`recover_inflight_turns`
  补写 history 后,装配阶段对每条崩溃轮补发 `turn_ended`(status=fail,turn_id 取
  inflight 预写标记里的值,旧标记无此键则为 null)。补发事件的 seq 落在新号段,
  必然大于任何重启前游标,重附着经重放即送达。进程代际经 `stream_attached`/`/turn`
  的 `generation` 字段下发(§2.4/§2.5);全局 feed 的同源缺陷走客户端重锚(§2.7)。
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
