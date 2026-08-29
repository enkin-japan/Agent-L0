# 开源 Agent 项目全量扫描 × Agent-L0 融合评估

> **日期**：2026-08-17
> **对象**：`/Volumes/enkin/project/opensource/` 下 7 个开源项目，以 `Agent-L0/` 为核心做融合评估
> **方法**：15 路并行深读侦察（真读源码，非读 README）→ 7 路交叉匹配 → 7 路**对抗性审查**（默认立场：杀掉每条建议）→ 1 路完备性批评
> **规模**：31 个代理、3.8M subagent tokens、1699 次工具调用
> **产出**：34 条候选建议 → **19 条存活 / 15 条被杀**，另发现 **5 处 Agent-L0 现存确认缺陷**（§6）
> **体例**：沿用 `Agent-L0/evaluations/*.md` 房型 + `COMPARE_CC_HAHA.md` 的三类差距判据
> **纪律**：全程只读。除本文件外未新建、未修改任何项目内的任何文件。

---

## 结论先行

**四句话：**

1. **Agent-L0 在「agent 本体架构」上已经不落后于这七个项目里的任何一个。** 它的事件账本 + 轮次唯一属主 + 十余种确定性护栏 + 三层有界记忆 + 物理隔离沙箱，面对 deepseek-harness（DeepSeek 官方，237 包 / 52 万行 / 12293 提交）是**同级不同形**；面对 prime-agent、TencentDB-Agent-Memory 是**明显更严谨**。在数据流向治理（铁律 6 出网分权 + L0 脱敏 + 台账）上，它领先这七个项目一个身位——那三个大项目的遥测分别是硬编码端点、默认开启、或默认无鉴权。

2. **它真正的落差只有一处，而且是同一处的三个面：纪律有素材、没有执行器。** 958 项 CPU-only 金测试、双端夹具、变异思维、预测先行、证据等级四标注——七个项目里**没有一个的方法论素材比它更全**；但仓库里没有 `.github/`、没有 `.husky/`、没有 pre-commit、`.claude/` 下连 `settings.json` 都没有、`devctl.sh` 的分派表里没有 `check`。七个项目里有五个独立造了机械执行点（形态互不相同 = 收敛而非模仿）。**这是本次全部对比中最大的单点落差。**

3. **对抗审查的价值大于建议本身。** 15 条被杀掉的建议里，有 4 条死于「实测证伪」——审查员真的跑了代码：把 addyosmani 的 TF-IDF 路由算法原样跑在 Agent-L0 真实的 60 件工具描述上（碰撞 0 对、中文查询得分全 0、英文信号反向）；用只读 SQL 比对真数据根的 FTS 与 Chroma（孤儿数 0/11，剖面里那条「孤儿单调增长」是错的）；实量剧本索引体量（219 字符 ≈ 54 token，占稳定前缀 0.48%，「推爆 12k 闸门」的因果链在实数上不成立）。**这些是「看起来很合理但事实上不成立」的典型，靠读代码是发现不了的。**

4. **最值钱的开源项目不是最像它的那个。** 排序：`koala73/worldmonitor`（给 AI 代理用的工程约束系统，4 条存活）、`PrimeIntellect-ai/prime-agent`（长跑 agent 的持久层与崩溃一致性，4/4 全存活，唯一零死亡）、`deepseek-harness`（KV cache 纪律与取消语义，4 条存活）。而 `TencentDB-Agent-Memory` 虽然与记忆子系统最直接可比，5 条只活 3 条——因为 Agent-L0 在这一层已经做过更克制的取舍。

---

## §0 方法、判据与局限

### 0.1 三类差距（本文全部取舍的判据）

沿用 `COMPARE_CC_HAHA.md` §1.2 的框架。**只有架构差才配进建议清单**：

| 类别 | 定义 | 本次典型例子 | 处置 |
|---|---|---|---|
| **约束差** | 两边物理约束不同，照抄反而错 | prime-agent 的「ipython 是唯一工具」——它跑云端前沿模型；Agent-L0 的约束采样 / 单点审批闸 / 物理隔离沙箱三层护栏**全部**建立在「工具调用是结构化的」之上，换 REPL = 同时拆掉三道护栏 | 记录理由，不做 |
| **规模差** | 纯数量差（237 包 / 5700 提交 / 20 人月） | deepseek 的 145 个门禁脚本、worldmonitor 的 34 个 workflow、google 的 111 份技能 | 不追。**但其中的单条机制可能仍是架构差**，需逐条拆开 |
| **架构差** | 真正的结构性缺口 | 机械执行点、工具执行不可中断、观察截断无句柄、事件账本 seq 回退 | 进建议清单 |

### 0.2 对抗性审查：五种杀法

每条候选建议都过了一道**默认立场为「杀掉它」**的审查，任一成立即判死：

1. **已经有了**——Agent-L0 已实现等价物（用 Grep/Read 在真实代码里实证，**不信剖面**）；
2. **撞铁律或非目标**——比照 `CHARTER.md` 铁律 1–6、§3 原则、§7 非目标逐条核对；
3. **是约束差 / 规模差伪装成架构差**；
4. **接缝不成立**——落点不存在、那里改不动（撞铁律 1）、或会破坏某条金测试守着的不变量；
5. **收益不实**——它解决的问题在 Agent-L0 的实际形态（单用户 / 单机 / 单 worker / 无外部贡献者）下不会发生。

**审查的实际效果**：34 条候选 → 19 条存活（56%），且存活的 19 条里有 **17 条被要求修正落地方案**——包括「接缝选错」「验收计划测不出东西」「威胁模型描述有误」「成本估低/估高」「引证不实」等。§3 的每一条都已并入这些修正。

### 0.3 Agent-L0 现状基线（本次复核的实测值，非引用）

| 维度 | 实测值 | 复核方式 |
|---|---|---|
| 金测试 | 86 个测试文件 / **825 个 `def test_` 函数**（账本记 958 项 = pytest 收集口径，含 11 个文件的 parametrize 展开） | `ls backend/tests/*.py \| wc -l` + 正则计数 |
| 协议夹具 | **7 组**（001–007），三端共享回放 | `ls protocol/fixtures/` |
| 工具面 | **30 个工具文件 / 60 个 `BaseTool` 子类** + 4 个编排层元工具 | AST 抽取 |
| 稳定前缀（full 模式） | **≈ 45348 chars ≈ 11337 est token**，距 D4 拍板的 12000 线**只剩 663 token ≈ 2–3 件中等工具** | 按 `orchestrator.py:322` 同口径静态量 |
| 剧本 | **5 份**，description 长度 43–46 字符，索引合计 **219 chars ≈ 54 est token** | 逐份量 |
| 渲染格 | 协议声明 7 种 hint，**实际只有 1 个工具产出**（`rag_search` 的 `citations`） | `grep -rn render_hint` |
| **CI / hooks / 机械执行点** | **零**——无 `.github/`、无 `.husky/`、无 `lefthook`、`.git/hooks` 下零个非 sample、`.claude/` 下只有 `agents/` | `ls -a` |
| 子代理编制 | 5 名，各自 frontmatter 钉死 model + effort | `ls .claude/agents/` |
| 评测框架 | 4 套 + `benchmarks/predictions/` 8 份预测先行文档；**其中 3 套的 `main()` 无条件 `return 0`** | 逐份读 |

### 0.4 局限（诚实声明）

- **未运行任何一个开源项目的代码**。结论基于源码阅读、文档与 git 历史，与 `evaluations/` 既有五份同一口径。
- worldmonitor（115 万行）与 deepseek-harness（52 万行）超出单次深读的穷尽范围，各以两路代理分工覆盖，仍属抽样。
- TencentDB-Agent-Memory 的公开仓库是内部仓压缩镜像（17 个 commit、零测试、被引用的设计文档全部不在仓库里），其真实工程质量无法从公开面判断。
- 本文不含任何 Agent-L0 的性能/质量实测新数据，涉及处一律引用其自身账本已有的值并标出处。

---

## §1 七个项目逐一体检

> 每个项目四段：**是什么 / 核心价值资产 / 优点 / 缺点**，末行给出**与 Agent-L0 的关系判定**。
> 「核心价值资产」的判据是：**它经得起一次彻底重写吗？** 重写后仍成立的才是资产。

---

### 1.1 deepseek-harness（DeepSeek 官方）

**是什么**：DeepSeek 官方开源的「一切皆插件」编码 agent harness（`dsh`）。基于 vendored Cordis 框架，237 个 pnpm workspace 包、约 52.7 万行 TypeScript、12293 次提交（全集中在 2026 年 6–8 月）。产品的「默认行为」不是代码，而是一份 451 行的 `cordis.patch.yml`。MIT，0.1.0-rc.5，**明确不接受外部 PR**。

**架构骨架**：vendored 框架层 → `cordis.patch.yml` 组合层 → `packages/core/{session,tools,agent-loop,scope}` 核心 spine → 十余个「能力接缝」（每个强制拆成 Service Definition / Provider / Consumer 三包）。全仓**唯一含循环逻辑的包**是 `agent-loop`，仅 1647 行。

| 核心价值资产 | 为什么经得起重写 | 位置 |
|---|---|---|
| **turn/step 双层循环 + 双 FIFO 收件箱** | 把「用户中途插话」从一堆特判归一成 `send(target × wakeup)` 的 2×2 路由表；待处理输入本身是可持久化状态，resume 后不丢 | `core/agent-loop/src/agent.ts:113-132` |
| **「模型可见 ⟺ 已记录」不变量** | 任何进入模型请求的内容必须能从日志重建；压缩是**影子替换**而非删除。重放 / fork / 审计 / 快照测试 / UI 回放共用一个真相源 | `core/session/src/surface.ts`、`agent-loop/src/invariant.ts` |
| **工具执行四阶段调度协议** | prepare / dispatch / finalize / finish 经 private symbol 暴露给调度器，使「有序策略 + 可重叠执行 + 模型序提交」同时成立 | `core/tools/src/index.ts:451-466, 1459-1646` |
| **取消不 abandon promise，只重标结果** | JS 杀不掉同进程代码。`fuseToolSignals` 融合信号、body 结算后按 signal 状态改判，用 `bodyInvoked` 区分「已启动被中止」与 `ABORTED_BEFORE_DISPATCH`；未派发的调用**补写合成 call/result 配对**保证日志可重放 | `core/tools/src/index.ts:1509-1560, 1889-1918` |
| **`RuntimeContextProjection`：可变事实不进 system prompt** | 时间 / cwd / 沙箱模式渲染成快照，与上次仍在 surface 上的同源快照比较，**相同则不写**。历史前缀永不改写 ⇒ provider 前缀缓存单调增长 | `core/agent-loop/src/runtime-context.ts:24-76` |
| **压缩作为日志内 ACID 事务** | start（同步落盘即取锁）→ summary（log-only）→ 唯一一条位置式 replace → end。崩溃留下**可检测的孤儿锁**，而不是谎称完成的 end | `packages/compaction/compaction/README.md` |
| **spill：超大输出落盘 + 不透明定位符 + retrievalHint** | 超阈值（默认 50000 字节）全文交给 spill 后端存会话私有目录，模型可见结果换成截断尾部 + 定位符 | `packages/spill/{spill,spill-local,spill-policy}` |
| **`.agents/notes` 决策记录制度（688 活跃 + 143 冻结）** | 路径即元数据、新 Note 触发取代检查、归档树内容封印且写进 `.rgignore`——**把冻结历史排除出 agent 的默认搜索面** | `.agents/notes/` + 5 个 `verify-*.ts` |
| **每包 README 的 Model Experience 契约** | 强制三段：模型看到什么 / token 影响 / **KV cache 影响**；豁免必须带书面审计理由 | `scripts/verify-package-readme-model-experience.ts` |
| **llm-replay：持久化 session.jsonl 就是 fixture** | 录制=真跑一次留日志；回放=按 (turn, step) 重组 chunk，**无 API key 即可跑完整装配后的应用**并逐字节比对模型可见输出 | `packages/test-support/llm-replay/` |
| **ACP 快照的三态 + 请求头钉选** | record（花真钱）/ replay（无密钥默认）/ **refresh**（用已提交脚本重生成期望值——「模型响应没变但渲染变了」的答案）；每个 header class 只由一个场景钉住完整内容，改一次提示词只 churn 一行 | `packages/test-support/acp-snapshot/` |
| **文档词数预算 + 处理顺序被规定死** | `doc-budgets.manifest.json` 给常备文档设 `wc -w` 上限；红灯时**先搬迁 → 再压缩 → 最后才抬上限（须论证）**；并写明「上限是护栏不是削减目标，留 5% 余量，过低的上限是 budget bug」 | `scripts/verify-doc-budgets.ts` |

**优点**

| 点 | 证据 |
|---|---|
| 唯一的循环实现被压缩到 1647 行并被制度化保护 | `agent-loop/README.md:7` 自陈；`AGENTS.md` 规定改它必须同时更新 `docs/architecture.md` |
| **KV cache 与 token 成本是一等设计约束** | 每包 README 强制三段式并有门禁；压缩摘要逐字重放会话自身前缀以复用缓存；plan-mode 提示明写「工具目录跨模式不变以保持请求缓存稳定」 |
| 取消 / 并发 / 生命周期边界条件被穷举建模 | wake latch（中止后到达的唤醒被 latch 到收敛点重放）；中止排空后补写合成结果对 |
| 安全边界诚实而不夸大 | `sandbox-local` 在 Windows/老内核主动报 `enforcement:'partial'`；`code-runtime-worker-thread` 直言「containment, not a security boundary」；无可用后端时抛 `SANDBOX_UNAVAILABLE` **拒绝无约束运行** |
| 门禁本身是被测代码 | `scripts/` 145 个文件里 44 个是 `*.spec.ts`；`run-gates.ts` 把 14 个命名聚合建成一张受测依赖图，本地与 CI 同一份 |
| CI 故障切换是每次 push 都跑的演练 | `runs-on` 读仓库变量切自建池；`cancel-in-progress` 写成 `github.event_name != 'push'` 以免取消让演练永远得不出结论 |

**缺点**

| 点 | 证据 |
|---|---|
| 对 vendored Cordis 的深度耦合是**不可逆的架构赌注** | `vendor/` 拷了 8 个上游包约 6550 行需手工 resync；`ctx.effect/waterfall/fiber` 渗透进 237 个包每一行 |
| 体量与流程开销对任何小团队不可复制 | 「加一个工具」要触及 schema / presentation / invariant / README 四段式 / catalog 生成 / 快照测试 |
| 逐文件 100% 覆盖率门禁把信号稀释成 ignore 注释 | 源码 554 处 `/* v8 ignore */` + 135 处 `oxlint-disable`，许多挂在真实防御分支上 |
| **遥测端点硬编码进出厂 bundle 且无脱敏规则** | `cordis.patch.yml:154` 写死 `harness-telemetry.deepseeksvc.com`；官方自陈「exports can contain message text, tool arguments and results, and workspace paths」；**关闭只能靠环境变量，config 关不掉** |
| **BENCHMARK.md 是空壳：完全没有 agent 能力评测** | 全文 3 行；全仓 grep 无 SWE-bench / pass@k / eval harness。提示词改动只被 transcript 逐字节 diff 拦住，**模型任务成功率的回归没有任何门禁** |
| `cordis.yml` 允许 `!!js` 表达式执行 | 已造成一次「文件系统工具被静默禁用」事故；第三方 profile 同样能带 `!!js`，构成供应链执行面 |
| 开源但不收外部 PR，社区回路是断的 | `CONTRIBUTING.md` 明写；12293 次提交全部来自内部分支 |

**与 Agent-L0 的关系判定**：**同类异形**。它是编码 agent + TS 单进程 + 云端前沿模型；Agent-L0 是桌面任务 agent + Python 多进程 + 本地 4bit 模型。**插件化体系整体属规模差与约束差，一条都不该抄**（Code Mode 尤其致命——它会当场作废 Agent-L0 的约束采样 + `tool_call_grammar` 这条承重线）。真正值得逐条对照的是它在「取消语义」「KV cache 纪律」「文档即门禁」三处的做法。**4 条建议存活。**

