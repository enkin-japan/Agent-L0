# 未竟架构账本（DEFERRED）

> **这是什么**：记录**先以轻量版交付、但完整版刻意推迟**的架构。轻量版已在跑、够用；
> 完整版是它的"一等公民"形态，等真实使用暴露出明确需求后再做。
>
> **和 `CHARTER.md` §7 的区别**：§7 是**非目标**（刻意不做、别当 bug 修，如 iOS 后台推送）；
> 本文是**待做**（做了一半的轻量版，完整版有明确设计、等触发条件）。§7 是"不做"，本文是"缓做"。
>
> **怎么用**：每当你把某个能力**先做了轻量版、把完整版留到以后**，就在这里加一条，写清：
> ①轻量版现状（已交付什么、承重代码在哪）②完整版设计（够详细，将来能照着做）
> ③为什么先轻量 ④重启触发条件（什么信号出现就该上完整版）。
> 完整版做掉后，把该条移除或标注"已完成 → 见某 commit"。

---

## 1. 问答对齐层（想法2）

**核心能力已经工作，缺的只是一等公民交互外壳。**

### 轻量版（已交付）
- 依托既有机制，零协议改动、无夹具：
  - 系统提示词的目标对齐条（`memory/session.py` `DEFAULT_SYSTEM_PROMPT` 中"Goal ambiguity"
    那条 bullet）：目标模糊时先调查、再表述打算、**问一个具体问题并给 2-3 个可选项**，
    写改动前对齐；且明示"提问是暂停不是结束，计划+记事板跨轮续接"。
  - 提前收尾门控（`core/orchestrator.py:588-604`）：计划未完成却吐文本时，**主动邀请**
    模型"若确需用户才知道的信息，就问一个具体问题"。
  - `task_state` 跨轮存续（`core/orchestrator.py:631-635`）：计划未完成而输出文本＝向用户
    提问，轮次正常结束但 `task_state` 保留，用户下一条普通消息即带着原计划继续。
- 形态：模型以**普通助手文本**提问，轮次 `success` 结束，用户**普通回复**开启新轮续任务。

### 完整版（未做，择机）——结构化澄清暂停态
- **`ask_user(questions[])` 元工具**：与 `update_plan`/`complete_step` 并列，让"提问"
  **显式化**（不再依赖提前收尾门控的启发式判断）。签名为**批式前沿**（F7，OSS 扫描
  2026-08-28 D.25 顺序 4 改定；原单数 `ask_user(question, options?)` 作废）：一次调用把
  **前置条件已确定的问题全部问完**——每项 `{question, options?, recommendation?}`，
  编号呈现、附推荐答案，**上限 2–3 题**（本地档一轮=冷预填十几秒×单 worker 串行，
  逐题打断的经济账比云端惨得多；但选项墙也是墙——上限 4 会叠乘出最多 12 个选项）。
  反向约束原样保留：Never burn a question on a request with a clear outcome。
- **`awaiting_clarification` 轮次暂停态**：`awaiting_approval` 的兄弟。轮次保持 alive、可查询、
  可断可重连（铁律2 暂停态语义），暂存态经 `/turn` 可查，刷新能重建澄清卡。
- **协议（铁律5：先落夹具再改实现）**：
  - 新事件 `clarification_requested`（payload：questions[]，每项 question + options? +
    recommendation?，与批式签名同形）；未知事件仍被忽略（向前兼容）。
  - 归约器新增一种卡片状态（"待你回答"），web `lib/turn-reducer.ts` + 原生 `Store/TurnReducer.swift`。
  - 恢复路径 `POST /turns/clarification`（带 answer），**照抄 `/turns/approval` 的形状**。
  - 落 `protocol/fixtures/`（事件流 + 期望归约终态），双端回放全绿方为验收。
- **TurnRunner**：新增一种挂起变体（暂存 `pending_clarification`，决议后合并），**复用 approval
  那套挂起/恢复管道**（`core/turn_runner.py`），别新造第二条恢复路。
- **双端 UI**：渲染澄清卡片 + 可点选项；单附着流照旧（切走即断、切回重放对账）。
- **通知**：由生命周期事件驱动"你的 Agent 在等你回答"推送（全局 feed）——对"帮普通用户"
  的北极星价值高（proactive 拉回）。

