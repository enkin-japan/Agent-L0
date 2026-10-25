"""设备票据：每部 iPhone 经 Mac 同意后各持一张，可单独改名、改有效期、吊销
（设计见 docs/plans/2026-10-25-device-tokens-design.md）。

守的是什么：
- 文件里只存 sha256(票据)，明文只在发放那一次响应里出现；日志只记 device_id；
- 文件损坏 / 结构不对 / 字段类型不对 → **fail-closed**：设备票据一律不认（共享密钥不受
  影响，Mac 照常可用）；坏文件改名留存，从空表重来，并给出 `notice` 供 Mac 设置页提示；
- 吊销即删记录，下一次请求就 401；到期返回 `expired`，由调用方报 device_expired；
- 写入：临时文件 + fsync + os.replace，目录 0700、文件 0600；进程内一把锁串行化。
票据 256 bit 随机，按哈希查表即可（无需慢哈希；高熵票据的查表时序泄露不出可用信息）。
"""
import hashlib
import json
import os
import secrets
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TOKEN_BYTES = 32
DEFAULT_TTL_DAYS = 90
MAX_NAME = 64
_TOUCH_PERSIST_SECONDS = 600     # last_used 先记内存，最多 10 分钟落一次盘
_DAY = 86400
_RECORD_FIELDS = {"id": str, "token_sha256": str, "name": str, "via": str, "platform": str,
                  "created": (int, float), "last_used": (int, float)}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_name(name: Any, fallback: str) -> str:
    # 去掉控制符与格式符（含 RTL 覆盖等双向控制）：名字会排在设备列表的「移除」按钮旁，
    # 不能让一行把自己显示成另一台设备的名字
    text = "".join(c for c in str(name or "")
                   if c.isspace() or unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp"))
    text = " ".join(text.split())[:MAX_NAME]
    return text or fallback


def _valid_record(r: Any) -> bool:
    if not isinstance(r, dict):
        return False
    for key, kind in _RECORD_FIELDS.items():
        if not isinstance(r.get(key), kind) or isinstance(r.get(key), bool):
            return False
    if len(r["token_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in r["token_sha256"]):
        return False
    if "expires" not in r:          # 缺字段 ≠ 永久：结构不对即 fail-closed
        return False
    expires = r.get("expires")
    if expires is not None and (isinstance(expires, bool) or not isinstance(expires, (int, float))):
        return False
    push = r.get("push_device_id")
    return push is None or isinstance(push, str)


class DeviceStore:
    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.Lock()
        self.devices: Dict[str, Dict[str, Any]] = {}
        self.by_hash: Dict[str, str] = {}
        self.default_ttl_days: Optional[int] = DEFAULT_TTL_DAYS
        self.notice: Optional[str] = None      # 文件损坏时给 Mac 设置页看的一句话
        self._dirty_since: Optional[float] = None
        self._load()

    # ---------- 落盘 ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("devices"), list):
                raise ValueError("unexpected structure")
            ttl = data.get("default_ttl_days", DEFAULT_TTL_DAYS)
            if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0):
                raise ValueError("bad default_ttl_days")
            records = data["devices"]
            if not all(_valid_record(r) for r in records):
                raise ValueError("bad device record")
            ids = [r["id"] for r in records]
            hashes = [r["token_sha256"] for r in records]
            if len(set(ids)) != len(ids) or len(set(hashes)) != len(hashes):
                raise ValueError("duplicate device")
        except Exception as exc:
            # fail-closed：一张设备票据都不认；坏文件留存以便排查，不覆盖
            stamp = time.strftime("%Y%m%d-%H%M%S")
            aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            try:
                os.replace(self.path, aside)
            except OSError:
                pass
            self.notice = "已连接设备记录损坏，需要重新连接 iPhone。"
            print(f"[Auth] 设备票据记录无法读取（{type(exc).__name__}），已改名留存为 {aside.name}；"
                  "所有设备票据暂不可用，共享密钥不受影响")
            return
        self.default_ttl_days = ttl
        self.devices = {r["id"]: dict(r) for r in records}
        self.by_hash = {r["token_sha256"]: r["id"] for r in records}

    def _persist(self) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        payload = json.dumps({"v": 1, "default_ttl_days": self.default_ttl_days,
                              "devices": list(self.devices.values())},
                             ensure_ascii=False, indent=1).encode("utf-8")
        tmp = directory / f".{self.path.name}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._dirty_since = None

    # ---------- 校验 ----------
    def verify(self, token: str) -> Tuple[str, Optional[str]]:
        """('ok', id) / ('expired', id) / ('unknown', None)。命中即记 last_used。"""
        with self.lock:
            device_id = self.by_hash.get(_digest(token))
            if device_id is None:
                return "unknown", None
            record = self.devices[device_id]
            now = self.clock()
            expires = record.get("expires")
            if expires is not None and now >= expires:
                return "expired", device_id
            record["last_used"] = now
            if self._dirty_since is None:
                self._dirty_since = now
            elif now - self._dirty_since >= _TOUCH_PERSIST_SECONDS:
                try:
                    self._persist()
                except OSError as exc:
                    print(f"[Auth] last_used 落盘失败（内存态仍有效）：{type(exc).__name__}")
            return "ok", device_id

    def __len__(self) -> int:
        return len(self.devices)

    # ---------- 管理（只给 Mac：require_owner） ----------
    def issue(self, *, name: str, via: str, platform: str = "ios",
              push_device_id: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with self.lock:
            now = self.clock()
            device_id = "d_" + secrets.token_urlsafe(12)
            record = {"id": device_id, "token_sha256": _digest(token),
                      "name": clean_name(name, "iPhone"), "via": via,
                      "platform": platform if platform in ("ios", "ipados", "macos") else "ios",
                      "created": now, "last_used": now,
                      "expires": None if self.default_ttl_days is None else now + self.default_ttl_days * _DAY,
                      "push_device_id": push_device_id if isinstance(push_device_id, str)
                      and 0 < len(push_device_id) <= 64 else None}
            self.devices[device_id] = record
            self.by_hash[record["token_sha256"]] = device_id
            try:
                self._persist()
            except BaseException:
                self.devices.pop(device_id, None)
                self.by_hash.pop(record["token_sha256"], None)
                raise
            self.notice = None           # 损坏后已重新连上设备：提示不再适用
        return token, dict(record)

    def public(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [{k: r.get(k) for k in ("id", "name", "via", "platform", "created", "last_used", "expires")}
                    for r in sorted(self.devices.values(), key=lambda r: r["created"])]

    def update(self, device_id: str, *, name: Optional[str] = None,
               ttl_days: Any = ...) -> Optional[Dict[str, Any]]:
        """ttl_days：省略 = 不动；None = 永久；正整数 = 从现在起算的天数。"""
        with self.lock:
            record = self.devices.get(device_id)
            if record is None:
                return None
            before = dict(record)
            if name is not None:
                record["name"] = clean_name(name, record["name"])
            if ttl_days is not ...:
                record["expires"] = None if ttl_days is None else self.clock() + int(ttl_days) * _DAY
            try:
                self._persist()
            except BaseException:
                self.devices[device_id] = before
                raise
            return dict(record)

    def revoke(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            record = self.devices.pop(device_id, None)
            if record is None:
                return None
            self.by_hash.pop(record["token_sha256"], None)
            try:
                self._persist()
            except BaseException:
                self.devices[device_id] = record
                self.by_hash[record["token_sha256"]] = device_id
                raise
            return record

    def set_default_ttl(self, days: Optional[int]) -> None:
        with self.lock:
            before = self.default_ttl_days
            self.default_ttl_days = days
            try:
                self._persist()
            except BaseException:
                self.default_ttl_days = before
                raise


def store_for(app) -> DeviceStore:
    """数据根下 auth/devices.json；首次用到时装载（不改 app.py 的装配顺序）。"""
    store = getattr(app.state, "device_store", None)
    if store is None:
        from framework.utils.config_loader import data_dir
        store = app.state.device_store = DeviceStore(data_dir() / "auth" / "devices.json")
    return store
