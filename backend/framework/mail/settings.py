"""邮件发送设置（send_email 工具；设置页「邮件发送」）。

用户要求：只发不收；发信前必须出审批卡（收件人 + 内容摘要），用户确认后才发。
传输：SMTP over implicit TLS（Fastmail 官方：smtp.fastmail.com:465，用户名 = 完整地址，
密码 = app 专用密码）。

存储（search_providers 同款）：
- 非密钥配置落 `<数据根>/mail_settings.json`（0600、原子写），按 (mtime, 大小, inode)
  变化重读——设置页保存后下一次调用即生效，不重启。文件缺失 = 缺省（未配置）；
  存在但损坏 = 按缺省处理并告警一次（工具不注册 / 拒发，不拒启）。
- 密码实值只经 api_tool_factory.read_secret / write_secret 进数据根 api_tools/.secrets.env
  （0600），变量名 MAIL_SMTP_PASSWORD；本文件与 json 里都只有变量名。

收件人只能是设置里登记的联系人（≤ 20 个）：工具按名字 / 地址解析到唯一一个联系人，
任何未登记地址一律拒发——模型（以及被注入的网页内容）无法把邮件发给任意地址。

密钥红线：密码不进返回值、日志、异常文本（校验错误文本里不带值、不带长度）。
"""
import json
import os
import re
import secrets
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

FILENAME = "mail_settings.json"
SECRET_NAME = "MAIL_SMTP_PASSWORD"
DEFAULT_HOST = "smtp.fastmail.com"
DEFAULT_PORT = 465
MAX_CONTACTS = 20
MAX_NAME_LEN = 40
MAX_ADDR_LEN = 254
# 与 search_providers.MAX_SECRET_LEN / _check_secret 同一口径（测试钉住两边一致）
MAX_SECRET_LEN = 512
# 控制字符与一切「行界」：逐字照抄 search_providers._CTRL_RE（测试钉住两边一致）。
# api_tool_factory 用 str.splitlines() 切密钥文件——这些字符混进密码值就能在
# .secrets.env 里注入一行任意变量；联系人名字同受此约束（它会进邮件头与审批卡）。
_CTRL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
# 纯主机名：字母数字点连字符；无 scheme、无路径、无端口、无空白
_HOST_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")
# 保守的单地址：ASCII、恰一个 @、无空白 / 控制字符 / <>,;" 等分隔符，域名须含点
_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_DOMAIN_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")
# , ; 也禁：send_email 的 `to` 含 , ; 一律按多收件人拒，名字里有它们的联系人就叫不到了
_NAME_FORBIDDEN = set('<>",;')
# 解析收件人时最多去掉一个结尾敬称（长的先试：ちゃん 先于 ちゃ…，先生 先于 生）
HONORIFICS = ("ちゃん", "先生", "さん", "さま", "くん", "様", "君", "氏")

# 测试隔离用：非 None 时取代数据根（conftest 钉到每个测试自己的空临时目录）。生产恒为 None。
_root_override: Optional[Path] = None
# 与 search_providers 共用的密钥文件锁（write_secret 是读改写；两个模块写同一个文件）。
# 本模块的存储段也在这把锁里——json 与密码要么一起改成、要么一起回滚。
from ..utils.secrets_lock import SECRETS_FILE_LOCK as _LOCK  # noqa: E402
_cache: Dict[str, Any] = {"key": None, "settings": None, "corrupt": False}
_warned: set = set()


def defaults() -> Dict[str, Any]:
    return {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "username": "", "from_addr": "",
            "contacts": []}


def _root() -> Path:
    if _root_override is not None:
        return Path(_root_override)
    from ..utils.config_loader import data_dir
    return data_dir()


def store_path() -> Path:
    return _root() / FILENAME


# ---------------------------------------------------------------- 校验 / 归一

def normalize_name(text: Any) -> str:
    """strip → NFKC → casefold → 去掉至多一个结尾敬称（再 strip）。名字匹配与重名判定共用。"""
    s = unicodedata.normalize("NFKC", str(text or "").strip()).casefold().strip()
    for h in HONORIFICS:
        if s.endswith(h):
            s = s[:-len(h)].strip()
            break
    return s


def is_address(text: Any) -> bool:
    """保守的单地址判定（见 _LOCAL_RE / _DOMAIN_RE）。"""
    if not isinstance(text, str) or not text or len(text) > MAX_ADDR_LEN or not text.isascii():
        return False
    if text.count("@") != 1:
        return False
    local, domain = text.split("@")
    return (len(local) <= 64 and bool(_LOCAL_RE.fullmatch(local))
            and len(domain) <= 253 and bool(_DOMAIN_RE.fullmatch(domain)))


