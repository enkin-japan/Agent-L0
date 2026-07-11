# 项目运行架构逻辑示意图

> 描述本仓库（monorepo）四个组件——backend（Agent 主体）、rag（RAG 数据库）、
> frontend（交互前端）与可选的 SearXNG——的运行架构与关键数据流。
> 配置统一在仓库根 config.yaml，启动统一走根 devctl.sh。
>
> **交互层重整进行中**（2026-07）：目标架构与分期计划见 `REARCHITECTURE.md`，
> 冻结的事件账本协议见 `PROTOCOL.md`（双端行为标准 + `protocol/fixtures/` 测试夹具）。
> 本文描述的 SSE/轮询交互面在 Phase 1~4 期间逐步被事件账本替代（旧端点兼容至 Phase 4）。

---

## 1. 服务总览

| 组件 | 端口 | 绑定 | 技术栈 | 角色 |
|---|---|---|---|---|
| frontend | 5173 | 0.0.0.0 | React 19 + Vite + Zustand | 用户交互界面（唯一对外入口） |
| backend | 14159 | 127.0.0.1 | FastAPI + MLX (gemma-4-e4b 4bit) | Agent 主体：推理/编排/工具/会话 |
| rag | 14160 | 127.0.0.1 | FastAPI + PyTorch(MPS) + ChromaDB | 分类路由式向量知识库 |
| SearXNG（可选） | 8080 | 原生 venv | searxng/.venv + searxng-src（pin 提交） | 联网搜索聚合（web_research 依赖） |

启停统一走根目录 `devctl.sh`（`setup` 首次部署；启动顺序 RAG → SearXNG → backend → 前端；日志在 `logs/`）。

---

## 2. 顶层拓扑

```
                        受信通路（Tailscale / Cloudflare Access）
                                        │
                                        ▼
                    ┌─────────────────────────────────────┐
                    │        浏览器（任意受信设备）          │
                    │  React SPA：聊天 / 审批卡片 / 任务面板 │
                    │  Deck 预览 / 记忆面板 / 定时任务侧栏    │
                    └───────────────────┬─────────────────┘
                                        │ 同源 /api/*（浏览器不持有密钥）
                                        ▼
                    ┌─────────────────────────────────────┐
                    │   Vite dev server :5173（生产=Nginx）  │
                    │   代理 /api → 127.0.0.1:14159         │
                    │   并注入 Authorization: Bearer <key>   │
                    └───────────────────┬─────────────────┘
                                        │ HTTP + SSE
        ════════════════════════════════▼══════════════════════════════════
                    ┌─────────────────────────────────────┐
                    │        backend  :14159 (仅回环)       │
                    │  FastAPI + Bearer 鉴权 + CORS 白名单   │
                    └──┬──────────────┬─────────────┬─────┘
                       │              │             │
          rag_search / │   web_research│             │ PDF 导出：无头 Chrome
          rag_admin /  │   (受限沙箱)  │             │ 打印前端 /print/deck
          记忆沉淀 HTTP │              │             │（渲染器只有前端一份）
                       ▼              ▼             ▼
        ┌──────────────────┐  ┌──────────────┐  ┌──────────────────┐
        │   rag :14160     │  │SearXNG :8080 │  │ 前端 :5173        │
        │  (仅回环, 无鉴权) │  │  (原生 venv) │  │  /print/deck 路由 │
        └──────────────────┘  └──────────────┘  └──────────────────┘
```

---

## 3. backend 内部分层

