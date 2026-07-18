"""dream 式笔记整理金测试（grok-build §3.2 采纳，2026-07-18）。

守护的失败形态：①闸门失效——整理梦高频抢生成位；②模型幻觉名字/越权删手写
笔记——DROP 只许删含自动抢救标记的笔记，改动前必备份；③空输出盖时间戳——
被抢占的一次「梦」吃掉 24h 配额，整理永远轮不到。
"""
import asyncio
import json
import time

import pytest

from framework.agents import runtime
from framework.memory import dream
from framework.memory.notes import NoteStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = NoteStore(tmp_path / "notes")
    monkeypatch.setattr(runtime, "get_note_store", lambda: st)
    import framework.memory.rag_sink as sink
    monkeypatch.setattr(sink, "sediment_note", lambda *a, **k: False)
    monkeypatch.setattr(sink, "recall_memory", lambda *a, **k: [])
    return st


def _stamp(st, hours_ago: float, applied=0):
    st.notes_dir.mkdir(parents=True, exist_ok=True)
    (st.notes_dir / dream.STAMP_FILE).write_text(
        json.dumps({"last_at": time.time() - hours_ago * 3600, "applied": applied}))


# ---------- 闸门 ----------

def test_gate_disabled(store):
    ok, why = dream.check_gates(store, {"enabled": False})
    assert not ok and why == "disabled"


def test_gate_too_soon(store):
    store.write("a-note", "d", "user", "b")
    _stamp(store, hours_ago=1)
    ok, why = dream.check_gates(store, {"min_hours": 24, "min_new_notes": 1})
    assert not ok and why == "too-soon"


def test_gate_too_few_changes(store):
    _stamp(store, hours_ago=48)
    store.write("a-note", "d", "user", "b")
    ok, why = dream.check_gates(store, {"min_hours": 24, "min_new_notes": 4})
    assert not ok and why.startswith("too-few-changes")


def test_gate_opens(store):
    _stamp(store, hours_ago=48)
    for i in range(4):
        store.write(f"note-{i}", "d", "user", "b")
    ok, why = dream.check_gates(store, {"min_hours": 24, "min_new_notes": 4})
    assert ok and "4 changed" in why


# ---------- 解析 ----------

def test_parse_ops_two_forms_and_cap():
    text = ("DROP: old-note | covered by new-note\n"
            "REWRITE: vague-note | user | clear desc | clear body 2026-07-18\n"
            "垃圾行\nMERGE: a + b\n")
    ops = dream.parse_ops(text)
    assert ops == [
        ("drop", ["old-note", "covered by new-note"]),
        ("rewrite", ["vague-note", "user", "clear desc", "clear body 2026-07-18"]),
    ]
    many = "\n".join(f"DROP: n-{i} | x" for i in range(10))
    assert len(dream.parse_ops(many)) == dream.MAX_OPS


def test_parse_clean_returns_nothing():
    assert dream.parse_ops("CLEAN") == []


# ---------- 应用护栏 ----------

def test_drop_only_touches_rescued_notes(store):
    store.write("hand-note", "手写", "user", "手写正文")
    store.write("auto-note", "自动", "user", f"事实\n\n（压缩前记忆冲洗{dream.RESCUE_MARK}：会话 x）")
    n = dream.apply_ops(store, [("drop", ["hand-note", "why"]),
                                ("drop", ["auto-note", "why"]),
                                ("drop", ["ghost-note", "幻觉名"])])
    assert n == 1
    assert store.get("hand-note") is not None      # 手写不许删
    assert store.get("auto-note") is None          # 自动抢救的可删
    # 删除前备份在案
    backups = list((store.notes_dir / dream.BACKUP_DIR).rglob("auto-note.md"))
    assert len(backups) == 1


