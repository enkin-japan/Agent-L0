"""多端点解析金测试（多模型协作接口准备③）。

守两件事：
  1. resolve_endpoints(inference_cfg)：注入路径下的合成/校验逻辑——[0] 恒为
     "main"；追加端点的 name/repo/port 各类非法输入都要响亮 ValueError（装配期
     拒启，不许静默吞掉配置错误）。
  2. 真实 config 缺省路径 + RemoteBackend.base_url_for：零参调用读现网
     config.yaml（当前未声明 endpoints，应恰为单端点，与改动前行为逐字节等价）；
     RemoteBackend 的端点访问器对已声明/未声明端点名的两条路径。
"""
import pytest

from framework.backends.remote.backend import RemoteBackend
from framework.utils.model_selection import resolve_endpoints


def _cfg(endpoints=None):
    return {
        "model": "mlx-community/gemma-4-31b-it-4bit",
        "model_revision": "deadbeef",
        "remote": {"host": "100.64.0.1", "port": 14161},
        "model_registry": [{"repo": "mlx-community/gemma-4-31b-it-4bit",
                            "revision": "deadbeef", "dialect": "gemma4"},
                           {"repo": "mlx-community/Qwen3.5-9B-4bit",
                            "revision": "cafebabe", "dialect": "qwen35"}],
        **({"endpoints": endpoints} if endpoints is not None else {}),
    }


def test_main_only_when_no_endpoints_declared():
    endpoints = resolve_endpoints(_cfg())
    assert len(endpoints) == 1
    ep = endpoints[0]
    assert ep["name"] == "main"
    assert ep["port"] == 14161
    assert ep["repo"] == "mlx-community/gemma-4-31b-it-4bit"
    assert ep["host"] == "100.64.0.1"


def test_valid_extra_endpoint_appended():
    endpoints = resolve_endpoints(_cfg([
        {"name": "vision", "repo": "mlx-community/Qwen3.5-9B-4bit", "port": 14163},
    ]))
    assert len(endpoints) == 2
    main, vision = endpoints
    assert main["name"] == "main"
    assert vision["name"] == "vision"
    assert vision["repo"] == "mlx-community/Qwen3.5-9B-4bit"
    assert vision["port"] == 14163
    assert vision["revision"] == "cafebabe"
    assert vision["host"] == main["host"]


def test_duplicate_name_rejected():
    with pytest.raises(ValueError):
        resolve_endpoints(_cfg([
            {"name": "vision", "repo": "mlx-community/Qwen3.5-9B-4bit", "port": 14163},
            {"name": "vision", "repo": "mlx-community/Qwen3.5-9B-4bit", "port": 14164},
        ]))


def test_name_main_rejected():
    with pytest.raises(ValueError):
        resolve_endpoints(_cfg([
            {"name": "main", "repo": "mlx-community/Qwen3.5-9B-4bit", "port": 14163},
        ]))


def test_repo_not_in_registry_rejected():
    with pytest.raises(ValueError):
        resolve_endpoints(_cfg([
            {"name": "vision", "repo": "mlx-community/unregistered-model", "port": 14163},
        ]))


def test_port_collision_with_main_rejected():
    with pytest.raises(ValueError):
        resolve_endpoints(_cfg([
            {"name": "vision", "repo": "mlx-community/Qwen3.5-9B-4bit", "port": 14161},
        ]))


def test_port_non_int_rejected():
    with pytest.raises(ValueError):
        resolve_endpoints(_cfg([
            {"name": "vision", "repo": "mlx-community/Qwen3.5-9B-4bit", "port": "14163"},
        ]))


def test_real_config_default_path_is_single_main_endpoint():
    endpoints = resolve_endpoints()
    assert len(endpoints) >= 1
    assert endpoints[0]["name"] == "main"
    # 现网 config.yaml 未声明 inference.endpoints：应恰为单端点（零回归基线）
    assert len(endpoints) == 1


def test_remote_backend_base_url_for():
    backend = RemoteBackend(None, "http://h:1")
    assert backend.base_url_for() == "http://h:1"
    with pytest.raises(KeyError, match="未声明的推理端点"):
        backend.base_url_for("nope")
