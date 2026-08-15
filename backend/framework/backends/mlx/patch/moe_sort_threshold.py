"""MoE 专家排序阈值：把 `SwitchGLU` 的 `do_sort` 门槛从 64 降到 16。

# 伤口

`mlx_vlm/models/switch_layers.py:175` 写着 `do_sort = indices.size >= 64`。
`indices.size = M × top_k`，26B-A4B 的 top_k=8，故 **M≥8 才排序**。
而 MTP 验证批的 M = `block_total` ∈ {2..6} → size = 16..48，**永远够不到**。

排序路（`_gather_sort` → `sorted_indices=True` → `_scatter_unsort`）把同一专家的
行聚拢。**收益不是"每个专家的权重只装载一次"**——那条在内核层为假，别再写进归档：
MLX（0.32.0 与 main 逐字相同）`GatherQMM::eval_gpu` 里唯一消费 sorted 标志的分支
守卫是 `M==1 && B>=16 && right_sorted && B/E>=4`（v0.32.0 `quantized.cpp:1583`）。
26b top_k=8 / E=128 时它要求 B≥512，即 M_tok≥64，而 MTP 验证批 B = M×top_k = 16..48，
**永远够不到**；实际派发的是 `affine_gather_qmv`，对每个 (token, 专家) 对独立读权重。

排序的真实收益是两条：
① 同一专家的 (token, 专家) 对在 `grid.z` 上相邻 ⇒ 重复读命中**硬件缓存**
   （每专家每矩阵约 2.1MB，可驻留 SLC）——省的是 DRAM 带宽，不是读取次数；
② 排序路把 `x_batch_ndims` 3→1，省掉 broadcast 寻址。
实测（§1.3-成本分解，26b-8bit）验证成本 `v(M)` 随 M **线性增长 0.21 T1/位**，
而同一批次实测的专家**并集**是强次线性的（M=4 时并集只有 1.93×，线性对数是 3.0×）
——两者的差就是这些重复读取，排序把其中一部分变成缓存命中。数值 +9.0% 不变，
变的只是归因。

净效果：MoE 档的 MTP 收益被压在 1.28×。本补丁把它抬到 **1.37×**（生产实测，
同一次装载内开/关交错 A/B **+9.0%**，输出逐字符相同）。

（**不要把 1.37× 直接去比 vLLM 的 1.50×**：那两个数用的草稿件不同——
1.37× 用 4bit 草稿件、1.50× 用 bf16。同草稿件对齐后 mlx 的每轮分母仍高
20%~52%，且其中只有约 15% 是可追的。原委见 `MTP_PROJECT.md` §4.24 与 §1.8-MoE。）

# 为什么改阈值是安全的

排序是**恒等重排**：`_gather_sort` 排完、算完、`_scatter_unsort` 还原，
数学上与不排序逐字节等价。已实测判据（`scripts/check_moe_sort_equiv.py`）：
同一次装载内开/关排序跑同一 prompt，**输出逐字符相同**。

阈值只影响"何时走排序路"，不影响任何数值。M=1（普通解码，size=8）本就
无重复可去，故缺省阈值 16 把 M=1 排除在外；预填（size≫64）上游本就排序。
落进新覆盖区间 [16,64) 的是 **MTP 验证批（M=2..7）**，以及**长度 2~7 token 的预填**
（前缀缓存命中后的尾巴常是这个形状）——后者同样是恒等重排，且同样受益。

# 覆盖面（**进程级全局，看清楚打到了谁**）

本补丁改的是 `SwitchGLU.__call__` 这个**类属性**，故一旦安装即**进程级生效**。

**打得到的**：`class SwitchGLU` 在 mlx_vlm 全树只有一处定义
（`models/switch_layers.py`），而 **41 个模型模块** import 它（mlx_vlm 0.6.13 实测，
口径 `grep -rn '^from ..switch_layers import' models/ | grep SwitchGLU | wc -l`，
**随上游版本变**，别把这个数当常量）。节选：dots1 / llama4 / gpt_oss / olmoe /
qwen3_next / mixtral / youtu_vl / kimi_k3 / deepseek_v32 / granitemoe /
minimax_m3_vl / gemma4_text。本栈只跑 gemma-4，但补丁对同进程内任何一个都生效。
另有 `inkling` 的 `InklingSwitchGLU(SwitchGLU)`（`models/inkling/language.py:491`）
**是真子类**，其 `__call__` 在 `scales_trivial()` 为真时 `super().__call__(...)`
直接落进本实现。

**打不到的**：
- laguna / moondream3 / diffusion_gemma 另有**独立的** MoE 调用点
  （各自 import `_gather_sort` 自行拼装，带各自的阈值），不经过 `SwitchGLU`。
- **同文件的 `SwitchMLP.__call__`（`switch_layers.py:214`）另有一处 64 阈值，
  本补丁不覆盖**——它是另一个类，gemma4 不用它。使用方：nemotron_h / phixtral /
  zaya1_vl。要覆盖它得再复制一份函数体，本模块有意不做（本栈用不到）。
- `minimax_m3_vl` 按配置二选一：走 `SwitchGLU` 的那支受本补丁影响，
  走 `MiniMaxPackedSwitchGLU` 的那支不受。

安全性不靠"只打到 gemma-4"，而靠两条：①排序是恒等重排，数值上任何模型都不受影响
（判据见 `scripts/check_moe_sort_equiv.py`）；②阈值 16 只让 M×top_k ∈ [16,64) 的批
新走排序路，M=1 的普通解码步（size = top_k < 16）不受影响。

草稿件那边**连 `SwitchGLU` 都没有**：`gemma-4-26b-a4b-it-assistant` 是**稠密**的
（`num_experts=None`、4 层，权重索引里零个 `expert`/`switch` 键），
故本补丁对草稿步零影响——比"M=1 够不到阈值"更彻底。

# 上游漂移

本模块**逐行复制**了上游 `SwitchGLU.__call__` 的函数体（只改一个常量）。
`install()` 先比对上游源码的 sha256，不一致就**拒绝安装并告警**——
上游若在该函数里加了新行为，我们的旧副本会把它悄悄丢掉，那比少一点性能危险得多。
"""

