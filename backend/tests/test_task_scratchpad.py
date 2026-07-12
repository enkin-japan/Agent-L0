"""记事板(scratchpad)金测试:remember 的有界写入 + 渲染 + 序列化往返。

记事板是模型自写、跨步可见、抗上下文压缩的持久便签(想法4)。
不变量:非空校验、精确去重、单条截断、FIFO 有界、快照往返、空板不渲染。
"""
from framework.core.task_state import TaskState, MAX_NOTES, MAX_NOTE_CHARS

DOING_STEP = [{"id": 1, "title": "s", "status": "doing", "note": ""}]


def test_remember_appends_and_renders_in_task_block():
    t = TaskState(goal="g")
    ok, msg = t.remember("API = https://x.example/v2")
    assert ok and t.notes == ["API = https://x.example/v2"]
    assert "https://x.example/v2" in msg
    t.plan = list(DOING_STEP)
    block = t.render(4, 3)
    assert "Scratchpad" in block and "https://x.example/v2" in block


def test_remember_empty_rejected():
    t = TaskState()
    ok, _ = t.remember("   ")
    assert not ok and t.notes == []


def test_remember_dedup_exact():
    t = TaskState()
    t.remember("same note")
    ok, msg = t.remember("same note")
    assert ok and t.notes == ["same note"]      # 不重复累积
    assert "Already" in msg


def test_remember_truncates_long_note():
    t = TaskState()
    t.remember("x" * (MAX_NOTE_CHARS + 50))
    assert len(t.notes[0]) == MAX_NOTE_CHARS


def test_remember_fifo_bounded():
    t = TaskState()
    for i in range(MAX_NOTES + 3):
        t.remember(f"note-{i}")
    assert len(t.notes) == MAX_NOTES            # 有界,永不溢出
    assert t.notes[0] == "note-3"               # FIFO:最旧三条被淘汰
    assert t.notes[-1] == f"note-{MAX_NOTES + 2}"


def test_scratchpad_survives_snapshot_roundtrip():
    t = TaskState(goal="g")
    t.plan = list(DOING_STEP)
    t.remember("carry me across suspend/resume")
    restored = TaskState.from_snapshot(t.snapshot())
    assert restored.notes == ["carry me across suspend/resume"]


def test_empty_scratchpad_not_rendered():
    t = TaskState(goal="g")
    t.plan = list(DOING_STEP)
    assert "Scratchpad" not in t.render(4, 3)    # 空板不渲染,保持任务块精简
