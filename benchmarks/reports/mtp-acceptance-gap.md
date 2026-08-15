# vLLM vs mlx_vlm 的 Gemma-4 MTP 收益差：命题核验、机制剖析与修复机会

> ⚠️ **时效声明（2026-08-15 补）**：本报告成文于 2026-08-14，其后一天内多项内容被
> `MTP_PROJECT.md` 的实测**取代或推翻**，本文未逐段回写：
> - **§2.3「专家并集税是主因」已被推翻两次**：先改判"(token,专家) 对数调度成本"
>   （该微基准又被评审打穿，MTP_PROJECT §4.20），终判为"内核逐专家重读的字节问题 +
>   两平台离各自 roofline 距离不同"（MTP_PROJECT §1.8-MoE、§4.24）。
> - ⑤未决表的 **U2/U3/U4/U5/U6 已全部实测闭环**（分别见 MTP_PROJECT §3.2-B1、
>   §1.3/§1.3-b、§1.2+§1.6、§1.8-MoE、§4.13/A1）；**U1 仍开放**（已继承为
>   MTP_PROJECT §3.1 A9），U7/U8 仍开放（词表裁剪已列后备）。
> - 阅读本文只取"命题核验/单位归一"的方法论与源码索引；**一切数字与归因以
>   `MTP_PROJECT.md` 现行版为准**。

调研日期 2026-08-14。全程零真模型进程、零服务起停、零权重装载——只读源码 + 联网检索 + 台账。

证据强度标注沿用四档：**［源码实证］／［官方文档］／［社区报告］／［推断］**。凡标［推断］者，
均给出可证伪路径；凡检索不到者明说检索不到。

版本口径：mlx_vlm **v0.6.13**（本机 `backend/.venv/lib/python3.13/site-packages/mlx_vlm/`），
vLLM 侧读 GitHub `main`（PR #41745 已于 2026-05-06 合入）。

---

## ① 命题核验与单位归一

### 1.1 结论先行

> **用户命题「vLLM 命中率高、mlx_vlm 命中率低」不成立。归一后 mlx_vlm 的接受率不低于
> vLLM，在同为 4bit 目标件的可比格上反而显著更高。真实的差距不在接受率，在
> 「接受率→墙钟加速」的转化效率，以及一个此前无人察觉的实现事实：我们实际跑的草稿深度
> 是 3，不是以为的 5。**

### 1.2 两侧统计量的精确定义

**mlx_vlm ［源码实证］**

`speculative/common.py:212-229` `_record_speculative_round(draft_model, accepted, draft_count)`：

- `speculative_total_rounds` += 1（每轮 +1）
- `speculative_total_accepted` += `accepted`
- `speculative_total_drafted` += `draft_count`

唯一调用点在 `speculative/mtp.py:642`：

```python
_record_speculative_round(draft_model, accepted, bs - 1)
```

`accepted` 由 `_mtp_acceptance_walk`（`mtp.py:393`）返回，语义是**首次不匹配之前被接受的草稿
token 数**，**不含 bonus**（`common.py:151-157` `_speculative_walk`：`accepted` 在第一个
`draft_tok != target_tok` 处截断，bonus 只进 `new_tokens`，不进 `accepted`）。
`draft_count = bs - 1`，即本轮实际起草的 token 数（`bs` 是块总长，含 bonus 槽）。

故我方口径的「接受率」= `accepted / drafted` = **每个草稿位的接受概率**，是一个**比例**。

上游自己也算另一个量，`common.py:284-296`：

```python
mean_accept          = accepted_drafts / rounds
mean_accepted_tokens = (accepted_drafts + rounds) / rounds     # ← 含 bonus
accept_rate          = 100 * accepted_drafts / sum(draft_lens) # ← 我方报的这个
```

`mean_accepted_tokens` 才是与 vLLM 同名同义的量。

本栈补丁 `backend/framework/backends/mlx/patch/mtp_processors.py:159-217` 替换了走查实现，
但注释第 178-181 行明确「计数语义**严格照抄上游**：预算满后仍继续走、仍累加 accepted」，
且 `accepted` 仍只在 `token == draft_list[pos]` 时 +1、bonus 不计。**统计口径未被补丁改动**。

**vLLM ［源码实证］**

`vllm/v1/spec_decode/metrics.py`：

```
mean_acceptance_length = 1 + (num_accepted_tokens / num_drafts)
# 注释原文：Conventionally, mean acceptance length includes the bonus token
```

`num_drafts` 每次投机尝试 +1（等价于 mlx 的 rounds），`num_accepted_tokens` 累加被接受的
草稿 token（`assert num_accepted_tokens <= self.num_spec_tokens`，同样不含 bonus）。

**结论：两边的分子（accepted）语义完全一致，差别只在归一化分母和是否 +1 bonus。**

$$\text{AL} = 1 + \gamma \cdot r,\qquad r = \frac{\text{accepted}}{\text{drafted}},\ \gamma = \text{每轮草稿数}$$

### 1.3 致命发现：我方实际的 γ 是 3，不是 5

我们下发 `draft_block_size=6`（期望 γ = 6−1 = 5）。但：

1. `speculative/drafters/gemma4_assistant/config.py:23`：`block_size: int = 4`（**默认值**）。
2. 三个官方 assistant 件的 `config.json` 中**都没有 `block_size` 键**——已逐件核对
   `backend/models/gemma-4-{31b,26b-a4b,e4b}-it-assistant-bf16/config.json`，
   只有 `backbone_hidden_size` / `num_centroids` / `use_ordered_embeddings` / `text_config.*`。
   故 `configured_block_total` 恒 = **4**。［源码实证 + 本地文件实证］
