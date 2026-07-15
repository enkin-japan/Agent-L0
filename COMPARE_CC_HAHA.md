# Agent × cc-haha 全量对比（执行层 + 前端）

> 对比日期：2026-07-16 ｜ 对比对象：`/Volumes/enkin/project/cc-haha`（NanmiCoder/cc-haha, HEAD `eddaec54`）
> 对比范围：本项目的**执行层（backend）+ 前端**——即算力分离拓扑下跑在设备 B 的那一半。
> 不含 rag / inference-server / MLX 后端（cc-haha 无对应物，见 §1.2）。
>
> **⚠️ 2026-07-16 修订**：初版有三处事实错误，经用户指正后已修（§1.1 测试项、§1.3 形态、
> §2.7 前端、§2.9 基建）。**根因**：原生 app 在 `apple-app/` 且 gitignored，本仓不可见，
> 我据此误判前端只有 web 一端。教训见 `ROADMAP.md` 附录。
>
> **§5 的建议已被 `ROADMAP.md` 取代**（用户补充了目标模型体量、规模化立场、原生端形态三点后重排）。
> 本文的**诊断部分（§1–§4）仍然有效**，是 ROADMAP 的依据。

---

## 结论先行

三句话：

1. **cc-haha 不是一个同量级的开源 Agent 项目，它是 Claude Code 本体。** 其 README 首段自述：「基于 2026-03-31 从
   Anthropic npm registry 泄露的 Claude Code 源码修复而来」。所以「它比我成熟太多」是事实陈述，不是水平评价——
   拿一个人 × 数月的项目对比 Anthropic 数百人 × 数年的旗舰产品，成熟度差距是**结论**而不是**问题**。
2. **你感受到的「散乱」是真的，但只有三处是真架构问题**：Skill 体系完全不存在、子 Agent 各写各的（三份
   `_EphemeralSession` 拷贝）、工具契约太薄导致渲染逻辑无处安放。其余「差距」大多是规模差或约束差，不该追。
3. **你有几处它没有、也不需要有的东西**（本地小模型上下文压缩、沙箱威胁模型论证、算力分离拓扑）。这些不是补丁，
   是这个项目的立身之本，别在"向它看齐"的过程中削掉。

---

## §1 基准校正：这到底是什么对比

### 1.1 规模

| 指标 | Agent（backend+frontend） | cc-haha (`src/`) | 倍数 |
|---|---|---|---|
| 源码行数 | ~29,800（backend 21,813 + frontend 7,966） | **606,150** | **20×** |
| 文件数 | 119 + ~40 | **2,316** | ~15× |
| 工具数 | 17 文件 / 34 类（`mcp/tools/`） | 60+（`src/tools/`，53,027 行） | ~2× |
| 测试 | 132 项金测试（13 文件）+ 20 原生 E2E + 6 组三端夹具 | 123 个测试**文件** | 不可比，见 §2.9 |
| 文档 | 8 篇根级 md | 71 篇 + VitePress 站点 | ~9× |
| 提交数 | ~30 | 1,223 | — |

cc-haha 单是 `src/utils/` 一个目录（629 文件 / 187,715 行）就是你整个项目的 6 倍。
`src/components/`（421 文件 / 83,204 行）是你前端的 10 倍。

**这一栏的意义只有一个：凡是「差距 = 数量」的地方，都不必看。** 它有 60 个工具你有 17 个，不说明架构问题。

### 1.2 约束差（最容易被误读成「架构差」的一类）

这是全文最重要的一节。两个项目跑在**根本不同的物理约束**下，很多所谓差距其实是约束的直接推论：

| 维度 | Agent | cc-haha |
|---|---|---|
| 模型 | 本地 gemma-4-e4b **4bit 量化**，16GB 统一内存 | Anthropic API（Opus/Sonnet 前沿模型） |
| 工具调用 | **文本正则解析**（`<\|tool_call>` 标记 → `response_parser.py` 手工反序列化） | API 原生 `tool_use` 结构化块，SDK 保证格式 |
| 上下文 | 受 KV 内存带宽约束，decode 随 KV 增大线性变慢 | 200k 窗口，云端算力，KV 不是本地成本 |
| 参数校验 | 手写 JSON Schema dict + `validate_arguments` 预检 | zod schema + API `strict` 模式 |
| 失败模式 | 模型吐半截 JSON → 解析崩 | 几乎不发生 |

