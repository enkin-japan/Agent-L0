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

> 🟢 **2026-08-10：#79/#83 真机验收收官 ✅（用户 L4 场顺跑，26B NVFP4+fp8 KV）**。
> **#79 对话式注册 5/5**：26B 起草天气工具（auth 只引 WEATHER_API_KEY 变量名、零
> 内联密钥）、起草 weekly-report 剧本（正文成体）、内联密钥诱导负例守住（pending
> 区零泄漏）、确认放行 effect 正确（api_tool 无钥=unavailable 属预期/skill=
> registered 即时上架）、上架后剧本表/声明清单可见，验后清场。**#83 工具行为准则
> 4/4**：中文问欧盟 AI 法案→信源含 ec.europa.eu 等拉丁域（信源社区语言检索生效）
> 终答中文；时事不误用 rag_search；英文材料中文对话→docx 中文为主（cjk 0.67）；
> 图表标题/轴中文、数据分类名保原文（判分口径修正+准则灰区写明：never rename
> data values）。**验收途中揪出并修掉架构级脆弱点**：lxml 解析畸形页 double-free
> SIGABRT 带走整个 backend → trafilatura 抽取围堵进 spawn 子进程（07211c0）。

> 🟢 **2026-08-10（晚）：app 全天 401 事故结案（勘误+根修）+ 打包漏拷第二例 +
> /tasks schema 漂移**。**401 根因**（HANDOFF-20260810 §1 的孤儿收养叙事系误诊，
> 已就地勘误）：14:38 `install-macos.sh`（日用快装，纯客户端）覆盖了早上的全栈包，
> 裸包缺 Resources/backend → resolveLayout **静默退 repo 布局**——后端注 `.env.local`
> 钥、客户端发钥匙串钥，构造性 401；退出重开/重新生成/开关关开每条路都重走 repo
> 布局，故全部「无效」。铁证：/Applications Resources 仅三件素材文件，与裸
> xcodebuild 产物同时间戳同内容；external 叙事与实况「运行中」互斥（external 态
> 重新生成钮不渲染 SettingsView:219、状态文案「外部后端在跑」:409）。**处置**：
> 重跑 bundle-macos.sh 完整打包（Developer ID 签名，顺带消 ad-hoc ACL 疑云），
> 组装期加**执行层完整性断言**（缺 backend/app.py 等四件即组装失败）；实测 401
> 归零全端点 200。**漏拷第二例**：`rag/fts_index.py`（U 系列后加）不在逐文件 cp
> 清单，包内 RAG 数据面启动即 ModuleNotFoundError（与 retrieval_extras/S.11 同型）
> ——rag 拷贝改**全量自动入包只排 compute 两件**（宁多勿漏）。**/tasks 500**：
> 08-09 多 worker 化（46974d7）把 snapshot.running 改列表，schema（Optional[str]）
> 与 Swift/web 双端未迁——空闲 `[]` 撞 str 校验 500，Swift 端 try? 静默吞掉快照
> 刷新；三端对齐 `running: List[str]`（web 真值判断改 length，`!![]` 恒真坑）。
> 金测试 745、tsc 干净。**#85 根治清单誊入 DEFERRED §5**（探活带鉴权/布局显性化/
> external 重启选项/handleCrash 重读钥匙串/regen-from-failed 收养缺口/keygen
> --rotate）；待办：四钥全套轮换（对话两度见值）、lxml 围堵两隐患（挂死工人泄漏/
> 单工人超时级联）。教训入 §S：**同路径双形态安装脚本 = 布局静默切换面**。

> 🟢 **2026-08-10（晚）：iOS TestFlight 68 发布（7-29 的 67 以来首发）+ macOS 同号
> 68 装机**。iOS「连不上」排查结论：**分发滞后非原因**（协议自 7-18 零改动，67 兼容）
> ——白天连不上=Mac 侧 401 事故同根，之后=排查期「重新生成」轮换了 AGENT_API_KEY，
> iPhone 重填新钥即通（实机验证 ✅；tailscale serve → 127.0.0.1:14159 链路实测
> health 200/现钥 200/错钥 401）。68 随包带上：#84 挂起/重启三态、GlobalTasksResponse
> running 列表迁移（67 的 str? 解码在 try? 里静默失败，任务中心不刷新）。**发布途中
> 踩到 ASC 停收过期 beta**：27A5218g（7-3 装，beta 2/3 代）7-29 尚可直传，8-10 被拒
> 「Unsupported SDK」——beta 4 出后旧 beta 逐步失效；升 27A5228h 后直传成功。
> ⚠️ 复发预告：**每出新 beta/RC 这个门会再落一次**（9 月 RC 季必踩），发 TestFlight
> 前先对 developer.apple.com/news/releases 核对本机 beta 是否仍受收。

> 🟢 **2026-08-10（夜）：68 真机首波任务用户自测通过 + trash_file 挂死根修 +
> keygen --rotate/指纹化 + L4 收摊**。用户实测多任务顺跑，唯一暴露：**trash_file
> 两连败 30s 超时**（模型自救绕道 shell rm 完成删除，废纸篓语义丢失）。根因：
> 工具走 osascript→Finder(Apple Events)，bundle 的 backend 是脱管守护进程，TCC
> 「自动化」授权窗弹不出来，AE 请求干等到超时；且 Developer ID 换签后旧授权作废，
> 此路在 bundle 形态**结构性不通**。修法：主路改 NSFileManager.trashItemAtURL
> （PyObjC 随包在位，进程内 API 零 TCC、保留「放回原处」），osascript 降级为
> PyObjC 缺席兜底；真目录冒烟实测入废纸篓 ✅，金测试 745→**746**。同类风险登记：
> apple_script/organizer 等凡走 AE 控他 app 的工具在 bundle 守护形态同样弹不出
> 授权窗（触发制观察，UI 侧引导授权归 #85 域）。**keygen 升级**：--rotate（强值
> 强制换新）+ 默认只打指纹（sha256 前 8，--show 显式看明文——值进终端即可能进
> 对话/日志，正是本次四钥暴露的通道）。**L4 会话收摊**：agentd-0810 全天 6.6h
> ≈10.2u，keepalive/tunnel 双守护清净。四钥轮换本体被会话安全闸拦（轮换操作须
> 用户亲手），命令已交付；Drive 正主 colab.env 的 INFER 同步待下次浏览器场前做
> （CLI 场用本机副本，daily.sh:53 上传即生效）。

> 🟢 **2026-08-10（夜二）：晚间 VM 拉起「启动过长」= 两故障叠加，双双根修**。
> ①**依赖漂移崩**：bootstrap pip 无 pin，新 VM 拉到刚发布的 transformers 5.15.0
> ——其对 gemma 系逐层异构配置强抛 AmbiguousGlobalPerLayerAttribute，vLLM
> `get_head_size` 全局读 head_dim 启动即炸，supervisor 六连重启后放弃
> （SUPERVISOR_GAVE_UP，20:27）——「不是慢，是早死了」。VM 上降级 5.14.1 复活
> （server 就绪实证 200）；bootstrap 钉 `"transformers<5.15"` 并注明解钉条件。
> ②**tailnet 释名竞态**：旧 ephemeral 节点停机后未即时 GC，新 VM 被迫领
> `colab-a100-1` 后缀名，而拨号侧全部钉死 colab-a100——就算 server 活着探活也
> 永远脱靶；且**机器名注册后不随 hostname 自改**（set --hostname 实测无效），
> 只能管理台手工正名。根修：daily.sh stop 终止前先 ssh 进 VM `tailscale logout`
> **主动释名**（节点即刻删除、名字即刻回收，下台 VM 恒拿正名；VM 已死则退回
> GC 慢路）。与上午 401 同谱系教训：**「名字对、实体错」**——401 是端口对身份错、
> 本案是域名对节点错，#85 的实例指纹思路对两者通杀。本台 -1 节点待用户管理台
> 一键正名（改完拨号侧零改动）。

> 🟢 **2026-08-10（夜三）：轮换收尾 + 上线前挂账清扫（#85 主体落地）**。轮换闭环：
> 用户重填 app INFER 钥即通（全端点 200）；Drive 正主 colab.env 旧钥挂账入持久
> 记忆（下次 80G 浏览器场前必同步，CLI 场不受影响）。**#85 完整版 1+2+5+6 落地**
> （84e76d8，implementer 施工+主会话审毕）：/health 实例指纹（service/layout/
> key_fp）+ app 四态探针收养前验钥 + 布局形态显性化上设置页 + AGENT_LAYOUT 双侧
> 自报；余 3/4（external 接管、handleCrash 重读钥匙串）按触发条件缓做。**lxml
> 围堵两纪律补齐**：回收补刀 kill（不再每超时漏一僵尸工人）+ 提交串行化（排队页
> 不再在死人队列里烧自己的 30s）；真击杀金测试 2 例。**code_explore PATH** 借
> shell_exec 登录 PATH 缓存（守护形态 npm 装的 codegraph 此前不可见）。
> ARCHITECTURE.md 补 §2.1 密钥拓扑与安装形态。金测试 746→**757**。
> **暂停轮发送确认**（#84 随案 UX 死角）：待审批/挂起态发新消息=机械作废旧轮
> （后端语义正确不改），app 侧此前零提示——ComposerView 发送前分态弹确认
> （「放弃并发送」destructive/取消留草稿），web 补待审批分支（输入区本禁用，
> 属兜底）；三语文案齐，双平台 BUILD SUCCEEDED、tsc/vitest/夹具 7/7 净。
> 另 #84 的 shell 登录 PATH 缺口经查已在 71c1a01 修复（bundle 已带上）。
> 生产机迁移准备按用户指示搁置（未上线）。

> 🟢 **2026-08-10（夜四）：重打包装机 + Q12/Q13 双结案 + Sparkle 拍板搁置**。
> **重打包**：bundle-macos.sh 全量重装（v0.16.0/1.2G，Developer ID），/health 实测
> 自报 `layout:bundle`+key_fp——#85 在生产形态首证。**Q13**：31GB 开源副本核验
> （远端逐字一致）后删除。**Q12 实测否决**：MTP 上 L4 净减速 -17%（48.5→40.3 tps）
> 且 KV 池腰斩，数据入 §5 行内；实验全程哨兵静默、结束回滚基线并复核。测量教训
> 一并入账：裸 /v1/generate 无模板测质量=模板伪象垃圾，险些误判采样损坏——质量
> 判定必须走 chat/completions。**Sparkle 分发**：用户拍板搁置
> （一两台设备收益不大）；方案留档=公开 releases 仓或 tailscale serve 自托管 feed，
> 前置为公证+staple（Q8 尾巴）+EdDSA 钥。**排障纪律新坑**：ssh 远程命令串含
> 目标进程名时 pkill -f 会自匹配杀掉自己的 shell（两次实撞）——杀进程一律
> 拼接串（`"serve_superv""isor"`）或走独立 ssh 会话。

