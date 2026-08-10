"""网页抽取崩溃围堵（web_tools 抽取工人池）的两条纪律。

背景：lxml/libxml2 解析畸形页面的原生崩溃（SIGABRT）进程内不可捕获，抽取挪进
spawn 子进程围堵（2026-08-10 实案）。本文件钉住随案补的两条纪律：
① 回收补刀——卡死的工人不会因 shutdown(wait=False) 退出，必须 kill，否则每次
   超时泄漏一个常驻工人进程；
② 工人被崩溃带走后，_extract 只折损本页（crash-contained 错误），下一页自动
   拿到新工人照常工作——绝不升级为后端事故。

真 spawn 真 trafilatura（秒级），不 mock 工人池本体。
"""
import time

import pytest

from framework.agents import web_tools


@pytest.fixture(autouse=True)
def _fresh_pool():
    """每例前后都回收，杜绝跨测试（以及跨向真实运行）的工人残留。"""
    web_tools._recycle_extract_pool()
    yield
    web_tools._recycle_extract_pool()


def _live_workers(pool):
    return [p for p in getattr(pool, "_processes", {}).values() if p.is_alive()]


def test_recycle_kills_hung_worker():
    pool = web_tools._extract_pool()
    # 模拟卡死：让唯一工人睡长觉（真实场景=原生解析死循环，shutdown 叫不动它）
    pool.submit(time.sleep, 120)
    deadline = time.time() + 15
    while not _live_workers(pool) and time.time() < deadline:
        time.sleep(0.1)   # spawn 工人上线需要约 1s
    procs = _live_workers(pool)
    assert procs, "工人未上线，测试前提不成立"

    web_tools._recycle_extract_pool()

    deadline = time.time() + 5
    while any(p.is_alive() for p in procs) and time.time() < deadline:
        time.sleep(0.1)
    assert not any(p.is_alive() for p in procs), "回收后仍有存活工人=进程泄漏"


def test_extract_survives_worker_crash_and_recovers():
    cfg = {"per_page_max_chars": 500}
    html = "<html><body><article><p>" + "围堵纪律实测正文。" * 20 + "</p></article></body></html>"

    # 先正常抽一次，确保工人在岗
    ok = web_tools._extract("http://a.example/", html, cfg, tier=1)
    assert ok["error"] is None

    # 外力击杀工人 = 模拟原生崩溃把工人带走
    pool = web_tools._extract_pool()
    for p in _live_workers(pool):
        p.kill()
    time.sleep(0.5)

    # 本页只折损自己：crash-contained 错误、空文本、不抛异常
    hit = web_tools._extract("http://b.example/", html, cfg, tier=1)
    assert hit["error"] and "crash-contained" in hit["error"]
    assert hit["text"] == ""

    # 下一页拿到新工人照常工作
    back = web_tools._extract("http://c.example/", html, cfg, tier=1)
    assert back["error"] is None
    assert "围堵纪律实测正文" in back["text"]
