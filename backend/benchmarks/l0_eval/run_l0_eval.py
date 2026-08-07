"""L0 出网守卫脱敏质量评测（混合架构第 3 站 b）。

用法（backend venv，仓库根）：
  backend/.venv/bin/python backend/benchmarks/l0_eval/run_l0_eval.py --model Qwen3.5-9B-4bit

机制：与 model_eval 同款——引擎直连（AgentEngine.create，部署同参），逐题跑
`egress_guard.sanitize`（规则腿 + 模型腿，与生产 cloud_consult 完全同路），判分
纯规则：must_mask 逐字串必须从脱敏文本消失（召回），must_keep 必须保留（防过遮）。
产物 results/<model>/<stamp>.json（不入库，轨迹含合成个人数据）；结论回填
BASELINE.md（入库）。**L0 选型依实测不依直觉**（model_registry profile 同一纪律）。
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
sys.path.insert(0, str(BACKEND))

from framework.cloud import egress_guard as eg               # noqa: E402
from framework.utils.config_loader import load_section       # noqa: E402
from framework.utils.model_selection import model_options    # noqa: E402
from framework.utils.path_resolver import resolve_model_path  # noqa: E402

sys.path.insert(0, str(BACKEND / "benchmarks" / "model_eval"))
from run_eval import resolve_repo                            # noqa: E402


def load_items() -> list:
    out = []
    for line in (HERE / "items.jsonl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out


def judge(item: dict, masked_text: str) -> dict:
    leaked = [s for s in item["must_mask"] if s in masked_text]
    over = [s for s in item["must_keep"] if s not in masked_text]
    return {"id": item["id"], "kind": item["kind"],
            "pass": not leaked and not over, "leaked": leaked, "over_masked": over,
            "n_mask": len(item["must_mask"]), "n_hit": len(item["must_mask"]) - len(leaked)}


async def amain(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.model)
    opts = model_options(repo)
    model_path = resolve_model_path(repo, revision=opts.get("revision"))
    items = load_items()

    from framework.core.engine import AgentEngine
    engine = await AgentEngine.create(model_path, kv_config=load_section("inference"),
                                      model_options=opts, env={}, backend=args.backend)
    print(f"== L0 评测 {repo}（方言 {opts.get('dialect') or 'gemma4'}）：{len(items)} 题")

    rows = []
    loop = asyncio.get_running_loop()
    for it in items:
        t0 = time.monotonic()
        # 与生产完全同形：工具线程里同步调 sanitize、模型腿经 run_coroutine_threadsafe
        # 投回主循环（loop=None 的 asyncio.run 分支在已有循环的进程里会炸——首跑实证）
        s = await asyncio.to_thread(eg.sanitize, it["payload"], engine=engine,
                                    loop=loop, timeout=float(args.timeout))
        r = judge(it, s["text"])
        r["model_leg"] = s["model_leg"]
        r["latency_s"] = round(time.monotonic() - t0, 2)
        r["masked_preview"] = s["text"][:160]
        rows.append(r)
        mark = "✓" if r["pass"] else "✗"
        print(f"  {mark} {it['id']} [{it['kind']}] {r['latency_s']}s"
              + (f"  漏: {r['leaked']}" if r["leaked"] else "")
              + (f"  过遮: {r['over_masked']}" if r["over_masked"] else ""))

    total_mask = sum(r["n_mask"] for r in rows)
    summary = {
        "model": repo,
        "n_items": len(rows),
        "item_pass_rate": round(sum(r["pass"] for r in rows) / len(rows), 3),
        "span_recall": round(sum(r["n_hit"] for r in rows) / max(1, total_mask), 3),
        "over_mask_items": sum(1 for r in rows if r["over_masked"]),
        "model_leg_ok_rate": round(
            sum(1 for r in rows if r["model_leg"] == "ok") / len(rows), 3),
        "latency_median_s": sorted(r["latency_s"] for r in rows)[len(rows) // 2],
        "by_kind": {},
    }
    kinds = {}
    for r in rows:
        kinds.setdefault(r["kind"], []).append(r)
    for k, rs in sorted(kinds.items()):
        summary["by_kind"][k] = round(sum(r["pass"] for r in rs) / len(rs), 3)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = HERE / "results" / Path(repo).name
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"{stamp}.json").write_text(
        json.dumps({"stamp": stamp, "summary": summary, "rows": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("== 总览:", json.dumps(summary, ensure_ascii=False))
    engine.backend.shutdown()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="注册表 repo 或短名")
    ap.add_argument("--backend", default="mlx",
                    help="推理后端 mlx|cuda（引擎直连进程内；A100 大档重测传 cuda）")
    ap.add_argument("--timeout", default=90, type=float, help="模型腿单发熔断（秒）")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