```
┌───────────────────────────── backend (app.py 装配) ──────────────────────────────┐
│                                                                                  │
│  表现层  framework/api/                                                          │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ routes.py：/chat /chat/stream(SSE) /approval /task/{cancel,suspend,resume}  │ │
│  │ /history /branch /turns/last /memory /tools /tasks /schedules /ui-state     │ │
│  │ /files/{sid}/assets|deck     auth.py：Bearer 常量时间比对（空key=放行+告警）│ │
│  └───────┬────────────────────────────────────────────────────────────────────┘ │
│          │ session.lock（同会话整轮串行）                                        │
│          ▼                                                                       │
│  调度层  core/scheduler.py —— 全局单 worker（Apple Silicon 带宽约束→串行最优）    │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ TaskScheduler + ClassPolicy：交互(chat/approval/resume, p=0)                │ │
│  │   > 后台(scheduled/summarize, p=-1)；老化300s防饿死；                        │ │
│  │ 抢占原语：summarize→cancel（可弃）、scheduled→suspend（可恢复）              │ │
│  └───────┬────────────────────────────────────────────────────────────────────┘ │
│          ▼                                                                       │
│  编排层  core/orchestrator.py —— ReAct 循环，护栏全是确定性代码                    │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ 元工具 update_plan/complete_step（计划是数据，task_state.py 校验）           │ │
│  │ 护栏：步内预算/总迭代熔断/重规划上限/同参重复拦截(读写指纹+写屏障)/           │ │
│  │       解析失败断环/截断升档续写/提前收尾推回/失败路由菜单/审批闸门            │ │
│  │ 只读工具并发批执行；观察截断；tail_notes 一次性护栏提醒（不进历史）           │ │
│  └───────┬────────────────────────────────────────────────────────────────────┘ │
│          ▼                                                                       │
│  引擎层  core/engine.py —— 单次生成（持推理互斥锁）                                │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ PromptBuilder（前缀分层：稳定前缀→追加历史→可变尾部，KV 缓存友好）           │ │
│  │ patch/gemma4_generate.stream_generate（KV前缀复用/TurboQuant量化/           │ │
│  │   prompt-lookup 投机采样）  constrained.py：llguidance 工具段约束采样        │ │
│  │ VisibleTextStreamer（真流式可见正文过滤） ResponseParser（容错解析+纠错闭环）│ │
│  └───────┬────────────────────────────────────────────────────────────────────┘ │
│          ▼                                                                       │
│  记忆层  memory/                                                                 │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ session.py：冷热会话（30min 冻结释放 KV）/ history.jsonl 落盘 /              │ │
│  │   inflight 预写防丢 / 分支(KV克隆+硬链接媒体) / 删轮三方同步                  │ │
│  │ compactor.py：①机械压缩（取代去重+预算驱逐工具观察）                          │ │
│  │              ②摘要折叠（高阈值8k活跃期 / 低阈值2k空闲期 → memory.json）      │ │
│  │ rag_sink.py：被折叠原文沉淀 → RAG agent_memory 类目（无损承重网，fail-open）│ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  工具层  mcp/（BaseTool 插件自动扫描+探活注册）    agents/（物理隔离的子沙箱）      │
│  ┌──────────────────────────────────┐  ┌──────────────────────────────────────┐ │
│  │ run_shell_command（只读白名单放行 │  │ web_research：SearXNG搜索→分层抓取    │ │
│  │   /其余弹审批） edit/write_file   │  │  (静态→TLS指纹→无头渲染)→反投毒清洗    │ │
│  │ rag_search / rag_admin(删除需审批)│  │ math_research：SymPy 沙箱多步写码验证 │ │
│  │ accessibility(GUI) applescript   │  │ render_chart / deck_*（PPT 工具组）   │ │
│  │ schedule_*(需审批) file_extract 等│  │ ——主表只见包壳工具，内部循环复用主模型 │ │
│  └──────────────────────────────────┘  └──────────────────────────────────────┘ │
│                                                                                  │
│  后台回合：_idle_maintenance_loop（空闲摘要压缩）                                  │
│           schedule/runner.py（30s tick 扫描 → 无人值守执行，审批自动拒绝）         │
└──────────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. rag 内部流程

```
                        ┌─────────────── rag :14160 ───────────────┐
   入库 POST /insert    │                                          │
  (title,doc,category?) │  Qwen3-Embedding-0.6B (MPS, fp16)        │
  ──────────────────────┼─▶ ①有 category：校验存在否则 400 拒绝     │
                        │   ②无 category：文档特征向量 ↔ 分类描述   │
                        │     距离 < 0.8 → 入该类，否则未分类池      │
                        │  ③切块(token感知,256/重叠128) → 批量向量  │
                        │  ④原文备份 storage/raw_files/{类}/…      │
                        │  ⑤ChromaDB(余弦) 按类分 collection 入库  │
                        │                                          │
   查询 POST /query     │  ①category+optimized_query 合批取向量    │
  (query,category,      │  ②软路由：meta_classification_db 里挑    │
   optimized_query)     │    距离≤0.85 的前 K 库 + 强制带未分类库    │
  ──────────────────────┼─▶③多库线程池并发召回（每库4条）           │
                        │  ④Qwen3-Reranker-0.6B 全局重排(yes/no    │
                        │    logits)→ 过滤 score<0.5 → 取前3        │
                        │                                          │
   治理                 │  /category CRUD（meta 库路由条目）        │
                        │  /document/delete 按 source_path 跨库清  │
                        │  /governance/reclassify 未分类池重分拣    │
                        └──────────────────────────────────────────┘
   特殊类目 agent_memory：backend 上下文压缩折叠掉的对话原文沉淀于此，
   经 rag_search 可召回（Agent 的长期记忆）。
