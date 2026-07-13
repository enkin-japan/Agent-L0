"""APNs HTTP/2 客户端：.p8 token-based auth（ES256 JWT）直发，无第三方中转。

为什么自实现而不用 aioapns：只需要"签 JWT + POST 一个 HTTP/2 端点"，
httpx(h2) + cryptography 十几行可覆盖，少一个供应链依赖。

环境自愈（TestFlight=生产 APNs、Xcode 直装=沙箱的错配是必踩坑）：
设备默认 env="auto"——先打生产，收到 BadDeviceToken 自动改打沙箱，
成功后由调用方（dispatcher）把学到的环境粘滞回注册表，此后不再双发。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

_PROD_HOST = "https://api.push.apple.com"
_SANDBOX_HOST = "https://api.sandbox.push.apple.com"
# Apple 要求 JWT 20~60 分钟内轮换；45 分钟刷新留足余量
_JWT_TTL = 45 * 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class APNsDisabled(RuntimeError):
    """推送未配置（缺 .p8 key）——调用方据此静默降级，不视为故障。"""


class APNsClient:
    """单例持有：ES256 签名器 + 双环境 HTTP/2 连接池。全部方法须在事件循环线程调用。"""

    def __init__(self, key_path: str, key_id: str, team_id: str):
        p = Path(key_path).expanduser()
        if not (key_path and key_id and team_id and p.exists()):
            raise APNsDisabled(
                f"APNs 未配置（key_path={key_path!r} key_id={key_id!r} team_id={team_id!r}）")
        from cryptography.hazmat.primitives import serialization
        self._key = serialization.load_pem_private_key(p.read_bytes(), password=None)
        self.key_id = key_id
        self.team_id = team_id
        self._jwt_cache: str = ""
        self._jwt_issued_at: float = 0.0
        self._clients: Dict[str, Any] = {}   # host -> httpx.AsyncClient（惰性建，连接复用）
        self._lock = asyncio.Lock()

    # ---------- JWT ----------
    def _jwt(self) -> str:
        now = time.time()
        if self._jwt_cache and now - self._jwt_issued_at < _JWT_TTL:
            return self._jwt_cache
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        header = _b64url(json.dumps({"alg": "ES256", "kid": self.key_id}).encode())
        claims = _b64url(json.dumps({"iss": self.team_id, "iat": int(now)}).encode())
        signing_input = f"{header}.{claims}".encode()
        der_sig = self._key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        # JWT 的 ES256 签名是 r‖s 各 32 字节的裸拼接，cryptography 给的是 DER，须转换
        r, s = utils.decode_dss_signature(der_sig)
        raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        self._jwt_cache = f"{header}.{claims}.{_b64url(raw_sig)}"
        self._jwt_issued_at = now
        return self._jwt_cache

    def _client_for(self, host: str):
        import httpx
        c = self._clients.get(host)
        if c is None:
            c = httpx.AsyncClient(http2=True, base_url=host, timeout=10.0)
            self._clients[host] = c
        return c

    # ---------- 发送 ----------
    async def send(self, token: str, payload: Dict[str, Any], *,
                   topic: str, push_type: str = "alert", priority: int = 10,
                   expiration: int = 0, collapse_id: Optional[str] = None,
                   environment: str = "prod") -> Dict[str, Any]:
        """发一条推送。返回 {ok, status, reason, environment}——不抛网络外的业务异常。

        environment: "prod" | "sandbox" | "auto"。auto = 先生产，BadDeviceToken 换沙箱
        重试一次；返回值的 environment 是**实际成功/最终尝试**的环境，供调用方粘滞。
        """
        envs = [environment] if environment in ("prod", "sandbox") else ["prod", "sandbox"]
        last: Dict[str, Any] = {"ok": False, "status": 0, "reason": "unsent", "environment": envs[0]}
        for env in envs:
            last = await self._send_once(token, payload, topic=topic, push_type=push_type,
                                         priority=priority, expiration=expiration,
                                         collapse_id=collapse_id, env=env)
            # 只有环境错配才值得换环境重试；其余失败换了也一样
            if last["ok"] or last["reason"] != "BadDeviceToken":
                return last
        return last

    async def _send_once(self, token: str, payload: Dict[str, Any], *, topic: str,
                         push_type: str, priority: int, expiration: int,
                         collapse_id: Optional[str], env: str) -> Dict[str, Any]:
        host = _PROD_HOST if env == "prod" else _SANDBOX_HOST
        headers = {
            "authorization": f"bearer {self._jwt()}",
            "apns-topic": topic,
            "apns-push-type": push_type,
            "apns-priority": str(priority),
            "apns-expiration": str(expiration),
        }
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id[:64]
        try:
            async with self._lock:  # httpx AsyncClient 并发安全，但 JWT 刷新竞态从简串行
                resp = await self._client_for(host).post(
                    f"/3/device/{token}", content=json.dumps(payload), headers=headers)
        except Exception as e:
            return {"ok": False, "status": 0, "reason": f"network:{e}", "environment": env}
        if resp.status_code == 200:
            return {"ok": True, "status": 200, "reason": "", "environment": env}
        try:
            reason = resp.json().get("reason", "")
        except Exception:
            reason = resp.text[:120]
        # 403 ExpiredProviderToken：强制刷新 JWT 重试一次（时钟漂移/缓存过界）
        if resp.status_code == 403 and reason == "ExpiredProviderToken":
            self._jwt_cache = ""
            return await self._send_once(token, payload, topic=topic, push_type=push_type,
                                         priority=priority, expiration=expiration,
                                         collapse_id=collapse_id, env=env)
        return {"ok": False, "status": resp.status_code, "reason": reason, "environment": env}

    async def aclose(self):
        for c in self._clients.values():
            try:
                await c.aclose()
            except Exception:
                pass
        self._clients.clear()
