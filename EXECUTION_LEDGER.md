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
> 🟢 **2026-07-16：R0 ✅；R1-0（建回归网）✅；R1-1（统一 EphemeralSession）✅，156 金测试全绿。下一步 = R1-2（提取 ReAct 循环 → `run_sandbox(spec)`）。**

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
| **R0** | 模型方言层协议手术 | ✅ **2026-07-16** | — | 建设无／真验证需 ≥32GB | 铁律 1（例外条款） |
| **R1** | 子 Agent 统一执行器 | 🟡 **R1-0 ✅ / R1-1 ✅** | R0 ✅ | 无 | 铁律 1（沙箱三不变量） |
| **R2a** | Skill 体系（显式调用） | ⬜ | R1 | 无 | 低 |
| **R3** | 工具渲染格（协议变更） | ⬜ | — | 无 | **铁律 4 + 5** |
| **R4** | 文件式长期记忆 | ⬜ | R2a | 无 | 低 |
| **R5** | macOS app 收编执行层 | ⬜ | R0–R4 | 无 | 铁律 1/4 + 改 CHARTER |
| **D1–D4** | 能力量变扩充 | ⬜ | R5 | 无 | 低 |

**R2b**（skill 自主发现）→ `DEFERRED.md`，触发条件：R0 完成 + 目标模型（30–70B）硬件到位。

### 细分进度

<details>
<summary>展开细分（每完成一小项打勾）</summary>

**R0** ✅ 2026-07-16
- [x] R0-1 读金测试，列出护栏不变量清单 → 见 §3.0「护栏不变量清单」
- [x] R0-2 判定 `core/constrained.py` 归属 → **机制留 core，schema 归方言**（见 §3.0）
- [x] R0-3 定义 `core/dialect_protocol.py`（`ModelDialect` + `DialectMarkers`）
- [x] R0-4 收编 gemma4 → `dialects/gemma4/`（git mv 保历史；内部逻辑一行未改）
- [x] R0-5 `model_registry` 9 条加 `dialect: "gemma4"`；engine 按注册表选方言
- [x] R0-6 验收：**142 全绿**（132 基线 + 10 新方言金测试）+ 逐项 A/B 对拍一致

**R1** 🟡 进行中
- [x] **R1-0 建回归网**（**计划外，但必须先做**：审计发现沙箱零测试覆盖，见下）
      → `backend/tests/test_sandbox_invariants.py` 9 项，**151 全绿**
- [x] **R1-1 提取 `EphemeralSession`** → `agents/sandbox_runtime.py`（**实为 5 份拷贝，非 3 份**）
      → 净删 53 行，**156 全绿**（151 一字未改 + 5 项新契约）
- [ ] R1-2 提取 ReAct 循环 → `run_sandbox(spec)`（骨架已实证同构，见下）
- [ ] R1-3 三条隔离不变量升级为显式契约 + 金测试断言（R1-0 已完成契约部分）
- [ ] R1-4 三沙箱改写为声明 + hook（保留 excel 状态机 / math 回填的差异）
- [ ] R1-5 验收：**156 全绿**（含 R1-0 那 9 项一字不变）+ 三沙箱 A/B 对拍一致

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

---

#### ✅ 实测结果（2026-07-16 完成）

**R0-1 · 护栏不变量清单**（方言层直接相关的 17 项，`test_response_parser.py` 12 + `test_prompt_env.py` 5）：