import hashlib
import inspect
import os
from typing import Any

import mlx.core as mx

from mlx_vlm.models import switch_layers as _sl

__all__ = ["install", "uninstall", "is_installed", "min_size",
           "switch_glu_call", "UPSTREAM_SHA256"]

_ORIGINAL_CALL = _sl.SwitchGLU.__call__

# 本副本对齐的上游版本（mlx_vlm 0.6.13）。核对方法见模块 docstring「上游漂移」。
UPSTREAM_SHA256 = "5fd90fb259959a94b11c403596b8b93ba0805e5012126fc0aae162619c5f5046"

# 缺省 16 = top_k 8 × M 2，即"只要验证批不止一位就排序"。
# **<=0 表示不安装**（保持上游的 64），不是"永远排序"——后者与直觉相反。
_DEFAULT_MIN_SIZE = 16

# 热路径读的是这个模块级 int，**不是每次都查环境变量**：
# `SwitchGLU.__call__` 每 token 每层各调一次（30 层 × 草稿步 × 验证步），
# 一次 `os.environ.get` 约 1~2 μs，400 token 的一轮生成能累出几十毫秒，
# 正好吃掉本补丁收益的一小块。阈值在 `install()` 时定死。
_min_size_active = _DEFAULT_MIN_SIZE


def min_size() -> int:
    """**上次成功解析的**阈值（`install()` 成功时从环境变量解析后定死）。

    注意它不等于"当前真正生效的阈值"：拒装态与卸载态下类上挂的是上游原件，
    真正生效的是上游写死的 **64**，本函数仍会返回上次解析出来的值。
    要判"补丁到底在不在"，调用方必须配合 `is_installed()` 一起看——
    只看本函数会把拒装读成已装（归档里两张差 9% 的表就是这么分不清来源的）。
    """
    return _min_size_active


def is_installed() -> bool:
    """本模块的实现是否真的挂在 `SwitchGLU` 上（唯一可信的"装没装"判据）。"""
    return _sl.SwitchGLU.__call__ is switch_glu_call


def _resolve_min_size() -> int:
    """从环境变量解析阈值。`MOE_SORT_MIN_SIZE` 供 bench 用，生产不设。

    值畸形时**要出声**再退回缺省：本模块每一次拒装都打了阴性声明，
    唯独打错一个字就静默用缺省，会让 bench 操作者以为覆盖生效了。
    """
    raw = os.environ.get("MOE_SORT_MIN_SIZE")
    if raw is None or raw.strip() == "":
        return _DEFAULT_MIN_SIZE
    try:
        return int(raw)
    except ValueError:
        print(f"[MoE] MOE_SORT_MIN_SIZE={raw!r} 不是整数，"
              f"退回缺省 {_DEFAULT_MIN_SIZE}（覆盖**未**生效）")
        return _DEFAULT_MIN_SIZE


def _upstream_sha() -> str:
    try:
        return hashlib.sha256(
            inspect.getsource(_ORIGINAL_CALL).encode()
        ).hexdigest()
    except (OSError, TypeError):  # 无源码（被编译/被别处包过）→ 视作漂移
        return ""