### 为什么先轻量
核心能力（中途提问 + 跨轮续任务）**已经工作**；完整版纯粹是交互外壳（独立卡片/可点选项/
通知）。先在真实任务里验证轻量够不够——够就不过度造脊柱（"护栏是代码"的克制：不为一个弱模型
已能处理的场景堆机器）。

### 重启触发条件（任一强烈即上完整版）
1. 模型以文本提问时，用户**分不清**"在等我回答"还是"已经答完了"；
2. 多选场景用户**嫌打字麻烦**，明确想要"点一下选项"；
3. 想要"Agent 在等你回答"的**推送通知**把用户拉回。

### 承重代码指针
- 轻量：`memory/session.py`（DEFAULT_SYSTEM_PROMPT 目标对齐条）、`core/orchestrator.py:588-604`
  （提前收尾门控）、`:631-635`（task_state 跨轮）。
- 完整版参照：`core/turn_runner.py` 的 approval 挂起/恢复 + `PROTOCOL.md` approval 事件族 +
  `protocol/fixtures/002-approval-pending`（照其形状造 clarification 族）。

---

## 1.5 定时任务的远程审批（推送批次的刻意搁置项，2026-07-12）

**通知基建已全量上线（APNs 直发 + 互动审批 + Live Activity），唯独无人值守场景的
审批语义保持"自动拒绝"不动——远程限时审批是它的一等公民形态。**

### 轻量版（已交付，随推送批次）
- 交互式轮次的审批已可远程决议：APNs 鉴权通知（time-sensitive + TTL 过期不投）带
  允许/拒绝按钮（允许=authenticationRequired 强制解锁，拒绝免解锁），灵动岛/锁屏
  另有免解锁"拒绝"（LiveActivityIntent）；决议带 `expected_turn_id` 防错批。
- 定时任务撞审批墙时**语义未变**：runner 立即 `approved=False` 决议（无人值守纪律，
  提示词也明示"审批类工具会被自动拒绝"）。推送侧做的是**诚实通报**：
  `approval_autodenied` 类推送（"定时任务遇到需授权操作，已自动拒绝"），
  绝不把已注定拒绝的请求渲染成互动审批卡（那是撒谎 UI）。

### 完整版（未做，择机）——挂起 → 推送 → 限时等待 → 超时拒绝
- `schedule/runner.py` 的 pending_approval 分支改为：emit awaiting_approval 后**不立即
  决议**，改为等待远程决议或超时（如 10 分钟，与推送的 apns-expiration 对齐），超时才
  `approved=False` 续跑。凌晨的定时调研要写一个文件，被窝里 Face ID 一下就放行。
- 随动改造（成套，不可只改一半）：
  - 注入提示词更新（runner.py preamble "will be DENIED automatically" 改为"会推送给
    用户限时决议，超时拒绝"）；
  - 等待期间调度器 worker 的让出语义（挂起等待不能占死单 worker——参照
    awaiting_approval 暂停态的 suspend/release 配对，铁律 3 的不变量清单逐条核对）；
  - `_MAX_AUTO_DENIES` 熔断保留（远程决议也可能连环触发审批）；
  - 推送侧：schedule-origin 的 awaiting_approval 从"通报"升级回互动审批卡
    （dispatcher._route 的 origin 分叉反转）；
  - config：`schedules.remote_approval_wait_minutes`（0=保持现状自动拒绝）。
- **两种审批场景语义要分开**：交互式轮次维持无限等待（awaiting_approval 是暂停态，
  通知只是提醒）；只有 schedule-origin 加 TTL。

### 为什么先轻量
改的是编排语义（runner 决议路径 + 调度器占用），金测试范围；与通知基建混一批会把
"推送坏了"和"定时任务坏了"耦成一个故障面。先让通知层在真机跑稳，再动无人值守语义。

### 重启触发条件（任一即上）
1. 定时任务因审批墙**实际失败/降级**的运行次数变多（runs.jsonl 里 denied_approval 常见）；
2. 用户在真实使用中想"半夜放行一次写文件"而做不到；
3. 通知链路（APNs 送达 + 互动按钮回程）在真机被验证可靠。

