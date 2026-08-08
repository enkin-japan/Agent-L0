#!/usr/bin/env python3
"""ChartQA 扩容采样（站 C，Colab 侧）：VETTING.md 拒绝规则的程序化复刻。

产出两批（图与 jsonl 落 Colab clone 的 items/vision/，不进 git）：
  ① chartqa_yn.jsonl    yes/no 全量批——单题蒙对率 50% 无信息量（首批逐题拒收），
                         大样本聚合精度才有意义（"留生产机全量批"的兑现）。
  ② chartqa_hard2.jsonl 加难续采——多步读数形态，行号 >617（首批消费到 617）,
                         排除已采 37 + 已剔 3。26B 先跑，31B 侧留 80G 场，
                         双模型共同失败作坏题探测器后再定稿。
规则为 VETTING 文字的忠实固化（原临时脚本未存档，如实记录）。
"""
import json
import re
from pathlib import Path

from datasets import load_dataset

ROOT = Path("/content/agent/backend/benchmarks/model_eval/items/vision")
TAKEN_HARD = {209, 227, 249, 256, 277, 291, 332, 359, 371, 381, 397, 419, 439, 443,
              447, 448, 459, 460, 461, 469, 471, 479, 483, 505, 509, 513, 515, 517,
              519, 534, 549, 552, 555, 556, 565, 598, 617}
REMOVED_HARD = {375, 501, 551}
HARD_KWS = ("difference", "sum", "total", "average", "ratio", "how many more",
            "add up", "combined", "more than the")

ds = load_dataset("HuggingFaceM4/ChartQA", split="test")
print("test split rows:", len(ds))


def single_answer(row):
    lab = row["label"]
    if isinstance(lab, list):
        return lab[0].strip() if len(lab) == 1 else None
    return str(lab).strip()


def is_int(s):
    try:
        int(s.replace(",", ""))
        return True
    except ValueError:
        return False


def int_regexes(s):
    n = s.replace(",", "")
    forms = {n, f"{int(n):,}"}
    return [rf"\b{re.escape(f)}\b" for f in forms]


yn_items, hard_items = [], []
for i, row in enumerate(ds):
    ans = single_answer(row)
    if not ans:
        continue
    q = row["query"].strip()
    low = ans.lower()

    if low in ("yes", "no"):
        yn_items.append((i, q, low, row["image"]))
        continue

    # 加难续采：行号 >617、多步关键词、整数或 ≤2 词短语、拒浮点/百分比小数/超长轴读数
    if i <= 617 or i in TAKEN_HARD or i in REMOVED_HARD:
        continue
    if not any(k in q.lower() for k in HARD_KWS):
        continue
    if "." in ans or "%" in ans:
        continue
    if is_int(ans):
        if abs(int(ans.replace(",", ""))) > 100000:
            continue
    elif len(ans.split()) > 2:
        continue
    hard_items.append((i, q, ans, row["image"]))

print(f"yes/no 全量批: {len(yn_items)}  加难续采候选: {len(hard_items)}")

(ROOT / "assets/chartqa_yn").mkdir(parents=True, exist_ok=True)
(ROOT / "assets/chartqa_hard2").mkdir(parents=True, exist_ok=True)


def write_batch(fname, prefix, adir, rows, make_item):
    with open(ROOT / fname, "w", encoding="utf-8") as f:
        f.write(f"# 站 C 扩容批（2026-08-09，规则=VETTING 拒绝类程序化复刻；评测态产物不入 git）\n")
        for i, q, ans, img in rows:
            png = ROOT / "assets" / adir / f"test_{i}.png"
            if not png.exists():
                img.convert("RGB").save(png, "PNG")
            f.write(json.dumps(make_item(i, q, ans, f"assets/{adir}/test_{i}.png"),
                               ensure_ascii=False) + "\n")


def yn_item(i, q, ans, rel):
    return {"id": f"vi-chartqa-yn-{i}", "dimension": "vision", "lang": "en",
            "source": {"origin": f"ChartQA-test#{i}",
                       "fit": "yes/no 大样本批：单题蒙对率 50% 无信息量，聚合精度才有意义（VETTING 首批拒收类的大样本兑现）"},
            "task": {"user": q + " Answer with just yes or no.", "images": [rel]},
            "expect": {"type": "text", "final": {"regex_any": [rf"(?i)\b{ans}\b"]}}}


def hard_item(i, q, ans, rel):
    if is_int(ans):
        user = q + " Answer with just the number."
        rx = int_regexes(ans)
    else:
        user = q
        rx = [rf"(?i)\b{re.escape(ans)}\b"]
    return {"id": f"vi-chartqa-hard2-{i}", "dimension": "vision", "lang": "en",
            "source": {"origin": f"ChartQA-test#{i}",
                       "fit": "多步读数加难续采（两段定位+算术）：自家统计图回读进阶问法，双模型裁决前的候选态"},
            "task": {"user": user, "images": [rel]},
            "expect": {"type": "text", "final": {"regex_any": rx}}}


write_batch("chartqa_yn.jsonl", "yn", "chartqa_yn", yn_items, yn_item)
write_batch("chartqa_hard2.jsonl", "hard2", "chartqa_hard2", hard_items, hard_item)
print("EXPAND_OK")