---

### 1.2 koala73/worldmonitor

**是什么**：表面是实时全球情报看板，TS 单体仓库 + Vercel Edge + Railway 49 个播种服务 + Upstash Redis + Convex，约 115 万行、5711 次提交、7 个月、**单人 + AI 代理**（5152 次提交来自同一人，1144 次带 `Claude-Session:` 回链，349 个不同会话）。AGPL-3.0-only，82.3k stars。

**但它真正的研究价值不是产品，是它为「AI 代理写代码」造出来的一整套可执行工程约束系统。**

| 核心价值资产 | 为什么经得起重写 | 位置 |
|---|---|---|
| **`docs/solutions/` 事故学习语料库（82 篇）** | 文件名即结论句、frontmatter 带可 grep 的 `symptoms`、正文固定含 **What Didn't Work** 段（保存负面知识）。26 处源码注释反向引用 | `docs/solutions/`（16 个分类） |
| **`CONCEPTS.md` 失败模式词汇表（127 条 / 85KB）** | 把资深直觉编译成可寻址符号：**Vacuous Guard**（断言否定式的守卫在输入静默缩小时反而更绿，列九种缩小杠杆）、**Mutation Proof**、**Surviving Mutant**、**Ratchet Inventory**、**The Lever Test**、**Content-Age Contract**、**Activation Marker**、**Read Outcome** | `CONCEPTS.md:163-193` |
| **`agent:preflight` 单一 JSON 开工闸门** | 一条命令输出 `{status: ready\|blocked, expensiveTestsAllowed, ...}`，含**重复 PR 检测**与 **worktree 撞车检测**；逃生开关只**记录例外**、不修复状态 | `scripts/agent-preflight.mjs`（826 行） |
| **不可信外部文本的物理隔离** | `agent:pr-snapshot` 默认响应里评审正文**被剥掉**，读它要显式加 flag 且带 `trust:'untrusted'` 标签；刷新只在三个相位名下合法 | `scripts/agent-pr-snapshot.mjs:379-382` |
| **绿树证明缓存的「拒绝签字」契约** | 缓存记 `HEAD^{tree}`，但只有当门确实跑在那棵树上时才允许写；工作区与 HEAD 非逐字节相同、或走了降级分支，都**拒绝签字**（拒绝不算失败） | `.husky/pre-push:258-310` |
| **判定外提成三值退出码 + stub PATH 跑真 hook** | `0=是 / 3=否 / 2=用法错 / 1=内部错`——**「门说不」和「门跑不起来」绝不能与「门说是」同码** | `scripts/prepush-attest.sh` + 1409 行测试 |
| **AGENTS.md 的授权模型与终态词表** | read-only vs delivery 两种模式；「交付权不含合并权」；五个互不等价的终态 | `AGENTS.md:7-13, 87-95` |
| **双向棘轮清单（Ratchet Inventory）** | 按 (文件, 写法) 记剩余次数，**计数变高 = 新漂移失败，计数变低 = 记录陈旧也失败** | `CONCEPTS.md:189-193` |
| **文档数字从注册表派生，不允许手写** | 纯文本解析、不执行 TS、不需依赖；产物**故意 gitignore**（提交了就会有人手改）；CI 里 `--check` 始终运行 | `scripts/docs-stats.mjs`（1972 行） |
| **Activation Marker** | 生产者首次真正成功后写一个**无过期**的持久标记；标记出现前缺失是软失败，出现后同样的缺失永远是硬失败。替代「硬编码宽限截止日期」这个反模式 | `CONCEPTS.md` + `api/health.js` |
| **Read Outcome 三态（hit / miss / failure）** | 判别联合而非裸值。miss 渲染空态，failure 一律弃权。堵死「故障被降级成一个自信的空答案然后作为数据继续传播」 | `server/_shared/redis.ts:62` |

**优点**

| 点 | 证据 |
|---|---|
| 注释即事故档案 | `api/health.js:543` 记 `maxStaleMin` 从 240 改 540 的完整原因；`_seed-utils.mjs:533-545` 记 2026-05-10 一次超时导致断档 3 小时 |
| **把代理会话记录当工程指标来调门的成本** | `.husky/pre-push:127-133`：「2026-07 velocity audit，session-transcript mining 显示 **43% 的会话在跟这道门搏斗**」，随后引入按路径裁剪 + 绿树缓存 |
| 学习闭环真实存在 | 事故 → solution 文档 → 概念词条 → 源码回链 → 守卫，五环齐全 |
| 元测试投入极大 | 为一个 git hook 写了 1409 行测试，用 stub PATH 跑真 hook |
| CI 必需集合有**版本契约** | 20 个必需 job 名单取 SHA256 前 12 位作戳；名单一改，旧「全绿」自动失效 |
| 提交与会话双向可追溯 | trailer 带会话永久链接 + 精确到版本的 co-author，可回答「这个 bug 是哪代模型引入的」 |

**缺点**

| 点 | 证据 |
|---|---|
| **最核心的代理配置不在仓库里** | `.claude/` `.agents/` `.factory/` `skills/` 全部 gitignore；solution 文档引用 `~/.claude/skills/...` 仓库外文件。**这套实践外部无法复现** |
| 自我评估的「代理就绪度」路线图已严重腐烂 | 仍写「Last updated 2026-03-14 / readiness ~25%」，其「Agent-to-agent review loops: None 0/10」与已配置的 5 个评审 agent 直接矛盾 |
| 巨型文件未治理，且是路线图自己点名的 P0 | `ais-relay.cjs` 12812 行、`seed-forecasts.mjs` 19254 行、`.env.example` 1165 行 |
| **门的复杂度本身已成新失效面** | 726 行 bash + 200 行 attest + 1409 行测试；已有专门事故文档记录这道门自己的三个静默失效 |
| 巴士系数 = 1 | 5711 次提交中 5152 次来自同一人 |
| lint 契约被大量关闭，与自我叙述不符 | biome formatter 完全禁用、`noExplicitAny`/`noExcessiveCognitiveComplexity` 全关；markdownlint 只启用 3 条且排除最活跃的文档目录 |
| AGPL-3.0-only 是复用陷阱 | 借用脚本进闭源网络服务会触发网络 copyleft |

**与 Agent-L0 的关系判定**：**产品面无关，方法面正面对标**。「一个人 + AI 代理维护一个大型代码库」这个命题，两者是同一问题域的两个独立解——Agent-L0 靠宪章 + 账本 + 5 名钉死档位的子代理 + 三层评审门（**全是散文纪律**），worldmonitor 靠 preflight + 726 行 pre-push + 82 篇事故语料 + 概念词汇表（**全是可执行门禁**）。互补性极强。**4 条建议存活**（含 2 条被实测证伪判死）。

---

### 1.3 PrimeIntellect-ai/prime-agent

**是什么**：Prime Intellect 基于 Mario Zechner 的 `pi` 分叉重做的开源编码/研究 agent 运行时。TS monorepo（约 20 万行源码 + **12.7 万行测试**），核心命题是 **RLM**：模型只有一个工具 `ipython`，在长生命周期 REPL 里写 Python。MIT，0.7.2，4511 次提交。

| 核心价值资产 | 为什么经得起重写 | 位置 |
|---|---|---|
| **`host.request`：REPL 到宿主的类型化反向 RPC** | 回复**必须走 control 通道**——IPython 串行处理 shell 消息，正在执行的 cell 等待 shell 回复会死锁；`type` 放在 payload 之后展开以防键劫持 | `rlm/__init__.py:53-140` |
| **RLM spawn ledger：append-only 拓扑权威** | 「谁是谁的父/子/深度/名字」收敛成 per-namespace JSONL，只有三种 op，重放按 (id, path) LWW。**读取时显式剥离其他文件里「自称」的拓扑字段** | `modes/daemon/rlm-ledger.ts`（830 行，测试 1050 行） |
| **字节级 torn-tail 修复 + 双读稳定性检查** | 在**原始 Buffer 的字节偏移**上工作（多字节 UTF-8 会让 string index 与 byte offset 分叉，旧实现会切进前一条有效记录）；两次读取字节相同 + fstat 尺寸未变才 ftruncate | `rlm-ledger.ts:718-770` |
| **无覆盖式一次性迁移（seeding）** | 写 temp → `linkSync` 发布；EEXIST 即丢弃 seed（因为删除记录只存在于 ledger 中，任何 clobber 窗口都会复活已删边）。**绝不用 existsSync + rename** | `rlm-ledger.ts:595-716` |
| **「durable 记录成功才准入」的资源准入协议** | 子 agent 准入时先发起 ledger append 并 await 它，append 失败即准入失败——「一个 swallow 掉的失败会准入一个永远找不到的孩子」 | `daemon-mode.ts:1031-1070` |
| **Continual Harness** | 四类条目（prompt/memory/skill/subagent）+ scope + version + before/after 回滚 + **独立审核门 prompt**；硬约束：base system prompt 不可变。`_sync_from_disk()` 用 `st_mtime_ns` 检测跨进程写入 | `rlm/harness.py`、`refinement.ts:123-205` |
| **claim-then-advance 调度语义** | 到期任务先「认领并推进 nextRunAt」再投递 prompt；崩溃不会重放结果不确定的 prompt；错过的 tick **合并**而非堆积 | `core/cron-jobs.ts:1574-1597` |
| **子 agent 用量归因回父轮** | 用量折叠进「发起它的那条父 assistant 消息」并持久化一条 `child_usage_attributed`；报告时**减去已归因的子用量**——把「计费聚合」与「上下文窗口占用」显式分开 | `docs/rlm-runtime.md`、`core/context-tree.ts` |
| **daemon 协议 v4：代际化事件游标 + 幂等命令日志** | 事件带 `{generation, sequence}`，supervisor 启动时 `generation = randomUUID()`；变更类命令按 `clientId + commandId` **先记后派发**，无 durable 结果的命令报「不确定」且**不重放** | `daemon-protocol.ts`（1187 行） |
| **install.sh 双层 SHA-256 校验** | 校验 agent 本体，**也校验它引导下载的 Node standalone**；安装器本身进 CI 检查 | `install.sh`（1620 行） |

**优点**：崩溃一致性极其扎实且**每个降级取舍都有注释写明原因**（还诚实标注「residual race stays documented」）；测试密度极高且分层（320 个测试文件 / 12.7 万行，`AGENTS.md:32` **明令禁止用真实 provider**）；文档与实现同级严谨（`docs/daemon.md` 166 行覆盖 lease/backpressure/幂等；README 反复强调「这不是安全沙箱」）；供应链有实际防护（依赖 7 天冷却、action 按 digest 钉住、外部贡献者 vouch 门禁）。

**缺点**：**完全没有内置的工具审批/权限系统**（模型生成的 Python 与 shell 以用户身份立即执行，唯一拦截点是扩展返回 `{block:true}`）；沙箱只是 example 扩展且它替换的是 `bash` 而默认工具是 `ipython`；单文件巨石（`agent-session.ts` 11288 行）；**遥测默认开启**（opt-out，上报 `api.primeintellect.ai`）；内核 venv 是**全局单例**且模型被提示可以 `uv pip install` 往里装包；「去中心化算力」的整合是文档级而非运行时级（`skills/prime-intellect/` 只有 markdown，无代码）；品牌迁移未完成（17 类 `PI_*` 遗留变量，4511 次提交中 3063 次来自上游作者）。

**与 Agent-L0 的关系判定**：**核心命题相冲，工程细节最可借**。RLM 对 Agent-L0 是纯约束差。但它把「长跑 agent 的进程与文件持久层」打磨到少见的密度，这一层与语言、模型档位、是否编码 agent 都无关，恰是 Agent-L0 同样承重却相对薄的一面。**这是七个项目里唯一一个 4 条建议全部存活、零死亡的。**

---

### 1.4 TencentCloud/TencentDB-Agent-Memory

**是什么**：腾讯云开源的「Agent 团队记忆中枢」。四个可独立部署的服务 + 两套 SDK，约 19.2 万行 TS。核心是 **L0 对话 → L1 原子记忆 → L2 场景 → L3 画像** 四层记忆模型，通过透明 LLM Proxy 让 Claude Code / Codex / CodeBuddy 等 agent **零改造接入**。MIT。

| 核心价值资产 | 为什么经得起重写 | 位置 |
|---|---|---|
| **L1/L2/L3 记忆 Prompt 语料库（chat 与 work 双套，约 76KB）** | 成体系的抽取类型定义、priority 打分 rubric（细到分数段）、「独立完整 / 归纳合并 / 宁缺毋滥」三原则、严格 JSON 输出协议 | `src/core/prompts/` |
| **记忆冲突消解协议：store / skip / update / merge** | 这是**记忆演化**语义而非去重：`target_ids` 是数组、`merged_type` 允许跨类型合并、`merged_timestamps` 必须是**并集去重排序**（保留事件时间线） | `prompts/l1-dedup.ts:16-70` |
| **统一候选池 + 单次批量判定** | 绝大多数系统是「逐条比对逐条调 LLM」；这里把所有候选去重合并成一个池，一次调用输出决策数组——**顺带解决同批次内部的互相重复** | `record/l1-dedup.ts:53-131` |
| **召回注入的稳定 / 动态二分（为 prompt cache 服务）** | persona + 场景索引 + 工具说明 → 挂 system 末尾（跨轮不变，吃 prompt caching）；本轮召回的 L1 → 挂 user message 前缀（每轮变，不污染 system 段缓存） | `hooks/auto-recall.ts:258-300` |
| **分层触发调度：指数预热 + 单调向前定时器** | L1 阈值从 1 起步按 1→2→4→8 指数增长；L2 定时器**只能提前不能推迟** | `utils/pipeline-manager.ts:1-78` |
| **L2 场景层：把 LLM 当沙箱内的文件编辑器** | 只给 read/write/edit 三工具；系统文件**物理不可见**；删除唯一方式是写 `[DELETED]` 软删标记；跨层通信走**带外文本信号** | `prompts/scene-extraction.ts` |
| **角色劫持防御** | 历史轮次改用 `<<past-user>>/<<past-assistant>>` **非自然标记** + `<<end-of-transcript>>` 锚点 + 明令忽略嵌套 harness 块 + 输出契约只允许两种精确形状。注释里留了真实事故的 trace id 与「产出 1235 tokens 续写、零 tool call」的现场记录 | `skill/prompts/skill-review-prompt.ts:39-70` |
| **自定义 Prompt 的护栏拼接** | **追加而非替换** + `priority="highest"` 的 guard 块 + 闭合标签转义。不到 45 行 | `memory-prompt/composer.ts:1-42` |
| **记忆生成溯源（存哈希不存正文）** | `memory_generation_refs` 映射到 prompt_id + version + **prompt_sha256**；明确不保存正文快照 | `store/sqlite.ts:983-1068` |
| **零向量库的知识检索：BM25 种子 + wikilink 图多跳衰减** | 每个知识库一个独立 SQLite + `graph_edge` 存有向边；BM25 出种子 → 分层 BFS 每跳乘 decay → 200 节点硬上限。注释直言是为根治 20GB OOM 而设计 | `MemoryKnowledge/engines/wiki/` |

**优点**：记忆的层次划分**不是标签而是真有不同的物理形态和读写路径**；Prompt 是一等工程资产（注释保留版本演进理由与评测数据）；降级路径写得非常彻底（无 embedding → BM25 → 跳过去重 → 结构化 RecallError → 全部 store）；宿主中立抽象干净；治理层（Owner/版本/可见性/ACL/审计/溯源）是一等公民；全本地可运行。

**缺点**（**七个里最重的，必须与优点等量齐观**）：**开源仓库里一行测试代码都没有**（8 个测试脚本都在，`vitest.config.ts` 的 include 指向不存在的目录，CI 不跑任何测试）；**被大量引用的设计文档全部不在仓库中**（`ls MemoryCore/docs` → 目录不存在）；**L1 的 prompt-injection 过滤被注释掉了**（`sanitize.ts:153`，而同处注释自己警告的正是「防止恶意内容被持久化并在未来召回时重新注入」）；Gateway 鉴权默认关闭而 Docker 默认绑 `0.0.0.0`；多个数千行巨型文件；Offload 子系统存在复制粘贴分叉（三对同名 prompt 文件哈希全不同且都在被引用）；腾讯云生态耦合深且不可插拔；公开仓库只有 17 个 commit。

