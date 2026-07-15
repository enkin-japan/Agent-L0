# frontend —— Web 交互前端

> [MacAgent](../README.md) monorepo 的用户界面。

SplitStack Agent（MacAgent）的 Web 前端：一个对接本地大模型推理服务的聊天界面，支持真流式输出、多会话管理、深度思考（思维链）、工具调用展示、敏感操作审批、任务挂起/恢复、多模态上传、会话分支、定时任务管理与 Deck（PPT）实时预览。

## 技术栈

- **React 19** + **TypeScript** + **Vite 8**
- **Tailwind CSS v4** + **shadcn/ui**（Radix UI）
- **Zustand** 状态管理
- lucide-react 图标 / framer-motion 动画

## 功能

| 功能 | 说明 |
|---|---|
| 多会话 | 会话 ID 由前端生成（UUID），即后端 `session_id`；首次发消息时后端自动建会话 |
| 发送消息（真流式） | 默认走 `POST /api/v1/chat/{session_id}/stream`（SSE）：可见正文逐 token 流出、思考块/工具卡片/任务进度结构化推送，`turn_end` 携权威终态对账（流为预览、终态为真）；非流式 `POST /chat` 保留为回退路径 |
| 深度思考 | 侧边栏 Control Center 的 Deep Thinking 开关，对应接口 `enable_thinking` 字段；思维链在消息内折叠展示 |
| 工具调用 | Agent 的 `tool_calls` / `tool_responses` 渲染为独立卡片（工具名 + 参数 JSON + 执行结果） |
| 敏感操作审批 | Agent 请求执行不可逆操作（如删除知识库数据）时返回 `pending_approval`，输入框上方弹出确认卡片，用户「允许执行 / 拒绝」后调用 `POST /approval` 恢复对话；确认前禁止发送新消息（多步任务中途挂起后按计划继续） |
| 任务进度面板 | 生成期间经 SSE `task_progress` 事件实时推送计划步骤推进（✔完成 / 执行中 / ✗失败 / ⊘跳过）；轮末以 `GET /task` 一次性对账，任务跨轮存续（模型中途提问）时也保持展示 |
| 实时步骤流 | 同一推送里的 `activity`：「当前在做什么（思考中 / 调用某工具）+ 最近完成的动作」，把黑盒转圈变成看得见的执行流 |
| 挂起 / 恢复 | 长任务可在安全点挂起让出（`POST /task/suspend`），会话出现「可恢复」横幅；`POST /task/resume` 从挂起处续跑，发新消息 = 放弃挂起任务。刷新后经 `GET /tasks` 与后端调度器对账恢复入口 |
| 真取消 | 停止按钮调用 `POST /task/cancel`：后端在最近检查点停止生成并整轮回滚（该轮不进模型记忆），返回 `cancelled` 状态；后端不可达时退回本地中断等待 |
| 附件 | 图片 / 音频 / PDF / 办公文档 / 代码及任意文件——后端摄入层按模态分路（图片原生、音频转写、文档解析、代码懒登记）；图片缩略图 / 音频试听本地预览，刷新后历史附件只显示文件名 |
| 定时任务 | 侧栏 Schedules 条 + 详情弹窗：启停 / 立即运行 / 中止本次 / 删除 / 运行历史（可跳转专属会话看全文）；创建与修改走对话（模型的 `schedule_*` 工具 + 审批卡片） |
| Deck / PPT 预览 | 检视面板 Deck 页签实时投影 `deck.json`（模型每 `deck_set_slide` 一页即时可见）；`/print/deck` 打印路由供后端无头 Chrome 导出 PDF（渲染器只有一份，预览即所得） |
| 记忆面板 | 展示会话上下文压缩状态（`GET /memory`）：上下文规模 vs 折叠阈值、滚动摘要水位线、被折叠原文是否已沉淀 RAG 可召回 |
| UI 状态云同步 | 工作区/会话元数据除 localStorage 外同步到后端 `PUT /ui-state`（防抖 + last-write-wins），换设备打开即恢复 |
| 多语言 / 主题 | 中 / 日 / 英三语（`lib/i18n.ts`），浅色 / 深色 / 跟随系统 |
| 实时录音 | 麦克风按钮直接录音（MediaRecorder），录完在浏览器内统一转码为 16-bit PCM WAV 再作为附件，兼容 Safari（`audio/mp4`）与 Chrome/Firefox（`webm/ogg`）的原生录制格式；需要安全上下文（localhost 或 HTTPS），否则按钮会提示改用文件上传 |
| 历史恢复 | 会话元数据存 localStorage，消息内容刷新后从 `GET /history` 懒加载恢复 |
| 会话分支 | 鼠标悬停任意模型回复出现 Branch 按钮，从该历史节点派生新会话（`POST /branch`，尽量复用 KV Cache） |
| 编辑/删除最后一轮 | 悬停最后一次用户发言出现「编辑 / 删除」按钮：编辑为内联改写后删除该轮并按新内容重新生成；删除（有确认框）调 `DELETE /turns/last`，后端同步回退模型记忆、KV Cache 与落盘日志 |
| 删除会话 | 同步删除后端该会话的全部落盘数据（有确认框，不可恢复） |
| Workspace | 纯前端的会话分组，支持搜索、重命名、移动 |
| 上下文面板 | 聊天页头部右侧按钮开合的右侧面板，展示当前会话的系统提示词（`GET /system`）与模型可用工具清单（`GET /tools`，含「需审批」标记与参数说明）；桌面端内嵌、移动端为带遮罩浮层 |

## 快速开始

本服务是 monorepo 的一部分，可由仓库根的启动器统一管理（`../devctl.sh setup && ../devctl.sh start`），
也可单独起：

