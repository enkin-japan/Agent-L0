"""模型身份护栏（分离部署审计缺陷二，2026-08-31）。

守的不变量：
- inference-server /health 报模型身份（model_repo/model_revision/capabilities +
  service），字段在 lifespan 定案——执行端据此**发现**算力端真身，不再两侧手抄；
- RemoteBackend 装配期比对本侧期望（cfg.expected_model，由 app.py 主端点 /
  engine_pool 专科端点注入）与服务端真身：repo 不符 / revision 两侧都钉且不符 /
  服务端未报身份（旧版 server、外来程序占端口）→ 响亮拒启（RuntimeError），
  绝不留到首轮生成让错分词器静默出垃圾；
- expected_model 未注入（直接构造/测试路径）→ 护栏整个跳过，既有行为不变；
- 接线源锚：app.py 的 remote 分支与 engine_pool 懒装配必须真的注入 expected_model，
  RemoteBackend.load 必须真的调用护栏——函数级测试杀不掉「护栏没接上」的变异。
"""
import ast
import asyncio
import importlib.util
import pathlib

import pytest

from framework.backends.remote.backend import RemoteBackend, guard_model_identity

_REPO = pathlib.Path(__file__).resolve().parent.parent.parent
_SERVER = _REPO / "inference" / "server.py"

_EXPECT = {"repo": "org/model-a", "revision": "rev1"}


def _ident(**over):
    base = {"status": "ok", "service": "macagent-inference",
            "model_repo": "org/model-a", "model_revision": "rev1",
            "capabilities": ["text", "vision"]}
    base.update(over)
    return base


# ---------- 护栏函数四态 ----------

def test_full_match_passes(capsys):
    guard_model_identity(_ident(), _EXPECT, "http://h:1")
    assert "核对通过" in capsys.readouterr().out


def test_repo_mismatch_refuses():
    with pytest.raises(RuntimeError, match="档位错开"):
        guard_model_identity(_ident(model_repo="org/model-b"), _EXPECT, "http://h:1")


def test_revision_mismatch_refuses_when_both_pinned():
    with pytest.raises(RuntimeError, match="revision 错开"):
        guard_model_identity(_ident(model_revision="rev2"), _EXPECT, "http://h:1")


@pytest.mark.parametrize("exp_rev,got_rev", [(None, "rev2"), ("rev1", None), (None, None)])
def test_revision_single_sided_not_compared(exp_rev, got_rev):
    # 单侧缺省不比：服务端可能解析了默认 revision 而本侧未钉扎（反之亦然）
    guard_model_identity(_ident(model_revision=got_rev),
                         {"repo": "org/model-a", "revision": exp_rev}, "http://h:1")


def test_missing_identity_refuses_old_server():
    with pytest.raises(RuntimeError, match="未报模型身份"):
        guard_model_identity({"status": "ok"}, _EXPECT, "http://h:1")


def test_foreign_service_refused_even_with_matching_repo():
    # service 无条件核对（首评 S4）：repo 恰好对上也不放行外来服务
    with pytest.raises(RuntimeError, match="外来程序"):
        guard_model_identity(_ident(service="some-other-app"), _EXPECT, "http://h:1")


def test_non_dict_ident_refused_legibly():
    # 外来 http 服务回 JSON 标量/数组（首评 S3）：按「未报身份」拒，不许裸 AttributeError
    for weird in ("ok", ["a"], 5, None):
        with pytest.raises(RuntimeError, match="未报模型身份"):
            guard_model_identity(weird, _EXPECT, "http://h:1")


def test_empty_expected_skips_entirely():
    # 文档契约（首评 S5）：expected 无 repo 时函数自身整个跳过，错开也不拦
    guard_model_identity({"status": "ok"}, {}, "http://h:1")
    guard_model_identity(_ident(model_repo="org/model-b"), {}, "http://h:1")
    guard_model_identity(_ident(), None, "http://h:1")


# ---------- RemoteBackend.load 接线（护栏必须真的被调用） ----------

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Session:
    """假 requests.Session：/health 回注入的 payload，其余方法不该被走到。"""
    payload = {}

    def __init__(self):
        self.headers = {}

    def get(self, url, timeout=None):
        assert url.endswith("/health")
        return _Resp(_Session.payload)


@pytest.fixture
def fake_wire(monkeypatch):
    import framework.backends.remote.backend as mod
    _Session.payload = {}
    monkeypatch.setattr(mod.requests, "Session", _Session)
    monkeypatch.setattr("transformers.AutoTokenizer.from_pretrained",
                        staticmethod(lambda p: object()))
    # 端点表桩掉：load 里的 resolve_endpoints() 零参会读真 config——将来 endpoints
    # 声明坏掉不该把护栏测试一起染红（首评 nit）
    monkeypatch.setattr("framework.utils.model_selection.resolve_endpoints",
                        lambda cfg=None: [{"name": "main", "repo": "org/model-a",
                                           "revision": "rev1", "host": "127.0.0.1", "port": 1}])

    def load(payload, **cfg):
        _Session.payload = payload
        return asyncio.run(RemoteBackend.load(
            "/nonexistent-tokenizer-dir",
            remote={"host": "127.0.0.1", "port": 1}, **cfg))
    return load