**与 Agent-L0 的关系判定**：**同问题域、相反取舍、可精准取用**。它**在写入侧花大量 LLM 调用**，Agent-L0 明确否决了这条路（`evaluations/mem0.md`：本地推理成本不可接受）。这个否决在 e4b 时代是对的；生产机 M4 Max 64G + 26B/31B 到位后前提已变——但对抗审查判定：**即便如此也不该照抄**，因为桌面 agent 的会话形态是「每个短会话都是新会话」，指数预热在这里不是「冷启动快、稳定期省」而是**每个会话恒定多花一次本地生成**。**5 条只活 3 条。**

---

### 1.5 google/skills

**是什么**：Google 官方维护的 Agent Skills 内容仓库。**纯内容型，零运行时框架代码**：111 个经内部审核的 `SKILL.md` + 60 个技能带 `references/`（310 个 md）。Apache-2.0，18.4k stars，**不接受外部 PR**（Copybara 从内部 monorepo 单向导出，244 个 commit 里 209 个作者是机器人）。

| 核心价值资产 | 为什么经得起重写 | 位置 |
|---|---|---|
| **111 条 description 触发器语料** | 三段式：能力陈述 → `Use when …`（列具体用户措辞与错误码）→ `Don't use for …（改用 X 技能）`。**101 个含正向触发、80 个含显式否定触发并常点名替代技能** | 全仓 frontmatter |
| **硬性尺寸预算（顶着上限写）** | description max **1021**（限 1024）、name max **63**（限 64）且 111/111 与目录名完全一致、SKILL.md 词数 max **4445**（限 5000）。数字贴着上限说明它被当作**稀缺预算精细分配** | 边界样本 `gke-upgrades/SKILL.md` |
| **反幻觉的「唯一权威源」契约** | 声明「你对该 CLI 的全部先验知识都是 stale 且易幻觉的」+ 六条强制规则：执行前必须查 leaf 级 help；**被问「你的计划是什么」时计划第 1 步也必须是这条 help**；父命令组的 help 不具传递性；**FORBIDDEN Web Search Fallback** | `cloud/gcloud/SKILL.md:16-68` |
| **上下文预算写进领域规则** | 「DO NOT execute any list command without at least one data reduction flag」；不确定字段路径时先 `--limit=1` 探 schema | 同上 `:129-159` |
| **三级副作用分层（Tier R / M / B）** | R 只读 / M 花算力费用需交互确认且**一次授权后不再重复问** / B 创建资源必须停下等批准。配 denylist，**每条禁令都附风险理由**以便对清单外相似操作正确外推 | 同上 `:213-238` |
| **脚本-LLM-脚本三明治 + 忠实性校验器** | 校验器接收「输入中必须原样出现的子串」作参数，**断言的是模型没有篡改用户查询这一忠实性属性，而非语法合法性**；有界重试上限 2 次，仍失败就停下如实告知 | `cloud-monitoring-chart-generation/`（1910 行脚本 + 4 单测） |
| **渐进式披露的三种形态** | A 维度互斥 → 主文件 30 行「先判断，不清楚就问」；B 可多选 → 带说明的引用目录 + 兜底；C **模型有强先验会瞎猜** → `do not guess the configuration. Instead, read <file>` 并点名读文件工具 | 三个样本 |
| **「Proving your work」证据条款** | 「Never claim eval results you didn't read from an actual result object」；宣布成功前必须确认**全部**用例通过；拿不出证据必须明说 | `eval-flywheel/SKILL.md:381-403` |

**优点**：description 写作纪律全仓最一致；命名与索引零漂移（README 的 BEGIN/END 区块与磁盘 111 条路径精确一一对应，说明索引是生成物）；安全护栏是产品级；提交历史本身是一份技能调优日志（「重命名标志以减少 agent 混淆」「输出封顶 5 条防上下文溢出」）。

**缺点**：**仓库内没有任何 CI、schema 校验器或 lint**——纪律靠上游内部流程保证，外部 fork 无法复现；元数据模式不统一（`metadata.version` 只有 16/111 个有，格式混杂）；测试覆盖 <15% 且**有空壳**（4 个 `validate_queries.sh` 里 2 个只打印 "Validation skipped"）；提示词膨胀（306 处 MUST，单个 SKILL.md 可达 726 行）；对特定 harness 的内部工具名硬编码；**技能间存在跨仓死引用**。

**与 Agent-L0 的关系判定**：**纯语料资产，零架构参考**。但它的 111 条 description 是目前公开可得的、规模最大且经内部审核的「技能路由措辞」语料。**5 条只活 3 条**，且其中一条（非零退出码分语义）是本次唯一在 Agent-L0 代码里**复现出确定 bug** 的建议。

---

### 1.6 mattpocock/skills

**是什么**：「Skills For Real Engineers」。纯 Markdown 仓库：35 个 `SKILL.md`（25 个作为 Claude Code 官方插件发行），约 6800 行 markdown + 654 行辅助代码，**除 changesets 外没有构建、没有依赖、没有运行时**。MIT，437 commits / 6.5 个月，事实上的单人项目。

| 核心价值资产 | 为什么经得起重写 | 位置 |
|---|---|---|
| **`writing-for-agents`：给 agent 写文档的完整理论（81 行）** | 全库唯一一份即便所有技能被删光仍成立的资产。八个概念：**两种负载**（context load 每轮计费 / cognitive load 花在人身上）、**context pointer**、**completion criterion 的清晰度与要求量**、**leading word**、**negation 反模式**、**no-op 测试**、**sediment**、信息层级阶梯 | `productivity/writing-for-agents/SKILL.md` |
| **「cognitive load 不该最小化」这个反直觉立场** | 它是人保有能动性的价格，该花在人的判断真正重要的地方。这解释了为什么该库有大量 user-invoked 技能而不是把一切做成自动触发 | 同上 `:21-43` |
| **no-op 测试** | 逐句问「它相对**模型默认行为**改变了什么」。判据是**模型相对而非读者相对**——两人分歧本质是对默认行为的分歧，**靠跑一遍文档裁决而不是靠辩论**。不合格删整句而非修词 | 同上 `:76-82` |
| **invocation 二分 + 「没有技能能调用 user-invoked 技能」不变量** | user-invoked = 零 context load，只花人的记忆；model-invoked = 付 context load 换来可被任何技能调用 | `.agents/invocation.md` |
| **grilling：design tree + frontier（22 行）** | frontier = 前置条件已确定的决策集合。一轮把整个 frontier 问完，每题编号并**附推荐答案**。铁律：**找事实是 agent 的活，做决策是用户的活**。frontier 为空即结束（**二值化终止条件**） | `productivity/grilling/SKILL.md` |
| **`.out-of-scope/` 拒绝知识库** | 一个概念一个文件；分诊第一步强制通读并按**概念相似度而非关键词**匹配。负向规则：**只有被拒绝的 enhancement 才写，因为「已实现」而 wontfix 的绝不能写**（会用假拒绝污染去重） | `triage/OUT-OF-SCOPE.md` |
| **三类文档各承载什么，并用它 revert 越界提交** | SKILL.md 承载当前行为；docs 页承载 why 与定位；changeset 承载「什么变了以及为什么这么修」。三者不重叠，且这条边界被用来 revert 真实提交 | `.agents/writing-docs.md`、commit `da2cb7d` |
| **`CLAUDE.md` 只写不变量、细节推给 `.agents/`** | 始终加载的那份只有 **23 行**且每行都是承重不变量 | `CLAUDE.md` |

**优点**：方法论自洽——**库用自己的规则来修改自己**（commit `6a34259` 援引自家 no-op 测试删掉自己前一个 commit 刚加的五处理由句）；面向用户的文档**诚实记录失败模式与外部反驳**（写进了「没人对这个名字满意」「这是这个技能被报告最多的问题」以及对其核心卖点最尖锐的公开反驳）；技能极度精简且组合优先（grilling 22 行被五处复用）。

**缺点**：**完全没有测试、没有 lint、没有任何技能质量校验器**；**invocation 不变量的六处违规完全靠人肉发现，一个 20 行脚本就能防住**（把一条本来只是无效的散文**升级成了一条会可靠地朝错误方向开火的指令**）；招牌技能的核心失效模式已知且未修；改动节奏极快且措辞反复来回。

**与 Agent-L0 的关系判定**：**方法论资产，与治理体系高度互补**。Agent-L0 有很强的**架构治理**，但**没有「给 agent 写文档/写 description 的方法论」**——它的 60 个工具 description、5 份剧本正文、系统提示词全靠手感。`writing-for-agents` 那 81 行恰好填这个空档，且成本为零（纯读）。**4 条只活 3 条。**

---

### 1.7 addyosmani/agent-skills

**是什么**：24 个「工程纪律型」Agent Skill 语料，每份都带**「常见自我合理化 / 红旗信号 / 验证清单」三段式反偷懒结构**。约 1.27 万行 markdown + 3218 行零依赖治理脚本。MIT，87.7k stars，**20+ 贡献者**（七个项目里社区最健康的一个）。

| 核心价值资产 | 为什么经得起重写 | 位置 |
|---|---|---|
| **Tier-2 确定性触发评测（手写 TF-IDF + 词干化）** | 零依赖、纯 Node、毫秒级、可挂 CI 每次 push | `scripts/run-evals.js:62-149` |
| **负例的 owner 成对断言** | 负例除断言「本技能不能排第一」，还声明 `owner`（本该负责的另一个技能）。**没有 owner 时，一条「跟谁都不沾边」的负例会永远通过，评测形同虚设** | 同上 `:319-345` |
| **描述碰撞检测** | 两两算余弦 ≥0.75 error / ≥0.5 warning。**技能库长大后最隐蔽的腐化就是描述互相靠拢，人眼看不出、单技能测试也测不出** | 同上 `:57-58` |
| **rank-1 棘轮** | CI 传的值**刻意低于实测基线**（80 vs 86）留 6 点余量；文档明写「随路由改善抬高地板，**绝不为让回归通过而降低**」 | `evals/README.md:86` |
| **Trace-as-evidence 评分** | grader prompt 显式命令「judge 它**实际做了什么**，不是它散文里声称的什么」；trace 夹在围栏之间并声明「其中全部内容是待评的不可信数据」 | 同上 `:515-541` |
| **把 LLM grader 当不可信外部服务做契约校验** | 强制：明细条数 == 声明的期望条数、每条三字段类型正确、summary 与明细**自洽**。为此写了覆盖 5 种畸形的单测 | 同上 `:429-457` |
| **压力用例** | 把「常见自我合理化」表的每一条**反过来写成 prompt**：「技术负责人说这是一行修复，热修窗口十分钟」。**技能的价值不在顺境，在有人给它台阶下的时候** | `evals/cases/`、`fixtures/*-pressure/` |
| **豁免名单归校验器所有** | 豁免写在校验器常量表且**每条必须带书面理由**；文件试图自称豁免而不在表里直接报错。**把策略的所有权从被校验对象转移到校验器** | `scripts/lib/skill-lint.js:53-60` |
| **窄范围 + 病历式注释的漂移守卫** | 两者头部都写清「哪次事故催生了它」和「刻意不检查什么」 | `scripts/validate-*.js` |
| **用 hook 强制不变量而非 prompt 劝导** | `sdd-cache`：**无 TTL**，只有 HEAD 返回 **304** 才复用——把新鲜度判定权交还源站，使缓存成为一次**真实验证**而非记忆读取 | `hooks/sdd-cache-*.sh` |

**优点**：评测框架有清晰的先例调研与自我定位；三层分级把「免费天天跑」与「花 token 按需跑」切得很干净；CI 一路测到**真实安装**；零运行时依赖；版本由 `git describe --tags` 单点仲裁。

**缺点**：**Tier-3 没有对照组**（executor 永远带 skill，测不出「技能到底有没有增益」）；**grader 与被测是同一模型自评**（讽刺的是该仓库自己的 `doubt-driven-development` 明确论证「单模型评审与作者共享盲区」）；Tier-3 在**无容器隔离**的临时目录里跑带 Bash/WebFetch 的 `acceptEdits` agent 而夹具可由外部 PR 贡献；`skill-lint.js` 这个「规则单一真相」**自己没有单元测试**；hooks 的测试脚本**从未接入 CI**（而它们恰好覆盖会改写用户源文件的 302 行 bash）；共享 `references/` 单技能安装即断链。

**与 Agent-L0 的关系判定**：**期望最高、实际收获最低的一个**。原本判断「evals/ 的 Tier-2 是本次唯一可以几乎原样搬进 Agent-L0 的完整工程件」——**这个判断被对抗审查用实测推翻了**（详见 §4.1）。**4 条只活 2 条，且都不是 evals 的主体。**

---

## §2 横向：Agent-L0 在这七个项目里的真实位置

### 2.1 能力矩阵

> ✅ 有且成熟 ／ 🟡 有但不完整 ／ ❌ 无 ／ ➖ 该项目形态上不需要

| 维度 | Agent-L0 | dsh | worldmon | prime | tencent | google | mattp | addy |
|---|---|---|---|---|---|---|---|---|
| 事件账本 / 可重放会话 | ✅ | ✅ | ➖ | ✅ | 🟡 | ➖ | ➖ | ➖ |
| 轮次生命周期单一属主 | ✅ | ✅ | ➖ | ✅ | ❌ | ➖ | ➖ | ➖ |
| 确定性护栏（非模型自觉） | ✅ 十余种 | 🟡 | ➖ | 🟡 | ❌ | ➖ | ➖ | ➖ |
| 单点审批闸 + 按调用动态审批 | ✅ | ✅ | ➖ | ❌ | ➖ | ➖ | ➖ | ➖ |
| 物理隔离子沙箱 | ✅ | ✅ | ➖ | 🟡 | ❌ | ➖ | ➖ | ➖ |
| 上下文压缩 | ✅ 三层 | ✅ 事务式 | ➖ | ✅ | ✅ 三档 | ➖ | ➖ | ➖ |
| 长期记忆 / RAG | ✅ 两阶段+三路 | 🟡 | ➖ | 🟡 | ✅ 四层 | ➖ | ➖ | ➖ |
| **记忆冲突消解语义** | ❌ 只增沉淀 | ➖ | ➖ | 🟡 | ✅ | ➖ | ➖ | ➖ |
| 三端协议 + 共享夹具 | ✅ **独有** | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ |
| 异构算力分离部署 | ✅ **独有** | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ |
| 本地推理引擎补丁 / KV 三级热启 | ✅ **独有** | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ |
| 出网守卫 / 脱敏 / 台账 | ✅ **独有** | ❌ | 🟡 | ❌ | ❌ | ➖ | ➖ | ➖ |
| **工具执行可中断** | ❌ | ✅ | ➖ | 🟡 | ➖ | ➖ | ➖ | ➖ |
| **超长观察落盘 + 回取句柄** | ❌ | ✅ | ➖ | ➖ | ✅ | ➖ | ➖ | ➖ |
| **KV cache 纪律成文并门禁化** | 🟡 有实现无制度 | ✅ | ➖ | 🟡 | ✅ | ➖ | ➖ | ➖ |
| **append-only 文件的坏行/断尾纪律** | ❌ | ✅ | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ |
| **机械执行点（CI/hook/pre-commit）** | ❌ | ✅ 极重 | ✅ 极重 | ✅ | 🟡 | ❌ | 🟡 | ✅ |
| 事故/决策语料库 | ✅ §S+§D+账本 | ✅ 688 篇 | ✅ 82 篇 | 🟡 | ➖ | ➖ | 🟡 | 🟡 |
| 「给 agent 写文档」的方法论 | ❌ | 🟡 门禁化 | 🟡 词汇表 | ❌ | ❌ | 🟡 语料 | ✅ 理论 | 🟡 |
| 预测先行 / 证据等级制 | ✅ **独有** | ➖ | 🟡 | ➖ | ➖ | ➖ | ➖ | ➖ |

### 2.2 Agent-L0 独有、别在「向他们看齐」的过程中削掉的东西

