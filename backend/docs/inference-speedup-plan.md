# 推理加速优化计划：投机采样 + 约束采样

> 状态（2026-07-07 更新）：**Phase 0~3 已在开发机（e4b）完成并验证，代码在分支
> `feat/inference-speedup`（3 个 commit，已推送 origin）。**
> 剩余 Phase 4（e2b 草稿模型）与 Phase 5（组合调优+全量回归）需生产机执行，
> 本文 §6~§9 即为生产机执行手册——不依赖任何聊天记录即可续作。

## 1. 背景与目标

- **生产目标硬件**：Mac Studio M4 Max 128G 级（带宽 ~546 GB/s）。
- **主模型**：Gemma-4-31B（4bit，权重 ~17GB）。decode 纯带宽理论上限 ~30 tok/s，实测预期 ~20 tok/s。
- **选型定案**：纯文本 MoE（gpt-oss 系）因无视觉被否决（桌面操控依赖截图）；Gemma-4 锚定为主模型；草稿模型 **e2b 优先**（词表校验已通过，见 §3）。
- **目标**：decode 有效吞吐 ×1.5~2.5；工具调用废轮清零；全程同口径可量化。

## 2. 已完成：Phase 0 仪表（commit d0/`feat: 推理加速 Phase 0 仪表…`）

- `ChatResponse` 新增（整轮累计口径，全 Optional 向后兼容）：
  `token_breakdown{thinking,visible,tool_json,marker_overhead}`、`generations`、
  `generation_tokens_turn`、`parse_error_count`（废轮）、`spec_drafted/spec_accepted`（接受率）。
- 实现位置：`engine.py::_token_breakdown`（按频道切段重编码）、`orchestrator.py` 轮级累计
  （`_stamp_turn_stats`）、`schemas.py`。
- **基准脚本 `scripts/bench_inference.py`**：5 类固定负载（自由中文/工具轮/复述/计划链/thinking），
  用法见文件头注释——`baseline`/`enabled` 两跑对比，结果落 `/tmp/bench_{tag}.json`。
  **生产机装好 31B 后第一件事就是跑它。**

## 3. 已完成：Phase 1 前置检查（结论均为 GO）

| 检查 | 脚本 | 结论 |
|---|---|---|
| 词表一致性 | `scripts/probe_draft_vocab.py` | e2b / e4b / 31B 词表 262144 逐 id 抽样一致、added_tokens(24) 一致 → **e2b 草稿路线成立** |
| KV 细粒度回滚 | `scripts/probe_spec_rollback.py` + `_impact.py` | `rollback_speculative_cache` 短上下文**逐位无损**；滑窗（512）旋转后被拒草稿会覆盖环内最老槽位、trim 不可恢复，但**分布级 KL≈5e-4、top-1 不变、连续 10 轮不累积**（污染被后续真实 token 自然淘汰）→ 放行；TurboQuant(kv_bits=3.5) 同口径 → 放行 |

重要事实（当时的关键发现，后续别再重查）：
- 装的 mlx_vlm **已原生支持** `generate_step(logits_processors=…)` 钩子与 `draft_model`
  投机（dflash/mtp 两种训练型草稿头），并提供 `_speculative_walk` 与 gemma4 的
  `rollback_speculative_cache(caches, None, accepted, block_size)`（trim = block_size-accepted-1；
  accepted=-1 可整块作废）。
- 31B/e2b 的 HF 仓库名：`mlx-community/gemma-4-31b-it-4bit`、`mlx-community/gemma-4-e2b-it-4bit`。

## 4. 已完成：Phase 2 约束采样（工具调用段 schema mask）

- `framework/core/constrained.py`：llguidance（requirements 已加）JSON-schema →
  bitmask logits mask。**只约束 `<|tool_call>`(id 48)…`<tool_call|>`(id 49) 区间**；
  JSON 到达可接受态即只放行收尾标记强制闭合；自由文本/思考段不受约束。
- 状态机 `ToolCallGuard` 双协议：普通路径走 generate_step 的 `(tokens, logits)`
  processors 协议（首调用视为 prompt 边界，历史回放的旧调用不会误触发）；
  投机路径走 `consume(token)/mask_logits(row)` 直连协议（**只消费已产出 token，
  被拒草稿不污染状态机**——Phase 4 组合时保持此约定）。
- 全链路 fail-open：缺库/语法编译失败/运行异常 → 告警并回自由采样，parser 纠错闭环兜底。
- 装配：`engine.py::_tool_guard`（LLTokenizer 进程内单例 ~1s 摊销、语法按工具集缓存、
  matcher 借推理互斥锁复用）；开关 `config.yaml inference.constrained_tool_calls`。
- 实测：每次工具调用 16~131 步被 mask，回归集废轮 0；未接入沙箱（遗留项，见 §8）。

## 5. 已完成：Phase 3 prompt-lookup 投机

