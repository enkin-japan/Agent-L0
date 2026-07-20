"""绑定护栏密钥强度 + config_loader 算力端地址 env 覆盖金测试。

守两条运行时纪律（均无既有专测，本文件新建）：
  1. 密钥强度护栏：非回环绑定时，密钥缺失/过弱（长度<16，含占位 "test"）= 拒启，
     回环绑定空钥放行（dev 现状不变）。backend/app.py 的通用 enforce_bind_guardrail
     是 §8 铁则的可执行副本（inference/rag 各持独立小副本，此处守通用函数的判据）。
  2. config_loader env 覆盖：INFER_REMOTE_HOST / RAG_COMPUTE_HOST 非空即夺权
     config.yaml 的烘死值——运行时改拓扑不必重打包/改 config。
"""
import importlib

import pytest

from framework.utils import config_loader
from app import (enforce_bind_guardrail, is_loopback_bind, preflight_bind_addr,
                 require_remote_inference_host)


# ── 1. 密钥强度护栏 ────────────────────────────────────────────────────

def test_loopback_empty_key_passes():
    # 回环绑定 + 空钥：dev 现状，放行（不抛）
    enforce_bind_guardrail("127.0.0.1", "", service="t", key_env="K")
    enforce_bind_guardrail("localhost", "", service="t", key_env="K")


def test_nonloopback_empty_key_rejected():
    with pytest.raises(SystemExit):
        enforce_bind_guardrail("100.64.0.1", "", service="t", key_env="K")


def test_nonloopback_weak_test_key_rejected():
    # 占位 "test"（4 字）属弱值 → 拒启
    with pytest.raises(SystemExit):
        enforce_bind_guardrail("100.64.0.1", "test", service="t", key_env="K")


def test_nonloopback_short_key_rejected():
    # 15 字仍不足 16 → 拒启
    with pytest.raises(SystemExit):
        enforce_bind_guardrail("100.64.0.1", "x" * 15, service="t", key_env="K")


def test_nonloopback_strong_key_passes():
    # 16 字随机 → 放行
    enforce_bind_guardrail("100.64.0.1", "0123456789abcdef", service="t", key_env="K")


def test_nonloopback_weak_key_still_passes_on_loopback():
    # 回环绑定时弱钥同样放行（强度门只在非回环生效）
    enforce_bind_guardrail("127.0.0.1", "test", service="t", key_env="K")


def test_is_loopback_bind_shapes():
    assert is_loopback_bind("127.0.0.1")
    assert is_loopback_bind("::1")
    assert not is_loopback_bind("100.64.0.1")
    assert not is_loopback_bind("0.0.0.0")


# ── 2. config_loader 算力端地址 env 覆盖 ──────────────────────────────

@pytest.fixture()
def _fresh_config():
    """每例前后清 lru_cache：env 覆盖在 load_config 返回前应用，缓存会固化首次结果。"""
    config_loader.load_config.cache_clear()
    yield
    config_loader.load_config.cache_clear()


def test_infer_remote_host_override(monkeypatch, _fresh_config):
    monkeypatch.setenv("INFER_REMOTE_HOST", "100.64.0.9")
    cfg = config_loader.load_config()
    assert cfg["inference"]["remote"]["host"] == "100.64.0.9"


def test_rag_compute_host_override(monkeypatch, _fresh_config):
    monkeypatch.setenv("RAG_COMPUTE_HOST", "100.64.0.7")
    cfg = config_loader.load_config()
    assert cfg["rag"]["compute"]["host"] == "100.64.0.7"


def test_no_env_keeps_config_value(monkeypatch, _fresh_config):
    monkeypatch.delenv("INFER_REMOTE_HOST", raising=False)
    monkeypatch.delenv("RAG_COMPUTE_HOST", raising=False)
    cfg = config_loader.load_config()
    # 未设 env：原 config 值不被替换（读回文件里的既有值，非 override 常量）
    raw = config_loader.yaml.safe_load(config_loader.CONFIG_PATH.read_text(encoding="utf-8")) or {}
    assert cfg["inference"]["remote"]["host"] == raw["inference"]["remote"]["host"]


