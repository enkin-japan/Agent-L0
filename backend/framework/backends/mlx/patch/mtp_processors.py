"""MTP 投机路补回 logits 处理器——上游草稿件投机会静默丢掉它们。

# 伤口

`generate_step` 构造好 `processors`（repetition_penalty / logit_bias / 调用方传入的
`logits_processors`）后，若 `draft_model is not None` 就整段 `yield from
run_speculative_rounds(...)` 并 `return`——而后者的签名里**没有 processors**。
processors 只在非投机的 `_step` 里应用（ar.py:360-363）。净效果：**开草稿件投机
= 全部 logits 处理器失效**。

对本栈的具体伤害有两条，都不是"慢一点"级别的：
- `repetition_penalty` 生产缺省 **1.1**，而 §S.18 实测它正是 12B `<|channel>thought`
  退化空转的兜底（裸贪心口径挂、生产采样口径过）。丢了它 = 把那道兜底拆掉。
  注意 `min_p` 在 sampler 里、活得下来，**只有处理器这一路会死**，所以症状会像
  "只有开投机时模型偶尔发疯"。
- 工具段约束采样 guard（`_GuardAdapter`）也是处理器。丢了它 = 投机段的 tool_call
  无约束——正是 §7-8 刚在格式层修好的那类问题，会从另一个入口回来。

# 修法落点

gemma-4 的路径已逐条查实：`speculative_argmax_from_hidden` **无**（融合 argmax 快路
不适用）、`speculative_verify_logits` **无**、`speculative_logits_from_hidden` **有**。
故 B=1 时走查都汇到 `_mtp_acceptance_walk` 一个函数，它内部再分两条：
- `_positioned_target_tokens` 批量采全部位置——**只在 sampler 带 `sample_target` 时**
  （`mtp.py:238-240` 拿不到就返回 None）；
- 否则 `_speculative_walk_deferred_greedy` 逐位延迟投影。

**本栈恒走后者**，这一点已查实、且与温度无关：`_PositionedTargetSampler` 的构造条件
是 `seed is not None and temperature > 0 and min_p == 0.0 and top_k == 0 and …`
（`generate/ar.py:266-278`），而本栈**从不传 seed**（`backend.py` 全文零处），
单这一条即永不满足（min_p 缺省 0.05 只是第二重保险，请求可覆盖它、覆盖不了 seed）
→ sampler 是普通 `make_sampler` 产物 → 无 `sample_target`。

所以：**本模块没有新增任何逐位同步点**。上游本来就逐位走。本模块只替换
`_mtp_acceptance_walk`。有处理器时的净效果是两条——①把处理器补回逐位判决，
②把上游那 γ+1 次逐位投影合并成 1 次批量投影（见 `_walk_with_processors` 内的
注释），故性能上是净赚而非净亏。**无活动处理器时也走同一条批量快路**（空上下文，
08-15 夜改，§2.1-c 门控修正）：投影次数不再随处理器开关而变。语义边界随之变化，
明记两条——①temp>0 普通 sampler 的 RNG 流与旧"委派上游"形态不同（每轮多耗
γ+1−k 个 key 且经 sync_draft 传导草稿流），该配置的接受率读数与修正前存档不可
互相对表；②temp=0 时批量投影与上游逐位投影存在 M=1 vs M>1 内核归约次序差
（§1.7.4：噪声中位 0.25 logprob），argmax 表示级平局位可翻——旧"零行为变化"
保证改为"平局位以内等价"。融合 target / 缺投影接口的形态仍原样委派上游。

# 逐位走查为什么不损失正确性

被接受的 token 恒等于该位置的 target token（草稿被接受 ⟺ draft[i] == target[i]），
且走查在首次不匹配处停。故本轮实际产出恒为 `target[0..accepted]`，位置 `pos` 的
正确历史就是「已产出 + 本轮 target[0..pos)」——全是真 token，**一个被拒草稿都不含**。
这条对 `_GuardAdapter` 是硬要求：它按 `_seen` 单调消费，喂进一个草稿就不可逆地跑偏。

批量采样那条路做不到这件事（一次性对全部位置采样，无法给每个位置不同历史），
故即便将来采样口径变成满足 `_PositionedTargetSampler` 条件的那种，本模块仍逐位走：
`sample_target` 接显式 positions，逐位调用与批量调用的 RNG 流等价，确定性不受影响。

# 覆盖面

`_mtp_acceptance_walk` 只有一个调用点（`_mtp_rounds`，**B=1**），本栈正是 B=1
（CHARTER §3 单 worker 串行）。B>1 的 `_mtp_rounds_batch` 走的是另一组走查函数，
**未覆盖**——Q14 若放开并发、MTP 与批处理叠加，那条路要同样补处理器，否则同一个
静默失效会从批处理入口回来。
"""

