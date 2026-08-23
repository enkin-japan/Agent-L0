"""MTP 投机走查的 logits 处理器契约（全离线，假 lm/sampler，无模型无 GPU）。

钉住的行为：
1. 无活动处理器 → 仍走批量快路（空上下文，apply=恒等；投影次数不随处理器开关
   而变，§2.1-c）；但融合 target / 缺投影接口的形态原样委派上游——那两条路上
   没有处理器可丢，上游语义即正确语义；
2. **两类处理器分开驱动**——约束 guard 走 `consume()`/`mask_logits()` 自有协议，
   惩罚类走 `(tokens, logits)` 且历史用 prompt 播种。首过评审的 Critical：
   拿 `(tokens, logits)` 喂 guard 会让 `_GuardAdapter._seen` 锁在 prompt 长度上，
   我方从空历史起步永远追不上 → guard 全程消费不到 → 工具约束等于不存在；
3. 被拒草稿**永不**进 guard/惩罚历史（consume 不可逆）；
4. 处理器真的能改变判决；
5. 预算截断时 accepted 计数照上游语义，但不产出的 token 不进 guard；
6. 模型自带融合 target × 有处理器 → 抛错，不静默采用。
"""
import pytest

mx = pytest.importorskip("mlx.core", reason="需要 mlx")
_mtp = pytest.importorskip("mlx_vlm.speculative.mtp", reason="需要 mlx_vlm")
_utils = pytest.importorskip("mlx_vlm.speculative.utils", reason="需要 mlx_vlm")

from framework.backends.mlx.patch import mtp_processors as mp  # noqa: E402

VOCAB = 16
PROMPT = [900, 901, 902]


class _FakeLM:
    """hidden[0, pos, 0] 编码该位置的"目标 token"，logits 在该 token 上取最大。

    必须**保留前置维度**（真件是 `embed_tokens.as_linear(norm(h))`，对最后一维做
    线性）：走查已改为一次性投影全部位置，假件若只吐单位置就测不到真实形状。
    """

    def speculative_logits_from_hidden(self, hidden):
        want = hidden[..., 0]                       # [1, n]
        onehot = (mx.arange(VOCAB)[None, None, :] == want[..., None])
        return mx.where(onehot, 10.0, 0.0)          # [1, n, VOCAB]


class _Verify:
    def __init__(self, targets, target_tokens=None):
        self.hidden = mx.array([[[float(t)] for t in targets]])   # (1, n, 1)
        self.target_tokens = target_tokens


def _sampler(logprobs):
    return mx.argmax(logprobs, axis=-1)


class _Recorder:
    """惩罚类处理器：记录每次看到的历史；不改 logits。"""

    def __init__(self):
        self.seen = []

    def __call__(self, tokens, logits):
        self.seen.append([int(t) for t in tokens.tolist()])
        return logits


class _FakeGuard:
    """约束 guard：走 consume/mask_logits 协议，可禁某个 token。"""

    def __init__(self, banned=None):
        self.consumed = []
        self.banned = banned

    def consume(self, t):
        self.consumed.append(int(t))

    def mask_logits(self, logits):
        if self.banned is None:
            return logits
        return logits + mx.where(mx.arange(VOCAB) == self.banned, -1e9, 0.0)


@pytest.fixture(autouse=True)
def _installed():
    was = _mtp._mtp_acceptance_walk is mp.acceptance_walk_with_processors
    mp.install()
    yield
    if not was:
        mp.uninstall()


def _ctx(procs):
    return mp.processor_context(procs, prompt_tokens=PROMPT, penalty_window=64)


def _walk(verify, drafts, budget=99):
    return _mtp._mtp_acceptance_walk(
        _FakeLM(), verify, mx.array([drafts]), _sampler, budget,
        row_id=0, base_position=0,
    )


def test_install_idempotent_and_uninstall():
    assert mp.install() is False
    assert mp.uninstall() is True
    assert mp.install() is True


def test_no_context_uses_batched_fast_path_not_upstream():
    """无处理器也走批量快路：投影次数不随处理器开关而变（§2.1-c 门控修正）。"""
    called = {}

    def _sentinel(*a, **k):
        called["hit"] = True
        return (0, [7])

    orig = mp._ORIGINAL_WALK
    try:
        mp._ORIGINAL_WALK = _sentinel
        got = _walk(_Verify([3, 4, 5]), [3, 4])
    finally:
        mp._ORIGINAL_WALK = orig
    assert called.get("hit") is None, "无处理器不该再委派上游"
    assert got == (2, [3, 4, 5])