> 🟢 **2026-08-11（凌晨，用户睡前布置）：31B NVFP4×MTP 40G 实测——成立，~1.7×**。
> 补齐 MTP 矩阵最后一格（稠密抗量化假设验证）：A100 40G、RedHatAI/gemma-4-31B-
> it-NVFP4（22G，与 26B 件同发行方同装载路）+ google/gemma-4-31B-it-assistant
> 草稿（927M）。同口径 chat 3-4×350tok：基线 **37.2 tps**（稳态 39.6）→ MTP k=4
> **65.7 tps**（57.8-87.3）= **1.66-1.77×**，质量完好（开头与基线逐字同、目检全对）；
> KV 108k→87k tok（bf16 KV，A100 无 fp8 KV）。**结论组合成立：稠密件量化后 MTP
> 红利保留大半**（fp8@80G 3.12× → NVFP4@40G ~1.7×，差值含卡带宽与接受率两因子），
> 与 MoE NVFP4@L4 的负收益形成完整对照——**31B NVFP4+MTP 让"质量王"上 CLI 可开
> 的 40G 档跑出 ~66tps 可用速度**（80G 浏览器场之外的新选项，费率 5.3u/h 同 40G）。
> **途中双修**：①transformers<5.15 钉漏了 daily_serve_chain.sh 自带的第二条 pip
> 行（昨晚只钉 bootstrap.sh，新场照崩 head_dim——双份安装行必须同钉，S.13 同谱系），
> 已补钉；②daily.sh stop 假警报（CLI ANSI 码致 grep 失配）修掉 + 释名失败改留证
> release.log（两连败无迹可查）。会话 0.9h≈4.8u，VM 已关停清净。

> 🟢 **2026-08-11：MTP 矩阵理论收口 + 生产机测试计划入账（§7）**。四点实测
> （fp8稠密@80G 3.12× / NVFP4稠密@40G 1.7× / fp8MoE@40G 1.92× / NVFP4MoE@L4
> -17%）拟合出解释模型：红利=目标解码成本×卡余力，接受率随目标量化劣化
> （8bit系 a≈2.7-3.4 vs 4bit系 ≈1.9，NVFP4 logits 偏离草稿 bf16 训练分布）。
> 上下文维度：混合注意 1:5+滑窗1024 把 KV 税钉低（64k 仅 -20~30%），MTP 批量
> 验证再摊薄——"上下文拉近 26B/31B 差距"的假设被数据否定（31B 的 KV/token 肥
> 4 倍自缴重税）。M4 Max 546GB/s 理论推算：**草稿 4bit 化是 Mac 上 MTP 成立的
> 前置**（bf16 草稿反亏）、8bit+MTP≈4bit 同速白赚精度（26B/31B 双档同构）、
> H 每压 1ms MTP 形态 +15-18%。**用户拍板：生产机到手后按 §7 全矩阵实测**
> （量化档×上下文×MTP草稿形态×TurboQuant KV 量化，四阶段：回归提取 H/B_eff
> → 接受率与草稿形态 → KV 量化速度+质量双测 → model_eval+24h 门禁定档）。

> 🟢 **2026-08-11（续）：Mac 并发形态 sizing 讨论收口（用户审定入 §7/§5）**。
> 40G 共驻 **B 方案拍板**（31B NVFP4+MTP+48k+vl-2b 8bit+whisper，
> `max_concurrency=1` 为隐含前提；`a100-31b` 档位设计已定**待施工**，随档补
> vl-2b 8bit 召回回归复核——rerank_min_score 0.3 是 fp16 分布标定值）。
> **Mac 端确认要做并发** → §7 加 0) 工程前置两轨制（引擎内连续批处理为最重项、
> mlx-vlm 升级手术为依赖链上游第二重项——本栈全建在 mlx_vlm 上钉 0.5.0，
> `load_drafter` 在新版，现 backend drafter 零接线）+ 并发 B_eff/MoE 并集税/
> MTP×批处理/并发流取针/64G-128G 分界与双模型共驻等增量格；Q14（CHARTER §3
> 修订案）挂待决。**勘误随案**：①"双人均分带宽路均砍半"说法错误——解码三分量
> 对批处理反应不同（权重可摊薄/KV 不可摊/MoE 专家并集反摊薄），修正后双人路均
> 31B dense ~85-87%、26B MoE ~55-65%，"双人唯 26B"结论反转为"差距收窄、
> dense 是批处理优等生"；②工程前置误引 mlx-lm，实栈为 mlx-vlm（用户指正）。
> sizing 结论：kv8 下两套配置足迹殊途同归（53-63G 窄带），64GB=单人并发下限、
> 128GB=双人并发起步+双模型共驻白拿；冗余系数 20%（MLX 无抢占安全阀）。

> 🟢 **2026-08-13（夜）：§7 阶段一基线格当日收官——35/36 格 + 回归 + 四发现**。
> 序架 bench_baseline.py 两版定型（v1 全格跑完验尸出缓存复用 bug→v2 每 run 独立
> 缓存重测全场），结果 `benchmarks/baseline/20260813-m4max64/`（JSONL+ANALYSIS.md）。
> **速度**：26B-4bit 123→63 tps（0→64k）全程压 e4b；26B-8bit 80→49；31B-4bit
> 22.6→15.9；31B-8bit 13.9（@64k 稠密 KV Metal OOM 实证=必配 kv8）；31B prefill
> 仅 ~155-200 tps（TTFT 424s@64k，长上下文必须前缀缓存）。**回归**：单一全局 H
> 证伪（§7-5 预期内）——真实结构=逐模型 H（e4b≈0.2 / 26B≈3 / 31B≈13ms）+ 边际
> B_eff 515GB/s（31B 对，标称 94%）；31B 大 H = MTP 红利上修依据。**KV 税实测
> =天真带宽账 5.6-8.4×**（注意力核实效 ~65-100GB/s）→ 阶段三 kv 量化优先级上调。
> **四发现**〔⚠️ 2026-08-14 大幅推翻，见 §S.16：A 与 D 同为我方 prompt bug〕：
> ~~A) 26B-4bit×抑制预填×≥4k = 贪心秒终止~~ **撤销**（真因=抑制预填多一个
> 尾随 `\n`，修复后全档正常；26B-4bit 恢复候选资格）；B) 31B 短上下文裸 prompt
> 秒终止/明文漏思考——**2026-08-14 重跑后不复现，撤销**〔警示：该重跑同时换了引擎栈（0.5.0→0.6.13），是双变量变更，不像发现 A 有单字节 A/B 背书；已读 31b 0/4k 的 count 样本确为干净「一,二,…,十」无思考泄漏，故撤销大概率成立，但严格说是「不复现」而非「已证伪」〕；~~C) e4b 长上下文工具
> 服从性崩塌~~ **撤销**（§S.17：工具声明用 JSON 而非官方 DSL，换 DSL 即干净调用）；
> ~~D) §S.14 缓存复用引擎 bug~~ **结案**（同 §S.16，引擎波缓存手术整项取消）。
> **四发现无一幸存，全部是我方 prompt 拼装层的字节偏离。**
> ~~26B-8bit = 唯一全 35 哨绿档~~ → 公平口径重跑后**全绿档有三个**
> （26b-8bit 18/18、31b-4bit 18/18、31b-8bit 15/15），且各档差异全落在被污染的
> tool 一列，剥掉后普遍打平——**缺省档之争本波无法裁决，留 model_eval**
> （详见 `20260814-sentinel-rerun/ANALYSIS.md`）。config 投机采样已还原
> prompt_lookup〔2026-08-14 因 §S.15 再次拨 off〕。
> 随场杂修：本机 SSH 钥未注册 GitHub 致 push 断（旧机身份迁移失效），gh 令牌
> HTTPS 过渡→用户注册 enkin@studio 钥后已切回 SSH。

> 🟢 **2026-08-13（续）：§7 第 0 批（环境就位）当日收官**。①金测试 757 全绿
> （50.6s，venv 随卷迁移完好）；②MLX/Metal 实证通过（mlx_vlm 0.5.0 钉住确认）；
> ③全栈冒烟：RAG/SearXNG/backend/前端四服务全绿，inprocess+e4b 真实轮次
> success、**gen 64.6 tps**（前缀缓存命中 15072/15161；验完收摊、config 已还原）；
> ④基线格权重六锚点齐：e4b/26B/31B × 4bit/8bit 全下齐（mlx-community 件，
> ~83G 新增，看门狗零报警）；⑤`scripts/wired-limit.sh` 落地（status/set/install
> 含 LaunchDaemon 持久化）。**三挂账当日全清（用户逐项处置）**：①wired limit
> 用户亲手 install ✅（57344MB 达标 + LaunchDaemon 已装）；②31B bf16 残件确诊
> =22G 断点缓存+12G 孤儿分片，只清权重垃圾 34G→31M，**tokenizer-only 件保留**
> （registry `google/gemma-4-31B-it` remote 档 edge 侧分词器在用，离线解析复验
> 秒过）；③HETERO_COMPUTE.md 断句删行确诊误删（全树 diff 仅此一处单行删除、
> 删在句中）git checkout 还原，树净。下一波=阶段一基线格 bench 序架
> （server 形态 + chat 模板路径，见 §7-1 口径）。

> 🟢 **2026-08-13：生产机落地——本机即生产机，M4 Max 64G Mac Studio**。
> 实核（sysctl + system_profiler）：Mac16,9 / Apple M4 Max / 64GB——64G 配置
> 即 546GB/s 满血带宽，正是 §7 理论表的原生口径，**阶段四「换算到实际机型带宽」
> 一步免除，预测表零换算直接对表**。定位=sizing 结论的「单人并发下限档」：
> 单模型 8bit 单用户全场景可行——26B-8bit 全栈足迹 ≈44G（含 20% 冗余，64k 无压力）；
> 31B-8bit ≈53-54G@64k（需 kv8；KV 稠密则 ≤32k）——两者均要求先上调 wired limit
> （实测现值默认 0 ≈ 75%/48G 上限，§7-0 ⑥ 升为到手第一批事项）。可行域裁边：
> 阶段一 bf16 回归锚点格划除（31B bf16 权重 ~62G、26B bf16 ~54G 均越限），
> 锚点改为每模型 4bit/8bit 两点 + e4b 小档；阶段四 128G 专属项（双人并发、
> 双模型共驻）转「等未来设备」存查，本机只验 64G 侧分界。旧 16GB 开发机纪律
> （e4b-only、大模型必上 Colab）随设备退役——本机可直跑 26B/31B MLX 档，
> Colab 仅剩 CUDA 特有形态（vLLM/NVFP4/CUDA MTP 对照）用途。

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
| ~~**Q12**~~ | ❌ **已实测否决（2026-08-10 夜四，L4 真机，chat 路径同条件 3×350tok）**：MTP 上 L4 **净减速**——基线 48.5 tps（42-56）vs MTP 40.3 tps（38.6-41.3，**-17%**），且 KV 池腰斩（166k→78.5k tok，64k 并发 2.54x→1.20x）、temp0 输出出现轻微措辞漂移（fp8 KV×投机数值非确定性；质量无恙）。预计 +30-50% 不成立：A4B 活性参数小+NVFP4 解码本就快，L4 58 SM 扛不动草稿+验证开销。另录：去 fp8 KV+32k 形态引擎 KV 定容步初始化即败（监理五连崩 GAVE_UP），此路也不通。A100 80G bf16 形态（1.92×实绩）不受影响。已回滚基线定形。**排障插曲**：裸 /v1/generate 无模板测质量会得退化垃圾（模板伪象），一度误判「采样被破坏」——测质量必须走 chat/completions 或带真模板 | ~~已结案~~ |
| ~~**Q13**~~ | ✅ **已清理（2026-08-10 夜四）**：31GB 副本核验后删除——remote=github.com/enkin-japan/MacAgent（已开源），HEAD 1b2c14f 与远端 main 逐字一致、树净无 stash。Spotlight 多副本索引源随之消除 | ~~已结案~~ |
| **Q14** | **CHARTER §3「单 worker 串行」修订案**：措辞已起草并经用户预审（全文存 §7-6），**进宪门禁（用户拍板 2026-08-11）= 生产机并发实测通过——吞吐有提升 + 零并发事故**（§7 阶段一并发格 + 阶段四门禁）。此前旧文「单 worker 串行」继续有效，不改 | 生产机 §7 实测 |

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

