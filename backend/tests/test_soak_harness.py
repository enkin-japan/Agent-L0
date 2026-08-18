"""长稳工装判据金测试（`scripts/soak_longrun.py`）。

守的是什么：这支工装的产出会直接当「生产就绪门禁」的判决进 EXECUTION_LEDGER。它的
缺陷史（首过评审 6 → 5+15 → 5 → 阶段复审 9+9 → 又 2 Critical）几乎全是同一类：
**判据写得不可能失败，或者写得不可能通过**——两种都不会报错，只会让结论看上去成立。
这类缺陷靠人读代码抓了五轮还在漏，所以要有回归网。

纯 CPU：不加载模型、不起服务、不碰 GPU、不读真产物，全部用合成行。

下面每个用例都对应一次真实被评审抓到的缺陷，注释里标了出处。
"""
import importlib.util
import json
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "soak_longrun_under_test", BACKEND_ROOT / "scripts" / "soak_longrun.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sk = _load()


def _req(t, kind="chat", **kw):
    d = {"i": int(t), "t": float(t), "kind": kind, "ok": True}
    d.update(kw)
    return d


def _sys(t, rss=16000, wired=3000, comp=1000, ag=0, **kw):
    d = {"t": float(t), "server_rss_mb": rss, "wired_mb": wired,
         "compressed_mb": comp, "metrics": {"active_generations": ag},
         "health_ms": 0.5}
    d.update(kw)
    return d


# ---------- _quiet_rows ----------

def test_quiet_rows_excludes_row_with_no_preceding_request():
    """没有已知的前一次请求 = 无从判定静默，必须排除。

    出处：首过评审 M1（第二轮）。`t0 基线` 行写在起跑自检的 768 token 长链刚跑完
    之后、t≈0.1，而 req.jsonl 里没有更早的请求。放行它会把全文件污染最重的一行
    （实测 wired=18511，真实基线 3066–3327）当成静默态，单它就把干净窗第一段从
    3086.9 抬到 3568.9——凭空造出「第一段高、后面走平」的假象。
    """
    rows = [_req(100), _req(200)]
    sysrows = [_sys(0.1, wired=18511), _sys(150, wired=3000), _sys(300, wired=3000)]
    quiet = sk._quiet_rows(sysrows, rows, quiet_s=5.0)
    assert all(r["t"] != 0.1 for r in quiet), "t0 基线行不该被当成静默态"
    assert {r["t"] for r in quiet} == {150.0, 300.0}


def test_quiet_rows_excludes_samples_too_soon_after_a_request():
    """距上一请求结束不足门槛的行要排除——wired 的回落有秒级滞后。"""
    rows = [_req(100)]
    sysrows = [_sys(100.5, wired=18000), _sys(104.9, wired=9000), _sys(106, wired=3000)]
    quiet = sk._quiet_rows(sysrows, rows, quiet_s=5.0)
    assert [r["t"] for r in quiet] == [106.0]


def test_quiet_rows_threshold_is_a_parameter_not_a_global():
    """门槛必须可由调用方传入：冒烟档与生产档用的不是同一个值。"""
    rows = [_req(100)]
    sysrows = [_sys(103)]
    assert sk._quiet_rows(sysrows, rows, quiet_s=5.0) == []
    assert len(sk._quiet_rows(sysrows, rows, quiet_s=3.0)) == 1


