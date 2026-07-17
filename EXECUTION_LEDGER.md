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
> 🟢 **2026-07-16：R0–R4 ✅；R5-1 ✅（app 源码入库）；R5-2 ✅（edge 瘦身 2.1G→888M，音频路移设备 A）；R5-3 ✅（内嵌运行时定 python-build-standalone，950M 可搬树，hardened runtime 签名后 212 金测试全绿）。R5-4 ✅ **完整收官**（D.16 旋钮 + `lib_edge.sh` 单一真相 + `SidecarManager.swift` 五态监护 + **实机查验五步全过**，查验揪出并修掉 2 个真 bug；222 金测试全绿）。R5-5 ✅（SearXNG 同树随包 +40M，990M 总，322/322 签名，恒为回环实测）；R5-6 ✅（Keychain 多账户）；R5-7 ✅（渲染格 citations + 轻改版色彩设计「青瓷」，设置页拆 Tab）；R5-8 ✅（iOS 瘦客户端核查 + TestFlight 实跑，build 21 已分发）；R5-9 ✅（bundle 组装 1.0G 交付形态 + 无头验收全过 + GUI 三项用户实机确认 + 实机查验四轮修复（密钥入库/鉴权门/Developer ID 签名/会话恢复）+ 两轮 UI 打磨，v0.16.0(30)）。**R5 全线收官（2026-07-17），CHARTER 三处同步已获用户同意并落地**。**D1 首批进行中**：新剧本 ×4 + D.17 斜杠确定性分发（e4b 不会主动调 skill_invoke，实测倒逼）+ 金测试 232；chart-report/ingest 验收 ✅，briefing/research/chart-report 三案疑难**同根归案（S.9）**：inference/server.py 约束采样构造少传 schema_builder → 服务端掩码永久禁用 → e4b 裸奔畸形 JSON——已修（一行）+ AST 调用点钉（金测试 233），infer 已重启、探针全绿。实机三轮后：**/research 端到端全通（D1 首个完整闭环）**；/briefing 剩模型智力项（观察清单：约束内调用块 2048 token 未收口）；点名不可用剧本改为如实告知（v0.16.0(34)，金测试 241）。chart-report 补测通过（架构链路含纠错回环全贯通）。**D1 首批收官**（5 剧本全实测，7 架构缺陷修复，模型智力项入观察清单）。**D4 ✅ 已定**（2026-07-17 用户拍板：full 声明 >12k tokens（≈50 工具）时切 catalog，D2 全程留 full；切换有两张前置工程票——约束/提示词解耦（catalog 条目无 parameters 会把 arguments 钳死成 `{}`）+ 目录摘要瘦身；顺带修正摘要阈值口径：锚定稳定前缀，金测试 242）。**D2 首批 🟡 已落地**（2026-07-17：桌面日常七件套 find_files/list_dir/open_path/clipboard×2/notify_user/system_status，40 工具 full 声明 8.8k tokens < 阈值，金测试 255；选型口径=生产环境未定型前按通用日常使用率，用户真实需求清单后补）。首批九问实测验收 ✅（find_files 自动降级内容模式修复 + TCC 过滤定性 + 三调三返定性=护栏正常，v0.16.0(37)，金测试 256）。**第二批实测验收 ✅（2026-07-17 六问全过，三次磕绊全被护栏同轮接住，纠错回环首次全套野外验证）——D2 阶段性收官**（46 工具 9.6k tokens，金测试 268，v0.16.0(38)；用户真实需求清单待生产环境定型后补充，届时按需加第三批）。**D 阶段工程票 ×2 ✅**（2026-07-17：注册表热重扫（只增不删 + 斜杠前置自动重扫 + REST 端点）、sidecar 孤儿 reap（bundle 布局限定），金测试 274，v0.16.0(39)；孤儿回收的实机回归 ✅ 通过（2026-07-18 用户复刻 kill -9，旧 pid 死/pid 文件换新/对话正常，销案））。**UI 打磨 ✅**（2026-07-17：消息主操作移到气泡下小钮行 + 复制成功反馈 + 删助手气泡右上角悬停复制钮，右键菜单保留待扩充，v0.16.0(40)；第二批：输入栏文件拖放 + 会话列表/消息时间戳三档规则（轮次 timestamp 字段 additive 入协议 §2.3），金测试 276，v0.16.0(41)；第三批：斜杠候选提示（GET /api/v1/skills + 前缀过滤候选 + ↑↓/Tab/回车补全 + 命中徽标，缺依赖灰显），金测试 280，v0.16.0(42)。R2b 澄清：skill_invoke 工具轨一直在，31B 后口头点名零代码实测即可；第四批：会话列表冷启快照（按后端地址隔离，真相到达即替换），v0.16.0(43)）。**媒体抓取 ✅**（2026-07-17：media_download 工具（yt-dlp 包壳，probe/audio/video 三模式，播放列表闸+1080p/2GB 熔断+ASCII 文件名，产物走会话 assets 出链接），金测试 286，v0.16.0(44)，用户后续要补充细节；MediaCrawler=用户拍板放弃，remotion/skills=挂起等需求（目录制渐进披露+需 Claude Code 级编码力，等 31B）。**文件面板 ✅**（2026-07-18：产物链接 -50 根治（相对路径+Bearer 双层死，app 内接管唯一正确）——GET /api/v1/files/{sid} listing + 检查器第四页签「文件」（音视频 AVPlayer 流式（Range 206+faststart 皆实证）/图片/QuickLook 三路预览 + 打开/保存）+ OpenURLAction 白名单劫持 + 窗口 minWidth 760→860 修侧栏错位，金测试 290，v0.16.0(45)；返修×2：46=yt-dlp 键名 S.10+动态 minWidth 960/760，金测试 291；47=SwiftUI VideoPlayer 在 macOS 27 beta 实例化即崩→换经典 AVPlayerView；**iOS 对齐 ✅**：共享源免费得全套+行内「分享」钮（UIActivityViewController），TestFlight build 48 已传；49=iOS 实机返修：静音拨键消声（AVAudioSession→.playback）+ macOS 播放窗不可调（frame 补 max），双端 49 已发；50=key 轮换两枚钮（复制/重新生成，macOS-only，重新生成即刻重启执行层旧 key 作废；算力端同模式挂起等生产设备））。下一步 = **D3 子 Agent 扩充**（建议等生产环境/真实需求定型）。Q5/Q6/Q7 已定（D.13/D.14/D.16）；**Q8 ✅ 闭环**：Developer ID Application (G2) 证书已由用户签发装入（有效期至 2031-07-17，partition list 已设，`.p12` 已由用户导出备份）；951M 运行时（322 Mach-O 覆盖签名）公证 **Accepted**（submission `1e13bf2a`，2026-07-16 通过；当日苹果队列积压 ~14h，对照组 tiny.zip 甚至更慢——大包无罪）。签名→公证全链路验证完毕，剩「对真实 .app 公证 + staple」在 R5 收官后的分发动作里做。**

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
| **R5** | macOS app 收编执行层 | 🟡 R5-1~3 ✅ / R5-4~9 ⬜ | R0–R4 | 无 | 铁律 1/4 + 改 CHARTER |
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

**R1** ✅ 2026-07-16
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

**R5** 🟡 R5-1~3 ✅ —— 见 §3.5 详细步骤

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
    | 签名 | 320 个 Mach-O 逐个签 hardened runtime **+ 安全时间戳**，`320/320` 覆盖；**签完 212 金测试仍全绿**。整个「解压→装依赖→验可搬→签名→核对」全程 **48s**（pip 缓存热） |

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

