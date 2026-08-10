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
> - `EXECUTION_LEDGER_ARCHIVE.md` = 已收官历史细节（R0–R5/D 阶段/B2c/七月各波，
>   2026-08-08 瘦身波逐字迁出）。当前活跃线之外要翻旧账去那里。
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
> 🟢 **2026-07-16～18：R0–R5 全线收官**——方言层/子 Agent 执行器/Skill 体系/渲染格/
> 文件式长期记忆/macOS app 收编（1.0G bundle、Developer ID 签名+公证 Accepted、
> iOS TestFlight 双端分发，v0.16.0(53)）。**D 阶段**：D1 剧本 ×5 全实测、D2 桌面工具
> 46 个两批验收、工程票（注册表热重扫/sidecar 孤儿 reap）、UI 打磨五批、媒体抓取、
> 文件面板、**B2c RAG 数据/计算拆分两阶段切换完成**。Q5–Q9 全定（D.13/D.14/D.16 等）。
> 逐步细节（实测结果/决策现场/代码位置）全部在 `EXECUTION_LEDGER_ARCHIVE.md`。
> 🟢 **2026-07-31：混合架构宪章修正波 ✅**——北极星改"数据流向精确可控 + 用户判断
> 独立性（反算法绑架）"，新增铁律 6（云端只顾问不执行 / 出网决策与执行分权）；
> **仓库已迁 `enkin-japan/Agent-L0`（origin），原仓库归档（archive，封存点 11d8cd8）**。
> 混合架构落地顺序见 CHARTER §7 分期纪律：任务路由器 → 云端推理后端 → L0 出网守卫。
> 🟢 **2026-07-31：任务路由器波 ✅**——model_router（确定性选端点：能力过滤+实测画像
> main 粘滞 margin 0.05）+ engine_pool（端点懒装配，env 不传专科）+ excel 视觉回读
> 首个消费方 + config 三模型 profile 回填；实机冒烟全通，金测试 622。下一站=云端推理后端。
> 🟢 **2026-07-31：云端顾问波 ✅（真 key 联调待用户 .env.local 配 CLOUD_*_API_KEY）**——
> framework/cloud/advisor（双供应商适配 openai_compat+anthropic、出网台账+日预算护栏、
> 内联密钥拒启）+ cloud_consult 工具（requires_approval 恒开=L0 前的人工脱敏闸，无 key
> 不注册），铁律 6 代码形状（解析面 banned 钉板），金测试 630。下一站=L0 出网守卫。
> 🟢 **2026-07-31：L0 出网守卫波 ✅**——egress_guard 双腿脱敏（规则腿确定性+模型腿独立
> 上下文，抗注入=只认逐字在场串，本地回填）+ 审批旋钮 + l0_eval 评测集三模型基线
> （e4b 0.60 最佳/零过遮/9B 档不足以放宽人工闸——诚实结论，require_approval 恒 true），
> 金测试 643。**混合架构三站全落地**；真 key 联调+场景标定+L0 大档重测=生产机清单。
> 🟢 **2026-08-05：OfficeCLI·Word 生成波 ✅（三波之 1）**——第三方 OfficeCLI（brew 钉版
> 1.0.143，禁更新/禁常驻）填 docx 生成空白：md→batch 纯函数 + 三重门禁（batch 原子/
> validate/markitdown 独立读回交叉校验）+ write_document 工具（模型面只见 markdown）。
> 9B 真管线实测一发命中，架构侧零回滚项，金测试 655。下一波=deck→pptx。
> 🟢 **2026-08-05：OfficeCLI·deck→pptx 交付波 ✅（三波之 2）**——pptx_export 第二投影
> （栅格→英寸/tokens 派生字风/chart 嵌真 PNG/占位诚实/icon 如实跳过），分工定死：
> PDF=预览真身、pptx=可编辑近似（写进工具描述）；9B 实测三页全对自主导出，
> 金测试 664。下一波=Excel 底盘基准。
> 🟢 **2026-08-05：OfficeCLI·Excel 底盘实测波 ✅（三波之 3·收口）**——35 题手写真值
> 基准：OC 35/35（0.3s）vs LO 33/35（1.7s），但 LO 两败根因=openpyxl 裸写新函数名
> 缺 `_xlfn.`（真 Excel 同样 #NAME?，LO 忠实还原）——**门禁保持 LO 不换**（忠实性+
> 独立性>速度），OC 引擎合格用途记生产机清单。三波总结：代替不成立、优化两处落地
> （Word/pptx 新能力）、一处实测否决——零回滚，金测试 666。
> 🟢 **2026-08-06～08：Colab CUDA 兼容波 + 大档评测波七站收官**——CUDA 后端实战
> 定型（26B/31B/Ornith 三家族、契约 19/19、edge 跨 tailnet 真轮次）；模型选型格局
> （31B 质量王 + MTP k=4 **3.12×** 翻身可用速度；26B MoE 1.92×；Ornith 投机负收益）；
> RAG 定档 vl-2b（MRR 0.969，本机已重灌）；路由 E2E 全通；M8 约束稳态零税；
> H 固定开销诊断收敛（CUDA graphs 已是最大杠杆，余量在 kernel 属生产原生核）；
> Xet 大内存解禁 357MB/s；CLI 孤儿 VM 自救破解。金测试 676。详见 §3 各站记录。
> 🟢 **2026-08-08：文档瘦身波 ✅ + 26B 12h 长稳站 ✅ PASS**——账本 3141→~680 行
> （历史逐字迁 `EXECUTION_LEDGER_ARCHIVE.md`）、六文档漂移修缮、三 README 代理
> 核查修缮、剔一份已被 PROTOCOL 取代的草案；12h 长稳二跑满程 12.2h **313 请求
> 零错误零泄漏零漂移**（一跑 1h40m 遭 Colab runtime 回收，三重加固后二跑通关；
> 详见 §3 站记录），26B 生产形态获长时常驻资格。金测试 676。
> 🟢 **2026-08-09：余量榨汁波 · 本机批 + 站 A ✅**——文档全量清点（Explore 代理，
> 20 条 A100 可解锁项菜单）后开工。**本机批**（金测试 676→706）：CUDA thinking
> 预算两段式落地（A1 最难点）+ ASR faster-whisper CUDA 腿（A3 收官）+ OfficeCLI
> `_xlfn` 前缀解锁 + L0 account 补题 ×5 + RAG 阈值扫参工具。**站 A（生产债，40G
> CLI）四项**：① thinking 预算真机验收——超预算强闭+段二 APC 续跑 ✓、约束×预算
> 共存 ✓；② **RAG vl-2b 三阈值标定**——rerank 0.5→0.3（继承自 0.6b 的占位值对
> vl-2b 太狠），全集 0.941→1.0/MRR→1.0，**且 multidoc 归零悬案一并破解**（纯 rerank
> 地板所致）；③ ASR faster-whisper 真机转写准确 ✓；④ L0 account 补题重测——26B
> 仅 2/7，坐实 account 是 L0 残余短板、放宽人工闸不成立。运维：kernel 第三次猝死
> （exec 404）经 ssh nohup 续跑+还原备份 stop 化解，"kernel 死≠VM 死"三度验证。

> 🟢 **2026-08-09：余量榨汁波 · 站 B（31B 智力验收，80G 浏览器）✅**——CLI 给不了
> 80G（`--gpu A100` 只 40G-s SKU、也 attach 不了浏览器 runtime），走浏览器 cell 接
> tailnet ssh 驱动（代码 clone 最新 9864131 覆盖 cell 解包的旧档）。**锚点**：31B
> 生产形态 fp8+MTP k=4 装 80G（权重 32.57G，叠 bf16 加载峰值+KV 在 40G 会爆=80G
> 硬需求实证）、契约 19/19、MTP **~2.93×**（126 vs 43 tps，第7站 3.12× 当前代码
> 复confirm）。**回归**：model_eval 31B tool_call 0.96／longchain 0.833 与第7站逐项
> 一致=站 A 的 cuda thinking 两段式改动在 31B 生产质量模型**零回归**（约束解码下
> 思考立即闭合=54 turn 思考空属一贯行为非回归；顺带轻验两段式 phase1 空→phase2
> 不损输出）。**诊断双闭**：① qwen35 KV前缀 0.0 **推翻账本原假设**——gemma4/qwen35
> 两 build 布局代码实证逐字节同构（工具进稳定前缀、task/env 置尾），真因是架构级
> （Qwen3.5-MoE 混合线性注意力 3:1，vLLM APC 只缓存全注意力 KV 块、线性层递归态
> 天然不可前缀缓存），无优化杠杆、qwen35 全线非生产选型归档；② 双频道全空=raw
> 生成本身为空（response_parser 逻辑健全），归因 ChatML thinking 头预填×L0 一次性
> 替身停止符接缝，qwen35 非生产、复现低 ROI 归档。**harness 漂移发现**：
> attention/driver.py 打已移除的 `/api/v1/chat/{sid}/approval`，且当前审批为异步
> 事件制（submit_approval 返 202、终态经 GET /events），driver 同步"审批即返终态"
> 循环**架构级不兼容**→ 31B 编排层 attention 验收（RESULTS.md 点名的模型层搁置项）
> 阻塞于 driver 现代化，记**免-GPU 后续**（开发机可做）。运维新知：tailnet 主机名
> `colab-a100` 跨天复用、新 VM 新 SSH host key、本地 known_hosts 钉旧钥致
> "HOST KEY CHANGED" 拒连（隧道本通）——`ssh-keygen -R colab-a100` 清钥即通。
> **降级记后续**：attention n≥10（阻塞于 driver 现代化）／R2b skill 自发现／DEFERRED§4
> 对话式工具注册（需起 app 走当前 chat API 驱动）／31B vision 补 5 题（需 author
> 过鉴定题；profile 已有 vision 15/15=1.0、hard 0.838）／Ornith NVFP4 换件（可选）。

