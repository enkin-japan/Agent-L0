# rag —— Agent 知识与记忆数据库

> [Agent](../README.md) monorepo 的检索服务。

为 AI Agent 提供的本地化 RAG（检索增强生成）后端服务，用于存储和检索知识、记忆等信息。基于 **FastAPI + ChromaDB + Qwen3 / Qwen3-VL 双模型**（Embedding + Reranker）构建，支持单机运行或将计算面部署到独立算力端；计算面设备优先级 `cuda`（Colab/生产 GPU 档）> `mps`（Apple Silicon 开发机）> `cpu`（`compute_core.py:92-94`）。

## 数据/计算拆分（B2c）

服务分两面，按 `config.yaml` 的 `rag.compute.host` 分派，**同一套代码两种形态**：

| | 数据面 `rag_server.py`（:14160） | 计算面 `compute_server.py`（:14162） |
|---|---|---|
| 职责 | Chroma 向量库、原文备份、分类路由、切块、治理、检索编排 | 嵌入 / 重排 / token 计数（无状态，不落任何用户数据） |
| 部署 | **工作端**（用户数据只落这里） | **算力端**（跑大档位模型） |
| 依赖 | 不 import torch/transformers | torch + transformers（`compute_core.py`） |

- **`compute.host` 留空 = 融合模式**：`rag_server.py` 进程内加载模型（`compute_core.py`），
  单机 all-in-one 现状，行为与拆分前完全一致。
- **非空 = 拆分模式**：数据面跨机拨号计算面。计算面离线时数据面照常启动——纯数据操作
  （列类/删除）恒可用，需要计算的操作返回 503、计算面恢复后自愈（工作端开机不被算力端卡死）。
- 环境变量布局旋钮：`RAG_CONFIG`（config 路径覆盖）、`RAG_DATA_DIR`（`chroma_data/`+
  `storage/` 的数据根覆盖；bundle 布局注入 app 数据根，缺省 = 本目录现状）。

## 核心特性

- **自动分类入库**：文档插入时根据向量相似度自动路由到对应的分类库，无法匹配的流入未分类库兜底
- **多库并发检索**：查询时按意图分类软路由到最相关的若干分类库，线程池并发查询
- **三路候选并集检索**：向量召回 + 标识符 contains 预取 + **FTS5 关键词腿**（CJK bigram 预分词，`fts_index.py`）——三路只扩池不定序，Reranker（yes/no 概率打分）终局裁决，低置信度结果自动过滤。FTS 跨全库不受软路由约束（路由漏路兜底），一切操作 fail-open，索引可删可重建（删 `fts_index.sqlite3` + reembed）
- **检索质量评估集**：`evals/`（夹具语料 + 51 条固定题集，八类形态，含误路由 misroute）——任何检索侧改动先跑 `evals/run_retrieval_eval.py` 对比 `BASELINE.json` 防负优化
- **Token 感知切块**：按行累积切块，块间保留 token 级重叠（overlap），保证上下文连续性
- **原始文件备份**：每篇入库文档在 `storage/` 下保留原文备份，支持按路径整篇删除；也是换档全量重建的数据源
- **离线数据治理**：新增分类后可触发重分拣，将未分类库中的文档重新归位
- **档位选择制**：一个「档位」= 一对同家族同尺寸的 {嵌入, 重排} 模型（0.6B / 4B / 8B 纯文本 + 2B / 8B 多模态）。换档走 `devctl rag-model` 选择，不手改配置；换档后维度不符会**快速失败**并要求重建

## 架构与数据流

```
入库:  文档 ──► 文档级向量 ──► 与分类描述比对(meta库) ──► 命中分类库 / 未分类库
                                                        │
                                    原文备份到 storage/ ─┤
                                                        ▼
                                         切块 ──► 块级向量 ──► ChromaDB

查询:  意图分类 ──► 软路由选库(≤N个) ─┐
                                     ├──► 并发向量召回 ─┐
       优化检索句 ──► 检索向量 ───────┘   (未分类库兜底) ├─► Reranker 重排 ──► 置信度过滤 ──► MMR ──► Top-K
       标识符 token ──► contains 预取(路由库内) ─────────┤
       查询全文 ──► FTS bigram 关键词命中(跨全库) ───────┘
```

