"""11-05 Colab D44 验收批（编排器四臂，无 Gemma / trace）：analyze_orch 的 A–Q 节原样 + U 节（D43 (a) / D43b / D44 验收）。

  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/context_knee/analyze_d44.py \
      --data benchmarks/data/context-knee-20261105-d44 --ref benchmarks/data/context-knee-20261031/qwen-current \
      --prev benchmarks/data/context-knee-20261102-b2 \
      [--out-md <data>/ANALYSIS.md] [--out-csv <data>/summary_by_level.csv]

为什么另起一个文件：analyze_orch.main_b2 要求 Gemma 两臂与 trace 三步都在（本批没有，直接 SystemExit），
且 --prev 只认 11-02 上午批的臂名（TURN + direct-*）。本文件复用 analyze_orch 的全部节函数，只换布局：
- 本批四臂 = analyze_orch.B2_ORCH；--prev = b2 批同名四臂，组名后缀 `@b2`（不是 `@am`）；
- 不渲染 R（Gemma）/ T（trace）节；N 节只读 run_colab.log；
- U 节（本文件新增）：D43 (a) 回忆与回读、D43b W2 免压、D44 预检回执、none 臂 L0、口径对照。
只读：各臂 transcripts.jsonl / guardrail_events.jsonl / sessions/<session_dir 末段>/（spill/、journal.jsonl）、
run_colab.log。CPU、标准库 + analyze.py / analyze_orch.py（bootstrap 种子固定 → 输出确定）。

已知未修（一审 Minor 5 / Nit 15 / Nit 17，记档）：
- 回读留存的「原文」判据是 feed ≥ spill 原文 90% 长，三类边界不严：原文很短时桩（首部 + 线索）也能过 90%；
  feed 在原文后附了别的文本也算原文；spill 原文本身被截过时比的是截后长度。本批 spill ≤ 2.6K 字，未触发。
- 本文件没有专属测试（只靠两次重跑逐字一致 + A–Q 节对 analyze_orch 零差异）。
- 回读 / 打回调用的 call ↔ feed 只按 turn 内同名第 k 个配对，feed 不带 name 的旧行没有按位置回退。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from argparse import Namespace
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parents[1] / "backend", _HERE.parent):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))
from context_knee import analyze as A  # noqa: E402
from context_knee import analyze_orch as O  # noqa: E402
from context_knee.quality import COHERENT_THRESHOLDS  # noqa: E402

ARMS = list(O.B2_ORCH)            # orch-turn-none / w1 / w1w2 / catalog
MANAGED = ARMS[1:]
TAG = "@b2"
HI = (49152, 57344)
NOT_EXEC = "was not executed"
META_KEYS = ("model", "revision", "dialect", "prefix_mode", "prefix_base_tokens", "stub_impl", "stub_clue",
             "agent_cfg_fingerprint", "max_total_iterations", "guard", "step_compaction", "guard_thresholds",
             "step_compaction_cfg", "thinking_replay_mode", "thinking_replay_k", "context_limit",
             "tool_schema_mode_effective", "seed", "sampling_seed", "sampling_seed_applied", "token_counter",
             "entry_sampling", "orchestrator_cfg_source")


# ---------------------------------------------------------------- 小件
def fisher_p(k1: int, n1: int, k2: int, n2: int) -> Optional[float]:
    """双侧 Fisher 精确检验（超几何，p ≤ 观测概率的表求和）。"""
    if n1 <= 0 or n2 <= 0:
        return None
    K, N = k1 + k2, n1 + n2

    def pr(x):
        return math.comb(K, x) * math.comb(N - K, n1 - x) / math.comb(N, n1)
    lo, hi = max(0, K - n2), min(K, n1)
    p0 = pr(k1)
    return min(1.0, sum(pr(x) for x in range(lo, hi + 1) if pr(x) <= p0 * (1 + 1e-9)))


def fp(p: Optional[float]) -> str:
    return "—" if p is None else f"{p:.2g}"


def kn_ci(k: int, n: int) -> str:
    lo, hi = A.wilson(k, n)
    if lo is None:
        return f"—（0/0）"
    return f"{k}/{n} = {k / n:.0%} [{lo:.0%},{hi:.0%}]"


def recall_kn(rs) -> Tuple[int, int]:
    """与 analyze.aggregate 同口径：recall_ok 非 None 且未 aborted 的 run。"""
    rec = [r["score"] for r in rs if r["score"].get("recall_ok") is not None and not r["score"].get("aborted")]
    return sum(1 for s in rec if s["recall_ok"]), len(rec)


def recall_itt(rs) -> Tuple[int, int]:
    """敏感性：aborted 也进分母（按未回忆计）。recall_ok 为 None 的 aborted run（中止在评回忆之前）
    同样进分母——ITT 的意思就是中止不得从分母里消失；未 aborted 且 recall_ok 为 None 的（无卡题）不进。"""
    rec = [r["score"] for r in rs if r["score"].get("recall_ok") is not None or r["score"].get("aborted")]
    return sum(1 for s in rec if s.get("recall_ok") and not s.get("aborted")), len(rec)


def iter_exhausted(r) -> bool:
    return "回合上限" in (r.get("aborted_reason") or "")


def cards_of(r) -> List[str]:
    return [str(c["value"]) for c in ((r.get("fill") or {}).get("cards") or [])]


def sess_name(r) -> Optional[str]:
    """session_dir 末段；空串 / 缺键 → None（O.sess_name 会回空串，拼出来就落到 sessions 根目录）。"""
    nm = str(r.get("session_dir") or "").rstrip("/").split("/")[-1]
    return nm if nm and nm not in (".", "..") else None


def attach_sessions(G, root: Path, names, tag: str = "") -> None:
    """r["_local_session"] = root/<臂>/sessions/<末段>（目录在才挂，否则 None）。替代 O._attach_sessions：
    后者对空 session_dir 会挂上 sessions 根目录。"""
    for n in names:
        for r in G[n + tag]["records"]:
            nm = sess_name(r)
            sd = root / n / "sessions" / nm if nm else None
            r["_local_session"] = sd if sd is not None and sd.is_dir() else None


def _jload(sd: Optional[Path]) -> Optional[List[Dict[str, Any]]]:
    """journal.jsonl 逐行（data 字符串时再解一层）。无会话目录或目录里没有 journal.jsonl → None（表里记 —）。"""
    if sd is None or not (sd / "journal.jsonl").is_file():
        return None
    out = []
    for ln in (sd / "journal.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        d = ev.get("data")
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except ValueError:
                d = None
        ev["data"] = d if isinstance(d, dict) else {}
        out.append(ev)
    return out


def spill_handle(args: Any) -> str:
    return str((args or {}).get("path", ""))[len("spill:"):].removesuffix(".txt")


def handle_ok(handle: str) -> bool:
    """空句柄、含 `/` 或 `..` 的句柄不拼路径（调用方记 ?）。"""
    return bool(handle) and "/" not in handle and ".." not in handle


def spill_path(sd: Optional[Path], handle: str) -> Optional[Path]:
    """句柄 → <会话>/spill/<句柄>.txt；无会话目录或句柄不合法（handle_ok）→ None。"""
    if sd is None or not handle_ok(handle):
        return None
    return Path(sd) / "spill" / f"{handle}.txt"


def spill_text(sd: Optional[Path], handle: str) -> Optional[str]:
    p = spill_path(sd, handle)
    return p.read_text(encoding="utf-8") if p is not None and p.is_file() else None


def classify_readback(feed: Optional[str], handle: str, orig: Optional[str]) -> str:
    """一次 spill 回读调用在最终 history 里的 feed 状态（readback_states / feed_state 共用）：
    ? = 句柄不合法、无配对 feed；拦截 = Duplicate / Denied / System Error 回执（没真读；先于「无 spill 原文」判定）；
    ? = 无 spill 原文可比；原文 = feed ≥ 原文 90% 长；桩 = 前 80 字与原文前 80 字相同但更短；其余 ?。"""
    if not handle_ok(handle) or feed is None:
        return "?"
    if feed.startswith(O._NOT_RUN_PREFIXES) or feed.startswith("System Error"):
        return "拦截"
    if orig is None:
        return "?"
    if len(feed) >= 0.9 * len(orig):
        return "原文"
    if feed.strip()[:80] == orig.strip()[:80]:
        return "桩"
    return "?"


def journal_readbacks(r) -> Optional[List[Dict[str, Any]]]:
    """journal 里每次 spill 回读：{seq, handle, text, status, card_hits}。text = journal 落盘的 response；只在
    status == ok 且 truncated 时补 spill 原文（与 analyze._journal_readback_texts 一致）；非 ok 的回读不算读到卡
    （card_hits 恒空）。无会话目录或无 journal.jsonl → None。seq 缺失保留 None（不可定位，不当 0）。"""
    sd = r.get("_local_session")
    j = _jload(Path(sd)) if sd is not None else None
    if j is None:
        return None
    started: Dict[str, Any] = {}
    out = []
    vals = cards_of(r)
    for ev in j:
        d = ev["data"]
        if ev.get("type") == "tool_started":
            started[str(d.get("call_id"))] = d.get("args")
        elif ev.get("type") == "tool_result":
            args = started.get(str(d.get("call_id")))
            if not A._is_spill_read(d.get("name"), args):
                continue
            handle = spill_handle(args)
            resp = str(d.get("response", ""))
            ok = str(d.get("status", "ok")) == "ok"
            sp = spill_text(sd, handle)
            text = resp + ("\n" + sp if ok and d.get("truncated") and sp is not None else "")
            orig = sp if sp is not None else resp
            out.append({"seq": ev.get("seq"), "handle": handle, "text": text, "status": d.get("status"), "ok": ok,
                        "orig_len": len(orig), "card_hits": [v for v in vals if v in text] if ok else []})
    return out


STUB_HEAD = A.STUB_DIGEST_CHARS


def call_feed_pairs(r) -> List[Tuple[int, Dict[str, Any], Optional[str]]]:
    """全 run 的 tool_call 按序 → (turn 序号, 调用, 配对 feed 文本或 None)；turn 内同名第 k 个调用 ↔ 同名第 k 条
    feed（与 analyze.recall_source 同规则）。feed 取自最终 history，事后被压会改写成桩。"""
    out = []
    for ti, t in enumerate(r.get("turns") or []):
        feeds: Dict[str, List[str]] = {}
        for f in t.get("feed") or []:
            if isinstance(f, dict):
                feeds.setdefault(str(f.get("name")), []).append(str(f.get("content", "")))
        seen: Counter = Counter()
        for c in t.get("tool_calls") or []:
            if not isinstance(c, dict):
                continue
            nm = str(c.get("name"))
            k = seen[nm]
            seen[nm] += 1
            fs = feeds.get(nm) or []
            out.append((ti, c, fs[k] if k < len(fs) else None))
    return out


def transcript_readbacks(r) -> List[Tuple[str, Optional[str]]]:
    """transcript 里每次 spill 回读调用 → (句柄, 配对的 feed 文本或 None)。"""
    return [(spill_handle(c.get("arguments")), f) for _ti, c, f in call_feed_pairs(r)
            if A._is_spill_read(c.get("name"), c.get("arguments"))]


def readback_states(r) -> Counter:
    """每次 spill 回读调用在最终 history 里的状态（判据见 classify_readback）。"""
    sd = r.get("_local_session")
    return Counter(classify_readback(f, h, spill_text(sd, h)) for h, f in transcript_readbacks(r))


def feed_state(r, rb: Dict[str, Any]) -> str:
    """journal 的一次回读（rb）对应句柄在最终 history 里的状态：该句柄的 transcript 回读里有原文就记原文，
    否则取第一种状态（同一句柄多次回读时只说明「至少一份留了原文」）。判据同 classify_readback（含拦截）。"""
    sd = r.get("_local_session")
    sts = [classify_readback(f, h, spill_text(sd, h)) for h, f in transcript_readbacks(r) if h == rb["handle"]]
    return "原文" if "原文" in sts else (sts[0] if sts else "?")


def stub_split(r) -> Counter:
    """recall_source 归「桩」的卡值再拆：head = 值在某个桩补写 spill 原文的首部 STUB_HEAD 字里；clue = 不在首部、
    只在 observation_clue 重建线索里命中（analyze.recall_source 的另一条桩判据）。重建线索与运行时桩不保证一致，
    运行时桩不在 transcripts 里，所以 clue 一列是重建口径。会话目录 / 桩补写 spill / 卡值缺 → ?。"""
    by_kind = r.get("recall_source") or {}
    stubs = [k for k, s in by_kind.items() if s == "stub"]
    out: Counter = Counter()
    if not stubs:
        return out
    sd = r.get("_local_session")
    spill_dir = Path(sd) / "spill" if sd is not None else None
    hosts = [f.read_text(encoding="utf-8") for f in sorted(spill_dir.glob("*.txt"))
             if A._STUB_SPILL_RE.fullmatch(f.stem)] if spill_dir is not None and spill_dir.is_dir() else []
    hosts = [t for t in hosts if not t.startswith("[Observation")]
    val = {(c.get("kind") or c.get("slot")): str(c["value"]) for c in ((r.get("fill") or {}).get("cards") or [])}
    for k in stubs:
        v = val.get(k)
        if v is None or not hosts:
            out["?"] += 1
        elif any(v in t.strip()[:STUB_HEAD].rstrip() for t in hosts if v in t):
            out["head"] += 1
        else:
            out["clue"] += 1
    return out


def fold_first_iter(r, ev) -> bool:
    return any(e["type"] == "context_guard_fold" and (e.get("detail") or {}).get("iteration") == 1
               for e in O.seg(r, ev))


def _is_receipt(text: Optional[str]) -> bool:
    return bool(text) and text.startswith("System Error") and NOT_EXEC in text


def precheck_receipts(r) -> List[Dict[str, Any]]:
    """feed 里每条 D44 预检回执（`System Error: '<名>' was not executed: …`），按序带类与是否附声明。
    类子句只在回执首行匹配（附的完整声明里可能出现同样的词）。"""
    out = []
    for i, (tool, text) in enumerate(O.feed_pairs(r)):
        if _is_receipt(text):
            first = text.split("\n", 1)[0]
            miss, unk = "missing required argument" in first, "unknown argument(s)" in first
            cls = "缺必填+未知键" if miss and unk else "缺必填" if miss else "未知键" if unk else "其他"
            out.append({"i": i, "tool": tool, "cls": cls, "fed": "Full declaration of" in text,
                        "full_hint": "Check the tool's declaration" in text, "text": text})
    return out


def recovered_by_class(r) -> Counter:
    """O.missing_arg_recovered 的同一 FIFO 规则，但只对 `was not executed` 预检回执、按类记账。"""
    pend: Dict[str, List[str]] = {}
    rec = Counter()
    pre = {x["i"]: x["cls"] for x in precheck_receipts(r)}
    for i, (tool, text) in enumerate(O.feed_pairs(r)):
        if i in pre:
            pend.setdefault(tool, []).append(pre[i])
        elif not O.feed_failed(text) and pend.get(tool):
            rec[pend[tool].pop(0)] += 1
    return rec


def rejected_calls(r) -> List[Dict[str, Any]]:
    """预检打回的调用本身（call ↔ feed 配对见 call_feed_pairs）。"""
    return [c for _ti, c, f in call_feed_pairs(r) if _is_receipt(f)]


def dup_resends(r) -> int:
    """被打回的调用之后，同名的下一个调用与它同参、且回执是 `Duplicate call blocked`（原样重发被去重拦下）的条数。"""
    seq = call_feed_pairs(r)
    n = 0
    for i, (_ti, c, f) in enumerate(seq):
        if not _is_receipt(f):
            continue
        nxt = next(((c2, f2) for _t2, c2, f2 in seq[i + 1:] if str(c2.get("name")) == str(c.get("name"))), None)
        if nxt and nxt[0].get("arguments") == c.get("arguments") and (nxt[1] or "").startswith("Duplicate call blocked"):
            n += 1
    return n


def param_syserr(r) -> int:
    """Q 节口径的参数类 System Error 回执数（O.syserr_class ≠ 其他；含 b2 的 TypeError 形态）。"""
    return sum(1 for _t, s in O.feed_pairs(r) if s.startswith("System Error") and O.syserr_class(s) != "其他")


def n_tool_calls(r) -> int:
    return sum(len(t.get("tool_calls") or []) for t in r.get("turns") or [])


def max_steps_of(r) -> Optional[int]:
    for w in (r.get("score") or {}).get("why") or []:
        if str(w).startswith("步数超限:"):
            try:
                return int(str(w).split(">")[1].strip())
            except (IndexError, ValueError):
                return None
    return None


# ---------------------------------------------------------------- U 节
def sec_u(G, aggs, evs, prev_ok: bool) -> List[str]:
    L = ["### U. D44 验收批专节（D43 (a) / D43b / D44；`@b2` = b2 批同名臂；本节 = analyze_d44.py 新增）\n"]
    pv = (lambda n: n + TAG) if prev_ok else None
    cmp_names = [x for n in MANAGED for x in ([n] + ([pv(n)] if pv and pv(n) in G else []))]

    # U1 ---------------------------------------------------------------- D43 (a)
    # 口径版本门（阶段复审附带项）：analyze.recall_source 只看行内 stub_clue 真值、不比规则版本，
    # 线索按**当前** compactor 规则重建——运行时读常量打印，规则一改这一列复跑就会变。
    from framework.memory.compactor import CLUE_RULE
    L.append("#### U1. D43 (a)：49K / 57K 档回忆、来源与回读\n")
    L.append(f"桩·仅线索 列按当前 `compactor.CLUE_RULE`（值 `{CLUE_RULE}`）重建；规则一改此列复跑会变。\n")
    L.append("recall = analyze.aggregate 同口径（recall_ok 非空、未 aborted 的 run）；ITT = aborted 也进分母按未回忆计；"
             "来源 = 卡值计数（单位卡值，含 aborted run；analyze.recall_source）；「含来源 run」= 末轮正文里至少一个卡值归该来源的 run；"
             "桩·桩头 = 归桩的卡值落在桩补写 spill 原文首部 200 字（STUB_DIGEST_CHARS）里；桩·仅线索 = 不在首部、只在 "
             "compactor.observation_clue 重建的线索里命中——重建线索与运行时桩不保证一致（运行时桩不在 transcripts 里），这一列是重建口径；"
             "回读 = transcript 里 file_extract(path=spill:…) 调用；「读到卡」= journal 里该次回读结果（status=ok 且截断时补 spill 原文；"
             "非 ok 的回读不算读到）含任一卡值；journal 列 — = 至少一个 run 无会话目录或无 journal.jsonl；"
             "首迭代 fold = 本 run 有 iteration=1 的 context_guard_fold 事件（首次生成前就压）。\n")
    L.append("| 组 | level | n | aborted（迭代耗尽） | 首迭代 fold run | recall [Wilson] | recall ITT | 卡值来源 桩/回读/原文/? | 桩·桩头命中 | 桩·仅线索命中 | 含来源 run 桩/回读/原文 | 回读调用（run） | 每回读 run 回读 中位/最大 | journal 回读 / 读到卡 | 回读 run 中 aborted（迭代耗尽） |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    rec57: Dict[str, Tuple[int, int, int, int]] = {}
    for name in cmp_names:
        ev = evs.get(name)
        for lv in HI:
            rs = O.by_level(G[name]["records"]).get(lv, [])
            if not rs:
                continue
            k, n = recall_kn(rs)
            ki, ni = recall_itt(rs)
            if lv == 57344:
                rec57[name] = (k, n, ki, ni)
            src = Counter(v for r in rs for v in (r.get("recall_source") or {}).values())
            run_src = {s: sum(1 for r in rs if s in (r.get("recall_source") or {}).values()) for s in ("stub", "readback", "original")}
            rb = [r.get("readback_calls") or 0 for r in rs]
            rbr = [r for r in rs if r.get("readback_calls")]
            jr = [journal_readbacks(r) for r in rs]
            j_tot = None if any(x is None for x in jr) else sum(len(x) for x in jr)
            j_hit = None if j_tot is None else sum(1 for x in jr for y in x if y["card_hits"])
            ab = sum(1 for r in rs if r["score"].get("aborted"))
            ss = Counter()
            for r in rs:
                ss.update(stub_split(r))
            ss_q = f"（? {ss['?']}）" if ss.get("?") else ""
            L.append(f"| {name} | {lv} | {len(rs)} | {ab}（{sum(1 for r in rs if iter_exhausted(r))}） | "
                     f"{sum(1 for r in rs if fold_first_iter(r, ev)) if ev is not None else '—'} | {kn_ci(k, n)} | {kn_ci(ki, ni)} | "
                     f"{src.get('stub', 0)}/{src.get('readback', 0)}/{src.get('original', 0)}/{src.get('unknown', 0)} | "
                     f"{ss.get('head', 0)}{ss_q} | {ss.get('clue', 0)} | {run_src['stub']}/{run_src['readback']}/{run_src['original']} | {sum(rb)}（{len(rbr)}） | "
                     f"{O.n0(O.med([r['readback_calls'] for r in rbr]))}/{max([r['readback_calls'] for r in rbr] or [0])} | "
                     f"{'—' if j_tot is None else j_tot} / {'—' if j_hit is None else j_hit} | "
                     f"{sum(1 for r in rbr if r['score'].get('aborted'))}（{sum(1 for r in rbr if iter_exhausted(r))}）/{len(rbr)} |")
    L.append("")
    # 全档回读合计（b2 的「43 / 107 / 75 次」口径核对）
    L.append("57K 档 recall_ok（未 aborted）run 按卡值来源分类：全桩（桩头）= 末轮正文里的卡值都归桩且都在桩头 200 字命中；"
             "全桩（含仅线索）= 都归桩、允许部分只在重建线索里命中（= 旧「全桩」口径）；含回读 = 至少一个归回读；含原文 / ? 同理（可重叠）：\n")
    for name in cmp_names:
        rs = [r for r in O.by_level(G[name]["records"]).get(57344, []) if r["score"].get("recall_ok") and not r["score"].get("aborted")]
        vs = [list((r.get("recall_source") or {}).values()) for r in rs]
        all_stub = [r for r, v in zip(rs, vs) if v and all(x == "stub" for x in v)]
        head_only = sum(1 for r in all_stub if set(stub_split(r)) == {"head"})
        L.append(f"- {name}：{len(rs)} run；全桩（桩头）{head_only}、全桩（含仅线索）{len(all_stub)}、含回读 {sum(1 for v in vs if 'readback' in v)}、"
                 f"含原文 {sum(1 for v in vs if 'original' in v)}、含 ? {sum(1 for v in vs if 'unknown' in v)}")
    L.append("")
    L.append("全档（四档合计）回读调用 / 回读 run / 回读 run 中 aborted（迭代耗尽）：\n")
    for name in cmp_names:
        rs = G[name]["records"]
        rbr = [r for r in rs if r.get("readback_calls")]
        L.append(f"- {name}：{sum(r.get('readback_calls') or 0 for r in rs)} 次 / {len(rbr)} run / "
                 f"{sum(1 for r in rbr if r['score'].get('aborted'))}（{sum(1 for r in rbr if iter_exhausted(r))}）；"
                 f"按档 run 数 " + "、".join(f"{lv}:{sum(1 for r in rbr if int(r['level']) == lv)}" for lv in sorted({int(r['level']) for r in rs})))
    L.append("")
    if prev_ok:
        L.append("57K 回忆 本批 − b2（双侧 Fisher 精确；Wilson 见上表）：\n")
        tot = [0, 0, 0, 0, 0, 0, 0, 0]
        for name in MANAGED:
            if name not in rec57 or pv(name) not in rec57:
                continue
            k, n, ki, ni = rec57[name]
            k0, n0_, ki0, ni0 = rec57[pv(name)]
            for j, x in enumerate((k, n, ki, ni, k0, n0_, ki0, ni0)):
                tot[j] += x
            L.append(f"- {name}：{kn_ci(k0, n0_)} → {kn_ci(k, n)}，Fisher p={fp(fisher_p(k, n, k0, n0_))}；"
                     f"ITT {ki0}/{ni0} → {ki}/{ni}，p={fp(fisher_p(ki, ni, ki0, ni0))}")
        k, n, ki, ni, k0, n0_, ki0, ni0 = tot
        both = [n for n in MANAGED if n in G and pv(n) in G]      # 只取两批都有的臂
        rb_now = [r for n in both for r in O.by_level(G[n]["records"]).get(57344, []) if r.get("readback_calls")]
        rb_old = [r for n in both for r in O.by_level(G[pv(n)]["records"]).get(57344, []) if r.get("readback_calls")]
        a_now, a_old = sum(1 for r in rb_now if iter_exhausted(r)), sum(1 for r in rb_old if iter_exhausted(r))
        x_now, x_old = sum(1 for r in rb_now if r["score"].get("aborted")), sum(1 for r in rb_old if r["score"].get("aborted"))
        L.append(f"- 57K 管理臂（两批都有的 {len(both)} 臂）回读 run 中 aborted（任意原因）：b2 {kn_ci(x_old, len(rb_old))} → 本批 {kn_ci(x_now, len(rb_now))}，Fisher p={fp(fisher_p(x_now, len(rb_now), x_old, len(rb_old)))}；"
                 f"其中迭代耗尽：b2 {kn_ci(a_old, len(rb_old))} → 本批 {kn_ci(a_now, len(rb_now))}，Fisher p={fp(fisher_p(a_now, len(rb_now), a_old, len(rb_old)))}")
        L.append(f"- 三管理臂合并：{kn_ci(k0, n0_)} → {kn_ci(k, n)}，Fisher p={fp(fisher_p(k, n, k0, n0_))}；"
                 f"ITT {ki0}/{ni0} → {ki}/{ni}，p={fp(fisher_p(ki, ni, ki0, ni0))}（合并跨臂、run 不独立于题，p 只作参考）")
        L.append("")

    # U2 ---------------------------------------------------------------- D43b
    L.append("#### U2. D43b：W2 步骤边界回读免压（step_readback_exempt 事件）\n")
    L.append("回读留存 = transcript 每次 spill 回读调用在最终 history 里的 feed 状态：原文 / 桩 / 拦截（去重、拒批、System Error，没真读；先于 ? 判定）/ ?（句柄不合法、无配对 feed、无 spill 原文或都不像；判据见 classify_readback docstring）。\n")
    L.append("| 组 | level | n | 有免压事件的 run | 免压事件数 | Σcount（w2_readback_exempt） | step_compaction 事件 | 回读 run | 回读留存 原文/桩/拦截/? |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    ex_runs = []
    for name in ARMS + ([pv(n) for n in ARMS if pv(n) in G] if pv else []):
        ev = evs.get(name)
        for lv, rs in O.by_level(G[name]["records"]).items():
            if ev is None:
                L.append(f"| {name} | {lv} | {len(rs)} | — | — | — | — | {sum(1 for r in rs if r.get('readback_calls'))} | — |")
                continue
            per = [[e for e in O.seg(r, ev) if e["type"] == "step_readback_exempt"] for r in rs]
            sc = sum(1 for r in rs for e in O.seg(r, ev) if e["type"] == "step_compaction")
            for r, es in zip(rs, per):
                if es and name in ARMS:
                    ex_runs.append((name, r, es))
            stc = Counter()
            for r in rs:
                stc.update(readback_states(r))
            L.append(f"| {name} | {lv} | {len(rs)} | {sum(1 for x in per if x)} | {sum(len(x) for x in per)} | "
                     f"{O._sum_or_none(O.w2_readback_exempt(r, ev) for r in rs)} | {sc} | {sum(1 for r in rs if r.get('readback_calls'))} | "
                     f"{stc.get('原文', 0)}/{stc.get('桩', 0)}/{stc.get('拦截', 0)}/{stc.get('?', 0)} |")
    L.append("")
    L.append(f"免压 run 逐条（本批全部 {len(ex_runs)} 个；回读 = journal 里的 spill 回读；「卡值共现（含桩来源）」= 回读读到的卡值是否出现在"
             "之后的 step_completed 结果 / tool_started 参数 / 末轮正文——只是共现，值也可能来自桩，不证明是回读带出的；"
             "journal 回读 seq 缺失 = 不可定位、不判后续；「transcript feed 状态」= 该回读在 transcript feed（取自最终 history）"
             "里是原文还是已被改写成桩）：\n")
    for name, r, es in ex_runs:
        sd = r.get("_local_session")
        j = _jload(Path(sd)) if sd is not None else None
        rbs = journal_readbacks(r)
        final = (r.get("turns") or [{}])[-1].get("text") or ""
        sc_steps = [(e.get("detail") or {}).get("step_id") for e in O.seg(r, evs.get(name)) if e["type"] == "step_compaction"]
        L.append(f"- {name} {sess_name(r) or '—'}（pass={r['score'].get('pass')}，recall_ok={r['score'].get('recall_ok')}，aborted_reason「{r.get('aborted_reason') or ''}」）："
                 f"免压事件 " + "、".join(f"step {(e.get('detail') or {}).get('step_id')} count {(e.get('detail') or {}).get('count')} level {(e.get('detail') or {}).get('level')}" for e in es)
                 + f"；step_compaction 边界 {sc_steps}；回读 {'—（无 journal）' if rbs is None else f'{len(rbs)} 次'}")
        for x in rbs or []:
            state = feed_state(r, x)
            if not isinstance(x["seq"], int):
                L.append(f"  - seq —（不可定位）spill:{x['handle']}（原文 {x['orig_len']} 字；transcript feed 状态 {state}）：卡值共现不判")
                continue
            later = [ev for ev in j if isinstance(ev.get("seq"), int) and ev["seq"] > x["seq"]]
            where = []
            for v in x["card_hits"]:
                hits = [ev["type"] for ev in later if ev.get("type") in ("step_completed", "tool_started")
                        and v in json.dumps(ev["data"], ensure_ascii=False)]
                where.append(f"{v} → {sorted(set(hits)) or '无'}{' + 末轮正文' if v in final else ''}")
            L.append(f"  - seq {x['seq']} spill:{x['handle']}（原文 {x['orig_len']} 字；transcript feed 状态 {state}）：卡值共现（含桩来源） "
                     + ("；".join(where) if where else "回读结果不含卡值"))
    L.append("")
    if pv:
        L.append("W2 有无对照（w1 无 W2）：57K 档回忆与「含回读来源」run 见 U1；免压事件只出现在上表所列 run，"
                 "其余回读 run 的回读观察不经过 W2 边界（或边界上没有可免的回读），D43b 对 recall 的贡献只能落在这些 run 上。\n")

    # U3 ---------------------------------------------------------------- D44
    L.append("#### U3. D44：实参预检回执与 catalog 对 w1w2\n")
    a_n, b_n = "orch-turn-w1w2", "orch-turn-catalog"
    L.append(f"{b_n} − {a_n} 按档（单档按题配对 bootstrap，analyze.paired；首 prompt = 首次生成引擎回报 prompt_tokens 中位）：\n")
    L.append("| 组对 | level | 成功率 a → b | 差 [95%] | Pass^2 a → b | 差 [95%] | 首prompt中位 a → b | 比值 b/a |")
    L.append("|---|---|---|---|---|---|---|---|")
    pairs = [(a_n, b_n)] + ([(pv(a_n), pv(b_n))] if pv and pv(a_n) in G else [])
    for x, y in pairs:
        for lv in sorted(set(aggs[x]) & set(aggs[y])):
            ax, ay = aggs[x][lv], aggs[y][lv]
            ps = A.paired(ax["_items"], ay["_items"], "success_rate")
            pp = A.paired(ax["_items"], ay["_items"], "pass_hat_2")
            L.append(f"| {y} − {x} | {lv} | {O.pc(ax['success_rate'])} → {O.pc(ay['success_rate'])} | {A.fmt_pair(ps)} | "
                     f"{O.pc(ax['pass_hat_2'])} → {O.pc(ay['pass_hat_2'])} | {A.fmt_pair(pp)} | "
                     f"{O.n0(ax['prompt_tokens_median'])} → {O.n0(ay['prompt_tokens_median'])} | {A.num(O.ratio(ay['prompt_tokens_median'], ax['prompt_tokens_median']), 3)} |")
        common = sorted(set(aggs[x]) & set(aggs[y]))
        ps = A.pooled(aggs[x], aggs[y], common, common, "success_rate")
        pp = A.pooled(aggs[x], aggs[y], common, common, "pass_hat_2")
        L.append(f"| {y} − {x} | 合并 {len(common)} 档 | — | {A.fmt_pair(ps)} | — | {A.fmt_pair(pp)} | — | — |")
    L.append("")
    L.append("预检回执（feed 里 `System Error: '<名>' was not executed:`；类 = 回执首行里出现的子句；恢复 = O.missing_arg_recovered 同一 FIFO 规则按类记账；"
             "schema_fed 事件 = tool_error 事件 kind=precheck 且 schema_fed=true；附声明 = 回执含 `Full declaration of`；"
             "参数类 System Error / 恢复 = Q 节口径（O.syserr_class ≠ 其他的 System Error 回执数 / O.missing_arg_recovered），"
             "b2 的 TypeError 形态也算）：\n")
    L.append("| 组 | 回执（run） | 缺必填 / 未知键 / 两者兼有 / 其他 | 各类恢复 | 恢复率 | 附声明（回执文本） | schema_fed 事件 | precheck tool_error 事件 | full 尾句 | 按工具 | 参数类 System Error / 恢复（Q 节口径） |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    pre_rows: Dict[str, List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]] = {}
    for name in ARMS + ([pv(n) for n in ARMS if pv(n) in G] if pv else []):
        ev = evs.get(name)
        rows = [(r, precheck_receipts(r)) for r in G[name]["records"]]
        rows = [(r, p) for r, p in rows if p]
        pre_rows[name] = rows
        cls = Counter(x["cls"] for _, p in rows for x in p)
        rec = Counter()
        for r, _p in rows:
            rec.update(recovered_by_class(r))
        tot = sum(cls.values())
        fed_ev = O._sum_or_none(O.schema_fed(r, ev) for r in G[name]["records"]) if ev is not None else None
        pce = sum(1 for r in G[name]["records"] for e in O.seg(r, ev) if e["type"] == "tool_error"
                  and (e.get("detail") or {}).get("kind") == "precheck") if ev is not None else None
        tools = Counter(x["tool"] for _, p in rows for x in p)
        pse = sum(param_syserr(r) for r in G[name]["records"])
        psr = sum(O.missing_arg_recovered(r) for r in G[name]["records"])
        mark = "†" if name.endswith(TAG) else ""
        L.append(f"| {name} | {tot}（{len(rows)}）{mark} | {cls.get('缺必填', 0)} / {cls.get('未知键', 0)} / {cls.get('缺必填+未知键', 0)} / {cls.get('其他', 0)} | "
                 f"缺 {rec.get('缺必填', 0)} / 未 {rec.get('未知键', 0)} / 兼 {rec.get('缺必填+未知键', 0)} | "
                 f"{O.pc(O.ratio(sum(rec.values()), tot))} | {sum(1 for _, p in rows for x in p if x['fed'])} | "
                 f"{'—' if fed_ev is None else fed_ev} | {'—' if pce is None else pce} | {sum(1 for _, p in rows for x in p if x['full_hint'])} | "
                 + ("、".join(f"{k} {v}" for k, v in tools.most_common()) or "—") + f" | {pse} / {psr} |")
    L.append("")
    if pv:
        L.append(f"† `{TAG}` 行：b2 在 D44 之前，没有 `was not executed` 预检回执，回执列为零是构造使然；b2 的参数类失败是工具执行时的 "
                 "TypeError 形态 System Error，见末列（Q 节口径）。\n")
    L.append("被打回的调用是否计失败（本批全部含预检回执的 run）。分三层：\n")
    L.append("- harness 记账（同源字段，恒真，仅作一致性核对）：失败正则（O._FAILED_RESP_RE）对回执文本、行字段 system_errors、"
             "score.steps（longchain 评分器：function 调用总数）对 transcript tool_calls 总数——三者与回执过滤看的是同一份 feed / 调用，"
             "不独立于回执本身，命中只说明记账没漏，不是「编排层把打回当失败」的证据；")
    L.append("- 执行侧：journal 未执行 = 被拒调用在 journal 里没有同名同参的 tool_started（journal 与 transcript 分开落盘）；"
             "「只因计步失败」= why 只有步数超限、且 steps 减去回执数（再减紧随其后被 `Duplicate call blocked` 的同名同参重发数）≤ 上限，分两列给；"
             "score.steps 缺的 run 不判（记入「steps 缺」）；")
    L.append("- 编排层（spec F1：打回 = 失败调用 → unresolved_failure 置位 / 批次失败 → failure_swallow_nudge，原样重发 → duplicate_call）："
             "每条回执按「同 run 同工具第 k 条回执 ↔ 第 k 个 kind=precheck 的 tool_error 事件」定位到事件流，看该事件之后（同 run 切片内）"
             "是否出现对应事件；`unresolved_failure` 是 orchestrator.py 的局部变量、不落事件流；某 kind 在本臂整个事件流里一次都没有 → "
             "「—（事件流无此 kind）」而不是 0。编排层对打回调用的处理只有 golden test 证明（test_tool_arg_precheck.py："
             "test_rejected_call_counts_as_failure_for_swallow_guard / test_identical_resend_of_rejected_call_is_blocked_as_duplicate，"
             "reviewer probe_swallow 为 oracle）；本节只核数据可见部分——failure_swallow_nudge 只在收尾时仍有未解失败才发，"
             "回执之后若有成功重发就不该出现，出现 0 次不是反证。\n")
    for name in ARMS:
        rows = pre_rows.get(name) or []
        if not rows:
            L.append(f"- {name}：0 条预检回执")
            continue
        ev = evs.get(name)
        n_rc = sum(len(p) for _, p in rows)
        fail_re = sum(1 for _, p in rows for x in p if O._FAILED_RESP_RE.search(x["text"]))
        se_ok = sum(1 for r, p in rows if (r.get("system_errors") or 0) >= len(p))
        st_eq = sum(1 for r, _p in rows if r["score"].get("steps") == n_tool_calls(r))
        passed = sum(1 for r, _p in rows if r["score"].get("pass"))
        only_a = only_b = no_steps = 0
        n_dup = 0
        for r, p in rows:
            why = r["score"].get("why") or []
            mx = max_steps_of(r)
            st = r["score"].get("steps")
            d = dup_resends(r)
            n_dup += d
            if not isinstance(st, int):
                no_steps += 1
                continue
            if why and all(str(w).startswith("步数超限") for w in why) and mx is not None:
                only_a += st - len(p) <= mx
                only_b += st - len(p) - d <= mx
        # journal 未执行核对：回执按 call_feed_pairs 配回调用参数，再看 journal 里有没有同名同参的 tool_started
        # （预检打回 = 不执行 → 应当没有）；无 journal 的 run 记「不可核」
        j_not = j_tot = j_na = 0
        for r, _p in rows:
            sd = r.get("_local_session")
            j = _jload(Path(sd)) if sd is not None else None
            if j is None:
                j_na += len(rejected_calls(r))
                continue
            started = [(e["data"].get("name"), e["data"].get("args")) for e in j if e.get("type") == "tool_started"]
            for c in rejected_calls(r):
                j_tot += 1
                if (c.get("name"), c.get("arguments")) not in started:
                    j_not += 1
        # 编排层事件
        kinds = {e.get("type") for e in ev} if ev is not None else set()
        loc = nud = dup_ev = dup_ev_rs = 0
        for r, p in rows:
            sg = O.seg(r, ev)
            pre_idx: Dict[str, List[int]] = {}
            for i, e in enumerate(sg):
                if e["type"] == "tool_error" and (e.get("detail") or {}).get("kind") == "precheck":
                    pre_idx.setdefault(str((e.get("detail") or {}).get("tool")), []).append(i)
            seen_t: Counter = Counter()
            resent = dup_resends(r) > 0
            for x in p:
                k = seen_t[x["tool"]]
                seen_t[x["tool"]] += 1
                ix = pre_idx.get(x["tool"]) or []
                if k >= len(ix):
                    continue
                loc += 1
                after = sg[ix[k] + 1:]
                nud += any(e["type"] == "failure_swallow_nudge" for e in after)
                hit = any(e["type"] == "duplicate_call" and str((e.get("detail") or {}).get("tool")) == x["tool"] for e in after)
                dup_ev += hit
                dup_ev_rs += hit and resent

        def ev_cell(kind: str, k: int) -> str:
            if ev is None:
                return "—（无事件流）"
            return f"{k}/{n_rc}" if kind in kinds else "—（事件流无此 kind）"
        L.append(f"- {name}：回执 {n_rc} 条 / {len(rows)} run；这些 run 通过 {passed}/{len(rows)}")
        L.append(f"  - harness 记账（同源字段，恒真，仅作一致性核对）：命中失败正则 {fail_re}/{n_rc}；行 system_errors ≥ 本 run 回执数 {se_ok}/{len(rows)}；"
                 f"score.steps = tool_calls 总数（打回的调用计步）{st_eq}/{len(rows)}")
        L.append(f"  - 执行侧：被拒调用在 journal 无同名同参 tool_started {j_not}/{j_tot}（配回调用数 {j_tot} 对回执 {n_rc}"
                 + (f"；无 journal 不可核 {j_na}" if j_na else "") + f"）；只因计步失败（steps − 回执）{only_a} run、"
                 f"（steps − 回执 − 同名同参重发拦截）{only_b} run；同名同参重发被 `Duplicate call blocked` {n_dup} 条"
                 + (f"；steps 缺 {no_steps} run（—）" if no_steps else ""))
        L.append(f"  - 编排层（出现 / 回执数；回执定位到 precheck 事件 {loc}/{n_rc}）：unresolved_failure —（事件流无此 kind；局部变量不落事件）；"
                 f"failure_swallow_nudge {ev_cell('failure_swallow_nudge', nud)}；duplicate_call（同工具）{ev_cell('duplicate_call', dup_ev)}"
                 + (f"，其中所在 run 有同名同参重发 {dup_ev_rs}（重发 {n_dup} 条）" if "duplicate_call" in kinds else ""))
        for r, p in rows[:3]:
            calls = [c for t in r.get("turns") or [] for c in t.get("tool_calls") or [] if str(c.get("name")) == p[0]["tool"]]
            rj = rejected_calls(r)
            st = r["score"].get("steps")
            L.append(f"  - 抽查 {sess_name(r) or '—'}：被拒 {json.dumps([c.get('arguments') for c in rj], ensure_ascii=False)[:90]}；同名调用序列 "
                     + " → ".join(json.dumps(c.get('arguments'), ensure_ascii=False)[:60] for c in calls[:3])
                     + f"；why「{'；'.join(map(str, r['score'].get('why') or []))}」；steps {st if isinstance(st, int) else '—'} / tool_calls {n_tool_calls(r)}")
    L.append("")

    # U4 ---------------------------------------------------------------- none L0
    L.append("#### U4. orch-turn-none L0 与 lc-hard-009\n")
    for name in ["orch-turn-none"] + ([pv("orch-turn-none")] if pv and pv("orch-turn-none") in G else []):
        a = aggs[name].get(0)
        if not a:
            L.append(f"- {name}：—（无 L0 数据）")
            continue
        L.append(f"- {name} L0 成功率 {kn_ci(a['pass_k'], a['n'])}，Pass^2 {kn_ci(a['pass2_k'], a['n_items2'])}")
        for lv in sorted(aggs[name]):
            rs = [r for r in G[name]["records"] if int(r["level"]) == lv and r["item_id"] == "lc-hard-009"]
            L.append(f"  - lc-hard-009 L{lv}：" + "；".join(
                f"r{r['rep']} pass={r['score'].get('pass')}（{'；'.join(str(w)[:60] for w in r['score'].get('why') or []) or '—'}）"
                for r in sorted(rs, key=lambda x: int(x['rep']))))
    L.append("")

    # U5 ---------------------------------------------------------------- 口径
    L.append("#### U5. 数据质量与口径对照（本批 vs b2，同名臂）\n")
    L.append("| 组 | 原始行 | 去重丢弃 | error 行 | 有效 run | 连贯（分母） | stamp 数 | 并发写入档 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for name in ARMS + ([pv(n) for n in ARMS if pv(n) in G] if pv else []):
        g = G[name]
        coh_k = sum(round((a["coherent_rate"] or 0) * a["coherent_n"]) for a in aggs[name].values())
        coh_n = sum(a["coherent_n"] for a in aggs[name].values())
        stamps = {str(r.get("stamp")) for r in g["records"]}
        L.append(f"| {name} | {len(g['records']) + len(g['dropped'])} | {len(g['dropped'])} | {len(g['missing'])} | {len(g['records'])} | "
                 f"{coh_k}/{coh_n} | {len(stamps)} | {g['concurrent_levels'] or '—'} |")
    L.append("")
    if pv:
        L.append("行 meta 口径（每组取全部行的去重值；只列本批与 b2 不同的键）：\n")
        for name in ARMS:
            if pv(name) not in G:
                continue
            diffs = []
            for k in META_KEYS:
                cur = sorted({json.dumps(r.get(k, "<缺键>"), sort_keys=True, ensure_ascii=False) for r in G[name]["records"]})
                old = sorted({json.dumps(r.get(k, "<缺键>"), sort_keys=True, ensure_ascii=False) for r in G[pv(name)]["records"]})
                if cur != old:
                    diffs.append(f"`{k}` {','.join(old)[:90]} → {','.join(cur)[:90]}")
            tf_c = {r.get("task_fp") for r in G[name]["records"]}
            tf_o = {r.get("task_fp") for r in G[pv(name)]["records"]}
            diffs.append(f"task_fp 集合{'相同' if tf_c == tf_o else f'不同（本批 {len(tf_c)} / b2 {len(tf_o)}，交集 {len(tf_c & tf_o)}）'}")
            L.append(f"- {name}：" + "；".join(diffs))
        L.append("")
    return L


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="D44 验收批复算（编排器四臂）")
    ap.add_argument("--data", required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--prev", default=None, help="b2 批目录（同名四臂，组名加 @b2）")
    ap.add_argument("--out-md", default=None)
    ap.add_argument("--out-csv", default=None)
    a = ap.parse_args(argv)
    data, ref = Path(a.data), Path(a.ref)
    missing = [n for n in ARMS if not (data / n / "transcripts.jsonl").is_file()]
    if missing:
        raise SystemExit(f"❌ D44 布局缺臂：{missing}")
    prev = Path(a.prev) if a.prev else None
    prev_arms = [n for n in ARMS if prev and (prev / n / "transcripts.jsonl").is_file()]
    if a.prev and not prev_arms:
        raise SystemExit(f"❌ --prev {a.prev} 下没有 {ARMS} 任何一臂")
    ref_key = os.path.basename(os.path.normpath(a.ref))
    if ref_key in ARMS or ref_key in {n + TAG for n in prev_arms}:
        raise SystemExit(f"❌ 参照目录名「{ref_key}」与臂名冲突")
    saved = (O.REF, O.ARMS, O.EXTRA_ORCH, A.comparisons, O.RAG_NOTE)
    saved_a = (A.KEEP, A.th_global, A.wall_global)
    O.REF, O.ARMS, O.EXTRA_ORCH = ref_key, list(ARMS), ["orch-turn-catalog"]
    O.RAG_NOTE = "rag_search 在本批是 W-K2 可用桩（回生产空结果形态）"
    try:
        args = Namespace(metric="pass_hat_2", drop=0.20, recover=0.10, baseline_levels=[0, 4096], high_from=16384,
                         max_model_len=65536, gen_reserve=12288, prod_prefix=None, pair=[])
        A.KEEP = "latest"
        A.th_global = dict(COHERENT_THRESHOLDS)
        A.wall_global = {x for x in A.DEFAULT_WALL_INVALID.split(",") if x}
        A.comparisons = lambda groups: []
        dirs = [ref] + [data / n for n in ARMS]
        groups = [A.load_group(d) for d in dirs]
        for n in prev_arms:
            g = A.load_group(prev / n)
            g["name"] = n + TAG
            groups.append(g)
        G = {g["name"]: g for g in groups}
        raws = {d.name: O.raw_rows(d) for d in dirs}
        raws.update({n + TAG: O.raw_rows(prev / n) for n in prev_arms})
        evs = {d.name: O.load_events(d) for d in dirs}
        evs.update({n + TAG: O.load_events(prev / n) for n in prev_arms})
        attach_sessions(G, data, ARMS)
        if prev_arms:
            attach_sessions(G, prev, prev_arms, TAG)
        pb = sorted({r.get("prefix_base_tokens") for r in G["orch-turn-none"]["records"]} - {None})
        args.prod_prefix = max(pb) if pb else None
        warn = None if len(pb) == 1 else f"orch-turn-none 的 prefix_base_tokens 不唯一：{pb or '全缺'}，取较大值"
        args.pair = [f"{n}{TAG}:{n}" for n in prev_arms] + [
            "orch-turn-none:orch-turn-w1", "orch-turn-none:orch-turn-w1w2", "orch-turn-w1:orch-turn-w1w2",
            "orch-turn-w1w2:orch-turn-catalog"]
        aggs = {g["name"]: A.aggregate(g, A.th_global, A.wall_global) for g in groups}
        prev_names = [n + TAG for n in prev_arms]
        parts = [A.render(groups, aggs, args), ""]
        parts += O.sec_i(G, aggs, raws, evs, args)
        parts += O.sec_j(G, aggs, raws, evs)
        parts += O.sec_k(G, evs)
        parts += O.sec_m(G, aggs, raws)
        parts += O.sec_n(G, raws, evs, data, logs=("run_colab.log",))
        parts += O.sec_o(G, aggs, ARMS + prev_names)
        parts += O.sec_p(G, aggs, evs, ARMS + prev_names, prev_names)
        parts += O.sec_q(G, aggs, evs)
        if "orch-turn-w1w2" + TAG in G and "orch-turn-catalog" + TAG in G:
            parts += O.sec_q(G, aggs, evs, a_name="orch-turn-w1w2" + TAG, b_name="orch-turn-catalog" + TAG)
        parts += sec_u(G, aggs, evs, bool(prev_arms))
        auto = "\n".join(parts).rstrip() + "\n"
        auto = auto.replace("`@am` = 11-02 上午批同名臂", f"`{TAG}` = b2 批同名臂")
        marker = "_自动生成：`benchmarks/context_knee/analyze.py`；"
        if marker not in auto:
            raise SystemExit(f"❌ analyze.render 输出里找不到预期的『{marker}』")
        auto = auto.replace(marker, "_自动生成：`benchmarks/context_knee/analyze_d44.py`（D44 布局：A–F 节 = analyze.render 原样，"
                            "自动对照关闭、E 节只列指定对；I/J/K/M/N/O/P/Q 节 = analyze_orch 原样，R/T 节本批无数据不渲染；"
                            f"F 节生产前缀取 orch-turn-none 行内 prefix_base_tokens {args.prod_prefix}"
                            + (f"；⚠️ {warn}" if warn else "") + f"；`{TAG}` = --prev 批同名臂）；", 1)
        note = "（engine.py:140 注释的 M5 实测）"
        if note in auto:
            auto = auto.replace(note, "（取 orch-turn-none 行内字段 prefix_base_tokens，见上）", 1)
        rerun = ("_复跑：`PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/context_knee/analyze_d44.py "
                 f"--data {a.data} --ref {a.ref}" + (f" --prev {a.prev}" if a.prev else "")
                 + (f" --out-md {a.out_md}" if a.out_md else "") + (f" --out-csv {a.out_csv}" if a.out_csv else "") + "`_\n\n")
        auto = rerun + auto
        if a.out_csv:
            O.write_csv_b2(Path(a.out_csv), groups, aggs, O.b2_csv_extra(G, evs, ARMS + prev_names))
        if a.out_md:
            A.write_md(Path(a.out_md), auto)
        else:
            print(auto)
        return 0
    finally:
        O.REF, O.ARMS, O.EXTRA_ORCH, A.comparisons, O.RAG_NOTE = saved
        A.KEEP, A.th_global, A.wall_global = saved_a


if __name__ == "__main__":
    sys.exit(main())