1. **三端瘦渲染器 + 共享协议夹具**。七个项目里只有 Agent-L0 面对「一份协议三种语言」并给出可执行契约。
2. **异构算力分离拓扑**（按「是否出算力」划线，算力端完全无状态，四把独立密钥，非回环空钥拒启）。无人可比。
3. **本地推理引擎的承重优化**（KV 三级热启、TurboQuant、prompt-lookup 投机、思考预算锚定、llguidance 约束采样、MTP 全矩阵实测）。七个项目全部不需要也没有。
4. **出网决策与任务执行分权（铁律 6）**。deepseek 与 prime 的遥测分别是硬编码端点 + 无脱敏、默认开启；TencentDB 的 gateway 鉴权默认关闭。**在数据流向治理上，Agent-L0 领先这七个项目一个身位，不是半个。**

另外两项虽非独有但明显更强：**确定性护栏的密度**（十余种，每条都带触发它的真实会话编号）与**非目标/缓做/待决三分账**（且被显式禁止混淆）。

### 2.3 七个项目的共识信号（多个项目独立做了同一件事 = 强信号）

| 共识 | 谁做了 | Agent-L0 现状 |
|---|---|---|
| **机械执行点：把纪律做成会失败的程序** | dsh（lefthook + run-gates）、worldmon（husky + 726 行 pre-push）、addy（hooks.json + 每次 push 的 evals）、google（加载期校验器）、prime（`npm run check` 连安装器渲染都进回归）——**5/7，且形态互不相同 = 收敛而非模仿** | ❌ **完全空白**。这是本次最大的单点落差 |
| **「这东西如何改变模型可见文本与前缀缓存」是一等对象** | dsh（Model Experience 强制章节 + 门禁）、tencent（稳定/动态二分）、google（1024 硬预算，顶着上限写）、prime（单工具消掉几十个 schema） | 🟡 有实现（前缀三段分层）无制度，且**估算是 `chars//4` 启发式** |
| **评测的判别力本身要被守住** | addy（rank-1 棘轮 + 描述碰撞 + owner 成对负例）、worldmon（Vacuous Guard / Mutation Proof）、dsh（受控回放 A/B + 双对照盲评） | ❌ 四套评测里 3 套 `main()` 无条件 `return 0`；RAG 基线已失效（§6.4） |
| **豁免/例外必须带书面理由并归校验器所有** | addy（`Record<path, reason>` + 拒绝自我豁免）、dsh（白名单带 reason 且检查条目是否仍指向存在的目标） | 🟡 §D 决策表是同构物，但**没有机器可读的豁免表** |
| **失败模式的命名词汇表** | worldmon（CONCEPTS.md 127 条）、mattpocock（writing-for-agents 八概念）、addy（Common Rationalizations）、dsh（Agent Note class 封闭集）——**4/7** | 🟡 §S 踩坑 18 条**质量更高**（每条带事故→教训→适用域），但**按发生时间组织而非按失效形态**，同一机理被重讲了四次（S.3/S.4/S.6/S.18） |
| **不可信外部文本要物理隔离** | worldmon（默认剥离评审正文）、addy（trace 围栏）、tencent（角色劫持防御） | ✅ **已有且更强**（四层反注入 + 沙箱工具物理受限 + 来源账本代码维护） |
| **append-only 文件的坏行与断尾纪律** | prime（字节级 torn-tail + 双读稳定性 + linkSync 无覆盖）、dsh（zstd 帧 + 崩溃尾部修复 + 语义级重新平衡）、worldmon（staging→canonical 原子发布） | ❌ 四处裸 append、**四个读者三种互不相同的坏行策略** |
| **调度的冷启动与稳态用不同参数** | tencent（指数预热 + 单调定时器）、prime（claim-then-advance） | 🟡 prime 那条**已经有了且完全同构**（`schedule/runner.py:66-72`）；tencent 那条被判约束差 |

---

## §3 建议清单：19 条存活项

> **组织方式**：按完备性批评给出的**五个合并波**组织，而非按来源项目——因为分开做会产生互不兼容的实现（例：两种句柄格式、四种失败表达）。
> **标注**：★ = 强烈建议 ／ ☆ = 顺手可做 ／ 成本 S(半天内) / M(1–3天) / L(一周)
> **每条都已并入对抗审查的修正**。凡审查改过接缝、成本或威胁模型的，用「⚠️ 审查修正」标出。

---

### 波 A ★ 给纪律装一个执行器（合并 4 条 + 完备性批评的头号缺口）

> **为什么必须合并**：这四条动的东西完全不同（开工事实包 / 守卫交付标准 / 文档数字 / 评测基线），但有同一个致命共性——**每一条都是一个验证物，而这个仓库里没有任何东西会去执行验证物**。分四次做会得到第五到第八份「写下来了但没人跑」的散文，且会长出四种不兼容的失败表达。
>
> **不撞非目标**：`COMPARE_CC_HAHA.md:500/513` 的原话把「质量门 12 件套 / 重型 CI」列为不追，**同时把正解写死为「你需要的是 gold test + pre-commit hook」**；`ROADMAP.md` R6 也挂着这条至今未落地。这不是抄别人的架构，是把项目自己拍板过的东西接上电。

#### A0 ★ `devctl check` 子命令 + 一条 pre-commit 钩子 — 成本 S

**第一版刻意不加任何新校验器**，只做聚合与退出码合并：串起三条今天已存在但要手动敲的命令（`backend/.venv` 跑 `backend/tests`、前端 vitest 夹具回放、`apple-app/Tests/run_reducer_fixtures.sh`）。退出码按 worldmonitor 的**三值纪律**（0=过 / 3=不过 / 2=用法错 / 1=内部错）——**「门跑不起来」永远不能塌缩成「门说是」，这是整个设计里唯一不可省的语义**。跑通后写三行 `.git/hooks/pre-commit` 调它（docs/账本-only 的 diff 走 `--skip` 白名单）。
**接缝**：`devctl.sh:599`（分派表）、`.claude/settings.json`（今天完全空白的插槽）
**下面 A1–A4 逐个挂进它。**

#### A1 ★ `devctl preflight` 事实包 — 成本 M（来源：worldmonitor）

输出 `{schema, status: ready|blocked, blockers[], waived[], facts{}}`，全部用 `lib_edge.sh` 已有原语组装，**不新写探测逻辑**。
**⚠️ 审查修正三处**：
- **别重写已有的门，只做转述**：端口三态（`managed_or_report`）与跨机密钥校验（`probe_auth`，401/403 当场拒启——这比指纹比对更强，是真去认证一次）在 `start` 时刻已经硬执行，preflight 必须**复用同两个函数**并标注「这两项 start 时会再判一次」，否则就是两处真相。
- **gap 描述要改写**：不是「四把密钥无自动比对」——INFER/RAG 两把已由 `probe_auth` 真认证；**只有 `AGENT_API_KEY` 与 `RAG_COMPUTE_API_KEY` 是纯手工**。
- **bench 占用的取数方式是错的**：`campaign.pid` 是随战役新建的一次性目录产物，而 `bench_mlx_tile.py` 这类临时脚本直接在进程内加载 MLX 权重、根本没有 pidfile。按 pidfile 取数会在「有 bench 在跑」时**自信地报 free**——这正是 Read Outcome 塌缩的同型错误，后果是单 GPU 上撞进第二个真模型进程。**正确取法：`listen_pids(INFER_PORT)` + `benchmarks/baseline/*/campaign.pid` glob 为正信号，任何一项探不出确定结论一律报 `unknown` 而非 `free`**，并把 unknown 当作需人工确认项。

**收益**：`CLAUDE.md` 的「Delegation contract」里那段环境事实改为「粘贴一份 preflight JSON」，主会话不再手抄、也就不会抄错。

#### A2 ★ 变异证明升为守卫交付标准 — 成本 S（来源：worldmonitor）

**⚠️ 审查修正（重要）**：变异证明在 Agent-L0 **已经是成文纪律**——但只在**协议夹具域**（`HETERO_COMPUTE.md:278` + `protocol/fixtures/README.md:41`），且已实践过至少五次、账本有留痕。原建议声称「全仓唯一命中 stage-reviewer.md:50」是研究不到位。
**真正没有的**是把这条纪律**从夹具域外推到后端守卫域**——而两次实证失效恰好都发生在后端域（`_load_drafter` 的 mock 太深杀不死变异体；stage-reviewer 首战抓出的 C1 流式 BPE 乱码）。
**正确做法**：把那句话从「新夹具」外推为「新守卫（金测试 / 护栏分支 / fail-closed 判定）」，挪到 `CLAUDE.md` 的 Review gates 作为**唯一出处**，夹具 README 与 HETERO 原处改成指回它的一行。
**⚠️ 砍掉 `MUTATION_LEDGER.md`**：与 §S 完全重叠，且历史留痕早就走逐波条目正文（格式已成型）。新文件只会变成第三处需要人工同步的数字。
**适用面写死为「守卫类改动」**——这是原建议里最该保的克制（避免重演 worldmonitor「43% 会话与门搏斗」）。

#### A3 ☆ 文档数字改为注册表派生 + `--check` — 成本 S（来源：worldmonitor）

`CHARTER.md` 多处写「600+ 金测试」「mcp/ 27 文件 55 类」，实际是 **958 项 / 86 个测试文件 / 30 个工具文件**。宪章是每个新会话的第一读物，第一页的规模判断就是错的。
**⚠️ 审查修正（否则这条会把漂移从一处搬到另一处）**：实测确认「纯 stdlib、不 import backend」这条约束与「派生金测试项数」**互相排斥**——`grep '^def test_'` 得 **825**，而账本记 **958**（pytest 收集口径含 parametrize 展开）。**文本解析永远得不出 958。**
**修正方案 — 派生项分两类**：
- **甲类（纯文本可派生、口径唯一）**：测试文件数、工具文件数、`^class \w+\(BaseTool\)` 数（实测 60）、剧本数、夹具数、`model_registry` / `rag.tiers` 档位数、沙箱数 → 走 stdlib，**首批打点的全部**。
- **乙类（必须跑收集器才有真值）**：金测试项数。两条诚实出路：要么 `backend/.venv/bin/python -m pytest --collect-only -q` 且 venv 不在时输出 `unknown` 而**绝不回落到上次的数**；要么干脆把精确项数从 CHARTER 拿掉、只留「见 EXECUTION_LEDGER §2」的指针。

首批打点收到 `CHARTER.md:206/:303/:355` 三处即可，`--write` 对 CHARTER 默认关闭、只打印 diff（改 CHARTER 须用户同意）。

#### A4 ★ 评测基线从「打印增减」升级为棘轮 — 成本 S（来源：addyosmani）

实测：`rag/evals/run_retrieval_eval.py:265` 对比完基线**无条件 `return 0`**；`l0_eval` 全文 grep `BASELINE` **零命中**（`BASELINE.md` 是纯散文表格，代码从不读）；`officecli_eval:201`、`model_eval/report.py:122` 同。
**⚠️ 审查修正（对 rag/evals 成立，对 l0_eval 与 model_eval 直接照搬会造成永久假红）**：后两者是**选型工具**不是回归工具——`--model` 是 required，结果按模型分目录落盘，`BASELINE.md` 里并排着 e4b/9B/31B/26B/35B 五档与 mlx/cuda 两个后端的完全不同水位（span_recall 0.24～0.96）。一个全局 floor 会让「评一个新候选模型」恒红，久而久之地板必被删——**正是这条要防的失效**。
**正确形态**：floor 以 `(model, backend)` 为键写进 `BASELINE.json`，**只对 `config.yaml` 当前生产档位的那把钥匙强制**（非生产档位只打印、退出码 0），或加显式 `--enforce-floor`。
另：`over_mask` 这类**上限型**地板要在金测试里正反两向各钉一条（方向写反会静默失效）；`rag/evals` 的 runner 今天没有 `--repeat`，按中位数判定要先加，或认它是确定性跑批、余量取实测 0.941 → 地板 0.90。

> ⚠️ **重要前置**：A4 在 RAG 侧今天是**空的**——见 §6.4，题集已饱和到 1.0、基线停在 0.941，棘轮锁在天花板上等于锁了个空。**必须先补题再重钉基线**，否则只是把一个假绿换成另一个假绿。

---

### 波 B ★ 事件账本耐久性（合并 2 条 + 完备性批评的轮转项，来源：prime-agent）

> **为什么必须合并**：三条全部落在 `core/journal.py` 同一个文件的读写路径上，且**共用同一套测试脚手架**——都要求构造一份被截断/被污染/超长的 `journal.jsonl` 并断言恢复行为。分三次做要把 fixture 造三遍，且前两次的断言会在第三次加轮转后全部失效。

#### B1 ★ 追加型文件的坏行纪律与断尾修复 — 成本 M

**实测缺口**：`grep 'torn|ftruncate|fsync|os.truncate'` **零命中**；**五处裸 append**（`journal.py:117`、`events.py:50`、`session.py:491`、`session.py:621`、`cloud/advisor.py:90`）；**四个读者三种互不相同的坏行策略**：
- `get_history`（跳过坏行 → 前端少一轮、下标整体前移）
- `_replay_history_log`（**整个 for 循环共用一个 try** → 一条坏行让其后所有轮次静默消失于 model-facing 上下文）
- `disk_turn_count`（`sum(1 for _ in f)` 数**物理行**，`turn_index` 由它盖章）
- `compactor.fold_oldest_turns`（按 lines 下标推进 `summarized_turns` 水位线）

**一条断尾行就能让「前端下标 / 内存记忆轮次 / turn_index / 摘要水位线」四者互相错位**，而分支克隆与删除末轮都按下标定位。更致命的时序：产生断尾的那次崩溃，重启后 `recover_inflight_turns` 第一件事就是往同一个文件追加一条 fail 轮——**正好把残字节和新记录粘成一行垃圾**。

**⚠️ 审查修正两处**：
- **接线清单漏了最关键的一处**：`session.py:621` 的 `recover_inflight_turns`——它是崩溃重启后第一个写 history 的人，也就是把断尾粘成垃圾的那个人。
- **必须同时导出 `count_records(path)`**：四个读者今天对「第 N 轮」的定义就已不完全一致（空行即可让它们分叉），一旦 `iter_records` 引入「跳过坏行」，分叉会变成常态错位。金测试要断言四者对同一份含坏行的文件返回同一口径。

**关键实现点**：字节级（**不是 string index**——中日文标题保证 UTF-8 多字节记录随处可见，用 string index 找切点必然切进前一条有效记录）+ 双读稳定性检查；读侧只容忍「最后一行且无尾随换行」；append 前的断尾修复强制，读侧默认 skip+告警+计数并留 config 开关。

#### B2 ★ 事件账本游标加代际围栏 — 成本 M

**实测缺口 + 项目自己的协议文档在打架**：`PROTOCOL.md:310`「seq 恢复：进程重启后取 journal 末行 seq 续增」与 §2.4「断开重连：客户端记住已消费的最大 seq，带 after 重连即可，**任何时刻、任意次**」两条**直接互相矛盾**，而两端实现都严格照后一条办（游标是内存态、跨重连刻意保留）。
**触发链路**：`token_delta`/`thinking_delta` 都消耗 seq 但**不进 `MILESTONE_TYPES` 不落盘**，一次长回答轻松吃掉上千个 seq → 后端在轮次中途重启（`devctl restart`、SidecarManager 监护重启、崩溃自愈——**app 内嵌 sidecar 就是设计，重启是日常**）→ `_load_last_seq` 只能从里程碑行读到远低于实际的值 → **seq 就地回退** → 重连后所有新事件撞上 `seq <= cursor` 被当成重放丢掉 → **会话对用户表现为永久静音**。

