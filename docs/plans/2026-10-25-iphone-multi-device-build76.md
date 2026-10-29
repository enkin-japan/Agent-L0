# 计划：iPhone ↔ Mac 多对多连接 + 合并版界面 → build 76

> 状态：**已发布（build 76，2026-10-25）；2026-10-26 完成阶段复审，见 §12**。用户原话：「A和B，以及前面的一起，按你的建议做，但是先将计划落盘，等我压缩一下上下文之后再做」。
> 「按你的建议」意味着：Phase B **先出设计、用户确认后才动代码**；其余按本文顺序推进。
> 恢复执行时先读完本文，再跑 `./devctl.sh preflight`。

---

## 0. 现状（落盘时刻的事实，执行前逐条复核）

- `main` HEAD = `bac3e9e`：iPhone 经 iCloud 钥匙串同步连接 + 二维码配对备用 + macOS 去掉后端地址项；build 75。**未推送到 git 远端。**
- build 75 已发布：macOS 版（公证、Sparkle 更新源、75→74/73/72 delta 已上传 `enkin-japan/Agent-L0` 的 `updates`；本机已装）；iOS 版 TestFlight VALID。
- **build 75 是从干净提交构建的，不含工作区里的未提交改动**——用户此前本机跑的是 10-24 14:00 用工作区构建的 build 74（带那批界面改动），所以装上 75 后「外观变回去了」。那批改动仍完整保存在工作区，未被动过。
- 已核实合并无冲突（2026-10-25）：
  - 文本层面两边都改的只有 `L10n.swift`（未提交改动的词条在第 29 行附近，本次功能的词条块在第 728 行，615 个键，没有重复）和 `backend/app.py`（路由挂载相邻但不重叠）；
  - 合并后的工作区 macOS 与 iOS 模拟器都能编译；提交 bac3e9e 时 pre-commit 测的就是合并后的工作区（hook 不 stash），四道门全过；设计预览工具能编译、能出图；
  - 语义层面：401 仍会映射为「未认证」状态，鉴权依赖排在计算端就绪闸之前，离线缓存的分区不受影响（Mac 端存的就是默认地址）；
  - 差异：未提交的 `lib_edge.sh` 去掉了「推理不可达就拒启 backend」这道门，已提交的版本（即 build 75）在算力端离线时后端起不来。
- 算力端（Colab VM）**离线**，开机由用户决定（计费）。
- **build 75 已知缺陷（Phase A 修）**：iCloud 同步条目全局只有一条。同一 Apple ID 下有两台 Mac 都同步时会互相覆盖；iPhone 离线时自愈重读，可能被**悄悄切到另一台 Mac**。

## 1. 硬约束（违反即停）

- 只用简体中文和用户沟通；代码、命令、commit message 保持英文惯例。
- 不用 `--no-verify`；`config.yaml` 的 `bundle_id` 不许改；`.p8`、`.env*` 不能进包也不能进 git；不读 `reminder/bark.env`；密钥不经过对话。
- 开机或停用计费算力由用户决定。单 GPU 纪律：同一时刻只允许一个真模型进程（本计划默认不跑真模型）。
- 每个非平凡 diff：reviewer 先审 + 主会话再审；守卫类改动要做变异证明（清 `__pycache__`、按 sha256 还原、记进 commit message）。
- 超过 10 分钟的任务挂 task-sentinel。只在全部完成或受阻时推送，23:00–06:00 静默时段被拒就不重试。
- 本机环境实测（详见记忆 release-env-facts）：
  - 提交用 `git -c user.name=enkin -c user.email=contact@enkin.dev commit …`；
  - 构建要显式设 `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`；
  - 有未提交改动时，发布必须从干净的 worktree 构建；
  - generate_appcast 可能弹钥匙串授权框，要等用户点允许。

## 2. Phase 0 — 整理未提交改动（合并版的前提）

目标：把工作区里的未提交改动整理成逻辑清楚、经过审查的若干提交，让 build 76 能从干净的提交构建出合并版。

1. 先只读盘点：`git status` 与 `git diff --stat`，逐组读 diff。按主题拆分（实际分组以读 diff 为准）：
   - G1 CUDA 后端 MTP 越界 `</think>` 修复 + 测试（本会话早期诊断的那个工具调用故障）；
   - G2 计算端生命周期：`backend/framework/api/compute_lifecycle.py`（未跟踪）、`routes.py` 的 `require_compute_ready`、`app.py` 的 lifespan 重写、`scripts/lib_edge.sh`、`backends/remote`、`rag/compute_*`、`inference/server.py`、`config.yaml`（**核对 bundle_id 未变**）；
   - G3 离线历史与文件：`backend/framework/api/offline_files.py`（未跟踪）、`AgentClient`/`AgentStore`/`OfflineHistoryStore`/`OfflineAssetStore` 相关；
   - G4 原生 app 界面：`Sources/Views/*`、`Theme.swift`、`L10n.swift` 那 68 个词条、`ServiceStatus`/`Models`；
   - G5 原生测试：`apple-app/Tests/*`（含未跟踪的 `design_preview*`）、`backend/tests/test_native_*` 等；
   - G6 其他后端：`orchestrator`、`engine`、`retrieval_evidence`、`schedule/runner`、`mcp/tools/rag_search`；
   - G7 工具配置与文档：`.codex/`、`AGENTS.md`、`ARCHITECTURE.md`。**默认不提交 `.codex/` 和 `AGENTS.md`**（Codex 侧的配置，归属不明），先问用户。
2. 对每组：有依赖的按依赖顺序合并成一个提交，每个提交前都要过 reviewer 和主会话自审；守卫类改动补变异证明。提交时 pre-commit 测的是**工作区**（不 stash），所以拆分提交时，要先用 `git checkout-index` 导出暂存树、在导出的树上跑测试，证明每个提交单独成立。
3. 若某组明显未完成或审查发现重大问题：停下来汇报，由用户决定修、搁置还是丢弃，**不擅自丢弃**任何改动。

验收：工作区只剩用户决定不提交的文件；每个提交单独可编译、能过相关测试。

## 3. Phase A — 一部 iPhone 对应多台 Mac

### A1 Mac 侧：同步条目按 Mac 分开
- 每台 Mac 生成稳定的 `macId`（UUID，首次生成后存 UserDefaults，`phone.macId`），同步条目的 account = `connection.<macId>`。条目内容在 `{url, key, mac}` 之外加 `macId` 和 `updated`（时间戳）。
- 只写、只删自己那一条。`unexpose`、关闭同步、轮换密钥都只动自己的。
- 迁移：首次启动新版时删除旧的全局条目 `connection`。只删 service `dev.enkin.macagent.phone-link`、account `connection` 这一条。

### A2 iPhone 侧：多 Mac 列表 + 当前连接
- 新建 `SavedMac`：`{macId?, name, url, key, source(icloud|qr|manual), addedAt}`。列表存 UserDefaults（不含 key），key 存本机钥匙串（account `mac-key.<id>`，不同步）。「当前连接」= 列表里的某一项 id。`baseURLString` 和 `apiKey` 从当前项派生，保持与现有 `AgentStore` 及离线缓存兼容。
- 读同步条目：用 `kSecMatchLimitAll` 读出全部 `connection.*`，与列表按 `macId` 合并（新增 / 更新 url、key；Mac 那边删掉的条目标记为「已从 iCloud 移除」，**不自动删**，由用户删）。
- 自愈只针对当前那台 Mac：401 或离线时，只重读与当前 `macId` 相同的那条。**绝不切换到别的 Mac**，这是 75 缺陷的根治点。
- 界面：
  - iOS 设置页「从 Mac 获取」改为 Mac 列表，可点选切换（切换走 `applySettings`），可左滑删除；
  - 引导页：只有一台时一键使用，多台时让用户选；
  - 扫码配对得到的连接加进列表，并设为当前。
- 已有的 75 用户迁移：当前 `baseURLString`/`apiKey` 迁成列表第一项（source 沿用 `connectionSource`）。

### A3 推送向所有已保存的 Mac 登记
- `PushManager` 改为：拿到 APNs token 后，向列表里每台 Mac 的后端各登记一次（同一 `deviceId`），失败的下次启动或切换时重试；删除某台 Mac 时，先向它注销。
- 收到推送后，按 payload 判断来自哪台 Mac：在 `ma` 段加 `src`（后端的实例指纹，可复用 `/health` 的 `key_fp` 或新加 `instance_id`，**不放密钥**）。点开通知时，先切到那台 Mac，再定位会话。
- 涉及 PROTOCOL.md §2.8（推送 payload 增加可选字段，additive，不升版本）。