def test_no_context_matches_upstream_semantics():
    """空上下文批量路与上游走查同走查/预算语义（accepted 计数、产出、截断）。

    注意钉的是**语义**不是数值：假件两种切法算得完全一致，真件上批量 vs 逐位
    投影存在内核归约次序差，argmax 平局位可翻（§1.7.4，模块头已明记）。
    budget=1 一档专门对"预算满仍继续计 accepted"（喂 KV 回滚裁剪量）的语义。
    """
    # 自防（08-16 复审修）：旧断言比的是已安装补丁函数与上游的**函数名**——
    # 那对名字是常量、恒不等，分派内部规回委派上游它照样绿（比对退化成
    # 上游自比，逐案空过）。真自防 = 逐案确认"我们这臂"没有调用过上游走查。
    for targets, drafts, budget in [([3, 4, 5], [3, 4], 99),
                                    ([3, 9, 5], [3, 4], 99),
                                    ([9, 4, 5], [3, 4], 99),
                                    ([3, 4, 5], [3, 4], 1)]:
        hits = {"n": 0}
        orig = mp._ORIGINAL_WALK

        def _spy(*a, _orig=orig, **k):
            hits["n"] += 1
            return _orig(*a, **k)

        try:
            mp._ORIGINAL_WALK = _spy
            ours = _walk(_Verify(targets), drafts, budget=budget)
        finally:
            mp._ORIGINAL_WALK = orig
        assert hits["n"] == 0, "无处理器臂委派了上游——批量快路分派被改回去了"
        upstream = mp._ORIGINAL_WALK(
            _FakeLM(), _Verify(targets), mx.array([drafts]), _sampler, budget,
            row_id=0, base_position=0)
        assert ours == upstream, (targets, drafts, budget)


def test_projection_called_once_regardless_of_processors():
    """门控修正的核心不变量：投影次数=1，不随处理器开关而变。"""

    class _CountingLM(_FakeLM):
        def __init__(self):
            self.calls = 0

        def speculative_logits_from_hidden(self, hidden):
            self.calls += 1
            return super().speculative_logits_from_hidden(hidden)

    for procs in (None, [_Recorder()]):
        lm = _CountingLM()
        with _ctx(procs):
            _mtp._mtp_acceptance_walk(
                lm, _Verify([3, 4, 5]), mx.array([[3, 4]]), _sampler, 99,
                row_id=0, base_position=0)
        assert lm.calls == 1, f"procs={procs}: 投影 {lm.calls} 次"


def test_no_context_fused_target_delegates_upstream():
    """融合 target 且无处理器：没有处理器可丢，照旧委派上游。"""
    called = {}

    def _sentinel(*a, **k):
        called["hit"] = True
        return (0, [7])

    orig = mp._ORIGINAL_WALK
    try:
        mp._ORIGINAL_WALK = _sentinel
        got = _walk(_Verify([3, 4, 5], target_tokens=mx.array([[3, 4, 5]])),
                    [3, 4])
    finally:
        mp._ORIGINAL_WALK = orig
    assert called.get("hit") is True
    assert got == (0, [7])


def test_no_context_without_projection_delegates_upstream():
    """缺 speculative_logits_from_hidden 且无处理器：照旧委派上游。"""
    called = {}

    def _sentinel(*a, **k):
        called["hit"] = True
        return (0, [7])

    class _BareLM:
        pass

    orig = mp._ORIGINAL_WALK
    try:
        mp._ORIGINAL_WALK = _sentinel
        got = _mtp._mtp_acceptance_walk(
            _BareLM(), _Verify([3, 4, 5]), mx.array([[3, 4]]), _sampler, 99,
            row_id=0, base_position=0)
    finally:
        mp._ORIGINAL_WALK = orig
    assert called.get("hit") is True
    assert got == (0, [7])


def test_penalty_history_is_seeded_with_prompt():
    """惩罚窗口必须含 prompt——从空起步会与非投机路语义分叉（评审 Critical）。"""
    rec = _Recorder()
    with _ctx([rec]):
        accepted, new_tokens = _walk(_Verify([3, 4, 5]), [3, 4])
    assert (accepted, new_tokens) == (2, [3, 4, 5])
    assert rec.seen == [PROMPT, PROMPT + [3], PROMPT + [3, 4]]


