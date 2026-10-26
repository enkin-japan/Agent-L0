"""iPhone 连接本机后端：每部 iPhone 都要经 Mac 同意，拿到它自己的设备票据
（设计见 docs/plans/2026-10-25-device-tokens-design.md；票据存储见 devices.py）。

两条入口：
- **同一 Apple ID**：Mac 把「请求凭据」（ticket）连同地址写进 iCloud 钥匙串。iPhone 凭它
  POST /request 提交连接请求，Mac 弹框显示 4 位核对码、由你点允许；iPhone 凭 poll_secret
  轮询 /request/{id}/poll，同意后只领一次票据。ticket 只能用来提交请求，不能调别的接口——
  它的用处是证明「读得到这台 Mac 的 iCloud 条目」，挡住 tailnet 上别的设备来骚扰 Mac。
- **不同 Apple ID**：Mac 显示二维码（一次性配对码），你亲手打开二维码本身就是同意；
  iPhone 扫码后 POST /claim 直接领票据。

守的是什么（/claim、/request、/request/{id}/poll 免 Bearer，是本文件的入口面）：
- 配对码 144 bit、5 分钟、单次；错码不作废（防乱猜把码烧光）；
- 请求：ticket 用 compare_digest 比；同时最多 3 条待处理、每分钟最多 3 次提交；未经同意
  的请求查询结果永不含票据；被拒 / 过期即作废；同意后票据只发一次；poll_secret 只存哈希；
- 管理接口（start、requests、devices、ticket、settings、upgrade）只给共享密钥（require_owner）；
- 一切响应 Cache-Control: no-store；日志不出现任何票据、ticket、poll_secret。
状态（配对码、待处理请求）只在进程内存：后端重启 = 全部作废，正是想要的。单 worker 适用。
"""
import hashlib
import hmac
import os
import secrets
import time
from collections import deque
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from framework.api.auth import require_owner, verify_api_key
from framework.api.devices import clean_name, store_for
from framework.utils.instance_id import instance_id

TTL_SECONDS = 300
CODE_BYTES = 18          # token_urlsafe(18) → 24 字符，144 bit
TICKET_BYTES = 32
REQUEST_TTL_SECONDS = 300
MAX_PENDING = 3
MAX_REQUESTS_PER_MINUTE = 3

router = APIRouter(prefix="/api/v1/pair")
_OWNER = [Depends(verify_api_key), Depends(require_owner)]
_REJECT = "配对码无效或已过期，请在 Mac 上重新生成二维码。"
_BAD_TICKET = "连接凭据无效，请确认 Mac 已开启 iCloud 同步并稍后重试。"
_BAD_REQUEST = "连接请求不存在或已失效。"


class ClaimBody(BaseModel):
    code: str = Field(max_length=64)
    name: str = Field("", max_length=64)
    platform: str = Field("ios", max_length=16)
    push_device_id: Optional[str] = Field(None, max_length=64)


class RequestBody(BaseModel):
    ticket: str = Field(max_length=128)
    name: str = Field("", max_length=64)
    platform: str = Field("ios", max_length=16)
    push_device_id: Optional[str] = Field(None, max_length=64)


class PollBody(BaseModel):
    poll_secret: str = Field(max_length=128)


class UpgradeBody(BaseModel):
    name: str = Field("", max_length=64)
    platform: str = Field("ios", max_length=16)
    push_device_id: Optional[str] = Field(None, max_length=64)


class DevicePatch(BaseModel):
    name: Optional[str] = Field(None, max_length=64)
    permanent: Optional[bool] = None
    ttl_days: Optional[int] = Field(None, ge=1, le=3650)


class SettingsBody(BaseModel):
    default_ttl_days: Optional[int] = Field(None, ge=1, le=3650)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _state(request: Request) -> dict:
    state = getattr(request.app.state, "pairing", None)
    if state is None:
        state = request.app.state.pairing = {}
    return state


