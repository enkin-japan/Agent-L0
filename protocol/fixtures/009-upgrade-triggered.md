# 009-upgrade-triggered · 编排层代发起云端咨询（⑤′-3）

**场景**：用户显式要求云端导师，`cloud.upgrade.mode=auto` → 编排层在**任何生成之前**发出新事件
`upgrade_triggered {trigger, action, boundaries, tier_target, reason}`，随即代模型发起 `cloud_consult`
并照常停在 `awaiting_approval`（审批卡 = 即将出网的原文；此前没有 iteration_started / tool_started，
模型一个 token 都没生成）→ 用户批准（`resumed`）→ 工具执行 → 模型据建议作答 → `turn_ended`
顶层多出 additive 字段 `cloud_upgrade: [...]`。

## 断言意图（守卫夹具，与 003/007/008 同型）

- `upgrade_triggered` 对两端归约器是未知类型，按 §0.5 忽略；
- 审批卡从 `awaiting_approval` 原样重建，且在没有任何生成事件之前就可出现——代发起的调用与
  模型发起的调用在协议面**不可区分**（铁律 6 的形状：出网永远经同一道审批闸）；
- `turn_ended` 顶层 `cloud_upgrade` 是未知字段，不得让归约器报错或吞掉 transcript；代发起的
  assistant(tool_calls) 消息（thoughts 写明 `[System upgrade]`）按 response 原样进 transcript。

它会红的场景：归约器把未知事件/未知顶层字段当错误；或把「无 iteration_started 的
awaiting_approval」视为非法序列而丢弃审批卡。