**推论一**：你必须有 `constrained.py`（161 行）、`response_parser.py`（188 行）、`stream_filter.py`（170 行）、
`max_parse_retries` 熔断——cc-haha **一行都不需要**。上个会话那个 `Unterminated tool call` 事故，
在 cc-haha 的世界里物理上不会发生。这不是你没做好，是你在解一个它不用解的问题。

**推论二**：反过来，cc-haha 的 `ToolSearch` + `defer_loading`（工具 schema 延迟加载）之所以能做，
是因为 API 支持这个协议字段。你的 `agent.tool_schema_mode: full|catalog`（config.yaml）是**同一思路的自研版**，
这一点你其实已经想到了，只是名字不同。

**推论三**：cc-haha 全程假设「模型很聪明」。你的 `react_max_steps: 4` / `replan_max: 3` / `goal_verify: true`
（收尾目标校验，防小模型虚报完成）整套护栏，是「模型不够聪明」倒逼出来的编排层。这是你的**净增量**，不是补课。

### 1.3 形态差

> ⚠️ **本节于 2026-07-16 经用户指正后重写。原文误将本项目前端记为「仅 Web 形态」，
> 并把桌面端列为待决策项——事实是原生 app 早已交付。**

cc-haha 从 CLI 转型为 **Electron 桌面工作台**（`desktop/`，独立 vite+electron 工程），
外加 5 个 IM 适配器（`adapters/`）。

本项目是**三端**：backend + web（React 19）+ **Apple 原生 app**
（`REARCHITECTURE.md:230`：native ✅ 2026-07-11 完成，v0.10.0，macOS 已装 `/Applications`，
iOS build 10 已推 TestFlight）。原生代码在 `apple-app/`，**全程 gitignored 不入库**
（`CHARTER.md:257`）——这是我此前误判的直接原因，本仓看不到 Swift 文件。

**这不只是「也有桌面端」，形态上本项目反而更严格**：

| | cc-haha | Agent |
|---|---|---|
| 桌面端 | Electron（Chromium 套壳，web 代码复用） | **SwiftUI 原生**（macOS + iOS） |
| 多端一致性靠什么 | 同一份 web 代码 | **协议 + 夹具**（`PROTOCOL.md` v1 冻结 + `protocol/fixtures/` 六组，web vitest 与 Swift `run_reducer_fixtures.sh` 回放同一批） |
| 客户端职责 | 较厚（Electron 主进程有逻辑） | **瘦渲染器**（铁律 4：`reduce(state, event) -> state` + UI 副作用，无旁路恢复） |

Electron 复用 web 代码是**省事**；原生 + 协议夹具是**更贵但更正确**——代价是 TS 与 Swift
不能共享代码，只能共享行为标准。你已经付过这个代价了（`REARCHITECTURE.md` Phase 2 两端各 2 批次）。

**这一节的结论反转**：形态上你不落后，桌面端是原生而非套壳。但它**直接改变了 §5 的 R3**——
任何「工具自带渲染」的方案在三端架构下都是不可能的，见 R3。

---

## §2 逐层对比

### 2.1 工具体系

**你的**（`backend/framework/mcp/base.py:4` + `registry.py:7`）：

```python
class BaseTool(ABC):
    name: str; description: str; parameters: Dict
    requires_approval: bool = False
    is_concurrency_safe: bool = False
    is_read_only: bool = None       # None = 跟随 is_concurrency_safe
    timeout_seconds: int = None
    def needs_approval(self, arguments) -> bool   # 按调用动态审批
    def is_available(self) -> bool                # 探活钩子
    def get_schema(self) -> Dict
    @abstractmethod
    def execute(self, **kwargs) -> Any
```

**它的**（`src/Tool.ts:362`）：`Tool` 类型有 **50+ 个成员**，`buildTool()` 填默认值（`Tool.ts:783`）。

诚实评价：**你的工具契约的「执行侧」设计得不差，甚至有几处它没有的**：

| 能力 | Agent | cc-haha | 评 |
|---|---|---|---|
| 探活后注册（不可用则不暴露 schema） | ✅ `is_available()` | ✅ `isEnabled()` | 平 |
| 按调用动态审批 | ✅ `needs_approval(args)` | ✅ `checkPermissions(input, ctx)` | 它更强（返回 `updatedInput` 可改写参数） |
| 并发安全标记 | ✅ | ✅ | 平 |
| 只读标记 | ✅（且能与并发安全解耦） | ✅ | 平 |
| 工具级超时 | ✅ `timeout_seconds` | ❌ 无对应 | **你有它无** |
| 参数预检（fail-open） | ✅ `validate_arguments` | ✅ `validateInput` | 平 |
| 审批判定异常 fail-closed | ✅ `registry.py:69` | ✅ | 平 |
| 目录扫描自动注册 | ✅ `_load_all_tools()` | ✅ | 平 |
| **结果渲染** | ❌ **完全没有** | ✅ 7 个 render 方法 | **核心差距** |
| 结果大小预算 | ❌（有全局 `observation_max_chars`） | ✅ 每工具 `maxResultSizeChars`，超限落盘转路径 | 它更细 |
| 破坏性标记 | ❌ | ✅ `isDestructive` | 小差距 |
| 中断行为 | ❌ | ✅ `interruptBehavior(): 'cancel'\|'block'` | 小差距 |

