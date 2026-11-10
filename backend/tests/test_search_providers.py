"""搜索 API 提供方金测试（build 85 批 C：API 优先、随机、SearXNG 兜底；Brave + Tavily 适配器）。

用户原话：「优先使用 API，拿现有的兜底」「多 API 可以随机安排，但是但凡有一个可用的 API，
则优先走 API」。本文件钉死：
① 搜索链：API 成功（含合法 0 条）不请求 SearXNG；顺序来自随机源；冷却跳过与过期；
   auth / quota / Retry-After 的冷却时长；全败回落 SearXNG；全链不可用 → SEARCH_UNAVAILABLE；
   停用 / 无密钥不参与，启用即时生效；
② 适配器：Brave（GET、X-Subscription-Token 头、count / freshness / news 端点、两种响应形态、
   去 HTML）与 Tavily（POST、Bearer 头、body 字段、密钥不进 body、432/433 → quota）；
   与 SearXNG 同一套信誉打分；
③ 密钥不外泄：返回文本、异常 str / repr / args / 异常链、日志与 stdout、工具观察、json 文件；
④ 存储：0600、原子写、损坏文件告警一次、id / 变量名校验、删除即清钥、数据根取法；
⑤ web_research 入口短路与注册判断看整条链。

**桩点纪律**（要过变异证明）：只桩网络出口 `web_tools._session`（get 与 post 分别记录）、
时钟 `web_tools._clock`、随机源 `search_providers._rng`、临时数据根（AGENT_DATA_DIR）。
存储、密钥读写（api_tool_factory 真实执行）、链、适配器、打分、沙箱循环全部真跑。
"""
import asyncio
import json
import logging
import os
import re
import stat
import threading

import pytest
import requests

from framework.agents import research_sandbox, search_providers, web_tools
from framework.agents import runtime as agent_runtime
from framework.mcp.tools import api_tool_factory

from .fake_engine import FakeEngine, call

SEARX = "http://10.9.8.7:18080"
BRAVE_WEB = "https://api.search.brave.com/res/v1/web/search"
BRAVE_NEWS = "https://api.search.brave.com/res/v1/news/search"
TAVILY = "https://api.tavily.com/search"
CFG = {"searxng_url": SEARX, "timeout": 5, "max_results": 8, "max_steps": 6,
       "engines": ["google", "bing"], "impersonate_enabled": False, "render_enabled": False,
       "min_valid_pages": 1}

KEY_A = "test-key-0001-AAAA"
KEY_B = "test-key-0002-BBBB"
KEY_T = "test-key-0003-TTTT"
ALL_KEYS = (KEY_A, KEY_B, KEY_T)

SEARX_DOWN = {"results": [], "unresponsive_engines": [["google", "timeout"]]}
SEARX_HIT = {"results": [{"url": "https://searx.example.com/s", "title": "S",
                          "content": "from searxng", "engine": "bing"}]}
NO_RESULTS = "No search results. Try different keywords, or answer from what you already have."


def brave_web(*urls):
    return {"type": "search", "web": {"results": [
        {"title": f"<strong>T</strong>{i}", "url": u, "description": f"desc <b>{i}</b> &amp; more"}
        for i, u in enumerate(urls)]}}


def tavily_body(*urls):
    return {"query": "q", "results": [
        {"title": f"T{i}", "url": u, "content": f"content {i}", "score": 0.01 * i}
        for i, u in enumerate(urls)]}


# ==========================================================================
# 网络出口替身（get 与 post 分别记录）
# ==========================================================================

class _Resp:
    def __init__(self, status=200, data=None, text="", headers=None):
        self.status_code = status
        self._data = data
        self.text = text
        self.content = text.encode()
        self.headers = dict(headers or {})
        self.headers.setdefault("Content-Type", "application/json")
        self.url = ""

    def json(self):
        if isinstance(self._data, BaseException):
            raise self._data
        return self._data


def _key_of(headers):
    headers = headers or {}
    if "X-Subscription-Token" in headers:
        return headers["X-Subscription-Token"]
    auth = headers.get("Authorization", "")
    return auth[len("Bearer "):] if auth.startswith("Bearer ") else None


class _Egress:
    """api[key] = 回放队列（用尽沿用最后一项）；searx = SearXNG 回放队列；alive = 探活回复。
    calls 记下每一次 API 请求（method, url, params, json, headers, key）。"""

    def __init__(self, api=None, searx=(SEARX_HIT,), alive=None):
        self.api = {k: list(v) for k, v in (api or {}).items()}
        self.searx = list(searx)
        self.alive = alive if alive is not None else _Resp(200, {})
        self.calls = []
        self.searx_calls = 0
        self.lock = threading.Lock()

    @staticmethod
    def _play(queue):
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, _Resp) else _Resp(200, reply)

    def _api(self, method, url, params=None, json=None, headers=None, timeout=None, **kw):
        key = _key_of(headers)
        with self.lock:
            self.calls.append({"method": method, "url": url, "params": dict(params or {}),
                               "json": json, "headers": dict(headers or {}), "key": key,
                               "timeout": timeout, "kw": dict(kw)})
            queue = self.api.get(key)
        assert queue is not None, "request with an unknown key"
        return self._play(queue)

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        if url.startswith(SEARX):
            if url.rstrip("/") == SEARX:
                if isinstance(self.alive, BaseException):
                    raise self.alive
                return self.alive
            with self.lock:
                self.searx_calls += 1
            return self._play(self.searx)
        return self._api("get", url, params=params, headers=headers, timeout=timeout, **kw)

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        assert not url.startswith(SEARX)
        return self._api("post", url, json=json, headers=headers, timeout=timeout, **kw)

    def keys_called(self):
        return [c["key"] for c in self.calls]


