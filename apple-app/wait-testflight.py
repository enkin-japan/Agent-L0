"""等指定构建号在 App Store Connect 处理完成（release-ios.sh 之后跑）。
用法: python3 wait-testflight.py <build_no>
依赖: pip install pyjwt cryptography requests（任意 venv）。
注意必须按构建号过滤——按最新排序会在新构建未入列时误判旧构建为完成。"""
import sys, time
import jwt, requests

from asc_env import asc_credentials

BUILD = sys.argv[1]
# 凭据经 .env.local（标识符）+ ~/.appstoreconnect（私钥），见 asc_env.py
KEY_ID, ISSUER, _P8_PATH = asc_credentials()
P8 = _P8_PATH.read_text()

def headers():
    token = jwt.encode({"iss": ISSUER, "iat": int(time.time()), "exp": int(time.time()) + 900,
                        "aud": "appstoreconnect-v1"}, P8, algorithm="ES256", headers={"kid": KEY_ID})
    return {"Authorization": f"Bearer {token}"}

for attempt in range(45):
    r = requests.get("https://api.appstoreconnect.apple.com/v1/builds", headers=headers(),
                     params={"filter[app]": "6789637764", "filter[version]": BUILD,
                             "fields[builds]": "version,processingState"}, timeout=30)
    data = r.json().get("data", [])
    state = data[0]["attributes"]["processingState"] if data else "（未出现）"
    print(f"[{attempt}] build {BUILD}: {state}", flush=True)
    if state == "VALID":
        print("== READY：TestFlight 可见（内部组自动分发）==")
        raise SystemExit(0)
    if state in ("FAILED", "INVALID"):
        raise SystemExit(f"处理失败: {state}")
    time.sleep(40)
raise SystemExit("超时")