def test_rewrite_validates_and_backs_up(store):
    store.write("vague-note", "含糊", "user", "上周做了个东西")
    n = dream.apply_ops(store, [
        ("rewrite", ["vague-note", "user", "清晰描述", "2026-07-11 那周完成了 X"]),
        ("rewrite", ["vague-note", "badtype", "d", "b"]),      # 类型非法跳过
        ("rewrite", ["ghost", "user", "d", "b"]),              # 幻觉名跳过
    ])
    assert n == 1
    assert "2026-07-11" in store.get("vague-note").body
    backups = list((store.notes_dir / dream.BACKUP_DIR).rglob("vague-note.md"))
    assert len(backups) == 1 and "上周" in backups[0].read_text(encoding="utf-8")


# ---------- 端到端（假引擎） ----------

class _Engine:
    def __init__(self, out):
        self.out = out
        self.called = 0

    async def oneshot_instruction(self, instruction, content, **kw):
        self.called += 1
        return self.out


def test_maybe_dream_applies_and_stamps(store):
    _stamp(store, hours_ago=48)
    store.write("hand-note", "手写", "user", "手写正文")
    for i in range(3):
        store.write(f"auto-{i}", "自动", "user", f"事实 {i}（{dream.RESCUE_MARK}）")
    eng = _Engine("DROP: auto-0 | redundant\nCLEAN")
    applied = asyncio.run(dream.maybe_dream(eng, {"min_hours": 24, "min_new_notes": 4}))
    assert applied == 1 and eng.called == 1
    stamp = json.loads((store.notes_dir / dream.STAMP_FILE).read_text())
    assert time.time() - stamp["last_at"] < 60      # 时间戳已刷新


def test_maybe_dream_empty_output_does_not_stamp(store):
    _stamp(store, hours_ago=48)
    for i in range(4):
        store.write(f"n-{i}", "d", "user", "b")
    eng = _Engine("")
    applied = asyncio.run(dream.maybe_dream(eng, {"min_hours": 24, "min_new_notes": 4}))
    assert applied == 0
    stamp = json.loads((store.notes_dir / dream.STAMP_FILE).read_text())
    assert time.time() - stamp["last_at"] > 47 * 3600   # 旧戳未动：下次还会重试


def test_maybe_dream_gate_closed_no_generation(store):
    eng = _Engine("CLEAN")
    applied = asyncio.run(dream.maybe_dream(eng, {"enabled": False}))
    assert applied == 0 and eng.called == 0


# ---------- 巡检戳（P2a）----------

def test_review_stamps_roundtrip_and_orphan_prune(store):
    store.write("kept", "d", "user", "b")
    store.write("doomed", "d", "user", "b")
    dream.stamp_reviewed(store, ["kept", "doomed"])
    stamps = dream.read_review_stamps(store)
    assert set(stamps) == {"kept", "doomed"}
    assert all(time.time() - v < 5 for v in stamps.values())

    store.delete("doomed")
    dream.stamp_reviewed(store, ["kept"])       # 再盖戳时孤儿被清
    assert set(dream.read_review_stamps(store)) == {"kept"}


def test_review_stamps_missing_or_corrupt_is_empty(store):
    assert dream.read_review_stamps(store) == {}
    store.notes_dir.mkdir(parents=True, exist_ok=True)
    (store.notes_dir / dream.REVIEW_FILE).write_text("not json{")
    assert dream.read_review_stamps(store) == {}
    dream.stamp_reviewed(store, [])             # 空名单不动盘
    assert not (store.notes_dir / dream.REVIEW_FILE).read_text().startswith("{\"")


# ---------- 巡检轮转（P2b）----------

def _set_stamps(store, stamps: dict):
    store.notes_dir.mkdir(parents=True, exist_ok=True)
    (store.notes_dir / dream.REVIEW_FILE).write_text(json.dumps(stamps))


def test_window_prefers_changed_then_never_reviewed(store, monkeypatch):
    monkeypatch.setattr(dream, "REVIEW_WINDOW", 2)
    for n in ("n1", "n2", "n3"):
        store.write(n, "d", "user", "b")
    now = time.time()
    # n1 巡检于写入之后（未变动）；n2 巡检远早于 mtime（变动）；n3 从未巡检
    _set_stamps(store, {"n1": now + 60, "n2": 100.0})
    assert dream.select_review_window(store) == ["n3", "n2"]