- [x] **R5-4 · `SidecarManager.swift`** ✅ 2026-07-16（含用户实机查验五步全过）
  - 职责：启停 backend + searxng、就绪探活、端口冲突处理、崩溃重启、日志落盘
  - **🔴 顺序**：searxng **必须先于** backend 就绪——`web_research` 的注册是**启动期一次性探活**，
    searx 晚到 = 整轮不注册（`HETERO_COMPUTE.md` §S.3 已入账的事故）
  - [x] **前置 ①（D.16）✅**：`AGENT_DATA_DIR` 旋钮落 `config_loader.data_dir()`。
    账本原列 4 个写点，现场查出 **6 个**——`guardrail_events.jsonl`（events.py 直接写
    backend 根）与 `models/`（权重下载目标）也是运行时写。events / path_resolver 直接
    吃旋钮，app.py 换根。验证三层：8 项新金测试（AST 守卫已变异验证）+ 全量 220 绿 +
    真 E2E（注入 env 起 app，六写点全落数据根）。顺带删无消费者的 `BASE_WORKSPACE`
  - [x] **前置 ②（D.13）✅**：`scripts/lib_edge.sh` 提取完成（405 行）。机制函数
    （probe/探活/spawn/wait/stop/status）+ SearXNG 配置生成 + `start_searx`/`start_backend`
    + **`edge_up`/`edge_down` 编队**（searx 先于 backend、searx 容错）全部单一真相化；
    devctl.sh 799→516 行，edge profile 分支改调 `edge_up`。布局参数化 = 7 个 `EDGE_*`
    环境变量（bundle 与仓库的差异**只**在这里）；密钥不进 lib（devctl source `.env.local`，
    app 将从 Keychain 注入——R5-6）。**SidecarManager 的调用形状**：直接执行
    `lib_edge.sh up|down|status`（source 与直执自动区分）。实测：devctl status 全通、
    searx 起→重入跳过（managed_or_report 认 pid）→停 全走新 lib、缺参数明确报错、
    220 金测试全绿。⚠️ 第三次踩「变量后紧跟全角字符」坑（bash 3.2 吞字节），已给三个
    shell 文件建了正则扫描，全部清零
  - [x] **实机查验五步全过** ✅ 2026-07-16（用户执行）：external 不误杀（app 退出后
    devctl 的 backend 存活）/ 接管态拉起 / kill -9 崩溃自愈 / 随 app 收摊干净 / 端口
    冲突呈现。**查验揪出两个真 bug 并当场修掉**：①设置页仓库路径框藏在开关后面，
    与「先填路径再开开关」的使用顺序自相矛盾（v0.16.0(17) 改常显）；②**端口守卫
    盲区**——占用者对 /health 回 404 时 is_ready 不通、managed_or_report 根本不会被
    调到，照常 spawn；且 BSD 的 SO_REUSEADDR 语义下特定地址与通配地址可**同端口
    共存**（0.0.0.0 的 http.server 占着 14159，backend 绑 127.0.0.1 照样成功），双监听
    悄悄成立。修法 = `guard_port_before_spawn` 第二道前置：有监听无 pid 记录 → 硬失败
    点名占用者；有监听 pid 存活 → 等就绪不双拉（顺带修掉「对启动中服务重复 start 会
    双 spawn」的既有缺陷）。盲区从 devctl 原样搬来非提取引入；单一真相兑现——修一处
    devctl 与 app 同时得到
  - [x] Swift 侧：`Sources/App/SidecarManager.swift` ✅ 2026-07-16（双端 BUILD
    SUCCEEDED，已装 /Applications v0.16.0(17)）
    - 五态机：disabled / **external**（devctl 起的只连接不接管——app 退出也不碰它，
      「顺手杀掉用户的开发后端」是最恶劣的越权）/ starting / running / failed(lib 原话)
    - 崩溃重启：`DispatchSourceProcess`（kqueue EVFILT_PROC，同用户可盯**非子进程**——
      lib 拉起的是脱管守护进程）盯 backend.pid/searx.pid；重启**只会重跑 `up`**（lib 幂等，
      活着的跳过、死了的按序补起），5 分钟窗口内超 3 次停手亮红，防 crash-loop 空转
    - 布局：bundle（Resources/backend 存在即选中；数据/日志/searx-settings 全落
      `AGENT_DATA_DIR`）/ repo（设置里填仓库路径；**dev 桥接**读 `.env(.local)` 注入同名
      变量——bundle 布局刻意不走这条，密钥形态是 Keychain，R5-6）
    - 收摊：`applicationWillTerminate` → `lib_edge.sh down`（仅收自己拉起的）
    - UI 最小面：设置页一节（开关/仓库路径/状态/故障原话）+ 菜单栏一行；Views 重做归 R5-7
    - 实测：`up` 失败面全链路验过——searx 先行就绪、RAG 门拦下 backend 并给跨机指引、
      退出码 1 → Swift `.failed` 呈现 lib 原话；`down` 幂等清场

- [x] **R5-5 · SearXNG 随包** ✅ 2026-07-16 —— **一棵树两个服务**，实测 +40M（990M 总）
  - **定案：searx 非 editable 装进 R5-3 的同一棵 PBS 运行时**，不背第二棵树（+174M）。
    三方投票定的：`pip check` 干净（名字面交集只有 PyYAML 且同 pin）、searx 从树里
    真跑通 JSON 搜索、**222 金测试在混装树上全绿**。`SEARX_COMMIT` pin 收编进
    `lib_edge.sh`（devctl setup 与 build-runtime 共用一份）
  - **两条顺序约束（实证，写进脚本注释）**：① searx 必须在**签名之前**装——签名后
    hardened runtime 的 library validation 拒载非同 Team ID 的 `.so`（实证 msgspec
    dlopen "different Team IDs"），**签过名的树从此拒绝带原生扩展的 pip 安装**；
    ② 必须**非 editable**——editable 装的是指回 searxng-src 的引用，树一搬就断
    （可搬性探针已加 searx 位置断言）。另：PBS 不带 setuptools，构建前先装
  - `settings.yml` 由 lib 的 `ensure_searx_settings` 生成到**数据根**（bundle 只读；
    R5-4 已参数化），bundle 形态全链路已实测：外源 settings + 签名树 + JSON 搜索 ✅
  - **🔴 恒为回环已验**：`lsof` 实测监听仅 `localhost:8080`（limiter 已关且无任何鉴权，
    离开回环 = 人人可用的搜索代理）
  - SidecarManager bundle 布局同步：`EDGE_SEARX_PY` 指同一个 PBS python（v0.16.0(18)）
  - 签名覆盖 320 → **322/322**（searx 的原生扩展一并入签）

- [x] **R5-6 · 密钥形态改造** ✅ 2026-07-16 —— Keychain 多账户 + 首启生成（v0.16.0(19)）
  - Keychain 枚举扩为多账户（`load/save(account)`），既有 `agent-api-key` 账户名
    不变——老用户的钥匙串条目继续有效
  - **AGENT_API_KEY 首启生成**（32 字节系统熵 base64url）而非退化为空：虽是同进程
    回环，生成一把成本为零，还顺带关掉「回环上其他本机进程白嫖 backend」；存进
    `settings.apiKey` 使 app 客户端与注入 backend 的是同一把
  - `INFER_API_KEY` / `RAG_API_KEY`：设置页两个 SecureField（即改即存 Keychain），
    bundle 布局注入子进程环境，与设备 A 的 `.env.local` 填同值
  - 两布局分工（R5-4 已定形）：repo 布局走 dev 桥接读仓库 `.env(.local)`；
    bundle 布局走 Keychain——本期只补 bundle 侧
  - **🔴 护栏保持**：非回环绑定 + 空密钥 = 拒绝启动——backend 侧既有代码
    （B3 §7 预留 2 已代码化），本期零改动、自然保持
  - 双端 BUILD SUCCEEDED；**bundle 布局的注入链路待 R5-9 组装后才可实测**（现在
    Resources/backend 尚不存在，bundle 分支不会被选中）

- [x] **R5-7 · Views 重做 + R3 的渲染格 Swift 侧** ✅ 2026-07-16（渲染格 + 轻改版色彩设计）
  - 保留 `Store/`（归约器，夹具锁定）+ `API/`（§D.7）
  - [x] **渲染格 Swift 侧 ✅ 2026-07-16**：`ToolResponseItem.render`（hint 不收窄成枚举，
    与 web 同一决定）+ `CitationItem.from` 容错解包 + `CitationsView`（与 web `Citations`
    同一信息面，默认展开）。不认识的 hint 照旧灰框（fail-open）。双端 BUILD SUCCEEDED，
    Swift 夹具 7/7（含 007）。v0.16.0(20)
  - [x] **chart 接管交付：审视后不做**（防重议记录）。markdown 交付有三个 render 载荷
    给不了的性质：①模型自己看得见（后续轮次能引用图；render 被 D.12 白名单隔离在
    模型上下文外）②随文本进 history/RAG 沉淀 ③对所有客户端与历史会话统一生效。
    `_append_missing_asset_links`（orchestrator.py:36）是确定性交付兜底，正是 R3 判
    「chart 是最差试点」的原因——接管过去 = 用三端改动买一个更差的交付面
  - [x] **Views 轻改版 + 色彩设计 ✅ 2026-07-16**（用户裁决：轻改版+色彩，设计由我规划）
    - **设计语言**（`Views/Theme.swift` 单一 token 源，各视图只引用不自造）：主色
      「青瓷」（浅 #177A7A / 深 #66C4C4，刻意避开系统蓝——自定义状态一眼可辨是 app
      的意志）；思考=紫罗兰、成功=苔绿、警示=琥珀、运行中=主色；浅色暖纸白底+白卡片、
      深色碳灰底+浮起卡片；4pt 网格，气泡 12 / 卡片 8 / 小件 6 圆角
    - 铺装面：MessageView（用户气泡青瓷染、助手卡片+发丝线、思考紫罗兰淡染块、
      citations 卡片化）、ChatView（画布底、活动条主色、挂起横幅弃系统蓝）、
      ComposerView（输入框描边、发送键主色、思考开关紫罗兰）、ApprovalCard（琥珀
      语义收细）、全局 `.tint` 收口
    - **设置页拆 Tab**（macOS：连接/通知/执行层/外观——实机查验实证的拥挤痛点；
      iOS 保持单列 sheet 惯例）
    - 双端 BUILD SUCCEEDED、夹具 7/7、v0.16.0(21) 已装；信息架构与 Store/ 归约器零改动

