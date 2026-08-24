#!/usr/bin/env python3
"""用**当前**题库判分规格离线重判战役产物，并把 score/cot **回写进 transcripts.jsonl**
（单一尺——表格与逐题矩阵读轨迹、维度聚合读 summary，两处必须同尺；阶段复审 MN-7）。

前提核查（阶段复审 MJ-7）：重判只能修判分、不能修题面。轨迹带 `task_fp`（run_eval
08-24 起写入）时逐条与当前题库指纹比对：同版本前缀（v2:）不一致 → 列进
`rescore_fp_mismatch` 并拒绝重判该条（题面变了必须整题重跑）；无指纹或旧版本指纹 →
记 `rescore_unverifiable_*`（能重判但前提靠 git 历史人工核）。聚合口径如实自述：
`rescore_note` 写明本次 summary 由多少可核验/不可核验/拒判记录构成——不可核验多时
"统一尺"只对判分规格成立，题面同一性由流程与 git 保证。flips 只记 pass 翻转且相对
文件当前态（回写后再跑必为零），历史前后对照走 git。

用法：cd backend && .venv/bin/python ../benchmarks/data/model-eval-20260824/rescore.py
"""
import json
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
BACKEND = DIR.parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from benchmarks.model_eval.itemlib import (DIMENSIONS, discover_item_files,  # noqa: E402
                                           load_items, task_fingerprint)
from benchmarks.model_eval.run_eval import summarize_scores  # noqa: E402
from benchmarks.model_eval.scorers import cot_purity, get_scorer  # noqa: E402
from benchmarks.model_eval.scorers.speed import aggregate as speed_aggregate  # noqa: E402


def main():
    items = {it["id"]: it for it in load_items(
        discover_item_files(BACKEND / "benchmarks/model_eval/items", list(DIMENSIONS)))}
    bank_fp = {i: task_fingerprint(it) for i, it in items.items()}
    for mdir in sorted((DIR / "q").iterdir()):
        tpath = mdir / "transcripts.jsonl"
        if not tpath.exists():
            continue
        recs = [json.loads(l) for l in tpath.open()]
        ver = bank_fp[next(iter(bank_fp))].split(":", 1)[0] + ":"
        changed, missing, mismatch, unverifiable = [], [], [], []
        for r in recs:
            it = items.get(r["item_id"])
            if it is None:
                missing.append(r["item_id"])
                continue
            fp = r.get("task_fp")
            if fp is None or not str(fp).startswith(ver):
                unverifiable.append(r["item_id"])     # 无指纹/旧版本算法：能判但前提走人工
            elif fp != bank_fp[r["item_id"]]:
                mismatch.append(r["item_id"])
                continue                      # 题面已变：拒绝重判，须整题重跑
            new = get_scorer(it["dimension"])(r, it)
            if bool(new.get("pass")) != bool(r["score"].get("pass")):
                changed.append((r["item_id"], r["score"].get("pass"), new.get("pass")))
            r["score"] = new
            r["cot"] = cot_purity.analyze(r, it.get("gen") or {})
        tmp = tpath.with_suffix(".jsonl.tmp")         # 原子替换：中断不毁唯一档案
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
                       encoding="utf-8")
        import os
        os.replace(tmp, tpath)
        old = json.loads((mdir / "summary.json").read_text()) if (mdir / "summary.json").exists() else {}
        summ = {**old, "n_runs": len(recs), "dimensions": summarize_scores(recs),
                "cot_purity": cot_purity.aggregate(recs), "speed": speed_aggregate(recs),
                "rescore_flips": [{"item": i, "before": a, "after": b} for i, a, b in changed],
                "rescore_missing_items": missing,
                "rescore_fp_mismatch": mismatch,
                "rescore_unverifiable_n": len(unverifiable),
                "rescore_unverifiable_items": sorted(set(unverifiable)),
                "rescore_note": f"可核验(同版本指纹一致) {len(recs)-len(unverifiable)-len(mismatch)-len(missing)}"
                                f" / 不可核验(无指纹或旧版本) {len(unverifiable)}"
                                f" / 拒判(指纹不符) {len(mismatch)}——不可核验记录的题面同一性由流程与 git 保证"}
        (mdir / "summary_rescored.json").write_text(json.dumps(summ, ensure_ascii=False, indent=2) + "\n")
        print(f"== {mdir.name}: {len(recs)} 轨迹，翻转 {len(changed)} {changed}，指纹不符 {mismatch}，"
              f"无指纹 {len(unverifiable)}，题库缺失 {missing}")
        for dim, agg in summ["dimensions"].items():
            print(f"   {dim}: pass {agg['pass_rate']} (n={agg['n']})  旧 "
                  f"{(old.get('dimensions') or {}).get(dim, {}).get('pass_rate')}")


if __name__ == "__main__":
    main()