@pytest.fixture
def root(monkeypatch, tmp_path):
    """真实数据根取法：AGENT_DATA_DIR → 临时目录（取消 conftest 的隔离覆盖，走 data_dir()）。"""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("AGENT_DATA_DIR", str(data))
    monkeypatch.setattr(search_providers, "_root_override", None)
    for name in list(os.environ):
        if name.startswith(search_providers.SECRET_PREFIX):
            monkeypatch.delenv(name)
    return data


@pytest.fixture
def egress(monkeypatch):
    def install(**kw):
        eg = _Egress(**kw)
        monkeypatch.setattr(web_tools, "_session", lambda: eg)
        return eg
    return install


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(web_tools, "_clock", lambda: now[0])
    return now


class _Order:
    """随机源替身：把候选按给定的密钥顺序排（证明顺序来自随机源而非配置序）。"""

    def __init__(self, *keys):
        self.keys = list(keys)
        self.seen = []

    def shuffle(self, items):
        self.seen.append([it[2] for it in items])
        items.sort(key=lambda it: self.keys.index(it[2]))


@pytest.fixture
def order(monkeypatch):
    def install(*keys):
        o = _Order(*keys)
        monkeypatch.setattr(search_providers, "_rng", o)
        return o
    return install


def _add(ptype, key, label=""):
    return search_providers.add_provider(ptype, label, key)["id"]


def _search(query="tokyo weather", cfg=CFG, **kw):
    return web_tools.web_search(query, cfg, **kw)


def _no_secret(*texts):
    for t in texts:
        for k in ALL_KEYS:
            assert k not in str(t), "secret leaked"


# ==========================================================================
# 1. 链：API 成功 → 不请求 SearXNG；合法 0 条 → 不降级、不冷却
# ==========================================================================

def test_api_success_returns_its_results_without_touching_searxng(root, egress, clock, order):
    a = _add("brave", KEY_A)
    eg = egress(api={KEY_A: [brave_web("https://a.example.com/1", "https://a.example.com/2")]})
    order(KEY_A)
    res = _search()
    assert [r["url"] for r in res] == ["https://a.example.com/1", "https://a.example.com/2"]
    assert {r["engine"] for r in res} == {"brave-api"}
    assert eg.searx_calls == 0 and eg.keys_called() == [KEY_A]
    assert web_tools.format_search_results(res).startswith("Search candidates")
    assert search_providers.status_of(a)[0] == "ok"


@pytest.mark.parametrize("ptype,key,empty", [
    ("brave", KEY_A, {"type": "search", "web": {"results": []}}),
    ("brave", KEY_A, {"type": "search", "query": {"original": "x"}}),   # web 块整块省略
    ("tavily", KEY_T, {"query": "x", "results": []}),
])
def test_api_zero_results_is_final_no_fallback_no_cooldown(root, egress, clock, order,
                                                           ptype, key, empty):
    pid = _add(ptype, key)
    eg = egress(api={key: [empty]})
    order(key)
    res = _search()
    assert res == [] and web_tools.format_search_results(res) == NO_RESULTS
    assert eg.searx_calls == 0                       # 不白耗：不降级到 SearXNG
    assert search_providers.status_of(pid)[0] == "ok"
    _search()
    assert eg.keys_called() == [key, key]            # 未进冷却
    assert eg.searx_calls == 0


# ==========================================================================
# 2. 链：随机顺序、冷却跳过与过期
# ==========================================================================

def test_rate_limited_provider_cools_down_then_returns(root, egress, clock, order):
    a, b = _add("brave", KEY_A), _add("brave", KEY_B)
    eg = egress(api={KEY_A: [_Resp(429, {}), brave_web("https://a.example.com/x")],
                     KEY_B: [brave_web("https://b.example.com/1")]})
    order(KEY_A, KEY_B)
    res = _search()
    assert [r["url"] for r in res] == ["https://b.example.com/1"]
    assert eg.keys_called() == [KEY_A, KEY_B] and eg.searx_calls == 0
    assert search_providers.status_of(a)[0] == "rate_limit"
    assert search_providers.status_of(b)[0] == "ok"

    clock[0] += 59.9                                 # 冷却期内：不请求 A
    _search()
    assert eg.keys_called() == [KEY_A, KEY_B, KEY_B]
    clock[0] += 0.1                                  # 恰到 60 s：恢复候选，按顺序先 A
    res = _search()
    assert eg.keys_called() == [KEY_A, KEY_B, KEY_B, KEY_A]
    assert [r["url"] for r in res] == ["https://a.example.com/x"]


def test_order_comes_from_the_random_source_not_config_order(root, egress, clock, order):
    _add("brave", KEY_A)
    _add("brave", KEY_B)                             # 配置序 A, B
    eg = egress(api={KEY_A: [brave_web("https://a.example.com/1")],
                     KEY_B: [brave_web("https://b.example.com/1")]})
    o = order(KEY_B, KEY_A)
    res = _search()
    assert eg.keys_called() == [KEY_B]               # 先请求 B，A 一次都没请求
    assert res[0]["url"] == "https://b.example.com/1"
    assert o.seen == [[KEY_A, KEY_B]]                # 随机源每次搜索都被调用（拿到配置序）
    _search()
    assert len(o.seen) == 2


def test_default_random_source_shuffles_each_search(root):
    """生产随机源真被用来打乱（不是恒等）：固定种子下 4 个候选的顺序会变化。"""
    for k in ("test-key-r1", "test-key-r2", "test-key-r3", "test-key-r4"):
        _add("brave", k)
    search_providers._rng.seed(7)
    orders = {tuple(c[2] for c in search_providers.candidates()) for _ in range(12)}
    assert len(orders) > 1