- **向量模型**：`Qwen3-Embedding-*`（last-token pooling + L2 归一化）
- **重排模型**：`Qwen3-Reranker-*`（判别式 yes/no logits → softmax 概率）
- **距离度量**：所有集合统一使用**余弦距离**（0~2，越小越相似），与配置阈值一致
- 具体尺寸由当前档位决定（默认 `vl-2b`）；模型优先从本地 `models/` 目录加载，缺失时自动从 Hugging Face Hub 下载

## 快速开始

### 环境要求

- macOS（Apple Silicon 推荐，自动启用 MPS）或 Linux + CUDA（生产 GPU 档）；均无则回退 CPU
- Python 3.11 ~ 3.13

### 启动

本服务是 monorepo 的一部分，统一由仓库根的启动器管理：

```bash
../devctl.sh setup       # 首次部署（建 .venv + 装依赖）
../devctl.sh start rag   # 仅启动本服务；start 缺省启动全部
```

单独调试也可直接 `.venv/bin/python rag_server.py`（数据面/融合）或
`.venv/bin/python compute_server.py`（计算面）。
数据面默认监听 `127.0.0.1:14160`，交互式 API 文档见 `http://127.0.0.1:14160/docs`。

算力分离部署（B2c 拆分）：数据面留在工作端（`rag.host` 回环，随 `--profile edge` /
app sidecar 启动），计算面跑在算力端（`../devctl.sh start --profile gpu`，
`rag.compute.host` 填算力端 Tailscale 地址——算力端拿它当绑定地址、工作端拿它当拨号
地址，一键两职）。跨机时**必须**两侧 `.env.local` 设同一 `RAG_COMPUTE_API_KEY`——
计算面绑定非回环地址却没有密钥会被护栏拒绝启动（见「鉴权」）。
（过渡拓扑：`compute.host` 留空 + `rag.host` 填算力端地址 = 整个融合 RAG 在算力端，
拆分前的旧形态，仍受支持；此时跨机密钥是 `RAG_API_KEY`。）

## 档位与换档

一个**档位（tier）**= 一对同家族同尺寸的嵌入 + 重排模型。当前档位由「`.rag-tier-selection`
指针 → `rag.active_tier` → `rag.embed_path`/`rerank_path` 兜底」次序解析：

```bash
../devctl.sh rag-model   # 交互选择 → 写仓库根 .rag-tier-selection（gitignored）
```

| 档位 | 模态 | 维度 | 状态（2026-08-08 A100 五档选型后） |
|---|---|---|---|
| `text-0.6b` | text | 1024 | 阈值实测标定 ✓（历史默认，已换档） |
| `text-4b` | text | 2560 | 选型否决（检索 0.882 反低于 0.6b，尺寸无红利），阈值故不标定 |
| `text-8b` | text | 4096 | 选型否决（0.912 仍低于 0.6b/vl-2b 的 0.941），阈值故不标定 |
| `vl-2b` | vl | 2048 | **默认/生产档（用户定档）**，阈值 2026-08-09 A100 实测标定 ✓（rerank 0.5→0.3，multidoc 归零一并修） |
| `vl-8b` | vl | 4096 | 选型否决（实验态检索塌方 0.588），阈值故不标定 |

> `modality: vl` 档位当前仅「加载 + 文本 I/O」——**图像真入库是另一期（L2）**，尚未实施。
> 各档的余弦分布不同，三个阈值（`insert_routing` / `query_routing` / `rerank_min_score`）
> 须逐档标定；已标定 = `text-0.6b` 与 `vl-2b`（数值与依据见 config.yaml 各档注记），
> 否决档保留的是占位继承值（真要启用先重标定）。

