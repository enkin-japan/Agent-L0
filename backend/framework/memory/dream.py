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
REVIEW_FILE = ".review_stamps.json"   # 每条笔记的 last_reviewed_at（P2a；P2b 巡检轮转用）
BACKUP_DIR = ".dream_backup"
RESCUE_MARK = "自动抢救"      # 与 compactor._flush_rescued_notes 的出处标记同一字面
STALE_MARK = "[待确认]"       # P2c：巡检发现证据冲突时的机械标记（只标不改内容）
MAX_OPS = 6
_BODY_SNIPPET = 400
_EVIDENCE_SNIPPET = 240
_CONTENT_CAP = 8000

DREAM_INSTRUCTION = (
    "You are tidying your own long-term notebook. Below are your notes "
    "(NAME / TYPE / DESCRIPTION / BODY, some with EVIDENCE recalled from your "
    "conversation archive). Identify ONLY clear problems: an "
    "auto-rescued note (body contains '自动抢救') whose fact is already covered "
    "by another note; two notes stating the same fact; a note whose wording is "
    "vague or uses relative dates; a note whose EVIDENCE clearly contradicts or "
    "supersedes it. Output one operation per line, at most "
    f"{MAX_OPS}, chosen from exactly these three forms:\n"
    "DROP: name | why it is redundant\n"
    "REWRITE: name | user|feedback|project|reference | one-line description | "
    "single-line body with absolute dates\n"
    "STALE: name | which EVIDENCE contradicts it\n"
    "Use STALE ONLY when the attached EVIDENCE contradicts the note — it flags "
    "the note for the user to confirm; NEVER rewrite a fact's content without "
    "evidence. Never invent new note names. If the notebook is already clean, "
    "output exactly: CLEAN\nOutput nothing else."
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


# ---------- 巡检戳（P2a）----------
# 为什么不写进笔记 frontmatter：闸门用文件 mtime 判「变动」，往笔记文件里盖戳会把
# 「看过了」伪装成「改过了」，喂假信号给自己的闸门。旁车 JSON 与 .dream_stamp 同哲学。

def read_review_stamps(store) -> Dict[str, float]:
    """读全部笔记的 last_reviewed_at（name → epoch）。缺档/坏档 = 全部视为从未巡检。"""
    try:
        raw = json.loads((store.notes_dir / REVIEW_FILE).read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in raw.items()}
    except Exception:
        return {}


def stamp_reviewed(store, names: List[str]) -> None:
    """给 names 盖巡检戳（=now）；顺带清掉已删除笔记的孤儿戳。fail-open。"""
    if not names:
        return
    stamps = read_review_stamps(store)
    now = time.time()
    for n in names:
        stamps[n] = now
    alive = set(store.names())
    stamps = {k: v for k, v in stamps.items() if k in alive}
    try:
        store.notes_dir.mkdir(parents=True, exist_ok=True)
        (store.notes_dir / REVIEW_FILE).write_text(
            json.dumps(stamps, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[Dream] 巡检戳写入失败（下次重巡，不致命）: {e}")


# ---------- 输入装配 / 输出解析 / 应用 ----------

REVIEW_WINDOW = 12   # 每梦巡检名额：窗口有界，靠盖戳轮转保证每条笔记总会轮到


def _note_mtime(store, name: str) -> float:
    try:
        note = store.get(name)
        if note is not None and note.source:
            return note.source.stat().st_mtime
    except OSError:
        pass
    return 0.0


def select_review_window(store) -> List[str]:
    """巡检窗口（P2b）：①改动过（mtime > 巡检戳，含从未巡检）优先；②余下名额给
    巡检戳最旧的——旧版「通读全本 + 8k 截断」让尾部笔记永远轮不到，轮转修掉它。"""
    stamps = read_review_stamps(store)
    names = list(store.names())
    changed = [n for n in names if _note_mtime(store, n) > stamps.get(n, 0.0)]
    changed.sort(key=lambda n: stamps.get(n, 0.0))
    rest = sorted((n for n in names if n not in set(changed)),
                  key=lambda n: stamps.get(n, 0.0))
    return (changed + rest)[:REVIEW_WINDOW]


def _render_notes(store, window: List[str], evidence: Dict[str, str] = None) -> str:
    """把巡检窗口铺成模型可读清单；超预算时优先保「自动抢救」条目（截断兜底）。"""
    blocks: List[str] = []
    evidence = evidence or {}
    rescued_first = sorted(
        window,
        key=lambda n: 0 if RESCUE_MARK in (store.get(n).body or "") else 1)
    for name in rescued_first:
        note = store.get(name)
        body = (note.body or "").strip()
        if len(body) > _BODY_SNIPPET:
            body = body[:_BODY_SNIPPET] + "…"
        block = (f"NAME: {note.name}\nTYPE: {note.type}\n"
                 f"DESCRIPTION: {note.description}\nBODY: {body}")
        if evidence.get(name):
            block += f"\nEVIDENCE: {evidence[name]}"
        blocks.append(block)
        if sum(len(b) + 2 for b in blocks) > _CONTENT_CAP:
            blocks.append("(…notebook truncated for length; tidy what you see)")
            break
    return "\n\n".join(blocks)


def _gather_evidence(store, window: List[str]) -> Dict[str, str]:
    """机械层为窗口内笔记召回对话记录证据（P2c）。fail-open：召回不到 = 无证据。"""
    out: Dict[str, str] = {}
    for name in window:
        note = store.get(name)
        if note is None:
            continue
        frags = rag_sink.recall_memory(
            f"{note.description} {(note.body or '')[:160]}")
        if frags:
            out[name] = " / ".join(f[:_EVIDENCE_SNIPPET] for f in frags)
    return out


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
        elif ln.upper().startswith("STALE:"):
            parts = [p.strip() for p in ln[6:].split("|", 1)]
            if len(parts) == 2 and all(parts):
                ops.append(("stale", parts))
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
        elif kind == "stale":
            # 只标不改（P2c 红线：无证据不许改事实，有证据也只到「请用户确认」为止）：
            # 机械前缀打在 description 上——笔记索引注入提示词，agent 下轮对话自然看见
            if (note.description or "").startswith(STALE_MARK):
                continue  # 已标过，幂等
            _backup(note, backup_dir)
            try:
                store.write(name, f"{STALE_MARK} {note.description}",
                            note.type, note.body or "", valid_until=note.valid_until)
                applied += 1
            except Exception as e:
                print(f"[Dream] 标记 {name!r} 失败（跳过）: {e}")
        else:  # rewrite
            _, type_, desc, body = parts
            if type_ not in NOTE_TYPES:
                continue
            _backup(note, backup_dir)
            try:
                store.write(name, desc, type_, body, valid_until=note.valid_until)
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
    window = select_review_window(store)
    print(f"[Dream] 闸门放行（{why}），巡检窗口 {len(window)} 条")
    evidence = _gather_evidence(store, window)
    content = _render_notes(store, window, evidence)
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
    stamp_reviewed(store, window)            # 窗口内的笔记「看过了」，轮转前进
    _write_stamp(store.notes_dir, applied)   # CLEAN / 无操作也盖：看过了就消耗配额
    print(f"[Dream] 整理完成：巡检 {len(window)} 条，应用 {applied} 项操作")
    return applied
