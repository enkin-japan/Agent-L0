"""B3 提交路径幂等键金测试。

守三件事:① `lookup_client_turn` 两级判定(活跃轮经账本状态机 / 已完结轮经 journal
尾窗回查);② routes 的判重发生在 `_prepare_turn`(multipart 摄入)**之前**——重复提交
不付摄入成本、不留孤儿媒体;③ 键随 turn_queued 落 journal(重启后仍判得住)。
"""
import asyncio
import json
from types import SimpleNamespace

from framework.core.journal import TurnJournal, new_turn_id
from framework.core.turn_runner import TurnRunner
from framework.memory.session import SessionManager


def _runner() -> TurnRunner:
    return TurnRunner(orchestrator=None, scheduler=None)


def _queued(journal, key, prompt="p"):
    tid = new_turn_id()
    env = journal.emit("turn_queued", {"prompt": prompt, "gen": {}, "origin": "chat",
                                       "client_turn_id": key}, turn_id=tid)
    return tid, env["seq"]


def test_lookup_hits_the_active_turn(make_session):
    """活跃轮(queued/running)带同键的重复提交 → 202 原轮,不是 409:
    同一次提交的重试应引导附着,不是冲突。"""
    session = make_session("idem-active")
    tid, seq = _queued(session.journal, "key-A")
    session.journal.emit("turn_started", {})
    hit = _runner().lookup_client_turn(session, "key-A")
    assert hit == {"turn_id": tid, "seq": seq, "duplicate": True}
    # 阳性对照:不同键不命中(否则任何并发提交都被吸进同一轮)
    assert _runner().lookup_client_turn(session, "key-B") is None


def test_lookup_hits_a_completed_turn_via_journal_tail(make_session):
    """已完结轮:唯一持久真相是 journal 的 turn_queued 里程碑(尾窗回查)。"""
    session = make_session("idem-done")
    tid, seq = _queued(session.journal, "key-C")
    session.journal.emit("turn_started", {})
    session.journal.emit("turn_ended", {"status": "success", "turn_index": 0})
    hit = _runner().lookup_client_turn(session, "key-C")
    assert hit == {"turn_id": tid, "seq": seq, "duplicate": True}


def test_lookup_survives_process_restart(make_session):
    """键在 journal 里,不在内存:重启(新账本实例)后照样判得住——这正是「响应回程
    丢失 + 客户端重试」跨越的那次生死。"""
    session = make_session("idem-restart")
    tid, seq = _queued(session.journal, "key-D")
    session.journal.emit("turn_ended", {"status": "success", "turn_index": 0})

    fresh = TurnJournal(session.session_dir)          # 进程重启后的新实例
    session2 = SimpleNamespace(journal=fresh)
    hit = _runner().lookup_client_turn(session2, "key-D")
    assert hit is not None and hit["turn_id"] == tid and hit["seq"] == seq


def test_window_counts_turns_not_milestones(make_session):
    """★ 首审 H2:窗口按 turn_queued **轮**计——一个工具密集轮上百条里程碑,
    按里程碑计会把自己的 turn_queued 挤出窗口,刚完结一秒的轮就判不了重。"""
    session = make_session("idem-toolheavy")
    tid, seq = _queued(session.journal, "key-heavy")
    for i in range(80):                               # 80 对工具里程碑 = 160 条
        session.journal.emit("tool_started", {"call_id": f"c{i}", "name": "t", "args": {}})
        session.journal.emit("tool_result", {"call_id": f"c{i}", "name": "t",
                                             "response": "ok", "status": "ok"})
    session.journal.emit("turn_ended", {"status": "success", "turn_index": 0})
    hit = _runner().lookup_client_turn(session, "key-heavy")
    assert hit == {"turn_id": tid, "seq": seq, "duplicate": True}


def test_lookup_misses_beyond_the_turn_window(make_session):
    """窗口有界(近 64 轮):越界旧键当新提交——幂等键防分钟级手动重发,
    不做无限期查重(如实界定)。"""
    session = make_session("idem-window")
    _queued(session.journal, "key-old")
    session.journal.emit("turn_ended", {"status": "success", "turn_index": 0})
    for i in range(70):                               # 70 轮把旧键挤出 64 轮窗口
        _queued(session.journal, f"key-{i}")
        session.journal.emit("turn_ended", {"status": "success", "turn_index": i + 1})
    assert _runner().lookup_client_turn(session, "key-old") is None
    # 阳性对照:窗口内的键命中
    assert _runner().lookup_client_turn(session, "key-69") is not None


def test_failed_turn_does_not_dedupe(make_session):
    """★ 首审 H4:fail/cancelled 的原轮不算命中——B2 的恢复文案明说「可直接重发」,
    吞掉这次重发等于把收敛承诺变谎话。阳性对照:同现场 success 轮照常命中。"""
    session = make_session("idem-fail")
    _queued(session.journal, "key-crashed")
    session.journal.emit("turn_ended", {"status": "fail", "turn_index": 0})
    assert _runner().lookup_client_turn(session, "key-crashed") is None

    _queued(session.journal, "key-cancelled")
    session.journal.emit("turn_ended", {"status": "cancelled", "turn_index": 1})
    assert _runner().lookup_client_turn(session, "key-cancelled") is None

    tid, seq = _queued(session.journal, "key-good")
    session.journal.emit("turn_ended", {"status": "success", "turn_index": 2})
    assert _runner().lookup_client_turn(session, "key-good") == {
        "turn_id": tid, "seq": seq, "duplicate": True}


