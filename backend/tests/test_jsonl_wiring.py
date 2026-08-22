"""B1 接线金测试:五处裸 append 与全部读者都走 core/jsonl 唯一口径。

模块本身的行为由 test_jsonl_discipline.py 守;本文件守的是**接线**——同一份被污染的
文件,所有读者必须给出同一个记录数与下标,所有写者追加前必须修尾。任何一处仍在
自己数行/裸 append,这里就会红。
"""
import asyncio
import json

import pytest

from framework.core import events
from framework.core.journal import TurnJournal, new_turn_id
from framework.core.jsonl import count_records, iter_records, reset_stats
from framework.memory.compactor import _read_log_lines
from framework.memory.session import SessionManager

from .jsonl_corpus import (
    blank_lines,
    encode,
    non_dict_line,
    truncated_multibyte,
    undecodable_line,
    write_corpus,
)


@pytest.fixture(autouse=True)
def _fresh_stats():
    reset_stats()
    yield
    reset_stats()


def _turn(i: int, status: str = "success") -> dict:
    """turn 形状的记录(与 history.jsonl 落盘轮次同构,重放/标题/沉淀都认得)。"""
    return {"prompt": f"第{i}问:中日混在プロンプト", "status": status,
            "response": [{"role": "assistant", "content": f"第{i}答:回答テキスト"}],
            "image": [], "audio": [], "ingested_text": "", "timestamp": 1752800000 + i}


def _corrupt_history(path):
    """5 条有效轮次,坏行/空行/断尾穿插其间——所有读者应一致地看见 5 条。"""
    parts = [
        encode(_turn(0)) + b"\n",
        blank_lines(),
        encode(_turn(1)) + b"\n",
        non_dict_line(),
        encode(_turn(2)) + b"\n",
        undecodable_line(),
        encode(_turn(3)) + b"\n",
        encode(_turn(4)) + b"\n",
        truncated_multibyte(),
    ]
    return write_corpus(path, parts)


# ---- 读者同口径:同一份被污染的文件,一个记录数,一个下标定义 ----

def test_all_readers_agree_on_a_corrupt_history(tmp_path):
    sdir = tmp_path / "sessions" / "b1-readers"
    sdir.mkdir(parents=True)
    log = _corrupt_history(sdir / "history.jsonl")
    mgr = SessionManager(tmp_path / "sessions")

    session = mgr.get_or_create("b1-readers")          # 内部走 _replay_history_log
    n_disk = session.disk_turn_count()
    n_hist = len(mgr.get_history("b1-readers"))
    n_comp = len(_read_log_lines(log))
    n_iter = count_records(log)
    row = mgr._scan_session_dir(sdir)

    assert n_disk == n_hist == n_comp == n_iter == 5
    assert row["turn_count"] == 5
    # 下标口径:get_history 的第 i 条 = 有效记录第 i 条(坏行不占号)
    assert [t["prompt"] for t in mgr.get_history("b1-readers")] \
        == [_turn(i)["prompt"] for i in range(5)]
    # 内存重放同口径:5 轮 = 5 对 user/assistant(system 头之外)
    roles = [m["role"] for m in session.history if m["role"] != "system"]
    assert roles == ["user", "assistant"] * 5
    # 标题取的是第一条**有效**记录的 prompt
    assert row["title"].startswith("第0问")


def test_replay_survives_a_poisoned_middle_turn(make_session):
    """一条坏轮次只丢它自己:原先整个 for 共用一个 try,其后所有轮次静默消失。"""
    session = make_session("b1-replay")
    session.append_log(_turn(0))
    session.append_log(_turn(1))
    session.append_log(_turn(2))
    # 把中间一轮污染成「合法 JSON 但重建时会炸」的形状:response 不是 list 而是 dict,
    # history.extend(dict) 会逐键展开成字符串——不炸;改用 image 非列表让 extend 炸
    lines = [json.dumps(_turn(i), ensure_ascii=False) for i in range(3)]
    bad = _turn(1); bad["image"] = 42          # extend(42) → TypeError
    lines[1] = json.dumps(bad, ensure_ascii=False)
    session.history_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    session.rebuild_from_log()
    prompts = [m["content"] for m in session.history
               if m["role"] == "user"]
    joined = "\n".join(str(p) for p in prompts)
    assert "第0问" in joined and "第2问" in joined   # 第 2 轮的后继活着