### S.12 · 同一安装路径的双形态安装脚本 = 布局静默切换面（2026-08-10）

`install-macos.sh`（日用快装，纯客户端）与 `bundle-macos.sh`（全栈包）都覆盖
`/Applications/MacAgent.app`，产物布局不同而 UI 无从分辨。快装覆盖全栈包后
`resolveLayout` 的 bundle 判据静默失败退 repo 布局——**密钥来源随之换轨**
（Keychain → `.env.local`），app 客户端与后端从此异源，全端点 401 且一切「重启/
重开/轮换」修法都在同一条错误布局里打转（全天事故，HANDOFF-20260810 §1 勘误版）。
前会话按「孤儿收养」误诊排查半天，实际两分钟的 `ls Resources/` 即可证伪。
**教训**：①布局分辨优先——「app 行为异常」先看装的是哪种形态的包（`ls
/Applications/MacAgent.app/Contents/Resources/backend` 一眼定案）；②同路径多形态
安装是结构性陷阱，已在 bundle-macos.sh 加执行层完整性断言兜打包侧，UI 亮出布局
形态（DEFERRED §5 #85-2）是消费侧的另一半；③排查密钥对不上时，先问「两侧各自
从哪里读」再比值——**来源异轨时值永远对不上，比值是白比**。

### S.13 · 逐文件拷贝清单第二次咬人后升级为反向排除（2026-08-10）

S.11 的同型复发：`rag/fts_index.py`（FTS 关键词索引，U 系列后加）没进 bundle 的
逐文件 cp 清单，包内 RAG 数据面启动即 ModuleNotFoundError——好在编队就绪门拦下
backend、app 原话上报，实机当场定位。**同一清单第二次漏拷 = 清单机制本身错了**：
已改为 `rag/*.py` 全量入包、只点名排除 compute 两件（torch 依赖）——多带纯 py
无害、漏一件致命，**白名单会随代码演进腐烂，反向排除只在「刻意不带」处设卡**。

### S.14 · MLX 前缀缓存复用 × 幽灵思考抑制预填 = 瞬停/窜标记（2026-08-13）

基线格 v1 实测：同 cache_id 复用（前缀命中→trim→增量 prefill）后，26B(MoE) 下一
请求**首 token 即 EOT**（gen_tokens=1，temp0 确定性复现），31B 轻症（输出头部窜出
孤立 `<channel|>`）；e4b（无 suppress_ghost_thinking）完全免疫——所以开发机时代
从未暴露。同 prompt 换全新缓存即恢复正常（26B-4bit 的 QA 秒停除外，当时归给
「发现 A 的模型级问题」——**该归因亦已撤销，同为 §S.16 的 prompt bug**）。**生产 Mac 跑 26b/31b inprocess + kv_persist/前缀复用 = 必踩**。
疑点集中在 trim 边界与抑制预填（`<|channel>thought\n<channel|>\n`）的 token 对齐。

✅ **2026-08-14 结案：不是引擎 bug，与发现 A 同因——抑制预填多一个尾随 `\n`
（§S.16）**。当时写下的疑点句里其实已经把凶器抄在纸上（`…<channel|>\n`），却因
先入为主认定「引擎缓存路径有鬼」而没去和官方模板逐字节对表。修复后复验（同
cache_id 连发两请求、命中前缀→trim→增量 prefill）：26B-4bit（原重症）与
31B-4bit（原轻症）在 4k 上下文下**均正常**（gen_tokens=4，无窜标记）。
**原定的引擎波「铁律 1 缓存路径手术」整项撤销**；`kv_persist: true` 维持生产开启。
遗留：本次复验覆盖 4bit 两档 × 4k，v1 形态（0-64k 全档 × 各量化）的完整回归
随下一轮基线格重跑自然覆盖。

### S.15 · mlx-vlm 0.6.x 升级后旋转滑窗回滚失真——投机采样正确性锚点碎裂（2026-08-14）

升级 0.5.0→0.6.13 手术中发现，两层：① **良性层**：S>1 验证批前向 vs 逐 token
前向出现 ~0.5-0.9 logits 形状相关数值抖动（argmax 全保；疑为新 `_make_masks` 的
"causal" 字符串掩码切换 SDPA 快路径改变累加序），自由生成近平局 token 可被翻转
（parity B 型 0.125 分歧）；② **实质层**：`rollback_speculative_cache`（trim）
在**旋转后的 RotatingKVCache** 上产生 max|Δ|=22 的 logits 失真（旧栈同探针
KL≈5e-4；短上下文未旋转态仍逐位无损 Δ=0）。RotatingKVCache 类字节级未变
（0.6.13 vendored 份 == mlx_lm 0.31.3 份），变的是 gemma4 `_make_masks` ——
**trim 后的旋转缓存几何 × 新掩码路径**才是伤口。端到端裁决实验（1290 token
前缀深旋转 × 自由生成低命中）：@41 实质分歧 logprob Δ2.125，且该域投机倒亏
（0.89x，接受率 4%）。处置：生产 speculative.mode 拨 off（正确性>速度）；
**根修列 MTP 接线波头号项**——MTP 共用同一回滚原语，不修则 MTP 波无从谈起。
教训：引擎升级的复验矩阵必须含「数值级探针」（parity/rollback），金测试
（FakeEngine）与装载 smoke 对这类分布级失真完全盲。

**✅ 根修完成（2026-08-14，`patch/rotating_cache_trim.py`）——兼原始定性勘误**

**伤口不在 `_make_masks`**（上文那句归因错了，据此保留原文并在此更正）。在
`RotatingKVCache` 自己的两个方法上：

    def is_trimmable(self): return self.offset < self.max_size   # 旋转后 = False
    def trim(self, n):      self.offset -= n; self._idx -= n     # 只动记账，不搬内容

「只递减计数器」在**未旋转的线性布局**下无损——`offset` 完全决定有效前缀，物理
缓冲区多大都无所谓（实测短上下文 max|Δ|=0.000000）。`is_trimmable()` 正是用来声明
这个前提的，而 `rollback_speculative_cache` **无条件**调 `trim()`、从不问。旋转态
逐步几何（实测）：

| 步骤 | offset | _idx | buf |
|---|---|---|---|
| prefill 900 | 900 | 900 | 900 |
| 喂 A+R（8） | 908 | 519 | 519 |
| **trim(5)** | 903 | **514** | **519**（内容一字节未搬，R 仍物理驻留） |
| 喂 probe | 904 | 1 | 512（`_update_in_place` 前裁 7 并把 `_idx` 重算成 512，
回滚那次递减被整个丢弃） |

后果不是「5 行被拒草稿泄漏」，是**环形对齐整体错位**：512 个槽位有 **511 个**与
基线路径不一致，掩码怎么算都对不上。

**修法**：`_update_concat` 结束时恒有 `_idx == keys.shape[2]`（缓冲区处于时序序），
投机验证批 block_size≥2 走的正是 concat 路，故旋转态的正确 trim 是
**`_temporal_order()` + 物理切尾**。实测：稠密短 / 稠密长旋转 / TurboQuant
kv_bits=3.5 长，三口径 max|Δ| 全部 **0.000000**；10 轮累计回滚 KL=0.000000、
top20 20/20（残差 max|Δ|=0.625 属良性层——验证批 8 宽 vs 干净批 3 宽的形状差）。

**可裁判据（首过评审 Critical#2 修正）**：判据是「**有没有 token 被淘汰过**」
（`valid == offset`），不是「切完还剩不剩 max_size」。刚跨窗但尚未淘汰任何 token 时
切到 max_size 以下完全安全（offset 同步降到窗下，回到线性布局）；我第一版只看
后者，把这个无损形状误判成不可裁。

**不可逆的那条路，从调用侧消除（首过评审 Critical#1）**：`_discard_block` 走
`accepted=-1` 即 `trim=block_size` **整块**作废（我原以为恒有 `n ≤ S-1`，错），
切完只剩 `max_size-1` 条，凑满窗所需的最老 token 已被 `_update_concat` 的前裁淘汰、
不复存在——**语义上无法回滚**。处置：`tb` 在函数入口绑定一次全程不重绑，故
「**有 thinking 预算就不预发**」使「pending 非空 ⟹ tb is None ⟹ 强制收束分支
不可达 ⟹ `_discard_block` 不可达」成为不变式。代价是有 thinking 预算的请求失去
一层流水重叠；若实测该重叠值钱，正解是预发前快照将被淘汰的那几行 KV，另案。
原语侧对该形状**显式抛错而非降级**——不变式破了要响，静默降级正是本条的成因。

**教训（在上面那条之上）**：**上游用某个谓词声明了前提，就要去查有没有人绕开它
调用。** `is_trimmable()` 把话说得很清楚，缺的不是知识而是「谁在不问就干」这一查。
另：我这次的三条推导有两条是错的（`n ≤ S-1`、余量判据），都是评审用 CPU-only
复现打掉的——**几何类改动的推导必须落成可执行的复现，不能停在纸面**。

### S.16 · 模板预填多一个 `\n` = 两周的「模型缺陷」幻觉（2026-08-14，本项目最贵一课）

我方 PromptBuilder 的幽灵思考抑制预填吐 `<|channel>thought\n<channel|>\n`，官方
chat_template 是 `'<|channel>thought\n<channel|>' -}}`（`-}}` 还显式吃空白）——
**只多一个尾随换行**。后果全是「看起来像模型/量化问题」的怪象：
- **26B-4bit ≥4k 上下文 QA 首 token 即 EOT**（原「发现 A」，曾定性为「PTQ 4bit
  分布损伤」，据此把 26B-4bit 判为「速度王带毒、不宜缺省」）；
- **12B 输出退化成 `<|channel>:: <|channel>::` 循环**，并被误判为「无视抑制预填」；
- QAT 波据此得出「QAT 把毒阈值从 4k 推到 16k、只解半程」——真相是 QAT 件对这个
  畸形 prompt 更耐受，与 QAT 本身的质量收益无关。
一字之差的对照实验（同 prompt、削掉尾随 `\n`）：0/4k/16k 全部正常答出 H2O。
**教训**（比结论重要）：
1. 「模型/量化有毒」是极高门槛的结论，下之前必须先把 **prompt 字节与官方模板
   逐字节 diff**——我们连续两波（基线格、QAT）在同一个畸形 prompt 上做对照，
   两个档位一起中毒时误以为是量化规律，其实是共因；
2. 质量哨发现异常时，第一嫌疑人是**我方拼装层**，不是权重；
3. 模板是**字节协议**不是文本格式：空白不可自由裁量，`-}}`/`{{-` 的空白控制是
   语义的一部分。凡自建 prompt 拼装（不走 jinja）就必须有逐字节对表测试。
处置：prompt_builder 三处发射点全部对齐官方字节（生成头 + 历史重放 ×2）；
发现 A 撤销、QAT 结论重写、26B-4bit 恢复候选资格（见 §7-2）。

**同波续查（reviewer 二审驱动，同日）**：
- **轮头角色名同类偏差**：官方模板 `role = 'model' if role=='assistant'`，我方一直
  发 `<|turn>assistant`。实测（26B-4bit × 0/4k/16k/32k）两种写法输出**均正确**
  ——此项与尾随 `\n` 不同、无实害，但仍按字节纪律改齐（"我测的档位没事"正是
  当初对多余 `\n` 会说的话）。副作用提醒：轮头字节一变，已持久化的 KV 与
  CUDA 侧 A100 画像口径同步作废。