def _str_field(body: Dict[str, Any], key: str, default: str) -> str:
    val = body.get(key, default)
    if val is None:
        val = default
    if not isinstance(val, str):
        raise ValueError(f"{key} must be a string")
    return val.strip()


def validate(body: Any) -> Dict[str, Any]:
    """校验非密钥设置并归一（缺省字段取缺省）。非法 → ValueError（文本可直接给设置页）。"""
    if not isinstance(body, dict):
        raise ValueError("settings must be an object")
    unknown = sorted(str(k) for k in body
                     if k not in ("host", "port", "username", "from_addr", "contacts"))
    if unknown:
        raise ValueError(f"unknown field(s): {unknown}")
    host = _str_field(body, "host", DEFAULT_HOST)
    if len(host) > 253 or not _HOST_RE.fullmatch(host):
        raise ValueError("server must be a plain host name such as smtp.fastmail.com "
                         "(letters, digits, dots and hyphens; no scheme, path or spaces)")
    port = body.get("port", DEFAULT_PORT)
    if port is None:
        port = DEFAULT_PORT
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    username = _str_field(body, "username", "")
    if username and not is_address(username):
        raise ValueError("username must be a single full mail address (name@example.com)")
    from_addr = _str_field(body, "from_addr", "")
    if from_addr and not is_address(from_addr):
        raise ValueError("from address must be a single mail address (name@example.com)")
    raw = body.get("contacts", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("contacts must be a list")
    if len(raw) > MAX_CONTACTS:
        raise ValueError(f"at most {MAX_CONTACTS} contacts")
    contacts: List[Dict[str, str]] = []
    names, addrs = set(), set()
    for i, c in enumerate(raw, 1):
        if not isinstance(c, dict) or set(c) - {"name", "address"}:
            raise ValueError(f"contact {i} must have exactly a name and an address")
        name, address = c.get("name"), c.get("address")
        if not isinstance(name, str) or not isinstance(address, str):
            raise ValueError(f"contact {i}: name and address must be strings")
        name, address = name.strip(), address.strip()
        if (not name or len(name) > MAX_NAME_LEN or _CTRL_RE.search(name)
                or any(ch in _NAME_FORBIDDEN for ch in name)):
            raise ValueError(f"contact {i}: name must be 1-{MAX_NAME_LEN} characters without "
                             'line breaks, control characters or < > " , ;')
        key = normalize_name(name)
        if not key:
            raise ValueError(f"contact {i}: name must not be only an honorific")
        if key in names:
            raise ValueError(f"contact {i}: the name {name!r} is the same as another contact's "
                             "(names are compared ignoring case, width and a trailing honorific)")
        if not is_address(address):
            raise ValueError(f"contact {i}: {name!r} needs a single valid mail address")
        if address.casefold() in addrs:
            raise ValueError(f"contact {i}: the address of {name!r} is already used by another contact")
        names.add(key)
        addrs.add(address.casefold())
        contacts.append({"name": name, "address": address})
    return {"host": host, "port": port, "username": username, "from_addr": from_addr,
            "contacts": contacts}


def check_password(value: Any) -> str:
    """search_providers._check_secret 同口径；错误文本里绝不带值本身（也不带长度）。"""
    if not isinstance(value, str):
        raise ValueError("password must be a string")
    value = value.strip()
    if not value:
        raise ValueError("password must not be empty")
    if len(value) > MAX_SECRET_LEN or _CTRL_RE.search(value):
        raise ValueError(f"password must be at most {MAX_SECRET_LEN} characters without "
                         "line breaks or control characters")
    return value


# ---------------------------------------------------------------- 读写

def _warn_once(path: Path, tag, reason: str) -> None:
    key = (str(path), tag)
    if key in _warned:
        return
    _warned.add(key)
    print(f"[MailSettings] ⚠️ {path} 无法使用（{reason}），按未配置邮件发送处理")


def _parse(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("unexpected structure")
    return validate({k: v for k, v in raw.items() if k != "version"})


def load() -> Dict[str, Any]:
    """当前设置（副本）。绝不抛。按 (mtime, 大小, inode) 判断是否重读。"""
    path = store_path()
    with _LOCK:
        try:
            st = path.stat()
        except FileNotFoundError:
            _cache.update(key=None, settings=None, corrupt=False)
            return defaults()
        except OSError as e:
            _warn_once(path, "stat", type(e).__name__)
            return defaults()
        key = (str(path), st.st_mtime_ns, st.st_size, st.st_ino)
        if _cache["key"] != key:
            corrupt = False
            try:
                parsed = _parse(json.loads(path.read_text(encoding="utf-8")))
            except Exception as e:
                _warn_once(path, key[1:], type(e).__name__)
                parsed, corrupt = defaults(), True
            _cache.update(key=key, settings=parsed, corrupt=corrupt)
        s = _cache["settings"]
        return {**s, "contacts": [dict(c) for c in s["contacts"]]}


def _backup_corrupt(path: Path) -> None:
    """要覆盖的是一份损坏文件：先改名成 .corrupt，不静默丢掉用户的原文件。调用方持锁。"""
    if not _cache.get("corrupt"):
        return
    try:
        os.replace(path, path.with_name(path.name + ".corrupt"))
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"[MailSettings] ⚠️ 损坏文件备份失败（{type(e).__name__}），直接覆盖")
    _cache["corrupt"] = False


def _save(settings: Dict[str, Any]) -> None:
    """原子写（同目录 O_EXCL 0600 临时文件 + fsync + os.replace），search_providers 同款。"""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_corrupt(path)
    payload = json.dumps({"version": 1, **settings}, ensure_ascii=False, indent=1).encode("utf-8")
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _cache.update(key=None, settings=None, corrupt=False)


def _factory():
    from ..mcp.tools import api_tool_factory
    return api_tool_factory


# 生产取法的别名：conftest 的自动隔离把 _factory 换成内存替身（任何测试都碰不到真实密钥文件），
# 需要真实读写（指向临时数据根）的测试显式换回这一个。
_default_factory = _factory


def password() -> str:
    """SMTP 密码实值（只交给 smtplib.login）。读不到 = 空串。"""
    try:
        return _factory().read_secret(SECRET_NAME) or ""
    except Exception:
        return ""


def has_password() -> bool:
    return bool(password())


def is_configured(settings: Optional[Dict[str, Any]] = None) -> bool:
    """已配置 ⇔ 用户名非空 ∧ 至少一个联系人 ∧ 密码在。先看前两项（不必读密钥文件）。"""
    s = settings if settings is not None else load()
    return bool(s.get("username")) and bool(s.get("contacts")) and has_password()


def sender(settings: Dict[str, Any]) -> str:
    """发件地址：from_addr，空则用户名。"""
    return settings.get("from_addr") or settings.get("username") or ""


def public_view() -> Dict[str, Any]:
    """对外视图：密码只以 has_password 布尔出现（无值、无变量名、无长度）。"""
    s = load()
    has_pw = has_password()
    return {**s, "has_password": has_pw,
            "configured": bool(s["username"]) and bool(s["contacts"]) and has_pw}


def update(body: Any, *, new_password: Optional[str] = None,
           clear_password: bool = False) -> Dict[str, Any]:
    """整体替换非密钥设置；new_password 为 None / 空串 = 保留已存的；clear_password 清掉。
    先全部校验（任何一项非法 → ValueError，什么都不写），再落 json、最后写 / 清密码。"""
    settings = validate(body)
    if new_password is not None and not isinstance(new_password, str):
        raise ValueError("password must be a string")
    pw = check_password(new_password) if new_password else None
    if clear_password and pw is not None:
        raise ValueError("send either a new password or clear_password, not both")
    with _LOCK:
        load()                      # 刷新 corrupt 标记
        path = store_path()
        try:
            previous = path.read_bytes()
        except FileNotFoundError:
            previous = None
        _save(settings)
        try:
            if clear_password:
                # api_tool_factory 没有删除函数：写空串 = 无（search_providers.delete_provider 同法）
                _factory().write_secret(SECRET_NAME, "")
            elif pw is not None:
                _factory().write_secret(SECRET_NAME, pw)
        except BaseException:
            _restore(path, previous)     # 密码没写成：json 回到保存前，不留半截改动
            raise
    return public_view()


def _restore(path: Path, previous: Optional[bytes]) -> None:
    """把设置文件恢复成保存前的字节（原本没有就删掉）。原子写；失败只告警（原异常照抛）。"""
    try:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(previous)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
    except OSError as e:
        print(f"[MailSettings] ⚠️ 回滚设置文件失败（{type(e).__name__}）")
    _cache.update(key=None, settings=None, corrupt=False)