**真正的差距是渲染归属。** cc-haha 的每个工具目录是一个**垂直切片**：

```
src/tools/BashTool/
  BashTool.tsx        ← 执行 + 渲染在一起
  prompt.ts           ← 该工具的提示词
  constants.ts
  *.test.ts
```

工具自己实现 `renderToolUseMessage()`（调用中怎么显示）、`renderToolResultMessage()`（结果怎么显示）、
`renderToolUseRejectedMessage()`（被拒怎么显示）、`renderToolUseProgressMessage()`（进行中怎么显示）。
新增一个工具 = 新增一个目录，**UI 自动就位，前端零改动**。

你的前端（`frontend/src/components/dashboard/chat-message.tsx:84`）：

```tsx
{message.toolResponses?.map((tr, i) => (
  <div className="group/tool rounded-xl border ...">
    {t.message.toolResult(tr.name)}     // 就是一个灰框 + 工具名
```

**所有 17 个工具长得一模一样**——`render_chart` 画的图、`rag_search` 的召回、`shell_exec` 的输出、
`deck_tools` 的幻灯片，全都是同一个灰框里一坨文本。这就是「使用体验感散乱」的**最直接来源**，
而且它不是前端的锅，是**工具契约里没有渲染这一格**，导致渲染逻辑无处安放。

> 注：你的 `mcp/` 命名有误导性——它**不对接任何外部 MCP server**（已验证：`grep stdio|jsonrpc|McpServer` 零命中）。
> 它是「内部工具插件系统」，只是借用了 MCP 这个名字。cc-haha 的 `services/mcp/` 是真的 MCP 客户端（`@modelcontextprotocol/sdk`）。
> 这不是缺陷，但名字应该诚实——将来若真接外部 MCP，现在这个名字会挡路。

### 2.2 Skill 体系

**你的**：不存在。

```
$ grep -rni "skill" backend/framework --include='*.py'
（零命中）
```

**它的**：
- `src/tools/SkillTool/` —— 一个工具，负责调用 skill
- `src/skills/loadSkillsDir.ts` —— 从目录加载用户自定义 skill（markdown + frontmatter）
- `src/skills/bundled/` —— 20 个内置 skill（`verify` / `debug` / `remember` / `loop` / `simplify` / `skillify`…）
- `src/skills/mcpSkills.ts` —— MCP server 也能提供 skill
- `src/services/skillSearch/` —— skill 的检索发现
- `src/tools/DiscoverSkillsTool/` —— 让模型自己发现可用 skill
- 完整文档：`docs/skills/01-usage-guide.md` + `02-implementation.md`

**skill 是什么、为什么你需要它**：skill = **一段可复用的、声明式的工作流提示词**，不是代码。
形如 `SKILL.md` + frontmatter（名字/描述/允许的工具/模型覆盖），模型在合适的时机自己调用它。
它填的是「工具（原子动作）」和「子 Agent（独立进程）」之间的空档：**有些东西不值得写成工具（没有确定性逻辑，
本质是提示词），也不值得起一个子 Agent（不需要隔离），但需要被复用和被发现**。

你现在这类东西散在哪儿？——硬编码在 `research_sandbox.py:21` 的 `SANDBOX_SYSTEM_PROMPT`、
`excel_sandbox.py:40` 的 `INSPECT -> PLAN -> WRITE -> VERIFY` 流程串里。**它们其实就是 skill，
只是被焊死在 Python 源码里，不能复用、不能发现、改一次要动代码。**

这是你说的「不成体系」里**最实的一条**。

### 2.3 子 Agent 体系

**你的**（`backend/framework/agents/`）：三个沙箱，`research_sandbox.py`(299) / `math_sandbox.py`(232) / `excel_sandbox.py`(630)。

已验证的重复：

```
$ grep -c "class _EphemeralSession" backend/framework/agents/*.py
excel_sandbox.py:1
math_sandbox.py:1
research_sandbox.py:1      ← 同一个类，抄了三遍
```