def _requests(request: Request) -> dict:
    pending = getattr(request.app.state, "pair_requests", None)
    if pending is None:
        pending = request.app.state.pair_requests = {}
    if getattr(request.app.state, "pair_request_times", None) is None:
        request.app.state.pair_request_times = deque()
    now = time.monotonic()
    for rid in [k for k, v in pending.items() if now >= v["expires"]]:
        pending.pop(rid)
    return pending


def _fresh_code(waiting) -> str:
    """待处理请求之间核对码不重复：否则两条请求显示同一个码，核对就失去意义。"""
    taken = {v["code"] for v in waiting}
    while True:
        code = f"{secrets.randbelow(10000):04d}"
        if code not in taken:
            return code


def _default_name(platform: str) -> str:
    return ("iPad" if platform == "ipados" else "iPhone") + time.strftime("（%m-%d 连接）")


def _issued(token: str, record: dict) -> dict:
    # api_key 同值：build 75 的 iPhone 只认这个字段名
    out = {"token": token, "api_key": token, "device_id": record["id"]}
    iid = instance_id()
    if iid:
        out["instance_id"] = iid      # iPhone 的多 Mac 列表按它认出同一台 Mac
    return out


# ---------- 请求凭据（ticket）：明文存数据根 auth/pair_ticket（0600），Mac 读去写进 iCloud ----------

def _ticket_path():
    from framework.utils.config_loader import data_dir
    return data_dir() / "auth" / "pair_ticket"


def _write_ticket(value: str) -> None:
    path = _ticket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".pair_ticket.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _ticket(create: bool) -> str:
    """现有的 ticket；没有（或长度不对）时 create=True 才新生成，否则返回空串（请求一律拒）。"""
    try:
        value = _ticket_path().read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if len(value) >= 32:
        return value
    if not create:
        return ""
    value = secrets.token_urlsafe(TICKET_BYTES)
    _write_ticket(value)
    return value


# ---------- 扫码（不同 Apple ID） ----------

def _require_shared_key(request: Request) -> None:
    # 没有共享密钥（未启用鉴权的 dev 后端）时不发设备票据：一旦有了票据，鉴权就会对所有
    # 请求生效，而 Mac 自己手里没有任何密钥——发了就把 Mac 锁在门外。
    if not (getattr(request.app.state, "api_key", "") or ""):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "后端未启用共享密钥鉴权，无法连接 iPhone。")


@router.post("/start", dependencies=_OWNER)
async def start_pairing(request: Request, response: Response):
    _require_shared_key(request)
    state = _state(request)
    state.clear()
    state.update(code=secrets.token_urlsafe(CODE_BYTES),
                 expires=time.monotonic() + TTL_SECONDS)
    response.headers["Cache-Control"] = "no-store"
    return {"code": state["code"], "expires_in": TTL_SECONDS}


@router.post("/claim")
async def claim_pairing(body: ClaimBody, request: Request, response: Response):
    state = _state(request)
    code = state.get("code")
    if not code or not getattr(request.app.state, "api_key", "") or time.monotonic() >= state.get("expires", 0):
        state.clear()
        raise HTTPException(status.HTTP_403_FORBIDDEN, _REJECT)
    if not hmac.compare_digest(body.code.encode("utf-8"), code.encode("utf-8")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, _REJECT)
    state.clear()
    token, record, superseded = store_for(request.app).issue(
        name=clean_name(body.name, _default_name(body.platform)), via="qr",
        platform=body.platform, push_device_id=body.push_device_id)
    print(f"[Pair] 扫码连接：已为设备 {record['id']} 发放票据")
    _adopt_push(request, record, superseded)
    response.headers["Cache-Control"] = "no-store"
    return _issued(token, record)


# ---------- 握手（同一 Apple ID） ----------