def test_window_rotates_after_stamping(store, monkeypatch):
    import os
    monkeypatch.setattr(dream, "REVIEW_WINDOW", 2)
    now = time.time()
    for n in ("n1", "n2", "n3", "n4"):
        store.write(n, "d", "user", "b")
        os.utime(store.get(n).source, (now - 1000, now - 1000))   # 回拨 mtime=旧笔记
    # 全部已巡检且未变动（戳 > mtime），戳 n1 最旧 → 首轮窗口 [n1, n2]
    _set_stamps(store, {n: now - 900 + i for i, n in enumerate(("n1", "n2", "n3", "n4"))})
    assert dream.select_review_window(store) == ["n1", "n2"]
    dream.stamp_reviewed(store, ["n1", "n2"])                     # 重盖=now，变最新
    assert dream.select_review_window(store) == ["n3", "n4"]   # 轮转前进，尾部总会轮到


def test_maybe_dream_stamps_window_on_clean(store):
    store.write("solo-note", "d", "user", "b")
    _stamp(store, hours_ago=48)
    eng = _Engine("CLEAN")
    asyncio.run(dream.maybe_dream(eng, {"min_hours": 24, "min_new_notes": 1}))
    assert "solo-note" in dream.read_review_stamps(store)


def test_maybe_dream_empty_output_stamps_nothing(store):
    store.write("solo-note", "d", "user", "b")
    _stamp(store, hours_ago=48)
    eng = _Engine("")
    asyncio.run(dream.maybe_dream(eng, {"min_hours": 24, "min_new_notes": 1}))
    assert dream.read_review_stamps(store) == {}   # 没看成不装看过


# ---------- 证据对照 + STALE（P2c）----------

def test_parse_stale_op():
    ops = dream.parse_ops("STALE: a-note | 2026-07 对话说项目已改名\nSTALE: bad |")
    assert ops == [("stale", ["a-note", "2026-07 对话说项目已改名"])]


def test_stale_marks_description_only_and_is_idempotent(store):
    store.write("fact", "项目叫 X", "project", "项目名为 X（手写）")
    applied = dream.apply_ops(store, [("stale", ["fact", "对话记录冲突"])])
    note = store.get("fact")
    assert applied == 1
    assert note.description.startswith(dream.STALE_MARK)     # 标在描述上，下轮提示词可见
    assert note.body == "项目名为 X（手写）"                  # 内容一个字不动
    backups = list((store.notes_dir / dream.BACKUP_DIR).glob("*/fact*"))
    assert backups, "标记前未备份"
    # 再标一次：幂等跳过，不叠前缀
    assert dream.apply_ops(store, [("stale", [ "fact", "again"])]) == 0
    assert store.get("fact").description.count(dream.STALE_MARK) == 1


def test_render_attaches_evidence_line(store):
    store.write("fact", "d", "project", "b")
    out = dream._render_notes(store, ["fact"], {"fact": "对话片段：项目已改名 Y"})
    assert "EVIDENCE: 对话片段：项目已改名 Y" in out
    out2 = dream._render_notes(store, ["fact"], {})
    assert "EVIDENCE" not in out2                            # 无证据不留空行头


def test_maybe_dream_gathers_evidence_into_prompt(store, monkeypatch):
    import framework.memory.rag_sink as sink
    store.write("fact", "项目名", "project", "项目名为 X")
    _stamp(store, hours_ago=48)
    monkeypatch.setattr(sink, "recall_memory", lambda *a, **k: ["记录：项目改名为 Y"])
    eng = _Engine("STALE: fact | evidence contradicts")
    applied = asyncio.run(dream.maybe_dream(eng, {"min_hours": 24, "min_new_notes": 1}))
    assert applied == 1
    assert store.get("fact").description.startswith(dream.STALE_MARK)
