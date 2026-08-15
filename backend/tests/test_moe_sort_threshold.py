"""MoE 专家排序阈值补丁的契约（全离线，无模型无 GPU）。

本补丁是**逐行复制上游函数体、只改一个常量**的那类补丁，风险不在算法而在
"副本悄悄过时"和"安装/卸载把别人的补丁抹掉"。故钉住的是这几件事：

1. **副本与上游逐语句一致**（AST 比对，把已知的三处替换归一化后不得有差异）。
   这是本文件最重要的一条：上游若在 `__call__` 里加了新行为，而我们仍跑旧副本，
   丢的是行为不是性能。
2. **漂移护栏本身有效**：`UPSTREAM_SHA256` 必须与当场 `inspect.getsource` 相符。
   护栏失效的典型方式不是上游变了，而是有人改了副本却忘了更新常量。
3. 安装/卸载幂等、返回值可信、阈值解析（含 `<=0` 与畸形值）。
4. **不覆盖第三方补丁、也不把第三方补丁卸掉**。
"""
import ast
import hashlib
import inspect
import textwrap

import pytest

pytest.importorskip("mlx.core", reason="需要 mlx")
_sl = pytest.importorskip("mlx_vlm.models.switch_layers", reason="需要 mlx_vlm")

from framework.backends.mlx.patch import moe_sort_threshold as P  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_production_state(monkeypatch):
    """每个用例跑完都回到生产态（已安装），避免用例间互相污染。"""
    monkeypatch.delenv("MOE_SORT_MIN_SIZE", raising=False)
    yield
    _sl.SwitchGLU.__call__ = P._ORIGINAL_CALL
    monkeypatch.delenv("MOE_SORT_MIN_SIZE", raising=False)
    P.install()


# ── 1. 副本与上游的一致性 ──────────────────────────────────────────────────

def _normalize(src: str, *, is_copy: bool) -> str:
    """把两侧归一化到可直接比对的形式。

    已知且**仅允许**的三处差异：
      · `_sl._gather_sort` / `_sl._scatter_unsort` ← 上游是裸名（副本在别的模块里）
      · `_min_size_active` ← 上游是字面量 64（这就是本补丁的全部内容）
    """
    src = textwrap.dedent(src)
    if is_copy:
        src = src.replace("_sl._gather_sort", "_gather_sort")
        src = src.replace("_sl._scatter_unsort", "_scatter_unsort")
        src = src.replace("_min_size_active", "64")
    tree = ast.parse(src)
    fn = tree.body[0]
    # 去掉 docstring（两侧本来就不同，不构成行为差异）
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    fn.name = "f"                      # 函数名不同，不算差异
    fn.returns = None                  # 返回标注不同，不算差异
    for a in fn.args.args:             # 形参类型标注不同（我方带 Any/mx.array），同上
        a.annotation = None
    fn.decorator_list = []
    return ast.dump(fn, annotate_fields=True)


def test_copy_is_statement_for_statement_identical_to_upstream():
    # 比的必须是 `P._ORIGINAL_CALL`（导入时捕获的上游原件），**不是**
    # `_sl.SwitchGLU.__call__`——后者在补丁已安装时就是我们自己的实现，
    # 一比就永远相等，用例退化成恒真。整套跑时 gemma4_generate 已装好补丁，
    # 单跑本文件时没装，于是"单跑过、全跑挂"，正是本用例第一版的症状。
    up = _normalize(inspect.getsource(P._ORIGINAL_CALL), is_copy=False)
    ours = _normalize(inspect.getsource(P.switch_glu_call), is_copy=True)
    assert ours == up, (
        "副本与上游 SwitchGLU.__call__ 已不逐语句一致。"
        "上游若加了新行为，跑旧副本丢的是行为不是性能——"
        "请逐行核对后同步副本并更新 UPSTREAM_SHA256。")


def test_upstream_sha_matches_live_source():
    live = hashlib.sha256(
        inspect.getsource(P._ORIGINAL_CALL).encode()).hexdigest()
    assert live == P.UPSTREAM_SHA256, (
        f"上游源码 sha 为 {live}，本模块记的是 {P.UPSTREAM_SHA256}。"
        "护栏会拒装（安全），但要么上游升级了、要么常量该更新了。")