def test_mixed_brave_and_tavily_degrade_one_by_one(root, egress, clock, order):
    t, a = _add("tavily", KEY_T), _add("brave", KEY_A)
    eg = egress(api={KEY_T: [_Resp(432, {"detail": {"error": "plan limit"}})],
                     KEY_A: [brave_web("https://a.example.com/1")]})
    order(KEY_T, KEY_A)
    cfg = {**CFG, "search_api_quota_cooldown": 100}
    res = _search(cfg=cfg)
    assert [c["method"] for c in eg.calls] == ["post", "get"]
    assert res[0]["engine"] == "brave-api" and eg.searx_calls == 0
    assert search_providers.status_of(t)[0] == "quota"
    clock[0] += 99.9
    _search(cfg=cfg)
    assert eg.keys_called() == [KEY_T, KEY_A, KEY_A]
    clock[0] += 0.1                                  # quota 冷却按配置 100 s
    eg.api[KEY_T] = [tavily_body("https://t.example.com/1")]
    res = _search(cfg=cfg)
    assert eg.keys_called()[-1] == KEY_T and res[0]["engine"] == "tavily-api"


# ==========================================================================
# 3. 链：全败 → SearXNG；全链不可用 → 标记；没配 API → 批 B 行为
# ==========================================================================

def test_all_apis_fail_then_searxng_serves(root, egress, clock, order):
    _add("brave", KEY_A)
    _add("tavily", KEY_T)
    eg = egress(api={KEY_A: [_Resp(500, {})],
                     KEY_T: [requests.exceptions.ConnectTimeout("t")]},
                searx=[SEARX_HIT])
    order(KEY_A, KEY_T)
    res = _search()
    assert eg.keys_called() == [KEY_A, KEY_T] and eg.searx_calls == 1
    assert [r["url"] for r in res] == ["https://searx.example.com/s"]


def test_all_apis_fail_and_searxng_down_is_unavailable(root, egress, clock, order):
    _add("brave", KEY_A)
    _add("tavily", KEY_T)
    egress(api={KEY_A: [_Resp(503, {})], KEY_T: [_Resp(401, {})]},
           searx=[SEARX_DOWN, {"results": []}])
    order(KEY_A, KEY_T)
    res = _search()
    text = web_tools.format_search_results(res)
    assert text.startswith(web_tools.SEARCH_UNAVAILABLE)
    names = web_tools.search_unavailable_names(res)
    assert names == "brave-api (http), tavily-api (auth), google (timeout)"
    assert "api.search.brave.com" not in text and "tavily.com" not in text and SEARX not in text
    # 整条链：API 都在冷却、SearXNG 上闩 → 入口查询同样判不可用（带 API 类型与 kind）
    assert web_tools.search_unavailable_engines() == \
        "brave-api (http), tavily-api (auth), google (timeout)"


def test_cooling_apis_and_latched_searxng_are_reported_together(root, egress, clock, order):
    """API 全在冷却（本次没尝试）+ SearXNG 闩锁回放 → 标记仍列出 API 的类型与 kind。"""
    _add("brave", KEY_A)
    eg = egress(api={KEY_A: [_Resp(429, {})]}, searx=[SEARX_DOWN, {"results": []}])
    order(KEY_A)
    _search()
    res = _search()                                   # A 冷却中、SearXNG 闩锁中：零请求
    assert eg.keys_called() == [KEY_A] and eg.searx_calls == 2
    assert web_tools.search_unavailable_names(res) == "brave-api (rate_limit), google (timeout)"


def test_without_any_api_behaviour_is_batch_b(root, egress, clock):
    eg = egress(searx=[SEARX_DOWN, {"results": []}])
    res = _search()
    assert web_tools.search_unavailable_names(res) == "google (timeout)"
    assert res[0]["error"] == (
        f"{web_tools.SEARCH_UNAVAILABLE} web search is temporarily unavailable (search engines "
        "not responding: google (timeout)). Answer from the material you already have.")
    assert eg.calls == [] and eg.searx_calls == 2
    assert web_tools.search_unavailable_engines() == "google (timeout)"


# ==========================================================================
# 4. 冷却时长：auth / quota / Retry-After；kind 分类
# ==========================================================================

@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_uses_auth_cooldown(root, egress, clock, order, status):
    a = _add("brave", KEY_A)
    eg = egress(api={KEY_A: [_Resp(status, {})]}, searx=[SEARX_HIT])
    order(KEY_A)
    cfg = {**CFG, "search_api_auth_cooldown": 300, "search_api_cooldown": 5}
    _search(cfg=cfg)
    assert search_providers.status_of(a)[0] == "auth_error"
    clock[0] += 299.9
    _search(cfg=cfg)
    assert eg.keys_called() == [KEY_A]
    clock[0] += 0.1
    _search(cfg=cfg)
    assert eg.keys_called() == [KEY_A, KEY_A]


def test_default_cooldowns_when_config_is_silent(root, egress, clock, order):
    a, t = _add("brave", KEY_A), _add("tavily", KEY_T)
    egress(api={KEY_A: [_Resp(401, {})], KEY_T: [_Resp(433, {})]}, searx=[SEARX_HIT])
    order(KEY_A, KEY_T)
    _search()
    assert search_providers.status_of(t)[0] == "quota"
    clock[0] += 599.9
    assert [c[2] for c in search_providers.candidates()] == []
    clock[0] += 0.1                                   # auth 缺省 600 s
    assert [c[2] for c in search_providers.candidates()] == [KEY_A]
    clock[0] += 3000.0                                # quota 缺省 3600 s
    assert sorted(c[2] for c in search_providers.candidates()) == sorted([KEY_A, KEY_T])


@pytest.mark.parametrize("retry_after,cooldown", [("120", 120.0), ("5000", 600.0), ("10", 60.0),
                                                  ("Wed, 21 Oct 2026 07:28:00 GMT", 60.0)])