- [x] **R5-8 · iOS 瘦客户端** ✅ 2026-07-16（§D.3）——核查项，非建设项
  - **平台守卫审计**：两 target 共享同一份 `Sources/`，R5-4~7 全部新增件核过——
    `SidecarManager.swift` 全文件 `#if os(macOS)`；`SettingsView` 执行层 Section 内建守卫；
    `Theme.swift` 双端各有 dynamic 实现（NSColor/UIColor）；`AppSettings` 的 sidecar 四字段
    在 iOS 侧只是闲置的 UserDefaults/Keychain 读写，无副作用（SidecarManager 类型在 iOS
    不存在，误引用会直接编译失败——守卫是编译期强制的）
  - **角色不变核实**：iOS 仍是纯瘦客户端（铁律 4），默认回环地址；跨设备按既有说明
    `tailscale serve` 反代 14159——sidecar 拉起的 backend 与 devctl 同为回环绑定，
    iOS 侧无感知差异（指向 macOS app 内的 backend ✅，无需任何 iOS 改动）
  - **通路实证**：iOS Debug BUILD SUCCEEDED；`release-ios.sh` 全程无人值守跑通
    （xcodegen → Release 归档 → 直传 ASC，Upload succeeded），v0.16.0(21) 已上
    TestFlight（青瓷主题 + citations 渲染格随包到 iPhone）
  - 附带发现：backend/.venv 经 R5-2 瘦身后无 pyjwt，`wait-testflight.py` 需独立 venv
    （其头注本就说「任意 venv」，发布工具依赖不回灌边缘 venv）

- [x] **R5-9 · 验收** ✅ 2026-07-17 **完整收官**（无头全过 + GUI 三项用户实机确认 +
  两轮 UI 打磨定案 + CHARTER 三处同步已获用户同意并落地）
  - **bundle 组装落地**（隐含前置，本阶段最大件）：新 `apple-app/bundle-macos.sh`——
    Release 构建 → Resources/ 注入 backend（rsync 排除 .venv/数据目录/测试）+
    skills/ + python（950M PBS 树，ditto 保符号链接）+ scripts/lib_edge.sh +
    config.yaml → `compileall` 预编译（S.8）→ 私钥零泄漏断言（包内 .p8/.env* 即
    组装失败）→ 重签 → 安装。产出 1.0G，v0.16.0(22)。与 install-macos.sh 分工：
    彼为日用快装（repo 布局），此为交付形态（双击即全栈）
  - **无头烟测**（复刻 SidecarManager bundle 分支 env，数据根=临时目录）：
    searx 就绪（恒回环）→ 分词器 → backend 就绪；`/health` ok；**工具数 36 断言
    通过**（= 仓库裸注册 34 + app.py 装配补注 memory_write/skill_invoke——风险 5
    「打包后静默丢工具」排除）；跑一轮后 `codesign --verify` 印章完好（S.8 闭环）
  - **冷启动（无头）**：全栈 down→up **6.6–9.8s**（searx+分词器解析+backend 就绪；
    GUI 双击在此之上加 SwiftUI 启动，待实机；Gatekeeper 首验一次性成本另计）
  - [x] **原生 E2E 20 项全绿**（`Tests/run_e2e.sh`）——**且靶子是 bundle backend**
    （批 1 11 项含真实模型回复：bundle→设备 A 推理链路通；批 2 9 项）。附带修掉
    E2E 环境耦合缺陷：批 2 资产检查写死已被回收的开发机会话 → 改为自带夹具
    （`Tests/fixtures/chart-fixture.png`，run_e2e.sh 跑前种进 `AGENT_DATA_DIR`）
  - [x] **夹具双端回放全绿**（web vitest 8/8 + Swift 7/7）；金测试 222→**223**
    （+APNs 兜底断言）
  - APNs 密钥 bundle 形态安家：相对路径仓库根落空 → **数据根兜底**（app.py，
    私钥永不入包；放 `~/Library/Application Support/dev.enkin.macagent/.keys/` 即启用）
  - 附带发现：bundle 首启需 HF 可达（tokenizer-only 拉取）——烟测当日撞上 HF
    整站不可达（504/超时），种本地快照后通过；离线首启的预种优化留给 D 阶段
  - **实机查验第一轮（2026-07-16）**：连接/鉴权/执行层运行中/会话落 Application
    Support 全过；首轮对话炸出「核心引擎故障」——**inference-server 401**：
    INFER/RAG_API_KEY 是 spawn 时注入 env 的（R5-6），先开开关再填密钥 = 正在跑的
    编队永远拿不到新值（RAG 建类目 401 同源）。修复（v0.16.0(23)）：
    ① `SidecarManager.restart()`（收自己拉起的→重走 resolveLayout 取 Keychain 新值；
    external 不碰）+ 设置页「重启执行层（应用新密钥）」按钮 + 生效说明文案；
    ② 系统条报错精简（实机反馈）：`SystemNoticeView` 只亮首行结论，traceback 折叠
    进「详细信息」，右键可复制全文——一个字不丢但不再刷屏；
    ③ 新对话按钮外露（实机反馈）：窄侧栏下 toolbar 图标收进 » 溢出菜单，macOS
    侧栏顶部加全宽主色按钮（safeAreaInset，toolbar 那枚保留）
  - **实机查验第二轮（2026-07-16）**：填钥匙+重启后 401 依旧。切割实验：同一 bundle
    同一设备 A，env 直给 .env.local 值 → **0 个 401**（E2E 模型真回复）⇒ 问题必在
    app 注入的**值**上。根因：`.env.local` 的值是 `KEY="xxx"` 引号包裹形态——shell
    `source` 剥引号（devctl/烟测全对），SecureField 整段粘贴不剥（带引号直注 env
    = 稳定 401，dev 值 4 字带引号变 6 字）。修复（v0.16.0(24)）：
    ① 粘贴清洗 `SidecarManager.cleanKey`（去空白/换行+剥一层包裹引号），输入端
    （binding）与注入端（resolveLayout，兜钥匙串里的旧脏值）各清一次；
    ② **lib_edge 鉴权门**：`probe_auth` 带钥匙探 RAG `/api/v1/category` 与 infer
    `/metrics`（/health 全免鉴权，探不出钥匙错）——401/403 当场 fail 并说人话
    （「与 .env.local 同值、不含引号」），不再放 backend 起来后第一句话才炸；
    实测：引号钥匙 up 退出码 1 + 原话进设置页故障详情；正钥匙全通 0 401；
    ③ 密钥盘点日志（只记有无+长度）落 backend.log，下轮 401 类故障有第一现场
  - **实机查验第三轮（2026-07-16）**：鉴权门按设计拦停（RAG 401 当场报），但值仍不对，
    且盘点日志排在门**之后**——门一拦就没有第一现场。修复（v0.16.0(25)）：
    ① 盘点日志移到 start_backend 一切门之前（证据先于裁决）；
    ② `cleanKey` 升级：整行拷贝 `RAG_API_KEY="xxx"`（含 `export` 前缀）也剥成裸值
    （真钥匙是随机 token，不会长得像 `NAME=`，不误伤）；
    ③ 设置页密钥**长度回显**（SecureField 打点看不出存了什么——存没存上必须可见）；
    ④ **结构性嫌疑排除：app 本体改签 Developer ID**（bundle-macos.sh 自动发现证书，
    缺证书回落 ad-hoc）——ad-hoc 每次构建都是新身份，Keychain 按签名身份做 ACL，
    每次重装都可能读不回旧密钥（弹窗/静默空值）；Developer ID 的 designated
    requirement 跨构建稳定，密钥存一次终身有效
  - **实机查验第四轮（2026-07-16）**：v25 观测点一击定案——盘点日志显示 up 时刻
    **INFER=无 RAG=无**（AGENT=43 字自动生成的那把，链路通）；再查钥匙串：
    **infer/rag 两条目从来不存在** ⇒ 主因是值从未成功入库（「引号」只是并发嫌疑），
    前三轮全部自洽。处置：`security add-generic-password -T /Applications/MacAgent.app`
    直接种入 .env.local 的两值 + ACL 授权（Developer ID 身份稳定，授权终身有效），
    消掉「手工输入」变量。**对话打通**（用户转入验收三项）。
  - **验收三项第一轮结果 + 修复（v0.16.0(26)）**：
    - 杀进程恢复实测 2~3s，且 native 不持久化选中会话——重启后要手点才回到原会话，
      不满足「回来 ≤1s 恢复完整直播视图」语义（web 刷新同 URL 天然满足）。修复：
      `selectedSessionId` 持久化（UserDefaults）+ 启动 `restoreLastSession()`
      （存在性校验过滤 draft/已删除；深链先到不抢）——重启自动回到原直播视图
    - sidecar 自愈项**未真测到**：给用户的命令少了 `-sTCP:LISTEN`，`lsof -ti :14159`
      连 app 自己的客户端连接一起命中，kill -9 把 app 也杀了（正确命令：
      `lsof -ti :14159 -sTCP:LISTEN | xargs kill -9`）
  - **功能验收通过（2026-07-16 用户口头：「功能性测试目前没有问题了」）**；
    随后三项 UI 打磨（v0.16.0(27)）：
    - **连接信号动态化**（实机反馈：原信号只在设置页关闭时刷一次）：AgentStore
      常驻探活循环（通了 15s 一巡、断了 2s 一巡追 sidecar 启动窗口），三态
      `ConnState`：绿=已连接 / 黄=连接中（连续失败 <10 次）/ 红=未连接（~20s 放弃）；
      侧栏底部与菜单栏同源；sidecar 开启时冷启不再误弹设置页（黄灯已说明状态）
    - **下拉刷新残影**（实机反馈：刷新完瞬移回顶+双重残影）：根因是刷新常拿回
      一模一样的列表、无脑重赋值让 List 整体重建，与刷新控件收起动画打架——
      等值守卫 `if items != sessions`（SessionMetaItem 本就 Hashable）
    - **首轮自动命名**（实机反馈：新会话顶着随机 id 生硬）：首条提问首行截 20 字
      写服务端 meta（renameSession，双端同源），用户改名可覆盖
  - ✅ **GUI 验收三项全过（2026-07-16 用户确认「验收三项跑完了没问题」）**：
    杀进程自动回原会话、sidecar 自愈（-sTCP:LISTEN 命令）、冷启可用。
    UI 打磨回访：#1 连接三态、#3 首轮命名「完美解决」；#2 下拉刷新残影仅略好转。
  - **打磨第二轮（v0.16.0(28)）**：
    - #2 决断：下拉刷新是 iOS 习语，macOS 上刷新控件收起与 List 更新天然打架——
      **macOS 撤掉下拉刷新**，由探活循环顺带巡刷列表（15s + 等值守卫 = 不变零重建）；
      iOS 保留下拉
    - 新观感问题「worker 与会话列表零隔断」：两轮误诊为分栏线（v28/29，画了看不见——
      悬浮玻璃侧栏下 detail 内容延伸到侧栏底下），**用户一张截图定案**：「worker」=
      顶部任务卡，真因是会话行是裸行、被吸附进「任务」Section 的分组卡——会话列表
      包显式 `Section(会话)`（v30），误诊的分栏线撤除。教训：UI 观感问题先要截图，
      盲推一轮不中就停
  - **CHARTER 同步三处已落地（2026-07-17 用户：「那三处改动没问题」）**：
    `apple-app/` 入库条目 R5-1 时已改；部署纪律重述适用域（sidecar 按设计自启不受限，
    开发流程照旧）；铁律 4 补注（app 兼任宿主，渲染部分仍是纯归约器投影）

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
| **D1** | Skill 库扩充 ✅ **首批收官**（2026-07-17，5 剧本全实测，见 D1 进展） | R2a 建好体系后 skill 是 md，AI 批量写。**每个需实测验收** |
| **D2** | Tool 扩充（现 22 文件 / 41 类；对标 cc-haha 60+） | **只加桌面级任务真需要的**。🟡 首批 7 件已落地（见 D2 进展）；用户定向补充清单待生产环境定型后由用户提出 |
| **D3** | 子 Agent 种类扩充 | R1 后新 agent = 一份声明 |
| **D4** | 切 `tool_schema_mode: catalog` | ✅ **阈值已定**（2026-07-17 用户拍板）：**full 声明 >12k tokens（≈50 工具）时切**，D2 全程留 full。切换有两张**前置工程票**（见「D4 阈值决策」段）——catalog 今天不是配置开关，直接切会把带参工具全灭 |

