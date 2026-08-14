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
故 B=1 时两条走查都汇到 `_mtp_acceptance_walk` 一个函数：
- 非贪心（生产 temperature 缺省 0.2）→ `_positioned_target_tokens` 批量采全部位置；
- 贪心（bench 口径）→ `_speculative_walk_deferred_greedy` 逐位延迟。

本模块只替换 `_mtp_acceptance_walk`，**无活动处理器时原样委派上游**（零行为变化）。

# 顺序化为什么不损失正确性

被接受的 token 恒等于该位置的 target token（草稿被接受 ⟺ draft[i] == target[i]），
且走查在首次不匹配处停。故本轮实际产出恒为 `target[0..accepted]`，位置 `pos` 的
正确历史就是「已产出 + 本轮 target[0..pos)」——全是真 token，**一个被拒草稿都不含**。
这条对 `_GuardAdapter` 是硬要求：它按 `_seen` 单调消费，喂进一个草稿就不可逆地跑偏。

批量采样那条路做不到这件事（一次性对全部位置采样，无法给每个位置不同历史），
所以此处一律顺序化；`sample_target` 接显式 positions，逐位调用与批量调用的 RNG
流等价，确定性不受影响。

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

    processors 为空时不建上下文——走查将原样委派上游。
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
        # 无活动处理器：原样委派上游，零行为变化
        return _ORIGINAL_WALK(
            lm, verify, draft_tokens, sampler, budget,
            row_id=row_id, base_position=base_position,
        )

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
    """装上替换实现（幂等，且校验实时性）。"""
    changed = False
    if _mtp._mtp_acceptance_walk is not acceptance_walk_with_processors:
        _mtp._mtp_acceptance_walk = acceptance_walk_with_processors
        changed = True
    if _ORIGINAL_ROUNDS_BATCH is not None and \
            _mtp._mtp_rounds_batch is not rounds_batch_guarded:
        _mtp._mtp_rounds_batch = rounds_batch_guarded
        changed = True
    return changed


def uninstall() -> bool:
    """还原上游实现（仅供测试隔离；生产不调）。"""
    changed = False
    if _mtp._mtp_acceptance_walk is not _ORIGINAL_WALK:
        _mtp._mtp_acceptance_walk = _ORIGINAL_WALK
        changed = True
    if _ORIGINAL_ROUNDS_BATCH is not None and \
            _mtp._mtp_rounds_batch is not _ORIGINAL_ROUNDS_BATCH:
        _mtp._mtp_rounds_batch = _ORIGINAL_ROUNDS_BATCH
        changed = True
    return changed