> 🟢 **2026-08-09：余量榨汁波 · 站 C Phase 1（评审回炉 feature，免-GPU）✅**——PPT
> 质量杠杆落地（ppt-production-todo 任务 B，e4b 时代因多模态评审无意义而搁置，31B/大档
> 到位后开工）。新增 `framework/ppt/deck_critique.py`：Playwright 逐页截 `.deck-print-page`
> （复用 pdf_export 宿主选择+等信标纪律）→ 每页一次性视觉会话（仿 excel `_visual_qa`：
> EphemeralSession 喂截图+当前规格、model_router 路由 vision 端点、fail-soft 回落主引擎）
> → 结构化 `submit_review`（pass / revise+修订槽位）→ `deck_ops.set_slide` 重展开回写。
> 关键地基改动：`set_slide` 持久化**源槽位** `slide["source"]`（expand_slide 只留展开块、
> 原始 layout+内容槽丢失，评审据此保真重展开不失 chart 真实数字；旧 deck 无 source 则
> 只报不改优雅降级）。MCP 工具 `deck_critique`（异步包壳仿 math_research 的
> run_coroutine_threadsafe 投递 + timeout_seconds 覆盖全局 180s）、config `ppt.critique` 段。
> 安全：只读 deck+写本会话 assets/deck 无外发（免审批同 deck_export）、锁页 set_slide
> 拒改天然守住、EphemeralSession inject_env_context=False 隐私不进。测试 chore-worker
> 补 7 例（tests/test_deck_critique.py：纯函数分支 + 主流程 pass/revise 回写/锁页跳过/
> 无source 只报/无deck，真 deck_ops 读写不 mock 回写），**金测试 706→713 全绿**。主会话
> 审 diff 通过 + 自查修 1 bug（_pick_print_host 需顶层 ppt.frontend_base_url 非 critique 子段）。
> **Phase 2 待办（需 40G + 本机 app 集成）**：26B/31B vision 端到端实测（本机 app 起服连
> Colab 推理 + 系统 Chrome 截图，验真机识别"标题截断/太挤/对比差"并正确回写）+ ChartQA
> 视觉扩容（yes/no 大样本 + 加难批余 ~186 候选续采，双模型共同失败作坏题探测器）。

> 🟡 **2026-08-09：站 C Phase 2a（评审回炉本机验证，e4b inprocess，零 unit）——机制验通+修 1 bug，
> 质量档卡本机内存**。用户授权后本机起 dev 栈（临时 config.mode=inprocess，验完还原）
> 用默认 e4b（MLX 缓存、有 vision）端到端验证：造缺陷 deck（超长标题+10 挤 bullets）→
> chat 触发 e4b 调 deck_critique → **管线全通**：Playwright+系统 Chrome 截 `.deck-print-page`
> 出 1280×720 真图（page_1 84KB 挤页/page_2 15KB 净页）、工具执行、print 路由供数、digest
> 良构、fail-soft 生效。**真机抓到 1 个单测抓不到的 bug**（测试 mock 了 _route_vision）：
> `_route_vision` 用了不存在的路由 kind `'deck_critique'`（词汇表仅 chat/longchain/summary/
> tool_use/**vision_qa**）→ 已修为 `vision_qa`（excel 同款）。e4b 对明显缺陷页判 pass=视觉
> 太弱抓不到（正是当初推迟到 31b 的原因，非管线 bug）。**质量档验证受阻**：本机切
> 26b-a4b-4bit MLX 触发 **Metal OOM**（需 14.6GB > GPU 推荐上限 12GB，vision 生成时爆）——
> 这台 Mac 只装得下 e4b/e2b，26b/31b 强模型 vision 质量验证需**更大内存机器或 Colab-hetero**
> （Mac edge 前端+backend 走 remote 连 Colab 40G 26B 推理）。运维发现：本机 `.model-selection`
> 钉着 `google/gemma-4-31B-it`（CUDA bf16 模型）——本机 inprocess MLX 跑不了且触发 62GB 下载
> hang，已按原样还原但需用户注意（清掉或换 MLX 档）。金测试 713 保持。

> 🟢 **2026-08-09：站 C Phase 2b（评审回炉质量档验证收官，Colab-hetero 全流程首证）✅**——
> Mac edge（devctl 全栈，config 零改动：mode=remote 默认 + `.env.local` 拨 colab-a100）
> 连 CLI 40G 26B 生产形态（fp8+MTP k=4，全链引导→拉取→起服→契约 19/19 仅 ~14min）。
> 缺陷 deck 三页（超长标题+10 挤 bullets / 干净对照 / 缺陷+锁定）验收全过：26B 判
> revise（overcrowding+imbalance）、修订槽位回写保真（id 稳定、锁页/对照页字节原样）、
> **revise→复查 pass 单轮收敛**（修订页渲染实证：标题删括号长尾两行放齐、bullets 10→4
> 无裁切）。**真机揪出第 2 个单测抓不到的架构 bug**：一次性沙箱会话没有主循环的工具
> 调用惯性，26B 把 submit_review JSON 当正文/```json 围栏吐 → parse_response 无
> tool_call → fail-soft 静默判 pass（首轮三页全 pass 假象；直问模式 26B 把截断/拥挤/
> 底部溢出碎片逐条点名，证伪"模型锅"）。约束采样只管进块后格式、管不了"进不进块"；
> 修复=tool_call 路径保留 + `_parse_text_review` 正文 JSON 兜底（verdict 校验，excel
> `_visual_qa` 自由文本先例的结构化版）+ 兜底路径留痕日志，单测 +3，**金测试 716**。
> 远程图片通路实证：`_ensure_media` 上传→服务端 media 落盘→generate 消费（media 目录
> 哈希与本地截图字节一致；锁页正确不上传）。运维：sessions.json 开机即备份；本机
> `.model-selection` 异常值已清（回落 config 默认 e4b）。

> 🟢 **2026-08-09：站 C 收官——ChartQA 视觉扩容 26B 侧（同一 40G 会话顺跑）✅，
> 全站仅 ~4u（预算 30u）**。采样脚本入库（`items/vision/assets/chartqa_expand_sampler.py`，
> Colab 侧确定性重生成、候选图不入 git）：①**yes/no 全量批**——test split 单答案
> yes/no 全量仅 104 题（首批"约 1/3"系对格式候选而言），26B **0.760**（蒙对基线 0.5；
> 失败抽查 6/6 真答错、零判分伪影，判分 `(?i)` 词边界防大小写）；②**加难续采**——
> 程序化规则复刻、行号>617 排除已采/已剔 → 193 候选，26B **0.674**（候选态含坏题，
> 低于定稿批 0.811 属预期）。**63 失败题池 + 逐题 transcripts 存
> `results/stationc-expand-26b/`，待 #77 的 80G 场 31B 同跑后按"双模型共同失败裁决"
> 定稿入 gold 题库**（VETTING 复审纪律的既定流程）。注册表 26B note 已回填。评测
> 全程仅 ~8min（gen 95tps 均值，297 题）。GPU 即停即清（会话 03c1c0 全程 ~45min）。
> 站 C 三阶段合计：评审回炉 feature+质量档验证收官+视觉扩容 26B 侧，金测试 716。

> 🟢 **2026-08-09：#77 收官（80G 浏览器场 ~2h ≈13u）——站 B 降级项+扩容裁决全清**。
> ① **ChartQA 裁决定稿**（9416b63）：31B 同跑 297 题→修空格千分位判分伪影（+9）→
> 双败 33 剔 12、160 题标签实证入 gold；定稿批 26B 0.838 / 31B 0.938（加难档区分度
> 2.7→10 个点），合并批画像回填 0.832/0.919；yn 全量聚合 26B 0.760 / 31B 0.837。
> ② **31B 编排层 attention 验收（n=10×6，新 driver 事件账本制全程零 driver_error）**：
> 产物 59/60（E 唯一"败"是澄清措辞无问号的判分启发式误判，行为 10/10 合规）；
> **F_ini_ports 复读吸引子 6/10 轮**（edit_file 重发风暴、dup 拦 30-61 次/轮）但
> 护栏全拦、产物 10/10 正确——护栏收益最硬实证；模型层复读毛病 D1 记档。
> ③ **PPT 三毛病 31b 判定**：②槽位多余字段、③语言漂移消失；①不先 update_plan 残留
> （产物 8/8 良构危害降级）；**新架构发现**：8 页 deck 单轮 ~32.9k token 撞服务端
> max_model_len=32768，生产起服需调大或轮内压缩（记生产调参项）。④ **R2b 口测**：
> 口头点名正确触发 skill_invoke ✓；未点名时守"不自主猜用"工具禁令 ✓（且手工路线
> 自发复刻了 chart-report 剧本步骤——自主发现的能力证据在、门是架构设计关的，
> 开不开=D.9 后续决策）。⑤ **DEFERRED§4 评估**：31B 起草工具声明达标（密钥自发只
> 引变量名），建完整版与否=产品决策待拍板。⑥ 31B vision 补 5 题一项被 hard2 扩容
> 160 题实质覆盖，关闭；Ornith NVFP4 可选项未做（预算考虑）。GPU 已清（0 MiB，
> 用户侧看门自动止损）。预算累计 ~307u/600。

> 🟢 **2026-08-09：L4 廉价档开辟 + 日用一键起停套件入库（~1.5h ≈2.3u）**。预算
> 台账修正：实际余 **386.56u**（用户查账为准）。**26B NVFP4（RedHatAI 件，第 6 站
> 同件）L4 承载实测**：装载顺（32768 顶格无 OOM，16GB 件 Xet 61s 拉完）、tool_call
> **0.96**/longchain **1.0** 零损、vision 0.764（易批 0.90/seed 1.0；**hard 档
> -8pp**=0.756 vs fp8 0.838——扩容题库把第 6 站"-2 题"钉成精确刻度，MoE 抗量化
> 弱的落点即 vision 多步）、gen ~50tps（每 unit 吞吐 32.3 vs A100 fp8 29.6 打平，
> 空闲日用档碾压）。**FP8 KV 孤儿项归位**（第 6 站 A100=SM80 被拒，L4=SM89 通）：
> tool_call 0.96 逐项同分零漂移 + gen **59.8tps（+20%）**——L4 日用档缺省开启。
> **日用套件**（colab/daily.sh + daily_serve_chain.sh + tunnel_keepalive.py +
> mtp_patch.py 收编入库）：start/stop/status/rescue 四命令，白天开晚上关；防线=
> sessions.json 开机即备份孤儿自救／VM 监理循环崩溃自动重启 ≤5／GPU 保活 0.5G 防
> 判零／隧道保活替身（长稳站 404 连坐教训）／哨兵健康轮询+macOS 通知+20min 指标
> 回拉／30min 停滞判死／start 自清 ssh 旧钥。L4 档=NVFP4+FP8 KV（视觉关键活
> `start a100`=26B fp8+MTP 生产形态）。386u ÷（L4 12h/天 ≈18.5u）≈ **20 天日用**。

