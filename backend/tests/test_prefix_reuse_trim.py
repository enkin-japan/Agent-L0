"""T8 根修回归：前缀复用对混合缓存类型的回滚能力门 + 逐类型回滚（2026-08-17）。

守的不变量：
- 稠密 KVCache：物理切片到 prefix_len，内容恒正确；
- RotatingKVCache 线性态：记账 trim 无损；
- RotatingKVCache **旋转态**：不可无损回裁（被淘汰出窗的 token 已不存在）时
  能力门必须拒绝复用——修复前按稠密语义切 `[:prefix_len]` 在该态恒假、缓存
  带着分叉尾静默复用（08-13 bench v1「26B 复用后瞬间 EOS」形态吻合）；
  concat 余量态（valid > max_size）小幅回裁合法，走 §S.15 时序化 trim；
- 无 keys 协议的递归状态缓存（qwen3_5 线性注意力 ArraysCache）：有状态且有
  分叉尾即拒绝复用——修复前被裁剪循环整个跳过（A8 评审 C1）；
- 混合列表一票否决：任何一层不可回滚，整轮放弃。

全部 CPU 可跑（mlx 小张量）。
"""
import mlx.core as mx
import pytest

from mlx_vlm.models.cache import ArraysCache, KVCache, RotatingKVCache

# 导入即安装 §S.15 修正版 trim（gemma4_generate 模块 import 时 _install_rotating_trim）
from framework.backends.mlx.patch.gemma4_generate import (
    _prefix_reuse_blocker, _trim_caches_to)


def _feed(c, start: int, n: int, step: int = 1):
    """按 step 大小分块喂入位置编码 K/V（K[..., t, :] = t），模拟真实 update 流。"""
    t = start
    while t < start + n:
        s = min(step, start + n - t)
        k = mx.arange(t, t + s, dtype=mx.float32).reshape(1, 1, s, 1) * mx.ones((1, 1, s, 4))
        c.update_and_fetch(k, k)
        t += s


def _temporal_values(c) -> list:
    """按时序序读出缓存内容的位置值（校验回滚后内容正确性的口径）。"""
    keys = c.keys
    if hasattr(c, "_temporal_order"):
        keys = c._temporal_order(keys)
    else:
        keys = keys[..., : c.offset, :]
    return [float(keys[0, 0, i, 0]) for i in range(keys.shape[2])]


# ---------- 稠密 KVCache ----------

def test_dense_cache_slices_to_prefix():
    c = KVCache()
    _feed(c, 0, 20)
    assert _prefix_reuse_blocker([c], 15) is None
    _trim_caches_to([c], 15)
    assert c.offset == 15
    assert _temporal_values(c) == [float(i) for i in range(15)]


# ---------- RotatingKVCache 线性态 ----------

def test_rotating_linear_state_trims_bookkeeping():
    c = RotatingKVCache(max_size=8)
    _feed(c, 0, 6)
    assert _prefix_reuse_blocker([c], 4) is None
    _trim_caches_to([c], 4)
    assert c.offset == 4
    # 续喂 1 个 token 应落在位置 4（覆盖写入，与稠密语义一致）
    _feed(c, 4, 1)
    assert _temporal_values(c)[:5] == [0.0, 1.0, 2.0, 3.0, 4.0]


# ---------- RotatingKVCache 旋转态（in-place，无余量）----------

def test_rotating_rotated_state_blocks_reuse():
    """旋转后窗内最老 token 已被淘汰：任何 excess>0 都无法无损回裁 → 门必须拒。"""
    c = RotatingKVCache(max_size=8)
    _feed(c, 0, 20)                     # 逐 token 喂 → in-place 旋转态，窗=[12..19]
    blocker = _prefix_reuse_blocker([c], 15)
    assert blocker is not None and "RotatingKVCache" in blocker


def test_rotating_rotated_pure_append_is_fine():
    """excess=0（纯追加）：无需回滚，旋转态照样可复用。"""
    c = RotatingKVCache(max_size=8)
    _feed(c, 0, 20)
    assert _prefix_reuse_blocker([c], 20) is None


# ---------- RotatingKVCache concat 余量态 ----------

def test_rotating_concat_slack_trims_with_temporal_order():
    """块状喂入（S>1 concat 路）留有余量：小幅回裁合法且内容按时序序正确。"""
    c = RotatingKVCache(max_size=8)
    _feed(c, 0, 20, step=5)             # concat 路：缓冲 > max_size，留有余量
    valid = c._temporal_order(c.keys).shape[2]
    assert valid > c.max_size           # 前提自检：确实处于余量态
    excess_ok = valid - c.max_size      # 切掉 excess 后仍 ≥ 满窗
    prefix = c.offset - excess_ok
    assert _prefix_reuse_blocker([c], prefix) is None
    _trim_caches_to([c], prefix)
    assert c.offset == prefix
    vals = _temporal_values(c)
    assert len(vals) == c.max_size       # 钉住保留条数：恰好一个满窗（不多裁不少裁）
    assert vals == [float(i) for i in range(prefix - len(vals), prefix)]