def test_load_refuses_on_mismatch_through_wire(fake_wire):
    with pytest.raises(RuntimeError, match="档位错开"):
        fake_wire(_ident(model_repo="org/model-b"), expected_model=_EXPECT)


def test_load_refuses_on_bare_health_through_wire(fake_wire):
    with pytest.raises(RuntimeError, match="未报模型身份"):
        fake_wire({"status": "ok"}, expected_model=_EXPECT)


def test_load_passes_on_match_through_wire(fake_wire):
    backend = fake_wire(_ident(), expected_model=_EXPECT)
    assert isinstance(backend, RemoteBackend)


def test_load_without_expected_model_skips_guard(fake_wire):
    # 既有行为不变：未注入期望（直接构造/测试路径）时旧式 /health 照常放行
    backend = fake_wire({"status": "ok"})
    assert isinstance(backend, RemoteBackend)


# ---------- 服务端 /health 身份 ----------

@pytest.fixture(scope="module")
def srv():
    spec = importlib.util.spec_from_file_location("inference_server_d2_under_test", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_health_reports_model_identity(srv, monkeypatch):
    monkeypatch.setattr(srv.S, "model_repo", "org/model-a")
    monkeypatch.setattr(srv.S, "model_revision", "rev1")
    monkeypatch.setattr(srv.S, "model_capabilities", ["text", "vision"])
    out = asyncio.run(srv.health())
    assert out["status"] == "ok"                       # 探活语义不变（devctl/lib_edge 轮询）
    assert out["service"] == "macagent-inference"
    assert out["model_repo"] == "org/model-a"
    assert out["model_revision"] == "rev1"
    assert out["capabilities"] == ["text", "vision"]


def test_server_lifespan_stamps_identity_fields():
    """源锚：lifespan 必须给 S.model_repo/model_revision/model_capabilities 赋值——
    否则 /health 报空串，执行端护栏把好端端的服务当「未报身份」拒掉（fail-closed
    的正确方向，但等于把缺陷二原样留着）。"""
    tree = ast.parse(_SERVER.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    stamped = {t.attr for node in ast.walk(fn) if isinstance(node, ast.Assign)
               for t in node.targets
               if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
               and t.value.id == "S"}
    assert {"model_repo", "model_revision", "model_capabilities"} <= stamped, stamped


# ---------- 注入点（app.py 主端点源锚 / engine_pool 专科端点功能级接线） ----------

def test_app_lifespan_injects_expected_model():
    """源锚（强化版，首评 S8）：注入必须 (a) 赋给 _inference_cfg，(b) 带 repo+revision
    两键，(c) 位于以 _backend_name 为条件的分支内（键 mode 会漏掉
    inference.backend=remote 的路，首评 S1），(d) 在 AgentEngine.create 之前——
    只查字符串存在杀不掉「注错变量/注错分支/注在 create 之后」的变异。"""
    tree = ast.parse((_REPO / "backend" / "app.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    inject_lines = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if "_backend_name" not in {x.id for x in ast.walk(node.test)
                                   if isinstance(x, ast.Name)}:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "_inference_cfg"
                    for t in sub.targets):
                consts = {c.value for c in ast.walk(sub.value)
                          if isinstance(c, ast.Constant)}
                if {"expected_model", "repo", "revision"} <= consts:
                    inject_lines.append(sub.lineno)
    assert inject_lines, ("app.py lifespan 必须在 _backend_name 分支内把 expected_model"
                          "（含 repo+revision）注入 _inference_cfg——否则主端点护栏永不生效")
    create_lines = [n.lineno for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "create" and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "AgentEngine"]
    assert create_lines and min(inject_lines) < min(create_lines), \
        (inject_lines, create_lines)


def test_engine_pool_wires_expected_model(monkeypatch):
    """功能级接线（首评 S9）：专科端点懒装配传给 AgentEngine.create 的 kv_config
    必须带本端点的 expected_model——源锚只证「字符串在」，这里证「值进了漏斗」。"""
    from framework.core.engine_pool import EnginePool
    captured = {}

    async def _fake_create(model_path, **kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr("framework.core.engine.AgentEngine.create", _fake_create)
    monkeypatch.setattr("framework.utils.path_resolver.resolve_tokenizer_only_path",
                        lambda repo, revision=None: "/fake-tokenizer")
    cfg = {"model": "org/main-model", "model_revision": "r0", "mode": "remote",
           "remote": {"host": "10.0.0.1", "port": 14161},
           "model_registry": [
               {"repo": "org/main-model", "revision": "r0", "capabilities": ["text"]},
               {"repo": "org/vision-model", "revision": "r1",
                "capabilities": ["text", "vision"]}],
           "endpoints": [{"name": "vision", "repo": "org/vision-model", "port": 14163}]}
    pool = EnginePool(object(), inference_cfg=cfg)
    asyncio.run(pool.engine_for("vision"))
    assert captured["kv_config"]["expected_model"] == \
        {"repo": "org/vision-model", "revision": "r1"}