### 承重代码指针
- 现状语义：`backend/framework/schedule/runner.py:186-221`（自动拒绝闭环 + `_MAX_AUTO_DENIES`）；
- 推送分叉：`backend/framework/push/dispatcher.py` `_route` 的 awaiting_approval origin 分支；
- 远程决议管道（现成，直接复用）：`POST /turns/approval` + `expected_turn_id` 防错批
  （`core/turn_runner.py accept_approval`）+ PushManager 通知动作处理。

---

## 2. 目的-结果对齐 / 收尾验证（想法3；想法5 的"监工"诉求也落这）

> **✅ 完整版已上马**（2026-07-12）：A/B 实测（4bit，n=10×2 侧）证实触发条件 1——长任务
> 两侧**确定性虚报完成**（读完 8 站日志却交不出任何数值，且自信宣告完成）。实现为
> `orchestrator._goal_alignment_check`：计划≥3步全完成后一次独立审计生成（goal+答复→
> VERDICT PASS/FAIL），FAIL 经 tail_note 推回自纠一次，异常/歧义 fail-open；
> `config.yaml agent.goal_verify` 开关。金测试 +4（test_goal_verify.py）。
> 采用简单 verdict 形态；结构化 checklist 仍属后续可选强化。以下原设计存档备考。

**任务级目标对齐已有轻量提醒；完整版是一次独立的验证生成。**

### 轻量版（已交付）
- 收尾结构提醒扩为**目标引用 + 漂移检查**（`core/orchestrator.py` `_structural_reminder`
  的 all_done 分支）：全部步骤完成、计划 ≥3 步时，提醒模型**读回用户最初的 goal**、确认结果
  真的达成它（不是漂移到相邻的事）+ 别把"已执行"当"已验证"。**确定性触发、零额外生成**
  ——只是最终答复前读到的一条提醒。
- 单步确定性：既有**写后验证门控**（`core/orchestrator.py:606-626`）已覆盖"改了状态却没复查
  就收尾"。**不做单步模型自评**（会让每步推理翻倍——"护栏是代码"原则）。

### 完整版（未做，择机）——任务级独立验证 PASS
- 生成最终答复后，再跑**一次专门的"结果 vs 目标"对齐检查**：喂入原始 goal + 刚产出的答复
  →判定达成/未达成→未达成则自纠续跑。可给**结构化 checklist**（每个关键 deliverable 对一条
  目标要求，逐条核对）。
- 成本：**每个多步任务末尾一次额外生成**（不是每步——每步会翻倍推理，明确避开）。
- **想法5（监工）的诉求就落在这里**：想要"防偷懒/防幻觉的复核"，正确形态是这个——**串行、
  任务级一次**的验证 pass，**绝不是并行第二个模型**（多实例并跑违反 CHARTER §3 单引擎批内并发的
  单实例铁律 + 翻倍权重/带宽；小模型监工小模型 ROI 也低）。防偷懒另有"提前收尾推回"、防幻觉另有写后验证。

### 为什么先轻量
提醒式（模型读到后自查，零额外 pass）多半够用；独立验证 pass 要花一次生成。先在真机看
漂移/虚报是否真频繁，再决定值不值这次生成。

### 重启触发条件（任一强烈即上完整版）
1. 模型读了收尾提醒**仍频繁漂移**或**虚报完成**（说"做好了"其实没达成目标）；
2. 长任务收尾质量不稳，需要**强制复核**才放心。

### 承重代码指针
`core/orchestrator.py` `_structural_reminder`（all_done 分支，目标对齐提醒）+ 写后验证门控
（`:606-626`）+ 提前收尾门控（`:588-604`，防偷懒）。

## 3. 依赖漂移巡检的受限自愈（phase 2，2026-07-18 用户拍板「第二阶段记一笔」）

### 轻量版（已交付，phase 1「只报不修」）
- `framework/maintenance/drift_patrol.py`：空闲维护回合逐项轮巡声明了 `canary()` 的工具
  （`mcp/base.py` 的 `drift_class`/`drift_fix`/`canary` 三声明），连续失败达阈值写
  `drift-*` 告警笔记——记忆索引每轮注入提示词，agent 下次对话自然告知用户并给修复指引。