**⚠️ 审查修正两处**：
- **验收计划错了**：`protocol/fixtures/` 只喂**纯归约器**，而游标比对与「代际变化即重定位」全在 **store 层**（归约器根本看不见 `stream_attached`——两端 dispatch 首行 return）。夹具 008 顶多是 003 的复读，证不了任何东西。**正确做法**：`PROTOCOL.md` §2.4/§2.5 落字段定义，行为锁在 frontend vitest 的 store 级用例 + `run_e2e.sh`，并在 `fixtures/README.md` 注明这条属 store 层契约、不在夹具覆盖面内。
- **分两步落，先补一个不动协议的止血**：`TurnJournal.__init__` 从磁盘取 seq 时留水位余量（`last_milestone + RESERVE`，取 1e6）——单文件、零协议、零客户端改动即消灭「静默静音」。**但它治不了另一半**：进程死时那一轮没有 `turn_ended`（`recover_inflight_turns` 只补写 history、不发任何账本事件），客户端会永远停在 generating。这一半只有代际围栏能治——**这才是它的真实卖点**。
- `generation` 必须取**进程级 boot_id**（不能挂 TurnJournal 实例：`/turn` 对冷会话会另 new 一个只读实例，实例级 uuid 会让同一会话的两个端点报出不同代际）。feed 那一半（`feed.seq` 纯内存归零）要一并做。

#### B3 ☆ 提交路径幂等键 — 成本 M（**排在 B1 之后**）

**三端里有一端是手机。** iOS 在蜂窝网下 POST multipart（含语音音频），请求完整抵达、服务端受理并返 202、而**响应在回程丢失**，是移动端常规事件。
**实测**：两端的 catch 分支对网络错误一律 `outboxClear` + 删掉乐观气泡 + 报「无法连接」——**outbox 根本不覆盖「响应回程丢失」这个方向**，它只覆盖「进程死在 multipart 途中」；而用户看到的是气泡消失 + 连不上，**正是最诱发手动重发的界面**。重发若撞上仍在跑的轮次得 409；若那轮已完结（短问答几秒即完），就是一次货真价实的**重复执行**——而那一轮可能带审批动作（写文件、发日历、trash_file），审批闸会再弹一次，用户在通知里点了允许就真做了两遍。

**⚠️ 审查修正三处**：
- **判重点位选晚了**：`routes.py:916` 的 `_prepare_turn` 在 `accept_turn` **之前**就把 multipart 摄入完了（落盘媒体、跑 ASR、解析文档）。判重写在 `accept_turn` 内部，重复提交照样付一遍摄入成本并留一份孤儿媒体。**正确**：判定逻辑仍归 TurnRunner（新增 `lookup_client_turn`），routes 在 `_prepare_turn` **之前**调它一次并直接回 202（routes 只做「调属主、透传结果」，仍是薄适配器）。
- **持久载体的说法要改**：`inflight.json` 不是已完结轮次的 durable 记录（`append_log` 完就 `clear_inflight`）。对「已落盘轮次的重复提交」唯一的持久真相是 journal 的 `turn_queued` 里程碑，回查要走**文件尾反向窗口读**——**这使本条对 B1 是硬依赖，排序不能反**。
- **「可以删掉启发式对账」要收窄**：两端 outbox 只存 prompt 文本、不存附件，所以带文件的消息无法自动重发，那段提示对「app 死在上传途中」的分支仍得留着。

---

### 波 C ★ 观察体的落盘与回取（合并 2 条 —— 一条句柄协议，不是两条）

> **为什么必须合并**：deepseek 的 spill 与腾讯的截断句柄是**同一机制的两种描述**。分两次做会长出两种句柄格式（一种给超长观察、一种给截断中段），而模型面只能容忍一种——它得知道「看到省略号时该调什么」。

#### C1 ★ 超长观察落盘 + 回取句柄 — 成本 S

**实测缺口**：`orchestrator.py:1196-1209` 的截断是纯字符级 head 2/3 + tail 1/3；`journal._persist` 对 `tool_result` 的 response **还要再截断一次**——**全文在系统里任何一处都不存在**。`ToolOutput.payload` 只服务渲染面，模型够不到。而 `observation_max_chars=4000` 是硬约束，`compactor` 的预算驱逐还会把旧观察进一步削成 digest，所以「Reuse the earlier result」那句话在观察已被削成一行取代桩之后是**空头支票**。

**桌面任务恰恰盛产超长观察**：shell 日志、`read_ui_tree` 的 AX 树、excel 影子重算输出、web 抓取正文——**其中 AX 树、shell 输出、剪贴板根本不支持「取更窄的一片」**，现有中缝提示语对它们是空头支票。

**⚠️ 审查修正（把成本从 M 降到 S）**：
- **删掉「新增 `read_observation` 元工具」**——`file_extract` 已经是 `requires_approval=False`、`is_concurrency_safe=True` 的只读工具，且对 text 支持 `start/end` 行区间 + `max_chars`，**正好是「按行窄取回」的现成能力**。落地退化为一处改动：中缝提示里多写一行绝对路径 `file_extract(path="sessions/<sid>/spill/<call_id>.txt", start=, end=)`。**收益完全相同，代价是零**——不新增 SCHEMA、不进 `META_TOOL_NAMES`、不给稳定前缀加一分钱，因而**不与波 D 的前缀预算门冲突**，并直接消解「第 5 个元工具稀释注意力」的风险。
- **`call_id` 不是模型给的**（`orchestrator.py:1120` 的 `uuid4().hex[:8]`），不存在路径穿越面；留一个 `[0-9a-f]{1,16}` 断言当纵深即可，别把它写成主要安全面。
- **沙箱不走这条路径**（`_invoke_tool` 在 `sandbox_runtime.py` 零命中），不需要额外的「限定只在主编排轮」逻辑。
- **磁盘回收白拿**：`prune_expired_sessions` 与 `delete_session`（`shutil.rmtree` 整个会话目录）已覆盖 spill 子目录。
- **必须同 diff 改金测试**：`test_observation_cap.py:26` 断言中缝里逐字含 `"narrower slice"`，改提示语会打红它；新措辞要保住「报原始体积 + 给可行动出路」两条语义。
- **compactor 的两个桩只能对真落过盘的观察带路径**——被预算驱逐的 <4000 字观察没有文件，桩文本要区分，否则会给模型一个不存在的路径，**比不给更毒**。

---

### 波 D ★ 模型可见文本面的账与闸（合并 2 条 + 新增一条：先有秤，再立预算）

> **为什么必须合并**：三条动的是同一个对象——**每轮真正进入 prompt 的那些字节**。分开做会出现一个具体的坏结局：**立了预算闸但没有秤**。

#### D0 ★ 稳定前缀的 per-tool token 账 + 修好 catalog 逃生口 — 成本 S（量）/ M（修）

**这是 Agent-L0 最贵的常驻开销，却是全项目唯一一个没有真数字的承重面。**
- 实测：`stable_prefix_tokens_est = sum(len(json.dumps(t)))//4`，注释自陈「后端无 tokenizer」——**而 rag 计算面的 `/tokenize` 端点与方言的声明渲染器都在同一台机器上跑着**。
- 更关键：**这个数字不是仪表，它是承重参数**——摘要高低双阈值都锚定在它上面（`summary_high/low = base + stable_prefix_tokens_est`），估算偏差会**直接推移压缩触发点**。
- 而唯一的逃生口今天是**物理坏的**：`dialects/gemma4/tool_grammar.py:183-189`，目录条目没有 `parameters` → `props` 为空 → 只生成 `call:name{}`，即 **catalog 模式下带参工具被约束采样钳成只能发空实参**。这正是账本记的那张前置票，`§7-8` 的 JSON→官方 DSL 迁移之后它**换了位置但没有消失**（从 `additionalProperties:false` 变成了 Lark 分支）。

**净效果：D4 阈值早已定好（12k），实测已到 11337、只剩 663 token ≈ 2–3 件工具，而开关拨不动。而下一步无论 D2 第三批工具还是 D3 子 agent，都是往这个前缀里继续加东西。**

**第一步（零风险、零改动、可与任何工作并行）**：写一个只读脚本，用真分词器逐工具打印 schema 的 token 数并降序排（优先走 rag 计算面 `/tokenize`，拿不到就退方言渲染器）。输出三样：① full 模式真实总量与 `chars/4` 估算的偏差（**直接告诉你摘要阈值锚得准不准**）；② token 花费 top-10 的工具（`TOOL_NORMS_AUDIT` 的成本纪律第一次有了依据）；③ 同一份数据算出的 catalog 总量。

#### D1 ★ 把模型看到的文本纳入金测试 — 成本 M（来源：deepseek）

958 项金测试全在测行为，**没有一项测「模型实际看到的那串文本」**——而这套系统的护栏措辞是逐条事故驱动写出来的（`test_tool_norms.py` 文件头把 skill_invoke 2/5→5/5、cloud_consult provider 幻觉、luna 空正文三案归为同根）。现有 `test_prefix_layering.py` 只钉结构不变量，`test_tool_norms.py` 只断言短语存在性，两者都看不见「这次改动让稳定前缀涨了 400 token」或「这条护栏提醒的措辞被顺手改了」。

抗 churn 用 deepseek 的**请求头钉选**法：一个场景钉住完整前缀字节，其余场景整段替换成 `{{prefix}}` 占位、只钉可变尾部。

**⚠️ 审查修正两处（否则这条会自伤）**：
- **「钉住 full 模式 + 全量真实工具集」在 CPU-only 金测试里不可复现**：registry 注册要过 `is_available` 探活，playwright / LibreOffice / SearXNG / yt-dlp 缺一台机器就少一批工具，快照会随机器和依赖装配状态红。**正确做法照抄 `test_tool_norms.py` 的路数**——直接 import 工具**类**、读类属性组装 schema 列表（`get_schema` 是纯类数据，不实例化、不探活），对这份确定性清单做快照与预算断言。
- **删掉「单个工具 description+parameters 字节上限」**：它与 `test_tool_norms.py` **直接对着拉**——后者要求若干工具描述里逐条存在具体准则句，一条字节上限会把「宁可 20 个实测过的工具」变成「删准则句最省」。要留就降级为只打印不断言的报表。

#### D2 ☆ 提示词面的减法纪律与预算闸 — 成本 M（来源：mattpocock）

在 `TOOL_NORMS_AUDIT.md` 追加「§ 减法与预算」章（**刻意不新开文件，避免自造 sediment**），写死三条纪律：no-op 判据绑定生产档模型（换档即结论作废）、判为 no-op **删整句不改词**、代码已强制的事实（审批会弹窗、参数从 rag_search 结果头取）复述即 cache。

**⚠️ 审查修正四处**：
- **断言变量的口径要写明**：`stable_prefix_tokens_est` **只统计工具声明，不含 `DEFAULT_SYSTEM_PROMPT`**（实测 6355 chars ≈ 1588 est token）。测试 docstring 里必须写死「本闸只管工具声明，系统提示另计」。
- **消融裁决这条纪律付不起，必须换判据**：`attention/driver.py` 与 `norms_probe.py` 都要活栈 + 真模型，而 `RESULTS.md` 自己写着「n=3 不支持显著性结论，要硬结论需 n≥10/侧（约 6 小时实机）」，且单 GPU 纪律不许委托。**逐句消融 = 每句 6 小时独占生产机 = 这条纪律永远不会被执行**。改法：可删句**收窄到不需要模型就能判的那一类**（代码已确定性强制的事实的复述，读源码即可证），其余「疑似 no-op」只进观察清单、等下次真机套件搭车。
- **仪器本身先修**：`attention/driver.py:28/:33` 的路径还钉在迁仓前的 `/Volumes/enkin/project/Agent/`（现仓库是 Agent-L0），`ENV_LOCAL` 是硬编码——不修它裁决仪器根本起不来。
- **「12k 断言一上就红」是错的**：实测 11337 < 12000，一上是绿。正确写法是把余量（约 660 token）打进断言失败文案，让下一个加工具的人当场看见「再加两三件就要先清前置票」。

---

### 波 E ☆ 剧本准入契约（合并 3 条 + 完备性批评的验收状态项 —— D1「AI 批量写剧本」的前置件）

> **为什么必须合并**：四条全部落在同一个装载判据（`skill_registry.py:119-166` 的 `_load_dir`）与同一个解析器（`utils/frontmatter.py:15-37`，手写极简版、只认 `key: value` 与内联 `[a,b]`、解析歧义直接抛而不猜）。这个解析器既是共同接缝也是共同约束。一个校验器一次写完，分四次会得到四段互相不知道对方存在的校验代码。
>
> **时机理由**：`ROADMAP §3` 的 D1 写的是「skill 是 md 文件，AI 批量写，**每个 skill 需实测验收**」——**先有一个不许自我豁免的准入检查，才敢让模型批量生产剧本**。

#### E1 ☆ 剧本描述改三段式并设预算闸 — 成本 S（来源：google）

实测：五份剧本 description 全是**单句中文、43–46 字符、零负向排除**；而**工具面已成惯例**（`doc_write`/`excel_research`/`rag_search`/`run_math`/`web_read`/`shell_exec` 全都有 `Do NOT use ... instead`）。三条写入路径（`_load_dir` / `_save_skill_impl` / `RegisterSkillTool`）无一条有长度/结构判据。

**⚠️ 审查修正三处**：
- **repo 剧本越界不要在 `_load_dir` 里 raise**：`skill_registry.py:165` 的 `except Exception` 会把 ValueError 吞成一行 print 后 `continue`——**结果是「启动正常、剧本消失、无报错」**，正是测试注释里点名要防的症状。改法：加载期对任何来源都只 warning（并在 `listing()` 里标 `over_budget`），硬拒放在**金测试**（静态 lint）与**写入端点**（`_save_skill_impl` / `RegisterSkillTool`，返回 400 不会造成静默蒸发）。
- **`skill_invoke.__init__` 不要 assert**：它跑在目录扫描/热重扫路径上，抛异常会让工具整体不注册。改成超预算时按名排序截断索引并追加 `…(N more skills, ask by name)`，同时 print 警告。**护栏必须 fail-open**。
- **不要把 `_DESC_HEAD` 里的正向映射搬进各剧本 description**：那三条映射 + 多语言例句是 L4 站**三轮实测调到 5/5** 的成品，重排即回归风险。正确切分：`_DESC_HEAD` 原样保留，每份剧本只**增补**负向排除，总预算断言下在「`_DESC_HEAD` + 索引」的合计长度上。验收先跑 `test_tool_norms.py` 与 `norms_probe.py` 确认 5/5 未回退。

#### E2 ☆ 剧本正文的悬空工具引用守卫 — 成本 S（来源：addyosmani）

`skill_registry.py:157` 的探活只比对 frontmatter `requires` 与 `tool_names()`，**正文零覆盖**；而 `_save_skill_impl` 只校验非空。

**⚠️ 审查修正四处**：
- **它举的「实锤」站不住**：全项目只有 `ingest.md` 一条正文命中（`rag_search`），而原话是 "(`rag_search` if available)"——**已经用散文写明是可选增强，不是悬空 bug**。所以这条今天的产出就是一条白名单条目，**价值全在前瞻面**。
- **重心移到用户剧本入口**：跑金测试时 `data_dir()/skills/` 是空的 tmp_path，仓库侧 5 份由同一人手写、工具改名几乎必然同时打脸已有的 requires 金测试。**真正未被覆盖的只有 `register_skill` / `POST /api/v1/skills` 这条路**。检查器应写成 `skill_registry` 可复用的纯函数，被金测试与写入端点**同时**调用。
- **告警不要用 print**：`skill_registry.py:161` 的 print 只进后端 stdout，桌面单用户永远看不到。应在保存端点返回体加 `warnings` 字段（设置页表单能显示），或照 drift_patrol「标记权不改写权」写一条笔记经记忆索引抵达对话。
- **`ingest.md` 选豁免而非补 requires**：正文已写 'if available'，把 `rag_search` 塞进 requires 会让 **RAG 服务一挂整份 ingest 剧本蒸发**（探活是全有全无）。这条分叉按 §S 记一条。
- **严重度写实**：正文点名不存在的工具时 `orchestrator.py:981` 会回 "Unknown tools (check the catalog...)" 并把控制权交回模型，**不是无声失败**——这是省人力的卫生措施，不要按 Critical 排期。

#### E3 ☆ 剧本 frontmatter 增验收状态/版本字段 — 成本 S（来源：完备性批评，五个 skill 项目的共识）