3. `mtp.py:447-478` `_effective_mtp_block_size` 把 configured 当作**天花板**：

```python
block_total = min(requested_block_total, remaining_budget)          # 6
configured_block_total = min(configured_block_total, block_total)    # 4
if block_total <= configured_block_total or configured_block_total <= 1:
    return block_total
if len(accept_lens) < 8:
    return configured_block_total                                    # 前 8 轮恒 4
recent = accept_lens[-32:]
configured_draft_count = configured_block_total - 1                  # 3
configured_prefix_hit_rate = (# rounds with accepted >= 3) / len(recent)
if configured_prefix_hit_rate < 0.65:
    return configured_block_total                                    # ← 恒走这条
return block_total
```

要解锁 γ=5，需要最近 32 轮里 **≥65% 的轮次连中 3 个草稿**。在 r≈0.54 的水平上
P(全中 3) 量级 0.15–0.30，永远够不到 0.65。逃生阀 `prefer_requested_block_size`
（`mtp.py:486-487`）在本仓**无任何赋值点**（`grep -rn prefer_requested_block_size backend/` 空）。

**故三档预扫的真实 γ = 3。**

**算术自洽校验［源码实证 + 数据实证］**：31B 记录 `accepted 257 / drafted 474`。
474 / 3 = **158（整除）**；474 / 5 = 94.8（非整数）。且 158 轮 × AL 2.63 = 415 tok，
与 400 tok 预算 + 尾轮溢出吻合；若 γ=5，凑够 400 tok 需 ~107 轮 → drafted ≈ 537 ≠ 474。
**γ=3 是唯一自洽解。**

顺带一条：`backend/scripts/smoke_mtp_drafter.py:43` 里的 `"num_draft_tokens": 8` 是**死键**
——`grep -rn num_draft_tokens` 在整个 mlx_vlm 包中**零命中**。唯一生效的旋钮是
`draft_block_size`，而它又被上面的天花板吃掉了。

### 1.4 归一后的对表

| 侧 | 目标件 | 量化 | γ | 每位接受率 r | **AL = 1+γr** | 加速比 | 证据 |
|---|---|---|---|---|---|---|---|
| mlx_vlm | e4b | 4bit | 3 | 51.3% | **2.54** | 1.36× | 实测（本机） |
| mlx_vlm | 26B-A4B | 4bit | 3 | 40.6% | **2.22** | 0.79× | 实测（本机） |
| mlx_vlm | 31B | 4bit | 3 | 54.2% | **2.63** | 1.46× | 实测（本机） |
| vLLM | 31B | fp8 | 4 | (42.5–60%) | 2.7–3.4 | 3.12× | **反拟合，非实测** |
| vLLM | 31B | NVFP4 | 4 | (22.5%) | 1.9 | 1.7× | **反拟合，非实测** |
| vLLM | 31B | bf16 | 8 | 未记录 | 未记录 | 3.19× | 官方 PR（H100） |
| vLLM | 26B-A4B | bf16 | 4 | 未记录 | 未记录 | 1.60× | 官方 PR（H100） |

括号内的 r 是我按 r=(AL−1)/γ 反推的，仅供对齐，不是任何一侧的实测值。

**「2.7–3.4 / 1.9」不是实测量。** 台账 `EXECUTION_LEDGER.md:411-414` 原文是
「四点实测（…四个加速比…）**拟合**出解释模型：…接受率随目标量化劣化（8bit系 a≈2.7-3.4
vs 4bit系 ≈1.9…）」——**a 是从四个墙钟加速比反解出来的自由参数**，CUDA 侧从未直接读过
`mean_acceptance_length`。vLLM PR #41745 的官方基准表也**只报 TPS 和 speedup，不报 AL 或
接受率**（已逐字核对 PR body）。［官方文档 + 台账实证］

### 1.5 命题的正确表述

归一后：

- **同为 4bit 目标件**：mlx 31B **AL 2.63 / r 54.2%（实测）** vs vLLM 31B NVFP4
  **AL 1.9 / r 22.5%（反拟合）**。mlx **高出约 38%（AL）/ 2.4×（r）**。
- **对上 8bit 档**：mlx 的 r=54.2% 落在 vLLM 8bit 反拟合带 42.5–60% 的**内部偏上**。

差距在**转化效率**——把「理论天花板 AL 倍速」兑成实际墙钟的比例：

| 格 | AL（理论上限倍速） | 实际加速比 | **转化率** |
|---|---|---|---|
| mlx 31B-4bit | 2.63 | 1.46× | **55.5%** |
| mlx e4b-4bit | 2.54 | 1.36× | **53.5%** |
| mlx 26B-A4B-4bit | 2.22 | 0.79× | **35.6%** |
| vLLM 31B bf16 γ=8 | ≥3.19（下界） | 3.19× | **≥80%**［推断］ |

> **真正的命题应当是：mlx_vlm 每轮起草得少（γ=3 vs 4–8）、且把约 45% 的理论红利烧在
> 每轮固定开销上；vLLM 起草得多、且靠 CUDA Graph + 单次批量验证把这份开销压到近零。
> 接受率是这场比较里唯一 mlx 不吃亏的维度。**

---

## ② 机制对比（逐轴，带源码证据）

### 2.1 草稿模型形态——**核心假设被推翻：两边是同一样东西**

**结论：vLLM 与 mlx_vlm 用的是同一个官方 checkpoint、同一种架构、同一种算法。
不存在「vLLM 走 MTP 头、mlx 走独立小模型」的差别。**［源码实证 × 双侧］

Gemma-4 的所谓 MTP 并不是模型自带的 in-weights 头（那是 Qwen3.5/DeepSeek-V4 的形态），
而是 Google 单独发布的 `google/gemma-4-{E2B,E4B,26B-A4B,31B}-it-assistant`——一个
**4 层、Q-only 注意力、复用目标件 KV 与输入嵌入**的轻量解码器。两边都装这个件。