import threading
from typing import Any, Callable, List, Optional, Tuple

import mlx.core as mx

from mlx_vlm.speculative import mtp as _mtp

# `utils` 在 import 期 `from .mtp import _mtp_rounds_batch` 抄走了一份同名副本，
# 而真实分派点在 utils（utils.py:79/181/288）。只改 mtp 的模块全局等于没改。
# import 失败降级为只覆盖 mtp 侧并打阴性声明——批处理守卫是安全网不是正确性前提，
# 不该有把后端拖挂的权力（walk 补丁那头是正确性前提，故仍无条件 import）。
try:
    from mlx_vlm.speculative import utils as _utils
except Exception as _exc:  # pragma: no cover - 上游模块结构变动
    print(f"[MTP] speculative.utils import 失败（{_exc!r}）：批处理守卫只能覆盖 mtp 模块侧")
    _utils = None

__all__ = ["install", "uninstall", "processor_context", "record_emitted"]

_ORIGINAL_WALK = _mtp._mtp_acceptance_walk

_local = threading.local()


def _is_guard(p: Any) -> bool:
    """约束 guard 的识别口径与 `lookup_generate_step` 一致（同文件既有先例）。"""
    return hasattr(p, "consume") and hasattr(p, "mask_logits")


class _Ctx:
    """一次生成的处理器上下文（线程本地：生成恒在专用推理线程内串行）。

    **两类处理器分开驱动**，这是本模块最初写错、经首过评审揪出的地方：
    - 约束 guard 走它自己的 `consume()` / `mask_logits()` 协议。用
      `(tokens, logits)` 协议喂它是错的：`_GuardAdapter` 首调用会把
      `_seen` 锁在**上游喂过的 prompt 长度**上（实测 prompt 12 token → _seen=12），
      而我方若从空历史起步，长度 1,2,3… 永远追不上那条水位线 → guard 一个
      token 都消费不到 → 全程不激活 → 工具约束在 MTP 路上等于不存在。
    - 惩罚类（repetition_penalty / logit_bias）走 `(tokens, logits)`，历史必须
      **用 input_ids 播种**（= 上游实际喂进 `_step` 的那批），再按窗口截断。
      从空起步会让惩罚窗口只看得见生成段，与非投机路语义不同。
    两条都照抄同文件 `lookup_generate_step` 的既有做法。
    """

    __slots__ = ("plain_procs", "guard", "hist", "window", "n_emitted")

    def __init__(self, plain_procs, guard, prompt_tokens, window: int):
        self.plain_procs = plain_procs
        self.guard = guard
        self.hist: List[int] = list(prompt_tokens)
        self.window = window
        # 已登记的**生成** token 数（不含 prompt）。走查与消费侧共用一个计数器
        # 决定归属，避免同一 token 被消费两次（guard 的 consume 不可逆）。
        self.n_emitted = 0

    def commit(self, token: int) -> None:
        """登记一个确定产出的 token：进惩罚历史、进 guard 状态机。"""
        token = int(token)
        self.hist.append(token)
        if self.guard is not None:
            self.guard.consume(token)
        self.n_emitted += 1

    def apply(self, logits: mx.array, extra: List[int]) -> mx.array:
        """按「已登记历史 + 本轮尚未登记的延长段」过一遍处理器。"""
        if self.plain_procs:
            hist = (self.hist + extra)[-self.window:] if extra else self.hist[-self.window:]
            tokens = mx.array(hist, dtype=mx.int32)
            for p in self.plain_procs:
                logits = p(tokens, logits)
        if self.guard is not None:
            logits = self.guard.mask_logits(logits)
        return logits


def _ctx() -> Optional[_Ctx]:
    return getattr(_local, "ctx", None)