- **补了字节回归测试**（`test_dialect.py` 三条：抑制预填字节精确、轮头角色名、
  生成头/重放同字节）。**测试当场抓到我修轮头时自己引入的 bug**：把 `role`
  覆写成 `"model"` 后，下游 `elif role == "assistant"` 的重放抑制分支静默失效
  → 历史重放不再吐空频道、KV 前缀断裂。这三条测试写完不到十分钟就还清了成本。
- ✅ **CUDA 侧口径对齐（2026-08-14 用户拍板：补 flag、重跑、排在 Mac 之后）**：
  google 31B/26B bf16 + RedHatAI 26B NVFP4 三件同为 gemma4 方言、官方模板本就
  带抑制，却从未设 suppress_ghost_thinking——A100 侧一直跑在**无抑制**口径上。
  **flag 已补齐（config 三条）**；已回填画像（31B tool 0.96/long 0.833/tps 43、
  26B tool 0.92/long 0.833/tps 157 等）**均属补 flag 前口径**，标注保留但不撤下
  （撤下=CUDA 主档失去优先选中资格，弊大于利）。**重跑排期见 §7-7**（优先级
  在 Mac 侧全部工作之后，且不为它单独起 A100 场）。
- 待办（同类风险仍在树上）：〔2026-08-14 晚 §S.17 波已修：tools 场景 `<turn|>`
  双闭合、多开 system 轮头、工具间换行、声明重复发送/丢失、工具观测重放出现两次〕
  **仍未修**：`enable_thinking=True` 头部与官方 `<|think|>` 形态不一致；
  `_build_tool_calls`/`_build_tool_responses` 块间仍多一个 `\n`（官方紧邻）；
  role=="tool" 我方另开 `<|turn>tool` 轮而官方不开；正文与 tool_calls 的先后
  次序与官方相反；`sot_token` 兜底值写成 `<|turn|>`（真值 `<|turn>`，且金测试
  的 _Tok 用空 special_tokens_map，测的正是这个错兜底）。后四条与工具协议
  迁移（§7-8）同域，建议并波做。

### S.17 · 工具声明格式整个是另一套——「模型不听话」的第三次误判（2026-08-14）

§S.16 的同族第二例，且更深。官方 Gemma-4 的工具**声明**是紧凑 DSL：
`<|tool>declaration:名字{description:<|"|>…<|"|>,parameters:{type:<|"|>OBJECT<|"|>}}<tool|>`
（多工具紧邻无分隔、全在同一 system 轮内、末尾单个 `<turn|>`）；本栈递的是
`<|tool>{"name":…,"parameters":{…}}<tool|>` **JSON**，外加另开一个 `<|turn>system`
轮头与 `<turn|><turn|>` 双闭合。

**A/B 实证（同模型同问题，只换声明格式）**：12b-4bit 我方 JSON → 吐
`<|channel>thought<channel|>` 无限循环不调用；官方 DSL → 干净地
`<|tool_call>call:get_current_time{}`。e4b-8bit@4k 我方 JSON → 幻觉"我已经获取了
当前时间"；官方 DSL → 正确调用。隔离实验排除约束采样（开/关 `tool_specs` 都循环）。

**后果**：「发现 C：e4b 长上下文工具服从性崩塌」**撤销**——不是模型不听话，是我们
用它没见过的格式递工具；12B 工具场景当前不可用同理。**至此阶段一四个发现
（A/B/C/D）全部是我方 prompt 拼装层的字节偏离**，无一是模型或引擎缺陷。

**已修**：结构缺陷三处（轮头/双闭合/工具间换行）。声明体 JSON→DSL 的整体迁移
经用户 2026-08-14 拍板后当日落地并**全量验收通过**（59 格 × 3 判据 177/177，
tool 判据翻正 24 格、零回退），见 §7-8。

**教训（在 §S.16 之上再加一条）**：字节对表不能只对"我改过的那几行"——
S.16 修完抑制预填就收工，结果同一文件里更大的偏离（工具段）又多活了半天。
**对表要按「模板里所有会出现的结构」逐项过一遍**，而不是哪里报错查哪里。

### S.18 · 探针与我方注入的上下文语义打架——测的不是你以为的东西（2026-08-14）

**现象**：§7-8 迁移后跑冒烟，12b-4bit 的工具哨在 ctx=0 通过、ctx=4096 挂：模型吐
`<|channel>thought\n<channel|>` 无限循环，不发调用。第一反应是"迁移没修干净"。

**排查**：拿我方 builder 的 prompt 与 transformers 渲染的官方模板做单变量 A/B——
两份只差我方在用户轮与生成头之间插的环境锚点轮
`<|turn>system\nCurrent date: 2026-08-14 (Friday)\n<turn|>`。**去掉它就干净调用。**

到这里像是"可变尾部位置"的架构问题。继续隔离（12b-4bit @4k，单变量各跑一次）：

| 变体 | 结果 |
|---|---|
| 无环境块（官方原样） | ✅ |
| 环境块 = 尾部 system 轮（现状） | ❌ |
| 环境块 = 尾部 user 轮 | ❌ |
| 环境块并进用户轮正文 | ✅ |
| 环境块提到用户轮之前 | ✅ |
| **尾部 system 轮但内容无关**（"请仔细作答。"） | ✅ |

换内容就通 ⇒ 不是位置的错；换位置也通 ⇒ 不是内容的错。是**这一对**的组合。
再验冲突假说：

| 组合 | 结果 |
|---|---|
| 时间工具 × 尾部日期锚 | ❌ |
| 时间工具 × 尾部无关句 / 尾部地点锚 | ✅ ✅ |
| **天气工具 × 尾部天气锚** | ✅ |

**定性**：工具哨要模型"调 get_current_time 查当前时间"，而我方在生成头前塞了
`Current date: …`。模型被告知没有时钟、又被递了当天日期，在思考频道里空转。
连"天气工具 × 天气锚"都通，说明不是"锚点满足了请求"的泛化效应，是日期/时间
这一对特有的冲突（日期锚只给了日期、任务要的是时间，矛盾更尖锐）。

**两条后果**：
1. **测量口径**：旧工具哨在 ctx>0 上测的不是工具调用能力，而是"模型顶不顶得住
   我们自己塞进去的矛盾"。探针已换成天气工具（顺带带一个字符串实参，正好压到
   紧凑 DSL 的 `<|"|>` 原文块编码路径），判据加严为"发出调用 **且** 实参到位"。
2. **生产隐患——经采样口径复核后降级为「低」（2026-08-14 当日追加，见下）**。

**追加：社区/官方检索 + 采样口径复核（同日，结论修正了上一条）**

联网核对后确认，这个空转本身是**已知的模型级缺陷，不是我方独有**：
- HF 讨论 #41 报的就是本症状（`thought\n thought\n …` 退化循环）。Google 与社区
  的调查结论是**权重级吸引子**——F16 全精度下同样复现（~38%），排除量化因素；
  12B 有、26B 无，属该档容量所限。官方模板修复把失败率从 ~44% 降到 ~35%，**没有
  消除**。GitHub gemma#727 同症状、跨 llama.cpp/Ollama/LM Studio、跨 Q4–Q8。
- 社区触发条件是「长 agent 提示（7.8k–23k）+ 大量工具声明（10–89 个）」。**我方的
  复现比它们便宜得多也尖锐得多**：4k 上下文、**一个**工具、temp=0 确定性复现，
  且单变量翻转（只换尾部锚点内容就通）。这是一份值得回报上游的最小复现。
- 社区缓解手段是**重复惩罚**（repeat-penalty 1.08 / 窗 4096，部分缓解）与思考预算
  （`max_soft_tokens=1120`）。

**关键复核**：我方基线测量口径是「真裸贪心」——`temperature=0, min_p=0,
repetition_penalty=None`（§7-1 为可复现性刻意钉死）。那恰好是这个吸引子**最容易
发作**的配置：重复惩罚被完全关掉。同一份失败 prompt 换采样口径实测：

| 采样口径 | 结果 |
|---|---|
| 基线（裸贪心，rep=None） | ❌ 空转 |
| **生产缺省（min_p 0.05 / rep 1.1）** | ✅ 正常调用 |
| 社区建议（rep 1.08 / 窗 4096） | ✅ 正常调用 |

**故结论修正**：生产链路（缺省带 min_p 0.05 + rep 1.1）**已经在跑社区推荐的缓解
手段**，该组合在生产口径下未复现。上一版写的"生产上能让 12B 退化成空转"是在
**基线测量口径**下测得的，不代表生产行为——记在这里以免日后又被当成生产缺陷。
剩余动作：可选的加固（环境锚点并进用户轮正文，实测在裸贪心下也能解），属可变
尾部布局的架构决策，非紧急；以及把最小复现回报上游。

**附带教训**：**测量口径本身会制造故障形态**。为可复现性关掉重复惩罚是对的，但
由此测到的退化必须标注"该口径下"，不能直接外推到生产——否则就是拿一个我们自己
制造的极端条件去给模型定罪。这与本条主教训（探针也是提示词的一部分）同源。

**教训**：**探针也是提示词的一部分**。此前三次误判（§S.14/§S.16/§S.17）都是"我方
拼装层的字节偏离被当成模型缺陷"，这次是它的变体——**我方注入的内容与探针语义
打架，被当成模型缺陷**。字节对表查不出这一类：两份 prompt 字节都合法。
分辨它的唯一办法仍是单变量 A/B，而且要一路问到"换了还挂吗"，不能在第一个
可疑变量处收工。

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

## §7 生产机落地测试计划（推理速度全矩阵，2026-08-11 用户拍板，等设备）

> **背景**：生产机（Apple Silicon，MLX 栈）到手后无租借费率约束，可做穷尽式测速。
> 维度：权重量化档 × 上下文长度 × MTP（含草稿件形态）× TurboQuant KV 缓存量化。
> 理论预测已先行入档（2026-08-11 对话内 M4 Max 546GB/s 推算，见当日横幅与本节
> 5)，实测按预测对表——**验证的是 H（固定开销）、B_eff（实效带宽）、a（MTP 接受
> 率）三个假设量**，不是漫无目的扫参。
>
> **设备已定（2026-08-13）**：M4 Max 64G Mac Studio（Mac16,9，546GB/s=预测表
> 原生口径，零换算）。wired limit 实测默认 0（≈75%/48G 上限），上调=§7-0 ⑥、
> 到手第一批。64G=单人并发档；128G 专属格的裁边见阶段一/阶段四批注。

### 0) 工程前置（架构完善项——两轨制，2026-08-11 用户审定）

**两轨**：串行基线格（阶段一的量化档×上下文 H/B_eff 回归）**不含 MTP**、到手第一天
即可测；MTP 格与并发格压在下列架构项之后，架构完善与基线测试并行推进。

1. **MLX 引擎并发能力**（新建，最重）：引擎内连续批处理，明确排除多实例并跑
   （后者是 CHARTER §3 正确禁掉的形态）；编排层配套：MLX 后端 `max_concurrency`
   从"恒 1"放开。宪章不变量重述见 §5 Q14（待拍板）。
