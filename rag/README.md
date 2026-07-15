# rag —— Agent 知识与记忆数据库

> [Agent](../README.md) monorepo 的检索服务。

为 AI Agent 提供的本地化 RAG（检索增强生成）后端服务，用于存储和检索知识、记忆等信息。基于 **FastAPI + ChromaDB + Qwen3 双模型**（Embedding + Reranker）构建，全程本地运行，在 Apple Silicon 上使用 MPS 加速。

## 核心特性

- **自动分类入库**：文档插入时根据向量相似度自动路由到对应的分类库，无法匹配的流入未分类库兜底
- **多库并发检索**：查询时按意图分类软路由到最相关的若干分类库，线程池并发查询
- **两阶段检索**：Embedding 粗召回 → Reranker（yes/no 概率打分）精排，低置信度结果自动过滤
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
                                     ├──► 并发向量召回 ──► Reranker 重排 ──► 置信度过滤 ──► Top-K
       优化检索句 ──► 检索向量 ───────┘        (未分类库强制参与兜底)
```

- **向量模型**：`Qwen3-Embedding-*`（last-token pooling + L2 归一化）
- **重排模型**：`Qwen3-Reranker-*`（判别式 yes/no logits → softmax 概率）
- **距离度量**：所有集合统一使用**余弦距离**（0~2，越小越相似），与配置阈值一致
- 具体尺寸由当前档位决定（默认 `text-0.6b`）；模型优先从本地 `models/` 目录加载，缺失时自动从 Hugging Face Hub 下载

## 快速开始

### 环境要求

- macOS（Apple Silicon 推荐，自动启用 MPS；无 MPS 时回退 CPU）
- Python 3.11 ~ 3.13

### 启动

本服务是 monorepo 的一部分，统一由仓库根的启动器管理：

```bash
../devctl.sh setup       # 首次部署（建 .venv + 装依赖）
../devctl.sh start rag   # 仅启动本服务；start 缺省启动全部
```

单独调试也可直接 `.venv/bin/python rag_server.py`。
服务默认监听 `127.0.0.1:14160`，交互式 API 文档见 `http://127.0.0.1:14160/docs`。

算力分离部署时本服务跑在设备 A（`../devctl.sh start --profile gpu`），`rag.host` 改为该机的
Tailscale 地址，设备 B 的 backend 跨机调用。此时**必须**设 `RAG_API_KEY`——
绑定非回环地址却没有密钥会被护栏拒绝启动（见「鉴权」）。

## 档位与换档

一个**档位（tier）**= 一对同家族同尺寸的嵌入 + 重排模型。当前档位由「`.rag-tier-selection`
指针 → `rag.active_tier` → `rag.embed_path`/`rerank_path` 兜底」次序解析：

```bash
../devctl.sh rag-model   # 交互选择 → 写仓库根 .rag-tier-selection（gitignored）
```

| 档位 | 模态 | 维度 | 状态 |
|---|---|---|---|
| `text-0.6b` | text | 1024 | 默认，阈值实测标定 ✓ |
| `text-4b` | text | 2560 | 阈值待标定 |
| `text-8b` | text | 4096 | 生产档，需设备 A（≥40GB 或双卡分置） |
| `vl-2b` | vl | 2048 | 加载契约已实测 ✓，阈值待标定 |
| `vl-8b` | vl | 4096 | 多模态生产档，需设备 A |

> `modality: vl` 档位当前仅「加载 + 文本 I/O」——**图像真入库是另一期（L2）**，尚未实施。
> 各档的余弦分布不同，三个阈值（`insert_routing` / `query_routing` / `rerank_min_score`）
> 须逐档标定；非 0.6B 档现为占位继承值。

**换档纪律（关键不变量）**：向量库一旦入库，档位即固定。换档 = 开发者**主动重建**，
运行中 / 用户侧绝不换档。库里盖着档位戳（`chroma_data/.tier_stamp.json`），启动时与运行时
探测到的真实维度比对，不符即快速失败并提示先重建——而不是让新旧维度的向量在同一个库里混。

标准换档流程（`scripts/reembed.py`，从 `storage/raw_files` 原文备份全量重灌）：

```bash
../devctl.sh rag-model                          # 1) 选新档位
python scripts/reembed.py dump-categories       # 2) 旧服务还在跑时备份分类描述
../devctl.sh stop rag                           # 3)
python scripts/reembed.py wipe                  # 4) 删 chroma_data（换新维度的空库）
../devctl.sh start rag                          # 5) 新档位加载 + 空库盖新戳
python scripts/reembed.py rebuild               # 6) 重建分类 + 全量重灌（--dry-run 可先看计划）
```

> 第 2 步不能省：分类的**路由描述**只存在 meta 库里，会随重建被清掉；原文备份里没有它。

## 鉴权

`/api/v1/*` 全量套 Bearer 校验，密钥读环境变量 `RAG_API_KEY`（仓库根 `.env.local`，
与 backend 侧三个 rag 客户端 `rag_search` / `rag_admin` / `rag_sink` 填同一个值）。
`/health` 免鉴权供探活。

- 密钥留空 = 未启用（启动告警后放行），仅限开发 / 回环
- **绑定非回环地址 + 密钥留空 = 拒绝启动**（不是告警）：跨机部署时"改 host"和"设密钥"是两个
  独立动作，先改 host 后忘密钥，服务就对整个内网裸奔

## 配置说明（仓库根 config.yaml 的 rag / tuning 段）

配置统一在**仓库根**的 `config.yaml`（backend 与 rag 共读一份）。本服务读取其中两段：

```yaml
rag:
  host: "127.0.0.1"                 # 分离部署时改为设备 A 的 Tailscale 地址（两侧填同一个值）
  port: 14160
  active_tier: "text-0.6b"          # 当前档位 id（被 .rag-tier-selection 指针覆盖）
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

### 数据入库与治理

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/document/insert` | 插入文档（后台异步处理：分类→备份→切块→向量化→入库）。可选 `category` 显式指定目标分类：不存在则 400 拒绝并附现有目录（不静默兜底）；缺省走向量自动路由 |
| DELETE | `/api/v1/document/delete` | 按 `source_path`（元数据中的备份路径）跨库整篇删除，含磁盘备份；零命中时返回引导提示与 `deleted_chunks: 0` |
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
├── rag_server.py          # 服务主程序（档位解析、模型加载、切块、路由、检索、重排、换档护栏）
├── scripts/
│   └── reembed.py         # 换档/升级用的全量重建（dump-categories / wipe / rebuild）
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
- **MPS 显存**：两个模型按档位 dtype 常驻（0.6B fp16 ≈ 2.4GB），推理通过全局锁串行化，避免 MPS 并发冲突；大档位（4B/8B）在 16GB 开发机上跑不动，需设备 A