> 🟢 **2026-08-09：R2b 全开 + 并发全链落地（免 GPU，金测试 716→719）**。
> ① **R2b 全开**（D.17）：skill_invoke 撤「只认点名」禁令，工具描述**装配期内嵌剧本
> 索引**（name+description；两表互为前置——app.py 在剧本表就绪后 load_tool 重注册时
> __init__ 拿到真索引），任务明确匹配即自主调用；回退开关=恢复旧描述（git 可查）。
> ② **并发四件套**：生成并发旋钮 agent.max_inference_concurrency 1→**4**（remote 才
> 生效，服务端 gen_sem=8 承载）；评审回炉**逐页 gather**（回写仍按页序）；excel
> visual QA **逐页 gather**（findings 按页序稳定）；**调度器多 worker 化**——
> TaskScheduler(max_workers) 参数化（_running 单值→集合、容量内循环准入、满员抢占
> 受害者=后台类优先、次取 priority 最低+最晚开跑），app 装配 worker 数与 B1 旋钮
> 同源（remote=4 / inprocess=1），默认 1 旧语义逐位保持（既有金测试零改动全绿）+
> 新增 3 例多 worker 语义锁定测试。并发度三层钳制：调度器准入 ≤4 → 引擎信号量 ≤4
> → 服务端 gen_sem ≤8。真机效果验证挂 L4 测试任务（并发 A/B）。

> 🟢 **2026-08-09：L4 测试站四连 ✅（agentd-0809 ~2h ≈3u）——日用形态全维验收+
> 路由三处调参定稿**。daily.sh 首跑即真车：bash 3.2 变量名吞多字节坑（`$name（`）
> 修复（20f7ef0）、哨兵拆 sentinel start|stop 子命令（评测停服窗静默）。
> ① **vision 批 fp8 KV 补跑**（n=212 单变量对照，maxlen 钉回 32768）：**0.816 vs
> auto-KV 0.764**——零损上抖（翻转 17↑/6↓ 边界噪声，vision gen 39.3tps）；L4 档
> NVFP4+FP8 KV **全维站住**（tool/long/vision 三线无一降档）。② **云端求助 E2E**：
> 26B 幻觉 provider=google→无钥 anthropic 双败弃求助实案 → cloud_consult 装配期
> 内嵌**有钥供应商枚举**（skill 索引同型）；luna 隐藏推理吃满 2048 正文空返实案 →
> advisor **空正文响亮失败**+单测、cloud.max_tokens 8192。终态 5 连发：**求助 3/5、
> web_research 自研 2/5**（能力边界双路皆合格零漂移，不再过度转向）；审批→L0 脱敏
> （模型自剥合成 PII+守卫掩 1 span 分层生效）→出网台账→费用记账全链验通（$0.021）。
> ③ **并发 A/B**：3 会话同时提交，旋钮 4=**13.8s**（三轮完全并行）vs 旋钮 1=
> **24.2s**（串行准入）——**1.75×**（连续批处理共享吞吐，未到 3× 属预期）；测后
> 旋钮还原 4。④ **R2b 全开行为**（#78 验收）：描述三轮调参 2/5→3/5→**5/5**——
> v2 线索例句多语言化（26B 只认英文 "use the deck skill"，"/deck"/中文「用 deck
> 剧本」不触发，31B 能自行桥接故 #77 口测未暴露）、v3 任务→剧本具体映射例+「会用
> 散工具也先装剧本」次序令；终态自主调对（chart-report/deck）+不匹配零误调+点名
> 回归全过，deck 案按剧本停下请示大纲（剧本遵循实证）。skill_invoke name 参数 R2a
> 措辞残留（"the user asked for"）一并清除。产物存
> `results/l4-nvfp4/route-tests/`（gitignored）。金测试 **720**。

> 🟢 **2026-08-09：#79 对话式注册建成（D.18 完整版，免 GPU）——待 L4 真机验收**。
> 后端（44b569c，主会话亲做）：`register_api_tool`/`register_skill` 起草工具——
> 参数面**没有密钥字段**（红线①物理执行）+ 形似密钥实值提交口拒绝（鉴权头/常见
> 前缀模式）；声明落 `data_dir()/registrations/` 待确认区（不进生效目录不进装配=
> 红线②），撞名不静默顶替；`/api/v1/registrations` 列表/确认/驳回三端点为唯一
> 放行口（模型无确认工具），确认复用表单面同一条保存/上架路（save_skill/
> save_api_tool 抽公共实现）。app 侧（e9c75f1，implementer 施工+主会话审毕+亲复
> 构建）：设置页「待确认」标签+审批队列面板（声明全文等宽审阅/缺密钥琥珀警示/
> effect 转述/驳回二次确认），macOS/iOS 双平台 BUILD SUCCEEDED。金测试 **732**。
> **明日 L4 验收**：26B 起草天气工具+周报剧本→pending→app 确认→真可用全流程；
> 起草质量与红线遵守（密钥只引变量名）实测。

> 🟢 **2026-08-10：#83 工具行为准则站建成（免 GPU，b2c35ed）——待 L4 真机验收**。
> 用户拍板：以 web search 语言选择为例全面规范工具用法（中文对话查欧美新闻→中文
> 关键词搜→中文圈二手转述以讹传讹损精度）。按 L4 站四原则施工（描述=行为契约按
> 26B 写／具体例句>抽象训令／每准则配行为测试+反向准则防加戏／只动错误代价高处）：
> ①检索沙箱 Rule 8 信源社区语言检索+跨区域双语交叉+主源优先报告分歧+本地题禁
> 加戏；②读取子模块专名原文保真；③rag_search 描述纠错（旧文案把外部验证划给
> 本地库=描述性 bug）+未命中换词重试；④web_research 不预翻译+时间范围入问句；
> ⑤三件交付物工具输出语言跟用户（excel 新建跟用户/编辑跟原簿）。61 工具全清单
> 审计 `mcp/TOOL_NORMS_AUDIT.md`（7 改/50 达标/4 候补+触发条件）；在位测试钉
> 准则存在性（金测试 **737**）；真机探针 `benchmarks/tool_norms/norms_probe.py`
> 4 用例。**明日 L4 验收单（与 #79 同场）**：norms_probe 4/4 + 对话式注册全流程。

> 🟢 **2026-08-10：#84 谎报围堵护栏站（06a45e0，金测试 745）+ app 三连闪退修复
> （c315c77/d49589c/525b833）**。26B 日用档真机首日暴露的病灶一次清账：
> **闪退**——macOS 27 beta 对约束阶段重入硬抛，三份 .ips 同链（SplitViewChild
> didUpdateMinSize），逐点钉尺寸两轮未根除后终局=macOS 弃 NavigationSplitView 换
> 手工分栏（27 正式版修执法后可回退）。**谎报实案**（node demo 任务）：heredoc 写
> 炸后谎报"三个文件都建好"（实际文件夹空）、零调用声称"已 nohup 拉起"（进程实为
> 两天前无关项目）、拒批同命令连发 4 次、写入的 index.js 尾部混入工具调用 JSON
> 垃圾运行时才炸。**用户拍板方向**：架构围堵可验证的谎、根治靠模型档位（26B 谎报
> 模式记 D1 病灶；日用档重要产物以护栏核实为准）。**四件套（域无关，与 dup-block
> 同级）**：①错误不可吞（失败后无成功即收尾→推回，坚持→warning）；②零证据动作
> 声明（无成功变更调用却称"已创建/安装/启动"→推回改写，自愈放行）；③拒批同参
> 拦截（拒过的调用原样重发不弹窗直接打回，按轮生效）；④写后语法自检（json/py/
> yaml/sh/js 写后即检响亮告知）。**随案记录的架构缺口**：工具 shell 非登录态不含
> 用户 PATH（nvm 有 node 却报没有→brew 重复装）→ 修复项在册；审批卡挂起时用户
> 发新消息=静默作废轮次（UX 死角）→ app 侧改进项在册。