# ---- 写者修尾:崩溃恢复正是「断尾之后第一笔追加」的现场 ----

def test_recover_inflight_repairs_the_torn_tail_before_appending(tmp_path):
    sdir = tmp_path / "sessions" / "b1-recover"
    sdir.mkdir(parents=True)
    write_corpus(sdir / "history.jsonl",
                 [encode(_turn(0)) + b"\n", encode(_turn(1)) + b"\n",
                  truncated_multibyte()])
    (sdir / "inflight.json").write_text(
        json.dumps({"prompt": "崩溃前的输入", "gen": {}}), encoding="utf-8")

    mgr = SessionManager(tmp_path / "sessions")
    recovered = mgr.recover_inflight_turns()
    assert len(recovered) == 1 and recovered[0]["session_id"] == "b1-recover"

    recs = list(iter_records(sdir / "history.jsonl"))
    assert len(recs) == 3                      # 2 条原有 + 1 条补档,断尾没把补档粘坏
    assert recs[-1].obj["status"] == "fail"
    assert recs[-1].obj["prompt"] == "崩溃前的输入"
    assert not (sdir / "inflight.json").exists()


def test_recover_without_repair_would_glue(tmp_path):
    """阳性对照:证明夹具真能复现「粘行」病——裸 append 到断尾文件,补档记录必坏。"""
    sdir = tmp_path / "sessions" / "b1-glue"
    sdir.mkdir(parents=True)
    log = write_corpus(sdir / "history.jsonl",
                       [encode(_turn(0)) + b"\n", truncated_multibyte()])
    with open(log, "a", encoding="utf-8") as f:     # 有意绕开纪律
        f.write(json.dumps(_turn(9), ensure_ascii=False) + "\n")
    recs = list(iter_records(log))
    assert [r.obj["prompt"] for r in recs] == [_turn(0)["prompt"]]  # 补档被粘没了


def test_journal_persist_repairs_and_survives_torn_tail(tmp_path):
    sdir = tmp_path / "sess"
    sdir.mkdir()
    j = TurnJournal(sdir)
    j.emit("turn_queued", {"prompt": "p", "gen": {}, "origin": "chat"}, turn_id=new_turn_id())
    j.emit("turn_ended", {"status": "success", "turn_index": 0})
    before = count_records(sdir / "journal.jsonl")
    used_max = j.seq
    # 崩溃在写一半:journal 尾部留下断尾
    with open(sdir / "journal.jsonl", "ab") as f:
        f.write(truncated_multibyte())

    j2 = TurnJournal(sdir)                     # 重启:_load_last_seq 走坏行纪律
    assert j2.seq > used_max                   # 断尾不掀翻续号(仍取有效记录的 max)
    env = j2.emit("turn_queued", {"prompt": "p2", "gen": {}, "origin": "chat"},
                  turn_id=new_turn_id())
    recs = list(iter_records(sdir / "journal.jsonl"))
    assert len(recs) == before + 1             # 修尾后追加,没有粘行
    assert recs[-1].obj["seq"] == env["seq"]
    assert [e["seq"] for e in j2.replay(after=used_max)] == [env["seq"]]


# ---- 按下标定位的两条危险路:删末轮 / 分支克隆 ----

def test_delete_last_turn_targets_the_last_valid_record(tmp_path):
    sdir = tmp_path / "sessions" / "b1-del"
    sdir.mkdir(parents=True)
    log = _corrupt_history(sdir / "history.jsonl")
    mgr = SessionManager(tmp_path / "sessions")

    result = mgr._delete_last_turn_locked(
        None, log, {"status": False, "response": "", "session_id": "b1-del"})
    assert result["status"] is True
    assert result["remaining_turns"] == 4
    kept = list(iter_records(log))
    assert [r.obj["prompt"] for r in kept] == [_turn(i)["prompt"] for i in range(4)]
    # 重写后的文件是干净的(坏行随重写消失——它们本就对所有读者不可见)
    assert count_records(log) == 4


