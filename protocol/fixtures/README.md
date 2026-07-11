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
