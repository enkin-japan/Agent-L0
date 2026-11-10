"""搜索 API 提供方（build 85 批 C）：用户在设置页「搜索」添加的付费 / 订阅搜索 API。

用户原话：「优先使用 API，拿现有的兜底」「多 API 可以随机安排，但是但凡有一个可用的 API，
则优先走 API」。本模块管三件事，搜索链本体在 web_tools.web_search：
- 存储：非密钥配置落 `<数据根>/search_providers.json`（0600、原子写，approval_policy 同款）；
  密钥实值只经 api_tool_factory.read_secret / write_secret 进数据根 api_tools/.secrets.env
  （0600），变量名由 id 派生（SEARCH_API_KEY_<ID>），本文件与 json 里都只有变量名。
  文件缺失 = 空列表；存在但损坏 / 读不了 = 按空列表处理并告警一次（只走 SearXNG，不拒启）。
- 适配器：类型注册表 PROVIDER_TYPES（加新类型 = 加一个适配器类）。请求一律走
  web_tools._session（与 SearXNG / 抓页同一 HTTP 出口）；失败抛 ProviderUnavailable(kind)。
- 冷却：进程内、线程安全的「最近状态 + 冷却到期」表（设置页显示用的状态也在这里）。

密钥红线：密钥只出现在发往提供方的请求头里。异常、返回文本、日志、状态表、json 文件里
都只有 kind / 状态码 / 变量名——ProviderUnavailable 在 except 块**之外**抛出，不挂原异常
（原异常文本可能含完整 URL 与请求头），也不留 __context__。
"""
import html
import json
import os
import random
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import web_tools

FILENAME = "search_providers.json"
FALLBACK = "searxng"
SECRET_PREFIX = "SEARCH_API_KEY_"
MAX_SECRET_LEN = 512
MAX_LABEL_LEN = 64
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")
_ENV_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
# 控制字符与一切「行界」：C0、DEL、C1（\x80-\x9f，含 NEL \x85）、Unicode 行 / 段分隔符。
# api_tool_factory 用 str.splitlines() 切密钥文件——这些字符它都当换行，混进密钥值就能
# 在 .secrets.env 里注入一行任意变量。密钥与标签都受此约束。
_CTRL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")

KINDS = ("auth", "rate_limit", "quota", "http", "network", "bad_response")
# kind → 设置页「最近状态」（ok 另记；从未用过 = unused）
_STATUS_OF_KIND = {"auth": "auth_error", "rate_limit": "rate_limit", "quota": "quota"}
RETRY_AFTER_CAP = 600.0          # Retry-After 最多把冷却抬到 10 分钟
# 搜索 API 单次请求超时（cfg web_search.search_api_timeout 可覆写）。不用 cfg 的 timeout（15 s，
# SearXNG / 抓页用）：两家都网络超时再叠 SearXNG 与确认查询，会吃掉子沙箱总预算的一大半。
API_TIMEOUT = 8.0


class ProviderUnavailable(Exception):
    """某一提供方本次不可用。str() 只含 kind 与 HTTP 状态码——绝不含异常原文、URL、请求头。"""

    def __init__(self, kind: str, status: Optional[int] = None,
                 retry_after: Optional[float] = None):
        self.kind = kind if kind in KINDS else "bad_response"
        self.status = status if isinstance(status, int) else None
        self.retry_after = retry_after
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"{self.kind} (HTTP {self.status})" if self.status is not None else self.kind


_TAG_RE = re.compile(r"<[^>]*>")


def _clean(text: Any) -> str:
    """去 HTML 标签（Brave 的 description 带 <strong> 高亮）+ 反转义实体。"""
    if not isinstance(text, str):
        return ""
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _retry_after(resp) -> Optional[float]:
    """Retry-After 只认整数 / 小数秒（HTTP 日期形态忽略）。"""
    try:
        raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
        val = float(str(raw).strip()) if raw is not None else None
    except (TypeError, ValueError, AttributeError):
        return None
    return val if val is not None and val >= 0 else None


