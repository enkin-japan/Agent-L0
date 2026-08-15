"""`_load_drafter` 装载期守卫的单元测试（kind 收口 + 融合采样击倒）。

08-16 复审补：此前该守卫无任何单测。三条防线各钉一条：
- kind 收口：resolved_kind != "mtp" 拒启（dflash/eagle3 轮循环不消费
  logits_processors，放行=静默丢处理器——正是 §S.3 要防的形态）；
- 融合采样击倒：mtp 形态目标的两个融合方法被实例属性遮蔽、hidden 路保留；
- 缺投影接口拒启。

不加载任何真模型：monkeypatch mlx_vlm.speculative.drafters 的两个入口，
目标件用最小假件。CPU-only，金测试安全。
"""
import types

import pytest

pytest.importorskip("mlx.core")
pytest.importorskip("mlx_vlm")

from framework.backends.mlx.backend import _load_drafter  # noqa: E402


class _HiddenLM:
    """只有 hidden 投影路的目标（gemma 形态）。"""

    def speculative_logits_from_hidden(self, hidden):
        return hidden


class _FusedLM(_HiddenLM):
    """自带融合目标采样的目标（qwen3_5 形态）。"""

    def speculative_argmax_from_hidden(self, hidden):
        return hidden

    def speculative_verify_logits(self, *a, **k):
        return None


class _Target:
    def __init__(self, lm):
        self.language_model = lm


def _stub_drafters(monkeypatch, resolved_kind):
    """把上游三个入口换成假件；backend 里的 import 在调用期发生，改模块属性即生效。

    resolve_drafter_kind 必须一并桩掉：真件会去 peek "fake-path"/config.json、
    peek 失败静默落 dflash，恰是被测守卫要拒的形态——不桩它 mtp 用例全翻车。
    """
    import mlx_vlm.speculative.drafters as drafters

    draft = types.SimpleNamespace()
    monkeypatch.setattr(
        drafters, "resolve_drafter_kind", lambda path, kind=None: resolved_kind)
    monkeypatch.setattr(
        drafters, "load_drafter", lambda path, kind=None: (draft, resolved_kind))
    monkeypatch.setattr(
        drafters, "validate_drafter_compatibility", lambda *a, **k: None)
    return draft


def test_non_mtp_kind_refused(monkeypatch):
    """dflash/eagle3 轮循环不接处理器 → 装载期拒启，不得静默降级。"""
    _stub_drafters(monkeypatch, "dflash")
    with pytest.raises(RuntimeError, match="kind='dflash'"):
        _load_drafter(_Target(_FusedLM()), "fake-path", {})


def test_eagle3_kind_refused_even_with_plain_target(monkeypatch):
    """kind 收口不依赖目标形态：普通 hidden 目标 + 非 mtp kind 同样拒。"""
    _stub_drafters(monkeypatch, "eagle3")
    with pytest.raises(RuntimeError, match="kind='eagle3'"):
        _load_drafter(_Target(_HiddenLM()), "fake-path", {})


def test_mtp_fused_methods_knocked_out(monkeypatch):
    """mtp + 融合形态：两个融合方法被遮蔽（不再可调），hidden 路保留。"""
    stub_draft = _stub_drafters(monkeypatch, "mtp")
    lm = _FusedLM()
    draft, kind = _load_drafter(_Target(lm), "fake-path", {})
    assert kind == "mtp"
    assert draft is stub_draft  # 返回的就是装载产物，未被中途偷换
    assert not callable(getattr(lm, "speculative_argmax_from_hidden", None))
    assert not callable(getattr(lm, "speculative_verify_logits", None))
    assert callable(lm.speculative_logits_from_hidden)
    # 实例级遮蔽，不污染类：新实例的融合方法必须原样可调
    assert callable(_FusedLM().speculative_argmax_from_hidden)


def test_mtp_plain_target_untouched(monkeypatch):
    """gemma 形态（无融合方法）零变化：击倒循环不进。"""
    _stub_drafters(monkeypatch, "mtp")
    lm = _HiddenLM()
    _load_drafter(_Target(lm), "fake-path", {})
    assert "speculative_argmax_from_hidden" not in vars(lm)


def test_mtp_missing_projection_refused(monkeypatch):
    """缺 hidden 投影接口 → 处理器补不上去 → 装载期拒启。"""
    _stub_drafters(monkeypatch, "mtp")

    class _NoProj:
        pass

    with pytest.raises(RuntimeError, match="speculative_logits_from_hidden"):
        _load_drafter(_Target(_NoProj()), "fake-path", {})


def test_knockout_readonly_property_refused(monkeypatch):
    """击倒兜底①：融合方法是只读 property（setattr 抛 AttributeError）→ 拒启。"""
    _stub_drafters(monkeypatch, "mtp")

    class _PropLM(_HiddenLM):
        @property
        def speculative_argmax_from_hidden(self):
            return lambda h: h

    with pytest.raises(RuntimeError, match="无法用实例属性"):
        _load_drafter(_Target(_PropLM()), "fake-path", {})


def test_knockout_silently_ignored_setattr_refused(monkeypatch):
    """击倒兜底②：setattr 被吞、置 None 后仍可调 → 拒启（不得静默带融合路上线）。"""
    _stub_drafters(monkeypatch, "mtp")

    class _StubbornLM(_HiddenLM):
        def speculative_argmax_from_hidden(self, h):
            return h

        def __setattr__(self, name, value):
            if name == "speculative_argmax_from_hidden":
                return  # 静默吞掉遮蔽
            super().__setattr__(name, value)

    with pytest.raises(RuntimeError, match="仍可调"):
        _load_drafter(_Target(_StubbornLM()), "fake-path", {})