实现全部在 `framework/patch/gemma4_generate.py`：

- `lookup_generate_step`：prefill 与上游 generate_step 同构（get_input_embeddings +
  分块 prefill + 末步采样），decode 换成轮循环：`_NgramIndex` n-gram 草稿（**单槽最近
  出现**，实测优于次近换长草稿）→ 主模型批量验证 → 走查 → 拒绝回滚。
  **轮内仍逐 token yield，外层 detokenizer/停止判定/流式过滤协议零改动**
  （计划里担心的 stream filter 撕裂问题在此设计下不存在）。
- 三层性能机制（调优时按此顺序理解代码）：
  1. **AIMD 草稿长**：全收 +2、全拒减半、下限 1（`cur_k`）；
  2. **冷却退避**：连续 3 轮全拒 → 暂停出草稿 8~64 轮指数退避（`miss_streak/cooldown`）；
  3. **轮间流水线 + 图内预折叠**：walk→回滚后立即 `_launch` 异步预发下一轮，且按
     「草稿全接受」假设把逐行 repetition_penalty+采样折进异步图（`pre`），同步路径只剩
     一次 `.tolist()`；guard 激活段（工具 JSON）回退逐位带 mask 走查。
- thinking 预算强制收束在轮循环内对齐 `apply_forced_token` 语义（含块中截断回滚 +
  预发轮作废 `_discard_block`）。
- 开关：`config.yaml inference.speculative.{mode,num_draft_tokens,ngram_max,ngram_min}`，
  `mode: off` 时路径与改动前完全一致。`engine.py` 透传 + `spec_stats` 回传。

### 开发机实测（e4b，服务端 A/B，`bench_inference.py`）

| 负载 | 基线 tps | 开启 | Δ | 接受率 |
|---|---|---|---|---|
| 复述/引用（t3） | 32.7 | 57.9 | **+77%** | 87% |
| 计划链（t4） | 30.7 | 34.3 | +12% | 37% |
| thinking（t5） | 32.2 | 32.5 | 持平 | 36% |
| 工具单调（t2） | 31.9 | 26.9 | -16%* | 28% |
| 自由中文（t1） | 31.9 | 27.9 | -13% | 4% |

\* 开发机热漂移噪声 ±10%（同代码基线在 26.7~33 间波动），短轮放大噪声。

**已知边界（重要，非 bug）**：
- 贪心逐位一致性验证通过（`scripts/probe_lookup_parity.py`），唯一分歧模式是
  **浮点平局翻转**：批量验证（S>1）与单步核累加顺序不同，top-2 logprob 差=0 的位置
  argmax 可翻转——实证分歧位 gap=0.000000，属良性破缺，生产温度采样下无影响。
  探针判据已含此容差（gap<1e-3 判良性）。
- 自研循环固定 python 开销 ≈3~5ms/token（e4b 单步 31ms 的 10~15%）——这是自由散文
  负收益的地板。**开销是常数、收益是比例**：31B 单步 ~47ms，占比自然减半。

## 6. 待做：Phase 4 e2b 草稿模型（生产机）★ 从这里续作

### 6.1 先测基线与常数（半天）

1. 31B 装机、`config.yaml inference.model` 换 31B；两特性先关
   （`constrained_tool_calls: false`、`speculative.mode: "off"`），跑
   `python3 scripts/bench_inference.py baseline`。
2. 打开两项重启，跑 `enabled`，确认 §5 结论在 31B 上成立（预期：t1/t2 负收益收窄到
   -5% 内，t3/t4 增幅放大）。
3. 下载 e2b：`mlx-community/gemma-4-e2b-it-4bit`（~1.5GB）。单独量一下 e2b 裸 decode
   单步耗时 → 得草稿开销比 c = t_e2b/t_31B（理论 ~0.06，python 开销可能恶化到 0.15~0.2，
   c 决定 k 的甜点：c≈0.1 时 k=4~6，别用 8）。

### 6.2 两级草稿实现（1~2 天）

改动集中在 `lookup_generate_step`，结构已为此铺好：

- **草稿源替换**：`_next_draft()` 改为两级——n-gram 命中则用（免费且接受率高），
  未命中时用 e2b 自回归草 k 个 token（替代现在的"返回空"）。e2b 持有**自己的 KV cache**，
  与主模型缓存同步推进：主模型每接受/产出一个 token，e2b 侧 `update` 同一 token；
  拒绝时 e2b 缓存同样 trim（e2b 是稠密小缓存，直接 `c.trim(n)`）。
- **验证/回滚/流水线全部复用现状**：verify forward、`_speculative_walk` 式走查、
  `rollback_speculative_cache`、AIMD、冷却（冷却逻辑对 e2b 草稿应放宽或停用——
  e2b 草自由文本命中率高，冷却是为 n-gram 的 0% 场景设计的）。
