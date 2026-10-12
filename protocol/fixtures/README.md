# 协议测试夹具(双端行为标准)

夹具是 `PROTOCOL.md` §5 的可执行形态:一份录制/手工构造的事件流 + 归约后的期望终态。
web(vitest)与原生 app(XCTest)各自实现 `reduce(state, event)`,**读取同一批夹具**
逐行归约并断言终态——TS 与 Swift 无法共享代码,但共享行为标准。

## 格式

```
<name>.events.jsonl      一行一个事件信封(客户端 attach 后收到的序列,含增量事件)
<name>.expected.json     归约终态(NormalizedState,形状见 PROTOCOL.md §5)
<name>.md                (可选)场景说明与断言意图
<name>.responses.json    HTTP 管理回执；由端点/客户端请求测试读取，不作为事件流归约
```

`017-delete-absent.responses.json` 区分明确缺席、成功删除和一般失败；不增加事件归约夹具数量。

## 录制（2026-08-28 起,手工构造之外的第二条路）

`record_fixture.py` 对着**跑起来的后端**(FakeEngine dev 栈即可)把
`GET /chat/{sid}/events?after=0` 的信封落盘(默认 seq/ts 重编号,`--raw` 保留)。
**边界如实**(首审 C1/S13):只能录**后端已实现的行为**,且必须**先附着新会话再发消息**
——journal 对历史轮瘦身持久化,录旧会话=把 gutted 事件固化成自洽的垃圾夹具(录制器
见无 response 的 turn_ended 会拒绝落盘);夹具先行的新协议行为仍走手工构造,铁律 5
不因有了录制器而松动。期望态由**已提交**的 web 归约器生成,对 web 是同义反复,
**Swift 回放才是独立校验**。

三步纪律(refresh 态:期望值由已提交归约器重生成,人审 diff——不许手写终态):
1. 录:`python3 protocol/fixtures/record_fixture.py <name> --session <sid>`
   （暂停态夹具用 `--until-turns 0` + 到态后 Ctrl-C;要 stream_attached 帧加
   `--keep-attach`,详见脚本文件头）
2. 生成期望:`cd frontend && EMIT_FIXTURE_EXPECTED=<name> npm test -- turn-reducer`
   ——生成**必红**(确认收据);人工审它是「应该」的终态而不只是「当前」的终态。
   注意:`JSON.stringify` 丢 undefined 键而 Swift 可能吐 null——step 3 若 Swift 红在
   「缺键」上,先怀疑这条序列化差异,别追错端。
3. 不带环境变量重跑 web 回放 + `apple-app/Tests/run_reducer_fixtures.sh` 双端全绿,
   **并且**:README 表格登记 + 写 `<name>.md` 场景说明,才算入库。
   (2026-08-29 前此处还要 `doc-numbers --write` 刷新夹具计数——该门已随台账冻结退役。)

历史注记:9 份既有 expected.json 已于 2026-08-28 统一走发射器重排版(逐文件深比对
语义等值、双端回放绿)——此后任何 refresh 的 diff 都是纯语义的。

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
| `009-upgrade-triggered` | **编排层代发起云端咨询**(⑤′-3):新事件 `upgrade_triggered` 被忽略;无 iteration_started 直接到 `awaiting_approval`,审批卡照常重建(代发起与模型发起在协议面不可区分)。守卫夹具,详见 `009-upgrade-triggered.md` |
| `010-approval-replaced-long-arguments` | 旧审批卡与后续轮次隔离，完整长参数保留 |
| `011-approval-batches-same-turn` | 同轮不同审批批次按事件 seq 绑定 |
| `012-discard-paused-approval` | 撤销待审批轮只清空临时状态，不新增历史 |
| `013-discard-paused-suspended` | 撤销挂起轮只清空临时状态，不新增历史 |
| `014-history-replay-boundary` | 历史下界与后来 turn=null 的完结窗口；restore.json 由真实 Web/Swift store 时序测试共享，覆盖重叠去重与 thin terminal 回同一恢复入口 |
| `015-cancelled-execution-receipts` | 工具返回后取消，模型上下文回滚但执行事实保留，参数省略如实标注 |
| `016-task-retry-inputs` | 重试保留输入及审批；history.json 锁定成功替换展示、失败保留旧版与所有尝试下标 |

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
