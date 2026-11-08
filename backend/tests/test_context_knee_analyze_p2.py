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


# ---------------------------------------------------------------- catalog 臂可缺（p2b 批起）与 --p2 W 节
def _arm_dirs(root, names):
    for n in names:
        (root / n).mkdir(parents=True, exist_ok=True)
        (root / n / "transcripts.jsonl").write_text("", encoding="utf-8")


def test_layout_arms_catalog_optional_but_turn_and_full_required(tmp_path):
    import pytest
    base = ["orch-turn-none", "orch-turn-w1", "orch-turn-w1w2", "orch-turn-full"]
    _arm_dirs(tmp_path / "b", base)
    assert P.layout_arms(tmp_path / "b") == base                      # 无 catalog：不报缺、不塞进臂表
    _arm_dirs(tmp_path / "c", base + ["orch-turn-catalog"])
    assert P.layout_arms(tmp_path / "c") == base[:3] + ["orch-turn-catalog", "orch-turn-full"]   # 有 catalog：p2 原顺序
    _arm_dirs(tmp_path / "d", ["orch-turn-none", "orch-turn-w1w2", "orch-turn-full"])
    with pytest.raises(SystemExit):
        P.layout_arms(tmp_path / "d")                                  # 缺 w1 仍报错


def test_prod_prefix_without_catalog_takes_none_only_if_decl_is_effective():
    new = {"prefix_decl": "catalog", "tool_schema_mode_effective": "catalog", "prefix_base_tokens": 6051}
    old = {"tool_schema_mode_effective": "catalog", "prefix_base_tokens": 13757}       # 2d3fd99c 之前：声明值
    assert P.prod_prefix({"orch-turn-none": {"records": [new, dict(new)]}}, False) == ("orch-turn-none", 6051, None)
    src, pb, warn = P.prod_prefix({"orch-turn-none": {"records": [new, old]}}, False)
    assert (src, pb) == ("orch-turn-none", None) and "本批无 catalog 臂" in warn          # 不拿声明值顶替
    cat = {"orch-turn-catalog": {"records": [{"prefix_base_tokens": 6022}]}, "orch-turn-none": {"records": [old]}}
    assert P.prod_prefix(cat, True) == ("orch-turn-catalog", 6022, None)


def test_strip_u3_catalog_replaces_only_the_catalog_table():
    u = ["### U.\n", "#### U2. x\n", "row", "#### U3. D44：实参预检回执与 catalog 对 w1w2\n", "表头", "| catalog − w1w2 | 合并 0 档 |",
         "", "预检回执（feed 里 …）：\n", "| 组 | 回执 |", "#### U4. y\n"]
    out = P.strip_u3_catalog(u)
    assert "| catalog − w1w2 | 合并 0 档 |" not in out and "表头" not in out
    assert out[:3] == u[:3] and out[-3:] == u[-3:] and any("本批无 catalog 臂" in x for x in out)


def test_prefix_meta_ok_old_prod_rows_untrusted():
    assert P.prefix_meta_ok({"prefix_decl": "catalog", "tool_schema_mode_effective": "catalog"})
    assert not P.prefix_meta_ok({"prefix_decl": "full", "tool_schema_mode_effective": "catalog"})
    assert not P.prefix_meta_ok({"prefix_mode": "prod", "tool_schema_mode_effective": "catalog"})      # 旧 prod 行
    assert P.prefix_meta_ok({"prefix_mode": "full", "tool_schema_mode_effective": "full"})             # 旧 full 行


def test_first_divergent_turn_and_prompt_delta():
    a = _row()
    assert P.first_divergent_turn(a, _row()) is None
    assert P.first_divergent_turn(a, _row(thoughts=("tX", "t2"))) == 0
    assert P.first_divergent_turn(a, _row(second_text="other")) == 1
    assert P.first_divergent_turn(a, _row(final="ok2")) == 2                # 轮全同、只差末轮答复 → 轮数
    assert P.first_prompt_delta(a, _row(prompt=6191)) == 29
    assert P.first_prompt_delta(a, {"gens": [{}]}) is None