def _api_timeout(cfg: dict) -> float:
    try:
        val = float(cfg.get("search_api_timeout", API_TIMEOUT))
    except (TypeError, ValueError):
        return API_TIMEOUT
    return val if val > 0 else API_TIMEOUT


def _limit(cfg: dict, n: Optional[int]) -> int:
    try:
        val = int(n or cfg.get("max_results", 8))
    except (TypeError, ValueError):
        val = 8
    return max(1, min(val, 20))


class _Adapter:
    type = ""
    label = ""
    engine = ""

    def _status_kind(self, status: int) -> str:
        if status in (401, 403):
            return "auth"
        if status == 429:
            return "rate_limit"
        return "http"

    def _send(self, method: str, url: str, timeout: float, **kwargs):
        """发一次请求并做状态分类。分类在 except 之外完成后再抛，异常不带原异常链。
        禁止重定向：requests 跨主机重定向只剥 Authorization，不剥 Brave 的
        X-Subscription-Token——3xx 一律落进「非 2xx → http」。"""
        kind, resp = None, None
        try:
            fn = getattr(web_tools._session(), method)
            resp = fn(url, timeout=timeout, allow_redirects=False, **kwargs)
        except Exception:      # 超时 / 连接错 / 其它请求失败：原文可能含 URL 与头，一概不留
            kind = "network"
        if kind is not None:
            raise ProviderUnavailable(kind)
        status = getattr(resp, "status_code", 0)
        if not isinstance(status, int) or not 200 <= status < 300:
            code = status if isinstance(status, int) else None
            raise ProviderUnavailable(self._status_kind(code or 0), code, _retry_after(resp))
        data, bad = None, False
        try:
            data = resp.json()
        except Exception:
            bad = True
        if bad or not isinstance(data, dict):
            raise ProviderUnavailable("bad_response")
        return data

    @staticmethod
    def _items(rows, desc_key: str, engine: str) -> List[Dict]:
        """归一成 web_tools._rank 吃的原始条目（title / url / content / engine）。"""
        out = []
        for x in rows:
            if not isinstance(x, dict):
                continue
            url = x.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            out.append({"title": _clean(x.get("title")), "url": url,
                        "content": _clean(x.get(desc_key)), "engine": engine})
        return out


class BraveProvider(_Adapter):
    """Brave Search API：GET web/search（category=news 时 news/search），密钥在 X-Subscription-Token 头。"""
    type = "brave"
    label = "Brave Search"
    engine = "brave-api"
    WEB_URL = "https://api.search.brave.com/res/v1/web/search"
    NEWS_URL = "https://api.search.brave.com/res/v1/news/search"
    FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}

    def search(self, key: str, query: str, cfg: dict, n: Optional[int] = None,
               time_range: Optional[str] = None, category: Optional[str] = None) -> List[Dict]:
        news = category == "news"
        params: Dict[str, Any] = {"q": query, "count": _limit(cfg, n)}
        if time_range in self.FRESHNESS:
            params["freshness"] = self.FRESHNESS[time_range]
        data = self._send("get", self.NEWS_URL if news else self.WEB_URL,
                          _api_timeout(cfg), params=params,
                          headers={"X-Subscription-Token": key, "Accept": "application/json"})
        web = data.get("web")
        if isinstance(web, dict) and isinstance(web.get("results"), list):
            rows = web["results"]
        elif isinstance(data.get("results"), list):
            rows = data["results"]
        elif web is None and not news and data.get("type") == "search":
            rows = []      # web 端点没有网页结果时整块 web 省略：合法 0 条
        else:
            raise ProviderUnavailable("bad_response")
        return self._items(rows, "description", self.engine)