每个沙箱各自实现：一份 `_EphemeralSession` 替身类、一份 `for step in range(max_steps)` ReAct 循环、
一份手工装配的 `_TOOLS` schema 列表、一份 system prompt 常量、一份收尾逻辑。
**三份 ~80% 相同的代码。** 加第四个沙箱 = 再抄一遍。

**它的**：一份 `runAgent.ts` + 声明式的 `AgentDefinition`。

```
src/tools/AgentTool/
  AgentTool.tsx        ← 唯一的执行器
  runAgent.ts          ← 唯一的循环
  loadAgentsDir.ts     ← 从 .claude/agents/*.md 加载用户定义的 agent
  builtInAgents.ts     ← 内置 agent 就是数据：GENERAL_PURPOSE_AGENT, EXPLORE_AGENT, PLAN_AGENT...
  forkSubagent.ts      ← fork（共享父提示词缓存）
  resumeAgent.ts       ← 续跑
  agentMemory.ts       ← 子 agent 自己的记忆
```

内置 agent 是**数据**（`builtInAgents.ts:59` 里 `agents: AgentDefinition[] = [GENERAL_PURPOSE_AGENT, ...]`），
用户 agent 是**markdown 文件**（frontmatter 声明 tools / model / permission-mode / effort / hooks / mcp servers）。
加一个 agent = 写一个 md 文件，**零代码**。

**差距的本质不是「它的子 agent 更强」，是「它把子 agent 做成了一个体系（1 个执行器 × N 份声明），
你做成了 N 个实例（N 份执行器）」。** 这正是你自己诊断的「不成体系」，而且诊断得很准。

⚠️ **但有一点必须说清楚**：你的沙箱有一层它没有的东西——**威胁模型论证**。
`research_sandbox.py:6-15` 那段注释（工具集物理受限 / KV 隔离 / 单 worker 不死锁）是真正的安全设计，
cc-haha 的 subagent 没有对应论证，因为它不需要（云端模型、无共享 KV、无推理锁）。
重构成统一执行器时，**这三条隔离性质必须作为执行器的不变量保下来**，不能在"抽象化"里丢掉。

### 2.4 记忆体系

这一节要纠正你的自评。**你的记忆不「粗糙」，你的记忆是另一个东西。**

**你的**（`memory/session.py` 1,164 行 + `compactor.py` + `rag_sink.py`）：会话尺度的记忆
- `history.jsonl` 全量落盘（无损）
- `memory.json` 滚动摘要 + 已摘要水位线
- model-facing 上下文 = `[system] + [摘要块] + [尾部轮次]`
- 被折叠的原文 → `rag_sink.sediment()` 沉入 RAG（`agent_memory` 类目），日后可召回
- 分支 / 克隆脑记忆 / 崩溃恢复（`recover_inflight_turns`）

**它的**（`src/memdir/` 9 文件 + `services/SessionMemory/` + `services/compact/` 18 文件）：两套并行
- **memdir** = Agent 自己撰写的**文件式长期记忆**：`MEMORY.md`（索引，≤200 行/25KB 硬上限）
  \+ 一堆单事实 md 文件（frontmatter: `name`/`description`/`type`）
  - 四类型分类学（`memoryTypes.ts:15`）：`user` / `feedback` / `project` / `reference`
  - 明确的**不该存什么**：能从代码/git/CLAUDE.md 推导的一律不存
  - 召回靠 LLM：`findRelevantMemories.ts` 扫描所有记忆的 header → 让 Sonnet 选出最相关的 ≤5 条
- **compact** = 会话尺度压缩（跟你的对位）

**关键判断：这两者正交，不是谁强谁弱。**

| | 会话记忆（你有） | 文件记忆（你没有） |
|---|---|---|
| 尺度 | 一个 session 内 | 跨 session、跨项目 |
| 作者 | 系统自动（摘要器） | **Agent 主动撰写** |
| 内容 | 发生过什么 | **学到了什么** |
| 召回 | 上下文重建 + RAG 向量 | LLM 语义选择 |

你的 RAG 沉降（`rag_sink.sediment`）在**功能上**接近文件记忆，但有本质区别：
沉降是**被动的、无损的、全量的**（折叠掉的对话原文原样入库），memdir 是**主动的、有损的、提炼的**
（Agent 判断「这条值得记」才写，且明确规定不记可推导的东西）。

**前者是录像带，后者是笔记。** 你有一柜子录像带，没有笔记本。这才是你感觉「记忆粗糙」的真实原因——
不是实现粗糙（`compactor.py` 的实现相当精细），是**缺一个层**。

