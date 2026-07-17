"""压缩前记忆冲洗（memory flush）金测试（grok-build §3.1 + mem0 §3.3 合并票）。

守护的失败形态：①MEMO 块渗进滚动摘要——下次合并时 [Existing summary] 里冒出
MEMO 教坏模型重复输出、摘要越滚越脏；②自动冲洗覆盖手写笔记——低质自动产出
悄悄盖掉 agent 深思后的 memory_write；③坏行（格式/类型/slug 不合法）拖垮流程。
"""
from framework.agents import runtime
from framework.memory.compactor import _flush_rescued_notes, _split_memo
from framework.memory.notes import NoteStore


# ---------- _split_memo ----------

def test_split_extracts_memo_and_cleans_summary():
    text = ("- 用户在做 Agent 项目\n- 2026-07-18 完成 B2c\n"
            "MEMO:\n"
            "- user-timezone | user | 用户在东京时区（2026-07-18 确认）\n"
            "- likes-terse | feedback | 用户偏好简短回复\n")
    summary, memo = _split_memo(text)
    assert "MEMO" not in summary
    assert summary.endswith("完成 B2c")
    assert len(memo) == 2 and memo[0].startswith("- user-timezone")


def test_split_without_memo_returns_original():
    text = "- 只有摘要\n- 没有备忘"
    summary, memo = _split_memo(text)
    assert summary == text and memo == []


def test_split_ignores_non_bullet_noise_after_memo():
    text = "摘要\nMEMO:\n以下是备忘：\n- a-b | user | 事实\n结束"
    _, memo = _split_memo(text)
    assert memo == ["- a-b | user | 事实"]


# ---------- _flush_rescued_notes ----------

def _store(tmp_path, monkeypatch):
    store = NoteStore(tmp_path / "notes")
    monkeypatch.setattr(runtime, "get_note_store", lambda: store)
    # 掐断 RAG 网络路（fail-open 本就允许离线）
    import framework.memory.rag_sink as sink
    monkeypatch.setattr(sink, "sediment_note", lambda *a, **k: False)
    return store


def test_flush_writes_valid_lines(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    n = _flush_rescued_notes(
        ["- user-timezone | user | 用户在东京时区（2026-07-18 确认）"], "sid-1")
    assert n == 1
    note = store.get("user-timezone")
    assert note is not None and "东京时区" in note.body
    assert "自动抢救" in note.body          # 出处标记在身
    assert "sid-1" in note.body


def test_flush_never_overwrites_existing_note(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.write("user-timezone", "手写的", "user", "手写正文（真相）")
    n = _flush_rescued_notes(
        ["- user-timezone | user | 自动产出的低质版本"], "sid-1")
    assert n == 0
    assert store.get("user-timezone").body == "手写正文（真相）"


def test_flush_skips_garbage_lines_silently(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    n = _flush_rescued_notes(
        ["- 没有竖线分隔",
         "- Bad-Slug! | user | 大写与符号非法",
         "- ok-name | notatype | 类型非法",
         "- only | two-parts",
         ""], "sid-1")
    assert n == 0


def test_flush_caps_at_three(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    lines = [f"- fact-{i} | project | 事实 {i}（2026-07-18）" for i in range(5)]
    n = _flush_rescued_notes(lines, "sid-1")
    assert n == 3
    assert store.get("fact-3") is None      # 第 4 条起被熔断


def test_flush_without_store_is_noop(monkeypatch):
    monkeypatch.setattr(runtime, "get_note_store", lambda: None)
    assert _flush_rescued_notes(["- a-b | user | x"], "sid") == 0
