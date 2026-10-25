"""CPU-only timing regressions execute the real query/HTTP wrappers, no models or DB.

The diagnostics must still identify a stuck phase, preserve fail-open retrieval
semantics, correlate remote primitives and exclude private inputs/error text.
"""
import ast
import json
import logging
import re
import threading
import uuid
from collections.abc import Sized
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = "PRIVATE-query-document-category-token"


def load_actual(filename, names, namespace):
    tree = ast.parse((ROOT / filename).read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
             and n.name in names]
    assert {n.name for n in nodes} == set(names)
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
    if "_query_stage" in names:
        namespace["_query_stage"] = contextmanager(namespace["_query_stage"])
    return namespace


class Clock:
    def __init__(self):
        self.value = 0.0
        self.lock = threading.Lock()

    def perf_counter(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds


@pytest.fixture
def query(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    pool = ThreadPoolExecutor(max_workers=1)

    class Collection:
        def __init__(self, name):
            self.name = name

        def count(self):
            return 0 if self.name == "meta_classification_db" else 1

        def query(self, **kw):
            clock.advance(.125)
            # 块 id 与库名是检索内部标识（原日志行就打它们），只有文档正文是私有
            return {"ids": [["vector-id"]], "documents": [[PRIVATE]], "metadatas": [[{}]]}

    class Compute:
        def ensure_ready(self):
            clock.advance(.010)

        def embed(self, texts):
            clock.advance(.200)
            return [[1.] for _ in texts]

        def rerank(self, query, docs):
            clock.advance(.300)
            return [.9 for _ in docs]

    def collection(name):
        clock.advance(.050)
        return Collection(name)

    ns = dict(time=clock, json=json, uuid=uuid, logger=logging.getLogger("timing-query-test"),
              _QUERY_TRACE=ContextVar("test-query-trace", default=None), List=List, Dict=Dict,
              Optional=Optional, QueryRequest=SimpleNamespace, as_completed=as_completed,
              QUERY_POOL=pool, COMPUTE=Compute(), get_chroma_collection=collection,
              _usable_category=lambda _: True, _SUBQUERY_MIN_TERMS=3, _CONTAINS_LIMIT_PER_TOKEN=5,
              THRESHOLDS={"query_routing_max_distance": .85, "rerank_min_score": .5},
              FTS=SimpleNamespace(search=lambda *a, **kw: []),
              extract_identifiers=lambda *a: [], mmr_select=lambda docs, k: docs[:k])
    load_actual("rag/rag_server.py", ["_query_log", "_query_stage", "_exc_brief", "get_embeddings",
        "_query_single_db", "_query_db_from_pool", "advanced_query_rag", "_advanced_query_rag",
        "split_subquery_terms", "derive_anchor", "_contains_prefetch", "_RemoteCompute"], ns)
    yield SimpleNamespace(ns=ns, clock=clock, caplog=caplog)
    pool.shutdown(wait=True)


def records(caplog, event="rag_query_stage"):
    result = []
    for record in caplog.records:
        try:
            data = json.loads(record.getMessage())
        except (ValueError, TypeError):
            continue
        if data.get("event") == event:
            result.append(data)
    return result


def request():
    return SimpleNamespace(query=PRIVATE, category=PRIVATE, optimized_query=PRIVATE,
                           top_k_dbs=3, chunk_k_per_db=4, rerank_top_k=3)


def test_complete_query_logs_stages_and_keeps_response(query):
    result = query.ns["advanced_query_rag"](request())
    assert result["original_query"] == PRIVATE
    assert result["results"][0]["document"] == PRIVATE
    rows = records(query.caplog)
    phases = {r["phase"] for r in rows}
    assert {"query", "compute_ready", "embed_route_query", "route_collections", "vector_retrieval",
            "vector_pool_wait", "vector_collection_open", "vector_collection_query",
            "identifier_prefetch", "fts_retrieval", "subquery_retrieval", "rerank",
            "anchor_filter", "mmr_select"} <= phases
    end = next(r for r in rows if r["phase"] == "query" and r["state"] == "end")
    assert end["result_count"] == end["retrieved_count"] == 1
    assert end["elapsed_ms"] == pytest.approx(735.)
    assert len({r["request_id"] for r in rows}) == 1
    assert re.fullmatch(r"[0-9a-f]{32}", end["request_id"])
    assert query.ns["_QUERY_TRACE"].get() is None
    assert PRIVATE not in query.caplog.text


def test_all_retrieval_legs_and_anchor_rescue_keep_results_and_private_text_out(query):
    original = query.ns["get_chroma_collection"]
    def collection(name):
        col = original(name)
        col.get = lambda **kw: {
            "ids": ["fts-id" if "ids" in kw else "identifier-id"],
            "documents": [PRIVATE], "metadatas": [{}]}
        return col
    query.ns["get_chroma_collection"] = collection
    query.ns["extract_identifiers"] = lambda *a: [PRIVATE]
    query.ns["FTS"].search = lambda *a, **kw: [{"db": "synthetic_fts_db", "chunk_id": "fts-id"}]
    query.ns["THRESHOLDS"].update(subject_veto_margin=.08, subquery_corroborate_score=.4,
        anchor_min_score=.3, subquery_rescue_score=.5)
    calls = []
    def rerank(text, docs):
        calls.append((text, len(docs)))
        if text == PRIVATE:
            return [.1, .55, .9]
        if text == "coffee" and len(docs) == 2:
            return [.1, .9]  # marginal vetoed; subquery candidate can be rescued
        if text == "coffee descale warranty":
            return [.8]
        return [.1 for _ in docs]
    query.ns["COMPUTE"].rerank = rerank
    req = request()
    req.optimized_query = "coffee descale warranty"
    result = query.ns["advanced_query_rag"](req)
    assert [(item["id"], item["rerank_score"]) for item in result["results"]] == [
        ("fts-id", .9), ("vector-id", .8)]
    assert result["total_chunks_retrieved"] == 3
    assert len(calls) == 6  # main, anchor, 3 corroboration rounds, rescue
    assert all(not any(k.startswith("_") for k in item) for item in result["results"])
    rows = records(query.caplog)
    phases = {r["phase"] for r in rows}
    assert {"identifier_collection_open", "identifier_collection_scan", "fts_collection_fetch",
            "embed_subqueries", "rerank_anchor", "rerank_corroborate", "rerank_rescue",
            "anchor_veto", "anchor_rescue"} <= phases
    # 阈值校准证据：否决/救援必须带块 id 与各口径分数（旧日志行打的就是这些），
    # 否则没法回答「这条阈值该不该动」；锚点词/子词派生自用户查询，不许入日志。
    veto = next(r for r in rows if r["phase"] == "anchor_veto")
    assert veto["chunk_id"] == "identifier-id"
    assert veto["sentence_score"] == .55 and veto["anchor_score"] == .1
    rescue = next(r for r in rows if r["phase"] == "anchor_rescue")
    assert rescue["chunk_id"] == "vector-id" and rescue["sentence_score"] == .1
    assert rescue["subword_score"] == .8 and rescue["anchor_score"] == .9
    assert rescue["term_count"] == 3
    # 库名可记（原日志行就带），并且每条带库的记录都认得出自己那一路
    assert {r["db"] for r in rows if r["phase"] == "fts_collection_fetch"} == {"synthetic_fts_db"}
    assert {r["db"] for r in rows if r["phase"] == "identifier_collection_scan"} == {"unclassified_db"}
    assert PRIVATE not in query.caplog.text and "coffee" not in query.caplog.text


def test_span_ids_pair_parallel_phase_records(query):
    query.ns["advanced_query_rag"](request())
    rows = [r for r in records(query.caplog) if "span_id" in r]
    spans = {r["span_id"] for r in rows}
    assert spans
    for span_id in spans:
        span = [r for r in rows if r["span_id"] == span_id]
        assert [r["state"] for r in span] == ["start", "end"]
        assert len({r["phase"] for r in span}) == 1


@pytest.mark.parametrize("failure", ["ensure_ready", "embed", "rerank"])
def test_error_has_elapsed_and_cleans_trace_without_logging_message(query, failure):
    def fail(*args):
        query.clock.advance(.400)
        raise RuntimeError(PRIVATE)
    setattr(query.ns["COMPUTE"], failure, fail)
    with pytest.raises(RuntimeError, match=PRIVATE):
        query.ns["advanced_query_rag"](request())
    rows = records(query.caplog)
    errors = [r for r in rows if r["state"] == "error"]
    assert len(errors) >= 2  # failing stage + total query
    assert all(r["error_type"] == "RuntimeError" for r in errors)
    assert all(r["elapsed_ms"] >= 400 for r in errors)
    assert not any(r["phase"] == "query" and r["state"] == "end" for r in rows)
    assert query.ns["_QUERY_TRACE"].get() is None
    assert PRIVATE not in query.caplog.text


def test_worker_wait_is_separate_and_worker_context_is_reset(query):
    query.clock.advance(.250)
    result = query.ns["_query_db_from_pool"]("a" * 32, 0., "synthetic_db", [1.], 4)
    rows = records(query.caplog)
    wait = next(r for r in rows if r["phase"] == "vector_pool_wait")
    assert wait["elapsed_ms"] == 250
    assert {r["db"] for r in rows if r["phase"] == "vector_collection_query"} == {"synthetic_db"}
    assert result[0]["document"] == PRIVATE
    assert query.ns["_QUERY_TRACE"].get() is None
    query.caplog.clear()
    query.ns["_query_db_from_pool"]("b" * 32, query.clock.perf_counter(), "synthetic_db", [1.], 4)
    assert {r["request_id"] for r in records(query.caplog)} == {"b" * 32}
    assert PRIVATE not in query.caplog.text


def test_route_rejection_keeps_db_and_distance_for_threshold_calibration(query):
    original = query.ns["get_chroma_collection"]
    def collection(name):
        col = original(name)
        if name == "meta_classification_db":
            col.count = lambda: 2
            col.query = lambda **kw: {"ids": [["near_db", "far_db"]],
                                      "distances": [[.2, .97]]}
        return col
    query.ns["get_chroma_collection"] = collection
    result = query.ns["advanced_query_rag"](request())
    assert result["scanned_dbs"] == ["near_db", "unclassified_db"]
    rejected = [r for r in records(query.caplog) if r["state"] == "rejected"]
    assert [(r["phase"], r["db"], r["distance"]) for r in rejected] == [
        ("route_collections", "far_db", .97)]
    assert PRIVATE not in query.caplog.text


def test_collection_failure_stays_fail_open_but_is_observable(query):
    original = query.ns["get_chroma_collection"]
    def collection(name):
        if name != "meta_classification_db":
            query.clock.advance(.600)
            raise RuntimeError("synthetic-chroma-detail")
        return original(name)
    query.ns["get_chroma_collection"] = collection
    result = query.ns["advanced_query_rag"](request())
    assert result["results"] == []
    error = next(r for r in records(query.caplog)
                 if r["phase"] == "vector_collection_open" and r["state"] == "error")
    assert error["elapsed_ms"] == 600
    assert error["error_type"] == "RuntimeError" and error["db"] == "unclassified_db"
    # 结构化记录只说「这路跳了」；排障要的是「谁跳的、第三方说了什么」——
    # 这一行（原 logger.error）不许被阶段记录顶替掉。
    line = next(r.getMessage() for r in query.caplog.records
                if r.levelname == "ERROR" and "db=unclassified_db" in r.getMessage())
    assert "RuntimeError: synthetic-chroma-detail" in line
    assert PRIVATE not in query.caplog.text


@pytest.mark.parametrize("leg,prepare", [
    ("vector", "query"), ("identifier", "prefetch"), ("fts", "fetch")])
def test_every_fail_open_leg_logs_bounded_exception_detail(query, leg, prepare):
    detail = "synthetic-chroma-detail-" + "x" * 500
    original = query.ns["get_chroma_collection"]
    def collection(name):
        if name == "meta_classification_db":
            return original(name)
        col = original(name)
        def explode(**kw):
            raise RuntimeError(detail)
        if leg == "vector":
            col.query = explode
        else:
            col.get = explode
        return col
    query.ns["get_chroma_collection"] = collection
    tokens = ["synthetic-token"] if leg == "identifier" else []
    query.ns["extract_identifiers"] = lambda *a: tokens
    hits = [{"db": "unclassified_db", "chunk_id": "fts-id"}] if leg == "fts" else []
    query.ns["FTS"].search = lambda *a, **kw: hits
    query.ns["advanced_query_rag"](request())
    lines = [r.getMessage() for r in query.caplog.records
             if "db=unclassified_db: " in r.getMessage()]
    assert lines, "fail-open 的每一路都必须留下一行可读的失败原因"
    brief = lines[0].split("db=unclassified_db: ", 1)[1]
    if leg == "identifier":
        # 过滤值是用户查询里的 token，驱动错误会回显它：这一路只记类型
        assert brief == "RuntimeError"
    else:
        assert brief.startswith("RuntimeError: synthetic-chroma-detail-")
        assert len(brief) == 300   # 截断：驱动会把整段 SQL/堆栈塞进消息里
    assert detail not in query.caplog.text


def test_subquery_leg_failure_is_logged_as_a_warning(query):
    calls = []
    original = query.ns["get_embeddings"]
    def embeddings(texts):
        calls.append(texts)
        if len(calls) > 1:                     # the route/full-sentence embed succeeds
            raise RuntimeError("synthetic-subquery-detail")
        return original(texts)
    query.ns["get_embeddings"] = embeddings
    query.ns["split_subquery_terms"] = lambda q: ["alpha", "beta", "gamma"]
    query.ns["THRESHOLDS"].update(subject_veto_margin=.08, subquery_corroborate_score=.4,
        anchor_min_score=.3, subquery_rescue_score=.5)   # the tier defaults the anchor step reads
    query.ns["advanced_query_rag"](request())
    warnings = [r.getMessage() for r in query.caplog.records if r.levelname == "WARNING"]
    assert any("RuntimeError: synthetic-subquery-detail" in w for w in warnings), warnings


def test_remote_headers_correlate_primitive_and_never_log_auth(query):
    sent = []
    def post(url, **kw):
        sent.append(kw)
        query.clock.advance(.250)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: {"embeddings": [[1.]]})
    query.ns.update(threading=threading, requests=SimpleNamespace(post=post), HTTPException=HTTPException)
    remote = query.ns["_RemoteCompute"]("synthetic", 14162)
    remote._headers = lambda: {"Authorization": "Bearer " + PRIVATE}
    remote.ensure_ready = lambda: {}
    token = query.ns["_QUERY_TRACE"].set("c" * 32)
    try:
        assert remote.embed([PRIVATE]) == [[1.]]
    finally:
        query.ns["_QUERY_TRACE"].reset(token)
    headers = sent[0]["headers"]
    assert headers["X-RAG-Trace-ID"] == "c" * 32
    assert re.fullmatch(r"[0-9a-f]{32}", headers["X-RAG-Call-ID"])
    row = next(r for r in records(query.caplog) if r["state"] == "end")
    assert row["call_id"] == headers["X-RAG-Call-ID"]
    assert row["elapsed_ms"] == 250 and row["input_count"] == 1
    assert PRIVATE not in query.caplog.text


@pytest.fixture
def compute(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    core = SimpleNamespace(embed_texts=lambda texts: [[1.] for _ in texts],
                           token_counts=lambda texts, mode: [1 for _ in texts],
                           rerank_scores=lambda query, docs, instruct: [.9 for _ in docs])
    ns = dict(time=clock, json=json, re=re, uuid=uuid, Request=Request, Optional=Optional,
              List=List, BaseModel=BaseModel, Sized=Sized, core=core,
              logger=logging.getLogger("timing-compute-test"))
    load_actual("rag/compute_server.py", ["_diagnostic_id", "trace_compute_request", "_run_compute",
        "EmbedRequest", "TokenizeRequest", "RerankRequest", "embed", "tokenize", "rerank"], ns)
    app = FastAPI()
    app.middleware("http")(ns["trace_compute_request"])
    for name in ["embed", "tokenize", "rerank"]:
        app.post("/api/v1/" + name)(ns[name])
    yield SimpleNamespace(ns=ns, clock=clock, core=core, client=TestClient(app), caplog=caplog)


@pytest.mark.parametrize("operation,payload,key", [
    ("embed", {"texts": [PRIVATE]}, "embeddings"),
    ("tokenize", {"texts": [PRIVATE]}, "counts"),
    ("rerank", {"query": PRIVATE, "documents": [PRIVATE], "instruct": PRIVATE}, "scores"),
])
def test_compute_http_logs_correlated_calls_without_payload(compute, operation, payload, key):
    response = compute.client.post("/api/v1/" + operation, json=payload,
        headers={"X-RAG-Trace-ID": "d" * 32, "X-RAG-Call-ID": "e" * 32,
                 "Authorization": "Bearer " + PRIVATE})
    assert response.status_code == 200 and len(response.json()[key]) == 1
    rows = records(compute.caplog, "rag_compute_stage")
    assert [r["state"] for r in rows] == ["start", "end"]
    assert all(r["request_id"] == "d" * 32 and r["call_id"] == "e" * 32 for r in rows)
    assert rows[0]["input_count"] == rows[1]["result_count"] == 1
    assert PRIVATE not in compute.caplog.text


def test_compute_dispatch_wait_and_failure_elapsed_are_accurate(compute):
    def fail(*args):
        compute.clock.advance(.450)
        raise RuntimeError(PRIVATE)
    compute.core.embed_texts = fail
    req = SimpleNamespace(state=SimpleNamespace(compute_arrival=0., compute_trace_id="f" * 32,
                                                compute_call_id="a" * 32))
    compute.clock.advance(.200)
    with pytest.raises(RuntimeError, match=PRIVATE):
        compute.ns["embed"](SimpleNamespace(texts=[PRIVATE]), req)
    rows = records(compute.caplog, "rag_compute_stage")
    assert rows[0]["dispatch_ms"] == 200
    assert rows[1]["state"] == "error" and rows[1]["elapsed_ms"] == 450
    assert rows[1]["error_type"] == "RuntimeError"
    assert PRIVATE not in compute.caplog.text


def test_missing_arrival_state_drops_the_timing_field_instead_of_failing(compute):
    # 诊断字段缺失（中间件没跑过）不许把成功的计算请求打成 500。
    req = SimpleNamespace(state=SimpleNamespace(compute_trace_id="f" * 32,
                                                compute_call_id="a" * 32))
    assert compute.ns["embed"](SimpleNamespace(texts=[PRIVATE]), req) == {"embeddings": [[1.]]}
    rows = records(compute.caplog, "rag_compute_stage")
    assert [r["state"] for r in rows] == ["start", "end"]
    assert "dispatch_ms" not in rows[0]
    assert rows[1]["result_count"] == 1
    assert PRIVATE not in compute.caplog.text


def test_unsized_core_result_is_logged_without_result_count(compute):
    compute.core.embed_texts = lambda texts: (row for row in [[1.]])   # 无 len() 的生成器
    req = SimpleNamespace(state=SimpleNamespace(compute_arrival=0., compute_trace_id="f" * 32,
                                                compute_call_id="a" * 32))
    result = compute.ns["embed"](SimpleNamespace(texts=[PRIVATE]), req)
    rows = records(compute.caplog, "rag_compute_stage")
    assert [r["state"] for r in rows] == ["start", "end"]
    assert "result_count" not in rows[1]
    assert list(result["embeddings"]) == [[1.]]


def test_untrusted_trace_headers_are_replaced(compute):
    response = compute.client.post("/api/v1/embed", json={"texts": [PRIVATE]},
                                   headers={"X-RAG-Trace-ID": PRIVATE, "X-RAG-Call-ID": PRIVATE})
    assert response.status_code == 200
    rows = records(compute.caplog, "rag_compute_stage")
    assert all(re.fullmatch(r"[0-9a-f]{32}", r["request_id"]) and
               re.fullmatch(r"[0-9a-f]{32}", r["call_id"]) for r in rows)
    assert PRIVATE not in compute.caplog.text