| 特征 | mlx_vlm | vLLM |
|---|---|---|
| 共享目标 KV | `mtp.py:28-55` `_mtp_shared_kv_from_prompt_cache()` 直接从目标 prompt cache 按 `layer.layer_type` 取 (K,V)；`gemma4_assistant.py:201-211` 每层 `layer(h, cache=None, shared_kv=kv, ...)` | `vllm/v1/spec_decode/gemma4.py` `_setup_gemma4_kv_sharing()`，经 `attn.kv_sharing_target_layer_name` 指向 `model.layers.{idx}.self_attn.attn` |
| Q-only 层 | `gemma4_assistant.py:23` `DecoderLayer(text_config, layer_idx=i, kv_shared_only=True)`；`models/gemma4/language.py:136,168,177` 在 `kv_shared_only` 时不建 K/V 投影 | `vllm/model_executor/models/gemma4_mtp.py`，PR 原文「lightweight decoder with Q-only attention layers that share KV cache with the target model」 |
| 消费目标隐状态 | `gemma4_assistant.py:270` `inputs_embeds = concat([tok_embed, h_prev])`，经 `pre_projection: 2*backbone_hidden → hidden`（`:40-42`）；出口 `post_projection` 回投 backbone（`:44-45, :214`） | 同构（`gemma4_mtp.py`） |
| 借用目标嵌入 | `gemma4_assistant.py:67-93` `bind()` 直接持有目标的 `embed_tokens` 与 `embed_scale` | 同 |
| 单位置多 token | `gemma4_assistant.py:236-237` 注释「`position_ids` is held constant across all draft steps」，实现见 `:255-258, :268-283` | `Gemma4Proposer.__init__`：`self.constant_draft_positions = True`，注释「All draft steps predict from the same position」 |
| centroids 掩码 | `drafters/gemma4_assistant/masked_embedder.py`（`use_ordered_embeddings` 开关） | `_setup_centroids_cuda_graphs()`，PR 原文 lm_head「~262K → ~4K」 |

**`mtp.py:_mtp_rounds` 的 `shared_kv_states` / `hidden` 参数不是徒有其名。**
`_mtp_rounds`（`mtp.py:523`）每轮把 `verify.hidden[:, accepted:accepted+1, :]` 传给下一轮
（`:658`）、把 `_slice_shared_kv_after_reject` 后的 K/V 重新 `set_shared_kv`（`:665-671`）。
共享隐状态与共享 KV 都是**真接线**。［源码实证］

**推论：一切「换成 MTP 头」的修复方向都不成立——mlx 已经在跑 MTP 头式的算法了。**

### 2.2 草稿长度与自适应

| | mlx_vlm | vLLM |
|---|---|---|
| 静态 γ | `--draft-block-size`，但被 §1.3 的天花板吃掉，官方件上恒 = 3 | `num_speculative_tokens`，直通，PR 基准用到 γ=8 |
| 动态 γ | **有**：`mtp.py:447-478` 按最近 32 轮的「满深度命中率 ≥65%」上调；`mtp.py:480-490` `_mtp_next_block_size` 分发 | **无**（Gemma4Proposer 无 adaptive k、无 early exit、无 disable-by-batch-size）［源码实证］ |
| 提前终止 | 走查在首次不匹配处 `break`（`mtp.py:216-222`、补丁 `:205-215`） | 同（拒绝即止） |
| cooldown | 无 | 无 |

**讽刺之处：mlx 是两边里唯一有自适应机制的，而这个机制正是当前收益受限的直接原因**
——它被设计成只向上放宽、且把一个来自默认值（而非 checkpoint）的 4 当作硬顶。
mlx-community 的 31B assistant 模型卡明写
「Recommended `--draft-block-size`: `6` for single requests, `3` for batched generation」
［官方文档］，而这个推荐值在**任何**已发布的 gemma-4 assistant 件上都是**静默失效**的。
这是一条可上报上游的实质缺陷。

### 2.3 验证批的实现代价

**vLLM**：一次 batched verify，一次 lm_head，一次同步；centroids 路径还有 CUDA Graph
（捕获 batch size [1,2,4,8,16,32,64]）。［源码实证］

**mlx_vlm**：分三处付钱。［源码实证］

1. **走查是逐位串行的，且每位一次全 GPU 同步。**
   `mtp.py:194-222` `_speculative_walk_deferred_greedy`：

   ```python
   for pos in range(n_draft + 1):
       logits = lm.speculative_logits_from_hidden(target_hidden[:, pos:pos+1, :])
       ...
       mx.eval(target_token)          # ← 每位一次阻塞同步
   ```

   本栈补丁 `mtp_processors.py:186-215` 结构相同（多一层处理器 apply）。
   设计意图是「只投影到被拒为止，省 FLOPs」；但 Mac 是**带宽瓶颈**机器：
   γ+1 个位置一次批量投影只读一遍 lm_head 权重，逐位则读 γ+1 遍。
   **这是一个在 CUDA 上正收益、在统一内存上负收益的优化。**

2. **融合快路从不生效。** `mtp.py:136-175` `_mtp_verify_target` 的批量 argmax 快路需要
   `sample_target_tokens=True`，而 `_mtp_rounds:626` 传的是 `greedy_sampling`；
   本栈调用点 `backend/framework/backends/mlx/patch/gemma4_generate.py:949` **未传
   `greedy_sampling`**（默认 False）。且 gemma-4 语言模型只实现了
   `speculative_logits_from_hidden`（`models/gemma4/language.py:668`），
   **没有** `speculative_argmax_from_hidden` / `speculative_verify_logits`
   （grep 零命中）。故三条快路全不可达，恒走逐位路。

