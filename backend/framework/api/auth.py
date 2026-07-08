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
from typing import Optional

from fastapi import Request, Header, HTTPException, status


async def verify_api_key(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> None:
    """FastAPI 依赖：校验 Bearer 密钥。挂到 /api/v1/* 全量路由（见 app.py include_router）。"""
    expected: str = getattr(request.app.state, "api_key", "") or ""
    if not expected:
        return  # 未配置密钥 → 放行（启动时已打印告警）

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少或格式错误的 Authorization 头（应为 'Bearer <key>'）。",
        )
    provided = authorization[len("Bearer "):]
    # encode 后比对：compare_digest 对含非 ASCII 的 str 会抛 TypeError（→500），bytes 恒安全
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API 密钥无效。",
        )
