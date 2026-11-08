"""11-07 Colab P2 噪声基线批（编排器五臂、seed 透传、L0 6 rep）：analyze_d44 的 A–Q / U 节 + V 节。

  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/context_knee/analyze_p2.py \
      --data benchmarks/data/context-knee-20261107-p2 --ref benchmarks/data/context-knee-20261031/qwen-current \
      --prev benchmarks/data/context-knee-20261105-d44 --b2 benchmarks/data/context-knee-20261102-b2 \
      [--p2 <上一同种子批>] [--out-md <md>] [--out-csv <data>/summary_by_level.csv]

布局（与 analyze_d44 的差别）：
- 本批臂 = O.orch_arms(--data)：B2_ORCH 四臂 + orch-turn-full；--prev = d44 批同名四臂，组名后缀 `@d44`；
  --b2 = b2 批同名四臂，只在 V 节对照表里用（不进 A–Q，免得 A–F 表再翻一倍）。
- F 节生产前缀取 orch-turn-catalog 行内 prefix_base_tokens：D44 (iii)（c76492f）后 prod 生效档位 = catalog，
  而 prod 臂行内 prefix_base_tokens / prefix_n_tools 仍按 full 声明计（run_knee.build_prefixes 用 production_decl("prod")），
  取 orch-turn-none 的值会把 F 节算成 full 前缀。V 节有逐臂对照。
- U 节 = analyze_d44.sec_u(prev_ok=False)：只列本批，不并跨批列（sec_u 的跨批文字写死 b2）。
- V 节（本文件新增）：V1 逐字相同率（跨臂 / 臂内跨 rep / 种子核对）→ V2 L0 噪声基线 → V3 五臂 × 四档与 d44 / b2
  → V4 full 对 catalog / w1w2 → V5 时长。
- catalog 臂可缺（11-08 p2b 批起删臂）：已知臂 = O.B2_ORCH 里目录在的；必需 = O.TURN 三臂 + orch-turn-full。缺 catalog 时
  Q / U3 / V4 里对 catalog 的部分显式写「本批无 catalog 臂」；Q / V4 的 full 对照改用 w1w2（p2 批 V1a：w1w2 与 catalog
  全档逐字相同），并列出 w1w2 行内 prefix_decl 作证。不做静默替换。F 节生产前缀：有 catalog 臂取其行内值；否则取
  orch-turn-none 行内值，前提是行内 prefix_decl == tool_schema_mode_effective（2d3fd99c 起前缀元数据随生效档位）。
- W 节（--p2 给出时）：本批对上一同种子批（同 KNEE_SEED → 同 per-request 种子）逐臂逐档——W1 跨批逐字相同与输入差
  （首 prompt token 差、system.json sha / 文本差）→ W2 成功率 / Pass^2 / recall 按题配对差 → W3 分叉对符号检验
  → W4 各批曲线与拐点。
- 同种子配对的 rep 对齐（aligned / e_pairs）：V4、W2 与 E 节本批臂间对照里，两侧某档 rep 编号集合不同（L0 6 rep 对 2 rep）
  时两侧都截到共同 rep 再配对，格内 / 标签注明「rep 0–1 配对」；跨批 @d44 对（无 seed）不截。
- F 节生产前缀取不到（prod_prefix 返回 None）→ SystemExit，不把 None 带进 analyze.render。
只读：各臂 transcripts.jsonl / guardrail_events.jsonl / sessions/ / run_colab.log（本批与 --prev）。
CPU、标准库 + analyze / analyze_orch / analyze_d44；bootstrap 种子固定（A.BOOT_SEED）→ 输出确定。
"""
from __future__ import annotations

import argparse
import difflib
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
CAT, W12 = "orch-turn-catalog", "orch-turn-w1w2"
TAG_P2 = "@p2"
NO_CAT = "本批无 catalog 臂（11-07 拍板删臂）"


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
    """b − a 按题聚类配对，区间只按题重抽（题内各档 rep 原样，不再抽 rep）。自 2d3fd99c 起与 analyze.paired_units
    同口径（同结构、同 RNG 消耗、成功率也只按题重抽），结果相同；返回字段少 median。留在本文件是因为 V4 / W2 先于该改动写成。
    rep 不在这里对齐：两侧 rep 数不同时调用方先用 aligned() 截到共同 rep。units[题] = (a 侧各档 rep 列表, b 侧各档 rep 列表)。"""
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


def lv_reps(rows: Iterable[Dict[str, Any]], lv: int) -> set:
    """该档出现过的 rep 编号集合。"""
    return {int(r["rep"]) for r in rows if int(r["level"]) == lv}


def lv_items(rows: Iterable[Dict[str, Any]], lv: int, reps: Optional[Iterable[int]] = None) -> Dict[str, List[bool]]:
    """该档逐题 rep 通过列表（analyze.per_item 同口径 = aggregate 的 _items）；reps 给定时只取这些 rep 编号的行。"""
    keep = None if reps is None else set(reps)
    return A.per_item([r for r in rows if int(r["level"]) == lv and (keep is None or int(r["rep"]) in keep)])


def rep_lab(reps: Iterable[int]) -> str:
    """rep 编号集合 → 「rep 0–1」（连续）/「rep 0,2,5」。"""
    rs = sorted(reps)
    if not rs:
        return "rep 无"
    if len(rs) > 1 and rs == list(range(rs[0], rs[-1] + 1)):
        return f"rep {rs[0]}–{rs[-1]}"
    return "rep " + ",".join(map(str, rs))


def aligned(rows_a: Sequence[Dict[str, Any]], rows_b: Sequence[Dict[str, Any]], lv: int,
            names: Tuple[str, str] = ("a", "b")) -> Dict[str, Any]:
    """同种子配对的一档：两侧 rep 编号集合不同（如 L0 6 rep 对 2 rep）时两侧都截到共同 rep——共同随机数下 rep r 只与
    rep r 配对，多出的 rep 没有对手，留着会把「6 对 2」当配对比（同输入同输出的两臂也比出非零差）。
    返回 items_a / items_b / rows_a / rows_b（截后）、reps（共同 rep 编号）、note（截了时「rep 0–1 配对，full 6→2」，否则 ""）。"""
    ra, rb = lv_reps(rows_a, lv), lv_reps(rows_b, lv)
    if ra == rb:
        keep, note = None, ""
        reps = ra
    else:
        keep = reps = ra & rb
        cut = [f"{short(n)} {len(x)}→{len(reps)}" for n, x in zip(names, (ra, rb)) if len(x) != len(reps)]
        note = f"{rep_lab(reps)} 配对，{'、'.join(cut)} rep"
    sa = [r for r in rows_a if int(r["level"]) == lv and (keep is None or int(r["rep"]) in keep)]
    sb = [r for r in rows_b if int(r["level"]) == lv and (keep is None or int(r["rep"]) in keep)]
    return {"items_a": A.per_item(sa), "items_b": A.per_item(sb), "rows_a": sa, "rows_b": sb, "reps": sorted(reps), "note": note}


def items_rate(items: Dict[str, List[bool]]) -> Optional[float]:
    n = sum(len(v) for v in items.values())
    return sum(sum(v) for v in items.values()) / n if n else None