def test_penalty_window_truncates_history():
    rec = _Recorder()
    with mp.processor_context([rec], prompt_tokens=list(range(200)),
                              penalty_window=64):
        _walk(_Verify([3, 4, 5]), [3, 4])
    assert all(len(h) <= 64 for h in rec.seen)
    assert rec.seen[0] == list(range(136, 200))     # 取末 64 个


def test_guard_driven_by_its_own_protocol():
    """guard 必须收到 consume(token)，而不是 (tokens, logits)。"""
    g = _FakeGuard()
    with _ctx([g]):
        accepted, new_tokens = _walk(_Verify([3, 4, 5]), [3, 4])
    assert (accepted, new_tokens) == (2, [3, 4, 5])
    assert g.consumed == [3, 4, 5]                  # 逐位推进，无遗漏无重复


def test_rejected_draft_never_consumed_by_guard():
    """草稿在位置 1 被拒：guard 绝不能吃到那个被拒的草稿值（consume 不可逆）。"""
    g = _FakeGuard()
    rec = _Recorder()
    with _ctx([g, rec]):
        accepted, new_tokens = _walk(_Verify([3, 9, 5]), [3, 4])
    assert accepted == 1
    assert new_tokens == [3, 9]
    assert g.consumed == [3, 9]                     # 只有真正产出的
    assert 4 not in g.consumed
    assert all(4 not in h for h in rec.seen)


def test_guard_mask_can_flip_the_verdict():
    """guard 禁掉草稿值 → 该位置必被拒（证明约束真的作用在判决上）。"""
    with _ctx([_FakeGuard()]):
        base = _walk(_Verify([3, 4, 5]), [3, 4])
    assert base[0] == 2

    with _ctx([_FakeGuard(banned=4)]):
        accepted, new_tokens = _walk(_Verify([3, 4, 5]), [3, 4])
    assert accepted == 1
    assert new_tokens[0] == 3 and new_tokens[1] != 4


def test_budget_truncation_keeps_upstream_accept_count():
    """预算满后仍按上游语义累加 accepted（它决定缓存回滚裁剪量），但不再 consume。"""
    g = _FakeGuard()
    with _ctx([g]):
        accepted, new_tokens = _walk(_Verify([3, 4, 5]), [3, 4], budget=1)
    assert accepted == 2            # 与上游一致：少报即多裁，会裁掉已产出的 KV
    assert new_tokens == [3]        # 只产出预算内的
    assert g.consumed == [3]        # 未产出的绝不进 guard


def test_fused_target_tokens_with_processors_raises():
    with _ctx([_Recorder()]):
        with pytest.raises(RuntimeError, match="融合"):
            _walk(_Verify([3, 4, 5], target_tokens=mx.array([[3, 4, 5]])), [3, 4])


def test_empty_processors_means_no_context():
    with _ctx([]):
        assert mp._ctx() is None
    with _ctx(None):
        assert mp._ctx() is None


def test_context_nesting_restores():
    assert mp._ctx() is None
    with _ctx([_Recorder()]):
        outer = mp._ctx()
        assert outer is not None
        with _ctx([_Recorder()]):
            assert mp._ctx() is not outer
        assert mp._ctx() is outer
    assert mp._ctx() is None


def test_record_emitted_without_context_is_noop():
    mp.record_emitted(5, 0)          # 不该抛
    assert mp._ctx() is None


def test_dispatch_batched_iff_no_guard(monkeypatch):
    """无 guard → 批量快路；有 guard → 顺序路（状态机不可虚拟前滚）。"""
    calls = []
    orig_b, orig_s = mp._walk_batched, mp._walk_sequential
    monkeypatch.setattr(mp, "_walk_batched",
                        lambda *a, **k: (calls.append("b"), orig_b(*a, **k))[1])
    monkeypatch.setattr(mp, "_walk_sequential",
                        lambda *a, **k: (calls.append("s"), orig_s(*a, **k))[1])
    with _ctx([_Recorder()]):
        _walk(_Verify([3, 4, 5]), [3, 4])
    with _ctx([_FakeGuard()]):
        _walk(_Verify([3, 4, 5]), [3, 4])
    assert calls == ["b", "s"]


class _LastTokenBan:
    """**依赖历史**的惩罚处理器：把历史末位 token 打成 -1e9。

    等价性测试必须用它而不是 _Recorder——后者不改 logits，喂错历史照样全绿，
    `extra = draft[:min(pos, budget)]` 那行的预算钳位就没有任何断言看着
    （首过评审 Critical：换成 `draft[:pos]` 变异体在纯 _Recorder 用例下存活）。
    """

    def __call__(self, tokens, logits):
        last = tokens[-1]
        return logits + mx.where(mx.arange(VOCAB) == last, -1e9, 0.0)


