# 架构修正与原生收编 · 执行账本（EXECUTION_LEDGER）

> **这是什么**：`ROADMAP.md` 方向的**可执行落地步骤 + 进度表**。自包含、可再入——
> 会话记录丢失或上下文压缩后，**只读本文即可接着干**。
>
> **与其他文档的关系**：
> - `CHARTER.md` = 约束（铁律）。**本文每一步开工前必须过它的自检清单**。
> - `ROADMAP.md` = 方向与理由（为什么是这个排序）。**本文不重复理由，只记步骤与状态**。
> - `COMPARE_CC_HAHA.md` = 诊断依据（§1–§4 有效）。
> - `HETERO_COMPUTE.md` = **算力线**账本（A/B 线，CUDA/算力分离/RAG 档位）。**本文是能力线 + 形态线**，两条线并行、互不阻塞。
> - `DEFERRED.md` = 缓做清单。本文的 R2b 挂在那里。
>
> **纪律**：§D 的决策**不再重议**；§S 的坑**不再踩第二次**；改架构方向须用户明确同意（`CHARTER.md:16`）。

---

## §R 再入协议（新会话/压缩后，从这里开始）

**读完本节 + §D + §2 进度表，就能接着干。不要通读全文。**

1. **先读 `CHARTER.md`**（约束），**再看代码**。
   ⚠️ 顺序反了会犯 §S.1 那个错——**代码只能证明存在，不能证明不存在**。
2. **校验现场**（账本可能滞后于现实；**现场状态以实测为准，架构事实以宪章/账本为准**——
   两者适用域不同，见 `HETERO_COMPUTE.md` §R）：
   ```bash
   cd /Volumes/enkin/project/Agent
   git log --oneline -5                    # 走到哪了
   ls backend/framework/core/dialect_protocol.py   # R0 是否已落地
   ls backend/framework/agents/sandbox_runtime.py  # R1 是否已落地
   ls backend/framework/core/skill_registry.py     # R2a 是否已落地
   ls protocol/fixtures/007-*                      # R3 是否已落地
   ls backend/framework/memory/notes/              # R4 是否已落地
   git ls-files apple-app | wc -l                  # R5.1 是否已入库（>0 = 已入库）
   ```
3. **对照 §2 进度表**认领下一个 ⬜ 项。**一次只开一项**（§D.4）。
4. **动智能层前**（`core/` `memory/` `mcp/` `agents/` `backends/`）：
   先读 `backend/tests/` 知道金测试守着哪些不变量（铁律 1 唯一例外条款）。
5. **每项完成**：更新 §2 进度表 → 更新 §3 对应阶段的「实测结果」→ commit + push
   （用户常规授权：每阶段完成均更新文档 + commit + push）。
6. **`git add` 用显式路径，不要 `git add -A`**——见 §S.2。

**当前位置（每次完成一项后更新这一行）**：
> 🟢 **2026-07-16：账本刚建立，R0 未开工。下一步 = R0-1。**

---

## §D 已定决策（不再重议）

| | 决策 | 定于 | 依据 |
|---|---|---|---|
| **D.1** | **执行层 + 前端进 macOS 原生 app**。app = `--profile edge` 的封装形态 | 2026-07-16 用户 | 用户 Q1 答复 |
| **D.2** | **与算力分离无张力**（我此前标的「张力」是误判，撤销）。edge profile 做的就是「执行层+前端在轻量设备」；设备 A 完全不变 | 2026-07-16 | 见 `ROADMAP.md` §5 Q1 的修正 |
| **D.3** | **iOS 保持瘦客户端**，经 Tailscale 连 macOS app 内的 backend。iOS 物理上无法承载执行层（不能 spawn 子进程 / 无 Python runtime） | 2026-07-16 用户 | 用户 Q2 答复 |
| **D.4** | **R 序列一次只开一项**。不因「AI 写得快」并行——验收需人在回路实测，实测是串行的 | 2026-07-16 用户 | `ROADMAP.md` 风险 1 |
| **D.5** | **R0 现在做**（不等硬件）。它是唯一「不先做就会让后面全部返工」的项 | 2026-07-16 用户 | `ROADMAP.md` §5 Q4 |
| **D.6** | **`apple-app/` 源码入库**，派生物继续忽略。**须改 `CHARTER.md:257` 与 `.gitignore:50`** | 2026-07-16 用户 | 用户 Q 答复；且这是「记录遗失也能续工」的前提 |
| **D.7** | **不全弃旧 app**。保留 `Store/`（归约器，夹具锁定的资产）+ `API/`；重做 `Views/`，新增 backend 宿主 | 2026-07-16 建议 | 铁律 1 精神：推倒重来只会得到更差的复制品 |
| **D.8** | **SearXNG 随 backend 进 app**（+138MB）。判据是我们自己 B3 定的「SearXNG 归 edge、与 backend 同机」 | 2026-07-16 | `HETERO_COMPUTE.md` §S.3 |
| **D.9** | **R2b（skill 模型自主发现）挂目标模型硬件**，记入 `DEFERRED.md`，非本阶段目标 | 2026-07-16 | `ROADMAP.md` R2 分期 |
| **D.10** | 规模化一分为二：**能力规模化追**（skill/tool/子agent），**协作规模化不追**（权限引擎/插件市场/CI/IM×5） | 2026-07-16 用户 | 用户补充 2 |