- 已声明探针：media_download（yt-dlp probe 恒存视频）、web_research（SearXNG 原始层
  数响应引擎+失联名单）、web_read（T1 curl_cffi 指纹实抓 + T2 真启系统 Chrome）。
- 旋钮 `agent.drift_patrol`（config.yaml）；金测试 `tests/test_drift_patrol.py`。

### 完整版（未做）——白名单受限自愈
- canary 失败 → 仅对 A 类（adversarial）白名单自动升级（`pip install -U yt-dlp` /
  searxng git pull 到新 pin）→ 复跑 canary + 相关金测试子集 → 过则记账并更新 pin，
  不过则回滚旧版本并告警。frozen 类永不自动碰（升级本身是它们唯一的风险源）。
- 需要先解决：升级的原子回滚路径（pip 装回旧版）、金测试子集的选取契约、
  bundle 形态下的 venv 写权限（app 的签名树内不可写，须升级在数据根侧的运行时）。

### 为什么先轻量
自动升级本身可能引入新断裂（yt-dlp 偶有 breaking release）；先让 phase 1 跑出真实失效
样本，看告警频率与误报率，再评估自愈的 ROI。零越权也符合记忆治理波「标记权不改写权」哲学。

### 重启触发条件
1. phase 1 告警实际发生且用户手动修复步骤被证明机械可复制（≥2 次同样的修复）；
2. 或告警频率高到手动跟进成为负担。

---

## 4. Agent 代办注册：工具/剧本的对话式添加（2026-07-19，随剧本管理面波次记录）

**工具/剧本的注册管线已经打通到"填表单即生效"，缺的只是让 Agent 自己在对话里发起注册。**

### 轻量版（已交付）
- 工具与剧本都已是声明式管线：API 工具工厂 + 表单面（`POST/DELETE /api/v1/api-tools`）、
  剧本管理面（`POST/DELETE /api/v1/skills`）均为"设置页填表单 → 保存即注册/即时重建"，
  人工始终在回路，把关声明合法性与密钥录入。

### 完整版（未做，择机）——模型对话式发起注册
- 把 `POST /api/v1/skills`、`POST /api/v1/api-tools` 包成工具挂进主循环，让 Agent 能在
  对话中直接写一份新工具/剧本声明并调用注册端点——端点契约现成，包壳成本低。
- **两条设计红线（届时必须守住）**：
  ① **密钥绝不走对话通道**——会落进 session 日志/prompt，密钥录入必须永远留在设置 UI
  的 0600 文件里，Agent 只能引用变量名，不能替用户填值；
  ② **模型自注册是权限自举回路**——Agent 创建的声明须带"待用户确认"标记，不可静默
  生效（现有变更类工具默认 `requires_approval` 已兜住大半场景，但这条仍需显式核对，
  不能被声明自身的字段悄悄绕开）。

### 为什么先轻量
e4b 写合法声明 + 高质量 description + 自主测试闭环当前不可靠（**D1 纪律**：智力侧问题
等生产设备）。表单已把门槛压到"填字段"，人工在回路的成本本就不高。

### 重启触发条件
生产设备（31B 模型）到位——与 D1 其余观察项同一口径：模型智力侧的能力项，等强模型
上线后再评估是否值得做对话式自动化。

**31B 能力评估已完成（2026-08-09 #77 真机）**：让 31B 起草天气 API 工具声明——
schema 干净、给模型看的 description/使用说明质量高、**密钥自发只引环境变量名不填值**
（红线①意识正确）；字段名与工厂实际 schema 有出入（自创 implementation/auth_strategy），
但真 schema 入 prompt 后可对齐。**能力侧已达标，建否=产品决策**（轻量版表单是否够用
vs 对话式便利，守红线②"声明须带待确认标记"）。

**已拍板：建（2026-08-09 用户，账本 D.18）**——两条红线照旧，待确认状态机+
设置页审批队列为主要工程量；任务单在册。