**顺带**：cc-haha 的记忆分类学（4 类型 + "不该存什么"清单）是可以直接借鉴的**设计**，不是代码。
那份 `memoryTypes.ts` 里的 prompt 写得非常好，值得逐字读。

### 2.5 会话窗口 / 上下文管理

**你的**（`compactor.py`）——两层：
1. 机械压缩（确定性、幂等、无模型调用）：
   - **取代去重**：同一只读工具对同一目标的重复读取，旧的削成桩
   - **写屏障**：两次读取之间有过写操作就切断，绝不跨写取代（GUI「读树→点击→再读树」两份都保）
   - **预算驱逐**：总量越过 `budget_chars` 才按体量驱逐旧的大观察；不越预算旧观察保持原文
   - **硬保护窗口**：最近 `keep_recent` 条永不动（保 KV 前缀复用）
2. 摘要压缩：越阈值折叠旧轮次进滚动摘要，双触发（活跃高阈值 / 空闲低阈值）

**它的**（`services/compact/` 18 文件）：
- `microCompact.ts` —— 只压缩白名单工具（`COMPACTABLE_TOOLS`：Read/Bash/Grep/Glob/WebSearch/WebFetch/Edit/Write）
- `autoCompact.ts` / `reactiveCompact.ts` / `snipCompact.ts` / `sessionMemoryCompact.ts` / `apiMicrocompact.ts`
- `timeBasedMCConfig.ts` —— **基于时间**清除旧工具结果
- `postCompactCleanup.ts` / `compactWarningHook.ts` / `grouping.ts`

**诚实评价：这一层你不落下风，某些维度上你更精细。**

你的「写屏障」（`compactor.py:87`：`if not safe: epoch += 1`）是一个 cc-haha **没有**的正确性保证——
它的 microCompact 按工具名白名单 + 时间清除，不做「同目标取代」的语义分析，因此也不需要屏障。
你的「预算驱逐 + 体量优先」也比它的时间驱逐更贴合真实成本（你的成本是 KV 字符数，不是时间）。

**你落后的地方**：它有 18 个文件是因为它有 **5 种不同的压缩策略在不同触发点**，
而且每一种都有 `.test.ts`。你是 196 行 2 个函数。这是**成熟度差**（覆盖的场景数），不是**设计差**。

`compactor.py` 开头那段 30 行注释——解释理念、解释为什么从纯 recency 升级、解释每个决策——
**写得比 cc-haha 的对应注释好。** 这话我是认真的。

### 2.6 权限与审批

**你的**：`requires_approval: bool` + `needs_approval(args) -> bool`，
orchestrator 挂起 → `pending_approval` → 前端弹窗 → `resume_after_approval`。

**它的**（`src/utils/permissions/` **26 个文件**）：
- `PermissionRule.ts` / `permissionRuleParser.ts` —— 规则语法（`Bash(git *)`、`Read(/etc/**)`）
- 三种规则集：`alwaysAllowRules` / `alwaysDenyRules` / `alwaysAskRules`，各自带来源（用户/项目/企业策略）
- `PermissionMode.ts` —— 模式：`default` / `plan` / `acceptEdits` / `bypassPermissions`
- `bashClassifier.ts` / `yoloClassifier.ts` —— **用模型判断命令危险性**
- `dangerousPatterns.ts` / `shadowedRuleDetection.ts`（规则相互遮蔽检测）/ `denialTracking.ts`

**这是量级差**：bool vs 一个带来源优先级、通配匹配、模式切换、模型分类器的策略引擎。
但**要不要追是个问题**——它的复杂度来自「多用户 × 企业策略 × 开源社区的千奇百怪需求」。
你是单用户自持系统。`CHARTER.md` 的非目标清单应该在这里发挥作用。

**值得抄的只有一个小点**：`checkPermissions` 返回 `{behavior, updatedInput}`——
**审批可以改写参数**（比如把 `rm -rf /` 降级成 `rm -rf ./tmp` 再执行）。
你的 bool 只能全放或全拦。这个能力很便宜，价值不低。

### 2.7 前端

> 本节于 2026-07-16 修正：原文遗漏了原生端（见 §1.3）。