3. **每轮的 KV 搬运。** `mtp.py:28-55` 每轮重建 `shared_kv_states` 字典（旋转缓存还要
   `_temporal_order()` 复制，`:48-50`）；`mtp.py:430-445` `_slice_shared_kv_after_reject`
   按拒绝数切尾（切片 = 新数组）；`language.py:753+` `rollback_speculative_cache` 再 trim
   一次并逐行清尾。

**MoE 目标件在验证批下的专家并集税**［源码实证 + 推断］：
`models/gemma4/language.py:109-127` `Experts` 用 `SwitchGLU` + `gather_mm`，
路由 `:333-335` 逐 token 取 top-k 专家。**验证批 4 个 token 各自选 top-k，读的是专家的
并集**。稠密件 verify(γ+1) 的字节成本 ≈ verify(1)（同一份权重读一遍，只是 FLOPs 翻倍）；
MoE 件 verify(γ+1) 的字节成本在专家饱和前**近似线性于 γ+1**。

> **投机采样的全部经济学前提——「验证 k 个 token 和验证 1 个一样贵」——对小 γ 的 MoE
> 是错的。** 这一条与硬件无关，正是 §Q12（L4/vLLM/NVFP4 MoE 净减速 −17%）与本机
> 26B-A4B 0.79× 同时成立的根因。

### 2.4 KV 缓存与回滚

两边都在拒绝后回退目标 KV。mlx 侧 `language.py:753+` `rollback_speculative_cache`
→ `cache.trim(block_size - accepted - 1)`。

**§S.15 与 MTP 的关系必须点明**（台账 `EXECUTION_LEDGER.md:1152-1200`）［台账实证］：
上游 `RotatingKVCache.trim()` **只递减记账、不搬内容**，而 `is_trimmable()`
（`offset < max_size`）在旋转后返回 False——`rollback_speculative_cache` 却**无条件**调
`trim()`。旋转态下后果不是「5 行草稿泄漏」而是**环形对齐整体错位**（512 槽位有 511 个
与基线不一致），实测 max|Δ| = 22 logits。该域投机**倒亏 0.89×、接受率 4%**。
本栈已根修（`patch/rotating_cache_trim.py`，`_temporal_order()` + 物理切尾，
三口径 max|Δ| = 0.000000）。

**这条对本报告的意义**：三档预扫是 **ctx=0** 跑的，缓存从未旋转，故预扫数字**未受**
S.15 污染；但**任何 ctx ≥ 滑窗（1024）的 MTP 格，若在根修之前测，接受率都不可信**。
§7 阶段二的「接受率随上下文衰减」格必须在根修之后重测——这一点已经满足。

vLLM 侧的 KV 回滚未检索到等价缺陷记录（**检索不到，不做推断**）。

### 2.5 硬件与带宽——为什么同一份算法两边收益不同

**屋顶线视角**［推断，基于公开规格］：投机采样的本质是**拿 FLOPs 换字节**。
这笔交易的划算程度取决于设备的算力/带宽比（ridge point）。

| 设备 | 峰值 bf16 稠密 | 带宽 | ridge ≈ FLOP/byte |
|---|---|---|---|
| H100 SXM | ~495 TFLOPS | 3.35 TB/s | ~148 |
| A100 40G | ~312 TFLOPS | 1.56 TB/s | ~200 |
| M4 Max（40 核） | ~34 TFLOPS | 546 GB/s | **~62** |

M4 Max 的 ridge 只有 H100 的 ~42%——把验证批从 1 拉到 4，在 H100 上几乎白送，在 M4 Max
上已经开始吃算力。但**这不是主项**：真正的主项是**每轮固定开销 H**（台账已命名的量）。

**墙钟拆账（31B-4bit，本机）**［推断，算术自洽但未经 profiler 验证］：

- 基线：25.0 tps → **40.0 ms/token**
- MTP：36.8 tps，AL 2.63 → **每轮 71.5 ms**
- 理想轮（verify 恰等于一次基线步、草稿与走查免费）= 40.0 ms → 65.8 tps = **2.63×**
- **实测每轮多花 31.5 ms，即一次基线步的 79%**

拆项估算：

| 项 | 字节/轮 | @546 GB/s | 备注 |
|---|---|---|---|
| 草稿 3 次前向 | 3 × 926 MB = 2.8 GB | ~5.1 ms | 其中 **537 MB 是绑定嵌入当 lm_head**（1024×262144×2B），占单件 58% |
| 走查 4 次 lm_head | 4 × ~352 MB（5376×262144@4bit） | ~2.6 ms | **本可批成 1 次 = 0.65 ms** |
| 4 次 `mx.eval` 阻塞同步 | — | ~1–4 ms［推断］ | vLLM 侧为 0 |
| Python 侧掩码/KV 切片/dispatch | — | 余量 ~20 ms［推断］ | 见下 |

**草稿件的每步 Python 开销是可证实的浪费**［源码实证］：
`gemma4_assistant.py:187-194` 在 `_forward_hidden` 里**每次前向**都调
`make_drafter_masks(...)`。但 `draft_block`（`:268-283`）的循环内，
`shared_kv`、`position_ids`、`self._kv_valid_len`、`h.shape[1]=1` **全部逐次不变**——
**同一轮内 γ−1 次前向重建的是逐字节相同的掩码**。

**且整个 gemma-4 MTP 路径没有任何 `mx.compile`**：
`grep -rn "mx.compile" mlx_vlm/speculative/` 只在
`drafters/muse_glimmer_assistant/dflash.py:40` 命中一处，与 gemma4 无关。
**vLLM 用 CUDA Graph 消灭的正是这一类开销，mlx 侧完全裸奔。**［源码实证］