**后端已建成（2026-08-09，44b569c）**：`register_api_tool`/`register_skill` 工具
（参数面**没有**密钥字段=红线①物理执行；形似密钥实值提交口拒绝）→ 声明落
`data_dir()/registrations/` 待确认区（不进生效目录不进装配=红线②）→ 设置页
API `/api/v1/registrations`（列表/确认/驳回；确认复用表单面同一条保存/上架路，
模型无确认工具）。金测试 732。app 侧审批队列面板+模型起草质量真机验收随后。

### 承重代码指针
`POST/DELETE /api/v1/skills`（剧本管理面）、`POST/DELETE /api/v1/api-tools` +
`.../test`（API 工具表单面）——两端点契约已现成，届时包壳即可。

## 5. #85 执行层实例身份与密钥观测（2026-08-10 401 事故根治清单）

**事故背景（当日档案，勘误后版本）**：install-macos.sh（日用快装，纯客户端）与
bundle-macos.sh（全栈包）覆盖同一个 /Applications/MacAgent.app，快装产物缺
Resources/backend → `SidecarManager.resolveLayout()` bundle 判据失败**静默退 repo
布局**，dev 桥接注 `.env.local` 的 AGENT_API_KEY，而 app 客户端恒发钥匙串值——
构造性 401，且退出重开/重新生成/开关关开全部原样复现（每条路都重走 repo 布局）。
交接文档 HANDOFF-20260810.md §1 的「孤儿收养」叙事系误诊（external 态下重新生成钮
不渲染、状态文案是「外部后端在跑」，与实况「运行中」互斥）。

### 轻量版（已交付，2026-08-10）
- bundle-macos.sh 组装期**执行层完整性断言**（backend/app.py、python3.13、
  lib_edge.sh、config.yaml 缺一即组装失败）——同款事故在打包侧不再可能静默产生。
- 既有：reap 第二判据（解释器路径认自家兵，v0.16.0(51)）、timeout_graceful_shutdown=5。

### 完整版（1/2/5/6 已落地 2026-08-10 晚；余 3/4 随波次）
1. ✅ **探活带鉴权**（2026-08-10）：/health 报实例指纹（`service`/`layout`/`key_fp`=
   密钥 sha256 前 8 位，明文永不出现）；`probeHealth()` 四态（down/foreign/
   keyMismatch/healthy），收养前认身份比指纹，钥匙不对不收养、当场亮红说清。
   金测试 +9（test_auth.py）。
2. ✅ **布局形态显性化**（2026-08-10）：resolveLayout 落成 `layoutKind`/`isBareBundle`
   published 属性，设置页亮「布局」行（bundle=钥匙串 / repo=dev 桥接 .env.local，
   对端自报形态不一致时并列显示）+ 裸包琥珀警示。
3. **external 态重启**给明示/接管选项（现状 restart() 直接 break，SidecarManager.swift:112）。
4. **handleCrash 改走 autostart(force)** 重读钥匙串（现状用启动时 env 快照复 spawn）。
5. ✅ **regen-from-failed 收养缺口**：随 1 一并堵死（收养前验钥）。
6. ✅ `devctl.sh keygen --rotate`（2026-08-10，bce0980）；devctl 侧探活验钥可复用
   /health 指纹（`AGENT_LAYOUT=repo` 已自报），暂无独立工程量。

### 重启触发条件（余项 3/4）
external 态用户真的需要「接管重启」时上 3；再出现「崩溃自愈后密钥失效」实案时上 4。

---

## 6. Computer Use：GUI 任务子回路（2026-10-30 用户决定「方案含 P0 探针落档，先不执行」）

**完整设计**：`docs/plans/2026-10-30-computer-use-design.md`（含外部方案逐条取舍、
事实核对表、P0 探针详细设计 §11.1、恢复前置条件 §11.2）。本条只记账本四要素。

### 轻量版（已交付，承重代码）
- AX 六工具：`backend/framework/mcp/tools/accessibility.py`（`list_applications / read_ui_tree /
  click_ui_element / set_ui_element_value / type_text / press_keys`，pyobjc + CGEvent，树上限
  深 18 / 节点 400）。
- `run_applescript` 恒审批（`apple_script.py:37`）、`open_path`（`desktop.py`）；系统提示词
  已定「终端 → AppleScript → GUI」优先级（`memory/session.py:125`）。
- 机械压缩器保「读树→点击→再读树」（`memory/compactor.py`）。