| | Agent | cc-haha |
|---|---|---|
| 端数 | **3**（backend + web 7,966 行 + Apple 原生 `apple-app/`，不入库） | 2（终端 Ink UI + Electron 桌面端 + H5） |
| 桌面端 | **SwiftUI 原生** | Electron |
| 一致性保障 | **协议夹具双端回放**（web vitest 4/4 + Swift 3/3，同一批夹具） | 同一份 web 代码，无跨端夹具 |
| 客户端厚度 | **瘦渲染器**（铁律 4，纯 reduce） | 较厚 |
| 恢复路径 | **统一一条**（`GET /turn` + `GET /events?after`），补偿动物园已整体退役 | 多路径 |
| 工具渲染 | **通用灰框**（`chat-message.tsx:84`） | 每工具自带 render（见 §2.1） |
| 状态 | zustand（`useChatStore.ts` 1,094 行）/ `AgentStore.swift` | `src/state/AppState.ts` + context |

**这一栏你赢的地方比输的多。** 「一份协议三处状态机零共享测试」正是 cc-haha **今天仍然是**
的样子（`utils/`+`components/`+`desktop/` 各自解释状态），而这恰恰是你 `CHARTER.md:223`
列为**已治好的第 2 条病**。你花了整个 Phase 2 把它治好，还立了夹具当契约。

你唯一输的就是**工具渲染**——但这一条在三端架构下反而更难，见 R3。

`useChatStore.ts` 1,094 行仍是个信号（占 web 前端 14%），建议自查职责边界。
但注意它已经是**换过脊柱**的版本（Phase 2 退役了补偿动物园），不是旧账。

### 2.8 扩展性与生态

| 扩展点 | Agent | cc-haha |
|---|---|---|
| 加工具 | ✅ 丢进 `mcp/tools/` 自动扫描 | ✅ |
| 加 skill | ❌ **无此概念** | ✅ md 文件 |
| 加子 agent | ❌ **写 Python** | ✅ md 文件 |
| 加斜杠命令 | ❌ 无 | ✅ 214 个 + 用户可加 |
| 外部 MCP server | ❌（`mcp/` 是内部插件系统） | ✅ 真 MCP 客户端 |
| 插件市场 | ❌ | ✅ `utils/plugins/` |
| Hooks（生命周期钩子） | ❌ | ✅ `utils/hooks/`（PreToolUse/PostToolUse/PreCompact…） |
| IM 接入 | ❌（有 APNs 推送） | ✅ 5 个适配器 |

**你唯一的扩展点是「加工具」，而且这一个做得不错。** 其余全部要改源码。
这是「不成体系」的第三条实证：**体系 = 有声明式扩展点；你只有一个。**

### 2.9 工程基建

> 本节于 2026-07-16 修正：原文用「13 个测试文件 vs 123 个测试文件」作比，是**不诚实的对比**
> （文件数 vs 文件数，但你的 13 个文件里有 132 个测试项）。且完全遗漏了原生 E2E 与三端夹具。

| | Agent | cc-haha |
|---|---|---|
| 测试 | **132 项**金测试（FakeEngine，无模型毫秒级）+ **20 项**原生 E2E + **6 组**三端协议夹具 | 123 个测试文件（项数未数） |
| 测试的**性质** | 金测试守护**护栏不变量**；夹具是**三端可执行契约**（改协议先改夹具，铁律 5） | 常规单测为主，无跨端契约测试 |
| 质量门 | ❌ | `quality-gate/`（coverage/quarantine/provider-smoke/desktop-smoke/persistence-upgrade） |
| CI | ❌ | `.github/` + 完整 PR 检查链 |
| git hooks | ❌ | `scripts/git-hooks/install.ts` |
| 文档站 | md 文件 | VitePress 站点 + 71 篇 + 架构图 8 张 |
| 类型 | Python 无强制 | TS strict + zod 运行时校验 |

**这一栏基本不是差距。** 你的 132 项金测试 + 20 项 E2E + 6 组夹具，对一个单人项目而言是**过量**的严谨，
而且**质量高于数量**：cc-haha 没有任何等价于「三端共享夹具」的东西——它靠同一份 web 代码回避了这个问题。

真正的规模差只在 CI/质量门/文档站，**且大部分不该追**（`check:pr` 那套 12 个检查是给 1,223 次提交的
开源社区用的）。两条便宜且有用：**金测试固化成 pre-commit hook**；**架构图**（它有 8 张，
你 `ARCHITECTURE.md` 的图还停在四组件、缺 inference-server）。

**顺带发现的文档漂移**（`CHARTER.md` 与实测不符，建议顺手校正）：
- `CHARTER.md:167` 记「mcp/（20 工具）+ agents/（4 沙箱）」→ 实为 **17 文件 / 34 类 + 3 沙箱**
- `CHARTER.md:239` 记「122 项金测试」→ 实为 **132 项**（涨了，正常漂移）

