#!/usr/bin/env python3
"""2026-08-24 六档同口径横评报告生成器（纯离线：读 q/<model>/{summary.json,transcripts.jsonl}
与 bench_baseline jsonl，不碰模型）。

用法：python3 report.py  → 写 REPORT.md（表格部分）并打印；结论段由主会话手写在 REPORT.md 顶部。
"""
import json
import statistics
from collections import defaultdict
from pathlib import Path

DIR = Path(__file__).resolve().parent
Q = DIR / "q"
BASE_OLD = DIR.parents[1] / "baseline" / "20260816-overnight" / "speed_off" / "baseline_v3.jsonl"
MTP_OLD = DIR.parents[1] / "baseline" / "20260816-overnight" / "speed_mtp" / "mtp_v3.jsonl"
BASE_NEW = DIR / "speed" / "baseline_v3_ornith.jsonl"

ORDER = [
    "Ornith-1.5-35B-A3B-MLX-4bit", "Ornith-1.5-35B-A3B-MLX-6bit", "Ornith-1.5-35B-A3B-MLX-8bit",
    "gemma-4-26b-a4b-it-4bit", "gemma-4-26b-a4b-it-8bit",
    "Qwen3.8-27B-4bit", "Qwen3.8-27B-8bit",
]
SHORT = {
    "Ornith-1.5-35B-A3B-MLX-4bit": "Ornith1.5-4b", "Ornith-1.5-35B-A3B-MLX-6bit": "Ornith1.5-6b",
    "Ornith-1.5-35B-A3B-MLX-8bit": "Ornith1.5-8b", "gemma-4-26b-a4b-it-4bit": "gemma26b-4b",
    "gemma-4-26b-a4b-it-8bit": "gemma26b-8b", "Qwen3.8-27B-4bit": "qwen38-4b", "Qwen3.8-27B-8bit": "qwen38-8b",
}
# 题批 = id 前缀 → (维度, 批名)
BATCHES = [
    ("tc-seed-", "tool_call", "seed(玩具工具)"), ("tc-bfcl-", "tool_call", "bfcl(借题)"),
    ("tc-real-", "tool_call", "real(生产工具面)"),
    ("lc-seed-", "longchain", "seed"), ("lc-hard-", "longchain", "hard(8–15步)"),
    ("tx-lcs-", "text", "longctx_struct"), ("tx-gen-", "text", "general(指令遵循)"),
    ("vi-seed-", "vision", "seed"), ("vi-chartqa-hard", "vision", "chartqa_hard(+hard2)"),
    ("vi-chartqa-", "vision", "chartqa(易)"), ("vi-app-", "vision", "app_screens(合成截图)"),
]


def batch_of(item_id):
    # 长前缀优先（vi-chartqa-hard 先于 vi-chartqa-）
    for pre, dim, name in sorted(BATCHES, key=lambda b: -len(b[0])):
        if item_id.startswith(pre):
            return dim, name
    return "?", "?"


def load_models():
    out = {}
    for m in ORDER:
        d = Q / m
        sp = d / "summary_rescored.json"       # 统一尺（rescore.py 产物）优先
        if not sp.exists():
            sp = d / "summary.json"
        if not sp.exists():
            continue
        summ = json.loads(sp.read_text())
        recs = [json.loads(l) for l in (d / "transcripts.jsonl").open()]
        out[m] = (summ, recs)
    return out


def pct(x):
    return "—" if x is None else f"{100 * x:.1f}%"


def fmt_table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