### A 验收
- Swift 纯逻辑测试（沿用 `backend/tests/test_phone_link.py` 驱动 swiftc 的方式）：多条目合并、只自愈当前 macId、迁移、删除；macOS 分支可直接测。iOS 分支的逻辑尽量抽成平台无关的纯函数，以便测到（弥补 75 中 iOS 分支无测试的缺口）。
- 变异证明：「自愈读到别的 Mac 也换上」「写条目不带 macId」「迁移误删他人条目」三个变异都必须被杀死。

## 4. Phase B — 一台 Mac 对应多部 iPhone：每台设备单独发票据

**先出设计给用户确认，确认前不写代码。** 设计草案要点（供写设计文档时展开）：
- 扫码 claim 不再返回共享的 AGENT 密钥，而是为这台设备新签一张**设备票据**（随机 256 bit），返回 `{token, device_id}`；后端只存 `sha256(token)` 和元数据（名称、首次/最近使用时间、来源），存放在数据根下的 `auth/devices.json`，权限 0600，写入要原子。
- `verify_token` 已预留 `devices` 分支（`framework/api/auth.py`，`app.state.auth_devices`）：改为从这份文件加载，比对哈希，并更新最近使用时间。共享密钥继续有效：Mac 自己用，iCloud 同步也继续用它。是否让 iCloud 也改成每台设备一张票据，留作待定问题。
- Mac 设置新增「已配对设备」列表：显示名称、最近使用、来源，可单独吊销（删掉哈希，下一次请求就 401）。
- 新增接口（需要 Bearer 共享密钥，只给 Mac 用）：`GET /api/v1/pair/devices`、`DELETE /api/v1/pair/devices/{id}`。白名单测试（只有 claim 免鉴权）保持不变。
- 推送登记的 `device_id` 与票据的 `device_id` 关联：吊销设备时一并注销它的推送。
- 安全面：票据只在 claim 的响应里出现一次（`no-store`），日志里只记 device_id；文件里只存哈希；吊销立即生效；绝不回显。属于 CLAUDE.md 所说的密钥处理类改动，建议在 Opus 会话里做，遇到分类器降级就停。
- 待用户决定：iCloud 同步继续用共享密钥（简单，同一 Apple ID 的设备都属于本人），还是同样改发设备票据（可单独吊销，但要有一次在线换领）；设备票据有没有有效期；名称取 iPhone 的设备名（`UIDevice.current.name`，iOS 16+ 需要授权才能拿到真实名称，否则只有「iPhone」）还是让用户自己填。

### B 验收（设计确认后细化）
- 后端金测试：吊销后立即 401；文件里只有哈希；损坏的文件不能导致放行（fail-closed）；并发写入不丢记录。
- 每条都要做变异证明。

## 5. Phase C — build 76 发布

1. 版本号升到 `CURRENT_PROJECT_VERSION: "76"`（与最后一个功能提交合在一起即可）。
2. 先推送 git 远端？——**问用户**（bac3e9e 起都未推送）。
3. 从干净的 worktree 构建（参照 75 的做法）：
   ```
   git worktree add --detach /Volumes/enkin/project/Agent-release-76 <commit>
   软链：build/runtime、apple-app/build/{sparkle-tools,SourcePackages}、apple-app/releases、.env.local
   DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer ./release-macos.sh   （挂 sentinel）
   …./release-ios.sh（挂 sentinel）→ ../backend/.venv/bin/python wait-testflight.py 76
   发布完：先 rm 各软链，再 git worktree remove --force
   ```
4. 核验：
   - `codesign --verify --strict`、`spctl -a -vv`、`stapler validate`；
   - entitlements 带两个钥匙串组，`embedded.provisionprofile` 存在；
   - appcast 含 76，delta 已生成；
   - 本机 app 能启动；
   - 界面和用户原先本机那版一致（用设计预览工具截图比对，或请用户确认）；
   - iOS 归档的 entitlements 正确，TestFlight 状态 VALID。
5. 推送一次完成通知（写清待用户在真机上确认的项）。

## 6. 需要用户在真机上确认的（不能自动化）
- Mac：「设置 → 连接 → 连接 iPhone」显示同步状态；
- iPhone：TestFlight 76 的引导页 / 设置页能列出 Mac、能切换，扫码能加入列表；
- 一台 Mac 有两部 iPhone 时（Phase B）：能分别吊销；
- 二维码配对的线上实测需要后端在跑：合并版去掉了「推理不可达就拒启」，所以算力端离线也能测。

## 7. 执行顺序小结
Phase 0（盘点 → 逐组审查提交）→ Phase A → 出 Phase B 设计 → **等用户确认** → Phase B 实现 → Phase C 发布 76 → 汇报。
每个 Phase 结束只做内部记录，不推送；只有整体完成或受阻时才推送。

---

## 8. 进度记录（2026-10-25 暂停时）

- Phase 0：工作区的未提交改动拆为六组：C1 测试脚手架（test_devctl_host_flag/test_preflight）、C2 CUDA、C3 RAG、C4 检索证据护栏、C5 remote 离线优先启动、C6 原生 app。
  - 在当前工作区跑 `./devctl.sh check`，四项全过。
  - **还没有提交任何一组。**
  - 审查结果：
    - C4 有重要问题：一次检索失败后，`_is_local_mutation` 会把 shell 命令（含 ls）一律拦下；改写过的查询无法消除 pending 状态；与去重拦截相撞。
    - C6 有重要问题：historyIsReadOnly 不会自动清除，导致「停止」和「审批」失效；媒体全量下载、没有上限；每 15 秒全量重新同步；`..` 按子串判断。
    - 以上两条已由主会话核实。**待用户决定**修、搁置还是丢弃。
    - C2、C3、C5 的 reviewer 报告暂停时尚未收到，恢复后需要重新发起审查。
- Phase A：已在 worktree `/Volumes/enkin/project/Agent-phaseA`（分支 phase-a-multi-mac，**未提交**）完成实现。
  - macOS 与 iOS 都能编译；test_phone_link、test_push、test_pairing 全部通过；11 个变异全部被杀死，脚本在 scratchpad 的 phaseA_mutants.py。
  - PROTOCOL §2.8 与 §2.9 已更新。
  - reviewer 报告未收到，恢复后重新送审。
  - 实例标识：Mac app 把 `AGENT_INSTANCE_ID` 注入执行层，后端在推送里带 `ma.src` 和 `attributes.src`，claim 返回 `instance_id`。
- Phase B：设计稿已写好，见 `docs/plans/2026-10-25-device-tokens-design.md`，等用户确认三个问题。
- C2 CUDA 的审查结果在暂停后才收到：没有阻断问题，83 项测试通过，bundle_id 没动，/health 只暴露白名单字段。有一项需要提交前核实：`attention_config` 和 `kv_cache_dtype="bfloat16"` 这两个参数，vLLM 0.24.0 的 AsyncEngineArgs 不一定接受，而测试里的桩接受任意参数，所以测不出来。如果不接受，第一次真实部署就会起不来。其余都是小问题，包括 server.py 的 unknown 分支缺 `reason` 字段、README 与实现不一致等。C2 不必再重新送审。
- C3 RAG 审查（恢复后收到）：没有阻断问题。有几条重要问题：
  - 配置值写错时 rag_search 和 rag_get_context 两个工具会一起消失，只在日志里留一行；
  - 三处错误日志被换成 `except: pass`，库名和分数都丢了，阈值校准的证据随之消失；
  - 客户端超时放宽到 120s，但服务端没有截止时间，被放弃的查询会在后台继续跑，单 GPU 上会叠加；
  - CPU 主机默认改用 fp32，内存大约翻倍。
  
  reviewer 跑全量测试时 test_native_recovery_transport 有 3 项失败，但主会话之前跑的 `./devctl.sh check` 全部通过。当时有多份全量测试在并发抢 CPU，疑似超时导致，需要复核。
- C5 离线优先启动审查（恢复后收到）：没有阻断问题，鉴权挂载顺序正确，offline_files 没有路径穿越问题。有几条重要问题：
  - 关机时等待后台任务结束没有设超时，可能被 SIGKILL 打断，GPU 来不及释放；
  - RAG 短暂不可达会让整个执行面返回 503，而且探活没有防抖，一次失败就翻转；
  - 重试日志只打印异常类名，丢掉了原来「地址或密钥配错」的那类可操作提示；
  - 引擎清理没有逐个隔离异常，一个后端报错，其余后端都不会被释放；
  - 闸门只按路径判断，`DELETE /chat/{id}` 在离线时也被拦下；
  - `/turn` 这个只读接口在首次装配之前可能返回 500。
  
  全量测试有 2 项失败（shell 超时 kill、native_recovery 的 overlap-same-early），单独跑能过，判断是不稳定的偶发失败，需要复核。

