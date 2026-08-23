"""生产日志回流 → 边界目录候选（⑤′-2，2026-08-23）。

扫 `data_dir()/sessions/*/history.jsonl` 的轮次记录，按**确定性信号**挑出「本地模型可能
力不从心」的轮次，写成候选行供人工鉴定进 `benchmarks/model_eval/BOUNDARY.md`
（候选不是结论：目录条目的升降级永远走人工 + 探针，不由脚本自动改目录）。

信号（全部来自 turn 记录，零模型判断）：
- status ∈ {fail, warning}（编排层保险丝/谎报围堵降级/续跑故障）；
- parse_error_count ≥ 2（工具调用格式反复失控）；
- generations ≥ 12（一轮转很多圈 = 在原地打转）；
- 工具观察里 `System Error` 条数 ≥ 3；
- cloud_degraded 非空（模型自己判定要求助云端——这是最直接的"边界"自述，即便降级了）；
- 用户**紧接着重发**近似同一 prompt（前 40 字相同）= 上一轮答案不被接受。
每条候选附：请求侧事实（是否带图 / prompt_tokens / 文本头 200 字）+ 目录谓词命中
（含 pending，供对表：命中而失败 = 目录证据；失败而不命中 = 目录盲区候选）。

隐私：候选含用户 prompt 片段，产物 **gitignored**（benchmarks/model_eval/boundary_candidates.jsonl）；
入档只允许经人工改写的去标识摘要。

用法：cd backend && .venv/bin/python scripts/boundary_harvest.py [--since 2026-08-01] [--out path]
"""
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.cloud.boundary import RequestFacts, match  # noqa: E402

SIGNALS = ("status_fail", "status_warning", "parse_errors", "many_generations",
           "tool_errors", "cloud_degraded", "user_retry")


def turn_facts(turn: Dict[str, Any]) -> RequestFacts:
    """关键词只看指令（prompt）；摄入材料只贡献长度（boundary 模块头的口径）。"""
    ingested = str(turn.get("ingested_text") or "")
    return RequestFacts(text=str(turn.get("prompt") or ""),
                        has_images=bool(turn.get("image")),
                        context_tokens=int(turn.get("prompt_tokens") or 0),
                        attachments_chars=len(ingested))


def _tool_error_count(turn: Dict[str, Any]) -> int:
    n = 0
    for msg in turn.get("response") or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        for tr in msg.get("tool_responses") or []:
            if str((tr or {}).get("response") or "").lstrip().startswith("System Error"):
                n += 1
    return n


def signals_for(turn: Dict[str, Any], prev_prompt: Optional[str]) -> List[str]:
    out: List[str] = []
    st = turn.get("status")
    if st == "fail":
        out.append("status_fail")
    elif st == "warning":
        out.append("status_warning")
    if int(turn.get("parse_error_count") or 0) >= 2:
        out.append("parse_errors")
    if int(turn.get("generations") or 0) >= 12:
        out.append("many_generations")
    if _tool_error_count(turn) >= 3:
        out.append("tool_errors")
    if turn.get("cloud_degraded"):
        out.append("cloud_degraded")
    cur = str(turn.get("prompt") or "").strip()[:40]
    if prev_prompt is not None and cur and cur == prev_prompt:
        out.append("user_retry")
    return out


def iter_turns(sessions_dir: Path) -> Iterable[Dict[str, Any]]:
    if not sessions_dir.is_dir():
        print(f"[harvest] sessions 目录不存在：{sessions_dir}（0 候选）")
        return
    for d in sorted(p for p in sessions_dir.iterdir() if p.is_dir()):
        h = d / "history.jsonl"
        if not h.exists():
            continue
        for idx, line in enumerate(h.read_text(encoding="utf-8", errors="replace").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if isinstance(r, dict) and "prompt" in r:
                r["_session"] = d.name
                r["_line"] = idx
                yield r


def harvest(sessions_dir: Path, since_ts: float = 0.0) -> List[Dict[str, Any]]:
    cands: List[Dict[str, Any]] = []
    prev: Dict[str, str] = {}          # session → 上一轮 prompt 头
    for t in iter_turns(sessions_dir):
        sid = t["_session"]
        sig = signals_for(t, prev.get(sid))
        prev[sid] = str(t.get("prompt") or "").strip()[:40]
        ts_raw = t.get("timestamp")
        ts = float(ts_raw) if ts_raw is not None else None
        # 无时间戳的轮次（旧记录）不被 --since 过滤掉：时间未知 ≠ 太旧
        if not sig or (since_ts and ts is not None and ts < since_ts):
            continue
        facts = turn_facts(t)
        hits = [b.id for b in match(facts, include_pending=True)]
        cands.append({
            "session": sid, "line": t["_line"], "ts": ts,   # None = 记录无时间戳
            "status": t.get("status"), "signals": sig,
            "facts": {"has_images": facts.has_images, "context_tokens": facts.context_tokens,
                      "attachments_chars": facts.attachments_chars,
                      "prompt_head": str(t.get("prompt") or "")[:200]},
            "catalog_hits": hits,
            "verdict_hint": ("目录证据候选" if hits else "目录盲区候选"),
            "metrics": {k: t.get(k) for k in ("generations", "parse_error_count",
                                               "generation_tokens_turn", "cloud_degraded")},
        })
    return cands


def _gitignored(path: Path) -> bool:
    """路径是否被 git 忽略（仓外路径视为忽略；git 不可用时保守返回 False）。"""
    import subprocess
    try:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                              text=True, cwd=str(Path(__file__).resolve().parent), check=True).stdout.strip()
        resolved = path.resolve()
        if root and not str(resolved).startswith(root + "/"):
            return True
        r = subprocess.run(["git", "check-ignore", "-q", str(resolved)], cwd=root)
        return r.returncode == 0
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", default="", help="sessions 目录（缺省 data_dir()/sessions）")
    ap.add_argument("--since", default="", help="只看此日期（YYYY-MM-DD）之后的轮次")
    ap.add_argument("--out", default="", help="候选 JSONL 路径（缺省 benchmarks/model_eval/"
                                              "boundary_candidates.jsonl；含 prompt 片段，须为 gitignored 路径）")
    ap.add_argument("--allow-tracked-out", action="store_true",
                    help="允许 --out 落在非 gitignored 路径（仅限仓外/临时分析，自担隐私责任）")
    a = ap.parse_args()
    if a.sessions:
        sdir = Path(a.sessions)
    else:
        from framework.utils.config_loader import data_dir
        sdir = data_dir() / "sessions"
    since = time.mktime(time.strptime(a.since, "%Y-%m-%d")) if a.since else 0.0
    out = Path(a.out) if a.out else Path(__file__).resolve().parents[1] / "benchmarks/model_eval/boundary_candidates.jsonl"
    if a.out and not a.allow_tracked_out and not _gitignored(out):
        print(f"[harvest] 拒绝：--out {out} 不在 gitignore 覆盖下（候选含用户 prompt 片段）；"
              f"换 gitignored 路径或显式 --allow-tracked-out")
        return 2
    cands = harvest(sdir, since)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for c in cands:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    from collections import Counter
    sigc = Counter(s for c in cands for s in c["signals"])
    hitc = Counter(h for c in cands for h in c["catalog_hits"])
    print(f"[harvest] 轮次候选 {len(cands)} → {out}")
    print(f"[harvest] 信号分布 {dict(sigc)}")
    print(f"[harvest] 目录命中 {dict(hitc)}；盲区候选 {sum(1 for c in cands if not c['catalog_hits'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
