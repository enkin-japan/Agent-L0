"""deck_critique 单元测试：纯函数分支 + critique_deck 主流程（mock 掉截图/引擎/路由，
不真起 Chrome/模型）。仿 test_turn_runner.py 的 asyncio.run(...) 跑法（本仓无 pytest-asyncio）。
"""
import asyncio
import json
from pathlib import Path

from framework.ppt import deck_critique, deck_ops


def run(coro):
    return asyncio.run(coro)


# ---------- _current_spec_text ----------

def test_current_spec_text_with_source():
    source = {"layout": "bullets", "title": "Hi", "bullets": ["a", "b"]}
    slide = {"layout": "bullets", "blocks": [], "source": source}
    text, has_source = deck_critique._current_spec_text(slide)
    assert has_source is True
    assert json.loads(text) == source


def test_current_spec_text_without_source():
    from framework.ppt import spec as deck_spec

    slide = deck_spec.expand_slide({"layout": "bullets", "title": "T", "bullets": ["x"]})
    assert "source" not in slide
    text, has_source = deck_critique._current_spec_text(slide)
    assert has_source is False
    assert "layout=bullets" in text


# ---------- _format_digest ----------

def test_format_digest_with_revised():
    revised = [(1, ["title too long"]), (2, [])]
    passed = [3]
    skipped = ["p4(locked)"]
    out = deck_critique._format_digest(revised, passed, skipped)
    assert "revised" in out
    assert "- slide 1 revised: title too long" in out
    assert "- slide 2 revised: visual fix applied" in out
    assert "3" in out  # passed list
    assert "p4(locked)" in out  # skipped list
    assert "Re-render" in out


def test_format_digest_empty_lists():
    out = deck_critique._format_digest([], [], [])
    assert "0 slide(s) revised" in out
    assert "Re-render" not in out


# ---------- _parse_text_review（正文 JSON 兜底：模型不走 tool_call 块时的判决通道）----------

def test_parse_text_review_fenced_json():
    text = '```json\n{"verdict": "pass"}\n```'
    assert deck_critique._parse_text_review(text) == {"verdict": "pass"}


def test_parse_text_review_revise_with_slot():
    text = ('评审如下：{"verdict": "revise", "issues": ["overcrowding"], '
            '"slot": {"layout": "bullets", "title": "T", "bullets": ["a"]}}')
    obj = deck_critique._parse_text_review(text)
    assert obj is not None and obj["verdict"] == "revise"
    assert obj["slot"]["title"] == "T"


def test_parse_text_review_rejects_garbage():
    assert deck_critique._parse_text_review("平平无奇的正文，没有 JSON") is None
    assert deck_critique._parse_text_review('{"foo": 1}') is None
    assert deck_critique._parse_text_review('{"verdict": "maybe"}') is None
    assert deck_critique._parse_text_review("") is None


# ---------- critique_deck ----------

def _build_deck(session_dir: Path, n: int = 3):
    ok, msg = deck_ops.init_deck(session_dir, "T", "light", False)
    assert ok, msg
    for i in range(1, n + 1):
        ok, msg = deck_ops.set_slide(session_dir, 0, {
            "layout": "bullets", "title": f"Slide {i}", "bullets": [f"point {i}a", f"point {i}b"],
        })
        assert ok, msg


def _lock_slide(session_dir: Path, pos: int):
    deck = deck_ops.load_deck(session_dir)
    deck["slides"][pos - 1]["lock"]["locked"] = True
    deck_ops.save_deck(session_dir, deck)


def _strip_source(session_dir: Path, pos: int):
    deck = deck_ops.load_deck(session_dir)
    del deck["slides"][pos - 1]["source"]
    deck_ops.save_deck(session_dir, deck)


def _patch_common(monkeypatch, session_dir: Path, n_pages: int):
    """打上三处外部重依赖的桩，绕开 print host / playwright 前置检查。"""
    from framework.ppt import pdf_export

    monkeypatch.setattr(pdf_export, "_pick_print_host", lambda cfg: ("http://x", "frontend"))

    def fake_screenshot(base, host_kind, url, shots_dir, timeout_s):
        return [shots_dir / f"page_{i + 1}.png" for i in range(n_pages)]

    monkeypatch.setattr(deck_critique, "_screenshot_pages", fake_screenshot)

    async def fake_route_vision(engine):
        return object()

    monkeypatch.setattr(deck_critique, "_route_vision", fake_route_vision)


