"""事件账本单元测试(PROTOCOL.md §2/§3 的实现契约)。"""
import json

from framework.core.journal import TurnJournal, new_turn_id


def make_journal(tmp_path):
    d = tmp_path / "sess"
    d.mkdir(parents=True, exist_ok=True)
    return TurnJournal(d), d


def disk_records(d):
    lines = (d / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")
    return [json.loads(l) for l in lines]


def test_seq_monotonic_and_reload(tmp_path):
    j, d = make_journal(tmp_path)
    j.emit("turn_queued", {"prompt": "p", "gen": {}, "origin": "chat"}, turn_id=new_turn_id())
    j.emit("turn_started", {})
    j.emit("token_delta", {"text": "hi"})
    assert [e["seq"] for e in j.ring] == [1, 2, 3]

    # 重启(新实例):seq 从磁盘里程碑续增(token_delta 不落盘,最后里程碑 seq=2)
    j2 = TurnJournal(d)
    assert j2.seq == 2
    env = j2.emit("turn_started", {})
    assert env["seq"] == 3


def test_milestone_only_persistence_and_slim_turn_ended(tmp_path):
    j, d = make_journal(tmp_path)
    j.emit("turn_queued", {"prompt": "p", "gen": {}, "origin": "chat"}, turn_id=new_turn_id())
    j.emit("token_delta", {"text": "秘密增量"})
    j.emit("thinking_delta", {"text": "思考增量"})
    full_end = {"status": "success", "prompt": "p", "response": [{"role": "assistant", "content": "答"}],
                "turn_index": 0}
    j.emit("turn_ended", full_end)

    recs = disk_records(d)
    assert recs[0] == {"kind": "journal_header", "v": 1}
    types = [r["type"] for r in recs[1:]]
    assert types == ["turn_queued", "turn_ended"]          # 增量不落盘
    ended = recs[-1]["data"]
    assert ended == {"status": "success", "turn_index": 0}  # 磁盘瘦身
    # 内存环里是全量(实时下发用)
    assert j.ring[-1]["data"]["response"][0]["content"] == "答"


def test_persist_truncates_big_tool_fields(tmp_path):
    j, d = make_journal(tmp_path)
    j.emit("turn_queued", {"prompt": "p", "gen": {}, "origin": "chat"}, turn_id=new_turn_id())
    big = "x" * 5000
    j.emit("tool_started", {"call_id": "c1", "name": "t", "args": {"blob": big}})
    j.emit("tool_result", {"call_id": "c1", "name": "t", "response": big, "status": "ok"})

    recs = {r["type"]: r["data"] for r in disk_records(d)[1:]}
    assert len(recs["tool_started"]["args"]) == 2000 and recs["tool_started"]["truncated"]
    assert len(recs["tool_result"]["response"]) == 2000 and recs["tool_result"]["truncated"]
    # 内存环(实时帧)不截断
    live = {e["type"]: e["data"] for e in j.ring}
    assert live["tool_result"]["response"] == big


def test_replay_merges_disk_and_ring(tmp_path):
    j, d = make_journal(tmp_path)
    t1 = new_turn_id()
    j.emit("turn_queued", {"prompt": "第一轮", "gen": {}, "origin": "chat"}, turn_id=t1)
    j.emit("turn_started", {})
    j.emit("token_delta", {"text": "a"})
    j.emit("turn_ended", {"status": "success", "turn_index": 0})
    # 第二轮开始:环清空,只装活跃轮
    t2 = new_turn_id()
    j.emit("turn_queued", {"prompt": "第二轮", "gen": {}, "origin": "chat"}, turn_id=t2)
    j.emit("token_delta", {"text": "b"})

    events = j.replay(0)
    # 旧轮:磁盘里程碑(无 token_delta);新轮:环全量(含 token_delta)
    assert [(e["type"], e["turn_id"]) for e in events] == [
        ("turn_queued", t1), ("turn_started", t1), ("turn_ended", t1),
        ("turn_queued", t2), ("token_delta", t2),
    ]
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    # 游标续播:after=最后一条,重放为空
    assert j.replay(events[-1]["seq"]) == []


def test_state_tracking_and_snapshot(tmp_path):
    j, _ = make_journal(tmp_path)
    assert j.turn_snapshot() is None
    j.emit("turn_queued", {"prompt": "帮我改文件", "gen": {}, "origin": "chat"},
           turn_id=new_turn_id())
    j.emit("turn_started", {})
    j.emit("token_delta", {"text": "好的,"})
    j.emit("thinking_delta", {"text": "需要审批。"})
    j.emit("awaiting_approval", {"pending_tools": [{"name": "edit_file", "arguments": {}}]})

    snap = j.turn_snapshot()
    assert snap["state"] == "awaiting_approval"
    assert snap["prompt"] == "帮我改文件"
    assert snap["partial_text"] == "好的,"
    assert snap["partial_thinking"] == "需要审批。"
    assert snap["pending_tools"][0]["name"] == "edit_file"
    assert snap["first_seq"] == 1 and snap["last_seq"] == 5

    j.emit("resumed", {"trigger": "approve"})
    assert j.turn_snapshot()["state"] == "queued"
    j.emit("turn_started", {})
    j.emit("turn_ended", {"status": "success", "turn_index": 0})
    assert j.turn_snapshot() is None


def test_subscribe_live_and_close(tmp_path):
    j, _ = make_journal(tmp_path)
    q = j.subscribe()
    env = j.emit("turn_queued", {"prompt": "p", "gen": {}, "origin": "chat"},
                 turn_id=new_turn_id())
    assert q.get_nowait() == env
    j.close()
    assert q.get_nowait() is None          # 终结哨兵
    q2 = j.subscribe()
    assert q2.get_nowait() is None         # 已终结账本的新订阅立即收哨兵