class TavilyProvider(_Adapter):
    """Tavily Search API：POST /search，密钥只在 Authorization: Bearer 头（绝不进 JSON body）。
    只用 basic 深度、不开 auto_parameters（可能把一次搜索升成 2 个额度）。"""
    type = "tavily"
    label = "Tavily"
    engine = "tavily-api"
    URL = "https://api.tavily.com/search"
    TIME_RANGES = ("day", "week", "month", "year")

    def _status_kind(self, status: int) -> str:
        if status in (432, 433):
            return "quota"      # 套餐 / 按量额度用尽
        return super()._status_kind(status)

    def search(self, key: str, query: str, cfg: dict, n: Optional[int] = None,
               time_range: Optional[str] = None, category: Optional[str] = None) -> List[Dict]:
        body: Dict[str, Any] = {
            "query": query, "search_depth": "basic", "max_results": _limit(cfg, n),
            "topic": "news" if category == "news" else "general",
            "include_answer": False, "include_raw_content": False,
        }
        if time_range in self.TIME_RANGES:
            body["time_range"] = time_range
        data = self._send("post", self.URL, _api_timeout(cfg), json=body,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"})
        rows = data.get("results")
        if not isinstance(rows, list):
            raise ProviderUnavailable("bad_response")
        return self._items(rows, "content", self.engine)


PROVIDER_TYPES = {"brave": BraveProvider, "tavily": TavilyProvider}


def type_listing() -> List[Dict[str, str]]:
    return [{"type": t, "label": cls.label} for t, cls in PROVIDER_TYPES.items()]


# ==========================================================================
# 存储
# ==========================================================================
# 测试隔离用：非 None 时取代数据根（conftest 按测试钉到空临时目录，dev 数据根里
# 用户添加过的提供方不许漏进金测试）。生产恒为 None。
_root_override: Optional[Path] = None
_STORE_LOCK = threading.RLock()
# corrupt：当前磁盘上的文件存在但损坏 / 结构不对——_save 覆盖它前先备份成 .corrupt
_cache: Dict[str, Any] = {"key": None, "providers": [], "corrupt": False}
_warned: set = set()


def _root() -> Path:
    if _root_override is not None:
        return Path(_root_override)
    from ..utils.config_loader import data_dir
    return data_dir()


def store_path() -> Path:
    return _root() / FILENAME


def env_name_for(provider_id: str) -> str:
    """id → 密钥变量名：SEARCH_API_KEY_ + 大写、`-` 换 `_`。不合规则 ValueError。"""
    if not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id):
        raise ValueError("invalid provider id")
    name = SECRET_PREFIX + provider_id.upper().replace("-", "_")
    if not _ENV_RE.fullmatch(name):
        raise ValueError("invalid provider id")
    return name


def _warn_once(path: Path, tag, reason: str) -> None:
    key = (str(path), tag)
    if key in _warned:
        return
    _warned.add(key)
    print(f"[SearchProviders] ⚠️ {path} 无法使用（{reason}），按未配置任何搜索 API 处理（只用内置搜索）")


def _parse(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("providers"), list):
        raise ValueError("unexpected structure")
    out, seen = [], set()
    for p in raw["providers"]:
        if not isinstance(p, dict):
            raise ValueError("unexpected structure")
        pid, ptype = p.get("id"), p.get("type")
        label, enabled = p.get("label", ""), p.get("enabled", True)
        if (not isinstance(pid, str) or not _ID_RE.fullmatch(pid) or pid in seen
                or not isinstance(ptype, str) or not isinstance(label, str)
                or not isinstance(enabled, bool)):
            raise ValueError("unexpected structure")
        seen.add(pid)
        out.append({"id": pid, "type": ptype, "label": label[:MAX_LABEL_LEN], "enabled": enabled})
    return out


