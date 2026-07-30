"""检索评估题库金测试（数据架构采纳波 R1）：夹具语料与固定题集的引用完整性。

守的是什么：评估集是检索侧一切优化的防负优化基线——题集引用了不存在的文档
title、分类对不上、id 重复，跑批会静默给出失真的通过率。零服务依赖，纯文件校验
（rag 独立 venv，按文件路径装载，与 test_log_rotate_shell 同惯例）。
"""
import importlib.util
import json
from pathlib import Path

EVALS = Path(__file__).resolve().parents[2] / "rag" / "evals"

_KINDS = {"semantic", "identifier", "name", "crosslingual", "multidoc", "temporal", "negative"}


def _load_jsonl(name):
    out = []
    for line in (EVALS / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out


def _runner_module():
    spec = importlib.util.spec_from_file_location(
        "run_retrieval_eval", EVALS / "run_retrieval_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_corpus_titles_unique_and_categories_known():
    corpus = _load_jsonl("corpus.jsonl")
    assert len(corpus) >= 14
    titles = [d["title"] for d in corpus]
    assert len(titles) == len(set(titles)), "语料 title 重复（title 是判分锚点）"
    cats = _runner_module().CATEGORY_DESCRIPTIONS
    assert {d["category"] for d in corpus} <= set(cats), "语料分类不在跑批建类清单内"
    assert all(len(d["text"]) > 50 for d in corpus)


def test_items_reference_existing_titles():
    """题集引用的每个 title 必须真实存在——引用错字 = 该题永远失败且原因隐蔽。"""
    corpus_titles = {d["title"] for d in _load_jsonl("corpus.jsonl")}
    items = _load_jsonl("items.jsonl")
    assert len(items) >= 30
    ids = [it["id"] for it in items]
    assert len(ids) == len(set(ids)), "题 id 重复"
    for it in items:
        assert it["kind"] in _KINDS, f"{it['id']} 未知 kind {it['kind']}"
        exp = it["expect"]
        referenced = (exp.get("hit_any") or []) + (exp.get("hit_all") or []) \
            + (exp.get("miss_all") or []) + (exp.get("stale_not_top1") or [])
        for t in referenced:
            assert t in corpus_titles, f"{it['id']} 引用了不存在的文档 title: {t!r}"
        assert referenced or exp.get("empty_ok"), f"{it['id']} expect 没有任何判分锚点"
        assert it["query"] and it["category"] and it["optimized_query"]


def test_kind_coverage_and_bilingual():
    """形态覆盖纪律：七类形态齐 + 跨语题存在（检索面是中英双语的）。"""
    items = _load_jsonl("items.jsonl")
    assert {it["kind"] for it in items} == _KINDS
    assert sum(1 for it in items if it["kind"] == "negative") >= 2
    assert sum(1 for it in items if it["kind"] == "identifier") >= 5


def test_runner_judgement_vocabulary_pinned():
    """判分词汇钉死：expect 只认这五个键（拼错静默放行=没判）。"""
    allowed = {"hit_any", "hit_all", "miss_all", "empty_ok", "stale_not_top1", "top_k"}
    for it in _load_jsonl("items.jsonl"):
        unknown = set(it["expect"]) - allowed
        assert not unknown, f"{it['id']} expect 含未知键 {unknown}"
