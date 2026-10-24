"""macOS 版的 Developer ID 描述文件（iCloud 钥匙串同步的签名前提）。幂等，可重跑。

为什么要它：Mac 把「iPhone 连接信息」写进可同步的 iCloud 钥匙串，必须走 data protection
钥匙串，而 macOS 上那要求 app 签名带 keychain-access-groups——受限 entitlement，须由
嵌入的描述文件授权（没有描述文件就带它签名 = app 起不来）。

1. 确保 macOS App ID dev.enkin.macagent.mac 已注册（platform MAC_OS）；
2. 没有「同名、MAC_APP_DIRECT、本 App ID、距到期 ≥ RENEW_DAYS 天」的描述文件，就删掉
   同名旧件、用本机钥匙串里有私钥的 Developer ID Application 证书重建一份
   （bundle-macos.sh 对不足 30 天的描述文件当场红，并让人来重跑本脚本）；
3. 下载装到 ~/Library/MobileDevice/Provisioning Profiles/<uuid>.provisionprofile——
   bundle-macos.sh 按名字找它、验到期日后嵌入并带共享钥匙串组签名。

只新增，不改不删 iOS 那两份描述文件（那归 asc-update-signing.py）。
用法：../backend/.venv/bin/python ./asc-macos-profile.py
"""
import base64
import datetime
import json
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from asc_env import asc_credentials

KEY_ID, ISSUER, KEY_PATH = asc_credentials()
API = "https://api.appstoreconnect.apple.com"

MAC_BUNDLE = "dev.enkin.macagent.mac"
MAC_PROFILE = "MacAgent macOS DeveloperID"
RENEW_DAYS = 60
PROFILE_DIR = Path.home() / "Library/MobileDevice/Provisioning Profiles"


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwt() -> str:
    key = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    now = int(time.time())
    header = b64url(json.dumps({"alg": "ES256", "kid": KEY_ID, "typ": "JWT"}).encode())
    claims = b64url(json.dumps({"iss": ISSUER, "iat": now, "exp": now + 900,
                                "aud": "appstoreconnect-v1"}).encode())
    der = key.sign(f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    return f"{header}.{claims}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


client = None  # main() 里才建：import 本模块不该读私钥


def connect():
    global client
    client = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {make_jwt()}"},
                          timeout=30)


def die(msg, resp=None):
    if resp is not None:
        print(resp.status_code, resp.text[:800])
    sys.exit(f"FATAL: {msg}")


def ensure_bundle_id():
    r = client.get("/v1/bundleIds", params={"filter[identifier]": MAC_BUNDLE, "limit": 200})
    if r.status_code != 200:
        die("bundleIds 查询失败", r)
    for item in r.json()["data"]:
        if item["attributes"]["identifier"] == MAC_BUNDLE:
            print(f"bundleId 已存在: {MAC_BUNDLE} → {item['id']}")
            return item["id"]
    r = client.post("/v1/bundleIds", json={"data": {
        "type": "bundleIds",
        "attributes": {"identifier": MAC_BUNDLE, "name": "MacAgent macOS", "platform": "MAC_OS"},
    }})
    if r.status_code not in (200, 201):
        die(f"注册 bundleId 失败 {MAC_BUNDLE}", r)
    print(f"bundleId 已注册: {MAC_BUNDLE} → {r.json()['data']['id']}")
    return r.json()["data"]["id"]


def local_identity_sha1s():
    out = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"],
                         capture_output=True, text=True).stdout
    return {line.split()[1] for line in out.splitlines()
            if "Developer ID Application" in line and len(line.split()) > 1}


def developer_id_cert():
    """账号里、且私钥在本机钥匙串的 Developer ID 证书（否则建出的描述文件本机签不了）。"""
    import hashlib
    r = client.get("/v1/certificates", params={"limit": 200})
    if r.status_code != 200:
        die("certificates 查询失败", r)
    local = local_identity_sha1s()
    certs = [c for c in r.json()["data"]
             if c["attributes"]["certificateType"].startswith("DEVELOPER_ID_APPLICATION")
             and hashlib.sha1(base64.b64decode(c["attributes"]["certificateContent"]))
             .hexdigest().upper() in local]
    if not certs:
        die("没有私钥在本机的 Developer ID Application 证书")
    return max(certs, key=lambda c: c["attributes"]["expirationDate"])["id"]


def usable_profile(bid):
    """同名描述文件里可直接用的那份；不够格的同名旧件删掉（名字在账号内须唯一）。"""
    r = client.get("/v1/profiles", params={
        "filter[name]": MAC_PROFILE, "limit": 200, "include": "bundleId",
        "fields[profiles]": "name,profileType,profileState,uuid,expirationDate,profileContent,bundleId"})
    if r.status_code != 200:
        die("profiles 查询失败", r)
    horizon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=RENEW_DAYS)
    keep = None
    for p in r.json()["data"]:
        a = p["attributes"]
        good = (a["profileState"] == "ACTIVE" and a["profileType"] == "MAC_APP_DIRECT"
                and p["relationships"]["bundleId"]["data"]["id"] == bid
                and datetime.datetime.fromisoformat(a["expirationDate"]) > horizon
                and a.get("profileContent"))
        if good and keep is None:
            keep = p
            continue
        d = client.delete(f"/v1/profiles/{p['id']}")
        print(f"删除不可用的同名描述文件 {p['id']}: HTTP {d.status_code}")
    return keep


def create_profile(bid, cert_id):
    r = client.post("/v1/profiles", json={"data": {
        "type": "profiles",
        "attributes": {"name": MAC_PROFILE, "profileType": "MAC_APP_DIRECT"},
        "relationships": {
            "bundleId": {"data": {"type": "bundleIds", "id": bid}},
            "certificates": {"data": [{"type": "certificates", "id": cert_id}]},
        },
    }})
    if r.status_code not in (200, 201):
        die(f"创建描述文件失败 {MAC_PROFILE}", r)
    print(f"描述文件已建: {MAC_PROFILE}")
    return r.json()["data"]


def install(profile):
    attrs = profile["attributes"]
    content = base64.b64decode(attrs["profileContent"])
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    target = PROFILE_DIR / f"{attrs['uuid']}.provisionprofile"
    target.write_bytes(content)
    # 自校验：真相在描述文件内容里——打出授权的钥匙串组与到期日
    plist = subprocess.run(["security", "cms", "-D", "-i", str(target)],
                           capture_output=True, check=True).stdout
    info = plistlib.loads(plist)
    ents = info.get("Entitlements", {})
    print(f"已安装: {target}")
    print(f"  到期: {info.get('ExpirationDate')}  keychain-access-groups: "
          f"{ents.get('keychain-access-groups')}  application-identifier: "
          f"{ents.get('com.apple.application-identifier')}")


def main():
    connect()
    bid = ensure_bundle_id()
    profile = usable_profile(bid) or create_profile(bid, developer_id_cert())
    install(profile)


if __name__ == "__main__":
    main()