**扩充纪律（硬性）**：
- 每个新 skill/tool **必须有金测试**，否则 132 项的回归网被稀释成摆设
- 每个新 tool **必须声明 `is_available` 探活**，否则重演 SearXNG 那次「优雅不注册」事故（§S.3 已入账）
- **宁可 20 个实测过的工具，不要 60 个没验过的**。cc-haha 的 60 个是 123 个测试文件撑着的

### D1 进展（2026-07-17 首批）

**新剧本 ×4**（全部只编排既有工具，零新增提权面；English 正文循 deck.md 惯例）：
`research`（调研报告：子问题拆解/两独立来源/引用编号/落盘）、`chart-report`（先看
数据形状/图型跟问题/图题写结论/被排数据要声明）、`ingest`（先列类目防近义增殖/提取
后先读防乱码/存原文不存摘要/收录清单可审计）、`briefing`（三要素不齐先问/查重/先干跑
一期/调度提示词必须自足）。UI 自动化剧本刻意押后（验收难度最高）。

**金测试 225→232**：requires 对真实工具面核对（打错名 = 上线静默蒸发，风险 5 同款）、
发货清单钉死、斜杠分发 ×5（注入/保摄入/三种不触发/连字符与空表/接线断言）。

**D.17 · 斜杠点名确定性分发**（实测倒逼的机制补全）：验收发现 **e4b 对「使用 X 剧本」
「/x」都不会主动调 `skill_invoke`**——四个案例全部裸奔（R2a 当年验的是机制路径，没验
模型会不会真调）。R2a 语义本就是「用户点名才调」，点名是机械可判的：摄入期
`dispatch_slash_skill`（`skill_registry.py`，turn_runner.accept_turn 接线）把
`/<剧本名>` 的正文注入 `ingested_text` 通道（与 PDF 摄入同形态，烘史重放不重解析，
用户气泡不变）。`skill_invoke` 保留为模型侧显式通路。零新增提权面（D.11 不变）。

**实测验收记录**（对 v31 bundle 栈 + repo 栈，全真实模型轮次）：
- `chart-report` ✅ 流程正确（inspect→analyze→chart，status success）；**揪出模型捏造
  图表链接**（`example.com/chart_link` 顶替 render_chart 真链）→ 剧本补规则 6（链接
  必须逐字来自工具返回）
- `ingest` ✅ 流程正确（list→upsert→insert，类目/清理均验证）
- `briefing` 🟡 分发后到达 schedule_create，但 **e4b 反复构造其参数失败**（JSON 结构
  不合法，道歉收场）——记为 D2 工具人机学待办（schedule_create 的 schema 对小模型
  过于复杂）；审批闸门无头旁路被权限系统正确拦截，**审批环节的验收留给用户在 app 真点**
- `research` 🟡→**根因已修**（见下）：`web_research` 内层沙箱 `calls=[]` 实为
  **服务端约束采样死亡**的下游症状
- 验收环境教训：①先猜 `/turn` 字段名浪费一轮（真形状 `{"turn": {...}|null}`）；
  ②证据（journal/history）要在删除会话**之前**取齐；③四轮并发提交会在推理队列互相
  饿死，验收必须串行

**D1 验收连环案收官（2026-07-17）——一个 bug 解释全部疑难（S.9）**：最小复刻探针
（scratchpad/probe_sandbox.py，直连设备 A 推理 + 独立 searx，不占 14159）拿到第一现场：
沙箱模型每步都发 `call:web_search{query": ...}`——**首键丢开引号**的畸形 JSON，三次
parse 重试全灭 → `calls=[]`。追到服务端日志一锤定音：
`[Server] 约束采样构建失败，永久禁用: TypeError: ToolConstraintEngine.__init__()
missing 1 required positional argument: 'schema_builder'`——R0 给构造加了方言
schema_builder 参数，engine.py 改了，**没人 import 的 inference/server.py 又被落下**
（S.7 的构造签名变体；import 级钉子对它全绿）。服务端约束死亡 = 全部生成裸奔，e4b
畸形 JSON 概率大增：沙箱 calls=[]、briefing×schedule_create 参数崩、chart-report
第二调畸形，**同一根因**。修复：server.py 补传 `S.dialect.tool_call_schema`（一行）；
重启 infer 后探针全绿（掩码激活「71 步受 schema 约束」，web_search→open_pages→带
[source N] 引用的真摘要）。金测试 +1（233）：`test_constraint_engine_call_sites_bind`
——AST 抓两个入口的构造调用点按 __init__ 签名 bind，签名再漂移当场红。
**用户可直接在 v32 重测 /research、/briefing、/chart-report（修复在服务端，包不用换）。**

**实机第二轮（2026-07-17，用户四会话，只做架构侧）**：约束修复在生产坐实
（research 案例真产出带证据链的汇率报告）。架构问题三件，全修（v0.16.0(33)）：
- **交付物无处安放**：剧本说「写进 workspace」，工具侧只收绝对路径——模型幻觉
  `/workspace/` 撞 macOS 只读根 Errno 30。修：write_file/edit_file 相对文件名落
  `sessions/{sid}/assets/`（render_chart 同范式），返回出货链接逐字进回复；文件名
  字符集与 `_ASSET_NAME_RE` 对齐（写得进必须取得出）；金测试 +6（test_file_workspace）
- **soffice 残链带病注册**：brew 卸载 LibreOffice 后 `/opt/homebrew/bin/soffice`
  残链仍中 `which`，excel_research 带病注册、运行到 soffice 才炸 exit 127。修：
  `find_soffice` 真执行 `--version` 探活，残链出局（探活不是摆设）；本机想用
  excel/chart-report 需 `brew reinstall --cask libreoffice`。发货剧本金测试改为
  环境感知（名字合法性对声明名全集静态核对；加载断言按本机可用面条件推导）
- **web_research 预算错配**：total_timeout 110s，实测远端 e4b 一次完整调查 2~3 分钟
  ——简报干跑必超时。修：110→240 + 工具自带 timeout_seconds=+30 覆盖全局
  （excel_research 同款）。金测试 240 全绿。

**实机第三轮（2026-07-17，用户重测三剧本）**：
- `/research` ✅ **端到端全通**——web_research 真调查 + write_file 相对文件名落工作区
  + 下载链接进回复。**D1 第一个完整闭环的剧本**。
- `/briefing` 🟡 剧本流程对（补问主题→干跑）；干跑摘要只带候选未读页 + 后续轮模型
  「口头承诺不行动」——偏模型智力（含一个观察项：约束内单次生成 2048 token 跑满、
  调用块未收口，`[Constrained] 1951/2048 步受约束` + `calls=[]` 签名），按用户口径
  **留待生产设备后再优化**，先入观察清单。
