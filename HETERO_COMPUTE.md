# 异构算力落地方案（HETERO_COMPUTE）——自持执行账本

> **这是什么**：两项算力架构改动的**可重入执行方案**——①CUDA 设备兼容层；
> ②重型计算（主模型推理 / RAG 嵌入重排 / ASR）与执行层（编排+工具+前端）的物理分离。
> 本文同时是**规划书**和**进度账本**：执行者（人或 Agent）不依赖任何聊天历史，
> 只读本文即可从任意中断点继续落地。
>
> **与 CHARTER 的关系**：本方案属于铁律 1 的"唯一例外"——以优化智能层本身为目标
> 的迭代。交互层协议（PROTOCOL v1）、编排护栏、三层记忆、调度语义**一概不动**；
> 改动的只有智能层的**物理承载**。每期完成必须跑后端金测试。
>
> **生产目标形态**：设备 A（GPU 服务器）承载推理 + RAG（8B）+ ASR；设备 B
> （用户手边 Mac）承载编排、工具执行与前端；设备 C（手机/随身设备）经账本协议
> 遥控 B（§7，只预留接口）。单机 all-in-one 模式（现状）永久保留。

---

## R. 重入协议（无历史上下文的执行者从这里开始）

每次开工（尤其是历史被压缩后的新会话）按此顺序执行，**不要跳步**：

1. **读 `CHARTER.md` 第 1 节**（铁律）——本方案动的是铁律 1 保护的智能层，
   开工前必须知道护栏护的是什么。
2. **读本文 §S 状态账本**——看总进度表定位当前期，读 §S.3 踩坑记录避免重蹈。
3. **跑 §S.2 现场核实命令**——账本可能滞后于现场（上次会话可能改了代码没更新
   本文）。以现场为准，发现偏差先把 §S.1 修正到与现场一致再继续。
4. **读当前期的小节**（§1–§3 中对应的一节），按任务清单逐项执行，做完一项
   勾掉一项（`[ ]`→`[x]`）并保存本文——期中中断也能精确重入。
5. **一期做完**：跑该期"验收"里的全部命令 → 全过后更新 §S.1（状态/日期/验收
   摘要）、把踩坑追加进 §S.3 → 保存本文 → 向用户报告"本期完成，可压缩历史"。

**纪律**：
- §D 的决策**不得重新讨论**（除非用户明确推翻）。不确定的事查代码，不猜。
- 本文引用的行号以"截至 2026-07-14"为快照，重入时用 grep 重定位，不盲信行号。
- 改动本文的架构方向（新增/砍掉阶段、换选型）= 改架构，须用户明确同意。
- **每期（含期内可独立验收的切片，如 B2a/B2b）完成 = 更新本文 + commit + push，
  不必再问**（用户 2026-07-15 常设授权，取代原先的"commit 时机由用户决定"）。
  次序固定：验收命令全过 → 更新 §S.1/§S.3/任务清单 → commit（message 带期号前缀）
  → push origin main（D6 的 `enkin-japan/Agent`）。工作快照 + 本文状态同步入库，
  历史压缩后 git log 即离线进度佐证。

---

## S. 状态账本

### S.1 总进度表（每期完成后必须更新本表）

| 期 | 内容 | 状态 | 完成日期 | 验收摘要 |
|---|---|---|---|---|
| P0 | 协议手术：InferenceBackend + 缓存句柄收编 | **完成** | 2026-07-14 | 金测试 122 全绿（3.7s，框架导入不再拉 mlx_vlm）；泄漏 grep 空；vitest 夹具 5 过；API 级实机冒烟全过：对话生成（spec 仪表经 GenChunk.extra 回吐）、分支克隆 kv_cache_reused=true、生成中取消、审批挂起→批准续跑（shell_exec 落盘验证）、math_research（弹跳球 36.112m 答对）、web_research（634m）、音频转写（mlx_whisper 经 backends 注册面）。浏览器手工冒烟留给用户。 |
| A1 | CUDA 后端最小可用（vLLM，特性全关） | 未开始 | — | — |
| A2 | CUDA 特性对齐（前缀缓存/投机/FP8 KV/约束） | 未开始 | — | — |
| A3 | 周边件 CUDA 化（ASR / RAG 设备层） | 未开始 | — | — |
| B1 | inference-server 服务化 + RemoteBackend | **完成** | 2026-07-14 | 金测试 122 全绿；泄漏 grep 空；vitest 5 过。同机双进程（:14161 包 MLX + backend mode=remote）实机冒烟全过：对话/跨轮 KV 热复用（次轮 7.6s）、分支克隆 kv_cache_reused=true（远程 clone 端点）、生成中取消（修复一处 SSE 收尾泄锁后 active_generations 归零）、审批挂起→批准续跑（shell 落盘验证）、math_research 与 web_research 沙箱子生成全走远程、约束采样跨网生效（constrained_steps 回填仪表）、图片经 /v1/media 内容寻址上传（识别正确）、音频转写、鉴权 401/401/200。实测 SSE 传输零吞吐损耗（端到端 29.1 tps=服务端 29.1 tps，首 chunk 0.35s）。拔线：SIGKILL 中断→轮次 fail 干净落账、重启服务端后同会话自愈（句柄换发+冷启动重建）；SIGTERM=uvicorn 优雅排水（在飞生成完成后才退）。mode=inprocess 回归零变化。ARCHITECTURE.md 拓扑图更新推迟到 B3（devctl profiles 一并画）。 |
| B2 | RAG 家族参数化（含多模态加载）+ 鉴权 + 远程化 + 8B 升级 | **进行中：B2a+B2b 完成** | 2026-07-15（B2b） | **B2b（VL 加载契约实测校准 + reembed 实跑，2026-07-15）**：用真机 2B VL 权重（各 4GB，单拉 RAG bf16）把 B2a"按约定装配"的 VL 路径校准为实测契约，并首次实跑 reembed——**揪出 1 个必崩错误 + 2 个静默 bug**（VL 重排加载直接 ValueError；重排收尾串误用使判别力静默打折；reembed 每跑一遍原文语料翻一倍）。①**VL 重排加载本会直接崩**：checkpoint 是 `Qwen3VLForConditionalGeneration`，`AutoModelForCausalLM` 不认 `Qwen3VLConfig`（ValueError）→ 改 `AutoModelForImageTextToText`（文本/VL 两族在此**分叉**，嵌入侧 `AutoModel`→`Qwen3VLModel` 则通用）。②**`trust_remote_code` 纯属多余**：两 repo 的 config.json 均无 auto_map，qwen3_vl 是 transformers 原生架构（venv 5.8.0）→ 三处全删（不给远程代码执行面）。③**重排收尾串必须按族分叉**：VL 自带 reranker.jinja 收在 `assistant\n`、无 `<think>` 块（文本族官方用例有）；实测误用文本收尾把相关/不相关分离度从 **0.61 打到 0.43**。④yes/no 口径**证实可复用**：VL 与文本 tokenizer 的 `yes=9693/no=2152` 完全一致，且与 VL 官方 `1_LogitScore` 声明相同——B2a 此处判断正确。⑤`embed_dim` 实测回填 vl-2b=**2048**（与官方 1_Pooling 一致；vl-8b=4096 仅取官方文档值、标注未实测）。验收：health 报 tier=vl-2b/modality=vl/dim=2048、VL 全链路检索通（embed→route→rerank，无关文档被阈值滤掉、命中 0.68）、**换档护栏首次真实换档触发**（vl-2b 库 dim2048 vs text-0.6b 运行 dim1024 → 拒启并指向 reembed，此前只用篡改戳测过）、**reembed.py 首次实跑全流程通**（dump-categories→wipe→start→rebuild，分类描述与检索均复原，0.999）。金测试 122 全绿、vitest 5 过、泄漏 grep 空、text-0.6b 回归零变化（收尾串分叉挂在 MODALITY 上）。**待设备 A 不变**：8B 实跑、三阈值标定、L2 图像入库、跨机远程化（并入 B3）。<br>**B2a（2026-07-14）**：①RAG 档位注册表（config.rag.tiers 5 档/10 仓库：文本 0.6B/4B/8B + 视觉多模态 VL-2B/8B）+ 选择制换档（`./devctl.sh rag-model`→`.rag-tier-selection` 指针，镜像主模型选择）；②文本/VL 两条加载装配路径（文本族 0.6B 行为零变化；VL 族按 Qwen 约定 loads+文本 I/O，图像真入库=L2 另一期）；③换档护栏（建库盖维度戳 + 启动/入库比对不符即快速失败提示先 reembed）；④`rag/scripts/reembed.py` 全量重建脚本；⑤RAG Bearer 鉴权（补原**零鉴权**缺口，RAG_API_KEY 常量时间比对，空=dev 放行）+ backend 三客户端经 `config_loader.rag_auth_headers` 统一注入；⑥user_id 埋点（X-Rag-User，D3，只定约定与参数面）。验收：金测试 122 全绿、vitest 5 过；单拉 RAG 新码实测——health 报 tier=text-0.6b/dim=1024、鉴权 401/401/200、0.6B 检索全链路（embed→route→retrieve→rerank，1 命中经 rerank 阈值过滤）、护栏盖戳 + 篡改戳→11s 拒启（RuntimeError 附 reembed 指引）、devctl rag-model 列全 5 档、inprocess 全系统回归绿。**待设备 A/GPU**：8B 实跑、全量重建实跑、三阈值数值标定、L2 图像入 RAG、跨机远程化联调（并入 B3）。VL 加载路径代码到位但未下 2B VL 权重实测（多 GB 下载未擅动，留作下一步可下载校验）。 |
| B3 | devctl profiles + §7 接口预留 + 生产联调 | **进行中：只剩真两台机器联调** | 2026-07-15（本机部分） | 本机能做的全部落地并实测：①**devctl profiles**（edge/gpu），含此前 devctl 完全不管的 inference-server 全套生命周期；②**tokenizer-only fetch**（edge 侧 31MB vs 全量 5.2GB，独立标记防误判）；③**§7 四处预留全落**——#1 `verify_token()` 单一比对面 +`auth.devices` 预留（10 项测试锁单密钥期行为不变）、#2 部署铁则**代码化**（非回环绑定+空密钥=三服务全部拒启，实测）、#3 现场核实为**早已完成**（账本原记"无发送实现"有误，实为完整 APNs ES256 客户端+11 项测试）、#4 多端附着夹具 005/006（**经变异测试确认有牙**）。验收：金测试 **132** 全绿（+10 鉴权）、vitest **7** 过（+2 夹具）、泄漏 grep 空、**all-in-one 零破坏**（实测生成 27.6 tps）。同机双进程 gpu+edge 双 profile 全链路实测：edge 不加载权重、跨进程调 gpu 推理成功（20.3 tps，答案正确）。**待第二台机器**：真跨机联调（含审批/分支/断线重连/定时任务）。 |