---

## §3 你有而它没有的

不列这一节的对比是不诚实的。

1. **算力分离拓扑**（`HETERO_COMPUTE.md` + `inference/` + `backends/remote/` + `devctl --profile`）——
   cc-haha 无对应物，它的算力在 Anthropic 机房。你的 `InferenceBackend` 协议 + edge/gpu profile
   是一套完整的自持异构算力方案。
2. **小模型编排护栏**——`goal_verify`（防虚报完成，有 A/B 实测依据）、`react_max_steps`、`replan_max`、
   `max_parse_retries`。cc-haha 假设模型聪明，不需要这些。
3. **工具级超时覆盖**（`timeout_seconds`）——它没有。
4. **压缩的写屏障语义**（§2.5）——它没有。
5. **沙箱威胁模型论证**——它的 subagent 没有这层，因为不需要。
6. **注释质量**——你的注释几乎全在讲 **why**（为什么这样设计、什么情况下会错、依据是什么实测），
   cc-haha 的注释多在讲 what。这是罕见的好习惯，**是这个项目最被低估的资产**。
   例：`compactor.py:1-30`、`research_sandbox.py:6-15`、`config.yaml` 的 `agent` 段。
7. **执行账本文化**（`HETERO_COMPUTE.md` 的 §R/§D/§S 结构、踩坑入账、决策不重议）——
   cc-haha 有 `docs/reference/fixes.md` 但远没有这种可再入的严谨度。

---

## §4 差距分类（决定该追什么）

| 类别 | 内容 | 该不该追 |
|---|---|---|
| **约束差** | 无原生 tool_use、KV 成本、4bit 小模型 → 需要 parser/constrained/护栏 | ❌ 不是差距，是你的问题域 |
| **规模差** | 工具数、测试数、文档数、压缩策略数、权限规则引擎、插件市场、IM 适配 | ❌ 一个人不追数量 |
| **形态差** | Electron 桌面端 vs Web、终端 UI | ⚠️ 属 `DEFERRED.md` 客户端形态决策 |
| **架构差** | **① 无 Skill 体系 ② 子 Agent 各写各的 ③ 工具契约缺渲染格 ④ 无声明式扩展点** | ✅ **只有这四条值得追** |

---

## §5 改进建议（按 ROI 排序）

> 全部为**建议**，不是计划。按 `CHARTER.md` 与账本 §D 纪律，架构方向变更需你明确同意后才可开工。

### R1 · 子 Agent 统一执行器（ROI 最高）

**做什么**：抽出 `agents/sandbox_runtime.py`——一份 `_EphemeralSession`、一份 ReAct 循环、
一份收尾。三个沙箱退化为**声明**：system prompt + tools + max_steps + 收尾策略。

**为什么先做**：① 消除三份拷贝，是纯粹的负债清偿；② 加第四个沙箱的成本从「抄 300 行」降到「写一份声明」；
③ 它是 R2 的地基（skill 需要一个统一的执行器来跑）。

**红线**：`research_sandbox.py:6-15` 的三条隔离性质（工具物理受限 / KV 隔离 / 不死锁）
必须提升为执行器的**显式不变量**并写进 gold test，不能在抽象化中蒸发。

**成本**：中。三个沙箱有真实差异（excel 有 INSPECT→PLAN→WRITE→VERIFY 状态机，
math 有解释器回填），抽象要留出 hook，别硬压成一个模子。

### R2 · Skill 体系（ROI 次高，填最大的空档）

**做什么**：`skills/*.md`（frontmatter: name/description/tools/max_steps）+ 一个 `skill` 工具
+ 目录扫描加载（复用 `MCPRegistry` 的模式）。跑在 R1 的统一执行器上。

**为什么**：现在焊死在 Python 里的那些流程串（excel 的四段式、research 的六条规则）
本质就是 skill。抽出来后它们变成可改、可加、可被模型发现的数据。

**先别做的**：skill 的自动发现（`DiscoverSkillsTool`）、skill 市场、MCP skill。先做能跑的最小闭环。

**⚠️ 小模型注意**：cc-haha 的 skill 靠模型自主选择调用。gemma-4-e4b 在 skill 数量上去后
大概率选不准——建议**从显式调用起步**（用户/编排层指定），别一上来就交给模型。
这条得实测，别照抄。

### R3 · 工具契约加渲染格（治「散乱感」的正解）

**做什么**：`BaseTool` 加一个可选的 `render_hint` —— 工具**声明**自己的结果该怎么渲染
（`type: chart|table|slides|code|text|diff` + 结构化 payload），前端按 hint 分发到对应组件。