2. **mlx-vlm 升级手术**（第二重项，依赖链上游）：本栈全建在 mlx_vlm 上
   （backend.py 实证，钉 0.5.0），MTP 的 `load_drafter(kind="mtp")` 在新版——
   升级 = 铁律 1 手术单独成波（backends/mlx/patch/ 全复验 + e4b revision 矩阵
   重核）；其后才是 **MTP 接线**进 backends/mlx/backend.py（现 drafter 零接线，
   沿既有 speculative.mode 开关制形状）。kv_bits 若也依赖新版 API 同压此后。
   〔2026-08-13 用户指示：手术同时**顺手评估 Gemma-4 模型版本升级**——顺手
   （同一波内代价小、revision 矩阵一次重核覆盖）就一起做；不顺手则不强求，
   留后面单独升，不得为此扩大手术面。〕
   〔2026-08-14 升级侦察（PyPI/GitHub releases 实读）：目标 0.6.13（8/12 发布，
   项目极活跃）。**破坏面集中在 v0.6.4**：移除 mlx-lm 依赖+文本模型全 vendoring
   （backends/mlx/patch/ 的 import 路径大概率全移位）、移除 video generate 命令。
   **上游白赚清单**：MTP 草稿(0.6.x)+MTP 前缀缓存(0.6.4)+接受统计入 response
   timings(0.6.10→前置④基本免做)+APC×KV 量化(0.6.5)+server --max-num-seqs
   连续批处理(0.6.12→前置①的引擎侧上游已给，剩编排层接线)+tool_choice(0.6.5)。
   **模型版本升级（用户 2026-08-14 澄清：指钉值落后于上游件更新，非代际）**：
   全注册表钉值 vs HEAD 探测——**e4b 三件（4bit/8bit/nvfp4）上游 2026-07-06
   集体更新**（=v0.6.4 发布日，疑为 vendoring 后重转换件，即当初直拉报错被迫
   钉旧版的根源），26B/31B 全系+Qwen+google 原件零漂移；手术波顺手项=e4b
   三件升钉到新 revision 并复验（大概率**需要**新 mlx-vlm 才装得动，与手术
   互为条件，天然同波）。横向：RedHatAI 26B NVFP4（colab 侧）上游 2026-08-13
   刚动，下次 colab 场前复核。另：Google 新出 Gemma 4 12B 稠密档（unified
   带音频，需 0.6.6+），入列与否=阵容决策待拍板，不并手术面。
   ——**12B 入列（2026-08-14 用户拍板）**：4bit@73bcf090 / 8bit@200bb6db
   双件拉取+contract+装载生成 smoke 全过，已入注册表；基线格（v3 harness）
   与 model_eval 画像后补；定位=e4b 与 26B 之间的稠密中档、带音频（e4b 音频
   位的上位替补候选）。**行为核实（两次结论，以后者为准）**：初测曾记「无视
   抑制预填」+「音频未打通」两条，经 reviewer 质疑后逐字节复验，**两条均为误判**：
   ① 抑制预填对 12B **有效**（与 26b/31b 同族），初测失败是我方多一个尾随 `\n`
   所致（同 §S.16，12B 的症状是输出退化成 `<|channel>::` 循环）→ 注册表已设
   suppress_ghost_thinking；② 音频在我方 `prepare_inputs` 里**是通的**
   （input_features 94×640 正常产出），初测走的是上游 generate 路径不作数——
   端到端能力仍待评估波实测，capabilities 暂不写 audio（未验不声明）。
   ⚠️ **12B 转正门禁（2026-08-14 用户指示"记一笔，防止以后转正时中招"）**：
   12B 有一个**上游未消除的权重级缺陷**——`<|channel>thought` 退化空转
   （证据、复现与检索结论见 §S.18）。Google 定性为权重级吸引子，F16 全精度同样
   复现（~38%），**26B 无此问题**；官方模板修复只把失败率 ~44%→~35%。
   社区报的触发条件是「长 agent 提示（7.8k–23k）+ 10–89 个工具声明」——
   **那正是本栈生产的形状**（工具注册表 ~52 个）。
   当前暴露面低仅仅因为缺省档是 e4b-4bit、12B 只是候选，**这层保护会随转正消失**。
   故：12B 若要顶替 e4b 成为缺省档，以下三项是**准入前置**，不得跳过——
   ① 满工具集（非哨兵的单工具）× 长上下文的 tool_call 通过率实测，且必须在
      **生产采样口径**（min_p 0.05 / rep 1.1）与**裸贪心口径**两条下各测一遍
      ——两者差异本身就是该吸引子的暴露度指标；
   ② 核实 `thinking_budget` 在**关思考态**下是否兜得住模型自发打开的思考频道
      （我方失败案例正是关思考态；该机制现有实现是否覆盖此路径**尚未验证**）；
   ③ 若 ①/② 任一不过关，转正需附一条显式的循环兜底（思考频道 token 上限），
      不能只靠重复惩罚——社区明说那只是 partial mitigation。
   **门禁不因 §7-8 全量验收而解除**（2026-08-14 同日补记）：DSL 迁移后 12b-4bit
   工具列六格由全挂变全过、12b-8bit 五格翻正，这只证明**格式这一层的诱因已消除**，
   不触及权重级吸引子——哨兵探针只挂 **1 个**工具声明，而社区报的触发条件是
   **10–89 个**。哨兵全绿与门禁①要问的问题不是同一个问题，不得据此免测。〕
   〔✅ **2026-08-14 手术执行完毕（0.5.0→0.6.13）**：破坏面实际仅一处——
   `normalize_audio_features` 被上游整个移除（非移位），两行 mel 归一化收编进
   patch/gemma4_utils（音频链路行为跟人不跟版本）；其余全部 API 面
   （generate 七件套/contract 三件套/turboquant/mlx_lm.sample_utils/whisper/
   audio）原位无恙。连带抬升：mlx 0.32.0 / mlx-metal 0.32.0 / transformers
   5.15.0（金测试全绿，colab <5.15 钉是 vLLM 锅与本栈无关）+ 新硬依赖
   miniaudio/opencv。复验：金测试 776 全绿；26B-QAT/e4b 旧钉双件装载生成
   smoke 过（旧钉在新栈向后兼容，e4b 升钉从被迫变择优）；`load_drafter`
   到位（MTP 前提成立）。**数值级复验揪出 S.15**（旋转滑窗回滚失真）——
   prompt_lookup 生产口径拨 off，根修列 MTP 波头号项。**连带最大收获**：
   reviewer 二审质疑「12B 抑制无效」的测量字节形态 → 顺藤揪出 §S.16（我方
   抑制预填多一个尾随 `\n`）→ **发现 A 撤销、S.14 结案（引擎波缓存手术整项
   取消）、QAT 质量结论作废重跑**。一次升级手术顺手清掉两个「模型/引擎缺陷」
   幻觉，代价是两波质量数据要重测。〕
   〔✅ **2026-08-14 MTP 接线落地（代码全绿 913，真草稿件端到端待跑）**：
   config `speculative.mode` 加 `drafter` 档 + `draft_model/draft_kind/
   draft_block_size` 键位；后端在**推理执行器同线程**内于目标件之后装载草稿件
   （MLX 流线程本地），`load_drafter` + `validate_drafter_compatibility`
   **失败即抛不降级**（§S.3）；解码整段交上游 `generate_step`，我方只补两件。
   **接线途中查实一个上游硬伤（本波真正的工作量所在）**：`generate_step` 在
   `draft_model is not None` 时整段 `yield from run_speculative_rounds(...)`
   并 return，而后者**签名里没有 logits_processors**——processors 只在非投机的
   `_step` 里应用。净效果：**开草稿件投机 = 全部 logits 处理器静默失效**。
   对本栈两条实害：① `repetition_penalty` 生产缺省 1.1，而 §S.18 实测它正是
   12B 退化空转的兜底（`min_p` 在 sampler 里活得下来，**只有处理器这一路会死**，
   故症状会像"只有开投机时模型偶尔发疯"）；② 工具段约束 guard 也是处理器，
   丢了它 = 投机段 tool_call 无约束，§7-8 刚在格式层修好的问题从另一个入口回来。
   补法落点逐条查实后收敛到**一个函数**：gemma-4 无 `speculative_argmax_from_hidden`、
   无 `speculative_verify_logits`、有 `speculative_logits_from_hidden`，assistant
   草稿件解析成 kind=mtp，B=1 时两条走查都汇到 `_mtp_acceptance_walk`。
   `patch/mtp_processors.py` 替换该函数并顺序化逐位应用处理器；**无活动处理器时
   原样委派上游**（零行为变化）。顺序化不损失正确性：被接受的 token 恒等于该位置
   的 target token，故位置 pos 的正确历史就是「已产出 + 本轮 target[0:pos)」。
   **首过评审揪出我一条致命设计错误**：我另造了一套 `(tokens, logits)` 协议喂
   guard，而 `_GuardAdapter` 首调用会把 `_seen` 锁在**上游喂过的 prompt 长度**上
   （实测 prompt 12 token → `_seen=12`），我方从空历史起步长度 1,2,3… 永远追不上
   → guard 一个 token 都消费不到 → 工具约束在 MTP 路上等于不存在。而**正解就在
   同一文件 400 行之上**：`lookup_generate_step` 早已把 guard 从处理器列表里拆出、
   用 `consume()`/`mask_logits()` 直连协议驱动，惩罚历史用 `input_ids` 播种再按
   窗口截断。已按该先例重写。**教训**：动一个已有两条实现的机制前，先读另一条
   怎么做的——我发明了一套平行机制，还发明错了，而正确答案在同文件里。
   评审另一条 Critical（MTP 跑过一轮后上游把缓存**就地**换成
   `BufferedRotatingKVCache`，其 `keys.shape[2]` 是带缓冲的容量而非有效长度，
   被前缀复用段按稠密语义切片 → 跨轮上下文静默错乱）已修：该形态整体放弃前缀
   复用退冷预填，且判据必须放在 `input_ids` 被截断**之前**——我第一版放在循环里，
   那时 prompt 已截断，中止等于拿残缺 prompt 冷预填，比原 bug 更糟。
   连带响亮化：`mode=drafter` 而 `draft_model` 为空 → 拒启；目标件若走融合目标
   采样（绕过 logits）或缺逐位投影 → **装载期**否掉（不等生成到一半从流里抛）；
   B>1 批处理轮循环有活动处理器时拒跑（那组走查未补，Q14 并发 × MTP 要先补它）；
   `mode=drafter` × 开思考态 thinking_budget → 拒跑（上游投机路 return 在
   `pop_forced_token_id` 之前，预算到点不会强制收束；生产 enable_thinking 缺省
   False 故正常不触发）。
   〔✅ **2026-08-14 端到端冒烟通过（真草稿件，生产机）**：件 =
   `mlx-community/gemma-4-E4B-it-assistant-bf16`@844e008e（78.8M/183MB 落盘），
   目标 = 本机 e4b-4bit。脚本 `scripts/smoke_mtp_drafter.py`，8/8：
   装载 1.2s、kind 自动判定 mtp、`draft_model` 空即拒启、接受率统计正常回吐。
   **实测数字**：接受率 31–59%（随 prompt 变），tps 127–172 vs 基线 116
   = **1.08×–1.48×**（e4b 目标 + **bf16** 草稿）。
   ⚠️ **这与阶段二可证伪预测①「bf16 草稿在 546GB/s 下预测反亏、4bit 化是 Mac 上
   MTP 成立的前置」不符**——e4b 上 bf16 草稿净赚。不作为该预测的裁决（草稿件仅
   159MB，带宽成本本就可忽略；预测的适用域应是 26B/31B 的 0.4B 草稿），但**预测①
   在 e4b 档不成立**这一条要如实记上，阶段二据实重述而非沿用原文。
   ⚠️ **上游「temp=0 逐字一致」承诺实测不成立**：模型卡自述 "Output is
   byte-identical to no-drafter at temperature=0"，实测三题在 token 36–127 处分歧
   （另一次在第 404 字符 / 共 428）。**关掉全部处理器**（我方补丁原样委派上游）
   **同样分歧** → 责任在上游，与本波补丁无关。机制与 §S.15 ①「良性层」同源：
   验证批（S>1）与逐 token 前向累加序不同，近平局处贪心翻转，一翻之后全程分岔。
   分歧本身不算错（每个 token 仍由目标模型裁定），但**"同 seed 同输出"不可依赖**
   ——阶段二凡需可复现对照的格，不能用"投机/非投机输出应相同"当判据。
   💥 **自伤一条（同 §S.18 家族：测量口径自己制造失败模式）**：冒烟初版拿
   `GenChunk.text` 当全量输出比对，而它是 detokenizer 的**本次分段**不是累积量
   （`delta` 又只在 `features.want_delta` 时才算）。投机路每轮吐多个 token，
   分段边界天然与逐 token 基线不同——于是比出一个"首 token 就分歧"的**假分歧**，
   我据此差点把上游数值抖动误报成接线错。改为累积分段后真相才出来（前 54–127
   token 逐字一致）。教训：**流式接口的"文本"字段是分段还是累积，比之前必须确认**；
   凡对比两条产出，先验证比较对象本身（空串比空串也会"通过"，我第二版就吃了这个）。〕
   **尚未做**：26B/31B 档（草稿件更大，需另取件）、接受率/速度正式实测归阶段二。〕
   〔🔎 **2026-08-14 社区检索交叉验证（用户要求"与实测实装相互印证"）**：
   **处理器丢失一条被独立佐证**——另一个消费同一上游的项目 omlx 在其发行说明里
   逐字写着「Fixed VLM MTP silently dropping per-request **grammar constraints,
   thinking budgets, and repetition or presence penalties**」，与我方查到的三条
   （工具约束 guard / thinking 预算强制收束 / 重复惩罚）**三比三全中**，
   且是各自独立发现。**但处置相反**：他们把"需要处理器的请求"整体改走非投机路
   （降级保正确），我方是把处理器补进走查（保住投机红利）。**对本栈而言他们那条
   路等于关掉 MTP**——生产恒带 rep 1.1，按他们的判据每个请求都会被踢回非投机。
   此处记一笔以防将来有人"参照社区做法"把它改成降级式。
   **上游至今未修**：mlx-vlm 最新发行版即我方所钉的 v0.6.13（8/12），其后无新版，
   发行说明中无任何处理器相关修复。v0.6.10 的「expose per-request speculative
   decoding acceptance stats」正是我方 ④ 所用的统计口径来源。
   **前缀缓存 × MTP 是上游认过的交互面**：v0.6.4 有 `fix(apc): enable APC prefix
   cache with MTP speculative decoding`——但那修的是**上游自己的 APC**，本栈用的是
   自建前缀复用路，不在其覆盖内，故评审那条 Critical（缓冲缓存被按稠密语义切片）
   仍是我方自己的账，上游修不到。
   **旋转缓存 trim 属公认雷区，但我方版本的形态更坏**：生态里多条同族问题
   （mlx-lm #980/#118、lmstudio-ai/mlx-engine #177、olmlx #207），描述多为
   「trim 返回 0 / 缓存变不可裁」——那种形态调用方**看得见失败**。而我方钉的
   0.6.13 里 `trim()` 返回的是 n（记账已改、内容没搬），**报告成功却是错的**，
   "trim 返回 0 就回退"这类社区防护在此完全不设防。§S.15 那条（投机回滚 ×
   旋转态 max|Δ|=22）**未检索到任何同题报告**，仍是本项目独有发现，只是与该
   已知族相邻。
   **社区数字（供阶段二对表，非我方实测）**：Gemma-4 MTP 接受率 conversational
   70–90%、omlx 报 code 负载 88–89%；M3/M4 Max 加速 1.6–2.2×（RTX 4090 1.8–2.5×）。
   注意这与 §7 阶段二预测①「bf16 草稿在 546GB/s 下反亏、4bit 化是前置」**尚未对上**
   ——社区的 Mac 数字未标注草稿件精度，不可直接采信为反证，列为阶段二待判。
   **模型卡自述「Output is byte-identical to no-drafter at temperature=0」**：
   该承诺只在无处理器时成立（即上面那条缺陷），且本栈生产 temperature 缺省 0.2
   本就不在其适用域内。〕