- `/chart-report` 依赖缺失路径按设计舍弃剧本，但暴露架构缺口：**点名不可用剧本被
  静默放行**，模型对着点名瞎打（run_math），用户不知道为什么没按剧本走。修
  （v0.16.0(34)）：SkillRegistry 记录舍弃原因（`unavailable_reason`），分发时注入
  如实告知（缺什么、别假装执行、剧本正文绝不注入）；金测试 +1（241）。
  另：xlsx 摄入层已把表结构送进上下文（[Spreadsheet] 块）——无 excel_research 时
  模型仍有数据可谈，降级路径成立。
  **续（用户纠正）**：测试时 LibreOffice 其实已装——真因是**时序**：backend 的 23:52
  启动恰落在 `brew reinstall` 的删旧装新窗口（候选文件全空），而**注册表只在执行层
  启动时扫一次**，装好 ≠ 生效。同代码同 PATH 现探针 0.28s 通过、excel_research 正常
  注册。修（v0.16.0(35)）：不可用告知补「重启执行层生效」指引。「注册表热重扫」
  列 D 阶段待办（两表互为前置，重扫需两段装配，非一行事）。

**实机第四轮 = D1 首批收官（2026-07-17，/chart-report 补测）**：重启后注册表如实
反映 LibreOffice 就位——剧本注入 ✓、excel_research 两次真执行（VERDICT 裁决行）✓、
纠错回环实战通过（思考内起草调用被截 → 解析器精确指引 → 下步恢复合法）✓。未出图
属模型侧（重复自查 + 流程复述当终答，与 briefing「承诺不行动」同族）→ 观察清单。
**D1 首批判定**：5 剧本架构路径全实测——research 端到端全通；deck/ingest 通过；
briefing/chart-report 架构贯通、卡模型智力。首批共修架构缺陷 7 件（约束采样死亡
S.9 / 工作区落点 / soffice 探活 / 超时预算 / 静默放行 / 注册表时序告知 / 斜杠分发
D.17）。**观察清单（模型智力，等生产设备）**：多步序列推进力（承诺不行动、重复
自查、流程复述当终答）、约束内调用块 2048 token 未收口、schedule_create 复杂
schema 参数构造。

**附带发现（R5-9 尾巴）**：`kill -9` app 的实机测试留下**孤儿 backend/searx**（SIGKILL
不走 willTerminate），此后每代 app 都把它当 external 只用不管——孤儿一直跑着已删除
的旧 bundle 代码。已手工清；「sidecar 孤儿检测/回收」列为 D 阶段待办。

### D4 阈值决策（2026-07-17，用户拍板）

**实测（真 e4b tokenizer）**：35 工具 full 声明 = **8.4k tokens**（~230 tokens/工具，
稳定前缀吃 KV 缓存，代价 = 窗口占用 + 缓存未命中的整段 prefill）；catalog 现实现仅省
46%（4.5k）——**description 多为单行长文，`split("\n")[0]` 剪不掉**，摘要截 100 字符
才到 2.0k；`get_tool_schema` 单次取中型工具 ≈ 480 tokens 观察 + 一次额外迭代。

**决议**：阈值 = **full 声明 >12k tokens（工具数只是换算，≈50 个）**，D2 全程留 full
（按「只加真需要的」估 +5~10 工具够不着线）。到线再做切换，且切换前必须清掉两张
**前置工程票**：
1. **约束/提示词解耦**——约束采样 schema 从请求携带的声明现场构建
   （`gemma4/tool_schema.py:31`）：catalog 条目没有 `parameters` →
   `additionalProperties: false` 把所有真工具的 arguments **钳死成空对象**。今天直接切
   = 带参工具全灭。解法方向：约束引擎拿全量 schema，提示词只放目录（grammar 不占
   prompt，两全）。
2. **目录摘要瘦身**——为每工具补一句话摘要（或截断纪律），否则 catalog 白切一半。
   另：e4b 不主动调元工具已有前科（skill_invoke，D.17），`get_tool_schema` 的实际
   使用率切换时必须实测验收。

**顺带修正（同日落地）**：摘要阈值口径——`summary_high_threshold: 8000` 比的是
**整个 prompt**（`last_prompt_tokens`），而工具声明+系统提示地板 ≥9k，裸阈值被前缀
单独吃满 → 条件恒真，超过 keep_tail 的会话**每轮都在折叠**，「高阈值兜底」名存实亡
（低阈值 2000 同病）。修：配置值重定义为「稳定前缀**之上**的历史预算」，编排层启动时
估算前缀（chars/4，对本声明实测 4.02 字/token）并把两个阈值抬为 前缀+配置值——D2
工具增长自动跟随，catalog 若切换亦自动收紧。金测试 +1（242：估算随声明体量同向、
阈值恰抬前缀量、纯前缀 prompt 够不着高阈值）。

### D2 进展（2026-07-17 首批：桌面日常七件套）

**选型口径（用户拍板）**：生产环境未定型，先按**通用桌面日常使用率高→低**选；
用户真实需求清单待搭建完成后由用户补充。首批 7 件（`file_nav.py` + `desktop.py`，
纯 macOS 原生命令薄包装，零第三方依赖）：

| 工具 | 日常场景 | 审批面 |
|---|---|---|
| `find_files` | Spotlight 找文件（名/内容，最新优先） | 免（只读） |
| `list_dir` | 目录浏览（dirs 先、大小/日期、上限 200） | 免（只读） |
| `open_path` | 默认应用打开文件/文件夹/网址 | **动态**：文档/目录/http(s) 放行；可执行物（.app/.sh/执行位）、装载类（.zip/.dmg/.pkg）、未知扩展、非 http(s) scheme 一律弹窗（`open` 对它们=运行不=查看，shell_exec 白名单同路数） |
| `clipboard_read` | 「总结我刚复制的」（8k 字符封顶） | 免（只读；描述约束「用户提及才调」） |
| `clipboard_write` | 「放到剪贴板」 | 免（副作用轻且立即可见） |
| `notify_user` | 长任务/定时任务收尾横幅 | 免（message 经 argv 交付 `on run argv`，无 AppleScript 注入面） |
| `system_status` | 电量/磁盘/内存/开机时长 | 免（只读） |

**为什么不让模型走 run_shell_command**：D1 实测 e4b 拼 shell 引号/转义常翻车，
专用扁平 schema 是小模型人机学下限；schema 刻意精简（~120 tokens/工具）。
**预算核验**：40 工具 full 声明 8.8k tokens < D4 阈值 12k，余量够第二批。
**金测试 242→255**（+13）：open_path 审批矩阵（放行/弹窗分界=提权面）、mdfind
命令构造、pbcopy 字节精确、osascript argv 无注入、批量元数据纪律（只读/审批标记
与实现一致、探活可判）。碰系统状态的一律 mock subprocess 断言命令构造——真弹通知/
真覆盖剪贴板是对开发机的骚扰。

**实测验收（2026-07-17，用户九问全跑，会话 app-e5e370deadab）**：七件套全部真调用。
四个疑难复盘：
- **「下载文件夹里有什么」→ 列了 `~/`**：模型把 Downloads 构造成 `~/`（思考里明明说
  Downloads），终答如实说了是 home——参数构造走样（schedule_create 同族），入观察清单。
- **「找 blog 文件夹」漏真身**：真身 `/Volumes/enkin/project/blog`。**同命令开发 shell
  中 3 条、app 内只中 2 条**——Spotlight 结果按进程 TCC 授权**静默过滤**，bundle 后端
  无外置卷/完全磁盘访问。工具无 bug，是权限环境；解法 = 系统设置给 MacAgent 完全磁盘
  访问；未中告知已补权限披露句。
- **「文稿里找提到アジャイル開発的文件」空手**：败因单一——e4b 漏传 `search_content=true`
  走了文件名模式（选型时预告过的失败形态；内容模式实测能中 `result.txt`）。修（架构侧，
  确定性）：**名字模式 0 命中自动降级内容模式**，结果标注按什么匹配；显式 true 语义不变。
  金测试 +1（256）。
- **「三调三返」**：真发起 3 次、**真执行 1 次**——后两次被重复调用拦截器挡下
  （`Duplicate call blocked`），浏览器只开一次，前端如实显示三对调用/返回。护栏按设计
  工作；重复冲动本身与轮 8 终答答非所问（扯回前几轮的通知/RAGframework）同入观察清单。

**第二批（2026-07-17 落地，待实测）**：日程组织 + 文件整理六件（`organizer.py` +
`file_manage.py`）：

| 工具 | 场景 | 审批面 |
|---|---|---|
| `calendar_add` | 「明天三点开会记一下」（同步 iCloud 全设备） | **恒审批**（写真实日历） |
| `calendar_list` | 「我今天/这周有什么安排」（days 封顶 31） | 免（只读） |
| `reminder_add` | 「提醒我买牛奶」（due 可选） | **恒审批** |
| `move_file` | 挪动/改名（目标为已存在目录=挪入） | **恒审批** |
| `copy_file` | 拷贝文件/文件夹 | **恒审批** |
| `trash_file` | 删除=进废纸篓（可恢复，刻意不提供硬删） | **恒审批** |

