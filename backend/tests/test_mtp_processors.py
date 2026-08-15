"""MTP 投机走查的 logits 处理器契约（全离线，假 lm/sampler，无模型无 GPU）。

钉住的行为：
1. 无活动处理器 → 原样委派上游（零行为变化，这是"开投机不改语义"的底线）；
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


def test_no_context_delegates_upstream():
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