### 2.6 「稠密赚、MoE 亏」这条规律——**本报告对它提出修正**

台账 `EXECUTION_LEDGER.md:1553-1559` 的现行结论是
「分界不在接受率，在成本比；MoE 活性参数小、目标解码步本就便宜，草稿摊不掉」。
**这个方向对，但归因错在把它挂到「目标件架构」上。**

反证在同一张表里［数据实证］：

| 目标件 | 基线 tps | 基线每步 | 草稿件大小 | **草稿/目标字节比** | 加速比 |
|---|---|---|---|---|---|
| e4b-4bit（稠密） | 114.0 | 8.77 ms | **183 MB** | 3×0.18/4.79 GB = **11%** | **1.36×** |
| 26B-A4B（MoE） | 121.2 | 8.25 ms | **831 MB** | 3×0.83/4.51 GB = **55%** | **0.79×** |
| 31B-4bit（稠密） | 25.0 | 40.0 ms | 926 MB | 3×0.93/21.8 GB = **13%** | 1.46× |

**e4b 与 26B-A4B 的目标解码步几乎一样贵（8.77 vs 8.25 ms）**——所以「目标解码步便宜」
无法解释为什么一个赚 1.36× 另一个亏 0.79×。**唯一的量级差在草稿件本身：183 MB vs 831 MB。**

差异来自 checkpoint 配置［本地文件实证］：

| assistant | `text_config.hidden_size` | `use_ordered_embeddings` | 体积 |
|---|---|---|---|
| e4b | **256** | **True**（centroids 掩码开） | 183 MB |
| 26B-A4B | 1024 | False | 831 MB |
| 31B | 1024 | False | 926 MB |

e4b 的草稿件既窄（256）又开了 centroids 掩码（lm_head 262144 → ~4K，
`masked_embedder.py`），于是便宜到即使目标步只有 8.77 ms 也摊得掉；
26B/31B 的草稿件宽（1024）且 centroids 关闭，**光绑定嵌入当 lm_head 就 537 MB**。

> **修正后的规律：决定 Mac 上 MTP 成败的既不是草稿件精度、也不是目标件架构，
> 而是「γ × 草稿件字节 ÷ 目标件单步活性字节」这个成本比，外加 MoE 独有的验证批
> 专家并集税。31B 赚是因为目标步贵到 40 ms（比 13%）；e4b 赚是因为草稿件小到 183 MB
> （比 11%）；26B 亏是因为两头都不占（比 55%）+ 专家并集税。**

这条修正也顺带解释了 CUDA 侧看似矛盾的两点：vLLM 26B **bf16** 在 H100 上 1.60×
（bf16 目标件的活性字节是 4bit 的 4 倍 → 比值降到 ~14%），而同一个 26B 换 **NVFP4** 到
L4 就 −17%（§Q12）——**量化目标件会把字节成本砍 4 倍，却不动 bf16 草稿件，成本比因此
翻两番**。这与台账「NVFP4 logits 偏离草稿 bf16 训练分布导致接受率劣化」的解释是
**两套不同机制**，而本机的直接测量（4bit 目标上 r=54.2%）**不支持**接受率劣化那一套。

### 2.7 在 vLLM 侧是否同样成立

**部分成立，方向一致**：vLLM 26B（MoE）在 H100 γ=4 只有 160%，而 31B（稠密）γ=8 有 319%
［官方文档］。同一份 PR 里 MoE 明显吃亏，与本机同向。
但 vLLM 侧的 MoE **没有翻负**——因为 H100 上 bf16 26B-A4B 的基线 169.2 tps（5.9 ms/步）
远高于纯带宽下界，说明该步本就是**延迟/开销主导而非带宽主导**，有富余空间吸收专家并集税；
Mac 上 4bit 26B 的 8.25 ms 已接近纯带宽，无富余可吸。［推断，可由 profiler 证伪］

---

## ③ 修复机会与优先级

排序依据：预期收益 ÷（改动风险 × 工作量）。

### F1 · 解开草稿深度天花板 —— **最高优先级**

- **预期收益量级**：31B AL 2.63 → **3.0–3.3**（γ=3→5，按边际接受率衰减外推），
  加速比 **1.46× → 1.6–1.8×**［推断］。e4b 同向、幅度更大（草稿边际成本仅 0.06 GB/步）。
- **改动位置**：**本栈补丁层**，`backend/framework/backends/mlx/backend.py::_load_drafter`
  装载后一行 `draft_model.prefer_requested_block_size = True`
  （上游在 `mtp.py:486-487` 留好的逃生阀），或 `draft_model.config.block_size = k`。
  **零上游改动。**
- **风险**：(a) 26B MoE 上会**加重**亏损（专家并集随 γ 线性增长）——必须按档开关；
  (b) 每轮验证批变宽，Mac 上算力占比上升；(c) 与 `max_concurrency` / 批处理形态交互
  （模型卡建议批处理用 3）。
- **证伪方法**：31B 上做 γ ∈ {3,4,5,6,8} 扫描，**同时**记录 AL 与 tps。
  若 tps 在 γ=3 就见顶，说明上游的天花板是对的、本项作废；
  若 AL 单调升而 tps 在 γ=5–6 见顶，取峰值定档。这正是 §7 阶段二已排期的 k 扫描，
  只需先把闸打开，否则**扫描出来的是同一个 γ=3，全是噪声**。
