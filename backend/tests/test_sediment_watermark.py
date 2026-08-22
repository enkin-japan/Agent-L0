"""零 LLM 沉淀与第二水位线 `sedimented_turns` 的金测试（波 F5）。

**病根**（本文件第一条测试就是它的取证）：折叠曾是沉淀的唯一入口，而折叠要两个条件
同时成立——上下文越过 token 阈值、且日志行数 > `keep_tail_turns`（默认 4）。桌面
agent 的主导形态恰是 ≤4 轮的短会话：两条都不满足 → 永不折叠 → 既不沉淀也不冲洗 →
30 天后整目录删除，**长期记忆产出恒为零**。

钉住的判据：
1. 短会话（够不着折叠）照样能沉淀；
2. 水位线**只在沉淀成功后**推进——失败就重试，绝不静默丢一段（那正是要修的病）；
3. 同一段原文不入库两次（折叠路径与空闲路径共用同一条水位线）；
4. 水位线越过日志末尾（分支只带前缀）必须向下修复，否则新轮次永远沉不下去；
5. ★ 回收前的最后一次沉淀**只挂 TTL 过期**，绝不挂用户手动删除——那是「忘掉它」。
"""
import asyncio
import json
import time

import pytest

import framework.memory.rag_sink as sink
from framework.memory.compactor import (
    _SEDIMENT_MIN_CHARS,
    last_chance_sediment,
    maybe_summarize_context,
    sediment_pending_turns,
)
from framework.memory.session import SessionManager


class _Recorder:
    """替身 rag_sink.sediment：记录每次入库的 (text, title)，可切换成失败。"""

    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, text, title):
        self.calls.append((text, title))
        return self.ok


