"""§S.15 根修金测试：旋转态 `RotatingKVCache.trim` 的几何契约。

全离线（无模型、无 GPU 依赖，只构造缓存对象与小张量），可与任何任务并跑。

钉住的行为：
1. **未旋转**（offset < max_size）：保持上游"只改记账"语义逐字节不变——线性布局
   下 offset 完全决定有效前缀，实测无损，不该被本修法波及；
2. **旋转 + 有余量**：物理切尾，缓冲区内容真的少掉 n 条，且与"从未写入被拒
   token"的路径逐位一致（这是 §S.15 的实质修复：原实现只递减计数器，被拒内容
   物理驻留，导致 512 槽位有 511 个错位、logits max|Δ|=22）；
3. **旋转 + 无余量**：显式抛错，不静默降级——该形状由单 token 就地写产生，被
   覆盖的槽位内容不可恢复，语义上无法回滚。静默降级正是 §S.15 的成因；
4. install 幂等。
"""
import pytest

mx = pytest.importorskip("mlx.core", reason="需要 mlx")
_cache_mod = pytest.importorskip("mlx_vlm.models.cache", reason="需要 mlx_vlm")

from framework.backends.mlx.patch.rotating_cache_trim import (  # noqa: E402
    install,
    trim_rotating_cache,
    uninstall,
)
from framework.backends.mlx.patch import rotating_cache_trim as _rct  # noqa: E402

RotatingKVCache = _cache_mod.RotatingKVCache
# 用补丁模块自己在导入时捕获的那份原始实现——本文件若在别处已 install 之后才导入，
# 此刻从类上读到的就是补丁本身，捕错了还原就成了空操作
_ORIGINAL = _rct._ORIGINAL_TRIM

B, H, D = 1, 2, 4


def _rows(start: int, n: int):
    """生成可辨认的 KV 行：第 i 行整行填值 start+i，便于逐行比对身份。"""
    vals = mx.arange(start, start + n, dtype=mx.float32).reshape(1, 1, n, 1)
    return mx.broadcast_to(vals, (B, H, n, D)) * mx.ones((B, H, n, D))


def _row_ids(keys):
    """把缓冲区还原成每行的标识值列表（行内恒定，取第 0 个元素即可）。"""
    return [int(v) for v in keys[0, 0, :, 0].tolist()]


@pytest.fixture(autouse=True)
def _installed():
    """装补丁跑本文件，跑完还原——这是个进程级的类方法替换，不还原会让后续
    测试的行为依赖收集顺序（本文件之外还有别处会构造 RotatingKVCache）。"""
    was_live = RotatingKVCache.trim is trim_rotating_cache
    install()
    assert RotatingKVCache.trim is trim_rotating_cache
    yield
    if not was_live:
        uninstall()


def test_install_is_idempotent_and_verifies_liveness():
    assert install() is False          # 已装 → 不动手
    RotatingKVCache.trim = _ORIGINAL   # 模拟被别处覆盖
    assert install() is True           # 校验到不是本模块的实现 → 重新装
    assert RotatingKVCache.trim is trim_rotating_cache


def test_uninstall_restores_upstream():
    assert uninstall() is True
    assert RotatingKVCache.trim is _ORIGINAL
    assert uninstall() is False
    install()


def test_unrotated_keeps_upstream_bookkeeping_semantics():
    """未旋转：只动 offset/_idx，内容与缓冲区尺寸都不该变。"""
    c = RotatingKVCache(max_size=512)
    c.update_and_fetch(_rows(0, 100), _rows(0, 100))
    buf_before = c.keys.shape[2]
    ids_before = _row_ids(c.keys)

    assert c.is_trimmable() is True
    assert c.trim(5) == 5
    assert (c.offset, c._idx) == (95, 95)
    # 上游语义：内容一个字节不搬
    assert c.keys.shape[2] == buf_before
    assert _row_ids(c.keys) == ids_before


def test_trim_zero_or_negative_is_noop():
    c = RotatingKVCache(max_size=512)
    c.update_and_fetch(_rows(0, 100), _rows(0, 100))
    assert c.trim(0) == 0
    assert c.trim(-3) == 0
    assert (c.offset, c._idx) == (100, 100)


def test_rotated_with_slack_physically_drops_tail():
    """旋转 + 有余量（concat 路留下的形状）：被拒行真的从缓冲区消失。"""
    c = RotatingKVCache(max_size=64)
    c.update_and_fetch(_rows(0, 100), _rows(0, 100))      # 首次 concat 不裁
    c.update_and_fetch(_rows(100, 8), _rows(100, 8))      # 验证批：3 接受 + 5 拒绝
    assert c.is_trimmable() is False                       # 上游声明：旋转态不可裁
    buf_before = c.keys.shape[2]
    ids_before = _row_ids(c.keys)
    assert ids_before[-5:] == [103, 104, 105, 106, 107]    # 被拒的 5 行在尾部

    assert c.trim(5) == 5

    assert c.keys.shape[2] == buf_before - 5               # 物理切尾，不是只改记账
    assert c.values.shape[2] == buf_before - 5
    assert _row_ids(c.keys) == ids_before[:-5]             # 被拒行确实没了
    assert c._idx == c.keys.shape[2]                       # 时序序标记成立
    assert c.offset == 103


