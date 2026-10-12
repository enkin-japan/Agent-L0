# 取消后保留本轮实际工具事件

工具已返回后用户停止，模型上下文整轮回滚，磁盘/网络副作用不随回滚撤销。
`turn_ended.execution_receipts` 只投影同轮 journal 的工具开始/返回事件。
开始事件省略非空参数，因此条目及整份凭据均明确标记 `truncated`。
`tool_started` 只证明开始；`tool_result.status=ok` 是已有工具返回状态，不证明
外部操作成功，更不能证明取消已撤销副作用。

该字段为顶层 additive 数据，旧归约器按未知字段忽略，transcript 仍以权威
response 对账。后端 `test_execution_receipts.py` 对照本夹具验证投影值；两端
store/UI 的凭据保存和展示由 R16 客户端子范围另行验证。
