"""鉴权校验面的单元契约（HETERO_COMPUTE §7 预留 1）。

锁两件事：
1. **单密钥期行为与收敛前逐字相同**——这是"预留不得改变现状"的可执行证明。
2. verify_token 是唯一的比对面：换设备票据/JWT/mTLS 只动它，路由与依赖面不动。
"""
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
