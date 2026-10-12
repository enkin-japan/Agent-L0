"""Regression coverage for dream operations preserving note expiry metadata."""
from datetime import date, timedelta

import pytest

from framework.memory import dream
from framework.memory.notes import NoteStore


@pytest.mark.parametrize("operation", ["stale", "rewrite"])
@pytest.mark.parametrize("expiry", ["past", "future", "empty"])
def test_dream_operation_preserves_valid_until_and_expiry_prompt(
        tmp_path, monkeypatch, operation, expiry):
    store = NoteStore(tmp_path / "notes")
    past = (date.today() - timedelta(days=3)).isoformat()
    future = (date.today() + timedelta(days=30)).isoformat()
    valid_until = {"past": past, "future": future, "empty": ""}[expiry]
    store.write("fact", "项目有效期", "project", "当前事实", valid_until=valid_until)

    monkeypatch.setattr(dream.rag_sink, "sediment_note", lambda *a, **k: False)
    if operation == "stale":
        ops = [("stale", ["fact", "对话证据冲突"])]
    else:
        ops = [("rewrite", ["fact", "project", "整理后的描述", "整理后的事实"])]

    assert dream.apply_ops(store, ops) == 1

    # Reopen from disk to prove frontmatter and the model-facing index retain the date.
    reopened = NoteStore(tmp_path / "notes")
    note = reopened.get("fact")
    assert note.valid_until == valid_until
    index = "\n".join(reopened.index_lines())
    if expiry == "past":
        assert f"[EXPIRED {past}" in index
        assert "verify before relying on it" in index
    elif expiry == "future":
        assert f"(valid until {future})" in index
        assert "EXPIRED" not in index
    else:
        assert "valid until" not in index
        assert "EXPIRED" not in index