## 9. 用户决定（2026-10-25 恢复后）
- Phase 0 按建议处理：
  - C1、C2 直接提交。C2 的两个 vLLM 参数已对照 v0.24.0 源码核实：`attention_config` 是 EngineArgs 的字段，`"bfloat16"` 在 CacheDType 的允许值里。
  - C3、C5、C6 修完重要问题再提交，三个 implementer 并行在修。
  - C4 这次不带，留在工作区。
  - 做完要向用户逐条说明残留问题、原因和推荐处理。
- Phase B 第二稿已确认（`docs/plans/2026-10-25-device-tokens-design.md`）：
  - 每部 iPhone 都要握手、由 Mac 同意。同一 Apple ID 用 iCloud 请求凭据，Mac 弹框并显示 4 位核对码；不同 Apple ID 扫码。
  - 共享密钥不再离开 Mac。
  - 有效期默认 90 天，从同意时起算，不自动顺延；单台设备可以改成永久。
  - 两端都能给对方改名。
  - 已拿到共享密钥的旧 iPhone 自动换成设备票据。
  - 75 格式的条目不再双写。

## 10. 提交与发布（2026-10-25 晚）
- 用户要求合并为一次提交，以免完整检查重复跑：`c9934c3`，包括 C1、C2、C3、C5、C6 和 Phase A+B，构建号升到 76。C4、`.codex/`、`AGENTS.md` 留在工作区，未提交。
- 用户决定：先不推送远端（`bac3e9e` 与 `c9934c3` 都只在本地）；先发布 build 76，阶段全盘复审（stage-reviewer）暂时搁置。
- 已发布（2026-10-25 23:20）：
  - macOS 76：已公证并钉票，更新源已更新（增量补丁 72–75），本机已装；算力端离线时 `data_ready` 为 true，可以启动。
  - iOS 76：TestFlight 状态 VALID。
  - 发布用的 worktree 和 Phase A 的 worktree 都已清理；完成通知遇上静默时段，已写入 suppressed.log。
- 待办：推送远端（用户决定暂不推送）；阶段复审（已搁置）；C4 的去留；真机确认（见第 6 节）。

## 11. 遗留问题清单（build 76 发布后，2026-10-25）

现状：build 76 已发布（见第 10 节）。下列问题都不阻断使用，各条注明了原因和建议。

### 11.1 待用户决定
- **C4 检索证据护栏**（`backend/framework/core/orchestrator.py`、`retrieval_evidence.py`、`backend/tests/test_retrieval_evidence_guard.py`、`run_retrieval_guard_mutations.py`）：留在工作区，未提交。
  - 原因：设计层面有问题。一次知识库检索失败后，`_is_local_mutation`（判据是「非只读」）会把 shell 命令连同只读的 `ls` 一起拦下；换个说法重新检索也消不掉待定状态；而且它会与去重拦截相撞，导致一次偶发失败就让整轮无法恢复。
  - 建议：只拦截真正产出内容的工具（如图表、文件写入），允许换个说法重新检索后解除；如果改不好，就丢掉。
  - 2026-10-26 复核（reviewer 首审 + verifier）：机制自洽——31 项用例全绿、10 个变异在隔离副本里全杀、相关回归
    474 项通过；但三条设计问题仍在：(1) `registry.is_read_only` 对 shell 恒为 False，一次失败/空结果后本轮
    `ls`/`cat`/`git status` 全被当交付拦下（FakeTool 默认只读，测不出）；(2) 改 optimized_query 可解除，改
    query/category 不能，而 rag_search 自己的文案让模型"换说法或分类重试"；(3) 空结果也计 pending，错分类搜空后
    在对的分类命中证据，正确答案仍被丢弃。另有：同批屏障不看 pending（已批准的 `[write, rag_search(成功)]` 里
    write 不执行）；子生成预算/解析失败/迭代上限三个出口被检索解释盖掉真实原因。结论：需修后合并；仍留工作区。
  - 2026-10-26 修复（用户决定"按建议修"）：R1 只拦真正产出内容的工具（显式 DELIVERY_TOOLS 含
    excel_research；shell 交给 `registry.requires_approval` 自判只读；动态 API 工具看 `_method` 且非只读）；
    R2 空结果算已完成检索，不再是 pending；R3 之后任一次完成的检索（任意目标）解除全部 pending，文案与
    rag_search 自己的重试措辞一致；R4 同批屏障、approval 前拦截与 complete_step/remember 拦截只在 pending
    时生效，同批里排在交付前的检索仍先被冲掉（不等 pending）；R5 四个预算/解析/失控出口保留真实原因，只是
    追加检索说明（`metrics["retrieval_incomplete"]`），`_retrieval_incomplete()` 只用于预判生成路径。
    状态：已修复，待提交（由主会话提交）。
- **阶段全盘复审（stage-reviewer）**：用户决定搁置。本阶段一共改了 114 个文件，建议之后补跑，范围 `78e1660^..0a2aac9`（5 条提交、161 个文件）。
- **推送远端**：`bac3e9e` 和 `c9934c3` 都只在本地。
- **`.codex/` 与 `AGENTS.md`**：属于 Codex 侧的配置，归属不明，未提交。

### 11.2 需要真机确认（自动化覆盖不到）
1. 同一 Apple ID：iPhone 的 Mac 列表点选后发起请求，两边显示同一个 4 位核对码，Mac 点「允许」后能连上。
2. 不同 Apple ID：扫码连接。
3. Mac 设置 → 连接 → 连接 iPhone：设备列表能改名、改为永久、移除；移除后对应设备立即断开。
4. 原本用共享密钥的 iPhone 升级后，自动换成设备票据（设备列表显示「旧连接升级」）。

### 11.3 技术遗留（按重要性排序）
1. **RAG 服务端没有截止时间**（C3）：客户端放弃的检索仍在 `QUERY_POOL` 里跑，单 GPU 上会越堆越多。建议给检索加服务端预算，超时即中止。
2. **设备票据没有权限分级**（Phase B）：设备票据能调用的接口与共享密钥相同，包括写入 `.secrets.env` 的 `/api/v1/api-tools`。目前唯一的控制手段是吊销。建议把这类接口限定为只有 Mac 本机（owner）能用。
3. **iPhone 端部分逻辑没有运行时测试**：
   - PhoneLink 的状态流转（`readSynced`、`select`、`remove`、`claim`、握手）只有 iOS 编译检查，测试只覆盖了 `MacDirectory` 里的纯函数；
   - 按流量计费 / 低数据模式下的附件延迟同步（`NetworkPathPolicy`）在 macOS 测试机上恒为允许，走不到。
   - 建议加一个只在 DEBUG 构建生效的测试接口，补上这两块的测试。
4. **启动顺序**（C5）：
   - remote 模式下后端先于本地 RAG 启动，第一次装配必然失败，要等一轮退避，冷启动多出 2–8 秒。建议本地 RAG 先启动，或者缩短第一次重试的间隔。
   - 算力端换了模型之后，身份检查会一直失败，必须重启后端才能重新装配。
   - 探活读到的是新密钥，已装好的 RemoteBackend 用的还是旧密钥；轮换密钥后会显示「就绪」，但生成请求返回 401。
5. **离线缓存的边界情况**（C6）：
   - 后端只要返回一次空的会话列表，就会清空本机全部缓存，没有防误删的检查。建议在这种情况下要求再确认一次。
   - 把附件预算调大后，之前标成「部分完成」的会话不会自动补下。
   - 附件同步期间，正文同步要等它完成。
   - `MarkdownText` 每次增量渲染都在主线程做一次完整的 cmark 解析，长代码块的流式输出可能变慢。
   - 本地缓存分区用的是未加盐的密钥 sha256。
6. **CUDA**（C2）：`/metrics` 每次抓取都会触发一次 `do_log_stats`，需要在下次真实部署时确认，必要时改为缓存快照。「投机解码指标可能一直显示 `counters_missing`」已证伪关闭：`colab/README.md`:203-204 的实测显示 `status=observed`，草稿 129 / 接受 107，计数器确实注册并更新。
7. **其他小问题**：
   - `device_expired` 没有单独的提示文案，到期与被移除显示同一句话；
   - `GET /pair/ticket` 有副作用（首次请求会生成凭据）；
   - 设备记录文件读取时遇到短暂的 OSError 也会按「损坏」处理并改名留存；
   - 第一次请求时才加载设备记录，因此记录损坏的日志不会在启动时出现；
   - `/health` 对外暴露 `data_ready`、`compute_ready`、`compute_state` 三个状态字段（不需要鉴权）；
   - RAG 每次查询会产生 20–40 行 INFO 日志；
   - README 里的 CUDA 环境变量说明还停留在旧版本。