---

## §2 总进度表

图例：⬜ 未开工 ｜ 🟡 进行中 ｜ ✅ 完成 ｜ ⛔ 阻塞

| 阶段 | 名称 | 状态 | 前置 | 硬件门槛 | 铁律风险 |
|---|---|---|---|---|---|
| **R0** | 模型方言层协议手术 | ⬜ | — | 建设无／真验证需 ≥32GB | 铁律 1（例外条款） |
| **R1** | 子 Agent 统一执行器 | ⬜ | R0 | 无 | 铁律 1（沙箱三不变量） |
| **R2a** | Skill 体系（显式调用） | ⬜ | R1 | 无 | 低 |
| **R3** | 工具渲染格（协议变更） | ⬜ | — | 无 | **铁律 4 + 5** |
| **R4** | 文件式长期记忆 | ⬜ | R2a | 无 | 低 |
| **R5** | macOS app 收编执行层 | ⬜ | R0–R4 | 无 | 铁律 1/4 + 改 CHARTER |
| **D1–D4** | 能力量变扩充 | ⬜ | R5 | 无 | 低 |

**R2b**（skill 自主发现）→ `DEFERRED.md`，触发条件：R0 完成 + 目标模型（30–70B）硬件到位。

### 细分进度

<details>
<summary>展开细分（每完成一小项打勾）</summary>

**R0** ⬜
- [ ] R0-1 读金测试，列出护栏不变量清单
- [ ] R0-2 判定 `core/constrained.py` 归属（工具段约束语法是否属方言）
- [ ] R0-3 定义 `core/dialect_protocol.py`
- [ ] R0-4 收编 gemma4 → `dialects/gemma4/`
- [ ] R0-5 `model_registry` 加 `dialect` 字段；engine 按注册表选方言
- [ ] R0-6 验收：132 金测试全绿 + 实机 A/B 对拍行为零变化

**R1** ⬜
- [ ] R1-1 提取 `EphemeralSession` → `agents/sandbox_runtime.py`
- [ ] R1-2 提取 ReAct 循环 → `run_sandbox(spec)`
- [ ] R1-3 三条隔离不变量升级为显式契约 + 金测试断言
- [ ] R1-4 三沙箱改写为声明 + hook（保留 excel 状态机 / math 回填的差异）
- [ ] R1-5 验收：132 全绿 + 三沙箱 A/B 对拍一致

**R2a** ⬜
- [ ] R2a-1 `skills/` 目录 + frontmatter 规范
- [ ] R2a-2 `core/skill_registry.py`（照 `MCPRegistry` 形状，含 `is_available` 探活）
- [ ] R2a-3 `mcp/tools/skill_invoke.py`
- [ ] R2a-4 首个 skill 试点（建议：research 的六条规则从 Python 抽成 md）
- [ ] R2a-5 验收：金测试 + 实测