### 完整版（设计见计划文档）
- 短命视觉子会话承载 `ui_task` 子回路（图不进主会话；先例 `ppt/deck_critique.py:146`）。
- 新工具 `capture_window / zoom / wait_for / ui_actions`（动作批 + `observe_after`，批内中止）。
- 审批分级进现有 read/write 框架：四个 AX 写工具显式 `effect="write"`；`ui_actions`
  钉死 + 批内高危判定；`approval-policy` 加 `ui_apps_allow/deny`；安全输入检测；急停热键
  → `POST /api/v1/chat/{sid}/task/cancel`。
- 引擎放开「只追加」图会话的 KV 前缀复用（现状 `core/engine.py:440,482` 含图即全量）。

### 为什么先轻量 / 搁置
- 现有「终端 + AppleScript + AX」已覆盖日常；截图型 CU 的收益取决于三件未验之事：
  打包形态下屏幕录制的 TCC 归责、Qwen3.8 在 mlx-vlm 下的坐标约定、含图 prefill 代价。
- 单 GPU 机器上探针 B/C 要切离 gemma 生产档，需用户在场放行；用户决定先不投入。

### 重启触发条件
- 用户点名重启；或日常任务反复卡在「必须点界面」且 AX 六工具失败率明显。
- 恢复步骤按计划文档 §11.2：GPU 空窗、Qwen3.8-27B-8bit 已就位、用户在场；先复核官方是否
  已公布 GUI 坐标约定，再决定探针 B 的规模。

### 已知缺口（即使不做完整版也该补的两处，列于此备查）
- 四个 AX 写工具未声明 `effect`，默认策略下点击 / 键入不审批（`registry.py:201-202`）。
- `read_ui_tree` 未排除 `AXSecureTextField` 的值（grep 0 命中）。

## 7. 本机 MLX + BPE 方言（qwen）：流式去 token 器延迟吐字，复读护栏拦不住中途
- **现象**（2026-10-30 一审发现，未实测）：`backends/mlx/backend.py` 对 BPE 模型给的 `delta = result.text`，
  而 mlx_vlm `BPEStreamingDetokenizer` 只在下一 token 以空格开头时 flush（`tokenizer_utils.py:234`）。
  无空格的纯中文产出要到末块才到 `VisibleTextStreamer` / `RepetitionDetector`，护栏只能在生成自然结束后
  命中：产出照样丢弃并推回，但算力一点没省。
- **影响面**：仅本机 MLX 跑 qwen35 系（注册表里的 Qwen3.8-27B 条目）。现生产远端算力（Colab vLLM）逐 token
  推 SSE 不受影响；本机 gemma4 是 SentencePiece 去 token 器，也不受影响。
- **修法候选**：MLX 后端对 BPE 模型按「已解码出完整 UTF-8 且不以不完整字节结尾」flush，或在 engine 侧用
  `generation_tokens` 步进为检测器补一条「字节级」喂入。两者都要对表 computer use 方案里的 qwen 探针一起做。

## 8. 中途正文：同一次生成里「正文 + 工具调用」的正文不入历史（直播有、落盘无）
- **现象**（build 83 一审）：模型在一次生成里先写「让我查一下…」再发工具调用时，正文经 `token_delta` 直播出来、
  直播态按 3b′ 切成「中途说明」；但编排层工具分支只 `add_assistant_message(tool_calls=…)`，正文不入 history，
  turn_ended 后权威消息替换预览，这段说明消失；导出同样没有。护栏推回型（纯文本消息其后再有工具步）才会在
  落盘轮次里出现 `.note`。
- **为什么没顺手修**：补进 history 会改变模型可见上下文（方言拼装要支持 content+tool_calls 同在一条 assistant
  消息）并牵动大量以 history 形状为断言的金测试；需要单独一波，含 KV 前缀影响评估。
- **同波记下的小项**：取消轮回执按首匹配认行（同名同内容两条返回行时可能错认）；/events 单端点持续 4xx 且无
  活跃轮 → 1 s 级重装历史循环（需代理级异常才触发）；`ConversationExport` 把中途说明压成单行；直播窗
  `suffix(6)` 把说明行也计入。