def test_batched_equals_sequential_positionwise():
    """批量与顺序两条路在 temp=0 下逐位等价：accepted/产出/持久历史/计数全同。

    覆盖：全接受、中途拒、首位拒、预算截断；且用历史敏感处理器（_LastTokenBan）
    让"喂错历史"必然翻转判决。最后一组专门设计成能杀死 `draft[:pos]` 变异体：
    budget=1、pos=2 仍在草稿范围内、目标恰等于草稿末位——顺序路 pos=2 的历史
    末位是 3（ban 3，目标 4 存活 → 接受），错误的未钳位历史末位是 4
    （ban 4 → 翻拒）。
    """
    cases = [
        ([3, 4], [3, 4, 5], 99),
        ([3, 4], [3, 9, 5], 99),
        ([3, 4], [7, 4, 5], 99),
        ([3, 4], [3, 4, 5], 1),
        ([3, 4, 4], [3, 4, 4, 6], 1),   # 杀 min(pos,budget) 变异体的那组
    ]
    for drafts, targets, budget in cases:
        outs, seens = [], []
        for fn in (mp._walk_batched, mp._walk_sequential):
            rec = _Recorder()
            with _ctx([_LastTokenBan(), rec]):
                ctx = mp._ctx()
                got = fn(_FakeLM(), _Verify(targets), mx.array([drafts]),
                         _sampler, budget, 0, 0, ctx)
                outs.append((got, list(ctx.hist), ctx.n_emitted))
                seens.append(rec.seen)
        assert outs[0] == outs[1], \
            f"批量与顺序分叉: drafts={drafts} targets={targets} budget={budget} {outs}"
        # 断点前的处理器所见历史逐行相同（批量路断点后多出的行不参与比较）
        n_seq = len(seens[1])
        assert seens[0][:n_seq] == seens[1], \
            f"处理器历史分叉: drafts={drafts} targets={targets} budget={budget}"


class _PositionedSampler:
    """带 sample_target 的假采样器：记录调用参数，按 hidden 编码返回目标。"""

    def __init__(self):
        self.calls = []

    def __call__(self, logprobs):          # 不该被走到（positioned 优先）
        raise AssertionError("positioned 采样器不该落到普通调用")

    def sample_target(self, logprobs, row_ids, positions):
        self.calls.append((tuple(row_ids), tuple(positions)))
        return mx.argmax(logprobs, axis=-1)


def test_batched_equals_sequential_positioned_branch():
    """positioned 分支：两条路对已走位置的 (row_ids, positions) 调用序列逐参相同。"""
    outs, calls = [], []
    for fn in (mp._walk_batched, mp._walk_sequential):
        s = _PositionedSampler()
        with _ctx([_LastTokenBan()]):
            ctx = mp._ctx()
            got = fn(_FakeLM(), _Verify([3, 9, 5]), mx.array([[3, 4]]),
                     s, 99, 7, 100, ctx)
            outs.append((got, list(ctx.hist)))
            calls.append(s.calls)
    assert outs[0] == outs[1]
    n_seq = len(calls[1])
    assert calls[0][:n_seq] == calls[1]
    assert calls[1][0] == ((7,), (100,))          # row_id 与 base_position 真的进了参数


class _FakeLM2D:
    """吐 2 维投影（[n, VOCAB]）的形态——批量路的 else 分支要能流经 concat。"""

    def speculative_logits_from_hidden(self, hidden):
        want = hidden[0, :, 0]                    # [n]
        onehot = (mx.arange(VOCAB)[None, :] == want[:, None])
        return mx.where(onehot, 10.0, 0.0)        # [n, VOCAB]


def test_batched_handles_2d_projection():
    with _ctx([_Recorder()]):
        ctx = mp._ctx()
        accepted, new_tokens = mp._walk_batched(
            _FakeLM2D(), _Verify([3, 4, 5]), mx.array([[3, 4]]),
            _sampler, 99, 0, 0, ctx)
    assert (accepted, new_tokens) == (2, [3, 4, 5])