def items_cell(items: Dict[str, List[bool]], key: str) -> str:
    """逐题 rep 列表 → 「k/n = 率」（success）/「Σ逐题无偏/题数 = 率」（pass_hat_2；rep ≥ 2 的题），与 _kn 同格式。"""
    if key == "success":
        n = sum(len(v) for v in items.values())
        return "—" if not n else f"{sum(sum(v) for v in items.values())}/{n} = {pc(items_rate(items))}"
    two = [v for v in items.values() if len(v) >= 2]
    if not two:
        return "—"
    k = A._ksum([A.pass_hat_2_of(v) for v in two])
    return f"{A.fmt_k(k)}/{len(two)} = {pc(k / len(two))}"


def items_ph2(items: Dict[str, List[bool]]) -> Optional[float]:
    two = [v for v in items.values() if len(v) >= 2]
    return sum(A.pass_hat_2_of(v) for v in two) / len(two) if two else None


def l0_rep_text(K, names: Sequence[str]) -> str:
    """各臂 L0 rep 数：全同 → 「6 rep」；不同 → 「none 6 / w1 2 / … rep」。"""
    cnt = {n: len({k[1] for k in K[n][0]}) for n in names}
    vals = set(cnt.values())
    if len(vals) == 1:
        return f"{vals.pop()} rep"
    return " / ".join(f"{short(n)} {c}" for n, c in cnt.items()) + " rep"


def pooled_aligned(al: Dict[int, Dict[str, Any]], sel: Sequence[int]) -> Tuple[Any, Any, str]:
    """多档合并按题配对（paired_item），每档用 aligned() 截后的 items；返回 (成功率, Pass^2, 截档注记)。"""
    items_u = sorted({i for lv in sel for x in ("items_a", "items_b") for i in al[lv][x]})
    units = {i: ([al[lv]["items_a"][i] for lv in sel if i in al[lv]["items_a"]],
                 [al[lv]["items_b"][i] for lv in sel if i in al[lv]["items_b"]]) for i in items_u}
    cut = [f"{lv} 档 {rep_lab(al[lv]['reps'])}" for lv in sel if al[lv]["note"]]
    return (paired_item(units, "success_rate"), paired_item(units, "pass_hat_2"),
            f"（{'、'.join(cut)} 配对）" if cut else "")


def w12_is_catalog(G) -> Tuple[List[str], List[str], bool]:
    """w1w2 行内 (prefix_decl 取值, tool_schema_mode_effective 取值, 两者都恰为 ["catalog"])——无 catalog 臂时以 w1w2 代 catalog 的前提。"""
    rs = G[W12]["records"]
    decl = sorted({str(r.get("prefix_decl")) for r in rs})
    eff = sorted({str(r.get("tool_schema_mode_effective")) for r in rs})
    return decl, eff, decl == eff == ["catalog"]


