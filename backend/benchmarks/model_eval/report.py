"""能力画像报告生成器：多份 summary.json → 跨模型对比 markdown。

用法：
  .venv/bin/python benchmarks/model_eval/report.py <summary.json ...> [--out 路径]
  .venv/bin/python benchmarks/model_eval/report.py --latest [--out 路径]
    （--latest 自动取 results/<模型>/ 下时间戳最新的一份）

产出即路由决策素材（HETERO_COMPUTE §10）：行=指标，列=模型，指标按「路由该看什么」
组织而非按打分器组织。不做加权总分——合成分会掩盖专科差异，恰与多模型互补的目的相反。
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _fmt(v):
    if v is None or v == {}:
        return "—"
    if isinstance(v, bool):
        return "✓" if v else "✗"
    if isinstance(v, float):
        return f"{v:.0%}" if 0 <= v <= 1 else f"{v:.2f}"
    return str(v)


def _get(d, *path):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


ROWS = [
    ("— 工具调用 —", None),
    ("首发合格率", ("dimensions", "tool_call", "first_shot_ok_rate")),
    ("最终合格率（含纠错）", ("dimensions", "tool_call", "valid_call_ok_rate")),
    ("通过率（含终答整合）", ("dimensions", "tool_call", "pass_rate")),
    ("解析错误总数", ("dimensions", "tool_call", "parse_errors_total")),
    ("纠错成功率", ("dimensions", "tool_call", "correction_success_rate")),
    ("无工具克制率", ("dimensions", "tool_call", "restraint_ok_rate")),
    ("— 长链任务 —", None),
    ("终态达成率", ("dimensions", "longchain", "state_ok_rate")),
    ("通过率（含步数/收尾）", ("dimensions", "longchain", "pass_rate")),
    ("步数合计", ("dimensions", "longchain", "steps_total")),
    ("复读调用合计", ("dimensions", "longchain", "redundant_calls_total")),
    ("错误恢复率", ("dimensions", "longchain", "recovered_rate")),
    ("— 视觉 —", None),
    ("视觉正确率", ("dimensions", "vision", "pass_rate")),
    ("— 思维链纯净度 —", None),
    ("控制标记泄漏率", ("cot_purity", "ctrl_leak_traj_rate")),
    ("思考复读度均值", ("cot_purity", "dup_rate_mean")),
    ("正文转述率均值", ("cot_purity", "recite_rate_mean")),
    ("异文字系统峰值", ("cot_purity", "other_script_max")),
    ("空思考轮合计", ("cot_purity", "empty_think_turns_total")),
    ("思考长度均值(字符)", ("cot_purity", "think_chars_mean")),
    ("— 速度/资源 —", None),
    ("生成 tps 中位", ("speed", "generation_tps", "median")),
    ("prefill tps 中位", ("speed", "prompt_tps", "median")),
    ("KV 前缀命中率均值", ("speed", "kv_prefix_hit_rate", "mean")),
    ("峰值内存 GB", ("speed", "peak_memory_max")),
]


def find_latest() -> list:
    roots = sorted((HERE / "results").iterdir()) if (HERE / "results").is_dir() else []
    out = []
    for r in roots:
        if not r.is_dir():
            continue
        runs = sorted(p for p in r.iterdir() if (p / "summary.json").exists())
        if runs:
            out.append(runs[-1] / "summary.json")
    return out


def build(paths: list) -> str:
    sums = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    names = [Path(s["model"]).name for s in sums]
    lines = ["# 模型能力画像（model_eval）", ""]
    for s in sums:
        lines.append(f"- **{Path(s['model']).name}** 方言 {s['dialect']}｜"
                     f"revision {str(s['revision'])[:8]}｜run {s['stamp']}｜"
                     f"{s['n_runs']} 轨迹")
    lines += ["", "| 指标 | " + " | ".join(names) + " |",
              "|---|" + "---|" * len(names)]
    pending_header = None
    for label, path in ROWS:
        if path is None:
            pending_header = f"| **{label}** |" + " |" * len(names)
            continue
        vals = [_fmt(_get(s, *path)) for s in sums]
        if all(v == "—" for v in vals):
            continue          # 全体无数据的行不出；其节头也只在有行时补出
        if pending_header:
            lines.append(pending_header)
            pending_header = None
        lines.append(f"| {label} | " + " | ".join(vals) + " |")
    lines += ["", "口径：判分全规则化（详见 README）；不设加权总分——合成分掩盖专科差异，"
              "与多模型互补目的相反。视觉行缺失 = 该档未声明 vision 能力。", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("summaries", nargs="*", help="summary.json 路径")
    ap.add_argument("--latest", action="store_true", help="每模型自动取最新 run")
    ap.add_argument("--out", default="", help="写入文件（缺省打印 stdout）")
    args = ap.parse_args()
    paths = [Path(p) for p in args.summaries] or (find_latest() if args.latest else [])
    if not paths:
        raise SystemExit("❌ 未给 summary.json 且 --latest 找不到任何 run")
    md = build(paths)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"== 画像已写入 {args.out}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