def test_empty_env_is_noop(monkeypatch, _fresh_config):
    # 空串/纯空白不算数（strip 后为空 → 不覆盖）
    monkeypatch.setenv("INFER_REMOTE_HOST", "   ")
    cfg = config_loader.load_config()
    raw = config_loader.yaml.safe_load(config_loader.CONFIG_PATH.read_text(encoding="utf-8")) or {}
    assert cfg["inference"]["remote"]["host"] == raw["inference"]["remote"]["host"]


def test_apply_host_overrides_on_partial_dict(monkeypatch):
    # 分段缺失/为 None 时覆盖仍安全建枝（不 AttributeError）
    monkeypatch.setenv("INFER_REMOTE_HOST", "1.2.3.4")
    monkeypatch.setenv("RAG_COMPUTE_HOST", "5.6.7.8")
    out = config_loader._apply_host_overrides({"inference": None, "rag": {}})
    assert out["inference"]["remote"]["host"] == "1.2.3.4"
    assert out["rag"]["compute"]["host"] == "5.6.7.8"


# ── 3. 绑定地址本机预检（preflight_bind_addr）──────────────────────────
# 实机事故（2026-07-19）：host 填了另一台设备的 IP → uvicorn 报晦涩的 EADDRNOTAVAIL。
# preflight 把「地址不属本机」提前成一条明确指引。回环/通配/空值不检；解析失败静默放行。

def test_preflight_loopback_wildcard_empty_noop():
    # 回环/通配/空值：无须验，直接放行（不抛）
    for h in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::", ""):
        preflight_bind_addr(h, service="t", cfg_key="config 的 X")


def test_preflight_foreign_addr_rejected():
    # 203.0.113.7（TEST-NET-3）保证非本机 → EADDRNOTAVAIL 拒启
    with pytest.raises(SystemExit) as ei:
        preflight_bind_addr("203.0.113.7", service="backend",
                            cfg_key="config 的 inference.host")
    msg = str(ei.value)
    assert "不是本机" in msg
    assert "config 的 inference.host" in msg


def test_preflight_unresolvable_host_noop():
    # 域名解析失败（gaierror）不算 EADDRNOTAVAIL → 静默放行，留给真实 bind 报错
    preflight_bind_addr("no-such-host.invalid", service="t", cfg_key="K")


def test_preflight_own_addr_passes():
    # 本机真实对外地址应放行：UDP connect 取 getsockname 的本机 IP，离线取不到时跳过
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        pytest.skip("离线：取不到本机对外 IP")
    finally:
        s.close()
    if not ip or ip.startswith("127."):
        pytest.skip("取到的是回环地址，无法验证非回环放行")
    preflight_bind_addr(ip, service="t", cfg_key="K")


# ── 4. mode=remote 配置完整性（require_remote_inference_host）─────────────
# config.yaml 的 inference.remote.host 缺省为空（通用模板不钉机器 IP）。remote 模式下
# 地址仍空 = 没告诉本进程去哪拨号 → 拒启；inprocess 模式下空地址无关紧要 → 放行。

def test_require_remote_empty_host_rejected():
    with pytest.raises(SystemExit) as ei:
        require_remote_inference_host({"inference": {"mode": "remote", "remote": {"host": ""}}})
    msg = str(ei.value)
    assert "inprocess" in msg
    assert "算力端地址" in msg


def test_require_remote_missing_remote_key_rejected():
    # remote 键缺失按空处理，不 AttributeError
    with pytest.raises(SystemExit):
        require_remote_inference_host({"inference": {"mode": "remote"}})


def test_require_remote_none_remote_rejected():
    # remote 为 None（YAML 空段）按空处理，不 AttributeError
    with pytest.raises(SystemExit):
        require_remote_inference_host({"inference": {"mode": "remote", "remote": None}})


def test_require_remote_nonempty_host_passes():
    require_remote_inference_host(
        {"inference": {"mode": "remote", "remote": {"host": "100.64.0.9"}}})


def test_require_inprocess_empty_host_passes():
    # inprocess（本地承载）下空地址无关紧要 → 放行
    require_remote_inference_host({"inference": {"mode": "inprocess", "remote": {"host": ""}}})


def test_require_missing_inference_passes():
    # 缺 inference 段 / None：mode 缺省 inprocess → 放行（不 AttributeError）
    require_remote_inference_host({})
    require_remote_inference_host({"inference": None})