8. **RAG CPU 主机默认 fp32**（C3，见第 8 节 :147）：未处理，保留默认，原因：待用户决定。
9. **变异证明脚本未入库**：Phase A 16 个、Phase B 26 个、C5 13+3 个、`bac3e9e` 的 M1-8/S1-8 变异证明脚本
   未入库，scratchpad 已清空，无法复算；本次复审新增的三份变异脚本已在工作树（未提交）
   （`backend/tests/run_pairing_push_mutations.py`、`backend/tests/run_shell_exec_secret_mutations.py`、
   `apple-app/Tests/run_build76_review_mutations.sh`）。

### 11.4 本次顺带修复的既有测试缺陷（已在 c9934c3 中）
- `test_batch_lane.py::test_decoding_count_gauge_distinct_from_active`：没有显式指定 `max_tokens`，被默认值 4096 截断，导致在这台机器上必定失败。
- `apple-app/Tests/task_control_test.swift` 的 `takeHeld()`：存在竞态，高负载下会在空队列上崩溃。

## 12. 2026-10-26 阶段复审

范围：`78e1660^..0a2aac9`（见 §11.1）。下列发现均已在工作树修复（2026-10-26，未提交）或保留待定；
修复详情见对应 commit message。

### Major（已在工作树修复，2026-10-26，未提交）
- **A1** `colab/compute_ctl.py:826-830`→`:612-622`：`colab new` 干净失败后 `allocation_attempted` 已置位，
  `stop` 永远记 `release-unconfirmed`，`start` 被拒，只能手删 `.colab-compute/current.json`；`finally`
  里的错顶替了原始 `CalledProcessError`。
- **A2** `colab/README.md:79-81,:187-208`：吞吐与 64K 依据的脚本和原件只在 gitignored 目录（见第 1 节）。
- **B1** `backend/framework/api/routes.py:583-592`、`pairing.py:360-367`、`devices.py:160-169`：推送条目绑死
  首个票据，同机重配对（到期或重扫码）后 `prefs`/`live-activity`/`register` 全 403；到期设备仍收推送；
  `_drop_push` 无条件删条目。
- **B2** `backend/framework/mcp/tools/shell_exec.py:95,:115,:139,:584`：工具子进程继承全部环境变量；
  `cat .env.local`/`grep -r` 免审批可读密钥文件；输出无脱敏（根因 71c1a01/3b2d4d6，早于本阶段；`ps -E` 在
  macOS 27 实测不显示环境，原报告机制修正）。
- **B3/C2** `apple-app/Sources/App/PhoneLink.swift:903-906`：`expose()` 不检查密钥，空钥 repo 布局下
  `tailscale serve` 把免鉴权 owner 接口暴露给整个 tailnet；`app.py:253` 护栏只看绑定地址。
- **C1** `apple-app/Sources/Store/OfflineAssetStore.swift:179`、`OfflineHistoryStore.swift:45-52`：缓存按
  地址+密钥哈希分区，换票据后旧分区永不回收，1G/5G 预算仅限当前分区。
- **C3** 变异证明脚本未入库：已在工作树（未提交）新增三份（见 §11.3#9）。

### Minor（已在工作树修复，2026-10-26，未提交）
A-2 `compute_common.py:85-95` `A=#foo` 读成空串与注释矛盾；B-m1 吊销不停正在进行的灵动岛；B-m2
`app.py:917/919` 就绪闸没有测试钉住；B-m3 `/pair/request`、`/pair/poll` 的 no-store 无断言；B-m4
`PROTOCOL.md` 漏 `DELETE /pair/self`、`503 compute_not_ready`、`/health` 新字段、`offline-files` 路由、
推送 403 归属规则，「一律 no-store」不实；B-m8/C-11 `phone_link_test.swift` 真实 tailnet 名；B-m9
`compute_lifecycle.py:182,199-202` 日志量；C-m1 停止开放竞态；C-m2 界面文案 75 版口径；C-m4 iOS 重试按钮被
dismiss 取消；C-m6 锁屏后台抹掉离线索引；C-m7 票据 Keychain 非 ThisDeviceOnly；C-m8 升级重试/轮询无上限；
C-m9 `bundle-macos.sh` 排除清单缺 `/push/` 与 `/registrations/`；C-m12 `ApprovalIntents` 重写
`approvalTarget` 规则；A-m4/5/6、C-m13 文档修正（本节与第 1、2 节）。

### Minor（保留待用户决定）
A-m1 `78e1660`/`c9e2772` 单独检出不可部署（历史，HEAD 已一致）；B-m5 设计稿 §7「仍有 N 台旧方式连接」
无落点、:106 与 `pairing.py:121` 保留 `api_key` 别名不符；B-m6 残留 11.3#4(3) 描述的状态进程内不可达；
C-m5 Mac 端同意框在设置页 sheet 下可能看不见；C-m10 `asc-macos-profile.py:112-132` 保留旧描述文件不核对
证书。

本次复审新发现，追加到本列表：
- **(a)** shell 工具对绝对路径整片目录的递归搜索（如 `grep -r KEY /Volumes/enkin/project/Agent`）仍免审批，
  只剩输出脱敏兜底。
- **(b)** 到期的设备票据自己仍无法注销（`DELETE /push/devices`、`/pair/self` 返回 401），已由发送侧跳过
  到期设备缓解。
- **(c)** 重新配对以自报 `push_device_id` 认同一部手机：首审后已收紧为「只取代**已到期**的同 push id
  旧记录；接管推送条目还要求调用方自己记录里的 push_device_id 与条目一致」，仍有效的票据不会被自报 id 顶掉
  （旧票据仍有效时重新配对改由 iPhone 先调 `DELETE /pair/self` 注销）。残余面：已获 Mac 同意、且知道别人
  push id 的设备，可在对方票据到期后接管其推送条目；push id 从不出现在任何响应里，需先泄露。
- `apple-app/Tests/service_credentials_test.swift`：补上 `ConnectionSource` 桩（这个类型已移到 PhoneLink.swift）。

### 12.4 两道门之后的追加修复（2026-10-26，已在工作树，未提交）

首审（reviewer）对上述修复又提出 2+2+5 条 Major 与若干 Minor，处置如下：

- colab：`colab new` 非零退出不等于服务端没分配——记 `stopped` 前 `_confirm_no_allocation` 连续三次
  （间隔 5 s）核对服务端既无本会话也无 `?` VM；`stop --confirm-unallocated` 只对本次调用生效、不落盘；
  启动失败的日志不再可能顶替原始异常。`compute_common` 的 `.env` 解析：`x\ #c` 保留 `#`，未引号的
  `; & | < > ( ) \`` 一律拒绝。
- backend 配对/推送：只取代**已到期**的同 push_device_id 旧记录；接管推送条目要求调用方记录的
  push_device_id 与条目一致；`_adopt_push` 失败不影响发票据；跳过日志按 owner 300 s 节流；灵动岛发送前
  按 token 快照复核（关掉通知的 iPhone 没有注册表条目，不能按"条目缺失"跳过）。
- shell 工具：递归读取类命令（grep -r/-d recurse、rg/ag/ack、diff -r）的每个搜索根解析为绝对路径后必须
  严格在仓库内且不与任何已知密钥位置（`.env*`、`.hf_token_tmp`、`.keys/`、`backend/{.secrets.env,auth,
  push,registrations}`、`.colab-compute/`、`reminder/`；家目录 `.ssh/.aws/.gnupg/.netrc/.envrc/.config/gh/
  .docker/config.json/Library/Keychains`）有祖先/后代关系；折叠危险字符（NFKC 变形或 casefold 后含
  ASCII 字母）审批、中文免审批；grep/rg 的模式操作数不当路径；免审批执行剔除全部密钥类变量，已审批命令
  只剔后端四把钥（保留 HF_TOKEN/GH_TOKEN）；`ps -ef` 免审批、`ps -E`/`ps e` 审批。工具描述多了一句，
  `model_visible_text.snap` 随之更新。
- Swift：expose() 先向后端发无 Authorization 的 `GET /api/v1/pair/devices`，必须 401 才 serve；已开放时
  探测失败显示持久警告（不自动关）；清理经 `OfflineCachePruner` actor 串行、`keepSet` 任一 key 为空或
  会删光时返回 nil 整轮不删；锁屏索引读失败只跳过本轮写入、不弹横幅、历史同步整轮跳过；附件淘汰先标记
  再删字节；5xx 一律瞬时错误；升级退避落 UserDefaults（键为地址+密钥哈希）；`stopRequested` 直到真正
  未开放才清；换票据前先用旧票据 `DELETE /pair/self`（≤5 s，best effort；upgrade 路径手里是共享密钥，
  不适用）。
- 新增残留（待用户决定）：iOS 删掉最后一台 Mac 后其缓存分区不再清理（"不能删光"守卫的后果）；仓库外的
  递归搜索一律审批（含用户其他项目）；半角片假名等兼容字符按折叠危险处理；仓库内 `.claude/worktrees/*`
  的 backend/auth 不按密钥位置处理；iOS 换票据 / 重试弹窗 / Keychain ThisDeviceOnly 迁移只过了编译与主机
  测试，未在模拟器实跑。
