"""D0①:catalog 逃生口的取详情面——get_tool_schema 回**方言声明格式**。

回裸 JSON 的话,模型在稳定前缀里看到的是 declaration DSL、取详情拿到的是 JSON、
要发的调用是 call DSL——三种形状互相换算,取详情这步等于在教它一种没见过的写法。
"""
from types import SimpleNamespace

from framework.core.orchestrator import GET_TOOL_SCHEMA_SCHEMA, Orchestrator
from framework.dialects.gemma4.dialect import Gemma4Dialect
from framework.dialects.gemma4.dsl import format_function_declaration

_SCHEMA = {
    "name": "run_probe",
    "description": "探针工具",
    "parameters": {"type": "object",
                   "properties": {"path": {"type": "string", "description": "目标路径"}},
                   "required": ["path"]},
}


def _orch(dialect):
    """最小 Orchestrator 替身:只装 _handle_meta_call 用到的面。"""
    orch = Orchestrator.__new__(Orchestrator)
    orch.engine = SimpleNamespace(dialect=dialect)
    orch.registry = SimpleNamespace(get_all_schemas=lambda: [_SCHEMA])
    return orch


def _fetch(orch, names):
    return orch._handle_meta_call(
        session=None, call={"name": "get_tool_schema", "arguments": {"names": names}})


def test_gemma4_dialect_renders_declaration_dsl():
    """方言实现与 prompt 面声明**同源同形**(直接复用 dsl 的同一个函数)。"""
    d = Gemma4Dialect.__new__(Gemma4Dialect)
    out = d.render_tool_declaration(_SCHEMA)
    assert out == format_function_declaration(_SCHEMA)
    assert out.startswith("declaration:run_probe{")
    assert "path" in out and "required" in out


def test_fetch_returns_dialect_declaration_not_json():
    d = Gemma4Dialect.__new__(Gemma4Dialect)
    out = _fetch(_orch(d), ["run_probe"])
    assert out.startswith("declaration:run_probe{")
    assert '"parameters"' not in out            # 不是裸 JSON


def test_fetch_lists_missing_names():
    d = Gemma4Dialect.__new__(Gemma4Dialect)
    out = _fetch(_orch(d), ["run_probe", "no_such_tool"])
    assert "declaration:run_probe{" in out
    assert "no_such_tool" in out and "Unknown tools" in out


def test_fetch_falls_back_to_json_without_renderer():
    """方言未实现渲染器:回退 JSON(fail-open,功能可用只是次优)。"""
    out = _fetch(_orch(SimpleNamespace()), ["run_probe"])
    assert '"name": "run_probe"' in out


def test_fetch_falls_back_to_json_when_renderer_raises():
    class _Boom:
        def render_tool_declaration(self, tool):
            raise RuntimeError("render broke")
    out = _fetch(_orch(_Boom()), ["run_probe"])
    assert '"name": "run_probe"' in out          # 渲染炸了也不能让取详情整个失败


def test_meta_schema_itself_advertises_the_flow():
    """目录模式的说明书:get_tool_schema 自己的描述必须讲清「先取再调」的流程。"""
    assert "catalog" in GET_TOOL_SCHEMA_SCHEMA["description"]


def test_meta_schema_teaches_fetch_then_call_flow():
    """目录模式说明书要讲清「先取再调」流程——上一版只查 'catalog' 一词出现,
    判据够不着流程本身(复审 N17)。"""
    desc = GET_TOOL_SCHEMA_SCHEMA["description"]
    assert "fetch" in desc.lower() and "before calling" in desc.lower()


def test_qwen35_dialect_renders_its_own_prefix_shape():
    """qwen35 前缀形状 = <tools> 内每行一个 JSON **对象**——取详情按同形回喂
    (数组形 json.dumps(found) 连自家前缀都不像,复审 S15)。"""
    import json as _json
    from framework.dialects.qwen35.dialect import Qwen35Dialect
    d = Qwen35Dialect.__new__(Qwen35Dialect)
    out = d.render_tool_declaration(_SCHEMA)
    obj = _json.loads(out)                      # 单对象,不是数组
    assert isinstance(obj, dict) and obj["name"] == "run_probe"
