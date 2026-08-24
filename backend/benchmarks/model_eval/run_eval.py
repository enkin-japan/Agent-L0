"""模型能力评测 CLI（用法见 README.md）。

cd backend && .venv/bin/python benchmarks/model_eval/run_eval.py \\
    --model Qwen3.5-9B-4bit --dims tool_call [--repeat 3] [--limit N] [--ids id1,id2]

模型名接受注册表 repo 全名或末段短名；生成/引擎参数全部部署同款（kv_config =
config.yaml inference 段整段透传，与 app.py 一致）。产物落
results/<短名>/<时间戳>/：transcripts.jsonl（逐题轨迹+得分，增量追加、断点可查）
+ summary.json（聚合画像素材）。
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))          # backend/ → import framework / benchmarks

from framework.utils.config_loader import load_section                    # noqa: E402
from framework.utils.model_selection import model_capabilities, model_options  # noqa: E402
from framework.utils.path_resolver import resolve_model_path              # noqa: E402
from benchmarks.model_eval.executor import run_item                       # noqa: E402
from benchmarks.model_eval.itemlib import (DIMENSIONS, discover_item_files,  # noqa: E402
                                           load_items, task_fingerprint)
from benchmarks.model_eval.scorers import get_scorer                      # noqa: E402
from benchmarks.model_eval.scorers import cot_purity                      # noqa: E402
from benchmarks.model_eval.scorers.speed import aggregate as speed_aggregate  # noqa: E402


def resolve_repo(name: str) -> str:
    registry = load_section("inference").get("model_registry") or []
    repos = [e.get("repo", "") for e in registry]
    if name in repos:
        return name
    matches = [r for r in repos if Path(r).name == name]
    if len(matches) == 1:
        return matches[0]
    raise SystemExit(f"❌ 模型 {name!r} 不在注册表（或短名歧义）。可用短名: "
                     f"{sorted(Path(r).name for r in repos)}")


def summarize_scores(records: list) -> dict:
    """按维度聚合得分：bool 字段→比率，数值字段→总和，pass→通过率。"""
    by_dim: dict = {}
    for rec in records:
        by_dim.setdefault(rec["dimension"], []).append(rec["score"])
    out = {}
    for dim, scores in by_dim.items():
        agg: dict = {"n": len(scores),
                     "pass_rate": round(sum(1 for s in scores if s.get("pass")) / len(scores), 3)}
        keys = {k for s in scores for k in s} - {"pass", "why"}
        for k in sorted(keys):
            vals = [s[k] for s in scores if k in s]
            if all(isinstance(v, bool) for v in vals):
                agg[f"{k}_rate"] = round(sum(vals) / len(vals), 3)
            elif all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
                agg[f"{k}_total"] = sum(vals)
        # 纠错/恢复成功率的分母是"出过错的轨迹"，单列（*_rate 的分母是全体，会稀释）
        err_trajs = [s for s in scores if (s.get("parse_errors") or 0) > 0]
        if err_trajs:
            agg["correction_success_rate"] = round(
                sum(1 for s in err_trajs if s.get("corrected")) / len(err_trajs), 3)
        env_err_trajs = [s for s in scores if (s.get("env_errors") or 0) > 0]
        if env_err_trajs:
            agg["recovery_success_rate"] = round(
                sum(1 for s in env_err_trajs if s.get("recovered")) / len(env_err_trajs), 3)
        out[dim] = agg
    return out


async def amain(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.model)
    opts = model_options(repo)
    model_path = resolve_model_path(repo, revision=opts.get("revision"))

    dims = [d.strip() for d in args.dims.split(",") if d.strip()]
    unknown = set(dims) - set(DIMENSIONS)
    if unknown:
        raise SystemExit(f"❌ 未知维度 {sorted(unknown)}（可用: {DIMENSIONS}）")
    files = discover_item_files(HERE / "items", dims)
    if not files:
        raise SystemExit(f"❌ 维度 {dims} 下没有题库文件（items/<dim>/*.jsonl）")
    items = load_items(files)
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        items = [it for it in items if it["id"] in wanted]
        missing = wanted - {it["id"] for it in items}
        if missing:
            raise SystemExit(f"❌ 题库中不存在: {sorted(missing)}")
    if args.id_prefix:
        prefixes = tuple(p.strip() for p in args.id_prefix.split(",") if p.strip())
        if not prefixes:
            raise SystemExit("❌ --id-prefix 为空（只有分隔符）")
        items = [it for it in items if it["id"].startswith(prefixes)]
        if not items:
            raise SystemExit(f"❌ 没有 id 以 {prefixes} 开头的题")
    if args.limit:
        items = items[:args.limit]
    caps = model_capabilities(repo)
    lacking = [it["id"] for it in items
               if it["dimension"] == "vision" and "vision" not in caps]
    if lacking:
        print(f"⚠️ {Path(repo).name} 未声明 vision 能力，跳过 {len(lacking)} 道视觉题"
              f"（注册表 capabilities 单一真相）")
        items = [it for it in items if it["id"] not in set(lacking)]
    if not items:
        raise SystemExit("❌ 过滤后题目为空")

    from framework.core.engine import AgentEngine   # 惰性：装载后端前先把参数错误挡完
    inference_cfg = load_section("inference")
    if args.spec_mode == "off":
        # 逃生口（D.21 评审 M6）：重测 mode=off 口径画像时压制条目级 speculative，
        # 不必去改生产 config（bench_baseline 同一条纪律）
        opts = {k: v for k, v in opts.items() if k != "speculative"}
        inference_cfg = {**inference_cfg, "speculative": {"mode": "off"}}
    engine = await AgentEngine.create(model_path, kv_config=inference_cfg,
                                      model_options=opts, env={}, backend=args.backend)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = args.out or (HERE / "results" / Path(repo).name / stamp)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    transcripts = outdir / "transcripts.jsonl"
    print(f"== 评测 {repo}（方言 {opts.get('dialect') or 'gemma4'}）："
          f"{len(items)} 题 × {args.repeat} 重复 → {outdir}")

    records = []
    with transcripts.open("a", encoding="utf-8") as fh:
        for item in items:
            scorer = get_scorer(item["dimension"])
            for rep in range(args.repeat):
                traj = await run_item(engine, item, repeat_idx=rep)
                score = scorer(traj, item)
                rec = {"model": repo, "dimension": item["dimension"],
                       "lang": item["lang"], "task_fp": task_fingerprint(item),
                       **traj, "score": score,
                       "cot": cot_purity.analyze(traj, item.get("gen") or {})}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                records.append(rec)
                mark = "✅" if score.get("pass") else "❌"
                why = ("" if score.get("pass")
                       else "  | " + "; ".join(score.get("why") or [])[:120])
                print(f"{mark} {item['id']} r{rep} ({traj['wall_s']}s){why}")

    # 生效投机配置入 summary（D.21 后条目级 speculative 会在引擎装配期合并生效，
    # 评测面即部署面——但画像口径必须自描述，否则 tps 跨 run 不可比）。
    # 只有暴露 speculative 属性的后端（MLX）才可断言；cuda 走 INFER_SPEC_CONFIG
    # env、remote 由服务端自决——记 unrecorded 而不是谎报 off（评审 M5）。
    if hasattr(engine.backend, "speculative"):
        _spec_eff = engine.backend.speculative or {"mode": "off"}
    else:
        _spec_eff = {"mode": "unrecorded",
                     "note": f"backend={args.backend} 不暴露生效投机配置"}
    summary = {"model": repo, "revision": opts.get("revision"),
               "dialect": opts.get("dialect") or "gemma4",
               "speculative": _spec_eff,
               "stamp": stamp, "n_runs": len(records),
               "dimensions": summarize_scores(records),
               "cot_purity": cot_purity.aggregate(records),
               "speed": speed_aggregate(records)}
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("== summary:", json.dumps(summary["dimensions"], ensure_ascii=False))
    print("== cot:", json.dumps(summary["cot_purity"], ensure_ascii=False))
    print("== speed:", json.dumps(summary["speed"], ensure_ascii=False))
    print(f"== 产物: {outdir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="模型能力评测（引擎直连，部署同款参数）")
    ap.add_argument("--model", required=True, help="注册表 repo 全名或末段短名")
    ap.add_argument("--backend", default="mlx",
                    help="推理后端 mlx|cuda（引擎直连进程内装载；A100 评测传 cuda）")
    ap.add_argument("--dims", default="tool_call", help=f"逗号分隔，可用: {DIMENSIONS}")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟用）")
    ap.add_argument("--ids", default="", help="只跑指定题 id（逗号分隔）")
    ap.add_argument("--id-prefix", default="",
                    help="只跑 id 以这些前缀开头的题（逗号分隔；新批次剔坏题专用）")
    ap.add_argument("--out", default="", help="产物目录（缺省 results/<模型>/<时间戳>）")
    ap.add_argument("--spec-mode", default="", choices=["", "off"],
                    help="off=压制条目级 speculative 测 mode=off 口径（D.21 后 qwen38 "
                         "缺省按部署配置跑 drafter）；留空=按部署配置")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
