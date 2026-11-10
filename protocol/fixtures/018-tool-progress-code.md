# 018-tool-progress-code · 进度事件带稳定代号（build 84）

**场景**：三个并行执行中的工具（web_research / web_read / media_download）各发 `tool_progress`，
负载在原有 `{call_id, label, detail}` 之外多了 additive 字段 `code`（点分小写稳定代号）与
`args`（只含 str / int 的扁平对象）。`label` / `detail` 仍是后端原样中文文本（与 build 83 逐字节相同，
由 `backend/framework/agents/progress_codes.py` 的 zh 模板渲染）。轮次停在 running（不以 `turn_ended`
收尾——收尾会把预览整体撤下，步骤 detail 就不在终态里了，断言会落空）。

## 断言意图（守卫夹具，与 007/008 同型）

- 新字段对两端归约器是**未知字段**：不得报错、不得吞掉步骤；
- 归约终态的 `detail` 仍是后端原文（`label · detail` 拼接规则不变）——**归约结果与界面语言无关**。
  按界面语言重渲染只发生在原生端展示层（`apple-app/Sources/Store/ChatMessage.swift` 的 `StepText`），
  代号/参数作为 Swift 归约器的内部簿记（`NormalizedStep.progress`）随步骤携带，不进公共形状；
- 第二条进度（seq 6）覆盖第一条：按 `call_id` 更新的语义不因新字段改变。

本夹具守的是「带 `code`/`args` 的 `tool_progress` 不改变归约结果的公共形状」——`detail`
仍是后端原文，归约出的步骤对象不多出键。展示层按界面语言重渲染由
`apple-app/Tests/step_text_test.swift` 守，不在本夹具范围内。

它会红的场景：任一端把 `tool_progress` 解成严格结构（未知键即失败）、或让 `code`/`args`
进入公共形状。