3. 草稿件 4bit 本地转换 + `check_model_contract.py` 静态验证。
   〔📌 **2026-08-14 供给再勘误（社区检索，二次修正）**：草稿件现成 MLX 件比
   两次旧记都多，本条的"本地量化"工作量可能整个消失——
   `mlx-community/gemma-4-E4B-it-assistant-bf16`（**78.8M / 159MB**，4 层）、
   `gemma-4-26B-A4B-it-qat-assistant-mxfp4`、`gemma-4-31B-it-qat-assistant-nvfp4`、
   `gemma-4-31B-it-assistant-bf16`、`gemma-4-26B-A4B-it-assistant-bf16`。
   **mxfp4/nvfp4 的 MLX 件存在但装载未验**（那两种格式在 Mac 侧不常见，
   v0.6.8 有「mixed-precision compressed-tensors (fp8 + NVFP4)」支持，可能相关）
   ——取件时先验装载再谈量化，**不得据本条直接宣布③已消**。
   附：E4B 草稿件仅 159MB，是端到端冒烟最便宜的一对（目标件本机已有）。
   模型卡给了 `draft_block_size` 建议值：**单请求 6、批处理 3**——我方 config
   该键此前无取值依据，阶段二 k 扫描以此为起点。〕
   〔2026-08-14 供给勘误：26B 草稿**有** MLX bf16 现成件
   `mlx-community/gemma-4-26B-A4B-it-assistant-bf16`@cda74908（0.4B/839MB，
   e4b 同系 @844e008e）——「无任何 MLX 转换」旧记不实；本地转换缩水为
   839MB 件的本地量化（分钟级）。第三方 4bit/8bit 转换存在但出处不明不采。〕
4. 接受数/步观测口径打通。〔✅ 2026-08-14：草稿件把轮次/接受/草稿数记在自身的
   **终身计数器**上，接线按 `speculative_stats_snapshot`/`_since` 取差，回吐进与
   prompt_lookup 路同一个 `spec_stats` 容器。注意上游明说轮次是 batch 级的，
   B=1 归属精确、并发下会跨请求串味（CHARTER §3 下无碍，Q14 放开时要重做归属）。〕
5. TurboQuant `kv_bits` 旋钮与 config 键位打通。〔✅ 已在线（早于本波）：
   backend 的 `kv_bits`/`kv_quant_scheme`/`quantized_kv_start` 三键直通
   `generate_step`，阶段三开格无需再接。〕
6. wired limit 管理：`iogpu.wired_limit_mb` 上调的持久化方案与开机自检。

### 1) 统一测量口径（先于一切，违者数据作废）

- **路径**：一律 chat 模板路径（server 形态经 `/v1/chat/completions`，或带真模板的
  `/v1/generate`）——2026-08-10 夜实案：裸 prompt 无模板测质量=退化垃圾伪象，
  险些把 MTP 误判成采样破坏。
- **形态**：inference server 进程态（非 in-process），含真实服务开销；单流
  （CHARTER §3 单 worker 串行=生产语义）；接电+性能模式、后台清空、温度稳定后测。
- **每格记录**：generation tps 中位（n≥3，首跑 JIT 单列不计中位）、prefill tps、
  TTFT、峰值内存；MTP 格另记**接受数/步**（引擎暴露则直读，否则按
  `a = tps_mtp × t_step` 反推）；每格附**质量哨**：数到十 + 一道知识问答 +
  一道 tool_call 探针（model_eval mini 抽样）——量化×投机×KV 量化的交叉毒性
  必须每格看一眼，不能只看数字。
- **上下文控制**：确定性填充文本按 tokenizer 计数配到目标长度（±2%，以
  `usage.prompt_tokens` 回验），固定问句收尾；KV 必须真实物化（先 prefill 后测
  decode）。档位：0 / 4k / 8k / 16k / 32k / 64k（至 max_model_len 上限）。
- **环境钉死**：机器型号/内存/标称带宽、macOS、mlx / mlx-lm / mlx-vlm 版本、
  模型件 revision 全部入记录头；中途升级任何一项=作废重测。

### 2) 阶段一 · 基线格（无 MTP、KV 稠密）——提取 H 与 B_eff

- 矩阵：{26B-A4B, 31B}（e4b 作小档对照可选）× {4bit, 8bit, nvfp4 转换件（如有）}
  × 全部上下文档。〔2026-08-13 设备定案裁边：bf16 锚点格在 64G 本机不可行——
  31B bf16 权重 ~62G、26B bf16 ~54G 均越限；回归锚点=每模型 4bit/8bit 两点
  + e4b 小档对照（bf16 可补）。〕
- **方法学核心**：同一上下文档内，多个权重体积点对 `1/tps` 做线性回归——斜率
  =1/B_eff、截距=H。**H 和 B_eff 是测出来的回归量，不是拍的系数**；分上下文档
  重复回归可看 B_eff 对 KV 读取的适用性（长上下文时 KV 读是否同速）。
- 产出：每模型×量化的 tps-ctx 曲线；H / B_eff 定数（后续一切理论修正的地基）。
- ✅ **单流部分 2026-08-13 收官**（35/36 格，31B-8bit@64k=OOM 实证格）：曲线/
  回归/四发现见 `benchmarks/baseline/20260813-m4max64/ANALYSIS.md` 与当日横幅。
  要点：H 逐模型（31B≈13ms）、边际 B_eff 515GB/s、KV 税 5.6-8.4× 天真账、
  26B-8bit 全哨唯一绿。并发格仍待前置①。
- ✅ **QAT-4bit 附加波 2026-08-14 收官**（用户拍板追加；12/12 格同口径，
  `benchmarks/baseline/20260814-qat/ANALYSIS.md`）。件与钉值：26b-a4b-qat-4bit
  @0e3cbab3（15G）、31b-qat-4bit@83b1a12d（27G），contract+装载冒烟双过后入
  注册表。**配方核验：两件均非纯 4bit**——26B 共享 MLP/router 8bit（专家仍
  4bit，+1G）；**31B 全 60 层 MLP 8bit（27G，近 8bit 字节量，"qat-4bit"名实
  不符）**。四判定〔⚠️ 2026-08-14 当日晚 §S.16 后修订：**凡涉质量的 ①②③ 前半
  全部作废**，下方 🔴 项重跑；速度/内存判定仍立〕：
  ~~① 26B 4bit 车道内 QAT 取代 PTQ（A 毒阈值 4k→16k、tool 全绿）~~ **作废**
  （质量差异实为对畸形 prompt 的耐受度差异）；速度部分仍成立：QAT 短 ctx
  慢 8~12%、64k 贴平。
  ~~② A 毒未根除 → 26B 缺省档维持 8bit~~ **作废**（A 毒本身已撤销）；
  「26B-8bit 换 qat」仍判**否**，但理由改为：qat-8bit 无理论收益、换件需重跑
  全验证，性价比低。
  ③ **31B 档 qat-4bit 替代 8bit**——**硬理由成立且充分**：64k 稠密 KV 47.2G
  不 OOM（8bit 同格 Metal OOM 实证）、快 15%、省 4G；~~发现 B 治愈~~ 存疑待复测。
  ④ 速度模型续验（27G/515GB/s+H≈13ms 预测 15.3 实测 16.2@0）✅ 不受影响。
  qat-8bit 判不测（q4_0 训练权重上 8bit=继承成本无收益）。