> 🟢 **2026-08-09：#76 attention/driver 现代化 ✅（免 GPU，本机 e4b）——31B 编排层
> 验收解锁**。站 B 发现的 harness 漂移清偿：审批环从已移除的同步 `/approval` 迁至
> 协议 v1 事件账本制（POST /turns 202 → SSE /events 附着观察 → awaiting_approval
> 即决 → turn_ended 收轮）。要点：after 游标跨重连保持（重连首帧 stream_attached
> seq=0，用 max 防游标回卷死循环）、心跳注释行不当帧、超预算显式拒批防轮次悬挂
> 泄漏会话、鉴权 BENCH_KEY/.env.local 自动解析（空 key 不加头与服务端放行语义对称）。
> 主会话定案设计→implementer 实施→主会话审 diff 通过→E2E 真机验收（inprocess e4b
> 临时切换已还原）：A_small_edit **双审批打穿**、turn_ended status=success、断言与
> 痕迹抓取照常。痕迹/断言逻辑未动，历史数据可比。金测试 716。#77 的 80G 场现可
> 加跑 31B 编排层 attention 验收（6 任务 × n≥10）。

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
| **D.11** | **skill = 主轮次的提示词剧本**，不是自带工具集的隔离子会话。工具沿用主循环 → 审批闸门照常生效，**零新增提权面**。要给 skill 加 `tools:`，须先解决「审批从沙箱透出到前端」 | 2026-07-16 用户 | 归档 §3.2 实测结果 |
| **D.14** | **整条音频路移设备 A**（Q6 定案，重构了原问法）：edge 不留 torch/librosa/silero/whisper，经**新增 `remote` ASR provider**（既有接缝 `backends/__init__.py:40`）+ A 侧 `/v1/transcribe`。**不选 Apple SpeechAnalyzer**——它是 Swift-only（ObjC 头只有旧的 SFSpeechRecognizer），PyObjC 够不到，会让 ASR 依赖 app 存在，与「无 app 的 dev/web-only/CUDA 线」冲突；且要先定 locale，牺牲现有的自动语言检测（`router.py:49`）。SpeechAnalyzer 日后可作同一接缝下的可选快路 | 2026-07-16 用户 | 归档 §3.5 R5-2 |
| **D.13** | **edge 启动逻辑提成单一真相**（`scripts/lib_edge.sh` 或等价），`devctl.sh` 与 app 都调它；Swift 只做它独有的**进程监护**（崩溃重启/日志/端口冲突/随 app 收摊）。**不在 app 里重写一份**——那 799 行里的顺序与探活判据是多起事故换来的（RAG 未就绪即拒启 backend，因工具探活只在启动期发生一次）。前置：启动逻辑须对「仓库布局 vs bundle 布局」「`.env.local` vs Keychain」参数化 | 2026-07-16 用户 | 归档 §3.5 R5-4；R1 同一教训：消除拷贝，不消除差异 |
| **D.16** | **可写数据经 `AGENT_DATA_DIR` 旋钮分家**（Q7 定案，选项 B）：dev 默认仓库内（保住「数据随手可见可删」的手感），app 注入 `~/Library/Application Support/dev.enkin.macagent/`。**不选「一律搬 App Support、dev 也用」**——dev 时数据离开仓库会让排查与清库都变绕。代价是两套路径都得测。与 D.13 要求的「启动逻辑对仓库布局 vs bundle 布局参数化」**是同一个旋钮**，不是第二个 | 2026-07-16 用户 | 归档 §3.5 R5-4 |
| **D.17** | **R2b（skill 自主发现）全开**（用户二次拍板改全开，非半开）：撤 skill_invoke「只认用户点名」禁令，工具描述内嵌剧本索引（装配期生成），任务明确匹配即**自主调用**、点名照常必调。依据：#77 口测=31B 未点名守禁✓且手工自发复刻 chart-report 步骤。回退开关=恢复旧描述（git 可查）；误配风险由 26B/31B 真机行为验证把关（L4 测试任务在册） | 2026-08-09 用户 | #77 口测；D.9 触发条件达成 |
| **D.18** | **DEFERRED§4 完整版（对话式工具/剧本注册）建**。红线照旧：①密钥只引变量名（31B 起草实测已自发遵守）；②声明落「待用户确认」态、不可静默生效（待确认状态机+设置页审批队列=主要工程量） | 2026-08-09 用户 | #77 能力评估达标 |
| **D.19** | **生产起服 `max_model_len` 调 64K**（config cuda 段 32768→65536）。依据：8 页 deck 单轮 ~33k 撞顶实案；L4 fp8 KV 池实测 114,963 token、A100 档 KV bf16 65536≈3.7GiB 均有余量 | 2026-08-09 用户 | #77 新架构发现；L4 站实测 |
| **D.15** | **内嵌运行时选 `python-build-standalone`**（R5-3 选型，三条判据全部实测）。**否决 PyInstaller**——与本仓库「文件系统即真相源」的约定直接相冲：`mcp/registry.py:26` 扫目录找 `.py` 发现工具、`skill_registry.py:58` 与 `memory/notes.py:86` 扫 `*.md`，冻结后统统扫不到；且 `:21-23` 目录不存在时**静默建空目录后 return** → **零工具注册且不报错**。**否决「venv 拷进 bundle」**——`backend/.venv/bin/python3.13` 是指向 `/Library/Frameworks/Python.framework` 的**绝对路径符号链接**（venv 里没有解释器本体），而该 framework 的 install-name 也是绝对路径、本身搬不动，净体积 307M 且签的是 PSF 的 Developer ID | 2026-07-16 实测 | 归档 §3.5 R5-3；`scripts/build-runtime.sh` |
| **D.12** | **`tool_responses[]` 条目是「模型上下文 + 渲染源」两用面**，模型可见键走**白名单** `{name, response, content}`。R3 的 `render` 载荷挂同一条目但不进提示词；顺带修掉 `_safe`/`_target` 的既有泄漏。代价（已知并接受）：模型提示词发生变化，16GB 机上无真模型可验证质量 | 2026-07-16 用户 | 归档 §3.3 实测结果 |

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
| **R5** | macOS app 收编执行层 | ✅ 2026-07-17（R5-9 完整收官，详见归档 §3.5） | R0–R4 | 无 | 铁律 1/4 + 改 CHARTER |
| **D1–D4** | 能力量变扩充 | 🟡 D1 首批收官 / D2 首批 7 件 / D4 阈值已定 | R5 ✅ | 无 | 低 |

**R2b**（skill 自主发现）→ `DEFERRED.md`，触发条件：R0 完成 + 目标模型（30–70B）硬件到位。

### 细分进度

R0–R5 各小项的打勾细分（含每步实测结果与计划外发现）已随 2026-08-08 文档瘦身波
迁至 `EXECUTION_LEDGER_ARCHIVE.md`（§3.0–§3.6 原文逐字保留）。


</details>

---

## §3 各阶段详细步骤

> **已收官历史（R0–R5 / D 阶段 / B2c / 七月各波 / OfficeCLI 三波，2026-07-09～08-05）**
> 已于 2026-08-08 文档瘦身波逐字迁至 `EXECUTION_LEDGER_ARCHIVE.md`（内容零删改）。
> 本节从当前活跃线（Colab CUDA 兼容波）开始。

### Colab CUDA 兼容波 · 布置阶段（2026-08-06，进行中）

**用户拍板的路线**：生产机到位前，先用 Colab A100 80GB 完善推理端 CUDA 兼容并
打通整套 Agent 流程，生产机上只剩验证与 debug。形态=Colab 当"设备 A"（backend/
RAG 留本机，remote 分离部署拨 Colab 的 tailnet 地址）；Drive 当跨会话持久层
（代码 tar/bundle + bf16 模型原件 + pip 缓存），tailscale userspace + ephemeral
key 打洞供直接操作。可行性评估已交（Drive FUSE 小文件坑→tar 中转、Colab ToS
风险已明示、量化口径=A100 无原生 FP8 但 vLLM Marlin weight-only 可在线量化故
存 bf16 原件无悔）。

**已落地**：`colab/stage_to_drive.sh`（一次性布置：4 主模型 + 10 向量/重排 bf16
原件 ~350GB + 可选代码 bundle 入 Drive；逐模型 .complete 断点续传、本地盘中转
绕 FUSE 慢写与符号链接坑、本地盘余量守卫、gated/私库缺凭据跳过不炸、密钥只经
Drive env 文件）+ `colab/README.md`（Drive 布局/前置动作/量化口径）。

**待做**：①用户侧：HF 接受 gemma 许可 + colab.env（HF_TOKEN、可选 GIT_TOKEN、
后续 TS_AUTHKEY）+ 首跑 stage 脚本；②bootstrap.sh（tailscale 入网→解包→依赖→
拉起→契约测试，目标冷启 <10min）；③CUDA 推理端本体（vLLM 包壳出 inference/
server.py 同协议）；④契约测试扩充；⑤sync_to_colab.sh 本地同步脚本。

**前置布置全落地（2026-08-06 第二段，上面待做②③④⑤清零）**：
- **CUDA 后端本体** `backends/cuda/`（vLLM 实现 InferenceBackend 协议，server
  一行不动）：缓存=记账语义（物理复用交引擎 APC；clone 独立记账/persist 恒
  False→server 冷路兜底）；生成=私有事件循环线程跑 AsyncLLMEngine，经有界队列
  桥接成协议要求的同步迭代器，弃置即 abort（段粒度取消对齐）；tool_guard 经
  _TorchGuardAdapter 挂 per-request logits processor（vLLM 二参约定只喂已生成
  序列——无 prompt 边界簿记，allow=None 路径零 torch 依赖故本地可金测试；
  版本拒绝时响亮降级为上游解析兜底）；skip_special_tokens=False（规范文本含
  频道/工具标记的协议口径）；add_special_tokens=False 分词自理防双 BOS。
  诚实边界（代码内响亮告警）：thinking 预算未实现/repetition_context_size 无
  对应物/多模态透传未实测/Qwen3.6 方言按 qwen35 预注册待首验。
- **server INFER_BACKEND 环境覆盖**（与 INFER_REMOTE_HOST/PORT/MODEL_REPO 同
  惯例）；config.yaml：inference.cuda 旋钮（fp8/0.92/32768/APC）+ CUDA 档 4
  模型注册（gemma4×2、qwen35×2，无 profile=路由绝不优先选中，revision 待
  Colab 首拉回填）。
- **契约测试** scripts/check_infer_contract.py：18 项黑盒协议断言（探活/401×2/
  缓存三态/SSE meta→chunk→done 形状/字段齐全/token 单调/缓存续用同 id/取消
  收场+无 error/OpenAI 门面×2）——**已对本机 MLX 服务实测 18/18 全过**（临时
  拉起 14164 用完即收），测试自身可信，Colab 上即 CUDA 端验收门禁。
- **colab/bootstrap.sh**（五步幂等：tailscale userspace 入网→解包→pip（Drive
  缓存；不用 requirements.txt——那是 MLX 家的）→权重落本地盘+补
  .download_complete 标记→拉起+契约；绑回环=userspace 入站自动转发，护栏零
  触碰）+ **agent_colab.ipynb**（挂载/引导/保活心跳三 cell）+
  **sync_to_colab.sh**（本地 HEAD 工作树 tar→Drive，日常免 GitHub 通道）。
金测试 666 → **674**（+8：导入安全/协议形状/记账语义/适配器纯逻辑×2/配置面/
env 覆盖锚点）。剩余=用户跑 stage/配 colab.env，然后 A100 会话实测联调。

**布置收官（2026-08-06 第三段）：14/14 模型 325G 全部入 Drive**。过程踩坑全记：
①Tailscale 打通后远程操作 Colab 成为常态（用户免费档入网点授权，其余全远程）；
②「本地中转+rsync」在 Colab 是 2× 本地盘占用（DriveFS 写缓存也落本地盘），
108G 免费档盘对 62G 大件必死——stage 脚本已改直下+排空闸门（64848b0）；
③HF Xet 高性能通道是内存杀手：Colab 免费档（13GB）下 16G 模型被 OOM 杀，
开发机 16GB 下 35B 档把 swap 顶到 24GB 全红——禁 Xet（HF_HUB_DISABLE_XET=1）
立愈，两侧实证；④大件改走本地家宽（3-4Gbps）下载 + rclone 上传（~60MB/s），
比 Colab FUSE 直下快数倍；⑤本地灌大权重必挂 .noindex 后缀防 Spotlight 索引
风暴（mds_stores 160% CPU 案）；⑥pkill -f 自匹配三连击（命令串含目标名先杀
自己）——远程杀进程一律 pidfile 或 pgrep -qf '模式[断]字'。
下一步=Pro/Pro+ 订阅后 A100 首跑 bootstrap（联调正式开工）。

