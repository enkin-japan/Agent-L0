# 协议测试夹具(双端行为标准)

夹具是 `PROTOCOL.md` §5 的可执行形态:一份录制/手工构造的事件流 + 归约后的期望终态。
web(vitest)与原生 app(XCTest)各自实现 `reduce(state, event)`,**读取同一批夹具**
逐行归约并断言终态——TS 与 Swift 无法共享代码,但共享行为标准。

## 格式

```
<name>.events.jsonl      一行一个事件信封(客户端 attach 后收到的序列,含增量事件)
<name>.expected.json     归约终态(NormalizedState,形状见 PROTOCOL.md §5)
<name>.md                (可选)场景说明与断言意图
```

## 规则

- **新增协议行为必须先落夹具再改实现**(两端实现以夹具为验收)。
- 夹具事件必须符合 PROTOCOL.md §2 的信封与负载定义;`seq` 单调、
  `tool_started`/`tool_result` 按 `call_id` 配对。
- 每个夹具聚焦一个场景;中间暂停态(审批中/挂起中)也是合法终态——
  夹具不必以 `turn_ended` 结尾。
- 未知事件类型的容忍是协议要求,`003-unknown-event-ignored` 永久保留。

## 现有夹具

| 夹具 | 场景 |
|---|---|
| `001-tool-turn` | 完整成功轮:排队→准入→思考→工具(带进度)→流式正文→turn_ended 对账 |
| `002-approval-pending` | 轮次停在 awaiting_approval:审批卡可从事件流重建 |
| `003-unknown-event-ignored` | 含未知事件类型,归约结果与 001 完全一致(向前兼容) |
| `004-tool-pending` | 工具已声明未执行(tool_call_pending)的中间态 |
| `005-late-attach-rebuilds-pending` | **多端附着**:第二个客户端在轮次挂起后才 attach,只凭首帧 `stream_attached` + 服务端重放即重建出与全程在线者相同的审批卡(§7 预留 4) |
| `006-reattach-midturn-keeps-state` | **断线重连**:流中途再来一帧 `stream_attached`(带游标重连),已累积状态不得被冲掉 |
| `007-render-hint` | **工具渲染格**:观察带 `render:{hint,payload}`(R3)穿过两端归约器不被吞。**守卫夹具**——实现前就是绿的,详见 `007-render-hint.md` |
| `008-cloud-degraded` | **云端导师降级轮**(⑤′-1):真实会发出的新事件类型 `cloud_degraded` 被忽略、`turn_ended` 顶层 additive 字段 `cloud_degraded`/`cloud_consult_ok` 不破坏归约、带「本地作答」标注的答复原样进 transcript。守卫夹具,详见 `008-cloud-degraded.md` |

## 为什么 005/006 建在 002(暂停态)而不是 001(完整轮)上

001 以 `turn_ended` 收尾,而该事件的 payload 自带**完整** response——终态是从它整个重建的,
中途状态被冲掉也照样收敛。实测:把归约器改成"收到 `stream_attached` 就重置状态",
建在 001 上的同款夹具**全绿**(测不出任何东西)。改建在 `awaiting_approval` 上——
状态全靠逐事件累积、没有 turn_ended 可兜底——同一个变异体立刻被 006 抓住。
**给夹具选基底时,先问"这条断言在实现坏掉时会不会红"。**

> **变异证明纪律的唯一出处是 `CLAUDE.md` 的「Mutation proof」节**,别在这里另记一份。
> 那里的适用面比本文件宽:不止新夹具,而是**一切守卫类改动**(金测试 / 护栏分支 /
> fail-closed 判定 / 协议夹具)——本仓两次实证失效都发生在后端守卫域,不在夹具域。

## B2 代际围栏为什么不在夹具覆盖面内（2026-08-22）

归约器**看得见** `stream_attached`（005/006 就喂了这一帧）,但对它的义务只有
「当无操作帧,不冲掉已累积状态」——那正是 005/006 已经守着的。围栏的语义
（用哪个 after 重连、feed 何时清零重锚、会话流为何不重锚）是**游标管理**,长在
store 层的重连循环里,归约器无从表达——建在归约器上的「代际夹具」顶多是 003 的
复读,实现坏掉也不会红。行为锁在:后端金测试(`backend/tests/test_generation_fence.py`,
含四个 generation 下发点的端点级断言)+ 前端 `src/lib/feed-fence.test.ts`
(围栏判定纯函数;store 重连循环的接线本身无自动化覆盖,由代码审查与真机演练守)+
真机重启演练(`backend/scripts/drill_restart_fence.py`,基线先证明演练看得见缺陷,
fixed 场同时断言会话流补发与 feed 重锚后的补发条目)。Swift 侧围栏是与
`feed-fence.ts` 语义同构的手工实现,无自动化覆盖——改任一侧时人工对照另一侧。