**换档纪律（关键不变量）**：向量库一旦入库，档位即固定。换档 = 开发者**主动重建**，
运行中不会静默切换已有向量库的档位；需要由执行端明确确认重建。库里盖着档位戳（`chroma_data/.tier_stamp.json`）。融合模式启动时比对，不符拒启
（换档必然伴随重启、操作员在场）；拆分模式启动或首次请求探到即比对，不符则缓存为
fatal、**数据面降级存活**（计算类请求 500 带指引，纯数据操作与重建端点照常——拒启
会把 app 的重建引导路一并堵死，build 71 实机教训）。绝不让新旧维度的向量混库。

**正式口径（分离部署审计 2026-08-31 用户拍板，已落地）**：重建由**执行端**驱动。
数据面自带受鉴权的重建管理端点（`retier.py` + rag_server 装配）：

- `GET /api/v1/maintenance/retier`——库戳 vs 计算面**活体**身份（绕缓存现查，与换档
  护栏同口径）+ 重建进度；`needs_rebuild` 是 app 的唯一提示判据（真换档 mismatch，
  或耐久标记揭示的**中断残局**——上一场重建死在半路时档位戳反而是「健康」假象）。
- `POST /api/v1/maintenance/retier/rebuild`——单飞后台重建：备份分类路由（手写描述
  绝不被空/缺项 dump 覆盖，孤儿分类目录占位补建）+ 过时标记快照 → 落耐久标记 →
  清库（向量+FTS+戳；**原文 storage/ 一字节不动**）→ 按活体身份重盖戳 → 重建路由 →
  同步逐篇重灌（逐篇记账，`docs_failed` 单列）→ 按 source_path 重挂过时标记（重灌中
  被自动路由迁移改了路径的进 `stale_lost` 记账）→ 事后戳断言，验收过了才撤标记。
  开始清库前先冻结新写入并排空已受理写入，治理元数据备份和中断标记耐久保存，失败后重试恢复。
  两侧已同档时 409 拒绝白重灌（`force` 仅修库场景）；重建窗口内**全部写入面**
  （insert/分类增删/purge/stale/删除/reclassify）一律 409 冻结。

macOS app 的 `RetierMonitor` 周期轮询（backend 薄代理 `/api/v1/rag/retier`）、发现
`needs_rebuild` 弹窗征求确认、驱动重建并跟进进度。知识库页面也可主动检查并确认重建；
“稍后”只抑制自动弹窗，不封锁手动入口。健康或未知状态不会被强制重建，连接变化后不会
把已发出的操作转到新连接；未确认受理与失败会显示在页面上，系统通知遵守用户设置。devctl 侧刻意不做重建
自动化（曾做过一版当日移除：模型的事归计算端，库的事归执行端，repo 工具不该是执行端
依赖）。接线自检：`rag/.venv/bin/python tests/retier_wiring_check.py`（真 chroma 临时
库，不进 devctl 门）。

手动后备流程（融合模式换档后服务起不来时仍需要它；`scripts/reembed.py`，从
`storage/raw_files` 原文备份全量重灌）：

```bash
../devctl.sh rag-model                          # 1) 选新档位
python scripts/reembed.py dump-categories       # 2) 旧服务还在跑时备份分类描述
../devctl.sh stop rag                           # 3)
python scripts/reembed.py wipe                  # 4) 删 chroma_data（换新维度的空库）
rm -f fts_index.sqlite3*                        # 4.5) FTS 索引连带清（fail-open，重灌时随 /insert 重建）
../devctl.sh start rag                          # 5) 新档位加载 + 空库盖新戳
python scripts/reembed.py rebuild               # 6) 重建分类 + 全量重灌（--dry-run 可先看计划）
```

> 第 2 步不能省：分类的**路由描述**只存在 meta 库里，会随重建被清掉；原文备份里没有它。