**R3** ⬜
- [ ] R3-1 **先落夹具** `protocol/fixtures/007-render-hint.*`（铁律 5）
- [ ] R3-2 `PROTOCOL.md` 加字段（新增字段**不升版本**，`CHARTER.md:148`）
- [ ] R3-3 `mcp/base.py` 加 `render_hint` + `render_payload`（可选，不声明走灰框 = 零破坏）
- [ ] R3-4 backend 发 hint 进账本
- [ ] R3-5 web reducer 投影 + 2–3 个试点（`chart` / `slides` / `citations`）
- [ ] R3-6 Swift reducer **接住字段**（最小实现保夹具绿；**渲染随 R5**，不在旧 app 上做）
- [ ] R3-7 验收：夹具双端回放全绿

**R4** ⬜
- [ ] R4-1 `memory/notes/` + `MEMORY.md` 索引（含行数/字节上限）
- [ ] R4-2 四类型分类学 + 「不该存什么」清单（借鉴 cc-haha `memoryTypes.ts` 的 **prompt 设计**，不抄代码）
- [ ] R4-3 RAG 新类目 `agent_notes`
- [ ] R4-4 笔记写入工具；召回走既有 `rag_search`
- [ ] R4-5 隐私：笔记**绝不进沙箱上下文**（沿用 `include_env=False` 惯例）

**R5** ⬜ —— 见 §3.5 详细步骤

**D 阶段** ⬜ —— 见 §3.6

</details>

---

## §3 各阶段详细步骤

### §3.0 R0 · 模型方言层协议手术

**目标**：给模型方言开一条接缝，使换目标模型（30–70B 稠密 / 100–300B MoE）不必动 engine 承重代码。

**病灶**（实测 2026-07-16）：
- `CHARTER.md:178` 自述「**仅有的 gemma 方言层**：`PromptBuilder` / `ResponseParser` / `patch/`」
- `prompt_builder.py:38-43` 频道式模板（`<|channel>thought` / `<|tool_call>`）为 gemma-4 结构
- `response_parser.py:20-24` 同一套标记的正则反序列化
- `config.yaml` `model_registry` **8 条全是 gemma-4 家族**（e4b / 26b-a4b MoE / 31b 稠密）
- 目标体量的开源模型（Qwen3 / DeepSeek / GLM / Kimi）用 **ChatML + `<tool_call>` JSON** 或 Harmony——**异构方言**

**先例**：`HETERO_COMPUTE.md` 的 P0「协议手术：`InferenceBackend` 协议 + MLX 收编 `backends/mlx/`」。
**R0 = 同一台手术，往上一层做一遍。**

**目标形态**：
```
core/dialect_protocol.py         # 新增：ModelDialect 协议
    build_prompt(messages, tools, ...) -> str
    parse_response(raw) -> {thoughts, text, tool_calls}
    tool_constraint_grammar()    -> llguidance 语法（若 R0-2 判定归方言）
    stop_tokens() -> list[str]

dialects/gemma4/                 # 收编现有 PromptBuilder + ResponseParser
dialects/chatml/                 # 将来：Qwen3 / DeepSeek / GLM
```
`model_registry` 每条加 `dialect: "gemma4"`；engine 按注册表选方言。

**范围红线（务必收窄）**：
- `backends/mlx/patch/gemma4_generate.py` **不动**。它是 `backend=mlx` × `dialect=gemma4` 的**交叉产物**，
  且已在 `backends/mlx/` 下（命名已表达归属）。R0 只收编 prompt/parse 两层。
  强行把它也抽象会让手术范围失控、验收失效。
- **这是搬运，不是重写**。任何「顺便优化一下」都会让「行为零变化」这个唯一可信的验收标准失效。

**步骤**：
1. **R0-1** 读 `backend/tests/`（132 项），列出护栏不变量清单。铁律 1 要求：动护栏前先读懂它护的是哪种失败形态。
2. **R0-2** 判定 `core/constrained.py`（161 行，llguidance 工具段约束采样）归属：
   若其语法硬绑 gemma 工具标记 → 归方言；若只是通用 JSON 约束 → 留 core。**先判定再动手**。
3. **R0-3** 定义 `core/dialect_protocol.py`（Protocol 类，照 `core/backend_protocol.py` 的形状）。
4. **R0-4** 收编：`PromptBuilder` / `ResponseParser` → `dialects/gemma4/`。纯搬运 + 实现协议。
5. **R0-5** `model_registry` 加 `dialect` 字段（8 条全填 `"gemma4"`）；engine 按注册表选。
6. **R0-6** 验收。

