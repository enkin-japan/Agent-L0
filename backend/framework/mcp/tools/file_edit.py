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
import difflib
import os
import re
import shutil
import subprocess
import tempfile
from typing import Optional

from ..base import BaseTool


# ── 写后语法自检（D2 谎报围堵 #84，2026-08-10 实案：write_file 落盘的 index.js 尾部
# 混入工具调用 JSON 垃圾，npm start 一跑才炸）。写入不阻断——文件已落盘、备份在，
# 但响应里响亮告知语法问题，让模型当场看见而不是等运行时。尽力而为：解释器/检查器
# 不在场就沉默跳过，绝不把「没装 node」当成语法错误。
_CHECKABLE = {".json", ".py", ".yaml", ".yml", ".sh", ".bash", ".zsh",
              ".js", ".mjs", ".cjs"}


def _syntax_note(path: str) -> Optional[str]:
    """返回警示串（含错误摘要）；None = 通过或不可检。"""
    ext = os.path.splitext(path)[1].lower()
    if ext not in _CHECKABLE:
        return None
    try:
        if ext == ".json":
            import json as _json
            _json.loads(open(path, encoding="utf-8").read())
            return None
        if ext in (".yaml", ".yml"):
            import yaml as _yaml
            _yaml.safe_load(open(path, encoding="utf-8").read())
            return None
        if ext == ".py":
            import py_compile
            py_compile.compile(path, doraise=True)
            return None
        cmd = None
        if ext in (".sh", ".bash", ".zsh"):
            cmd = ["bash", "-n", path]
        elif ext in (".js", ".mjs", ".cjs"):
            if shutil.which("node"):
                cmd = ["node", "--check", path]
        if cmd:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return (r.stderr or r.stdout or "syntax check failed").strip()[:300]
        return None
    except subprocess.TimeoutExpired:
        return None                      # 检查器自身卡死不当语法错
    except Exception as e:               # json/yaml/py_compile 的解析异常 = 真语法错
        if type(e).__name__ in ("JSONDecodeError", "YAMLError", "PyCompileError",
                                "ParserError", "ScannerError"):
            return str(e)[:300]
        return None                      # 其余环境性异常（权限/编码等）不误报


def _syntax_suffix(path: str) -> str:
    note = _syntax_note(path)
    if note:
        return (f"\n⚠️ SYNTAX CHECK FAILED for {os.path.basename(path)} — the file was written "
                f"but will not run/parse as-is. Fix it before claiming success. Error: {note}")
    return ""

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


# 匹配失败时回喂真实文本的相似度门槛与扫描规模上限。
# 门槛偏高（0.6）：宁可不给候选，也不给一段误导的文本让模型照抄错的。
_FUZZY_MIN_RATIO = 0.6
_FUZZY_MAX_SCAN_LINES = 4000  # 超大文件跳过模糊搜索，控制单次工具延迟（钝器优先于慢）


def _closest_snippet(text: str, old_string: str) -> Optional[str]:
    """old_string 精确匹配失败时，在文件里找与它最相似的一段**真实文本**回喂给模型。

    动机：弱模型复现不出逐字原文（把 onclick="addRow" 记成 onclick="add"），只回
    「没找到，重新读文件」是把可自纠的错误变成死胡同。返回文件里最接近的原文（含精确
    空白），模型下一步就有了逐字正确的锚点。相似度过低或文件过大则返回 None（不猜、
    不误导）。按目标行数及其邻域（模型常多算/少算一行）滑窗，SequenceMatcher 计分。"""
    file_lines = text.splitlines()
    if not file_lines or len(file_lines) > _FUZZY_MAX_SCAN_LINES:
        return None
    target = old_string.strip("\n")
    if not target:
        return None
    n = len(target.splitlines()) or 1
    sizes = {s for s in (n - 1, n, n + 1) if s >= 1}
    # 固定序列设为 seq2：其内部 b2j 链只算一次，跨 set_seq1 复用（滑窗更快）
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(target)
    best_ratio = 0.0
    best_span: Optional[tuple] = None
    for size in sizes:
        for start in range(0, len(file_lines) - size + 1):
            window = "\n".join(file_lines[start:start + size])
            sm.set_seq1(window)
            if sm.quick_ratio() <= best_ratio:  # 上界剪枝：先廉价筛，命中才精算
                continue
            r = sm.ratio()
            if r > best_ratio:
                best_ratio = r
                best_span = (start, size)
    if best_span is None or best_ratio < _FUZZY_MIN_RATIO:
        return None
    start, size = best_span
    return "\n".join(file_lines[start:start + size])