```bash
npm install
npm run dev        # 监听 0.0.0.0:5173，Tailscale 内网设备可直接访问
```

后端地址默认走同源 `/api` 相对路径，由 Vite dev server 代理到本机 Agent
（`http://localhost:14159`，见 `vite.config.ts` 的 `server.proxy`）。
backend 默认只绑定 127.0.0.1，内网设备统一通过前端访问——本端口是唯一对外入口。

算力分离部署时（`../devctl.sh start --profile edge`）本前端与 backend 仍同机、代理路径不变；
变的只是 backend 背后的推理与 RAG 跑在另一台机器上，前端无感。

**鉴权**：后端启用 `AGENT_API_KEY` 后，密钥由代理在 Node 侧注入 `Authorization` 头，
浏览器 bundle 永不持有 key。环境变量统一在**仓库根**（Vite 的 `envDir` 指向上级，
与后端共用同一份 `.env.local`，无 `VITE_` 前缀、已被 gitignore）：

```bash
echo 'AGENT_API_KEY=<你的密钥>' >> ../.env.local
```

> ⚠️ 安全边界：`0.0.0.0:5173` + 代理注入 key 意味着**任何能访问 5173 的设备都等于
> 免钥使用 Agent**（含 shell 等强力工具）。本端口只允许暴露在受信通路（Tailscale 内网 /
> Cloudflare Access 通道），绝不可裸端口转发到公网或不受信 LAN。

如需让浏览器直连远程 Agent，可通过环境变量覆盖（此路径上浏览器直连 14159，
后端启用鉴权时还需自行解决 `Authorization` 注入，推荐仍走代理）：

```bash
cp ../.env.example ../.env.local
# 编辑 ../.env.local
VITE_AGENT_API_BASE=http://<host>:14159
```

其他命令：

```bash
npm run build      # 类型检查 + 生产构建（输出 dist/）
npm run lint       # ESLint
npm run preview    # 预览生产构建
```

## 局域网 / Tailscale 访问

Vite 已配置监听 `0.0.0.0`，同网段或 Tailscale 设备可直接用本机 IP 访问（如 `http://100.x.x.x:5173`）。

> 注意：通过 IP 的 http 访问属于浏览器的**不安全上下文**，部分 Web API 不可用：
> - `crypto.randomUUID` — 已做降级处理（见 `src/lib/agent-data.ts` 的 `newSessionId()`）
> - **麦克风录音**（`getUserMedia`）— 浏览器硬性禁用，无法降级；此时录音按钮会提示改用文件上传。如需在内网设备上录音，可用 `tailscale cert` + `tailscale serve` 为前端套一层 HTTPS
>
> 后续新增代码时避免直接依赖仅限安全上下文的 API。

## 目录结构

```
src/
├── api/
│   ├── agent.ts              # 后端接口封装（chat/stream/approval/task/schedules/ui-state…）
│   └── sse.ts                # SSE over fetch 解析器（EventSource 不支持 POST/自定义头）
├── types/agent-api.ts        # 接口请求/响应类型
├── store/
│   ├── useChatStore.ts       # 会话/消息/审批/挂起/流式对账主状态机 + UI 状态云同步
│   └── useUiStore.ts         # 语言/主题 + /tasks、/schedules 轮询对账
├── lib/
│   ├── agent-data.ts         # 前端消息模型 + 后端响应 → 消息的映射
│   └── i18n.ts               # 中/日/英三语字典
├── components/
│   ├── dashboard/
│   │   ├── agent-sidebar.tsx # 侧边栏：会话列表、Workspace、Deep Thinking 开关、设置
│   │   ├── chat-area.tsx     # 聊天主区域：头部、消息流、打字指示、错误横幅
│   │   ├── chat-message.tsx  # 消息渲染：思维链、工具调用/结果卡片、附件、错误态
│   │   ├── chat-input.tsx    # 输入框：附件选择、录音、发送/停止按钮
│   │   ├── chat-navigator.tsx# 右侧消息定位条
│   │   ├── inspector-panel.tsx # 检视面板：系统提示词/工具清单/任务/记忆/Deck 页签
│   │   ├── scheduler-strip.tsx # Worker 状态条（/tasks 快照 + 任务停止按钮）
│   │   └── schedule-strip.tsx  # 定时任务条 + 详情弹窗
│   ├── deck/                 # SlideRenderer（Deck 唯一渲染器）、预览与 /print/deck 打印路由
│   └── ui/                   # shadcn/ui 组件
└── App.tsx                   # 组装 store 与各组件
```

## 错误处理约定

后端业务失败不走 HTTP 错误码（终态语义见根 [PROTOCOL.md](../PROTOCOL.md)），前端按以下方式呈现：

- `status: "fail"`（引擎异常）→ 聊天流内红色提示条，traceback 仅打印到 console；该轮用户消息已被后端记入历史
- `status: "warning"`（达到最大迭代次数）→ 黄色提示「未能得出结论，请重试或简化问题」
- `status: "cancelled"`（用户取消）→ 黄色提示「本轮已取消」；该轮未进入模型记忆，可直接重发
- 网络不可达 → 红色提示条，提示确认后端已启动
- 分支/删除等业务失败（`status: false`）→ 页面顶部横幅展示后端返回的中文描述

## 已知限制

- 上传文件（图片/音频原件）无法从服务端回显，刷新后历史附件只显示文件名（图表 PNG / 导出 PDF 例外，走 `/files` 资产接口）
- `system` 提示词仅会话首次创建时生效
- 鉴权依赖代理注入：绕过代理直连后端时浏览器不携带 `Authorization`（后端启用密钥后会被 401）