**验收标准**（写死，不许放宽）：
- `backend/tests/` **132 项全绿**
- **实机 A/B 对拍**：同一 prompt，手术前后生成**行为一致**
- ⚠️ **诚实记账**：金测试全绿只证明「gemma4 收编无回归」，**不证明「已支持大模型」**。
  账本里**不得**写成后者（`ROADMAP.md` 风险 2）。

**已知约束**：真·换模型验证需 ≥32GB 统一内存（30B@4bit ≈ 18.4GB + KV）。
开发机 16GB，设备 A 亦非目标算力（`HETERO_COMPUTE.md` 记「等待真正的 GPU 设备 A」）。
**R0 会长期停在「建设完成、零回归验证通过、未真换模型」状态——这是可接受的、也是设计目的。**

**实测结果**：（完成后填写）

---

### §3.1 R1 · 子 Agent 统一执行器

**病灶**（实测 2026-07-16）：
```
$ grep -c "class _EphemeralSession" backend/framework/agents/*.py
excel_sandbox.py:1   math_sandbox.py:1   research_sandbox.py:1     ← 同一个类抄了三遍
```
每个沙箱另各写一份 `for step in range(max_steps)` ReAct 循环、一份手工 `_TOOLS` 装配、一份收尾。
加第四个 = 再抄一遍。

**目标**：`agents/sandbox_runtime.py` —— 一份 `EphemeralSession` + 一份循环 + 一份收尾。
三沙箱退化为**声明**（system prompt + tools + max_steps + 收尾策略 + 可选状态机 hook）。

**🔴 红线（三条隔离不变量，必须从「注释里的论证」升级为「执行器的显式契约 + 金测试断言」）**
（原文见 `research_sandbox.py:6-15`）：
1. **工具集物理受限**——沙箱手工装配工具，主 `MCPRegistry` **永不扫描**它们。
   注入即便劫持沙箱也够不到任何能执行动作的工具。
2. **KV 隔离**——一次性 `EphemeralSession`（全新缓存），**不碰任何真实会话的 KV**。
3. **单 worker 不死锁**——走 `engine.generate_once` 复用推理锁；工具线程此刻已释放锁，故可安全取锁。

这三条是 `CHARTER.md:211`「物理隔离沙箱」的实现，**cc-haha 无等价物**（它不需要）。
**抽象化过程中它们绝不能蒸发。**

**留 hook，别硬压成一个模子**——三沙箱有真实差异：
- `excel_sandbox`：`INSPECT→PLAN→WRITE→VERIFY` 状态机（`:40`）+ 内部 QA 子会话（`:271`）
- `math_sandbox`：解释器输出回填
- `research_sandbox`：来源编号 + 双源交叉验证

**抽象的目标是消除拷贝，不是消除差异。**

**验收**：132 金测试全绿 + 三沙箱 A/B 对拍一致 + 三条隔离不变量的新增断言。

**实测结果**：（完成后填写）

---

### §3.2 R2a · Skill 体系（显式调用）

**病灶**：`grep -rni "skill" backend/framework --include='*.py'` → **零命中**。
而焊死在 Python 里的流程串（`research_sandbox.py:21` 六条调查规则、`excel_sandbox.py:40` 四段式）
**本质就是 skill**——不能复用、不能发现、改一次要动代码。

**skill 填的空档**：不值得做成工具（无确定性逻辑，本质是提示词），也不值得起子 agent（不需隔离），
但需要**被复用**与**被发现**。

**目标形态**（复用你已有的模式）：
```
skills/*.md                  # frontmatter: name / description / tools / max_steps / model
core/skill_registry.py       # 目录扫描加载，照 MCPRegistry 形状（含 is_available 探活）
mcp/tools/skill_invoke.py    # 一个工具，调用 skill
```
**跑在 R1 的统一执行器上——这是 R1 必须先做的原因。**

**分期**：
- **R2a（本阶段）**：**显式调用**（用户指定 / 编排层指定）。e4b 可用。
- **R2b（`DEFERRED.md`）**：**模型自主选择**。触发条件 = R0 完成 + 目标模型硬件到位。
  e4b 上 skill 数量一多大概率选不准——这是**缓做**，不是非目标。