def test_smoke_profile_idle_window_is_longer_than_its_quiet_threshold():
    """冒烟档的空闲窗必须长于静默门槛，否则静默态判据恒不可过。

    出处：首过评审 C1（第三轮）。旧冒烟档 IDLE_SHORT=(2,8) 短于 QUIET=5.0，实测三份
    冒烟产物的静默行只有 1/2/4 行，会让每一次冒烟都带着一条纯属档位假象的
    「判据阻断」退 1——而冒烟是每次长稳前必跑的一关。恒不可过的判据与恒可过的判据
    一样有害：前者训练操作者无视告警。
    """
    import copy
    saved = {k: copy.copy(getattr(sk, k))
             for k in ("MIX", "IDLE_SHORT", "IDLE_LONG", "LONG_IDLE_EVERY",
                       "SAMPLE_INTERVAL", "HEARTBEAT_INTERVAL", "QUIET_AFTER_REQ_S")}
    try:
        sk._apply_smoke_profile()
        assert sk.IDLE_SHORT[0] > sk.QUIET_AFTER_REQ_S, (
            f"冒烟空闲窗下界 {sk.IDLE_SHORT[0]}s 不大于静默门槛 "
            f"{sk.QUIET_AFTER_REQ_S}s：静默态采样会恒为个位数")
    finally:
        for k, v in saved.items():
            setattr(sk, k, v)


# ---------- measure ----------

def test_measure_takes_its_caliber_from_spec_not_module_globals():
    """口径必须由调用方传入。

    出处：首过评审 C2（第三轮）。`--reanalyze` 不会调用 `_apply_smoke_profile()`，
    读全局就会给一份冒烟产物盖上生产权重的 `口径`——**记一份假的出处比不记更糟**，
    对着它核 `by_kind` 会得出「负载配错了」。
    """
    rows = [_req(10, "chat", turn=1, tps=90.0, ttft=0.3)]
    spec = {"mix": [("chat", 0.25), ("tool", 0.25), ("longchain", 0.25),
                    ("cancel", 0.25)],
            "sample_interval": 60.0, "quiet_after_req_s": 3.0, "来源": "profile.json"}
    m, _ = sk.measure(rows, [], 100.0, spec=spec)
    assert m["口径"]["SAMPLE_INTERVAL"] == 60.0
    assert m["口径"]["QUIET_AFTER_REQ_S"] == 3.0
    assert m["口径"]["来源"] == "profile.json"
    assert set(m["by_kind"]) == {"chat", "tool", "longchain", "cancel"}


def test_measure_segment_counts_are_per_key():
    """分段行数要按**每个键**算，不能只数行。

    出处：首过评审 建议5（第三轮）。`_quiet_rows` 只保证 `server_rss_mb`，
    而 `wired_mb`/`compressed_mb` 来自独立会失败的 `vm_stat`。只数行的话，某一段
    `wired_mb` 全空、表里那一格是 null，而分段下限阻断照样不触发——正是它要堵的洞
    换个层级复发。
    """
    rows = [_req(10)]
    # 第二段的行没有 wired_mb（vm_stat 失败）
    sysrows = [_sys(20), _sys(40),
               {"t": 130.0, "server_rss_mb": 16000, "metrics": {"active_generations": 0},
                "vm_stat_error": "boom"},
               _sys(230), _sys(250)]
    m, _ = sk.measure(rows, sysrows, 300.0, spec={"quiet_after_req_s": 5.0})
    n = m["mem_三段_静默态_n"]
    assert n["server_rss_mb"][1] == 1, "第二段有一行 RSS"
    assert n["wired_mb"][1] == 0, "第二段没有任何 wired 样本，必须数出来"
    assert m["mem_三段_静默态"]["wired_mb"][1] is None


def test_measure_returns_valid_rows_separately_not_smuggled_in_the_dict():
    """有效采样行走返回值，不夹带在判据 dict 里让每个调用方去 pop。"""
    m, valid = sk.measure([_req(10)], [_sys(20), {"t": 30.0}], 100.0)
    assert not any(k.startswith("_") for k in m), "判据 dict 不该有内部键"
    assert [r["t"] for r in valid] == [20.0]


def test_measure_rounds_sub_second_keys_to_four_places():
    """ttft/wall 三位小数会把 0.3692/0.3774 压成 0.369/0.377，引用方一旦写了四位
    就再也复现不出来（台账里真出过这么一处）。"""
    rows = [_req(t, "tool", ttft=0.36921, tps=90.0) for t in range(1, 31)]
    m, _ = sk.measure(rows, [], 100.0)
    assert m["drift_三段"]["tool"]["ttft"]["三段"][0] == 0.3692