def test_recall_items_and_band_knee():
    rows = [_row(item="x", rep=0), _row(item="x", rep=1), _row(item="y", rep=0)]
    rows[0]["score"].update(recall_ok=True)
    rows[1]["score"].update(recall_ok=False, aborted=True)                  # aborted 不计（aggregate 同口径）
    assert P.recall_items(rows) == {"x": [True]}
    ag = {0: {"_items": {"a": [True, True], "b": [True, True]}}, 32768: {"success_rate": 1.0}, 49152: {"success_rate": 0.5}}
    assert P.band_knee(ag) == (1.0, 49152)
    assert P.band_knee({32768: {"success_rate": 0.0}}) == (None, None)


def _sess(tmp, name, text):
    d = tmp / name
    d.mkdir()
    (d / "system.json").write_text(json.dumps({"role": "system", "content": text}), encoding="utf-8")
    return d


def _group(name, rows):
    from context_knee import analyze as A
    from context_knee.quality import COHERENT_THRESHOLDS
    g = {"name": name, "records": rows, "missing": [], "concurrent_levels": set()}
    return g, A.aggregate(g, dict(COHERENT_THRESHOLDS), set())


def test_sec_w_identity_input_diff_and_paired_rows(tmp_path):
    n = "orch-turn-none"
    meta = {"prefix_decl": "catalog", "tool_schema_mode_effective": "catalog"}
    old_rows, new_rows = [], []
    for i, item in enumerate(["i1", "i2"]):
        for rep in (0, 1, 2):
            # 输出按 rep 不同（second_text 带 rep 号）、p2 只有 rep 0 过：rep 错位配对（(i, r) 对 (i, 1−r)）会改变逐字相同数与分叉对数
            o = _row(item=item, rep=rep, ok=rep in (0, 2), prompt=6165, second_text=f"fin{rep}")
            o.update(prefix_mode="prod", tool_schema_mode_effective="catalog", prefix_base_tokens=13757,
                     _local_session=_sess(tmp_path, f"o{i}{rep}", "a\n- re-read the changed section\nb"))
            old_rows.append(o)
            if rep == 2:
                continue                       # 本批只有 rep 0/1：p2 的 rep 2 只在一侧（W1 仅 p2 计数、W2 截到共同 rep）
            # 本批：i1 两 rep 改过（输出第 2 轮分叉、两 rep 都过），i2 两 rep 输出逐字相同（同过同败）
            nw = _row(item=item, rep=rep, ok=item == "i1" or rep == 0, prompt=6194,
                      second_text=f"fin{rep}x" if item == "i1" else f"fin{rep}")
            nw.update(prefix_mode="prod", prefix_base_tokens=6051, **meta,
                      _local_session=_sess(tmp_path, f"n{i}{rep}", "a\n- do not re-read\nb"))
            new_rows.append(nw)
    g_new, ag_new = _group(n, new_rows)
    g_old, ag_old = _group(n + P.TAG_P2, old_rows)
    L = P.sec_w({n: g_new}, {n: ag_new}, [n], {g_old["name"]: g_old}, {g_old["name"]: ag_old}, "ref", {}, {},
                only_p2=["orch-turn-catalog"])
    txt = "\n".join(L)
    assert "p2 有、本批无：catalog" in txt                                   # main 从 p2 目录算出的缺臂显式列出
    # 仅 p2 2 行（rep 2）不进配对；逐字相同 2/4 = i2 的两 rep；「声明值」标在实际不合格的 p2 侧
    assert "| none | 0 | 2 / 0 | 2/4 | 0/4 | 0/4 | 4/4 | +29:4 | 13757（声明值，不可比） → 6051 | t1:2 |" in txt
    assert "⚠️ L0 输出对该改动稳健" in txt                                  # 2/4 ≥ 一半 → 告警
    assert "旧：- re-read the changed section ‖ 新：- do not re-read" in txt and "1 种不同的差（覆盖 4 对" in txt
    # W2：p2 截到 rep 0–1（2/4 = 50%），不截是 4/6 = 67%
    assert "| none | 0（rep 0–1 配对，none@p2 3→2 rep） | 2/4 = 50% → 3/4 = 75% |" in txt
    assert "| none | 0 | 2 | 1 / 0 |" in txt                                # W3：只数分叉的 i1 两对，r1 p2b 赢