**先别做**：skill 市场、MCP skill、skill 向量检索发现。先做能跑的最小闭环。

**实测结果**：（完成后填写）

---

### §3.3 R3 · 工具渲染格（协议变更）

**病灶**：`frontend/src/components/dashboard/chat-message.tsx:84` 对所有工具结果渲染同一个灰框 + 工具名。
`render_chart` 的图、`deck_tools` 的幻灯片、`rag_search` 的召回、`shell_exec` 的输出——**全长一个样**。
根因不在前端，在 `BaseTool` 契约**没有渲染这一格**，渲染逻辑无处安放。

**为什么不能抄 cc-haha**：它的工具直接 `return React.ReactNode`（`Tool.ts:566`），因为工具与 UI 同进程同语言。
本项目是 **Python backend + TS web + Swift 原生**——「工具返回组件」物理不可能，且违反铁律 4。

**方案：声明式 render_hint**——hint 是**账本里的数据**，三端各自投影。
**这恰好就是铁律 4 要求的形状**，不是妥协，是这个架构下的正解。

```python
# mcp/base.py 新增（可选；不声明则走现有灰框 = 零破坏增量迁移）
render_hint: str = None      # chart | table | slides | code | diff | citations | text
def render_payload(self, result) -> dict: ...
```

**⚠️ 铁律 5 强制顺序（不许颠倒）**：
1. **先落夹具** `protocol/fixtures/007-render-hint.*`
2. 更新 `PROTOCOL.md`（**新增字段不升版本**——客户端忽略未知即可，`CHARTER.md:148`）
3. 三端实现
4. 夹具双端回放全绿（web vitest + Swift `run_reducer_fixtures.sh`）

**Swift 侧的特殊安排**：app 要在 R5 重做，**在旧 app 上做渲染是浪费**。
故 R3-6 只做「**归约器接住字段**」的最小实现（保夹具双端绿），**渲染随 R5 落地**。
归约器逻辑被夹具锁定，R5 重写时可无损带走。

**试点**：先上 2–3 个收益最大的 hint（`chart` / `slides` / `citations`），其余留灰框。

**实测结果**：（完成后填写）

---

### §3.4 R4 · 文件式长期记忆

**先纠正一个自评**：会话记忆**不粗糙**。`compactor.py` 的取代去重 + **写屏障**（`:87`）+ 预算驱逐
实现精细，写屏障甚至是 cc-haha **没有**的正确性保证。缺的不是质量，是**一个层**：

| | 会话记忆（已有） | 文件记忆（缺） |
|---|---|---|
| 尺度 | session 内 | 跨 session |
| 作者 | 系统自动（摘要器） | **Agent 主动撰写** |
| 内容 | 发生过什么 | **学到了什么** |
| 性质 | 被动/无损/全量 = **录像带** | 主动/提炼/有损 = **笔记本** |

`rag_sink.sediment` 是录像带。**一柜子录像带，没有笔记本。**

**方案**：`memory/notes/` + `MEMORY.md` 索引 + 单事实 md（frontmatter: name/description/type）；
召回走 **RAG**（新类目 `agent_notes`）。

**借鉴（设计，不是代码）**：cc-haha `src/memdir/memoryTypes.ts` 的四类型分类学
（`user` / `feedback` / `project` / `reference`）+「不该存什么」清单（能从代码/git/CLAUDE.md 推导的一律不存）。
**那份 prompt 写得很好，值得逐字读。**

**本项目的地基更好**：cc-haha 只能靠 Sonnet 扫全部记忆 header 做语义选择（额外 API 调用，
`findRelevantMemories.ts`）。**本项目有真向量库**，`rag_search` 直接召回——更快更便宜。
这是 RAG 线（B2a 已交付的档位化 RAG）的免费红利。

**🔴 隐私红线**：笔记走本地嵌入不出门；**绝不进沙箱上下文**（沿用 `agent.environment` 的 `include_env=False` 惯例）。

**实测结果**：（完成后填写）

---

### §3.5 R5 · macOS app 收编执行层（最大的一块）

**目标**：macOS app = **执行层 + 前端**（即 `--profile edge` 的封装形态）。设备 A 完全不变（§D.2）。

