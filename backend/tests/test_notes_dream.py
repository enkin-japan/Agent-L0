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
