"""检索质量评估跑批（数据架构采纳波 R1）：防负优化基线。

用法（rag venv）：
  .venv/bin/python evals/run_retrieval_eval.py [--save-baseline] [--keep]

机制：隔离数据根（临时目录）+ 融合模式 0.6b 档拉起真实 rag_server（部署同款 HTTP 面），
灌入 corpus.jsonl 夹具语料（强制分类保证确定性），跑 items.jsonl 固定题集，按命中
文档 title 判分（Recall / MRR / 分形态通过率）。产物 results/<时间戳>.json（不入库），
与 BASELINE.json（入库）对比打印增减——**任何检索侧改动后先跑这个**，变差即回退或解释。

判分词汇（items.jsonl expect）：
  hit_any        列表内至少一篇进 top-k
  hit_all        全部进 top-k（可带 top_k 放宽名额）
  empty_ok       允许空结果；miss_all 列表内的文档出现即失败（库外问题不该硬凑）
  stale_not_top1 该文档若排第 1 即失败（时效裁决探针：旧事实不应压过新事实）
临时服务端口 14967；用完即收（CHARTER 部署纪律）。
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
RAG_ROOT = HERE.parent
REPO_ROOT = RAG_ROOT.parent
PORT = 14967
BASE = f"http://127.0.0.1:{PORT}"

# 分类名必须 ASCII（Chroma collection 命名约束，与生产部署同）；路由语义在描述里
CATEGORY_DESCRIPTIONS = {
    "device_tech": "设备与技术：路由器、网络、电脑、汽车等设备的配置、固件、备份与维修保养记录",
    "household": "家庭事务：房租、保险、孩子接送、住址、家电维护等家庭日常事务",
    "health_life": "健康与生活：家人健康记录、食谱烘焙、旅行订房等生活信息",
}


def _load_jsonl(path: Path):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out


def _wait(pred, timeout: float, interval: float = 1.0, what: str = ""):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"等待超时: {what}")


def start_server(tmp: Path) -> subprocess.Popen:
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg.setdefault("rag", {})
    cfg["rag"]["host"] = "127.0.0.1"
    cfg["rag"]["port"] = PORT
    cfg["rag"]["compute"] = {**(cfg["rag"].get("compute") or {}), "host": ""}
    cfg_path = tmp / "eval_config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    env = {**os.environ,
           "RAG_CONFIG": str(cfg_path),
           "RAG_DATA_DIR": str(tmp / "data"),
           "RAG_COMPUTE_HOST": "",      # 清掉 .env.local 可能带来的拆分覆盖：评估恒融合
           "RAG_API_KEY": ""}
    proc = subprocess.Popen([str(RAG_ROOT / ".venv/bin/python"), "rag_server.py"],
                            cwd=str(RAG_ROOT), env=env,
                            stdout=(tmp / "server.log").open("w"),
                            stderr=subprocess.STDOUT)
    try:
        _wait(lambda: requests.get(f"{BASE}/health", timeout=2).json().get("status") == "healthy",
              timeout=240, what="rag_server /health（含模型加载）")
    except Exception:
        proc.terminate()
        print((tmp / "server.log").read_text(encoding="utf-8")[-3000:], file=sys.stderr)
        raise
    return proc


def seed(corpus: list) -> None:
    for name, desc in CATEGORY_DESCRIPTIONS.items():
        requests.post(f"{BASE}/api/v1/category",
                      json={"db_name": name, "description": desc}, timeout=30).raise_for_status()
    for doc in corpus:
        requests.post(f"{BASE}/api/v1/document/insert",
                      json={"title": doc["title"], "document": doc["text"],
                            "category": doc["category"]}, timeout=60).raise_for_status()

    wanted = {d["title"] for d in corpus}

    def _all_indexed():
        seen = set()
        for cat in CATEGORY_DESCRIPTIONS:
            r = requests.get(f"{BASE}/api/v1/document/list",
                             params={"category": cat}, timeout=30).json()
            seen |= {d["title"] for d in r.get("documents", []) if d.get("chunks", 0) > 0}
        return wanted <= seen
    _wait(_all_indexed, timeout=600, interval=2, what="夹具语料全部入库")

    # 时效探针：旧住址标过时（U3 部署同款操作）
    r = requests.get(f"{BASE}/api/v1/document/list",
                     params={"category": "household"}, timeout=30).json()
    docs_listed = r.get("documents", [])
    for d in docs_listed:
        if d["title"] == "旧住址档案":
            requests.post(f"{BASE}/api/v1/document/stale",
                          json={"source_path": d["source_path"],
                                "reason": "2025-08 已搬离，现住址见「现住址与快递偏好」"},
                          timeout=30).raise_for_status()

    # 凭证指针链路冒烟（R3）：随手取一篇的 chunk 0 相邻块，端点断了当场炸而非静默
    if docs_listed:
        ctx = requests.get(f"{BASE}/api/v1/document/context",
                           params={"source_path": docs_listed[0]["source_path"],
                                   "seq": 0, "n": 1}, timeout=30).json()
        assert ctx.get("chunks"), f"取邻块冒烟失败: {ctx}"
        print(f"== 指针冒烟 ✓ {docs_listed[0]['title']} 共 {ctx.get('total_chunks')} 块")


def run_item(item: dict) -> dict:
    expect = item["expect"]
    top_k = int(expect.get("top_k", 3))
    payload = {"query": item["query"], "category": item["category"],
               "optimized_query": item["optimized_query"],
               "chunk_k_per_db": max(5, top_k * 2),   # 部署同款（rag_search 工具的放大规则）
               "rerank_top_k": top_k}
    r = requests.post(f"{BASE}/api/v1/document/query", json=payload, timeout=120)
    r.raise_for_status()
    results = r.json().get("results", [])
    titles = [(res.get("metadata") or {}).get("title", "") for res in results]

    ok, why = True, []
    first_hit_rank = None
    wanted = expect.get("hit_any") or expect.get("hit_all") or []
    for rank, t in enumerate(titles, 1):
        if t in wanted:
            first_hit_rank = rank
            break
    if "hit_any" in expect and not (set(titles) & set(expect["hit_any"])):
        ok = False
        why.append(f"hit_any 全缺 {expect['hit_any']}（实际 {titles}）")
    if "hit_all" in expect:
        missing = set(expect["hit_all"]) - set(titles)
        if missing:
            ok = False
            why.append(f"hit_all 缺 {sorted(missing)}（实际 {titles}）")
    if expect.get("miss_all"):
        bad = set(titles) & set(expect["miss_all"])
        if bad:
            ok = False
            why.append(f"库外问题硬凑出 {sorted(bad)}")
    if expect.get("stale_not_top1") and titles and titles[0] in expect["stale_not_top1"]:
        ok = False
        why.append(f"过时文档压过新事实排第 1: {titles[0]}")
    return {"id": item["id"], "kind": item["kind"], "pass": ok, "why": why,
            "titles": titles, "n_results": len(results),
            "mrr": (1.0 / first_hit_rank) if first_hit_rank else 0.0,
            "judged_recall": bool(wanted)}


def summarize(rows: list) -> dict:
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    out = {"overall": {"n": len(rows),
                       "pass_rate": round(sum(r["pass"] for r in rows) / len(rows), 3),
                       "mrr": round(sum(r["mrr"] for r in rows if r["judged_recall"])
                                    / max(1, sum(1 for r in rows if r["judged_recall"])), 3)},
           "by_kind": {}}
    for k, rs in sorted(by_kind.items()):
        out["by_kind"][k] = {"n": len(rs),
                             "pass_rate": round(sum(r["pass"] for r in rs) / len(rs), 3)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save-baseline", action="store_true",
                    help="把本次结果写入 BASELINE.json（接受当前水平为基线）")
    ap.add_argument("--keep", action="store_true", help="保留临时数据根（调试）")
    args = ap.parse_args()

    corpus = _load_jsonl(HERE / "corpus.jsonl")
    items = _load_jsonl(HERE / "items.jsonl")
    tmp = Path(tempfile.mkdtemp(prefix="rag-eval-"))
    print(f"== 隔离数据根 {tmp}，拉起融合模式评估实例 :{PORT} …")
    proc = start_server(tmp)
    try:
        print(f"== 灌入夹具语料 {len(corpus)} 篇 …")
        seed(corpus)
        print(f"== 跑题 {len(items)} 条 …")
        rows = [run_item(it) for it in items]
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
        if not args.keep:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    summary = summarize(rows)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = HERE / "results"
    outdir.mkdir(exist_ok=True)
    (outdir / f"{stamp}.json").write_text(
        json.dumps({"stamp": stamp, "summary": summary, "rows": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    for r in rows:
        if not r["pass"]:
            print(f"❌ {r['id']} [{r['kind']}] " + "; ".join(r["why"])[:180])
    print("== 总览:", json.dumps(summary["overall"], ensure_ascii=False))
    print("== 分形态:", json.dumps(summary["by_kind"], ensure_ascii=False))

    baseline_path = HERE / "BASELINE.json"
    if baseline_path.exists():
        base = json.loads(baseline_path.read_text(encoding="utf-8"))
        b = base["summary"]
        print(f"== 对比基线（{base.get('stamp')}）: "
              f"pass {b['overall']['pass_rate']} → {summary['overall']['pass_rate']}  "
              f"MRR {b['overall']['mrr']} → {summary['overall']['mrr']}")
        for k, v in summary["by_kind"].items():
            bv = (b["by_kind"].get(k) or {}).get("pass_rate")
            if bv is not None and v["pass_rate"] != bv:
                print(f"   {k}: {bv} → {v['pass_rate']}")
    if args.save_baseline:
        baseline_path.write_text(json.dumps({"stamp": stamp, "summary": summary,
                                             "rows": [{k: r[k] for k in ("id", "kind", "pass")}
                                                      for r in rows]},
                                            ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        print(f"== 基线已更新: {baseline_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