@pytest.fixture
def rag(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(sink, "sediment", rec)
    return rec


def _write_turns(session, n, *, start=0, status="success", filler="内容" * 120):
    """写 n 条落盘轮次。默认**单条**正文就够长，避开 _SEDIMENT_MIN_CHARS 门槛
    （门槛作用于待沉淀区整体，增量只有一条时也必须自己过线，否则测的是门槛不是增量）。"""
    for i in range(start, start + n):
        session.append_log({
            "prompt": f"问题{i}",
            "response": [{"role": "assistant", "content": f"回答{i}" + filler}],
            "status": status, "timestamp": 1752800000 + i})


class _SummEngine:
    async def summarize(self, batch_text, existing, cancel_check=None):
        return "- 折叠摘要"


# ---- ★ 病根取证：短会话够不着折叠，但必须能沉淀 ----

def test_short_session_never_folds_but_still_sediments(make_session, rag):
    """★ F5 的取证：2 轮会话（< keep_tail_turns=4）折叠恒为 0，此前因此长期记忆产出为零。"""
    session = make_session("f5-short")
    _write_turns(session, 2)

    folded = asyncio.run(maybe_summarize_context(
        session, _SummEngine(), threshold_tokens=1, keep_tail_turns=4,
        last_prompt_tokens=10_000))
    assert folded == 0                      # 折叠这条路对短会话永远走不通
    assert rag.calls == []                  # …所以沉淀也从未发生（旧行为）
    assert session.summarized_turns == 0

    n = asyncio.run(sediment_pending_turns(session))
    assert n == 2
    assert len(rag.calls) == 1
    assert "回答0" in rag.calls[0][0] and "回答1" in rag.calls[0][0]
    assert session.sedimented_turns == 2
    assert session.summarized_turns == 0    # 沉淀绝不冒充折叠：摘要水位线不动


def test_watermark_survives_reload_without_any_summary(make_session, rag):
    """★ 只有第二水位线、没有摘要时 memory.json 也必须留下来。

    `save_memory` 的清除条件若漏掉 sedimented_turns，每次落盘都会把水位线抹回 0，
    同一段原文会被反复沉淀——这条测试就是那个漏项的取证。
    """
    session = make_session("f5-persist")
    _write_turns(session, 2)
    asyncio.run(sediment_pending_turns(session))
    assert session.rolling_summary == "" and session.summarized_turns == 0
    assert session.memory_file.exists()

    session.sedimented_turns = 0            # 模拟冷恢复
    session.load_memory()
    assert session.sedimented_turns == 2


def test_legacy_archive_falls_back_to_the_summary_watermark(make_session, rag):
    """老存档没有 sedimented_turns 键。缺省取 0 会让所有既有会话把历史原文重沉一遍；
    取摘要水位线兜底才有据可依——那部分原文在折叠时确已入库。"""
    session = make_session("f5-legacy")
    session.memory_file.write_text(
        json.dumps({"summary": "旧摘要", "summarized_turns": 4}), encoding="utf-8")
    session.load_memory()
    assert session.sedimented_turns == 4


def test_second_pass_sediments_nothing(make_session, rag):
    """幂等：没有新轮次就不该再入库一份（agent_memory 是只增史料，重复即噪声）。"""
    session = make_session("f5-idem")
    _write_turns(session, 3)
    assert asyncio.run(sediment_pending_turns(session)) == 3
    assert asyncio.run(sediment_pending_turns(session)) == 0
    assert len(rag.calls) == 1


def test_only_new_turns_are_sedimented(make_session, rag):
    session = make_session("f5-incr")
    _write_turns(session, 2)
    asyncio.run(sediment_pending_turns(session))
    _write_turns(session, 1, start=2)
    assert asyncio.run(sediment_pending_turns(session)) == 1
    assert "回答2" in rag.calls[1][0]
    assert "回答0" not in rag.calls[1][0]     # 已入库的不重发
    assert "turns 2-3" in rag.calls[1][1]


# ---- ★ 失败不推进：宁可重复，不可静默丢 ----

def test_failed_sediment_does_not_advance_the_watermark(make_session, monkeypatch):
    """★ RAG 掉线时水位线不动，下一回合原样重试。

    反面（失败也推进）等于把「长期记忆产出静默为零」这个病换个地方复发——只不过
    从「短会话永不沉淀」变成「RAG 掉线那一小时的会话永久蒸发」。
    """
    rec = _Recorder(ok=False)
    monkeypatch.setattr(sink, "sediment", rec)
    session = make_session("f5-retry")
    _write_turns(session, 2)

    assert asyncio.run(sediment_pending_turns(session)) == 0
    assert session.sedimented_turns == 0
    assert len(rec.calls) == 1

    rec.ok = True                            # RAG 恢复
    assert asyncio.run(sediment_pending_turns(session)) == 2
    assert session.sedimented_turns == 2


def test_disabled_makes_no_call(make_session, rag):
    session = make_session("f5-off")
    _write_turns(session, 2)
    assert asyncio.run(sediment_pending_turns(session, enabled=False)) == 0
    assert rag.calls == []
    assert session.sedimented_turns == 0
    # 阳性对照：同一条会话、同一个替身，开关打开就必须入库。没有它，替身哪天没接上
    # 这条测试照样绿（首审 #14——本仓的复发病，「判据写得不可能失败」）。
    assert asyncio.run(sediment_pending_turns(session, enabled=True)) == 2
    assert len(rag.calls) == 1


# ---- 门槛与作废轮 ----

def test_tiny_session_is_not_worth_a_document(make_session, rag):
    """「你好」级会话不占一条长期记忆；水位线**不推进**，等它长大再一起沉淀。"""
    session = make_session("f5-tiny")
    session.append_log({"prompt": "你好", "response": [{"role": "assistant", "content": "你好"}],
                        "status": "success", "timestamp": 1752800000})
    assert asyncio.run(sediment_pending_turns(session)) == 0
    assert rag.calls == []
    assert session.sedimented_turns == 0

    _write_turns(session, 1, start=1)         # 会话长大了
    assert asyncio.run(sediment_pending_turns(session)) == 2
    assert "你好" in rag.calls[0][0]           # 起点仍是 0：早先那轮没被跳过


def _chars_of_pending(session):
    """按 _sediment_range 的口径算「待沉淀文本」的长度，用来精确落在门槛两侧。"""
    from framework.memory.compactor import _collect_turn_texts, _read_log_lines
    lines = _read_log_lines(session.history_log)
    return len("\n\n---\n\n".join(_collect_turn_texts(lines[session.sedimented_turns:])))


@pytest.mark.parametrize("delta, expect", [(-1, 0), (0, 1)])
def test_min_chars_is_a_real_boundary(make_session, rag, delta, expect):
    """★ 精确卡在 min_chars 两侧。原测试用 ~237 字符打 200 的门槛，把 `<` 改成 `<=`
    照样绿——那是「判据写得不可能失败」的老毛病（首审 #13）。"""
    session = make_session(f"f5-bound{delta}")
    session.append_log({"prompt": "x", "response": [{"role": "assistant", "content": "y"}],
                        "status": "success", "timestamp": 1752800000})
    pad = _SEDIMENT_MIN_CHARS + delta - _chars_of_pending(session)
    assert pad >= 0
    session.history_log.write_text("", encoding="utf-8")
    session.append_log({"prompt": "x", "response": [{"role": "assistant", "content": "y" + "z" * pad}],
                        "status": "success", "timestamp": 1752800000})
    assert _chars_of_pending(session) == _SEDIMENT_MIN_CHARS + delta
    assert asyncio.run(sediment_pending_turns(session)) == expect


def test_the_gate_only_guards_a_wholly_trivial_session(make_session, rag):
    """★ 门槛只挡「整条会话都琐碎」。实质会话的小尾巴（「谢谢」）若也被卡住，那一轮
    永远沉不下去，且每个空闲回合都要为一次注定的空转重读整份日志（首审 #7）。"""
    session = make_session("f5-tail")
    _write_turns(session, 2)
    assert asyncio.run(sediment_pending_turns(session)) == 2
    session.append_log({"prompt": "谢谢", "response": [{"role": "assistant", "content": "不客气"}],
                        "status": "success", "timestamp": 1752800002})
    assert asyncio.run(sediment_pending_turns(session)) == 1     # 短尾巴照样带走
    assert "谢谢" in rag.calls[1][0]


def test_a_backlog_is_split_into_bounded_passes(make_session, monkeypatch, rag):
    """★ 积压很久时不许攒成一个巨型 POST：那种请求在 10s 超时下大概率永远失败，
    水位线永不推进，于是每 120s 重发一次整个积压（首审 #5）。分片让进度单调向前。"""
    monkeypatch.setattr("framework.memory.compactor._SEDIMENT_MAX_CHARS_PER_PASS", 1200)
    session = make_session("f5-backlog")
    _write_turns(session, 6)
    first = asyncio.run(sediment_pending_turns(session))
    assert 0 < first < 6                       # 一次没吃完
    assert asyncio.run(sediment_pending_turns(session)) == 6 - first
    assert session.sedimented_turns == 6
    assert len(rag.calls) == 2


def test_an_oversized_single_turn_still_makes_progress(make_session, monkeypatch, rag):
    """预算再小也至少走一行——否则超大单轮把整条会话的进度永久卡死。"""
    monkeypatch.setattr("framework.memory.compactor._SEDIMENT_MAX_CHARS_PER_PASS", 10)
    session = make_session("f5-huge")
    _write_turns(session, 2)
    assert asyncio.run(sediment_pending_turns(session)) == 1


def test_an_unreadable_log_never_moves_the_watermark(make_session, monkeypatch, rag):
    """★ 「读不动」与「日志是空的」严格区分：一次读盘失败若被当成 0 行，水位线会被
    夹到 0，整条会话的原文随后重沉一遍。

    B1 起坏行/坏字节按行级跳过（不再把整份日志判成读不动——原语义下一条半截写入
    就让沉淀永久停摆），「读不动」只剩真 I/O 失败一种含义，故此处用 OSError 模拟。"""
    session = make_session("f5-badlog")
    _write_turns(session, 3)
    asyncio.run(sediment_pending_turns(session))
    assert len(rag.calls) == 1                 # 阳性对照：替身接得上、正常路真的走通

    def _boom(path, **kw):
        raise OSError("disk unplugged")
        yield  # pragma: no cover  （保持生成器形态，与被替对象同构）
    monkeypatch.setattr("framework.memory.compactor.iter_records", _boom)
    assert asyncio.run(sediment_pending_turns(session)) == 0
    assert session.sedimented_turns == 3       # 没被夹回 0
    assert len(rag.calls) == 1


def test_void_turns_only_advance_without_a_document(make_session, rag):
    """整段都是作废轮（fail/cancelled）：没有可存的原文，但水位线要推进——
    否则每个空闲回合都重扫同一段。"""
    session = make_session("f5-void")
    _write_turns(session, 2, status="fail")
    assert asyncio.run(sediment_pending_turns(session)) == 2
    assert rag.calls == []
    assert session.sedimented_turns == 2


# ---- ★ 水位线越界修复（分支只带前缀 / 日志被重写） ----

def test_watermark_beyond_the_log_is_repaired_downward(make_session, rag):
    """★ 不修复的话，后续新轮次一直落在水位线之下，这条会话从此再不沉淀（静默失效）。"""
    session = make_session("f5-repair")
    _write_turns(session, 2)
    session.sedimented_turns = 9            # 分支只带了前缀，却继承了父会话的水位线
    assert asyncio.run(sediment_pending_turns(session)) == 0
    assert session.sedimented_turns == 2    # 已夹回日志末尾
    assert rag.calls == []                  # 前 2 轮是父会话沉过的，不重发

    _write_turns(session, 1, start=2)
    assert asyncio.run(sediment_pending_turns(session)) == 1
    assert "回答2" in rag.calls[0][0]


# ---- 折叠路径与空闲路径共用同一条水位线 ----

def test_fold_advances_the_shared_watermark(make_session, rag):
    session = make_session("f5-fold")
    _write_turns(session, 6)
    n = asyncio.run(maybe_summarize_context(
        session, _SummEngine(), threshold_tokens=1, keep_tail_turns=2,
        last_prompt_tokens=10_000))
    assert n == 4
    assert session.sedimented_turns == 4     # 折叠顺带把沉淀水位线推到 fold_until
    assert len(rag.calls) == 1
    assert asyncio.run(sediment_pending_turns(session)) == 2   # 只剩尾部两轮
    assert "回答0" not in rag.calls[1][0]


def test_fold_does_not_resediment_what_idle_already_stored(make_session, rag):
    """★ 空闲回合先沉淀过的原文，折叠时不许再发一遍。"""
    session = make_session("f5-noredup")
    _write_turns(session, 6)
    assert asyncio.run(sediment_pending_turns(session)) == 6
    assert len(rag.calls) == 1

    n = asyncio.run(maybe_summarize_context(
        session, _SummEngine(), threshold_tokens=1, keep_tail_turns=2,
        last_prompt_tokens=10_000))
    assert n == 4                            # 照样折叠（摘要与沉淀互不从属）
    assert len(rag.calls) == 1               # 但一次都没重发
    assert session.summarized_turns == 4 and session.sedimented_turns == 6


def test_fold_sediments_only_the_gap(make_session, rag):
    """空闲沉淀跑在前面但没跑完时，折叠只补上缺口那一段。"""
    session = make_session("f5-gap")
    _write_turns(session, 2)
    asyncio.run(sediment_pending_turns(session))          # 沉到 2
    _write_turns(session, 4, start=2)
    asyncio.run(maybe_summarize_context(
        session, _SummEngine(), threshold_tokens=1, keep_tail_turns=2,
        last_prompt_tokens=10_000))                       # fold_until = 4
    assert len(rag.calls) == 2
    assert "turns 2-4" in rag.calls[1][1]
    assert "回答1" not in rag.calls[1][0]


def test_fold_with_sediment_disabled_leaves_the_watermark_alone(make_session, rag):
    session = make_session("f5-folddis")
    _write_turns(session, 6)
    asyncio.run(maybe_summarize_context(
        session, _SummEngine(), threshold_tokens=1, keep_tail_turns=2,
        last_prompt_tokens=10_000, sediment=False))
    assert rag.calls == []
    assert session.sedimented_turns == 0


# ---- 回收前的最后一次沉淀 ----

def test_last_chance_sediments_the_pending_tail(make_session, rag):
    session = make_session("f5-last")
    _write_turns(session, 2)
    assert last_chance_sediment(session.session_dir, session.session_id) is True
    assert "回答0" in rag.calls[0][0]


def test_last_chance_respects_an_existing_watermark(make_session, rag):
    session = make_session("f5-last2")
    _write_turns(session, 2)
    asyncio.run(sediment_pending_turns(session))
    rag.calls.clear()
    assert last_chance_sediment(session.session_dir, session.session_id) is None
    assert rag.calls == []


def test_last_chance_uses_the_same_legacy_fallback(make_session, rag):
    """★ 读 sedimented_turns 的三处（load_memory / 冷会话快照 / 回收前沉淀）必须同一口径。
    这里回落 0，每条老会话过期时就会把已折叠部分整段重沉一遍。"""
    session = make_session("f5-last-legacy")
    _write_turns(session, 3)
    session.memory_file.write_text(
        json.dumps({"summary": "旧摘要", "summarized_turns": 2}), encoding="utf-8")
    assert last_chance_sediment(session.session_dir, session.session_id) is True
    assert "回答2" in rag.calls[0][0]
    assert "回答0" not in rag.calls[0][0]     # 已折叠沉淀过的那两轮不重发
    assert "turns 2-3" in rag.calls[0][1]


def test_last_chance_on_a_trivial_session_is_a_noop(make_session, rag):
    session = make_session("f5-last3")
    session.append_log({"prompt": "在吗", "response": [{"role": "assistant", "content": "在"}],
                        "status": "success", "timestamp": 1752800000})
    assert last_chance_sediment(session.session_dir, session.session_id) is None
    assert rag.calls == []


def test_ttl_expiry_sediments_before_deleting(make_session, rag):
    session = make_session("f5-ttl")
    _write_turns(session, 2)
    mgr = SessionManager(session.session_dir.parent)
    reaped = asyncio.run(mgr.prune_expired_sessions(
        retention_days=0, empty_ttl_hours=0, sediment_on_expire=True))
    assert reaped == ["f5-ttl"]
    assert len(rag.calls) == 1
    assert not session.session_dir.exists()


def test_ttl_expiry_without_the_flag_sediments_nothing(tmp_path, make_session, rag):
    """开关关掉这条路就整个不走。阳性对照放在同一条测试里：另一条同样过期的会话在
    开关打开时必须入库——否则替身没接上时本条照样绿（首审 #14）。"""
    off = make_session("f5-ttl-off")
    _write_turns(off, 2)
    mgr = SessionManager(off.session_dir.parent)
    assert asyncio.run(mgr.prune_expired_sessions(0, 0)) == ["f5-ttl-off"]
    assert rag.calls == []

    on = make_session("f5-ttl-on")
    _write_turns(on, 2)
    assert asyncio.run(mgr.prune_expired_sessions(0, 0, sediment_on_expire=True)) == ["f5-ttl-on"]
    assert len(rag.calls) == 1


def test_a_corrupt_memory_json_falls_back_to_zero(make_session, rag):
    """坏掉的 memory.json 不该让这条会话的最后一次沉淀整个失败——读不出水位线就从 0 起。"""
    session = make_session("f5-badmem")
    _write_turns(session, 2)
    session.memory_file.write_bytes(b"\xff\xfe{not json")
    assert last_chance_sediment(session.session_dir, session.session_id) is True
    assert len(rag.calls) == 1


def test_a_session_that_blows_up_does_not_disable_the_rest(make_session, monkeypatch):
    """★ 止损只针对「RAG 不可达」这一种全局故障。单条会话自己炸掉（任何未预期异常）
    不该把整轮沉淀关掉——那是把局部故障放大成全局失效（首审 #6）。"""
    rec = _Recorder()

    def _boom(text, title):
        if "boom" in title:
            raise RuntimeError("这条会话自己炸了")
        return rec(text, title)

    monkeypatch.setattr(sink, "sediment", _boom)
    for sid in ("f5-boom", "f5-fine"):
        _write_turns(make_session(sid), 2)
    mgr = SessionManager((make_session("f5-fine").session_dir.parent))
    reaped = asyncio.run(mgr.prune_expired_sessions(0, 0, sediment_on_expire=True))
    assert sorted(reaped) == ["f5-boom", "f5-fine"]
    assert len(rec.calls) == 1      # 炸掉的那条跳过，好的那条照沉——止损没被误触发


def test_one_rag_failure_stops_sedimenting_for_the_rest_of_the_pass(
        tmp_path, make_session, monkeypatch):
    """★ RAG 不可达时不许让磁盘回收被 N×10s 超时拖住：一次失败即本轮停手，
    但**回收照做**——「回收不动」比「少沉淀一条」严重得多。"""
    rec = _Recorder(ok=False)
    monkeypatch.setattr(sink, "sediment", rec)
    for sid in ("f5-a", "f5-b", "f5-c"):
        _write_turns(make_session(sid), 2)
    mgr = SessionManager((tmp_path / "sessions"))
    reaped = asyncio.run(mgr.prune_expired_sessions(0, 0, sediment_on_expire=True))
    assert sorted(reaped) == ["f5-a", "f5-b", "f5-c"]   # 三个全回收
    assert len(rec.calls) == 1                          # 只试了一次


def test_manual_delete_never_sediments(make_session, rag):
    """★ 用户手动删除会话是「忘掉它」的明确信号——那条路上沉淀等于把用户刚删掉的
    东西搬进长期记忆。最后一次沉淀刻意只挂 TTL 过期。"""
    session = make_session("f5-manual")
    _write_turns(session, 2)
    mgr = SessionManager(session.session_dir.parent)
    res = asyncio.run(mgr.delete_session("f5-manual"))
    assert res["status"] is True
    assert rag.calls == []
    # 阳性对照：内容一样的另一条会话走 TTL 过期就必须入库——证明替身接得上、
    # 差别只在「哪条路删的」（首审 #14）
    other = make_session("f5-manual-ttl")
    _write_turns(other, 2)
    assert asyncio.run(mgr.prune_expired_sessions(0, 0, sediment_on_expire=True)) == ["f5-manual-ttl"]
    assert len(rag.calls) == 1


# ---- ★ 接线：空闲维护回合真的会调它 ----

def test_idle_maintenance_actually_sediments(make_session, rag, monkeypatch):
    """★ 一个没人调用的函数与「永不沉淀」是同一种病，只是换了个地方。

    本条驱动 `_idle_maintenance_loop` 真跑一圈，钉的是**接线**：短会话
    （last_prompt_tokens=0，够不着低阈值那一遍）照样被这一遍捡起来沉淀。
    """
    import app

    async def _noop(*a, **k):
        return 0

    monkeypatch.setattr(app.notes_dream, "maybe_dream", _noop)
    monkeypatch.setattr(app.drift_patrol, "maybe_patrol", _noop)

    session = make_session("f5-wire")
    _write_turns(session, 2)
    session.last_access_time = 0          # 早就空闲了
    session.last_prompt_tokens = 0        # 低阈值那一遍不会碰它

    class _Sched:
        def is_active(self, sid):
            return False

        async def acquire(self, *a, **k):
            pass

        async def release(self, *a, **k):
            pass

    class _Orch:
        summary_idle_seconds = 300
        summary_low_threshold = 2000
        summary_keep_tail_turns = 4
        sediment_to_rag = True
        engine = _SummEngine()
        notes_dream_cfg = {}
        drift_patrol_cfg = {}

        async def prewarm_base_cache(self):
            return False

        async def prewarm_session_cache(self, s):
            return False

    class _Mgr:
        _all_sessions = {"f5-wire": session}

    async def _drive():
        task = asyncio.create_task(
            app._idle_maintenance_loop(_Mgr(), _Sched(), _Orch(), interval=0))
        for _ in range(60):
            await asyncio.sleep(0.05)
            if rag.calls:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())
    assert len(rag.calls) == 1
    assert session.sedimented_turns == 2