def test_retry_after_raises_cooldown_up_to_cap(root, egress, clock, order, retry_after, cooldown):
    _add("brave", KEY_A)
    egress(api={KEY_A: [_Resp(429, {}, headers={"Retry-After": retry_after})]},
           searx=[SEARX_HIT])
    order(KEY_A)
    _search()
    clock[0] += cooldown - 0.1
    assert search_providers.candidates() == []
    clock[0] += 0.1
    assert [c[2] for c in search_providers.candidates()] == [KEY_A]


def test_tavily_retry_after_is_honoured(root, egress, clock, order):
    _add("tavily", KEY_T)
    egress(api={KEY_T: [_Resp(429, {}, headers={"Retry-After": "200"})]}, searx=[SEARX_HIT])
    order(KEY_T)
    _search()
    clock[0] += 199.9
    assert search_providers.candidates() == []
    clock[0] += 0.1
    assert len(search_providers.candidates()) == 1


NOT_JSON = ValueError(f"Expecting value: line 1 column 1 (token {KEY_A})")


@pytest.mark.parametrize("reply,kind,code", [
    (_Resp(401, {}), "auth", 401), (_Resp(403, {}), "auth", 403),
    (_Resp(429, {}), "rate_limit", 429), (_Resp(500, {}), "http", 500),
    (_Resp(503, {}), "http", 503), (_Resp(404, {}), "http", 404),
    (requests.exceptions.ReadTimeout("read timed out"), "network", None),
    (requests.exceptions.ConnectionError("refused"), "network", None),
    (_Resp(200, NOT_JSON), "bad_response", None),
    (_Resp(200, ["not", "a", "dict"]), "bad_response", None),
    (_Resp(200, {"type": "ErrorResponse"}), "bad_response", None),
    (_Resp(200, {"web": {"results": "x"}}), "bad_response", None),
])
def test_brave_failure_kinds(egress, reply, kind, code):
    egress(api={KEY_A: [reply]})
    with pytest.raises(search_providers.ProviderUnavailable) as e:
        search_providers.BraveProvider().search(KEY_A, "q", CFG)
    assert (e.value.kind, e.value.status) == (kind, code)
    assert str(e.value) == (f"{kind} (HTTP {code})" if code else kind)


@pytest.mark.parametrize("reply,kind,code", [
    (_Resp(401, {}), "auth", 401), (_Resp(429, {}), "rate_limit", 429),
    (_Resp(432, {}), "quota", 432), (_Resp(433, {}), "quota", 433),
    (_Resp(400, {}), "http", 400), (_Resp(422, {}), "http", 422),
    (_Resp(500, {}), "http", 500),
    (requests.exceptions.ConnectTimeout("t"), "network", None),
    (_Resp(200, NOT_JSON), "bad_response", None),
    (_Resp(200, {"answer": None}), "bad_response", None),
    (_Resp(200, {"results": None}), "bad_response", None),
])
def test_tavily_failure_kinds(egress, reply, kind, code):
    egress(api={KEY_T: [reply]})
    with pytest.raises(search_providers.ProviderUnavailable) as e:
        search_providers.TavilyProvider().search(KEY_T, "q", CFG)
    assert (e.value.kind, e.value.status) == (kind, code)


@pytest.mark.parametrize("reply,status", [
    (_Resp(429, {}), "rate_limit"), (_Resp(401, {}), "auth_error"), (_Resp(432, {}), "quota"),
    (_Resp(500, {}), "error"), (requests.exceptions.ConnectionError("x"), "error"),
    (_Resp(200, NOT_JSON), "error"),
])
def test_status_recorded_for_settings_page(root, egress, clock, order, reply, status):
    t = _add("tavily", KEY_T)
    egress(api={KEY_T: [reply]}, searx=[SEARX_HIT])
    order(KEY_T)
    _search()
    st, at = search_providers.status_of(t)
    assert st == status and isinstance(at, float)


# ==========================================================================
# 5. 停用 / 无密钥不参与；启用即时生效
# ==========================================================================

def test_disabled_and_secretless_providers_do_not_take_part(root, egress, clock, order):
    a, b = _add("brave", KEY_A), _add("brave", KEY_B)
    eg = egress(api={KEY_A: [brave_web("https://a.example.com/1")],
                     KEY_B: [brave_web("https://b.example.com/1")]}, searx=[SEARX_HIT])
    order(KEY_A, KEY_B)
    search_providers.patch_provider(a, {"enabled": False})
    api_tool_factory.write_secret(search_providers.env_name_for(b), "")   # B 没有密钥
    res = _search()
    assert eg.calls == [] and eg.searx_calls == 1
    assert res[0]["url"] == "https://searx.example.com/s"
    assert search_providers.has_configured_api() is False

    search_providers.patch_provider(a, {"enabled": True})      # 不重启，下一次搜索即参与
    res = _search()
    assert eg.keys_called() == [KEY_A] and res[0]["url"] == "https://a.example.com/1"
    assert eg.searx_calls == 1


# ==========================================================================
# 6. 适配器：请求形态、解析、打分
# ==========================================================================

@pytest.mark.parametrize("time_range,freshness", [("day", "pd"), ("week", "pw"),
                                                  ("month", "pm"), ("year", "py"),
                                                  (None, None), ("decade", None)])
def test_brave_request_shape(egress, time_range, freshness):
    eg = egress(api={KEY_A: [brave_web()]})
    search_providers.BraveProvider().search(KEY_A, "tokyo weather", CFG, time_range=time_range)
    (c,) = eg.calls
    assert (c["method"], c["url"]) == ("get", BRAVE_WEB)
    assert c["headers"] == {"X-Subscription-Token": KEY_A, "Accept": "application/json"}
    expected = {"q": "tokyo weather", "count": 8}
    if freshness:
        expected["freshness"] = freshness
    assert c["params"] == expected
    assert c["timeout"] == 8.0                      # search_api_timeout 缺省，不用 SearXNG 的 timeout
    assert c["kw"] == {"allow_redirects": False}