依赖关系：P0 → {A1→A2, B1}；A3、B2 依赖 P0（A3 的 RAG 部分与 B2 协同）；
B3 依赖 B1+B2。A 线与 B 线在 P0 后**完全独立**，可并行或只做其一。
最短生产路径：P0 → B1(先包 MLX) → B2 → A1/A2（服务端换 CUDA）→ B3。
客户端形态（§6）不在此依赖链上；§7 iOS 端只预留不排期。

### S.2 现场核实命令（重入时先跑，以输出为准修正 S.1）

```bash
cd /Volumes/enkin/project/Agent      # 仓库位置（2026-07-15 起；旧路径 ~/MacAgent 已废弃）
git log --oneline -10        # 提交史里的期号标记（commit message 带 [P0]/[A1]/... 前缀）
git status --short           # 未提交的进行中改动

# P0 是否完成/进行中：
ls backend/framework/backends/ 2>/dev/null                    # 存在 mlx/ ⇒ P0 至少进行中
grep -rln "mlx" backend/framework --include="*.py" | grep -v backends/  # 输出为空 ⇒ P0 收编完成
ls backend/framework/core/backend_protocol.py 2>/dev/null      # 协议文件存在 ⇒ P0 进行中+

# A 线：
ls backend/framework/backends/cuda/ 2>/dev/null                # 存在 ⇒ A1 至少进行中
# B 线：
ls inference/ 2>/dev/null                                      # 存在 ⇒ B1 至少进行中
grep -n "mode:" config.yaml | grep -A1 inference               # inference 段存在 ⇒ B1 进行中+
grep -n "8B\|auth" config.yaml | head                          # rag 8B/鉴权配置 ⇒ B2 痕迹

# 金测试基线（任何期开工前后都应全绿）：
cd backend && .venv/bin/python -m pytest tests/ -q; cd ..
# 协议夹具回放（交互层未被误伤的证明）：
cd frontend && npm run test; cd ..
```

### S.3 踩坑与偏差记录（追加式，只增不删；格式：日期 / 期号 / 现象 / 处置）

- 2026-07-14 / P0 / **开发环境是纯克隆状态**：backend/rag 无 `.venv`、frontend 无
  node_modules、`models/`、`rag/storage/`、`.env.local` 全缺席（仓库迁移时 gitignored
  资产未随迁）。处置：`./devctl.sh setup` 重建环境；pytest 不在 requirements 里，需
  `backend/.venv/bin/pip install pytest pytest-asyncio`；首次 `devctl start` 会重新下载
  模型权重。
- 2026-07-14 / P0 / **GenChunk 需要比 §1.1 草图多两个字段**：`delta`（展示增量——MLX
  的 SPM detokenizer 差分逻辑属于后端，中文逐 token 流式靠它；None=后端无差分能力，
  引擎退回用 text，恰好就是 B1 远程后端的既定路径）与 `extra`（投机采样接受率等后端
  仪表经 chunk 回吐，engine 不再持有 spec_stats 容器）。协议语义不变，字段面已定型。
- 2026-07-14 / P0 / **约束采样 guard 的双协议**：补丁投机路径按
  `hasattr(consume/mask_logits)` 识别 guard 并直连驱动——纯逻辑化时必须保留该识别面，
  故 MLX `_GuardAdapter` 同时实现 logits_processors 协议与 consume/mask_logits 直连
  协议，纯逻辑 `ToolCallGuard.next_allowed()` 返回三态（None/只放行单 token/allowed 集）。
- 2026-07-14 / P0 / **ASR provider 配置键取平铺式** `ingestion.asr_provider`（方案文本
  原写 `ingestion.asr.provider`）：既有键 asr_model/asr_language 都是平铺，嵌套段不一致。
  provider 名与默认模型名（含 "mlx" 字样）收编进 `backends/__init__.py` 注册面，
  泄漏 grep 才可为空。
- 2026-07-14 / P0 / **验收 grep 对注释同样敏感**：`utils/path_resolver.py` 的两处纯注释
  （hf 导入顺序、模型 re-upload 事故）也含小写 mlx，已改中性措辞/大写 MLX 指称；
  默认后端名 "mlx" 从 engine.py 默认参数收进 `backends.DEFAULT_BACKEND`。
- 2026-07-14 / P0 / zsh 里 `echo ===` 会被当作 `=word` 路径展开炸掉复合命令——
  现场核实命令若手打分隔符须加引号（S.2 命令本身无此问题）。
- 2026-07-14 / P0 / **web_research 冒烟揪出一个"疑似回归"，深挖后定性为存量病理，
  与 P0 无关**。现象：沙箱内工具调用 JSON 的 query 字符串偶发不闭合，整段生成被约束
  顶满 max_tokens（`[Constrained] 2047 步`），110s 熔断报超时。三重取证定性：
  ① 离线重放——纯逻辑 guard 对健康序列逐 token 全放行、JSON 完成即强制收尾，状态机
  正确；② live 探针——卡死段每步 mask 放行 261124/262144（闭引号在放行集里），是模型
  自己在字符串内漂走（KV 上下文与产出流错位，自以为引号早已闭合）；③ A/B——stash 掉
  P0 改动跑 HEAD 老代码，同样出现 1575 步同病理段（恰好在顶满前恢复）。根因指向
  prompt-lookup 投机采样的拒绝回滚在滑窗旋转态的近似损耗（inference-speedup Phase 1
  已记录 KL≈5e-4），约束 JSON 内草稿命中率高（复读 prompt/schema 片段）时误差可累积
  到改变采样。关 speculative 即消失。发作随机（沙箱 prompt 含分钟级时间锚，每轮路径
  不同），且沙箱 parse-retry 闭环多数能兜住（实测卡死后重试即成功）。**留待后续期**：
  候选缓解=guard.active 期间暂停出草稿（约束段本就短，投机收益趋零），属行为变更，
  不进 P0。
- 2026-07-14 / B1 / **SSE 生成流的 finally 里绝不能 await（粘性取消泄锁）**：客户端
  断连时 starlette 取消流任务，async 生成器 finally 里的 `await task` 在粘性取消语境
  下立即再抛 CancelledError，其后的 gens.pop 与 gen_lock.release() 被跳过——实测断连后
  active_generations 卡 1、后续生成永久挂死。修复：放锁改为生成线程退出时的
  done-callback（跑在事件循环线程），既保住"放锁严格晚于生成线程退出"的单例不变量，
  又对取消免疫。
