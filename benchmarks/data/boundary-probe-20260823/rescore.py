"""用当前题库的判分规格离线重判归档 transcripts（⑤′-2：任务 B 判分收紧为 regex_all 后确认结论不变）。
用法：cd backend && .venv/bin/python ../benchmarks/data/boundary-probe-20260823/rescore.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
from benchmarks.model_eval.matchers import match_text  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNS = ("gemma-4-26b-a4b-it-4bit", "gemma-4-26b-a4b-it-8bit", "gemma-4-26b-a4b-it-4bit-think", "Qwen3.8-27B-4bit",
        "gemma-4-26b-a4b-it-4bit-listfirst")


def main():
    items = {}
    for line in open("benchmarks/model_eval/items/text/longctx_struct.jsonl", encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        it = json.loads(line)
        items[it["id"]] = it
    out = {}
    for run in RUNS:
        rows = []
        for line in open(HERE / run / "transcripts.jsonl", encoding="utf-8"):
            r = json.loads(line)
            t = r["turns"][-1] if r["turns"] else {}
            ok, fails = match_text(items[r["item_id"]]["expect"]["final"], t.get("text") or "")
            rows.append({"item_id": r["item_id"], "old_pass": r["score"]["pass"],
                         "pass": ok and not r.get("aborted_reason"), "fails": fails, "wall_s": r.get("wall_s")})
        out[run] = rows
        print(run, [(x["item_id"].replace("tx-lcs-", ""), "✓" if x["pass"] else "✗") for x in rows])
    (HERE / "rescore.json").write_text(json.dumps(
        {"note": "用 gen_longctx_struct.py 修订后的判分（任务 B regex_all 钉条数+合计；任务 A 不变）对归档 transcripts 离线重判；文档与 prompt 逐字未变",
         "rescored": out}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
