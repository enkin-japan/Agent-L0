"""文件写入/编辑工具：把「改文件」从 shell heredoc 通道里解放出来。

动机（实测事故）：工具层此前没有文件写入工具，模型只能走 run_shell_command 的
`cat > file <<EOF` ——文件内容要经受 JSON 字符串 + shell 双层转义，长内容必崩：
llguidance 对长字符串字面量 ParserTooComplex fail-open，之后自由采样混入裸换行
→ JSON 非法 → 任务失败（模型只好把代码贴聊天里让用户手抄）。

设计原则（弱模型友好）：
- edit_file 精确替换是主通道：每次调用只携带几百字符，转义面小、约束采样不炸、
  审批卡片上用户能直接看清改动；大改动应拆成多次 edit_file 小步走。
- write_file 只用于新建文件/彻底重写；覆盖已有文件前自动备份 path.bak。
- 两者一律需审批（requires_approval=True）：走既有审批闸门，定时任务无人值守
  时被自动拒绝，不新增免审批写盘面。
- 原子写：tmp + os.replace，同目录保证不跨设备；写坏进程也不会留半截文件。
"""
import os
import re
import shutil
import tempfile
from typing import Optional

from ..base import BaseTool

_MAX_CONTENT_CHARS = 2_000_000  # 单次写入上限（防失控生成把磁盘当输出流）

# file_extract 输出的行号前缀（如 「   14 | const x = 1」）。实测弱模型会把它连行号
# 一起「逐字拷贝」进 old_string——直接匹配必失败。剥离规则须与 file_extract 的
# 渲染格式对齐；只在直接匹配失败后作为回退启用，不影响正常路径。
_LINENO_PREFIX = re.compile(r"^\s{0,8}\d{1,6} \| ?")