## 鉴权

两面各一把钥匙，`/health` 均免鉴权供探活：

- **数据面** `/api/v1/*`：`RAG_API_KEY`（backend 侧三个 rag 客户端
  `rag_search` / `rag_admin` / `rag_sink` 填同一个值）
- **计算面** `/api/v1/*`：`RAG_COMPUTE_API_KEY`（数据面作为客户端读同一变量携带；
  两台机器的 `.env.local` 填同一个值）

计算面对外只暴露三条无状态计算原语（`compute_server.py:82-93`，拆分模式下跑在算力端）：
`POST /api/v1/embed`（向量化）、`POST /api/v1/tokenize`（token 计数）、`POST /api/v1/rerank`
（重排打分）——不落任何用户数据，数据面作为唯一客户端调用。

通则（部署铁则 §8，四个服务同一条纪律）：

- 密钥留空 = 未启用（启动告警后放行），仅限开发 / 回环
- **绑定非回环地址 + 密钥留空 = 拒绝启动**（不是告警）：跨机部署时"改 host"和"设密钥"是两个
  独立动作，先改 host 后忘密钥，服务就对整个内网裸奔
- 数据面与计算面密钥不一致时，数据面返回 500 并明说「检查两侧 .env.local 同值」，
  不伪装成网络错误

## 配置说明（仓库根 config.yaml 的 rag / tuning 段）

配置统一在**仓库根**的 `config.yaml`（backend 与 rag 共读一份）。本服务读取其中两段：

```yaml
rag:
  host: "127.0.0.1"                 # 数据面绑定/拨号地址（拆分部署=工作端回环；
                                    #   过渡拓扑=算力端 Tailscale 地址，两侧同值）
  port: 14160
  compute:
    host: ""                        # 空=融合模式；非空=算力端 Tailscale 地址（拆分模式）
    port: 14162
  active_tier: "vl-2b"              # 当前档位 id（被 .rag-tier-selection 指针覆盖）
  tiers:                            # 档位注册表（字段见 config.yaml 内注释）
    - id: "text-0.6b"
      modality: "text"              # text 纯文本 / vl 视觉多模态
      embed_repo: "Qwen/Qwen3-Embedding-0.6B"
      rerank_repo: "Qwen/Qwen3-Reranker-0.6B"
      embed_dim: 1024               # 护栏比对用的**提示值**：启动恒以运行时探测为准，不符只告警
      dtype: "fp16"
      thresholds: { insert_routing_max_distance: 0.8, query_routing_max_distance: 0.85, rerank_min_score: 0.5 }
    # … 另有 text-4b / text-8b / vl-2b / vl-8b
  embed_path: "Qwen/Qwen3-Embedding-0.6B"   # 兜底：无 tiers / active_tier 未命中时用
  rerank_path: "Qwen/Qwen3-Reranker-0.6B"
  max_length: 8192                  # tokenizer 截断长度
  batch_size: 32                    # 向量化批大小

tuning:
  insert_routing_max_distance: 0.8    # 入库路由阈值：文档与分类描述的最大余弦距离
  query_routing_max_distance: 0.85    # 查询路由阈值：超过该距离的分类库被剔除
  query_max_dbs: 3                    # 查询时最多并发检索的分类库数
  query_max_chunks: 4                 # 每个库召回的片段数
  query_final_max_results: 3          # 重排后最终返回条数
  rerank_min_score: 0.5               # 重排置信度下限（0~1），低于则过滤
  chunk_max_tokens: 256               # 切块最大 token 数
  chunk_overlap_tokens: 128           # 相邻块重叠 token 数
```

> 三个阈值（`insert_routing` / `query_routing` / `rerank_min_score`）若档位的 `thresholds` 里
> 给了，**以档位值为准**——余弦分布随模型尺寸变，各档必须各自标定；`tuning` 段是档位未覆盖时
> 的全局兜底。其余切块 / 召回参数与档位无关，恒读 `tuning`。

