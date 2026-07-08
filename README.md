# MacAgent

运行在 Apple Silicon Mac 上的**全本地** AI Agent：MLX 本地推理 + 分类路由式 RAG 长期记忆 + Web 前端，
数据不出门。单仓（monorepo）组织，三个服务 + 一个可选搜索层。

```
MacAgent/
├── devctl.sh            # 标准启动器（唯一入口）：setup / start / stop / restart / status / logs
├── config.yaml          # 全项目唯一配置文件（backend 与 rag 共读，改这里即可）
├── .env.example         # 环境变量模板（复制为 .env.local 填写密钥；backend 与前端代理共读）
├── ARCHITECTURE.md      # 运行架构逻辑示意图（拓扑 / 分层 / 数据流 / 生命周期）
├── backend/             # Agent 主体（FastAPI + MLX, :14159）：推理引擎 / 编排循环 / 工具 / 会话记忆
├── rag/                 # RAG 知识库（FastAPI + PyTorch MPS, :14160）：嵌入 + 重排 + ChromaDB
├── frontend/            # Web 前端（React 19 + Vite, :5173）：聊天 / 审批 / 任务 / Deck / 定时任务
├── searxng/             # SearXNG 配置挂载目录（docker, :8080, 可选联网搜索层）
└── logs/                # 各服务运行日志与 pid（devctl 管理，不入库）
```

## 快速开始

```bash
./devctl.sh setup    # 首次部署：自动建 venv、装依赖、npm install
./devctl.sh start    # 启动全部服务（RAG → SearXNG → backend → 前端，依序等待就绪）
./devctl.sh status   # 查看各服务状态
```

启动后浏览器打开 `http://localhost:5173`。停止：`./devctl.sh stop`；看日志：`./devctl.sh logs backend`。

模型权重（gemma-4 / whisper / Qwen3-Embedding / Qwen3-Reranker）首次启动自动从
Hugging Face 下载到各自的 `models/` 目录（已 gitignore）。

## 配置与密钥

- **`config.yaml`（仓库根）**：全项目唯一配置。段落归属——`inference` / `server` / `ingestion` /
  `web_search` / `math_research` / `ppt` / `agent` / `schedules` → backend；`rag`（host/port 两侧共用）/
  `tuning` → rag。各项含义见文件内注释。
- **`.env.local`（仓库根，不入库）**：`cp .env.example .env.local` 后填写 `AGENT_API_KEY`
  即启用后端 Bearer 鉴权；devctl 启动时自动 source 并 export 给 backend，Vite 代理
  （`envDir` 指向仓库根）读同一份文件、在 Node 侧注入 `Authorization` 头——浏览器永不持有密钥。

## 端口与安全边界

| 服务 | 端口 | 绑定 |
|---|---|---|
| frontend (Vite) | 5173 | `0.0.0.0`（唯一对外入口，仅限受信通路暴露） |
| backend | 14159 | `127.0.0.1`（外部访问统一走前端 `/api` 代理） |
| rag | 14160 | `127.0.0.1`（只供同机 backend 调用，无鉴权） |
| searxng | 8080 | `127.0.0.1`（docker，只供 web_research 沙箱调用） |

⚠️ 5173 + 代理注入密钥意味着**能访问 5173 的设备等于免钥使用 Agent**（含 shell 等强力工具），
只允许暴露在 Tailscale 内网 / Cloudflare Access 等受信通路上。

## 各服务详情

- [backend/README.md](backend/README.md) —— 编排循环、工具插件、会话记忆、上下文压缩、API 全量说明
- [rag/README.md](rag/README.md) —— 分类路由、两阶段检索、数据治理、API
- [frontend/README.md](frontend/README.md) —— 功能清单、目录结构、局域网/HTTPS 访问
- [ARCHITECTURE.md](ARCHITECTURE.md) —— 整体运行架构示意图
