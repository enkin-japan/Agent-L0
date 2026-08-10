"""工具行为准则真机探针（#83，26B 档验收用）。

驱动复用 attention/driver.py 事件账本协议；判定读会话 history.jsonl 的
tool_calls 参数与工具响应。全合成材料。**要真联网**（web_research 打真搜索引擎），
跑前确认算力端与 Mac 栈都在服务中。

用例设计对应准则条目（NORMS_AUDIT.md）：
  lang_west_news     中文问欧美时事 → 走 web_research（不误用 rag_search）、
                     信源含拉丁域名（信源社区语言检索生效）、终答中文
  rag_not_for_news   时事问题 → rag_search 零调用（rag 职权纠错生效）
  doc_lang_user      英文材料+中文对话要 docx → write_document 的 markdown 以中文为主
  chart_lang_user    英文表头 CSV+中文对话要图 → render_chart 标题/标签以中文为主

用法:  cd backend && .venv/bin/python benchmarks/tool_norms/norms_probe.py \
           --work /tmp/normswork --out /tmp/norms_results.json [--only case1,case2]
"""
import argparse
import json
import os
import re
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "attention"))
from driver import run_turn, scrape_session, SESSIONS  # noqa: E402


# ---------------------------------------------------------------- 材料（合成）

CSV_EN = """quarter,region,revenue_usd,headcount
2026Q1,North America,1250000,42
2026Q1,Europe,980000,35
2026Q2,North America,1410000,44
2026Q2,Europe,1120000,38
"""

MATERIAL_EN = """# Meeting notes (synthetic, for testing)
The platform team shipped the new ingestion pipeline in June 2026. Throughput
rose from 1,200 to 4,800 documents/hour after the batching rework. Two incidents
(June 12, June 30) were both traced to schema drift in the upstream feed; a
contract test now gates deployment. Remaining risk: the retry queue is unbounded.
"""


# ---------------------------------------------------------------- 工具函数

def calls_of(sid):
    path = os.path.join(SESSIONS, sid, "history.jsonl")
    calls, responses = [], []
    if not os.path.exists(path):
        return calls, responses
    for line in open(path):
        if not line.strip():
            continue
        rec = json.loads(line)
        for m in rec.get("response") or []:
            for tc in m.get("tool_calls") or []:
                if tc.get("type") != "error":
                    calls.append((tc.get("name", "?"), tc.get("arguments") or {}))
            for tr in m.get("tool_responses") or []:
                responses.append((tr.get("tool", tr.get("name", "?")),
                                  str(tr.get("response", ""))))
    return calls, responses


_CJK = re.compile(r"[一-鿿]")


def cjk_ratio(text):
    """中日汉字占比（去空白）。判「中文为主」用 > 0.3——图表标签常混数字/单位。"""
    t = re.sub(r"\s", "", str(text))
    if not t:
        return 0.0
    return len(_CJK.findall(t)) / len(t)


def latin_domains_in(digest_text):
    """digest 来源清单里的非 CJK 圈域名（粗判：域名不含 .cn/.jp 且主体为 ASCII）。"""
    urls = re.findall(r"https?://([^/\s)\]]+)", digest_text)
    return [u for u in urls
            if u.isascii() and not re.search(r"\.(cn|jp)(:|$)", u)]


# ---------------------------------------------------------------- 用例

def build_cases(work):
    os.makedirs(work, exist_ok=True)
    csv_path = os.path.join(work, "regional_sales.csv")
    open(csv_path, "w").write(CSV_EN)
    mat_path = os.path.join(work, "meeting_notes_en.md")
    open(mat_path, "w").write(MATERIAL_EN)

    def judge_west_news(calls, responses, final):
        names = [n for n, _ in calls]
        if "web_research" not in names:
            return False, f"未走 web_research; calls={names[:8]}"
        if "rag_search" in names:
            return False, f"误用 rag_search; calls={names[:8]}"
        digests = " ".join(r for t, r in responses if "来源" in r or "source" in r)
        latin = latin_domains_in(digests)
        if not latin:
            return False, "digest 来源无拉丁域名（信源社区语言检索可能未生效）"
        if cjk_ratio(final) < 0.3:
            return False, f"终答非中文为主（cjk={cjk_ratio(final):.2f}）"
        return True, f"latin_domains={latin[:4]}; 终答中文 ✓"

    def judge_no_rag(calls, responses, final):
        names = [n for n, _ in calls]
        ok = "rag_search" not in names and "web_research" in names
        return ok, f"calls={names[:8]}"

    def judge_doc_lang(calls, responses, final):
        args = [a for n, a in calls if n == "write_document"]
        if not args:
            return False, f"未调 write_document; calls={[n for n,_ in calls][:8]}"
        md = str(args[0].get("markdown", args[0].get("content", "")))
        r = cjk_ratio(md)
        return r > 0.3, f"markdown cjk_ratio={r:.2f}（>0.3 判中文为主）"

    def judge_chart_lang(calls, responses, final):
        # 只判模型自著文本（标题/轴标签）：分类名来自数据本身，保持原文是对的
        # （改写数据值破坏可追溯性；首跑 0.15 的失败即把 North America 计入所致）
        args = [a for n, a in calls if n == "render_chart"]
        if not args:
            return False, f"未调 render_chart; calls={[n for n,_ in calls][:8]}"
        a = args[0]
        text = " ".join([str(a.get("title", "")),
                         str(a.get("x_label", "")), str(a.get("y_label", ""))])
        r = cjk_ratio(text)
        return r > 0.3, f"title/axis cjk_ratio={r:.2f}; text={text[:80]!r}"

    return [
        ("lang_west_news",
         "帮我查一下欧盟《人工智能法案》最近的执行进展（截至本月），用中文总结要点。",
         judge_west_news),
        ("rag_not_for_news",
         "美联储最近一次的利率决定是什么？简单说说。",
         judge_no_rag),
        ("doc_lang_user",
         f"把这份英文会议纪要整理成一份正式的中文总结文档（docx）：{mat_path}",
         judge_doc_lang),
        ("chart_lang_user",
         f"用这份数据画一张各区域营收对比图：{csv_path}",
         judge_chart_lang),
    ]


# ---------------------------------------------------------------- 执行

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    todo = build_cases(args.work)
    if args.only:
        keep = set(args.only.split(","))
        todo = [t for t in todo if t[0] in keep]

    rows = []
    for name, prompt, judge in todo:
        sid = f"norms-{name}-{uuid.uuid4().hex[:6]}"
        t0 = time.time()
        try:
            status, approvals = run_turn(sid, prompt)
        except Exception as e:
            status, approvals = f"driver_error:{type(e).__name__}", 0
        wall = round(time.time() - t0, 1)
        calls, responses = calls_of(sid)
        s = scrape_session(sid)
        final = s.get("final_text_head", "")
        ok, detail = judge(calls, responses, final)
        row = {"case": name, "sid": sid, "ok": ok, "detail": detail,
               "status": status, "wall_s": wall, "approvals": approvals,
               "n_calls": len(calls)}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))

    summary = {"pass": sum(1 for r in rows if r["ok"]), "total": len(rows)}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"rows": rows, "summary": summary}, f, ensure_ascii=False, indent=1)
    print("== SUMMARY ==", json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