关键纪律：①**日期绝不进 AppleScript 字面量**——`date "…"` 解析随系统区域设置，
跨机即碎；Python strptime 校验后拆年/月/日/时/分数字经 argv 交付，脚本对
`current date` 逐字段赋值（先 day=1 防月末翻转）。②文本参数同走 argv（notify_user
同款，无注入面）。③move/copy **绝不静默覆盖**（已存在即拒绝，让模型换名）。
④三个新 TCC 面（Calendar/Reminders/Finder 自动化）：-1743 错误带系统设置指引。
金测试 256→268（argv 日期分量/覆盖拒绝/TCC 提示/元数据纪律）。
**预算**：46 工具 full 声明 9.6k tokens < 12k，剩 ~10 个工具余量。
**实测验收 ✅（2026-07-17，用户六问全过，会话 app-256fc8ee9cd2 / app-c49610dee482，
跨会话分测排除上下文记忆）**：六件全部真执行；calendar_add 一发命中（「明天下午三点」
→ 正确的 'YYYY-MM-DD HH:MM'）；calendar_list 跨会话命中另一会话建的事件（走真日历）；
move 改名/copy 目录语义/trash 进废纸篓全过。三次磕绊全被护栏同轮接住、无一漏到用户：
①思考频道泄漏（`<|channel>thought` 原文）→ 解析器打回后恢复；②`due` 尾随 `)` →
**参数预检在审批前打回**（用户没为坏调用点过弹窗，validate_call 设计兑现）；
③`days: 202607189999…` 数字退化复读 + `202026` 畸形年份 → 两次打回后第三发正确。
**三种报错文案（解析/预检/格式）全部换来同轮自纠——纠错回环首次全套野外验证**。
观察清单新增：数字退化复读（约束内未收口家族的数值变体）。

**刻意不加**：邮件/信息外发（外发面等真需求）、联系人、音量媒体控制（低频，
run_applescript 可代）、硬删除（可逆优先，真要 rm 有 run_shell_command 审批路径）。

### D 阶段工程票 ×2（2026-07-17 落地，v0.16.0(39)）

**① 注册表热重扫**（LibreOffice 时序案的根治）：依赖后装不再要求重启执行层。
- `MCPRegistry`：探活未过的工具实例留档（`_discarded`），`rescan_unavailable()`
  重探补注册——**只增不删**（删除与进行中轮次竞态；服务死亡由执行期真实报错兜底）；
  `load_tool` 探活异常补 fail-safe（此前目录扫描有 try、补注册路径裸奔）。
- 重扫编排（app.py 闭包，经 runtime 钩子登记）与启动两段装配同构：重探工具 →
  重建剧本表 → 二次重探（skill_invoke 探活看新剧本表）→
  `orchestrator.refresh_tools_declaration()`（声明重建 + 摘要阈值从基值重锚，
  连续刷新不漂移）。
- 触发点 ×2：`POST /api/v1/tools/rescan`；**斜杠点名不可用剧本时自动前置重扫再分发**
  （turn_runner，to_thread 慢路径只在该分支走）。不可用告知文案随之改为
  「已即时重扫仍缺失」（不再叫用户重启）。金测试 268→274。

**② sidecar 孤儿检测/回收**（R5-9 kill -9 遗留的根治）：`lib_edge.sh` 新增 `reap`
命令——数据根 pid 文件仍存活 = 上代 app 经本脚本所拉（谁的数据根谁的兵），回收后
重新拉起；陈尸 pid 文件顺手清。SidecarManager 在 autostart 探活**之前**先 reap，
且 **bundle 布局限定**（`Layout.isBundle`）：repo 布局与 devctl 共用 logs/ 的 pid
文件、无法区分归属，reap 会误杀用户的开发后端——「app 杀用户 dev 后端」是最恶劣
越权，宁可 repo 布局维持孤儿现状。陈尸分支已隔离实测（真端口零接触）；活孤儿分支
复用 stop_one 老代码，回归验收 = 用户复刻 kill -9 场景。**✅ 实机回归通过
（2026-07-18 用户复刻 kill -9 → 重启后旧 pid 已死、pid 文件换新、对话正常）——
R5-9 kill -9 遗留案销案。** 澄清（用户问「为何不做成关 app 时杀进程」）：正常退出
**本来就杀**（applicationWillTerminate → shutdownSync → lib down，R5-4 起就有）；
reap 兜的是 willTerminate 物理上不会执行的路（SIGKILL 不可捕获、崩溃、断电）——
两层是带子加背带，不是二选一。关主窗口 ≠ 退出（菜单栏常驻设计，进程活着 sidecar
照常服务）；真退出 = ⌘Q / 菜单栏「退出」。

### UI 打磨 · 消息操作钮下移（2026-07-17 落地，v0.16.0(40)）

用户提出三点（原生 app，`MessageView.swift`）：
1. **主操作从右键菜单移到气泡下方小钮行**（Claude AI 形态）：用户消息 = 复制 +
   撤回重编（editLastTurn，仅最后一条且非生成中）；助手消息 = 复制 + 分支 +
   重新生成（仅最后一条且非生成中）。macOS 上最后一条常显、旧消息悬停浮现
   （行**始终占位只变透明度**，布局不跳）；iOS 无悬停一律常显。
2. **右键菜单原样保留**（用户后续要往里放其他功能）；助手气泡右上角的悬停
   复制钮**删除**（与新钮行重复）。
3. **复制成功反馈**：`CopyActionButton` 点击后图标翻成功色对勾（Theme.success），
   1.5s 复位；连点取消上一枚复位任务重新计时，不闪跳。L10n 新增「已复制」。

操作行挂在流式预览之外（MessageView 只渲染定稿消息），生成中不会出现抖动的
钮行。金测试 274 全绿（后端未动），bundle 印章 SEAL-OK，新组件符号已在装机
二进制确认。

### UI 打磨 · 第二批（2026-07-17 落地，v0.16.0(41)）

用户提出三点：

1. **拖文件进输入栏**：`ComposerView` 挂 `dropDestination(for: URL.self)`，
   悬停时输入区描虚线主色框；生成中拒收（与其他附件入口同口径）。文件选择器 /
   ⌘V 粘贴 / 拖放三路收编为同一 `addFileURLs`（security scope 按需取还，
   macOS 无沙箱返回 false 无害）。
2. **会话列表时间戳改时间点**（原为 relative 经过时长）。用户定的三档规则：
   当日内=时分秒、当日外 7 天内=周几、更早=年月日。新建 `Timestamps.swift`
   （档位判定 + 按 格式|语言 缓存 formatter，周几随 app 语言：星期三/水曜日/
   Wednesday；1–6 自然日才算「周几」档——第 7 天与今天同名有歧义，落年月日）。
3. **消息时间戳**：轮次记录新增 `timestamp`（epoch 秒，**additive 不升版本**，
   PROTOCOL.md §2.3 已记）。落盘 `append_log` 是唯一漏斗，在那里对**调用方原
   dict** setdefault——/chat 响应、turn_ended 事件、history.jsonl 三面天然同值；
   旧历史行没有该字段，客户端按可选处理不显示。Swift 侧 `Turn.timestamp` →
   `ChatMessage.timestamp`（同轮消息同值）→ 气泡下操作行旁常显小字
   （`messageLabel`：非当日档位追加时分，便于会话内定位）。金测试 274→276
   （盖章范围 + setdefault 不覆盖已有值）。

### 斜杠候选提示（2026-07-17 落地，v0.16.0(42)）＋ R2b 路线澄清

用户问了两个剧本问题，先记**架构事实澄清**：斜杠（D.17）不是把 skill 降级成了
斜杠功能——`skill_invoke` 至今是正常注册、模型可调的工具，斜杠是**加的一条确定性
轨**（e4b 实测从不主动调它才加的）。换 31B 后：①用户口头点名（不打斜杠）零代码、
直接实测；②模型自主挑剧本 = R2b（DEFERRED.md），需目录进提示词 + 强模型实测选得准；
③斜杠轨建议永久保留（零 token 零错率，Claude Code 同款取舍）。

**候选提示落地**（廉价档，用户拍板）：
- 后端 `GET /api/v1/skills`：数据源 `SkillRegistry.listing()`（新方法；`catalog()`
  形状不动）。**含被探活舍弃者**——描述此前没留档（`_unavailable` 只存缺失清单），
  补 `_unavailable_desc`；灰显 + missing 比「凭空消失」可解释，且点名它们会触发
  turn_runner 前置自动重扫。金测试 276→280。
- 客户端：`store.skills` 搭 loadSessions 车刷新（变动极低频，拉不到保持旧值不弹错）。
  ComposerView 首令牌 `/` 且无空白 → 前缀过滤候选列表（缺依赖灰显 + 缺什么），
  ↑↓ 移选、Tab/回车补全（`onKeyPress`，候选收起后回车走原 onSubmit 发送路）；
  名字敲全 + 空白 → 绿色 `✓ /name — 描述` 命中徽标。真·行内变色需包 NSTextView
  重写输入组件，性价比差，明确不做。

### 会话列表冷启快照（2026-07-17 落地，v0.16.0(43)）

冷启时 sidecar 拉起前列表空窗数秒（真相在后端）。纯客户端修：loadSessions 真变
时落 UserDefaults 快照（元数据几 KB），启动/换后端时先摆快照、服务端真相到达即
替换（等值守卫兜底，不变则不闪）。**快照按后端地址隔离**——连远程时闪出本机列表
是错误信息，比空列表更糟，地址对不上宁可空着；applySettings 先撤旧列表再摆新址
快照。幽灵行窗口（关 app 期间别处删了会话）与窗口期点击失败横幅：几秒窗口，
接受不防（禁点反引入困惑）。铁律 4 合规：只缓存 API 响应，不读后端数据根目录。