- 2026-07-14 / B1 / **服务端进程死亡 = SSE 干净 EOF，不是异常**：TCP FIN 让
  iter_lines 安静结束，RemoteBackend 若静默返回，上层拿到"空生成"继续走解析重试，
  轮次状态被污染（实测卡出 cancelled 假象）。修复：流在 done/error 终态前结束一律
  显式 raise。另：SIGTERM 触发 uvicorn 优雅排水（监听即关但在飞 SSE 保留、生成做完
  才退，运维友好）；测拔线必须 SIGKILL。
- 2026-07-14 / B1 / **设备 B 侧分词器纯 transformers 加载**：AutoProcessor 对本模型
  目录不可用（缺 video_preprocessor_config.json），AutoTokenizer 可用且
  special_tokens_map 23 键齐全（与 MLX 路径同源同值）、stc/etc 单 token——
  `_ProcessorShim` 包一层 .tokenizer 即满足 PromptBuilder/Parser/约束引擎/仪表全部
  消费面。设备 B 不需要装 MLX，也不需要权重（只要模型目录的 tokenizer 文件，
  B3 落地时做 tokenizer-only fetch）。
- 2026-07-14 / B1 / **约束采样跨网**：纯逻辑 guard 无法逐 token 跨网驱动。方案：引擎
  构建 guard 时把工具声明一并放进 features.tool_specs（可序列化），远程后端剥掉
  guard 只发声明，服务端用自己的 ToolConstraintEngine 重建 guard 挂进 MLX 适配器，
  done 事件把 constrained_steps 回填本地 guard.masked_positions——仪表口径两模式一致
  （实机 21/39/53 步均正确回报）。进程内后端忽略 tool_specs，语义单一来源。
- 2026-07-14 / B1 / **远程缓存句柄两条纪律**：①new_cache 被 ChatSession.__init__ 在
  事件循环线程同步调用，绝不能发网络请求→懒注册（首次生成时服务端分配）；②客户端
  始终采纳 meta 事件回执的生效 cache_id（不只在句柄为空时）——服务端重启/TTL 回收后
  旧 id 失效，服务端换新冷启动并回执，客户端句柄自愈、后续轮次恢复热复用。
  沙箱一次性会话的服务端缓存无人显式释放，靠 45min TTL 兜底（设计如此）。
- 2026-07-14 / B1 / **传输开销实测**：同机双进程下端到端 decode 29.1 tps = 服务端
  原生 29.1 tps（零吞吐损耗），首 chunk 延迟 0.35s；逐 token SSE + 有界队列
  （maxsize 256 背压）在本机/内网量级不构成瓶颈。远程模式 backend 启动只需数秒
  （不加载权重）。
- 2026-07-14 / B2a / **RAG 代码本就 Qwen3 家族无关**：`rag_server.py` 用 `AutoModel`/
  `AutoModelForCausalLM` 泛型加载、`last_token_pool` 池化、yes/no token logits 重排——全 603
  行无一处写死尺寸/维度。故"兼容 0.6B/4B/8B(+VL)"不是新写兼容代码，而是把"选哪档"做成
  **选择制**（档位注册表 + `.rag-tier-selection` 指针，镜像主模型选择）+ 换档护栏 + reembed。
  与 §4 早写的"接口/池化/yes-no 全同，代码路径不变仅换权重"一致。
- 2026-07-14 / B2a / **换档纪律（用户澄清，收窄了设计）**：档位是**开发者调试旋钮**，非
  用户/运行时特性；**向量库一旦入库，档位即固定**，换档=开发者主动重建。据此砍掉"运行时
  无痛换档"才需要的复杂件（按模型命名空间的 collection、MRL 维度对齐），护栏收敛为：建库
  盖维度戳（`chroma_data/.tier_stamp.json`）+ 启动探测运行维度比对，不符即 lifespan 抛
  RuntimeError 拒启并提示先 `reembed`（实测篡改戳→11s 拒启，报错含 reembed 指引）。入库处
  chroma 维度错误另包装成同样提示，兜底"旧库无戳"边缘。
- 2026-07-14 / B2a / **多模态分层 L1/L2**：Qwen3 家族 5 档 = 文本 0.6B/4B/8B + 视觉 VL-2B/8B
  （各嵌入+重排，10 仓库）。"兼容多模态"诚实拆两层：**L1** 加载+文本 I/O（VL 用
  `AutoProcessor.tokenizer` + `trust_remote_code`，池化/重排复用文本路径；16GB M4 单拉 RAG
  bf16 可实测 2B VL）= B2a 已交付；**L2** 图像真入 RAG（收图/图文索引/检索）动 RAG API 契约 +
  Agent 喂图 + 存储，属独立能力另立一期。**VL 加载确切契约（是否 trust_remote_code、专用类、
  池化口径）未下 2B VL 权重实测——多 GB 下载未擅动，代码按 Qwen VL 通行约定装配、失败显式上抛，
  留作下一步可下载校验。**
- 2026-07-14 / B2a / **硬件天花板**：开发机 = Apple M4 / 16GB 统一内存。8B×2（嵌入+重排）
  fp16≈30GB、8bit≈16GB，即便单拉 RAG 也装不下 → 8B 生产档归设备 A（§4.1）。开发机能实测：
  0.6B/4B 文本 + 2B VL（单拉 RAG bf16，不起 Agent 主体）。dtype 做成档位字段（0.6B 保持
  fp16 零变化；bf16 供 VL 实测；8bit 量化 MPS 上不干净，归设备 A）。
- 2026-07-14 / B2a / **RAG 原本零鉴权（安全缺口）**：`rag_server.py` grep 无任何 Bearer/校验，
  跨机前必补。补法照抄 backend `api/auth`（RAG 独立 venv 不 import backend，就地实现）：全局
  FastAPI 依赖 `verify_rag_key`（自豁免 `/health`+docs），env `RAG_API_KEY` 常量时间比对，
  空=dev 放行+告警。backend 三客户端（rag_search/rag_admin/rag_sink）经 `config_loader.
  rag_auth_headers()` 读同一 env 统一注入，空则不加头（与服务端"空放行"对称）。
- 2026-07-14 / B2a / macOS 无 `timeout`（GNU coreutils，默认未装）——测试脚本里 `timeout 90 …`
  直接 exit 127（command not found），会假过（进程根本没跑）。护栏拒启测试改用后台起进程 +
  轮询"死亡 vs 就绪"判定，才真正验到 lifespan 抛错退出。测脚本踩坑，非产品代码。
- 2026-07-15 / B2b / **仓库换位置 + 又是纯克隆状态**：仓库现居 `/Volumes/enkin/project/Agent`
  （旧 `~/MacAgent` 已废弃，§S.2 命令已同步改），且同 P0 那次一样是纯克隆——venv/node_modules/
  models/storage/.env.local 全缺席（gitignored 资产不随 git 走）。处置：`./devctl.sh setup`
  一把重建（rag/backend/searxng venv + npm，约 5 分钟），pytest 仍需单独装
  （`backend/.venv/bin/pip install pytest pytest-asyncio`，requirements 里没有）。基线复现：
  金测试 122 全绿、vitest 5 过。**教训：账本里写死的绝对路径会过期，重入先 pwd 再照抄命令。**