@pytest.mark.parametrize("n,count", [(None, 8), (3, 3), (50, 20)])
def test_brave_count_and_news_endpoint(egress, n, count):
    eg = egress(api={KEY_A: [{"type": "news", "results": []}]})
    search_providers.BraveProvider().search(KEY_A, "q", CFG, n=n, category="news")
    assert eg.calls[0]["url"] == BRAVE_NEWS and eg.calls[0]["params"]["count"] == count


def test_brave_parses_web_and_news_shapes_and_strips_html(egress):
    eg = egress(api={KEY_A: [brave_web("https://a.example.com/1"),
                             {"type": "news", "results": [
                                 {"title": "<b>N</b> &quot;x&quot;", "url": "https://n.example.com/1",
                                  "description": "x" * 500 + "<strong>", "age": "1h"}]}]})
    rows = search_providers.BraveProvider().search(KEY_A, "q", CFG)
    assert rows == [{"title": "T0", "url": "https://a.example.com/1",
                     "content": "desc 0 & more", "engine": "brave-api"}]
    rows = search_providers.BraveProvider().search(KEY_A, "q", CFG, category="news")
    assert rows[0]["title"] == 'N "x"' and "<" not in rows[0]["content"]
    assert eg.calls[1]["url"] == BRAVE_NEWS


@pytest.mark.parametrize("category,time_range,n", [(None, None, None), ("news", "week", 30),
                                                   ("general", "day", 4), (None, "bogus", 2)])
def test_tavily_request_shape_and_key_only_in_header(egress, category, time_range, n):
    eg = egress(api={KEY_T: [tavily_body()]})
    search_providers.TavilyProvider().search(KEY_T, "tokyo weather", CFG, n=n,
                                             time_range=time_range, category=category)
    (c,) = eg.calls
    assert (c["method"], c["url"]) == ("post", TAVILY)
    assert c["headers"] == {"Authorization": f"Bearer {KEY_T}", "Content-Type": "application/json"}
    expected = {"query": "tokyo weather", "search_depth": "basic",
                "max_results": min(n or 8, 20), "topic": "news" if category == "news" else "general",
                "include_answer": False, "include_raw_content": False}
    if time_range in ("day", "week", "month", "year"):
        expected["time_range"] = time_range
    assert c["json"] == expected
    assert KEY_T not in json.dumps(c["json"]) and "auto_parameters" not in c["json"]
    assert c["params"] == {}


def test_tavily_parses_results_and_ignores_its_score(egress):
    egress(api={KEY_T: [{"results": [
        {"title": "<i>A</i>", "url": "https://a.example.com/", "content": "c" * 600, "score": 0.99},
        {"title": "no url"}, "junk",
        {"title": "B", "url": "javascript:alert(1)", "content": "x"}]}]})
    rows = search_providers.TavilyProvider().search(KEY_T, "q", CFG)
    assert rows == [{"title": "A", "url": "https://a.example.com/", "content": "c" * 600,
                     "engine": "tavily-api"}]


def test_api_results_use_the_same_reputation_scoring_as_searxng(root, egress, clock, order):
    urls = ["https://blog.example.com/x", "https://www.city.go.jp/y", "https://farm.example.net/z",
            "https://www.reuters.com/w"]
    cfg = {**CFG, "content_farms": ["farm.example.net"]}
    searx_payload = {"results": [{"url": u, "title": "t", "content": "c", "engine": "bing"}
                                 for u in urls]}
    eg = egress(api={KEY_A: [brave_web(*urls)], KEY_T: [tavily_body(*urls)]},
                searx=[searx_payload])
    via_searx = {r["url"]: r["score"] for r in _search(cfg=cfg)}
    assert eg.searx_calls == 1
    _add("brave", KEY_A)
    order(KEY_A)
    via_brave = {r["url"]: r["score"] for r in _search(cfg=cfg)}
    search_providers.patch_provider(search_providers.load_providers()[0]["id"], {"enabled": False})
    _add("tavily", KEY_T)
    order(KEY_T)
    hits = _search(cfg=cfg)
    via_tavily = {r["url"]: r["score"] for r in hits}
    assert via_searx == via_brave == via_tavily
    assert via_brave["https://farm.example.net/z"] == 0.1                # 内容农场恒沉底
    assert hits[0]["url"] == "https://www.city.go.jp/y"                    # 信誉 + 位次混合
    assert [h["snippet"] for h in hits] == [f"content {i}" for i in (1, 0, 3, 2)]


# ==========================================================================
# 7. 密钥不外泄
# ==========================================================================

def _leaky_exception():
    return RuntimeError(f"HTTPSConnectionPool(host='api.search.brave.com'): {BRAVE_WEB}?q=x "
                        f"headers={{'X-Subscription-Token': '{KEY_A}', "
                        f"'Authorization': 'Bearer {KEY_T}'}}")


LEAKY_REPLIES = [
    _leaky_exception(),
    requests.exceptions.ConnectionError(f"refused {TAVILY} Bearer {KEY_T} {KEY_A}"),
    _Resp(401, {"error": f"invalid token {KEY_A} {KEY_T}"}, text=f"bad key {KEY_A} {KEY_T}"),
    _Resp(429, {}, text=KEY_A + KEY_T),
    _Resp(432, {}, text=KEY_A + KEY_T),
    _Resp(200, ValueError(f"Expecting value {KEY_A} {KEY_T}"), text=KEY_A),
    _Resp(200, {"error": KEY_A + KEY_T}),
]


@pytest.mark.parametrize("reply", LEAKY_REPLIES)
@pytest.mark.parametrize("adapter,key", [(search_providers.BraveProvider, KEY_A),
                                         (search_providers.TavilyProvider, KEY_T)])