- ✅ **质量哨全量重跑 2026-08-14 收官**（`benchmarks/baseline/20260814-sentinel-rerun/`，
  59/59 格零错误，harness `--sentinels-only`）。**count 与 qa 全绿 59/59**——
  发现 A、发现 B 撤销确证。失败全在 tool 一列，经逐字节对表查明为 §S.17
  （工具声明用 JSON 而非官方 DSL），发现 C 随之撤销 → **阶段一四发现全部是
  我方字节偏离，无一为模型/引擎缺陷**。选型更新（**tool 列污染⇒质量比较一律暂缓**）：
  全绿档有三个（26b-8bit 18/18、31b-4bit 18/18、31b-8bit 15/15），剥掉 tool 列后
  各档在 count+qa 上普遍打平——两支探针分辨不出真实质量差距，**26B 缺省档之争
  本波无法裁决，留 model_eval**。31B 档：PTQ-4bit 在**速度(+33~50%)与体积
  (-9.7G 权重/-10.4G 峰值)上决定性胜出**且不像 8bit 那样 64k OOM，但「质量更好」
  不能说；原「qat 替代 8bit」作废，QAT 是否值那 33-50% 速度损失待 model_eval。
- 🔴 ~~质量哨全量重跑（2026-08-14 新增，阶段四定档的前置）~~：§S.16 的 prompt
  bug 污染了**两波全部质量哨数据**（基线波 35 格 + QAT 波 12 格）。速度/内存/
  H/B_eff/KV 税等**非质量结论不受影响**（解码量由 max_tokens 定，畸形 prompt
  对各档同等施加）。重跑范围：{26B-4bit, 26B-8bit, 26B-qat4, 31B-4bit,
  31B-8bit, 31B-qat4, 12B-4bit, 12B-8bit, e4b×2} × 全 ctx 档的**哨兵段**
  （harness v3 已支持哨兵段独立失败记录；**待建** `--sentinels-only` 开关以
  跳过解码段省时——当前尚无此 flag）。
  待判问题：① 26B 缺省档 4bit vs 8bit（原判据已废）；② QAT 是否真有质量增益
  （原「毒阈值 4k→16k」只是耐受度差异）；③ 发现 B（31B 短 ctx）是否仍在。
- **并发格（前置①齐后补测）**：batch 1/2/4 的实效 B_eff 回归——定位算力墙拐点
  （M4 Max 算强/带宽比 ~62 FLOP/byte，双人+MTP 预计用到 ~40，墙位实测）；
  **MoE 专家并集税**：26B batch 1/2/4 路均衰减曲线（预测 batch2 路均 55-65%），
  对照 31B dense 同格（预测 ~85-87%）——批处理三分量（权重可摊薄/KV 不可/
  MoE 专家并集反摊薄）的直接验证。

### 3) 阶段二 · MTP 格——接受率与草稿形态

- 前置：见 §7-0（mlx-vlm 升级手术 + MTP 接线 + 草稿转换 + 观测口径）。
- **草稿件供给已查证（2026-08-14 HF API，SHA=当日主干）**，QAT 线全存在：
  官方 bf16 草稿 `google/gemma-4-26B-A4B-it-assistant`@6e5aaaf4、
  `google/gemma-4-31B-it-assistant`@627c5ec1；官方 **QAT 底草稿**
  `google/gemma-4-26B-A4B-it-qat-q4_0-unquantized-assistant`@95371415、
  `google/gemma-4-31B-it-qat-q4_0-unquantized-assistant`@96d4c8ca；现成 MLX 件
  `mlx-community/gemma-4-31B-it-qat-assistant-4bit`@5234fd58（**31B 的 4bit QAT
  草稿免本地转换**）。26B 草稿无任何 MLX 转换 → §7-0 ③ 的本地转换以 26B 为主，
  转换底优先用 QAT-unquantized（4bit 友好权重）。〔2026-08-14 勘误：26B 草稿
  MLX bf16 现成件存在（@cda74908，0.4B/839MB），详见 §7-0 ③ 注；「以 26B 为主
  的本地转换」降级为分钟级本地量化。〕阶段二矩阵草稿维度相应扩为
  {bf16, PTQ-4bit, QAT-4bit}；「QAT 目标 × QAT 草稿」接受率组合列为重点格
  （接受率=分布匹配性质，QAT 双端同源假设待验）。
- 矩阵（在阶段一优胜档上做，不全叉乘）：{26B, 31B} × {4bit, 8bit} × k=4 ×
  草稿 {bf16, 4bit} × ctx {0, 8k, 32k}。〔2026-08-14 QAT 波后修订：31B 目标件
  以 qat-4bit 顶替 8bit（B 治愈+64k 不 OOM），26B 加 qat-4bit 目标点；
  「QAT 目标 × QAT 草稿」（31B 草稿 @5234fd58 现成）为最高价值格。〕
- **前置追加（2026-08-14，harness v3）**：bench_baseline.py 过 reviewer 首审
  出 4 Critical + 13 Suggestion（EOF 误记成功/哨兵不记 gen_tokens 无退化闸/
  错误路径缓存泄漏/peak_mem 为进程级高水位非格级），均不动摇已跑两轮数据
  （升序 ctx + 全格 gen_tokens 事后审计；~~发现 A 独立复现背书~~ 该条已随
  §S.16 撤销，前两条仍足以支撑「速度数据未被 harness 缺陷动摇」），但阶段二格
  形态更复杂（MTP 接受率、多 run 交错），**v3 修复列为阶段二开格前置**；
  报告全文见会话 2026-08-14，修复清单以 4 Critical 为必修、Suggestion 择要。
- 重点验证三件（均为 2026-08-11 理论的可证伪预测）：
  ① **草稿 4bit 化是 Mac 上 MTP 成立的前置**（bf16 草稿在 546GB/s 下预测反亏）；
  ② **8bit 目标接受率 > 4bit 目标**（A100 侧 3.4 vs 1.9 的分布效应是否复现）——
  这是"8bit+MTP 白赚精度"成立与否的裁决变量；
  ③ **接受率随上下文的衰减**（理论未建模项，c=32k 与 c=0 对比）。
- 追加：优胜组合上 k ∈ {2, 4, 6} 扫描（k 按设备实测取值=既定纪律 D 口径）。
- **MTP×批处理交互**（前置①②齐后）：多流草稿+验证批叠加的正确性与收益衰减
  ——MTP 与流间批处理吃同一份权重摊薄红利（MTP=时间轴批处理），叠加收益
  递减且逼近算力墙，衰减曲线实测。

### 4) 阶段三 · TurboQuant KV 缓存量化格

- 矩阵：优胜组合 × kv {稠密, 8bit, 4bit} × MTP {off, on} × ctx {8k, 32k, 64k}
  （短上下文 KV 税可忽略，不测）。
- 速度侧：KV 税减半/减四的实收（混合注意 1:5 下税基本就小，预期收益有限——
  值不值得开靠数据说话）。
- **质量侧加测两项**：① 长文取针（needle）探针——KV 量化伤的是远距召回，
  普通问答测不出；② MTP×KV 量化交叉的输出漂移专项（L4 fp8KV×MTP 曾观察到
  temp0 措辞漂移；MLX 侧须重看，量化 KV 改变验证路径数值）。
- 并发能力齐后：取针质量从单流扩到**并发流**（kv8/kv4 × batch 2）。

### 5) 阶段四 · 定档收口

- 候选 2-3 组合跑 model_eval mini（tool_call / longchain 子集）+ 既定 24h 长稳
  门禁 → 定生产缺省档；`config.yaml` 注册表 profile 回填（PROFILES.md 口径），
  账本横幅收案。
- 对表清单（理论 vs 实测，M4 Max 口径换算到实际机型带宽）：
  ① 26B-4bit 无MTP ~145 tps@0 / ~91@32k；② 26B-8bit+MTP(4bit草稿) ≈ ①同速
  且 32k 后反超；③④ 31B 档 8bit+MTP ≥ 4bit+MTP（接受率补偿权重体积）；
  ⑤ H 每压 1ms，MTP 形态收益 ~15-18%（5 次发射/步的放大效应）。
  偏离超 ±25% 的格 = 假设错了，追因入坑账。
- **定档随附三验**（2026-08-11 讨论增补）：① wired limit 实测（默认 ~70-75%
  上限 vs sysctl 上调后的稳定性）；② 64G/128G 分界确认——sizing 预测：单人
  并发足迹 A′(31B-4bit+MTP+vl-2b 8bit+whisper)≈53G / B′(26B-8bit+MTP+同)≈55G
  （kv8、冗余 20%——MLX 无抢占安全阀，超限=换页悬崖），**64GB=单人并发下限
  （须上调 wired limit）**；双人 ≈63/58G，**128GB=双人并发起步**；③ 128G
  **双模型共驻形态评估**（26B 速度档+31B 质量档同驻走 model_router 分流，
  ~65G 足迹——单卡 A100 做不到的形态，128G 的核心卖点）。
  〔2026-08-13 设备定案：本机=64G，② 只验 64G 侧分界（单人并发在上调
  wired limit 后成立/不成立）；②的双人格与 ③ 双模型共驻本机不可验，
  存查等未来 128G 设备，不阻塞定档收口。〕
- **Q14 进宪判定**（随本阶段收口）：并发实测吞吐有提升 + 全程零并发事故
  （抢占互踢/换页悬崖/交互被后台挤占均算事故）→ §7-6 草案写入 CHARTER §3；
  不达标则维持「单 worker 串行」原文，Q14 以"实测否决"销案。

### 6) Q14 宪章修订案文本（已预审存档 2026-08-11；进宪门禁见阶段四）

替换对象 CHARTER.md「单 worker 串行」段（现 242-244 行）。预审定稿：

> **单引擎批内并发**（修订自「单 worker 串行」）：推理引擎全进程**单实例**——
> 多实例并跑 = 权重多读、互抢带宽，永远禁止（原不变量的物理内核保留）。并发的
> 唯一合法形态是**引擎内连续批处理**：一次权重读取服务批内全部流，dense 档近
> 线性白拿吞吐；成本三分量分开记账——**权重可摊薄、KV 每流全额、MoE 批内专家
> 并集反摊薄**，MoE 档的批红利以实测定夺，不得按 dense 直觉外推。
> **交互优先不变**：交互轮次恒享最高优先级；后台生成（摘要/沙箱/定时）经调度器
> **准入闸**进批，KV 池水位超限时后台流让位排队——批处理放开的是引擎，不放开
> 编排层的准入纪律。
> **不要**为"并发感"绕过准入闸直塞引擎；**不要**多实例并跑；批大小上限以实测
> 算力墙标定（并发×MTP 的 B_eff 拐点，§7 阶段一），不拍脑袋。

### 7) CUDA 侧口径对齐后的画像重跑（2026-08-14 用户拍板，**排在 Mac 之后**）

**起因**：§S.16 同波查出 CUDA 侧三件从未设 `suppress_ghost_thinking`（官方模板
本就带抑制），A100 侧一直跑在无抑制口径。flag 已补齐（config 三条），画像因此
与配置不同源。

**优先级（用户明示）**：Mac 侧全部工作在前——含 §7-2 质量哨全量重跑、阶段二
MTP、阶段三 KV 量化、阶段四定档；本项在其后，**不为它单独起 A100 场**，
搭下一次因别的事开的 colab/A100 场顺手做。

**范围**（三件均需，付费算力，起停须用户经手）：
- `google/gemma-4-31B-it`（A100 fp8 档）、`google/gemma-4-26B-A4B-it`（同）、
  `RedHatAI/gemma-4-26B-A4B-it-NVFP4`（L4 日用档）；