def test_batched_probes_beyond_break_are_called_but_discarded():
    """批量路为断点之后的位置也会构图调用惩罚处理器（纯函数，结果丢弃）。

    钉住语义：被拒草稿值可出现在那些行各自的 extra 里（行级视图），
    但**绝不进持久历史**——这与"被拒草稿永不进 guard/惩罚历史"不冲突：
    持久历史（ctx.hist）只收真产出。"""
    rec = _Recorder()
    with _ctx([rec]):
        ctx = mp._ctx()
        accepted, new_tokens = mp._walk_batched(
            _FakeLM(), _Verify([3, 9, 5]), mx.array([[3, 4]]),
            _sampler, 99, 0, 0, ctx)
    assert (accepted, new_tokens) == (1, [3, 9])
    assert len(rec.seen) == 3                    # 三行全被调用（含断点后一行）
    assert rec.seen[2] == PROMPT + [3, 4]        # 断点后行的 extra 含被拒草稿
    assert ctx.hist == PROMPT + [3, 9]           # 持久历史只有真产出


def test_batch_rounds_raise_when_processors_active():
    """B>1 走的是另一组未补处理器的走查——有处理器时必须响，不能悄悄丢。"""
    if mp._ORIGINAL_ROUNDS_BATCH is None:
        pytest.skip("上游无 _mtp_rounds_batch")
    with _ctx([_Recorder()]):
        with pytest.raises(RuntimeError, match="批处理"):
            _mtp._mtp_rounds_batch()


def test_batch_guard_replaces_both_bindings():
    """守卫必须换掉**两处**绑定，否则它是聋的。

    真实分派点是 `utils`（`from .mtp import _mtp_rounds_batch` 在 import 期抄走了
    一份副本，utils.py:79/181/288 用的都是那一份）。只换 `mtp` 的模块全局，
    B>1 × 有处理器时不会响，处理器照样被静默丢掉——那正是本模块要防的事。
    """
    if mp._ORIGINAL_ROUNDS_BATCH is None:
        pytest.skip("上游无 _mtp_rounds_batch")
    try:
        mp.install()
        assert _mtp._mtp_rounds_batch is mp.rounds_batch_guarded
        assert _utils._mtp_rounds_batch is mp.rounds_batch_guarded

        # 卸载对称还原两处，否则测试隔离本身就是漏的
        mp.uninstall()
        assert _mtp._mtp_rounds_batch is not mp.rounds_batch_guarded
        assert _utils._mtp_rounds_batch is not mp.rounds_batch_guarded
    finally:
        # 断言中途失败也要回到已安装态，不能指望下一条用例的 setup 兜底
        mp.install()


def test_batch_rounds_raise_via_utils_binding():
    """从 utils 那一份副本调进去，同样要响（这才是生产实际走的分派点）。"""
    if mp._ORIGINAL_ROUNDS_BATCH is None:
        pytest.skip("上游无 _mtp_rounds_batch")
    with _ctx([_Recorder()]):
        with pytest.raises(RuntimeError, match="批处理"):
            _utils._mtp_rounds_batch()


def test_batch_rounds_pass_through_without_processors():
    if mp._ORIGINAL_ROUNDS_BATCH is None:
        pytest.skip("上游无 _mtp_rounds_batch")
    called = {}
    orig = mp._ORIGINAL_ROUNDS_BATCH
    try:
        mp._ORIGINAL_ROUNDS_BATCH = lambda *a, **k: called.setdefault("hit", 1)
        _mtp._mtp_rounds_batch()
    finally:
        mp._ORIGINAL_ROUNDS_BATCH = orig
    assert called.get("hit") == 1


def test_logits_processor_factory_arg_order_matches_upstream():
    """钉住 make_logits_processors 的位参顺序。

    我方在 stream_generate 里按位置传七个参数构造处理器。上游若调换顺序
    （例如 penalty 与 context_size 互换），会变成"惩罚系数被当成窗口长度"这类
    **静默错**——不报错、只是惩罚全错。故把参数名序列钉在这里。
    """
    import inspect
    from mlx_vlm.sample_utils import make_logits_processors

    names = list(inspect.signature(make_logits_processors).parameters)[:7]
    assert names == [
        "logit_bias",
        "repetition_penalty",
        "repetition_context_size",
        "presence_penalty",
        "presence_context_size",
        "frequency_penalty",
        "frequency_context_size",
    ]