**形态**：
```
macOS app (.app bundle)                     设备 A（不变）
├── SwiftUI 前端（瘦渲染器，铁律 4）          ├── inference-server :14161
├── SidecarManager → backend :14159 (回环)   └── rag :14160
└── SidecarManager → searxng :8080 (回环)         ↑
         └────────── RemoteBackend / RAG 拨号 ────┘

iOS app（瘦客户端，§D.3）── Tailscale ──→ macOS app 的 backend
```

**实测前提（2026-07-16）**：
- app 真实源码 **37 文件 / 6,373 行**（`Sources/` 5,909 + `Tests/` + `Widgets/`）。
  ⚠️ 别被 `find -name '*.swift' | wc -l` 的 **226 / 19,039** 骗了——那含 `build/` 派生物。
- `backend/.venv` = **2.1GB**（torch 385M + mlx 178M + playwright 133M + pyarrow 126M + llvmlite 125M + cv2 119M…）
- `searxng/` = **138MB**（venv 109M + src 30M）

**步骤**：

- [ ] **R5-1 · app 源码入库**（§D.6）
  - 改 `.gitignore:50`：`apple-app/` → 只忽略 `apple-app/build/`、`apple-app/Generated/`、`apple-app/*.xcodeproj/`
  - 改 `CHARTER.md:257`（「`apple-app/` 全程 gitignored」）——**这是宪章改动，已获用户同意（§D.6）**
  - 入库 `Sources/` `Tests/` `Widgets/` `project.yml` `install-macos.sh` 等
  - ⚠️ 入库前先 `git status` 确认没有密钥/证书/描述文件（`ExportOptions.plist`、`asc-update-signing.py` 需检查）

- [ ] **R5-2 · edge 依赖瘦身**（硬前提，2.1GB 直接打包不现实）
  - 判定 edge 形态（`inference.mode: remote`）真正需要什么：
    - **可能可去**：`mlx` / `mlx-lm` / `mlx-vlm`（推理在远端）、`mlx-audio`
    - **不能去**：`transformers`（backend 仍拉分词器，README:66）、`playwright`（T2 抓取档在 backend）、
      `trafilatura`/`selectolax`（正文抽取）、ingestion 全家（PyMuPDF/markitdown）
    - **待判**：`mlx-whisper`（音频 ASR 摄入**在 backend 跑**）、`torch`/`torchaudio`（被谁拖进来）
  - 产出 `backend/requirements-edge.txt`；目标 **< 1GB**
  - ⚠️ 这是**真实的裁剪工程**，不是顺手能做的。**去一个包就跑一遍 132 金测试。**

- [ ] **R5-3 · Python runtime 打包选型**
  - 候选：`python-build-standalone` 嵌入 / PyInstaller / 完整 venv 拷进 bundle
  - 判据：签名与公证（notarization）能否过、启动时延、体积
  - **参考**：cc-haha `desktop/electron/services/sidecarManager.ts` + `desktop/scripts/build-sidecars.ts`
    （它的 sidecar 是 TS 不是 Python，但**进程生命周期管理的形状**可借鉴）

- [ ] **R5-4 · `SidecarManager.swift`**
  - 职责：启停 backend + searxng、就绪探活、端口冲突处理、崩溃重启、日志落盘
  - **🔴 顺序**：searxng **必须先于** backend 就绪——`web_research` 的注册是**启动期一次性探活**，
    searx 晚到 = 整轮不注册（`HETERO_COMPUTE.md` §S.3 已入账的事故）
  - ⚠️ **反模式风险（见 §5 Q5，未决）**：`devctl.sh` 已有一份 edge 启动逻辑。
    在 app 里再写一份 = `CHARTER.md:225` 反模式 3「第二个编排器」的变体。**开工前先定 Q5。**

- [ ] **R5-5 · SearXNG 随包**（§D.8，+138MB）
  - `settings.yml` 由 app 首启生成（照 `devctl.sh` 的 `ensure_searx_settings`）
  - **🔴 恒为回环**：limiter 已关且**无任何鉴权**，离开回环 = 人人可用的搜索代理（`searxng/settings.yml:9`）