- **约束组合约定（勿破坏）**：guard 的 mask 必须**两侧同挂**——e2b 草稿时每个草稿
  token 也过 `guard.mask_logits`（用 guard 的 `deep_copy`/`rollback(num_tokens)` 支持
  草稿态推进、拒绝后回滚 llguidance 状态；llguidance matcher 原生有 `rollback`）。
  装反/漏挂会出现"草稿全被否决、投机负收益"。
- **装配**：`app.py` 启动时按 `inference.speculative.draft_model` 加载 e2b（懒加载亦可），
  传入 engine → stream_generate kwargs；config 增：
  ```yaml
  speculative:
    mode: "draft"            # off | prompt_lookup | draft（draft=两级草稿）
    draft_model: "models/gemma-4-e2b-it-4bit"
    num_draft_tokens: 5      # 按 6.1 测得的 c 定：k*c 控制在 0.5 以内
  ```
- **词表已验证一致**（§3），e2b 草稿 token id 可直接送主模型验证，无需映射。

### 6.3 视觉轮与摘要 A/B（各半天）

- 视觉轮单独观测：e2b 不看图（图像在主模型前缀 KV 里，草稿只做文本续写），看图描述段
  接受率预期低——GUI 轮收益主要在 JSON 动作段，`spec_accepted/drafted` 按轮分开记，
  别让平均数掩盖。
- 摘要一鱼两吃 A/B：e2b 直跑滚动摘要 vs 31B，同一批真实会话对比关键事实保留率与语言
  跟随。够用则 `engine.summarize` 切 e2b（摘要快数倍，调度抢占触发频率自然下降）；
  不够用则只迁会话标题类轻任务。

### 6.4 预期收益（按 e4b 实测常数外推，装机后以实测为准）

加速比 ≈ (E[接受]+1)/(1+k·c+开销)。同家族 15× 参数差草稿接受率文献口径 α≈0.6~0.75：

| 负载 | 只 prompt-lookup（31B） | +e2b 两级草稿 |
|---|---|---|
| 复述/引用 | 1.9~2.3x | 2.0~2.5x |
| 工具/计划轮 | 1.15~1.3x | 1.5~2.0x |
| 自由对话/thinking | 0.93~0.97x | 1.3~1.7x |
| 混合加权 | ~1.2~1.4x | **~1.5~2.0x**（20tps→30~40tps） |

最大不确定项：e2b 对 31B 的真实 α、e2b 实际单步耗时 c——6.1 先测这两个数再定 k 与预期。

## 7. 待做：Phase 5 组合调优 + 全量回归（生产机）

- 约束 + 两级草稿 + TurboQuant(kv_bits=3.5) 全开回归：三语镜像、审批挂起→批准、
  多步计划、摘要折叠、后台抢占取消（摘要在投机路径中被 cancel）、分支/删轮、
  **长任务 GUI 多步操控（截图轮不劣化）**。
- `bench_inference.py` 产出最终前后对比报告；视觉轮口径单列。
- 全绿后合并 `feat/inference-speedup` → main。

## 8. 遗留项（非阻塞，择机做）

- 沙箱 `research_sandbox` 的 web_search/open_pages 尚未接约束采样（主 Agent 已接）。
- 自由散文下自研循环仍有 ~3-5ms/token 固定开销（31B 上占比 ~6-9%）；若在意，
  方向是把 lm forward 的 python 构图也挪进重叠区（收益有限，优先级低）。
- `speculative.mode=off` + `constrained_tool_calls: false` 即完全回退到改动前路径，
  两项互相独立。

## 9. 复测/排障速查

```bash
# 词表（改草稿模型时重跑）
python3 scripts/probe_draft_vocab.py
# KV 回滚无损性（升级 mlx_vlm 后重跑；含 TurboQuant 口径）
python3 scripts/probe_spec_rollback.py && python3 scripts/probe_spec_rollback_impact.py
# 投机贪心一致性 + 加速比（改 lookup_generate_step 后必跑）
python3 scripts/probe_lookup_parity.py
# 服务端 A/B（改 config 两跑）
python3 scripts/bench_inference.py baseline   # 两特性关
python3 scripts/bench_inference.py enabled    # 两特性开
```

排障要点：约束器问题看服务日志 `[Constrained]`（fail-open 会打异常类型）；投机问题先看
`spec_accepted/spec_drafted`（接受率异常低→草稿源问题；为 0→speculative 未生效）；
输出乱序/重复→查 `rollback_speculative_cache` 调用处的 accepted/block_size 数学
（trim = block_size - accepted - 1，整块作废用 accepted=-1）。

## 10. 明确不做

- 3bit 及以下权重量化（JSON 稳定性不可接受）；
- 全程 grammar 约束（伤多语言自由表达）；
- 换纯文本 MoE 主模型（视觉是桌面操控硬需求）；
- 堆硬件路线。
