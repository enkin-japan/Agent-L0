"""m9：inference/server 约束采样引擎**启动期预建** + 请求期分级兜底（第 4 层阶段复审立案）。

守的不变量：
- 引擎在 lifespan 内、`S.dialect` 就绪后预建（源锚：调用点顺序），不再在首个工具请求
  里于事件循环线程懒建——那条路与推理线程并发碰 Rust 分词器 → `Already borrowed`
  → 进程级永久静默禁用（m9 本体）；
- 请求路径 `_tool_guard` **零 encode、零触碰 S.backend**：预建后把分词器 encode 换成
  必炸、S.backend 置 None，guard 仍能建；
- 分级：config 关 / 无 llguidance / 方言不出语法（NotImplementedError，**先于建引擎**判，
  评审 M1）→ 启动期禁用并明示；引擎建不起来 → SystemExit 拒启；编译探针其它异常 →
  不禁用、probe_ok=False；请求期异常 → 本请求 fail-open + 计数，**不**永久禁用（坏工具集
  不毒化全进程），同集进负缓存只响一次（评审 M3）；未禁用却无引擎 → 响亮抛错；
- 装配位置：guard 在 `_blocking_generate`（推理线程、gen_sem 内）装配，不在 generate()
  事件循环线程——缓存 matcher 是共享对象，闸外 reset 会拨回在飞生成的状态机（评审 M4）。
"""
import ast
import asyncio
import importlib.util
import pathlib

import pytest

from framework.core.constrained import ToolConstraintEngine as _RealEngine

_REPO = pathlib.Path(__file__).resolve().parent.parent.parent
_SERVER = _REPO / "inference" / "server.py"