- [ ] **R5-6 · 密钥形态改造**
  - 现状：`.env.local` 的三个 KEY，由 `devctl.sh` source 后 export
  - app 内：`.env.local` 不再适用 → **Keychain** + 首启生成
  - `AGENT_API_KEY` 在 app 内是**同进程回环**，可退化；`INFER_API_KEY` / `RAG_API_KEY` 要拨设备 A，**必须留**
  - **🔴 护栏保持**：非回环绑定 + 空密钥 = **拒绝启动**（硬失败，不是告警）

- [ ] **R5-7 · Views 重做 + R3 的渲染格 Swift 侧**
  - 保留 `Store/`（归约器，夹具锁定）+ `API/`（§D.7）
  - 重做 `Views/`（2,255 行）
  - 落地 R3 的 `render_hint` 渲染（R3-6 只接了字段）

- [ ] **R5-8 · iOS 瘦客户端**（§D.3）
  - 角色不变；指向 macOS app 内的 backend（经 Tailscale）
  - 保留 `release-ios.sh` / TestFlight 通路

- [ ] **R5-9 · 验收**
  - 冷启动：app 双击 → searx 就绪 → backend 就绪 → 界面可用（记录时延）
  - **原生 E2E 20 项全绿**（`Tests/run_e2e.sh`）
  - **夹具双端回放全绿**（web vitest + Swift）
  - **杀进程恢复 ≤1s**（铁律 4 的验收标准，`REARCHITECTURE.md` Phase 2）
  - sidecar 崩溃后自动重启且不丢轮次（轮次是账本资源，铁律 2）

**CHARTER 需同步改动**（R5 完成时一并更新，**属架构方向改动**）：
| 位置 | 现状 | 改为 |
|---|---|---|
| `:257` | `apple-app/` 全程 gitignored | 源码入库，派生物忽略（§D.6） |
| `:254` | 「**不要**在开发机上替用户启动后端（Xcode 资源紧张，用户特意分开）」 | app 按设计自启 backend——此纪律的适用域需重述 |
| `:115` 铁律 4 | 「客户端是同一协议的瘦渲染器」 | 仍成立（macOS app 的**渲染部分**仍是纯归约器投影，只是同时充当 backend 宿主）——**建议补一句说明，而非改铁律** |

**实测结果**：（完成后填写）

---

### §3.6 D 阶段 · 能力量变扩充

**前提：R0–R5 完成**（用户原话：「先把其他架构修正好之后，再来逐渐扩充」）。
这一段是**填充体系**，不是**建立体系**。

| | 内容 | 纪律 |
|---|---|---|
| **D1** | Skill 库扩充 | R2a 建好体系后 skill 是 md，AI 批量写。**每个需实测验收** |
| **D2** | Tool 扩充（现 17 文件 / 34 类 → 对标 cc-haha 60+） | **只加桌面级任务真需要的** |
| **D3** | 子 Agent 种类扩充 | R1 后新 agent = 一份声明 |
| **D4** | 切 `tool_schema_mode: catalog` | `config.yaml` 已预留。**D2 开工前先定「多少工具时切」的阈值**，别等出事 |

**扩充纪律（硬性）**：
- 每个新 skill/tool **必须有金测试**，否则 132 项的回归网被稀释成摆设
- 每个新 tool **必须声明 `is_available` 探活**，否则重演 SearXNG 那次「优雅不注册」事故（§S.3 已入账）
- **宁可 20 个实测过的工具，不要 60 个没验过的**。cc-haha 的 60 个是 123 个测试文件撑着的

---

## §4 风险（已识别，持续有效）

| | 风险 | 缓解 |
|---|---|---|
| **1** | 「AI 写代码所以工作量不是问题」——**在新增上成立，在承重代码上不成立**。护栏参数「全是实测调的」（`CHARTER.md:42`），瓶颈是实测不是打字 | §D.4 一次只开一项；每项验收要人在回路 |
| **2** | **R0 验证悬空**：16GB 开发机跑不动目标模型，设备 A 亦非目标算力 | 接受。验收只声明「零回归」，**不得**声明「已支持大模型」 |
| **3** | **R5-4 的启动逻辑重复** = 反模式 3「第二个编排器」的变体 | **§5 Q5 未决，R5-4 开工前必须先定** |
| **4** | R5-2 依赖瘦身可能发现「edge 去不掉 mlx」（若 ingestion 的 whisper 强依赖） | 去一个包跑一遍金测试；去不掉就接受体积 |