def test_record_emitted_skips_tokens_the_walk_already_committed():
    """归属判据：走查已 commit 的不再重复登记（重复 consume 会让 guard 跑偏）。"""
    g = _FakeGuard()
    with _ctx([g]):
        ctx = mp._ctx()
        # 模拟消费侧：第 0 个 yield 是走查没经手的 bonus
        mp.record_emitted(77, 0)
        assert g.consumed == [77] and ctx.n_emitted == 1
        # 走查产出两个
        _walk(_Verify([3, 4, 5]), [3, 4])
        assert g.consumed == [77, 3, 4, 5] and ctx.n_emitted == 4
        # 消费侧随后 yield 这三个：索引 1/2/3 均 < n_emitted → 不重复登记
        for i, t in enumerate([3, 4, 5], start=1):
            mp.record_emitted(t, i)
        assert g.consumed == [77, 3, 4, 5]


# ── 思考预算强制收束（轮界注入，2026-08-23 补课；模块头「思考预算强制收束」）──


class _FakeCriteria:
    """ThinkingBudgetCriteria 的走查侧表面：两面稳定旗 + 收束序列。

    `feed(token)` 模拟外层消费循环回流 criteria 的那次调用里**与收束相关**的
    转移：真 end token 过手 → 双旗落下（真件 __call__ 2115-2118 行为）。
    """

    NL, END = 11, 12

    def __init__(self, exceeded=True):
        self.in_thinking = True
        self.budget_exceeded = exceeded
        self.enable_thinking = True
        self._forced_sequence = [self.NL, self.END]
        self.thinking_end_token_id = self.END

    def feed(self, token):
        if token == self.END:
            self.in_thinking = False
            self.budget_exceeded = False


class _UntouchableLM:
    """收束轮不许碰模型：投影被调即失败（断言强制分支真的短路了采样）。"""

    def speculative_logits_from_hidden(self, hidden):
        raise AssertionError("收束轮不应发生投影/采样")


def _tb_ctx(criteria, procs=()):
    return mp.processor_context(list(procs), prompt_tokens=PROMPT,
                                penalty_window=64,
                                thinking_criteria=criteria)


def test_collapse_injects_sequence_without_sampling():
    """收束待决：逐轮注出 \\n → </think>，全程零采样；旗翻后回正常流。"""
    c = _FakeCriteria()
    with _tb_ctx(c):
        got1 = _mtp._mtp_acceptance_walk(
            _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 99,
            row_id=0, base_position=0)
        assert got1 == (0, [c.NL])
        c.feed(c.NL)  # 外层回流：\n 不翻旗
        got2 = _mtp._mtp_acceptance_walk(
            _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 99,
            row_id=0, base_position=0)
        assert got2 == (0, [c.END])
        c.feed(c.END)  # end 过手 → 双旗落下
        # 收束完成后走查恢复正常语义（含游标复位）
        assert _walk(_Verify([3, 4, 5]), [3, 4]) == (2, [3, 4, 5])
        assert mp._collapse().index == 0


def test_collapse_flags_down_is_passthrough():
    """criteria 在场但旗未立（预算未到/模型自行收束）→ 正常走查，游标不动。"""
    c = _FakeCriteria(exceeded=False)
    with _tb_ctx(c):
        assert _walk(_Verify([3, 4, 5]), [3, 4]) == (2, [3, 4, 5])
        assert mp._collapse().index == 0


def test_collapse_commits_forced_token_to_history():
    """强制 token 与正常产出同责：进惩罚历史/guard 水位线，消费侧不双登。"""
    rec, g = _Recorder(), _FakeGuard()
    c = _FakeCriteria()
    with _tb_ctx(c, procs=[rec, g]):
        ctx = mp._ctx()
        # 真实轮结构：bonus token（索引 0，走查没经手）先行——收束 token 在
        # 消费流里占的是 bonus 之后的索引（评审 m13：轮界不变量是
        # 「yield_index == n_emitted 时归消费侧登记，commit 后 +1」）
        mp.record_emitted(77, 0)
        assert g.consumed == [77] and ctx.n_emitted == 1
        _mtp._mtp_acceptance_walk(
            _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 99,
            row_id=0, base_position=0)
        assert ctx.hist[-1] == c.NL and g.consumed == [77, c.NL]
        assert ctx.n_emitted == 2
        # 消费侧 yield 强制 token：索引 1 < n_emitted → 不重复登记
        mp.record_emitted(c.NL, 1)
        assert g.consumed == [77, c.NL]
        c.feed(c.NL)
        # 下一轮（注 end）后，惩罚历史应含 \n（后续走查窗口看得见强制段）
        c2 = _mtp._mtp_acceptance_walk(
            _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 99,
            row_id=0, base_position=0)
        assert c2 == (0, [c.END]) and ctx.hist[-2:] == [c.NL, c.END]