def load_providers() -> List[Dict[str, Any]]:
    """当前提供方列表（副本）。绝不抛。按 (mtime, 大小, inode) 判断是否重读——
    设置页的改动下一次搜索即生效，不重启。"""
    path = store_path()
    with _STORE_LOCK:
        try:
            st = path.stat()
        except FileNotFoundError:
            _cache.update(key=None, providers=[], corrupt=False)
            return []
        except OSError as e:
            _warn_once(path, "stat", type(e).__name__)
            return []
        key = (str(path), st.st_mtime_ns, st.st_size, st.st_ino)
        if _cache["key"] != key:
            corrupt = False
            try:
                providers = _parse(json.loads(path.read_text(encoding="utf-8")))
            except Exception as e:
                _warn_once(path, key[1:], type(e).__name__)
                providers, corrupt = [], True
            _cache.update(key=key, providers=providers, corrupt=corrupt)
        return [dict(p) for p in _cache["providers"]]


def _backup_corrupt(path: Path) -> None:
    """要覆盖的是一份损坏文件：先改名成同目录 search_providers.json.corrupt（已有则覆盖），
    不静默丢掉用户的原文件。改名失败不拦落盘，只告警（不带内容）。调用方持 _STORE_LOCK。"""
    if not _cache.get("corrupt"):
        return
    try:
        os.replace(path, path.with_name(path.name + ".corrupt"))
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"[SearchProviders] ⚠️ 损坏文件备份失败（{type(e).__name__}），直接覆盖")
    _cache["corrupt"] = False


def _save(providers: List[Dict[str, Any]]) -> None:
    """原子写（同目录 O_EXCL 0600 临时文件 + fsync + os.replace），approval_policy.save 同款。
    调用方先 load_providers()（同一把锁内），corrupt 标记据此是最新的。"""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_corrupt(path)
    payload = json.dumps({"version": 1, "providers": providers},
                         ensure_ascii=False, indent=1).encode("utf-8")
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
    _cache.update(key=None, providers=[], corrupt=False)


def _check_label(label: Any) -> str:
    if label is None:
        return ""
    if not isinstance(label, str):
        raise ValueError("label must be a string")
    label = label.strip()
    if len(label) > MAX_LABEL_LEN or _CTRL_RE.search(label):
        raise ValueError(f"label must be at most {MAX_LABEL_LEN} characters without control characters")
    return label


def _check_secret(secret: Any) -> str:
    """校验密钥值；错误文本里绝不带值本身（也不带长度）。"""
    if not isinstance(secret, str):
        raise ValueError("secret must be a string")
    secret = secret.strip()
    if not secret:
        raise ValueError("secret must not be empty")
    if len(secret) > MAX_SECRET_LEN or _CTRL_RE.search(secret):
        raise ValueError(f"secret must be at most {MAX_SECRET_LEN} characters without "
                         "line breaks or control characters")
    return secret


def _factory():
    from ..mcp.tools import api_tool_factory
    return api_tool_factory


def has_secret(provider_id: str) -> bool:
    try:
        return bool(_factory().read_secret(env_name_for(provider_id)))
    except Exception:
        return False


def add_provider(ptype: Any, label: Any, secret: Any) -> Dict[str, Any]:
    """新增一条（id 服务端生成）。先写密钥再落列表；列表落盘失败则把密钥清回空。"""
    if not isinstance(ptype, str) or ptype not in PROVIDER_TYPES:
        raise ValueError(f"unknown provider type (allowed: {sorted(PROVIDER_TYPES)})")
    label = _check_label(label)
    secret = _check_secret(secret)
    factory = _factory()
    with _STORE_LOCK:
        providers = load_providers()
        taken = {p["id"] for p in providers}
        pid = f"{ptype}-{secrets.token_hex(3)}"
        while pid in taken:
            pid = f"{ptype}-{secrets.token_hex(3)}"
        env = env_name_for(pid)
        factory.write_secret(env, secret)
        entry = {"id": pid, "type": ptype, "label": label, "enabled": True}
        try:
            _save(providers + [entry])
        except BaseException:
            factory.write_secret(env, "")
            raise
    forget(pid)
    return public_view(entry)