调优建议：入库文档总被丢进未分类库 → 调大 `insert_routing_max_distance`；检索结果太少 → 调低 `rerank_min_score` 或调大 `query_max_chunks`。

## API 接口

### 基础监控

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查，返回运行设备（免鉴权） |
| POST | `/tokenize` | 统计文本 token 数（`model_type`: `embedding` / `rerank`） |

### 分类管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/category` | 列出所有分类及描述 |
| POST | `/api/v1/category` | 创建/更新分类（`db_name` + `description`，描述用于路由匹配，写得越具体路由越准） |
| DELETE | `/api/v1/category/{db_name}` | 删除分类路由（该分类库中的数据保留） |
| DELETE | `/api/v1/category/{db_name}/purge` | 真删分类：向量 collection 整库丢弃 + 原文目录移入 `.trash` 回收 + 路由条目删除（与上一行「只删路由」分工不同，这是「删库」） |

### 数据入库与治理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/document/list` | 管理面板数据源：不带 `category` 返回分类总览（文档数/切片数），带则返回该分类的文档清单 |
| POST | `/api/v1/document/insert` | 插入文档（后台异步处理：分类→备份→切块→向量化→入库）。可选 `category` 显式指定目标分类：不存在则 400 拒绝并附现有目录（不静默兜底）；缺省走向量自动路由 |
| GET | `/api/v1/document/context` | 取相邻块（R3 凭证指针）：按 `source_path`+`seq` 返回 [seq-n, seq+n] 窗口；纯数据操作，计算面离线可用；旧格式切片（无 seq）明说并指向全量重建 |
| DELETE | `/api/v1/document/delete` | 按 `source_path`（元数据中的备份路径）跨库整篇删除，含磁盘备份；零命中时返回引导提示与 `deleted_chunks: 0` |
| POST | `/api/v1/document/stale` | 标记/取消标记文档为过时（只写元数据不动内容），检索结果与管理面板据此带出提示 |
| POST | `/api/v1/governance/reclassify` | 触发未分类文档重分拣（后台异步） |

> `insert` 另有可选 `source_path` 字段：**供 `reembed.py` 重灌时复用已有原文备份**，避免
> 再写一份副本把语料翻倍。取值须落在 `storage/` 内且文件存在，否则 400。普通入库不要传。

### 核心检索

`POST /api/v1/document/query`

```json
{
  "query": "怎么在 Python 里同时跑多个协程？",     // 原始提问（用于重排）
  "category": "Python 编程 异步编程",              // LLM 分析出的意图分类（用于路由选库）
  "optimized_query": "asyncio 并发运行多个协程",   // LLM 优化的检索句（用于向量召回）
  "top_k_dbs": 3,          // 可选，默认取配置值
  "chunk_k_per_db": 4,     // 可选
  "rerank_top_k": 3        // 可选
}
```

返回：

```json
{
  "original_query": "...",
  "scanned_dbs": ["python_programming", "unclassified_db"],
  "total_chunks_retrieved": 5,
  "results": [
    {
      "db": "python_programming",
      "id": "…",
      "document": "命中的文本片段",
      "metadata": { "title": "…", "source_path": "storage/raw_files/…" },
      "rerank_score": 0.999
    }
  ]
}
```

### 使用示例

```bash
# 启用鉴权后各请求需带 -H "Authorization: Bearer $RAG_API_KEY"

# 1. 先建分类（描述用于路由匹配）
curl -X POST http://127.0.0.1:14160/api/v1/category \
  -H 'Content-Type: application/json' \
  -d '{"db_name": "python_programming", "description": "Python 编程语言相关的知识，包括语法、标准库、异步编程等"}'

# 2. 插入文档（指定分类；省略 category 字段则走向量自动路由）
curl -X POST http://127.0.0.1:14160/api/v1/document/insert \
  -H 'Content-Type: application/json' \
  -d '{"title": "asyncio 入门", "document": "asyncio.gather() 可以并发运行多个协程……", "category": "python_programming"}'

# 3. 检索
curl -X POST http://127.0.0.1:14160/api/v1/document/query \
  -H 'Content-Type: application/json' \
  -d '{"query": "如何并发跑协程", "category": "Python 异步编程", "optimized_query": "asyncio 并发协程"}'
```

