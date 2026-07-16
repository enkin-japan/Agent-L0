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
   ls backend/framework/memory/notes.py            # R4 是否已落地（笔记数据在 backend/notes/，gitignore）
   git ls-files apple-app | wc -l                  # R5.1 是否已入库（>0 = 已入库）
   ```
3. **对照 §2 进度表**认领下一个 ⬜ 项。**一次只开一项**（§D.4）。
4. **动智能层前**（`core/` `memory/` `mcp/` `agents/` `backends/`）：
   先读 `backend/tests/` 知道金测试守着哪些不变量（铁律 1 唯一例外条款）。
5. **每项完成**：更新 §2 进度表 → 更新 §3 对应阶段的「实测结果」→ commit + push
   （用户常规授权：每阶段完成均更新文档 + commit + push）。
6. **`git add` 用显式路径，不要 `git add -A`**——见 §S.2。

**当前位置（每次完成一项后更新这一行）**：
> 🟢 **2026-07-16：R0–R4 ✅；R5-1 ✅（app 源码入库）；R5-2 ✅（edge 瘦身 2.1G→888M，音频路移设备 A）；R5-3 ✅（内嵌运行时定 python-build-standalone，950M 可搬树，hardened runtime 签名后 212 金测试全绿）。下一步 = R5-4（`SidecarManager.swift`）。Q5/Q6/Q7 已定（D.13/D.14/D.16）；**Q8 待用户操作**（公证只差 Developer ID 证书，且只能账号持有人本人在后台签发——ASC API 密钥无此权限，已实测；CSR 与配对私钥已备好，不阻塞 R5-4）。**

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
| **D.11** | **skill = 主轮次的提示词剧本**，不是自带工具集的隔离子会话。工具沿用主循环 → 审批闸门照常生效，**零新增提权面**。要给 skill 加 `tools:`，须先解决「审批从沙箱透出到前端」 | 2026-07-16 用户 | 本节 §3.2 实测结果 |
| **D.14** | **整条音频路移设备 A**（Q6 定案，重构了原问法）：edge 不留 torch/librosa/silero/whisper，经**新增 `remote` ASR provider**（既有接缝 `backends/__init__.py:40`）+ A 侧 `/v1/transcribe`。**不选 Apple SpeechAnalyzer**——它是 Swift-only（ObjC 头只有旧的 SFSpeechRecognizer），PyObjC 够不到，会让 ASR 依赖 app 存在，与「无 app 的 dev/web-only/CUDA 线」冲突；且要先定 locale，牺牲现有的自动语言检测（`router.py:49`）。SpeechAnalyzer 日后可作同一接缝下的可选快路 | 2026-07-16 用户 | 本节 §3.5 R5-2 |
| **D.13** | **edge 启动逻辑提成单一真相**（`scripts/lib_edge.sh` 或等价），`devctl.sh` 与 app 都调它；Swift 只做它独有的**进程监护**（崩溃重启/日志/端口冲突/随 app 收摊）。**不在 app 里重写一份**——那 799 行里的顺序与探活判据是多起事故换来的（RAG 未就绪即拒启 backend，因工具探活只在启动期发生一次）。前置：启动逻辑须对「仓库布局 vs bundle 布局」「`.env.local` vs Keychain」参数化 | 2026-07-16 用户 | 本节 §3.5 R5-4；R1 同一教训：消除拷贝，不消除差异 |
| **D.16** | **可写数据经 `AGENT_DATA_DIR` 旋钮分家**（Q7 定案，选项 B）：dev 默认仓库内（保住「数据随手可见可删」的手感），app 注入 `~/Library/Application Support/dev.enkin.macagent/`。**不选「一律搬 App Support、dev 也用」**——dev 时数据离开仓库会让排查与清库都变绕。代价是两套路径都得测。与 D.13 要求的「启动逻辑对仓库布局 vs bundle 布局参数化」**是同一个旋钮**，不是第二个 | 2026-07-16 用户 | 本节 §3.5 R5-4 |
| **D.15** | **内嵌运行时选 `python-build-standalone`**（R5-3 选型，三条判据全部实测）。**否决 PyInstaller**——与本仓库「文件系统即真相源」的约定直接相冲：`mcp/registry.py:26` 扫目录找 `.py` 发现工具、`skill_registry.py:58` 与 `memory/notes.py:86` 扫 `*.md`，冻结后统统扫不到；且 `:21-23` 目录不存在时**静默建空目录后 return** → **零工具注册且不报错**。**否决「venv 拷进 bundle」**——`backend/.venv/bin/python3.13` 是指向 `/Library/Frameworks/Python.framework` 的**绝对路径符号链接**（venv 里没有解释器本体），而该 framework 的 install-name 也是绝对路径、本身搬不动，净体积 307M 且签的是 PSF 的 Developer ID | 2026-07-16 实测 | 本节 §3.5 R5-3；`scripts/build-runtime.sh` |
| **D.12** | **`tool_responses[]` 条目是「模型上下文 + 渲染源」两用面**，模型可见键走**白名单** `{name, response, content}`。R3 的 `render` 载荷挂同一条目但不进提示词；顺带修掉 `_safe`/`_target` 的既有泄漏。代价（已知并接受）：模型提示词发生变化，16GB 机上无真模型可验证质量 | 2026-07-16 用户 | 本节 §3.3 实测结果 |

---

## §2 总进度表

图例：⬜ 未开工 ｜ 🟡 进行中 ｜ ✅ 完成 ｜ ⛔ 阻塞

| 阶段 | 名称 | 状态 | 前置 | 硬件门槛 | 铁律风险 |
|---|---|---|---|---|---|
| **R0** | 模型方言层协议手术 | ✅ **2026-07-16** | — | 建设无／真验证需 ≥32GB | 铁律 1（例外条款） |
| **R1** | 子 Agent 统一执行器 | ✅ 2026-07-16 | R0 ✅ | 无 | 铁律 1（沙箱三不变量） |
| **R2a** | Skill 体系（显式调用） | ✅ 2026-07-16 | R1 | 无 | 低 |
| **R3** | 工具渲染格（协议变更） | ✅ 2026-07-16 | — | 无 | **铁律 4 + 5** |
| **R4** | 文件式长期记忆 | ✅ 2026-07-16 | R2a | 无 | 低 |
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
- [x] **R1-2 提取 ReAct 循环** → `run_sandbox(spec)` + `SandboxSpec`（**hook 实为 6 个，非预估的 ≤5**）
- [x] **R1-3** 三条隔离不变量升级为显式契约 + 金测试断言（R1-0 建契约，R1-1 把隐私那条从「偶然」改为写死）
- [x] **R1-4 三沙箱改写为声明 + hook**（math → research → excel，按难度序）
- [x] **R1-5 验收**：**157 全绿**（R1-0 那 9 项一字未改）+ 三沙箱提示词逐字节 A/B 对拍一致

**R2a** ✅ 2026-07-16
- [x] R2a-1 `skills/` 目录 + frontmatter 规范（`name` / `description` / `requires`；**无 `tools`**，见 D.11）
- [x] R2a-2 `core/skill_registry.py`（照 `MCPRegistry` 形状，含探活舍弃）
- [x] R2a-3 `mcp/tools/skill_invoke.py`（+ `MCPRegistry.load_tool` / `tool_names`）
- [x] R2a-4 首个 skill 试点：**`skills/deck.md`**（原定的 research 试点**不可行**，见下）
- [x] R2a-5 验收：**170 金测试全绿** + 装配链路与显式调用闭环实测通过

**R3** ✅ 2026-07-16
- [x] R3-0 **前置**：提示词白名单（D.12）——两用面隔离，顺带修 `_safe`/`_target` 泄漏
- [x] R3-1 **先落夹具** `protocol/fixtures/007-render-hint.*`（铁律 5）
- [x] R3-2 `PROTOCOL.md` §2.3.1 加字段（新增字段**不升版本**，§0.5 已含"未知字段须忽略"）
- [x] R3-3 `mcp/base.py` 加 `render_hint` + `ToolOutput`（**改自 `render_payload`**，见实测）
- [x] R3-4 backend 发 render 进账本（`execute_tool` 返回 `(text, render)`）
- [x] R3-5 web 投影 + 试点 `citations`（**chart 试点作废**，见实测）
- [x] R3-6 Swift **无需改动**（`transcript: [JSONValue]` 本就穿透，见实测）
- [x] R3-7 验收：夹具双端回放全绿（web 8 / Swift 7）+ 后端 184 金测试全绿

**R4** ✅ 2026-07-16
- [x] R4-1 `memory/notes.py`（NoteStore）+ `MEMORY.md` 索引（上限 40 行 / 4000 字符）
      **数据落 `backend/notes/`**（与 `sessions/` 平级并 gitignore），**不是**账本原写的
      `backend/framework/memory/notes/`——那是代码包，个人事实会被提交上 GitHub
- [x] R4-2 四类型分类学 + 「不该存什么」清单 → `mcp/tools/memory_write.py` 的描述
- [x] R4-3 RAG 新类目 `agent_notes`（`rag_sink.ensure_notes_category`，须先于 MCPRegistry）
- [x] R4-4 `memory_write` 工具；召回走既有 `rag_search`
- [x] R4-5 隐私：索引随 `include_env` 门控（门写在 builder 里）；正文侧沙箱无 `rag_search`
- [x] R4-6 验收：**206 金测试全绿**（184 + 22）+ 装配与隐私边界闭环实测

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

**R1-2/R1-4 · ReAct 循环收编（2026-07-16 完成）**

产出 `run_sandbox(engine, spec)` + `SandboxSpec`（声明 + hook）。三沙箱改写完毕，
**157 全绿**（R1-0 那 9 项一字未改，新增 1 项分歧契约）。

骨架拥有：步数循环与软截止 · 取消检查 · 调用过滤（`type != "error"`）· 解析纠错闭环
· 纯文本推回 · assistant/tool 两类历史追加 · 预算用尽转收尾。

**两处与此前可行性分析不符**（先前那份分析是逐段对读得出的，仍不够准——教训同 R1-1）：
 · **`on_exhausted` 不是同一形状**：web/math 在步数用尽后还要再做一次强制生成，
   **excel 不做**——交付资格由门禁状态决定，多问模型一次也变不出 GO。故此 hook 是
   完整 async 回调，不是「补一句提示词再生成」的模板。
 · **hook 实为 6 个，不是预估的 ≤5**：漏算了 excel 的修复预算通告（在 tool 响应入历史
   **之后**追加，故需 `on_step_end`）。6 个里 3 必需（dispatch / on_give_up /
   on_exhausted）+ 3 可选（should_converge / on_step / on_step_end），每个仍对应一个
   真实差异，未出现「为了凑数把不同的东西塞进同一个模子」。

**刻意不统一的：三段解析纠错提示词。** 三份措辞不同且各自调优过——research 那两句额外
引导（"the exact same format as your successful web_search call" / "Do not flatten the
parameters…"）源于一次真实事故：沙箱曾因单次格式错误就「未能形成结论」。把它们揉成一个
模板 = 在没有真模型可验证的 16GB 机器上**悄悄改提示词**。故骨架只统一闭环控制流
（计数/入历史/continue），提示词原文（`parse_feedback` / `nudge`）归各沙箱自带。

**提示词漂移的取证**：这是本阶段的真风险——散文是手工搬运的，金测试只覆盖 math 一条
路径，漂移了不报错、只会让模型在 research/excel 上悄悄变笨。故拿重构前的 git 版本
（`2057897`）逐字节对拍：math/web 走 spec 截获比对，excel 因需真实工作区改用 AST 取
字面量比对（scratchpad `verify_r1_2.py`）。三份纠错提示词 + 两份 nudge + 预算参数 +
取消标记文案，全部一致。

**清掉的死代码**：math 的 `last_submit`（赋值后从未被读）、excel 残留的 `parse_retries`
/ `nudged`（职责已归骨架）。

**一处刻意的非零变化**：诊断 print 统一进骨架（`[{tag}] step i/n: calls=[...]`）。
此前 math/excel 各印各的、research 干脆没有——调试时三个沙箱三种可见度本身就是缺陷。
stdout 无任何测试/契约依赖，故判定可改；excel 的 `repairs=N` 经 `trace_extra` 保留。

**新钉的契约**：`test_sandboxes_keep_their_real_differences` 守三条**分歧**（research
无推回 / 只有 research 有软截止 / token 预算按产物形态不同）。抽象消除的是拷贝、不是
差异——这三条最容易被后来者「顺手统一」掉，故钉死。

---

**R1-1/R1-2 的可行性实证**（三份源码逐段对读的结论，**事后校正见上**）：

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

**实测结果**（2026-07-16 完成）：

产出 `core/skill_registry.py` + `mcp/tools/skill_invoke.py` + `skills/deck.md`，
**170 金测试全绿**（新增 13 项）。

**病灶描述要改准**：账本原文说 `grep -rni "skill"` **零命中**——实测有 4 处命中，但都是
指向仓库根 `excel_skill/`（上游参考件）的注释，不是 skill 体系。病灶实质成立，措辞不实。

**本节此前自相矛盾**，是 R2a 的第一个障碍：正文说 skill「不值得起子 agent（**不需隔离**）」，
同节又加粗写「**跑在 R1 的统一执行器上**」+ frontmatter 草案带 `tools / max_steps / model`
——前者是提示词剧本，后者是隔离子会话。两种读法的**安全后果完全不同**，故提交用户裁决 → D.11。

**要害（此前无人算到）**：审批闸门**只存在于 `orchestrator.py:551`**（经
`registry.requires_approval`）。沙箱从不碰 registry，`run_sandbox` **没有审批通路**，
此前也不需要——不变量 1 保证沙箱工具由构造即无害。若 skill 能自带 `tools:` 又跑在沙箱
执行器上，**一个 md 文件就成了绕过审批执行 `run_shell_command` / `write_file` 的路径**。
这个洞今天不存在，不能由 skill 引入；R2b（模型自主选 skill）会把它放大成提权面。
→ 用户选定剧本形态：工具沿用主循环，闸门照常生效，零新增提权面。

**注入路径天然存在**：`skill_invoke` 把正文作为 tool 响应返回，正文即随 tool 响应落进
主编排轮上下文，模型随后用主循环工具照做。不需要新的注入机制。

**R2a-4 原定试点不可行**：账本原建议「research 的六条规则从 Python 抽成 md」。但
research 的价值大半在它的 bespoke dispatch（来源注册表、归一化去重、候选池补位、账本
拼装），md 表达不了；只抽提示词会得到一个**干不了 research 活的 skill**。且在剧本形态下，
沙箱系统提示本就不是 skill——它们是沙箱的系统提示，留在原地。
→ 改选 **`skills/deck.md`**：deck 的五个工具有天然多步序列（建纲→init→逐页→导出 PDF），
今天**哪里都没写**，全靠模型从工具描述里自己拼。真实、可验、正是「不值得做成工具、
不需隔离、需要复用」那个空档。

**装配顺序有个死结**：`skill_invoke` 的探活要看剧本表，剧本表的 `requires` 探活要看工具表
——**两表互为前置**，只能两段装配：MCPRegistry 目录扫描（此时 skill_invoke 必然被跳过）
→ 建剧本表 → 登记 runtime → `mcp_registry.load_tool(SkillInvokeTool())` 补注册。
为此给 MCPRegistry 加了 `load_tool()`（走同一条探活路径）与 `tool_names()`。
skill_invoke 的探活在剧本表未装配时会打印说明行，免得后来者把 ⛔→✅ 的日志当故障。

**探活不是摆设**：`deck.md` 的 `requires: [..., deck_export_pdf]`，而 `deck_export_pdf`
自身探 playwright——playwright 缺失时整份剧本跑不通，此时剧本被舍弃、不 advertise 给模型。
实测覆盖了这条真实路径。

**D.11 由金测试守死**：`test_skill_grants_no_tool_authority` 断言 `Skill` 面上不得出现
`tools` / `allowed_tools` / `max_steps` / `model` 任一字段。这条不是口味，是安全边界——
加字段前必须先解决审批透出。

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

**实测结果**（2026-07-16）：**本节原计划有四处与现场不符，逐条记下。**

**① 病灶属实，但根因还深一层。** `chat-message.tsx` 确实对所有工具结果画同一个灰框。
但渲染源不是 `tool_result` 事件——归约器只把它喂给 `preview.steps`（进行中的活动条）。
transcript 是从 `turn_ended.response` **整个重建**的（`turn-reducer.ts:147` "response 原样"），
而 `PROTOCOL.md` §2.3 明写该数组与 `session.history` **同构**——也就是**模型的上下文**。
`render` 只能挂在这条两用面上（`tool_responses[]` 条目没有 `call_id`，另开平行面只能靠
下标 join，错位 = 卡片配错工具结果）。→ 由此引出 D.12 与 R3-0。

**② `_safe` / `_target` 早就在泄漏给模型**（顺手发现的既有缺陷）。
`prompt_builder._build_tool_responses` 此前 `json.dumps` **整个 dict**，实测输出：
`{"name":"x","response":"ok","_safe":true,"_target":"k"}`。
`_` 前缀的「内部元数据」约定只在**落盘**（`_strip_internal_meta`）与**顶层**
（`public_turn`）兑现，**提示词构建处从来没剥过**。R3-0 的白名单一并修掉。

**③ R3-5 / R3-6 描述的工作不存在。** 两端 transcript 都**刻意是不透明子树**
（web `...response` 直接展开；Swift `transcript: [JSONValue]`，`TurnReducer.swift:30`），
新字段本就穿透。→ **Swift 侧零改动**；web 的真实工作在组件（`renderByHint`），不在归约器。
故 007 是**守卫夹具而非验收夹具**：它在实现前就是绿的。按夹具 README 的规矩做了变异测试
（把 web 归约器改成按字段挑选）——**只有 007 红，001–006 全绿**，证明这条不变量此前无人守
（`003` 只守未知**事件类型**，未知**字段**没人守）且本夹具守得住。

**④ chart 是最不该做的试点**（账本原话「先上收益最大的 chart」）——它是唯一**已经解决**
交付的：`orchestrator.py:720` 的 `_append_missing_asset_links` 会确定性地把模型漏贴的
资产链接补进答复正文，正是为「交付不该依赖模型的转写忠实度」而建。再加 chart 渲染格 =
**同一张图出两次**。已实现后回退，改挑真正的空档 → **`citations`（`rag_search`）**：
今天是灰框里一大坨 `--- Result N (Match Score: …) ---`，命中分/来源/路径全埋在文字里。
（chart 接管交付留到 R5——原生 app 能渲染 render 之后再删 markdown，否则 app 侧图会直接消失。）

**契约改动：`render_payload(result, args)` → `ToolOutput(text, payload)`。**
原设计要工具**从自己刚格式化出的文本里再解析回结构**：chart 只是一行 markdown 尚可，
`rag_search` 的 N 条结果就得靠 `--- Result N ---` 分隔符切，正文里出现同样的串就散架。
结构在 `execute` 里本来就活着，直接交出去即可。`execute_tool` 改返回 `(text, render)`
（**全仓只有 `orchestrator.py:1015` 一个调用点**，改动面极小）。
两个白拿的好处：载荷**天然不吃观察截断**（截断只作用于给模型的文本——原设计里我曾专门
写了 `raw_result` 绕开截断，新契约下这个坑由构造消失）；载荷**天然不进模型上下文**。
`execute` 照旧可返回 `str`，存量工具一个未改。

**夹具与真实后端逐字节对账**：007 的 `render` 与 `response` 文本均与 `rag_search` 真实产出
实测比对一致（payload 顶层键、item 键、分值精度、全文）。夹具形状若和真实对不上，
比没有夹具更糟——两端照着它实现，跑起来却对不上后端。

**验收**：后端 **184 金测试全绿**（170 + R3-0 白名单 2 + 渲染格 12）；
web vitest 8 / Swift 夹具 7 全绿；`tsc --noEmit` 与 eslint 干净。

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

**实测结果**（2026-07-16）：

**① 落盘位置：账本原写的 `memory/notes/` 是错的**（§R 再入清单里那行 `ls
backend/framework/memory/notes/` 已一并改掉）。那是**代码包内部**——笔记是用户的个人
事实，进代码树就等于被提交上 GitHub。仓库既有惯例很明确：运行时数据一律
`backend/<name>/` 且 gitignore（`sessions/` `schedules/` `push/`）。故笔记落
**`backend/notes/`**，`.gitignore` 同步加一条，并由金测试钉死那条 gitignore 存在。
（`skills/` 放仓库根是因为剧本是**要入库的内容**；笔记恰恰相反，两者不是一回事。）

**② 两条召回路，缺一不可。** 账本说「召回走 RAG」，但只有 RAG 是不够的：模型压根不会
想到去搜一件它不知道存在的事。故 `MEMORY.md` 的一行摘要**随环境块注入主编排轮**，
模型先"知道自己知道"，再按需用 `rag_search` 取正文。索引带上限（40 行 / 4000 字符，
超出时**明说"还有 N 条，去搜"**而不是悄悄截断——悄悄截断会让模型以为那就是全部）。

**③ 隐私红线由既有闸门承担，不另造。** 索引经 `build_prompt(env_extra=...)` 与静态个人
事实**共用同一道 `include_env` 门**，且**门写在 builder 里**而不是靠调用方自觉——R1-1 的
教训：不变量要写死（当年 5 份沙箱拷贝全靠引擎的 `getattr` 兜底才没泄漏）。实测：
主轮含笔记与"東京"，沙箱两者皆无。正文侧则由构造保证——**三沙箱的工具集里根本没有
`rag_search`**（沙箱不变量 1），金测试扫源码钉死这条。

**④ frontmatter 解析器提成共用**（`utils/frontmatter.py`）。笔记与 skill 剧本同格式，
按 R1 的教训——拷一份就会漂移——不再各写一个。

**⑤ 顺带修掉一个真 bug**：名字校验原本收大写，而 macOS/Windows 文件系统大小写不敏感
——`X.md` 与 `x.md` 是同一个文件，内存字典却当两条：索引说两条、磁盘只有一条，
后写的悄悄盖掉先写的。改为只收小写 kebab-case，入口挡掉。

**⑥ 装配顺序**：`agent_notes` 类目与笔记本句柄都必须先于 `MCPRegistry`——`rag_search`
在 `__init__` 里探测类目来构建自己的 schema，晚了它就不认识这个类目、笔记正文再也搜不到
（`rag_sink` 的既有注释早就写着这条，照办）。与 `skill_invoke` 不同，这里没有互为前置
的环，一段装配即可。实测 RAG 离线时 `memory_write` 照常注册——笔记文件与索引不依赖 RAG，
只有全文检索降级（fail-open）。

**验收**：**206 金测试全绿**（184 + 22）；装配与隐私边界闭环实测通过。

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

- [x] **R5-1 · app 源码入库**（§D.6）✅ 2026-07-16 —— 58 文件（37 swift + 图标/清单/脚本）
  - 已改 `.gitignore` 与 `CHARTER.md` 提交纪律段（宪章改动，D.6 授权）
  - **审计结果**：.p8 在仓库外 ✅；`Sources/` 走 Keychain、无硬编码密钥/Tailscale 地址 ✅；
    **但**三个发布脚本 + README 硬编码了 ASC 的 Key ID / Issuer ID → **参数化走 `.env.local`**
    （用户裁决；新增 `apple-app/asc_env.py` 供两个 py 共用，`release-ios.sh` 用 bash 原生 source）
  - **潜伏 bug（入库才致命）**：`apple-app/.gitignore` 有条裸的 `Contents.json` 规则，
    而资产目录的 `Contents.json` 是**源码不是派生物**——缺了 Xcode 不认 catalog、新克隆编不出图标，
    正好废掉 D.6 的前提。已删该规则

- [x] **R5-2 · edge 依赖瘦身** ✅ 2026-07-16 —— **实测 2.1GB → 888M**（目标 <1GB 达成）
  - 产出 `backend/requirements-edge.txt`（每条排除项都注了实测的反向依赖来源）
  - **`torch` 的主人查错了**：不是 `mlx-whisper`（本体仅 1.9M），是 **`silero-vad`**
    （`Requires: packaging, torch, torchaudio`）。见 D.14——原 Q6 问法建在这个错前提上
  - 代码改动：VAD 惰性化（`app.py` + `utils/file_handler.py` 顶层 import 下沉；
    默认 `modalities: ["text","image"]` 下 torch 本就从不参与音频路，却被无条件加载）；
    **区间切片下放进 provider**（原在 `ingestion/parsers/asr.py` 用 librosa 做 →
    会把 numba/llvmlite/scipy/sklearn 钉回 edge）
  - 新增 `remote` ASR provider + A 侧 `POST /v1/transcribe`（复用既有 `/v1/media`
    内容寻址上传；跑在 `S.executor` 单线程上 = 白拿「单 worker 串行」纪律）
  - `ingestion.asr_provider` 改默认 `auto` = 跟随 `inference.mode`（两个旋钮各说各话时
    症状极隐蔽：mode=remote 却把 asr 留 mlx_whisper，edge 白背 torch 全家且不报错）
  - **实测验收**：干净 venv 装 888M；**206 金测试在无 torch/mlx/librosa 的 edge venv 里全绿**；
    `import app` 成功（transformers 优雅降级为"只有分词器"）；A 不可达时 fail-open 成
    标记文本且说清是哪台机器。新增 6 项金测试（`test_edge_slimming.py`）钉死顶层 import，
    并已变异测试确认它会红

- [x] **R5-3 · Python runtime 打包选型** ✅ 2026-07-16 —— **定 `python-build-standalone`**（D.15）
  - 产出 `scripts/build-runtime.sh`（下载→装 edge 依赖→验可搬→逐个签名，全程可复现；
    版本钉死 `PBS_TAG=20260623` / cpython 3.13.14，与 `backend/.venv` 同为 3.13.x）
  - **三条判据的实测值**：

    | 判据 | 实测 |
    |---|---|
    | 体积 | **950M** = 解释器 65M + edge 依赖 888M（对比：python.org 净解释器 307M） |
    | 启动时延 | 裸解释器 **~10ms**（与 python.org framework 无差）；`import app` **514ms 温 / 940ms 冷** |
    | 签名 | 320 个 Mach-O 逐个签 hardened runtime = **3.6s**；`codesign -v --strict` 通过；**签完 212 金测试仍全绿** |

  - **可搬性是这条路线的全部前提，已实测**：PBS 把 libpython **静态链进二进制**，`otool -L`
    只剩 `/usr/lib` 与 `/System/Library` 的系统库，无任何指向自身安装路径的绝对引用 →
    搬到任意路径后 `sys.prefix` 自动跟随、212 金测试全绿。**故意不建 venv**：venv 的
    `pyvenv.cfg` 里 `home=` 又是绝对路径，等于把刚拿到的可搬性再丢一次；依赖直接装进
    PBS 自带的 site-packages，整棵树自成一体。此验证已固化进脚本（不是一次性手测）
  - **首次启动会慢一次**：签名后首跑 9.22s → 二跑 1.45s。是 Gatekeeper 对新签名的一次性
    校验（整棵树约 +7.8s），装后仅付一次。**冷启动验收（R5-9）计时须避开这一次**，否则会
    把一次性成本误记成常态时延
  - **`import app` 的 514ms 主要是 R5-2 的功劳不是 PBS 的**：同一份代码在开发机 2.1G 的
    `.venv` 上要 **3,109ms**（torch 导入链）。别把这 6× 记到打包选型账上
  - **🔴 遗留（三个候选都躲不掉，非 PBS 特有）**：`app.py:94` `BASE_DIR = Path(__file__).parent`，
    而 `sessions/` `schedules/` `notes/`(R4 个人事实) `push/` 全挂它下面 → 装进 .app 后
    **写 bundle 内部会破坏代码签名**，且 `/Applications` 下通常不可写。**可写数据必须搬出
    bundle**（惯例位置 `~/Library/Application Support/`）。这是 R5-4 的前置，见 Q7
  - **参考**：cc-haha `desktop/electron/services/sidecarManager.ts` + `desktop/scripts/build-sidecars.ts`
    （它的 sidecar 是 TS 不是 Python，但**进程生命周期管理的形状**可借鉴）

- [ ] **R5-4 · `SidecarManager.swift`**
  - 职责：启停 backend + searxng、就绪探活、端口冲突处理、崩溃重启、日志落盘
  - **🔴 顺序**：searxng **必须先于** backend 就绪——`web_research` 的注册是**启动期一次性探活**，
    searx 晚到 = 整轮不注册（`HETERO_COMPUTE.md` §S.3 已入账的事故）
  - ✅ **Q5 已定（D.13）**：启动逻辑**提成单一真相**（`scripts/lib_edge.sh`），`devctl.sh` 与 app 都调它。
    Swift 侧**只做进程监护**——崩溃重启 / 日志落盘 / 端口冲突 / 随 app 收摊，这些才是 app 独有的。
    「启什么 / 什么顺序 / 怎么算就绪」一律不在 Swift 里重写（R1 同一教训：消除拷贝，不消除差异）。
    **前置**：启动逻辑须对「仓库布局 vs bundle 布局」「`.env.local` vs Keychain」参数化。
  - 🔴 **前置（R5-3 挖出）**：**可写数据必须先搬出 bundle**——`app.py:94` 的 `BASE_DIR`
    现在既是「代码在哪」也是「数据写哪」，而 `sessions/` `schedules/` `notes/`(R4 个人事实)
    `push/` 全挂它下面；往 .app 内部写会**破坏代码签名**，`/Applications` 下通常也不可写。
    **见 Q7**（dev 与 app 是否共用同一套路径），它与本条的「布局参数化」是同一个旋钮。

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
| **3** | ~~R5-4 的启动逻辑重复~~ | ✅ **已定 D.13**：提共享库，Swift 只做进程监护 |
| **4** | ~~R5-2 依赖瘦身可能发现「edge 去不掉 mlx」~~ | ✅ **已消**：mlx 去得掉（D.14）。**但担心错了对象**——真正钉住体积的是 `silero-vad`→torch，与 whisper 无关 |
| **5** | **打包后的失败是静默的**：本仓多处以文件系统为真相源（工具/skills/notes 扫目录），扫不到时 `registry.py:21-23` 只是建个空目录后 return。选 PBS 已避开冻结（D.15），但 **bundle 内路径一旦搬错，症状仍是「启动正常、零工具、无报错」** | R5-9 验收**必须断言工具数**，不能只看「起来了」 |

---

## §5 待决（开工前需拍板）

| | 问题 | 阻塞谁 |
|---|---|---|
| ~~**Q5**~~ | **已定 → D.13**：提取共享启动库，`devctl.sh` 与 app 都调；Swift 只做进程监护 | ~~R5-4~~ ✅ |
| ~~**Q6**~~ | **已定 → D.14**：整条音频路移 A（原问法有误：torch 是被 silero-vad 钉住的，不是 whisper——去掉 whisper 几乎不省空间） | ~~R5-2~~ ✅ |
| ~~**Q7**~~ | **已定 → D.16**：`AGENT_DATA_DIR` 旋钮，dev 默认仓库内、app 注入 App Support | ~~R5-4~~ ✅ |
| **Q8** | **macOS 公证：只差 Developer ID Application 证书，且只能由你本人签发**（行动项，不阻塞 R5-4）。已实测：ASC API 密钥 **notarytool 鉴权通过**（`notarytool history` → "No submission history"）；但**任何 ASC API 密钥都创建不了 Developer ID 证书**——`POST /v1/certificates` 返回 `403 FORBIDDEN_ERROR: This operation can only be performed by the Account Holder`，而 ASC API 密钥的可选角色里（Admin/App Manager/Developer/Marketing/Sales/Finance/Customer Support）**根本没有 Account Holder**。**待你操作**：developer.apple.com → Certificates → 新建 **Developer ID Application**，上传已备好的 `~/Desktop/DeveloperID.csr`（配对私钥已在 login Keychain，是唯一副本），下载 .cer 后双击装入即可。**选 G2**：经典的 "Developer ID Certification Authority" 中间证书 **2027-02-01 到期**（距今仅半年余），现在签发经典型等于拿一张很快要换的证书 | R5-9 验收形态 |

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

### S.4 · 「没报权限错」不等于「有权限」——校验顺序会骗人（2026-07-16）

**事故**：为在不产生副作用的前提下探明 ASC API 密钥能否创建 Developer ID 证书，
我用**故意无效的 CSR** 去 POST，理由是「CSR 无效就绝不可能真建出证书」。
苹果回 `409 ENTITY_ERROR.ATTRIBUTE.INVALID: csrContent 无效`。我据此向用户报告
「✅ 有权限（报的是 CSR 错，不是权限错）」。换上真 CSR 后立刻 `403 FORBIDDEN_ERROR:
This operation can only be performed by the Account Holder`。

**教训**：探针的「安全」设计和它的「有效」是两回事。那个 CSR 保证了不产生副作用——
这部分是对的；但我从「错误信息不是权限错」反推「权限没问题」，**默认了服务端先查权限
再校验属性**，而苹果的顺序恰好相反。**一个失败请求的错误类型，只能证明它走到了哪一步，
不能证明它没走到的那些步会通过**。要判定权限，就得发一个除权限外一切都合法的请求——
而那正是有副作用的那个。**「无副作用地探明有副作用的操作可行性」有时根本不可得**，
此时正确做法是承认不确定、把它作为不确定项交给用户，而不是拿一个弱信号当结论。

**已付代价**：向用户报了一次错误结论，用户据此选了「我用 API 建」，做不到。

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