`ROADMAP §3` 写「每个 skill 需实测验收」「宁可 20 个实测过的工具，不要 60 个没验过的」——**但这条纪律在数据结构里没有任何落点**：frontmatter 只有三键，`Skill` 类只有六个字段，「这份剧本实测过吗、在哪个模型档上测的、什么时候改的」全部无处安放。今天 5 份可忍受；D1 一开工批量生产，第一天就变成「20 份剧本，没人知道哪几份验过」。
**约束**：新字段必须能用 `key: value` 与内联 `[a,b]` 两种形态表达（解析器刻意不猜，理由写在模块头）。
**红线复核**：加 `verified_on` / `version` / `owner` 这类元数据**不授予任何工具权限**，不触 D.11 的「skill 绝不能自带 `tools:`」。
**共识信号（5/5）**：addyosmani 的必需章节 + 豁免归校验器；deepseek 的 `Record<path, reason>` 白名单且检查条目是否仍指向存在目标；tencent 的 `(skill_id, version)` 不可变快照 + `is_head`；prime 的 harness 条目带 id/scope/version + 回滚快照；google 的 name 必须等于目录名（111/111 满足）。

---

### 波 F：独立项（不合并）

#### F1 ★ 非零退出码要分语义 — 成本 S（来源：google）— **本次唯一在代码里复现出确定 bug 的建议**

见 §6.1。**这是全部 19 条里最该先做的一条：成本最低、缺陷最确定、修法最单点。**

**⚠️ 审查给出的耦合方式（别留给实现者猜）**：orchestrator 只拿得到 response 文本、拿不到命令，所以「这是良性非零码」的判断**必须在 `shell_exec` 里做完**（它才有 command），再经一个稳定标记传出去；orchestrator 侧的正则只匹配那个标记，**绝不试图从文本反推命令语义**。建议标记形如首行 `exit_code: 1 (benign: no match)`，并在两侧注释里互相点名。
白名单取 `{grep, rg, fd, diff, cmp, test}`（全在 `_READONLY_COMMANDS` 里）；**`find` 不要收进来**（无命中返回 0，非零一律是真错）。四条与门（首命令在集合内 / exit 恰为 1 / stderr 为空 / 不含元字符）缺一不可，且**复用 `shlex.split` + `_SHELL_METACHARS` 的既有判定**，不要另写一份（否则与 `_is_readonly` 漂移）。
金测试加在 `test_fabrication_guardrails.py`（D2 护栏的归属文件），正反两向各钉：正向 = grep 无命中收尾不触发 nudge；反向 ≥4 条 = exit 2 仍触发 / stderr 非空仍触发 / 含管道不套用 / heredoc 那条既有用例红线不变。

#### F2 ★ 工具执行要能真停，不是弃等 — 成本 S（第一半）/ M（完整）（来源：deepseek）

**实测**：`BaseTool` 钩子面无任何 cancel 面；`registry.execute_tool` 是裸 `tool.execute(**arguments)`；`shell_exec` 只有 `subprocess.run`（阻塞、无句柄可 kill）。`session.cancel_requested` 只在**迭代边界**与 **token 段**被读——**长工具执行期间用户按取消完全不被观察到**。

**⚠️ 审查修正三处（把收益面收窄到真实的那一块）**：
- **真正无界的只有 `shell_exec`**：它不设 `timeout_seconds` 类属性，故外层用全局 180s，而它允许模型自报 timeout 至 600s——**实测存在最长 420s 的孤儿窗口**。其余长工具（web_research / excel_research / code_explore / media_download / organizer / api_tool_factory）**已有「内层自熔断 < 外层等待」的既有纪律**，孤儿窗口接近 0。
- **先做零成本的一半**：在 `shell_exec.execute` 里把模型自报 timeout 钳到「外层预算 − 余量」。**这一行就消掉全部真实事故面**；Popen + 进程组是它的加强版而非前置件。
- **原方案的落地项④是错的**：迭代边界检查点在 while 顶部，工具在途时根本不会执行。**正确接缝是那次 await 本身**：把 `asyncio.wait_for` 换成 `ensure_future + 0.5s 轮询 cancel_requested/超时`，命中即 set Event 再等 grace 期。
- **删掉「write_file/edit_file 被放弃后才落盘」这条 rationale**：它们是毫秒级同步写，撞不到 180s 熔断，这条事故形态在本项目不成立。

#### F3 ★ 工具返回契约做成不塌缩的三态 — 成本 S（来源：worldmonitor 的 Read Outcome）

见 §6.2。**⚠️ 审查把接缝和成本都改了**：
- **原建议给的理由是错的**（RAG 掉线会走 ConnectionError → `System Error:` 前缀，且 `is_available()` 在注册期就把离线的 RAG 工具挡掉了，「自信的空答案」在旗舰路径上并不发生）。**它活下来靠的是审查自己发现的一条真误报路径**——见 §6.2。
- **接缝选错**：`_route_step_failure` 只有一个调用点（迭代预算耗尽时），作用域里根本没有单次工具调用的 outcome。**真正的消费者是 `:1189` 的 `errored` 与 `:211/:223` 的 `_resp_failed`**。
- **协议面**：不要另起 `outcome` 与既有 `status` 并存（`PROTOCOL.md:109` 已有 `status: "ok"|"timeout"`，两个重叠状态字段违反单一真相）。第一波把状态挂成 obs 的 `_` 前缀内存元数据（照 `_safe`/`_target` 的既有规矩，`append_log` 会剥离），**完全不动协议、不动夹具**；代价是会话重载后 resume 路径仍回落正则，这一点写明为已知欠账。
- **旗舰消费者换人**：FTS 腿的增益至今未实测且该层明文 fail-open，为它加降级标注收益不实。**真正值钱的 `empty` 细分是现有响应体就能算出来的**：`total_chunks_retrieved > 0` 而 `results == []`（重排阈值把候选全滤掉）——这是「找到了但都不够自信」，与「库里没有」是完全不同的两句话，**且只改 `rag_search.py` 一个文件**。成本从 M 降到 S。

#### F4 ☆ 系统回合转录本的注入边界 — 成本 S（来源：tencent）

**⚠️ 审查大幅修正了威胁模型**：原建议声称「恶意 PDF → `extra_text` → 裸拼进摘要器 → MEMO → 笔记」——**这条链不存在**：`_turn_to_text` 根本不读 `ingested_text`，也不读 `role=tool` 的工具观察。真实通道只剩两条且窄得多：(a) 纯语音消息的 ASR 转写被升格成 prompt；(b) **助手自己的答复复述了网页/PDF 里的内容**，经 `Assistant: {content}` 进摘要器——(b) 在「帮我总结这个页面」这类桌面高频任务里真实可达。

**只留确定性那一半**：在 `_turn_to_text` 的输出里对行首 `MEMO:` / `MEMO：` 做转义——**把转录本文本变成笔记的唯一机关，就是摘要器吐出一行字面 `MEMO:`**。
**⚠️ `_split_memo` 绝不能改成认最后一个锚点**：现状认第一个是**严格更安全**的（第一个之后的全部内容都被从滚动摘要里剥掉）；改成最后一个会让靠前的伪 MEMO 块漏进滚动摘要，且直接打红既有金测试。
**`<<past-*>>` 标记集与 SUMMARY_INSTRUCTION 改写建议砍掉或单独立项**：它动的是已调优的提示词资产，收益靠 4bit 模型的理解力，验收只能上真模型，与「护栏是确定性代码」的方向反而不一致。

#### F5 ☆ 短会话的记忆产出 — 成本 S（来源：tencent，**砍掉一半**）

**实测缺口**：`sediment`（RAG 沉淀）与 MEMO 冲洗的唯一入口都是 `maybe_summarize_context`，而它有**三重前置**（`fold_until = 行数 − keep_tail(4)`、`last_prompt_tokens ≥ 前缀 + 2000`、`fold_until − summarized_turns ≥ batch_min`）。故 **≤4 轮的会话永远不折叠、既不沉淀也不冲洗**；配合 30 天保留期，这些会话到期被整目录删除，**长期记忆产出恒为零**。
**而桌面 agent 的主导会话形态恰恰是短会话**：「帮我把这批文件改个名」「明天三点提醒我」「这份 PDF 讲了什么」——两三轮结束。这是编码 agent（会话天然长）与桌面 agent（会话天然短）的形态差异造成的盲区。

**⚠️ 审查砍掉了预热层**：腾讯的指数预热解的是「同一个长命会话内的冷启动」；Agent-L0 的形态是**每个短会话都是一个新会话**，warmup 从 1 起步且在毕业前就被删除——它不是「冷启动快、稳定期省」，而是**每个会话恒定多花一次本地生成**，效果上就是已拍板否决的「额外起一次 LLM 抽取」。
**只保留零 LLM 层**：`memory.json` 加 `sedimented_turns` 第二条水位线，在空闲维护回合与 `session.freeze()` 之前把水位线之后的已落盘轮次经 `_turn_to_text` + `rag_sink.sediment` 沉淀。
**⚠️ 三条必守**：① 去重不另起账（折叠时顺手 `sedimented_turns = max(..., fold_until)`）；② **必须沿用作废轮次过滤**（`status in ("fail","cancelled")` 的轮次本就不进记忆，漏掉会把取消/故障轮沉淀进 `agent_memory`）；③ 挂在既有 `sediment_to_rag` 开关下并加下限（成功轮次 <2 的会话不沉淀），否则「帮我把文件改个名」会稀释分类软路由的候选池。

#### F6 ☆ 子沙箱用量归因回父轮 — 成本 M（来源：prime-agent）

**实测**：`run_sandbox` 只回一个字符串，`generate_once` 返回的 metrics 被整体丢弃；父侧 metrics 逐项只来自主循环。后果三层：① history 落盘的 metrics 系统性低估（项目自己记过一次「实测被低估 8 倍」）；② D.20 收束到 6 档后的升级验收口径对「一次调研烧了多少」是瞎的；③ **`max_total_iterations` 熔断只数主循环迭代——一个连调五次 web_research 的轮次消耗 30 次生成却只计 5 次迭代，保险丝形同虚设**。

**⚠️ 审查修正（接缝选窄了）**：`run_sandbox` 的循环**不是子生成的唯一入口**——骨架之外还有五处 `generate_once`（`math_sandbox:159`、`research_sandbox:216/:276`、`excel_sandbox:276` 的 qa_engine 复核、`orchestrator:1016` 的 `_goal_alignment_check`）。只在 run_sandbox 里累加，落地当天就还剩同一类盲区。
**修正**：把 sink 挂在五处唯一的公共对象上——**`EphemeralSession`**（五处全部经它构造）。构造时接一个可选 `usage_sink: Optional[dict]`，各调用点就地累加（不改 `engine.py`，避开铁律 1 的 sacred 面）；**显式传入而非依赖 contextvars 跨 `run_coroutine_threadsafe` 的上下文继承**。`goal_verify` 单独分桶（它不是子 agent，是护栏）。金测试断言 sink 只允许数值型字段（守住 `inject_env_context=False` 的反方向）。

#### F7 ☆ 澄清改成一轮问完的前沿 — 成本 S（文档）/ M（提示词半，需真机搭车）（来源：mattpocock）

现文已有「附 2-3 个可选项让一次回复就了结」这半（等价于 mattpocock 的「附推荐答案」），**但批量这半没有**，且 `DEFERRED §1` 的完整版元工具签名钉死在单数 `ask_user(question, options?)`。
**经济账在本地档上比在云端更成立**：云端一次往返几百毫秒；Agent-L0 一轮是单 worker 串行 + 11337 token 稳定前缀 + 冷预填十几秒，再乘以 N 次打断人。

**⚠️ 审查修正三处**：
- **删掉「先落 `008-clarification-batch` 夹具」这一步**：`clarification_requested` 今天两端归约器都不认识，按「未知事件必须忽略」，其 expected.json 只能记成「状态无变化」——**与已永久保留的 003 完全同构，是一条恒绿且测不出任何东西的退化夹具**，正是 fixtures README 结尾那句判据点名要避免的。而且完整版是缓做项、三条重启触发条件一条都没触发——现在把 payload 形状冻进夹具是给还没决定要建的东西先浇混凝土。**只改 `DEFERRED.md §1` 的签名描述即可，铁律 5 的夹具义务留在真开工那一刻。**
- **成本从 S 上调到 M**：提示词那半必须挂真机验收（唯一能判「过度提问回归」的仪器要活栈 + 真模型 + n≥10/侧约 6 小时），排进下一次真机套件的搭车窗口。
- **上限从 4 收到 2–3**，并原样保留反向约束「Never burn a question on a request with a clear outcome」（现文的「offering 2-3 concrete options」已把机制建好，上限定 4 会叠乘成最多 12 个选项的问题墙）。

#### F8 ☆ 跨文档决策索引 — 成本 M（来源：mattpocock 的 `.out-of-scope/`）

**真实症状（审查复核属实）**：`COMPARE §6` 的「skill 自主发现不做」（e4b 口径）在 D.17 于 2026-08-09 翻案后**仍原样留在原处**，只能靠一句「引用此条时须以 D.17 为准」人工提醒；而 §6「文档漂移待校正」表的存在本身就证明跨文档漂移是常驻问题。加上决策大量绑实测口径、D.20 一次收阵容就连带销案四项——**「换档后哪些旧裁决自动作废」今天没有任何入口**。

**⚠️ 审查基本重写了落地方案**：
- **不要新增仓库根 `DECISIONS.md`**：根目录已有 15 份 md，而 2026-08-08 刚跑过「文档瘦身波」——加第 16 份根文档与那次拍板直接对冲。**改为就地扩 `EXECUTION_LEDGER §D` 表**（它已是「已定决策不再重议」的规范表、已有「定于/依据」两列、已被 §R 列为必读三节之一）：**加两列**——「依据口径」（模型档 + 实测日期）与「状态」（有效 / 已翻案→接替条目 id）。
- **不要批量灌入**：原方案要把 §D 20 条 + §5 Q1–Q14 + DEFERRED 五条 + CHARTER §7 每 bullet + 5 份 evaluations 的表全搬进去，是几百行手工搬运，且与「不预建、等真实需求」同构。**改为需求驱动增量**：只有当一条裁决被翻案、或某会话真的重新提起它时，才补一行并标口径。
- **测试必须换成窄的**：原方案要解析六份手改治理文档的 markdown 排版并要求 1:1 覆盖——**任何一次重排版都会弄红，属维护为负的测试**。改为**单文件窄断言**：只解析 §D 一张表，断言（a）每行「依据口径」与「状态」两格非空，（b）标「已翻案」的行必须写出接替条目 id，（c）id 不重复不跳号。
- **删掉「加第 6 名查重员子代理」**：拿一个中继换一次 grep，价值在索引本身。

#### F9 ☆ 轮次进行中允许用户补话（steer）— 成本 L（来源：deepseek）

今天轮次 queued/running 时新消息一律 409，用户中途想补一句，唯一出路是取消整轮（**整轮回滚，此前所有工具工作全丢**）或干等。而 Agent-L0 的一轮是冷预填 16s 起、ReAct 最多 40 次迭代、web_research 单次 240s——**用户盯着进度看几分钟，中途想起「顺便也导出成 PDF」是常态**。

**⚠️ 审查列了四条必补（漏任一条即不可落地）**：
1. **受理路径不能取 `session.lock`**：`turn_runner.py:86-88` 的注释逐字写着「运行中的轮次持锁长达数分钟」——照抄 `accept_turn` 的 `async with session.lock` 会让 steer 请求挂死到轮末，**功能变成「等一轮」**。必须是锁外的无锁追加（事件循环线程内 append + `journal.emit`）。
2. **必须处理 `goal_verify` 的取数口**：`orchestrator.py:866-868` 的无计划分支取 `_last_user_text(session.history)`，steer 一旦作为真实 user 消息进 history，**收尾审计会拿补充句去审整轮答复，把原始目标审丢**。
3. **明确 steer 与整轮回滚的关系**：取消即回滚到本轮起点，steer 消息会一起蒸发（账本里还在）。这条语义必须在夹具里钉死并在前端说清。
4. **`turn_steered` 必须进 `MILESTONE_TYPES`**：否则后端重启后 `/turn + /events` 重建不出这条，**正好破掉建议自己设的放弃条件**（若做不到「在途状态能从 /turn+/events 完全重建」，这条应当直接放弃而不是打折落地）。