def patch_provider(provider_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    """部分更新 enabled / label / secret（None = 不变）。未知 id → KeyError；非法值 → ValueError。"""
    unknown = sorted(str(k) for k in changes if k not in ("enabled", "label", "secret"))
    if unknown:
        raise ValueError(f"unknown field(s): {unknown}")
    with _STORE_LOCK:
        providers = load_providers()
        entry = next((p for p in providers if p["id"] == provider_id), None)
        if entry is None:
            raise KeyError(provider_id)
        enabled, label, secret = changes.get("enabled"), changes.get("label"), changes.get("secret")
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        label = _check_label(label) if label is not None else None
        secret = _check_secret(secret) if secret is not None else None
        try:
            # 先落列表、再写钥：落盘失败时旧钥原样不动（不留「钥换了、开关没改」的半截提交）
            if enabled is not None or label is not None:
                if enabled is not None:
                    entry["enabled"] = enabled
                if label is not None:
                    entry["label"] = label
                _save(providers)
            if secret is not None:
                _factory().write_secret(env_name_for(provider_id), secret)
        finally:
            if secret is not None:
                forget(provider_id)      # 带了新钥：旧钥的冷却 / 状态一律作废（写钥成败都清）
    return public_view(entry)


def delete_provider(provider_id: str) -> bool:
    """删除一条：密钥清空（api_tool_factory 没有删除函数，写空串 = 无）、列表去掉、冷却清掉。"""
    with _STORE_LOCK:
        providers = load_providers()
        rest = [p for p in providers if p["id"] != provider_id]
        if len(rest) == len(providers):
            return False
        # 先清钥、再落列表：落盘失败时条目还在且 has_secret=False（界面看得到、可重试），
        # 绝不留下「条目没了、钥还在文件里」；清钥失败则什么都没动。
        _factory().write_secret(env_name_for(provider_id), "")
        _save(rest)
    forget(provider_id)
    return True


def get_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    return next((p for p in load_providers() if p["id"] == provider_id), None)


def public_view(entry: Dict[str, Any]) -> Dict[str, Any]:
    """对外条目：只有「有 / 无」密钥布尔，没有密钥值与变量名。"""
    status, at = status_of(entry["id"])
    return {"id": entry["id"], "type": entry["type"], "label": entry.get("label", ""),
            "enabled": bool(entry.get("enabled", True)), "has_secret": has_secret(entry["id"]),
            "status": status, "status_at": at}


def listing() -> Dict[str, Any]:
    return {"types": type_listing(), "providers": [public_view(p) for p in load_providers()],
            "fallback": FALLBACK}


# ==========================================================================
# 冷却 + 最近状态（进程内，线程安全）
# ==========================================================================
_STATE_LOCK = threading.Lock()
_state: Dict[str, Dict[str, Any]] = {}
_wall = time.time            # status_at 用墙钟（设置页显示）；冷却到期用 web_tools._clock


def reset_state() -> None:
    """清空冷却与状态（测试复位用）。"""
    with _STATE_LOCK:
        _state.clear()


def forget(provider_id: str) -> None:
    with _STATE_LOCK:
        _state.pop(provider_id, None)


def status_of(provider_id: str) -> Tuple[str, Optional[float]]:
    with _STATE_LOCK:
        rec = _state.get(provider_id)
        return (rec["status"], rec["at"]) if rec else ("unused", None)


def _cooldown_seconds(kind: str, cfg: dict, retry_after: Optional[float]) -> float:
    if kind == "auth":
        key, default = "search_api_auth_cooldown", 600
    elif kind == "quota":
        key, default = "search_api_quota_cooldown", 3600
    else:
        key, default = "search_api_cooldown", 60
    try:
        base = float(cfg.get(key, default))
    except (TypeError, ValueError):
        base = float(default)
    if retry_after is not None:
        base = max(base, min(float(retry_after), RETRY_AFTER_CAP))
    return base


def note_ok(provider_id: str) -> None:
    with _STATE_LOCK:
        _state[provider_id] = {"status": "ok", "kind": None, "at": _wall(), "until": 0.0}


def note_failure(provider_id: str, kind: str, cfg: dict,
                 retry_after: Optional[float] = None) -> None:
    secs = _cooldown_seconds(kind, cfg, retry_after)
    with _STATE_LOCK:
        _state[provider_id] = {"status": _STATUS_OF_KIND.get(kind, "error"), "kind": kind,
                               "at": _wall(), "until": web_tools._clock() + secs}


def _cooling(provider_id: str, now: float) -> bool:
    with _STATE_LOCK:
        rec = _state.get(provider_id)
        return bool(rec) and now < float(rec["until"])


# 随机源（测试注入一个带 shuffle 的替身，钉住顺序）
_rng = random.Random()


def _usable(providers) -> List[Tuple[Dict[str, Any], type, str]]:
    """已启用 ∧ 类型已知 ∧ 不在冷却 ∧ 有密钥（按配置序）。"""
    now = web_tools._clock()
    out = []
    for p in providers:
        cls = PROVIDER_TYPES.get(p["type"])
        if not p.get("enabled") or cls is None or _cooling(p["id"], now):
            continue
        try:
            key = _factory().read_secret(env_name_for(p["id"]))
        except Exception:
            key = ""
        if key:
            out.append((p, cls, key))
    return out


def candidates() -> List[Tuple[Dict[str, Any], type, str]]:
    """本次搜索的候选 API（每次随机打乱）。元素 (条目, 适配器类, 密钥)——密钥只交给适配器。"""
    out = _usable(load_providers())
    _rng.shuffle(out)
    return out


def api_available() -> bool:
    """此刻是否存在一个「已启用 ∧ 有密钥 ∧ 不在冷却」的 API。"""
    return bool(_usable(load_providers()))


def has_configured_api() -> bool:
    """是否存在已启用且有密钥的 API（不看冷却；web_research 注册判断用）。"""
    return any(p.get("enabled") and p["type"] in PROVIDER_TYPES and has_secret(p["id"])
               for p in load_providers())


def unavailable_notes() -> List[str]:
    """已启用且有密钥、但正在冷却的提供方摘要「brave-api (rate_limit)」——只有类型与 kind。"""
    now = web_tools._clock()
    notes = []
    for p in load_providers():
        if not p.get("enabled") or p["type"] not in PROVIDER_TYPES or not has_secret(p["id"]):
            continue
        with _STATE_LOCK:
            rec = _state.get(p["id"])
            if rec and now < float(rec["until"]):
                note = f"{p['type']}-api ({rec['kind']})"
                if note not in notes:
                    notes.append(note)
    return notes


def run_test(provider_id: str, cfg: dict) -> Dict[str, Any]:
    """设置页「测试」：用该条目发一次常青查询。只会改动它的冷却 / 状态。
    未知 id → KeyError；没有密钥 → 不发请求，kind=auth。"""
    entry = get_provider(provider_id)
    if entry is None:
        raise KeyError(provider_id)
    cls = PROVIDER_TYPES.get(entry["type"])
    try:
        key = _factory().read_secret(env_name_for(provider_id))
    except Exception:
        key = ""
    if cls is None or not key:
        return {"ok": False, "kind": "auth", "results": 0}
    kind, retry_after, rows = "ok", None, []
    try:
        rows = cls().search(key, web_tools.SEARCH_CONFIRM_QUERY, cfg)
    except ProviderUnavailable as e:
        kind, retry_after = e.kind, e.retry_after
    except Exception:
        kind = "bad_response"
    if kind != "ok":
        note_failure(provider_id, kind, cfg, retry_after)
        return {"ok": False, "kind": kind, "results": 0}
    note_ok(provider_id)
    return {"ok": True, "kind": "ok", "results": len(rows)}