```

---

## 5. 一次对话轮的完整生命周期

```
 用户输入(文字+文件)
      │
      ▼
 前端 useChatStore.sendMessage ── multipart POST /api/v1/chat/{sid}/stream
      │                                    │
      │                          Ingestion 摄入分路（推理锁之外）：
      │                            图像→原生直喂 / 音频→Whisper ASR 转写
      │                            PDF→文本抽取或渲染成图 / 代码大文件→懒登记路径
      │                                    │
      │                          session.lock → 清理残留审批/挂起 → 追加用户消息
      │                          write_inflight 预写标记 → scheduler.acquire
      │                                    │
      │                       ┌── Orchestrator.run_turn（ReAct 循环）──┐
      │   SSE: token/thinking │  生成(engine) → 解析 → 元工具│物理工具  │
      │◀──tool_started/result─┤  审批命中 → pending_approval 挂起返回   │
      │      task_progress    │  取消/挂起检查点 → cancelled/suspended  │
      │                       │  纯文本+计划完成 → success              │
      │                       └────────────────┬───────────────────────┘
      │                                        │ scheduler.release/suspend
      │                          结果分流：成功/警告→append_log 落盘+save_task
      │                                 待审批→session.pending_turn（暂存内存）
      │                                 挂起→session.suspended_turn（暂存内存）
      │                                 取消/故障→history 整轮回滚，落盘存档
      │                                        │
      │                          落盘后若 prompt_tokens > 8000：
      │                            折叠旧轮→滚动摘要(memory.json)
      │                            + 原文沉淀 RAG agent_memory
      ▼
 SSE: turn_end（权威终态，前端撤下流式预览、按 response 对账）
      │
      ├─ pending_approval → 前端弹审批卡片 → POST /approval {approve}
      │                     批准→执行工具→回到 run_turn 续跑；拒绝→反馈模型改路
      └─ suspended → 前端「可恢复」横幅 → POST /task/resume 续跑
                     （或发新消息 = 放弃挂起任务）
```

---

## 6. 前端结构（frontend/src）

```
api/agent.ts      全部 REST/SSE 封装（同源 /api 相对路径，鉴权由代理注入）
api/sse.ts        fetch+ReadableStream 的 SSE 解析（EventSource 不支持 POST/头）
store/useChatStore.ts  会话/消息/审批/挂起/流式对账主状态机
                       （localStorage 本地缓存 + /ui-state 服务端跨设备同步）
store/useUiStore.ts    语言/主题 + /tasks(3s) /schedules(10s) 轮询对账
components/dashboard/  聊天区/输入/侧栏/检视面板(工具·任务·记忆)/定时任务条
components/deck/       SlideRenderer（Deck 唯一渲染器）+ /print/deck 打印路由
                       （后端 deck_export_pdf 用无头 Chrome 打印它 → 预览即所得）
```

---

## 7. 磁盘布局（数据流的落点）

```
MacAgent/                        （monorepo 根）
├── devctl.sh                    标准启动器：setup/start/stop/restart/status/logs；logs/*.log+pid
├── config.yaml                  全项目唯一配置（backend 与 rag 共读；段落归属见文件头注释）
├── .env(.local)                 密钥/环境变量（devctl source 后 export；Vite envDir 也指向这里）
├── backend/
│   ├── framework/utils/config_loader.py   配置统一寻址（CONFIG_PATH=仓库根）
│   ├── sessions/{sid}/          history.jsonl(全量日志) system.json meta.json
│   │                            task.json(跨轮计划) memory.json(滚动摘要+水位线)
│   │                            inflight.json(预写标记) image/ audio/ attach/
│   │                            assets/(图表PNG/PDF) deck/deck.json(PPT真相)
│   ├── schedules/{id}/          schedule.json(定义) runs.jsonl(运行历史)
│   ├── ui_state.json            前端工作区状态（服务端化，换设备恢复）
│   └── models/                  gemma-4-e4b-it-4bit / whisper-large-v3-turbo
├── rag/
│   ├── chroma_data/             ChromaDB 持久化（每分类一个 collection + meta 库）
│   ├── storage/raw_files/{类}/  入库文档原文备份；unclassified_raw/ 未分类池
│   └── models/                  Qwen3-Embedding-0.6B / Qwen3-Reranker-0.6B
├── frontend/                    React SPA（Vite；密钥经代理注入，bundle 不持有）
└── searxng/settings.yml         devctl 首次生成（开启 json 输出；源码/venv 由 setup 建在同目录）
```

---

## 8. 关键机制速查

- **单 worker 串行**：统一内存带宽约束下并行生成互相拖慢，全进程同时只有一个
  任务占推理 worker；交互任务优先，后台摘要/定时任务被抢占让位。
- **KV 缓存策略**：prompt 前缀分层（稳定前缀/追加历史/可变尾部）；分支克隆 KV、
  删轮/摘要折叠后靠前缀匹配自动裁剪对齐；冻结释放、恢复重放。
- **审批闸门**：敏感调用（写文件/删库/非只读 shell/定时任务管理）由后端挂起
  整轮，前端卡片决议；无人值守的定时运行一律自动拒绝。
- **有界记忆三层**：机械压缩（观察去重/驱逐）→ 摘要折叠（滚动摘要+水位线）→
  RAG 沉淀（原文可召回）。全量日志永远保留在 history.jsonl。
- **可靠性**：inflight 预写防输入蒸发；取消/故障整轮回滚；SSE 断线不丢轮
  （后台跑完照常落盘，前端重连后从 /history 对账）。
