"""三阈值标定扫参（HETERO B2「阈值重标（不可跳过）」的执行工具）。

背景：config.yaml rag.tiers 各档的 thresholds 只有 text-0.6b 是实测标定值，
其余档位为占位继承——vl-2b 定档（2026-08-08）后生产档实际跑着 0.6B 的阈值。
本工具在当前指针档位（.rag-tier-selection）上对三阈值做网格扫参，用 34 题
固定题集选出该档余弦空间的实测最优组合，写回 config 注释（标定日期+依据）。

用法（rag venv，GPU 机上跑——档位模型要真装载）：
  .venv/bin/python evals/threshold_sweep.py                # 三阈值网格
  .venv/bin/python evals/threshold_sweep.py --multidoc     # multidoc 归因：召回参数扫参

机制：复用 run_retrieval_eval 的 start/seed/judge 全套；插入阈值影响入库路由，
故按 insert 档分建数据根（seed 一次多次复用）；查询侧阈值组合逐个重启服务
（只重载模型不重灌语料）。临时端口/数据根用完即收（--keep 保留排障）。
"""
import argparse
import itertools
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run_retrieval_eval import (  # noqa: E402
    BASE, PORT, RAG_ROOT, REPO_ROOT, _load_jsonl, _wait, run_item, seed,
    summarize)

import os  # noqa: E402


def _resolved_tier_id() -> str:
    sel = REPO_ROOT / ".rag-tier-selection"
    if sel.exists():
        try:
            return json.loads(sel.read_text())["tier"]
        except Exception:
            pass
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    return cfg["rag"]["active_tier"]


def start_server_with(tmp: Path, thresholds: dict,
                      tuning: dict | None = None) -> subprocess.Popen:
    """与 run_retrieval_eval.start_server 同构，但按参注入阈值/召回旋钮。"""
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg.setdefault("rag", {})
    cfg["rag"]["host"] = "127.0.0.1"
    cfg["rag"]["port"] = PORT
    cfg["rag"]["compute"] = {**(cfg["rag"].get("compute") or {}), "host": ""}
    tier_id = _resolved_tier_id()
    for t in cfg["rag"].get("tiers", []):
        if t.get("id") == tier_id:
            t["thresholds"] = dict(thresholds)
            break
    if tuning:
        cfg["tuning"] = {**(cfg.get("tuning") or {}), **tuning}
    cfg_path = tmp / "eval_config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    sel = REPO_ROOT / ".rag-tier-selection"
    if sel.exists():
        (tmp / ".rag-tier-selection").write_text(sel.read_text(encoding="utf-8"),
                                                 encoding="utf-8")
    env = {**os.environ, "RAG_CONFIG": str(cfg_path),
           "RAG_DATA_DIR": str(tmp / "data"),
           "RAG_COMPUTE_HOST": "", "RAG_API_KEY": ""}
    proc = subprocess.Popen([str(RAG_ROOT / ".venv/bin/python"), "rag_server.py"],
                            cwd=str(RAG_ROOT), env=env,
                            stdout=(tmp / "server.log").open("a"),
                            stderr=subprocess.STDOUT)
    try:
        _wait(lambda: requests.get(f"{BASE}/health", timeout=2).json()
              .get("status") == "healthy",
              timeout=240, what="rag_server /health（含模型加载）")
    except Exception:
        proc.terminate()
        print((tmp / "server.log").read_text(encoding="utf-8")[-3000:],
              file=sys.stderr)
        raise
    return proc


def _stop(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(20)
    except subprocess.TimeoutExpired:
        proc.kill()
    time.sleep(1)


def eval_combo(tmp: Path, thresholds: dict, items: list, corpus: list,
               tuning: dict | None = None, need_seed: bool = False) -> dict:
    proc = start_server_with(tmp, thresholds, tuning)
    try:
        if need_seed:
            seed(corpus)
        rows = [run_item(it) for it in items]
        s = summarize(rows)
        s["fails"] = [{"id": r["id"], "why": r["why"]} for r in rows if not r["pass"]]
        return s
    finally:
        _stop(proc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--multidoc", action="store_true",
                    help="multidoc 归因模式：固定现行阈值，扫召回参数")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    items = _load_jsonl(HERE / "items.jsonl")
    corpus = _load_jsonl(HERE / "corpus.jsonl")
    tier = _resolved_tier_id()
    print(f"== 档位: {tier}")
    results = []

    if args.multidoc:
        # 归因：multidoc 形态全档归零是否召回名额不足所致
        base_thr = {"insert_routing_max_distance": 0.8,
                    "query_routing_max_distance": 0.85, "rerank_min_score": 0.5}
        md_items = [it for it in items if it["kind"] == "multidoc"]
        tmp = Path(tempfile.mkdtemp(prefix="rag-md-"))
        try:
            grids = [{}, {"query_max_dbs": 3}, {"query_max_chunks": 8},
                     {"query_max_dbs": 3, "query_max_chunks": 8},
                     {"query_max_chunks": 12}]
            first = True
            for tun in grids:
                s = eval_combo(tmp, base_thr, md_items, corpus,
                               tuning=tun, need_seed=first)
                first = False
                results.append({"tuning": tun, **s})
                print(f"tuning={tun} -> multidoc {s['overall']['pass_rate']} "
                      f"fails={s['fails']}")
        finally:
            if not args.keep:
                shutil.rmtree(tmp, ignore_errors=True)
    else:
        insert_grid = [0.75, 0.8, 0.85]
        query_grid = [0.8, 0.85, 0.9]
        rerank_grid = [0.3, 0.4, 0.5, 0.6]
        for ins in insert_grid:
            tmp = Path(tempfile.mkdtemp(prefix=f"rag-thr-{ins}-"))
            try:
                first = True
                for qry, rr in itertools.product(query_grid, rerank_grid):
                    thr = {"insert_routing_max_distance": ins,
                           "query_routing_max_distance": qry,
                           "rerank_min_score": rr}
                    s = eval_combo(tmp, thr, items, corpus, need_seed=first)
                    first = False
                    row = {"thresholds": thr,
                           "pass_rate": s["overall"]["pass_rate"],
                           "mrr": s["overall"]["mrr"],
                           "by_kind": {k: v["pass_rate"]
                                       for k, v in s["by_kind"].items()},
                           "fails": s["fails"]}
                    results.append(row)
                    print(f"ins={ins} qry={qry} rr={rr} -> "
                          f"pass {row['pass_rate']} mrr {row['mrr']}")
            finally:
                if not args.keep:
                    shutil.rmtree(tmp, ignore_errors=True)
        results.sort(key=lambda r: (r["pass_rate"], r["mrr"]), reverse=True)
        print("\n== TOP 5 ==")
        for r in results[:5]:
            print(json.dumps(r, ensure_ascii=False))

    out = HERE / "results" / f"sweep_{tier}_{int(time.time())}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n产物: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
