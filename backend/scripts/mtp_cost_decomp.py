#!/usr/bin/env python3
"""MTP 每轮成本分解：把一轮拆成 draft / verify / walk / 其它，逐档 block 报表。

# 与 `profile_mtp_round.py` 的分工（两者插桩手法相同，别混用）

- `profile_mtp_round.py`：**单一 block**、报**绝对秒数与占比**，阶段切得更细
  （含 `next_block_size` / `accept_verified`）。回答"这一档的时间去哪了"。
- 本脚本：**扫一串 block**、把每相换算成 **T1 倍数**（T1 = 同一次装载下 `mode=off`
  的每 token 时间），并用分解值反算加速比与实测对表。回答"**跨平台可比的分母**
  长什么样、差距在哪一项"——vLLM 侧的绝对秒数没有可比性，倍数才有。

# 为什么要做这件事

跨平台对照已经证死了两件事：两侧**接受率逐位一致**（B4，31 个位置）、
**AL 一致**。故 `加速比 = AL / (γ·d + v(M) + w(M) + dh + oh)` 里（五项，与代码
:270 的 `den` 一致：`dh` = draft_hidden，它必须自成一项，见 `dh_units` 处注释），
分子两侧相同，
**全部差距在分母**。但分母是五项之和，此前从未分开量过——
§4.16 我曾用解析式拆过一次，总量对上了而**拆分是错的（verify 低估 2.4 倍）**。
所以这次不解析，直接在真实生成循环里插桩。

# 口径（每个数都随口径记，缺一作废）

- 真权重、真 KV、真 prompt，**不是微基准**（§4.20 的教训：微基准会掉进缓存里）
- MLX 是惰性图，不在相边界强制 eval，时间会全部堆到第一个 `.item()` 上。
  故每相结束处 `mx.eval` 该相产物。**代价**：损失跨相异步重叠，
  故插桩后的总时长会略高于不插桩——两者都报，差值即扰动量。
- 每档跑 `REPEATS` 次取中位；批前批后各测一次基线查热漂移。

用法：
  python3 scripts/mtp_cost_decomp.py <目标件> <草稿件> [块列表] [tokens] [repeats]
  python3 scripts/mtp_cost_decomp.py models/gemma-4-31b-it-8bit \\
      models/gemma-4-31b-it-assistant-bf16 2,3,4,5,6,7,8,10 400 2
"""
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.chdir(str(Path(__file__).resolve().parents[1]))

import mlx.core as mx  # noqa: E402

PROMPT_TEXT = ("详细说明光合作用的完整过程，分光反应与暗反应两部分展开，每部分至少三点。")

TARGET = sys.argv[1] if len(sys.argv) > 1 else "models/gemma-4-31b-it-8bit"
DRAFT = sys.argv[2] if len(sys.argv) > 2 else "models/gemma-4-31b-it-assistant-bf16"
BLOCKS = [int(x) for x in (sys.argv[3] if len(sys.argv) > 3 else "2,3,4,5,6,7,8,10").split(",")]
MAX_TOKENS = int(sys.argv[4]) if len(sys.argv) > 4 else 400
REPEATS = int(sys.argv[5]) if len(sys.argv) > 5 else 2

# 相计时累加器：{相名: [秒, ...]}
PHASES: dict = {}
_ROUND_T: list = []


def _acc(name: str, dt: float) -> None:
    PHASES.setdefault(name, []).append(dt)


