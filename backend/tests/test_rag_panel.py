"""RAG 管理面板代理金测试（记忆治理波 P1）。

守护的失败形态：①RAG 离线时代理把连接异常炸成 500——面板要的是可识别的 503
离线态；②RAG 侧的教学式拒绝（detail）被吞成笼统错误——用户在面板上看不到原因；
③分类名不过白名单直接拼进代理 URL——路径穿越载荷经 backend 转手进 RAG 侧文件操作。
"""
import asyncio

import pytest
from fastapi import HTTPException

from framework.api import routes


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _patch_request(monkeypatch, handler):
    import requests
    monkeypatch.setattr(requests, "request", handler)


def test_offline_becomes_503(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("refused")
    _patch_request(monkeypatch, boom)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes.rag_overview())
    assert ei.value.status_code == 503


def test_success_payload_passes_through(monkeypatch):
    seen = {}

    def ok(method, url, **kw):
        seen.update(method=method, url=url, params=kw.get("params"))
        return _Resp(payload={"categories": [{"name": "agent_notes"}]})
    _patch_request(monkeypatch, ok)
    out = asyncio.run(routes.rag_documents(category="agent_notes"))
    assert out == {"categories": [{"name": "agent_notes"}]}
    assert seen["method"] == "GET" and seen["params"] == {"category": "agent_notes"}
    assert "/api/v1/document/list" in seen["url"]


def test_error_detail_passes_through(monkeypatch):
    _patch_request(monkeypatch, lambda *a, **k: _Resp(
        status_code=400, payload={"detail": "系统路由表不可删除"}))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes.rag_purge_category("meta_classification_db"))
    assert ei.value.status_code == 400
    assert "系统路由表不可删除" in ei.value.detail


def test_category_name_whitelist_blocks_traversal(monkeypatch):
    def never(*a, **k):
        raise AssertionError("穿越载荷不该到达代理请求")
    _patch_request(monkeypatch, never)
    for bad in ("../etc", "a/b", ""):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(routes.rag_purge_category(bad))
        assert ei.value.status_code == 400


def test_delete_document_requires_source_path(monkeypatch):
    def never(*a, **k):
        raise AssertionError("空 source_path 不该发出请求")
    _patch_request(monkeypatch, never)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes.rag_delete_document({}))
    assert ei.value.status_code == 400
