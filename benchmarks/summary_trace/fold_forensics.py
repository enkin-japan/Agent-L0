"""POST 折叠失败的取证（只读 trace 产物目录，CPU；不调模型、不起服务）。

  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/summary_trace/fold_forensics.py <trace_dir> [...]

run_trace.py 的 POST 支在 `maybe_summarize_context` 返回 0 或 `s.rolling_summary` 为空时抛
「POST 折叠失败（摘要为空或无可折叠轮次）」。摘要器原始输出不落盘，这里从会话目录间接取证：
- 「无可折叠轮次」路径能否排除：post 会话的 history.jsonl 行数、轮次 status（fail/cancelled 会被
  `_collect_turn_texts` 剔除）、`_turn_to_text` 是否非空（= 摘要器输入）。
- 摘要器是否被调用、跑了多久：
  成功对 = memory.json `updated_at` − post 会话 meta `created_at`（摘要落盘时刻 − 分叉时刻）；
  失败对 = t1 meta `created_at` + error 行 `wall_s` − post `created_at`（t0 到 t1 建会话之间的
  装配耗时没扣，所以是**上界**偏大几十毫秒）。
- 摘要器输入：字符数、有无 `[Task state at end of turn]` 块、轮内助手正文字符、调用的工具数。
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "backend") not in sys.path:
    sys.path.append(str(_REPO / "backend"))
from framework.memory.compactor import _collect_turn_texts, _turn_to_text  # noqa: E402

FAIL_MARK = "POST 折叠失败"


def _json(p: Path) -> Optional[Dict[str, Any]]:
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _sessions(d: Path) -> Dict[tuple, Dict[str, Path]]:
    """(item, rep) → {t1/pre/post: 会话目录}。会话名 = <item>-r<rep>-<hex6>-<branch>。"""
    out: Dict[tuple, Dict[str, Path]] = {}
    sdir = d / "sessions"
    if not sdir.is_dir():
        raise SystemExit(f"❌ {d}: 没有 sessions/ 目录（trace 产物不完整，或这不是一个 trace 目录）")
    for p in sorted(sdir.iterdir()):
        if not p.is_dir():
            continue
        head, br = p.name.rsplit("-", 1)
        base, _hx = head.rsplit("-", 1)
        item, rep = base.rsplit("-r", 1)
        key = (item, int(rep))
        if br in out.get(key, {}):
            raise SystemExit(f"❌ {d.name}: {key} {br} 会话重复（{p.name}）")
        out.setdefault(key, {})[br] = p
    return out


def pair_rows(d: Path) -> List[Dict[str, Any]]:
    rows = [json.loads(x) for x in (d / "transcripts.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    by: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        k = (str(r["item"]), int(r["rep"]))
        slot = by.setdefault(k, {})
        slot["error" if "error" in r else r["branch"]] = r
    ss = _sessions(d)
    out = []
    for k in sorted(set(by) | set(ss)):
        slot, sess = by.get(k, {}), ss.get(k, {})
        err = slot.get("error")
        post_dir = sess.get("post")
        rec = {"dir": d.name, "item": k[0], "rep": k[1], "status": "error" if err else ("ok" if "post" in slot else "missing"),
               "error": (err or {}).get("error"), "sessions": sorted(sess)}
        if post_dir is None:
            out.append(rec)
            continue
        lines = [x for x in (post_dir / "history.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()] \
            if (post_dir / "history.jsonl").is_file() else []
        turns = [json.loads(x) for x in lines]
        mem = _json(post_dir / "memory.json")
        mpost_json = _json(post_dir / "meta.json")
        mt1_json = _json(sess["t1"] / "meta.json") if "t1" in sess else None
        mpost, mt1 = mpost_json or {}, mt1_json or {}
        # 摘要器输入：与 maybe_summarize_context 同一取法（fold 前 summarized_turns=0、keep_tail_turns=0），
        # 与生产同一拼接口径（compactor._compact_session 的 "\n\n---\n\n".join(_collect_turn_texts(...))）。
        first = turns[0] if turns else {}
        inp = _collect_turn_texts(lines)
        text = "\n\n---\n\n".join(inp)
        asst = sum(len(str(m.get("content") or "")) for m in first.get("response") or []
                   if isinstance(m, dict) and m.get("role") == "assistant")
        ntc = sum(len(m.get("tool_calls") or []) for m in first.get("response") or []
                  if isinstance(m, dict) and m.get("role") == "assistant")
        post_created = mpost.get("created_at")
        if err:
            t1_created = mt1.get("created_at")
            dur = (None if post_created is None or t1_created is None
                   else float(t1_created) + float(err.get("wall_s") or 0) - float(post_created))
        elif mem:
            dur = None if post_created is None else float(mem["updated_at"]) - float(post_created)
        else:
            dur = None
        post_row = slot.get("post") or {}
        rec.update({
            "hist_lines": len(lines), "turn_status": [t.get("status") for t in turns],
            "foldable_blocks": len(inp), "input_chars": len(text),
            "input_has_task_state": "[Task state at end of turn]" in text,
            "record_has_task_state": "task_state" in first,
            "assistant_text_chars": asst, "turn1_tool_calls": ntc,
            "memory_json": mem is not None, "summarized_turns": (mem or {}).get("summarized_turns"),
            "summarize_s": None if dur is None else round(dur, 2),
            "summary_chars": post_row.get("summary_chars"), "summary_tokens_est": post_row.get("summary_tokens_est"),
            "turn1_status": (slot.get("pre") or {}).get("turn1_status"),
            "input_text": text,
        })
        out.append(rec)
    return out


_TS = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\n", re.MULTILINE)


def strip_ts(text: str) -> str:
    """去掉 `_turn_to_text` 盖的每轮时间戳头（各步跑的时刻不同，多轮 join 后每轮各有一个），
    剩下的才是可跨目录比的摘要器输入。"""
    return _TS.sub("", text or "")


def fit_duration(rows: List[Dict[str, Any]]) -> Optional[Tuple[float, float, float, int]]:
    """成功对上 摘要调用耗时 ~ 摘要 token(est) 的最小二乘：(截距 s, 斜率 s/token, 相关系数, n)。<3 点 = None。"""
    pts = [(r["summary_tokens_est"], r["summarize_s"]) for r in rows
           if r.get("status") == "ok" and r.get("summary_tokens_est") is not None and r.get("summarize_s") is not None]
    if len(pts) < 3:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    slope, icpt = statistics.linear_regression(xs, ys)
    return icpt, slope, statistics.correlation(xs, ys), len(pts)


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _rng(xs):
    xs = [x for x in xs if x is not None]
    return f"{min(xs)}–{max(xs)}" if xs else "—"


def render(dirs: List[Path]) -> str:
    L = ["| 目录 | 状态 | 对数 | history 行（status） | 可折叠块 | 摘要器输入字符 中位 [区间] | 输入含 task_state 块 | 助手正文字符 | memory.json 落盘 | 摘要调用耗时 s 中位 [区间] | 摘要 token(est) 中位 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    allrows = []
    for d in dirs:
        rows = [r for r in pair_rows(Path(d)) if "hist_lines" in r]
        allrows += rows
        for st in ("ok", "error"):
            rs = [r for r in rows if r["status"] == st]
            if not rs:
                continue
            hs = sorted({f"{r['hist_lines']}({','.join(map(str, r['turn_status']))})" for r in rs})
            L.append(f"| {Path(d).name} | {st} | {len(rs)} | {'; '.join(hs)} | {_rng([r['foldable_blocks'] for r in rs])} | "
                     f"{_med([r['input_chars'] for r in rs])} [{_rng([r['input_chars'] for r in rs])}] | "
                     f"{sum(r['input_has_task_state'] for r in rs)}/{len(rs)} | {_rng([r['assistant_text_chars'] for r in rs])} | "
                     f"{sum(r['memory_json'] for r in rs)}/{len(rs)} | {_med([r['summarize_s'] for r in rs])} [{_rng([r['summarize_s'] for r in rs])}] | "
                     f"{_med([r['summary_tokens_est'] for r in rs])} |")
    L.append("")
    fit = fit_duration(allrows)
    if fit:
        icpt, slope, rr, n = fit
        L.append(f"成功对上「摘要调用耗时 = 截距 + 斜率 × 摘要 token(est)」：截距 {icpt:.2f} s、斜率 {slope:.4f} s/token（≈ {1 / slope:.0f} tok/s）、"
                 f"r = {rr:.3f}、n = {n}。失败对的「等效 token 上界」=（耗时 − 截距）÷ 斜率；摘要 token(est) 只数剥掉 MEMO 后的正文，故是粗估。\n")
    L.append("失败对逐条（摘要调用耗时 = t1 建会话 + error wall_s − post 建会话，上界；「同输入他目录」= 去掉时间戳后摘要器输入逐字相同的其他目录同题同 rep 的结果）：\n")
    L.append("| 目录 | 题 | rep | 摘要调用耗时 s | 等效 token 上界 | 输入字符 | 输入含 task_state | 轮内工具调用数 | 同目录同题成功 rep 的耗时 s / 摘要 token | 同输入他目录（摘要 token） |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in allrows:
        if r["status"] != "error":
            continue
        sib = [x for x in allrows if x["dir"] == r["dir"] and x["item"] == r["item"] and x["status"] == "ok"]
        same = [x for x in allrows if x["dir"] != r["dir"] and x["item"] == r["item"] and x["rep"] == r["rep"]
                and strip_ts(x.get("input_text")) == strip_ts(r.get("input_text"))]
        eq = (f"{(r['summarize_s'] - fit[0]) / fit[1]:.0f}" if fit and r["summarize_s"] is not None else "—")
        L.append(f"| {r['dir']} | {r['item']} | {r['rep']} | {r['summarize_s']} | {eq} | {r['input_chars']} | {r['input_has_task_state']} | "
                 f"{r['turn1_tool_calls']} | {', '.join(f'{x['summarize_s']}/{x['summary_tokens_est']}' for x in sib) or '—'} | "
                 f"{', '.join(f'{x['dir']} {x['status']}({x['summary_tokens_est']})' for x in same) or '无'} |")
    errs = [r for r in allrows if r["status"] == "error"]
    if errs:
        same_n = sum(1 for r in errs if any(x["dir"] != r["dir"] and x["item"] == r["item"] and x["rep"] == r["rep"]
                                            and x["status"] == "ok" and strip_ts(x.get("input_text")) == strip_ts(r.get("input_text"))
                                            for x in allrows))
        L.append(f"\n失败对 {len(errs)} 个：摘要器输入（去时间戳）与他目录同题同 rep 的成功对逐字相同的 {same_n} 个；"
                 f"post 会话 history 恰 1 行且 status ∉ {{fail, cancelled}} 的 {sum(1 for r in errs if r['hist_lines'] == 1 and r['foldable_blocks'] == 1)} 个；"
                 f"memory.json 未落盘的 {sum(1 for r in errs if not r['memory_json'])} 个。")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("用法：fold_forensics.py <trace_dir> [...]")
    print(render([Path(a) for a in argv]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