def install_phase_probes():
    """在真实循环的三个相边界插桩，返回卸载函数。

    强制 eval 的落点选在**该相唯一的下游依赖**上：
      draft → draft_tokens；verify → verify.hidden（同图还带 KV 写入）；
      walk  → 返回的是 python int，已同步，无需再 eval。
    """
    from mlx_vlm.speculative import mtp as M
    from mlx_vlm.speculative import common as C

    o_verify = M._mtp_verify_target
    o_walk = M._mtp_acceptance_walk          # 可能已被 mtp_processors 换过
    o_draft = C._SpeculativeSamplerRNG.draft_tokens
    o_hidden = M._mtp_draft_hidden

    def w_draft(self, fn, *a, **k):
        t = time.perf_counter()
        r = o_draft(self, fn, *a, **k)
        mx.eval(r)
        _acc("draft", time.perf_counter() - t)
        return r

    def w_verify(*a, **k):
        t = time.perf_counter()
        r = o_verify(*a, **k)
        mx.eval(r.hidden)
        if r.target_tokens is not None:
            mx.eval(r.target_tokens)
        _acc("verify", time.perf_counter() - t)
        return r

    def w_walk(*a, **k):
        t = time.perf_counter()
        r = o_walk(*a, **k)
        _acc("walk", time.perf_counter() - t)
        return r

    def w_hidden(*a, **k):
        t = time.perf_counter()
        r = o_hidden(*a, **k)
        mx.eval(r)
        _acc("draft_hidden", time.perf_counter() - t)
        return r

    M._mtp_verify_target = w_verify
    M._mtp_acceptance_walk = w_walk
    M._mtp_draft_hidden = w_hidden
    C._SpeculativeSamplerRNG.draft_tokens = w_draft

    def uninstall():
        M._mtp_verify_target = o_verify
        M._mtp_acceptance_walk = o_walk
        M._mtp_draft_hidden = o_hidden
        C._SpeculativeSamplerRNG.draft_tokens = o_draft
    return uninstall


def install_accept_probe():
    """逐轮 (accepted, drafted)，用于精确 AL = 1 + mean(accepted)。"""
    from mlx_vlm.speculative import mtp as M
    rounds: list = []
    orig = M._record_speculative_round

    def wrapped(dm, accepted, drafted):
        rounds.append((int(accepted), int(drafted)))
        return orig(dm, accepted, drafted)

    M._record_speculative_round = wrapped
    return rounds, lambda: setattr(M, "_record_speculative_round", orig)


def build_prompt(target_dir: str) -> str:
    from transformers import AutoTokenizer
    from framework.dialects import get_dialect_class
    tok = AutoTokenizer.from_pretrained(target_dir)
    d = get_dialect_class("gemma4").create(
        SimpleNamespace(tokenizer=tok), options={"suppress_ghost_thinking": True})
    return d.build_prompt(messages=[{"role": "user", "content": PROMPT_TEXT}],
                          include_env=False)


def run_once(be, prompt: str, sampling: dict):
    """跑一次；返回 (解码tps, tokens, 逐轮列表, 相分解秒, 全量文本)。

    tps 一律取后端的 `generation_tps`（仅解码段）。拿不到就抛——
    退回墙钟 n/dt 会混进预填，与本表其余格不同口径（§4.12 栽过）。
    """
    PHASES.clear()
    rounds, un = install_accept_probe()
    parts, last = [], None
    try:
        for ch in be.stream_generate(prompt, cache=be.new_cache(), sampling=sampling):
            if ch.text:
                parts.append(ch.text)
            last = ch
    finally:
        un()
    if not last or not last.generation_tps:
        raise RuntimeError("后端未报 generation_tps——口径不可混用墙钟估计值（§4.12）")
    phases = {k: sum(v) for k, v in PHASES.items()}
    return last.generation_tps, last.generation_tokens, list(rounds), phases, "".join(parts)


def exact_al(rounds) -> float:
    return 1.0 + (sum(a for a, _ in rounds) / len(rounds)) if rounds else 1.0


