# api_tools —— 私人 API 声明目录

一份 `*.yaml` = 一个模型可调用的工具。后端启动时由
`backend/framework/mcp/tools/api_tool_factory.py` 读取并合成工具类；`POST /api/v1/tools/rescan`
重探已加载但依赖尚未就绪的候选，不是重新读取所有 YAML，走与内置工具完全
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

1. **密钥不进声明**：`auth.env` 只写变量名。手写声明使用运行进程的环境变量（devctl 启动时
   读取根 `.env.local`）；macOS 工具设置表单保存的密钥放在数据根 `api_tools/.secrets.env`
   （权限 0600），进程环境中的同名值优先。修改磁盘上的 `.env.local` 不会自动改变已运行
   进程的环境；候选能读取到所需密钥后才可补注册。密钥不进入聊天或 pending 声明正文。
2. **description 写"何时调用"**：模型全凭这句话决定用不用，写清触发场景与返回内容。
3. **工具数克制**：每个工具的 schema 都进稳定前缀（小模型注意力稀释）。不用的声明
   `enabled: false` 停用而非删除。

`example.yaml` 是一份完整字段示范（`enabled: false`，永不注册）。

## 保存、注册与验证

仓库手写声明在本目录；UI 保存的私人声明位于数据根 `api_tools/`（开发形态通常为
`backend/api_tools/`，不入库）。确认推荐声明后，可继续编辑同一个已保存对象，无需重填。
PATCH 只更新指定字段，保留未展示的 schema、headers 与审批设置。

保存结果区分 `registered`、`unavailable`、`saved_disabled` 与 `restart_required`。
`registered` 只表示加入工具清单；`has_secret` 只表示存在密钥；`runtime_declaration_current`
只比较保存声明与运行实例，均不证明远程连接或认证成功。显式试调可能产生真实副作用。
已注册实例保持到重启；尚未上架的候选被停用或删除后立即撤回，重扫不会将旧候选复活。