### 媒体抓取工具 media_download（2026-07-17 落地，v0.16.0(44)）＋ 三开源项目评估

用户给了三个开源项目让评估接入。**项目 1（yt-dlp）→ 已落地为工具**：按本仓自己的
分界原则（skill_registry.py 头注：确定性逻辑→工具，提示词性质流程→剧本），URL→
文件是纯确定性逻辑，做 tool 不做 skill。单工具三模式：probe（只取元数据）/
audio（抽音轨 m4a）/ video（合流 mp4）。产物落 `sessions/{id}/assets/` 经
`/api/v1/files/` 出链接（与 deck PDF 同一出口）——后端可能在远端机，落
`~/Downloads` 对远程客户端毫无意义，served link 三端通用。守护四件套：**纯播放
列表 URL 拒绝下载**（前置 `process=False` 轻探拦截；`noplaylist` 只管二义 URL，
纯列表 URL 它不管，没这道闸一个 URL 拖回几百个文件）、分辨率上限（默认 1080p）、
体积熔断（默认 2GB `max_filesize`，开发机 16GB/磁盘有限）、`restrictedfilenames`
纯 ASCII 文件名（markdown 链接空格/中文断链）。`timeout_seconds=900`（整片下载
以分钟计，全局 120s 必死）。探活 = pip yt-dlp + 系统 ffmpeg 双探（合流/抽音轨都
要 ffmpeg；GUI 拉起的 sidecar PATH 已由 baseEnv 前置 `/opt/homebrew/bin`，两种
布局都通）。依赖三处齐：requirements.txt + requirements-edge.txt + **直接 pip 进
build/runtime**（yt-dlp 纯 Python 零 Mach-O，签名树可后装不破印章——build-runtime.sh
头注的「签过名拒装原生扩展」约束不适用于纯 Python 包）。金测试 286（+6：选项装配
守护/播放列表拒绝/无会话契约/缺 ffmpeg 不注册/schema 形状）；实机 probe 冒烟通过
（YouTube 元数据取回正常）。

**项目 2（MediaCrawler）→ ~~建议不融合、缓行~~ 用户拍板：直接放弃（2026-07-18）**：它不是搜索是「登录态平台爬取」
（Playwright CDP 挂真 Chrome + JS 取签名参数，扫小红书/抖音/快手/B站/微博/贴吧/
知乎的帖子评论），与 web_research 的「无登录公开网页调查」是两个物种——融进
web search 会把「需要账号、会封号、有法律面」的动作混进无审批的只读调查沙箱，
边界直接脏掉。license 为 NON-COMMERCIAL LEARNING 1.1（禁商用禁大规模爬取）。
真有中文社媒数据需求时的正确形态 = 独立 opt-in 工具（显式审批 + 用户自己的账号
登录态），挂 DEFERRED 等真实需求。**可借鉴点已吸收过**：抓取升级阶梯 T2 本就复用
系统 Chrome（requirements.txt 头注），CDP 挂真浏览器思路同源。

**项目 3（remotion-dev/skills）→ 形态不合，挂观察**：它是 Anthropic Agent Skills
规格的目录制渐进披露包（SKILL.md + 相对路径互链子文档，markup 主文档 9.5KB + 十几
个子文档），假设的执行环境 = Claude Code 级编码 agent（Node 工程脚手架、写 React/
TSX、跑 npx remotion studio 常驻进程）。本仓 skill = 扁平单 md 全文注入，且 e4b
写不动 React 视频工程——属「模型智力引发」维度（D1 用户规则：等生产设备）。31B
到位后若真有做视频需求，改造路径 = SkillRegistry 支持目录制剧本 + 互链改为按需
file_extract，工程量中等，先不动。（2026-07-18 用户确认：项目 3 按此挂起等需求；项目 1
media_download 有细节要补充，待用户后续给出。）

### 文件面板 + 产物链接接管（2026-07-18 落地，v0.16.0(45)）

用户实机反馈：消息里的产物链接（deck PDF / media_download）点击弹 -50。根因两层：
①相对路径丢给系统 openURL 必死（-50 = paramErr）；②就算拼绝对 URL，文件端点在
Bearer 鉴权后面，浏览器 401——**浏览器路线是死路，app 内接管是唯一正确形态**。

落地（用户拍板）：
- **后端** `GET /api/v1/files/{sid}`：列 assets/（name/size/mtime 降序），隐藏文件/
  子目录/不合出货白名单的名字滤掉；无 assets 目录 = 空清单不是 404（「还没有产物」
  是正常态）。**只列 assets**——上传原件与任意路径写盘有意不列（用户拍板：面板
  语义 = Agent 产物）。金测试 286→290。不入 PROTOCOL.md（那是轮次事件协议，
  既有 files 端点本就不在其中）。
- **检查器第四页签「文件」**：行点击 = 预览——音视频 **AVPlayer 流式**（AVURLAsset
  注入 Authorization 头；两项实证：Starlette FileResponse 开箱 Range 206、yt-dlp
  对 ffmpeg 输出无条件 +faststart——拖进度条按字节段取，个人流媒体基本盘齐，
  不做 HLS/多码率，高码率+慢公网的对策是下载时选档位）；图片复用 AssetImage；
  其余下载后 Quick Look（iOS 的 QL 自带分享）。macOS 行尾：打开（临时目录+默认
  app）/ 保存（NSSavePanel）。下载走 URLSession.download 流式落盘（视频上 GB
  不过内存）。
- **链接接管**：ChatView 挂 OpenURLAction——只认**无 scheme 相对链接**且路径精确
  匹配 assets 形态（带 host 的外站链接绝不劫，撞巧同路径也放行浏览器）；命中 →
  开检查器文件页签 + 高亮（同名重点用先清后置触发 onChange）。/research 的外部
  来源链接照旧进浏览器。
- **架构澄清落档**：部署端就是两个（算力端 + 工作端 bundle 一体，用户原构想），
  iOS 是追加的瘦屏幕拨工作端 API；面板走 API 本机零代价、iOS 自动免费获得。
- **窄窗修复**：窗口 minWidth 760→860（= 侧栏 min 220 + 检查器 min 260 + 聊天区
  下限；三栏全开时 SwiftUI 不再硬压侧栏突破 min 导致标题错位）。
  **→ 实机复测不过（46 返修）**：860 时聊天列拒绝压缩到 ~414 以下，三栏总需求
  ~934，两侧同时溢出被裁（左侧栏文字缺头、检查器页签缺尾）。改**动态最窄**：
  showInspector 上移 store，检查器开 = 960 / 关 = 760（恒定大 min 会惩罚不开
  检查器的窄窗用户）；检查器页签 picker 加 controlSize(.small)（四页签日文词表
  260pt 默认字号放不下）。

### 文件面板返修 ×2（2026-07-18 实机截图反馈，v0.16.0(46)）

用户截图两问题：①窄窗错位没修好且左侧栏也错位；②点视频链接进了文件区但列表空白。

**②的根因 = S.10**（yt-dlp 选项键名错，`restrictedfilenames`→静默忽略→日文原题
落盘→过不了出货白名单：listing 滤掉 + serving 400，链接从生下来就是死的）。修复：
正确键名 `restrictfilenames` + outtmpl 加恒定 `media-` 前缀（消毒后 CJK 标题可能
只剩前导下划线，白名单要求首字符字母数字，前缀兜死）；金测试改对真实消费方钉
（YoutubeDL 实例认账 + 前缀断言 + 极端消毒形态过白名单），291。既有搁浅文件已
手工改名归位（media-kaguya-sama-PV-ED-v9nUcqxVXc.mp4）。

**返修 46 实机复验：窗口宽度 ✅、文件可见 ✅，但点视频即崩（47 返修）**：崩溃报告
×2 同签名——`_AVKit_SwiftUI` 泛型元数据初始化 `getSuperclassMetadata` fatalError，
即 **SwiftUI 的 VideoPlayer 组件在 macOS 27 beta 上实例化即崩**（系统 beta 兼容
问题，非调用姿势）。规避：换 AVKit 经典 `AVPlayerView`（macOS，NSViewRepresentable
包壳）/ `AVPlayerViewController`（iOS），完全不走 _AVKit_SwiftUI 代码路径；
AVURLAsset 鉴权头注入与流式 Range 语义不变。v0.16.0(47)。

**iOS 对齐（文件面板批次收尾，2026-07-17）**：两 target 共享 Sources，文件页签/
链接接管/播放器 iOS 免费获得（`.inspector` 在 iOS 17+ 自适应 sheet/尾栏；
ClassicPlayerView 的 iOS 分支 = AVPlayerViewController，写 47 时已备好）。唯一
真缺口：文件行操作按钮此前 macOS-only（打开/保存 = NSWorkspace/NSSavePanel），
iOS 对位物是一张系统 share sheet——新增行内「分享」钮（square.and.arrow.up）：
下载完弹 `UIActivityViewController`（SwiftUI 包壳 ActivityShareView；ShareLink
是按钮不是面板，接不上「下载完成后再弹」的时序），覆盖 存到文件/存入相册/
AirDrop/转发。`.other` 类型行点击本就走 Quick Look，iOS 的 QL 自带分享，不变。
词表补「分享」（共有/Share）。双端编译均过（iOS 模拟器 + macOS Debug）；
CURRENT_PROJECT_VERSION 47→48，TestFlight 已传 48（macOS 装机仍 47——本批
无 macOS 可见变化，不重打包）。wait-testflight 依赖补装：backend/.venv 缺
pyjwt（脚本自述「任意 venv」，实际此前用过的环境已不可考），已 pip 入 dev venv。