def test_adapter_failures_never_carry_the_key(egress, reply, adapter, key):
    egress(api={key: [reply]})
    with pytest.raises(search_providers.ProviderUnavailable) as e:
        adapter().search(key, "q", CFG)
    exc = e.value
    _no_secret(str(exc), repr(exc), exc.args)
    for host in ("api.search.brave.com", "tavily.com", "Bearer", "Token"):
        assert host not in str(exc) + repr(exc)
    assert exc.__context__ is None and exc.__cause__ is None      # 原异常（含钥）不挂在链上


@pytest.mark.parametrize("reply", LEAKY_REPLIES)
def test_chain_text_logs_and_marker_never_carry_the_key(root, egress, clock, order, capsys,
                                                       caplog, reply):
    caplog.set_level(logging.DEBUG)
    _add("brave", KEY_A)
    _add("tavily", KEY_T)
    egress(api={KEY_A: [reply], KEY_T: [reply]}, searx=[SEARX_DOWN, {"results": []}])
    order(KEY_A, KEY_T)
    res = _search()
    text = web_tools.format_search_results(res)
    assert text.startswith(web_tools.SEARCH_UNAVAILABLE)
    out = capsys.readouterr()
    _no_secret(text, json.dumps(res), web_tools.search_unavailable_engines(), out.out, out.err,
               caplog.text, json.dumps(search_providers.listing()))
    assert "api.search.brave.com" not in text and "tavily.com" not in text


def test_store_file_has_no_key_and_is_0600(root):
    pid = _add("brave", KEY_A, label="home")
    content = (root / search_providers.FILENAME).read_text()
    _no_secret(content)
    assert json.loads(content) == {"version": 1, "providers": [
        {"id": pid, "type": "brave", "label": "home", "enabled": True}]}
    assert stat.S_IMODE((root / search_providers.FILENAME).stat().st_mode) == 0o600
    assert api_tool_factory.read_secret(search_providers.env_name_for(pid)) == KEY_A
    _no_secret(json.dumps(search_providers.listing()))


# ==========================================================================
# 8. 存储
# ==========================================================================

def test_store_lives_in_the_data_root(root):
    assert search_providers.store_path() == root / "search_providers.json"
    pid = _add("tavily", KEY_T)
    assert (root / "api_tools" / ".secrets.env").exists()
    assert search_providers.has_secret(pid)


def test_existing_world_readable_file_is_rewritten_0600(root):
    path = root / search_providers.FILENAME
    path.write_text('{"version": 1, "providers": []}')
    os.chmod(path, 0o644)
    _add("brave", KEY_A)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in root.iterdir()) == ["api_tools", search_providers.FILENAME]


def test_failed_write_keeps_old_file_and_rolls_back_the_new_key(root, monkeypatch):
    pid = _add("brave", KEY_A)
    path = root / search_providers.FILENAME
    before = path.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")
    with monkeypatch.context() as m:
        m.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            search_providers.add_provider("tavily", "", KEY_T)
        with pytest.raises(OSError):
            search_providers.patch_provider(pid, {"enabled": False})
    assert path.read_bytes() == before
    assert sorted(p.name for p in root.iterdir()) == ["api_tools", search_providers.FILENAME]
    assert KEY_T not in (root / "api_tools" / ".secrets.env").read_text()
    assert [p["enabled"] for p in search_providers.load_providers()] == [True]


@pytest.mark.parametrize("content", ["{not json", '{"version": 2, "providers": []}', "[]",
                                     '{"version": 1, "providers": [{"id": "BAD", "type": "brave"}]}',
                                     '{"version": 1, "providers": [{"id": "a", "type": "brave", '
                                     '"enabled": "yes"}]}'])
def test_corrupt_file_means_no_apis_and_warns_once(root, egress, clock, capsys, content):
    (root / search_providers.FILENAME).write_text(content)
    eg = egress(searx=[SEARX_HIT])
    for _ in range(3):
        assert search_providers.load_providers() == []
    res = _search()
    assert res[0]["url"] == "https://searx.example.com/s" and eg.calls == []
    warnings = [ln for ln in capsys.readouterr().out.splitlines() if "[SearchProviders]" in ln]
    assert len(warnings) == 1


def test_ids_and_secret_variable_names(root):
    pid = _add("brave", KEY_A)
    assert re.fullmatch(r"brave-[0-9a-f]{6}", pid)
    assert search_providers.env_name_for("brave-a1b2c3") == "SEARCH_API_KEY_BRAVE_A1B2C3"
    assert search_providers.env_name_for("tavily-00ff00") == "SEARCH_API_KEY_TAVILY_00FF00"
    for bad in ("Brave-a1", "../etc", "a_b", "", "-x", "a" * 50, "a b", "x\n", None):
        with pytest.raises(ValueError):
            search_providers.env_name_for(bad)


@pytest.mark.parametrize("ptype,label,secret", [
    ("google", "", KEY_A), ("brave", "", ""), ("brave", "", "   "),
    ("brave", "", KEY_A + "\nEVIL=1"), ("brave", "", "k" * 513), ("brave", "l" * 65, KEY_A),
    ("brave", "", None), ("brave", 5, KEY_A),
])
def test_add_rejects_bad_input_without_echoing_it(root, ptype, label, secret):
    with pytest.raises(ValueError) as e:
        search_providers.add_provider(ptype, label, secret)
    _no_secret(str(e.value))
    assert "EVIL" not in str(e.value)
    assert search_providers.load_providers() == []


def test_delete_clears_key_entry_and_cooldown(root, egress, clock, order):
    pid = _add("brave", KEY_A)
    egress(api={KEY_A: [_Resp(429, {})]}, searx=[SEARX_HIT])
    order(KEY_A)
    _search()
    assert search_providers.status_of(pid)[0] == "rate_limit"
    assert search_providers.delete_provider(pid) is True
    assert api_tool_factory.read_secret(search_providers.env_name_for(pid)) == ""
    assert search_providers.has_secret(pid) is False
    assert search_providers.load_providers() == []
    assert search_providers.status_of(pid) == ("unused", None)
    assert search_providers.delete_provider(pid) is False
    _no_secret((root / "api_tools" / ".secrets.env").read_text())