async def main() -> int:
    from framework.backends.mlx.backend import MLXBackend

    samp = {"max_tokens": MAX_TOKENS, "temperature": 0.0,
            "repetition_penalty": 1.1, "repetition_context_size": 50}

    print(f"目标 {TARGET}\n草稿 {DRAFT}\n块 {BLOCKS}  tokens={MAX_TOKENS} repeats={REPEATS}")
    print("口径：temp=0 / rep1.1 / ctx50 / 冷启动 / 单请求；相边界强制 mx.eval\n")

    # 一次装载走全场：基线用 mode=off 在同一后端上跑，避免重装 31GB，
    # 也避免"基线与投机各自一次装载"引入的页缓存态差异。
    be = await MLXBackend.load(TARGET, speculative={
        "mode": "drafter", "draft_model": DRAFT, "draft_block_size": BLOCKS[0]})
    prompt = build_prompt(TARGET)

    # ── 基线（mode=off），拿 T1 ────────────────────────────────────────────
    be.speculative["mode"] = "off"
    base = [run_once(be, prompt, samp)[0] for _ in range(REPEATS)]
    base_tps = statistics.median(base)
    T1 = 1.0 / base_tps
    print(f"基线 {base_tps:.2f} tps  (T1 = {T1*1000:.3f} ms/token)   样本 "
          f"{[round(x,2) for x in base]}")

    # ── 投机各档 ─────────────────────────────────────────────────────────
    be.speculative["mode"] = "drafter"
    unp = install_phase_probes()
    rows = []
    try:
        for blk in BLOCKS:
            gamma = blk - 1
            be.speculative["draft_block_size"] = blk
            samples = []
            for _ in range(REPEATS):
                tps, n, rounds, ph, _ = run_once(be, prompt, samp)
                samples.append((tps, rounds, ph, n))
            # 基线与本表必须用**同一个中心估计量**。`samples[len//2]` 在
            # REPEATS=2 时取的是**较快**的那次（sorted 后的 index 1），而基线用
            # `statistics.median`（2 个样本时＝均值）——两端估计量不同会把
            # 全部加速比系统性抬高（首过评审 Major）。此处一律取 median，
            # 再挑**最接近 median 的那次**的相分解随行。
            tps = statistics.median([s[0] for s in samples])
            _, rounds, ph, n = min(samples, key=lambda s: abs(s[0] - tps))
            R = len(rounds)
            if R == 0:
                print(f"block {blk}: 零轮，跳过")
                continue
            AL = exact_al(rounds)
            got_gamma = statistics.median([d for _, d in rounds])
            per = {k: v / R for k, v in ph.items()}          # 每轮秒
            tot = sum(per.values())
            row = {
                "block": blk, "gamma": gamma, "measured_gamma": got_gamma,
                "tps": tps, "speedup": tps / base_tps, "AL": AL, "rounds": R,
                "tokens": n,
                "draft_ms": per.get("draft", 0) * 1e3,
                "verify_ms": per.get("verify", 0) * 1e3,
                "walk_ms": per.get("walk", 0) * 1e3,
                "dhidden_ms": per.get("draft_hidden", 0) * 1e3,
                "probed_round_ms": tot * 1e3,
                # 未插桩口径下的每轮时长（含异步重叠），用来估插桩扰动
                "real_round_ms": (n / tps) / R * 1e3,
            }
            row["d_per_tok"] = (per.get("draft", 0) / max(gamma, 1)) / T1
            row["v_units"] = per.get("verify", 0) / T1
            row["w_units"] = per.get("walk", 0) / T1
            # `draft_hidden` 必须自成一项进分母。此前它只进了 `tot`（因而被
            # `other_units` 减掉），结果 D 里**整项缺失**，缺口 0.4%~0.9% 恰好
            # 等于"预测 vs 实测"那一列的残差——即把自己的漏项读成了"模型可信"。
            # 这正是 §4.16 的同一类错误在自制仪器里的复刻（首过评审 Critical）。
            row["dh_units"] = per.get("draft_hidden", 0) / T1
            row["other_units"] = (row["real_round_ms"] / 1e3 - tot) / T1
            rows.append(row)
            print(f"block {blk:<3} γ={gamma:<2} {tps:6.2f} tps  {row['speedup']:.3f}×  "
                  f"AL={AL:.3f} 轮={R:<4} | 每轮 draft {row['draft_ms']:6.2f} "
                  f"verify {row['verify_ms']:6.2f} walk {row['walk_ms']:6.2f} "
                  f"dh {row['dhidden_ms']:5.2f} | 插桩合计 {row['probed_round_ms']:6.2f} "
                  f"真实 {row['real_round_ms']:6.2f} ms")

        # 漂移哨必须在**卸探针之前**测：卸掉后测到的差 = 热漂移 + 探针开销，
        # 两者混在一起报成"漂移"是错的（首过评审 Major）。此处先在**同口径**下
        # 复测首档拿净漂移，再卸探针复测一次，两者之差即探针扰动量。
        print()
        if not rows:
            raise RuntimeError("全部 block 的轮数为 0——没有可对照的批内首测，"
                               "漂移无从谈起，本次作废")
        # **与 rows[0] 配对**，不能写死 BLOCKS[0]：若首个 block 因零轮被跳过，
        # rows[0] 会是另一个档，跨档比出来的"漂移"入了 JSON 就成了假权威数。
        drift_block = rows[0]["block"]
        be.speculative["draft_block_size"] = drift_block
        tps_drift = run_once(be, prompt, samp)[0]
    finally:
        unp()

    tps_noprobe = run_once(be, prompt, samp)[0]
    drift = tps_drift / rows[0]["tps"] - 1
    probe = tps_noprobe / tps_drift - 1
    print(f"批后复测 block{drift_block}（**仍带探针**，同口径）: {tps_drift:.2f} tps"
          f"（批内首测 {rows[0]['tps']:.2f}） ⇒ 净漂移 {drift*100:+.1f}%")
    print(f"再卸探针复测: {tps_noprobe:.2f} tps ⇒ 探针扰动 {probe*100:+.1f}%")

    print("\n=== 以 T1 为单位的每轮成本（这才是跨平台可比的量）===")
    print(f"{'γ':<3} {'AL':>6} {'γ·d':>7} {'v(M)':>7} {'w(M)':>7} {'dh':>7} {'其它':>7} "
          f"{'分母':>7} {'预测×':>7} {'实测×':>7} {'偏差':>7}")
    for r in rows:
        gd = r["gamma"] * r["d_per_tok"]
        den = gd + r["v_units"] + r["w_units"] + r["dh_units"] + r["other_units"]
        pred = r["AL"] / den if den > 0 else 0
        r["denominator"] = den
        print(f"{r['gamma']:<3} {r['AL']:6.3f} {gd:7.3f} {r['v_units']:7.3f} "
              f"{r['w_units']:7.3f} {r['dh_units']:7.3f} {r['other_units']:7.3f} "
              f"{den:7.3f} {pred:7.3f} {r['speedup']:7.3f} "
              f"{(pred/r['speedup']-1)*100:+6.1f}%")

    # 漂移与探针扰动必须**随表落盘**：它们是这张表的口径的一部分，
    # 只 print 的话，归档里的 JSON 事后无法判断某一格是真差异还是热漂移。
    out = {"target": TARGET, "draft": DRAFT, "base_tps": base_tps, "T1_ms": T1 * 1e3,
           "max_tokens": MAX_TOKENS, "repeats": REPEATS, "rows": rows,
           # 净漂移：批后在**同口径**（仍带探针）复测首档 vs 批内首测，比值-1
           "drift": drift,
           # 探针扰动：卸探针复测 vs 仍带探针复测，比值-1
           "probe_disturbance": probe,
           # 上面两个比值各自的原始 tps（首档 block=BLOCKS[0]）
           "tps_drift_raw": tps_drift,
           "tps_noprobe_raw": tps_noprobe,
           "drift_block": drift_block}
    p = Path("../benchmarks/data/mtp-cost/decomp_"
             + Path(TARGET).name + "__" + Path(DRAFT).name + ".json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n落盘 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