- 终审（第二遍 reviewer）追加：shell 工具的密钥位置清单原写 `backend/.secrets.env`，而该文件实际在
  `data_dir()/api_tools/.secrets.env`——改为整个 `backend/api_tools` 目录，并把运行时 `data_dir()` 下的
  auth/push/registrations/api_tools 与 `~/Library/Application Support/dev.enkin.macagent` 列入；词元层也按绝对
  路径与密钥位置比对。`stop --confirm-unallocated` 的 CLI 布线补了测试（verifier 的变异曾存活）。Swift 侧：
  当前 Mac 曾拒绝本机后重扫其二维码时新票据未被采用（HEAD 已有，本次注销让它更易触发）、`stopRequested`
  持久化、鉴权探测周期重探、等价性测试区分 refuse/legacy、票据键孤儿、守卫变异接进 pytest。
- 变异证明（commit message 为正式记录）：run_pairing_push_mutations 16、run_shell_exec_secret_mutations 25、
  run_build76_review_mutations 13（已接进 pytest：backend/tests/test_native_build76_mutations.py）、
  run_offline_history_mutations 18，另有 compute_ctl/compute_common 5 个由 verifier 手工注入；全部变红并按字节
  还原。

### 12.5 C4 检索证据护栏重设计（2026-10-26）

首审（reviewer）发现 11.1 所列三条设计问题属实后，用户决定按建议修，而不是丢弃。五条规则：

- R1 拦截范围：新增 `is_delivery(registry, call)` 谓词，取代两处拦截点上的 `_is_local_mutation`。显式
  `DELIVERY_TOOLS` 集合（图表/文档/文件/幻灯片、memory_write、rag_insert_document、通知/剪贴板/日程/文本
  输入、`run_applescript`、`excel_research`）；`run_shell_command` 交给它自己的只读判据
  （`registry.requires_approval`）；动态 API 工具（api_tool_factory）只有同时满足「带 `_method` 属性」且
  「非只读」才算交付，没有 `_method` 的非只读工具不算。
- R2 空不算待定：`observe()` 里 "evidence" 与 "empty" 都算已完成检索，只有 "failed" 才进入 pending；
  `rag_get_context` 的"未找到"类服务端消息不再被错分类为失败（见下方 reviewer 首审修复）。
- R3 全局解除：之后任何一次已完成的检索（证据或空结果，任意目标、任意工具）清空全部 pending 并重置
  overflow；REPAIR_NOTE/DELIVERY_BLOCKED 改成与 rag_search 自己的重试措辞一致（换说法/换分类/换上下文指针）。
- R4 同批屏障只在 pending 时生效：approval 前拦截与 `complete_step`/`remember` 拦截都改为只在
  `retrieval.unresolved` 时触发；同批里排在交付前的检索（无论是否已有 pending）一律先冲刷执行，不参与屏障
  判断（即附录条款，见下）。
- R5 出口保真：`max_sub_generations`/`parse_loop`/`max_total_iterations`/`runaway_call` 四个出口保留各自
  原本的 status/response/activity，只在 unresolved 时追加检索说明并置
  `metrics["retrieval_incomplete"] = True`；`_retrieval_incomplete()` 只用在预判生成路径（工具调用耗尽后的
  一次 REPAIR_NOTE 提醒）。
- 附录（规范之后商定）：同批里排在交付前的检索一律先冲刷，不管是否已有 pending：`[rag(失败), chart]` 挡住
  chart，`[rag(成功), chart]` 正常执行；`[chart, rag(失败)]` 且本来无 pending 时按 C4 之前的行为执行（已知
  缺口，见下）。

首审（reviewer）发现并已修复：`rag_get_context` 的"未找到"类服务端消息（如"未找到该 source_path 的任何
切片…"、旧格式入库、`chunks=[]`）曾被错分类为失败，现改为：任何非失败、非证据的 `rag_get_context` 响应都算
"空"（已完成）。连带 Minor：C4 拦截的响应带 `_blocked: True`，不计入 ⑤′-3 的 upgrade_failures 计数器、也不进
未验证写入扫描（批处理循环与 approval 恢复路径两处），`_failed` 字段仍保留给 D2 用；`runaway_call` 出口补上
检索说明；`write_thing` 测试夹具的 `concurrency_safe=False`；串行执行路径的 observe 调用补了覆盖；
`RetrievalEvidence.__init__` 对畸形 checkpoint 容错不抛异常。

测试：`test_retrieval_evidence_guard.py` + `test_fabrication_guardrails.py` + `test_turn_runner.py` 共
84 项通过（重设计前是 60 项；护栏文件本身 55 项）。变异证明 `backend/tests/run_retrieval_guard_mutations.py`：
19 个变异全部变红（20 行结果，M-A 对应两条测试）、基线全绿、副本跑完后工作树按字节还原（verifier 独立复跑）。
verifier 跑的完整后端套件：3304 通过、3 跳过。主循环里对被拦写调用的 `_blocked` 跳过（orchestrator.py ~1096）
证明不了：被拦的写之后要么进临时生成提前返回，要么后续检索本来就清旗，两种候选测试在变异下都过；保留为
防御性代码，无变异证明。审批续跑一侧的同类跳过有测试与变异 M-L。

保留待用户决定（已知弱点，未修）：
- 全局解除代价低：`rag_search` 返回"无结果……知识库为空或暂不可用"也算一次完成的检索，照样解除全部 pending。
- 一个全新批次 `[render_chart, rag_search(失败)]` 仍会先跑图表（交付排在检索之前、当时无 pending），与 C4
  之前的行为一致。
- 已审批的交付排在检索之后、检索随后失败时，仍会在审批之后被拦（approval 对话框等于白问一次——只在检索失败
  这一种情况下发生）。
- 预判生成路径上，模型自己写的诚实文本（"检索未完成"或 `web_research` 给出的回答）仍会被替换成固定的
  「尚未完成」文案。
- `_retrieval_incomplete()` 走 `_exit_audit`，因此像其他非回答类出口一样会丢掉云端降级/升级提示。
- `runaway_call` 出口的追加说明没有测试钉住（预判路径下不可达）。
- 动态 API 工具靠 `_method` 属性这个启发式识别，不是协议层面的保证。

## 13. macOS 发布整理（2026-10-26，用户决定）

整理前：Agent-L0 公共仓只有一个滚动 Release（tag `updates`，标 Latest），堆着 appcast.xml、build 70–76 的七个
DMG（约 5GB）和十个 delta；旧仓 Agent-L0-macOS-app 的 `updates` 镜像 appcast 并留着 70–74 的 DMG。
整理后：
- 每个构建一个 Release：`macos-0.16.0-74/75/76`，tag 分别打在 52c82fd、bac3e9e、c9934c3；各放自己的 DMG 与升到
  该版的 delta（74：3 个资产，75：4 个，76：5 个），76 标 Latest。资产名与大小与本地 `apple-app/releases/` 逐一核对。
- `updates`（两仓）只留 appcast.xml，标题改为「Sparkle 更新源（appcast）」；已装 app 的 SUFeedURL 指着它，tag 不改名。
- appcast 的 12 个 enclosure URL 由新脚本 `apple-app/appcast_release_urls.py` 改写到各构建的 Release（EdDSA 签的是
  文件字节，不含 URL）；两仓 feed 与本地 appcast 哈希一致，12 个地址 HEAD 全 200。
- 70–73 的 DMG 已不在 appcast 里，从两仓 `updates` 删除（本地 `releases/`、`old_updates/` 仍有存档）；74 的旧仓副本同删。
- `release-macos.sh` 改为新流程：生成 appcast 后先改写 URL → 以草稿建本构建 Release（--target 当前提交）→ 上传 DMG +
  本版 delta → 远端资产名/大小/sha256 与本地核对 → 公开并标 Latest → appcast 推两仓 `updates`。守门：发布前工作树
  必须干净且 HEAD 已在 origin/main 上（tag 要指向发布所用提交）；重复构建号按发布列表判且 gh 出错即停（不再
  fail-open）；上次失败留下的草稿删掉重建；origin 已有同名 tag 且指向别的提交则拒绝。发布说明可放
  `releases/notes-<build>.md`。改写器对六类畸形输入（外域地址、单引号外域、指向他版的 delta、与 sparkle:version
  不符、releaseNotesLink 仍指 updates、`../..` 仓名）均拒绝且不改文件（reviewer 首审后补的反向用例）。README、
  project.yml 与 MacAgentApp.swift 的注释同步更新。
- 本机验证：feed 可解析（3 条目、12 enclosure），本机装的 76 即 Latest，无更新可提示；新脚本的真实首跑要等 build 77。

