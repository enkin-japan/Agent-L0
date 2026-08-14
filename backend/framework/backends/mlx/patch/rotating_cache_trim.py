"""§S.15 根修：`RotatingKVCache.trim` 在旋转态只改记账、不搬内容。

# 伤口

上游 `RotatingKVCache` 自己声明过这条边界：

    def is_trimmable(self):
        return self.offset < self.max_size      # 旋转后 = False

    def trim(self, n):
        n = min(self.offset, n)
        self.offset -= n                        # 只动记账
        self._idx  -= n                         # 不动内容
        return n

「只递减计数器」在**未旋转的线性布局**下是无损的——`offset` 完全决定有效前缀，
物理缓冲区多大都无所谓（实测：短上下文回滚 max|Δ|=0.000000）。`is_trimmable()`
正是用来声明这个前提的。但 `trim()` 本身不检查它，而
`rollback_speculative_cache` 无条件调用 `trim()`——于是旋转态下：

    喂 A+R 后   offset=908  _idx=519  buf=519
    trim(5)     offset=903  _idx=514  buf=519   ← 内容一个字节没搬，R 仍物理驻留
    喂 probe    offset=904  _idx=1    buf=512   ← _update_in_place 前裁 7 并把
                                                  _idx 重算成 max_size，回滚那次
                                                  递减被整个丢弃

实测后果不是「5 行被拒草稿泄漏」，是 512 个槽位有 **511 个**与基线路径不一致
（token↔槽位映射整体错位），logits max|Δ|=22。掩码怎么算都对不上——所以伤口
不在 `_make_masks`（§S.15 原始定性据此修正）。

# 修法

`_update_concat` 结束时恒有 `_idx == keys.shape[2]`，即缓冲区处于**时序序**；
投机验证批 block_size≥2 走的正是 concat 路。故旋转态的正确 trim 是
**时序化 + 物理切尾**，而非递减计数器。实测三个口径全部逐位归零
（22.000000 → 0.000000，TurboQuant kv_bits=3.5 量化态同样归零）。

# 余量前提（为什么无余量态是抛错而不是降级）

concat 路留下 `max_size - 1 + S` 条，切掉 `n ≤ S-1` 后仍 ≥ max_size，故切尾后
缓冲区不会掉到 max_size 以下——这是本修法成立的前提，破了会掉进上游
`_update_in_place` 的增长分支算出负尺寸。

而 `_update_in_place`（S=1，环形就地写）留下的是**无余量**态：那次写入把最老的
槽位就地覆盖了，被覆盖的内容已不存在，**语义上无法回滚**——任何"修法"都只能
给出比基线少一个 token 的窗口。故此处显式抛错，由调用侧从源头消除该路径
（见 `gemma4_generate._launch` 的空草稿分支）。**静默降级是 §S.15 本身的成因，
不再重演。**
"""

from typing import Any

import mlx.core as mx

from mlx_vlm.models import cache as _cache_mod

__all__ = ["install", "uninstall", "trim_rotating_cache"]

_ORIGINAL_TRIM = _cache_mod.RotatingKVCache.trim


def trim_rotating_cache(self: Any, n: int) -> int:
    """`RotatingKVCache.trim` 的替换实现（语义见模块 docstring）。"""
    n = min(self.offset, n)
    if n <= 0:
        return 0

    # 未旋转 / 尚无内容：线性布局，上游"只改记账"语义本就无损，逐字节保持
    if self.keys is None or self.offset < self.max_size:
        self.offset -= n
        self._idx -= n
        return n

    # 旋转态：先还原时序序，再物理切尾
    keys = self._temporal_order(self.keys)
    values = self._temporal_order(self.values)
    valid = keys.shape[2]
    if n >= valid:
        raise ValueError(
            f"旋转态 RotatingKVCache 回滚越界：请求裁 {n} 条，时序序只有 {valid} 条"
        )

    kept = valid - n
    # 可裁的真正判据是「有没有 token 被淘汰过」，不是「切完还剩不剩 max_size」：
    #   - valid == offset：缓冲区仍持有全部历史，一个 token 都没淘汰过。此时切到
    #     max_size 以下完全安全——offset 同步降到 max_size 以下，后续回到线性布局，
    #     上游 _update_in_place 的增长式 min(step, max_size - prev) 恒为正。
    #   - kept >= max_size：有淘汰过，但切完仍够一个满窗，被淘汰的都是本就该淘汰的。
    # 两者皆不满足 = 要用已被淘汰的 token 才能凑出满窗，而它们已不存在。
    if kept < self.max_size and valid != self.offset:
        raise ValueError(
            "旋转态 RotatingKVCache 无余量可裁："
            f"时序序 {valid} 条、offset={self.offset}，裁 {n} 条后 {kept} < "
            f"max_size={self.max_size}，且已有 token 被淘汰出窗。"
            "凑满窗所需的最老 token 已不存在，语义上无法回滚——"
            "调用侧不应产生这种回滚请求（见 gemma4_generate._discard_block）。"
        )

    # 物化切片：否则惰性视图会拽住整个原缓冲区不放（上游在同类切片后同样
    # mx.contiguous）。KV 是常驻大件，这里省下的是真内存。
    self.keys = mx.contiguous(keys[..., :kept, :])
    self.values = mx.contiguous(values[..., :kept, :])
    self.offset -= n
    self._idx = kept
    return n


def install() -> bool:
    """把修正版 trim 装到 `RotatingKVCache` 上。

    幂等，且**校验实时性**：只要类上挂的不是本模块的实现（例如被别处重新打补丁
    覆盖过），就重新装上。返回 True 表示本次动了手，False 表示本来就是对的。
    """
    if _cache_mod.RotatingKVCache.trim is trim_rotating_cache:
        return False
    _cache_mod.RotatingKVCache.trim = trim_rotating_cache
    return True


def uninstall() -> bool:
    """还原上游原始 trim（仅供测试隔离；生产不调）。"""
    if _cache_mod.RotatingKVCache.trim is _ORIGINAL_TRIM:
        return False
    _cache_mod.RotatingKVCache.trim = _ORIGINAL_TRIM
    return True