def _default_cfg():
    return {"enabled": True, "screenshot_timeout": 5, "max_pages": 12, "max_tokens": 100}


def test_critique_deck_all_pass_leaves_deck_untouched(tmp_path, monkeypatch):
    session_dir = tmp_path / "sess"
    _build_deck(session_dir, n=3)
    before = deck_ops.deck_path(session_dir).read_text(encoding="utf-8")

    _patch_common(monkeypatch, session_dir, n_pages=3)

    async def fake_review_page(qa_engine, spec_text, pos, png, cfg):
        return {"verdict": "pass"}

    monkeypatch.setattr(deck_critique, "_review_page", fake_review_page)

    out = run(deck_critique.critique_deck(object(), "t-sess", session_dir, _default_cfg()))
    assert "0 slide(s) revised" in out

    after = deck_ops.deck_path(session_dir).read_text(encoding="utf-8")
    assert before == after


def test_critique_deck_revise_locked_and_no_source(tmp_path, monkeypatch):
    session_dir = tmp_path / "sess"
    _build_deck(session_dir, n=3)
    _lock_slide(session_dir, 2)
    _strip_source(session_dir, 3)

    before = deck_ops.load_deck(session_dir)
    before_slide2 = json.dumps(before["slides"][1], ensure_ascii=False)
    before_slide3 = json.dumps(before["slides"][2], ensure_ascii=False)
    before_id1 = before["slides"][0]["id"]

    _patch_common(monkeypatch, session_dir, n_pages=3)

    new_slot = {"layout": "bullets", "title": "Revised Title",
               "bullets": ["new point 1", "new point 2"]}

    async def fake_review_page(qa_engine, spec_text, pos, png, cfg):
        if pos == 1:
            return {"verdict": "revise", "issues": ["title too long"], "slot": new_slot}
        if pos == 2:
            # 锁页在到达 _review_page 之前就该被短路跳过；万一被调用也给个 revise，
            # 用来反证锁页优先级更高（下方断言 deck 未变即可证明它没被这条路径改动）。
            return {"verdict": "revise", "issues": ["should never apply"],
                   "slot": {"layout": "bullets", "title": "SHOULD NOT APPLY", "bullets": ["x"]}}
        if pos == 3:
            return {"verdict": "revise", "issues": ["bad crop"],
                   "slot": {"layout": "bullets", "title": "SHOULD NOT APPLY EITHER", "bullets": ["y"]}}
        return {"verdict": "pass"}

    monkeypatch.setattr(deck_critique, "_review_page", fake_review_page)

    out = run(deck_critique.critique_deck(object(), "t-sess", session_dir, _default_cfg()))

    assert "1 slide(s) revised" in out
    assert "locked" in out
    assert "no source" in out

    after = deck_ops.load_deck(session_dir)
    # slide 1: revised — id 稳定，标题已更新
    assert after["slides"][0]["id"] == before_id1
    title_texts = [b["content"].get("text") for b in after["slides"][0]["blocks"]
                   if b["type"] == "text" and b.get("role") == "title"]
    assert title_texts == ["Revised Title"]
    assert after["slides"][0]["source"] == new_slot

    # slide 2: locked — 完全未变
    assert json.dumps(after["slides"][1], ensure_ascii=False) == before_slide2
    assert after["slides"][1]["lock"]["locked"] is True

    # slide 3: no source — 只报不改，完全未变
    assert json.dumps(after["slides"][2], ensure_ascii=False) == before_slide3
    assert "source" not in after["slides"][2]


def test_critique_deck_no_deck(tmp_path):
    session_dir = tmp_path / "sess-empty"
    out = run(deck_critique.critique_deck(object(), "t-sess", session_dir, _default_cfg()))
    assert "No deck" in out