**CUDA 联调波·第 1 站 ✅（2026-08-06 A100 首会话）：bootstrap 首跑 + 契约 19/19
一次全过**——盲写的 CudaVLLMBackend 首次真机装配即通过全部协议断言（health/鉴权/
缓存三态+clone 新 id/SSE 形状/token 单调/缓存续用/取消收场/门面），"换后端 server
一行不动"兑现。gemma-4-31B-it fp8 在线量化：VRAM 76.0/80GB（0.92 预算含 APC
预分配），prompt_tps 672 / **gen_tps 41.9**（128 tok 壁钟 3.1s）。首会话修掉三坑
（全部固化回 bootstrap，f5cef59/最新）：①SSH 裸环境缺 GPU 库路径（LD_LIBRARY_PATH
=/usr/lib64-nvidia）②tailscale 手工入网场景免 authkey 复用 ③vLLM 0.26 原生扩展
链 cu13 × Colab torch cu128 混装——pip nvidia/*/lib 全量前置 LD 路径即愈。
INFER_API_KEY 经管道从本机 .env.local 同步 colab.env（不经对话）。运维纪律新立：
等计费 GPU 长任务必挂看门狗（停滞 10min/超时报警，记忆持久化）。
下一站=其余 3 模型逐个起测钉版 + 工具约束实测。

〔2026-08-06 用户拍板·算力预算口径〕Pro+ 总 600 units，A100 80GB 7.52u/h ≈ 80h，
**按 70h 预算规划**（需求估 25-42h，余量近倍）。据此原"留生产机"的 GPU 项全部
搬进 Colab：视觉加难批/ChartQA 全量、MoE 并发衰减、T7 并发、L0/L1/L2 多模型
常驻分层、大档脾气摸底（全加约 40-55h，预算内）。24h 不掉线可承接大部分实机
验证；真·必须等生产机的只有三条：①跨多日常驻/跨天定时任务 ②真实个人数据
（Colab 盘不在信任边界，一律合成数据）③最终硬件性能标定（2026-08-08 用户质疑
26B 单流 140tps 远低于带宽账触发拆解：fp8/nvfp4/80G 三组实测反解每步固定开销
恒 ~4.5ms——Triton 强制后端+Marlin 仿真核+MoE 路由 launch 开销，单流是开销瓶颈
非带宽瓶颈，8 路 824tok/s 与 MTP 269tps 旁证硬件有余量；用户拍板：不在 Colab
做分解实验，生产机到手直接重跑速度画像）。生产机侧因此缩为
"迁移安装+复跑契约与金剧本，约半天到两天"。省点数纪律：0.6-4B 档评测走免费
T4，本地能做的不上 A100。

**CUDA 联调波·第 2 站 ✅（2026-08-06/07）：四模型契约全过 + revision 钉死 +
qwen35 方言转正**——逐个起测全部 19/19：31B fp8（672/41.9，76.0G）、26B-A4B
fp8 MoE（392/**149.5**，75.2G）、Qwen3.6-35B-A3B fp8（202/**138.7**，73.8G）、
Ornith-1.0-35B fp8（200/**141.4**，73.8G）（prompt_tps/gen_tps，VRAM；128tok 探针）。
判明：**Ornith 实为 qwen3_5_moe（256 专家激活 8）非稠密**——四模型仅 31B 稠密，
三 MoE 生成全在 138-150 档、稠密 41.9，MoE 速度红利 ~3.4×。qwen35 方言预注册
转正（facade chat/completions + 中日文问答内容正确，ChatML 模板/解析链成立）；
vLLM 直接接受 Ornith config.json（MLX 侧 qwen3_5_vision fixup 案在 CUDA 侧无需
重演）。revision 四仓钉死进 config（HF API 主干 SHA，lastModified 均早于入库日；
`.download_complete` revision:null 走"任一未指定即命中"不冲突）。
本站新坑（累计自匹配案升至 5）：①看门狗退勤条件 `pgrep -f bootstrap.sh` 自匹配
→ 永判存活（第 4 案，方括号模式修）；②**换代杀进程树不完整**——pkill 只杀 server
父进程，vLLM EngineCore 独立子进程孤儿化抱 76G 显存致 26B 拒启（free 4.65G）→
bootstrap 改为父+EngineCore 双杀+等 nvidia-smi 清零+残留 -9 兜底；③方括号技巧
边界：同一 bash -c 里 pgrep 模式与 nohup 明文并存时明文仍被匹配（第 5 案，pidfile
分独立 ssh 写）；④**Colab 判闲回收实证**：qwen35 rsync 纯 IO 阶段（26B server 常驻
但 GPU 零计算）运行时被回收，全新 VM /content 全失、Drive 无损、全量 bootstrap
重跑续命——常驻≠防判闲。用户新规：闲时挂 `colab/gpu_keepalive.py`（30s 一次 16MB
matmul 心跳，pidfile 管理），重活前停（已入持久记忆）。
下一站=工具段约束实测（TorchGuardAdapter 真机）+ edge 拨号整套剧本。

**CUDA 联调波·第 3 站 ✅（2026-08-07）：工具段约束 structural-tag 迁移落地实测
全绿**——实测判死旧路：vLLM 0.26 SamplingParams 拒收 per-request logits_processors
（Unexpected keyword argument，响亮降级如设计触发）→ 按预案迁移 vLLM 原生
structural-tag（begin/schema/end+triggers，触发前自由/段内按 schema 掩码/收尾
恢复自由——与方言 stc/etc 锚定语义同构）。架构：server 在 features 附**声明式
约束规格** tool_constraint（schema=方言 tool_call_schema + markers.stc/etc；
guard 构建成功才附，qwen35 XML 方言在 fail-open 内走不到）；cuda 后端首选
structural-tag、_TorchGuardAdapter 降为旧版后备；仪表由后端尾声按 stc/etc 清点
约束段 token 回填 guard.masked_positions（同进程直改，server done 口径不变）。
26B 实测：模型自发 stc → 段内 JSON 合法 + name const 命中 + required(city) 强制
补齐 + etc 强制闭合，constrained_steps=31，零降级打印；换代码后契约 19/19 复验
零回归。金测试 674→676（structural_tag_json 形状 + 约束段清点仪表，纯函数无
vLLM 依赖本地可测）。**probe 两连坑（含金量在教训）**：①chunk.text 是增量段
（协议 backend_protocol:22 白纸黑字"全程累加"），按 MLX 误记的累积语义读=空文
虚惊一场；②工具声明生产形状是 parameters{properties,required}（registry:193），
探针手写 input_schema（Anthropic API 习惯）→ schema 空属性+additionalProperties
:false=强制空 arguments——**表象酷似"约束坏了"，实为喂错声明**。
thinking 预算勘定不接：vLLM 原生位需引擎级 reasoning_config + 强制 V1 runner
（V2 不支持），为兜底功能降级全局 runner 不划算，接线路径已注释留生产机。
额外基建（用户发现+指示）：**权重根迁 NVMe scratch**（/mnt/local-scratch 369G
全空盘，backend/models 符号链接指过去，四主模型 241G 可共存免删拉循环，写速
410MB/s）+ 盘面自清兜底（<80G 清非目标模型，33G 剩量惊险案）+ gpu_keepalive
防判闲纪律执行中。下一站=edge 拨号整套 Agent 剧本实测。

**CUDA 联调波·第 4 站 ✅（2026-08-07）：edge 拨号整套 Agent 剧本 4/4 全通，
联调波收官**——本机 edge 编队（RAG 融合本机 + SearXNG + backend + 前端，混合
拓扑手维 .env.local 两键：INFER_REMOTE_HOST=colab、RAG_COMPUTE_HOST 置空）拨号
colab-a100 26B。剧本：①时间工具（get_current_time 闭环+答复）②联网搜索
（web_research 5 次进度事件+摘要回喂+77 token 总结——研究子模块内部多次生成
同样走 colab）③shell（echo 执行 exit 0 原样转述）④thinking 数学题（先思考后
tool 验算 1/(1/3+1/6)=2.0，答案正确，**控制标记零泄漏**进正文=CUDA 段语义×
VisibleTextStreamer 全栈成立）。全部 turn_ended: success。
edge 模型口径经 .model-selection 切 26B（分词器 tokenizer-only 拉取）。途中修
一坑：`.hf_token_tmp` 被行内中文注释污染（121B 非 ascii）→ httpx 头 ascii 编码
炸——colab.env 的 HF_TOKEN 行内注释 shell source 会截断而 awk 全吞，重取时按
首个空白截断（37B 纯 ascii ✓）。收场按部署纪律：devctl stop 全停 + .env.local
恢复单机双角色 + .model-selection 清除。**CUDA 联调波四站全毕**：协议契约、
四模型、约束采样、整套 Agent 全绿——"生产机=迁移安装+复跑"的承诺基座齐了。
下一波=大档评测波（model_eval 4 模型画像回填 → L0 大档重测 → RAG 10 模型选型
（小档免费 T4）→ 视觉加难/MoE 并发/T7/分层/24h 长稳）。

〔2026-08-07 休会〕Colab 会话由用户手动停止（当日累计 ~40/600 units）。次日重开
= 新 VM：挂 Drive → 手工 tailscale 入网 → `bash /content/drive/MyDrive/AgentColab/
bootstrap.sh <模型repo>` 一键到位（代码包 7a719ae，权重自动落 NVMe scratch）。
本地 191G staging（colab-models-staging.noindex）已删——三大模型 Colab 装载验证
完毕、Drive 真身 14/14 在库，删除条件闭合。

**大档评测波·第 1 站 ✅（2026-08-08）：model_eval 四模型画像回填**——A100 引擎
直连（run_eval 新增 --backend cuda），46/33 题电池（tool_call 25 计分 + longchain
6 + vision 15/20 计分 + speed/cot 顺带）。画像（已钉 config profile）：
| 模型 | tool_call | longchain | vision | gen_tps | KV命中 |
| 31B 稠密 | 0.96 | 0.833 | 15/15 | 43 | 0.52 |
| 26B-A4B | 0.92 | 0.833 | 20/20 | 157 | 0.60 |
| Qwen3.6-35B-A3B | 0.88 | **1.0** | — | 143 | **0.03⚠** |
| Ornith-1.0-35B | 0.92 | **1.0** | — | 144 | **0.0⚠** |
**站中揪出架构缺口并修复**：首跑 26B tool_call 仅 0.72、12 次解析错、bfcl-multiple
成批裸格式（`call:x{...}`）——根因=tool_constraint 只有 server 附，**进程内引擎
直连路径全场无约束**（降级打印 1 次实锤）→ engine.py 在 guard 旁同源附声明式
规格 → 复测 0.92、解析错 0、首发合格 0.696→0.913。金测试 676 零回归。
判分结论：剩余失败全为模型侧（选错工具/克制未调/参数取值），D1 记档；
multiple_21 三模型同倒=题目诱导弱信号。**9B 先验"编排弱"大档正式反转**（量化
伪像），"长链强"保持（qwen35 系 longchain 双满分）。cot 纯净度全员零泄漏零复读。
**新观察项（架构侧）**：qwen35 方言 KV 前缀命中 0.03/0.0 vs gemma 系 0.5-0.6——
方言 prompt 布局疑破坏前缀稳定性，缓存友好性待查（下一波候选）。
〔站 B 闭合 2026-08-09〕**原"布局破坏前缀"假设推翻**：gemma4/qwen35 两 build 布局
代码实证逐字节同构（工具进稳定前缀、task/env 置尾）；真因架构级=Qwen3.5-MoE 混合
线性注意力 3:1，vLLM APC 只缓存全注意力 KV 块、线性层递归态天然不可前缀缓存 →
命中恒 ~0.0 是该架构固有属性、无优化杠杆，qwen35 全线非生产选型故负结果归档。
运维：pkill 自匹配第 6/7 案（ssh 命令行内模式+明文并存两变体）；四模型权重全驻
NVMe scratch（241G）随换随用；评测产物已同步 Drive eval_results/。
保活纪律 v2 落地（用户 2026-08-08 修订）：工作空窗 5GB 驻留+占空比心跳；
**阶段汇报即清 GPU**——用户侧 25min 判零看门 cell 自动注销止损。
下一站=L0 大档重测 / 视觉加难批（ChartQA 全量）/ RAG 10 模型选型（小档 T4）。

**大档评测波·第 2 站 ✅（2026-08-08）：L0 大档重测 + ChartQA 加难批**——
**L0 四连（23 题生产同路，--backend cuda 补装）**：31B 召回 **0.96**/全对 0.957/
0.30s、26B **0.92**/0.913/**0.09s**、qwen35 系双胞胎 0.68/0.652（org 归零跨代
复现=该底座对机构名脱敏无感，L0 排除）。四家零过遮。**9B 档结论全面改写**
（原最优 e4b 仅 0.60）：L0 生产候选=gemma 大档（26B 甜点位/31B 质量位），org
从全军覆没→1.0，残余短板唯 account 形态（补题方向）。放宽审批闸与否=隐私权衡
留用户（0.96 仍有 4% 漏出面），BASELINE.md 已回填大档表。
**ChartQA 加难批**：借题流水线（拒绝类别程序化+6 图人工抽查核算）从 test#200 后
1199 候选取 80 加难（difference/sum/average/ratio 多步读数），首采 40；26B/31B
双跑后对共同失败 6 题逐题裁决**剔 3 坏题**（语义两可/标签偏读法）定稿 37 题：
**26B 0.811 / 31B 0.838**（易批双满分→区分度达成），vision_hard 已入画像。
新复审纪律固化：**双模型共同失败集=廉价坏题探测器**（入 VETTING）。
运维：L0 评测器 --backend 补装与 model_eval 同款；评测产物随站同步 Drive。
下一站=RAG 10 模型选型（0.6-4B 档免费 T4 / 8B 档 A100）→ MoE 并发衰减 / T7 并发。

**大档评测波·第 3 站（进行中，2026-08-08）：T7 并发落地 + 并发衰减画像 +
分层常驻**——**T7 纪律票兑现**：服务面 gen_lock→gen_sem（Semaphore）+ executor
按后端参数化（MLX=1 旧互斥语义逐字保留；cuda=config cuda.max_concurrency 默认 8，
vLLM 连续批处理白拿），契约 19/19 复验零回归。**并发衰减实测（128×256tok/路）**：
26B MoE 单路 151 → 8 路每路 106（衰 30%）总吞吐 **824 tok/s**（5.6×）；31B 稠密
单路 41.6 → 8 路 38.9（**仅衰 6.5%**）总吞吐 304（7.4× 近线性）；TTFT 两档均
≤0.15s。结论：MoE 衰减大于稠密（专家路由争抢）但绝对值仍碾压，两档并发均够
生产。**分层常驻（进行中）**：INFER_GPU_FRAC 每实例显存占比 env 钩已落
（cuda/backend.py，env 非空即赢惯例）；实测教训①31B fp8 在 0.50 占比（40G）下
KV 块分配失败拒启——权重+激活已 ~38G，需 ≥0.56；教训②（待复盘实锤）A@0.56
装载 + B 常驻并存时**整机失联**，疑似双引擎宿主内存峰值挤爆 VM——恢复后改
**顺序装载**验证。26B@0.40 单档曾就绪（14163 健康），双档齐验中断于节点崩溃。

〔第 3 站收官 ✅ 2026-08-08〕**分层常驻双档齐验**（80G hm VM，顺序装载纪律）：
A=31B@0.56 + B=26B@0.40 并存，**双端契约各 19/19**；同时 4×4 互扰：A 每路
41.6→29.0（-28%）、B 128→47.5（-63%，SM 争抢主要打 MoE 侧），TTFT ≤0.21s，
合计 300 tok/s——分层形态成立、代价已定量。宿主 RAM 全程 ≥155G 可用，
**OOM 假说正式证伪；三连崩真凶=用户侧 25min 判零看门 cell**（新形态：while
True 保活 cell + 阶段汇报手动停机）。
本站坑账（自匹配已 8 案）：⑥⑦⑧=ssh 命令串内 pkill 模式与明文并存的三个变体
（修法=杀进程独立 ssh + 方括号 + 绝不与明文同串）；**env 变量不在 cmdline，
pkill -f "INFER_PORT=x" 恒打空**——误启 B 的壳被杀本体存活，orphan EngineCore
抱 33G 且**用污染权重**（rsync 与 HF snapshot_download 同目标目录互踩产出假
.download_complete——dry-run 差 12 文件实锤；修法=杀后 nvidia-smi 按 pid 清点
+ 标记只信落盘链路自写）。
〔Colab CLI 采用记录 2026-08-08〕官方 google-colab-cli 实测：oauth 认证/exec
持久 kernel/upload/stop 全通（依赖钉 jupyter-kernel-client==0.15.0）；TS_AUTHKEY
headless 入网生产首用 ✅（colab.env 引号修复无副作用，INFER_API_KEY 两侧同值）。
**格局=混合双轨**：CLI `--gpu A100` 只给 40G s 档（无 scratch，~5.3u/h；H100 无
容量）→ 80G 活走浏览器三 cell（挂 Drive/后台 bootstrap/while True 保活）+
tailscale；≤40G 活走 CLI 全自动。CLI 坑：会话丢句柄（404）后服务端 VM 不随清
=孤儿继续计费，CLI 停不了（status/stop 均 not found）——须浏览器"管理会话"手杀
（已实锤一次 ~5u 学费）；rclone 拉 Drive 替代 drivemount（interactive-only）。
下一目=RAG 五档选型（同 VM 接力）→ FP4(NF4 代理) 能力偏移三连（31B/26B/Ornith，
Qwen3.6 被 Ornith 全维压制弃测）→ 26B 24h 长稳（CLI 40G 档即可）。

**大档评测波·第 4 站 ✅（2026-08-08）：RAG 五档选型收官**——检索质量 harness
（34 题 8 形态，部署同路 CUDA）：**text-0.6b 0.941/0.938 并列榜首**（尺寸无红利：
4b 0.882、8b 0.912 反降）；**vl-2b 0.941/MRR 0.969 全场最高**=多模态升级路文本
零回归白拿；vl-8b 塌方 0.588（跨语 0.33/name 归零，"仅加载+文本 I/O"实验态记
异常排除待查，不阻塞选型）。**选型定案：生产维持 text-0.6b，VL 升级路 vl-2b，
4b/8b/vl-8b 不采。**
本站三连环 bug 全歼（真相全靠用户盯 VRAM 面板）：①harness 收尾 proc.wait(30)
把算完的结果连 summarize 一起带走（SIGTERM→SIGKILL 兜底修）；②sweep 从无 GPU
env 的 ssh 起 → venv torch 缺 libcuda **静默落 CPU**（显存恒零案；脚本 env 自带
修）；③RAG_CONFIG 指 tmp 后档位指针失联 → "五档"实为 0.6b 跑五遍（五档全同分
+8B 装载时长露馅；指针随行修）。教训：**静默降级三件套（CPU 回退/默认档回退/
收尾吞结果）都要显式指纹**——VRAM 阶梯与装载时长就是最好的换档指纹。
产物 Drive eval_results/rag_tiers/。下一目=FP4 偏移三连 → 26B 24h 长稳。

**大档评测波·第 5 站 ✅（2026-08-08）：FP4 能力偏移（NF4 代理，用户新增）**——
INFER_QUANT env 钩实测可用（vLLM 自动配 load_format=bitsandbytes）。
**31B 稠密 NF4：能力零偏移**——tool_call 0.92（fp8 0.96，±1 题噪声带）、
longchain 1.0（反超 fp8 0.833）、vision 合并 0.904（fp8 0.885，+1 题）、cot 零
泄漏零复读；代价唯速度 gen_tps 34.4 vs 43（-20%，BnB 内核锅——nvfp4 生产核
另当别论）。**结论：FP4 对稠密 31B 够用信号强烈。**
**MoE 档（26B/Ornith）NF4 不可行**：vLLM 0.26 BnB 装载器明确拒绝 MoE padded
专家布局（Parameter shape (991232,1) != checkpoint (2816,704)）——FP4 判断
对 MoE 双档只能等生产机 nvfp4 原生核。Qwen3.6 弃测（Ornith 全维压制）如前。
产物 Drive eval_results/{26B,31B}_nf4/。下一目=26B 24h 长稳（时点待用户拍板：
24h×~5.3-6.8u≈27-35u，CLI 40G 档即可承载）。

〔账本复盘补测清单落账 2026-08-08，用户令"先复盘再开工"产出〕遗漏项全数入列：
① **Gemma-4 官方 MTP assistant 草稿**（用户记忆正确、我首查主模型 config 误判无
——官方独立草稿檔 google/gemma-4-{26B-A4B,31B}-it-assistant，共享嵌入+复用目标
KV，宣称至 3×；vLLM v0.22 起支持，写法 {"model":"…-assistant",
"num_speculative_tokens":N} 无 method 键）——头牌；② FP8 KV cache 漂移；
③ qwen 系 in-weights MTP（method=mtp）；④ M8 约束税 CUDA A/B；⑤ facade
streaming；⑥ ASR /v1/transcribe CUDA；⑦ 多模型路由 E2E（80G 站）。
**显存账拆批**：31B fp8 需 ~45G（0.56×80 实测底线）、Ornith-35B fp8 ~35G+KV
——均超 40G s 档 → ①之 31B 与③挪 80G 浏览器站；其余 26B/NVFP4 件走 CLI 40G。

**RAG vl-2b 换档本机落地 ✅（2026-08-08）**：钉档重验（指针显式 vl-2b）
pass 0.941 / **MRR 0.969 与 A100 完全一致**（name 3/3 满分、multidoc 归零同 A100
形态）——本机身份验真；此前一次 0.941/0.938"零回归"实为 **0.6b 误跑**（指针缺席
+config 未落盘时段，逐位等于旧基线即同档指纹——第 4 站教训本机复现第一案）。
生产库按 reembed.py 七步走完：dump 2 分类 → wipe → vl-2b 空库盖戳
（tier=vl-2b dim=2048 fused/mps）→ 重灌 6 篇 → 检索实测命中 ✓。
顺手修：.env.local 拆分实验残留 RAG_COMPUTE_HOST（指已回收 VM）注释掉，
本机回融合模式。云端顾问 key 首验 ✅（/v1/models 200，luna 在列；密钥体检
经静默指纹不过对话通道）。

**大档评测波·第 6 站 ✅（2026-08-08）：CLI 40G 补测批（batch6，~1.65h≈8.7u）**——
**头牌 · Gemma-4 官方 MTP assistant（26B）成立**：k=2 +62%、**k=4 1.92×**
（139.8→269.0 gen_tps），tool_call 0.92 逐项同分/解析错 0/first_shot 0.913 不动
——质量零漂移，且全程 structural-tag 约束在位=**投机 × 约束共存实证**；KV 跨模型
共享接线日志实锤（草稿 4 层→目标 28/29 层）。排障三层洋葱（各自入坑账）：
①speculative_config 须 `method:"mtp"`（无 method 落通用 draft 路径维度崩；网上
PR 摘要误导，源码才是真相）；②vLLM 0.26 **嵌入共享维度护栏回归**误杀 gemma4
（目标 2816≠草稿 1024 即弃共享，可 pre_projection 期待 2×2816——新 worker 栈无
条件共享、活跃旧栈误加护栏；VM 热补丁三行豁免通过，**值得上游 issue**）；
③投机硬拒 min_p/logit_bias→cuda backend 增 INFER_SPEC_CONFIG 在位弃 min_p 分支。
观察项：longchain k=2 单题翻面（n=6 噪声带）；k>1 同 MTP 层复跑官方警告（k=4
仍比 k=2 快 19%，净赚）。
**NVFP4 双件**：26B（RedHatAI 官方件）tool_call **0.96**(+1)/longchain 0.833(-1)/
vision 0.827（52 题混合，fp8 折算≈0.865，-2）——**带内且提速 25%**（174.8 tps，
Marlin 仿真下犹快，生产原生核只好不坏）→ MoE 档 FP4 判断补全：**够用信号成立**；
Ornith-35B（社区件）tool_call 0.84（fp8 0.92）+**解析错 0→3**/first_shot 0.783=
**实劣化**，采用前须换件或自量化复验；qwen35 系 KV 前缀命中 0.0 又复现（三证）。
**FP8 KV 40G 卡双拒**：gemma4 强制 Triton 后端要 SM89+（A100=SM80 响亮拒）；
Ornith 替测被 Mamba 页对齐断言拦（block 2096>batched 2048，可解未追）→ 归等
生产机清单。**server 形态四连**：契约 19/19 **投机在位零破坏**；门面流式 OpenAI
chunk 形状逐段增量 ✓；ASR /v1/transcribe **CUDA 端缺 provider**（钉死 mlx-whisper
→ 需后端无关 provider，架构侧 TODO 下波实现）；M8 约束税 A/B **首探作废**（探针
未走方言构造致输出退化、A/B 条件不对等〔APC 前缀复用抹平 B 的 prompt 账〕、片计
≠token 计——重做并入 80G 站，与 guard_probe 合并设计）。
运维坑账：**仓库可见性复归 PRIVATE**（好事）→ 匿名 clone 静默失败而引导未设致命
——bootstrap fallback 已改 GIT_TOKEN 认证 + clone 失败即死；colab exec 无位置参数
（只吃 -f/stdin，首"点火"实为未执行）；会话 stop 干净无孤儿（sessions 复核零残留）。
产物 Drive eval_results/batch6/。**下一目=80G 浏览器站**（31B MTP assistant +
Ornith fp8 in-weights MTP + 多模型路由 E2E + M8 重做 + gemma ngram 可选）；
26B 24h 长稳时点仍待用户拍板。