def _drive_idle_loop(session, monkeypatch, *, until, lock=None):
    """驱动 `_idle_maintenance_loop` 真跑，直到 until() 为真或超时。"""
    import app

    async def _noop(*a, **k):
        return 0

    monkeypatch.setattr(app.notes_dream, "maybe_dream", _noop)
    monkeypatch.setattr(app.drift_patrol, "maybe_patrol", _noop)
    if lock is not None:
        session.lock = lock

    class _Sched:
        def is_active(self, sid):
            return False

        async def acquire(self, *a, **k):
            pass

        async def release(self, *a, **k):
            pass

    class _Orch:
        summary_idle_seconds = 300
        summary_low_threshold = 2000
        summary_keep_tail_turns = 4
        sediment_to_rag = True
        engine = _SummEngine()
        notes_dream_cfg = {}
        drift_patrol_cfg = {}

        async def prewarm_base_cache(self):
            return False

        async def prewarm_session_cache(self, s):
            return False

    class _Mgr:
        _all_sessions = {session.session_id: session}

    async def _drive():
        task = asyncio.create_task(
            app._idle_maintenance_loop(_Mgr(), _Sched(), _Orch(), interval=0))
        for _ in range(40):
            await asyncio.sleep(0.02)
            if until():
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())


def test_idle_maintenance_repairs_an_overshooting_watermark(make_session, rag, monkeypatch):
    """★ 越界水位线的修复必须在**生产路径上够得着**。

    空闲那一遍原先用 `>=` 跳过「无事可做」的会话，而水位线**高于**行数正是越界态——
    它会被那个 `>=` 永久挡在门外，修复分支在生产里永远跑不到，守它的金测试也就成了
    「守一条产品进不去的路」（首审 #2）。本条从循环入口驱动，不给判据留后门。
    """
    session = make_session("f5-wire-repair")
    _write_turns(session, 2)
    session.last_access_time = 0
    session.last_prompt_tokens = 0
    session.sedimented_turns = 9              # 分支/删末轮留下的越界态

    _drive_idle_loop(session, monkeypatch, until=lambda: session.sedimented_turns == 2)
    assert session.sedimented_turns == 2
    assert rag.calls == []                    # 修复本身不发文档

    _write_turns(session, 1, start=5)         # 修好之后新轮次真能沉下去
    _drive_idle_loop(session, monkeypatch, until=lambda: bool(rag.calls))
    assert len(rag.calls) == 1
    assert "回答5" in rag.calls[0][0]


