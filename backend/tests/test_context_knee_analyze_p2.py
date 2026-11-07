"""P2 噪声基线批分析（analyze_p2.py）新函数的金测试：逐字相同率、种子核对、按档 rep 标准差、rep 拆分零分布与
尾概率门槛、按题聚类 bootstrap、分叉对符号检验、臂墙钟。

守的是什么：ANALYSIS 的「seed 生效 / 未生效」判读与「两臂差多少点才超噪声」门槛直接抄这几个函数的输出。
签名只比第一轮或漏掉 thoughts / 工具参数 / 多出的生成、rep 对按臂间而非臂内配、SD 用总体式、门槛用分位数而非尾概率、
拆分重复计数——都会给出看着合理的错数。CPU、合成数据，不读真实产物。
"""
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "benchmarks") not in sys.path:
    sys.path.append(str(REPO / "benchmarks"))

from context_knee import analyze_p2 as P   # noqa: E402


def _row(item="lc-hard-001", rep=0, level=0, ok=True, text="done", args=None, prompt=6162, final="ok",
         thoughts=("t1", "t2"), second_text="fin", gens_n=None, seed=1107, sent=True, sampling_seed=None):
    turns = [{"thoughts": thoughts[0], "text": text,
              "tool_calls": [{"name": "read_file", "arguments": args or {"path": "a.txt"}}]},
             {"thoughts": thoughts[1], "text": second_text, "tool_calls": []}]
    gens = [{"prompt_tokens": prompt, "generation_tokens": 10 + i, "truncated": False, "cancelled": False,
             "repetition_guard": None} for i in range(gens_n or len(turns))]
    return {"item_id": item, "rep": rep, "level": level, "score": {"pass": ok}, "turns": turns,
            "turn_response": final, "gens": gens, "fill": {"prompt_tokens_measured": prompt},
            "seed": seed, "sampling_seed_sent": sent,
            "sampling_seed": seed * 1000 + rep if sampling_seed is None else sampling_seed}


def test_output_sig_covers_every_turn_thoughts_args_final_and_extra_gens():
    a = _row()
    assert P.output_sig(a) == P.output_sig(_row())
    assert P.output_sig(a) != P.output_sig(_row(args={"path": "b.txt"}))      # 只差工具参数
    assert P.output_sig(a) != P.output_sig(_row(final="ok2"))                  # 只差末轮答复
    assert P.output_sig(a) != P.output_sig(_row(second_text="other"))          # 只差第二轮（「只比第一轮」变异）
    assert P.output_sig(a) != P.output_sig(_row(thoughts=("t1", "tX")))        # 只差第二轮 thoughts（「去掉 thoughts」变异）
    assert P.output_sig(a) != P.output_sig(_row(thoughts=("tX", "t2")))
    assert P.output_sig(a) != P.output_sig(_row(gens_n=3))                     # gens 比 turns 多一次生成
    assert P.output_sig(a) == P.output_sig(_row(prompt=9999))                  # 输入不进输出签名
    assert P.input_sig(a) != P.input_sig(_row(prompt=9999))


def test_identity_rate_pairs_by_item_and_rep_on_common_keys():
    a = P.keyed([_row(rep=0), _row(rep=1, text="x"), _row(item="lc-hard-002", rep=0), _row(level=32768)], 0)
    b = P.keyed([_row(rep=0), _row(rep=1, text="y"), _row(item="lc-hard-003", rep=0)], 0)
    assert sorted(a) == [("lc-hard-001", 0), ("lc-hard-001", 1), ("lc-hard-002", 0)]   # 只取该档
    assert P.identity_rate(a, b) == (1, 2)          # 共同键 2 个：r0 相同、r1 不同
    assert P.identity_rate(a, a) == (3, 3)


def test_within_rep_identity_counts_rep_pairs_inside_each_item():
    rows = P.keyed([_row(rep=0, text="a"), _row(rep=1, text="a"), _row(rep=2, text="b"),
                    _row(item="lc-hard-002", rep=0, text="a"), _row(item="lc-hard-002", rep=1, text="c")], 0)
    # 题 001：3 rep → 3 对，相同 1 对（r0/r1）；题 002：2 rep → 1 对，不同；跨题的 001-r0 与 002-r0 相同不计
    assert P.within_rep_identity(rows) == (1, 4)


def test_identical_classes_greedy():
    same = lambda x, y: {x, y} in ({"a", "b"}, {"b", "c"})
    assert P.identical_classes(["a", "b", "c", "d"], same) == [["a", "b", "c"], ["d"]]


def test_seed_check_flags_cross_arm_mismatch_and_unsent():
    ok = {"x": [_row(rep=0), _row(rep=1)], "y": [_row(rep=0, level=32768), _row(rep=1)]}
    assert P.seed_check(ok) == (0, {0: {1107000}, 1: {1107001}})
    bad = {"x": [_row(rep=0)], "y": [_row(rep=0, sampling_seed=999)]}       # 同 rep 跨臂种子不一致
    n_bad, seeds = P.seed_check(bad)
    assert n_bad == 1 and len(seeds[0]) == 2
    assert P.seed_check({"x": [_row(rep=0, sent=False)]})[0] == 1          # 没发种子也算异常


def test_divergent_sign_counts_only_diverged_pairs():
    a = P.keyed([_row(rep=0, ok=True), _row(rep=1, ok=False, text="a1"), _row(rep=2, ok=True, text="a2"),
                 _row(rep=3, ok=False, text="a3")], 0)
    b = P.keyed([_row(rep=0, ok=False), _row(rep=1, ok=True, text="b1"), _row(rep=2, ok=False, text="b2"),
                 _row(rep=3, ok=False, text="b3")], 0)
    # r0 输出相同（pass 不同也不计——逐字相同的对不算分叉）；r1 b 赢、r2 a 赢、r3 同败
    ds = P.divergent_sign(a, b)
    assert ds["diverged"] == 3 and ds["b_wins"] == 1 and ds["a_wins"] == 1 and ds["p"] == 1.0