def test_branch_point_counts_valid_records_only(tmp_path):
    sdir = tmp_path / "sessions" / "b1-branch"
    sdir.mkdir(parents=True)
    _corrupt_history(sdir / "history.jsonl")
    mgr = SessionManager(tmp_path / "sessions")

    result = asyncio.run(mgr.branch_session("b1-branch", 2, "b1-branch-child"))
    assert result["status"] is True
    child = mgr.get_history("b1-branch-child")
    # 节点 2 = 有效记录第 2 条:分支带走 [0,2],与 get_history 的下标同口径
    assert [t["prompt"] for t in child] == [_turn(i)["prompt"] for i in range(3)]


# ---- 另两处写点:护栏事件流 / 出网台账 ----

def test_guardrail_emit_repairs_torn_tail(tmp_path):
    path = events.get_path()                   # conftest 已隔离到 tmp
    write_corpus(path, [encode({"type": "old"}) + b"\n", truncated_multibyte()])
    events.emit("test_event", session="s1")
    recs = list(iter_records(path))
    assert len(recs) == 2 and recs[-1].obj["type"] == "test_event"


def test_egress_repairs_tail_and_still_raises_on_bad_entry(tmp_path, monkeypatch):
    from framework.cloud import advisor
    monkeypatch.setattr(advisor, "egress_log_path",
                        lambda: tmp_path / "cloud" / "egress.jsonl")
    p = advisor.egress_log_path()
    p.parent.mkdir(parents=True)
    write_corpus(p, [encode({"ts": "2026-08-22", "cost_usd": 1}) + b"\n",
                     truncated_multibyte()])
    advisor.record_egress({"ts": "2026-08-22", "cost_usd": 2})
    assert count_records(p) == 2               # 修尾后追加成功
    # 审计语义保留:不可序列化的条目照旧上抛,不静默降级
    with pytest.raises(TypeError):
        advisor.record_egress({"ts": object()})


# ---- 首审修正的回归钉子(H1/H2/H3/M8/M9) ----

def test_whole_file_corruption_never_moves_the_watermark(make_session, monkeypatch):
    """★ H1:整份日志坏成零有效记录(文件非空)= 读不动,水位线不动、memory.json 不删。
    这是 B1 换口径时被让出去的旧覆盖面,补回来。"""
    from framework.memory import compactor
    from framework.memory.compactor import sediment_pending_turns

    class _Rag:
        calls = []
        def sediment(self, text, title):
            self.calls.append((text, title)); return True
    monkeypatch.setattr(compactor, "rag_sink", _Rag())
    session = make_session("b1-h1")
    for i in range(3):
        t = _turn(i)
        t["response"][0]["content"] += "补足字符" * 30   # 越过 min_chars 门槛
        session.append_log(t)
    assert asyncio.run(sediment_pending_turns(session)) == 3   # 阳性对照:正常路走通
    assert session.sedimented_turns == 3

    session.history_log.write_bytes(b"\xff\xfe entirely garbage \xff")
    assert asyncio.run(sediment_pending_turns(session)) == 0
    assert session.sedimented_turns == 3                       # 不被夹到 0
    assert (session.session_dir / "memory.json").exists()      # 不被清删


def test_spent_today_survives_torn_tail_and_pseudo_newlines(tmp_path, monkeypatch):
    """★ H2:出网台账读者走唯一口径——断尾不炸(否则预算检查先于记账,顾问永久 500),
    U+2028 正文不劈行少计费用。"""
    import json as _json
    from datetime import date
    from framework.cloud import advisor
    p = tmp_path / "cloud" / "egress.jsonl"
    p.parent.mkdir(parents=True)
    today = date.today().isoformat()
    rec1 = {"ts": f"{today}T01:00:00", "cost_usd": 1.0, "error": "上半 下半"}
    rec2 = {"ts": f"{today}T02:00:00", "cost_usd": 2.0}
    write_corpus(p, [
        _json.dumps(rec1, ensure_ascii=False).encode("utf-8") + b"\n",
        _json.dumps(rec2, ensure_ascii=False).encode("utf-8") + b"\n",
        truncated_multibyte(),
    ])
    monkeypatch.setattr(advisor, "egress_log_path", lambda: p)
    assert advisor.spent_today_usd() == 3.0