@router.post("/request")
async def submit_request(body: RequestBody, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    ticket = _ticket(create=False) if getattr(request.app.state, "api_key", "") else ""
    if not ticket or not hmac.compare_digest(body.ticket.encode("utf-8"), ticket.encode("utf-8")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, _BAD_TICKET)
    pending = _requests(request)
    times = request.app.state.pair_request_times
    now = time.monotonic()
    while times and now - times[0] >= 60:
        times.popleft()
    waiting = [v for v in pending.values() if v["status"] == "pending"]
    if len(times) >= MAX_REQUESTS_PER_MINUTE or len(waiting) >= MAX_PENDING:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "连接请求过多，请稍后再试。")
    times.append(now)
    rid = secrets.token_urlsafe(12)
    poll_secret = secrets.token_urlsafe(32)
    pending[rid] = {"id": rid, "poll_sha256": _sha(poll_secret), "status": "pending",
                    "code": _fresh_code(waiting),
                    "name": clean_name(body.name, _default_name(body.platform)),
                    "platform": body.platform, "push_device_id": body.push_device_id,
                    "created": time.time(), "expires": now + REQUEST_TTL_SECONDS}
    print(f"[Pair] 收到连接请求 {rid}，等待 Mac 同意")
    return {"request_id": rid, "poll_secret": poll_secret, "code": pending[rid]["code"],
            "expires_in": REQUEST_TTL_SECONDS}