def test_sec_w_only_p2_from_loaded_groups_still_listed():
    n = "orch-turn-none"
    g, ag = _group(n, [_row()])
    go, ago = _group(n + P.TAG_P2, [_row()])
    gc, agc = _group("orch-turn-catalog" + P.TAG_P2, [_row()])
    L = P.sec_w({n: g}, {n: ag}, [n], {go["name"]: go, gc["name"]: gc}, {go["name"]: ago, gc["name"]: agc}, "ref", {}, {})
    assert "p2 有、本批无：catalog" in "\n".join(L)


def test_aligned_truncates_to_common_reps_only_when_rep_sets_differ():
    a = [_row(item="x", rep=r, ok=r < 2) for r in range(6)]
    b = [_row(item="x", rep=r, ok=False) for r in range(2)]
    al = P.aligned(a, b, 0, ("orch-turn-full", "orch-turn-w1w2"))
    assert al["items_a"] == {"x": [True, True]} and al["reps"] == [0, 1]
    assert al["note"] == "rep 0–1 配对，full 6→2 rep"
    same = P.aligned(b, b, 0)
    assert same["note"] == "" and same["items_a"] == {"x": [False, False]}
    assert P.rep_lab({0, 2, 5}) == "rep 0,2,5" and P.rep_lab({3}) == "rep 3"


def test_e_pairs_aliases_only_same_seed_pairs_with_unequal_reps():
    G = {"orch-turn-none": {"name": "orch-turn-none", "missing": [], "dropped": [],
                            "records": [_row(item="x", rep=r) for r in range(6)] + [_row(item="x", rep=r, level=32768) for r in range(2)]},
         "orch-turn-w1w2": {"name": "orch-turn-w1w2", "missing": [], "dropped": [],
                            "records": [_row(item="x", rep=r) for r in range(2)]},
         "orch-turn-w1w2@d44": {"name": "orch-turn-w1w2@d44", "missing": [], "dropped": [],
                                "records": [_row(item="x", rep=r) for r in range(2)]}}
    out, alias = P.e_pairs(["orch-turn-none:orch-turn-w1w2", "orch-turn-w1w2@d44:orch-turn-none"], G,
                           ["orch-turn-none", "orch-turn-w1w2"])
    (a1, b1, lab1), (a2, b2, lab2) = out
    assert lab1 == "orch-turn-none→orch-turn-w1w2（指定；L0 rep 0–1 配对）"
    al = {g["name"]: g for g in alias}
    assert sorted(int(r["rep"]) for r in al[a1]["records"] if r["level"] == 0) == [0, 1]
    assert sum(1 for r in al[a1]["records"] if r["level"] == 32768) == 2          # 未截的档原样
    assert (a2, b2, lab2) == ("orch-turn-w1w2@d44", "orch-turn-none", "orch-turn-w1w2@d44→orch-turn-none（指定）")   # 跨批不截


# ---------------------------------------------------------------- main 端到端（合成批目录）
LV4 = (0, 32768, 49152, 57344)


def _arm_rows(l0reps, ok, full=False, decl=None, eff=None):
    rows = []
    for lv in LV4:
        for it in ("i1", "i2"):
            for rep in range(l0reps if lv == 0 else 2):
                r = _row(item=it, rep=rep, level=lv, ok=ok(it, lv, rep), second_text=f"fin{rep}")
                r.update(model="m", stamp="s1", prefix_mode="full" if full else "prod", wall_s=10.0,
                         prefix_decl=decl or ("full" if full else "catalog"),
                         tool_schema_mode_effective=eff or ("full" if full else "catalog"),
                         prefix_base_tokens=13786 if full else 6051)
                rows.append(r)
    return rows


