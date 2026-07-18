# api_tools —— 私人 API 声明目录

一份 `*.yaml` = 一个模型可调用的工具。后端启动（或 `POST /api/v1/tools/rescan`）时由
`backend/framework/mcp/tools/api_tool_factory.py` 读取并合成工具类，走与内置工具完全
相同的注册/探活/审批/巡检管线。字段说明与安全边界见工厂模块的文件头注释。

## 最小示例

```yaml
name: my_service_status
description: >
  Check the health/status of my personal XXX service. Call when the user asks
  whether XXX is up or wants its current stats. Returns a JSON status summary.
endpoint: "http://127.0.0.1:8000/status"
method: GET
```

## 三条纪律

1. **密钥不进本目录**：`auth.env` 只写环境变量名，实值放 `.env.local`（devctl 启动时
   source）。变量缺失时工具不注册，补上后 rescan 即上架。
2. **description 写"何时调用"**：模型全凭这句话决定用不用，写清触发场景与返回内容。
3. **工具数克制**：每个工具的 schema 都进稳定前缀（小模型注意力稀释）。不用的声明
   `enabled: false` 停用而非删除。

`example.yaml` 是一份完整字段示范（`enabled: false`，永不注册）。
