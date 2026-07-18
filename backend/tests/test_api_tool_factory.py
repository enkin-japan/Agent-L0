"""api_tool_factory 金测试：声明校验 fail-open、合成类的注册契约（零参实例化）、
请求语义（路径模板/参数位置/鉴权头）、审批缺省、canary 选择性挂载。"""
import requests as _requests

import pytest

from framework.mcp.base import BaseTool
from framework.mcp.tools.api_tool_factory import build_tool_classes


def _write(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")


_MINIMAL = """
name: ping_api
description: ping my service
endpoint: "http://127.0.0.1:9999/ping"
"""


def _build_one(tmp_path, text=_MINIMAL):
    _write(tmp_path, "t.yaml", text)
    classes = build_tool_classes(tmp_path)
    assert len(classes) == 1
    return classes[0]()   # 注册契约：零参实例化（registry 扫描同款）


# ---------- 声明校验（坏声明只废自己） ----------

@pytest.mark.parametrize("bad", [
    "name: BadName\ndescription: x\nendpoint: 'http://a/'",      # 名字非蛇形
    "name: ok_tool\ndescription: ''\nendpoint: 'http://a/'",     # 描述空
    "name: ok_tool\ndescription: x\nendpoint: 'ftp://a/'",       # 非 http(s)
    "name: ok_tool\ndescription: x\nendpoint: 'http://a/'\nmethod: FETCH",
    "name: ok_tool\ndescription: x\nendpoint: 'http://a/'\nauth: {header: X}",  # auth 缺 env
    "just a string",
])
def test_invalid_declarations_skipped(tmp_path, bad):
    _write(tmp_path, "bad.yaml", bad)
    assert build_tool_classes(tmp_path) == []


def test_disabled_and_duplicate_skipped(tmp_path):
    _write(tmp_path, "a.yaml", _MINIMAL + "enabled: false\n")
    assert build_tool_classes(tmp_path) == []
    _write(tmp_path, "a.yaml", _MINIMAL)
    _write(tmp_path, "b.yaml", _MINIMAL)   # 同名第二份
    assert len(build_tool_classes(tmp_path)) == 1


def test_broken_yaml_does_not_break_others(tmp_path):
    _write(tmp_path, "a.yaml", "{{{ not yaml")
    _write(tmp_path, "b.yaml", _MINIMAL)
    assert len(build_tool_classes(tmp_path)) == 1


def test_missing_dir_is_empty(tmp_path):
    assert build_tool_classes(tmp_path / "nope") == []


# ---------- 合成类契约 ----------

def test_schema_and_defaults(tmp_path):
    tool = _build_one(tmp_path, """
name: get_thing
description: fetch thing
endpoint: "http://127.0.0.1:9999/things/{id}"
params:
  properties:
    id: {type: string, description: which}
  required: [id]
""")
    assert isinstance(tool, BaseTool)
    schema = tool.get_schema()
    assert schema["name"] == "get_thing"
    assert schema["parameters"]["required"] == ["id"]
    assert tool.requires_approval is False        # GET 缺省免审批
    assert tool.is_concurrency_safe is True
    assert tool.drift_class == "api"


def test_mutating_method_defaults_to_approval(tmp_path):
    tool = _build_one(tmp_path, _MINIMAL + "method: POST\n")
    assert tool.requires_approval is True
    assert tool.is_concurrency_safe is False


def test_explicit_approval_overrides_default(tmp_path):
    tool = _build_one(tmp_path, _MINIMAL + "method: POST\nrequires_approval: false\n")
    assert tool.requires_approval is False


def test_auth_env_gates_availability(tmp_path, monkeypatch):
    tool = _build_one(tmp_path, _MINIMAL + "auth: {env: T_API_KEY}\n")
    monkeypatch.delenv("T_API_KEY", raising=False)
    assert tool.is_available() is False           # 缺密钥不注册
    monkeypatch.setenv("T_API_KEY", "sk-1")
    assert tool.is_available() is True


# ---------- 请求语义（打桩 requests.request） ----------

class _Resp:
    def __init__(self, status=200, text="ok"):
        self.status_code = status
        self.text = text


def _capture(monkeypatch, resp=None):
    calls = {}

    def fake(method, url, headers=None, timeout=None, **kw):
        calls.update(method=method, url=url, headers=headers or {},
                     timeout=timeout, **kw)
        return resp or _Resp()

    monkeypatch.setattr(_requests, "request", fake)
    return calls


def test_path_template_and_query(tmp_path, monkeypatch):
    tool = _build_one(tmp_path, """
name: get_thing
description: fetch
endpoint: "http://127.0.0.1:9999/things/{id}"
params:
  properties:
    id: {type: string}
    verbose: {type: string}
""")
    calls = _capture(monkeypatch)
    out = tool.execute(id="a/b", verbose="1")
    assert out == "ok"
    assert calls["url"] == "http://127.0.0.1:9999/things/a%2Fb"   # URL 编码替换
    assert calls["params"] == {"verbose": "1"}                     # 用掉的不再进 query
    assert "json" not in calls


def test_post_sends_json_body_and_auth_header(tmp_path, monkeypatch):
    tool = _build_one(tmp_path, _MINIMAL + """method: POST
auth: {env: T_API_KEY, header: X-Key, format: "Token {key}"}
headers: {X-Client: agent}
""")
    monkeypatch.setenv("T_API_KEY", "sk-1")
    calls = _capture(monkeypatch)
    tool.execute(title="hi")
    assert calls["method"] == "POST"
    assert calls["json"] == {"title": "hi"}
    assert calls["headers"]["X-Key"] == "Token sk-1"
    assert calls["headers"]["X-Client"] == "agent"


def test_http_error_and_exception_degrade(tmp_path, monkeypatch):
    tool = _build_one(tmp_path)
    _capture(monkeypatch, _Resp(503, "down"))
    assert "HTTP 503" in tool.execute()

    def boom(*a, **k):
        raise ConnectionError("refused")
    monkeypatch.setattr(_requests, "request", boom)
    assert "API 调用失败" in tool.execute()


# ---------- 模块命名空间卫生（registry 扫描面） ----------

def test_module_namespace_has_no_duplicate_class_refs():
    """模块级循环变量残留会让 getmembers 把同一个类数两次 → registry 重复注册
    （冒烟实测坑）。合成类在模块属性里必须每类恰好一个引用。"""
    import inspect
    from framework.mcp.tools import api_tool_factory as m
    refs = [obj for _, obj in inspect.getmembers(m)
            if inspect.isclass(obj) and issubclass(obj, BaseTool) and obj is not BaseTool]
    assert len(refs) == len(set(map(id, refs)))
    assert not hasattr(m, "_cls")


# ---------- canary 选择性挂载（巡检纳入判据 = 类覆写了 canary） ----------

def test_canary_absent_by_default(tmp_path):
    tool = _build_one(tmp_path)
    assert type(tool).canary is BaseTool.canary   # 未声明探针 = 不入巡检

def test_canary_attached_and_probes(tmp_path, monkeypatch):
    tool = _build_one(tmp_path, _MINIMAL + "canary: {arguments: {q: '1'}}\n")
    assert type(tool).canary is not BaseTool.canary
    calls = _capture(monkeypatch, _Resp(500, "boom"))
    ok, detail = tool.canary()
    assert ok is False and "HTTP 500" in detail
    assert calls["params"] == {"q": "1"}
    _capture(monkeypatch, _Resp(200, "fine"))
    ok, detail = tool.canary()
    assert ok is True
