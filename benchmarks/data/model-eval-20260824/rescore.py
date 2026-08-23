#!/usr/bin/env python3
"""用**当前**题库判分规格离线重判战役产物（q/<model>/transcripts.jsonl → summary_rescored.json）。

为什么要有它：Ornith-1.5-4bit 在题库规格修订（reviewer 一审：CJK 词边界改 lookaround、
must_not 误伤等）之前就开跑，后五档装的是修订后的题库——不重判就是两把尺。重判只动
score 字段（纯函数 scorer 吃 transcripts 里的 turns/env_state），不碰模型、不碰轨迹。
题面（task.user / history / env_init）改过的题不能靠重判修正——脚本会把这类题列出来。

用法：cd backend && .venv/bin/python ../benchmarks/data/model-eval-20260824/rescore.py
"""
import hashlib
import json
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
BACKEND = DIR.parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from benchmarks.model_eval.itemlib import DIMENSIONS, discover_item_files, load_items  # noqa: E402
from benchmarks.model_eval.run_eval import summarize_scores  # noqa: E402
from benchmarks.model_eval.scorers import cot_purity, get_scorer  # noqa: E402
from benchmarks.model_eval.scorers.speed import aggregate as speed_aggregate  # noqa: E402


def task_fingerprint(item):
    t = item["task"]
    keep = {k: t.get(k) for k in ("user", "system", "history", "env", "env_init", "images", "tools_ref_resolved")}
    return hashlib.sha256(json.dumps(keep, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def main():
    items = {it["id"]: it for it in load_items(discover_item_files(BACKEND / "benchmarks/model_eval/items", list(DIMENSIONS)))}
    for mdir in sorted((DIR / "q").iterdir()):
        tpath = mdir / "transcripts.jsonl"
        if not tpath.exists():
            continue
        recs = [json.loads(l) for l in tpath.open()]
        changed, missing = [], []
        for r in recs:
            it = items.get(r["item_id"])
            if it is None:
                missing.append(r["item_id"])
                continue
            new = get_scorer(it["dimension"])(r, it)
            if bool(new.get("pass")) != bool(r["score"].get("pass")):
                changed.append((r["item_id"], r["score"].get("pass"), new.get("pass")))
            r["score"] = new
            r["cot"] = cot_purity.analyze(r, it.get("gen") or {})
        old = json.loads((mdir / "summary.json").read_text()) if (mdir / "summary.json").exists() else {}
        summ = {**old, "n_runs": len(recs), "dimensions": summarize_scores(recs),
                "cot_purity": cot_purity.aggregate(recs), "speed": speed_aggregate(recs),
                "rescored_with_items_fingerprint": hashlib.sha256(
                    "".join(sorted(f"{i}:{task_fingerprint(it)}" for i, it in items.items())).encode()).hexdigest()[:12],
                "rescore_flips": [{"item": i, "before": a, "after": b} for i, a, b in changed],
                "rescore_missing_items": missing}
        (mdir / "summary_rescored.json").write_text(json.dumps(summ, ensure_ascii=False, indent=2) + "\n")
        print(f"== {mdir.name}: {len(recs)} 轨迹，翻转 {len(changed)} {changed}，题库缺失 {missing}")
        for dim, agg in summ["dimensions"].items():
            print(f"   {dim}: pass {agg['pass_rate']} (n={agg['n']})  旧 {old.get('dimensions', {}).get(dim, {}).get('pass_rate')}")


if __name__ == "__main__":
    main()