class processor_context:
    """在 with 块内让 MTP 走查看到这批处理器；块外恢复（可嵌套）。

    processors 为空时不建上下文——走查走空上下文批量快路（见
    acceptance_walk_with_processors 的 ctx is None 分支）。
    """

    def __init__(self, processors: Optional[List[Callable]],
                 prompt_tokens: Optional[List[int]] = None,
                 penalty_window: int = 64):
        procs = [p for p in (processors or []) if p is not None]
        self._guard = next((p for p in procs if _is_guard(p)), None)
        self._plain = [p for p in procs if not _is_guard(p)]
        self._prompt = list(prompt_tokens or [])
        self._window = max(int(penalty_window or 64), 64)
        self._prev = None

    def __enter__(self):
        self._prev = getattr(_local, "ctx", None)
        _local.ctx = (
            _Ctx(self._plain, self._guard, self._prompt, self._window)
            if (self._plain or self._guard) else None
        )
        return self

    def __exit__(self, *exc):
        _local.ctx = self._prev
        return False


def record_emitted(token: int, yield_index: int) -> None:
    """消费侧在 yield 后调用，登记走查**没有**经手的产出 token。

    `_mtp_rounds` 在进轮循环前会先吐一个 bonus token，它不经走查；轮内产出则由
    走查自己 `commit`（guard 必须在轮内逐位推进，否则下一位的 mask 就是错的）。
    用「已 yield 数 vs 已登记数」判归属，比依赖上游"每轮先吐几个"的结构更稳：
    上游改了 yield 结构，这里仍然自洽。没有活动上下文时是空操作。
    """
    ctx = _ctx()
    if ctx is not None and yield_index >= ctx.n_emitted:
        ctx.commit(token)


def _walk_with_processors(
    lm: Any,
    verify: Any,
    draft_tokens: mx.array,
    sampler: Callable[[mx.array], mx.array],
    budget: int,
    row_id: int,
    base_position: Optional[int],
    ctx: _Ctx,
) -> Tuple[int, List[int]]:
    """走查分派：无 guard 走批量快路（一次同步），有 guard 走逐位顺序路。

    guard（工具约束状态机）的 `consume` 不可逆、`mask_logits` 依赖逐位推进后的
    状态——批量路要为位置 pos 预知「接受 draft[:pos] 之后」的 mask，等于要求
    状态机可虚拟前滚，它做不到。故 guard 活动时（工具调用构造段）回退顺序路；
    其余时段（绝大多数解码）走批量路。两条路在 temp=0 下逐位等价（见各自 docstring），
    tests/test_mtp_processors.py 有并排断言。
    """
    if ctx.guard is None:
        return _walk_batched(lm, verify, draft_tokens, sampler, budget,
                             row_id, base_position, ctx)
    return _walk_sequential(lm, verify, draft_tokens, sampler, budget,
                            row_id, base_position, ctx)