def test_drift_guard_refuses_install(monkeypatch):
    monkeypatch.setattr(P, "UPSTREAM_SHA256", "0" * 64)
    _sl.SwitchGLU.__call__ = P._ORIGINAL_CALL
    assert P.install() is False
    assert _sl.SwitchGLU.__call__ is P._ORIGINAL_CALL


# ── 2. 阈值解析 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (None, 16), ("", 16), ("  ", 16),
    ("24", 24), (" 8 ", 8), ("128", 128),
    ("0", 0), ("-1", -1),
    ("l6", 16), ("abc", 16),          # 畸形 → 退回缺省（并打印告警）
])
def test_resolve_min_size(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("MOE_SORT_MIN_SIZE", raising=False)
    else:
        monkeypatch.setenv("MOE_SORT_MIN_SIZE", raw)
    assert P._resolve_min_size() == expected


def test_threshold_zero_means_do_not_install(monkeypatch):
    """`<=0` 是**不安装**，不是"永远排序"——后者与直觉相反且会拖慢 M=1 解码。"""
    _sl.SwitchGLU.__call__ = P._ORIGINAL_CALL
    for value in ("0", "-1"):
        monkeypatch.setenv("MOE_SORT_MIN_SIZE", value)
        assert P.install() is False
        assert _sl.SwitchGLU.__call__ is P._ORIGINAL_CALL


def test_threshold_above_64_actually_takes_effect(monkeypatch):
    """曾经的缺陷：阈值 >64 时委派回上游，上游内部仍按 64 判 ⇒ 静默无效。"""
    _sl.SwitchGLU.__call__ = P._ORIGINAL_CALL
    monkeypatch.setenv("MOE_SORT_MIN_SIZE", "128")
    assert P.install() is True
    assert P.min_size() == 128
    assert _sl.SwitchGLU.__call__ is P.switch_glu_call


def test_hot_path_reads_module_global_not_environ(monkeypatch):
    """阈值必须在 install 时定死：热路径每层每 token 调一次，不能查环境变量。"""
    _sl.SwitchGLU.__call__ = P._ORIGINAL_CALL
    monkeypatch.setenv("MOE_SORT_MIN_SIZE", "24")
    P.install()
    assert P.min_size() == 24
    monkeypatch.setenv("MOE_SORT_MIN_SIZE", "0")   # 装好之后再改
    assert P.min_size() == 24, "生效阈值不该随环境变量事后变化"


# ── 3. 安装/卸载语义 ───────────────────────────────────────────────────────

def test_install_uninstall_idempotent():
    _sl.SwitchGLU.__call__ = P._ORIGINAL_CALL
    assert P.install() is True
    assert P.install() is False
    assert _sl.SwitchGLU.__call__ is P.switch_glu_call
    assert P.uninstall() is True
    assert P.uninstall() is False
    assert _sl.SwitchGLU.__call__ is P._ORIGINAL_CALL


def test_install_refuses_to_overwrite_third_party_patch():
    def _someone_elses(self, x, indices):    # pragma: no cover - 只作标记
        raise AssertionError("不该被调用")

    _sl.SwitchGLU.__call__ = _someone_elses
    try:
        assert P.install() is False
        assert _sl.SwitchGLU.__call__ is _someone_elses
    finally:
        _sl.SwitchGLU.__call__ = P._ORIGINAL_CALL


def test_uninstall_does_not_clobber_third_party_patch():
    def _someone_elses(self, x, indices):    # pragma: no cover - 只作标记
        raise AssertionError("不该被调用")

    P.install()
    _sl.SwitchGLU.__call__ = _someone_elses
    try:
        assert P.uninstall() is False
        assert _sl.SwitchGLU.__call__ is _someone_elses
    finally:
        _sl.SwitchGLU.__call__ = P._ORIGINAL_CALL


def test_missing_upstream_helpers_refuses_install(monkeypatch):
    monkeypatch.delattr(_sl, "_gather_sort", raising=True)
    _sl.SwitchGLU.__call__ = P._ORIGINAL_CALL
    assert P.install() is False
    assert _sl.SwitchGLU.__call__ is P._ORIGINAL_CALL