def test_route_conflict_branch_dedupes_concurrent_same_key(tmp_path):
    """★ 首审 M7:真并发同键双提交都越过预检、后到者撞 turn_active——若活跃轮正是
    同键先到者,回 202 原轮而非 409(在受理冲突分支二次判重)。"""
    import pytest
    from framework.api import routes
    mgr = SessionManager(tmp_path / "sessions")
    session = mgr.get_or_create("idem-race")
    tid, seq = _queued(session.journal, "key-race-0000")   # 先到者已活跃(queued)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        turn_runner=_runner(), session_manager=mgr)))
    # 活跃轮同键在预检层就命中(状态机分支)→ 202,连摄入都不进
    resp = asyncio.run(routes.submit_turn(
        "idem-race", req, prompt="同一句", client_turn_id="key-race-0000"))
    assert resp.status_code == 202
    assert json.loads(resp.body)["turn_id"] == tid
    # 阳性对照:同会话**不同键**在活跃轮期间提交 → 走正常路(残缺 request 上炸摄入)
    with pytest.raises(AttributeError):
        asyncio.run(routes.submit_turn(
            "idem-race", req, prompt="别的话", client_turn_id="key-other-000"))


def test_route_sanitizes_malformed_keys(tmp_path):
    """首审 M9:畸形键按「未带键」受理(fail-open)。判据必须可分辨「被拒」与「查无」:
    账本里**预先埋好同样的畸形键并配 success 终态**——消毒在,走正常路(残缺 request
    上炸摄入);消毒不在,就会命中判重回 202。初版夹具没埋对应物,变异体存活揪出。"""
    import pytest
    from framework.api import routes
    mgr = SessionManager(tmp_path / "sessions")
    session = mgr.get_or_create("idem-badkey")
    malformed = ("x" * 300, "键中文键中文键中文", "a b c d e f g h")
    for i, bad in enumerate(malformed):
        _queued(session.journal, bad)                  # 账本里就躺着这个畸形键
        session.journal.emit("turn_ended", {"status": "success", "turn_index": i})
    for bad in malformed + ("   ",):
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            turn_runner=_runner(), session_manager=mgr)))
        with pytest.raises(AttributeError):
            asyncio.run(routes.submit_turn(
                "idem-badkey", req, prompt="p", client_turn_id=bad))
    # 阳性对照:同现场的合法键真的会命中(证明「不命中」是消毒的功劳,不是查无)
    tid, seq = _queued(session.journal, "key-legal-000")
    session.journal.emit("turn_ended", {"status": "success", "turn_index": 9})
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        turn_runner=_runner(), session_manager=mgr)))
    resp = asyncio.run(routes.submit_turn(
        "idem-badkey", req, prompt="p", client_turn_id="key-legal-000"))
    assert resp.status_code == 202


def test_lookup_without_key_or_records(make_session):
    session = make_session("idem-none")
    assert _runner().lookup_client_turn(session, "") is None
    assert _runner().lookup_client_turn(session, "no-such") is None


def test_route_dedupe_short_circuits_before_ingestion(tmp_path):
    """★ 判重点位:命中时 routes 在 `_prepare_turn` 之前直接 202——本测的 request 里
    **根本没有** orchestrator/ingestion,走到摄入就 AttributeError,能拿到 202 本身就是
    时序证明(重复提交不付摄入成本、不留孤儿媒体)。"""
    from framework.api import routes
    mgr = SessionManager(tmp_path / "sessions")
    session = mgr.get_or_create("idem-route")
    tid, seq = _queued(session.journal, "key-R0000000")
    session.journal.emit("turn_ended", {"status": "success", "turn_index": 0})

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        turn_runner=_runner(), session_manager=mgr)))   # 有意缺 orchestrator/ingestion
    resp = asyncio.run(routes.submit_turn(
        "idem-route", req, prompt="重发同一句", client_turn_id="key-R0000000"))
    assert resp.status_code == 202
    body = json.loads(resp.body)
    assert body["duplicate"] is True and body["turn_id"] == tid and body["seq"] == seq


def test_route_without_key_or_no_hit_takes_the_normal_path(tmp_path):
    """阳性对照:无键/未命中必须走正常受理——在本测的残缺 request 上表现为
    AttributeError(证明确实进入了 _prepare_turn,而不是被短路挡下)。"""
    import pytest
    from framework.api import routes
    mgr = SessionManager(tmp_path / "sessions")
    mgr.get_or_create("idem-route2")
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        turn_runner=_runner(), session_manager=mgr)))
    with pytest.raises(AttributeError):
        asyncio.run(routes.submit_turn("idem-route2", req, prompt="新话"))
    with pytest.raises(AttributeError):
        asyncio.run(routes.submit_turn("idem-route2", req, prompt="新话",
                                       client_turn_id="key-miss"))


def test_key_is_stripped_nowhere_and_persisted(make_session):
    """键必须活着落盘:turn_queued 的持久化副本带 client_turn_id(_persist 只瘦身
    turn_ended/tool_*,别哪天顺手把它剪了)。"""
    from framework.core.jsonl import iter_records
    session = make_session("idem-persist")
    _queued(session.journal, "key-P")
    recs = [r.obj for r in iter_records(session.journal.path)
            if r.obj.get("type") == "turn_queued"]
    assert recs and recs[0]["data"]["client_turn_id"] == "key-P"