@pytest.fixture(scope="module")
def srv():
    spec = importlib.util.spec_from_file_location("inference_server_m9_under_test", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Tok:
    """假 HF 分词器：只认两个标记；`encode` 可被换成必炸来证明请求路径零 encode。"""
    def __init__(self, table):
        self.table = dict(table)
        self.calls = 0

    def encode(self, text, add_special_tokens=False):
        self.calls += 1
        return list(self.table[text])


class _Markers:
    stc = "<|tool_call>"
    etc = "<tool_call|>"


class _Dialect:
    markers = _Markers()

    @staticmethod
    def tool_call_grammar(tools):
        from types import SimpleNamespace
        return SimpleNamespace(kind="lark", body=f"grammar:{[t.get('name') for t in tools]}")


class _Backend:
    def __init__(self, tok):
        self.processor = tok


class _FakeEngine:
    """桩掉 ToolConstraintEngine：记录构造实参；matcher_for 按工具名决定成败。
    键语义借真引擎的 staticmethod：负缓存键必须与正缓存同一摘要。"""
    instances = []
    _tools_key = staticmethod(_RealEngine._tools_key)

    def __init__(self, tok, stc_id, etc_id, grammar_builder):
        self.tok, self.stc_id, self.etc_id = tok, stc_id, etc_id
        self.grammar_builder = grammar_builder
        self.vocab_size = 32
        self.matcher_calls = []
        self.fail = {}           # name -> exception to raise
        _FakeEngine.instances.append(self)

    def matcher_for(self, tools):
        self.matcher_calls.append([t.get("name") for t in tools])
        for t in tools:
            exc = self.fail.get(t.get("name"))
            if exc is not None:
                raise exc
        return object()


@pytest.fixture
def armed(srv, monkeypatch):
    """config 开 + llguidance 在 + 假后端/方言/引擎；返回 (tok, 引擎桩类)。"""
    _FakeEngine.instances.clear()
    tok = _Tok({_Markers.stc: [7], _Markers.etc: [8]})
    monkeypatch.setattr(srv, "INFERENCE_CFG", {"constrained_tool_calls": True})
    monkeypatch.setattr(srv, "_llg", object())
    monkeypatch.setattr(srv, "ToolConstraintEngine", _FakeEngine)
    monkeypatch.setattr(srv.S, "backend", _Backend(tok))
    monkeypatch.setattr(srv.S, "dialect", _Dialect())
    monkeypatch.setattr(srv.S, "constraint_engine", None)
    monkeypatch.setattr(srv.S, "constraint_disabled", False)
    monkeypatch.setattr(srv.S, "constraint_probe_ok", None)
    monkeypatch.setattr(srv.S, "constraint_failed_keys", set())
    monkeypatch.setattr(srv.S, "metrics", dict(srv.S.metrics, constraint_failopen_total=0))
    return tok


_SPECS = [{"name": "good", "parameters": {"type": "object", "properties": {}}}]


def test_prebuild_builds_engine_and_probes_dialect(srv, armed):
    srv._build_constraint_engine()
    eng = srv.S.constraint_engine
    assert eng is not None and not srv.S.constraint_disabled
    assert (eng.stc_id, eng.etc_id) == (7, 8)
    assert eng.grammar_builder is srv.S.dialect.tool_call_grammar
    assert armed.calls == 2, "预建恰好 encode 两次（两个标记）"
    # 启动探针走了一遍 builder→编译链路
    assert eng.matcher_calls == [["_startup_probe"]]
    assert srv.S.constraint_probe_ok is True


def test_lifespan_calls_prebuild_after_dialect_before_registry():
    """源锚：预建调用点在 lifespan 内，且位于 S.dialect 赋值之后、S.registry 之前
    （方言/后端就绪是前提；注册表之后才受理请求）。去掉这一行 = 回到懒建 = m9 复发。"""
    tree = ast.parse(_SERVER.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    dialect_lines, build_lines, registry_lines = [], [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                        and t.value.id == "S":
                    if t.attr == "dialect":
                        dialect_lines.append(node.lineno)
                    if t.attr == "registry":
                        registry_lines.append(node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_build_constraint_engine":
            build_lines.append(node.lineno)
    assert build_lines, "lifespan 内找不到 _build_constraint_engine() 调用"
    assert dialect_lines and registry_lines, "lifespan 里找不到 S.dialect / S.registry 赋值锚点"
    assert len(build_lines) == 1, f"预建调用点应唯一（源序），现有 {build_lines}"
    assert min(dialect_lines) < build_lines[0] < min(registry_lines), \
        (dialect_lines, build_lines, registry_lines)


def test_request_path_touches_neither_tokenizer_nor_backend(srv, armed, monkeypatch):
    srv._build_constraint_engine()

    def _boom(*a, **k):
        raise AssertionError("请求路径不得 encode（与推理线程并发 → Already borrowed）")
    monkeypatch.setattr(armed, "encode", _boom)
    monkeypatch.setattr(srv.S, "backend", None)      # 连 S.backend 都不许碰
    guard = srv._tool_guard(_SPECS)
    assert isinstance(guard, srv.ToolCallGuard)
    assert (guard._stc, guard._etc) == (7, 8)
    assert srv.S.constraint_engine.matcher_calls[-1] == ["good"]


def test_request_time_failure_is_per_request_not_permanent(srv, armed):
    srv._build_constraint_engine()
    eng = srv.S.constraint_engine
    eng.fail["bad"] = RuntimeError("llguidance grammar error: synthetic")
    bad = [{"name": "bad", "parameters": {}}]
    assert srv._tool_guard(bad) is None
    assert srv.S.metrics["constraint_failopen_total"] == 1
    assert srv.S.constraint_disabled is False, "请求期异常不得永久禁用"
    assert srv.S.constraint_engine is eng
    # 下一位客户端的好工具集照常约束
    assert srv._tool_guard(_SPECS) is not None
    assert srv._tool_guard(bad) is None
    assert srv.S.metrics["constraint_failopen_total"] == 2


def test_prebuild_structural_failure_refuses_startup(srv, armed):
    armed.table[_Markers.stc] = [7, 9]        # 标记不是单 token：结构性
    with pytest.raises(SystemExit) as ei:
        srv._build_constraint_engine()
    assert "constrained_tool_calls" in str(ei.value), "拒启文案须指出逃生旋钮"


def test_prebuild_engine_ctor_failure_refuses_startup(srv, armed, monkeypatch):
    class _Broken(_FakeEngine):
        def __init__(self, *a, **k):
            raise TypeError("signature drift")
    monkeypatch.setattr(srv, "ToolConstraintEngine", _Broken)
    with pytest.raises(SystemExit):
        srv._build_constraint_engine()


def test_dialect_without_grammar_disables_before_engine_build(srv, armed, monkeypatch, capsys):
    """qwen35 形态：方言 builder 抛 NotImplementedError → 禁用，且**不建引擎、不碰分词器**
    （评审 M1：不为用不上的引擎付代价，更不因它拒启）。"""
    class _NoGrammarDialect(_Dialect):
        @staticmethod
        def tool_call_grammar(tools):
            raise NotImplementedError("qwen35 按设计不启用约束采样")
    monkeypatch.setattr(srv.S, "dialect", _NoGrammarDialect())
    srv._build_constraint_engine()                    # 不拒启
    assert srv.S.constraint_disabled is True
    assert srv.S.constraint_engine is None
    assert _FakeEngine.instances == [], "方言不出语法就不该建引擎"
    assert armed.calls == 0, "方言不出语法就不该 encode"
    assert srv.S.constraint_probe_ok is None
    assert "方言声明不出调用体语法" in capsys.readouterr().out
    assert srv._tool_guard(_SPECS) is None            # 请求期直接放行，不抛


def test_dialect_probe_generic_failure_still_builds_engine(srv, armed, monkeypatch):
    class _PickyDialect(_Dialect):
        @staticmethod
        def tool_call_grammar(tools):
            if tools and tools[0].get("name") == "_startup_probe":
                raise ValueError("probe spec not to my taste")
            return _Dialect.tool_call_grammar(tools)
    monkeypatch.setattr(srv.S, "dialect", _PickyDialect())
    srv._build_constraint_engine()
    assert srv.S.constraint_engine is not None and srv.S.constraint_disabled is False


def test_request_failure_negative_cache_same_toolset_once(srv, armed, capsys):
    """评审 M3：编译失败 matcher_for 不缓存——没有负缓存就是每请求重编译+刷屏。"""
    srv._build_constraint_engine()
    eng = srv.S.constraint_engine
    eng.fail["bad"] = RuntimeError("llguidance grammar error: synthetic")
    bad = [{"name": "bad", "parameters": {}}]
    capsys.readouterr()
    assert srv._tool_guard(bad) is None
    n_calls = len(eng.matcher_calls)
    assert "fail-open" in capsys.readouterr().out
    assert srv._tool_guard(bad) is None
    assert srv._tool_guard(bad) is None
    assert len(eng.matcher_calls) == n_calls, "同一坏工具集不得重编译"
    assert capsys.readouterr().out == "", "同一坏工具集不得再刷屏"
    assert srv.S.metrics["constraint_failopen_total"] == 3, "每次放行都计数"
    assert srv.S.constraint_disabled is False
    assert srv._tool_guard(_SPECS) is not None         # 好工具集不受影响


def test_arm_tool_guard_fills_features_on_success_only(srv, armed):
    srv._build_constraint_engine()
    feats = {}
    g = srv._arm_tool_guard(feats, _SPECS)
    assert g is not None and feats["tool_guard"] is g
    assert feats["tool_constraint"]["begin"] == _Markers.stc
    assert feats["tool_constraint"]["end"] == _Markers.etc
    feats2 = {}
    assert srv._arm_tool_guard(feats2, None) is None and feats2 == {}


def test_guard_is_armed_inside_inference_thread_not_event_loop():
    """源锚（评审 M4）：`_arm_tool_guard` 只在 `_blocking_generate`（推理线程、gen_sem 内）
    调用；generate() 体内闸外不得再出现 `_tool_guard(`/`_arm_tool_guard(` 调用。"""
    tree = ast.parse(_SERVER.read_text(encoding="utf-8"))
    gen = next(n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "generate")
    inner = next(n for n in ast.walk(gen)
                 if isinstance(n, ast.FunctionDef) and n.name == "_blocking_generate")
    inner_ids = {id(n) for n in ast.walk(inner)}
    hits_inside, hits_outside = 0, []
    for node in ast.walk(gen):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("_arm_tool_guard", "_tool_guard"):
            if id(node) in inner_ids:
                hits_inside += 1
            else:
                hits_outside.append(node.lineno)
    assert hits_inside == 1, "推理线程内应恰有一处装配"
    assert not hits_outside, f"闸外装配回流（事件循环线程 reset 共享 matcher）: {hits_outside}"


def test_probe_generic_failure_keeps_engine_armed(srv, armed, monkeypatch):
    class _PickyProbe(_FakeEngine):
        def matcher_for(self, tools):
            if tools and tools[0].get("name") == "_startup_probe":
                raise ValueError("probe spec not to my taste")
            return super().matcher_for(tools)
    monkeypatch.setattr(srv, "ToolConstraintEngine", _PickyProbe)
    srv._build_constraint_engine()
    assert srv.S.constraint_engine is not None and srv.S.constraint_disabled is False
    assert srv._tool_guard(_SPECS) is not None


def test_config_off_disables_without_touching_tokenizer(srv, armed, monkeypatch):
    monkeypatch.setattr(srv, "INFERENCE_CFG", {"constrained_tool_calls": False})
    srv._build_constraint_engine()
    assert srv.S.constraint_disabled is True and srv.S.constraint_engine is None
    assert armed.calls == 0
    assert srv._tool_guard(_SPECS) is None


def test_missing_llguidance_disables_without_exit(srv, armed, monkeypatch):
    monkeypatch.setattr(srv, "_llg", None)
    srv._build_constraint_engine()
    assert srv.S.constraint_disabled is True and srv.S.constraint_engine is None
    assert armed.calls == 0


def test_guard_without_prebuild_raises_instead_of_silent_bypass(srv, armed):
    # 未禁用、未预建：lifespan 没走预建（入口漂移）——不得静默裸奔
    with pytest.raises(RuntimeError, match="_build_constraint_engine"):
        srv._tool_guard(_SPECS)


def test_guard_without_tool_specs_is_noop(srv, armed):
    assert srv._tool_guard(None) is None
    assert srv._tool_guard([]) is None


def test_metrics_exposes_constraint_engine_ready(srv, armed, monkeypatch):
    monkeypatch.setattr(srv.S, "registry", [])
    monkeypatch.setattr(srv.S, "gens", {})
    monkeypatch.setattr(srv.S, "batch_lane", None)
    m0 = asyncio.run(srv.metrics())
    assert m0["constraint_engine_ready"] is False and m0["constraint_probe_ok"] is None
    srv._build_constraint_engine()
    out = asyncio.run(srv.metrics())
    assert out["constraint_engine_ready"] is True
    assert out["constraint_probe_ok"] is True
    assert out["constraint_failopen_total"] == 0


def test_metrics_probe_ok_false_when_compile_probe_fails(srv, armed, monkeypatch):
    class _PickyProbe(_FakeEngine):
        def matcher_for(self, tools):
            if tools and tools[0].get("name") == "_startup_probe":
                raise ValueError("probe spec not to my taste")
            return super().matcher_for(tools)
    monkeypatch.setattr(srv, "ToolConstraintEngine", _PickyProbe)
    monkeypatch.setattr(srv.S, "registry", [])
    monkeypatch.setattr(srv.S, "gens", {})
    monkeypatch.setattr(srv.S, "batch_lane", None)
    srv._build_constraint_engine()
    out = asyncio.run(srv.metrics())
    assert out["constraint_engine_ready"] is True and out["constraint_probe_ok"] is False


def test_lazy_build_is_gone_from_request_path():
    """源锚：`_tool_guard` 体内不得再出现 encode / ToolConstraintEngine( 构造——
    懒建回流就是 m9 复发，哪怕行为测试因桩的缘故没抓到。"""
    tree = ast.parse(_SERVER.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_tool_guard")
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            assert name not in ("encode", "ToolConstraintEngine"), \
                f"_tool_guard:{node.lineno} 请求路径重新碰分词器/建引擎"
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr == "constraint_disabled":
                    raise AssertionError(f"_tool_guard:{node.lineno} 请求期不得改 constraint_disabled（永久禁用回流）")
