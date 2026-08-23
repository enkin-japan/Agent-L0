# 008-cloud-degraded · 云端导师降级轮（⑤′-1）

**场景**：`cloud_consult` 因日预算耗尽降级——观察是 `System Error: [cloud advisor degraded: budget_exhausted]…`，
紧随其后一条新事件 `cloud_degraded {call_id, name, kind}`；本地作答收尾，最终答复末尾带编排层
确定性追加的「—— 本回答由本地模型独立完成，未经云端导师：云端日预算已用尽。」；`turn_ended`
顶层多两个 additive 字段 `cloud_degraded: ["budget_exhausted"]`、`cloud_consult_ok: false`。

## 断言意图（守卫夹具，与 003/007 同型）

- 新事件类型 `cloud_degraded` 对两端归约器是**未知类型**，必须按 §0.5 忽略，归约终态与没有这条
  事件时完全一致（003 守的是"任意未知类型"，本条守的是**这一个真实会发出的**类型）；
- `turn_ended` 新增的两个顶层字段不得让归约器报错或吞掉 transcript（007 守"观察内未知字段"，
  本条守"turn_ended 顶层未知字段"）；
- 最终答复（含标注行）按 `turn_ended.response` 原样进 transcript——标注是答复正文的一部分，
  不是另一条消息、不是独立渲染格，客户端不需要任何新 UI 即可把它呈现给用户。

它会红的场景：两端若把 `turn_ended` 解成严格 struct（未知键即解码失败），或把事件类型白名单化
（未知即抛错而非忽略），本夹具立刻红。