---

## §5 待决（开工前需拍板）

| | 问题 | 阻塞谁 |
|---|---|---|
| **Q5** | **app 的 sidecar 启动逻辑 vs `devctl.sh`**：app 内重写一份 = 两份 edge 启动逻辑必然漂移（反模式 3 变体）。选项：(a) app 调用打包进 bundle 的 devctl；(b) app 用 Swift 重写、把「就绪探活/端口/密钥注入」的不变量提成夹具锁住；(c) 接受漂移（不推荐） | **R5-4** |
| **Q6** | R5-2 若判定 `mlx-whisper` 无法从 edge 去除，音频摄入是否改为「送设备 A 转写」？ | R5-2（届时再议） |

---

## §S 踩坑（不再踩第二次）

### S.1 · 「工作区看不见 ≠ 不存在」（2026-07-16）

**事故**：我据「本仓无 `.swift` 文件」推断前端只有 web 一端，写进 `COMPARE_CC_HAHA.md` §1.3/§2.7，
被用户当场指正。实际 `apple-app/` 是**刻意 gitignored**（`CHARTER.md:257`），
而三端架构在 `CHARTER.md` 铁律 4、`REARCHITECTURE.md` Phase 2、`PROTOCOL.md`、`protocol/fixtures/`
**四处有据**，且原生 app 早在 2026-07-11 就交付了（v0.10.0，已装 `/Applications`）。

**教训**：`CHARTER.md:4` 写着「每次阶段性迭代开工前通读一遍」。**我先读代码后读宪章，顺序反了。**
凡「本项目是什么形态」这类问题，**账本与宪章优先于代码扫描**——
**代码只能证明存在，不能证明不存在。**

**与 `HETERO_COMPUTE.md` §R「verify site over ledger」不矛盾**：那条讲**现场状态**
（服务是否在跑、配置是否生效）以实测为准；**架构事实**（有几个端、什么形态、什么纪律）以宪章账本为准。
**两者适用域不同。**

### S.2 · `git add -A` 会扫进不属于本次改动的东西（2026-07-15）

**事故**：`git add -A` 把用户未提交的 `.env.example`（`INFER_API_KEY="test"`）扫进了提交 `9e27465`。
用户当时判为「安全工作区，问题不大」不必回退，但习惯必须改。

**教训**：**永远用显式路径**（`git add ROADMAP.md COMPARE_CC_HAHA.md`）。
在 `apple-app/` 入库后（§D.6）这条尤其要紧——那个目录有签名/证书相关文件
（`ExportOptions.plist`、`asc-update-signing.py`），扫错了就是密钥泄露。

### S.3 · 「优雅降级」会把配置错误伪装成模型故障（2026-07-15，原载 `HETERO_COMPUTE.md`）

`web_research` 的注册是**启动期一次性探活**——searx 晚到/不可达 = 整个进程生命周期不注册。
而系统提示词仍宣告该工具存在 → 模型以为有 → 调不到 → 自由发挥 → 用户看到的是「工具调用解析错误」。
**降级的告警必须跟着能力宣告走。** 本文 R5-4 的 searx 启动顺序、D 阶段的 `is_available` 纪律皆源于此。

---

## §6 文档漂移待校正（顺手修，非阻塞）

| 位置 | 记载 | 实测（2026-07-16） |
|---|---|---|
| `CHARTER.md:167` | `mcp/（20 工具）+ agents/（4 沙箱）` | **17 文件 / 34 类 + 3 沙箱** |
| `CHARTER.md:239` | 122 项金测试 | **132 项** |
| `ARCHITECTURE.md` | 开篇「四个组件」，缺 inference-server；`:462` 图仍标 `SearXNG（跟随部署，可选）` | 五组件；SearXNG 已归 edge（B3） |
| `MacAgent.docx` 图1 | 单机分层，无算力分离 / inference-server | 待重画（用户未答复过） |

---

*建立：2026-07-16 ｜ 依据：`ROADMAP.md` + 用户 Q1/Q2 答复 + `CHARTER.md` / `REARCHITECTURE.md` 实读 + 两侧代码实测。*