def switch_glu_call(self: Any, x: mx.array, indices: mx.array) -> mx.array:
    """`SwitchGLU.__call__` 的替换实现。

    与上游逐行相同，**唯一差异**是 `do_sort` 的阈值常量由 64 换成 `_min_size_active`
    （模块级 int，`install()` 时定死——这是热路径，不能每次查环境变量）。
    """
    x = mx.expand_dims(x, (-2, -3))

    do_sort = indices.size >= _min_size_active
    idx = indices
    inv_order = None
    if do_sort:
        x, idx, inv_order = _sl._gather_sort(x, indices)
    if self.training:
        idx = mx.stop_gradient(idx)
    x_up = self.up_proj(x, idx, sorted_indices=do_sort)
    x_gate = self.gate_proj(x, idx, sorted_indices=do_sort)
    x = self.down_proj(
        self.activation(x_up, x_gate),
        idx,
        sorted_indices=do_sort,
    )

    if do_sort:
        x = _sl._scatter_unsort(x, inv_order, indices.shape)

    return x.squeeze(-2)


def install() -> bool:
    """把降阈值版 `__call__` 装到 `SwitchGLU` 上。

    幂等：**已安装则最先早退**（静默返回 False，不动阈值也不打声明——
    早退若放在拒装分支之后，"已安装 + MOE_SORT_MIN_SIZE=0" 会打出一句
    假的"未安装"声明，恰是本模块的打印纪律要防的）。四种情况会**拒绝安装**
    （按代码顺序），每种都打印阴性声明——静默不装 = 归档里两张差 8% 的表
    分不出来源：
      1. `MOE_SORT_MIN_SIZE <= 0`：显式关闭
      2. `_gather_sort` / `_scatter_unsort` 不在上游模块里：接口变了
      3. 上游源码 sha 与本副本不符：上游漂移，副本可能已丢新行为
      4. 类上已挂第三方实现：拒绝覆盖别人的补丁
    """
    global _min_size_active
    # 已安装 ⇒ 真正什么都不做。**这一条必须最先判**，两个理由：
    # ① 早退若晚于 `_min_size_active = thresh`，装好后再 install() 会静默改阈值；
    # ② 早退若晚于拒装分支，已安装态会打出假的"未安装"声明。
    if _sl.SwitchGLU.__call__ is switch_glu_call:
        return False

    thresh = _resolve_min_size()
    if thresh <= 0:
        print("[MoE] 专家排序阈值补丁**未安装**（MOE_SORT_MIN_SIZE<=0），"
              "沿用上游阈值 64 —— MTP 验证批不会排序")
        return False

    if not (hasattr(_sl, "_gather_sort") and hasattr(_sl, "_scatter_unsort")):
        print("[MoE] 专家排序阈值补丁**未安装**：上游 switch_layers 缺 "
              "_gather_sort/_scatter_unsort，接口已变")
        return False

    actual = _upstream_sha()
    if actual != UPSTREAM_SHA256:
        print(f"[MoE] 专家排序阈值补丁**未安装**：上游 SwitchGLU.__call__ 已改动"
              f"（sha {actual[:12]} ≠ 本副本对齐的 {UPSTREAM_SHA256[:12]}）。"
              "请核对上游新实现后更新本模块的副本与 UPSTREAM_SHA256。")
        return False

    # 类上挂的既不是上游原件也不是本模块的 ⇒ 中间有第三个补丁。
    # 覆盖掉它等于**静默丢掉别人的修改**，宁可不装并说清楚
    # （`rotating_cache_trim` 的"校验实时性"是同一条纪律，首过评审 Major）。
    current = _sl.SwitchGLU.__call__
    if current is not _ORIGINAL_CALL and current is not switch_glu_call:
        print(f"[MoE] 专家排序阈值补丁**未安装**：SwitchGLU.__call__ 已被第三方"
              f"替换为 {getattr(current, '__qualname__', current)!r}，"
              "覆盖它会静默丢掉那份修改。")
        return False

    # （已安装的早退在函数最上方；走到这里 current 只可能是上游原件。）
    # 阈值先定死再换实现：反过来会有一瞬间用着旧阈值的新实现。
    _min_size_active = thresh
    _sl.SwitchGLU.__call__ = switch_glu_call
    # 阈值直接决定"哪些批会排序"，故必须打出来：归档里两张差 8% 的表若不带这个数，
    # 事后分不清是补丁生效还是热漂移（首过评审 Major）。
    covers = "覆盖 MTP 验证批" if thresh <= 16 else f"MTP 验证批需 M×top_k ≥ {thresh} 才覆盖"
    print(f"[MoE] 专家排序阈值 64 → {thresh}（{covers}）：达阈的批走排序路，"
          "同专家 (token,专家) 对聚拢，靠缓存命中回收重复读取（A/B 实测 +9%）")
    return True


def uninstall() -> bool:
    """还原上游实现（供测试隔离与 A/B 对照）。

    **只在类上挂的确实是本模块的实现时才还原**。若中间有人又打了一层，
    无条件还原会把那一层一起抹掉（首过评审 Major）。
    """
    if _sl.SwitchGLU.__call__ is not switch_glu_call:
        return False
    _sl.SwitchGLU.__call__ = _ORIGINAL_CALL
    return True