def test_a_user_turn_arriving_while_we_wait_for_the_lock_aborts_the_pass(
        make_session, rag, monkeypatch):
    """★ 等锁期间用户发了新消息 → 这一遍必须放弃。

    锁外那次 `lock.locked()` 是 await **之前**看的；持锁期间有一次网络写（最坏 ~15s），
    不在锁内复检空闲就等于在用户打字时把会话锁住（首审 #4）。这里用一个在 `__aenter__`
    里刷新 last_access_time 的锁替身，把那个竞态钉成确定性的。
    """
    session = make_session("f5-wire-race")
    _write_turns(session, 2)
    session.last_access_time = 0
    session.last_prompt_tokens = 0

    real = session.lock

    class _RacingLock:
        async def __aenter__(self):
            await real.acquire()
            session.last_access_time = time.time()   # 等锁期间用户来了
            return self

        async def __aexit__(self, *exc):
            real.release()

        def locked(self):
            return real.locked()

    _drive_idle_loop(session, monkeypatch, until=lambda: bool(rag.calls),
                     lock=_RacingLock())
    assert rag.calls == []
    assert session.sedimented_turns == 0


# ---- 分支继承水位线 ----

def test_branch_clamps_on_the_copy_path_too(make_session, rag):
    """★ 短会话**从不折叠**（summarized_turns 恒为 0），所以走的永远是「摘要旁挂原样
    复制」那条路。只在另一条路上夹水位线，等于给主流形态留了个静默跳段：分支自己的
    新轮次会全部落在越界水位线之下，永不沉淀（首审 #1）。"""
    parent = make_session("f5-copy-parent")
    _write_turns(parent, 8)
    asyncio.run(sediment_pending_turns(parent))
    assert parent.summarized_turns == 0 and parent.sedimented_turns == 8

    mgr = SessionManager(parent.session_dir.parent)
    kept = [json.dumps({"prompt": f"q{i}", "status": "success"}) for i in range(3)]
    mgr._materialize_branch("f5-copy-parent", "f5-copy-child", kept, branch_point=2)
    child_mem = json.loads((mgr.sessions_dir / "f5-copy-child" / "memory.json").read_text())
    assert child_mem["sedimented_turns"] == 3

    # 而且分支后续的新轮次真的会被沉淀（跳段的真正后果在这里取证）
    child = make_session("f5-copy-child")
    child.load_memory()
    _write_turns(child, 2, start=90)
    rag.calls.clear()
    assert asyncio.run(sediment_pending_turns(child)) == 2
    assert "回答90" in rag.calls[0][0]


