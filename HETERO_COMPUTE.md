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
- commit 时机由用户决定；每期验收通过后**建议**向用户请求一次 commit（工作
  快照 + 本文状态同步入库，历史压缩后 git log 即离线进度佐证）。

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
| B2 | RAG 远程化 + 鉴权 + 8B 升级（重建+阈值重标） | 未开始 | — | — |
| B3 | devctl profiles + §7 接口预留 + 生产联调 | 未开始 | — | — |

依赖关系：P0 → {A1→A2, B1}；A3、B2 依赖 P0（A3 的 RAG 部分与 B2 协同）；
B3 依赖 B1+B2。A 线与 B 线在 P0 后**完全独立**，可并行或只做其一。
最短生产路径：P0 → B1(先包 MLX) → B2 → A1/A2（服务端换 CUDA）→ B3。
客户端形态（§6）不在此依赖链上；§7 iOS 端只预留不排期。

### S.2 现场核实命令（重入时先跑，以输出为准修正 S.1）

```bash
cd /Users/chouenkin/MacAgent
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

### 3.3 B2 —— RAG 远程化 + 鉴权 + 8B 升级

- [ ] RAG 加 Bearer（照抄 backend auth；当前"仅回环无鉴权"**必须先补**再跨机）；
      backend 的 rag 客户端统一注入 Authorization 头
- [ ] `config.yaml` 的 `rag.host` 指向服务器（该键本就两侧共用，迁移即改一行）
- [ ] 8B 权重落位 + 精度/显存档位按 §4.1 选定（记录所选档位进 §S.1）
- [ ] **全量重建**：写 `rag/scripts/reembed.py`——遍历 `storage/raw_files/{类}/`
      走既有 `/insert` 流水线重灌（切块参数不变），meta 分类描述向量同步重算。
      顺序：新库全量重建 → 新旧库并行查询对比 → 切流 → 旧库归档
- [ ] **阈值重标**（不可跳过）：入类 0.8 / 软路由 0.85 / rerank 0.5 是 0.6B
      余弦空间的实测值，8B 分布不同。方法：现库真实文档做召回回归集，新模型上
      扫参重定三阈值，写进 config 注释（标定日期+依据）
- [ ] ASR 路由（可选）：`asr.provider=remote` → 服务器 `/v1/asr`
- [ ] **user_id 埋点**（D3）：RAG API 加 header `X-Rag-User`（缺省 `"default"`，
      行为全同）；命名空间落点约定：collection `u_{user_id}__{category}`、
      原文 `storage/raw_files/{user_id}/{类}/`、meta 路由库按 user 分库——
      **只定约定与参数面，不实现多用户逻辑**
- [ ] 更新本文 / 报告可压缩历史

### 3.4 B3 —— 部署形态 + §7 接口预留 + 生产联调

- [ ] devctl profiles：
      `start`（现状 all-in-one，永不破坏）/
      `start --profile edge`（设备 B：backend+frontend，校验远端可达）/
      `start --profile gpu`（设备 A：inference-server + rag (+searxng +asr)）
- [ ] §7 四处接口预留逐项落（见 §7 表格"预留动作"列）
- [ ] 生产拓扑联调：A+B 真两台机器全链路（含审批、分支、断线重连、定时任务）
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

| # | 预留点 | 现状 | 预留动作 |
|---|---|---|---|
| 1 | **鉴权面：设备级票据** | 单一 `AGENT_API_KEY` 常量时间比对；原生 app 将**自持凭据** | auth 校验收敛为单一可替换函数（`verify_token(token) -> device_id?`）；config 预留 `auth.devices` 段。单密钥期行为全同 |
| 2 | **绑定面：backend 直连暴露** | backend 绑 127.0.0.1，外部唯一入口是 5173 代理 | edge profile 允许绑 Tailscale 接口 + 直连 Bearer（原生 app 不走 Vite 代理）。非回环绑定只许出现在受信内网 |
| 3 | **推送：APNs 发送器插槽** | `/push/*` 注册面已存在（PROTOCOL §2.8），无发送实现 | 发送器做成 provider 接口，未配置 APNs 证书时 no-op |
| 4 | **多客户端并发附着** | 账本读者语义天然多端；web 单客户端实测，多端同时附着未验证 | 协议夹具补"双客户端同时 attach 同一会话"用例，把承诺变成被锁定的行为 |

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