| 不变量 | 出处 |
|---|---|
| **标记残缺的任何事故形态都不得把垃圾漏进正文**（贯穿线，见文件首行 docstring） | `test_response_parser.py:1` |
| 有头无尾的工具调用 → error 调用走纠错闭环（**非**漏成正文） | `:32` |
| 有尾无头的残片 → error（实测事故：思考预算强插 `<channel\|>` 落在调用中间） | `:41` |
| 未闭合思考频道 → 整段路由进 thoughts（实测事故：会话 e2348829，独白直呈用户） | `:86` |
| error 的 `raw_input` 必须截断 <2000（它会被回显给模型并进账本，失控会自体膨胀） | `:68` |
| 未闭合思考块内的完整调用照常执行（调用不因频道残缺而丢） | `:119` |
| 游离标记从正文剥除 | `:59` |
| **隐私门控**：个人环境事实只进主编排轮，`include_env=False` 时不注入（沙箱边界） | `test_prompt_env.py:42` |
| 日期锚点两种情况都注入 | `test_prompt_env.py:49` |

**R0-2 · `constrained.py` 归属判定 → 机制留 core，schema 归方言**：
- 该模块首行自陈「框架无关（P0 收编）」；其机制（**起止标记夹一段 JSON**，llguidance 做 logits mask）
  是**跨方言通用形状**，且已被参数化（`ToolConstraintEngine(tok, stc_id, etc_id)` 从外部收标记 id）。
- 唯一的方言事实是 `build_tool_call_schema`——它的 docstring **自陈**「锚定 prompt_builder 回放给模型看的格式」。
- **判定**：`ToolConstraintEngine` / `ToolCallGuard` 留 `core/`，新增 `schema_builder` 入参由方言喂；
  `build_tool_call_schema` → `dialects/gemma4/tool_schema.py`。

**关键发现（R0 的真正接缝所在）**：`ResponseParser` **事实上兼任了方言标记登记处**——
engine 在 **5 处**直读 `self.parser.soc/eoc/stc/etc`（思考播种 `:138`、流过滤器 `:156`、
features 预算锚点 `:178`、约束采样编码 `:291`、token 仪表正则 `:318/:328`）。
R0 把这个隐式职责显式化为 `dialect.markers`，取值来源与结果完全不变。

**落地产物**：
```
core/dialect_protocol.py          新增：ModelDialect 协议 + DialectMarkers
dialects/__init__.py              新增：注册面（照 backends/ 同构，惰性导入）
dialects/gemma4/dialect.py        新增：Gemma4Dialect（只做组装，无新逻辑）
dialects/gemma4/prompt_builder.py ← git mv core/prompt_builder.py（内部一行未改）
dialects/gemma4/response_parser.py ← git mv core/response_parser.py（内部一行未改）
dialects/gemma4/tool_schema.py    ← 从 core/constrained.py 摘出（逻辑一行未改）
```
**引擎签名变更**：`AgentEngine.create(..., suppress_ghost_thinking=bool)` →
`model_options=dict`（注册表条目整条透传）。理由：`suppress_ghost_thinking` 是
**gemma-4 的方言事实**，出现在引擎签名里正是 R0 要消除的泄漏。唯一调用方 app.py 已同步。

**验收（全绿）**：
```
基线（手术前）:  132 passed in 4.46s
手术后:          142 passed in 2.57s     ← 132 全数保持 + 10 项新方言金测试
```
A/B 逐项对拍（`Gemma4Dialect` vs 原实现，同一 fake processor）：
```
build_prompt 一致: True (221 chars)      parse_response 一致: True
markers 取自 parser: True               thinking_prefill 一致: True '<|channel>thought\n'
tool_call_schema 一致: True             suppress_ghost_thinking 透传生效: True
未知方言拒绝: OK → 未知模型方言: 'qwen3'（可用: gemma4）
```
装配路径校验：`AgentEngine.create` 签名含 `model_options`、不含 `suppress_ghost_thinking`；
`core/` 下无方言模块残留。

**新增金测试** `backend/tests/test_dialect.py`（10 项）——补的是**存量 132 项的空白**：
它们走 FakeEngine，**不覆盖 AgentEngine 的方言装配**。新测钉死：注册面缺省与未知方言
拒绝、markers 来源、build/parse 与原实现逐字节一致、思考播种用实际 token、
**schema 与回放格式锚定**（脱锚会让约束采样把模型逼成没见过的形状，且因 fail-open 不报错）、
空工具集 schema 宽松（空 anyOf 会让 llguidance 编译失败）、模型级开关经 options 透传、
隐私门控穿过方言层不变。