**iOS 实机反馈返修（v0.16.0(49)，2026-07-17）**：用户经 tailscale serve 接通
iOS（测试连接过）后报两点：① iOS 播视频无声、存出去用系统 app 放就有声——
iOS 默认 AVAudioSession 类别 `.soloAmbient` 跟随静音拨键，静音模式下 app 内播放
被消声；播放前设 `.playback`（正经媒体语义，无视静音键，与系统播放器一致）。
macOS 无 AVAudioSession 概念故不受影响。② macOS 播放 sheet 窗口锁死不可调——
frame 只给了 min 没给 max，SwiftUI 的 macOS sheet 只在内容尺寸有弹性区间时才可
拖拽；补 `maxWidth/maxHeight: .infinity`（ideal 840×560）。双端构建过；macOS
49 装机（SEAL-OK），TestFlight 49 已传。

**key 轮换两枚钮（v0.16.0(50)，2026-07-17 用户拍板「按建议来」）**：设置→连接→
API Key 栏下（macOS-only）：①「复制」——SecureField 抄不出来是 iPhone 填 key 的
真实痛点（此前要跑 security 命令行），复制到剪贴板配合通用剪贴板跨端填；1.5s
「已复制」反馈。②「重新生成」——key 轮换：SidecarManager.randomKey()（private
放开）→ 钥匙串 + applySettings + sidecar.restart() 即刻生效；旧 key 随即作废
（其他拨号端 401 须重填，轮换本意），旁注文案写明。仅内建执行层在管时显示
（external/repo 布局的 key 在 .env.local，这里换了对不上）。词表 +3。
坑：ViewBuilder 里链式 `sidecar.state == .failed` 的裸 `.failed` 被类型检查器
解析到 SwiftUI 自家 PencilSqueezeGesturePhase 上（Release 构建失败）——改视图外
显式 switch（sidecarManaged 计算属性）。算力端同模式包装/操作界面：挂起，等
生产设备定型后独立立项（用户同批认可）；近期设备 A 用 openssl rand 手工等价。
macOS 50 装机（SEAL-OK）；iOS 面不受影响（按钮 macOS-only），49 不重发。

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
| **Q8** | ✅ **已闭环（2026-07-16）**：Developer ID Application (G2) 证书由用户在门户签发（API 建不了——Account Holder 专属，见 S.4）；951M 运行时公证 **Accepted**（submission `1e13bf2a`）。签名→公证链路全通，真实 .app 的公证 + staple 留到分发动作 | R5-9 验收形态 |

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

### S.5 · 手工导入的签名私钥会让 codesign 无限挂起（2026-07-16）

**事故**：拿新签发的 Developer ID 身份签运行时，`codesign` 一个文件都签不出来，
进程无限挂起、不报错。我先怀疑 `--timestamp` 卡在苹果的 TSA 上，实测
`timestamp.apple.com` 0.2s 回 302——通的。做了个能区分的测试（**同身份、去掉
`--timestamp` 再签**），照样挂 → 与网络无关。

**真因**：`security import -T /usr/bin/codesign` 只设了旧的 **ACL**，没设 macOS Sierra
之后真正管事的 **partition list**。缺它，codesign 每次用该私钥都要弹钥匙串授权框；
非交互 shell 里那个框弹不出来，于是**静默无限等**。修：

    security set-key-partition-list -S apple-tool:,apple:,codesign: -s ~/Library/Keychains/login.keychain-db

**为什么对比很误导**：此前用 Apple Development 身份签 320 个文件只要 3.6s，毫无征兆——
因为**那把私钥的 partition list 是 Xcode 装它时设好的**。「同样是签名，换把钥匙就挂」
看起来像钥匙坏了，其实是钥匙从没被授权过。

**教训**：`security import` 不是完整的安装。凡是手工导入的签名私钥，partition list
是必须的第二步。已写进 `scripts/build-runtime.sh` 的签名段注释。
**更该记的**：那一步当初就该导进 `macagent-build` 钥匙串——它的密码在
`~/.appstoreconnect/macagent-build-keychain-pass`，`release-ios.sh` 正靠它做非交互解锁，
整个坑本可绕过。我只顾了「证书要和私钥同库」，漏了「自动化管线怎么解锁」。

### S.6 · `2>/dev/null` 会把静默失败伪装成成功（2026-07-16）

**事故**：`build-runtime.sh` 的签名行写成
`xargs -I{} codesign ... {} 2>/dev/null`，本意是滤掉 `replacing existing signature`
的噪音。结果 S.5 的挂起发生时，脚本这一段**看不出任何异常**。

**教训**：要滤的是**已知的那一句噪音**，不是整条 stderr——
`2>&1 | grep -v "replacing existing signature"`。与 §S.3（「优雅降级」把配置错误
伪装成模型故障）是同一个病：**为了好看而丢掉的，正是出事时唯一的线索**。
同时给签名加了**全量覆盖核对**（带时间戳的文件数必须等于 Mach-O 总数）——
只验一个 `$PY` 挡不住部分漏签，而漏签要到公证被拒才暴露。

### S.7 · 模块收编手术会静默打断「没人 import 的入口」（2026-07-16）

**事故**：R0 把 `core/response_parser.py` 收编进 `dialects/gemma4/`，
`inference/server.py:54` 的旧路径 import 静默断裂。金测试 import 的是 framework
包，**没有任何测试 import 这个独立进程入口**；R5-2 给它加 `/v1/transcribe` 时也只
测了「A 不可达 fail-open」——服务器一次都没真启过。用户按 R5-4 查验步骤
`start --profile gpu` 才炸出 ModuleNotFoundError，离引入隔了整整一个阶段。

**教训**：①**移动模块时 grep 全仓引用，包括 backend/ 之外**（inference/ rag/ scripts/
都是独立入口）；②「fail-open 验证」证明的是**对端缺席时**的行为，永远替代不了
把对端真启一次；③已补 `tests/test_entrypoints.py`：入口模块级 import 全通 +
禁止家族字面量（`dialects.gemma4`）出现在 server 源码——两个断裂形态都钉死。

### S.8 · Python 默认字节码写回会破坏只读 bundle 的 codesign 印章（2026-07-16）

**事故**：R5-9 首次组装的 .app 烟测一切通过，但跑完 `codesign --verify` 报
"a sealed resource is missing or invalid"——首启 import 把 `__pycache__` 写满了
Resources/（rsync 组装时刻意排除了 backend 字节码；python/bin 也有缺口），印章
在**功能全绿**的情况下被静默破坏，公证分发形态下这就是 Gatekeeper 拒启。

**教训**：①「bundle 只读」不是声明出来的，是**验证出来的**——烟测收尾必须跑
`codesign --verify`；②修法两层：组装期 `compileall` 让字节码随包进印章 +
SidecarManager bundle 分支设 `PYTHONDONTWRITEBYTECODE=1` 兜漏网；③一切「运行后
包内容不得变化」的断言，都要在**真跑过一轮之后**再验一次。

### S.9 · 构造签名漂移在「没人 import 的入口」上静默积累（2026-07-17）

**事故**：R0 给 `ToolConstraintEngine.__init__` 加 `schema_builder` 参数，engine.py
同步改了，inference/server.py 没有——**import 级钉子（S.7 的 test_entrypoints）全绿**，
因为炸点在运行时构造。结果服务端约束采样「构建失败，永久禁用」且 fail-open，全部
生成裸奔：e4b 畸形 JSON 概率大增，D1 验收里沙箱 calls=[]、schedule_create 参数崩、
chart-report 二调畸形全是它的下游症状，伪装成三个不相干的模型能力问题。

**教训**：①改共享构造签名时 grep 的不只是 import，还有**调用点**（尤其独立入口）；
②import 钉子挡不住运行时签名漂移——已补 AST 调用点 bind 钉
（`test_constraint_engine_call_sites_bind`）；③多个「不相干」的模型行为劣化同时出现
时，先怀疑**共同的基础设施**（这里是约束采样），再怀疑模型。

### S.10 · 选项字典的错误键名会被消费方静默忽略——自己写的字典自己验 = 自洽但错（2026-07-18）

**事故**：media_download 给 yt-dlp 传 `restrictedfilenames: True`（正确键名是
`restrictfilenames`，无 ed）。yt-dlp 对不认识的 params 键不报错不告警，ASCII 文件名
守护整个失效——日文原题落盘，名字过不了出货白名单 `_ASSET_NAME_RE`：listing 滤掉
（文件面板空白）、serving 400（链接死）。**金测试当时是绿的**，因为它断言的是
「我写的字典里有我写的键」——测试和实现共享同一个错误认知，自洽但错。

**教训**：跨库传选项字典时，守护类断言要**对着真实消费方钉**：不测自己的 dict，
测消费方实例化后的认账结果（`YoutubeDL(opts).params.get("restrictfilenames")`）。
kwargs/TypedDict 有拼写保护，字符串键字典没有——凡「配置以 dict 交给第三方」的
守护项都该这么钉。同型风险：ffmpeg 参数、requests kwargs、playwright options。

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