def main():
    models = load_models()
    names = [m for m in ORDER if m in models]
    md = []

    # ---- 维度总表 ----
    md.append("## 1. 维度通过率（同题库同口径，repeat=1）\n")
    rows = []
    for dim in ("tool_call", "longchain", "text", "vision"):
        row = [dim]
        for m in names:
            d = models[m][0]["dimensions"].get(dim)
            row.append("—(无vision)" if d is None else f"{pct(d['pass_rate'])} (n={d['n']})")
        rows.append(row)
    md.append(fmt_table(["维度"] + [SHORT[m] for m in names], rows))

    # ---- 分批表 ----
    md.append("\n## 2. 分题批通过率（新旧批拆开看——旧批天花板、新批才有区分度）\n")
    per = {m: defaultdict(lambda: [0, 0]) for m in names}
    for m in names:
        for r in models[m][1]:
            _, b = batch_of(r["item_id"])
            per[m][b][1] += 1
            per[m][b][0] += 1 if r["score"].get("pass") else 0
    rows = []
    for pre, dim, bname in BATCHES:
        row = [f"{dim}/{bname}"]
        for m in names:
            p, n = per[m][bname]
            row.append("—" if n == 0 else f"{p}/{n} ({100 * p / n:.0f}%)")
        rows.append(row)
    md.append(fmt_table(["题批"] + [SHORT[m] for m in names], rows))

    # ---- tool_call 细指标 ----
    md.append("\n## 3. tool_call 细指标\n")
    keys = [("first_shot_ok_rate", "首发合格"), ("valid_call_ok_rate", "合格调用(含纠错)"),
            ("parse_errors_total", "解析错误总数"), ("corrected_rate", "纠错成功占比"),
            ("restraint_ok_rate", "克制率"), ("final_ok_rate", "终答合格")]
    rows = []
    for k, label in keys:
        row = [label]
        for m in names:
            v = models[m][0]["dimensions"].get("tool_call", {}).get(k)
            row.append(v if isinstance(v, int) and not isinstance(v, bool) else pct(v))
        rows.append(row)
    md.append(fmt_table(["指标"] + [SHORT[m] for m in names], rows))

    # ---- longchain 细指标 ----
    md.append("\n## 4. longchain 细指标\n")
    keys = [("state_ok_rate", "终态达成"), ("steps_ok_rate", "步数达标"), ("steps_total", "步数合计"),
            ("redundant_calls_total", "复读调用合计"), ("env_errors_total", "环境报错次数"),
            ("recovery_success_rate", "错误恢复成功率(出错轨迹)")]
    rows = []
    for k, label in keys:
        row = [label]
        for m in names:
            v = models[m][0]["dimensions"].get("longchain", {}).get(k)
            row.append("—" if v is None else (v if isinstance(v, int) and not isinstance(v, bool) else pct(v)))
        rows.append(row)
    md.append(fmt_table(["指标"] + [SHORT[m] for m in names], rows))

    # ---- cot / speed（评测负载） ----
    md.append("\n## 5. 思维链纯净度 + 评测负载速度（引擎直连，chat 负载，部署同款采样）\n")
    rows = []
    for k, label in [("ctrl_leak_traj_rate", "控制标记泄漏率"), ("dup_rate_mean", "思考复读度均值"),
                     ("recite_rate_mean", "正文转述率均值"), ("other_script_max", "异文字系统峰值"),
                     ("empty_think_turns_total", "空思考轮合计"), ("think_chars_mean", "思考长度均值(字符)")]:
        row = [label]
        for m in names:
            v = models[m][0]["cot_purity"].get(k)
            row.append(v if isinstance(v, (int,)) and not isinstance(v, bool) else (pct(v) if k.endswith("rate") or k.endswith("max") or k.endswith("mean") and "chars" not in k else v))
        rows.append(row)
    for k, label in [("generation_tps", "生成 tps 中位"), ("prompt_tps", "prefill tps 中位")]:
        rows.append([label] + [models[m][0]["speed"][k]["median"] for m in names])
    rows.append(["峰值内存 GB"] + [models[m][0]["speed"]["peak_memory_max"] for m in names])
    rows.append(["KV 前缀命中率均值"] + [pct(models[m][0]["speed"]["kv_prefix_hit_rate"]["mean"]) for m in names])
    rows.append(["投机配置"] + [json.dumps(models[m][0]["speculative"].get("mode")) for m in names])
    rows.append(["墙钟合计(s)"] + [round(sum(r["wall_s"] for r in models[m][1]), 1) for m in names])
    md.append(fmt_table(["指标"] + [SHORT[m] for m in names], rows))

    # ---- 速度基线（server 生产口径） ----
    md.append("\n## 6. 速度基线（bench_baseline v3：server 口径、真裸贪心、350 token、n=3 中位）\n")
    cells = {}
    for src, tag in ((BASE_OLD, "off"), (BASE_NEW, "off"), (MTP_OLD, "mtp")):
        if not src.exists():
            continue
        for line in src.open():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") == "cell" and r.get("complete"):
                cells[(Path(r["model"]).name, tag, r["ctx"])] = r
    ctxs = [0, 4096, 16384, 32768, 65536]
    rows = []
    for m in ORDER:
        for tag in ("off", "mtp"):
            have = [cells.get((m, tag, c)) for c in ctxs]
            if not any(have):
                continue
            label = SHORT.get(m, m) + ("（生产 MTP）" if tag == "mtp" else "")
            rows.append([label + " decode tps"] + [("—" if c is None else c["decode_tps_median"]) for c in have])
            rows.append([label + " prefill tps"] + [("—" if c is None else c.get("prefill_tps", "—")) for c in have])
            rows.append([label + " 峰值 GB"] + [("—" if c is None else round(c["peak_mem_gb"], 1)) for c in have])
    md.append(fmt_table(["档位 / ctx"] + [str(c) for c in ctxs], rows))
    md.append("\n来源：Ornith-1.5 各档 = 本次 `speed/baseline_v3_ornith.jsonl`；gemma/Qwen3.8 = "
              "`benchmarks/baseline/20260816-overnight/speed_off/baseline_v3.jsonl`（同口径同脚本，08-16）；"
              "Qwen3.8「生产 MTP」行 = 同目录 `speed_mtp/mtp_v3.jsonl`。\n")

    # ---- 逐题矩阵：全败题 / 分歧题 ----
    md.append("\n## 7. 逐题矩阵摘要（新批：全员失败 = 疑似坏题候选；分歧题 = 区分度来源）\n")
    byitem = defaultdict(dict)
    for m in names:
        for r in models[m][1]:
            byitem[r["item_id"]][m] = bool(r["score"].get("pass"))
    new_prefixes = ("tc-real-", "lc-hard-", "tx-gen-", "vi-app-")
    all_fail, split = [], []
    for iid, res in sorted(byitem.items()):
        if not iid.startswith(new_prefixes):
            continue
        vals = list(res.values())
        if not any(vals):
            all_fail.append(iid)
        elif not all(vals):
            split.append((iid, "".join("✅" if res.get(m) else ("❌" if m in res else "·") for m in names)))
    md.append(f"- 新批全员失败（{len(all_fail)}）：{', '.join(all_fail) or '无'}")
    md.append(f"- 新批分歧题（{len(split)}，列序 = {' '.join(SHORT[m] for m in names)}）：")
    for iid, pat in split:
        md.append(f"  - `{iid}` {pat}")

    text = "\n".join(md) + "\n"
    (DIR / "REPORT_tables.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
