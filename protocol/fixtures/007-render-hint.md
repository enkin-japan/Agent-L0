# 007-render-hint · 工具渲染格穿过归约器

**场景**：`rag_search` 的观察带 `render: {hint, payload}`（R3 试点 citations）。断言该字段随
`turn_ended.response` 原样进 transcript，两端归约器都不得吞掉它。

## 这是一条守卫夹具，不是验收夹具

**它在实现之前就是绿的**——两端的 transcript 都刻意是不透明子树
（web: `...response` 直接展开；Swift: `transcript: [JSONValue]`），
新字段本就穿透。故它**不能**用来证明 R3 做完了；R3 的真实工作在
后端产出 `render` 与 web 组件的投影，二者都不在归约器里。

按本目录 README 的规矩（「先问这条断言在实现坏掉时会不会红」），
如实记下它会红的场景：

- **Swift 侧把 transcript 收成强类型 struct**（把 `[JSONValue]` 换成
  `[TranscriptMessage]`，`tool_responses` 解成 `{name, response}`）——
  `render` 在解码时被静默丢弃，本夹具立刻红。这是真实会发生的重构冲动，
  且症状是「图在 web 上有、在 app 上没有」，不写夹具就只能靠人肉发现。
- web 侧把 `...response` 改成按字段挑选（如为了做类型收敛）——同上。

**已实测（不是推断）**：把 web 归约器的 `...response` 改成
`.map(tr => ({name: tr.name, response: tr.response}))` 后回放——
**只有 007 红，001–006 全绿**。即这条不变量此前确实无人守，
且本夹具确实守得住。变异已回退。

`PROTOCOL.md` §0.5 要求客户端忽略**未知数据字段**，但此前只有
`003-unknown-event-ignored` 守着未知**事件类型**，未知字段无人守。
本夹具补的是那半条。

## 为什么 render 挂在观察条目上而不另开平行面

`tool_responses[]` 条目没有 `call_id`，另开 `turn_ended.renders[]` 就只能靠
下标 join 回去，错位 = 图配错工具结果。挂在条目上则天然对齐。
代价是该条目同时是模型上下文——由提示词白名单隔开（D.12，
`prompt_builder._MODEL_VISIBLE_KEYS`），模型看不到 `render`。

## 夹具与真实后端的形状对账

本夹具的 `render` 不是手捏的：它与 `rag_search` 真实产出**逐字节相同**
（payload 与 response 文本均已实测比对）。夹具若和真实形状对不上，
比没有夹具更糟——两端照着它实现，跑起来却对不上后端。