**大档评测波·第 7 站 ✅（2026-08-08）：80G 收官批（~3h≈20u）**——
**① 31B MTP assistant：3.12×**（42.6→132.7 gen_tps，基线与画像 43 吻合），
tool_call 0.96 逐项同分/解析错 0/longchain 0.833 持平——质量零漂移；稠密档吃满
官方"至 3×"宣称（MoE 26B 1.92× 对照：稠密带宽占比高+接受率高）。**31B 从
"质量王但 43tps"翻身 133tps 可用速度，生产分层格局改写**。
**② Ornith in-weights MTP：0.50× 负收益弃用**（143.8→72.3，且解析错 0→1）——
SSM 混合架构投机回滚成本反噬；**投机红利只属 gemma 系**。Ornith 基线 tool_call
本轮 0.96（画像 0.92 的 +1 题带内）。
**③ 路由 E2E 全通**：双档常驻复现（31B@0.56+26B@0.40，79.9/81.9G）→ edge 拨号
双端点注册（`追加推理端点：moe-fast`）→ 真轮次 tailnet 流式完成 → 26B 端点真实
承载；`select_endpoint` 活体五 kind 全按设计（31B 全维领先→0.05 边际挑战不成立
→恒 main；chat/summary 设计粘滞）。测后 config 回归注释态（生产常开待实机拓扑
拍板）；devctl -h 一键两职带写 RAG_COMPUTE_HOST 残留已复原（同型坑第二案）。
**④ M8 约束税定案：稳态零税**（153.1→153.2 tps，-0.1%；掩码步 71 真实发生），
成本全在**首次 schema 语法编译 ~9.5s**（一次性，缓存后 TTFT 0.03s；生产可预热）
——首探"10×"确系编译期误判税率。
**⑤ H 固定开销诊断（用户令，代码层）**：纯引擎稳态 154.8 tps/6.46ms 步（评测环
另垫 0.7ms）；eager 对照 85.8ms 步=**CUDA graphs 已省 79ms/步（13×），是最大
杠杆且默认已启用**；FULL_DECODE_ONLY/FULL_AND_PIECEWISE/async_scheduling/
FLASH_ATTN 覆盖全部试尽**零增益**——引擎参数层无剩余杠杆，余下 ~3.9ms/步在
kernel 内部（Marlin fp8 反量化+MoE 分发+Triton 强制注意力），属上游/生产原生核
范畴。结论：单流提速的现实路径=MTP（已入账）与生产机原生核，非调参。
**⑥ 权重通道革命（用户实测触发）**：大内存 VM 解禁 Xet——Ornith 65G/182s=
**357MB/s**、26B 52G/185s=281MB/s，较 Drive FUSE rsync（45MB/s）**6-8×**；
纪律修订入记忆（小内存环境仍禁 Xet；A100 会话权重获取 HF+Xet 直下 > Drive）。
坑账：h_diag v1 的 `| tail -2` 吞了结果 JSON（S.6"为好看丢线索"变体，重跑修正）。
产物 Drive eval_results/station7_80g/。**MTP 采用已拍板（2026-08-08）：k 不定死，
按设备取测试最佳**——Colab 期以实测最佳为准（A100：26B/31B 均 k=4），生产机重测；
同设备测过即记录，复见同设备直接取值。
〔第 7 站补记〕M5 预估修正两笔（用户指正）：① Ornith 误按稠密算——config 实锤
Qwen3.5-MoE 系（256 专家/激活 8+shared，hidden 2048，40 层，线性注意力 3:1 混合，
激活 ~3B）→ M5 4bit 解码与 26B 同档（~140-200 tps 级），非 21-24；② MLX 侧
MTP 存在——mlx-community 已有 26B/31B/E4B assistant bf16 转换件，**mlx-vlm 有
load_drafter(kind="mtp")**（12B @ M3 Ultra 实测至 1.72×），mlx-lm（纯文本）支持
缺口仍开、ollama PR 在途 → Mac 上 gemma 系投机红利可期（幅度待实机测，验证算
占比高于 CUDA，预期低于 3.12×）。
〔CLI 孤儿案破解 2026-08-08，用户令"先查 docs 再实证"〕CPU 会话零成本复刻：孤儿
成因=exec 404 时 CLI **自动清理本地状态**（官方 Recovery 文档明载），而 stop 鉴权
靠 sessions.json 里的 per-session JWT——token 被自清即孤儿（sessions 列 `[?]` 可见
但按名/按 ID stop 均 not found）。**自救=new 后即备份 sessions.json，孤儿时还原再
stop**（实证闭环 ✓）；备份缺席才需用户浏览器手杀。旧结论"必须手动"作废，纪律入
记忆（colab-cli-orphan-rescue）。12h 长稳（用户已拍板缩时版，~64u）可全程免人值守。

