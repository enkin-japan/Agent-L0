# 项目运行架构逻辑示意图

> 描述本仓库（monorepo）五个组件——backend（Agent 主体）、inference-server（推理
> 服务化，算力分离用）、rag（RAG 数据库：数据面 + 计算面，B2c）、frontend（交互前端）
> 与可选的 SearXNG——的运行架构与关键数据流。
> 配置统一在仓库根 config.yaml，启动统一走根 devctl.sh。
>
> **交互层已重整为事件账本架构**（2026-07 完成）：轮次是可寻址、可重放的持久资源，
> 编排"先记账后分发"，客户端是账本事件的瘦渲染器（web + 原生 app 共享
> `protocol/fixtures/` 行为夹具）。**架构原则与开发铁律见 `CHARTER.md`**；
> 协议冻结见 `PROTOCOL.md`，蓝图与踩坑见
> `REARCHITECTURE.md`。核心交互面：`POST /turns`(202 受理即返) +
> `GET /chat/{sid}/events`(附着流,断线任意重连重放) + `GET /turn`(可恢复状态) +
> `GET /events`(全局任务 feed)。旧 SSE/阻塞端点已退役（`POST /chat` 阻塞版留作
> App Intents 便捷入口）。

---

## 1. 服务总览

| 组件 | 端口 | 绑定 | 技术栈 | 角色 |
|---|---|---|---|---|
| frontend | 5173 | 0.0.0.0 | React 19 + Vite + Zustand | 用户交互界面（唯一对外入口） |
| backend | 14159 | 127.0.0.1 | FastAPI + MLX (gemma-4-e4b 8bit) | Agent 主体：推理/编排/工具/会话 |
| inference-server | 14161 | 按 `inference.remote.host` | FastAPI + MLX/CUDA | 推理服务化（`mode: remote` 时算力端承载） |
| rag 数据面 | 14160 | 127.0.0.1 | FastAPI + ChromaDB | 分类路由向量库：数据/路由/治理（用户数据只落这里） |
| rag 计算面 | 14162 | 按 `rag.compute.host` | FastAPI + PyTorch(MPS/CUDA) | 嵌入/重排/分词（无状态；`compute.host` 空=融合进数据面） |
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
          rag_search / │   web_research│             │ PDF 导出：无头 Chrome 打印
          rag_admin /  │   (受限沙箱)  │             │ /print/deck（前端在跑打前端,
          记忆沉淀 HTTP │              │             │  否则打后端自托管包;渲染器同一份）
                       ▼              ▼             ▼
        ┌──────────────────┐  ┌──────────────┐  ┌──────────────────────────┐
        │ rag 数据面 :14160 │  │SearXNG :8080 │  │ 前端 :5173 /print/deck    │
        │ (回环, Bearer；   │  │  (原生 venv) │  │ 或 backend/static/webapp  │
        │  数据/路由/治理)  │  └──────────────┘  └──────────────────────────┘
        └────────┬─────────┘
                 │ /embed /rerank /tokenize（Bearer，Tailscale）
                 ▼
        ┌──────────────────────┐
        │ rag 计算面 :14162     │ ← 算力端；无状态（B2c 拆分。融合模式=并入数据面进程）
        │ (嵌入+重排, MPS/CUDA) │
        └──────────────────────┘