**⚠️ 诚实记账（`ROADMAP.md` 风险 2）**：以上只证明「**gemma4 收编无回归**」，
**不证明「已支持 30–70B 大模型」**。真·换模型验证等硬件。第二个方言（ChatML 系）
落地前，本接缝的正确性只有单实现背书——**单实现的抽象总是可疑的**，
届时大概率要微调协议（这是正常的，不是返工）。

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

**验收**：**156 金测试全绿**（含 R1-0 那 9 项**一字不变**）+ 三沙箱 A/B 对拍一致。

---

#### 🟡 实测结果（进行中）

**R1-0 · 建回归网（计划外，2026-07-16 完成）**

**为什么计划外**：开工前审计发现——**三个沙箱零测试覆盖**。142 项金测试无一碰沙箱循环
（`grep -rln "sandbox|run_math_research|run_research|run_excel_task" backend/tests/` 仅命中
`test_push.py` 的一处无关字样）。**在无回归网的情况下重构铁律 1 的承重代码，正是
CHARTER 警告的那件事**。故先建网再动刀——这不是决策，是方法。

产出 `backend/tests/test_sandbox_invariants.py`（9 项，以 math_sandbox 为载体）：
- **不变量 1 工具集物理受限**：沙箱工具集恒为手工装配的 `{run_python, submit_answer}`
  （皆纯计算，无 shell/文件写/GUI/网络）；每次生成传给引擎的 `n_tools` 恒为 2，
  与主 MCPRegistry 的 34 个类无关。
- **不变量 2 KV 隔离**：`new_cache()` 恰好被调 1 次，且沙箱的 `brain_memory_state`
  **就是**那一份新建句柄（spy 后端验证）；会话替身不得暴露任何持久化面
  （`append_log`/`save_memory`/`save_meta`/`history_log`）。
- **不变量 3 隐私边界**：`getattr(sess, "inject_env_context", False) is False`。
  ⚠️ **此前这条是靠「三个沙箱都忘了写这个属性」偶然成立的**——引擎 `getattr(..., False)`
  兜底。R1-0 把它钉成契约；R1-1 统一会话时必须**显式**设 False，而不是继续靠遗漏。
- **循环骨架 4 项**（重构必须逐条保持）：cancelled 返回标记 / 解析失败回灌纠错示例 /
  纯文本结论推回一次后降级标注 UNVERIFIED / submit_answer 成功即以解释器输出早返。

**建网中发现的既有行为**（此前无人记录）：`max_steps` 用尽后**还有一次强制收尾生成**
（`math_sandbox.py:218`）。故驱动 `max_steps=N` 需要 `N+1` 条脚本。这不是 bug，
是「到死也要逼它提交一次」的护栏——已在测试注释里钉住。

```
建网前:  142 passed
建网后:  151 passed in 3.13s      ← R1 重构的验收基准
```

**R1-1 · 统一 EphemeralSession（2026-07-16 完成）**

产出 `backend/framework/agents/sandbox_runtime.py`：`EphemeralSession(backend,
system_prompt, opening, images=None, time_anchor=False)`。净删 53 行，**156 全绿**
（R1-0 那 9 项 + 存量 142 项一字未改，新增 5 项契约）。

**实为 5 份拷贝，不是账本此前记的 3 份**——审计时 `grep` 抓出另外两处：
 · `core/orchestrator.py:826` `_AuditSession`（目标校验），其 docstring 自陈
   「与 agents/research_sandbox._EphemeralSession 同型」——**注释诚实地记着它是拷贝，
   但注释阻止不了漂移**；
 · `excel_sandbox.py:271` 视觉 QA 复用了 excel 自己那份（带图分支）。
