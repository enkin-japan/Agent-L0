"""Deck 操作层：deck.json 的读写与增删改（MCP deck_* 工具的能力本体）。

落盘：sessions/{id}/deck/deck.json（deck 归会话，随会话删除）。原子写（tmp+rename），
对齐 session.py 的落盘习惯。资产（图表 PNG 等）与 render_chart 共用 sessions/{id}/assets/。

锁语义（P6 共享编辑的地基，本期只守约定）：slide.lock.locked=True 的页由人定稿，
set_slide / remove_slide 拒绝触碰——评审回炉上线后同样以此为界。
"""
import json
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import spec as deck_spec

MAX_SLIDES = 40


def deck_path(session_dir: Path) -> Path:
    return session_dir / "deck" / "deck.json"


def load_deck(session_dir: Path) -> Optional[Dict]:
    p = deck_path(session_dir)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_deck(session_dir: Path, deck: Dict) -> None:
    p = deck_path(session_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(deck, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def init_deck(session_dir: Path, title: str, theme: str, overwrite: bool) -> Tuple[bool, str]:
    """新建 deck。已存在且未 overwrite 时拒绝（防误覆盖整份工作成果）。"""
    existing = load_deck(session_dir)
    if existing is not None and not overwrite:
        return False, ("A deck already exists in this session "
                       f"({len(existing['slides'])} slides). Use deck_status to inspect it, "
                       "deck_set_slide to edit it, or call deck_init with overwrite=true to "
                       "discard it and start fresh.")
    deck = deck_spec.new_deck(title, theme or deck_spec.DEFAULT_THEME)
    save_deck(session_dir, deck)
    return True, f"Deck \"{title}\" created (theme {deck['meta']['theme']}, 0 slides)."


def set_slide(session_dir: Path, position: int, slot: Dict) -> Tuple[bool, str]:
    """写入一页：position 在 1..N 内为替换，N+1（或 0/缺省）为追加。含视觉路由解析。"""
    from ..agents.chart_tools import ChartSpecError
    from .visual_router import resolve_visuals

    deck = load_deck(session_dir)
    if deck is None:
        return False, "No deck in this session yet — call deck_init first."

    n = len(deck["slides"])
    pos = int(position or 0)
    if pos == 0:
        pos = n + 1
    if not (1 <= pos <= n + 1):
        return False, f"position {pos} out of range: the deck has {n} slide(s); " \
                      f"use 1..{n} to replace or {n + 1} (or omit) to append."
    if pos > MAX_SLIDES:
        return False, f"deck is at the {MAX_SLIDES}-slide limit."

    replacing = pos <= n
    if replacing and deck["slides"][pos - 1].get("lock", {}).get("locked"):
        return False, (f"slide {pos} is LOCKED (finalized by the user) and cannot be modified. "
                       "Leave it as is, or ask the user to unlock it.")

    try:
        slide = deck_spec.expand_slide(slot)
        notes = resolve_visuals(deck, slide, session_dir / "assets")
    except (deck_spec.DeckSpecError, ChartSpecError) as e:
        return False, f"Slide spec error (fix the arguments and retry): {e}"

    if replacing:
        slide["id"] = deck["slides"][pos - 1]["id"]  # 替换保 id 稳定（锁页/diff/评审定位依赖）
        deck["slides"][pos - 1] = slide
    else:
        deck["slides"].append(slide)
    save_deck(session_dir, deck)

    # 回执里明示下一步落点：小模型的复读吸引子实测会原样重发上一调用（被去重拦截后
    # 仍不变参），把「下一页的 position 与要求不同内容」写进成功回执能显著打断该模式。
    total = len(deck["slides"])
    msg = f"Slide {pos} {'replaced' if replacing else 'added'}: " \
          + deck_spec.slide_summary(slide, pos)
    if notes:
        msg += "\nNote: " + " ".join(notes)
    msg += (f"\nDeck now has {total} slide(s). To add the next slide, call deck_set_slide "
            f"with position {total + 1} and DIFFERENT content (this exact call cannot be "
            f"repeated). To revise an existing slide, call deck_set_slide with its position "
            f"(1..{total}) and the complete new content — the deck only changes through these "
            "calls. When the deck is complete, tell the user it is ready.")
    return True, msg


def remove_slide(session_dir: Path, position: int) -> Tuple[bool, str]:
    deck = load_deck(session_dir)
    if deck is None:
        return False, "No deck in this session yet."
    n = len(deck["slides"])
    if not (1 <= int(position) <= n):
        return False, f"position {position} out of range (deck has {n} slide(s))."
    if deck["slides"][position - 1].get("lock", {}).get("locked"):
        return False, f"slide {position} is LOCKED (finalized by the user) and cannot be removed."
    removed = deck["slides"].pop(position - 1)
    save_deck(session_dir, deck)
    return True, (f"Slide {position} removed ({deck_spec.slide_summary(removed, position)}). "
                  f"{len(deck['slides'])} slide(s) remain.")


def outline(session_dir: Path) -> str:
    deck = load_deck(session_dir)
    if deck is None:
        return "No deck in this session yet — call deck_init to start one."
    return deck_spec.deck_outline(deck)