### 26B 12h 长稳测试站 ✅ PASS（2026-08-08～09，CLI 40G，两跑合计 ~14h≈75u）

**判决：通过**——二跑（`longrun2`）满程 12.2h，**313 有效请求零错误**（chat 220 /
tool 65 / longchain 19 / cancel 9）、零健康失败（146 采样 health 9.4ms 三段平）、
零报警。**无泄漏**：VRAM 起步一档上抬（37.6→38.8G，GPU 保活 0.5G+KV 池就位）后
全程字节级不动；服务 RSS 2720→2750MB 即平；宿主内存平；收官 `cache_count=0`
（TTL 回收+显式删除双路零残留）、`active_generations=0`（信号量干净）。**无漂移**：
tool tps 三段 143→134→147、chat 时长 1.70→1.74s 持平；tool TTFT 0.38→0.06s
（首次 schema 编译后缓存命中，复现 M8 定案）；chat TTFT 0.42→0.38→0.50 属预填
长度方差非单调劣化。取消双路（取消端点 7 + 纯断连 2）清理全净。
**一跑阵亡与三重加固**：一跑（`longrun`）1h40m 处 runtime 被回收——因果链=
Jupyter kernel 死（Colab 侧，不可控）→ CLI exec 404 自清本地状态**连坐杀死本机
keep_alive 守护**→ 会话失活被回收。二跑加固后扛满全程：① VM 侧 GPU 保活 0.5G 档
常驻（空闲窗不归零）；② 指标每 20min ssh 回拉本地（再亡最多丢 20min）；③ **自制
隧道保活 pinger**（复刻 CLI keep-alive 请求：`X-Colab-Tunnel: Google` 头 +
`authuser=0`，read-timeout=成功语义，OAuth 自动续签）。运维新知：**kernel 死 ≠ VM
死**（nohup 进程树全存活，tailscale ssh 照常）；kernel 亡后 exec 每试一次就再清一次
sessions.json（须停用 exec 转纯 ssh）；`restart-kernel` 对死 runtime 无效（Jupyter
面整体 404）；**还原备份的 stop 在 kernel 全程死亡的会话上实战成功**（孤儿自救
纪律两连胜），收 VM 干净零残留。产物：req.jsonl/sys.jsonl 已回拉本地留档。
**门禁结论：26B 生产形态（fp8+MTP k=4+约束+gen_sem）具备长时常驻资格**；
kernel 猝死属 Colab 平台层，不计入服务本体缺陷。