def _not_found_feedback(text: str, old_string: str) -> str:
    """old_string 未命中时的可执行反馈：尽量附上文件里最接近的真实文本供逐字照抄。"""
    snippet = _closest_snippet(text, old_string)
    base = ("System Error: old_string was NOT found in the file. It must match the file "
            "content exactly (including spaces, indentation and line breaks). If you copied "
            "it from file_extract output, drop the line-number prefixes ('  14 | ') — they "
            "are NOT part of the file.")
    if snippet is None:
        return base + " Re-read the file and copy the text verbatim."
    return (base + " The closest actual text in the file is shown between the markers below — "
            "copy it VERBATIM as your old_string (and adjust new_string accordingly), or "
            "extend it with surrounding lines if you need a larger/unique match:\n"
            "<<<<<<< closest actual file text\n"
            f"{snippet}\n"
            ">>>>>>> end")


# 相对路径落点 = 会话工作区（D1 实测：剧本让「写进 workspace」，工具侧却只收绝对
# 路径——模型幻觉出 /workspace/ 撞上只读根目录 Errno 30。落点与 render_chart 同范式：
# sessions/{sid}/assets/，经 /api/v1/files 出货，用户可点击下载）。
# 文件名字符集与出货路由的 _ASSET_NAME_RE 对齐，否则写得进、取不出。
_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _resolve_path(path: str, must_exist: bool):
    """路径解析：返回 (绝对路径, 出货URL或None, 错误消息或None)。

    绝对路径/~ 开头：原语义不变。相对路径：视为会话工作区文件名，落
    sessions/{sid}/assets/——这是模型产出交付物（报告/导出）的家。
    """
    if not path or not str(path).strip():
        return None, None, "System Error: 'path' is empty."
    p = os.path.expanduser(str(path))
    url = None
    if not os.path.isabs(p):
        from ...agents import runtime
        session = runtime.get_current_session()
        if session is None:
            return None, None, ("System Error: relative paths need a session workspace, which is "
                                "unavailable here. Use an absolute path instead.")
        name = str(path).strip()
        if not _WORKSPACE_NAME_RE.match(name) or ".." in name:
            return None, None, (f"System Error: {path!r} is not a valid workspace filename. "
                                "Use a plain filename like report-topic.md (letters, digits, . _ -), "
                                "or an absolute path for locations outside the workspace.")
        assets = session.session_dir / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        p = str(assets / name)
        url = f"/api/v1/files/{session.session_id}/assets/{name}"
    if must_exist and not os.path.isfile(p):
        return None, None, (f"System Error: file does not exist: {p}. "
                            "Check the path with run_shell_command (ls), or use write_file to create a new file.")
    if os.path.isdir(p):
        return None, None, f"System Error: {p} is a directory, not a file."
    return p, url, None


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
        "file content. This is the PREFERRED way to modify files. The user approves each edit "
        # D2-减法(类B):heredoc 禁令三副本去重,主本留 run_shell_command(事故发生地)。
        "before it is applied."
    )
    requires_approval = True
    parameters = {
        "properties": {
            "path": {
                "type": "string",
                "description": ("Path of the file to edit: absolute for real filesystem locations, "
                                "or a plain filename (e.g. report.md) for the session workspace.")
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
        p, _url, err = _resolve_path(path, must_exist=True)
        if err:
            return err
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
                return _not_found_feedback(text, old_string)
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
                f"({len(old_string)} chars -> {len(new_string)} chars, file size {delta:+d})."
                + _syntax_suffix(p))


class WriteFileTool(BaseTool):
    """整文件写入（新建/彻底重写）；覆盖已有文件前自动备份 .bak。"""
    name = "write_file"
    description = (
        "Create a new text file or completely overwrite an existing one with the given content. "
        "Use this ONLY for new files or full rewrites — to change part of an existing file, use "
        "edit_file with a small unique snippet instead (smaller, safer, easier to review). "
        # D2-减法(类B):heredoc 禁令副本删,主本在 run_shell_command。
        "If the file already exists, a backup "
        "is saved as <path>.bak before overwriting. The user approves each write."
    )
    requires_approval = True
    parameters = {
        "properties": {
            "path": {
                "type": "string",
                "description": ("Path to write: a plain filename (e.g. report-topic.md) lands in the "
                                "session workspace and returns a download link for the user — use this "
                                "for reports/deliverables. Absolute paths write to the real filesystem "
                                "(parent directories created if missing).")
            },
            "content": {
                "type": "string",
                "description": "The complete file content to write (UTF-8 text)."
            },
        },
        "required": ["path", "content"]
    }

    def execute(self, path: str, content: str = "") -> str:
        p, url, err = _resolve_path(path, must_exist=False)
        if err:
            return err
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
        if url:
            # 工作区交付物：把可点击的下载链接交回——模型须把它原样放进回复
            #（与 render_chart 同约定；链接逐字来自这里，不许改写）。
            name = os.path.basename(p)
            return (f"OK: wrote {len(content)} chars to workspace file {name}{note}. "
                    f"Download link for your reply (copy verbatim): [{name}]({url})"
                    + _syntax_suffix(p))
        return f"OK: wrote {len(content)} chars to {p}{note}." + _syntax_suffix(p)