def test_collapse_zero_budget_does_not_consume_cursor():
    """budget<1 防御位：不注入也不推游标（下轮 budget 恢复时从头注）。"""
    c = _FakeCriteria()
    with _tb_ctx(c):
        got = _walk(_Verify([3, 4, 5]), [3, 4], budget=0)
        assert got[1] == [] and mp._collapse().index == 0
        got2 = _mtp._mtp_acceptance_walk(
            _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 1,
            row_id=0, base_position=0)
        assert got2 == (0, [c.NL])


def test_collapse_stuck_flags_raise_instead_of_spinning():
    """序列注完旗仍立（产出未回流 criteria）→ 响，不空转。"""
    c = _FakeCriteria()
    with _tb_ctx(c):
        for _ in range(2):
            _mtp._mtp_acceptance_walk(
                _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 99,
                row_id=0, base_position=0)
        with pytest.raises(RuntimeError, match="旗未翻"):
            _mtp._mtp_acceptance_walk(
                _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 99,
                row_id=0, base_position=0)


def test_collapse_seam_armed_tracks_walk_binding():
    """在位性检查对着真实绑定：uninstall 后必须报未在位（分派门 fail-closed 依据）。"""
    assert mp.collapse_seam_armed() is True
    mp.uninstall()
    try:
        assert mp.collapse_seam_armed() is False
    finally:
        mp.install()
    assert mp.collapse_seam_armed() is True


def test_collapse_slot_nesting_restores():
    """收束槽与处理器槽同规矩：嵌套进出各自还原。"""
    c = _FakeCriteria(exceeded=False)
    assert mp._collapse() is None
    with _tb_ctx(c):
        assert mp._collapse() is not None
        with mp.processor_context([], prompt_tokens=[], penalty_window=64):
            assert mp._collapse() is None
        assert mp._collapse() is not None and mp._collapse().criteria is c
    assert mp._collapse() is None


def test_dispatch_gate_predicate_behaviour():
    """分派门谓词行为级：只在「mtp + B=1 + 接缝在位」放行，其余全拒（评审 M2：
    只锚源码字符串测不出门的活性——把门提成谓词直接驱动）。"""
    from framework.backends.mlx.patch.gemma4_generate import (
        _mtp_thinking_collapse_refusal as refusal,
    )
    assert refusal("mtp", 1) is None
    assert refusal("eagle3", 1) is not None
    assert refusal("dflash", 1) is not None
    assert refusal(None, 1) is not None
    assert refusal("mtp", 2) is not None
    mp.uninstall()
    try:
        r = refusal("mtp", 1)
        assert r is not None and "在位" in r, "接缝不在位必须拒跑"
    finally:
        mp.install()
    assert refusal("mtp", 1) is None


def test_dispatch_gate_source_anchors():
    """分派门的接线锚：谓词真的被分派处调用、非 None 真的被 raise，
    且 criteria 真的交给 bridge。行为在上一测试；这里钉接线不被绕开。"""
    import inspect
    from framework.backends.mlx.patch import gemma4_generate as gg
    src = inspect.getsource(gg)
    assert "refusal = _mtp_thinking_collapse_refusal(" in src
    assert "raise RuntimeError(refusal)" in src
    assert "thinking_criteria=mtp_thinking" in src
    # 旧的无条件拒跑不复存在（不兼容案文已由在位性门取代）
    assert "上游草稿件投机路不消费 forced_token_id" not in src


def test_bridge_arms_collapse_slot():
    """评审 C1：bridge → processor_context 的 thinking_criteria 转发是收束的
    唯一接线，删掉它整个套件曾照绿。行为级钉住：bridge 迭代期间收束槽在场
    且持有该 criteria，退出后清空；不传 criteria 则槽保持空。"""
    from framework.backends.mlx.patch.gemma4_generate import _mtp_processor_bridge

    seen = {}

    def _gen():
        col = mp._collapse()
        seen["inside"] = col is not None and col.criteria is c
        yield (5, None)

    c = _FakeCriteria(exceeded=False)
    out = list(_mtp_processor_bridge(_gen(), [], prompt_tokens=PROMPT,
                                     penalty_window=64, thinking_criteria=c))
    assert out == [(5, None)]
    assert seen["inside"] is True, "bridge 没把 criteria 装进收束槽——预算承诺空转"
    assert mp._collapse() is None, "bridge 退出后收束槽未清"

    def _gen2():
        seen["inside2"] = mp._collapse() is None
        yield (5, None)

    list(_mtp_processor_bridge(_gen2(), [], prompt_tokens=PROMPT,
                               penalty_window=64))
    assert seen["inside2"] is True, "未传 criteria 时收束槽应保持空"