```

### 2.1 密钥拓扑与安装形态（AGENT_API_KEY 的家在哪）

四把密钥，各守一条边（值全部不入仓、不入对话，比对一律走指纹）：

| 密钥 | 守哪条边 | 校验方 |
|---|---|---|
| `AGENT_API_KEY` | 客户端（app/web 代理）→ backend :14159 | backend `verify_token()` |
| `INFER_API_KEY` | backend → inference-server :14161（算力端） | inference-server |
| `RAG_API_KEY` | backend → rag 数据面 :14160 | rag 数据面 |
| `RAG_COMPUTE_API_KEY` | rag 数据面 → rag 计算面 :14162（算力端） | rag 计算面 |

**同一个 /Applications/MacAgent.app 有两种安装形态，密钥的家不同**——这是全系统
最隐蔽的故障面（2026-08-10 全天 401 事故根因，账本 S.12）：

- **bundle 布局**（`bundle-macos.sh` 全栈包）：判据为包内 `Resources/backend/app.py`
  存在。**Keychain 是密钥的家**：`AGENT_API_KEY` 首启随机生成落 Keychain，
  `INFER/RAG_API_KEY` 用户在设置页填（同样落 Keychain）；SidecarManager spawn
  执行层时以 env 注入，app 客户端发同一份 Keychain 值——**天然同源**。
- **repo 布局**（开发形态，设置页填仓库路径）：dev 桥接平面解析仓库 `.env(.local)`
  注入后端（与 devctl 同源）；但 **app 客户端发的仍是 Keychain 值**——两源异物，
  必须人工保持同值，漂移即构造性 401。
- **形态切换是静默的**：装了裸包（`install-macos.sh` 纯客户端快装）会让 bundle
  判据失败、静默退 repo 布局=密钥换源。防线三层：打包侧完整性断言（缺执行层
  四件套即组装失败）、`/health` 实例指纹、设置页布局形态显性化（见下）。

**实例身份探针（#85）**：`/health` 免鉴权返回 `service`（身份标识）、`layout`
（bundle/repo，来自 `AGENT_LAYOUT` env）、`key_fp`（`AGENT_API_KEY` 的 sha256
前 8 位指纹，明文永不出现）。app 探活收养（external 态）前比对指纹：不认端口
认身份——端口被他人占用 / 密钥不匹配 / 健康三态分明，旧钥后端不再被静默收养。

**iOS**：瘦客户端，只持 `AGENT_API_KEY`（Keychain），经 tailscale serve →
Mac 127.0.0.1:14159 直拨；不持有 INFER/RAG 钥（那两把是 Mac 执行层的拨出边）。

---

## 3. backend 内部分层

```
┌───────────────────────────── backend (app.py 装配) ──────────────────────────────┐
│                                                                                  │
│  表现层  framework/api/                                                          │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ routes.py：/turns(202) /turns/{approval,resume} /turn /events(附着流)      │ │
│  │ /history /branch /turns/last /meta /memory /tools /tasks /events(全局feed) │ │
│  │ /schedules /files/{sid}/assets|deck /push/*（iOS 推送注册面,PROTOCOL §2.8）│ │
│  │ auth：Bearer 常量时间比对（空key=告警放行）                                  │ │
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
│  引擎层  core/engine.py —— 单次生成（持推理互斥锁，经 InferenceBackend 协议驱动） │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │ PromptBuilder（前缀分层：稳定前缀→追加历史→可变尾部，KV 缓存友好）           │ │
│  │ backends/mlx/（MLXBackend + patch/gemma4_generate：KV前缀复用/TurboQuant    │ │
│  │   量化/prompt-lookup 投机采样）  constrained.py：llguidance 工具段约束采样   │ │
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

> B2c 拆分：下图流程全在**数据面**（:14160）编排；标 ◆ 的模型计算（嵌入/重排/token
> 计数）经计算面执行——融合模式=同进程内调（compute_core），拆分模式=HTTP 拨算力端
> :14162（无状态，Bearer）。数据（ChromaDB + storage 原文）只落数据面一侧。

```
                        ┌────────── rag 数据面 :14160 ──────────────┐
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

事件账本架构（"先记账后分发"）：轮次是可寻址资源，提交与观察解耦——
提交端 202 受理即返，过程与终态经附着流观察，断线不改变轮次状态。

```
 用户输入(文字+文件)
      │
      ▼
 客户端 submitTurn ── multipart POST /api/v1/chat/{sid}/turns ──▶ 202 {turn_id, seq}
      │                                    │                          (受理即返,不长连)
      │                          TurnRunner.accept_turn（短临界区,持 session.lock）：
      │                            Ingestion 摄入分路（图像原生/音频 ASR/PDF 解析…）
      │                            放弃暂停态旧轮 → 追加用户消息 → write_inflight
      │                            → 账本 turn_queued（→ 全局 feed 镜像）
      │                                    │
      │  ┌─ 客户端 attachEvents ── GET /api/v1/chat/{sid}/events?after=seq (SSE) ─┐
      │  │  重放 seq 之后的账本事件 → 转实时续播；断线带游标任意重连          │
      │  ▼                                                                       │
 TurnRunner.run_accepted（背景任务,持锁全程）：scheduler.acquire → turn_started
      │                       ┌── Orchestrator.run_turn（ReAct 循环）──────────┐
      │  账本(唯一写入者)：    │  engine.generate（专用推理线程,事件循环自由）  │
      │  iteration_started /   │   → VisibleTextStreamer 增量过滤：              │
      │  token_delta /         │      · 可见正文 → token_delta                  │
      │  thinking_delta /      │      · thought 频道 → thinking_delta（逐段）    │
      │  tool_call_pending /   │      · 工具块解析出 name → tool_call_pending    │
      │  tool_started/result / │   → 解析 → 元工具│物理工具                     │
      │  plan_updated /        │  审批命中 → awaiting_approval（暂停,worker 让出）│
      │  step_completed…       │  取消/挂起检查点 → cancelled / suspended       │
      │                        │  纯文本+计划完成 → success                     │
      │                        └────────────────┬──────────────────────────────┘
      │                          终态分流（_settle,持锁）：
      │                            成功/警告/取消/故障 → append_log 落盘 + save_task
      │                                            → 账本 turn_ended（→ feed）
      │                            待审批 → pending_turn 暂存 + awaiting_approval
      │                            挂起   → suspended_turn 暂存 + suspended
      │                            落盘后 prompt_tokens>8000 → 折叠摘要 + RAG 沉淀
      ▼
 账本事件流(经 events 附着) —— 客户端 TurnReducer 归约为规范态投影 UI：
      token/thinking 逐段预览；turn_ended 权威终态撤下预览、按 response 对账
      │
      ├─ awaiting_approval → 审批卡（刷新可经 /turn 重建）→ POST /turns/approval {approve}
      │                       批准→执行工具→续跑；拒绝→反馈模型改路（均走事件流）
      └─ suspended → 「可恢复」横幅 → POST /turns/resume 续跑
                     （或发新消息 = 放弃挂起任务）

恢复（刷新/杀进程/切后台/断线）统一一条路：GET /turn 拿可恢复状态（含审批卡/
进行中预览）→ GET /events?after 续播。崩溃防丢仍靠 inflight.json（write-ahead）。
```

---

## 6. 前端结构（frontend/src）

```
api/agent.ts      全部 REST/SSE 封装（同源 /api 相对路径，鉴权由代理注入）
                  含账本协议 v1：submitTurn(202)/submitApproval/submitResume/
                  getTurnState/attachEvents（重放+续播的事件附着流）
api/sse.ts        fetch+ReadableStream 的 SSE 解析（EventSource 不支持 POST/头）
lib/turn-reducer.ts    事件归约器（PROTOCOL.md §5 的纯函数实现）——
                       行为由 protocol/fixtures/ 夹具锁定（npm run test 回放）
store/useChatStore.ts  会话/消息主状态机：附着流驱动（单附着=活跃会话一条
                       /events 长连；断线静默退避重连；刷新/切回经 /history +
                       /turn + 事件重放恢复,审批卡/直播预览可完整重建）
                       + outbox 发送暂存（会话元数据 localStorage + /sessions 对账）
store/useUiStore.ts    语言/主题 + /tasks(3s) /schedules(10s) 轮询对账
components/dashboard/  聊天区/输入/侧栏/检视面板(工具·任务·记忆)/定时任务条
components/deck/       SlideRenderer（Deck 唯一渲染器）+ /print/deck 打印路由
                       （后端 deck_export_pdf 用无头 Chrome 打印它 → 预览即所得）
```

---

## 7. 磁盘布局（数据流的落点）

```
Agent/                           （monorepo 根）
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
│   ├── static/webapp/           deck 打印页自托管包（devctl bundle-print 生成，gitignored）
│   └── models/                  gemma-4-e4b-it-8bit / whisper-large-v3-turbo
├── rag/
│   ├── chroma_data/             ChromaDB 持久化（每分类一个 collection + meta 库）
│   ├── storage/raw_files/{类}/  入库文档原文备份；unclassified_raw/ 未分类池
│   └── models/                  Qwen3-Embedding-0.6B / Qwen3-Reranker-0.6B
├── frontend/                    React SPA（Vite；密钥经代理注入，bundle 不持有）
└── searxng/settings.yml         devctl 首次生成（开启 json 输出；源码/venv 由 setup 建在同目录）
```

---

## 8. 关键机制速查

- **单引擎批内并发**（2026-08-23 进宪，修订自「单 worker 串行」）：推理引擎
  全进程单实例，多实例并跑永远禁止；并发唯一合法形态是引擎内连续批处理
  （一次权重读取服务批内全部流）。交互轮次恒享最高优先级，后台生成经调度器
  准入闸进批、超水位让位排队。生产缺省仍单流（`max_concurrency: 1`，准入闸
  未建前不放开）。
- **KV 缓存策略**：prompt 前缀分层（稳定前缀/追加历史/可变尾部）；分支克隆 KV、
  删轮/摘要折叠后靠前缀匹配自动裁剪对齐；冻结释放、恢复重放。
- **审批闸门**：敏感调用（写文件/删库/非只读 shell/定时任务管理）由后端挂起
  整轮，前端卡片决议；无人值守的定时运行一律自动拒绝。
- **有界记忆三层**：机械压缩（观察去重/驱逐）→ 摘要折叠（滚动摘要+水位线）→
  RAG 沉淀（原文可召回）。全量日志永远保留在 history.jsonl。
- **可靠性**：inflight 预写防输入蒸发；取消/故障整轮回滚；SSE 断线不丢轮
  （后台跑完照常落盘，前端重连后从 /history 对账）。
