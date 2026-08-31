# Agent

运行在 Apple Silicon Mac 上的**全本地** AI Agent：本地推理 + 分类路由式 RAG 长期记忆 + Web 前端，
数据不出门。单仓（monorepo）组织，四个服务 + 一个可选搜索层。

可单机跑全套（all-in-one），也可把重型计算（模型推理 / RAG 嵌入重排）拆到另一台机器上，
执行层与会话数据留在用户机。**入库的 `config.yaml` 是空模板形态**（`inference.mode: remote`
但两个 host 留空 = 融合/本机模式）：机器差异全部走库外——算力端地址与密钥填在各机的
`.env.local`（macOS app 则在应用设置里填）。dev 形态配置文件两机 pull 即同步；app 形态
执行端没有 repo，config 随包烘入、跨端状态（主模型/RAG 档位）靠**发现比对**而非同步
（inference /health 模型身份护栏、RAG 档位戳 + app 换档检测——分离部署审计 2026-08-31）。
退回单机见下文「部署形态」。

```
Agent/
├── devctl.sh            # 标准启动器（唯一入口）：setup / model / rag-model / start / stop / restart / status / logs
├── config.yaml          # 全项目唯一配置文件（backend / rag / inference-server 共读，改这里即可）
├── .env.example         # 环境变量模板（复制为 .env.local 填写密钥；各服务与前端代理共读）
├── CHARTER.md           # ★ 架构宪章：核心思想 + 开发铁律（每次迭代开工前通读）
├── ARCHITECTURE.md      # 运行架构逻辑示意图（拓扑 / 分层 / 数据流 / 生命周期）
├── PROTOCOL.md          # 交互协议 v1（事件账本契约，三端行为标准）
├── DEFERRED.md          # 未竟架构账本（轻量已交付·完整版待做；与 CHARTER §7 非目标相区分）
├── IDEAS.md             # 巧思搁置账本（想过、评估过、暂时不做）
├── docs/history/        # 开发历史档案（2026-08-29 冻结归档：执行台账 / 各专项账本 / 复盘 / 蓝图）
├── evaluations/         # 开源项目评估文档（grok-build / mem0 / browser-use / codegraph / markitdown）
├── protocol/fixtures/   # 协议测试夹具（web 与原生 app 共享的行为标准）
├── backend/             # Agent 主体（FastAPI, :14159）：编排循环 / 工具 / 会话记忆 / 推理后端
├── inference/           # 推理服务（FastAPI, :14161）：把推理后端 HTTP/SSE 化，供算力分离部署
├── rag/                 # RAG 知识库：数据面（:14160，ChromaDB+原文+路由，归工作端）
│                        #   + 计算面（:14162，嵌入/重排，PyTorch MPS，归算力端；可融合单进程）
├── frontend/            # Web 前端（React 19 + Vite, :5173）：聊天 / 审批 / 任务 / Deck / 定时任务
├── apple-app/           # macOS / iOS 原生客户端（SwiftUI；macOS 端内嵌执行层打包，R5）
├── api_tools/           # 手写 API 工具声明（UI 里添加的私人声明在 backend/api_tools/，不入库）
├── skills/              # 内置剧本（用户自建剧本在 backend/skills/，不入库）
├── colab/               # Colab 算力端引导（bootstrap / 保活 / Drive 中转脚本）
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
⚠️ 换 RAG 档位后向量库必须重建——正式口径是执行端驱动（app 检测提示；数据面
`GET /api/v1/maintenance/retier` 查状态、`POST …/retier/rebuild` 执行），
`rag/scripts/reembed.py` 是手动后备，详见 [rag/README.md](rag/README.md)。

## 部署形态

| 形态 | 命令 | 说明 |
|---|---|---|
| 算力分离 · 算力端 | `./devctl.sh start --profile gpu` | inference-server + RAG 计算面：只出算力，不持久化会话文本/记忆。RAG 计算面不落任何用户数据；inference-server 的会话**派生物**面：`media/`（跨机媒体中转，按最后使用 TTL 自动清扫，`inference.media_ttl_days`）；`kvstore/`（会话 KV 冻结落盘）**缺省关闭**——执行端显式开启（app 设置 → 执行层，或 `INFER_KV_PERSIST=1`）才落，留存期由执行端随请求携带、到期自动清，app 设置页有「立即清除」按钮（`DELETE /v1/kvstore`） |
| 算力分离 · 工作端 | `./devctl.sh start --profile edge -h <算力端地址>` | RAG 数据面 + SearXNG + backend + 前端：编排 / 工具 / 记忆 / 会话与知识库落盘 |
| all-in-one（入库缺省） | `./devctl.sh start` | 单机跑全部；host 留空即融合/本机模式，`inference.mode` 改回 `inprocess` 走进程内推理 |

`inference.remote.host` 与 `rag.compute.host` 各自身兼两职：算力端拿它当绑定地址，
工作端拿它当拨号地址（填算力端的 Tailscale 地址）。入库的 `config.yaml` 两处 host 留空
（空模板形态），实际地址由各机库外供给：`.env.local` 的 `INFER_REMOTE_HOST` /
`RAG_COMPUTE_HOST`（`devctl.sh start -h <host>` 会代写），macOS app 则在应用设置里填。
密钥不入库：两侧 `.env.local` 的 `INFER_API_KEY` / `RAG_COMPUTE_API_KEY` 须手工填成一致。
用户数据——会话（`history.jsonl` / `memory.json` / deck 等）与**整个知识库**（Chroma 向量库 +
原文备份，B2c 拆分后随数据面归工作端）——**全部留在工作端**，算力端只见 prompt
与在途文本、不持久化任何内容。

edge 侧的 backend 不加载模型权重，只拉分词器（B3 实测：31MB vs 全量 4.9GB；
进程常驻 1.0GB vs 设备 A 推理侧 6.5GB）。

拆分只按「是否出算力」划线：**SearXNG 归设备 B**（与 backend 同机）——它无模型无 GPU，是
`web_research` 的工具依赖，而抓正文的三档升级本就在 backend 里跑，出网口已经在 B。
完整拓扑与执行记录见 [docs/history/HETERO_COMPUTE.md](docs/history/HETERO_COMPUTE.md)（已冻结归档）。

## 配置与密钥

- **`config.yaml`（仓库根）**：全项目唯一配置。段落归属——`inference`（含 `mode` / `backend` /
  `remote` / `model_registry`）/ `server` / `ingestion` / `web_search` / `math_research` / `ppt` /
  `agent` / `schedules` → backend；`inference` 的 `backend` + `remote` 键 → inference-server；
  `rag`（含档位注册表 `tiers`；host/port 两侧共用）/ `tuning` → rag；`auth.devices` → 后端多设备
  票据（留空即单密钥模式）。各项含义见文件内注释。
- **`.env.local`（仓库根，不入库）**：`cp .env.example .env.local` 后填写密钥——
  `AGENT_API_KEY`（前端 → backend）、`INFER_API_KEY`（backend → inference-server）、
  `RAG_API_KEY`（backend → rag 数据面）、`RAG_COMPUTE_API_KEY`（rag 数据面 → 计算面，
  拆分部署两侧同值）。devctl 启动时自动 source 并 export 给各服务；Vite 代理
  （`envDir` 指向仓库根）读同一份文件、在 Node 侧注入 `Authorization` 头——浏览器永不持有密钥。

## 端口与安全边界

| 服务 | 端口 | 默认绑定 | 鉴权 |
|---|---|---|---|
| frontend (Vite) | 5173 | `0.0.0.0`（唯一对外入口，仅限受信通路暴露） | 无（代理代持密钥） |
| backend | 14159 | `127.0.0.1`（外部访问统一走前端 `/api` 代理） | `AGENT_API_KEY` |
| inference-server | 14161 | `127.0.0.1`（仅 `inference.mode: remote` 时需要） | `INFER_API_KEY` |
| rag 数据面 | 14160 | `127.0.0.1`（拆分后归工作端本机；过渡拓扑可绑算力端地址） | `RAG_API_KEY` |
| rag 计算面 | 14162 | 按 `rag.compute.host`（拆分形态绑算力端 Tailscale 地址；空=不启用/融合） | `RAG_COMPUTE_API_KEY` |
| searxng | 8080 | `127.0.0.1`（**恒为回环**：始终与 backend 同机，分离形态下随 backend 在工作端） | 无（故不可离开回环） |

各后端服务的密钥留空 = 未启用（告警后放行），便于首启 / dev。**但绑定到非回环地址时留空会被拒绝启动**：
改 host 与设密钥是两个独立动作，先改 host 后忘密钥就等于对整个 tailnet 裸奔，故这条护栏是硬失败而非告警。

⚠️ 5173 + 代理注入密钥意味着**能访问 5173 的设备等于免钥使用 Agent**（含 shell 等强力工具），
只允许暴露在 Tailscale 内网 / Cloudflare Access 等受信通路上。

## 各服务详情

- [backend/README.md](backend/README.md) —— 编排循环、工具插件、会话记忆、上下文压缩、API 全量说明
- [rag/README.md](rag/README.md) —— 档位选择、分类路由、两阶段检索、数据治理、API
- [frontend/README.md](frontend/README.md) —— 功能清单、目录结构、局域网/HTTPS 访问
- [ARCHITECTURE.md](ARCHITECTURE.md) —— 整体运行架构示意图
- [docs/history/HETERO_COMPUTE.md](docs/history/HETERO_COMPUTE.md) —— 算力分离与 inference-server 的执行账本（已冻结归档；该服务无独立 README）
- [docs/history/](docs/history/) —— 全部开发历史档案（执行台账 / MTP / OSS 对比 / 复盘等，2026-08-29 冻结）