# ---------- 递归状态缓存（qwen3_5 线性注意力 ArraysCache）----------

def test_arrays_cache_with_state_blocks_reuse():
    """有状态的 ArraysCache 无回滚接口：有分叉尾（excess>0）必须拒绝复用。

    修复前它被裁剪循环整个跳过——全注意力层回滚、线性层带旧尾巴（A8 评审 C1）。
    """
    kv = KVCache()
    _feed(kv, 0, 20)                    # 全注意力层：offset=20 提供流位置
    ac = ArraysCache(size=2)
    ac[0] = mx.ones((1, 4))             # 线性层递归状态非空
    blocker = _prefix_reuse_blocker([kv, ac], 15)
    assert blocker is not None and "ArraysCache" in blocker


def test_arrays_cache_pure_append_is_fine():
    kv = KVCache()
    _feed(kv, 0, 20)
    ac = ArraysCache(size=2)
    ac[0] = mx.ones((1, 4))
    assert _prefix_reuse_blocker([kv, ac], 20) is None


def test_empty_arrays_cache_does_not_block():
    """空 ArraysCache（无状态）不该拦：没有内容就没有回滚问题。"""
    kv = KVCache()
    _feed(kv, 0, 20)
    ac = ArraysCache(size=2)
    assert _prefix_reuse_blocker([kv, ac], 15) is None


# ---------- 混合列表一票否决 ----------

def test_mixed_list_single_veto():
    dense = KVCache()
    _feed(dense, 0, 20)
    rot = RotatingKVCache(max_size=8)
    _feed(rot, 0, 20)                   # 旋转态不可回裁
    assert _prefix_reuse_blocker([dense], 15) is None          # 稠密单独可以
    assert _prefix_reuse_blocker([dense, rot], 15) is not None  # 加一层即否决


# ---------- 复审补钉（2026-08-17 首过评审 C1/C2/M3/M6/minor）----------

def test_all_stateful_list_refuses_not_fail_open():
    """整组缓存都不暴露 offset：流位置不可判必须拒绝（评审 C1——fail-open 即缺陷本形）。"""
    ac = ArraysCache(size=2)
    ac[0] = mx.ones((1, 4))
    blocker = _prefix_reuse_blocker([ac], 15)
    assert blocker is not None and "offset" in blocker


def test_dense_trim_beyond_offset_is_noop():
    """prefix_len > offset 时稠密分支不得把未写入的预分配全零行抬进有效区（评审 C2）。"""
    c = KVCache()
    _feed(c, 0, 20)
    assert c.keys.shape[2] > 20          # 前提自检：256 步预分配的物理缓冲更宽
    _trim_caches_to([c], 21)
    assert c.offset == 20                # 纯追加口径：不动


def test_upstream_quantized_cache_routes_to_trim_protocol():
    """上游 QuantizedKVCache（keys 是 tuple）：稠密切片会炸，必须走 trim 协议（评审 M3）。"""
    from mlx_vlm.models.cache import QuantizedKVCache
    c = QuantizedKVCache()
    k = mx.random.normal((1, 1, 20, 64))
    c.update_and_fetch(k, k)
    assert _prefix_reuse_blocker([c], 15) is None
    _trim_caches_to([c], 15)
    assert c.offset == 15


def test_rotating_trim_then_concat_refeed_matches_reference():
    """余量态裁剪后 concat 重喂 = 生产复用路的真实形态；内容须与从未分叉的参照一致（评审 M6）。"""
    c = RotatingKVCache(max_size=8)
    _feed(c, 0, 20, step=5)              # 内容 [0..19]，concat 余量态
    valid = c._temporal_order(c.keys).shape[2]
    prefix = c.offset - (valid - c.max_size)   # 裁到仍满窗的边界
    _trim_caches_to([c], prefix)
    _feed(c, prefix, 5, step=5)          # 重喂新增段（concat 路）
    vals = _temporal_values(c)
    end = prefix + 5
    assert vals == [float(i) for i in range(end - len(vals), end)]
    assert vals[-1] == float(end - 1)


def test_prefix_beyond_stream_position_refused():
    """prefix_len 超过缓存流位置：不是纯追加而是异常态，门必须拒绝（阶段复审 m12）。"""
    c = KVCache()
    _feed(c, 0, 20)
    blocker = _prefix_reuse_blocker([c], 25)
    assert blocker is not None and "流状态异常" in blocker