def _walk_batched(
    lm: Any,
    verify: Any,
    draft_tokens: mx.array,
    sampler: Callable[[mx.array], mx.array],
    budget: int,
    row_id: int,
    base_position: Optional[int],
    ctx: _Ctx,
) -> Tuple[int, List[int]]:
    """批量走查（无 guard 专用）：全部位置一次建图、一次同步。

    可行性（§1.3-b 早已论证）：位置 pos 只有在 `0..pos-1` 全被接受时才会被消费，
    而"被接受 ⟺ target==draft"，故其正确历史恒为 `hist + draft[:pos]`——
    **在采样之前就已知**，惩罚可逐位按各自历史在图内算好，无需逐位同步。

    与顺序路的等价性（temp=0 逐位相同，实测判据）：
    - 惩罚历史逐位对齐：顺序路只 commit 进预算内的产出，故位置 pos 实际看到的
      延长段是 `draft[:min(pos, budget)]`——此处照抄这个式子，含"预算满后历史
      停更"的语义，保证与顺序路**逐位同 logits**；
    - 采样逐行调用同一个 sampler（懒建图，不依赖采样器支持批维）。temp=0 时
      argmax 逐行等价。temp>0 分两种：**positioned 分支逐位可复现**
      （`_PositionedTargetSampler` 按 (seed,row,position) 派生 key，不耗全局 RNG，
      两条路位到位相同）；普通 sampler 分支则**多耗 RNG 且量随数据变**——
      本路对全 γ+1 位都构了采样图，被拒轮比顺序路多耗 γ+1−k 次全局 key，
      且 `target_sampled(sync_draft=True)` 会把走查后的全局态喂给草稿流 ⇒
      下一轮草稿的 key 随之移动。**分布不受影响**（接受判据是相等比较、产出
      恒取自目标 logits），但 temp>0 下两条路的接受率读数不可互相对表。
    - accepted 计数/预算语义逐句照抄顺序路（accepted 决定回滚裁剪量，少报即多裁）。
    - 前置条件：plain_procs 必须是 (tokens, logits) 的**纯函数**——本路对断点后
      的行也会调用它们（结果丢弃）。有状态处理器必须走 guard 协议（consume/
      mask_logits），那会被分派到顺序路；两个协议都不带的有状态处理器会被
      静默多驱动，属接线错误。

    收益与代价（实测定案）：每轮同步 1+γ+1 次 → 2 次；代价是被拒轮多算
    γ+1−k 行词表宽的惩罚/归一化/采样图。26b-8bit 端到端净赚 **+2.3%**
    （block4 109.89→112.43 tps，AL/轮数逐位相同，§6.2-b S5）。
    """
    n_draft = int(draft_tokens.shape[1])
    draft_list = [int(x) for x in draft_tokens.reshape(-1).tolist()]
    accepted = 0
    new_tokens: List[int] = []

    sample_target = getattr(sampler, "sample_target", None)
    positioned = callable(sample_target) and base_position is not None

    with mx.stream(_mtp.generation_stream):
        all_logits = lm.speculative_logits_from_hidden(verify.hidden)
        targets = []
        for pos in range(n_draft + 1):
            row = (all_logits[:, pos, :] if all_logits.ndim == 3
                   else all_logits[pos : pos + 1, :])
            row = ctx.apply(row, draft_list[: min(pos, budget)])
            logprobs = row - mx.logsumexp(row, axis=-1, keepdims=True)
            if positioned:
                t = sample_target(logprobs, row_ids=[int(row_id)],
                                  positions=[int(base_position) + pos])
            else:
                t = sampler(logprobs)
            targets.append(t.reshape(-1))
        stacked = mx.concatenate(targets)
    mx.eval(stacked)
    if int(stacked.shape[0]) != n_draft + 1:
        # 逐位索引依赖"每行恰一元素"。采样器若返回 keepdim/批维，长度对不上时
        # 此后每个位置读的都是别人的 token → accepted 错 → KV 回滚裁剪错
        # （顺序路同场景会在 .item() 上响，这里必须补上同等响度）。
        raise RuntimeError(
            f"批量走查的采样结果长度 {int(stacked.shape[0])} ≠ 位置数 {n_draft + 1}："
            "sampler 每行返回了多于/少于一个元素，逐位对应已错位，拒绝继续。")
    target_list = [int(x) for x in stacked.tolist()]

    for pos in range(n_draft + 1):
        token = target_list[pos]
        if pos < n_draft and token == draft_list[pos]:
            accepted += 1
            if len(new_tokens) < budget:
                new_tokens.append(token)
                ctx.commit(token)
            continue
        if len(new_tokens) < budget:
            new_tokens.append(token)
            ctx.commit(token)
        break

    return accepted, new_tokens


