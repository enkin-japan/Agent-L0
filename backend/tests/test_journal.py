"""事件账本单元测试(PROTOCOL.md §2/§3 的实现契约)。"""
import json

from framework.core.journal import SEQ_RESTART_RESERVE, TurnJournal, new_turn_id


def make_journal(tmp_path):
    d = tmp_path / "sess"
    d.mkdir(parents=True, exist_ok=True)
    return TurnJournal(d), d


def disk_records(d):
    lines = (d / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")
    return [json.loads(l) for l in lines]


def test_seq_monotonic_within_process(tmp_path):
    """新会话冷启动:seq 从 1 起、逐事件单调(增量事件同样占号)。"""
    j, _ = make_journal(tmp_path)
    assert j.seq == 0
    j.emit("turn_queued", {"prompt": "p", "gen": {}, "origin": "chat"}, turn_id=new_turn_id())
    j.emit("turn_started", {})
    j.emit("token_delta", {"text": "hi"})
    assert [e["seq"] for e in j.ring] == [1, 2, 3]


def test_seq_never_regresses_across_restart(tmp_path):
    """重启后 seq 必须严格大于崩溃前用过的最大号——**含未落盘的增量事件**。

    旧实现直接接末条里程碑续号(本测试原先正是这么断言的),而 token_delta /
    thinking_delta 占号却不落盘 → seq 就地回退到已用号段(OSS 扫描 §6.3)。
    """
    j, d = make_journal(tmp_path)
    j.emit("turn_queued", {"prompt": "p", "gen": {}, "origin": "chat"}, turn_id=new_turn_id())
    j.emit("turn_started", {})                        # 末条里程碑 seq=2
    for _ in range(50):
        j.emit("token_delta", {"text": "x"})          # 占号不落盘
    used_max = j.seq
    assert used_max == 52                             # 崩溃前真实用到的最大号

    j2 = TurnJournal(d)                               # 进程重启
    assert j2.seq >= used_max                         # 起号不得落回已用号段
    assert j2.emit("turn_started", {})["seq"] > used_max


def test_restart_events_clear_the_client_cursor(tmp_path):
    """要害:重启后的新事件必须越得过客户端游标,否则被服务端直播循环静默丢弃。

    `routes.attach_events` 对 `seq <= 已发送最大号` 的事件直接 continue,而两端客户端
    的游标是内存态、跨重连保留且不读 stream_attached.last_seq——seq 一回退,整条会话
    对用户就永久静音。这条测的是那个后果,不是实现细节。
    """
    j, d = make_journal(tmp_path)
    j.emit("turn_queued", {"prompt": "p", "gen": {}, "origin": "chat"}, turn_id=new_turn_id())
    j.emit("turn_started", {})
    for _ in range(200):
        j.emit("thinking_delta", {"text": "x"})
    client_cursor = j.seq                             # 客户端已消费到这里

    j2 = TurnJournal(d)                               # devctl restart
    env = j2.emit("turn_queued", {"prompt": "p2", "gen": {}, "origin": "chat"},
                  turn_id=new_turn_id())
    assert env["seq"] > client_cursor
    # 重放同样要能把新事件交出去(游标之后的都在)
    assert [e["seq"] for e in j2.replay(after=client_cursor)] == [env["seq"]]


def test_seq_monotonic_across_repeated_restarts(tmp_path):
    """连续多次重启:每次起号都高于**上一实例真正分配到的最高号**(含未落盘增量)。

    每轮必须发未落盘的增量事件,否则本条会退化成「里程碑 seq 单调」——那在修复前
    就已成立,测不出任何东西(首过评审 M3:原写法把 RESERVE 改成 0 照样绿)。
    """
    j, d = make_journal(tmp_path)
    j.emit("turn_queued", {"prompt": "p", "gen": {}, "origin": "chat"}, turn_id=new_turn_id())
    highest = j.seq
    for _ in range(3):
        jn = TurnJournal(d)
        assert jn.seq >= highest                      # 起号不得落回上一实例用过的号段
        jn.emit("turn_started", {})                   # 里程碑,落盘
        for _ in range(30):
            jn.emit("token_delta", {"text": "x"})     # 占号不落盘 —— 余量要盖住的正是这些
        highest = jn.seq


def test_unreadable_journal_does_not_restart_from_zero(tmp_path):
    """账本存在但读不动 → 仍按续跑加余量起号,绝不退回 0。

    `_load_last_seq` 对「无账本」「空账本」「读失败」原本都返回 0;若按真假判断,
    账本损坏这个最该防的场景反而会从 0 起号,把永久静音以最大严重度重现
    (首过评审 M1)。
    """
    j, d = make_journal(tmp_path)
    j.emit("turn_queued", {"prompt": "p", "gen": {}, "origin": "chat"}, turn_id=new_turn_id())
    for _ in range(50):
        j.emit("token_delta", {"text": "x"})
    used_max = j.seq

    # 账本被写坏成不可解析的字节(断尾/乱码)
    (d / "journal.jsonl").write_text("{{{ not json at all", encoding="utf-8")
    assert TurnJournal(d).seq > used_max

    # 极端:整份账本连读都读不动(此处用「路径是目录」构造 IO 异常,走 except 分支)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "journal.jsonl").mkdir()
    assert TurnJournal(broken).seq >= SEQ_RESTART_RESERVE

    # 对照:真正的冷启动(没有账本文件)才从 0 起
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert TurnJournal(fresh).seq == 0


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
