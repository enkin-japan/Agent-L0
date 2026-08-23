"""重算 vision_diff.json（⑤′-2 B1 证据）。输入是 backend/benchmarks/model_eval/results/<model>/<stamp>/
transcripts.jsonl——**results/ 整目录 gitignored**，故只能在跑过这三份 run 的机器上复算；换机只能核对清单。
用法：cd backend && .venv/bin/python ../benchmarks/data/boundary-probe-20260823/vision_diff.py
"""
import json
from pathlib import Path

R = Path("benchmarks/model_eval/results")
RUNS = {"gemma-4-26b-a4b-it-4bit": "20260816-022318",
        "Qwen3.8-27B-4bit": "20260823-140448",
        "Qwen3.8-27B-8bit": "20260823-133216"}


def load(model, stamp):
    out = {}
    for line in (R / model / stamp / "transcripts.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r["dimension"] == "vision":
                out[r["item_id"]] = bool(r["score"]["pass"])
    return out


def main():
    a = load("gemma-4-26b-a4b-it-4bit", RUNS["gemma-4-26b-a4b-it-4bit"])
    q4 = load("Qwen3.8-27B-4bit", RUNS["Qwen3.8-27B-4bit"])
    q8 = load("Qwen3.8-27B-8bit", RUNS["Qwen3.8-27B-8bit"])
    keys = sorted(a)
    for label, run in (("Qwen3.8-27B-4bit", q4), ("Qwen3.8-27B-8bit", q8)):
        missing = [k for k in keys if k not in run]
        if missing:
            raise SystemExit(f"{label} 缺题 {len(missing)}：{missing[:5]} …（三份 run 题集须一致）")
    d = {
        "note": ("vision 212 题逐题差分；定义对称：26b_fail_qwen_both_pass = 26b ✗ 且 qwen38 4bit/8bit 都 ✓；"
                 "qwen_both_fail_26b_pass = 26b ✓ 且两档 qwen ✗；all_fail = 三者皆 ✗；qwen_any_fail_26b_pass 为宽口径参考。"
                 "来源 run 是 results/ 下 gitignored 本地件。"),
        "source_runs": {m: f"results/{m}/{st}" for m, st in RUNS.items()},
        "counts": {"n": len(keys), "26b_fail": sum(1 for k in keys if not a[k]),
                   "qwen4_fail": sum(1 for k in keys if not q4[k]), "qwen8_fail": sum(1 for k in keys if not q8[k])},
        "26b_fail_qwen_both_pass": [k for k in keys if not a[k] and q4[k] and q8[k]],
        "qwen_both_fail_26b_pass": [k for k in keys if a[k] and not q4[k] and not q8[k]],
        "qwen_any_fail_26b_pass": [k for k in keys if a[k] and (not q4[k] or not q8[k])],
        "all_fail": [k for k in keys if not a[k] and not q4[k] and not q8[k]],
        "snippet": "本文件由同目录 vision_diff.py 生成",
    }
    for k in ("26b_fail_qwen_both_pass", "qwen_both_fail_26b_pass", "qwen_any_fail_26b_pass", "all_fail"):
        print(k, len(d[k]))
    out = Path(__file__).with_name("vision_diff.json")
    out.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→", out)


if __name__ == "__main__":
    main()
