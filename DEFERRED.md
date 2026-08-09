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
- **`ask_user(question, options?)` 元工具**：与 `update_plan`/`complete_step` 并列，让"提问"
  **显式化**（不再依赖提前收尾门控的启发式判断），并可携带结构化选项。
- **`awaiting_clarification` 轮次暂停态**：`awaiting_approval` 的兄弟。轮次保持 alive、可查询、
  可断可重连（铁律2 暂停态语义），暂存态经 `/turn` 可查，刷新能重建澄清卡。
- **协议（铁律5：先落夹具再改实现）**：
  - 新事件 `clarification_requested`（payload：question + options）；未知事件仍被忽略（向前兼容）。
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
  任务级一次**的验证 pass，**绝不是并行第二个模型**（并行违反 CHARTER §3 单 worker 串行 +
  翻倍权重/带宽；小模型监工小模型 ROI 也低）。防偷懒另有"提前收尾推回"、防幻觉另有写后验证。

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

### 承重代码指针
`POST/DELETE /api/v1/skills`（剧本管理面）、`POST/DELETE /api/v1/api-tools` +
`.../test`（API 工具表单面）——两端点契约已现成，届时包壳即可。
