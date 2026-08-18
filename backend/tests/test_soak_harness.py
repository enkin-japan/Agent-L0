"""长稳工装判据金测试（`scripts/soak_longrun.py`）。

守的是什么：这支工装的产出会直接当「生产就绪门禁」的判决进 EXECUTION_LEDGER。它的
缺陷史（首过评审 6 → 5+15 → 5 → 阶段复审 9+9 → 又 2 Critical）几乎全是同一类：
**判据写得不可能失败，或者写得不可能通过**——两种都不会报错，只会让结论看上去成立。
这类缺陷靠人读代码抓了五轮还在漏，所以要有回归网。

纯 CPU：不加载模型、不起服务、不碰 GPU、不读真产物，全部用合成行。

下面每个用例都对应一次真实被评审抓到的缺陷，注释里标了出处。
"""
import contextlib
import importlib.util
import inspect
import json
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _load():
    """从**源码**编译加载，刻意绕开 .pyc。

    字节码缓存按 (mtime 秒, 源码大小) 判新旧。变异证明注入的改动常常**同大小、且落在
    同一秒内**（`== 1` → `== 0` 就是），于是 `exec_module` 会拿旧字节码顶上，整份回归网
    跑的是变异前的代码、变异体凭空「存活」。实测：S3 冷/温档交替那个变异体第一次就是
    这样活下来的，隔离复跑却证明它忠实——问题在夹具不在变异体。
    这份文件的全部价值就是回归网，读到陈旧字节码等于网是假的。
    """
    path = BACKEND_ROOT / "scripts" / "soak_longrun.py"
    spec = importlib.util.spec_from_file_location("soak_longrun_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), mod.__dict__)
    return mod


sk = _load()


def _req(t, kind="chat", **kw):
    d = {"i": int(t), "t": float(t), "kind": kind, "ok": True}
    d.update(kw)
    return d


@contextlib.contextmanager
def _smoke_profile():
    """套上冒烟档，退出时把模块全局逐个还原。

    `sk` 是整份文件共享的模块单例：`_apply_smoke_profile()` 改的是全局，漏还原一个就是
    一次**顺序相关**的静默污染。手抄「要还原哪些键」的清单会随 `_apply_smoke_profile`
    的演进而漂移，故直接从函数源码里现读它声明的 `global`，加一个忘一个的情况不存在。
    """
    import copy
    import re
    src = inspect.getsource(sk._apply_smoke_profile)
    names = sorted({n.strip() for line in re.findall(r"^\s*global (.+)$", src, re.M)
                    for n in line.split(",")})
    assert names, "读不到 _apply_smoke_profile 声明的 global——还原清单会是空的"
    saved = {k: copy.copy(getattr(sk, k)) for k in names}
    try:
        sk._apply_smoke_profile()
        yield
    finally:
        for k, v in saved.items():
            setattr(sk, k, v)


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
    with _smoke_profile():
        assert sk.IDLE_SHORT[0] > sk.QUIET_AFTER_REQ_S, (
            f"冒烟空闲窗下界 {sk.IDLE_SHORT[0]}s 不大于静默门槛 "
            f"{sk.QUIET_AFTER_REQ_S}s：静默态采样会恒为个位数")


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


# ---------- 「离开」窗与占空比（长稳工装遗留两项，2026-08-18） ----------

def test_the_cold_away_window_actually_outlives_the_ttl():
    """★ 冷档「离开」窗必须真的**越过** TTL，否则冷重建路径永远零样本。

    出处：08-18 长稳跑的遗留修正项①。旧写法 `IDLE_LONG = (15min, 45min)` 的上界正好
    **等于**服务端 TTL 45min——数学上 uniform 取到上界的概率为 0，实测 8 个窗最大
    37.3min、`cache_rotated` 全程 0 次，一整轮 8 小时的冷重建路径没留下一个样本。
    这是「判据写得不可能通过」的另一种形态：负载档结构上就到不了被测的那个状态。
    """
    ttl = 45 * 60.0
    lo, hi, kind = sk._idle_window(sk.LONG_IDLE_EVERY, ttl)
    assert kind == "away_cold"
    assert lo > ttl, f"冷档下界 {lo}s 没越过 TTL {ttl}s：这一档一个冷样本都拿不到"
    assert hi > lo


def test_the_warm_away_window_stays_under_the_ttl():
    """温档反过来必须**够不着** TTL——两档要真的测两种东西，否则交替没有意义。"""
    ttl = 45 * 60.0
    lo, hi, kind = sk._idle_window(sk.LONG_IDLE_EVERY * 2, ttl)
    assert kind == "away_warm"
    assert hi < ttl, f"温档上界 {hi}s 越过了 TTL {ttl}s：它会和冷档测同一件事"


def test_the_first_away_window_is_the_cold_one():
    """冷档排第一：跑被提前中止时也至少留下一个冷样本。"""
    assert sk._idle_window(sk.LONG_IDLE_EVERY, 2700.0)[2] == "away_cold"


def test_away_windows_alternate_cold_and_warm():
    """交替而非随机——随机意味着一次跑仍可能一个冷样本都抽不到，而「零样本」正是要修的。"""
    kinds = [sk._idle_window(sk.LONG_IDLE_EVERY * k, 2700.0)[2] for k in range(1, 5)]
    assert kinds == ["away_cold", "away_warm", "away_cold", "away_warm"]


def test_ordinary_requests_get_the_short_window():
    assert sk._idle_window(1, 2700.0)[2] == "short"
    assert sk._idle_window(0, 2700.0)[2] == "short"


def test_smoke_profile_never_draws_an_away_window():
    """冒烟档磨不到 45min TTL，故「离开」窗整个不该出现（不是「跳过检查」，是不在射程内）。"""
    with _smoke_profile():
        for n in (1, 40, 80, 1000, 10 ** 5):
            assert sk._idle_window(n, 2700.0)[2] == "short", n


def test_duty_cycle_is_reported_so_the_window_is_not_read_as_full_load():
    """★ 占空比进产物：没有它，「8 小时无劣化」会被读成「连续满载 8 小时无劣化」。

    出处：08-18 长稳跑的遗留修正项②——8.87h 里 3.65h（41%）在「离开」窗中，实际生成
    占空比约 2.5%，而当时的口径文字写的是「一个完整工作日的连续使用」，差一个量级。
    """
    rows = [_req(10, "chat", wall=5.0), _req(20, "chat", wall=15.0),
            _req(30, "cancel")]                       # cancel 无 wall，不计入分子
    m, _ = sk.measure(rows, [], 100.0)
    assert m["生成占空比"] == 0.2


def test_the_two_away_bands_hold_against_the_real_configured_ttl():
    """★ 上面几条钉的是 `_idle_window(n, 2700.0)`——**测试自己写死的** TTL。

    生产的 TTL 从 `config.yaml` 读。把 `cache_ttl_minutes` 改成 ≤30，温档 (15,30)min
    就跨过了真 TTL、冷档 `(ttl+6, ttl+10)min` 与它重叠，两档塌成「都是冷」、交替失去
    意义——而那六条测试全都还是绿的。那正是这份文件开篇说的病：**判据够不着被测的
    那件事**，这次出现在为防它而写的守卫里（首审 #3）。故这一条只对**真配置**发问。
    """
    ttl = sk._server_cache_ttl_s()
    assert sk.IDLE_LONG[1] < ttl, (
        f"温档上界 {sk.IDLE_LONG[1]}s 没低于配置里的 TTL {ttl}s：两档会测同一件事")
    lo, hi, kind = sk._idle_window(sk.LONG_IDLE_EVERY, ttl)
    assert kind == "away_cold"
    assert lo > ttl + sk.SERVER_SWEEP_PERIOD_S, (
        f"冷档下界 {lo}s 没越过实际寿命上界 {ttl + sk.SERVER_SWEEP_PERIOD_S}s"
        f"（TTL+扫描周期）：缓存可能还活着，冷档只是名字冷")


def test_the_cold_margin_is_tied_to_the_server_sweep_period():
    """冷档下界与泄漏判据的宽限窗必须同源：都是服务端 `_sweep_loop` 的那个 300s。
    把扫描周期调大而余量没跟着走，冷档就不再保证越过实际寿命（首审 #8）。"""
    assert sk.IDLE_LONG_COLD_MARGIN[0] > sk.SERVER_SWEEP_PERIOD_S


def test_a_zero_long_idle_every_never_divides_by_it():
    """`LONG_IDLE_EVERY <= 0` 不能把窗口选择炸掉（首审 #15）。"""
    saved = sk.LONG_IDLE_EVERY
    try:
        for bad in (0, -1):
            sk.LONG_IDLE_EVERY = bad
            assert sk._idle_window(40, 2700.0)[2] == "short"
    finally:
        sk.LONG_IDLE_EVERY = saved


# ---------- 冷重建：造出来的样本必须有去处，且零样本必须被看见 ----------

def test_a_rebuilt_turn_lands_in_its_own_drift_bucket():
    """★ 冷重建轮既被「turn2+ 热」剔除、又不满足「turn1 冷」的 turn==1——不单开一格，
    「离开」冷档等出来的唯一测量物造出来就被丢掉（首审 #2）。"""
    rows = [_req(t, "chat", turn=2, cache_rotated=True, ttft=2.5, tps=40.0, wall=9.0)
            for t in range(1, 31)]
    m, _ = sk.measure(rows, [], 100.0)
    assert m["drift_三段"]["chat_冷重建"]["ttft"]["三段"][0] == 2.5
    assert m["drift_三段"]["chat_turn2+_热"]["ttft"]["三段"] == [None, None, None]


def test_the_cold_sample_count_reaches_the_summary():
    """★ 上一轮 `cache_rotated` 全程 0 次，而 summary 里没有任何字段提到它——一份零
    冷样本的产物与一份真测过的长得一模一样，只能靠事后 grep req.jsonl 发现（首审 #1）。"""
    m, _ = sk.measure([_req(10, "chat", turn=2, cache_rotated=True),
                       _req(20, "chat", turn=2)], [], 100.0)
    assert m["冷重建样本"] == 1
    m2, _ = sk.measure([_req(10, "chat", turn=2)], [], 100.0)
    assert m2["冷重建样本"] == 0


def test_duty_cycle_survives_a_corrupt_wall_field():
    """`float(r["wall"])` 会在手改过/半截写入的行上抛，而 measure() 在 finish() 里是
    无保护调用——一份 8 小时的 summary 会因为一行坏数据整个写不出来（首审 #6）。"""
    m, _ = sk.measure([_req(10, "chat", wall="1.2s"), _req(20, "chat", wall={"x": 1}),
                       _req(30, "chat", wall=10.0)], [], 100.0)
    assert m["生成占空比"] == 0.1


def test_duty_cycle_refuses_a_nonsense_window():
    """负窗口不该产出一个负占空比落进产物（首审 #7）。"""
    assert sk._duty_cycle([{"wall": 5}], -100.0) is None
    assert sk._duty_cycle([{"wall": 5}], 0) is None


def test_a_long_nap_never_overshoots_the_deadline():
    """冷档把最坏超时从 45min 抬到 TTL+25min，而「实跑不足目标」那条阻断只看下限、
    看不见超出——一个 8h 的跑会在 8.6h 才收（首审 #4）。"""
    assert sk._clamp_nap(3900.0, deadline=1000.0, now=500.0) == 500.0
    assert sk._clamp_nap(60.0, deadline=1000.0, now=500.0) == 60.0
    assert sk._clamp_nap(3900.0, deadline=1000.0, now=1200.0) == 0.0


def test_zero_cold_samples_blocks_the_verdict_on_a_full_run():
    """★ 检测器那一半：冷档哪天又失效，产物必须自己喊出来。上一轮就是没人喊（首审 #1）。"""
    assert sk._cold_sample_blocker(0, elapsed=8 * 3600, hours_target=8.0, smoke=False)
    assert sk._cold_sample_blocker(1, elapsed=8 * 3600, hours_target=8.0, smoke=False) is None


def test_the_cold_sample_blocker_spares_smoke_and_short_runs():
    """冒烟档不设「离开」窗、被中止的跑也没跑到——对它们判就是恒不可过的判据，
    而恒不可过的判据会训练操作者无视告警（与本文件既有的那条同一道理）。"""
    assert sk._cold_sample_blocker(0, elapsed=8 * 3600, hours_target=8.0, smoke=True) is None
    assert sk._cold_sample_blocker(0, elapsed=600, hours_target=8.0, smoke=False) is None


def test_a_server_running_a_different_ttl_refuses_to_start():
    """服务起于改 config 之前 → 运行态 TTL 与工装的假设分家，冷档窗宽就是按错的数算的
    （首审 #9）。老服务没有这个键：如实记「未核验」，不默认相符。"""
    assert sk._ttl_verdict(2700.0, 2700.0)[0] is True
    ok, note = sk._ttl_verdict(1800.0, 2700.0)
    assert ok is False and "不符" in note
    ok, note = sk._ttl_verdict(None, 2700.0)
    assert ok is True and "未核验" in note
    assert sk._ttl_verdict(True, 2700.0)[0] is True      # bool 不是 TTL