def _write_arm(d, rows):
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcripts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _batch(tmp, w12_decl=None, none_decl=None, p2=True):
    """无 catalog 的本批：none / full L0 6 rep，w1 / w1w2 L0 2 rep。full L0 rep 0/1 全过、rep 2–5 全败；
    w1w2 / w1 L0 i1 过 i2 败。p2 批：w1w2 L0 6 rep（rep 0/1 全败、rep 2–5 全过）+ catalog 臂（本批没有）。"""
    data = tmp / "data"
    _write_arm(data / "orch-turn-none", _arm_rows(6, lambda it, lv, rep: rep % 2 == 0, decl=none_decl))
    for n in ("orch-turn-w1", "orch-turn-w1w2"):
        _write_arm(data / n, _arm_rows(2, lambda it, lv, rep: it == "i1" or lv > 0,
                                       decl=w12_decl if n == "orch-turn-w1w2" else None))
    _write_arm(data / "orch-turn-full", _arm_rows(6, lambda it, lv, rep: rep < 2, full=True))
    (data / "run_colab.log").write_text("2026-11-08 10:00:00 START deploy m\n2026-11-08 11:00:00 DONE BATCH_RC=0\n",
                                        encoding="utf-8")
    _write_arm(tmp / "ref", _arm_rows(2, lambda it, lv, rep: True))
    argv = ["--data", str(data), "--ref", str(tmp / "ref"), "--out-md", str(tmp / "out.md")]
    if p2:
        _write_arm(tmp / "p2" / "orch-turn-w1w2", _arm_rows(6, lambda it, lv, rep: lv == 0 and rep >= 2))
        _write_arm(tmp / "p2" / "orch-turn-catalog", _arm_rows(2, lambda it, lv, rep: True))
        argv += ["--p2", str(tmp / "p2")]
    return argv


def test_main_catalog_absent_and_asymmetric_reps(tmp_path):
    assert P.main(_batch(tmp_path)) == 0
    txt = (tmp_path / "out.md").read_text(encoding="utf-8")
    # 缺 catalog：U3 的 catalog − w1w2 表换成说明行（main 的 strip_u3_catalog），不渲染空表
    assert "本批无 catalog 臂（11-07 拍板删臂）：U3 的 catalog − w1w2 按档表不渲染" in txt
    assert "orch-turn-catalog − orch-turn-w1w2 按档" not in txt and "合并 0 档" not in txt
    assert "### Q. 前缀对照：orch-turn-w1w2（prod 前缀，本批生效 = catalog，行内 prefix_decl 为证）" in txt
    # W：p2 的 catalog 臂没载入（本批没有），仍要点名
    assert "p2 有、本批无：catalog" in txt
    # rep 数按臂实数写，不写死 6
    assert "臂内 none 6 / w1 2 / w1w2 2 / full 6 rep" in txt
    # V4：full 6 rep 对 w1w2 2 rep → 截到 rep 0–1：full 4/4 = 100% → w1w2 2/4 = 50%（不截是 33% → 50%，+16.7）
    assert "| w1w2 − full | 0（m=2；rep 0–1 配对，full 6→2 rep） | 100% → 50% | -50（P=" in txt
    assert "| w1w2 − full | 四档合并（0 档 rep 0–1 配对） |" in txt
    # E 节同口径：本批臂间 rep 数不同的对走截后别名组，标签注明
    assert "| orch-turn-full→orch-turn-w1w2（指定；L0 rep 0–1 配对） | 0 | -50 [" in txt
    assert "〔" not in txt                                                     # 别名组名不外泄
    # W2：p2 w1w2 6 rep 截到 rep 0–1（全败）→ 本批 2/4（不截是 8/12 = 67% → 50%）
    assert "| w1w2 | 0（rep 0–1 配对，w1w2@p2 6→2 rep） | 0/4 = 0% → 2/4 = 50% |" in txt
    assert "| w1w2 | 0 | 8 / 0 |" in txt                                       # W1：仅 p2 8 行（rep 2–5）


def test_main_flags_w1w2_not_catalog_in_q_title(tmp_path):
    assert P.main(_batch(tmp_path, w12_decl="full", p2=False)) == 0
    txt = (tmp_path / "out.md").read_text(encoding="utf-8")
    assert "### Q. ⚠️ orch-turn-w1w2 行内 prefix_decl=['full']、tool_schema_mode_effective=['catalog']，不是 catalog" in txt
    assert "本批生效 = catalog，行内 prefix_decl 为证" not in txt


def test_main_exits_when_prod_prefix_unavailable(tmp_path):
    import pytest
    with pytest.raises(SystemExit, match="F 节生产前缀取不到"):
        P.main(_batch(tmp_path, none_decl="full", p2=False))       # none 行 prefix_decl ≠ 生效 → 不取声明值，停