def _walk_sequential(
    lm: Any,
    verify: Any,
    draft_tokens: mx.array,
    sampler: Callable[[mx.array], mx.array],
    budget: int,
    row_id: int,
    base_position: Optional[int],
    ctx: _Ctx,
) -> Tuple[int, List[int]]:
    """逐位走查：每个位置先按真实历史过处理器，再采样、再比对草稿。"""
    n_draft = int(draft_tokens.shape[1])
    draft_list = [int(x) for x in draft_tokens.reshape(-1).tolist()]
    accepted = 0
    new_tokens: List[int] = []

    sample_target = getattr(sampler, "sample_target", None)
    positioned = callable(sample_target) and base_position is not None

    # 计数语义**严格照抄上游**：预算满后仍继续走、仍累加 accepted，只是不再产出。
    # 不能提前 break——`accepted` 决定缓存回滚的裁剪量（rejected = block - accepted - 1），
    # 少报即多裁，会把已产出 token 的 KV 裁掉、污染下一轮前缀复用。
    # 代价：预算满之后那几位的历史是旧的（未 commit 的不进 guard/惩罚，因为它们
    # 不会产出，commit 进 guard 不可逆）。那些位置的 token 一律丢弃且生成即将结束，
    # 故只影响被丢弃的判决本身，不影响任何产出。
    # **一次性投影全部位置**（上游是逐位调）：`speculative_logits_from_hidden` 是
    # `embed_tokens.as_linear(norm(h))`，每调一次就把 [vocab, H] 那张大表整读一遍
    # （31B 的量化嵌入表数百 MB 级）。逐位调 = 同一张表在一轮里被重读 γ+1 遍，
    # 纯带宽浪费。对任意前置维度成立，故可直接喂 [1, n, H] 拿 [1, n, V]。
    # 即便走查在首位就 break，一次大投影仍优于多次小投影——瓶颈是表的读取次数。
    with mx.stream(_mtp.generation_stream):
        all_logits = lm.speculative_logits_from_hidden(verify.hidden)

    for pos in range(n_draft + 1):
        with mx.stream(_mtp.generation_stream):
            logits = (all_logits[:, pos, :] if all_logits.ndim == 3
                      else all_logits[pos : pos + 1, :])
            # 此刻 ctx 已含本轮前若干个（guard 也已推进到位），无额外延长段
            logits = ctx.apply(logits, [])
            logprobs = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
            if positioned:
                target = sample_target(
                    logprobs,
                    row_ids=[int(row_id)],
                    positions=[int(base_position) + pos],
                )
            else:
                target = sampler(logprobs)
        mx.eval(target)
        token = int(target.reshape(-1).item())

        if pos < n_draft and token == draft_list[pos]:
            accepted += 1
            if len(new_tokens) < budget:
                new_tokens.append(token)
                ctx.commit(token)
            continue

        if len(new_tokens) < budget:
            new_tokens.append(token)
            ctx.commit(token)
        break

    return accepted, new_tokens


def acceptance_walk_with_processors(
    lm: Any,
    verify: Any,
    draft_tokens: mx.array,
    sampler: Callable[[mx.array], mx.array],
    budget: int,
    row_id: int = 0,
    base_position: Optional[int] = None,
) -> Tuple[int, List[int]]:
    """`_mtp_acceptance_walk` 的替换实现。"""
    ctx = _ctx()
    if ctx is None:
        # 无活动处理器：仍走批量快路，只是挂一个空上下文（apply=恒等）。
        # 修正前这里委派上游逐位投影路，导致「投影次数」随处理器开关而变——
        # 关惩罚的 bench 吃不到单次大投影收益，且开/关处理器的 A/B 连投影
        # 次数都不同，§1.7/T4 类对照不可比（§2.1-c Major）。
        # 上游融合 target 或缺投影接口的模型形态照旧委派：那两条路上没有
        # 处理器可丢，上游语义即正确语义。
        if (getattr(verify, "target_tokens", None) is not None
                or not hasattr(lm, "speculative_logits_from_hidden")):
            return _ORIGINAL_WALK(
                lm, verify, draft_tokens, sampler, budget,
                row_id=row_id, base_position=base_position,
            )
        # 空 ctx **故意不装进 _local**（载重决策，勿"顺手"改掉）：装了会让
        # record_emitted 开始登记、让 B>1 的 rounds_batch_guarded 误判"有活动
        # 处理器"而拒跑——两处都以 _ctx() is None 为"无处理器"的判据。
        # 空 ctx 只作 apply=恒等的走查参数，用完即弃。
        ctx = _Ctx([], None, [], 64)
        return _walk_batched(lm, verify, draft_tokens, sampler, budget,
                             row_id, base_position, ctx)

    if getattr(verify, "target_tokens", None) is not None:
        # 模型自带融合 target（未经处理器）。gemma-4 不走这条（既无
        # speculative_argmax_from_hidden 也无 speculative_verify_logits），
        # 换模型形态踩到时必须响——静默用它 = 处理器照样失效，等于没修。
        raise RuntimeError(
            "MTP 走查拿到模型融合的 target_tokens，但当前有活动 logits 处理器："
            "融合路径不经过处理器，采用即等于静默丢弃 repetition_penalty / "
            "工具约束 guard。该模型形态需要单独接线，不得降级。"
        )

    if not hasattr(lm, "speculative_logits_from_hidden"):
        raise RuntimeError(
            "MTP 走查需要 speculative_logits_from_hidden 才能逐位应用处理器，"
            f"但 {type(lm).__name__} 未实现。"
        )

    return _walk_with_processors(
        lm, verify, draft_tokens, sampler, budget, row_id, base_position, ctx
    )