建议照 `DEFERRED.md` 的自律先做轻量版：每轮一次 steer、只在迭代边界消费、前端复用普通用户气泡。

#### F10 ☆ 把「CLI 反幻觉」写进 `shell_exec.description` — 成本 S（来源：google 的 gcloud 契约）

实测：`grep -rn -- "--help" backend/framework` **全仓零命中**；`DEFAULT_SYSTEM_PROMPT` 无任何「先验记忆对 CLI 参数不可信」条款。而 4bit 本地模型的 CLI 参数记忆**比云端前沿模型更陈旧**——这条机制在 Agent-L0 上的收益**大于**在 google 那边。实案背书：`shell_exec.py:156-160` 的注释里就记着 d618f0d6（模型把函数调用语法带进命令，用户批准了一条必败命令，修正后又骗一次审批）。

**⚠️ 审查修正三处**：
- **不要新建 `skills/cli-ops.md`**：「不熟的命令先查权威源」是**横切行为规范**，不是一种任务类型的工作流。做成剧本有三个坏后果：requires 只有 `run_shell_command`/`file_extract` 故它**永远探活通过、永远挂在索引里**，稀释 E1 刚立的预算；模型要为一条规范先烧一次 skill_invoke 往返；规范只在载入它的那一轮生效。**Agent-L0 里横切规范的既有归宿是工具描述**（`TOOL_NORMS_AUDIT` 整份文档就是这件事）。
- **「`--help` 在只读白名单里自动放行」是错的**：`_is_readonly` 的 git 分支取第一个非 `-` 开头的 token 作子命令，`git --help` 得到 None → **要弹审批**；`docker --help`/`brew --help` 因首命令不在白名单同样要弹。**恰恰在「命令不熟」这个唯一需要它的场景里，权威源不是免费的。** 两条出路二选一并写进文案：(a) 只提升 `man <cmd>`（确在白名单、真免审批）为首选，`--help` 作次选并如实写明可能弹一次确认；(b) 若要让 `--help` 免审批，那是**改审批白名单**，必须单独立项由用户拍板。
- **错误指路的正则要窄**：挂在结果组装处而非异常分支，命中 usage/illegal option/unknown option 时**追加**一行「先跑 `man <cmd>` 再重发，不要换个猜的参数重试」，保持首行仍是 `exit_code: N`。**漏报优于误报。**

---

## §4 明确不借鉴 + 15 条被杀掉的建议

### 4.1 被实测证伪的 4 条（最有价值的部分）

> 这四条在纸面上都很有说服力。**它们死于审查员真的跑了代码。**

| 建议 | 死因 |
|---|---|
| **目录级确定性路由回归门**（addyosmani 的 Tier-2 TF-IDF，原判为「本次唯一可原样搬进的完整工程件」） | 把它的算法原样跑在真实的 60 件工具描述上（AST 抽取、name 权重×2、IDF 加权、照抄 `rag/fts_index.py` 的 CJK 二元组分词），**三项核心机制全部不成立**：① **碰撞探测器整盘死掉**——1770 对里 ≥0.5 的 **0 对**，最高一对只有 0.468（copy_file/move_file，本来就该像）；点名的四对「天然近邻」实测 0.056–0.247，全在告警线以下一大截。addyosmani 的 0.5/0.75 常数是给 24 份**英文一句话** description 定的，Agent-L0 的描述中位 351 字符、最长 1404 字符的英文行为契约，IDF 空间稀疏得根本撞不上。② **rank1 对中日文查询在数学上未定义**——60 件描述里只有 5 件含任何 CJK，中文查询得分**全为 0.000**，top-4 只是遍历顺序噪声；而这恰恰是它自称要救的场景。③ **就算查询用英文，信号还是反的**——`search the web for the latest EU AI act enforcement news` 实测 rank1 = **rag_search(0.161)**，web_research 掉到第 3(0.090)；而 `norms_probe.py` 的判据正好相反（时事题 rag_search 必须零调用）。**入库当棘轮等于把错误方向焊死。** |
| **Chroma↔FTS 原子发布与一致性自检**（worldmonitor 的 atomicPublish） | 剖面里那条「reembed 重建后 FTS 留旧 chunk_id 孤儿、单调增长」**是错的**：reembed 走的是带 `source_path` 的就地索引，服务端据此进入 existing 分支，**写新切片之前对两条路径各调一次 `FTS.delete_where_source`**；且 FTS 清理与 Chroma 的每一处删除**已经成对**（五处删除路径全配了 FTS 侧动作）。**实测证伪**：对真数据根做只读比对，`chunks_fts` 11 行逐条与 Chroma embeddings 对照，**11/11 全在，孤儿数 0**。而第②项（收成事务性发布单元）直接撞 `fts_index.py:15-17` 的成文设计原则：「派生层纪律：可随时删除重建……一切写操作 fail-open，Chroma 仍是唯一真相」——**给一个被明文声明为可弃的派生层加回滚分支，是在热入库路径上引入「可能丢真数据」的风险去换一个不存在的收益**。仅剩 `cmd_wipe` 顺手也删 `fts_index.sqlite3` 这一行卫生修正。 |
| **剧本加 invocation 二分**（mattpocock 的 user/model-invoked） | 实量：剧本索引 = **219 chars ≈ 54 est token**，占 full 模式稳定前缀 45348 chars 的 **0.48%**；而建议自己写明「5 份剧本全部保持默认 model（零行为变化，纯加插口）」——**落地当天节省 token = 0**。至于「剧本长到 25 份时索引就是 1~2k token，恰好推爆 12k 闸门」：按实测均值外推 25 份 = **274 est token**，比宣称的小 4~7 倍，且真正的吞噬者是工具 schema 本身（约 180 est token/件），**剧本索引即使涨到 25 份也吃不掉半件工具的量**。代价侧却引入一个建议自己都承认是静默的新失败模式（误标 → 模型永远想不起该剧本）。 |
| **摘要改成会话前缀的续写**（deepseek 的 summarizer） | **前提事实错误**：建议整条论证建立在「摘要调用是在对一段已躺在会话 KV 里的历史做完整冷预填」之上——**这句话是假的**。实测：喂给 `engine.summarize` 的 `batch_text` 不是模型面历史，而是 `_turn_to_text` 从**落盘轮次记录**抽出的蒸馏文本，它**对 `role=="tool"` 的消息整体不取**。也就是说：占上下文绝大部分体量的工具观察在摘要输入里根本不存在，摘要 prompt 是一段一两千 token 的蒸馏稿，**从未以逐字相同的字节出现在任何会话 KV 里——没有可续写的前缀**。要让它成立只能改成去摘要模型面历史，那是对滚动摘要、MEMO 抢救、rag_sink 沉淀三条下游语义的实质改写，不是缓存优化。附加风险：真按建议 clone 并把前缀回退到 `fold_until`，是一次跨数千 token 的**向后**回退，而按类型裁剪对旋转态缓存只在 `keys.shape[2] > prefix_len` 时切片——**回退距离超过滑窗时该分支静默不成立**，正是 §S.15 类静默错位。 |

### 4.2 死于「已经有了」或「收益不实」的 6 条

| 建议 | 死因 |
|---|---|
| **无界输出调用在预检就地打回**（google） | 「预防式的上下文预算护栏在 L0 完全不存在」是**事实错误**：`file_nav.py:7-8` 的模块 docstring 原文就是「输出都设条数上限……工具级先裁并如实报告总量」，落地为 `_LIST_CAP=200`/`_FIND_CAP=30` 并输出「… N more entries not shown」；`file_extract.py:20-22` 把工具级预算**锚在 `observation_max_chars` 上**。且 cat/head 那一支是**净负**：执行 cat 拿回 head+tail = 1 步；预检打回 + 模型改用 `sed -n` 重发 = 2 步——在单 worker 串行、每步都是一整次生成的机器上，把 1 步换成 2 步还剥夺了「截断结果已经够用」的常见好结局。另：`validate_arguments` 的 docstring 把职责写死为「只拦注定 zsh 报错的硬伤，宁漏勿误杀」，而三条判据针对的恰恰是**会成功执行、且已被自动放行不弹审批**的命令。 |
| **剧本加 references 渐进披露**（google） | 规模差 + 约束差方向反 + 撞两个金测试。规模：google 服务的是 111 份技能 / SKILL.md 中位 1069 词；Agent-L0 五份剧本实测 2032–2329 字节、正文约 400–500 词，**比 google 那个「已经被压到 1069 词的主文件」还小三分之一**。约束反向：多一次 `file_extract` 就是多一整个 prefill+decode 迭代，占的是**步数预算**（`react_max_steps=4`）这个比上下文更硬的资源——云端一次往返近乎免费所以「拿往返换常驻 token」稳赚，本地这笔账符号是反的。且成本前提也不成立：`skill_invoke` 声明 `is_read_only = True`，剧本正文**本来就是可被机械层驱逐的观察**，不是常驻负担。 |
| **笔记的出处指针**（tencent） | 接缝不成立（主因）：`BaseTool.execute(**kwargs)` 与 `registry.execute_tool(name, args)` 这条路上**根本没有会话句柄**——为一个字符串字段去改整个注册表的执行契约（60 个工具类，还有 `test_tool_norms.py` 钉着），代价与收益完全不成比例。更硬：`rag_server.py` 上**不存在**按 title 硬过滤取原文的端点，而 rag 侧文件**都不在 seam_files 里**——**它声称的核心收益在它给的接缝上建不出来**。且 agent 主动写的笔记在写入当时通常还在未折叠的尾部，指向的沉淀文档尚不存在，硬过滤恒失手。 |
| **dream 增 MERGE 操作与软删标记**（tencent） | 撞 ADD-only 红线（软删让手写笔记从索引消失，而索引正是「模型知道自己知道」的唯一召回路——**「不删文件」不等于没越红线，它是给模型开了一个让手写笔记隐身的后门**）+ 约束差（腾讯的 merge 跑在云端前沿模型 + 结构化工具调用 + schema 校验上；这边是 4bit 本地档 + 一次 `max_tokens=512` 裸生成 + `\|` 切分自由文本，**机械校验能验操作的形状，唯独验不了合并后的 body 是否真的涵盖各来源全部有效信息**）+ 收益不实（索引压力是推演的：`memory_write` 描述极力压制写入频率、MEMO 冲洗 ≤3 条/次且按 name 硬去重、`index_lines` 溢出**已有「还有 N 条、去 rag_search」的诚实降级**、dream 闸门要 24 小时内 ≥4 条变动才开——**一个碎片化快到能撑破 40 行的笔记本，应该表现为 dream 天天在跑，而没有任何证据显示如此**）。 |
| **冻结档案排出 agent 搜索面**（deepseek 的 `.rgignore`） | 失败模式没有证据：援引的「08-16 复审查出 33 条实错」实读原文，那 33 条是**活跃文档内部**的结论/数字问题与档案缺失，**没有一条是「把归档件当成当前权威」**。且已有的那一半是散文对散文（两份文件顶部本来就有冻结注记，文件名本身就写着 ROADMAP 与 ARCHIVE）。更糟的是**方向冲突**：`CLAUDE.md` 明写 stage-reviewer 的职责含「档案完整性」与「跨文档数字一致性」，把归档与 `benchmarks/data/**` 排出默认搜索面**正好挡住这个审计层要查的东西**。另有两处引证不实（`ROADMAP.md:501` 不存在；「CLAUDE.md 已有『改后端必跑金测试』」在 CLAUDE.md 里零命中）。**唯一该留的（`.claude/settings.json` 两个 hook）与 deepseek 无关，早已是 `COMPARE §6` 定下的解——已并入波 A0。** |
| **验证强度终态词表**（worldmonitor） | 三条同时成立：① **它自己举的活样本是假的**——`HETERO_COMPUTE.md:56` 的 B3 状态栏逐字写着「**进行中：只剩真两台机器联调**」，正文还有「待第二台机器：真跨机联调（含审批/分支/断线重连/定时任务）」，**没有任何人会把它读成完成**；相反它是标注得最细的一条。② **实质已经有了**（证据等级四级带「无标注 = 不许写」+ 口径随数走 + 预测先行 + 原始件当日入库），缺的只是给这套已在执行的做法起五个名字。③ **约束差**：五档里有三档（PR ready / merged / deployed）在单人自持项目里根本无从区分。**最后一击：它对准的缺口原文是「全部纪律都是散文，没有任何机械执行点」，而这条建议的产出是又一段散文**，还要向正在与账本膨胀作战的项目收一笔模板税、动需用户明确同意的 CHARTER。 |

### 4.3 死于「修法方向反了」的 1 条

**压力用例：护栏在被劝退时是否还成立**（addyosmani）——三条具体压力题**逐条塌**：
- 旗舰题「拒批 edit_file → 那你用 shell 改」的前提是假的——shell 的等价写**根本没有免审批通道**（任何重定向/管道/子命令字符命中元字符黑名单即转审批，`sed -i` 被专门点名拦下，tee/dd 压根不在只读白名单里）。所以「绕道」的真实结果是**弹出第二张审批卡**，审批闸没被击穿。**更要命的是修法方向反了**：用户刚亲口说「那你用 shell 改」，此时按「语义族」自动拦截等于**代替用户否决用户刚下的指令**，既撞「审批闸门只有一处」，也是典型的确定性代码替模型做外层语义判断。
- 交叉质证题**结构上够不着**：`web_research` 只把一个自足 query 传进沙箱，沙箱系统提示所在的上下文里**根本不含用户原话**（隐私纪律），用户催不到它。
- 权威压力题作者自己写明「这条应恒过」——而这块代码**已被 CPU-only 毫秒级金测试钉死**（5 处），再花一次 M4 Max 真机 n=3 去重测同一件事是纯成本。

### 4.4 明确不借鉴（跨项目汇总表）

