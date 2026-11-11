"""页面发布设置（publish_page 工具；设置页「页面发布」）。

用户的设计：邮件里带链接而不是附件。publish_page 把 ~/AgentPages 里的一个 HTML 文件复制进
用户自己的、由 web 服务器对外提供的文件夹（root_dir），放在一个长随机目录名下，返回
`<base_url>/<随机目录>/<文件名>` 这条公开链接。

存储（mail.settings / search_providers 同款）：`<数据根>/page_publish.json`（0600、原子写），
按 (mtime, 大小, inode) 变化重读——设置页保存后下一次调用即生效。文件缺失 = 未配置；
存在但损坏 = 按未配置处理并告警一次。没有密钥。

「已配置」只看两个字段都非空——is_available 不碰 root_dir 所在的文件系统（macOS 上 stat
~/Documents 之类的路径会在启动时弹系统权限框）；目录是否存在留到执行时再查。
"""
import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

FILENAME = "page_publish.json"
# 控制字符与一切「行界」（mail.settings / search_providers 同一口径）
_CTRL_RE = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029]")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")       # 须含点
_PATH_RE = re.compile(r"^(?:/[A-Za-z0-9._~%-]*)*$")

# 测试隔离用：非 None 时取代数据根（conftest 钉到每个测试自己的空临时目录）。生产恒为 None。
_root_override: Optional[Path] = None
_LOCK = threading.RLock()
_cache: Dict[str, Any] = {"key": None, "settings": None}
_warned: set = set()


def defaults() -> Dict[str, str]:
    return {"root_dir": "", "base_url": ""}


def _root() -> Path:
    if _root_override is not None:
        return Path(_root_override)
    from ..utils.config_loader import data_dir
    return data_dir()


def store_path() -> Path:
    return _root() / FILENAME


def _check_root_dir(raw: str) -> str:
    if _CTRL_RE.search(raw):
        raise ValueError("publish folder must not contain line breaks or control characters")
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise ValueError("publish folder must be an absolute path (or start with ~/)")
    path = os.path.normpath(expanded)
    if path == "/" or path == os.path.normpath(os.path.expanduser("~")):
        raise ValueError("publish folder must not be / or your home folder itself")
    return path


def _check_base_url(raw: str) -> str:
    if _CTRL_RE.search(raw) or any(ch.isspace() for ch in raw):
        raise ValueError("public URL must not contain spaces or control characters")
    bad = ValueError("public URL must look like https://example.com or https://example.com/path "
                     "(https only; no user name, port, query or fragment)")
    if "?" in raw or "#" in raw or "@" in raw or not raw.isascii():
        raise bad
    try:
        parts = urlsplit(raw)
    except ValueError:
        raise bad
    if parts.scheme != "https" or not raw.startswith("https://"):
        raise bad
    netloc = parts.netloc
    if not netloc or len(netloc) > 253 or not _HOST_RE.fullmatch(netloc):
        raise bad          # 拒 userinfo / 端口（含 `:`）/ 非法主机名
    path = parts.path.rstrip("/")
    if not _PATH_RE.fullmatch(path):
        raise bad
    return f"https://{netloc}{path}"


def validate(body: Any) -> Dict[str, str]:
    """校验并归一。两个都空 = 未配置；否则两个都必须合法。非法 → ValueError。"""
    if not isinstance(body, dict):
        raise ValueError("settings must be an object")
    unknown = sorted(str(k) for k in body if k not in ("root_dir", "base_url"))
    if unknown:
        raise ValueError(f"unknown field(s): {unknown}")
    vals = {}
    for key in ("root_dir", "base_url"):
        v = body.get(key, "")
        if v is None:
            v = ""
        if not isinstance(v, str):
            raise ValueError(f"{key} must be a string")
        vals[key] = v.strip()
    if not vals["root_dir"] and not vals["base_url"]:
        return defaults()
    if not vals["root_dir"] or not vals["base_url"]:
        raise ValueError("set both the publish folder and the public URL, or leave both empty")
    return {"root_dir": _check_root_dir(vals["root_dir"]),
            "base_url": _check_base_url(vals["base_url"])}


def _warn_once(path: Path, tag, reason: str) -> None:
    key = (str(path), tag)
    if key in _warned:
        return
    _warned.add(key)
    print(f"[PagePublish] ⚠️ {path} 无法使用（{reason}），按未配置页面发布处理")


def load() -> Dict[str, str]:
    """当前设置（副本）。绝不抛。按 (mtime, 大小, inode) 判断是否重读。"""
    path = store_path()
    with _LOCK:
        try:
            st = path.stat()
        except FileNotFoundError:
            _cache.update(key=None, settings=None)
            return defaults()
        except OSError as e:
            _warn_once(path, "stat", type(e).__name__)
            return defaults()
        key = (str(path), st.st_mtime_ns, st.st_size, st.st_ino)
        if _cache["key"] != key:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("version") != 1:
                    raise ValueError("unexpected structure")
                parsed = validate({k: v for k, v in raw.items() if k != "version"})
            except Exception as e:
                _warn_once(path, key[1:], type(e).__name__)
                parsed = defaults()
            _cache.update(key=key, settings=parsed)
        return dict(_cache["settings"])


def _save(settings: Dict[str, str]) -> None:
    """原子写（同目录 O_EXCL 0600 临时文件 + fsync + os.replace）。"""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
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
    _cache.update(key=None, settings=None)


def is_configured(settings: Optional[Dict[str, str]] = None) -> bool:
    """已配置 ⇔ 两个字段都非空。只读设置文件，不碰 root_dir。"""
    s = settings if settings is not None else load()
    return bool(s.get("root_dir")) and bool(s.get("base_url"))


def public_view() -> Dict[str, Any]:
    s = load()
    return {**s, "configured": is_configured(s)}


def update(body: Any) -> Dict[str, Any]:
    """整体保存；先校验（非法 → ValueError，什么都不写）再落盘。"""
    settings = validate(body)
    with _LOCK:
        _save(settings)
    return public_view()
