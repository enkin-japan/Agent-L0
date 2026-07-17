"""dream 式笔记整理（grok-build 评估 §3.2 采纳，2026-07-18）。

R4 笔记只增不理：memory_write 只写、压缩冲洗只增，写多必碎片化冗余。本模块
在空闲维护回合里低频触发一次「整理梦」——模型通读笔记本，输出一小组保守操作
（丢弃冗余的自动抢救笔记 / 改写含糊或相对时间的笔记），机械层校验后落盘。

安全设计（护栏宁硬勿软）：
- **三重闸**（照 grok dream 的形状，便宜的先查）：开关 → 距上次整理 ≥ min_hours
  → 变动笔记数 ≥ min_new_notes。整理完成即盖时间戳（`.dream_stamp.json`），
  无操作也盖——「看过了没问题」同样消耗一次配额。
- **DROP 只许删自动抢救笔记**（body 含 RESCUE_MARK）：手写笔记是 agent 深思的
  产出，模型整理回合只能改写（REWRITE）不能删；改写前原文件备份进
  `.dream_backup/<时间戳>/`。操作上限 6 条/次。
- **只在主编排环境跑**（笔记是个人事实，红线同 notes.py）；引擎侧走
  oneshot_instruction（隔离缓存 + 推理锁），app 侧只在无会话忙碌时调用。
- RAG 镜像：REWRITE 后重新 sediment_note；DROP 不清 RAG（sink 是只增史料，
  文件才是真相——与 rag_sink 头注的定位一致）。
"""
import json
import shutil
import time
from pathlib import Path
from typing import Dict, List, Tuple

from . import rag_sink
from .notes import NOTE_TYPES

STAMP_FILE = ".dream_stamp.json"
BACKUP_DIR = ".dream_backup"
RESCUE_MARK = "自动抢救"      # 与 compactor._flush_rescued_notes 的出处标记同一字面
MAX_OPS = 6
_BODY_SNIPPET = 400
_CONTENT_CAP = 8000

DREAM_INSTRUCTION = (
    "You are tidying your own long-term notebook. Below are your notes "
    "(NAME / TYPE / DESCRIPTION / BODY). Identify ONLY clear problems: an "
    "auto-rescued note (body contains '自动抢救') whose fact is already covered "
    "by another note; two notes stating the same fact; a note whose wording is "
    "vague or uses relative dates. Output one operation per line, at most "
    f"{MAX_OPS}, chosen from exactly these two forms:\n"
    "DROP: name | why it is redundant\n"
    "REWRITE: name | user|feedback|project|reference | one-line description | "
    "single-line body with absolute dates\n"
    "Never invent new note names. If the notebook is already clean, output "
    "exactly: CLEAN\nOutput nothing else."
)


# ---------- 闸门 ----------

