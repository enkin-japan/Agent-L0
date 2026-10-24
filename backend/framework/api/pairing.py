"""iPhone 扫码配对：一次性配对码换 AGENT 密钥（iCloud 钥匙串同步之外的备用出口）。

流程：Mac app（持密钥，经回环）POST /api/v1/pair/start 领一枚配对码，连同自己的
tailnet 地址画成二维码；iPhone 用相机扫码后经 tailnet POST /api/v1/pair/claim 交出
配对码，换回密钥存进自己的钥匙串。长期有效的密钥从不出现在屏幕或二维码里。

守的是什么（claim 免鉴权，是本文件唯一的入口面）：
- 配对码随机 144 bit、5 分钟过期、**用一次即作废**；同一时刻只有一枚，重新 start 顶掉旧的；
- 比对走 hmac.compare_digest。错码**不**作废配对码：144 bit 猜不中，而「错 N 次即作废」
  会让任何一台 tailnet 节点不停乱猜、在用户扫码前把每一枚码都烧掉（备用出口被永久堵死）；
- 没有共享密钥可发（未启用鉴权，或只配了设备票据）时 start 直接拒绝，claim 永不返回空密钥；
- 响应 Cache-Control: no-store；失败一律同一句话，不区分「过期 / 错码 / 没有码」。
状态只在进程内存里（app.state.pairing）：后端重启 = 所有配对码作废，正是想要的。
也因此只适用单 worker（现行 uvicorn 即单进程）；多 worker 会把码与认领分到不同进程。
"""
import hmac
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from framework.api.auth import verify_api_key

TTL_SECONDS = 300
CODE_BYTES = 18  # token_urlsafe(18) → 24 字符，144 bit

router = APIRouter(prefix="/api/v1/pair")
_REJECT = "配对码无效或已过期，请在 Mac 上重新生成二维码。"


class ClaimBody(BaseModel):
    code: str = Field(max_length=64)


def _state(request: Request) -> dict:
    state = getattr(request.app.state, "pairing", None)
    if state is None:
        state = request.app.state.pairing = {}
    return state


@router.post("/start", dependencies=[Depends(verify_api_key)])
async def start_pairing(request: Request, response: Response):
    if not (getattr(request.app.state, "api_key", "") or ""):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "后端没有共享密钥可发（未启用鉴权，或只配置了设备票据），无法配对。")
    state = _state(request)
    state.clear()
    state.update(code=secrets.token_urlsafe(CODE_BYTES),
                 expires=time.monotonic() + TTL_SECONDS)
    response.headers["Cache-Control"] = "no-store"
    return {"code": state["code"], "expires_in": TTL_SECONDS}


@router.post("/claim")
async def claim_pairing(body: ClaimBody, request: Request, response: Response):
    state = _state(request)
    key = getattr(request.app.state, "api_key", "") or ""
    code = state.get("code")
    if not code or not key or time.monotonic() >= state.get("expires", 0):
        state.clear()
        raise HTTPException(status.HTTP_403_FORBIDDEN, _REJECT)
    if not hmac.compare_digest(body.code.encode("utf-8"), code.encode("utf-8")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, _REJECT)
    state.clear()
    response.headers["Cache-Control"] = "no-store"
    return {"api_key": key}
