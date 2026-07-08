# rag —— Agent 知识与记忆数据库

> [MacAgent](../README.md) monorepo 的检索服务。

为 AI Agent 提供的本地化 RAG（检索增强生成）后端服务，用于存储和检索知识、记忆等信息。基于 **FastAPI + ChromaDB + Qwen3 双模型**（Embedding + Reranker）构建，全程本地运行，在 Apple Silicon 上使用 MPS 加速。

## 核心特性

- **自动分类入库**：文档插入时根据向量相似度自动路由到对应的分类库，无法匹配的流入未分类库兜底
- **多库并发检索**：查询时按意图分类软路由到最相关的若干分类库，线程池并发查询
- **两阶段检索**：Embedding 粗召回 → Reranker（yes/no 概率打分）精排，低置信度结果自动过滤
- **Token 感知切块**：按行累积切块，块间保留 token 级重叠（overlap），保证上下文连续性
- **原始文件备份**：每篇入库文档在 `storage/` 下保留原文备份，支持按路径整篇删除
- **离线数据治理**：新增分类后可触发重分拣，将未分类库中的文档重新归位

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

- **向量模型**：`Qwen/Qwen3-Embedding-0.6B`（last-token pooling + L2 归一化）
- **重排模型**：`Qwen/Qwen3-Reranker-0.6B`（判别式 yes/no logits → softmax 概率）
- **距离度量**：所有集合统一使用**余弦距离**（0~2，越小越相似），与配置阈值一致
- 模型优先从本地 `models/` 目录加载，缺失时自动从 Hugging Face Hub 下载

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
服务默认监听 `127.0.0.1:14160`（仅本机回环，只供同机的 Agent 调用），交互式 API 文档见 `http://127.0.0.1:14160/docs`。

## 配置说明（仓库根 config.yaml 的 rag / tuning 段）

配置统一在**仓库根**的 `config.yaml`（backend 与 rag 共读一份）。本服务读取其中两段：

```yaml
rag:
  host: "127.0.0.1"
  port: 14160
  embed_path: "Qwen/Qwen3-Embedding-0.6B"   # 本地路径或 HF Repo ID
  rerank_path: "Qwen/Qwen3-Reranker-0.6B"
  max_length: 8192                           # tokenizer 截断长度
  batch_size: 32                             # 向量化批大小

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

调优建议：入库文档总被丢进未分类库 → 调大 `insert_routing_max_distance`；检索结果太少 → 调低 `rerank_min_score` 或调大 `query_max_chunks`。

## API 接口

### 基础监控

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查，返回运行设备 |
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
├── rag_server.py          # 服务主程序（模型加载、切块、路由、检索、重排）
├── requirements.txt       # Python 依赖（配置见仓库根 config.yaml，部署/启动走根 devctl.sh）
├── models/                # 本地模型权重（首次运行自动下载）
│   ├── Qwen3-Embedding-0.6B/
│   └── Qwen3-Reranker-0.6B/
├── chroma_data/           # ChromaDB 持久化数据（自动生成）
└── storage/               # 原始文档备份
    ├── raw_files/<分类名>/    # 已分类文档原文
    └── unclassified_raw/      # 未分类文档原文（重分拣的数据源）
```

## 注意事项

- **入库是异步的**：`insert` 接口立即返回，实际处理在后台进行，结果看服务日志
- **系统类目 `agent_memory`**：MacAgent 启动时会自动创建该分类，用作其长期记忆沉淀汇——被上下文压缩折叠掉的对话原文存于此、供 `rag_search` 召回。请勿随意删除该分类的路由
- **分类描述决定路由质量**：`meta_classification_db` 中存的是分类描述的向量，描述写得越具体、越贴近该类文档的实际内容，自动分类越准
- **删除分类 ≠ 删除数据**：`DELETE /api/v1/category/{db_name}` 只移除路由，该分类库中的向量数据仍在
- **MPS 显存**：两个 0.6B 模型以 float16 常驻，推理通过全局锁串行化，避免 MPS 并发冲突
