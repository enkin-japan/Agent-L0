"""11-07 Colab P2 噪声基线批（编排器五臂、seed 透传、L0 6 rep）：analyze_d44 的 A–Q / U 节 + V 节。

  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/context_knee/analyze_p2.py \
      --data benchmarks/data/context-knee-20261107-p2 --ref benchmarks/data/context-knee-20261031/qwen-current \
      --prev benchmarks/data/context-knee-20261105-d44 --b2 benchmarks/data/context-knee-20261102-b2 \
      [--out-md <md>] [--out-csv <data>/summary_by_level.csv]

布局（与 analyze_d44 的差别）：
- 本批臂 = O.orch_arms(--data)：B2_ORCH 四臂 + orch-turn-full；--prev = d44 批同名四臂，组名后缀 `@d44`；
  --b2 = b2 批同名四臂，只在 V 节对照表里用（不进 A–Q，免得 A–F 表再翻一倍）。
- F 节生产前缀取 orch-turn-catalog 行内 prefix_base_tokens：D44 (iii)（c76492f）后 prod 生效档位 = catalog，
  而 prod 臂行内 prefix_base_tokens / prefix_n_tools 仍按 full 声明计（run_knee.build_prefixes 用 production_decl("prod")），
  取 orch-turn-none 的值会把 F 节算成 full 前缀。V 节有逐臂对照。
- U 节 = analyze_d44.sec_u(prev_ok=False)：只列本批，不并跨批列（sec_u 的跨批文字写死 b2）。
- V 节（本文件新增）：V1 逐字相同率（跨臂 / 臂内跨 rep / 种子核对）→ V2 L0 噪声基线 → V3 五臂 × 四档与 d44 / b2
  → V4 full 对 catalog / w1w2 → V5 时长。
只读：各臂 transcripts.jsonl / guardrail_events.jsonl / sessions/ / run_colab.log（本批与 --prev）。
CPU、标准库 + analyze / analyze_orch / analyze_d44；bootstrap 种子固定（A.BOOT_SEED）→ 输出确定。
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import random
import re
import statistics
import sys
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parents[1] / "backend", _HERE.parent):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))
from context_knee import analyze as A  # noqa: E402
from context_knee import analyze_d44 as D  # noqa: E402
from context_knee import analyze_orch as O  # noqa: E402
from context_knee.quality import COHERENT_THRESHOLDS  # noqa: E402

TAG = "@d44"
TAG_B2 = "@b2"
FULL = "orch-turn-full"
LEVELS = (0, 32768, 49152, 57344)
#: p2 臂 → d44 / b2 里「同形态」的臂：full 臂（guard on / step on / --prefix full）= d44、b2 时 w1w2 的真实形态
FORM_MATCH = {"orch-turn-none": "orch-turn-none", "orch-turn-w1": "orch-turn-w1", "orch-turn-w1w2": "orch-turn-w1w2",
              "orch-turn-catalog": "orch-turn-catalog", FULL: "orch-turn-w1w2"}


# ---------------------------------------------------------------- V1：逐字相同
def output_sig(r: Dict[str, Any]) -> str:
    """模型输出签名：每轮 (thoughts, text, tool_calls) + turn_response，JSON 排序键序列化。工具调用序列与参数都在内。
    gens 比 turns 多出来的生成（回合上限耗尽时最后一次生成没有对应 turn）在 transcripts 里没有输出文本，
    改记它们的 (generation_tokens, truncated, cancelled, repetition_guard)，至少能区分「多生成了几次、各多长」。"""
    turns = [t for t in (r.get("turns") or []) if isinstance(t, dict)]
    gens = [g for g in (r.get("gens") or []) if isinstance(g, dict)]
    extra = [[g.get("generation_tokens"), g.get("truncated"), g.get("cancelled"), g.get("repetition_guard")]
             for g in gens[len(turns):]]
    return json.dumps({"turns": [[t.get("thoughts"), t.get("text"), t.get("tool_calls")] for t in turns],
                       "extra_gens": extra, "final": r.get("turn_response")}, ensure_ascii=False, sort_keys=True)


def input_sig(r: Dict[str, Any]) -> Tuple[Any, ...]:
    """首 prompt token 数代理（transcripts 不存 prompt 原文）：首次生成引擎回报 prompt_tokens + fill.prompt_tokens_measured。
    只比 token 数；内容相同的旁证是 system_sha（会话 system.json）与输出逐字相同。"""
    g = (r.get("gens") or [{}])[0]
    return (g.get("prompt_tokens"), (r.get("fill") or {}).get("prompt_tokens_measured"))


def system_sha(r: Dict[str, Any]) -> Optional[str]:
    """会话目录 system.json 的 sha256（r["_local_session"] 由 analyze_d44.attach_sessions 挂；无目录 / 无文件 → None）。"""
    sd = r.get("_local_session")
    p = Path(sd) / "system.json" if sd is not None else None
    if p is None or not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def keyed(rs: Iterable[Dict[str, Any]], level: int) -> Dict[Tuple[str, int], Dict[str, Any]]:
    """(item_id, rep) → 行（只取该档；同键重复时保留后出现者——A.load_group 已去重，这里只是索引）。"""
    return {(r["item_id"], int(r["rep"])): r for r in rs if int(r["level"]) == level}


def identity_rate(a: Dict[Tuple[str, int], Dict[str, Any]], b: Dict[Tuple[str, int], Dict[str, Any]],
                  sig=output_sig) -> Tuple[int, int]:
    """两臂按 (item, rep) 配对，sig 逐字相同的对数 / 共同键数。"""
    ks = sorted(set(a) & set(b))
    return sum(1 for k in ks if sig(a[k]) == sig(b[k])), len(ks)


def within_rep_identity(a: Dict[Tuple[str, int], Dict[str, Any]], sig=output_sig) -> Tuple[int, int]:
    """同臂同档内，同一题不同 rep 两两比较：逐字相同的 rep 对数 / rep 对总数。"""
    by: Dict[str, List[str]] = {}
    for (item, _rep), r in sorted(a.items()):
        by.setdefault(item, []).append(sig(r))
    eq = tot = 0
    for sigs in by.values():
        for x, y in itertools.combinations(sigs, 2):
            tot += 1
            eq += x == y
    return eq, tot


def identical_classes(names: Sequence[str], same) -> List[List[str]]:
    """贪心归类：按 names 顺序，n 与某已有类里任一成员 same 为真就并入第一个这样的类，否则自成一类。
    same 是等价关系（逐字 100% 相同）时结果就是等价类；不是等价关系时不保证传递闭包。"""
    cls: List[List[str]] = []
    for n in names:
        for c in cls:
            if any(same(m, n) for m in c):
                c.append(n)
                break
        else:
            cls.append([n])
    return cls


def seed_check(rows_by_arm: Dict[str, List[Dict[str, Any]]]) -> Tuple[int, Dict[int, set]]:
    """种子核对：返回 (异常行数, rep → 跨臂跨档出现过的 sampling_seed 集合)。异常行 = sampling_seed ≠ seed×1000+rep
    或 sampling_seed_sent 非真。共同随机数成立 ⇔ 异常 0 且每个 rep 的集合大小 1。"""
    seeds: Dict[int, set] = {}
    bad = 0
    for n in sorted(rows_by_arm):
        for r in rows_by_arm[n]:
            s = r.get("sampling_seed")
            rep = int(r["rep"])
            seeds.setdefault(rep, set()).add(s)
            if s != 1000 * int(r.get("seed", 0) or 0) + rep or not r.get("sampling_seed_sent"):
                bad += 1
    return bad, dict(sorted(seeds.items()))


def divergent_sign(a: Dict[Tuple[str, int], Dict[str, Any]], b: Dict[Tuple[str, int], Dict[str, Any]]) -> Dict[str, Any]:
    """只在输出分叉（output_sig 不同）的 (题, rep) 对上数：b 过 a 不过 = b 赢，反之 a 赢，同过同败不计；
    双侧二项（符号）检验 p = A.sign_test_p。(题, rep) 对按独立计，同题多 rep 的相关不校正。"""
    ks = [k for k in sorted(set(a) & set(b)) if output_sig(a[k]) != output_sig(b[k])]
    pa = {k: bool(a[k]["score"].get("pass")) for k in ks}
    pb = {k: bool(b[k]["score"].get("pass")) for k in ks}
    bw = sum(1 for k in ks if pb[k] and not pa[k])
    aw = sum(1 for k in ks if pa[k] and not pb[k])
    return {"diverged": len(ks), "b_wins": bw, "a_wins": aw, "p": A.sign_test_p(bw, aw)}


# ---------------------------------------------------------------- V2：噪声基线
def rep_rates(rows: Dict[Tuple[str, int], Dict[str, Any]]) -> Dict[int, float]:
    """rep → 该 rep 的臂级通过率（该 rep 下全部题的均值）。"""
    by: Dict[int, List[bool]] = {}
    for (_item, rep), r in sorted(rows.items()):
        by.setdefault(rep, []).append(bool(r["score"].get("pass")))
    return {rep: sum(v) / len(v) for rep, v in sorted(by.items())}


def rep_sd(rows: Dict[Tuple[str, int], Dict[str, Any]]) -> Optional[float]:
    """按档 rep 标准差：各 rep 臂级通过率的样本标准差（n−1）；rep < 2 → None。"""
    v = list(rep_rates(rows).values())
    return statistics.stdev(v) if len(v) >= 2 else None


def item_matrix(rows: Dict[Tuple[str, int], Dict[str, Any]]) -> Dict[str, Dict[int, bool]]:
    out: Dict[str, Dict[int, bool]] = {}
    for (item, rep), r in sorted(rows.items()):
        out.setdefault(item, {})[rep] = bool(r["score"].get("pass"))
    return out


def _metric(mat: Dict[str, Dict[int, bool]], reps: Sequence[int], metric: str) -> float:
    """reps 子集上的臂级指标：success = 全部 (题, rep) 通过率；pass_hat_2 = 逐题无偏 Pass^2 的均值。"""
    items = [i for i in sorted(mat) if all(r in mat[i] for r in reps)]
    if metric == "success":
        return sum(mat[i][r] for i in items for r in reps) / (len(items) * len(reps))
    return sum(A.pass_hat_2_of([mat[i][r] for r in reps]) for i in items) / len(items)


def split_null(mat: Dict[str, Dict[int, bool]], m: int, metric: str) -> List[float]:
    """无效应零分布：从同一臂的 rep 里取两组不相交的 m 个 rep 当「两臂」（每组 = 全部题 × m rep），
    |指标差| 的全部无序拆分（rep 数 6、m = 2 → 45 个；m = 3 → 10 个）。拆分之间共用 rep，不独立。"""
    reps = sorted({r for v in mat.values() for r in v})
    out = []
    seen = set()
    for p in itertools.combinations(reps, m):
        rest = [r for r in reps if r not in p]
        for q in itertools.combinations(rest, m):
            key = frozenset((p, q))
            if key in seen:
                continue
            seen.add(key)
            out.append(abs(_metric(mat, q, metric) - _metric(mat, p, metric)))
    return sorted(out)


EPS = 1e-9


def tail_p(null: Sequence[float], v: float) -> Optional[float]:
    """零分布里 P(|Δ| ≥ |v|)（直接按枚举计数；容差 1e-9 吸收浮点误差）。空零分布 → None。"""
    if not null:
        return None
    return sum(1 for x in null if x >= abs(v) - EPS) / len(null)


def null_threshold(null: Sequence[float], alpha: float = 0.05) -> Tuple[Optional[float], Optional[float]]:
    """门槛 t = 零分布可取值里使 P(|Δ| ≥ t) ≤ alpha 的最小值；返回 (t, P(|Δ| ≥ t))。
    零分布最大值的尾概率仍 > alpha（门槛超出支撑）→ (None, None)。零分布离散，不用分位数。"""
    for v in sorted(set(round(x, 12) for x in null)):
        p = tail_p(null, v)
        if p is not None and p <= alpha + EPS:
            return v, p
    return None, None


def tail_table(null: Sequence[float]) -> List[Tuple[float, float]]:
    """零分布每个可取值 v → P(|Δ| ≥ v)。"""
    return [(v, tail_p(null, v)) for v in sorted(set(round(x, 12) for x in null))]


def q_rank(xs: Sequence[float], q: float) -> Optional[float]:
    """最近秩分位数（xs 已排序）：第 ceil(q·n) 个。"""
    if not xs:
        return None
    return xs[max(0, math.ceil(q * len(xs) - 1e-9) - 1)]


def boot_l0(mat: Dict[str, Dict[int, bool]], metric: str) -> Tuple[float, float]:
    """L0 臂级指标的 bootstrap 95% CI：success = 两段（先按题、再题内按 rep 重抽）；pass_hat_2 = 只按题重抽（题内 rep
    原样保留，同 analyze.paired_units）。种子 A.BOOT_SEED、次数 A.BOOT_N。"""
    rng = random.Random(A.BOOT_SEED)
    items = sorted(mat)
    vals = {i: [mat[i][r] for r in sorted(mat[i])] for i in items}
    bs = []
    for _ in range(A.BOOT_N):
        tot = 0.0
        for _k in items:
            v = vals[rng.choice(items)]
            if metric == "success":
                tot += sum(rng.choice(v) for _ in v) / len(v)
            else:
                tot += A.pass_hat_2_of(v)
        bs.append(tot / len(items))
    bs.sort()
    return bs[int(0.025 * A.BOOT_N)], bs[int(0.975 * A.BOOT_N) - 1]


def boot_item_rate(mat: Dict[str, Dict[int, bool]]) -> Tuple[float, float]:
    """臂级成功率的按题聚类 bootstrap 95% CI（只按题重抽，题内 rep 原样；每题值 = 该题 rep 通过率）。"""
    rng = random.Random(A.BOOT_SEED)
    items = sorted(mat)
    val = {i: sum(mat[i].values()) / len(mat[i]) for i in items}
    bs = sorted(sum(val[rng.choice(items)] for _ in items) / len(items) for _b in range(A.BOOT_N))
    return bs[int(0.025 * A.BOOT_N)], bs[int(0.975 * A.BOOT_N) - 1]


def paired_item(units: Dict[str, Tuple[List[List[bool]], List[List[bool]]]], metric: str) -> Optional[Dict[str, Any]]:
    """b − a 按题聚类配对，区间只按题重抽（题内各档 rep 原样，不再抽 rep）。与 analyze.paired_units 同结构、同 RNG 消耗，
    差别只在 success_rate 不做题内 rep 重抽——共同随机数下 rep 是配对的，两段重抽会把 rep 方差重复计入、区间偏宽。
    units[题] = (a 侧各档 rep 列表, b 侧各档 rep 列表)。"""
    def keep(lists):
        return [v for v in lists if v and (metric != "pass_hat_2" or len(v) >= 2)]
    u = {i: (keep(a), keep(b)) for i, (a, b) in units.items()}
    u = {i: ab for i, ab in u.items() if ab[0] and ab[1]}
    if not u:
        return None

    def f(reps):
        return A.pass_hat_2_of(reps) if metric == "pass_hat_2" else sum(reps) / len(reps)

    def side(lists):
        return sum(f(v) for v in lists) / len(lists)
    ids = sorted(u)
    per = {i: side(u[i][1]) - side(u[i][0]) for i in ids}
    rng = random.Random(A.BOOT_SEED)
    boots = sorted(sum(per[rng.choice(ids)] for _k in ids) / len(ids) for _b in range(A.BOOT_N))
    up = sum(1 for i in ids if per[i] > 1e-12)
    down = sum(1 for i in ids if per[i] < -1e-12)
    return {"n_items": len(ids), "diff": sum(per.values()) / len(ids), "lo": boots[int(0.025 * A.BOOT_N)],
            "hi": boots[int(0.975 * A.BOOT_N) - 1], "up": up, "down": down, "p": A.sign_test_p(up, down)}


# ---------------------------------------------------------------- V5：时长
_TS = r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"


def arm_minutes(log_text: str) -> Dict[str, float]:
    """run_colab.log 里 `ARM <臂> … start` 到 `ARM <臂> harness rc=` 的分钟数（同臂多次起止取最后一对）。"""
    st: Dict[str, datetime] = {}
    out: Dict[str, float] = {}
    for ln in log_text.splitlines():
        m = re.match(_TS + r" ARM (\S+) .*start:", ln)
        if m:
            st[m.group(2)] = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            continue
        m = re.match(_TS + r" ARM (\S+) harness rc=", ln)
        if m and m.group(2) in st:
            out[m.group(2)] = (datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") - st[m.group(2)]).total_seconds() / 60
    return out


def batch_span(log_text: str) -> Dict[str, Any]:
    """run_colab.log 全批时间线：首行到 DONE 行的分钟数、READY 时刻、`START deploy` 次数（> 1 = 起机重试过）。"""
    ts = [datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") for m in
          (re.match(_TS, ln) for ln in log_text.splitlines()) if m]
    done = [datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") for m in
            (re.match(_TS + r" DONE ", ln) for ln in log_text.splitlines()) if m]
    ready = [m.group(1) for m in (re.match(_TS + r" READY ", ln) for ln in log_text.splitlines()) if m]
    return {"minutes": (done[-1] - ts[0]).total_seconds() / 60 if ts and done else None,
            "ready": ready, "deploys": sum(1 for ln in log_text.splitlines() if re.match(_TS + r" START deploy", ln))}


# ---------------------------------------------------------------- 格式
def pp(x: Optional[float], d: int = 0) -> str:
    return "—" if x is None else f"{x * 100:+.{d}f}"


def pt(x: Optional[float], d: int = 1) -> str:
    return "—" if x is None else f"{x * 100:.{d}f}"


def pc(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:.0%}"


def short(n: str) -> str:
    return n.replace("orch-turn-", "")


def pair_cell1(p) -> str:
    """同 pair_cell，一位小数（V4：区间端点贴 0 时看得出是否跨 0）。"""
    if p is None:
        return "—"
    return f"{p['diff'] * 100:+.1f} [{p['lo'] * 100:+.1f},{p['hi'] * 100:+.1f}]（{p['up']}↑/{p['down']}↓）"


def pair_cell(p) -> str:
    if p is None:
        return "—"
    return f"{p['diff'] * 100:+.0f} [{p['lo'] * 100:+.0f},{p['hi'] * 100:+.0f}]（{p['up']}↑/{p['down']}↓）"


def tail_cell(d: Optional[float], null: Sequence[float], t: Optional[float]) -> str:
    """观察差 d 的尾概率 P(|Δ| ≥ |d|) 与按门槛 t 的判定（t = None：门槛超出零分布支撑，任何差都判噪声内）。"""
    if d is None:
        return "—"
    p = tail_p(null, d)
    over = t is not None and abs(d) >= t - EPS
    return f"{d * 100:+.0f}（P={'—' if p is None else f'{p:.1%}'}，{'超噪声' if over else '噪声内'}）"


def pt_items(items: Dict[str, List[bool]], reps: Optional[Sequence[int]] = None) -> Dict[str, List[bool]]:
    """逐题 rep 列表；reps 给定时只取这些下标（L0 前 2 rep 与旧批 n=2 设计对齐）。"""
    return {i: ([v[r] for r in reps if r < len(v)] if reps is not None else list(v)) for i, v in items.items()}


# ---------------------------------------------------------------- V 节
def sec_v(G, aggs, arms: List[str], data: Path, prev: Optional[Path], b2G, b2aggs, args) -> List[str]:
    L = ["### V. P2 噪声基线专节（本节 = analyze_p2.py 新增；`@d44` = d44 批同名臂，`@b2` = b2 批同名臂）\n"]
    K = {n: {lv: keyed(G[n]["records"], lv) for lv in LEVELS} for n in arms}
    lvs = {n: [lv for lv in LEVELS if K[n][lv]] for n in arms}

    # V1 ------------------------------------------------------------
    L.append("#### V1. 逐字相同率（先看这个：判 vLLM 逐请求 seed 是否生效）\n")
    L.append("输出签名 = 每轮 (thoughts, text, tool_calls 含参数) + turn_response 的 JSON（排序键）逐字比较，按 (item, rep) 配对；"
             "gens 比 turns 多出的生成（回合上限耗尽时末次生成无对应 turn，transcripts 不存其文本）记 (generation_tokens, truncated, "
             "cancelled, repetition_guard) 进签名。首 prompt token 数 = 首次生成引擎回报 prompt_tokens + fill.prompt_tokens_measured"
             "（只比 token 数，transcripts 不存 prompt 原文）；system.json sha = 会话目录 system.json 的 sha256。\n")
    L.append("V1a. 跨臂（每格 = 输出逐字相同 / 配对数；填充档括号 = 首 prompt token 数相同的对数）：\n")
    L.append("| 臂 a | 臂 b | L0 输出相同 | L0 首 prompt token 数相同 | L0 system.json sha 相同 | 32768 | 49152 | 57344 |")
    L.append("|---|---|---|---|---|---|---|---|")
    same_lv: Dict[Tuple[str, str, int], Tuple[int, int]] = {}
    for a, b in itertools.combinations(arms, 2):
        cells = []
        for lv in LEVELS:
            e, n = identity_rate(K[a][lv], K[b][lv])
            same_lv[(a, b, lv)] = (e, n)
            ei, _ = identity_rate(K[a][lv], K[b][lv], input_sig)
            if lv == 0:
                es, _ = identity_rate(K[a][lv], K[b][lv], lambda r: system_sha(r) or "<无>")
                cells += [f"{e}/{n} = {e / n:.0%}" if n else "—", f"{ei}/{n}", f"{es}/{n}"]
            else:
                cells.append(f"{e}/{n}（{ei}）" if n else "—")
        L.append(f"| {short(a)} | {short(b)} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("- system.json 只存 system 消息（role / content），不含工具声明：sha 相同只说明 system prompt 相同，"
             "分不出 full 与 catalog 前缀（none–full 也全同）。prod 与 catalog 输入相同的证据 = 首 prompt token 数逐对相同 + "
             "system.json sha 相同 + 输出逐字相同三者合起来，不是 prompt 原文比对。")
    xg = {lv: sum(1 for n in arms for r in K[n][lv].values() if len(r.get("gens") or []) > len(r.get("turns") or [])) for lv in LEVELS}
    L.append(f"- gens 比 turns 多的行（签名里只记其 token 数与标记）：全 {len(arms)} 臂合计 {sum(xg.values())}，按档 "
             + "、".join(f"{lv}:{v}" for lv, v in xg.items()))
    lg = [f"{short(n)} {lv} {k[0]} r{k[1]}" for n in arms for lv in LEVELS for k, r in sorted(K[n][lv].items())
          if len(r.get("gens") or []) < len(r.get("turns") or [])]
    L.append(f"- gens 比 turns 少的行（turns 全部进签名，不受影响）：{len(lg)}" + (f"（{'；'.join(lg)}）" if lg else ""))
    nosys = sum(1 for n in arms for r in K[n][0].values() if system_sha(r) is None)
    if nosys:
        L.append(f"- ⚠️ L0 无 system.json 的 run：{nosys}")

    def full_same(x, y, lv):
        e, n = same_lv.get((x, y, lv), same_lv.get((y, x, lv), (0, 0)))
        return n > 0 and e == n
    common = sorted(set.intersection(*[set(K[n][0]) for n in arms]))
    all5 = sum(1 for k in common if len({output_sig(K[n][0][k]) for n in arms}) == 1)
    L.append(f"- 全 {len(arms)} 臂 L0 输出逐字相同：{all5}/{len(common)}")
    cls = identical_classes(arms, lambda x, y: full_same(x, y, 0))
    L.append("- L0 逐字 100% 相同的类：" + "；".join("{" + ", ".join(short(n) for n in c) + "}" for c in cls)
             + "（类内 L0 差恒为 0、CI 零宽，不是零效应证据；独立的 L0 样本只有 " + str(len(cls)) + " 份）")
    for lv in LEVELS[1:]:
        cl = identical_classes(arms, lambda x, y, lv=lv: full_same(x, y, lv))
        js = "；".join("{" + ", ".join(short(n) for n in c) + "}" for c in cl if len(c) > 1)
        L.append(f"- {lv} 档逐字 100% 相同的类：" + (js or "无"))
    in_cls = identical_classes(arms, lambda x, y: (lambda en: en[1] > 0 and en[0] == en[1])(identity_rate(K[x][0], K[y][0], input_sig)))
    L.append("- L0 首 prompt token 数逐 (item, rep) 全同的类：" + "；".join("{" + ", ".join(short(n) for n in c) + "}" for c in in_cls)
             + "；L0 首 prompt 中位：" + "、".join(f"{short(n)} {O.n0(aggs[n][0]['prompt_tokens_median'])}" for n in arms if 0 in aggs[n]))
    all_lv_same = [(a, b) for a, b in itertools.combinations(arms, 2) if all(full_same(a, b, lv) for lv in lvs[a] if lv in lvs[b])]
    L.append("- 全档逐字相同的臂对：" + ("、".join(f"{short(a)}–{short(b)}" for a, b in all_lv_same) or "无"))
    L.append("")
    L.append("V1b. 同臂内不同 rep 之间（同题 rep 两两比；应 ≈ 0%，否则 rep 没换种子）：\n")
    L.append("| 臂 | " + " | ".join(str(lv) for lv in LEVELS) + " |")
    L.append("|---|" + "---|" * len(LEVELS))
    for n in arms:
        cs = []
        for lv in LEVELS:
            e, t = within_rep_identity(K[n][lv])
            cs.append(f"{e}/{t}" if t else "—")
        L.append(f"| {short(n)} | " + " | ".join(cs) + " |")
    L.append("")
    bad_seed, seeds = seed_check({n: G[n]["records"] for n in arms})
    L.append(f"V1c. 种子核对（seed_check）：行 sampling_seed ≠ seed×1000+rep 或 sampling_seed_sent 非真的行 {bad_seed} 条；"
             f"每个 rep 跨臂跨档的种子取值集合大小 " + "、".join(f"r{k}:{len(v)}" for k, v in seeds.items())
             + "（1 = 共同随机数成立）。sampling_seed_applied 是进程内 MLX 播种字段，远端恒 False，不用。\n")
    L.append("V1d. 分叉对符号检验：只在输出分叉的 (题, rep) 对上数 b 过 a 不过（b 赢）/ a 过 b 不过（a 赢），同过同败不计；"
             "双侧二项 p（A.sign_test_p）。(题, rep) 对按独立计，同题多 rep 相关未校正。部分逐字相同的臂对（如 w1–w1w2）"
             "在未分叉的对上差恒 0，V2 的拆分零分布按「全部 (题, rep) 都独立抽样」构造，用在这类臂对上偏保守，本表是补充。\n")
    L.append("| 臂 a | 臂 b | level | 分叉对 | b 赢 / a 赢 | p |")
    L.append("|---|---|---|---|---|---|")
    for a, b in itertools.combinations(arms, 2):
        if (a, b) in all_lv_same:
            continue
        tot = {"diverged": 0, "b_wins": 0, "a_wins": 0}
        for lv in LEVELS:
            ds = divergent_sign(K[a][lv], K[b][lv])
            if not ds["diverged"]:
                continue
            for k in tot:
                tot[k] += ds[k]
            L.append(f"| {short(a)} | {short(b)} | {lv} | {ds['diverged']} | {ds['b_wins']} / {ds['a_wins']} | {A.num(ds['p'], 2)} |")
        L.append(f"| {short(a)} | {short(b)} | **全档** | {tot['diverged']} | {tot['b_wins']} / {tot['a_wins']} | "
                 f"{A.num(A.sign_test_p(tot['b_wins'], tot['a_wins']), 2)} |")
    L.append("")

    # V2 ------------------------------------------------------------
    L.append("#### V2. L0 噪声基线（臂内 6 rep；旧「三臂 L0 差 15 点」口径作废）\n")
    mats = {n: item_matrix(K[n][0]) for n in arms}
    items = sorted(set.intersection(*[set(m) for m in mats.values()]))
    L.append("V2a. 逐题通过数 / rep 数（L0）：\n")
    L.append("| 题 | " + " | ".join(short(n) for n in arms) + " |")
    L.append("|---|" + "---|" * len(arms))
    for i in items:
        L.append(f"| {i} | " + " | ".join(f"{sum(mats[n][i].values())}/{len(mats[n][i])}" for n in arms) + " |")
    L.append("")
    L.append("V2b. 臂级 L0：各 rep 通过率（10 题）、rep 间标准差、合计与 bootstrap 95% CI（种子 "
             f"{A.BOOT_SEED}、{A.BOOT_N} 次）。成功率给两种区间：按题聚类（只按题重抽，题内 rep 原样）与两段（再题内按 rep 重抽，"
             "rep 方差重复计入、偏宽，只作对照）；Pass^2 只按题重抽：\n")
    L.append("| 臂 | 各 rep 通过率 | 均值 | rep 间 SD | 成功率 k/n | 成功率 CI 按题 | 宽 | 成功率 CI 两段 | 宽 | Pass^2（无偏） | Pass^2 CI | 宽 | Pass@2 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    sds: Dict[str, Optional[float]] = {}
    for n in arms:
        if 0 not in aggs[n]:
            continue
        rr = rep_rates(K[n][0])
        sd = rep_sd(K[n][0])
        sds[n] = sd
        a0 = aggs[n][0]
        ilo, ihi = boot_item_rate(mats[n])
        lo, hi = boot_l0(mats[n], "success")
        plo, phi = boot_l0(mats[n], "pass_hat_2")
        L.append(f"| {short(n)} | {' / '.join(f'{v:.0%}' for v in rr.values())} | {pt(statistics.mean(rr.values()))} | "
                 f"{pt(sd)} 点 | {a0['pass_k']}/{a0['n']} | [{pt(ilo, 0)},{pt(ihi, 0)}] | {pt(ihi - ilo, 0)} | [{pt(lo, 0)},{pt(hi, 0)}] | {pt(hi - lo, 0)} | "
                 f"{A.fmt_k(a0['pass2_k'])}/{a0['n_items2']} = {pt(a0['pass_hat_2'])} | [{pt(plo, 0)},{pt(phi, 0)}] | {pt(phi - plo, 0)} | "
                 f"{pt(a0['pass_at_2'])} |")
    L.append("")
    reps_cls = [c[0] for c in cls]
    L.append("V2c. 拆分零分布：从同一臂 L0 的 6 rep 里取两组不相交 rep 当两条假臂（每条 = 10 题 × m rep；m=2 同真实 32K/49K/57K 档设计），"
             f"枚举全部无序拆分得 |Δ| 的零分布。只用 L0 逐字相同类的代表（{', '.join(short(x) for x in reps_cls)}），逐字相同的臂不重复计。"
             "**门槛 t = 零分布可取值里 P(|Δ| ≥ t) ≤ 5% 的最小值**（零分布离散，直接按枚举计尾概率，不用分位数）。"
             "拆分之间共用 rep，尾概率是近似；L0 通过率 ≈ 50–65%（方差接近最大），用到通过率近 0 / 100% 的档偏保守。\n")
    L.append("| 零分布 | m | 拆分数 | |Δ| 中位 | 最大 | 门槛 t（P(|Δ|≥t)） | 尾概率表 v:P(|Δ|≥v) |")
    L.append("|---|---|---|---|---|---|---|")
    nulls: Dict[Tuple[str, int], List[float]] = {}
    thr: Dict[Tuple[str, int], Optional[float]] = {}
    for metric, lab in (("success", "成功率"), ("pass_hat_2", "Pass^2")):
        for m in (2, 3):
            pool: List[float] = []
            for n in reps_cls:
                xs = split_null(mats[n], m, metric)
                pool += xs
                t, tp = null_threshold(xs)
                L.append(f"| {lab} · {short(n)} | {m} | {len(xs)} | {pt(q_rank(xs, .5), 0)} | {pt(xs[-1] if xs else None, 0)} | "
                         f"{'> 最大' if t is None else f'{pt(t, 0)}（{tp:.1%}）'} | "
                         + "、".join(f"{pt(v, 0)}:{p:.1%}" for v, p in tail_table(xs)) + " |")
            pool.sort()
            nulls[(metric, m)] = pool
            t, tp = null_threshold(pool)
            thr[(metric, m)] = t
            L.append(f"| **{lab} · 合并** | {m} | {len(pool)} | {pt(q_rank(pool, .5), 0)} | {pt(pool[-1] if pool else None, 0)} | "
                     f"**{'> 最大' if t is None else f'{pt(t, 0)}（{tp:.1%}）'}** | "
                     + "、".join(f"{pt(v, 0)}:{p:.1%}" for v, p in tail_table(pool)) + " |")
    L.append("")
    sd_vals = [sds[n] for n in reps_cls if sds.get(n) is not None]
    sd_pool = math.sqrt(statistics.mean([x ** 2 for x in sd_vals])) if sd_vals else None
    if sd_pool is not None:
        L.append(f"V2d. 解析近似值（对照）：s = 等价类代表 rep 间 SD 的方差均值开方 = {pt(sd_pool)} 点（由 "
                 + "、".join(f"{short(n)} {pt(sds[n])}" for n in reps_cls if sds.get(n) is not None)
                 + f" 合并，两者差一倍多，s 本身不确定性大）；两条独立臂各 m rep 的成功率差 SD ≈ s·√(2/m)，1.96 倍："
                 f"m=2 → {pt(1.96 * sd_pool)} 点，m=6 → {pt(1.96 * sd_pool * math.sqrt(2 / 6))} 点。只含 rep 间噪声。\n")
    else:
        L.append("V2d. 解析近似值：rep 数不足，无 SD。\n")

    def tlab(metric, m):
        t = thr[(metric, m)]
        return "> 零分布最大值" if t is None else f"≥ {pt(t, 0)} 点（P={tail_p(nulls[(metric, m)], t):.1%}）"
    L.append(f"**建议门槛**：同档两臂（10 题 × 2 rep，m=2 零分布）成功率差 {tlab('success', 2)}、Pass^2 差 {tlab('pass_hat_2', 2)} 才算超出 L0 rep 噪声"
             f"（P(|Δ| ≥ t) ≤ 5%）；L0 两臂各 6 rep 时以 m=3 拆分值作参考（成功率 {tlab('success', 3)}、Pass^2 {tlab('pass_hat_2', 3)}；"
             "6 rep 对 6 rep 的真实噪声更小，m=3 值偏保守）。逐字相同的臂对不适用（差恒 0）。下表凡「超噪声 / 噪声内」都按此判，并给观察差的尾概率。\n")

    # V3 ------------------------------------------------------------
    L.append("#### V3. 五臂 × 四档与 d44 / b2 同臂同档（成功率 k/n；Pass^2 = Σ逐题无偏 / 题数；L0 本批 n=6 rep）\n")
    L.append("口径差异：本批 prod 臂（none/w1/w1w2）生效前缀 = catalog（D44 (iii) c76492f），d44 / b2 的 prod 生效 = full；"
             "所以 d44 / b2 的 w1w2 与本批 **full** 臂同形态（guard on / step on / full 前缀），表里「同形态」列按此对齐。"
             "d44 / b2 无 seed（sampling_seed_sent 缺键），本批 seed 透传、臂间共同随机数。"
             "b2 → d44 → 本批之间另有生产代码改动（见方案 §6），跨批差不只归前缀。L0「前 2 rep」= 本批 rep 0/1，与旧批 n=2 设计对齐。\n")
    L.append("| 本批臂 | level | 本批 成功率 | 本批 Pass^2 | Pass@2 | L0 前 2 rep 成功率 / Pass^2 | d44 同名 成功率 / Pass^2 | b2 同名 成功率 / Pass^2 | 同形态 d44 / b2 臂 | 同形态 d44 成功率 / Pass^2 | 同形态 b2 成功率 / Pass^2 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")

    def sp(ag, lv):
        a = (ag or {}).get(lv)
        if not a:
            return "—"
        return f"{a['pass_k']}/{a['n']} / {A.fmt_k(a['pass2_k'])}/{a['n_items2']}"
    for n in arms:
        fm = FORM_MATCH.get(n, n)
        for lv in LEVELS:
            a = aggs[n].get(lv)
            if not a:
                continue
            first2 = "—"
            if lv == 0:
                sub = pt_items(a["_items"], (0, 1))
                two = [x for x in sub.values() if len(x) == 2]
                first2 = (f"{sum(sum(x) for x in sub.values())}/{sum(len(x) for x in sub.values())} / "
                          f"{A.fmt_k(A._ksum([A.pass_hat_2_of(x) for x in two]))}/{len(two)}")
            L.append(f"| {short(n)} | {lv} | {a['pass_k']}/{a['n']} = {pc(a['success_rate'])} | {A.fmt_k(a['pass2_k'])}/{a['n_items2']} = {pc(a['pass_hat_2'])} | "
                     f"{pc(a['pass_at_2'])} | {first2} | {sp(aggs.get(n + TAG), lv)} | {sp(b2aggs.get(n + TAG_B2), lv)} | "
                     f"{short(fm)} | {sp(aggs.get(fm + TAG), lv)} | {sp(b2aggs.get(fm + TAG_B2), lv)} |")
    L.append("")
    L.append("V3b. 本批 − d44 同形态臂（共同题集点差，A.common_diff；L0 取本批前 2 rep，与 d44 的 2 rep 对齐；括号 = m=2 零分布里 "
             "P(|Δ| ≥ |观察差|) 与按 V2 门槛的判定）：\n")
    L.append("| 本批臂 | d44 臂 | level | 成功率差 | Pass^2 差 |")
    L.append("|---|---|---|---|---|")
    for n in arms:
        fm = FORM_MATCH.get(n, n)
        if fm + TAG not in aggs:
            continue
        for lv in LEVELS:
            a_old, a_new = aggs[fm + TAG].get(lv), aggs[n].get(lv)
            if not a_old or not a_new:
                continue
            new_items = pt_items(a_new["_items"], (0, 1) if lv == 0 else None)
            ds, _ = A.common_diff(a_old["_items"], new_items, "success_rate")
            dh, _ = A.common_diff(a_old["_items"], new_items, "pass_hat_2")
            L.append(f"| {short(n)} | {short(fm)}{TAG} | {lv} | {tail_cell(ds, nulls[('success', 2)], thr[('success', 2)])} | "
                     f"{tail_cell(dh, nulls[('pass_hat_2', 2)], thr[('pass_hat_2', 2)])} |")
    L.append("")
    L.append("拐点（analyze.knee：metric Pass^2、基线候选 [0, 4096]、drop 0.20、recover 0.10；本批 L0 基线 n=6，其余档 n=2）：\n")
    for n in arms + [x + TAG for x in arms if x + TAG in aggs] + [x + TAG_B2 for x in arms if x + TAG_B2 in b2aggs]:
        ag = aggs.get(n) or b2aggs.get(n)
        kn = A.knee(ag, "pass_hat_2", args.baseline_levels, args.drop, args.recover)
        L.append(f"- {n}：{kn['verdict']}" + (f"（基线 L{kn['base_level']} = {pc(kn['base'])}）" if kn.get("base") is not None else ""))
    L.append("")

    # V4 ------------------------------------------------------------
    cat, w12 = "orch-turn-catalog", "orch-turn-w1w2"
    L.append(f"#### V4. full 臂对 catalog / w1w2（b − a，按题配对；区间只按题聚类重抽，种子 {A.BOOT_SEED}）\n")
    if all(lv in lvs[cat] and lv in lvs[w12] for lv in lvs[FULL]):
        eqs = [same_lv.get((w12, cat, lv), same_lv.get((cat, w12, lv), (0, 0))) for lv in lvs[FULL]]
        L.append(f"w1w2 – catalog 逐字相同（按档，V1a）：" + "、".join(f"{lv}:{e}/{t}" for lv, (e, t) in zip(lvs[FULL], eqs))
                 + ("——全档相同，两者对 full 的差完全一样，只列 catalog。" if all(t and e == t for e, t in eqs) else "——不全相同，两对都列。"))
        pairs = [(FULL, cat)] + ([] if all(t and e == t for e, t in eqs) else [(FULL, w12)])
    else:
        pairs = [(FULL, cat)]
    L.append("区间 = paired_item（只按题重抽、题内 rep 原样）；analyze.paired_units 的成功率两段重抽在共同随机数下把 rep 方差重复计入、"
             "区间偏宽（E 节、Q 节、U3 用的是它），本节不用。尾概率 = V2c 零分布 P(|Δ| ≥ |观察差|)：32K/49K/57K 用 m=2，"
             "L0（6 rep 对 6 rep）用 m=3 拆分值（偏保守）。\n")
    L.append("| 对比 | level | 成功率 a → b | 成功率差（尾概率，判定） | 按题 95% CI（题升/降） | Pass^2 a → b | Pass^2 差（尾概率，判定） | 按题 95% CI（题升/降） |")
    L.append("|---|---|---|---|---|---|---|---|")
    for a_n, b_n in pairs:
        lv_c = [lv for lv in LEVELS if lv in aggs[a_n] and lv in aggs[b_n]]
        for lv in lv_c:
            aa, bb = aggs[a_n][lv], aggs[b_n][lv]
            units = {i: ([aa["_items"][i]], [bb["_items"][i]]) for i in aa["_items"] if i in bb["_items"]}
            ps, ph = paired_item(units, "success_rate"), paired_item(units, "pass_hat_2")
            m = 3 if lv == 0 else 2
            L.append(f"| {short(b_n)} − {short(a_n)} | {lv} | {pc(aa['success_rate'])} → {pc(bb['success_rate'])} | "
                     f"{tail_cell(ps['diff'] if ps else None, nulls[('success', m)], thr[('success', m)])} | {pair_cell1(ps)} | "
                     f"{pc(aa['pass_hat_2'])} → {pc(bb['pass_hat_2'])} | "
                     f"{tail_cell(ph['diff'] if ph else None, nulls[('pass_hat_2', m)], thr[('pass_hat_2', m)])} | {pair_cell1(ph)} |")
        for lab, sel in (("四档合并", lv_c), ("三填充档合并", [lv for lv in lv_c if lv > 0])):
            items_u = sorted({i for lv in sel for i in aggs[a_n][lv]["_items"]} | {i for lv in sel for i in aggs[b_n][lv]["_items"]})
            units = {i: ([aggs[a_n][lv]["_items"][i] for lv in sel if i in aggs[a_n][lv]["_items"]],
                         [aggs[b_n][lv]["_items"][i] for lv in sel if i in aggs[b_n][lv]["_items"]]) for i in items_u}
            ps, ph = paired_item(units, "success_rate"), paired_item(units, "pass_hat_2")
            L.append(f"| {short(b_n)} − {short(a_n)} | {lab} | — | {pp(ps['diff'] if ps else None)}（合并无零分布） | {pair_cell1(ps)} | — | "
                     f"{pp(ph['diff'] if ph else None)} | {pair_cell1(ph)} |")
    L.append("")
    hi_lv = max(lvs[FULL]) if lvs[FULL] else None
    L.append(f"首 prompt 中位（L0 / {hi_lv}）：" + "、".join(
        f"{short(n)} {O.n0(aggs[n].get(0, {}).get('prompt_tokens_median'))} / {O.n0(aggs[n].get(hi_lv, {}).get('prompt_tokens_median'))}"
        for n in (FULL, cat)))
    cd = cat + TAG
    if cd in aggs and 0 in aggs[cd] and 0 in aggs[cat]:
        x0, x1 = aggs[cd][0]["prompt_tokens_median"], aggs[cat][0]["prompt_tokens_median"]
        L.append(f"catalog 臂 L0 首 prompt 中位 d44 → 本批：{O.n0(x0)} → {O.n0(x1)}（{x1 - x0:+,.0f}；D44 (iii) core 加 run_math / rag_search）。")
    L.append("前缀元数据（行内字段；prod 臂按 full 声明计、与实际送入的 catalog 前缀不符）：" + "；".join(
        f"{short(n)} prefix_mode={sorted({r.get('prefix_mode') for r in G[n]['records']})} tool_schema_mode_effective="
        f"{sorted({str(r.get('tool_schema_mode_effective')) for r in G[n]['records']})} prefix_base_tokens="
        f"{sorted({r.get('prefix_base_tokens') for r in G[n]['records']}, key=str)} agent_cfg_fingerprint="
        f"{sorted({str(r.get('agent_cfg_fingerprint')) for r in G[n]['records']})}" for n in arms))
    L.append("")

    # V5 ------------------------------------------------------------
    L.append("#### V5. 时长（run_colab.log 臂起止；Σwall = transcripts 行 wall_s 之和）\n")
    cur = arm_minutes((data / "run_colab.log").read_text(encoding="utf-8"))
    old = arm_minutes((prev / "run_colab.log").read_text(encoding="utf-8")) if prev and (prev / "run_colab.log").is_file() else {}
    L.append("| 臂 | 本批墙钟 min | d44 同形态臂 | d44 墙钟 min | 墙钟增量 min | 本批 Σwall L0 min（6 rep） | 本批 Σwall 填充三档 min | L0 多出 4 rep 估计 min（= L0 Σwall × 4/6） | d44 Σwall L0 / 填充三档 min |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for n in arms:
        rs = G[n]["records"]
        w0 = sum(r.get("wall_s") or 0 for r in rs if int(r["level"]) == 0) / 60
        wh = sum(r.get("wall_s") or 0 for r in rs if int(r["level"]) > 0) / 60
        od = "—"
        fm = FORM_MATCH.get(n, n)
        if fm + TAG in G:
            ro = G[fm + TAG]["records"]
            od = f"{sum(r.get('wall_s') or 0 for r in ro if int(r['level']) == 0) / 60:.1f} / {sum(r.get('wall_s') or 0 for r in ro if int(r['level']) > 0) / 60:.1f}"
        L.append(f"| {short(n)} | {cur.get(n, float('nan')):.1f} | {short(fm)} | {('%.1f' % old[fm]) if fm in old else '—'} | {('%+.1f' % (cur[n] - old[fm])) if fm in old and n in cur else '—'} | {w0:.1f} | {wh:.1f} | {w0 * 4 / 6:.1f} | {od} |")
    tot = sum(cur.values())
    L.append("")
    L.append(f"- 本批五臂墙钟合计 {tot:.1f} min（{tot / 60:.2f} h）；d44 四臂合计 {sum(old.values()):.1f} min。")
    bs = batch_span((data / "run_colab.log").read_text(encoding="utf-8"))
    mins = "—" if bs["minutes"] is None else f"{bs['minutes']:.1f} min（{bs['minutes'] / 60:.2f} h）"
    L.append(f"- 本批 run_colab.log 首行 → DONE：{mins}；READY 时刻 {bs['ready']}；"
             f"`START deploy` {bs['deploys']} 次（> 1 = 首次起机未 READY 后按 READY_RETRY 重试）。")
    if prev and (prev / "run_colab.log").is_file():
        bo = batch_span((prev / "run_colab.log").read_text(encoding="utf-8"))
        L.append(f"- d44 run_colab.log 首行 → DONE：{'—' if bo['minutes'] is None else format(bo['minutes'], '.1f') + ' min（' + format(bo['minutes'] / 60, '.2f') + ' h）'}；`START deploy` {bo['deploys']} 次。")
    L.append("")
    return L


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P2 噪声基线批复算（编排器五臂）")
    ap.add_argument("--data", required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--prev", default=None, help="d44 批目录（同名四臂，组名加 @d44）")
    ap.add_argument("--b2", default=None, help="b2 批目录（同名四臂，只进 V3 对照）")
    ap.add_argument("--out-md", default=None)
    ap.add_argument("--out-csv", default=None)
    a = ap.parse_args(argv)
    data, ref = Path(a.data), Path(a.ref)
    arms = O.orch_arms(data)
    missing = [n for n in O.B2_ORCH + [FULL] if not (data / n / "transcripts.jsonl").is_file()]
    if missing:
        raise SystemExit(f"❌ P2 布局缺臂：{missing}")
    prev = Path(a.prev) if a.prev else None
    prev_arms = [n for n in arms if prev and (prev / n / "transcripts.jsonl").is_file()]
    if a.prev and not prev_arms:
        raise SystemExit(f"❌ --prev {a.prev} 下没有 {arms} 任何一臂")
    b2 = Path(a.b2) if a.b2 else None
    b2_arms = [n for n in arms if b2 and (b2 / n / "transcripts.jsonl").is_file()]
    ref_key = os.path.basename(os.path.normpath(a.ref))
    if ref_key in arms or ref_key in {n + TAG for n in prev_arms}:
        raise SystemExit(f"❌ 参照目录名「{ref_key}」与臂名冲突")
    saved = (O.REF, O.ARMS, O.EXTRA_ORCH, A.comparisons, O.RAG_NOTE)
    saved_a = (A.KEEP, A.th_global, A.wall_global)
    saved_d = (D.ARMS, D.MANAGED, D.TAG)
    D.ARMS, D.MANAGED, D.TAG = arms, arms[1:], TAG
    O.REF, O.ARMS, O.EXTRA_ORCH = ref_key, list(arms), ["orch-turn-catalog"] + arms[len(O.B2_ORCH):]
    O.RAG_NOTE = "rag_search 在本批是 W-K2 可用桩（回生产空结果形态）"
    try:
        args = Namespace(metric="pass_hat_2", drop=0.20, recover=0.10, baseline_levels=[0, 4096], high_from=16384,
                         max_model_len=65536, gen_reserve=12288, prod_prefix=None, pair=[])
        A.KEEP = "latest"
        A.th_global = dict(COHERENT_THRESHOLDS)
        A.wall_global = {x for x in A.DEFAULT_WALL_INVALID.split(",") if x}
        A.comparisons = lambda groups: []
        dirs = [ref] + [data / n for n in arms]
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
        D.attach_sessions(G, data, arms)
        if prev_arms:
            D.attach_sessions(G, prev, prev_arms, TAG)
        # 生产前缀 = catalog 臂行内值（D44 (iii) 后 prod 生效 = catalog；prod 臂行内仍是 full 声明计数）
        pb = sorted({r.get("prefix_base_tokens") for r in G["orch-turn-catalog"]["records"]} - {None})
        args.prod_prefix = max(pb) if pb else None
        warn = None if len(pb) == 1 else f"orch-turn-catalog 的 prefix_base_tokens 不唯一：{pb or '全缺'}，取较大值"
        args.pair = [f"{n}{TAG}:{n}" for n in prev_arms] + ([f"orch-turn-w1w2{TAG}:{FULL}"] if "orch-turn-w1w2" in prev_arms else []) + [
            "orch-turn-none:orch-turn-w1", "orch-turn-none:orch-turn-w1w2", "orch-turn-w1:orch-turn-w1w2",
            "orch-turn-w1w2:orch-turn-catalog", f"{FULL}:orch-turn-catalog", f"{FULL}:orch-turn-w1w2"]
        aggs = {g["name"]: A.aggregate(g, A.th_global, A.wall_global) for g in groups}
        b2G, b2aggs = {}, {}
        for n in b2_arms:
            g = A.load_group(b2 / n)
            g["name"] = n + TAG_B2
            b2G[g["name"]] = g
            b2aggs[g["name"]] = A.aggregate(g, A.th_global, A.wall_global)
        prev_names = [n + TAG for n in prev_arms]
        parts = [A.render(groups, aggs, args), ""]
        parts += O.sec_i(G, aggs, raws, evs, args)
        parts += O.sec_j(G, aggs, raws, evs)
        parts += O.sec_k(G, evs)
        parts += O.sec_m(G, aggs, raws)
        parts += O.sec_n(G, raws, evs, data, logs=("run_colab.log",))
        parts += O.sec_o(G, aggs, arms + prev_names)
        parts += O.sec_p(G, aggs, evs, arms + prev_names, prev_names)
        q = O.sec_q(G, aggs, evs)
        q[0] = q[0].replace("（prod 前缀）", "（prod 前缀；本批生效 = catalog，见 V1）", 1)
        parts += q
        q = O.sec_q(G, aggs, evs, a_name=FULL, b_name="orch-turn-catalog")
        q[0] = q[0].replace("（prod 前缀）", "（full 前缀）", 1)
        parts += q
        if "orch-turn-w1w2" + TAG in G and "orch-turn-catalog" + TAG in G:
            parts += O.sec_q(G, aggs, evs, a_name="orch-turn-w1w2" + TAG, b_name="orch-turn-catalog" + TAG)
        u = D.sec_u(G, aggs, evs, False)
        u[0] = "### U. analyze_d44 U 节（D43 (a) / D43b / D44 机制计数；本批只列本批五臂，不并跨批列）\n"
        parts += u
        parts += sec_v(G, aggs, arms, data, prev, b2G, b2aggs, args)
        auto = "\n".join(parts).rstrip() + "\n"
        auto = auto.replace("`@am` = 11-02 上午批同名臂", f"`{TAG}` = d44 批同名臂")
        marker = "_自动生成：`benchmarks/context_knee/analyze.py`；"
        if marker not in auto:
            raise SystemExit(f"❌ analyze.render 输出里找不到预期的『{marker}』")
        auto = auto.replace(marker, "_自动生成：`benchmarks/context_knee/analyze_p2.py`（P2 布局：A–F 节 = analyze.render 原样，"
                            "自动对照关闭、E 节只列指定对；I/J/K/M/N/O/P/Q 节 = analyze_orch 原样，R/T 节本批无数据不渲染；"
                            "U 节 = analyze_d44 原样（只列本批）；V 节新增；"
                            f"F 节生产前缀取 orch-turn-catalog 行内 prefix_base_tokens {args.prod_prefix}"
                            + (f"；⚠️ {warn}" if warn else "") + f"；`{TAG}` = --prev 批同名臂）；", 1)
        note = "（engine.py:140 注释的 M5 实测）"
        if note in auto:
            auto = auto.replace(note, "（取 orch-turn-catalog 行内字段 prefix_base_tokens，见上）", 1)
        rerun = ("_复跑：`PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/context_knee/analyze_p2.py "
                 f"--data {a.data} --ref {a.ref}" + (f" --prev {a.prev}" if a.prev else "") + (f" --b2 {a.b2}" if a.b2 else "")
                 + (f" --out-md {a.out_md}" if a.out_md else "") + (f" --out-csv {a.out_csv}" if a.out_csv else "") + "`_\n\n")
        auto = rerun + auto
        if a.out_csv:
            O.write_csv_b2(Path(a.out_csv), groups, aggs, O.b2_csv_extra(G, evs, arms + prev_names))
        if a.out_md:
            A.write_md(Path(a.out_md), auto)
        else:
            print(auto)
        return 0
    finally:
        O.REF, O.ARMS, O.EXTRA_ORCH, A.comparisons, O.RAG_NOTE = saved
        A.KEEP, A.th_global, A.wall_global = saved_a
        D.ARMS, D.MANAGED, D.TAG = saved_d


if __name__ == "__main__":
    sys.exit(main())