## 目录结构

```
rag/
├── rag_server.py          # 数据面（切块、路由、检索编排、治理、换档护栏；融合模式内含计算）
├── compute_core.py        # 计算核（档位解析、模型下载/加载、嵌入、重排、token 计数）
├── compute_server.py      # 计算面 HTTP 服务（:14162，无状态；拆分模式跑在算力端）
├── fts_index.py           # FTS 关键词腿：与 Chroma 并置的 SQLite FTS5 索引（CJK bigram 预分词）
├── retrieval_extras.py    # 检索侧纯函数增强：extract_identifiers（标识符预取）+ mmr_select（多样性去重）
├── fts_index.sqlite3      # FTS5 索引文件（gitignored 运行期产物，可删可重建：删了 + reembed 即恢复）
├── scripts/
│   ├── reembed.py                # 换档/升级用的全量重建（dump-categories / wipe / rebuild）
│   └── migrate_source_paths.py   # 数据根搬迁后 source_path 前缀一次性修正（B2c 拓扑切换用）
├── evals/                 # 检索质量评估集：夹具语料 + 51 条固定题集（八类形态）+ run_retrieval_eval.py + BASELINE.json
├── requirements.txt       # Python 依赖（配置见仓库根 config.yaml，部署/启动走根 devctl.sh）
├── models/                # 本地模型权重（首次运行按档位自动下载）
│   ├── Qwen3-Embedding-0.6B/  Qwen3-Reranker-0.6B/
│   └── Qwen3-VL-Embedding-2B/ Qwen3-VL-Reranker-2B/
├── chroma_data/           # ChromaDB 持久化数据（自动生成）
│   └── .tier_stamp.json   # 档位戳：建库时所用的档位 id / 模态 / 维度（换档护栏的依据）
└── storage/               # 原始文档备份（重建的承重网）
    ├── raw_files/<分类名>/    # 已分类文档原文
    └── unclassified_raw/      # 未分类文档原文（重分拣的数据源）
```

## 注意事项

- **入库是异步的**：`insert` 接口立即返回，实际处理在后台进行，结果看服务日志
- **系统类目 `agent_memory`**：Agent 启动时会自动创建该分类，用作其长期记忆沉淀汇——被上下文压缩折叠掉的对话原文存于此、供 `rag_search` 召回。请勿随意删除该分类的路由
- **分类描述决定路由质量**：`meta_classification_db` 中存的是分类描述的向量，描述写得越具体、越贴近该类文档的实际内容，自动分类越准
- **删除分类 ≠ 删除数据**：`DELETE /api/v1/category/{db_name}` 只移除路由，该分类库中的向量数据仍在
- **换档必重建**：库维度与档位绑定，靠 `.tier_stamp.json` + 运行时探测把关，不符即拒绝服务而非静默混库
- **MPS 显存**：两个模型按档位 dtype 常驻（当前默认 `vl-2b`：bf16 2B × 2 ≈ 8GB），推理通过全局锁串行化，避免 MPS 并发冲突；大档位（4B/8B）在 16GB 开发机上跑不动，需设备 A
- **换档须重灌**：`text-0.6b`(1024 维) → `vl-2b`(2048 维) 这类跨维度换档须全量重建——正式口径是执行端驱动（app 检测提示 / 数据面 `POST /api/v1/maintenance/retier/rebuild`，见「档位与换档」节；手动后备 `rag/scripts/reembed.py`）。库维度不符时护栏会拒绝服务，不是静默混库