def test_rotated_rollback_matches_never_fed_path():
    """回滚路径与"从未写入被拒 token"的路径，缓冲区逐行一致（§S.15 的实质判据）。"""
    rolled = RotatingKVCache(max_size=64)
    rolled.update_and_fetch(_rows(0, 100), _rows(0, 100))
    rolled.update_and_fetch(_rows(100, 8), _rows(100, 8))  # 100..107，后 5 个是草稿
    rolled.trim(5)

    clean = RotatingKVCache(max_size=64)
    clean.update_and_fetch(_rows(0, 100), _rows(0, 100))
    clean.update_and_fetch(_rows(100, 3), _rows(100, 3))   # 只喂被接受的 3 个

    assert _row_ids(rolled.keys) == _row_ids(clean.keys)
    assert (rolled.offset, rolled._idx) == (clean.offset, clean._idx)


def test_rotated_but_nothing_evicted_yet_is_trimmable():
    """刚跨窗、尚未淘汰任何 token：可裁，且允许切到 max_size 以下。

    首过评审 Critical#2 的回归：判据一度只看「切完还剩不剩 max_size」，
    把这个**无损**的形状误判成不可裁。真正的判据是「有没有 token 被淘汰过」。
    """
    c = RotatingKVCache(max_size=64)
    c.update_and_fetch(_rows(0, 60), _rows(0, 60))          # 60 < 64，未淘汰
    c.update_and_fetch(_rows(60, 5), _rows(60, 5))          # 跨窗：offset=65 >= 64
    assert c.keys.shape[2] == c.offset == 65                # valid == offset：无淘汰
    assert c.is_trimmable() is False                        # 上游判据在此过严

    assert c.trim(4) == 4
    assert _row_ids(c.keys) == list(range(61))
    assert (c.offset, c._idx) == (61, 61)

    # 切到 max_size 以下后仍能继续解码：上游增长式 min(step, max_size - prev)
    # 在 prev < max_size 时恒为正，回到线性布局
    c.update_and_fetch(_rows(61, 1), _rows(61, 1))
    assert c.offset == 62


def test_full_block_discard_on_evicted_window_raises():
    """整块作废（n == block_size）且窗口已淘汰过 token：抛错，不静默降级。

    首过评审 Critical#1：`_discard_block` 走 accepted=-1 → trim=block_size，
    切完只剩 max_size-1 条，凑满窗所需的最老 token 已被 _update_concat 的前裁
    淘汰、不复存在。调用侧已用「有 thinking 预算就不预发」消除该请求
    （见 gemma4_generate 预发段），此处是不变式破了要响的兜底。
    """
    c = RotatingKVCache(max_size=64)
    c.update_and_fetch(_rows(0, 100), _rows(0, 100))        # 已淘汰
    c.update_and_fetch(_rows(100, 5), _rows(100, 5))        # 预发块 S=5
    assert c.keys.shape[2] == 68 and c.offset == 105        # valid != offset

    with pytest.raises(ValueError, match="已有 token 被淘汰出窗"):
        c.trim(5)


def test_rotated_without_slack_from_in_place_ring_raises():
    """环形就地写留下的无余量态：同样抛错。"""
    c = RotatingKVCache(max_size=64)
    c.update_and_fetch(_rows(0, 100), _rows(0, 100))
    c.update_and_fetch(_rows(100, 3), _rows(100, 3))        # 落到 buf == max_size
    for i in range(5):                                       # 单 token 就地写，环形
        c.update_and_fetch(_rows(200 + i, 1), _rows(200 + i, 1))
    assert c.keys.shape[2] == 64

    with pytest.raises(ValueError, match="已有 token 被淘汰出窗"):
        c.trim(1)


def test_rotated_trim_beyond_valid_raises():
    c = RotatingKVCache(max_size=64)
    c.update_and_fetch(_rows(0, 100), _rows(0, 100))
    c.update_and_fetch(_rows(100, 8), _rows(100, 8))
    with pytest.raises(ValueError, match="回滚越界"):
        c.trim(c.keys.shape[2] + 1)


def test_keep_rows_are_preserved():
    """keep>0（保留前若干行不参与旋转）：切尾不该动到被保留的头部。"""
    c = RotatingKVCache(max_size=64, keep=4)
    c.update_and_fetch(_rows(0, 100), _rows(0, 100))
    c.update_and_fetch(_rows(100, 8), _rows(100, 8))
    ids_before = _row_ids(c.keys)
    assert ids_before[:4] == [0, 1, 2, 3]                    # 头部被 keep 住

    assert c.trim(5) == 5

    assert _row_ids(c.keys)[:4] == [0, 1, 2, 3]              # 仍在
    assert _row_ids(c.keys) == ids_before[:-5]