def test_replay_rolls_back_a_half_applied_turn(make_session):
    """★ H3:单轮重建炸在半途要整轮回滚——否则孤悬的 user 消息打断 user/assistant
    交替,污染 model-facing 上下文的形状。"""
    import json as _json
    session = make_session("b1-h3")
    good0, good2 = _turn(0), _turn(2)
    poison = _turn(1)
    poison["response"] = 42        # add_user_message 成功后 history.extend(42) 必炸
    session.history_log.write_text(
        "\n".join(_json.dumps(t, ensure_ascii=False) for t in (good0, poison, good2)) + "\n",
        encoding="utf-8")
    session.rebuild_from_log()
    roles = [m["role"] for m in session.history if m["role"] != "system"]
    assert roles == ["user", "assistant"] * 2      # 中毒轮整轮消失,交替不被打断
    texts = "".join(str(m) for m in session.history)
    assert "第0问" in texts and "第2问" in texts and "第1问" not in texts


def test_journal_header_survives_crash_during_first_write(tmp_path):
    """M9:首次写 header 写到一半崩溃 → 修尾把文件截空 → 下一条事件仍要先补 header。"""
    sdir = tmp_path / "sess"
    sdir.mkdir()
    (sdir / "journal.jsonl").write_bytes(b'{"kind": "journal_hea')   # 半截 header,无换行
    j = TurnJournal(sdir)
    j.emit("turn_queued", {"prompt": "p", "gen": {}, "origin": "chat"}, turn_id=new_turn_id())
    recs = list(iter_records(sdir / "journal.jsonl"))
    assert recs[0].obj == {"kind": "journal_header", "v": 1}
    assert recs[1].obj["type"] == "turn_queued"


def test_schedule_runs_survive_pseudo_newline_summary(tmp_path):
    """M8:runs.jsonl 的裁环重写不许把含 U+2028 的 summary 劈成两半。"""
    from framework.schedule.store import ScheduleStore
    store = ScheduleStore(tmp_path / "schedules")
    sid = "s1"
    (tmp_path / "schedules" / sid).mkdir(parents=True, exist_ok=True)
    store.append_run(sid, {"run_id": "r1", "summary": "上半 下半"})
    store.append_run(sid, {"run_id": "r2", "summary": "普通"})
    runs = store.read_runs(sid)
    assert [r["run_id"] for r in runs] == ["r2", "r1"]
    assert runs[1]["summary"] == "上半 下半"


def test_stale_overshooting_summary_watermark_is_clamped_on_load(make_session):
    """M4:存量摘要水位线按旧口径(含坏行)偏大 → 载入时夹回记录数,否则越界区间里的
    真实轮次永远进不了 model-facing 上下文。"""
    import json as _json
    session = make_session("b1-m4")
    session.append_log(_turn(0))
    session.append_log(_turn(1))
    (session.session_dir / "memory.json").write_text(_json.dumps(
        {"summary": "旧摘要", "summarized_turns": 5, "sedimented_turns": 0}),
        encoding="utf-8")
    session.load_memory()
    assert session.summarized_turns == 2       # 夹回有效记录数
    # 对照:界内水位线不动
    (session.session_dir / "memory.json").write_text(_json.dumps(
        {"summary": "旧摘要", "summarized_turns": 1, "sedimented_turns": 0}),
        encoding="utf-8")
    session.load_memory()
    assert session.summarized_turns == 1


def test_summary_clamp_lets_go_when_log_is_unreadable(make_session):
    """M4 护栏的护栏:整份日志读不出有效记录(文件非空)时不夹——与 H1 同一判据,
    防外部还原坏日志时把摘要水位线错误归零。"""
    import json as _json
    session = make_session("b1-m4b")
    session.append_log(_turn(0))
    (session.session_dir / "memory.json").write_text(_json.dumps(
        {"summary": "旧摘要", "summarized_turns": 1, "sedimented_turns": 0}),
        encoding="utf-8")
    session.history_log.write_bytes(b"\xff\xfe all garbage \xff")
    session.load_memory()
    assert session.summarized_turns == 1       # 读不动:不夹