def test_new_key_clears_old_cooldown(root, egress, clock, order):
    pid = _add("brave", KEY_A)
    egress(api={KEY_A: [_Resp(401, {})], KEY_B: [brave_web("https://b.example.com/")]},
           searx=[SEARX_HIT])
    order(KEY_A, KEY_B)
    _search()
    assert search_providers.candidates() == []
    search_providers.patch_provider(pid, {"secret": KEY_B})
    assert [c[2] for c in search_providers.candidates()] == [KEY_B]


# ==========================================================================
# 10. web_research：入口短路与注册判断看整条链
# ==========================================================================

class _BgLoop:
    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *a):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


def _run_tool(monkeypatch, eng, query):
    from framework.mcp.tools import web_research as wr
    monkeypatch.setattr(wr, "_CFG", {**CFG, "total_timeout": 60, "convergence_reserve_seconds": 1})
    with _BgLoop() as loop:
        monkeypatch.setattr(agent_runtime, "is_ready", lambda: True)
        monkeypatch.setattr(agent_runtime, "get_engine", lambda: eng)
        monkeypatch.setattr(agent_runtime, "get_loop", lambda: loop)
        monkeypatch.setattr(agent_runtime, "get_progress_hook", lambda: None)
        tool = wr.WebResearchTool.__new__(wr.WebResearchTool)
        return tool.execute(query)


def _latch_searxng():
    web_tools._searxng_search("x", CFG)               # 真实判定上闩（SEARX_DOWN + 确认 0 条）
    assert web_tools._searxng_latched() is not None


def test_latched_searxng_with_usable_api_does_not_short_circuit(root, egress, clock, order,
                                                                monkeypatch):
    _add("brave", KEY_A)
    eg = egress(api={KEY_A: [brave_web("https://a.example.com/1")]},
                searx=[SEARX_DOWN, {"results": []}])
    order(KEY_A)
    _latch_searxng()
    assert web_tools.search_unavailable_engines() is None
    eng = FakeEngine([{"tool_calls": [call("web_search", query="tokyo")]}, {"text": "digest"}])
    out = _run_tool(monkeypatch, eng, "東京の天気")
    assert len(eng.calls) == 2 and "\ndigest\n</research_digest>" in out
    assert eg.keys_called() == [KEY_A] and eg.searx_calls == 2
    obs = eng.calls[1]
    _no_secret(out, repr(obs))


def test_latched_searxng_with_all_apis_cooling_short_circuits(root, egress, clock, order,
                                                              monkeypatch):
    _add("brave", KEY_A)
    egress(api={KEY_A: [_Resp(429, {})]}, searx=[SEARX_HIT, SEARX_DOWN, {"results": []}])
    order(KEY_A)
    _search()                                         # A 进冷却（SearXNG 照常出结果）
    _latch_searxng()
    eng = FakeEngine([{"text": "never"}])
    out = _run_tool(monkeypatch, eng, "東京の天気")
    assert eng.calls == []
    assert out.startswith("System Error: web search is temporarily unavailable")
    assert "brave-api (rate_limit)" in out and "google (timeout)" in out
    _no_secret(out)


@pytest.mark.asyncio
async def test_sandbox_observation_from_failing_apis_has_no_key(root, egress, clock, order):
    _add("brave", KEY_A)
    _add("tavily", KEY_T)
    egress(api={KEY_A: [_leaky_exception()], KEY_T: [_Resp(401, {}, text=KEY_T)]},
           searx=[SEARX_DOWN, {"results": []}])
    order(KEY_A, KEY_T)
    eng = FakeEngine([{"tool_calls": [call("web_search", query="x")]}] * 6 + [{"text": "s"}])
    out = await research_sandbox.run_research(eng, "東京の天気", CFG)
    assert out.startswith("System Error: web search is temporarily unavailable")
    assert "brave-api (network)" in out and "tavily-api (auth)" in out
    _no_secret(out, repr(eng.calls))


@pytest.mark.parametrize("alive", [requests.exceptions.ConnectionError("down"), _Resp(502, {})])
def test_registration_needs_searxng_or_a_configured_api(root, egress, monkeypatch, alive):
    from framework.mcp.tools import web_research as wr
    monkeypatch.setattr(wr, "_CFG", CFG)
    egress(alive=alive)
    tool = wr.WebResearchTool.__new__(wr.WebResearchTool)
    assert tool.is_available() is False               # SearXNG 不可达、未配 API
    pid = _add("brave", KEY_A)
    assert tool.is_available() is True                # 配了 API：照常注册
    search_providers.patch_provider(pid, {"enabled": False})
    assert tool.is_available() is False               # 停用的不算
    search_providers.patch_provider(pid, {"enabled": True})
    api_tool_factory.write_secret(search_providers.env_name_for(pid), "")
    assert tool.is_available() is False               # 无密钥的不算


def test_registration_with_searxng_up_and_no_api(root, egress, monkeypatch):
    from framework.mcp.tools import web_research as wr
    monkeypatch.setattr(wr, "_CFG", CFG)
    egress()
    assert wr.WebResearchTool.__new__(wr.WebResearchTool).is_available() is True


# ==========================================================================
# 11. 一审修补（F1–F6）
# ==========================================================================

@pytest.fixture
def chmod_restore():
    touched = []

    def chmod(path, mode):
        touched.append((path, stat.S_IMODE(path.stat().st_mode)))
        os.chmod(path, mode)
    yield chmod
    for path, mode in reversed(touched):
        os.chmod(path, mode)


def _secrets_file(root):
    return root / "api_tools" / ".secrets.env"


