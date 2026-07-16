"""ASC API 签名资产更新（推送批次）：
1. dev.enkin.macagent：确保 PUSH_NOTIFICATIONS + USERNOTIFICATIONS_TIME_SENSITIVE capability；
2. 注册 dev.enkin.macagent.widgets bundle id（widget 扩展）；
3. 重建 "MacAgent iOS AppStore" 描述文件（capability 变更后旧文件不含 aps-environment）；
4. 新建 "MacAgent Widgets AppStore" 描述文件；
5. 两份都装进本机两个描述文件目录。
鉴权：ASC API key（App Manager）——与 release-ios.sh 同一把。
"""
import base64
import json
import sys
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from asc_env import asc_credentials

# 凭据经 .env.local（标识符）+ ~/.appstoreconnect（私钥），见 asc_env.py
KEY_ID, ISSUER, KEY_PATH = asc_credentials()
API = "https://api.appstoreconnect.apple.com"

APP_BUNDLE = "dev.enkin.macagent"
WIDGET_BUNDLE = "dev.enkin.macagent.widgets"
APP_PROFILE = "MacAgent iOS AppStore"
WIDGET_PROFILE = "MacAgent Widgets AppStore"


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwt() -> str:
    key = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    now = int(time.time())
    header = b64url(json.dumps({"alg": "ES256", "kid": KEY_ID, "typ": "JWT"}).encode())
    claims = b64url(json.dumps({"iss": ISSUER, "iat": now, "exp": now + 900,
                                "aud": "appstoreconnect-v1"}).encode())
    signing = f"{header}.{claims}".encode()
    der = key.sign(signing, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    return f"{header}.{claims}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


client = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {make_jwt()}"},
                      timeout=30)


def die(msg, resp=None):
    if resp is not None:
        print(resp.status_code, resp.text[:800])
    sys.exit(f"FATAL: {msg}")


def find_bundle_id(identifier):
    r = client.get("/v1/bundleIds", params={"filter[identifier]": identifier, "limit": 200})
    if r.status_code != 200:
        die(f"bundleIds 查询失败 {identifier}", r)
    for item in r.json()["data"]:
        if item["attributes"]["identifier"] == identifier:
            return item["id"]
    return None


def ensure_bundle_id(identifier, name):
    bid = find_bundle_id(identifier)
    if bid:
        print(f"bundleId 已存在: {identifier} → {bid}")
        return bid
    r = client.post("/v1/bundleIds", json={"data": {
        "type": "bundleIds",
        "attributes": {"identifier": identifier, "name": name, "platform": "IOS"},
    }})
    if r.status_code not in (200, 201):
        die(f"注册 bundleId 失败 {identifier}", r)
    bid = r.json()["data"]["id"]
    print(f"bundleId 已注册: {identifier} → {bid}")
    return bid


def current_capabilities(bid):
    r = client.get(f"/v1/bundleIds/{bid}/bundleIdCapabilities", params={"limit": 200})
    if r.status_code != 200:
        return set()
    return {c["attributes"]["capabilityType"] for c in r.json()["data"]}


def ensure_capability(bid, cap):
    r = client.post("/v1/bundleIdCapabilities", json={"data": {
        "type": "bundleIdCapabilities",
        "attributes": {"capabilityType": cap},
        "relationships": {"bundleId": {"data": {"type": "bundleIds", "id": bid}}},
    }})
    if r.status_code in (200, 201):
        print(f"capability 已启用: {cap}")
        return True
    print(f"WARN capability {cap} 启用失败: {r.status_code} {r.text[:300]}")
    return False


def distribution_cert_ids():
    ids = []
    for ctype in ("DISTRIBUTION", "IOS_DISTRIBUTION"):
        r = client.get("/v1/certificates",
                       params={"filter[certificateType]": ctype, "limit": 200})
        if r.status_code != 200:
            continue
        for c in r.json()["data"]:
            exp = c["attributes"].get("expirationDate", "")
            ids.append((c["id"], exp, c["attributes"].get("certificateType")))
    if not ids:
        die("找不到分发证书")
    print("分发证书:", ids)
    return [i[0] for i in ids]