## 14. iPhone 手填入口移除（2026-10-27）

用户决定：iPhone 连 Mac 只有两条路——iCloud 钥匙串同步（同一 Apple ID，Mac 上点允许）与扫 Mac 上的配对二维码；
不留手填，也不留「高级」折叠。macOS 的 AGENT 密钥框只在用户确实需要时出现。

移除：
- iOS 设置页的「后端地址」框、「API Key」框、「测试连接」及其说明文字（`SettingsView.swift`）；iOS 设置页已无待保存
  输入，工具栏的「保存 / 取消」换成「完成」。
- iOS 引导页的手填分支（「或者手动填入…」、地址 / 密钥框、「保存设置」，`OnboardingView.swift`）：引导页只剩 Mac
  列表 + 「稍后配对」，点选或扫码确认后由 `PhoneLink.apply()` 标记引导完成。
- 引导页出口「稍后配对」：与旧版「地址留空保存」同效——`baseURLString` 清空（否则下次启动 build 75 迁移会把默认
  回环地址当成一台 Mac 迁进列表）、来源记 manual、`setupCompleted = true`、`applySettings()`（无地址即不连）。进主界面
  后照样能配对：系统相机扫码由 `MacAgentApp.swift` 的 `.onOpenURL`（引导页与主界面共用同一处）弹确认框；Mac 列表在
  主界面 ⚙（`SessionListView.swift` 工具栏）→ 设置。
- 随之失效的代码：`PhoneLink.saveManual(url:key:)`（唯一会新建 `.sharedKey` 条目的入口）与 `PhoneLink.disconnect()`、
  `PhoneConnectionSection.onApplied` 回调；L10n 里只服务手填入口的 5 个词条（含两条早已无引用的手填提示）。
- macOS 连接页：AGENT 密钥框、「复制」与 `.env.local` 说明只在 devctl / repo 布局、未启用内建执行层、收养了外部后端、
  或 14159 上的后端自报布局 repo / unknown（未设 AGENT_LAYOUT 起的后端）时出现；bundle 布局、执行层在管且对端
  没探到或自报 bundle 时只显示「后端密钥已自动生成并同步」+「重新生成」
  （它写钥匙串后 `sidecar.restart()`，`resolveLayout` 重新注入 `AGENT_API_KEY`，会同步运行中的后端）。repo 布局不再
  显示「重新生成」：那里的后端从 `.env.local` 取密钥，换了重启后也对不上。「重新生成」的说明改为：仍用旧共享密钥的
  客户端（旧版手填或 devctl 工具）会 401、须重新配对或同步，有设备票据的 iPhone 不受影响。
- Siri「问 MacAgent」未连接时的提示改为「尚未连接 Mac，请先在 MacAgent 里通过 iCloud 或扫码配对。」并走 L10n
  （`App/Intents.swift`）。

刻意保留：
- 存储键与属性：UserDefaults `agent.baseURL`、钥匙串 `agent-api-key`、`settings.baseURL` / `settings.apiKey`——锁屏旧路径
  `LiveActivity/ApprovalIntents.swift` 直接读它们；配对后它们由 `PhoneLink.apply()` 写入（apiKey = 本机设备票据）。
- `.sharedKey` 凭据与 `POST /pair/upgrade` 升级路径（`PhoneLink.upgradeSharedKeys()`）：build 75 的 iPhone 与旧版手填的
  条目仍持共享密钥，启动后照常换成设备票据；`Credential.sharedKey`、`ConnectionSource.manual` 与「手动填写」来源标签留着
  显示这些旧条目。
- 后端 `backend/framework/api/pairing.py` `_issued()` 的 `api_key` 同值别名（build 75 的 iPhone 只认这个字段名）。
- 源码扫描护栏：`backend/tests/test_phone_link.py::test_wiring` 断言 iOS 设置页（连接区与整个 iOS Form）、iOS 引导页
  与 `PhoneConnectionSection`（改名框除外）不含地址 / 密钥输入与「测试连接」，`saveManual` 不再出现，引导页留有
  「稍后配对」，且 macOS 自管判据含 `peerLayout == nil || peerLayout == "bundle"`。6 个变异体（四处各塞一个输入框、
  macOS 自管分支塞密钥框、判据退回 `!= "repo"`）均转红（在源码副本上跑，仓库未动）。

恢复路径（不再有 app 内重填）：build 75 手填 / 迁移来的 `.sharedKey` 条目，若 Mac 之后轮换了 AGENT 密钥、旧共享
密钥已失效而该条目还没换到设备票据，就在 Mac 列表左滑删除这一行，再扫这台 Mac 的配对二维码（同一 Apple ID 的也可等
iCloud 条目出现后点选、在 Mac 上点允许）。

已知、未修：macOS 设置页「保存」写回的 `keyText` 是开页时的快照（devctl / repo 布局下密钥框可见时才写），开页期间
别处改了 `settings.apiKey` 会被旧值盖回（复审 nit 7，既有问题，本次不动）。

## 15. build 77 发布（2026-10-27）

- 版本号提交 `b71606b`（四门全过，已推送）。随包内容：iPhone 仅 iCloud/扫码配对（§14）、macOS 自管密钥隐藏（§14）、
  C4 检索证据护栏重设计（§12.5）、每构建一个 Release 的发布布局（§13）。发布说明 `apple-app/releases/notes-77.md`（gitignored）。
- macOS：`release-macos.sh` 新流程首次实跑成功——两次公证通过，Release `macos-0.16.0-77`（tag 指 b71606b，6 个资产：DMG +
  77←72…76 五个 delta）公开并标 Latest；appcast 由 generate_appcast 保留 75/76/77 三版、URL 改写到各构建 Release，15 个地址
  HEAD 全 200；两仓 `updates` 仍只剩 appcast.xml 且哈希与本地一致；本机已重装钉票版 77。
- iOS：`release-ios.sh` 归档、导出、上传 App Store Connect 成功（06:27 上传），等 ASC 处理后 TestFlight 可见；本文档写时尚未确认
  处理完成。
- 踩坑：`bundle-macos.sh` 与 `release-ios.sh` 的 `DEVELOPER_DIR` 默认值指向已不存在的 Xcode-beta，首跑在 ① 构建处即退（无副作
  用），显式传 `/Applications/Xcode.app/Contents/Developer` 后重跑成功。已改默认为 `xcode-select -p`（见后续提交）。
- 真机待确认：iOS 引导页「稍后配对」后能否扫码连接；macOS 设置页在捆绑形态下显示的是状态行而非输入框；Sparkle 从 76 升 77 的
  delta 更新是否正常（本机是重装而非 delta 升级，未验）。

## 16. build 78：就绪信号拆分、审批策略、聊天执行过程折叠、iPhone 取消配对（2026-10-27）

### 16.1 问题与根因（用户 2026-10-27 提出五项）

- 「计算端启动中」约 30 s：捆绑形态 `scripts/lib_edge.sh` 远端分支先起 backend、等其 `/health` 后才起
  RAG；backend 后台装配首试探算力端（通常通过）后探本机 RAG（:14160 未起）必败，进入 2/4/8/16/30 s
  退避；RAG 冷启约 14 s 赶不上第 14 s 那次重试即落到约 30 s；app 可达后每 15 s 轮询一次再取整。两层
  错配：「后端已就绪」只代表 HTTP 端口通（装配在后台），「计算端启动中」实际是整条本地装配链。
- iPhone 删除 Mac 后「重新检查」找不回：删除把 macId 与 url 写进 UserDefaults `phone.hiddenMacs`
  永久隐藏名单，merge 过滤；唯一解除路径是扫码 `claim()`。
- 聊天页被执行过程条目填满；审批卡在消息流末尾被输入栏盖住；权限无统一控制。

### 16.2 决策与实现

- **Contract H**（PROTOCOL §2.10 增补）：`/health` 新增 `engine_ready`、`compute_node_state`
  （local/probing/ready/unreachable/unauthorized）、`rag_state`（skipped/probing/ready/unreachable/
  unauthorized）；`compute_state` 语义不变（`backend/app.py:339-343,938-943`）。装配前只差本机（回环）
  RAG 且为连接级/5xx 失败 → 2 s 固定重试 ≤120 s，钥错/远端 RAG 走退避；就绪后监控期
  `compute_node_state` 与 `compute_state` 共用连续失败阈值翻转（不闪，`backend/app.py:412-449,584-585`）。
  `lib_edge.sh` 远端分支 backend 与 RAG 并行拉起（`start_*` 带 `--no-wait` 只过前置门并拉起，真拉起了
  才记一笔待等，由 `wait_*_spawned` 补等；`scripts/lib_edge.sh:670-789`），backend 就绪失败撤掉本次
  拉起的 RAG。app：计算端标签只看 `compute_node_state`；可达但 `engine_ready=false` 显示「后端启动中
  （等待检索服务）」；未就绪时 2 s 轮询（probing 或 engine 未就绪），unreachable/unauthorized 与旧后端
  仍 15 s；sidecar 起来即刷新一次。