- 跑 model_eval 全套（tool_call / longchain / vision / vision_hard / tps），
  与补 flag 前画像**并列对表**——抑制预填省掉的幽灵思考 token 理论上应体现为
  tps 上升与 longchain 稳定性变化，是一次天然的 A/B；
- 回填 `config.yaml` profile 与 `PROFILES.md`，撤下"补 flag 前口径"标注。

**搭车顺手项**（同场做，勿单独起场）：RedHatAI NVFP4 上游 2026-08-13 新
revision（5557756b）核验与升钉；`colab.env` 正主（Drive）INFER 钥仍是旧值，
开场前必须先同步，否则 401 复发（见记忆项）。

### 8) 工具协议迁移 JSON→官方 DSL（**2026-08-14 用户拍板"迁移，开工"；✅ 已落地并全量验收通过**）

**落地状态**：代码全部完成，`backend/tests` 886 全绿（迁移前基线 785）。
**全量验收已过**（同日 16:48–19:28，生产机，见文末「验收」）。

**新增件**：
- `dialects/gemma4/dsl.py`——官方 jinja 四个宏（`format_argument` /
  `format_parameters` / `format_function_declaration` / `format_tool_response_block`）
  的逐字节移植，外加与之互逆的实参解析器 `parse_arguments`。三个消费方
  （prompt 组装 / 约束语法 / 差分测试）共用同一份，杜绝漂移。
- `dialects/gemma4/tool_grammar.py`——llguidance **Lark** 语法生成器，取代原
  `tool_schema.py` 的 JSON schema（调用体不再是 JSON，schema 表达不了）。
  键序按字典序（与 jinja `dictsort` 回放同口径）、必填强制、未知键拒、
  每键至多一次；`nullable` 声明放行 null/None。
- `tests/test_gemma4_dsl.py`——**差分金测试**：基准是 transformers 渲染官方
  chat template 的实际输出，不是我方期望值。这是 §S.16/§S.17 换来的纪律。

**协议面**：`ModelDialect.tool_call_schema` → `tool_call_grammar`，返回
`ToolCallGrammar(kind, body)`；`core/constrained.py` 按 kind 选 llguidance 编译
入口（lark / json_schema），仍不解读语法内容（R0 接缝完好）。

**同波连带修正**（都属"教学与实际协议脱锚"这一类，即 §S.17 的形态）：
- `parse_error_retry_hint` 改教 DSL——迁移前它教 JSON 且明写「不要用 `call:` 前缀」，
  迁移后正好把模型往废格式带；
- `response_parser` 的四条 error_msg 同理（它们会被回显给模型）；
- 三个沙箱（excel/math/research）各自硬编码的 JSON 格式示例删除，改从
  `engine.dialect.parse_error_retry_hint()` 取（与编排层同一来源）。

**本波最值得记住的一条**（迁移移除了一层**偶然的**保护）：
原文字符串块 `<|"|>…<|"|>` 没有转义机制，内容里出现一个真的 `<|"|>` 就当场
闭合该块。迁移前这条路走 `json.dumps`，它把 `"` 转义掉、**顺带**让 `<|"|>` 不再
是那个特殊 token；迁到原文块后这层保护无声消失。而工具观测是**外部可控**内容
（抓到的网页、`read_file` 读到的源码、纠错反馈回显的原文），不中和即为越权闭合
观测块、进而伪造控制标记的注入面。首过评审实证：一条纠错反馈里带进 10 个定界符
和 1 个真的 `<|tool_call>`。现由 `dsl._raw()` 统一中和。
**教训**：换编码格式时，要清点旧格式**顺带**提供了哪些保证——那些保证没写在
任何地方，只存在于旧实现的副作用里。

**首过评审 + 自审揪出并修掉的**（全部已实证复现后再修）：
- 轮拓扑状态机两处畸形：history 以工具结果开头时吐出两个轮头中间无收口符；
  `<|turn>user` 轮被留开、吞掉观测块且把用户正文挪到观测之后。根因是"折轮"
  与"留轮"两个判据不同口径，现统一为不变量「**开放轮恒为 model 轮**」。
- 解析器四类**静默错**（比报错坏得多，工具会照单执行）：`{code:"print(1)"}`
  让 run_python 去执行字面量、`{a:1,,b:2}` 解析出键 `',b'`、`{a:1 b:2}` 吞掉
  第二个参数、NaN/Inf 要到下游账本 `json.dumps` 才炸。现一律就地判失败。
- `nullable` 声明放行 null 而语法拒收（声明许诺、约束拒收）。
- `role=tool` 的 list 正文既不进观测块又漏成模型散文。
- 声明块在 `messages[0]` 不是 system 时被塞进对话中段（官方是"有工具就必有一个
  领头的 system 轮"）；续轮的 assistant 被插入杂散空思考频道。

**与官方模板的残留偏离**（全部有意，均已逐字节钉测试）：
1. `parameters` 缺 `type` 时补 `OBJECT` 闭合——官方在此吐出花括号不平衡的
   `…required:[…],}`。**这是热路径**：`update_plan` 等元工具的 parameters 就没有
   `type` 键，每份生产 prompt 都走。
2. 原文块内容里的定界符中和（见上）——官方不中和。
3. 观测后直接换 user 说话时我方收口——官方留下永不闭合的 model 轮。
4. 连续 assistant 不合并、孤立 tool 消息不丢弃——官方在这两处分别吐出无轮头的
   续写段、和**把整条工具观测静默丢弃**。两处官方自身有缺陷，不跟。
5. 历史以「调用→观测」结尾时我方收口再发生成头——官方保持轮开放、不发生成头，
   模型在轮内续写。**这条是唯一还想解决的**：它与本栈可变尾部（task_block /
   tail_notes / 环境锚点必须排在生成头之前）不相容，解决需重排可变尾部的位置，
   属 KV 缓存架构决策，另案。

**已知降级（CUDA 侧，属 §7-7）**：vLLM 的 structural-tag 只能表达 JSON schema，
表达不了紧凑 DSL；而 vLLM 0.26 又拒 per-request logits_processors（2026-08-07 实测）。
**净效果是 CUDA 侧 gemma-4 工具段无约束**，靠解析纠错闭环兜底。`structural_tag_json`
现对非 json_schema 语法显式抛出（拿 JSON schema 硬约束 DSL 模型比不约束更坏）。
补法（xgrammar 的 grammar 形态 / 服务端全局 LP）需 A100 现场验证，不可臆测形态。

**冒烟验收（2026-08-14，生产机实测）**：12b-4bit 与 e4b-8bit × ctx {0, 4k, 16k}
六格，count/qa/tool **全过**，调用体逐字节正确：
`<|tool_call>call:get_weather{city:<|"|>东京<|"|>}<tool_call|>`（实参走 `<|"|>` 原文块）。
对照迁移前：12b-4bit 工具列**整列全挂**、e4b-8bit ≥4k 全挂。

**✅ 全量验收（2026-08-14 16:48–19:28，生产机 Mac16,9 64G / wired=57344MB /
mlx 0.32.0 / spec=off，与迁移前那轮环境头逐字一致，故可逐格对表）**
`benchmarks/baseline/20260814-dsl-fullmatrix/`（10 档 × 6 ctx，跳 `31b-8bit:65536`
OOM 已证 = 59 格 × 3 判据 = **177/177 全过，100%**）。

逐格对表迁移前那轮（`20260814-sentinel-rerun`，同 59 格）：
- **❌→✅ 翻正 24 格**，全部落在 tool 判据上；
- **✅→❌ 回退 0**；**两轮都失败 0**；count/qa 两列 118 项一项未动。

翻正分布本身就是证据——**失败集恰好等于"被 JSON 格式压垮的弱档 + 长上下文"**：
- `12b-4bit` 六格全翻（整列由全挂变全过）、`12b-8bit` 五格翻；
- `e4b-8bit` 五格翻（≥4k 全挂 → 全过）、`e4b-4bit` ≥16k 三格翻；
- 强档只在长上下文端零星翻：`26b-4bit` @32k/64k、`26b-qat-4bit` @16k、
  `31b-qat-4bit` @32k/64k。
即：迁移前"能用但吃力"的判断成立，且吃力程度随档位变弱、上下文变长而放大。

10 个档位吐出的调用体逐字节一致，无一例外：
`<|tool_call>call:get_weather{city:<|"|>东京<|"|>}<tool_call|>`

**口径加严说明（结论比数字更强的原因）**：本轮判据比上一轮**更严**——除工具名与
`<|tool_call>` 标记外，还要求实参值 `东京` 出现（§7-8 迁移后实参走 `<|"|>` 原文块，
只查工具名会漏掉"调用发了但参数编码坏了"这一整类）。也就是说 177/177 是在比
那 24 格当初被判失败时更高的门槛上取得的。探针本身也已于同日更换，原因见 §S.18
——旧探针（`get_current_time`）与我方注入的日期锚点语义打架，在 ctx>0 上测的
不是工具调用能力。

**过程记一笔（看门狗阈值）**：首次挂的停滞告警阈值 20 分钟，被
`31b-qat-4bit × 65536` 这一格正常触发——该格上一轮实测 21m48s，即**阈值卡在了
矩阵最慢格的合法耗时之下**。看门狗阈值必须由**历史实测的最坏格**定，不能拍脑袋；
已改为 35 分钟并补一条"bench 进程消失即报"，避免只靠日志静默单一判据。

---

#### 迁移前的原始论证（拍板依据，存档）

**证据**：§S.17 的 A/B——同模型同问题，只换工具声明格式，12b-4bit 从
「`<|channel>thought` 无限循环不调用」变为干净调用，e4b-8bit@4k 从「幻觉已获取
时间」变为干净调用。约束采样已被隔离实验排除。

**现状**：本栈工具声明与调用体均为自建 JSON（配 llguidance 约束采样保证语法），
与官方紧凑 DSL（`declaration:名字{…}` / `<|tool_call>call:名字{k:v}`）是架构级
分叉。模型在 JSON 上「能用但吃力」——强档扛得住（26B-8bit/31B-4bit 全绿），
弱档直接退化（12B 全挂、e4b 长上下文挂）。

**若迁移，手术面**（铁律 1，单独成波）：
1. `prompt_builder`：`_build_tools_declaration` / `_build_tool_calls` /
   `_build_tool_responses` 三处改 DSL；
2. `response_parser`：解析 `call:名字{k:v}` 取代 JSON 解析；
3. `tool_schema.py` + llguidance 约束语法：语法从 JSON 换成 DSL（**风险最高项**
   ——约束采样是 Phase 2 成果，语法写错会静默放行非法调用）；
4. 全部 tool_call 画像重测（Mac 侧 model_eval + CUDA 侧 §7-7 同场）；
5. 金测试里工具相关断言（`test_dialect` / `test_openai_facade` 等）随之重写。

**不迁移的代价**：12B 工具场景几乎全挂（刚入列即废半条腿）、e4b 长上下文工具不可用；
26B/31B 强档虽能跑，但一直在分布外输入上工作，长上下文/复杂工具场景的
tool_call 通过率天花板未知。

**建议**：迁移。但这是**架构决策 + 高风险改约束采样**，请拍板后再动；
拍板前 tool 哨数据一律标注「污染口径」不作选型依据。
→ 2026-08-14 用户拍板「迁移，开工」，落地记录见本节开头。

---

*建立：2026-07-16 ｜ 依据：`ROADMAP.md` + 用户 Q1/Q2 答复 + `CHARTER.md` / `REARCHITECTURE.md` 实读 + 两侧代码实测。*