三条不变量此前散在 5 处注释里，改一处修不了另外四处——这正是收编的理由。

**隐私边界：从「偶然成立」变成「写死」。** 重构前 5 份拷贝都恰好没设
`inject_env_context`，引擎 `getattr(..., False)` 兜底 → 边界成立**纯属巧合**。任何
一份哪天手抖加上它，个人环境事实就静默流进沙箱且无人报错。现在它是类属性
`inject_env_context = False`，且金测试断言 `"inject_env_context" in
EphemeralSession.__dict__`——**钉死"必须显式存在"，而非只断言取值为 False**（后者靠
遗漏也能过，等于没测）。

**行为零变化的取证**：金测试只走 math 一条路径，research/excel 的会话装配无覆盖，
故另跑 A/B 脚本逐字段对拍 5 个调用点（scratchpad `verify_r1_1.py`）。据此发现一处
差点漏掉的行为：旧 research 类**无条件**注时间锚，故 `run_page_digest`（web_read）
也一直带锚——新签名下必须显式传 `time_anchor=True` 才是零变化。若只照"web_read 不需要
时间"的直觉写，就会悄悄改掉既有提示前缀。

**放置位置**：放 `agents/` 而非 `core/`。`agents/` 对 `core/` 是**零静态导入**的
（沙箱只鸭子类型地收 `engine`——这正是 FakeEngine 能测它们的原因）；orchestrator 反向
惰性导入，沿用 `orchestrator.py:989` 既有先例，不新增依赖边。

---

**R1-1/R1-2 的可行性实证**（三份源码逐段对读的结论）：

*会话替身*——三份 `_EphemeralSession` 公共核完全一致
（`backend.new_cache()` + `[system, user]` 两条历史 + `images`/`audios` + `cancel_requested`），
差异仅三处：research 注**分钟级时间锚**（主循环只注日粒度，沙箱无时间工具且 KV 一次性，
故直接注到分钟零缓存代价）、excel 支持**图像**（多模态 content 数组）、math 无。
→ 统一签名：`EphemeralSession(backend, system_prompt, opening, images=None, time_anchor=False)`。

*ReAct 循环*——骨架**实证同构**，math 与 excel 近乎逐行相同：
```
for step in range(max_steps):
    result = await engine.generate_once(sess, tools=_TOOLS, temperature=0.2, max_tokens=N)
    if result.get("cancelled"): return f"[{name}: generation cancelled]"
    calls = [c for c in raw_calls if c.get("type") != "error"]
    if not calls:
        if errors and parse_retries < max: → 回灌纠错示例 + continue   ← 仅 JSON 示例不同
        if not nudged: → 推回 + continue                              ← 仅推回话术不同
        return <放弃话术>                                              ← 沙箱专属
    sess.history.append(assistant)
    responses = [dispatch(c) for c in calls]                          ← 沙箱专属（真差异）
    sess.history.append(tool)
<步数用尽处理>                                                          ← 沙箱专属
```
→ 可统一的：循环控制、cancelled 检查、调用过滤、纠错闭环（示例入 spec）、
推回（话术入 spec，`None` = 无推回，research 即如此）、历史追加。
→ 必须留 hook 的：`dispatch(calls) -> (responses, final)`（math 的 submit 早返、
excel 的 `_deliver` 早返）、`on_give_up(text)`、`on_exhausted()`（research 与 math
都要再跑一次收敛/强制提交生成）、`should_converge()`（research 独有的软截止）、
`on_step(step)`（research 上报进度、math/excel 打印）。

**判断**：hook 集收敛在 5 个以内，且每个都对应真实差异——**R1-2 可做，不是硬压模子**。
但 excel 的 `INSPECT→PLAN→WRITE→VERIFY` 状态机与内部 QA 子会话（`excel_sandbox.py:271`
另起一个 `_EphemeralSession`）是最复杂的一例，**R1-4 从 math 起步、excel 压后**。

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
