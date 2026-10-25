"""鉴权校验面的单元契约（HETERO_COMPUTE §7 预留 1）+ /health 实例指纹（#85）。

锁三件事：
1. **单密钥期行为与收敛前逐字相同**——这是"预留不得改变现状"的可执行证明。
2. verify_token 是唯一的比对面：换设备票据/JWT/mTLS 只动它，路由与依赖面不动。
3. /health 自证身份（service/layout/key_fp），且**明文密钥永不出现在响应里**——
   探针据此认身份认钥匙而非认端口（2026-08-10 全天 401 事故的根治面）。
"""
import asyncio
import hashlib
import os

import app as app_module
from app import SERVICE_NAME, health, key_fingerprint
from framework.api.auth import DEFAULT_DEVICE_ID, verify_token


# ---- 单密钥期（auth.devices 空）：与预留前同义 ----

def test_shared_key_match_returns_default_device():
    assert verify_token("s3cret", shared_key="s3cret") == DEFAULT_DEVICE_ID


def test_shared_key_mismatch_returns_none():
    assert verify_token("wrong", shared_key="s3cret") is None


def test_empty_token_never_passes():
    assert verify_token("", shared_key="s3cret") is None


def test_no_key_configured_matches_nothing():
    # 「未配置 = 放行」由 verify_api_key 判定，不是本函数的职责：
    # 本函数在无票据可比时必须一律不通过（否则空 key 会让任意串通过）
    assert verify_token("anything", shared_key="") is None
    assert verify_token("", shared_key="") is None


def test_non_ascii_token_does_not_explode():
    # compare_digest 对含非 ASCII 的 str 会抛 TypeError（→500）；必须 encode 后比对
    assert verify_token("密钥", shared_key="s3cret") is None
    assert verify_token("密钥", shared_key="密钥") == DEFAULT_DEVICE_ID


# ---- 多设备期（auth.devices 非空）：预留面本身 ----

def test_device_token_returns_its_device_id():
    devices = {"mac-studio": "tok-a", "iphone-15": "tok-b"}
    assert verify_token("tok-a", devices=devices) == "mac-studio"
    assert verify_token("tok-b", devices=devices) == "iphone-15"


def test_unknown_device_token_rejected():
    assert verify_token("tok-zzz", devices={"mac-studio": "tok-a"}) is None


def test_devices_and_shared_key_coexist_for_migration():
    devices = {"mac-studio": "tok-a"}
    assert verify_token("tok-a", shared_key="s3cret", devices=devices) == "mac-studio"
    assert verify_token("s3cret", shared_key="s3cret", devices=devices) == DEFAULT_DEVICE_ID


def test_revoking_one_device_does_not_affect_others():
    # 设备票据的存在理由：吊销一台不牵连其余
    devices = {"mac-studio": "tok-a", "iphone-15": "tok-b"}
    del devices["iphone-15"]
    assert verify_token("tok-a", devices=devices) == "mac-studio"
    assert verify_token("tok-b", devices=devices) is None


def test_blank_device_token_never_passes():
    # 配置里某台设备的票据留空，不能变成"空 Bearer 万能钥匙"
    assert verify_token("", devices={"mac-studio": ""}) is None


# ---- /health 实例指纹（#85 完整版 1）----

HEX = set("0123456789abcdef")


def _health_body() -> dict:
    return asyncio.run(health())


def test_health_reports_identity_fields():
    # 四字段齐全：探针要能一次问出「你是谁 / 哪套布局 / 认哪把钥匙」
    body = _health_body()
    # 钉住**整个**键集而非子集：/health 免鉴权，任何新字段漏到这里都是对未认证
    # 调用方的额外披露，必须由一个测试红掉来拍板，而不是顺手跟着实现走。
    assert set(body) == {"status", "service", "layout", "key_fp",
                         "data_ready", "compute_ready", "compute_state"}
    assert body["status"] == "ok"           # 旧消费方只看这个，语义不变
    assert body["service"] == SERVICE_NAME == "macagent-backend"
    assert isinstance(body["layout"], str) and body["layout"]
    assert type(body["data_ready"]) is bool
    assert type(body["compute_ready"]) is bool
    assert isinstance(body["compute_state"], str) and body["compute_state"]


def test_health_before_lifespan_reports_unavailable_capabilities(monkeypatch):
    from fastapi import FastAPI
    monkeypatch.setattr(app_module, "app", FastAPI())
    body = _health_body()
    assert body["status"] == "ok"
    assert body["data_ready"] is False
    assert body["compute_ready"] is False
    assert body["compute_state"] == "starting"


def test_health_key_fp_is_8_hex_or_empty():
    fp = _health_body()["key_fp"]
    assert fp == "" or (len(fp) == 8 and set(fp) <= HEX)


def test_health_never_leaks_plaintext_key(monkeypatch):
    # 红线：响应里只有指纹，明文（及其任意长度的前缀）绝不出现
    secret = "super-secret-agent-key-0123456789"
    monkeypatch.setattr(app_module, "_HEALTH_KEY_FP", key_fingerprint(secret))
    body = _health_body()
    blob = repr(body)
    assert secret not in blob
    assert secret[:8] not in blob
    assert body["key_fp"] == hashlib.sha256(secret.encode()).hexdigest()[:8]


def test_health_layout_echoes_env_declaration(monkeypatch):
    # devctl.sh 的 export AGENT_LAYOUT=repo / app bundle 布局的 "bundle" 原样回显
    monkeypatch.setattr(app_module, "_HEALTH_LAYOUT", "repo")
    assert _health_body()["layout"] == "repo"


def test_health_layout_defaults_to_unknown():
    # 未声明布局（绕过启动器直启）时是 "unknown"，不是空串——UI 要能区分"没报"
    assert app_module._HEALTH_LAYOUT == (os.environ.get("AGENT_LAYOUT") or "unknown")


def test_key_fingerprint_is_sha256_prefix():
    assert key_fingerprint("s3cret") == hashlib.sha256(b"s3cret").hexdigest()[:8]
    assert len(key_fingerprint("s3cret")) == 8


def test_key_fingerprint_empty_key_is_empty_string():
    # 空 = "未启用鉴权"，不是"某把钥匙的指纹"——app 侧据此判 healthy 而非 keyMismatch
    assert key_fingerprint("") == ""


def test_key_fingerprint_distinguishes_keys():
    # 钥匙漂移必须能被看见：这正是 401 事故里缺的那一条信息
    assert key_fingerprint("key-a") != key_fingerprint("key-b")


def test_key_fingerprint_handles_non_ascii():
    # 非 ASCII 密钥不得炸（verify_token 已有同款教训）
    assert key_fingerprint("密钥") == hashlib.sha256("密钥".encode()).hexdigest()[:8]