def _strip_line_numbers(s: str) -> str:
    """剥离每行开头的 file_extract 行号前缀；仅当多数行确实带前缀时才认定是行号拷贝。"""
    lines = s.split("\n")
    stripped = [_LINENO_PREFIX.sub("", ln, count=1) for ln in lines]
    n_hit = sum(1 for a, b in zip(lines, stripped) if a != b)
    if n_hit == 0 or n_hit < max(1, len(lines) // 2):
        return s
    return "\n".join(stripped)


def _check_path(path: str, must_exist: bool) -> Optional[str]:
    """路径预检：返回错误消息（None = 通过）。错误消息面向模型，指明如何改正。"""
    if not path or not str(path).strip():
        return "System Error: 'path' is empty."
    p = os.path.expanduser(str(path))
    if not os.path.isabs(p):
        return (f"System Error: path must be absolute (got {path!r}). "
                "Prepend the full directory, e.g. /Users/name/project/file.txt")
    if must_exist and not os.path.isfile(p):
        return (f"System Error: file does not exist: {p}. "
                "Check the path with run_shell_command (ls), or use write_file to create a new file.")
    if os.path.isdir(p):
        return f"System Error: {p} is a directory, not a file."
    return None


def _read_text(path: str) -> str:
    """读入 UTF-8 文本；非 UTF-8 抛 UnicodeDecodeError 由调用方转成可读报错。"""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _atomic_write(path: str, content: str):
    """同目录 tmp + os.replace 原子落盘；保留原文件权限位。"""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_file_edit_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class EditFileTool(BaseTool):
    """精确文本替换（唯一命中才执行）——修改已有文件的首选通道。"""
    name = "edit_file"
    description = (
        "Edit an existing text file by exact string replacement: old_string is replaced with "
        "new_string. old_string must match EXACTLY ONE location in the file (copy it verbatim "
        "from the file, including whitespace and line breaks; extend it with surrounding lines "
        "until it is unique). Keep each edit SMALL — old_string should be at most ~10 lines; "
        "for a big change, make a SERIES of small edit_file calls, one per spot. Never include "
        "line-number prefixes like '  14 | ' in old_string — they are display decoration, not "
        "file content. This is the PREFERRED way to modify files; NEVER write files via shell "
        "heredocs (cat <<EOF). The user approves each edit before it is applied."
    )
    requires_approval = True
    parameters = {
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path of the file to edit."
            },
            "old_string": {
                "type": "string",
                "description": "The exact text to replace (verbatim from the file, must be unique in it)."
            },
            "new_string": {
                "type": "string",
                "description": "The replacement text (empty string deletes old_string)."
            },
        },
        "required": ["path", "old_string", "new_string"]
    }

    def execute(self, path: str, old_string: str, new_string: str = "") -> str:
        err = _check_path(path, must_exist=True)
        if err:
            return err
        p = os.path.expanduser(str(path))
        old_string = str(old_string)
        new_string = str(new_string)
        if not old_string:
            return ("System Error: 'old_string' is empty. To create or fully rewrite a file, "
                    "use write_file instead.")
        if old_string == new_string:
            return "System Error: old_string and new_string are identical — nothing to change."

        try:
            text = _read_text(p)
        except UnicodeDecodeError:
            return f"System Error: {p} is not a UTF-8 text file; edit_file only handles text."
        except Exception as e:
            return f"System Error: failed to read {p}: {e}"

        count = text.count(old_string)
        if count == 0:
            # 回退：模型常把 file_extract 的行号前缀（「  14 | 」）一起拷进来——剥掉重试
            cleaned = _strip_line_numbers(old_string)
            if cleaned != old_string and text.count(cleaned) == 1:
                old_string = cleaned
                new_string = _strip_line_numbers(new_string)
                count = 1
            else:
                return ("System Error: old_string was NOT found in the file. It must match the "
                        "file content exactly (including spaces, indentation and line breaks). "
                        "If you copied it from file_extract output, drop the line-number "
                        "prefixes ('  14 | ') — they are NOT part of the file. Re-read the file "
                        "and copy the text verbatim.")
        if count > 1:
            return (f"System Error: old_string matches {count} locations — refusing an ambiguous "
                    "edit. Extend old_string with a few surrounding lines so it is unique.")

        new_text = text.replace(old_string, new_string, 1)
        try:
            _atomic_write(p, new_text)
        except Exception as e:
            return f"System Error: failed to write {p}: {e}"

        delta = len(new_text) - len(text)
        return (f"OK: replaced 1 occurrence in {p} "
                f"({len(old_string)} chars -> {len(new_string)} chars, file size {delta:+d}).")


class WriteFileTool(BaseTool):
    """整文件写入（新建/彻底重写）；覆盖已有文件前自动备份 .bak。"""
    name = "write_file"
    description = (
        "Create a new text file or completely overwrite an existing one with the given content. "
        "Use this ONLY for new files or full rewrites — to change part of an existing file, use "
        "edit_file with a small unique snippet instead (smaller, safer, easier to review). "
        "NEVER write files via shell heredocs (cat <<EOF). If the file already exists, a backup "
        "is saved as <path>.bak before overwriting. The user approves each write."
    )
    requires_approval = True
    parameters = {
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path of the file to write. Parent directories are created if missing."
            },
            "content": {
                "type": "string",
                "description": "The complete file content to write (UTF-8 text)."
            },
        },
        "required": ["path", "content"]
    }

    def execute(self, path: str, content: str = "") -> str:
        err = _check_path(path, must_exist=False)
        if err:
            return err
        p = os.path.expanduser(str(path))
        content = str(content)
        if len(content) > _MAX_CONTENT_CHARS:
            return (f"System Error: content too large ({len(content)} chars, limit "
                    f"{_MAX_CONTENT_CHARS}). Write the file in parts: write_file the first part, "
                    "then append the rest with edit_file.")

        backed_up = False
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if os.path.isfile(p):
                shutil.copy2(p, p + ".bak")
                backed_up = True
            _atomic_write(p, content)
        except Exception as e:
            return f"System Error: failed to write {p}: {e}"

        note = f" (previous version backed up to {p}.bak)" if backed_up else ""
        return f"OK: wrote {len(content)} chars to {p}{note}."