**为什么不照抄 cc-haha**（2026-07-16 修正后此条更强）：它的工具**直接返回 React 组件**
（`renderToolResultMessage(): React.ReactNode`），因为它的工具和 UI 在**同一进程、同一语言**。
你是 Python backend + **TS web + Swift 原生**——三端、两种客户端语言、跨进程。
「工具返回组件」在你这里**物理上不可能**，而且会同时违反铁律 4（客户端必须是瘦渲染器）。
声明式 hint 是同一目标在三端架构下的**唯一**正确形态：hint 是**账本里的数据**，
三端各自投影——这恰好就是铁律 4 要求的形状。

⚠️ **这是协议变更，受铁律 5 约束**：先落 `protocol/fixtures/` 夹具 → 更新 `PROTOCOL.md`
（新增字段不升版本）→ 三端实现 → 夹具回放全绿。成本比原估的高（要写 Swift 那一份），
但没有捷径。

**收益**：`render_chart` 出图、`deck_tools` 出预览、`rag_search` 出召回列表、`shell_exec` 出终端块——
17 个工具不再长一个样。这是你「体验感散乱」感受里**最能立刻见效**的一条。

**成本**：中（要动 base.py + 前端分发 + 逐个工具补 hint），但可**增量**——不声明 hint 的工具走现有灰框，
零破坏迁移。

### R4 · 文件式长期记忆（补正交的那一层）

**做什么**：`memory/` 目录 + `MEMORY.md` 索引 + 单事实 md 文件，Agent 主动撰写；
召回用 RAG（你已经有向量库了，比 cc-haha 的「LLM 扫 header 选 5 条」更便宜也更快）。

**直接借鉴**：`cc-haha/src/memdir/memoryTypes.ts` 的四类型分类学（user/feedback/project/reference）
和「不该存什么」清单（能从代码/git 推导的一律不存）。**借鉴 prompt 设计，不借鉴代码。**

**你的优势**：cc-haha 只能靠 Sonnet 扫 header 做语义选择（一次额外 API 调用）。
你有真向量库，`rag_search` 直接召回，**这一层你的地基比它好**。

**排在 R3 后**的原因：它是「更聪明」，前三条是「更工整」。你的自评是散乱，先治散乱。

### R5 · 审批可改写参数

`needs_approval(args) -> bool` 升级为返回 `(decision, updated_args)`。便宜，价值明确（§2.6）。

### R6 · 杂项

- `mcp/` 更名或在 README 注明它不是 MCP（§2.1 注）——现在这个名字会挡住将来接真 MCP
- `ARCHITECTURE.md` 补 inference-server（仍写「四个组件」）+ 补架构图（cc-haha 有 8 张）
- `useChatStore.ts` 1,094 行的职责审计（§2.7）
- gold tests 固化成 pre-commit hook

---

## §6 明确不建议做的

| 不做 | 理由 |
|---|---|
| 权限规则引擎（26 文件那套） | 多用户/企业策略的产物；你是单用户自持。`CHARTER.md` 非目标 |
| 插件市场 / plugin 系统 | 生态产物，无生态则是纯负债 |
| 214 个斜杠命令 | 数量差，不是架构差 |
| IM 适配器 ×5 | 你有 APNs，够了 |
| 追工具数量到 60 | 同上 |
| 质量门 12 件套 | 1,223 提交的开源社区才需要；你需要的是 gold test + hook |
| **照抄 `renderToolResultMessage`** | 跨语言跨进程，物理上抄不了（见 R3） |
| **照抄 skill 的模型自主发现** | 前沿模型的假设，e4b 上大概率不成立（见 R2） |

---

## §7 一句话总结

你的直觉「核心框架有条理但整体散乱」**是准确的自我诊断**，而且比大多数人的自评准。
但「散乱」的成因不是你以为的「不够努力/不够成熟」，是**四个结构性空档**：
没有 Skill 层（工具与子 Agent 之间的空档）、子 Agent 没有统一执行器（N 份实例 vs 1 份体系）、
工具契约没有渲染格（导致 17 个工具长一个样）、只有一个声明式扩展点。
补上这四处，**在不增加一行「功能」的前提下**，整个项目的「工整感」会跨一个台阶。

而你和 Claude Code 之间那 20 倍的代码量差距，**其中 19 倍不必追，也不该追**。

---

*生成：2026-07-16 ｜ 依据：两侧源码实读 + 行数实测。cc-haha 侧 HEAD `eddaec54`。*