_ORIGINAL_ROUNDS_BATCH = getattr(_mtp, "_mtp_rounds_batch", None)

# `_mtp_rounds_batch` 有**两处**绑定要一起换：`mlx_vlm.speculative.mtp` 的模块全局，
# 以及 `mlx_vlm.speculative.utils` 在 import 期抄走的同名副本。真正被调用的是后者
# （utils.py:79 返回它、181/288 直接 `yield from`），只换前者 = 守卫是聋的：
# B>1 × 有处理器时不会响，处理器照样被静默丢掉——那正是本模块要防的那件事。
_ROUNDS_BATCH_MODULES = tuple(m for m in (_mtp, _utils) if m is not None)

# {模块: install 时垫在守卫下面的原件}。**在 install 时按当时现状捕获**，不能用
# import 期快照——第三方若在本模块 import 之后打了补丁，按陈旧快照还原会把它抹掉。
_rounds_batch_originals: dict = {}


def rounds_batch_guarded(*args, **kwargs):
    """B>1 的批处理轮循环：有活动处理器时拒跑。

    批处理走的是另一组走查函数（`_speculative_walk_batch_*`），本模块没补。
    本栈恒 B=1（CHARTER §3 单 worker 串行），故正常不可达；Q14 若放开并发、
    MTP 与批处理叠加，这里会**响**而不是把处理器悄悄丢掉——那正是本模块存在的
    理由，不能从批处理入口再漏一次。
    """
    if _ctx() is not None:
        raise RuntimeError(
            "MTP 批处理（B>1）轮循环尚未补 logits 处理器：该路会静默丢弃 "
            "repetition_penalty 与工具约束 guard。并发 × MTP 需先补 "
            "_speculative_walk_batch_* 一族，见 mtp_processors 模块头。"
        )
    return _ORIGINAL_ROUNDS_BATCH(*args, **kwargs)


def install() -> bool:
    """装上替换实现（幂等，且校验实时性）。

    批处理守卫的覆盖校验对着**模块本体**查、逐处打阴性声明——
    对着刚写过的容器查是恒真（首过评审 Critical），什么也证明不了。
    """
    changed = False
    if _mtp._mtp_acceptance_walk is not acceptance_walk_with_processors:
        _mtp._mtp_acceptance_walk = acceptance_walk_with_processors
        changed = True
    for mod in _ROUNDS_BATCH_MODULES:
        cur = getattr(mod, "_mtp_rounds_batch", None)
        if cur is rounds_batch_guarded:
            continue
        if cur is None:
            # 上游改名/挪走：守卫覆盖不到这个分派点。静默不装 = B>1 时处理器
            # 被丢而无人知；按"每种拒装都打一行"的纪律打出来。
            print(f"[MTP] 批处理守卫未覆盖 {mod.__name__}：上游无 _mtp_rounds_batch，"
                  "接口已变——放开 B>1 前必须先重适配")
            continue
        if _ORIGINAL_ROUNDS_BATCH is None or cur is not _ORIGINAL_ROUNDS_BATCH:
            # 第三方已占位：宁可不装也不抹别人（moe_sort_threshold 同纪律）。
            # 守卫的委派目标是上游原件，盖在第三方上还会把它整个旁路掉。
            print(f"[MTP] 批处理守卫未覆盖 {mod.__name__}：该绑定已被第三方实现 "
                  f"{getattr(cur, '__qualname__', repr(cur))} 占用，拒绝覆盖")
            continue
        _rounds_batch_originals[mod] = cur
        mod._mtp_rounds_batch = rounds_batch_guarded
        changed = True
    return changed


def uninstall() -> bool:
    """还原上游实现（仅供测试隔离；生产不调）。"""
    changed = False
    if _mtp._mtp_acceptance_walk is not _ORIGINAL_WALK:
        _mtp._mtp_acceptance_walk = _ORIGINAL_WALK
        changed = True
    for mod in list(_rounds_batch_originals):
        # 只还原**当前确实挂着本模块守卫版**的那处：有人垫在守卫之上时不动它、
        # 也保留原件记录，等下次真轮到我们时再还原（不抹第三方）。
        if getattr(mod, "_mtp_rounds_batch", None) is rounds_batch_guarded:
            mod._mtp_rounds_batch = _rounds_batch_originals.pop(mod)
            changed = True
    return changed