def _writable_check(path):
    if os.access(path, os.W_OK):
        pytest.skip("running with privileges that ignore file modes")


def test_f1_delete_when_clearing_the_key_fails_changes_nothing(root, chmod_restore):
    pid = _add("brave", KEY_A)
    store = root / search_providers.FILENAME
    before = store.read_bytes()
    chmod_restore(_secrets_file(root), 0o400)            # 清钥写不进去
    _writable_check(_secrets_file(root))
    with pytest.raises(OSError):
        search_providers.delete_provider(pid)
    assert store.read_bytes() == before
    assert [p["id"] for p in search_providers.load_providers()] == [pid]


def test_f1_delete_when_saving_fails_leaves_the_entry_without_a_key(root, chmod_restore):
    pid = _add("brave", KEY_A)
    chmod_restore(root, 0o500)                            # 数据根写不进临时文件 → 落盘失败
    _writable_check(root)
    with pytest.raises(OSError):
        search_providers.delete_provider(pid)
    assert [p["id"] for p in search_providers.load_providers()] == [pid]
    assert search_providers.has_secret(pid) is False      # 界面看得到、可重试；钥已不在
    assert search_providers.listing()["providers"][0]["has_secret"] is False


LINE_BREAKERS = ["\x85", "\u2028", "\u2029", "\x9b", "\x1e"]


@pytest.mark.parametrize("ch", LINE_BREAKERS)
def test_f2_line_break_characters_are_rejected_in_keys_and_labels(root, ch):
    evil = f"{KEY_A}{ch}EVIL_VAR=1"
    with pytest.raises(ValueError) as e:
        search_providers.add_provider("brave", "", evil)
    assert "EVIL" not in str(e.value)
    with pytest.raises(ValueError):
        search_providers.add_provider("brave", f"home{ch}x", KEY_A)
    assert search_providers.load_providers() == []
    pid = _add("brave", KEY_B)
    with pytest.raises(ValueError):
        search_providers.patch_provider(pid, {"secret": evil})
    content = _secrets_file(root).read_text(encoding="utf-8")
    assert "EVIL" not in content and KEY_A not in content
    assert api_tool_factory.read_secret(search_providers.env_name_for(pid)) == KEY_B
    assert api_tool_factory.read_secret("EVIL_VAR") == ""


@pytest.mark.parametrize("adapter,key", [(search_providers.BraveProvider, KEY_A),
                                         (search_providers.TavilyProvider, KEY_T)])
def test_f3_redirects_are_never_followed(egress, adapter, key):
    eg = egress(api={key: [_Resp(302, {}, headers={"Location": "https://evil.example/x"})]})
    with pytest.raises(search_providers.ProviderUnavailable) as e:
        adapter().search(key, "q", CFG)
    assert (e.value.kind, e.value.status) == ("http", 302)
    assert len(eg.calls) == 1 and eg.calls[0]["kw"] == {"allow_redirects": False}


def test_f4_corrupt_file_is_backed_up_before_being_overwritten(root, capsys):
    store = root / search_providers.FILENAME
    garbage = b'{"version": 1, "providers": [ <<< not json'
    store.write_bytes(garbage)
    assert search_providers.load_providers() == []
    pid = _add("tavily", KEY_T)
    assert (root / (search_providers.FILENAME + ".corrupt")).read_bytes() == garbage
    assert [p["id"] for p in json.loads(store.read_text())["providers"]] == [pid]
    _no_secret((root / (search_providers.FILENAME + ".corrupt")).read_text())


def test_f4_backup_replaces_an_older_backup_and_happens_once(root):
    store = root / search_providers.FILENAME
    backup = root / (search_providers.FILENAME + ".corrupt")
    backup.write_text("old backup")
    store.write_text("[1, 2, 3]")
    pid = _add("brave", KEY_A)
    assert backup.read_text() == "[1, 2, 3]"
    search_providers.patch_provider(pid, {"label": "x"})   # 新文件正常：不再备份
    assert backup.read_text() == "[1, 2, 3]"


def test_f4_healthy_files_never_produce_a_backup(root):
    pid = _add("brave", KEY_A)
    search_providers.patch_provider(pid, {"enabled": False, "label": "work"})
    _add("tavily", KEY_T)
    search_providers.delete_provider(pid)
    assert not (root / (search_providers.FILENAME + ".corrupt")).exists()


@pytest.mark.parametrize("cfg_value,expected", [(None, 8.0), (3, 3.0), ("2.5", 2.5),
                                                ("abc", 8.0), (0, 8.0), (-1, 8.0)])
@pytest.mark.parametrize("adapter,key", [(search_providers.BraveProvider, KEY_A),
                                         (search_providers.TavilyProvider, KEY_T)])
def test_f5_api_timeout_is_its_own_knob(egress, adapter, key, cfg_value, expected):
    eg = egress(api={key: [_Resp(500, {})]})
    cfg = {**CFG, "timeout": 15}
    if cfg_value is not None:
        cfg["search_api_timeout"] = cfg_value
    with pytest.raises(search_providers.ProviderUnavailable):
        adapter().search(key, "q", cfg)
    assert eg.calls[0]["timeout"] == expected


def test_f6_patch_saves_the_list_before_writing_the_key(root, clock, chmod_restore):
    pid = _add("brave", KEY_A)
    search_providers.note_failure(pid, "auth", CFG)
    assert search_providers.status_of(pid)[0] == "auth_error"
    chmod_restore(root, 0o500)
    _writable_check(root)
    with pytest.raises(OSError):
        search_providers.patch_provider(pid, {"enabled": False, "secret": KEY_B})
    assert api_tool_factory.read_secret(search_providers.env_name_for(pid)) == KEY_A   # 旧钥没变
    assert search_providers.load_providers()[0]["enabled"] is True
    assert search_providers.status_of(pid) == ("unused", None)      # 带了新钥：旧冷却照清