- **附带动作**：向上游报缺陷——mlx-community 模型卡推荐 `--draft-block-size 6`，
  但三个官方 assistant 件的 `config.json` 均无 `block_size` 键，
  `Gemma4AssistantConfig.block_size=4` 的默认值 + `_effective_mtp_block_size` 的
  ≥65% 门槛使该推荐值在所有已发布件上静默失效。

### F2 · 走查批量化 —— 高价值，中等复杂度

- **预期收益量级**：每轮省 γ 次 lm_head 权重读 + γ 次 GPU 同步。
  31B ≈ **−2 至 −6 ms/轮（+3%–8%）**；e4b/26B 这类轮时长只有 16–24 ms 的档，
  占比更高，**+10%–15%**［推断］。
- **改动位置**：`backend/framework/backends/mlx/patch/mtp_processors.py:186-215`。
  纯无处理器场景可直接走上游 `_positioned_target_tokens`（`mtp.py:230-256`，已是批量），
  但本栈恒有 `repetition_penalty`，故需要：一次批量投影拿全部 γ+1 位 logits，
  再逐位应用处理器增量（rep penalty 的惩罚集只随本轮已 commit 的 token 单调增长，
  可用 per-position 增量掩码精确表示）。
- **风险**：**这是正确性红线区**——§S.16/omlx 的教训是处理器静默失效不报错。
  必须由 golden tests（FakeEngine, CPU-only）+ 与逐位实现的逐 token 比对闸守住。
- **证伪方法**：先做零风险测量——把 `greedy_sampling=True` 传进
  `gemma4_generate.py:949`（临时、无处理器场景），对比同一 prompt 的轮时长。
  差值即本项收益上限。若 < 3%，不值得动补丁。

### F3 · 同轮内掩码复用 —— 纯赚，零风险

- **预期收益量级**：省 γ−1 次 Python 侧掩码构造。绝对值未知（需 profiler），
  但 §2.5 的 ~20 ms/轮未拆账余量里，这一项是**唯一被源码证实为纯粹浪费**的。
- **改动位置**：上游 `gemma4_assistant.py:_forward_hidden`（或本栈 shim 包 `draft_block`）。
  论证：`draft_block` 循环内 `shared_kv` / `position_ids` / `_kv_valid_len` /
  `query_len=1` 全部不变 ⇒ `make_drafter_masks` 的入参逐次相同 ⇒ 结果可缓存。
- **风险**：**无**（纯函数结果缓存，作用域限于单轮）。
- **证伪方法**：缓存前后逐 token 输出必须逐字一致；若轮时长无变化，说明掩码构造不在
  热路径，撤销即可。

### F4 · 草稿件词表裁剪（自研 centroids 等价物）—— 高收益，需实验

- **背景**：26B/31B 的 assistant **没有** centroids（`use_ordered_embeddings=False`），
  Google 只给 E2B/E4B 发了这个优化（vLLM PR 同样只对 E2B/E4B 生效）。
  **检索结论：26B/31B 的 centroids 权重不存在，此路直接走不通。**
- **但等价效果可自建**：草稿件的输出**永远会被目标件验证**，故草稿分布不必精确。
  把草稿 lm_head 从 262144 行裁到 32K 高频行（静态 gather 一次，常驻 67 MB），
  每步省 ~470 MB。
- **预期收益量级**：26B/31B 每轮省 3 × 470 MB = 1.4 GB ≈ **−2.6 ms/轮**
  （31B +3.7%；26B 0.79× → ~0.85×）［推断］。
- **改动位置**：本栈补丁层，包住 `Gemma4AssistantDraftModel._lm_head_fn`（`gemma4_assistant.py:67-76`）。
- **风险**：真实 next-token 落在 shortlist 之外时该位必被拒 → AL 下降。
  **输出正确性不受影响**（草稿恒被验证），只影响速度。
- **证伪方法**：固定语料上测 shortlist 覆盖率与 AL 差。若 AL 掉超过 ~8%，字节收益被吃光。

### F5 · 草稿件量化 —— 中等收益，方向被本报告修正

- **预期收益量级**：31B 草稿 bf16→4bit，926 MB → ~250 MB，每轮省 2.0 GB ≈ **−3.7 ms**
  （1.46× → **~1.55×**）。26B 同理 0.79× → **~0.90×，仍为负**［推断］。
- **改动位置**：离线转换（`mlx_vlm.convert` 跑 assistant 件），无代码改动。
  注意 `masked_embedding.token_ordering` 是 int32 静态缓冲，
  `gemma4_assistant.py:285-294` `sanitize` 只处理 dtype，不得被量化（仅 e4b 有此结构）。
- **风险**：**草稿件是提案分布，量化直接损接受率**——与量化目标件的影响方向相反。
  这是 F5 与台账原预测①的关键分歧点。
- **证伪方法**：同件 bf16 vs 8bit vs 4bit 三跑，读 AL 与 tps。若 AL 掉 >10%，8bit 封顶。
- **对台账的处置建议**：§7 阶段二预测① 的两次表述都应再改一次。
  原版「bf16 草稿在 546GB/s 下反亏、4bit 化是前置」——被 e4b/31B 实测证伪；
  08-14 改版「决定成败的是目标架构（稠密/MoE）」——被 §2.6 的 e4b vs 26B 对照证伪。
  **建议表述：决定成败的是 `γ × 草稿字节 / 目标单步活性字节` 这个比值（经验阈 ~0.35），
  外加 MoE 的验证批专家并集税。** 该表述同时解释 Mac 三档与 CUDA 四点，共七个数据点。

### F6 · 按成本比自动关闭 —— 低收益但止损，零风险

