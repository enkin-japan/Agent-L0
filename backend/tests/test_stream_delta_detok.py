"""展示增量 detokenizer 门卫的单元测试（08-16 stage review C1 回归钉）。

历史缺陷：门卫用 hasattr(text/_unflushed) 鸭子判"是不是 SPM"，而
BPEStreamingDetokenizer 恰好同名同属性 → ByteLevel 词表（Qwen 全家族）的
字节面原文被拼进流式增量，中文全程乱码（速度矩阵 31 格质量哨齐响）。
现判据 = isinstance(SPMStreamingDetokenizer)。本测试杀的变异体：把 isinstance
退回 hasattr（BPE 用例即红）。CPU-only，不装模型。
"""
import types

import pytest

pytest.importorskip("mlx.core")
pytest.importorskip("mlx_vlm")

from mlx_vlm.tokenizer_utils import (BPEStreamingDetokenizer,  # noqa: E402
                                     NaiveStreamingDetokenizer,
                                     SPMStreamingDetokenizer)

from framework.backends.mlx.backend import _select_delta_detokenizer  # noqa: E402


def _proc_with(detok_cls):
    p = types.SimpleNamespace()
    if detok_cls is not None:
        d = object.__new__(detok_cls)     # 只验 isinstance 分派，不触 __init__
        try:
            d.text, d._unflushed = "", ""  # 复刻鸭子判会看到的同名属性
        except AttributeError:
            pass                           # Naive 的 text 是只读 property，无碍判型
        p.detokenizer = d
    return p


def test_spm_selected():
    p = _proc_with(SPMStreamingDetokenizer)
    assert _select_delta_detokenizer(p) is p.detokenizer


def test_bpe_rejected():
    """BPE 有同名 text/_unflushed——鸭子判的陷阱面，必须拒。"""
    p = _proc_with(BPEStreamingDetokenizer)
    # 陷阱前提自证：若上游把这俩属性改成 property 等形态，本用例对 hasattr 变异体
    # 就不再有杀伤力，必须在这里先炸出来（评审建议）
    assert hasattr(p.detokenizer, "text") and hasattr(p.detokenizer, "_unflushed")
    assert _select_delta_detokenizer(p) is None


def test_spm_trim_space_false_still_selected():
    """上游 no-space 词表走 partial(SPMStreamingDetokenizer, trim_space=False)——
    partial 只是构造包装不改类型，isinstance 必须照样放行。"""
    import functools
    ctor = functools.partial(SPMStreamingDetokenizer, trim_space=False)
    assert ctor.func is SPMStreamingDetokenizer   # 类型不变的前提本身也钉住
    p = _proc_with(SPMStreamingDetokenizer)
    assert _select_delta_detokenizer(p) is p.detokenizer


def test_call_site_uses_helper():
    """钉调用点：把 backend 的 want_delta 支退回内联 hasattr 判（绕开 helper）的
    变异体，前四条用例全绿也杀不死——源级断言补这个缺口（评审建议）。"""
    from pathlib import Path
    import framework.backends.mlx.backend as B
    src = Path(B.__file__).read_text(encoding="utf-8")
    assert "_select_delta_detokenizer(self.processor)" in src


def test_naive_rejected():
    assert _select_delta_detokenizer(_proc_with(NaiveStreamingDetokenizer)) is None


def test_missing_detokenizer_rejected():
    assert _select_delta_detokenizer(types.SimpleNamespace()) is None
