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


def test_inference_server_not_family_coupled():
    """server 不得 import 任何家族私有模块（CHARTER 方言层：引擎不认模型家族字面量）。

    方言只能经 `framework.dialects` 注册面取。直接 `from framework.dialects.gemma4
    import ...` 就是把 R0 修掉的耦合在服务入口重新长回来。
    """
    src = (_REPO / "inference" / "server.py").read_text(encoding="utf-8")
    assert "dialects.gemma4" not in src
    assert "core.response_parser" not in src   # R0 前的旧路径，已不存在