- 2026-07-15 / B2b / **"按通行约定装配"的三处猜测，实测两错一对**（B2a 诚实标注"待实测校准"
  的价值兑现）：①`AutoModelForCausalLM` 加载 VL 重排**直接 ValueError**（不认 Qwen3VLConfig）——
  VL checkpoint 是 `Qwen3VLForConditionalGeneration`，须走 `AutoModelForImageTextToText`；
  嵌入侧 `AutoModel`→`Qwen3VLModel` 反而通用。**两族在重排加载类上分叉，是唯一必须分叉的加载点。**
  ②`trust_remote_code=True` 多余（config.json 无 auto_map，qwen3_vl 原生于 transformers ≥4.57，
  本 venv 5.8.0）——已全删：不给远程代码执行面是隐私项目的默认姿态。③yes/no 复用**判断正确**：
  VL 与文本 tokenizer `yes=9693/no=2152` 同值，且与 VL 官方 `1_LogitScore` 声明一致。
  **方法论：HF repo 的 config.json/1_Pooling/1_LogitScore/*.jinja 是免费的契约真相，
  下权重前先 curl 一遍能省掉大半猜测（本次 10 个 repo 的存在性也是这么先验的）。**
- 2026-07-15 / B2b / **重排收尾串按族分叉，且分叉有实测代价**：文本族 Qwen3-Reranker 官方用例
  收尾 `<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n`；VL 族自带
  `additional_chat_templates/reranker.jinja` 收在 `assistant\n` 即止、**无 think 块**（前缀两族
  字节相同）。2B VL 实测：VL 收尾的相关/不相关分离度 **0.61**，误用文本收尾降到 **0.43**——
  分数仍"能用"（不会崩、也不会明显报错），但判别力静默打折，正是最难发现的那类退化。
  故收尾串挂 `MODALITY` 分派，text 路径字节不变。
- 2026-07-15 / B2b / **reembed 实跑揪出原文语料翻倍（B2a 静默 bug，未跑过就发现不了）**：
  `rebuild` 把 raw_files 的原文经 `/insert` 重灌，而 `auto_classify_and_insert` 每次都按新 uuid
  **另写一份备份** → 实测 2 篇原文重建一次变 4 篇，再建变 8 篇（指数增长），且下一轮重建会把
  副本也灌进库。**raw_files 是重建的承重网（脚本自己的 docstring 这么写的），迁移工具反而污染它
  是最坏的一种 bug**——而账本原把"reembed 实跑"整个推给设备 A，等于让 8B 生产迁移去踩。
  修法（守住"raw_files 是原文唯一真相、向量库是派生物"这条不变量）：`InsertRequest` 加可选
  `source_path`＝"备份已在盘上，就地索引"，服务端跳过备份写入并直接用该路径作元数据；
  reembed 传自己读的那个文件路径。缺省 None = 普通入库照旧另写备份（Agent 侧行为零变化）。
  未采用 `reclassify_background_task` 那种"先复制再删原件"的既有惯例：迁移工具在后台入库失败时
  会把承重网删掉，方向错了。附带两条：①`source_path` 必须落在 `storage/` 内（接口层 400 拦截）——
  否则元数据能指向库外文件，而 `/document/delete` 正是照元数据删盘上文件的，等于把删除面扩到库外；
  ②就地重索引前先 `delete(where={"source_path": ...})` 清旧切片，让 rebuild **可安全重跑**
  （大语料迁移中途失败后操作员必然重跑——原本会静默灌出重复向量，实测同一篇命中两次）。
- 2026-07-15 / B2b / **重灌未分类池会跨库留下孤儿切片（上一条修法的二阶 bug，实测才现形）**：
  rebuild 把 `unclassified_raw/` 的原文以 category=None 重灌 → 路由到某分类（重建后 meta 库
  已有分类了，正是常见情形）→ 原文被移进 `raw_files/{类}/`，但**旧切片仍躺在 unclassified_db**
  里指向已移走的路径 → 实测同一篇命中两次（新旧路径各一）。原因：清理只按**移动后**的
  路径清**目标库**。修法照 `reclassify_background_task` 的既有惯例——按**原路径**清**原库**
  （`_collection_of_raw_file()`：文件所在目录即它当初入的库），再加目标库同路径清理（同类重跑
  去重），两处并集。**教训：这条只有真跑"未分类→有分类"的重灌序列才会现形，
  文档和 dry-run 都照不出来；数据搬运类改动必须把每条分支都真跑一遍。**
- 2026-07-15 / B3 / **§7 表格第 3 行是错的：APNs 发送器早就完整实现了**（账本滞后于现场的
  实例）。表格记"`/push/*` 注册面已存在，无发送实现"，而现场是完整的 ES256 JWT 直发客户端
  （`framework/push/apns.py`，生产/沙箱环境自动回退 + 学到的环境粘滞回注册表）、
  `NotificationDispatcher`、config `push` 段（连 Key ID/Team ID 都填好了）、11 项测试。
  缺 .p8 时抛 `APNsDisabled`、调用方静默降级——正是"预留"想要的 no-op 语义，只是**做过头了**。
  **教训：§R 第 3 步"以现场为准"不只针对本文的进度表，§7/§D 这类看似静态的表格同样会过期；
  动手前先 grep 一遍要落的东西是不是已经在了。**
- 2026-07-15 / B3 / **"部署铁则"此前只是散文，现已代码化**：§8 写着"非回环绑定只允许出现在
  受信内网"，但代码里改 host 与设了 key 是两个独立动作——先改 host 后忘 key，服务就对整个
  tailnet 裸奔，且**只打印一行启动告警**（谁看启动日志？backend 那行告警甚至已经写明"接口对
  可达网络裸奔"，仍然只是告警）。profile 让非回环绑定成为常规操作，故三服务
  （backend/inference/rag）各自实现同一条护栏：非回环 + 空密钥 = **拒启**（SystemExit），
  回环时空密钥仍放行（dev 不变）。三处独立实现不共享代码——RAG/inference 各自独立 venv，
  与 §S.3 B2a 补 RAG 鉴权时同一条纪律。护栏放在 `__main__` 里、**先于加载权重**：
  配置错误在 1 秒内暴露，而不是等几十秒加载完才报。
- 2026-07-15 / B3 / **macOS 自带 bash 3.2 会把紧跟变量的全角字符吃进变量名**：
  `"$INFER_MODE）"` → `INFER_MODE）: unbound variable`（`set -u` 下直接崩）。
  devctl.sh 头部第 46-49 行**早就记着这个坑**并用 LANG/LC_ALL 兜底，但兜不住——
  既有代码一律写 `${VAR}` 花括号正是这个原因。新增代码照抄这条纪律。
  自检：`grep -n '\$[A-Z_]*[（）—，：]' devctl.sh` 应为空。
- 2026-07-15 / B3 / **tokenizer-only 必须用独立标记，否则埋雷**：edge 只拉分词器（31MB），
  若也写 `.download_complete`，同一台机器日后切回 `mode=inprocess` 会命中标记、跳过下载，
  然后加载器缺权重报错——**正是标记机制当初要防的"半截模型被判有效"**（§S.3 P0）。
  故用 `.tokenizer_only_complete`，两标记可并存、全量标记优先（全量是分词器的超集，实测
  gpu profile 下全量下载后两个标记同时存在、edge 秒过）。另：判据取 config 的
  `inference.mode` 而非 profile 旗标——"要不要权重"取决于承载模式本身（同机 remote
  双进程也一样不必在 backend 侧再拉一份）。实测该目录 `AutoTokenizer` 可加载、
  23 键 special_tokens_map 齐全、`<|channel>`/`<channel|>`/`<|tool_call>`/`<tool_call|>`/`<eos>`
  全部单 token（注意：`<start_of_turn>` 是 Gemma-3 写法，本模型**不用**，拿它自测会得到假警报）。
- 2026-07-15 / B3 / **夹具建在"完整轮"上会一测一个准地骗过你**：给 §7 #4 写的头两版夹具建在
  001（以 `turn_ended` 收尾）之上，全绿——但把归约器改成"收到 `stream_attached` 就重置状态"
  的变异体**照样全绿**，即断言毫无意义。原因：`turn_ended` 的 payload 自带**完整** response，
  终态是从它整个重建的，中途状态被冲掉也照样收敛。改建在 002（`awaiting_approval`，
  状态全靠逐事件累积、无 turn_ended 可兜底）之上，同一变异体立刻被 006 抓红。
  **纪律：新夹具落地前先跑一次变异测试（故意改坏实现，确认它会红）——不会红的夹具是装饰。**
  已写进 `protocol/fixtures/README.md`。
- 2026-07-15 / B2b / **换档护栏首次被"真实换档"触发**（B2a 只用篡改戳测过）：vl-2b 建的库
  （dim2048）在选回 text-0.6b（dim1024）后启动 → lifespan 抛 RuntimeError 拒启，报错同时点名
  两个档位/维度并指向 reembed。配套实跑 `reembed.py` 全流程（dump-categories → stop → wipe →
  start → rebuild）：分类路由描述从备份复原（非占位）、检索复原（0.999）。**流程设计经得起实跑**：
  第 2 步"旧服务在跑时 dump"之所以成立，是因为换档指针只在重启时生效——先选档不影响在跑的旧服务。

---

## D. 已定决策（用户已拍板，不再重议）

| # | 决策 | 要点 |
|---|---|---|
| D1 | CUDA 引擎选型 = **vLLM 进程内 AsyncEngine** | 理由见 §2.1（四项智能层优化全有原生对应物）。不用其 OpenAI server（丢失原始 prompt 控制面）。llama.cpp 仅作低显存回退预案，不先做 |
| D2 | 分离形态 = **inference-server（:14161）+ RemoteBackend** | B 侧执行层/会话数据不出用户设备；服务端不持久化会话 |
| D3 | RAG 生产档 = **Qwen3-8B 嵌入+重排**，多用户隔离**只埋 user_id 点** | 见 §4；命名空间落点约定已写死，实现推迟 |
| D4 | 客户端形态 = **macOS 原生 app（SwiftUI）+ 内嵌 Python 执行层** | 全 Swift 重写执行层已否决（生态硬墙/资产归零/维护倒挂）。web 前端保留为远程窗口。见 §6 |
| D5 | iOS 远程端 = **只留四处接口预留**（B3 期顺手落），本体不排期 | 见 §7 |
| D6 | 托管：2026-07-14 与旧仓库（enkin-japan/MacAgent）断开，同日改托管至 **`git@github.com:enkin-japan/Agent.git`**（origin/main，已 push） | 每期验收通过后 commit（message 带期号前缀）并 push 到此仓库；旧仓库不再使用 |
| D7 | 端口约定：inference-server **:14161**；目录约定：CUDA 后端 `backend/framework/backends/cuda/`、MLX 后端 `backend/framework/backends/mlx/`、服务 `inference/` | 避免每次重入重新发明命名 |

---

## 0. 现状盘点：MLX 耦合面清单（截至 2026-07-14 快照；重入时以 S.2 的 grep 为准）

引擎层自身的防腐设计已完成 90% 的隔离——编排层只经 `generate_once()` /
`summarize()` 拿纯 dict，`backend/tests/fake_engine.py` 证明接口可替换。
真正的耦合是**类型泄漏**：`PromptCacheState` 与 `mx` 漏出了引擎层边界。

| # | 文件 | 泄漏内容 | 用途 |
|---|---|---|---|
| 1 | `memory/session.py:12-13,46` | `mx` + `PromptCacheState` + `clone_kv_cache` | 会话 KV 持有 / 分支克隆 / 冻结释放 |
| 2 | `agents/math_sandbox.py:17,85` | `PromptCacheState` | 一次性会话替身的全新 KV |
| 3 | `agents/research_sandbox.py:18,103` | 同上 | 同上 |
| 4 | `agents/excel_sandbox.py:28` | 同上 | 同上 |
| 5 | `core/orchestrator.py:822` | 惰性 import `PromptCacheState` | 收尾审计的一次性会话 |
| 6 | `core/constrained.py:19` | `mx` 实现 logits mask | llguidance 约束采样（协议对齐 mlx_vlm） |
| 7 | `ingestion/parsers/asr.py:28` | `mlx_whisper` | 音频转写（懒加载、fail-open） |
| 8 | `app.py:9,342` | `mx.clear_cache()` | 退出时释放资源 |
| 9 | `core/engine.py` + `patch/gemma4_*.py` | 引擎与补丁本体 | 智能层资产（原样保留，整体挪入 MLX 后端） |

RAG 侧（`rag/rag_server.py`）是标准 PyTorch，设备耦合只有 `DEVICE` 一行
（`:64`）与两处 `torch.mps.empty_cache()`（`:579-580`）。

**关键既有优势**（规划的立足点，全部实测存在）：

- 所有 MLX 专属优化都是**配置开关制**：`kv_bits=None`→稠密、`speculative.mode=off`→
  普通路径、`constrained_tool_calls=false`→零开销。新后端可以全关上线，行为一致。
- `clone_kv_cache` 失败返回 None → 调用方回退冷启动预填充。**架构天然容忍
  "不支持缓存克隆"的后端**。
- 约束采样构建失败→永久禁用 fail-open；ASR 异常→fail-open 字符串。
- RAG 原文备份在 `rag/storage/raw_files/{类}/`——**换嵌入模型后全量重建向量库可行**。

---

## 1. P0（共同前置手术）：InferenceBackend 协议 + 缓存句柄收编

两个方向共享的唯一前置。目标：**MLX 的名字从引擎层之外消失**。
验收标准：**零行为变化**（金测试全绿 + 实机冒烟全过）。

### 1.1 协议定义（新增 `backend/framework/core/backend_protocol.py`）

```python
class CacheHandle(Protocol):
    """不透明的会话级推理缓存句柄。对上层只承诺身份与生命周期，不承诺实现。
    MLX 实现 = 包一层 PromptCacheState；远程实现 = 服务端注册表里的 id。"""
    token_ids: Optional[List[int]]     # 前缀匹配裁剪的依据（只读暴露）

class InferenceBackend(Protocol):
    async def load(cls, model_path, **cfg) -> "InferenceBackend": ...
    def new_cache(self) -> CacheHandle: ...
    def clone_cache(self, src: CacheHandle) -> Optional[CacheHandle]:
        """分支复用。返回 None = 不支持/失败，调用方回退冷启动（既有语义）。"""
    def release_cache(self, h: CacheHandle) -> None:
        """冻结/删除会话时释放物理资源。"""
    def shutdown(self) -> None: ...
    def stream_generate(self, prompt, *, cache: CacheHandle,
                        images=None, audios=None, sampling=..., features=...
                        ) -> Iterator[GenChunk]:
        """同步迭代器（跑在推理执行器线程），逐 token 段产出。
        GenChunk = {text, prompt_tokens, generation_tokens, tps, peak_memory}。"""
```

### 1.2 任务清单（对照 §0 清单逐项；勾掉即存档）

- [x] 新建 `backend/framework/core/backend_protocol.py`（§1.1 的协议 + GenChunk；
      GenChunk 增设 `delta` 展示增量与 `extra` 后端仪表字段，见 §S.3 2026-07-14）
- [x] `engine.py` + `patch/` **原样平移**到 `backend/framework/backends/mlx/`
      （补丁一行不动 `git mv` 至 `backends/mlx/patch/`；MLX 物理层收进
      `backends/mlx/backend.py` 的 `MLXBackend`+`MLXCacheHandle`）。`AgentEngine`
      保留在 `core/engine.py`，改为持有 `InferenceBackend` 实例；`PromptBuilder` /
      `ResponseParser` / `VisibleTextStreamer` / 推理锁 / 执行器线程 /
      token 分解仪表**留在 `AgentEngine`**（后端无关资产）
- [x] `memory/session.py`：`PromptCacheState()` → `backend.new_cache()`；
      `clone_kv_cache` 挪进 MLX 后端，分支处改调 `backend.clone_cache()`；
      冻结处改调 `release_cache()`。ChatSession/SessionManager 注入 backend
      （app.py 装配点 `session_manager.backend = engine.backend`；无后端时
      句柄为 None，分支冷启动——金测试环境语义）
- [x] `agents/{math,research,excel}_sandbox.py`：一次性会话替身改从传入的
      engine 取 `engine.backend.new_cache()`（沙箱本就持有 engine 引用）
- [x] `core/orchestrator.py` 收尾审计处同上，删惰性 import（改为
      `engine.backend` 缺席时跳过审计，与旧「无 mlx_vlm 环境跳过」语义对齐）
- [x] `core/constrained.py`：mask 计算抽出纯逻辑（`next_allowed()` 三态：
      None/单 token/np bool 集），数组实现下沉到 MLX 后端 `_GuardAdapter`
      （兼容上游 logits_processors 协议与投机走查 consume/mask_logits 直连协议）
- [x] `ingestion/parsers/asr.py`：抽 provider 接口经 `backends/` 注册面分发，
      config 新增 `ingestion.asr_provider`（默认 `mlx_whisper`，行为不变；
      键名取平铺式对齐既有 asr_model/asr_language，见 §S.3）
- [x] `app.py`：`mx.clear_cache()` → `engine.backend.shutdown()`
- [x] `tests/fake_engine.py`：FakeEngine 增 `backend` 属性（FakeBackend：缓存面
      替身，clone 恒 None=冷启动语义）；另 `scripts/{probe_lookup_parity,
      probe_reuse_e2e,measure_kv_mem}.py` 的补丁 import 路径同步改到
      `framework.backends.mlx.patch.*`

### 1.3 验收（全过才算完成）

```bash
cd backend && .venv/bin/python -m pytest tests/ -q          # 全绿
grep -rln "mlx" backend/framework --include="*.py" | grep -v backends/   # 输出为空
cd frontend && npm run test                                  # 夹具回放全绿（交互层未伤）
./devctl.sh restart && ./devctl.sh status                    # 服务全部就绪
```

实机冒烟（浏览器 :5173 手工过一遍，每项在 §S.1 验收摘要里记结果）：
分支克隆一次 / 触发一次审批并批准续跑 / 取消一轮 / 跑一次 math_research 与
web_research / 发一段音频转写。

- [x] 验收全过 → 更新 §S.1、踩坑进 §S.3、保存本文、报告用户可压缩历史
      （2026-07-14：验收命令全过；冒烟以 API 级替代浏览器手工——分支克隆/审批
      续跑/取消/math_research/web_research/音频转写全过，浏览器过一遍留给用户）

---

## 2. 方向 A：CUDA 兼容层

### 2.1 选型依据（决策 D1 的理由，重入时不必重读，争议时才查）

vLLM 的决定性理由——几个月调出来的四项智能层优化全有原生对应物：

| MLX 侧手工实现 | vLLM 原生机制 | 备注 |
|---|---|---|
| KV 前缀分层复用（PromptBuilder 三段式 + token_ids 前缀匹配裁剪） | Automatic Prefix Caching（radix 前缀树） | 每请求发全量 prompt，prefill 自动命中前缀。分支克隆/删轮裁剪语义自动退化为前缀命中——`clone_cache` 可直接返回 None（既有回退） |
| prompt-lookup 投机采样（`_NgramIndex` 单槽版，1.6~2.2x） | ngram speculative decoding | 同一算法思想的官方实现 |
| TurboQuant KV 量化 | FP8 KV cache | 精度语义不同，显存收益同向 |
| llguidance 工具段约束采样 | guided decoding（llguidance/xgrammar）+ per-request logits_processors | llguidance 语法可直接复用 |

不用 vLLM 的 OpenAI 兼容 server：本项目生成协议是原始 prompt 级的（Gemma 方言
频道标记 soc/eoc、思考预算锚定强制收尾、detokenizer 级流式过滤），OpenAI API
抹掉了这些控制面。正确形态：`backends/cuda/` 直接驱动 `AsyncLLMEngine`。

### 2.2 A1 —— 最小可用（特性全关）

- [ ] **开工 spike**：核实当前模型（config 的 model registry 所选档位）在 vLLM
      的支持状态（含音频多模态）。不支持 → 在 registry 加同方言替代档位条目，
      记录进 §S.3，再继续
- [ ] `backends/cuda/engine_backend.py`：load / new_cache（会话 id 占位）/
      clone_cache（返回 None，走冷启动回退）/ stream_generate（AsyncEngine
      streaming 适配成同步迭代器，喂给既有执行器线程模型）
- [ ] 采样参数对齐：temperature / min_p / repetition_penalty(1.1, window 50) /
      max_tokens / 停止符（vLLM 原生支持 min_p 与 repetition_penalty）
- [ ] **思考预算锚定（A1 最难点）**：MLX 靠 ThinkingBudgetCriteria 超限强插
      `\n<eoc>`。vLLM 无生成中注入 → 两段式：思考段单独一次生成（stop=eoc,
      max_tokens=thinking_budget），超限则 prompt 尾拼 `\n<eoc>` 发第二段；
      前缀缓存使二段 prefill 近零成本。写验收用例逐字锁定与 MLX 路径语义一致
- [ ] `scripts/check_model_contract.py` 对 CUDA 后端全绿
- [ ] 金测试全绿 + 真机（CUDA 环境）跑通「计划→工具→审批→续跑」闭环
- [ ] 更新 §S.1 / §S.3 / 保存本文 / 报告可压缩历史

### 2.3 A2 —— 特性对齐（逐项开关、逐项校验，纪律同 MLX 侧当年上线）

- [ ] 开 Automatic Prefix Caching → 复测多轮会话 prompt_tps
- [ ] 开 ngram speculative → 仿 `scripts/probe_lookup_parity.py` 做贪心一致性探针
- [ ] 开 FP8 KV → 长上下文质量抽查（复用 TurboQuant 上线时的评估口径）
- [ ] 接 guided decoding：constrained 纯逻辑层 + torch mask 实现
- [ ] 每项独立开关独立回退；结果记 §S.1，异常记 §S.3
- [ ] 更新本文 / 报告可压缩历史

### 2.4 A3 —— 周边件 CUDA 化

- [ ] ASR：`asr.provider=faster_whisper`（CTranslate2/CUDA，large-v3-turbo 有
      现成转换权重），返回语义不变（fail-open 字符串）
- [ ] RAG：`rag_server.py` 的 `DEVICE` 改 `cuda > mps > cpu` 优先级；
      `torch.mps.empty_cache()` 两处按设备分派
- [ ] 更新本文 / 报告可压缩历史

---

## 3. 方向 B：重型计算分离（推理服务化）

### 3.1 目标拓扑

```
   设备 B（用户 Mac：执行层+前端）                 设备 A（GPU 服务器）
  ┌─────────────────────────────────┐            ┌────────────────────────────────┐
  │ frontend :5173                   │            │ inference-server :14161        │
  │ backend  :14159                  │            │   §1.1 协议的 HTTP/SSE 化       │
  │   编排/护栏/记忆/调度/账本        │  Tailscale │   （内包 vLLM CUDA 或 MLX 后端）│
  │   工具执行（作用于本机）          │◀══════════▶│ rag :14160（8B 嵌入+重排）      │
  │   RemoteBackend（协议实现）       │   Bearer   │ ASR（faster-whisper，可选）     │
  └─────────────────────────────────┘            │ SearXNG :8080（跟随部署，可选） │
                                                  └────────────────────────────────┘
```

- 会话数据（history.jsonl / memory.json / deck 等）**全部留在设备 B**——隐私
  边界不动：服务端只见 prompt 与媒体，不持久化会话。
- 单机模式 = InProcess 后端（现状）；分离模式 = RemoteBackend。同一协议两个实现。

### 3.2 B1 —— inference-server（新顶层目录 `inference/`，FastAPI，:14161）

**推进策略**：先包 MLX 后端在两台 Mac（或同机双进程）间验证分离，再换 CUDA——
A、B 两线解耦。

HTTP 面（把 §1.1 协议 SSE 化）：

```
POST /v1/generate                    SSE 流；body: {cache_id, prompt, media_ids[],
                                     sampling{...}, features{...}}
                                     事件: chunk(text,tokens,tps) / done(metrics) / error
POST /v1/generate/{gen_id}/cancel    段粒度取消（对应既有 cancel_requested 检查点）
POST /v1/media                       内容寻址上传（sha256 去重），返回 media_id
POST /v1/cache                       新建缓存 → {cache_id}
POST /v1/cache/{id}/clone            分支克隆 → 501 时客户端走冷启动回退（既有语义）
DELETE /v1/cache/{id}                冻结/删会话时释放
GET  /health  /metrics               devctl 探活 + 仪表
```

任务清单：

- [x] `inference/` 服务骨架 + 上表端点；Bearer 鉴权照抄 backend 的常量时间比对，
      密钥走 `.env.local`（新键 `INFER_API_KEY`，已补进 `.env.example`；
      实机验证无 key 401 / 错 key 401 / 对 key 200）
- [x] 服务端缓存注册表：`cache_id → CacheHandle`，TTL 空闲释放默认 45min
      （略长于 backend 侧 30min 冻结——释放顺序由 backend 主导，TTL 只兜底；
      config 键 `inference.remote.cache_ttl_minutes`）
- [x] `backends/remote/`：RemoteBackend。stream_generate = SSE 消费迭代器；
      媒体先查本地 sha256 记录、未传则上传；网络错误/超时映射为 RuntimeError
      → 整轮 fail 干净落账（拔线实测："核心引擎故障: 网络异常"）。缓存句柄
      **懒注册**（new_cache 在事件循环线程，不发网络请求），服务端经 meta 事件
      回填/换发生效 cache_id（重启/TTL 回收后自愈，见 §S.3）
- [x] **流式语义**：远程模式服务端做 detokenize，chunk 同时携带规范段与 delta
      展示增量——比清单预想更强：GenChunk.delta 原样跨网透传，中文逐 token
      流式不降级（engine 的 text 回退分支保留作后端无 delta 时的兜底）
- [x] config 新增 `inference: {mode, backend, remote: {host, port}}`；
      `agent.max_inference_concurrency`（默认 1，行为与现状全同——推理互斥锁
      改为等价的 Semaphore(1)；>1 仅对 remote 生效，进程内后端强制钳制回 1；
      调度器 ClassPolicy/抢占/老化**不动**）
- [x] 验收：mode=remote 同机双进程 → 金测试 + 实机冒烟全过（含拔线：SIGKILL
      中断→失败路由+账本重放，重启后同会话自愈）；mode=inprocess 回归零变化
      （同一会话跨模式无缝续跑）
- [x] 更新本文 / 报告可压缩历史（2026-07-14）

### 3.3 B2 —— RAG 家族参数化（含多模态加载）+ 鉴权 + 远程化 + 8B 升级

> **范围扩充（2026-07-14，用户拍板）**：原 B2 只到"8B 升级"。用户追加"兼容 Qwen3 家族
> 全 5 档"方向——文本 0.6B/4B/8B + 视觉多模态 VL-2B/8B（各有嵌入+重排，共 10 仓库）。
> 关键洞察：RAG 代码本就家族无关（`AutoModel`/`last_token_pool`/yes-no logits 全同），
> "兼容"≠新写兼容代码，= **选择制换档 + 换档护栏 + reembed**。换档纪律（用户澄清）：
> 档位是**开发者调试旋钮**非用户/运行时特性；库一旦入库档位即固定；换档 = 主动重建。
> 多模态分两层：**L1**（加载 + 文本 I/O，16GB M4 单拉 RAG bf16 可实测 2B VL）；
> **L2**（图像真入 RAG——API 收图/图文索引/检索）属独立能力，另立一期（需账本追加 + 用户同意）。
> 本机可完成的可移植切片 = **B2a**（下列前半已落地并验证，见 §S.1）；8B 实跑与跨机远程
> 依赖设备 A（16GB M4 装不下 8B×2，§4.1）。

- [x] （B2a）RAG 加 Bearer（照抄 backend auth；原"仅回环无鉴权"缺口**已补**）；
      backend 三客户端（rag_search/rag_admin/rag_sink）经 `config_loader.rag_auth_headers`
      统一注入 Authorization 头。实测无 key 401 / 错 key 401 / 对 key 200；空 key=dev 放行
- [x] （B2a）**档位注册表 + 选择制换档**：`config.rag.tiers`（5 档/10 仓库，每档
      {embed_repo, rerank_repo, modality, embed_dim, dtype, thresholds}）+ `active_tier`；
      `./devctl.sh rag-model` 交互选择写 `.rag-tier-selection` 指针（镜像主模型 `.model-selection`）；
      下载仍收敛 `rag/models/`。文本/VL 两条加载装配路径
- [x] （B2b, 2026-07-15）**VL 加载契约实测校准**（真机 2B VL 权重，单拉 RAG bf16）：
      重排改 `AutoModelForImageTextToText`（原 `AutoModelForCausalLM` 会 ValueError 崩）、
      删掉多余的 `trust_remote_code`、重排收尾串按族分派（VL 无 `<think>` 块，实测分离度
      0.61 vs 误用文本收尾 0.43）、`embed_dim` 回填 vl-2b=2048。验收：health tier=vl-2b/dim=2048、
      VL 全链路检索通、text-0.6b 回归零变化。**L2 图像入库仍未动**（另一期）
- [x] （B2a）**换档护栏**：建库给 chroma 盖维度戳（`.tier_stamp.json`：模型身份+维度），
      启动探测运行维度比对——不符即快速失败并提示先 `reembed`；入库处 chroma 维度错误
      同样包装成"先重建"提示，兜底旧库无戳边缘情形
- [x] （B2a）**reembed.py 脚本**：`rag/scripts/reembed.py`（dump-categories/wipe/rebuild/--dry-run）
      遍历 `storage/raw_files/{类}/` 走既有 `/insert` 流水线重灌
- [x] （B2b, 2026-07-15）**reembed 全流程首次实跑**（0.6B 库，真实换档触发护栏后重建）：
      dump→wipe→start→rebuild 全通，分类描述与检索复原。**实跑揪出原文语料每重建翻一倍**
      （raw_files 是承重网，迁移工具反而污染它）→ 修：`/insert` 加可选 `source_path`
      ＝"备份已在盘上就地索引"（缺省 None 时行为零变化）+ storage/ 越界 400 拦截 +
      就地重索引先清旧切片（按原路径清原库 + 目标库同路径，覆盖跨库重路由）使 rebuild
      可安全重跑。四条分支均实跑验证：同类重跑 ×2 幂等、未分类→分类重路由（原文**移动**
      而非复制、旧库无孤儿）、普通入库照旧另写备份、越界/不存在 source_path → 400。
      **8B 新库上的实跑仍待设备 A**（脚本本身已验）
- [x] （B2a）**user_id 埋点**（D3）：RAG API 加 header `X-Rag-User`（缺省 `"default"`，行为全同），
      `_scoped_collection` 默认用户返回原名；约定 collection `u_{user_id}__{category}`、
      原文 `storage/raw_files/{user_id}/{类}/`、meta 按 user 分库——**只定约定与参数面，不实现逻辑**
- [ ] （设备 A）`config.yaml` 的 `rag.host` 指向服务器（键已两侧共用+鉴权已补，跨机联调并入 B3）
- [ ] （设备 A）8B 权重落位 + 精度/显存档位按 §4.1 选定（档位表已备位，记录所选进 §S.1）
- [ ] （设备 A）**全量重建实跑**：新库全量重建 → 新旧库并行查询对比 → 切流 → 旧库归档
      （脚本机制已在 0.6B 上实跑验证并修好翻倍 bug，见 B2b；此处剩的是 8B 新库上的真实迁移）
- [ ] （设备 A）**阈值重标**（不可跳过）：入类 0.8 / 软路由 0.85 / rerank 0.5 是 0.6B 余弦空间
      实测值，各档分布不同（档位 thresholds 面已备位）。现库真实文档做召回回归集扫参重定，
      写进 config 注释（标定日期+依据）
- [ ] （L2 另一期）图像真入 RAG：`/insert`·`/query` 收图、图像切块/索引、图文混合检索
- [ ] ASR 路由（可选）：`asr.provider=remote` → 服务器 `/v1/asr`
- [x] （B2a）更新本文 / 报告可压缩历史（2026-07-14）

### 3.4 B3 —— 部署形态 + §7 接口预留 + 生产联调

> **联调硬件的现实（2026-07-15 用户澄清）**：两台机器都是**轻量 Mac**，只能跑小模型。
> 故 B3 的"设备 A"在联调期 = 一台 Mac 跑 `--profile gpu`（MLX 小模型 + RAG 0.6B），
> **不是** §3.1 拓扑图里的 GPU 服务器。这与 §3.2 早定的推进策略一致（"先包 MLX 后端在
> 两台 Mac 间验证分离，再换 CUDA"）——B3 验收的是**分离形态本身**（协议/profile/鉴权/
> 断线重连），换 CUDA 是 A 线的事，两者解耦。8B RAG 与 CUDA 仍待真正的 GPU 机器。

- [x] devctl profiles（2026-07-15）：
      `start`（现状 all-in-one，永不破坏——实测生成 27.6 tps 正常）/
      `start --profile edge`（设备 B：backend+frontend，校验远端 RAG 与推理可达）/
      `start --profile gpu`（设备 A：inference-server + rag (+searxng)）。
      配套：①devctl 此前**根本不管 inference-server**（B1 只手动起），补 start/stop/logs/status
      全套（跑 backend 的 venv，无独立 venv）；②探活 URL 改为按 config 的**绑定地址**组装
      （写死 127.0.0.1 会让绑 Tailscale 的服务被误判未就绪）；③`status` 对"本机无进程但探活通"
      报**远端就绪**而非未运行；④`restart --profile X` 透传 profile
- [x] **tokenizer-only fetch**（2026-07-15，§S.3 B1 早记的 B3 待办）：edge 侧
      `inference.mode=remote` 时 backend 只拉分词器（实测 **31MB** vs 全量 5.2GB），
      走**独立**标记 `.tokenizer_only_complete`——绝不写 `.download_complete`，否则同机切回
      inprocess 会命中标记跳过下载、加载器缺权重报错。按 config 的 mode 判（非 profile 旗标）
- [x] §7 四处接口预留逐项落（见 §7 表格"状态"列；#3 现场核实为**早已完成**，账本原记录有误）
- [ ] 生产拓扑联调：A+B 真两台机器全链路（含审批、分支、断线重连、定时任务）
      ——**待第二台机器**。同机双进程（gpu+edge 双 profile）已全链路实测通过：
      edge 的 backend 不加载权重、跨进程调 gpu 的 inference-server 生成成功（20.3 tps）
- [ ] 更新本文 / 报告可压缩历史

---

## 4. RAG 8B 参考资料（B2 执行时查阅）

生产档位 Qwen3-Embedding-8B + Qwen3-Reranker-8B（与 0.6B 同家族，接口/池化/
yes-no logits 重排语义全同，`rag_server.py` 代码路径不变，仅换权重与设备）。

### 4.1 资源档位

| 配置 | 显存（两模型合计） | 适用 |
|---|---|---|
| 0.6B ×2 fp16 | ~2.4GB | 现状（开发机 MPS） |
| 8B ×2 fp16 | ~30GB + 激活 | ≥40GB 卡（A100/A6000）或双卡分置 |
| 8B ×2 int8/AWQ | ~16GB | 24GB 卡（4090）可行，重排精度需抽查 |
| 嵌入 8B + 重排 4B | ~24GB fp16 | 折中档：重排对尺寸敏感度低于嵌入 |

config 已有 `rag.batch_size`；重排 K 值钳制（防峰值内存无界，见
`rag_server.py` 中 `/query` 的钳制注释）在 CUDA 上重新标定。

### 4.2 两个硬点（B2 清单里已列为必做）

1. **向量维度 1024 → 4096**：ChromaDB 现有 collection 全部失效，靠原文备份
   全量重建（§3.3 的 reembed 条目）。
2. **调优阈值全部重标**：三个距离阈值不可沿用（§3.3 的重标条目）。

---

## 5.（本节已并入 §S.1 的依赖关系说明，编号保留防旧引用失效）

---

## 6. 客户端形态决策：macOS 原生 app（已采纳，独立轨道，不占 A/B 依赖链）

**决策（D4）**：前端 + 应用壳原生化（SwiftUI），执行层保留 Python 作为 app 的
内嵌子进程。**否决项**：执行层全量 Swift 重写——生态硬墙（SymPy/openpyxl/
解析器无 Swift 等价物）、金测试与护栏调优资产归零（违背铁律 1 精神）、把维护
成本最高的语言压在变更频率最高的层上。

形态要点：

- **单一 .app 分发**：Python 执行层打包为 app 内嵌运行时，app 负责其生命周期。
  目标架构下 edge 侧走 RemoteBackend，设备端不装 MLX、不含模型权重，内嵌运行时
  很小。这是北极星（CHARTER §0"普通用户"）在分发层的达成方式。
- **TCC 责任进程**：辅助功能/自动化权限授予签名的 .app（Python 子进程继承归属），
  替代现在挂在终端/python 解释器上的混乱授权。
- **客户端 = 协议瘦渲染器**：移植 `lib/turn-reducer.ts` → `TurnReducer.swift`
  （`protocol/fixtures/` 夹具 + XCTest 锁行为；PROTOCOL.md 本就是三端标准，
  DEFERRED.md 已预名此文件）。协议 v1 冻结 → 客户端日常变更频率低。
- **AX/AppleScript 渐进 Swift 化**：`mcp/tools/accessibility.py`（pyobjc）与
  `apple_script.py` 做成 app 内原生工具服务（XPC 或本地端口），Python 编排经
  既有 BaseTool 探活注册机制调用。渐进替换，不是重写；其余工具留在 Python 生态。
- **web 前端保留**，定位"任意受信设备的远程窗口"。Deck 渲染：app 内 WKWebView
  装同一份 SlideRenderer 自托管包（`backend/static/webapp` 机制现成），
  "预览即所得"的 PDF 导出链路不破。

维护边界：语言数不变（Swift 客户端 + Python 服务侧）；护栏/工具/记忆的高频迭代
仍在 Python，金测试全保留；新增持续成本 = SwiftUI 界面 + 打包签名公证管线。

---

## 7. 三设备拓扑与 iOS 远程端（接口预留，不排期实施）

```
  设备 A（算力）              设备 B（生产）                    设备 C（随身遥控）
  GPU 服务器/高性能桌面机     笔记本/低性能桌面机               手机 / 随身笔记本
 ┌────────────────────┐    ┌─────────────────────────┐      ┌──────────────────┐
 │ inference-server    │    │ macOS 原生 app（§6）      │      │ iOS 原生 app      │
 │ rag (8B) / ASR      │◀──▶│  内嵌 Python 执行层       │◀────▶│  或任意浏览器开   │
 │ (§3 既有规划)        │    │  工具作用于本机           │ 账本  │  B 的 web 前端    │
 └────────────────────┘    │  账本 = 唯一真相           │ 协议  └──────────────────┘
        RemoteBackend       └─────────────────────────┘   HTTP+SSE / APNs
```

**核心判断**：C 层在协议上近乎免费。事件账本架构（CHARTER 铁律 2）下客户端是
无状态的"账本读者+提交者"——提交 202 即返、断线不改变轮次状态、带游标任意重放。
C 遥控 B = C 向 B 的账本提交轮次并附着观察；算力在 A 对 C 完全透明。
C 是笔记本时**零新增工件**；唯一新工件是 iOS app，与 macOS app 共享同一个
SwiftPM 包（TurnReducer + 协议模型 + API 客户端），UI 层各写各的。
审批远程决议（`awaiting_approval` → C 上 Face ID 放行）与 DEFERRED.md
"定时任务审批推送"条目合流，协议面已备。

**四处接口预留**（全部是"不写死错误假设"，B3 期落）：

| # | 预留点 | 现状 | 预留动作 | 状态 |
|---|---|---|---|---|
| 1 | **鉴权面：设备级票据** | 单一 `AGENT_API_KEY` 常量时间比对；原生 app 将**自持凭据** | auth 校验收敛为单一可替换函数（`verify_token(token) -> device_id?`）；config 预留 `auth.devices` 段。单密钥期行为全同 | **已落**（B3）：`framework/api/auth.py` 的 `verify_token()` 是唯一比对面（换 JWT/mTLS 只改它）；`config.auth.devices` 留空 = 单密钥期，`request.state.device_id` 恒 `"default"`；`tests/test_auth.py` 10 项锁住"单密钥期行为不变"+ 吊销隔离 |
| 2 | **绑定面：backend 直连暴露** | backend 绑 127.0.0.1，外部唯一入口是 5173 代理 | edge profile 允许绑 Tailscale 接口 + 直连 Bearer（原生 app 不走 Vite 代理） | **已落**（B3）：三服务的 host 本就来自 config（改值即可绑 Tailscale），补的是**代码强制的部署铁则**——非回环绑定 + 空密钥 = **拒启**（§8 此前只是散文）。三处各自实现（独立 venv 不共享代码），实测三服务全部拒启 |
| 3 | **推送：APNs 发送器插槽** | ~~`/push/*` 注册面已存在，无发送实现~~ **账本滞后：B3 开工前就已完整实现** | ~~发送器做成 provider 接口~~ | **早已完成**（非 B3 所为）：`framework/push/apns.py` 是完整的 ES256 JWT 直发客户端（生产/沙箱环境自动回退 + 粘滞学习）、`dispatcher.py`、config `push` 段、`tests/test_push.py` 11 项。缺 .p8 时抛 `APNsDisabled`、调用方静默降级 = 预留想要的 no-op 语义 |
| 4 | **多客户端并发附着** | 账本读者语义天然多端；web 单客户端实测，多端同时附着未验证 | 协议夹具补"双客户端同时 attach 同一会话"用例，把承诺变成被锁定的行为 | **已落**（B3）：夹具 `005-late-attach-rebuilds-pending`（晚到客户端只凭重放重建审批卡）+ `006-reattach-midturn-keeps-state`（流中途再来 `stream_attached` 不得冲掉累积态，**经变异测试确认会红**）。后端 fan-out 本就是每客户端一个 queue（`journal.py:142`），代码早支持、此前只是没人证明 |

发现/配对不发明协议：依赖 Tailscale MagicDNS，app 内手动配 host + 票据。
iOS 端无工具执行面（工具永远作用于 B），不触碰执行层任何代码。

---

## 8. 风险清单

| 风险 | 缓解 |
|---|---|
| gemma-4-e4b（含音频多模态）在 vLLM 的支持不完整 | A1 开工先做支持性 spike；不行则 CUDA 侧换同方言替代档位（model registry 加条目），或 llama.cpp 路径提前 |
| 思考预算锚定的两段式生成与 MLX 单段语义有差 | A1 验收用例逐字锁定；前缀缓存保证二段 prefill 近零成本 |
| 跨栈数值差异导致贪心翻转/护栏参数漂移 | 复用 probe_*_parity 探针纪律；护栏参数（步内预算/熔断）与后端无关，不动 |
| 8B 阈值沿用旧值导致召回质量静默退化 | §3.3 强制重标 + 新旧库并行对比期 |
| 远程模式网络分区打断轮次 | inflight 预写 + 失败路由菜单 + 账本重放全是现成机制；RemoteBackend 只需把网络错误映射到既有异常类型 |
| rag/inference 服务跨机暴露 | 两服务均加 Bearer；部署铁则：非回环绑定只允许出现在受信内网 |
| 缓存注册表泄漏（客户端崩溃未释放） | 服务端 TTL 兜底 + /metrics 暴露缓存计数 |
| **账本（本文）与现场脱节**（会话中断在期中，本文未同步） | 重入协议 §R 第 3 步强制现场核实；任务清单逐项勾选即保存；commit message 带期号前缀留 git 佐证 |
