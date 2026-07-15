# Agent

运行在 Apple Silicon Mac 上的**全本地** AI Agent：本地推理 + 分类路由式 RAG 长期记忆 + Web 前端，
数据不出门。单仓（monorepo）组织，四个服务 + 一个可选搜索层。

默认单机跑全套（all-in-one）；也可把重型计算（模型推理 / RAG 嵌入重排）拆到另一台机器上，
执行层与会话数据留在用户机——见下文「部署形态」。

```
Agent/
├── devctl.sh            # 标准启动器（唯一入口）：setup / model / rag-model / start / stop / restart / status / logs
├── config.yaml          # 全项目唯一配置文件（backend / rag / inference-server 共读，改这里即可）
├── .env.example         # 环境变量模板（复制为 .env.local 填写密钥；各服务与前端代理共读）
├── CHARTER.md           # ★ 架构宪章：核心思想 + 开发铁律（每次迭代开工前通读）
├── ARCHITECTURE.md      # 运行架构逻辑示意图（拓扑 / 分层 / 数据流 / 生命周期）
├── REARCHITECTURE.md    # 交互层重整蓝图（体检报告 / 目标架构 / 四期迁移计划 + 踩坑）
├── PROTOCOL.md          # 交互协议 v1（事件账本契约，三端行为标准）
├── HETERO_COMPUTE.md    # 异构算力落地方案（执行账本）：CUDA 兼容层 + 算力分离 + RAG 档位
├── DEFERRED.md          # 未竟架构账本（轻量已交付·完整版待做；与 CHARTER §7 非目标相区分）
├── protocol/fixtures/   # 协议测试夹具（web 与原生 app 共享的行为标准）
├── backend/             # Agent 主体（FastAPI, :14159）：编排循环 / 工具 / 会话记忆 / 推理后端
├── inference/           # 推理服务（FastAPI, :14161）：把推理后端 HTTP/SSE 化，供算力分离部署
├── rag/                 # RAG 知识库（FastAPI + PyTorch MPS, :14160）：嵌入 + 重排 + ChromaDB
├── frontend/            # Web 前端（React 19 + Vite, :5173）：聊天 / 审批 / 任务 / Deck / 定时任务
├── searxng/             # SearXNG 原生部署目录（settings.yml + 源码 + venv, :8080, 可选联网搜索层）
└── logs/                # 各服务运行日志与 pid（devctl 管理，不入库）
```

## 快速开始

```bash
./devctl.sh setup    # 首次部署：自动建 venv、装依赖、npm install
./devctl.sh start    # 启动全部服务（RAG → SearXNG → backend → 前端，依序等待就绪）
./devctl.sh status   # 查看各服务状态
```

启动后浏览器打开 `http://localhost:5173`。停止：`./devctl.sh stop`；看日志：`./devctl.sh logs backend`。

模型权重（gemma-4 / whisper / Qwen3 嵌入+重排）首次启动自动从 Hugging Face 下载到各自的
`models/` 目录（已 gitignore）。换模型走**选择**而非手改配置：

```bash
./devctl.sh model       # 从 inference.model_registry 选主模型 → 写 .model-selection
./devctl.sh rag-model   # 从 rag.tiers 选 RAG 档位 → 写 .rag-tier-selection
```

两个指针文件都是 gitignored，优先级高于 config.yaml 里的默认值。
⚠️ 换 RAG 档位后向量库必须重建（`rag/scripts/reembed.py`），详见 [rag/README.md](rag/README.md)。

## 部署形态

| 形态 | 命令 | 说明 |
|---|---|---|
| all-in-one（默认） | `./devctl.sh start` | 单机跑全部，`inference.mode: inprocess`，backend 进程内加载模型 |
| 算力分离 · 设备 A | `./devctl.sh start --profile gpu` | inference-server + RAG（+ SearXNG）：只出算力，不碰会话 |
| 算力分离 · 设备 B | `./devctl.sh start --profile edge` | backend + 前端：编排 / 工具 / 记忆 / 会话落盘，需 `inference.mode: remote` |