def _read_stamp(notes_dir: Path) -> Dict:
    try:
        return json.loads((notes_dir / STAMP_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_stamp(notes_dir: Path, applied: int) -> None:
    try:
        notes_dir.mkdir(parents=True, exist_ok=True)
        (notes_dir / STAMP_FILE).write_text(
            json.dumps({"last_at": time.time(), "applied": applied}),
            encoding="utf-8")
    except Exception as e:
        print(f"[Dream] 时间戳写入失败（下次会提前重试）: {e}")


def check_gates(store, cfg: Dict) -> Tuple[bool, str]:
    """三重闸，便宜的先查。返回 (是否放行, 说明)。"""
    if not cfg.get("enabled", True):
        return False, "disabled"
    notes_dir = store.notes_dir
    if not notes_dir.exists():
        return False, "no-notes-dir"
    last_at = float(_read_stamp(notes_dir).get("last_at", 0))
    min_hours = float(cfg.get("min_hours", 24))
    if (time.time() - last_at) < min_hours * 3600:
        return False, "too-soon"
    changed = 0
    for f in notes_dir.glob("*.md"):
        if f.name == "MEMORY.md":
            continue
        try:
            if f.stat().st_mtime > last_at:
                changed += 1
        except OSError:
            continue
    min_new = int(cfg.get("min_new_notes", 4))
    if changed < min_new:
        return False, f"too-few-changes({changed}/{min_new})"
    return True, f"open({changed} changed)"


# ---------- 输入装配 / 输出解析 / 应用 ----------

def _render_notes(store) -> str:
    """把笔记本铺成模型可读清单；超预算时优先保「自动抢救+改动嫌疑」的条目。"""
    blocks: List[str] = []
    rescued_first = sorted(
        store.names(),
        key=lambda n: 0 if RESCUE_MARK in (store.get(n).body or "") else 1)
    for name in rescued_first:
        note = store.get(name)
        body = (note.body or "").strip()
        if len(body) > _BODY_SNIPPET:
            body = body[:_BODY_SNIPPET] + "…"
        blocks.append(f"NAME: {note.name}\nTYPE: {note.type}\n"
                      f"DESCRIPTION: {note.description}\nBODY: {body}")
        if sum(len(b) + 2 for b in blocks) > _CONTENT_CAP:
            blocks.append("(…notebook truncated for length; tidy what you see)")
            break
    return "\n\n".join(blocks)


def parse_ops(text: str) -> List[Tuple[str, List[str]]]:
    """解析操作行（cap MAX_OPS）。返回 [("drop", [name, why]) | ("rewrite", [name, type, desc, body])]。"""
    ops: List[Tuple[str, List[str]]] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln.upper().startswith("DROP:"):
            parts = [p.strip() for p in ln[5:].split("|", 1)]
            if len(parts) == 2 and parts[0]:
                ops.append(("drop", parts))
        elif ln.upper().startswith("REWRITE:"):
            parts = [p.strip() for p in ln[8:].split("|", 3)]
            if len(parts) == 4 and all(parts):
                ops.append(("rewrite", parts))
        if len(ops) >= MAX_OPS:
            break
    return ops


def apply_ops(store, ops: List[Tuple[str, List[str]]]) -> int:
    """机械校验后应用操作。所有被动过的笔记先备份原文件。返回应用条数。"""
    if not ops:
        return 0
    backup_dir = store.notes_dir / BACKUP_DIR / time.strftime("%Y%m%d%H%M%S")
    applied = 0
    for kind, parts in ops:
        name = parts[0]
        note = store.get(name)
        if note is None:
            continue  # 模型幻觉出的名字：跳过（Never invent 的机械兜底）
        if kind == "drop":
            if RESCUE_MARK not in (note.body or ""):
                continue  # 手写笔记绝不许删
            _backup(note, backup_dir)
            if store.delete(name):
                applied += 1
        else:  # rewrite
            _, type_, desc, body = parts
            if type_ not in NOTE_TYPES:
                continue
            _backup(note, backup_dir)
            try:
                store.write(name, desc, type_, body)
            except Exception as e:
                print(f"[Dream] 改写 {name!r} 失败（跳过）: {e}")
                continue
            try:
                rag_sink.sediment_note(name, desc, type_, body)
            except Exception:
                pass
            applied += 1
    return applied


def _backup(note, backup_dir: Path) -> None:
    try:
        if note.source and note.source.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(note.source, backup_dir / note.source.name)
    except Exception as e:
        print(f"[Dream] 备份 {note.name} 失败: {e}")


# ---------- 入口 ----------

async def maybe_dream(engine, cfg: Dict) -> int:
    """闸门全过则跑一次整理梦。返回应用的操作数（0=未触发/无操作/失败）。fail-open。"""
    from ..agents import runtime  # 惰性导入避免 memory↔agents 环
    store = runtime.get_note_store()
    if store is None:
        return 0
    ok, why = check_gates(store, cfg)
    if not ok:
        return 0
    print(f"[Dream] 闸门放行（{why}），开始整理笔记本")
    content = _render_notes(store)
    if not content.strip():
        _write_stamp(store.notes_dir, 0)
        return 0
    try:
        out = await engine.oneshot_instruction(
            DREAM_INSTRUCTION, content, max_tokens=512, temperature=0.2)
    except Exception as e:
        print(f"[Dream] 生成失败（下次再试，不盖时间戳）: {e}")
        return 0
    if not (out or "").strip():
        return 0  # 空输出（被抢占/异常）：不盖时间戳，下次再试
    applied = apply_ops(store, parse_ops(out))
    _write_stamp(store.notes_dir, applied)   # CLEAN / 无操作也盖：看过了就消耗配额
    print(f"[Dream] 整理完成：应用 {applied} 项操作")
    return applied