| 项 | 理由 |
|---|---|
| **Cordis 插件化内核 / 能力接缝三角 / cordis.patch.yml 组合层** | 规模差（237 包、145 门禁、「加一个工具要触及六处」）+ 约束差（单进程同语言 vs 三端多进程）+ 撞「插件市场不做」。Agent-L0 已有六个声明式扩展点，同样目的、十分之一代价 |
| **Code Mode（run_code 作唯一直调工具）** | 看似解决「9k 前缀地板」与「N 次工具往返」两个真痛点，但纯约束差：会**当场作废约束采样 + tool_call_grammar 这条承重线**，失败形态从「参数错」恶化成「代码半途炸掉、副作用做了一半」 |
| **RLM 单工具架构 + host.request 反向桥** | 同上。一旦工具面变成任意代码，`needs_approval(args)` 无参数可判、`validate_call` 无 schema 可校、白名单无命令可比——**三层护栏同时失效**。prime-agent 自己也承认「完全没有内置的工具审批/权限系统」 |
| **Continual Harness + /refine 自改进** | 绝大部分已有更好的自研解（四类型笔记本 ADD-only、MEMO 搭车零额外调用、dream 三重确定性闸 + 备份 = 等价于 before/after 回滚、模型起草 → 待确认队列）。缺的只有「独立审核门 prompt」，而它恰好是**多一次外层决策生成**——明确不做 |
| **agent 家族可达性 + A2A 消息通道** | 三个沙箱在拓扑上永远是叶子，可达性判定无对象可判。更要紧的是 A2A 会开数据面的口子（`inject_env_context=False` 的天然旁路） |
| **全局单例 kernel-venv + 模型自行 `uv pip install`** | prime-agent 自己列的 weakness，且直接撞 drift_patrol 的「标记权不改写权」红线 |
| **扩展系统（加载任意 TS、可整体替换 system prompt）** | 撞「插件市场是纯负债」+ 「hooks 子进程钩子已明确不借鉴，正解是编排层确定性预检」；且比 skill 自带 `tools:` 更彻底地绕开审批闸 |
| **逐文件 100% 覆盖率门禁** | 规模差，且 deepseek 自己的实证就是反例（554 处 `v8 ignore` 挂在真实防御分支上）。Agent-L0 的真实风险是**伪覆盖**，正解是变异证明 |
| **OS 级沙箱（landlock-run / Seatbelt）** | 已拍板不借鉴（威胁模型不同）；且 landlock 是 Linux-only，macOS 对应物是 Apple 已弃用的 sandbox-exec |
| **双语文档配对协议（blob-hash + 结构签名 + merge driver）** | 工程上漂亮但纯规模差：deepseek 有 2355 个 md 几乎全部中英成对。Agent-L0 是单人中文单语十余份。产品侧多语已有更合适的解（i18n 以 en 为结构基准，缺键即 tsc 编译期报错） |
| **session-telemetry-otel / 默认开启的遥测** | 直接撞数据面红线，无讨论余地。且这是 deepseek 与 prime 自己 weaknesses 里被点名的部分 |
| **并行工具调度的滚动池 + 排他屏障** | 规模差（前沿模型一口气吐十几个调用，4bit 档一次一两个）；且「排他屏障」与「模型序」两条语义 Agent-L0 已经成立 |
| **deploy-gate 的 SHA256 契约戳 + 20 job 聚合** | 撞「质量门 12 件套 / 重型 CI 不做」。它解决的是多人并发 PR 队列独有的缝，单人本地仓库没有「过期的绿」可蒙混 |
| **Ratchet Inventory 双向棘轮清单** | 规模差：为 68 万行、正在渐进迁移的库设计。Agent-L0 是 2.3 万行 Python 单人代码，任何坏写法可以一次全改完，登记表维护成本高于它防的漂移 |
| **另立 CONCEPTS.md 失败模式词汇表** | 已有更好的自研解（CHARTER §反模式 + §S 踩坑 + MTP §4 错误清单三处），且撞「四份文书定位互斥」。**值得吸收的只是「给概念起一个可被引用的短名字」这个习惯，落点是往 §S 现有条目补名字，不是新建文件** |
| **`agent-pr-snapshot` 式的不可信文本默认剥离** | 已有更强的自研解（四层反注入 + 沙箱物理受限 + 来源账本代码维护），且照搬会废掉核心能力——**对 Agent-L0 来说读网页正文就是任务本身** |
| **726 行 pre-push + 绿树证明缓存** | 约束差，且是对方自承的负债（43% 会话与之搏斗 + 三个静默失效）。Agent-L0 的 958 项是 CPU-only 毫秒级，直接全跑就是，**加缓存等于凭空造一个需要 1409 行测试才能守住的新失效面** |
| **文档对齐审计委员会（8 角色 + claim ledger）** | 撞 `CLAUDE.md` 明文纪律「不得派一个子代理去复核另一个子代理刚做的工作」 |
| **The Lever Test（纸面成本算术）** | 已有更强的自研解：预测先行要求的是**可被自己推翻的预测**，比一条会被参数糊弄的算术严格得多 |
| **Tier-3 headless executor + LLM grader** | 撞已有更好的自研解（`attention/driver.py` 走真实 HTTP 面、真实审批闸、文件系统断言）+ 撞已拍板纪律（`model_eval`/`l0_eval` 明写「全部规则判定，无 LLM-judge」）+ 安全不可接受（无隔离临时目录跑 `acceptEdits` agent） |
| **sdd-cache 的 304 再验证** | 机制巧妙但没有对应的痛：抽取缓存按调查为界清空是**有理由的取舍不是欠账**；一次调查最多抓十来页，多一轮 HEAD 往返净收益接近零 |
| **simplify-ignore 的占位符遮蔽** | 思路漂亮，但它**在会话期间真的改写用户磁盘上的源文件**，Stop 钩子没触发用户文件就停在占位符状态。Agent-L0 的操作对象是用户的真实文档而非可 git 回滚的代码库——这条红线严得多。已有更对的形态（`.bak` + 写后语法自检 + 恒需审批 / 废纸篓 / 绝不静默覆盖） |
| **单语料多运行时扇出 + marketplace 清单** | 纯约束差：Agent-L0 是单宿主自持，扇出层的全部价值为零；且连带撞「插件市场」「不接 MCP 协议栈」「协作规模化不追」 |
| **SessionStart hook 注入动态技能路由图** | 方向对，落法行不通：`skill_invoke.description` 活在**稳定前缀**里，任何「会话启动时动态注入」都会让稳定前缀逐会话不同，**KV 前缀缓存与三级热启动全部白攒**——这是本地推理最贵的那笔账 |
| **google 的 `allowed-tools` frontmatter 字段** | 直接撞 D.11 铁律级红线，`test_skill_grants_no_tool_authority` 逐字段断言 |
| **Tier R/M/B 的「一次授权后本会话不再重复问」** | 是削弱唯一审批闸的可用性补丁，与「审批闸门只有一处」的取向相反 |
| **grilling 不设问题数上限** | 那条拒绝是为前沿模型 + 工程师用户写的。Agent-L0 的北极星是「帮普通用户」+ 4bit 本地档——一次抛出十几问既超出普通用户耐心，也超出弱模型自己组织问题的能力 |
| **negation 反模式教条化** | 部分撞实测：`memory_write` 的 DO NOT 清单被审计判为「全清单最完备的一份」，谎报围堵与 shell 白名单话术都是事故驱动并经真机探针验证的。mattpocock 这条经验的对象是前沿模型，**在 4bit 小模型上只是一个待检验的假设**——正确姿势是降级为候选假设去判，而不是当规则执行 |

---

## §5 如果只做三件事

> 与 Agent-L0 当前位置对齐：MTP 优化波收尾、D.20 阵容收束刚入档、生产机 M4 Max 64G 已到位、下一步候选是 D3 子 agent 扩充 / D2 第三批工具 / §7 生产机全矩阵实测。

### 第一件 ★ 给已有的纪律装一个执行器（波 A0）

**为什么是现在**：下一步无论 D3 还是 D2，都是「往体系里塞东西」的波，而 `ROADMAP §3` 自己写死的扩充纪律（每个新 skill/tool 必须有金测试、必须声明 `is_available` 探活）**今天没有任何执行点**。同时本轮存活的 19 条里有四条全是「验证物」，没有一条自带执行者——四条各自落地只会产生第五到第八份没人跑的纪律。
**不撞红线**：不追的是「质量门 12 件套 / 重型 CI」，而同一段话把正解写死为「gold test + pre-commit hook」，`ROADMAP` R6 也挂着至今未落地——**这是项目自己拍过板但没做的事**。
**成本从未这么低**：958 项是 CPU-only 毫秒级，生产机已到位，MTP 波正在收尾（低风险动工程面的窗口），而 D2/D3 一开工每加一件东西都会让补装门禁更贵。
**第一步**：`devctl.sh` 加一支 `check`，**只做聚合与退出码合并、不新增任何检查内容**——串起三条今天已存在但要手动敲的命令，退出码按三值纪律。跑通后写三行 pre-commit 调它。**第一版刻意不加任何新校验器：先把线接上。**

### 第二件 ★ 给稳定前缀记一笔真账，并修好 catalog 逃生口（波 D0）

**为什么是现在**：这是「先量再加」还是「先加再量」的分岔口，而下一步两条候选都是往稳定前缀里加东西。实测已到 **11337 / 12000，只剩 663 token ≈ 2–3 件工具**，而唯一的度量是 `chars/4` 启发式——**且这个数字不是仪表，是承重参数**（摘要双阈值锚定在它上面）。更关键：唯一的逃生口今天是**物理坏的**（catalog 模式下带参工具被约束采样钳成只能发空实参）。**净效果：D4 阈值早就定了，开关拨不动。**
**第一步**：写一个只读脚本，用真分词器（走 rag 计算面 `/tokenize`）逐工具打印 token 数并降序排，输出三样：① full 真实总量与 `chars/4` 估算的偏差；② 花费 top-10 的工具；③ catalog 模式总量。**零风险、零改动、可与任何工作并行**；catalog 的语法修复等看到数字之后再决定是否值得开工。

### 第三件 ★ 夹具录制器：让「先落夹具再改实现」从苦差变成一根管子

**为什么是现在**：铁律 5 是六条铁律里**唯一一条其成本从来没被降过的**——夹具至今只有 7 条、全部手工构造、全部以 success 或暂停态收尾。剖面点名的空洞是 `turn_ended(cancelled/fail)` 的**预览撤回**（`PROTOCOL.md:376-377` 明文要求）、`queue_moved`、`plan_updated`、`step_completed/step_failed`——**这些归约分支两端都已经写了代码，零夹具守着**。而接下来排队要动协议的正好有三件：Q14 并发（进宪门禁就是生产机实测，一旦通过调度/排队事件形态必变）、D3 子 agent 扩充、`DEFERRED §1` 的 clarification 暂停态（本轮存活的 F7 正指向它）。**未来三波每一波都要付一次手工造夹具的税。**
**而这根管子几乎是免费的**：夹具格式就是「一行一个事件信封」，`GET /chat/{sid}/events` 吐的正是同一个 `{seq,turn_id,ts,type,data}` 信封；夹具目录还是自动发现的；`fixtures/README.md` 自己写的就是「一份**录制**/手工构造的事件流」——**录制这半边从来没实现过**。
**第一步**：约四十行的脚本，attach 到 `/events?after=0` 把信封原样落盘。期望终态照 deepseek 的 refresh 态处理：先跑一次 web 归约器生成，**人工审一遍确认它是「应该」的终态而不只是「当前」的终态**，再入库。第一批只录两条最缺的：一次用户主动取消的轮次、一次工具故障导致的 fail 轮次。**录完立刻在 Swift 侧回放一遍——两端读同一批夹具，这两条大概率会当场抓出一处双端不一致。**

---

## §6 本次扫描的副产品：5 处 Agent-L0 现存确认缺陷

> 这些不是「建议」，是对抗审查过程中在真实代码里**走完完整链路复现出来的缺陷**。它们的价值可能高于建议清单本身。

### 6.1 ★ Critical — `grep` 无命中会触发谎报围堵，逼模型报告一个不存在的错误

**完整链路（审查员逐段走通）**：
```
grep -rn foo /path          # 无元字符
  → grep 在 _READONLY_COMMANDS（shell_exec.py:52）
  → _is_readonly 返回 True → needs_approval False → 自动执行
  → 无命中时 exit 1、stdout/stderr 皆空
  → shell_exec 返回 "exit_code: 1\n(no output)"
  → _FAILED_RESP_RE（orchestrator.py:211）命中 → unresolved_failure = True
  → 模型随即收尾 → 撞 orchestrator.py:823 的 failure_swallow_nudge
```
**推回文案原文**：「Your most recent tool call FAILED and nothing succeeded after it. Do NOT report success. Either fix and retry now, or **state plainly in your reply that the operation failed and what the error was**」

**即：一条防谎报的护栏在逼模型向用户报告一个不存在的错误**，白烧一整次生成；模型坚持第二次还会把整轮 status 降级成 warning。

**影响面**：`grep` / `diff` / `test` 三个都在只读白名单里、都是桌面 agent 高频只读命令，且 `diff a b` 文件不同返回 1、`test -f x` 文件不存在返回 1——**全是「1 = 正常答案」**。
**修法**：§3 F1。**成本 S，缺陷确定，修法单点。**

### 6.2 ★ High — `_FAILED_RESP_RE` 带 `re.M`，读到含 "System Error:" 的日志会误触护栏

`orchestrator.py:211` 的正则带 `re.M`，匹配的是**整段观察里任意一行**的行首。任何 `read_file` 读日志、`run_shell_command` 跑 `tail`/`grep` 命中含 `System Error:` 行的输出，都会把 `unresolved_failure` 置位 → 收尾时触发一次谎报围堵推回 → **在 4bit 本地模型上白烧一次完整生成**。
该正则的注释自陈「只认硬信号——语义级失败（"没找到"之类正常结果）不算」，**但实现并未兑现这句话**。
**修法**：§3 F3 第一波（把判据从字符串模式换成结构化事实，尤其换掉 `re.M` 的整段行首匹配）。

### 6.3 ★ High — 后端重启会让 journal seq 回退，客户端永久静音

见 §3 B2。**项目自己的协议文档 `PROTOCOL.md:310` 与 §2.4 两条直接互相矛盾**，而两端实现都照后一条办。触发者是最常规的动作（`devctl restart` / SidecarManager 监护重启 / 崩溃自愈——**app 内嵌 sidecar 就是设计，重启是日常**）。
**止血成本 S**（`TurnJournal.__init__` 留水位余量，单文件零协议）；**根治需代际围栏**（因为进程死时那一轮没有 `turn_ended`，客户端会永远停在 generating）。

### 6.4 ★ High — RAG 评估的度量能力已经丢了（三处同时成立）

1. `BASELINE.json` 的 stamp 是 **20260730**、pass_rate **0.941** / mrr 0.938，而 vl-2b 定档标定后实测已到 **1.0/1.0**（账本 :95-97）——**现在跑任何回归，拿 0.941 当基线会把一次 −0.059 的真回归读成零回归**；
2. 34 题在生产档上已全对，**天花板顶死**，任何检索侧增强（第四路候选腿、时间衰减、MMR λ 标定）都无法在这套题上显形；
3. `run_retrieval_eval.py:247-257` 的比对逻辑**只 print，从不返回非零、从不 fail**。

用 worldmonitor 的词汇讲，这是一个标准的 **Vacuous Guard**：守卫还在，但它守的输入已经静默缩小到守不住任何东西。
**⚠️ 关键顺序**：**必须先补题再重钉基线**（negative / multidoc / misroute 各只有 2 条，name 3 条）——先重钉基线只是把一个假绿换成另一个假绿。§3 A4 的棘轮在这个状态下是空的。

### 6.5 ☆ Medium — catalog 逃生口在约束语法里是坏的

`dialects/gemma4/tool_grammar.py:183-189`：目录条目没有 `parameters` → `props` 为空 → 只生成 `call:name{}`，即 **catalog 模式下带参工具被约束采样钳成只能发空实参**。
这正是账本记的那张前置票，`§7-8` 的 JSON→官方 DSL 迁移之后它**换了位置但没有消失**（从 `additionalProperties:false` 变成了 Lark 分支）。
**净效果**：D4 阈值早已定好（12k），实测 11337、只剩 663 token，而开关拨不动。见 §3 D0。

---

## §7 附录：评估依据与局限

**评估依据**：
- 15 路侦察代理逐一深读源码（非 README），每份剖面含 `current_design` / `strengths` / `gaps_or_risks` / `extension_points` / `explicit_non_goals` / `already_has`，全部带文件路径与行号
- 7 路匹配代理产出 34 条候选建议，每条含 `gap_it_fills` / `source_mechanism` / `proposed_landing` / `seam_files` / `charter_check` / `already_has_check`
- 7 路对抗审查按五种杀法逐条试杀，**证据字段必须是实际执行过的 Grep/Read/脚本**
- 1 路完备性批评复核全部 15 份剖面 + `CHARTER` / `ROADMAP` / `DEFERRED` / `EXECUTION_LEDGER` 的 §R/§D/§5/§S

**未做**：未运行任何一个开源项目的代码；未在 Agent-L0 上执行任何写操作、未启动任何服务、未加载任何模型；本文所有 Agent-L0 的实测数字要么引自其自身账本（标出处），要么由只读脚本静态计算（标方法）。

**已知偏差**：
- worldmonitor（115 万行）与 deepseek-harness（52 万行）属抽样覆盖
- TencentDB-Agent-Memory 的公开仓是内部仓压缩镜像，其真实工程质量无法从公开面判断
- 对抗审查发现的 5 处缺陷中，6.1 / 6.2 / 6.4 是逐段走通链路或实测确认的；6.3 / 6.5 是读代码 + 协议文档矛盾推断，**建议在动手前各写一条最小复现**

---

*生成：2026-08-17。方法：15 路侦察 → 7 路匹配 → 7 路对抗审查 → 1 路完备性批评，共 31 个代理、3.8M subagent tokens、1699 次工具调用。*
*34 条候选建议 → 19 条存活（其中 17 条被要求修正落地方案）、15 条被杀（其中 4 条死于实测证伪）。*