def test_deleting_the_last_turn_lowers_the_watermark(make_session, rag):
    """★ 日志缩短后水位线不夹回来，用户重发的**替代轮**正好落在旧水位线之下，
    从此再没人沉淀它——一条用户可见的消息静默缺席长期记忆，全程无报错（首审 #3）。"""
    session = make_session("f5-deltail")
    _write_turns(session, 3)
    assert asyncio.run(sediment_pending_turns(session)) == 3

    mgr = SessionManager(session.session_dir.parent)
    mgr._all_sessions["f5-deltail"] = session
    res = asyncio.run(mgr.delete_last_turn("f5-deltail"))
    assert res["status"] is True
    assert session.sedimented_turns == 2

    _write_turns(session, 1, start=77)      # 用户重发的替代轮
    rag.calls.clear()
    assert asyncio.run(sediment_pending_turns(session)) == 1
    assert "回答77" in rag.calls[0][0]


def test_branch_inherits_the_clamped_watermark(make_session, rag):
    """父会话折叠过头（摘要旁挂不可用）时，沉淀水位线仍要带给分支——否则分支会把
    整个前缀重沉一遍。带过去的值必须夹在分支窗口内。"""
    parent = make_session("f5-parent")
    _write_turns(parent, 6)
    parent.summarized_turns = 5
    parent.sedimented_turns = 6
    parent.rolling_summary = "父会话摘要"
    parent.save_memory()

    mgr = SessionManager(parent.session_dir.parent)
    kept = [json.dumps({"prompt": f"q{i}", "status": "success"}) for i in range(3)]
    mgr._materialize_branch("f5-parent", "f5-child", kept, branch_point=2)

    child_mem = json.loads((mgr.sessions_dir / "f5-child" / "memory.json").read_text())
    assert child_mem["sedimented_turns"] == 3      # 夹到分支窗口
    assert child_mem["summary"] == ""              # 摘要覆盖了分支点之后，不能带