@router.post("/request/{request_id}/poll")
async def poll_request(request_id: str, body: PollBody, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    pending = _requests(request)
    item = pending.get(request_id)
    if item is None or not hmac.compare_digest(_sha(body.poll_secret), item["poll_sha256"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, _BAD_REQUEST)
    if item["status"] == "pending":
        return {"status": "pending"}
    pending.pop(request_id)          # 被拒或已同意：这条请求到此作废
    if item["status"] != "approved":
        return {"status": "denied"}
    token, record, superseded = store_for(request.app).issue(
        name=item["name"], via="icloud", platform=item["platform"],
        push_device_id=item["push_device_id"])
    print(f"[Pair] Mac 已同意请求 {request_id}：为设备 {record['id']} 发放票据")
    _adopt_push(request, record, superseded)
    return {"status": "approved", **_issued(token, record)}


@router.get("/requests", dependencies=_OWNER)
async def list_requests(request: Request):
    pending = _requests(request)
    return {"requests": [{k: v[k] for k in ("id", "code", "name", "platform", "created")}
                         for v in pending.values() if v["status"] == "pending"]}


def _decide(request: Request, request_id: str, decision: str) -> dict:
    item = _requests(request).get(request_id)
    if item is None or item["status"] != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, _BAD_REQUEST)
    item["status"] = decision
    return {"status": decision}


@router.post("/requests/{request_id}/approve", dependencies=_OWNER)
async def approve_request(request_id: str, request: Request):
    return _decide(request, request_id, "approved")


@router.post("/requests/{request_id}/deny", dependencies=_OWNER)
async def deny_request(request_id: str, request: Request):
    return _decide(request, request_id, "denied")


@router.get("/ticket", dependencies=_OWNER)
async def get_ticket(request: Request, response: Response):
    _require_shared_key(request)
    response.headers["Cache-Control"] = "no-store"
    return {"ticket": _ticket(create=True)}


@router.post("/ticket/rotate", dependencies=_OWNER)
async def rotate_ticket(request: Request, response: Response):
    _require_shared_key(request)
    response.headers["Cache-Control"] = "no-store"
    value = secrets.token_urlsafe(TICKET_BYTES)
    _write_ticket(value)
    _requests(request).clear()       # 旧凭据提交的待处理请求一并作废
    request.app.state.pair_request_times.clear()
    return {"ticket": value}


# ---------- 旧连接升级：手里还是共享密钥的 iPhone 换一张自己的票据 ----------

@router.post("/upgrade", dependencies=_OWNER)
async def upgrade(body: UpgradeBody, request: Request, response: Response):
    _require_shared_key(request)
    token, record, superseded = store_for(request.app).issue(
        name=clean_name(body.name, _default_name(body.platform)), via="upgrade",
        platform=body.platform, push_device_id=body.push_device_id)
    print(f"[Pair] 旧连接升级：为设备 {record['id']} 发放票据")
    _adopt_push(request, record, superseded)
    response.headers["Cache-Control"] = "no-store"
    return _issued(token, record)


# ---------- 设备列表（Mac 设置页） ----------

_PUBLIC = ("id", "name", "via", "platform", "created", "last_used", "expires")


@router.get("/devices", dependencies=_OWNER)
async def list_devices(request: Request):
    store = store_for(request.app)
    return {"devices": store.public(), "default_ttl_days": store.default_ttl_days,
            "notice": store.notice}


@router.patch("/devices/{device_id}", dependencies=_OWNER)
async def patch_device(device_id: str, body: DevicePatch, request: Request):
    store = store_for(request.app)
    if body.permanent:
        ttl = None
    elif body.ttl_days is not None or body.permanent is False:
        ttl = body.ttl_days or store.default_ttl_days or 90
    else:
        ttl = ...
    record = store.update(device_id, name=body.name, ttl_days=ttl)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在。")
    return {k: record.get(k) for k in _PUBLIC}


@router.delete("/devices/{device_id}", dependencies=_OWNER)
async def revoke_device(device_id: str, request: Request):
    record = store_for(request.app).revoke(device_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在。")
    _drop_push(request, device_id, record)
    print(f"[Pair] 已吊销设备 {device_id}")
    return {"status": True, "revoked": True}


@router.delete("/self", dependencies=[Depends(verify_api_key)])
async def forget_self(request: Request):
    """iPhone 在自己的列表里删掉这台 Mac 时调：吊销它自己的票据，Mac 的设备列表随之少一行。"""
    device_id = getattr(request.state, "device_id", "default")
    if device_id == "default":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "共享密钥不是设备票据。")
    record = store_for(request.app).revoke(device_id)
    if record is not None:
        _drop_push(request, device_id, record)
        print(f"[Pair] 设备 {device_id} 已自行断开")
    return {"status": True}


def _adopt_push(request: Request, record: dict, superseded: list) -> None:
    """同一部手机重新配对后，它自报推送 id 的条目改归新票据。

    只接管无主、或原主票据已不算数（被取代 = 记录已删、到期、吊销）的条目；仍有效的
    另一张票据登记的条目不动——自报的推送 id 不能拿来顶替别的手机。票据此时已经签发：
    这里任何失败只记一行，绝不能让手机拿不到票据（推送条目下次登记时还会再接管）。"""
    try:
        for old in superseded:
            print(f"[Pair] 设备 {record['id']} 取代了同机旧记录 {old}")
        push_id = record.get("push_device_id")
        registry = getattr(request.app.state, "push_registry", None)
        if not push_id or registry is None:
            return
        entry = registry.devices.get(push_id)
        if entry is None:
            return
        owner = entry.get("auth_device")
        if owner is None or not store_for(request.app).active(owner):
            if registry.rebind(push_id, record["id"]):
                print(f"[Pair] 推送条目已改绑到设备 {record['id']}")
    except Exception as exc:
        print(f"[Pair] 推送条目改绑失败（票据照发，下次登记推送时再接管）：{type(exc).__name__}")


def _drop_push(request: Request, device_id: str, record: dict) -> None:
    registry = getattr(request.app.state, "push_registry", None)
    if registry is None:
        return
    # 吊销即停推：它登记的条目（按票据归属）删掉；它自报的推送 id 只在条目无主或归它时
    # 才删——同机重新配对后条目已改绑到新票据，删旧票据不能把新票据的推送一起带走
    dropped = list(registry.remove_bound(device_id))
    push_id = record.get("push_device_id")
    if push_id:
        entry = registry.devices.get(push_id)
        if entry is not None and entry.get("auth_device") in (None, device_id):
            registry.remove(push_id)
            dropped.append(push_id)
    forget = getattr(getattr(request.app.state, "push_dispatcher", None), "forget_push_device", None)
    if forget is not None:
        for pid in dropped:
            forget(pid)


@router.put("/settings", dependencies=_OWNER)
async def put_settings(body: SettingsBody, request: Request):
    # 必须显式给出（null = 永久）：空 body 不能悄悄把所有新设备变成永久
    if "default_ttl_days" not in body.model_fields_set:
        raise HTTPException(422, "缺少 default_ttl_days。")
    store = store_for(request.app)
    store.set_default_ttl(body.default_ttl_days)
    return {"default_ttl_days": store.default_ttl_days}