def test_measure_is_deterministic_for_the_same_inputs():
    """同样的输入 + 同样的窗口 = 同样的判据。判据必须可复现，这是重算入口的前提。"""
    rows = [_req(t, "chat", turn=1 + t % 3, tps=90.0 + t % 5, ttft=0.3,
                 reused=10, reuse_expect=10, assembly="delta", delta_feed=True)
            for t in range(1, 40)]
    sysrows = [_sys(t) for t in range(50, 300, 20)]
    a, _ = sk.measure(rows, sysrows, 300.0)
    b, _ = sk.measure(rows, sysrows, 300.0)
    assert json.dumps(a, sort_keys=True, default=str) == \
        json.dumps(b, sort_keys=True, default=str)


# ---------- 前缀复用判据（本工装做得最扎实、也最该守住的一条）----------

def test_reuse_ratio_denominator_drops_rows_without_an_expectation():
    """`0 >= (None or 0) - 2` 恒真：没有期望值的行必须剔出分母。

    出处：v2 首过评审 M5。上一轮生成挂掉的会话会让这条占比在退化最严重时读出 100%。
    """
    rows = [
        _req(10, "chat", turn=2, reused=100, reuse_expect=100),   # 达标
        _req(20, "chat", turn=2, reused=0, reuse_expect=None),    # 没期望值，不该算达标
    ]
    m, _ = sk.measure(rows, [], 100.0)
    assert m["前缀复用"]["命中达期望占比"] == 1.0
    assert m["前缀复用"]["无期望值行数"] == 1


def test_reuse_ratio_catches_a_miss():
    """命中不足要真的把占比拉下来——这条判据不能只会给 1.0。"""
    rows = [_req(10, "chat", turn=2, reused=100, reuse_expect=100),
            _req(20, "chat", turn=2, reused=50, reuse_expect=100)]
    m, _ = sk.measure(rows, [], 100.0)
    assert m["前缀复用"]["命中达期望占比"] == 0.5


def test_hot_bucket_excludes_cold_rebuild_rounds():
    """冷重建轮（缓存被 TTL 回收后）不能混进「turn2+ 热」。

    出处：阶段复审 M4。一次冷预填的 TTFT 高一个量级，随机落段足以造出假的单调劣化。
    """
    rows = [_req(t, "chat", turn=2, tps=90.0, ttft=0.3) for t in range(1, 10)]
    rows.append(_req(10, "chat", turn=2, tps=20.0, ttft=3.0, cache_rotated=True))
    m, _ = sk.measure(rows, [], 100.0)
    assert m["前缀复用"]["turn≥2 非冷轮样本"] == 9


# ---------- 其它 ----------

def test_peak_steps_does_not_count_the_first_establishment():
    """首次把高水位从 0 抬到 17.5GB 不是「抬升」。出处：阶段复审 m1（原文多算一次）。"""
    rows = [_req(1, peak_mem=17.5), _req(2, peak_mem=17.5),
            _req(3, peak_mem=17.8), _req(4, peak_mem=18.0)]
    assert sk._peak_steps(rows) == 2


def test_first_last_skips_rows_missing_the_key():
    """首末行若恰好是 sampler_error 行，直接下标会得到两个 null，
    而看的人无从知道是「没换页」还是「没采到」。出处：阶段复审 m23。"""
    rows = [{"t": 1, "sampler_error": "boom"}, {"t": 2, "swapouts": 5},
            {"t": 3, "swapouts": 9}, {"t": 4, "sampler_error": "boom"}]
    assert sk._first_last(rows, "swapouts") == [5, 9]
    assert sk._first_last([{"t": 1}], "swapouts") == [None, None]


def test_reanalyze_rejects_a_nonpositive_window(tmp_path, capsys):
    """`if t_end` 对 0.0 为假会静默退回全程窗口。出处：首过评审 m6（第二轮）。"""
    (tmp_path / "req.jsonl").write_text(json.dumps(_req(10)) + "\n", encoding="utf-8")
    assert sk.reanalyze(tmp_path, 0.0, "t") == 1
    assert "非正" in capsys.readouterr().out