def test_rep_rates_and_sample_sd():
    rows = P.keyed([_row(item=i, rep=r, ok=ok) for i, r, ok in
                    [("x", 0, True), ("y", 0, True), ("x", 1, True), ("y", 1, False), ("x", 2, False), ("y", 2, False)]], 0)
    assert P.rep_rates(rows) == {0: 1.0, 1: 0.5, 2: 0.0}
    assert P.rep_sd(rows) == statistics.stdev([1.0, 0.5, 0.0]) == 0.5      # 样本式（n−1），不是总体式
    assert P.rep_sd(P.keyed([_row(rep=0)], 0)) is None                       # 单 rep 无 SD


def test_split_null_enumerates_unordered_disjoint_splits():
    mat = {"x": {r: r < 3 for r in range(6)}, "y": {r: True for r in range(6)}}
    xs = P.split_null(mat, 2, "success")
    assert len(xs) == 45 and xs == sorted(xs)               # C(6,2)·C(4,2)/2
    assert len(P.split_null(mat, 3, "success")) == 10       # C(6,3)/2
    # 最大差：{0,1} 对 {3,4}（x 全过对全不过）→ 成功率 (2+2)/4 − (0+2)/4 = 0.5
    assert max(xs) == 0.5 and min(xs) == 0.0
    ph = P.split_null(mat, 2, "pass_hat_2")
    assert max(ph) == 0.5                                    # Pass^2：x 两 rep 都过 1 对 0，y 恒 1 → (2−1)/2


def test_null_threshold_uses_tail_probability_not_quantile():
    # 20 个值：0.0 ×14、0.2 ×5、0.3 ×1 → P(≥0.2)=30%、P(≥0.3)=5%
    null = sorted([0.0] * 14 + [0.2] * 5 + [0.3])
    assert P.q_rank(null, 0.95) == 0.2                       # 最近秩 95% 分位 = 0.2，但 P(≥0.2)=30% 不是 5%
    assert P.tail_p(null, 0.2) == 0.3 and P.tail_p(null, -0.2) == 0.3 and P.tail_p(null, 0.25) == 0.05
    assert P.null_threshold(null) == (0.3, 0.05)
    assert P.null_threshold([0.0] * 10 + [0.1] * 10) == (None, None)       # 最大值尾概率 50% > 5%：超出支撑
    assert P.tail_p([], 0.1) is None


def test_q_rank_nearest_rank():
    xs = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    assert P.q_rank(xs, 0.95) == 0.9 and P.q_rank(xs, 0.9) == 0.8 and P.q_rank(xs, 0.5) == 0.4
    assert P.q_rank([], 0.5) is None


def test_boot_small_n_deterministic():
    const = {"a": {0: True, 1: True}, "b": {0: True, 1: True}}
    assert P.boot_l0(const, "success") == (1.0, 1.0) and P.boot_l0(const, "pass_hat_2") == (1.0, 1.0)
    assert P.boot_item_rate(const) == (1.0, 1.0)
    mixed = {"a": {0: True, 1: True}, "b": {0: False, 1: False}}
    # 按题重抽 2 题：均值只能取 0 / 0.5 / 1，10000 次里两端都会出现
    assert P.boot_item_rate(mixed) == (0.0, 1.0)
    assert P.boot_l0(mixed, "success") == P.boot_l0(mixed, "success")     # 固定种子 → 确定


def test_paired_item_resamples_items_only():
    same = {"i1": ([[True, False]], [[True, False]]), "i2": ([[False, False]], [[False, False]])}
    r = P.paired_item(same, "success_rate")
    assert (r["diff"], r["lo"], r["hi"], r["up"], r["down"]) == (0.0, 0.0, 0.0, 0, 0)   # 相同数据零宽
    one = {"i1": ([[False, False]], [[True, True]]), "i2": ([[True, False]], [[True, False]])}
    r = P.paired_item(one, "success_rate")
    assert r["diff"] == 0.5 and r["lo"] == 0.0 and r["hi"] == 1.0 and (r["up"], r["down"]) == (1, 0)


def test_arm_minutes_parses_start_and_rc_lines():
    log = ("2026-11-07 13:11:50 ARM orch-turn-none [A m] start: --guard off\n"
           "2026-11-07 15:04:58 ARM orch-turn-none harness rc=0 hung=0\n"
           "2026-11-07 15:04:58 arm orch-turn-none: ok\n"
           "2026-11-07 15:05:00 ARM orch-turn-w1 [A m] start: --guard on\n")
    out = P.arm_minutes(log)
    assert list(out) == ["orch-turn-none"]                   # 没有 rc 行的臂不计
    assert abs(out["orch-turn-none"] - (113 + 8 / 60)) < 1e-9


def test_batch_span_counts_deploys_and_done():
    log = ("2026-11-07 12:55:54 config: X\n2026-11-07 12:55:55 START deploy m --retain\n"
           "2026-11-07 12:57:43 START deploy m --retain\n2026-11-07 13:10:50 READY host=h\n"
           "[colab] Session terminated.\n2026-11-07 22:15:31 DONE BATCH_RC=0 SKIPPED=0\n")
    bs = P.batch_span(log)
    assert bs["deploys"] == 2 and bs["ready"] == ["2026-11-07 13:10:50"]
    assert abs(bs["minutes"] - (9 * 60 + 19 + 37 / 60)) < 1e-9
