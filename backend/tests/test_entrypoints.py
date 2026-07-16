"""独立进程入口的可导入性金测试。

守的是**入口盲区**：`inference/server.py` 是独立进程入口，金测试 import 的是
framework 包，谁都不 import 它——R0 把 `core/response_parser.py` 收编进
`dialects/gemma4/` 时它就静默断了（旧路径 import），直到设备 A 真正
`devctl.sh start --profile gpu` 才以 ModuleNotFoundError 炸出来，离引入隔了
整整一个阶段。模块重组（R0/P0 这类收编手术）最容易打断的就是这种"没人 import
的入口"，故在这里把模块级 import 钉进回归网。

只验 import 与关键装配面存在，不起 uvicorn、不加载权重（那是 lifespan 的事）。
"""
import importlib.util
import pathlib

_REPO = pathlib.Path(__file__).resolve().parent.parent.parent


def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # ModuleNotFoundError 在此直接红
    return mod


def test_inference_server_imports():
    m = _load(_REPO / "inference" / "server.py", "inference_server_under_test")
    # 关键装配面：app 存在（uvicorn 的挂载点）、状态类带 dialect（R0 后的方言消费面）
    assert hasattr(m, "app")
    assert hasattr(m.S, "dialect"), "ServerState 应持 dialect（markers 喂约束采样）"


def test_constraint_engine_call_sites_bind():
    """构造签名漂移钉（D1 实测揪出的 S.7 变体）：`ToolConstraintEngine.__init__` 加了
    `schema_builder` 参数后，engine.py 改了、server.py 没改——服务端约束采样
    **运行时才 TypeError → 永久禁用 → e4b 裸奔发畸形 JSON**（沙箱 calls=[] /
    schedule_create 参数崩的共同根因），而 import 级测试对此全绿。

    静态钉法：AST 抓出两个入口里所有 `ToolConstraintEngine(...)` 调用点的实参数量，
    与真实 `__init__` 签名 bind——再漂移时这里当场红，不必等设备 A 真跑。
    """
    import ast
    import inspect
    from framework.core.constrained import ToolConstraintEngine

    sig = inspect.signature(ToolConstraintEngine.__init__)
    call_sites = 0
    for rel in ("inference/server.py", "backend/framework/core/engine.py"):
        tree = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                    (isinstance(node.func, ast.Name) and node.func.id == "ToolConstraintEngine")
                    or (isinstance(node.func, ast.Attribute)
                        and node.func.attr == "ToolConstraintEngine")):
                call_sites += 1
                assert not node.keywords, f"{rel}:{node.lineno} 用了关键字实参，钉子只按位置数——改用位置实参或升级本测试"
                try:
                    sig.bind(None, *([object()] * len(node.args)))
                except TypeError as e:
                    raise AssertionError(
                        f"{rel}:{node.lineno} 的 ToolConstraintEngine 调用与 __init__ 签名不符"
                        f"（{len(node.args)} 个位置实参）：{e}") from None
    assert call_sites >= 2, "两个入口的构造调用点至少各一处——找不到说明有人改了名/路径"


def test_inference_server_not_family_coupled():
    """server 不得 import 任何家族私有模块（CHARTER 方言层：引擎不认模型家族字面量）。

    方言只能经 `framework.dialects` 注册面取。直接 `from framework.dialects.gemma4
    import ...` 就是把 R0 修掉的耦合在服务入口重新长回来。
    """
    src = (_REPO / "inference" / "server.py").read_text(encoding="utf-8")
    assert "dialects.gemma4" not in src
    assert "core.response_parser" not in src   # R0 前的旧路径，已不存在