def test_reanalyze_prefers_the_exact_window_recorded_by_finish(tmp_path):
    """缺省窗口要精确复现收官口径。

    出处：首过评审 M3（第二轮）+ 建议10（第三轮）。`hours` 是两位小数、±18 秒，
    足以把某条请求挪到隔壁段；而拿 `窗口秒` 与末行比大小再决定用不用，会在采样线程
    比 finish() 晚落一行时悄悄退回近似窗口。
    """
    (tmp_path / "req.jsonl").write_text(
        "\n".join(json.dumps(_req(t)) for t in (10, 20, 30)) + "\n", encoding="utf-8")
    (tmp_path / "sys.jsonl").write_text(
        json.dumps(_sys(999)) + "\n", encoding="utf-8")   # 比 窗口秒 还晚的采样行
    (tmp_path / "summary.json").write_text(
        json.dumps({"窗口秒": 100.0, "hours": 0.03, "smoke": False}), encoding="utf-8")
    assert sk.reanalyze(tmp_path, None, "t") == 0
    out = json.loads((tmp_path / "summary-t.json").read_text(encoding="utf-8"))
    assert out["重算窗口"]["t_end 秒"] == 100.0
    assert "窗口秒" in out["重算窗口"]["来源"]
    assert out["重算窗口"]["窗外丢弃行数"] == 1


def test_reanalyze_stamps_the_caliber_of_the_run_being_reanalyzed(tmp_path):
    """重算一份冒烟产物，`口径` 必须是那次跑的，不是当前模块常量。出处：C1/C2 同源。"""
    (tmp_path / "req.jsonl").write_text(json.dumps(_req(10)) + "\n", encoding="utf-8")
    (tmp_path / "profile.json").write_text(json.dumps({
        "smoke": True, "mix": [["chat", 0.25], ["tool", 0.25],
                               ["longchain", 0.25], ["cancel", 0.25]],
        "sample_interval_s": 60.0}), encoding="utf-8")
    assert sk.reanalyze(tmp_path, None, "t") == 0
    out = json.loads((tmp_path / "summary-t.json").read_text(encoding="utf-8"))
    assert out["smoke"] is True
    assert out["口径"]["SAMPLE_INTERVAL"] == 60.0
    assert out["口径"]["来源"] == "profile.json"


def test_reanalyze_says_unknown_rather_than_null_when_identity_is_missing(tmp_path):
    """产物必须自报是不是冒烟；两边都没标记时明说不知道，别留 null 让人当成「不是」。"""
    (tmp_path / "req.jsonl").write_text(json.dumps(_req(10)) + "\n", encoding="utf-8")
    assert sk.reanalyze(tmp_path, None, "t") == 0
    out = json.loads((tmp_path / "summary-t.json").read_text(encoding="utf-8"))
    assert out["smoke"] != {} and out["smoke"] is not None
    assert "未知" in str(out["smoke"])


def test_reanalyze_refuses_a_directory_with_no_usable_rows(tmp_path, capsys):
    """空目录/坏数据要走友好拒绝，不是甩 traceback。出处：首过评审 m7（第二轮）。"""
    (tmp_path / "req.jsonl").write_text(json.dumps({"i": 1}) + "\n", encoding="utf-8")
    assert sk.reanalyze(tmp_path, None, "t") == 1
    assert "无法重算" in capsys.readouterr().out


def test_server_cache_ttl_is_read_not_hardcoded():
    """泄漏判据的期望值靠它；硬编码必然与 config 漂移。"""
    assert sk._server_cache_ttl_s() > 0


@pytest.mark.parametrize("kind", ["chat", "tool", "longchain", "cancel"])
def test_all_four_legs_appear_in_by_kind(kind):
    """四条腿都要在 by_kind 里有格子——某条腿零样本要看得见，不能悄悄消失。"""
    m, _ = sk.measure([_req(10, kind)], [], 100.0)
    assert kind in m["by_kind"]