def delete_profile_by_name(name):
    r = client.get("/v1/profiles", params={"filter[name]": name, "limit": 200})
    if r.status_code != 200:
        return
    for p in r.json()["data"]:
        if p["attributes"]["name"] == name:
            d = client.delete(f"/v1/profiles/{p['id']}")
            print(f"旧描述文件删除 {name}: {d.status_code}")


PROFILE_DIRS = (Path.home() / "Library/MobileDevice/Provisioning Profiles",
                Path.home() / "Library/Developer/Xcode/UserData/Provisioning Profiles")


def _purge_local_profiles(name):
    """删除本地同名旧描述文件——手动签名按名字取文件，同名多份会取到旧的
    （实测坑：连跑三次堆了三份，最旧的缺 push entitlement）。"""
    import subprocess
    for d in PROFILE_DIRS:
        if not d.is_dir():
            continue
        for f in d.glob("*.mobileprovision"):
            try:
                plist = subprocess.run(
                    ["security", "cms", "-D", "-i", str(f)],
                    capture_output=True, timeout=10).stdout.decode("utf-8", "ignore")
                if f"<string>{name}</string>" in plist:
                    f.unlink()
                    print(f"  本地旧文件已清: {f.name}")
            except Exception:
                pass


def create_profile(name, bid, cert_ids):
    r = client.post("/v1/profiles", json={"data": {
        "type": "profiles",
        "attributes": {"name": name, "profileType": "IOS_APP_STORE"},
        "relationships": {
            "bundleId": {"data": {"type": "bundleIds", "id": bid}},
            "certificates": {"data": [{"type": "certificates", "id": c}
                                      for c in cert_ids]},
        },
    }})
    if r.status_code not in (200, 201):
        die(f"创建描述文件失败 {name}", r)
    attrs = r.json()["data"]["attributes"]
    content = base64.b64decode(attrs["profileContent"])
    uuid = attrs["uuid"]
    _purge_local_profiles(name)
    for d in PROFILE_DIRS:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{uuid}.mobileprovision").write_bytes(content)
    # 自校验：网页端勾选的 capability（如 time-sensitive）API 读不到也写不了，
    # 唯一的真相是描述文件内容——直接打出关键 entitlement 落没落上
    text = content.decode("utf-8", "ignore")
    marks = [k for k in ("aps-environment",
                         "com.apple.developer.usernotifications.time-sensitive")
             if k in text]
    print(f"描述文件已建并安装: {name} uuid={uuid} 内嵌: {marks or '（无推送类 entitlement）'}")
    return uuid


def main():
    app_bid = find_bundle_id(APP_BUNDLE)
    if not app_bid:
        die(f"主 App bundleId 不存在: {APP_BUNDLE}")
    print(f"主 App bundleId: {app_bid}")

    caps = current_capabilities(app_bid)
    print("现有 capabilities:", sorted(caps))
    if "PUSH_NOTIFICATIONS" not in caps:
        if not ensure_capability(app_bid, "PUSH_NOTIFICATIONS"):
            die("PUSH_NOTIFICATIONS 启用失败——推送无从谈起")
    # Time-Sensitive：ASC API 的 capability 枚举不含此类型（写 409、读也列不出来），
    # 只能在 developer.apple.com 网页端的 App ID 配置里勾选。勾没勾生效看下方
    # 描述文件自校验行的"内嵌"清单，不看这里。
    print("提示: Time-Sensitive capability 须在网页端勾选（API 不支持），"
          "生效与否以下方描述文件'内嵌'清单为准")

    widget_bid = ensure_bundle_id(WIDGET_BUNDLE, "MacAgent Widgets")

    certs = distribution_cert_ids()
    delete_profile_by_name(APP_PROFILE)
    create_profile(APP_PROFILE, app_bid, certs)
    delete_profile_by_name(WIDGET_PROFILE)
    create_profile(WIDGET_PROFILE, widget_bid, certs)

    print("\nDONE（若上方'内嵌'清单缺 time-sensitive：去网页端勾选后重跑本脚本；"
          "或从 project.yml entitlements 删掉该行再发布）")


if __name__ == "__main__":
    main()