### 〔原启动记录〕26B 12h 长稳测试站（2026-08-08 启动，CLI 40G `longrun` 会话）

**目的**：常驻服务长时稳定性门禁——显存/宿主内存慢泄漏、KV/前缀缓存增长、
tps/TTFT 漂移、信号量/SSE 清理累积误差、挂死。**形态**：26B 生产形态
（fp8 在线量化 + MTP assistant k=4〔A100 已测最佳档，vLLM 嵌入共享热补丁在位〕+
约束/gen_sem 原样），装载后契约 19/19 ✓、MTP 冒烟 322 tps（≈基线 2.3×）✓。
**负载**：生活节律仿真非饱和——短聊爆发（门面多轮，流式为主）/工具轮（tool_specs
约束在位，缓存删除 70%+TTL 30% 双路磨）/长链（长预填+768 生成）/流式中途取消
（取消端点与纯断连五五开）/短长空闲窗（含 15-45min"离开"），全合成数据。
**仪表**：逐请求指标 req.jsonl + 5min 系统采样 sys.jsonl（VRAM/RSS/health/metrics）
+ 心跳；本机看门狗 20min 采样（停滞>75min/进程死/健康失败即报警，只报警不自愈）。
运维：new 后 sessions.json 已备份（孤儿自救纪律首次生产使用）；起跑时点
16:25Z，预计 04:25Z 收官后出漂移三分段判决。

### 文档瘦身波 ✅（2026-08-08，12h 长稳空闲窗执行，用户三点指示）

**① 账本瘦身**：EXECUTION_LEDGER 3141→~660 行——已收官历史细节（R0–R5/D 阶段/
B2c/七月各波，2026-07-09～08-05）**逐字迁出**至 `EXECUTION_LEDGER_ARCHIVE.md`
（零删改），主账本留 §R/§D/§2/当前活跃线/§5/§S/§6；「当前位置」巨行压缩为里程碑
摘要；§D 依据列指向改「归档 §3.x」。**② 漂移修缮**：README（config 空模板化描述、
目录树补 apple-app/api_tools/skills/colab、部署形态表与 -h 落盘语义对齐）；
HETERO_COMPUTE §S.1 A 线三行由"未开始"更新为实况（A1 完成/A2 基本完成/A3 进行中，
结论指向本账本）；ROADMAP 加执行现状注记（保留为方向依据，不再随执行更新）；
inference-speedup-plan 加 MTP 取代 e2b 路线的修订头；colab/README 补运维纪律
（Xet 分级/CLI 孤儿自救/顺序装载/看门狗）与 colab.env 四钥实况；backend/rag/
frontend 三 README 漂移由 Explore 代理全量核查（幽灵端点 /stream、退役 ui-state/
Workspace、审批/恢复路径、vl-2b 换档、24 个未文档化工具、cloud 出网通路零文档等）
→ chore-worker 照单修缮。**③ 剔除与治理**：删 `frontend/docs/frontend-refactor-
decisions.md`（2026-07-05 草案，分期全完成，协议已被 PROTOCOL.md 正式化，零引用）；
.gitignore 补 `backend/maintenance/`（drift_patrol 运行态）与 `rag/fts_index.sqlite3*`
（FTS 运行期索引）。金测试 676 全绿。

---

## §5 待决（开工前需拍板）

| | 问题 | 阻塞谁 |
|---|---|---|
| ~~**Q5**~~ | **已定 → D.13**：提取共享启动库，`devctl.sh` 与 app 都调；Swift 只做进程监护 | ~~R5-4~~ ✅ |
| ~~**Q6**~~ | **已定 → D.14**：整条音频路移 A（原问法有误：torch 是被 silero-vad 钉住的，不是 whisper——去掉 whisper 几乎不省空间） | ~~R5-2~~ ✅ |
| ~~**Q7**~~ | **已定 → D.16**：`AGENT_DATA_DIR` 旋钮，dev 默认仓库内、app 注入 App Support | ~~R5-4~~ ✅ |
| **Q8** | ✅ **已闭环（2026-07-16）**：Developer ID Application (G2) 证书由用户在门户签发（API 建不了——Account Holder 专属，见 S.4）；951M 运行时公证 **Accepted**（submission `1e13bf2a`）。签名→公证链路全通，真实 .app 的公证 + staple 留到分发动作 | R5-9 验收形态 |
| ~~**Q9**~~ | ✅ **已定（2026-07-18）：折中方案 a+开关**——`inference.kv_persist` 默认 true、用户可关（真两机拓扑介意残片即 false，退基座克隆）。server 三端点+RemoteBackend 三方法+删除清理已落，实机 HTTP 全链冒烟通过，详见 `HETERO_COMPUTE.md` §9.5 | ~~T2 分离编队收益~~ ✅ |
| ~~**Q10**~~ | ✅ **已定（2026-07-29）**：**多 port + 固定端口**。每模型实例一端口；单口网关否决（新单点 + 自维护成本 + SSE 经代理坑，且内部照样多 port）；"全动态端口"是伪选项（拨号侧总需固定会合点）。端口约定：14161 主推理档（D7），追加模型 14163 起顺延（14162 归 RAG 计算面），写死 `model_registry` 条目 `port` 字段=单一真相（host 同款"一键两职"）；`${VAR:-default}` env 逃生口对付罕见外来占用（141xx 不在 ephemeral 范围，OS 不会随机占）；将来需单一入口时可在多 port 上叠可选薄门面（T6 形态），基础形态不变 | ~~多端点 config 形态~~ ✅ |
| **Q11** | **实体链接层（entities/mentions/facts）与跨文档矛盾裁决**：数据架构采纳波（2026-07-30）明确**不预建**——设计文档自己的反模式第一条（开工把 schema 设计完）。开工条件：真实使用中出现聚合/算术/时间序列查询的 badcase 积累（检索评估集的 negative/multidoc 形态会先暴露），届时按 badcase 定表。同批延后：向量迁 sqlite-vec 单库重构（同库 JOIN 在链接层存在前无消费方） | 数据层 L5（无阻塞，等 badcase） |

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

### S.11 · bundle 的单文件拷贝不会自动带上新依赖（2026-07-18）

`bundle-macos.sh` 对 rag 数据面是**逐文件 cp**（只支持拆分模式、torch 不进包的刻意
设计）。上波给 `rag_server.py` 抽了零依赖伴生模块 `retrieval_extras.py`，打 build 54
时包里没有它——**包内数据面启动即 ModuleNotFoundError，而组装/签名/安装全程无警告**
（S.5「打包后的失败是静默的」的又一变体）。当场在安装验证一步抓到（用包内 python
实际 import），已补拷贝并重装。**教训**：给单文件拷贝的入口添加伴生模块时，打包脚本
是必须同步的第三处（代码 + 金测试 + 打包清单）；bundle 验收永远要用包内解释器实测
import/启动，不能只看装上了。

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
| ~~`CHARTER.md:167`~~ | ~~`mcp/（20 工具）+ agents/（4 沙箱）`~~ | **已修**（2026-07-29：26 文件 / 53 类 + 3 沙箱，随漂移矫正波） |
| ~~`CHARTER.md:239`~~ | ~~122 项金测试~~ | **已修**（2026-07-29：两处计数更新为 517，随漂移矫正波） |
| ~~`ARCHITECTURE.md`~~ | ~~开篇「四个组件」，缺 inference-server~~ | **已修**（2026-07-18 随 B2c 文档波：五组件 + rag 双面 + 服务表补 inference/计算面） |
| `MacAgent.docx` 图1 | 单机分层，无算力分离 / inference-server | 待重画（用户未答复过） |

---

*建立：2026-07-16 ｜ 依据：`ROADMAP.md` + 用户 Q1/Q2 答复 + `CHARTER.md` / `REARCHITECTURE.md` 实读 + 两侧代码实测。*