- **预期收益量级**：不提速，但避免 26B 档 −21% 的净亏被误开进生产。
- **改动位置**：本栈，`_load_drafter` 里按 §2.6 的字节比预判 + 运行期滚动闸
  （上游的 `_effective_mtp_block_size` 已有「按接受率上调」的钩子，
  可在补丁层扩成「按实测轮时长 vs 滚动基线估计下调乃至关停」——
  **上游 mlx_vlm 与 vLLM 都没有基于成本的自适应**，这是本栈能领先的一点）。
- **风险**：低（保守方向）。
- **证伪方法**：闸应当自动在 26B-A4B 上关停、在 31B/e4b 上保持开启——
  即精确复现预扫表。若它误关 31B，阈值错。

### 不建议做的

- **「改走 MTP 头」**：mlx 已经在跑 MTP 头式算法（§2.1），此路是无。
- **等 centroids 版 26B/31B assistant**：Google 未发布，vLLM 也吃不到。检索不到任何在途迹象。
- **靠调 `num_draft_tokens`**：该键在 mlx_vlm 中不存在（§1.3），`smoke_mtp_drafter.py:43`
  里那行应删。

---

## ④ 与本项目已有实测的对表

| 台账结论 | 本报告核验 | 处置建议 |
|---|---|---|
| 三档接受率 40–54%（`:1547-1551`） | **数值成立**，但口径需补注：这是 `accepted/drafted`，且 **γ=3 而非 5** | 表头改「接受率（每位，γ=3）」，并补一列 AL |
| 「接受率都在 40–54% 窄带，差别全在成本比」（`:1553`） | **前半对，后半的归因错**：成本比不是「目标解码步贵不贵」，而是草稿/目标字节比。e4b 与 26B 目标步一样贵（8.77 vs 8.25 ms）却一赚一亏 | 按 §2.6 改写 |
| 「决定成败的是目标件架构（稠密/MoE）」（`:1558`） | **被同表内 e4b vs 26B 对照证伪** | 改为字节比 + 专家并集税双因子 |
| 「8bit系 a≈2.7-3.4 vs 4bit系 ≈1.9」（`:414`） | **不是实测，是从四个墙钟加速比反拟合的自由参数**；vLLM PR 官方基准也不报 AL | 台账标注降级为「反拟合估计」。§7 阶段二预测② 现有唯一直接测量（4bit 31B, r=54.2%, AL 2.63）**不支持**该劣化假设 |
| 「草稿 4bit 化是 Mac 上 MTP 成立的前置」（`:417-418`） | 已被 08-14 预扫证伪并重述；本报告给出第三版表述（F5） | 见 F5 |
| §Q12 L4 MoE −17%（`:976`） | **与本机 26B 0.79× 同源**，但根因是「NVFP4 目标把字节砍 4 倍、bf16 草稿不变 → 成本比翻两番」+ 专家并集税，不是「58 SM 扛不动」 | Q12 已结案，补一条根因注 |
| 「31B 3.12×（k=4）」（`:866`） | vLLM PR 官方表 31B 基线 **42.6 tps → 135.9 tps @ γ=8**，与我方 42.6→132.7 基线逐字相同、终值相近。**我方记的 k=4 与官方 γ=8 存在冲突**，其一有误 | 需回查当时的 `num_speculative_tokens` 实参。若实为 γ=8，则「vLLM 用 4 我们用 6」的对比前提整个作废 |
| §S.15 旋转滑窗回滚失真（`:1152-1200`） | 预扫为 ctx=0，**未受污染**；但根修前的任何长上下文 MTP 数（含那次 0.89×/接受率 4%）不可用作接受率证据 | 已根修，阶段二可放心开 ctx 扫描 |

**一处未解冲突（重要）**：台账两处记 31B 为 `MTP k=4`（`:81`、`:866`），
而 vLLM 官方 PR 达到 319% 用的是 **γ=8**，且基线 42.6 tps 与我方完全一致。
若我方当时实际也是 γ=8，则本报告 §1.4 表中 vLLM 31B 那两行的 γ 需改，
反拟合的 r 随之从 22.5%/42.5–60% 降到 11%/21–30%——**结论方向不变，只会更强**
（mlx 的接受率优势更大）。若确为 γ=4，则表保持不变。**此项无法从本机源码判定，需查
CUDA 侧运行配置原件。**

---

## ⑤ 未决问题与证伪路径

| # | 问题 | 为什么未决 | 证伪/证实路径 | 成本 |
|---|---|---|---|---|
| U1 | CUDA 侧 31B 那一跑的 `num_speculative_tokens` 究竟是 4 还是 8 | 台账记 k=4，官方 PR 同基线用 γ=8 | 查 `colab/` 下的 `INFER_SPEC_CONFIG` / 起服日志原件 | 只读，零成本 |
| U2 | vLLM 侧从未直接读过 `mean_acceptance_length` | 四点全是墙钟反拟合 | 下次 CUDA 场把 `/metrics` 的 `spec_decode_num_accepted_tokens` / `num_drafts` 抓下来，一次跑完事 | 需 GPU 场，但零额外时长 |
| U3 | §2.5 的每轮 31.5 ms 开销拆账是算术推断，非 profiler 实测 | 本次严禁跑真模型 | 单独计时草稿循环（`draft_block` 前后打点）与走查循环；三者相加应等于轮时长 | 需 GPU，~10 min |
| U4 | 解开 γ 天花板后 AL 的真实增长曲线 | 边际接受率衰减率未知 | F1 的 γ 扫描，**必须同时报 AL 和 tps** | 阶段二已排期 |
| U5 | MoE 验证批的专家并集税量级 | 源码证实机制存在，量级未测 | 固定 prompt 下测 26B verify(1) vs verify(4) 的纯前向耗时比。稠密应 ≈1.05，MoE 若 ≈k 则坐实 | 需 GPU，~10 min |
| U6 | 目标件量化是否真的劣化接受率（阶段二预测②） | 现有唯一直测（4bit, 54.2%）反向；反拟合值支持 | 同机 31B **8bit vs 4bit** 目标件、同 γ、同 prompt，直读 AL。这是最便宜的裁决格 | 需 GPU，~20 min |
| U7 | 词表裁剪（F4）的 shortlist 覆盖率 | 未测 | 离线：取生产语料的 next-token 分布，算 top-32K 覆盖率。**无需 GPU** | 纯 CPU |
| U8 | vLLM 侧是否存在与 §S.15 等价的 KV 回滚缺陷 | 检索未覆盖 | 未做；非本项目路径，优先级低 | — |

