"""算力端 media/ TTL 清扫 + 客户端上传缓存鲜度（分离部署审计缺陷三，2026-08-31）。

守的不变量：
- 服务端：闲置超过 TTL 的媒体被清扫，刚用过的活下来（_media_paths / HEAD /
  上传去重命中都算「使用」并 touch 续命）；TTL=0 整面关闭；
- 客户端（RemoteBackend._ensure_media）：新鲜确认过的 media_id 零网络直接复用；
  过期条目先 HEAD 探活（200 → 只续本地缓存，不重传字节；404/探活失败 → 重传）。
  没有这半场，服务端清扫会把长命 backend 变成「拿幻影 id 发生成、图静默不见」。

服务端按路径装载（m9 测试同惯例，CPU-only 不碰模型）。
"""
import asyncio
import importlib.util
import os
import pathlib
import time

import pytest
from fastapi import HTTPException

_REPO = pathlib.Path(__file__).resolve().parent.parent.parent
_SERVER = _REPO / "inference" / "server.py"

DIGEST = "a" * 64


@pytest.fixture(scope="module")
def srv():
    spec = importlib.util.spec_from_file_location("inference_server_media_under_test", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def media_dir(srv, tmp_path, monkeypatch):
    d = tmp_path / "media"
    d.mkdir()
    monkeypatch.setattr(srv, "MEDIA_DIR", d)
    monkeypatch.setattr(srv, "MEDIA_TTL_SECONDS", 3600.0)
    return d


def _aged(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


# ---------- 服务端清扫 ----------

def test_media_ttl_validation_refuses_sub_day(srv):
    # 量级差防线是代码不是散文：恰为 0=关，≥1 天合法；其余全拒（阶段复审 M1：
    # 负数/空串曾被 `or 0` 静默放成「关闭」——缺陷三原症状无声复发）
    assert srv._media_ttl_seconds(0) == 0.0
    assert srv._media_ttl_seconds(7) == 7 * 86400.0
    assert srv._media_ttl_seconds("2") == 2 * 86400.0
    for bad in (0.5, -1, -0.5):
        with pytest.raises(SystemExit, match="media_ttl_days"):
            srv._media_ttl_seconds(bad)
    for nonnum in (None, "", "一周", []):
        with pytest.raises(SystemExit, match="不是数值"):
            srv._media_ttl_seconds(nonnum)

def test_sweep_deletes_idle_keeps_recent(srv, media_dir):
    old = media_dir / (DIGEST + ".png")
    fresh = media_dir / ("b" * 64 + ".png")
    stale_tmp = media_dir / ("c" * 64 + ".png.tmp")
    for f in (old, fresh, stale_tmp):
        f.write_bytes(b"x")
    _aged(old, 7200)
    _aged(stale_tmp, 7200)
    assert srv._sweep_media() == 2
    assert fresh.exists() and not old.exists() and not stale_tmp.exists()


def test_sweep_disabled_when_ttl_zero(srv, media_dir, monkeypatch):
    monkeypatch.setattr(srv, "MEDIA_TTL_SECONDS", 0.0)
    f = media_dir / (DIGEST + ".png")
    f.write_bytes(b"x")
    _aged(f, 10 ** 8)
    assert srv._sweep_media() == 0 and f.exists()


def test_media_paths_touch_renews_ttl(srv, media_dir):
    f = media_dir / (DIGEST + ".png")
    f.write_bytes(b"x")
    _aged(f, 7200)
    out = srv._media_paths([DIGEST + ".png"], "image")
    assert out == [str(f)]
    assert srv._sweep_media() == 0, "刚被生成引用过的媒体不得清走（touch 续命）"


def test_head_endpoint_touches_and_404s(srv, media_dir):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(srv.media_exists("../etc/passwd"))
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        asyncio.run(srv.media_exists(DIGEST + ".png"))
    assert ei.value.status_code == 404
    f = media_dir / (DIGEST + ".png")
    f.write_bytes(b"x")
    _aged(f, 7200)
    asyncio.run(srv.media_exists(DIGEST + ".png"))
    assert srv._sweep_media() == 0, "HEAD 命中即续命（客户端马上要用它）"


# ---------- 客户端鲜度 ----------

class _Resp:
    def __init__(self, code=200, payload=None):
        self.status_code = code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:   # 不用 assert：python -O 下会蒸发
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.heads, self.posts = [], []
        self.head_code = 404   # 缺省当「服务端还没有」：冷路 HEAD miss 后走上传

    def head(self, url, timeout=None):
        self.heads.append(url)
        return _Resp(self.head_code)

    def post(self, url, files=None, data=None, timeout=None):
        self.posts.append(url)
        return _Resp(200, {"media_id": data["sha256"] + ".png"})


@pytest.fixture
def rb(tmp_path):
    from framework.backends.remote.backend import RemoteBackend
    b = RemoteBackend.__new__(RemoteBackend)
    b.base_url = "http://h:1"
    b._session = _Session()
    b._uploaded_media = {}
    p = tmp_path / "pic.png"
    p.write_bytes(b"img-bytes")
    return b, p


def test_first_upload_then_fresh_cache_zero_network(rb):
    b, p = rb
    mid = b._ensure_media(str(p))
    assert mid and len(b._session.posts) == 1
    assert b._ensure_media(str(p)) == mid
    assert len(b._session.posts) == 1 and len(b._session.heads) == 1, \
        "新鲜缓存必须零网络（HEAD 只发生在首次的冷路上）"


def test_cold_cache_head_hit_skips_full_upload(rb):
    # backend 重启后缓存全空：内容寻址 id 在服务端还在 → 一次 HEAD 就够，
    # 不许把百 MB 音频整份重推（首评 S）
    b, p = rb
    b._session.head_code = 200
    mid = b._ensure_media(str(p))
    assert mid and b._session.posts == [], "HEAD 命中不得重传字节"
    assert len(b._session.heads) == 1


def test_stale_cache_head_200_skips_reupload(rb):
    b, p = rb
    mid = b._ensure_media(str(p))
    b._uploaded_media[mid] -= 7200          # 人为催老
    b._session.head_code = 200
    assert b._ensure_media(str(p)) == mid
    assert len(b._session.heads) == 2 and len(b._session.posts) == 1, \
        "HEAD 200 只续缓存，不重传字节"
    assert b._ensure_media(str(p)) == mid
    assert len(b._session.heads) == 2, "续过之后又是新鲜缓存"


def test_stale_cache_head_404_reuploads(rb):
    b, p = rb
    mid = b._ensure_media(str(p))
    b._uploaded_media[mid] -= 7200
    assert b._ensure_media(str(p)) == mid   # head_code 仍 404 = 服务端 TTL 已清走
    assert len(b._session.posts) == 2, "服务端没有了就必须重传——否则图静默不见"


# ---------- 接线与鉴权 ----------

def test_head_route_is_authenticated(srv):
    # HEAD /v1/media/{id} 是内容存在性预言机，服务可绑非回环 tailnet——必须带鉴权
    route = next(r for r in srv.app.routes
                 if getattr(r, "path", "") == "/v1/media/{media_id}"
                 and "HEAD" in getattr(r, "methods", set()))
    assert any(getattr(d, "dependency", None) is srv.verify_api_key
               for d in route.dependencies), "HEAD 媒体探活端点丢了 Bearer 鉴权"


def test_all_media_usage_sites_touch_source_anchor():
    """源锚：四处「使用媒体」的函数都必须调 _touch_media 续命——续命少一处，
    那条路径反复使用的媒体照样被 TTL 清走，症状只是费带宽，永远没人报错。"""
    import ast
    tree = ast.parse(_SERVER.read_text(encoding="utf-8"))
    must_touch = {"_media_paths", "transcribe", "media_upload", "media_exists"}
    touched = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name in must_touch:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                        and sub.func.id == "_touch_media":
                    touched.add(node.name)
    assert touched == must_touch, f"缺续命的媒体使用点: {must_touch - touched}"