分离形态下两台机器各读自己的 `config.yaml`，但 `inference.remote.host` 与 `rag.host` 要填**同一个值**
（设备 A 拿它当绑定地址，设备 B 拿它当拨号地址，一般是设备 A 的 Tailscale 地址），
两侧 `.env.local` 的 `INFER_API_KEY` / `RAG_API_KEY` 也必须一致。会话数据（`history.jsonl` /
`memory.json` / deck 等）**全部留在设备 B**，设备 A 只见 prompt 与媒体、不持久化会话。

edge 侧的 backend 不加载模型权重，只拉分词器（实测 31MB vs 全量 5.2GB）。
完整拓扑与执行记录见 [HETERO_COMPUTE.md](HETERO_COMPUTE.md)。

## 配置与密钥

- **`config.yaml`（仓库根）**：全项目唯一配置。段落归属——`inference`（含 `mode` / `backend` /
  `remote` / `model_registry`）/ `server` / `ingestion` / `web_search` / `math_research` / `ppt` /
  `agent` / `schedules` → backend；`inference` 的 `backend` + `remote` 键 → inference-server；
  `rag`（含档位注册表 `tiers`；host/port 两侧共用）/ `tuning` → rag；`auth.devices` → 后端多设备
  票据（留空即单密钥模式）。各项含义见文件内注释。
- **`.env.local`（仓库根，不入库）**：`cp .env.example .env.local` 后填写三个密钥——
  `AGENT_API_KEY`（前端 → backend）、`INFER_API_KEY`（backend → inference-server）、
  `RAG_API_KEY`（backend → rag）。devctl 启动时自动 source 并 export 给各服务；Vite 代理
  （`envDir` 指向仓库根）读同一份文件、在 Node 侧注入 `Authorization` 头——浏览器永不持有密钥。

## 端口与安全边界

| 服务 | 端口 | 默认绑定 | 鉴权 |
|---|---|---|---|
| frontend (Vite) | 5173 | `0.0.0.0`（唯一对外入口，仅限受信通路暴露） | 无（代理代持密钥） |
| backend | 14159 | `127.0.0.1`（外部访问统一走前端 `/api` 代理） | `AGENT_API_KEY` |
| inference-server | 14161 | `127.0.0.1`（仅 `inference.mode: remote` 时需要） | `INFER_API_KEY` |
| rag | 14160 | `127.0.0.1`（分离形态下改为设备 A 的地址） | `RAG_API_KEY` |
| searxng | 8080 | `127.0.0.1`（原生 venv 部署、无 Docker，只供 web_research 沙箱调用） | 无（仅回环） |

三个后端服务的密钥留空 = 未启用（告警后放行），便于首启 / dev。**但绑定到非回环地址时留空会被拒绝启动**：
改 host 与设密钥是两个独立动作，先改 host 后忘密钥就等于对整个 tailnet 裸奔，故这条护栏是硬失败而非告警。

⚠️ 5173 + 代理注入密钥意味着**能访问 5173 的设备等于免钥使用 Agent**（含 shell 等强力工具），
只允许暴露在 Tailscale 内网 / Cloudflare Access 等受信通路上。

## 各服务详情

- [backend/README.md](backend/README.md) —— 编排循环、工具插件、会话记忆、上下文压缩、API 全量说明
- [rag/README.md](rag/README.md) —— 档位选择、分类路由、两阶段检索、数据治理、API
- [frontend/README.md](frontend/README.md) —— 功能清单、目录结构、局域网/HTTPS 访问
- [ARCHITECTURE.md](ARCHITECTURE.md) —— 整体运行架构示意图
- [HETERO_COMPUTE.md](HETERO_COMPUTE.md) —— 算力分离与 inference-server 的执行账本（该服务无独立 README）