- **Contract P**（PROTOCOL §2.11 新增）：`BaseTool.effect` ∈ read|write（缺省 write，18 read / 42
  write，`backend/framework/mcp/tools/*`；`run_shell_command` 按命令动态，API 工具 GET/HEAD=read；
  `run_math`/`math_research` 因沙箱放行子进程归 write）；策略 {read, write} ∈ auto/default/ask，缺省
  均为 default（等于 build 77 行为，`backend/framework/core/approval_policy.py`）。
  `MCPRegistry.approval_required`（`backend/framework/mcp/registry.py`）：未知名恒审批（闸门侧未注册
  名不进闸、沿旧路径报 `No tool named`）；三条硬规则压过档位——钉死（API 工具声明里显式写的
  `requires_approval`、`cloud.require_approval` 开着的云咨询）auto 不得越过；无人值守轮
  （`turn_origin != chat`）一律按 default 判；策略文件缺失按缺省、存在但损坏按 ask/ask 并告警一次。
  `GET`/`PATCH /api/v1/approval-policy`（`routes.py:152-180；策略模块 approval_policy.py`；PATCH 落盘失败 503
  `policy_save_failed`，内存策略不变），两路由首次装配前可用；`GET /api/v1/tools` 多报 `effect`。
  macOS 设置新增「权限」页。
- 聊天：实时步骤单行紧凑（状态符号+标签+单行详情）；结束后同一轮连续工具行折叠为「执行过程 · N
  步 · X 秒」（N 按调用对计，回执按工具名+角色在行数与回执数相等时按序贴、否则不贴状态（行上没有 callId））；展开态按 `turnIndex` 加首行序号
  的稳定键存于 `AgentStore`（`apple-app/Sources/Views/ChatView.swift:58-59,280-485`、
  `ChatMessage.groupedForDisplay`、`AgentStore.expandedToolGroups`、`MessageView.swift` 的 `ToolGroupView`）。审批卡移到输入栏上方，消息流留「等待审批 N 项…」占位、决议后按
  `resumed` 事件显示已批准/已拒绝；引用卡仍在顶层。
- iPhone：删除隐藏名单机制，启动时移除旧 UserDefaults 键
  `phone.hiddenMacs`（`apple-app/Sources/App/PhoneLink.swift:1397`）；左滑改「取消配对」（仅已配对行）=
  注销推送 + `DELETE /pair/self` + 清本机 mac-key/mac-ticket（`PhoneLink.swift:1706-1715`，
  `MacDirectory.unpair`、`PairingAPI.forgetSelf` 见 `:320-329,610`）。iCloud 仍有的留在列表为「未配对」，
  点按经 `/pair/request`（Mac 点允许）重配；iCloud 条目消失的未配对行移出；merge 不再自动采用 75 旧
  共享密钥（列为未配对，点按走 `/pair/upgrade`），通知点击不触发配对，握手中/当前/请求中的行不被丢，
  空读不丢。

### 16.3 复审与证明

reviewer 对四路各一遍：health 无 Major；macOS UI 3 Major（local 短路成就绪、2 s 轮询永不停、回执按名
FIFO 错贴）；policy 3 Major（math 工具沙箱、API 显式审批被 auto 越过、云咨询被 auto 越过）；iOS 1
Major（75 Mac 不再能从 iCloud 连上——明确接受）。均已修或接受。变异清单：health 8（H-M1…M7+M3b）、
policy 14（P-M1…M14）、iOS 29（`unpair_mutants.py`）、macOS 状态映射 7（`test_native_service_status`）；
结果留给提交说明记录。

### 16.4 残留/已知

- 75 Mac 需升级；`forgetSelf` 失败时 Mac 侧留僵尸设备行（同 77）。
- `upgradeLegacy` 对残留 75 条目的 76+ Mac 无需 Mac 同意（同 77 暴露面）。
- read=auto 下 `web_read` 等出网读工具可被注入链式调用（同 77）。
- `ToolInfo.requires_approval` 在 app API 工具面板不反映旋钮。
- 时长包含人工审批等待。

变异证明结果（verifier，2026-10-27）：后端 22/22 被杀（health H-M1…M7+M3b，policy P-M1…M14；H-M1 实际由
`test_local_retries_do_not_advance_the_backoff` 杀死，清单里点名的两条在该场景下与原码等价）、全量 3463 passed /
3 skipped（定稿树在 pre-commit 第二跑为 3468 passed / 3 skipped）；Swift 侧 iOS 29/29（复件驱动 unpair_mutants.py）、macOS 状态映射 7/7、connState 1/1、回执匹配与步数 2/2 全红；
macOS/iOS Debug 构建通过，原生测试 432 passed，reducer 夹具 16/16。构建与发布记录见 §16.5。

### 16.5 提交与发布记录（2026-10-27）
- 提交 `b1ff204`（62 文件，+3264/−193），pre-commit 首跑 backend 门不过：`test_service_credentials::test_native_credential_mapping`
  用 swiftc 单独编译 `AppSettings.swift`，而它引用了 `Models.swift` 的 `ApprovalPolicy.modes`；改为本地字面量集合后第二跑四门全过
  （1081 s）。教训：`AppSettings.swift` 不得依赖其他源文件的类型。已推送 origin/main。
- macOS：`release-macos.sh` 一次通过，Release `macos-0.16.0-78` 指向 b1ff204，6 个资产（DMG + 78-73/74/75/76/77 delta），
  两仓 `updates` 仅 appcast.xml，feed 列 76/77/78 共 17 条链接；本机已装钉票版。
- iOS：`release-ios.sh` 归档、导出、上传成功，等 ASC 处理后 TestFlight 可见。
- 构建警告（非阻塞，Swift 6 模式）：PhoneLink.swift:797 main-actor isolation、PhoneLink.swift:1814 captured self（upgradeLegacy 的 Task）、
  MacAgentApp.swift:53 captured var。
- 真机待确认：macOS 捆绑形态冷启左下角先「后端启动中」再「计算端就绪」且总时长明显短于 30 s；聊天页任务结束后执行过程自动折叠；
  审批卡贴在输入栏上方；设置「权限」页读写两档可切换并即时生效；iPhone 列表「已配对 / 未配对」标识、左滑「取消配对」后仍列出且可点按重配。

### 16.6 阶段复审（stage-reviewer，2026-10-27，区间 5162fc6..b1ff204）与修复
无 Critical；4 Major 全部成立（M1–M3 经 skeptic 反驳未遂、M4 为事实），4 Minor 已改文档：
- M1 write=auto 让 shell 的密钥隔离失效：`approval_required` 的 auto 分支在 `needs_approval` 之前返回 False，`cat .env.local`
  这类非只读命令既不审批又拿宽松环境（execute 以「只读」代替「已审批」选 env）。修法：shell 工具整体钉死（`approval_pinned = True`，
  变更命令始终询问，只读命令仍按读取档），PROTOCOL §2.11 钉死规则相应补入，设置页脚注改写。
- M2 AgentStore「只在变化时赋值」切断了 PhoneLink 依赖每轮探活发射的离线自愈与旧钥升级重试（探针：离线 40 轮 b77 触发 31 次、b78 1 次）。
  修法：新增每轮必发的 `probeTick`，PhoneLink 改订阅它，UI 去重保留。
- M3 `stillStarting` 含 `engine_ready == false`，未装配过的后端遇算力端 unreachable/unauthorized 或 RAG 鉴权失败时 2 s 轮询永不停。
  修法：节点已定失败或 RAG 有告警时不算 starting。
- M4 变异驱动只在会话 scratchpad：已入库 `backend/tests/run_build78_mutations.py`（22 后端 + 树内 Swift 变异）与
  `apple-app/Tests/run_unpair_mutations.py`（29 个 iOS 复件变异），全跑 32/32、29/29 被杀。
- Minor：全量数字以定稿树 3468 passed 为准；PROTOCOL §2.11 补 PATCH 仅 owner/403；§16.2 回执匹配与引用改正；README 取消配对措辞收窄。
已发出的 build 78 含 M1（捆绑后端，仅 write=auto 时暴露）与 M2/M3 的 app 侧回归：修复过两道门与变异证明后升 build 79 重发。

### 16.7 build 79 提交与发布（2026-10-27）
- 提交 `76c529d`（20 文件，+993/−26）：§16.6 的四项修复 + 变异驱动入库，pre-commit 四门一次全过（1079 s）；修复 diff 过 reviewer
  一遍（又改两处：RAG 临时不可达仍按冷启 2 s 轮询、只有 RAG 鉴权失败算已定；`schedule_delete` / `register_skill` 同样钉死）。
  新变异 P-M15/P-M16、S-k/S-l/S-l2/S-l3、S-m/S-n/S-o 全红（驱动现 41 项）。已推送 origin/main。
