"""
HTTP 鉴权（P0）：前后端握手的共享密钥 Bearer 校验。

设计（见 docs/frontend-refactor-decisions.md §2）：
- 弃用 IP 绑定：IP 认证的是"网络位置"而非"客户端"，同网段谁都过、换网即废。
- 单一共享密钥，前端带 `Authorization: Bearer <key>`；比对用 hmac.compare_digest（常量
  时间，防时序侧信道），禁用 `==`。
- 密钥来源优先级：环境变量 AGENT_API_KEY > config.yaml 的 server.api_key。仓库不放真 key。
- 密钥为空视为"未配置"：放行并在启动时告警——避免首启/dev 被卡死；设了 key 即自动强制校验。
- 浏览器侧不持有 key：dev 由 Vite 代理注入 Authorization，生产由 Nginx 注入（前后端分离）。

密钥在 app 启动时解析并挂到 app.state.api_key，本依赖按请求读取，避免每次触碰环境变量。
"""
import hmac
from typing import Dict, Optional

from fastapi import Request, Header, HTTPException, status

# 设备级票据（HETERO_COMPUTE §7 预留 1）：原生 app / iOS 端将**自持凭据**，届时校验要回答的
# 不再是"这个 key 对不对"而是"这是哪台设备"。故把比对收敛到下面这个**单一可替换函数**——
# 换成设备票据/JWT/mTLS 只改它一处，路由与依赖面不动。
# 单密钥期（auth.devices 为空）行为与收敛前逐字相同：命中共享密钥 → 设备名 "default"。
DEFAULT_DEVICE_ID = "default"


def verify_token(token: str, *, shared_key: str = "", devices: Optional[Dict[str, str]] = None) -> Optional[str]:
    """校验票据 → 设备 id；None = 不通过。

    devices（config 的 auth.devices，形如 {device_id: token}）优先于共享密钥：
    多设备期给每台设备发独立票据，吊销一台不牵连其余。两者都为空时调用方不应走到这里
    （未配置 = 放行，由 verify_api_key 判定）。
    全部比对走 hmac.compare_digest（常量时间，防时序侧信道），禁用 `==`；
    encode 后比对——compare_digest 对含非 ASCII 的 str 会抛 TypeError（→500），bytes 恒安全。
    """
    raw = token.encode("utf-8")
    for device_id, device_token in (devices or {}).items():
        if device_token and hmac.compare_digest(raw, str(device_token).encode("utf-8")):
            return str(device_id)
    if shared_key and hmac.compare_digest(raw, shared_key.encode("utf-8")):
        return DEFAULT_DEVICE_ID
    return None


async def verify_api_key(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> None:
    """FastAPI 依赖：校验 Bearer 票据。挂到 /api/v1/* 全量路由（见 app.py include_router）。

    通过后把设备 id 记在 request.state.device_id——单密钥期恒为 "default"，
    多设备期即调用方身份（审批推送按设备路由等将消费它）。
    """
    expected: str = getattr(request.app.state, "api_key", "") or ""
    devices: Dict[str, str] = getattr(request.app.state, "auth_devices", None) or {}
    if not expected and not devices:
        return  # 未配置任何票据 → 放行（启动时已打印告警）

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少或格式错误的 Authorization 头（应为 'Bearer <key>'）。",
        )
    device_id = verify_token(authorization[len("Bearer "):], shared_key=expected, devices=devices)
    if device_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API 密钥无效。",
        )
    request.state.device_id = device_id