**一条口径纪律建议（成本为零，价值很高）**：
mlx_vlm 自己就会算 AL（`common.py:284-296` 的 `mean_accepted_tokens`），
但我方 harness 只上报了百分比。**在 bench 输出里同时打印 AL 和 γ**，
本次这场「40% vs 3.4 谁高谁低」的混淆就不会发生第二次。
这也正是台账反复强调的「区分读到的与推断的」在数值口径上的对应物。

---

## 来源清单

**本地源码（本机只读）**

- `backend/.venv/lib/python3.13/site-packages/mlx_vlm/version.py`（v0.6.13）
- `.../mlx_vlm/speculative/common.py`（`:135-158` `_speculative_walk`、`:212-229`
  `_record_speculative_round`、`:232-255` snapshot/since、`:277-301` `_format_speculative_stats`）
- `.../mlx_vlm/speculative/mtp.py`（`:28-55`、`:57-175`、`:177-256`、`:393-428`、`:430-445`、
  `:447-490`、`:496-521`、`:523-685`、`:722-731`）
- `.../mlx_vlm/speculative/drafters/__init__.py`（`:9-27` 种类映射）
- `.../mlx_vlm/speculative/drafters/gemma4_assistant/gemma4_assistant.py`（全文，重点 `:14-27`、
  `:29-102`、`:104-151`、`:152-215`、`:217-283`、`:285-294`）
- `.../mlx_vlm/speculative/drafters/gemma4_assistant/config.py`（`:23` `block_size: int = 4`）
- `.../mlx_vlm/speculative/drafters/gemma4_assistant/masked_embedder.py`、`masks.py`
- `.../mlx_vlm/models/gemma4/language.py`（`:79-98` router、`:109-127` `Experts`/SwitchGLU、
  `:136-177` `kv_shared_only`、`:328-340` MoE 调用、`:668` `speculative_logits_from_hidden`、
  `:753+` `rollback_speculative_cache`）
- `backend/framework/backends/mlx/patch/mtp_processors.py`（`:159-217`、`:220-261`）
- `backend/framework/backends/mlx/patch/gemma4_generate.py:949`
- `backend/framework/backends/mlx/backend.py`（`:130-160`、`:182-229`、`:372-379`）
- `backend/scripts/smoke_mtp_drafter.py:43`
- `backend/models/gemma-4-{31b,26b-a4b,e4b}-it-assistant-bf16/config.json`
- `EXECUTION_LEDGER.md`（`:81`、`:397-404`、`:411-421`、`:866-871`、`:976` Q12、
  `:1152-1200` S.15、`:1481-1560` MTP 接线波与三档预扫、`:1720-1740` §7 阶段二）
- `config.yaml:304`

**联网来源**

- vLLM PR #41745 "[Spec Decode] Add Gemma4 MTP speculative decoding support" —
  https://github.com/vllm-project/vllm/pull/41745
- vLLM `vllm/v1/spec_decode/metrics.py` —
  https://raw.githubusercontent.com/vllm-project/vllm/main/vllm/v1/spec_decode/metrics.py
- vLLM `vllm/v1/spec_decode/gemma4.py` —
  https://raw.githubusercontent.com/vllm-project/vllm/main/vllm/v1/spec_decode/gemma4.py
- vLLM MTP 文档 — https://docs.vllm.ai/en/latest/features/speculative_decoding/mtp/
- `mlx-community/gemma-4-31B-it-assistant-bf16` 模型卡 —
  https://huggingface.co/mlx-community/gemma-4-31B-it-assistant-bf16
- `mlx-community/gemma-4-26B-A4B-it-assistant-bf16` —
  https://huggingface.co/mlx-community/gemma-4-26B-A4B-it-assistant-bf16
- `mlx-community/gemma-4-E4B-it-assistant-bf16` —
  https://huggingface.co/mlx-community/gemma-4-E4B-it-assistant-bf16
- `google/gemma-4-31B-it-assistant` 模型卡 —
  https://huggingface.co/google/gemma-4-31B-it-assistant
- Blaizzy/mlx-vlm Releases — https://github.com/Blaizzy/mlx-vlm/releases
- ollama PR #15980 "mlx: Gemma4 MTP speculative decoding" —
  https://github.com/ollama/ollama/pull/15980
- mlx-swift-lm Issue #282（Gemma 4 loader gaps） —
  https://github.com/ml-explore/mlx-swift-lm/issues/282
- Gemma 4 Technical Report — https://arxiv.org/pdf/2607.02770

**检索不到（明示）**

- vLLM 侧 Gemma-4 MTP 的实测 acceptance length / acceptance rate：官方 PR 与文档均只报
  TPS 与 speedup，未报 AL。
- `google/gemma-4-*-it-assistant` 官方卡对 `draft_block_size` 的推荐值（只有 mlx-community
  的转换卡给了 6/3）。
- 26B/31B assistant 的 centroids（`use_ordered_embeddings`）权重：Google 只对 E2B/E4B 发布。
- vLLM 侧与 §S.15 等价的 KV 回滚缺陷记录。