- macOS：Release `macos-0.16.0-79` 指向 76c529d，6 个资产（DMG + 79-74/75/76/77/78 delta），两仓 `updates` 仅 appcast，feed 列 77/78/79；
  本机已装钉票版。iOS：归档、导出、上传 ASC 成功，等处理后 TestFlight 可见。build 78 的二进制保留在各自 Release 里，appcast 已不再指向它。
- 教训：阶段复审应在发布前完成；本次 78 发完才审出 M1–M3，当天补发 79。

## 17. 窗口三色按钮 + 「后端启动中」终态（2026-10-29）

### 17.1 问题（用户提出）
- macOS 主窗口顶部没有关闭 / 最小化 / 全屏控件；要求关闭只关窗口、不退出 app，且尺寸与现有设计协调。
- 计算端未启动时打开 macOS app，左下角后端一栏**永远**「后端启动中」；计算端已启动时数秒即「计算端就绪」。用户猜测是计算端判定阻塞了后端判定。

### 17.2 根因（scout，只读）
- 窗口：`RootView` 早已 `.toolbar(.hidden, for: .windowToolbar)`，整条标题栏连同系统三色按钮一起没了；关窗本就不退出（MenuBarExtra 常驻），只是没有按钮可关。
- 启动中：后端**没有**被探测卡住——远端不可达时 5 s 左右就把 `compute_node_state` 写成 `unreachable` 并如实上报。真因是 `engine_ready`
  要等引擎装配完成，而装配必须先连上计算端，计算端起不来它永远 false；app 的 `engineStarting` 只看这一位（PROTOCOL §2.10 原口径如此），
  标签没有终态。另有一条潜在「永远 probing」：远端地址为空 / 端口非法在探测前就抛错，节点状态从未写过。

### 17.3 决策与实现
- 外观：用户在两案矢量图（A 系统原生红黄绿 / B 自绘单色顶栏）中选 A。`WindowGroup.windowStyle(.hiddenTitleBar)`，三色按钮留在左上角原生位置，
  悬停符号 / ⌥缩放 / 全屏退出全由系统包办；侧栏 `sidebarHeader` 顶距 20→40pt（按钮条约 28pt + 12pt），底距 22→20pt，Logo 行尺寸不变。
  `MacAppDelegate.applicationShouldTerminateAfterLastWindowClosed → false` 把「关窗不退出」钉成显式合同。
- 口径（PROTOCOL §2.10 修订）：后端标签在可达、`engine_ready=false` 且故障尚未定格时才是「后端启动中」；定格失败
  （`compute_node_state ∈ {unreachable, unauthorized}` 或 `rag_state=unauthorized`）时回到「后端已连接」，故障由算力端标签 / 检索附注承载，
  判据与 2 s 快轮询退出判据相同；`compute_node_state` 须有终态，装配前失败报 `unreachable`。
- app：`ServiceStatus.settledFailure` 抽出，`engineStarting` 排除之，`stillStarting = probing || engineStarting`（与原式等价）；
  `AgentStore.ragNotice` 只要 engine 未装配就保留（不再绑 engineStarting），新增 `connectedLabel`（「后端已连接（检索服务密钥不匹配）」等）
  供侧栏两处与菜单栏面板共用；菜单栏面板在 connected / engineStarting 时补一行计算端状态。
- 后端：`_assemble_compute` 地址为空 / 端口非法两条分支在抛错前写 `compute_node_state = "unreachable"`；重试、退避、`engine_ready` 不动。
- 测试：`test_native_service_status` 合同 H 工具加 7 例（定格三例不算 starting / 瞬态四例仍算）；`recovery_transport_test.swift` 加
  `auth-engine-blocked`、`auth-engine-rag-key` 两模式；`test_compute_readiness_split` 加地址为空（selection/default）与端口非法三例。

### 17.4 复审与证明（2026-10-29）
- reviewer 首审：1 Major + 4 Minor + 7 Nit。Major 经主会话读码确认成立：`_assemble_compute` 失败分支的 `finish_task` 刻意吞掉取消，
  随后 RuntimeError 被 `_run` 接住继续退避重试，`stop()` 永远等不到（`_install` 的 publish 分支有 `cancelling()` 守卫，这里没有）；
  改动前就有，新用例让「每轮立即失败、间隔 0.01 s」的触发窗口更密，疑为 implementer 遇到的一次 15 分钟 pytest 挂起（verifier 3 次复现未中）。
  修法同 publish 分支：收摊后 `current_task().cancelling()` 则改抛 CancelledError，并补取消用例。
- Minor 采纳：侧栏后端栏 `.connected` 带检索附注时颜色改琥珀（文案带故障不能配绿点）；标题栏让位改为按实际安全区自适应——RootView 用
  GeometryReader 算 `titleBarClearance = max(0, 28 − safeAreaInsets.top)` 下发环境值，侧栏头 `12 + clearance`、聊天头 `clearance`，
  SwiftUI 给不给顶部安全区两种情况 Logo 行都落在按钮条之下；`ConnState` 注释、`connectedLabel` 两分支写法对称；
  `recovery_transport_test` 的 `!contains("启动中")` 空断言改为精确相等；端口非法用例补 selection 参数。
- 主会话第二道门：ServiceStatus / AgentStore / app.py 三处逐行；`stillStarting` 展开后与原式严格等价，`ragNotice` 在 engine 变 true
  或失去认证时清空，后端两条失败分支均在抛错前写终态。
- verifier（串行）：xcodebuild MacAgent-macOS Debug 成功（0 error，6 warning，本次文件仅 MacAgentApp.swift:58 Swift 6 捕获旧警告）；
  8 个测试文件 177 passed；`test_compute_readiness_split` 单独 3 次各 ~3 s。变异 5/5 被杀，sha256 逐字节恢复一致：
  M1 `settledFailure→false` → contract_h_projection + recovery[auth-engine-blocked] + [auth-engine-rag-key] 红；
  M2 去 rag unauthorized 分支 → contract_h_projection + [auth-engine-rag-key] 红；M3 `connectedLabel` 恒「后端已连接」→ [auth-engine-rag-key] 红；
  M4/M5 去掉 app.py 两处 unreachable 写入 → `test_missing_remote_host_reports_unreachable[selection|default]` / `test_invalid_remote_port_reports_unreachable` 红。
- 修补批后最终轮（verifier）：xcodebuild MacAgent-macOS Debug 成功（0 error；本批文件无新警告，MacAgentApp.swift:58 为旧警告）；
  8 个测试文件 182 passed；M6 去掉取消守卫 → `test_stop_during_failed_assembly_cleanup_is_not_swallowed[missing_host|probe_offline]`
  红（`stop() hung` 断言，7 s 内结束不挂），sha256 恢复一致、复跑绿。变异合计 6/6 被杀。

### 17.5 状态（2026-10-29，提交前）
- 真机待确认（无法在本会话替代，app 未运行且不能由会话拉起）：三色按钮位置与侧栏 Logo 行间距（安全区两种情况之一会生效）、
  聊天头控件不被拖拽条压住、红点关窗后进程留在菜单栏且点 Dock 图标能复现窗口、计算端未启动时左下角「后端已连接」+「计算端不可达」
  并在计算端起来后转「计算端就绪」。若 Dock 点击不复现窗口，补 `applicationShouldHandleReopen` 走 openWindow。
- 遗留：一个卡住的 pytest（PID 72580，守卫加入前启动，卡在 stop() 吞取消——即 M6 所修缺陷的活体），主会话 kill 被权限拦下，
  用户已手动结束。

### 17.6 build 80 提交与发布（2026-10-29）
- 提交 `605aaa9`（15 文件：§17.5 列的 14 个 + `apple-app/project.yml` 79→80），sectioned message 记第二道门读法（ServiceStatus /
  AgentStore / app.py 逐行，其余按摘要）与变异 6/6。pre-commit 四门一次全过（backend 3547 / colab 130 / web 252 / apple 16，1127 s）。
  已推送 origin/main。
- macOS：`release-macos.sh` 一次通过，Release `macos-0.16.0-80` 指向 605aaa9，6 个资产（DMG + 80-75/76/77/78/79 delta），appcast 已更新；
  本机 /Applications/MacAgent.app 已重装钉票版（CFBundleVersion 80）。发布说明 `releases/notes-80.md`（gitignored）。
- iOS：`release-ios.sh` 归档、导出、上传 ASC 成功（12:41），`wait-testflight.py 80` 轮询到 VALID，TestFlight 内部组已可见。
- 真机确认项仍是 §17.5 那四条，装好 80 后由用户过一遍。