# ---------------------------------------------------------------- V 节
def sec_v(G, aggs, arms: List[str], data: Path, prev: Optional[Path], b2G, b2aggs, args,
          stash: Optional[Dict[str, Any]] = None) -> List[str]:
    """stash 给出时写入 nulls / thr（V2c 零分布与门槛），供 W 节判噪声。"""
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
             "分不出 full 与 catalog 前缀（none–full 也全同）。" + (
                 "prod 与 catalog 输入相同的证据 = 首 prompt token 数逐对相同 + system.json sha 相同 + 输出逐字相同三者合起来，"
                 "不是 prompt 原文比对。" if CAT in arms else
                 f"{NO_CAT}；prod 臂生效前缀看行内 prefix_decl / tool_schema_mode_effective（V4 末行）。"))
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
    L.append(f"#### V2. L0 噪声基线（臂内 {l0_rep_text(K, arms)}；旧「三臂 L0 差 15 点」口径作废）\n")
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
    L.append(f"V2c. 拆分零分布：从同一臂 L0 的 {l0_rep_text(K, reps_cls)} 里取两组不相交 rep 当两条假臂（每条 = 10 题 × m rep；m=2 同真实 32K/49K/57K 档设计），"
             f"枚举全部无序拆分得 |Δ| 的零分布。只用 L0 逐字相同类的代表（{', '.join(short(x) for x in reps_cls)}），逐字相同的臂不重复计。"
             "**门槛 t = 零分布可取值里 P(|Δ| ≥ t) ≤ 5% 的最小值**（零分布离散，直接按枚举计尾概率，不用分位数）。"
             "拆分之间共用 rep，尾概率是近似；本批 L0 通过率 " + "、".join(f"{short(n)} {pc(aggs[n][0]['success_rate'])}" for n in reps_cls if 0 in aggs[n])
             + "；填充档的题级通过率分布可能与 L0 不同（本节未逐档核算），用到通过率近 0 / 100% 的档时门槛偏保守。"
             "rep 数 < 2m 的代表拆不出零分布（拆分数 0）。\n")
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
                 + " 合并" + (("，两者差一倍多" if len(sd_vals) == 2 else "，最大与最小差一倍多")
                              if len(sd_vals) >= 2 and min(sd_vals) > 0 and max(sd_vals) / min(sd_vals) >= 2 else "")
                 + f"，s 本身不确定性大）；两条独立臂各 m rep 的成功率差 SD ≈ s·√(2/m)，1.96 倍："
                 f"m=2 → {pt(1.96 * sd_pool)} 点，m=6 → {pt(1.96 * sd_pool * math.sqrt(2 / 6))} 点。只含 rep 间噪声。\n")
    else:
        L.append("V2d. 解析近似值：rep 数不足，无 SD。\n")

    def tlab(metric, m):
        t = thr[(metric, m)]
        return "> 零分布最大值" if t is None else f"≥ {pt(t, 0)} 点（P={tail_p(nulls[(metric, m)], t):.1%}）"
    L.append(f"**建议门槛**：同档两臂（10 题 × 2 rep，m=2 零分布）成功率差 {tlab('success', 2)}、Pass^2 差 {tlab('pass_hat_2', 2)} 才算超出 L0 rep 噪声"
             f"（P(|Δ| ≥ t) ≤ 5%）；L0 两臂各 6 rep 时以 m=3 拆分值作参考（成功率 {tlab('success', 3)}、Pass^2 {tlab('pass_hat_2', 3)}；"
             "6 rep 对 6 rep 的真实噪声更小，m=3 值偏保守）。逐字相同的臂对不适用（差恒 0）。下表凡「超噪声 / 噪声内」都按此判，并给观察差的尾概率。\n")
    if stash is not None:
        stash.update(nulls=nulls, thr=thr)

    # V3 ------------------------------------------------------------
    L.append(f"#### V3. {len(arms)} 臂 × 四档与 d44 / b2 同臂同档（成功率 k/n；Pass^2 = Σ逐题无偏 / 题数；L0 本批 {l0_rep_text(K, arms)}）\n")
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
    L.append(f"拐点（analyze.knee：metric Pass^2、基线候选 [0, 4096]、drop 0.20、recover 0.10；本批 L0 基线 {l0_rep_text(K, arms)}，"
             f"其余档 {'/'.join(sorted({str(len({k[1] for k in K[n][lv]})) for n in arms for lv in LEVELS[1:] if K[n][lv]})) or '—'} rep）：\n")
    for n in arms + [x + TAG for x in arms if x + TAG in aggs] + [x + TAG_B2 for x in arms if x + TAG_B2 in b2aggs]:
        ag = aggs.get(n) or b2aggs.get(n)
        kn = A.knee(ag, "pass_hat_2", args.baseline_levels, args.drop, args.recover)
        L.append(f"- {n}：{kn['verdict']}" + (f"（基线 L{kn['base_level']} = {pc(kn['base'])}）" if kn.get("base") is not None else ""))
    L.append("")

    # V4 ------------------------------------------------------------
    cat, w12 = CAT, W12
    has_cat = cat in arms
    L.append(f"#### V4. full 臂对 {'catalog / w1w2' if has_cat else 'w1w2 / none（本批生效前缀 = catalog）'}（b − a，按题配对；区间只按题聚类重抽，种子 {A.BOOT_SEED}）\n")
    if not has_cat:
        decl, eff, ok = w12_is_catalog(G)
        L.append(f"{NO_CAT}：对照臂用 w1w2——p2 批 V1a 已证 w1w2 与 catalog 全档逐字相同（同输入同输出）；本批 w1w2 行内 "
                 f"prefix_decl={decl}、tool_schema_mode_effective={eff}"
                 + ("。" if ok else "。⚠️ w1w2 生效前缀不是 catalog，下表不能读作 full 对 catalog。"))
        # L0 上 none 与 w1w2 同输入同输出（V1a），none 有 6 rep：full − none 是 L0 6 对 6 的对照，填充档 none 是无管理臂
        pairs = [(FULL, w12), (FULL, "orch-turn-none")]
    elif all(lv in lvs[cat] and lv in lvs[w12] for lv in lvs[FULL]):
        eqs = [same_lv.get((w12, cat, lv), same_lv.get((cat, w12, lv), (0, 0))) for lv in lvs[FULL]]
        L.append(f"w1w2 – catalog 逐字相同（按档，V1a）：" + "、".join(f"{lv}:{e}/{t}" for lv, (e, t) in zip(lvs[FULL], eqs))
                 + ("——全档相同，两者对 full 的差完全一样，只列 catalog。" if all(t and e == t for e, t in eqs) else "——不全相同，两对都列。"))
        pairs = [(FULL, cat)] + ([] if all(t and e == t for e, t in eqs) else [(FULL, w12)])
    else:
        pairs = [(FULL, cat)]
    L.append("区间 = paired_item（只按题重抽、题内 rep 原样）；analyze.paired_units 自 2d3fd99c 起成功率也只按题重抽（同结构、同 RNG 消耗），"
             "E / Q / U3 节区间与本节同口径（p2 档案里的两段重抽宽区间不再复现）。尾概率 = V2c 零分布 P(|Δ| ≥ |观察差|)：两侧都 ≥ 6 rep 的格"
             "（L0 6 rep 对 6 rep）用 m=3 拆分值（偏保守），其余用 m=2。两侧 rep 编号集合不同的格（如 L0 6 rep 对 2 rep）"
             "两侧都截到共同 rep 再配对（共同随机数下 rep r 只对 rep r），格内注明「rep 0–1 配对」与截前→截后 rep 数，"
             "a → b 列也是截后值。\n")
    L.append("| 对比 | level | 成功率 a → b | 成功率差（尾概率，判定） | 按题 95% CI（题升/降） | Pass^2 a → b | Pass^2 差（尾概率，判定） | 按题 95% CI（题升/降） |")
    L.append("|---|---|---|---|---|---|---|---|")
    for a_n, b_n in pairs:
        lv_c = [lv for lv in LEVELS if lv in aggs[a_n] and lv in aggs[b_n]]
        al = {lv: aligned(G[a_n]["records"], G[b_n]["records"], lv, (a_n, b_n)) for lv in lv_c}
        for lv in lv_c:
            ia, ib = al[lv]["items_a"], al[lv]["items_b"]
            units = {i: ([ia[i]], [ib[i]]) for i in ia if i in ib}
            ps, ph = paired_item(units, "success_rate"), paired_item(units, "pass_hat_2")
            m = 3 if min(len(v) for x in (ia, ib) for v in x.values()) >= 6 else 2
            def tc(p, metric):
                d = p["diff"] if p else None
                c = tail_cell(d, nulls[(metric, m)], thr[(metric, m)])
                if m == 3 and d is not None:      # 6 对 6 的格另给 m=2 尾概率作对照
                    c += f"；m=2 P={tail_p(nulls[(metric, 2)], d):.1%}"
                return c
            nt = f"；{al[lv]['note']}" if al[lv]["note"] else ""
            L.append(f"| {short(b_n)} − {short(a_n)} | {lv}（m={m}{nt}） | {pc(items_rate(ia))} → {pc(items_rate(ib))} | "
                     f"{tc(ps, 'success')} | {pair_cell1(ps)} | "
                     f"{pc(items_ph2(ia))} → {pc(items_ph2(ib))} | "
                     f"{tc(ph, 'pass_hat_2')} | {pair_cell1(ph)} |")
        for lab, sel in (("四档合并", lv_c), ("三填充档合并", [lv for lv in lv_c if lv > 0])):
            ps, ph, nt = pooled_aligned(al, sel)
            L.append(f"| {short(b_n)} − {short(a_n)} | {lab}{nt} | — | {pp(ps['diff'] if ps else None)}（合并无零分布） | {pair_cell1(ps)} | — | "
                     f"{pp(ph['diff'] if ph else None)} | {pair_cell1(ph)} |")
    L.append("")
    hi_lv = max(lvs[FULL]) if lvs[FULL] else None
    L.append(f"首 prompt 中位（L0 / {hi_lv}）：" + "、".join(
        f"{short(n)} {O.n0(aggs[n].get(0, {}).get('prompt_tokens_median'))} / {O.n0(aggs[n].get(hi_lv, {}).get('prompt_tokens_median'))}"
        for n in (FULL, pairs[0][1])))
    cd = cat + TAG
    if cd in aggs and cat in aggs and 0 in aggs[cd] and 0 in aggs[cat]:
        x0, x1 = aggs[cd][0]["prompt_tokens_median"], aggs[cat][0]["prompt_tokens_median"]
        L.append(f"catalog 臂 L0 首 prompt 中位 d44 → 本批：{O.n0(x0)} → {O.n0(x1)}（{x1 - x0:+,.0f}；D44 (iii) core 加 run_math / rag_search）。")
    follow = all(r.get("prefix_decl") is not None and r.get("prefix_decl") == r.get("tool_schema_mode_effective")
                 for n in arms for r in G[n]["records"])
    L.append("前缀元数据（行内字段；" + ("prefix_decl 逐行 == tool_schema_mode_effective，前缀元数据随生效档位（2d3fd99c）"
                                    if follow else "prod 臂按 full 声明计、与实际送入的 catalog 前缀不符") + "）：" + "；".join(
        f"{short(n)} prefix_mode={sorted({r.get('prefix_mode') for r in G[n]['records']})} prefix_decl="
        f"{sorted({str(r.get('prefix_decl')) for r in G[n]['records']})} prefix_n_tools(prompt, prompt_full)="
        f"{sorted({(str((r.get('prefix_n_tools') or {}).get('prompt')), str((r.get('prefix_n_tools') or {}).get('prompt_full'))) for r in G[n]['records']})} tool_schema_mode_effective="
        f"{sorted({str(r.get('tool_schema_mode_effective')) for r in G[n]['records']})} prefix_base_tokens="
        f"{sorted({r.get('prefix_base_tokens') for r in G[n]['records']}, key=str)} agent_cfg_fingerprint="
        f"{sorted({str(r.get('agent_cfg_fingerprint')) for r in G[n]['records']})}" for n in arms))
    L.append("")

    # V5 ------------------------------------------------------------
    L.append("#### V5. 时长（run_colab.log 臂起止；Σwall = transcripts 行 wall_s 之和）\n")
    cur = arm_minutes((data / "run_colab.log").read_text(encoding="utf-8"))
    old = arm_minutes((prev / "run_colab.log").read_text(encoding="utf-8")) if prev and (prev / "run_colab.log").is_file() else {}
    L.append("| 臂 | 本批墙钟 min | d44 同形态臂 | d44 墙钟 min | 墙钟增量 min | 本批 L0 rep 数 | 本批 Σwall L0 min | 本批 Σwall 填充三档 min | L0 多出的 rep 估计 min（= L0 Σwall × (rep−2)/rep） | d44 Σwall L0 / 填充三档 min |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for n in arms:
        rs = G[n]["records"]
        w0 = sum(r.get("wall_s") or 0 for r in rs if int(r["level"]) == 0) / 60
        wh = sum(r.get("wall_s") or 0 for r in rs if int(r["level"]) > 0) / 60
        r0 = len({int(r["rep"]) for r in rs if int(r["level"]) == 0}) or 1
        od = "—"
        fm = FORM_MATCH.get(n, n)
        if fm + TAG in G:
            ro = G[fm + TAG]["records"]
            od = f"{sum(r.get('wall_s') or 0 for r in ro if int(r['level']) == 0) / 60:.1f} / {sum(r.get('wall_s') or 0 for r in ro if int(r['level']) > 0) / 60:.1f}"
        L.append(f"| {short(n)} | {cur.get(n, float('nan')):.1f} | {short(fm)} | {('%.1f' % old[fm]) if fm in old else '—'} | {('%+.1f' % (cur[n] - old[fm])) if fm in old and n in cur else '—'} | {r0} | {w0:.1f} | {wh:.1f} | {w0 * max(0, r0 - 2) / r0:.1f} | {od} |")
    tot = sum(cur.values())
    L.append("")
    L.append("- 每 run wall_s 中位（s，按档 " + "/".join(str(lv) for lv in LEVELS) + "）：" + "；".join(
        f"{short(n)} " + "/".join(A.num(aggs[n].get(lv, {}).get("wall_median")) for lv in LEVELS) for n in arms))
    L.append(f"- 本批 {len(arms)} 臂墙钟合计 {tot:.1f} min（{tot / 60:.2f} h）；d44 四臂合计 {sum(old.values()):.1f} min。")
    bs = batch_span((data / "run_colab.log").read_text(encoding="utf-8"))
    mins = "—" if bs["minutes"] is None else f"{bs['minutes']:.1f} min（{bs['minutes'] / 60:.2f} h）"
    L.append(f"- 本批 run_colab.log 首行 → DONE：{mins}；READY 时刻 {bs['ready']}；"
             f"`START deploy` {bs['deploys']} 次（> 1 = 首次起机未 READY 后按 READY_RETRY 重试）。")
    if prev and (prev / "run_colab.log").is_file():
        bo = batch_span((prev / "run_colab.log").read_text(encoding="utf-8"))
        L.append(f"- d44 run_colab.log 首行 → DONE：{'—' if bo['minutes'] is None else format(bo['minutes'], '.1f') + ' min（' + format(bo['minutes'] / 60, '.2f') + ' h）'}；`START deploy` {bo['deploys']} 次。")
    L.append("")
    return L


# ---------------------------------------------------------------- W：本批对上一同种子批（--p2）
def first_divergent_turn(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[int]:
    """两行输出首个不同的轮下标（轮 = (thoughts, text, tool_calls)）；output_sig 相同 → None；
    轮逐个相同、只差轮数 / 末轮答复 / 多出的生成 → 较短一侧的轮数。"""
    if output_sig(a) == output_sig(b):
        return None

    def tt(r):
        return [[t.get("thoughts"), t.get("text"), t.get("tool_calls")] for t in (r.get("turns") or []) if isinstance(t, dict)]
    ta, tb = tt(a), tt(b)
    for i, (x, y) in enumerate(zip(ta, tb)):
        if x != y:
            return i
    return min(len(ta), len(tb))


def first_prompt_delta(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[int]:
    """b − a：首次生成引擎回报 prompt_tokens 之差；任一缺 → None。"""
    pa, pb = input_sig(a)[0], input_sig(b)[0]
    return None if pa is None or pb is None else pb - pa


def prefix_meta_ok(r: Dict[str, Any]) -> bool:
    """行内前缀元数据（prefix_base_tokens / prefix_n_tools）是否按生效档位计：prefix_decl（2d3fd99c 起）== tool_schema_mode_effective；
    旧行无 prefix_decl 时，--prefix full / catalog 的声明就是 prefix_mode 本身，--prefix prod 的旧行按 full 声明计、不可信。"""
    decl = r.get("prefix_decl") or (r.get("prefix_mode") if r.get("prefix_mode") not in (None, "prod") else None)
    return decl is not None and decl == r.get("tool_schema_mode_effective")


def fill_fp(r: Dict[str, Any]) -> Optional[str]:
    return (r.get("fill") or {}).get("fingerprint")


def recall_items(rs: Iterable[Dict[str, Any]]) -> Dict[str, List[bool]]:
    """题 → recall_ok 列表（analyze.aggregate 同口径：recall_ok 非空、未 aborted 的 run）。"""
    out: Dict[str, List[bool]] = {}
    for r in sorted(rs, key=lambda x: (x["item_id"], int(x["rep"]))):
        sc = r["score"]
        if sc.get("recall_ok") is not None and not sc.get("aborted"):
            out.setdefault(r["item_id"], []).append(bool(sc["recall_ok"]))
    return out


def band_knee(ag: Dict[int, Dict[str, Any]]) -> Tuple[Optional[float], Optional[int]]:
    """成功率带拐点：L0 成功率按题聚类 bootstrap 95% CI 下界（boot_item_rate）；首个填充档成功率 < 下界 → 该档。
    返回 (下界, 档)；无 L0 → (None, None)；没有档跌破 → (下界, None)。"""
    if 0 not in ag:
        return None, None
    lo, _hi = boot_item_rate({i: dict(enumerate(v)) for i, v in ag[0]["_items"].items() if v})
    for lv in sorted(x for x in ag if x > 0):
        sr = ag[lv].get("success_rate")
        if sr is not None and sr < lo - EPS:
            return lo, lv
    return lo, None


def _sys_text(r: Dict[str, Any]) -> Optional[str]:
    sd = r.get("_local_session")
    f = Path(sd) / "system.json" if sd is not None else None
    if f is None or not f.is_file():
        return None
    return str(json.loads(f.read_text(encoding="utf-8")).get("content"))


def sys_diff(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[Tuple[str, ...]]:
    """两行 system.json content 的逐行差（只留 `- ` / `+ ` 行）；任一缺 → None。"""
    ta, tb = _sys_text(a), _sys_text(b)
    if ta is None or tb is None:
        return None
    return tuple(x for x in difflib.ndiff(ta.splitlines(), tb.splitlines()) if x[:2] in ("- ", "+ "))


def _kn(ag: Optional[Dict[int, Dict[str, Any]]], lv: int, key: str) -> str:
    a = (ag or {}).get(lv)
    if not a:
        return "—"
    if key == "success":
        return f"{a['pass_k']}/{a['n']} = {pc(a['success_rate'])}"
    if key == "pass_hat_2":
        return "—" if a["pass_hat_2"] is None else f"{A.fmt_k(a['pass2_k'])}/{a['n_items2']} = {pc(a['pass_hat_2'])}"
    if key == "recall":
        if not a.get("recall_n"):
            return "—"
        return f"{round((a['recall_rate'] or 0) * a['recall_n'])}/{a['recall_n']}"
    return f"{a['repetition_runs']}/{a['n']}"


def p2_only_arms(p2: Optional[Path], arms: Sequence[str]) -> List[str]:
    """--p2 批有、本批没有的臂（从 p2 目录的全部 orch-turn-* 臂算，不经「只载入共同臂」的过滤）。"""
    return sorted(set(O.orch_arms(p2, [])) - set(arms)) if p2 else []


def sec_w(G, aggs, arms: List[str], p2G, p2aggs, ref_key: str, b2aggs, stash: Dict[str, Any],
          only_p2: Optional[Sequence[str]] = None) -> List[str]:
    """本批对 --p2 批（同 KNEE_SEED → 同 per-request 种子；p2G / p2aggs 的组名 = 臂名 + TAG_P2）。
    only_p2 = p2 有、本批无的臂（main 用 p2_only_arms 从 p2 目录算；p2G 只载入共同臂，单靠 p2G 算不出来）。"""
    L = [f"### W. 本批对 --p2 批（同 KNEE_SEED、同 per-request 种子；`{TAG_P2}` = p2 批同名臂；本节 = analyze_p2.py 新增）\n"]
    both = [n for n in arms if n + TAG_P2 in p2G]
    only_p2 = sorted(set(only_p2 or []) | ({k[:-len(TAG_P2)] for k in p2G} - set(arms)))
    L.append(f"共同臂：{', '.join(short(n) for n in both) or '无'}；p2 有、本批无：{', '.join(short(n) for n in only_p2) or '无'}"
             "（不进本节）。配对键 = (item, rep)，同档。\n")
    if not both:
        return L
    K = {n: {lv: keyed(G[n]["records"], lv) for lv in LEVELS} for n in both}
    P = {n: {lv: keyed(p2G[n + TAG_P2]["records"], lv) for lv in LEVELS} for n in both}
    nulls, thr = stash.get("nulls", {}), stash.get("thr", {})

    # W1 ------------------------------------------------------------
    L.append("#### W1. 跨批逐字相同与输入差（先看这个：输出大多相同 = 改动没碰到模型输入或没改变输出）\n")
    L.append("每格 = 相同对 / 配对数（配对 = 两批都有的 (item, rep)；「仅 p2 / 仅本批」= 只在一侧的行数，不进配对）。"
             "首 prompt 差 = p2b − p2 的首次生成 prompt_tokens（引擎回报）取值:对数；"
             "填充档输入签名另含 fill.prompt_tokens_measured。首个分叉轮 = 第一处 (thoughts, text, tool_calls) 不同的轮下标:对数。\n")
    L.append("| 臂 | level | 仅 p2 / 仅本批 行数 | 输出相同 | 输入签名相同 | system.json sha 相同 | 填充 fingerprint 相同 | 首 prompt 差:对数 | prefix_base_tokens p2 → p2b | 首个分叉轮:对数 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    tot = {lv: [0, 0] for lv in LEVELS}
    diffs: Dict[Tuple[str, ...], int] = {}
    nodiff = 0
    pb_delta = set()
    pd_all: Dict[Optional[int], int] = {}
    pd_fp: Dict[Optional[int], int] = {}      # 填充 fingerprint 相同的对上的首 prompt 差
    fp_exc: List[str] = []
    fp_cnt: Dict[Tuple[str, int], int] = {}      # (臂, 档) → 填充 fingerprint 不同的配对数（W2 / W3 标注用）
    for n in both:
        ro, rn = p2G[n + TAG_P2]["records"], G[n]["records"]
        pa = sorted({r.get("prefix_base_tokens") for r in ro}, key=str)
        pb = sorted({r.get("prefix_base_tokens") for r in rn}, key=str)
        ok_a, ok_b = all(prefix_meta_ok(r) for r in ro), all(prefix_meta_ok(r) for r in rn)
        ok_meta = ok_a and ok_b
        if ok_meta and len(pa) == 1 and len(pb) == 1 and None not in pa + pb:
            pb_delta.add(pb[0] - pa[0])
        for lv in LEVELS:
            a, b = P[n][lv], K[n][lv]
            if not a and not b:
                continue
            ks = sorted(set(a) & set(b))
            only = f"{len(set(a) - set(b))} / {len(set(b) - set(a))}"
            e, c = identity_rate(a, b)
            tot[lv][0] += e
            tot[lv][1] += c
            ei, _ = identity_rate(a, b, input_sig)
            es, _ = identity_rate(a, b, lambda r: system_sha(r) or "<无>")
            pdl: Dict[Optional[int], int] = {}
            fdt: Dict[Optional[int], int] = {}
            ef = 0
            for k in ks:
                d = first_prompt_delta(a[k], b[k])
                pdl[d] = pdl.get(d, 0) + 1
                pd_all[d] = pd_all.get(d, 0) + 1
                if fill_fp(a[k]) == fill_fp(b[k]):
                    ef += 1
                    pd_fp[d] = pd_fp.get(d, 0) + 1
                else:
                    fp_exc.append(f"{short(n)} {lv} {k[0]} r{k[1]} 差 {'—' if d is None else f'{d:+d}'}")
                    fp_cnt[(n, lv)] = fp_cnt.get((n, lv), 0) + 1
                f = first_divergent_turn(a[k], b[k])
                if f is not None:
                    fdt[f] = fdt.get(f, 0) + 1
                sd = sys_diff(a[k], b[k])
                if sd is None:
                    nodiff += 1
                else:
                    diffs[sd] = diffs.get(sd, 0) + 1
            bad = "（声明值，不可比）"
            L.append(f"| {short(n)} | {lv} | {only} | {e}/{c} | {ei}/{c} | {es}/{c} | {ef}/{c} | "
                     + ("、".join(f"{'—' if d is None else f'{d:+d}'}:{v}" for d, v in sorted(pdl.items(), key=lambda x: (x[0] is None, x[0] or 0))) or "—")
                     + f" | {','.join(map(str, pa))}{'' if ok_a else bad} → {','.join(map(str, pb))}{'' if ok_b else bad} | "
                     + ("、".join(f"t{t}:{v}" for t, v in sorted(fdt.items())) or "—") + " |")
    L.append("")
    e0, c0 = tot[0]
    L.append("- 按档合计输出相同：" + "、".join(f"{lv}:{e}/{c}" for lv, (e, c) in tot.items() if c))
    def dist(dd):
        return "、".join(f"{'—' if d is None else f'{d:+d}'}:{v}" for d, v in sorted(dd.items(), key=lambda x: (x[0] is None, x[0] or 0)))
    L.append(f"- prefix_base_tokens 差（p2b − p2，只取两批行内前缀元数据都按生效档位计的臂，prefix_meta_ok）：{sorted(pb_delta) or '—'}；"
             f"全部配对的首 prompt 差：{dist(pd_all)}；其中填充 fingerprint 相同的对：{dist(pd_fp)}")
    L.append(f"- 填充 fingerprint 不同的对 {len(fp_exc)}：" + ("；".join(fp_exc) if fp_exc else "无")
             + "（这些对的填充内容不同，跨批输入不只差前缀 / 系统提示；原因本脚本未核验。W2 / W3 按臂档标出其个数）")
    same_shift = len(pb_delta) == 1 and set(pd_fp) == pb_delta
    L.append("- 判读：" + (f"填充相同的对上首 prompt 差恒等于 prefix_base_tokens 差 {next(iter(pb_delta)):+d}——两批输入的 token 数差只来自"
                           "前缀这一项（system.json 文本差见下），填充与任务部分不变；" if same_shift else
                           "⚠️ 填充相同的对上首 prompt 差不恒等于 prefix_base_tokens 差（或后者不唯一 / 不可比）：两批输入差不只在前缀；")
             + ("L0 输出逐字相同 ≥ 一半——⚠️ L0 输出对该改动稳健（多数配对未被改动改变）。" if c0 and e0 * 2 >= c0 else
                f"L0 输出逐字相同 {e0}/{c0}：同种子下输出已分叉（首个分叉轮见表末列；改动是否有效看 W2 与步数 / 回读证据，分叉本身不是证据）。"))
    L.append(f"- system.json content 逐行差：{len(diffs)} 种不同的差（覆盖 {sum(diffs.values())} 对；缺 system.json 的对 {nodiff}）"
             + ("。各种差按出现对数：" if diffs else ""))
    for sd, v in sorted(diffs.items(), key=lambda x: -x[1])[:3]:
        L.append(f"  - {v} 对（{len(sd)} 行）：" + " ‖ ".join(("旧：" if x[0] == "-" else "新：") + x[2:].strip()[:400] for x in sd[:6])
                 + (" ‖ …" if len(sd) > 6 else ""))
    L.append("")

    # W2 ------------------------------------------------------------
    L.append("#### W2. 同臂同档 p2b − p2（按题配对；区间 = paired_item，只按题重抽，种子 "
             f"{A.BOOT_SEED}；尾概率 = V2c 本批零分布，两侧都 ≥ 6 rep 的格用 m=3、其余用 m=2。两批 rep 编号集合不同的格"
             "（如 w1 / w1w2 L0：p2 6 rep、本批 2 rep）两侧都截到共同 rep 再配对，格内注明「rep 0–1 配对」与截前→截后 rep 数，"
             "p2 → p2b 列与 recall 也是截后值）\n")
    L.append("| 臂 | level | 成功率 p2 → p2b | 差（尾概率，判定） | 按题 95% CI（升/降） | Pass^2 p2 → p2b | 差（尾概率，判定） | 按题 95% CI（升/降） | recall p2 → p2b | recall 差 按题 95% CI（升/降） |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for n in both:
        ao, an = p2aggs[n + TAG_P2], aggs[n]
        lv_c = [lv for lv in LEVELS if lv in ao and lv in an]
        al = {lv: aligned(p2G[n + TAG_P2]["records"], G[n]["records"], lv, (n + TAG_P2, n)) for lv in lv_c}
        for lv in lv_c:
            ia, ib = al[lv]["items_a"], al[lv]["items_b"]
            units = {i: ([ia[i]], [ib[i]]) for i in ia if i in ib}
            ps, ph = paired_item(units, "success_rate"), paired_item(units, "pass_hat_2")
            ra, rb = recall_items(al[lv]["rows_a"]), recall_items(al[lv]["rows_b"])
            pr = paired_item({i: ([ra[i]], [rb[i]]) for i in ra if i in rb}, "success_rate")
            reps = min(len(v) for x in (ia, ib) for v in x.values()) if ia and ib else 0
            m = 3 if reps >= 6 else 2

            def rc(rr):
                k = sum(sum(v) for v in rr.values())
                n_ = sum(len(v) for v in rr.values())
                return f"{k}/{n_}" if n_ else "—"
            nt = [al[lv]["note"]] if al[lv]["note"] else []
            if fp_cnt.get((n, lv)):
                nt.append(f"⚠️ 填充 fingerprint 不同 {fp_cnt[(n, lv)]} 对")
            L.append(f"| {short(n)} | {lv}{'（' + '；'.join(nt) + '）' if nt else ''} | {items_cell(ia, 'success')} → {items_cell(ib, 'success')} | "
                     f"{tail_cell(ps['diff'] if ps else None, nulls.get(('success', m), []), thr.get(('success', m)))} | {pair_cell1(ps)} | "
                     f"{items_cell(ia, 'pass_hat_2')} → {items_cell(ib, 'pass_hat_2')} | "
                     f"{tail_cell(ph['diff'] if ph else None, nulls.get(('pass_hat_2', m), []), thr.get(('pass_hat_2', m)))} | {pair_cell1(ph)} | "
                     f"{rc(ra)} → {rc(rb)} | {pair_cell1(pr)} |")
        for lab, sel in (("四档合并", lv_c), ("三填充档合并", [lv for lv in lv_c if lv > 0])):
            ps, ph, nt = pooled_aligned(al, sel)
            L.append(f"| {short(n)} | {lab}{nt} | — | {pp(ps['diff'] if ps else None)}（合并无零分布） | {pair_cell1(ps)} | — | "
                     f"{pp(ph['diff'] if ph else None)} | {pair_cell1(ph)} | — | — |")
    L.append("")

    # W3 ------------------------------------------------------------
    L.append("#### W3. 跨批分叉对符号检验（a = p2，b = p2b；只数输出分叉的 (题, rep) 对，同题多 rep 相关未校正；"
             "配对键 (item, rep)，只在一侧的 rep 自然不进；「填充 fp 不同」= 该臂档填充 fingerprint 跨批不同的配对数，不论是否分叉，见 W1）\n")
    L.append("| 臂 | level | 分叉对 | p2b 赢 / p2 赢 | p | 填充 fp 不同 |")
    L.append("|---|---|---|---|---|---|")
    for n in both:
        t3 = {"diverged": 0, "b_wins": 0, "a_wins": 0}
        for lv in LEVELS:
            ds = divergent_sign(P[n][lv], K[n][lv])
            if not ds["diverged"]:
                continue
            for k in t3:
                t3[k] += ds[k]
            L.append(f"| {short(n)} | {lv} | {ds['diverged']} | {ds['b_wins']} / {ds['a_wins']} | {A.num(ds['p'], 2)} | "
                     f"{fp_cnt.get((n, lv), 0)} |")
        L.append(f"| {short(n)} | **全档** | {t3['diverged']} | {t3['b_wins']} / {t3['a_wins']} | "
                 f"{A.num(A.sign_test_p(t3['b_wins'], t3['a_wins']), 2)} | {sum(v for (x, _l), v in fp_cnt.items() if x == n)} |")
    L.append("")

    # W4 ------------------------------------------------------------
    L.append(f"#### W4. 曲线：本批 / p2 / d44 / b2（同形态臂，FORM_MATCH）/ 参照 {ref_key}（直调）\n")
    L.append("重复护栏率 = repetition_runs / n（score.repetition_hits > 0 的 run）。recall = analyze.aggregate 口径（recall_ok 非空、未 aborted）。"
             "成功率带拐点 = 首个成功率 < 本组 L0 成功率按题聚类 95% CI 下界的填充档。\n")
    for key, lab in (("success", "成功率"), ("pass_hat_2", "Pass^2"), ("recall", "recall"), ("rep", "重复护栏率")):
        L.append(f"W4 · {lab}：\n")
        L.append("| 组 | " + " | ".join(str(lv) for lv in LEVELS) + " |")
        L.append("|---|" + "---|" * len(LEVELS))
        for n in arms:
            fm = FORM_MATCH.get(n, n)
            for lab2, ag in ((n, aggs.get(n)), (n + TAG_P2, p2aggs.get(n + TAG_P2)), (fm + TAG, aggs.get(fm + TAG)),
                             (fm + TAG_B2, b2aggs.get(fm + TAG_B2))):
                if ag:
                    tag2 = f"（{short(n)} 同形态）" if fm != n and lab2.startswith(fm + "@") else ""
                    L.append(f"| {lab2}{tag2} | " + " | ".join(_kn(ag, lv, key) for lv in LEVELS) + " |")
        if aggs.get(ref_key):
            L.append(f"| {ref_key} | " + " | ".join(_kn(aggs[ref_key], lv, key) for lv in LEVELS) + " |")
        L.append("")
    L.append("拐点（analyze.knee：Pass^2、基线 [0, 4096]、drop 0.20、recover 0.10）与成功率带拐点：\n")
    for n in arms:
        for lab2, ag in ((n, aggs.get(n)), (n + TAG_P2, p2aggs.get(n + TAG_P2))):
            if not ag:
                continue
            kn = A.knee(ag, "pass_hat_2", [0, 4096], 0.20, 0.10)
            lo, blv = band_knee(ag)
            L.append(f"- {lab2}：{kn['verdict']}；成功率带拐点：L0 CI 下界 {pc(lo)} → "
                     + (f"首个跌破 {blv}" if blv is not None else "无填充档跌破"))
    L.append("")
    return L


# ---------------------------------------------------------------- 布局（catalog 臂可缺）
def layout_arms(data: Path) -> List[str]:
    """本批臂：O.B2_ORCH 里目录在的（catalog 可缺）+ 其余 orch-turn-* 按名；缺 O.TURN 三臂或 full 任一 → SystemExit。"""
    arms = O.orch_arms(data, [n for n in O.B2_ORCH if (data / n / "transcripts.jsonl").is_file()])
    missing = [n for n in O.TURN + [FULL] if n not in arms]
    if missing:
        raise SystemExit(f"❌ P2 布局缺臂：{missing}")
    return arms


def prod_prefix(G, has_cat: bool) -> Tuple[str, Optional[int], Optional[str]]:
    """F 节生产前缀 → (来源臂, prefix_base_tokens, 警告)。有 catalog 臂取其行内值（D44 (iii) 后 prod 生效 = catalog；
    p2 批 prod 臂行内仍是 full 声明计数）；无 catalog 臂取 orch-turn-none 行内值，但只在每行 prefix_decl ==
    tool_schema_mode_effective（2d3fd99c）时，否则不取（None + 警告），不拿声明值顶替。"""
    src = CAT if has_cat else "orch-turn-none"
    rows = G[src]["records"]
    warn = None
    if not has_cat and not all(r.get("prefix_decl") is not None and r.get("prefix_decl") == r.get("tool_schema_mode_effective")
                               for r in rows):
        warn = f"{NO_CAT}，且 orch-turn-none 行内 prefix_decl ≠ tool_schema_mode_effective（2d3fd99c 之前的行）：行内前缀是声明值，不取"
        rows = []
    pb = sorted({r.get("prefix_base_tokens") for r in rows} - {None})
    if warn is None and len(pb) != 1:
        warn = f"{src} 的 prefix_base_tokens 不唯一：{pb or '全缺'}，取较大值"
    return src, (max(pb) if pb else None), warn


def strip_u3_catalog(u: List[str]) -> List[str]:
    """analyze_d44.sec_u 的 U3 首表（catalog − w1w2 按档）换成「本批无 catalog 臂」一行；预检回执部分原样保留。"""
    i3 = next(i for i, x in enumerate(u) if x.startswith("#### U3."))
    ip = next(i for i, x in enumerate(u) if i > i3 and x.startswith("预检回执（feed"))
    return u[:i3] + ["#### U3. D44：实参预检回执\n", f"{NO_CAT}：U3 的 catalog − w1w2 按档表不渲染（前缀对照见 Q 节、V4）。\n"] + u[ip:]


def e_pairs(specs: Sequence[str], G: Dict[str, Dict[str, Any]], same_seed: Sequence[str]) -> Tuple[List[Tuple[str, str, str]], List[Dict[str, Any]]]:
    """E 节对照对 → (analyze.render 的 (a, b, 标签) 列表, 截 rep 的别名组)。两端都在 same_seed（本批臂，臂间共同随机数）
    且某档两侧 rep 编号集合不同（L0 6 rep 对 2 rep）时，两端各换成「该档只留共同 rep」的别名组（只进 E 节，不进 A–D / F / CSV），
    标签注明「L0 rep 0–1 配对」；其余对原样（标签与 analyze.render 的 --pair 相同）。跨批对（@d44 无 seed）不截。
    名字不在 G 里的对跳过并告警（同 analyze.render）。"""
    out: List[Tuple[str, str, str]] = []
    alias: Dict[str, Dict[str, Any]] = {}
    for spec in specs:
        a_n, _, b_n = spec.partition(":")
        if a_n not in G or b_n not in G:
            print(f"⚠️ --pair {spec}：目录不在本次输入里，跳过", file=sys.stderr)
            continue
        cut: Dict[int, set] = {}
        if a_n in same_seed and b_n in same_seed:
            ra_all, rb_all = G[a_n]["records"], G[b_n]["records"]
            for lv in sorted({int(r["level"]) for r in ra_all} & {int(r["level"]) for r in rb_all}):
                ra, rb = lv_reps(ra_all, lv), lv_reps(rb_all, lv)
                if ra != rb:
                    cut[lv] = ra & rb
        if not cut:
            out.append((a_n, b_n, f"{a_n}→{b_n}（指定）"))
            continue
        tag = "、".join(f"{'L0' if lv == 0 else lv} {rep_lab(r)}" for lv, r in sorted(cut.items()))
        names = []
        for n in (a_n, b_n):
            an = f"{n}〔{tag}〕"
            g = G[n]
            alias[an] = {**g, "name": an, "dropped": [],
                         "records": [r for r in g["records"] if int(r["level"]) not in cut or int(r["rep"]) in cut[int(r["level"])]],
                         "missing": [m for m in g["missing"] if m[0] not in cut or m[2] in cut[m[0]]]}
            names.append(an)
        out.append((names[0], names[1], f"{a_n}→{b_n}（指定；{tag} 配对）"))
    return out, list(alias.values())


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P2 噪声基线批复算（编排器五臂）")
    ap.add_argument("--data", required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--prev", default=None, help="d44 批目录（同名四臂，组名加 @d44）")
    ap.add_argument("--b2", default=None, help="b2 批目录（同名四臂，只进 V3 对照）")
    ap.add_argument("--p2", default=None, help="上一同种子批目录（同名臂，组名加 @p2，只进 W 节）")
    ap.add_argument("--out-md", default=None)
    ap.add_argument("--out-csv", default=None)
    a = ap.parse_args(argv)
    data, ref = Path(a.data), Path(a.ref)
    arms = layout_arms(data)
    has_cat = CAT in arms
    prev = Path(a.prev) if a.prev else None
    prev_arms = [n for n in arms if prev and (prev / n / "transcripts.jsonl").is_file()]
    if a.prev and not prev_arms:
        raise SystemExit(f"❌ --prev {a.prev} 下没有 {arms} 任何一臂")
    b2 = Path(a.b2) if a.b2 else None
    b2_arms = [n for n in arms if b2 and (b2 / n / "transcripts.jsonl").is_file()]
    p2 = Path(a.p2) if a.p2 else None
    p2_arms = [n for n in O.orch_arms(p2, []) if n in arms] if p2 else []
    if p2 and not p2_arms:
        raise SystemExit(f"❌ --p2 {a.p2} 下没有 {arms} 任何一臂")
    p2_only = p2_only_arms(p2, arms)
    ref_key = os.path.basename(os.path.normpath(a.ref))
    if ref_key in arms or ref_key in {n + TAG for n in prev_arms} | {n + TAG_P2 for n in p2_arms} | {n + TAG_B2 for n in b2_arms}:
        raise SystemExit(f"❌ 参照目录名「{ref_key}」与臂名冲突")
    saved = (O.REF, O.ARMS, O.EXTRA_ORCH, A.comparisons, O.RAG_NOTE)
    saved_a = (A.KEEP, A.th_global, A.wall_global)
    saved_d = (D.ARMS, D.MANAGED, D.TAG)
    D.ARMS, D.MANAGED, D.TAG = arms, arms[1:], TAG
    O.REF, O.ARMS, O.EXTRA_ORCH = ref_key, list(arms), [n for n in arms if n not in O.TURN]
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
        pf_src, args.prod_prefix, warn = prod_prefix(G, has_cat)
        if args.prod_prefix is None:          # analyze.render F 节要算 top*0.8 − prod_prefix：取不到就停，不带 None 进去
            raise SystemExit(f"❌ F 节生产前缀取不到（{pf_src}）：{warn}")
        pair_specs = [f"{n}{TAG}:{n}" for n in prev_arms] + ([f"orch-turn-w1w2{TAG}:{FULL}"] if "orch-turn-w1w2" in prev_arms else []) + [
            "orch-turn-none:orch-turn-w1", "orch-turn-none:orch-turn-w1w2", "orch-turn-w1:orch-turn-w1w2"] + (
            [f"orch-turn-w1w2:{CAT}", f"{FULL}:{CAT}"] if has_cat else []) + [f"{FULL}:orch-turn-w1w2"]
        aggs = {g["name"]: A.aggregate(g, A.th_global, A.wall_global) for g in groups}
        # E 节：本批臂间同种子，某档 rep 数不同（L0 6 对 2）的对截到共同 rep（别名组只进 render 的 aggs）；
        # 全部对经 A.comparisons 送入（它不按组名过滤），args.pair 置空
        e_list, e_alias = e_pairs(pair_specs, G, arms)
        aggs_r = {**aggs, **{g["name"]: A.aggregate(g, A.th_global, A.wall_global) for g in e_alias}}
        A.comparisons = lambda _groups: list(e_list)
        args.pair = []
        b2G, b2aggs = {}, {}
        for n in b2_arms:
            g = A.load_group(b2 / n)
            g["name"] = n + TAG_B2
            b2G[g["name"]] = g
            b2aggs[g["name"]] = A.aggregate(g, A.th_global, A.wall_global)
        p2G, p2aggs = {}, {}
        for n in p2_arms:
            g = A.load_group(p2 / n)
            g["name"] = n + TAG_P2
            p2G[g["name"]] = g
            p2aggs[g["name"]] = A.aggregate(g, A.th_global, A.wall_global)
        if p2_arms:
            D.attach_sessions(p2G, p2, p2_arms, TAG_P2)
        prev_names = [n + TAG for n in prev_arms]
        parts = [A.render(groups, aggs_r, args), ""]
        parts += O.sec_i(G, aggs, raws, evs, args)
        parts += O.sec_j(G, aggs, raws, evs)
        parts += O.sec_k(G, evs)
        parts += O.sec_m(G, aggs, raws)
        parts += O.sec_n(G, raws, evs, data, logs=("run_colab.log",))
        parts += O.sec_o(G, aggs, arms + prev_names)
        parts += O.sec_p(G, aggs, evs, arms + prev_names, prev_names)
        if has_cat:
            q = O.sec_q(G, aggs, evs)
            q[0] = q[0].replace("（prod 前缀）", "（prod 前缀；本批生效 = catalog，见 V1）", 1)
            parts += q
            q = O.sec_q(G, aggs, evs, a_name=FULL, b_name=CAT)
            q[0] = q[0].replace("（prod 前缀）", "（full 前缀）", 1)
            parts += q
        else:
            q = O.sec_q(G, aggs, evs, a_name=FULL, b_name=W12)
            decl, eff, ok = w12_is_catalog(G)
            q[0] = ((f"### Q. ⚠️ {W12} 行内 prefix_decl={decl}、tool_schema_mode_effective={eff}，不是 catalog——下表不能读作 full 对 catalog。"
                     if not ok else "### Q. ")
                    + f"前缀对照：{W12}（prod 前缀" + ("，本批生效 = catalog，行内 prefix_decl 为证" if ok else "") + f"）对 {FULL}（full 前缀）——"
                    "两臂管理开关相同（guard on + step_compaction on），只差前缀档。"
                    f"{NO_CAT}：p2 批 V1a 已证 w1w2 与 catalog 全档逐字相同，故以 w1w2 代 catalog；w1w2 − catalog 对照不渲染\n")
            parts += q
        if W12 + TAG in G and CAT + TAG in G:
            parts += O.sec_q(G, aggs, evs, a_name=W12 + TAG, b_name=CAT + TAG)
        elif not has_cat:
            parts += [f"（{NO_CAT}：d44 批 catalog@d44 对 w1w2@d44 的 Q 表不并入。）", ""]
        # sec_u 的 U3 首表写死 catalog − w1w2：缺 catalog 时给它一个空占位让函数走完，再把那张表整段换成显式说明
        u = D.sec_u(G, aggs if has_cat else {**aggs, CAT: {}}, evs, False)
        u[0] = f"### U. analyze_d44 U 节（D43 (a) / D43b / D44 机制计数；本批只列本批 {len(arms)} 臂，不并跨批列）\n"
        if not has_cat:
            u = strip_u3_catalog(u)
        parts += u
        stash: Dict[str, Any] = {}
        parts += sec_v(G, aggs, arms, data, prev, b2G, b2aggs, args, stash)
        if p2_arms:
            parts += sec_w(G, aggs, arms, p2G, p2aggs, ref_key, b2aggs, stash, only_p2=p2_only)
        auto = "\n".join(parts).rstrip() + "\n"
        auto = auto.replace("`@am` = 11-02 上午批同名臂", f"`{TAG}` = d44 批同名臂")
        marker = "_自动生成：`benchmarks/context_knee/analyze.py`；"
        if marker not in auto:
            raise SystemExit(f"❌ analyze.render 输出里找不到预期的『{marker}』")
        auto = auto.replace(marker, "_自动生成：`benchmarks/context_knee/analyze_p2.py`（P2 布局：A–F 节 = analyze.render 原样，"
                            "自动对照关闭、E 节只列指定对；I/J/K/M/N/O/P/Q 节 = analyze_orch 原样，R/T 节本批无数据不渲染；"
                            "U 节 = analyze_d44 原样（只列本批）；V 节新增" + ("；W 节 = 对 --p2 批" if p2_arms else "") + "；"
                            f"F 节生产前缀取 {pf_src} 行内 prefix_base_tokens {args.prod_prefix}"
                            + (f"；⚠️ {warn}" if warn else "") + f"；`{TAG}` = --prev 批同名臂）；", 1)
        note = "（engine.py:140 注释的 M5 实测）"
        if note in auto:
            auto = auto.replace(note, f"（取 {pf_src} 行内字段 prefix_base_tokens，见上）", 1)
        rerun = ("_复跑：`PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/context_knee/analyze_p2.py "
                 f"--data {a.data} --ref {a.ref}" + (f" --prev {a.prev}" if a.prev else "") + (f" --b2 {a.b2}" if a.b2 else "")
                 + (f" --p2 {a.p2}" if a.p2 else "") + (f" --out-md {a.out_md}" if a.out_md else "") + (f" --out-csv {a.out_csv}" if a.out_csv else "") + "`_\n\n")
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