def test_collapse_guard_active_fails_open_not_consume():
    """评审 M4：强制 token 未经 mask 授权——活动态 guard 整体 fail-open 且
    不喂语法外 token；非活动态照常 consume（水位线/激活扫描连续）。"""

    class _ActiveGuard(_FakeGuard):
        def __init__(self):
            super().__init__()
            self.active = True
            self.failed_with = None

        def fail_open(self, e):
            self.failed_with = e

    g = _ActiveGuard()
    c = _FakeCriteria()
    with _tb_ctx(c, procs=[g]):
        got = _mtp._mtp_acceptance_walk(
            _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 99,
            row_id=0, base_position=0)
        assert got == (0, [c.NL])
        assert g.consumed == [], "活动态 guard 不许被喂语法外 token"
        assert g.failed_with is not None, "活动态 guard 应被 fail-open"
        assert mp._ctx().hist[-1] == c.NL and mp._ctx().n_emitted == 1

    class _ActiveNoFailOpen(_FakeGuard):
        def __init__(self):
            super().__init__()
            self.active = True

    g2 = _ActiveNoFailOpen()
    c2 = _FakeCriteria()
    with _tb_ctx(c2, procs=[g2]):
        got = _mtp._mtp_acceptance_walk(
            _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 99,
            row_id=0, base_position=0)
        assert got == (0, [c2.NL]) and g2.consumed == []


def test_guard_adapter_proxies_fail_open():
    """_GuardAdapter 的 fail_open 代理真通到逻辑 guard（收束分支的依赖面）。"""
    from framework.backends.mlx.backend import _GuardAdapter

    class _Logical:
        def __init__(self):
            self.err = None
            self.active = False
            self.failed = False

        def consume(self, t):
            pass

        def fail_open(self, e):
            self.err = e

    lg = _Logical()
    adapter = _GuardAdapter(lg)
    boom = RuntimeError("x")
    adapter.fail_open(boom)
    assert lg.err is boom


def test_batch_rounds_raise_when_collapse_armed_without_processors():
    """评审 M5：开思考 × 空处理器列表时 ctx=None 而收束槽在场——批处理守卫
    必须同样拒跑，否则预算承诺从批入口静默失守。"""
    c = _FakeCriteria(exceeded=False)
    with _tb_ctx(c):
        assert mp._ctx() is None and mp._collapse() is not None
        with pytest.raises(RuntimeError, match="思考预算收束"):
            _mtp._mtp_rounds_batch()


def test_collapse_empty_forced_sequence_falls_back_to_end_token():
    """评审 m14：上游撤掉 _forced_sequence 时退化为单 token 收束；
    连 end id 都没有则响而不是 AttributeError。"""
    c = _FakeCriteria()
    c._forced_sequence = []
    with _tb_ctx(c):
        got = _mtp._mtp_acceptance_walk(
            _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 99,
            row_id=0, base_position=0)
        assert got == (0, [c.END])

    c2 = _FakeCriteria()
    c2._forced_sequence = []
    c2.thinking_end_token_id = None
    with _tb_ctx(c2):
        with pytest.raises(RuntimeError, match="重适配"):
            _mtp._mtp_acceptance_walk(
                _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 99,
                row_id=0, base_position=0)


def test_collapse_cursor_resets_mid_sequence():
    """评审 m15：\\n 已注（游标=1）后真 </think> 混在溢出里抢先落旗——
    下一轮回正常流且游标复位，第二次收束从头注。"""
    c = _FakeCriteria()
    with _tb_ctx(c):
        assert _mtp._mtp_acceptance_walk(
            _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 99,
            row_id=0, base_position=0) == (0, [c.NL])
        c.feed(c.END)  # 模型自己在溢出段吐了真 end，旗落
        assert _walk(_Verify([3, 4, 5]), [3, 4]) == (2, [3, 4, 5])
        assert mp._collapse().index == 0
        # 再触发一次：从序列头（\n）开始，不是从游标 1
        c.in_thinking = c.budget_exceeded = True
        assert _mtp._mtp_acceptance_walk(
            _UntouchableLM(), _Verify([3]), mx.array([[3]]), _sampler, 99,
            row_id=0, base_position=0) == (0, [c.NL])
