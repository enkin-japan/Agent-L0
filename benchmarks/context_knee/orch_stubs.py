"""拐点 harness 编排器模式的生产工具**可用桩**（W-K2）：`--prefix prod|catalog` 下每个生产工具名一个
`UsableStub`，声明照生产完整 schema，执行是确定性的离线行为，不再回 `Unknown tool`。

四类行为（按工具名，`behaviour_kind`）：
1. vfs 桥接（题目 mock env 有 vfs 工具集时；否则落到第 4 类）：list_dir / find_files / edit_file /
   copy_file / move_file / trash_file / run_shell_command（只读 shell 仿真）/ file_extract 的非 spill
   路径（经 orch_tools.SpillReadTool 的 bridge）。一切**读**取 `env.snapshot()`（不消耗题目为模型自己的
   read_file 设计的故障配额）；只有写 / 删走 `env.handle`（write_file / delete_file，与 EnvTool 同一把锁；
   env 的只读路径、写侧故障配额照常生效）。路径是 vfs 相对键：前导 `~/`、`./`、`/` 剥掉。
2. 检索：rag_search 恒回生产「空库」原句（retrieval_evidence 判 empty），rag_get_context 回生产
   「无切片」原句（非失败、非证据 → empty）——检索护栏不会因桩挂起待决标记。
3. 计算 / 确定性状态：run_math 真算（ast 白名单四则 + 少数函数），get_current_time 固定时刻，
   system_status / calendar_list / schedule_list / deck_status / list_applications / read_ui_tree /
   clipboard_read 回生产同形的空 / 常态结果。
4. 离线不可用：其余工具回一句「本离线沙箱不可用、什么都没做」的正常文本（不伪造副作用成功）。

失败回执分两种：写类桩（edit_file / copy_file / move_file / trash_file）的失败与缺必填参数照生产
发 `System Error: …`（编排层判失败；计 stub_failed，不计 system_errors）；读类失败（找不到文件等）
用同一措辞的 `Error: …`（不判失败，同生产「没找到」这类正常否定答案的待遇差一档，见 README）；
shell 仿真的失败照生产 shell 的 `exit_code: N` 首行（N≠0 被编排层 D2 判失败；grep 无命中打良性标）。
离线不可用类一律 is_read_only=True：「什么都没做」不得被当成成功变更。
效果分类（effect / is_concurrency_safe / is_read_only）取生产工具类属性（不构造实例）；审批属性
（requires_approval / approval_pinned）不照搬：harness 进程内审批策略 = auto，钉死审批的生产工具
（shell / schedule_* / register_*）在无人值守的 run 里会挂成 pending_approval。
"""
from __future__ import annotations

import ast
import contextlib
import copy
import fnmatch
import importlib
import inspect
import io
import math
import pkgutil
import posixpath
import re
import shlex
import threading
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from framework.mcp.base import BaseTool

#: 桥接所需的 mock env 工具集（VfsEnv / OfficeEnv 都有）
VFS_TOOLS = frozenset({"list_files", "read_file", "write_file", "delete_file"})
#: 桩失败回执前缀（生产是 "System Error: "；见模块说明）
ERR = "Error: "
#: 写类桩（edit/copy/move/trash）失败与缺必填参数：照生产用 "System Error: "——编排层据此判失败
#: （否则一次失败的写会被当成成功变更，D2「零变更却声称已做」守门失效）；UsableStub.failed 计数，
#: traj_from_history 记 stub_failed 并从 system_errors 里剔除
WERR = "System Error: "
#: 列目录 / 搜索结果里的固定 mtime（vfs 无时间戳）
MTIME = "2026-01-01"
#: get_current_time 的固定时刻（生产形：%Y-%m-%d <weekday> %H:%M:%S %Z (UTC%z)）
FIXED_TIME = "2026-01-01 Thursday 09:00:00 JST (UTC+0900)"

VFS_BRIDGED = frozenset({"list_dir", "find_files", "edit_file", "copy_file", "move_file",
                         "trash_file", "run_shell_command", "file_extract"})
RETRIEVAL = frozenset({"rag_search", "rag_get_context"})
COMPUTE = frozenset({"run_math", "get_current_time", "system_status", "calendar_list",
                     "schedule_list", "deck_status", "list_applications", "read_ui_tree",
                     "clipboard_read"})

#: 生产 rag_search 空库回执（rag_search.py「target store is empty or unavailable」分支，逐字）
RAG_EMPTY = ("No results for '{query}' in category '{category}': the target store is empty or "
             "unavailable — nothing was retrieved at all. Pick a different category, or the "
             "material may need to be ingested first.")
#: 生产 rag_get_context 无切片回执（服务端没给 msg 时的原句）
RAG_CONTEXT_EMPTY = "No chunks found for that path."

#: grep 仿真认的单字母选项（-A/-B/-C 带参、--long 一律拒）
GREP_FLAGS = frozenset("nirRcvlhEFw")
SHELL_SUPPORTED = ("ls", "cat", "grep", "find", "head", "tail", "wc", "echo", "pwd")
SHELL_NOTE = ("this offline sandbox emulates only read-only commands over the task's files: "
              "ls [path], cat <file>…, grep [-n] [-i] [-r] <pattern> <file|dir>…, "
              "find [dir] -name <glob>, head|tail -n N <file>, wc -l <file>, echo …, pwd — "
              "no pipes, redirects or command chaining. Use the file tools (list_files / "
              "read_file / write_file …) for anything else.")


# ---------------- 生产工具的效果分类 ----------------

_TRAIT_DEFAULT = {"effect": "write", "is_concurrency_safe": False, "is_read_only": None}


@lru_cache(maxsize=1)
def production_traits() -> Dict[str, Dict[str, Any]]:
    """{生产工具名: {effect, is_concurrency_safe, is_read_only}}——读类属性、不构造实例（与
    tool_token_audit.collect_summaries 同一密封枚举；rag_admin 同口径排除）。"""
    from context_knee import prefix as prefix_mod
    import framework.mcp.tools as tools_pkg
    audit = prefix_mod._audit()
    out: Dict[str, Dict[str, Any]] = {}
    # 模块 import 期的探活 / 剧本装载日志与本 harness 无关，吞掉
    with audit.hermetic_construction(), contextlib.redirect_stdout(io.StringIO()):
        for info in pkgutil.iter_modules(tools_pkg.__path__):
            if info.name == "rag_admin":
                continue
            try:
                mod = importlib.import_module(f"framework.mcp.tools.{info.name}")
            except Exception:       # noqa: BLE001 —— 与 collect_schemas 同：载不了的模块跳过
                continue
            for _attr, obj in inspect.getmembers(mod):
                if (inspect.isclass(obj) and issubclass(obj, BaseTool) and obj is not BaseTool
                        and isinstance(getattr(obj, "name", ""), str) and obj.name
                        and obj.name not in out):
                    ro = getattr(obj, "is_read_only", None)
                    out[obj.name] = {
                        "effect": "read" if getattr(obj, "effect", "write") == "read" else "write",
                        "is_concurrency_safe": getattr(obj, "is_concurrency_safe", False) is True,
                        "is_read_only": ro if isinstance(ro, bool) else None}
    return out


def traits_for(name: str) -> Dict[str, Any]:
    return dict(production_traits().get(name) or _TRAIT_DEFAULT)


# ---------------- vfs 桥 ----------------

def vfs_key(path: Any) -> str:
    """模型给的路径 → vfs 键：剥前导 `~/` `./` `/`，规范化 `a//b`、`a/./b`、尾斜杠；根 = ""。"""
    p = str(path if path is not None else "").strip()
    if p in ("", ".", "~", "/", "./", "~/"):
        return ""
    while True:
        for pre in ("~/", "./", "/"):
            if p.startswith(pre):
                p = p[len(pre):]
                break
        else:
            break
    p = posixpath.normpath(p) if p else ""
    return "" if p in (".", "/") else p.strip("/")


def _human_size(n: int) -> str:
    """file_nav._human_size 同式。"""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"


class VfsBridge:
    """一道题的 mock env（有 vfs 工具集时）上的文件操作；锁与 EnvTool 同一把。"""

    def __init__(self, env, lock: threading.Lock):
        self.env = env
        self.lock = lock

    @staticmethod
    def supports(env) -> bool:
        try:
            return env is not None and VFS_TOOLS <= {t["name"] for t in env.tools()}
        except Exception:       # noqa: BLE001
            return False

    def files(self) -> Dict[str, str]:
        """当前全部文件（快照，不经 handle、不消耗故障配额）。"""
        with self.lock:
            return dict(self.env.snapshot().get("files") or {})

    def _call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            return self.env.handle(name, args)

    def read(self, key: str) -> Tuple[Optional[str], Optional[str]]:
        """读快照（不经 handle：读侧故障配额留给模型自己的 read_file）。"""
        files = self.files()
        if key not in files:
            return None, f"File not found: {key}"
        return str(files[key]), None

    def write(self, key: str, content: str) -> Optional[str]:
        r = self._call("write_file", {"path": key, "content": content})
        return str(r["error"]) if "error" in r else None

    def delete(self, key: str) -> Optional[str]:
        r = self._call("delete_file", {"path": key})
        return str(r["error"]) if "error" in r else None

    @staticmethod
    def under(files: Sequence[str], key: str) -> List[str]:
        """key 是目录时其下全部文件（key == "" = 全部）。"""
        if not key:
            return sorted(files)
        return sorted(f for f in files if f.startswith(key + "/"))

    def is_dir(self, key: str, files: Optional[Sequence[str]] = None) -> bool:
        files = self.files() if files is None else files
        return key == "" or any(f.startswith(key + "/") for f in files)

    # ---- file_extract（非 spill 路径） ----

    def extract(self, path: Any, start: Any = None, end: Any = None, max_chars: Any = None) -> str:
        """生产 file_extract 文本分支（ingestion/parsers/text.read_text_range）同形输出。"""
        key = vfs_key(path)
        files = self.files()
        if key not in files:
            if self.is_dir(key, files) and key:
                return f"{ERR}{key} is a directory, not a file."
            return f"{ERR}file not found: {path}"
        text, err = self.read(key)
        if err is not None:
            return f"{ERR}failed to read {key}: {err}"
        return text_range(posixpath.basename(key), text, start, end, max_chars)


def _int_or_none(v: Any) -> Optional[int]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def text_range(name: str, text: str, start: Any = None, end: Any = None,
               max_chars: Any = None) -> str:
    """read_text_range 同式：`[<名> lines a-b of N]` + 行号前缀 `    1 | …`。"""
    s, e = _int_or_none(start), _int_or_none(end)
    cap = _int_or_none(max_chars) or 12000
    lines = text.splitlines()
    total = len(lines)
    lo = max(0, (s - 1) if s else 0)
    hi = min(total, e if e else total)
    if lo >= hi:
        return f"[Empty range: file has {total} lines, requested [{s}, {e}]]"
    body = "\n".join(f"{lo + i + 1:>5} | {line}" for i, line in enumerate(lines[lo:hi]))
    if len(body) > cap:
        body = body[:cap] + "\n[Truncated; narrow the line range]"
    return f"[{name} lines {lo + 1}-{hi} of {total}]\n{body}"


# ---------------- 第 1 类：vfs 桥接工具 ----------------

def _disp(key: str) -> str:
    return key or "."


def do_list_dir(bridge: VfsBridge, a: Dict[str, Any]) -> str:
    key = vfs_key(a.get("path"))
    files = bridge.files()
    if key in files:
        return (f"{ERR}{key} is a file, not a folder. "
                "Use file_extract to read it or open_path to open it.")
    below = bridge.under(files, key)
    if key and not below:
        return f"{ERR}path not found: {a.get('path')}"
    dirs, plain = set(), {}
    for f in below:
        rel = f[len(key) + 1:] if key else f
        head, _, rest = rel.partition("/")
        if rest:
            dirs.add(head)
        else:
            plain[head] = f
    if not a.get("show_hidden"):
        dirs = {d for d in dirs if not d.startswith(".")}
        plain = {n: f for n, f in plain.items() if not n.startswith(".")}
    entries = sorted([(0, d) for d in dirs] + [(1, n) for n in plain],
                     key=lambda e: (e[0], e[1].lower()))
    if not entries:
        return f"{_disp(key)}  (empty folder)"
    lines = [f"{_disp(key)}  ({len(entries)} entries)"]
    for kind, n in entries:
        if kind == 0:
            lines.append(f"  [dir]  {n}/")
        else:
            size = len(files[plain[n]].encode("utf-8"))
            lines.append(f"  {_human_size(size):>9}  {MTIME}  {n}")
    return "\n".join(lines)


def do_find_files(bridge: VfsBridge, a: Dict[str, Any]) -> str:
    query = str(a.get("query") or "").strip()
    if not query:
        return f"{ERR}Invalid arguments: 'query' must be a non-empty string."
    files = bridge.files()
    folder = a.get("folder")
    scope = sorted(files)
    if folder:
        fk = vfs_key(folder)
        if fk and not bridge.is_dir(fk, files):
            return f"{ERR}folder not found: {folder}"
        scope = bridge.under(files, fk)
    q = query.casefold()
    by_content = [f for f in scope if q in files[f].casefold()]
    if a.get("search_content"):
        hits, matched_by = by_content, "name"
    else:
        hits = [f for f in scope if q in posixpath.basename(f).casefold()]
        matched_by = "name"
        if not hits:
            hits, matched_by = by_content, "content"
    if not hits:
        where = f" in {folder}" if folder else ""
        return (f"No files matched {query!r}{where} by file name or content. Note: results "
                "are limited to folders this app may access — files on external volumes or "
                "protected folders stay invisible unless the app is granted Full Disk Access "
                "(System Settings → Privacy & Security). Try different keywords or list_dir "
                "a likely folder.")
    shown = hits[:30]
    note = " — no file NAME matched; these match by file CONTENT" if matched_by == "content" else ""
    lines = [f"{len(hits)} matches (showing {len(shown)}, newest first{note}):"]
    lines += [f"  {MTIME}  {_human_size(len(files[f].encode('utf-8'))):>9}  {f}" for f in shown]
    return "\n".join(lines)


def do_edit_file(bridge: VfsBridge, a: Dict[str, Any]) -> str:
    from framework.mcp.tools import file_edit as fe_real
    raw = str(a.get("path") or "").strip()
    if not raw:
        return f"{WERR}'path' is empty."
    key = vfs_key(raw)
    files = bridge.files()
    if key not in files:
        if key and bridge.is_dir(key, files):
            return f"{WERR}{key} is a directory, not a file."
        return (f"{WERR}file does not exist: {key or raw}. Check the path with run_shell_command "
                "(ls), or use write_file to create a new file.")
    old, new = str(a.get("old_string", "")), str(a.get("new_string", "") or "")
    if not old:
        return (f"{WERR}'old_string' is empty. To create or fully rewrite a file, "
                "use write_file instead.")
    if old == new:
        return f"{WERR}old_string and new_string are identical — nothing to change."
    text, err = bridge.read(key)
    if err is not None:
        return f"{WERR}failed to read {key}: {err}"
    count = text.count(old)
    if count == 0:
        cleaned = fe_real._strip_line_numbers(old)
        if cleaned != old and text.count(cleaned) == 1:
            old, new, count = cleaned, fe_real._strip_line_numbers(new), 1
        else:
            fb = fe_real._not_found_feedback(text, old)
            return fb if fb.startswith(WERR) else WERR + fb
    if count > 1:
        return (f"{WERR}old_string matches {count} locations — refusing an ambiguous "
                "edit. Extend old_string with a few surrounding lines so it is unique.")
    new_text = text.replace(old, new, 1)
    err = bridge.write(key, new_text)
    if err is not None:
        return f"{WERR}failed to write {key}: {err}"
    return (f"OK: replaced 1 occurrence in {key} "
            f"({len(old)} chars -> {len(new)} chars, file size {len(new_text) - len(text):+d}).")


def _sources(bridge: VfsBridge, raw: Any) -> Tuple[str, List[str], Dict[str, str]]:
    key = vfs_key(raw)
    files = bridge.files()
    if key and key in files:
        return key, [key], files
    return key, (bridge.under(files, key) if key else []), files


def _dest(src: str, raw: Any, files: Dict[str, str], bridge: VfsBridge) -> Tuple[str, Optional[str]]:
    """file_manage._resolve_dest 同语义：已存在目录 = 挪入其中；已存在 = 拒绝覆盖。vfs 目录是隐式的，
    目标父目录不必预先存在。"""
    dk = vfs_key(raw)
    if str(raw or "").rstrip().endswith("/") or (dk and bridge.is_dir(dk, files)) or not dk:
        dk = posixpath.join(dk, posixpath.basename(src)) if dk else posixpath.basename(src)
    if dk in files or bridge.under(files, dk):
        return dk, (f"destination already exists: {dk} — refusing to overwrite. "
                    "Pick another name, or trash/move the existing file first.")
    return dk, None


def _copy_like(bridge: VfsBridge, a: Dict[str, Any], *, move: bool) -> str:
    src, keys, files = _sources(bridge, a.get("source"))
    if not keys:
        return f"{WERR}source not found: {a.get('source')}"
    dst, err = _dest(src, a.get("destination"), files, bridge)
    if err:
        return WERR + err
    if dst == src or dst.startswith(src + "/"):
        return f"{WERR}cannot {'move' if move else 'copy'} {src} into itself"
    plan = []
    for k in keys:
        text, rerr = bridge.read(k)
        if rerr is not None:
            return f"{WERR}failed to read {k}: {rerr}"
        plan.append((k, dst if k == src else dst + k[len(src):], text))
    for _k, target, text in plan:
        werr = bridge.write(target, text)
        if werr is not None:
            return f"{WERR}failed to write {target}: {werr}"
    if move:
        for k, _t, _x in plan:
            derr = bridge.delete(k)
            if derr is not None:
                return f"{WERR}copied to {dst} but could not remove the source {k}: {derr}"
        return f"Moved {src} → {dst}"
    return f"Copied {src} → {dst}"


def do_copy_file(bridge: VfsBridge, a: Dict[str, Any]) -> str:
    return _copy_like(bridge, a, move=False)


def do_move_file(bridge: VfsBridge, a: Dict[str, Any]) -> str:
    return _copy_like(bridge, a, move=True)


def do_trash_file(bridge: VfsBridge, a: Dict[str, Any]) -> str:
    src, keys, _files = _sources(bridge, a.get("path"))
    if not keys:
        return f"{WERR}path not found: {a.get('path')}"
    for k in keys:
        derr = bridge.delete(k)
        if derr is not None:
            return f"{WERR}could not move to Trash: {derr}"
    return f"Moved to Trash (recoverable): {src}"


# ---------------- 第 1 类：只读 shell 仿真 ----------------

def _shell_result(code: int, out: str = "", err: str = "", benign: bool = False) -> str:
    """shell_exec.RunShellCommandTool.execute 同形：首行 exit_code（良性 exit 1 打标），其后
    stdout: / stderr: 段，都空 = (no output)。"""
    from framework.mcp.tools.shell_exec import _BENIGN_EXIT1_MARK
    mark = _BENIGN_EXIT1_MARK if (benign and code == 1 and not err.strip()) else ""
    parts = [f"exit_code: {code}{mark}"]
    out, err = out.rstrip(), err.rstrip()
    if out:
        parts.append(f"stdout:\n{out}")
    if err:
        parts.append(f"stderr:\n{err}")
    if not out and not err:
        parts.append("(no output)")
    return "\n".join(parts)


def bre_to_python(pat: str) -> str:
    r"""grep 缺省 BRE → Python re：`\|` `\(` `\)` `\{` `\}` `\+` `\?` 是元字符，裸 `| ( ) { } + ?` 是字面量。"""
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if c == "\\" and i + 1 < len(pat):
            n = pat[i + 1]
            out.append(n if n in "|(){}+?" else c + n)
            i += 2
            continue
        out.append("\\" + c if c in "|(){}+?" else c)
        i += 1
    return "".join(out)


def shell_tokens(command: str) -> Optional[List[str]]:
    """命令 → 词表；含管道 / 重定向 / 串接 / 子命令（未加引号的 ;|&<>`$()）或解析失败 = None。"""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";|&<>()")
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return None
    if any(t and set(t) <= set(";|&<>()") for t in toks):
        return None
    if any("`" in t or "$(" in t for t in toks):
        return None
    return toks


def shell_supported(command: Any) -> bool:
    toks = shell_tokens(str(command or ""))
    return bool(toks) and posixpath.basename(toks[0]) in SHELL_SUPPORTED


class _Shell:
    """vfs 上的只读 shell 仿真（ls / cat / grep / find / head / tail / wc / echo / pwd）。"""

    def __init__(self, bridge: VfsBridge, cwd: Any = ""):
        self.b = bridge
        self.files = bridge.files()
        self.cwd = vfs_key(cwd)

    def key(self, p: str) -> str:
        p = str(p)
        if self.cwd and not p.startswith(("/", "~")):
            return vfs_key(posixpath.join(self.cwd, p))
        return vfs_key(p)

    def is_dir(self, k: str) -> bool:
        return self.b.is_dir(k, self.files)

    def read(self, cmd: str, p: str) -> Tuple[Optional[str], Optional[str]]:
        k = self.key(p)
        if k in self.files:
            text, err = self.b.read(k)
            return (text, None) if err is None else (None, f"{cmd}: {p}: {err}")
        if self.is_dir(k):
            return None, f"{cmd}: {p}: Is a directory"
        return None, f"{cmd}: {p}: No such file or directory"

    def run(self, toks: List[str]) -> str:
        cmd, args = posixpath.basename(toks[0]), toks[1:]
        return getattr(self, f"c_{cmd}")(args)

    @staticmethod
    def _split_flags(args: List[str]) -> Tuple[List[str], List[str]]:
        flags, rest, done = [], [], False
        for t in args:
            if not done and t == "--":
                done = True
            elif not done and t.startswith("-") and len(t) > 1:
                flags.append(t)
            else:
                rest.append(t)
        return flags, rest

    def c_pwd(self, args: List[str]) -> str:
        return _shell_result(0, _disp(self.cwd))

    def c_echo(self, args: List[str]) -> str:
        if args and args[0] == "-n":
            return _shell_result(0, " ".join(args[1:]))
        return _shell_result(0, " ".join(args))

    def c_ls(self, args: List[str]) -> str:
        flags, paths = self._split_flags(args)
        paths = self.globbed(paths)
        long = any("l" in f for f in flags)
        out, err = [], []
        targets = paths or [""]
        for i, p in enumerate(targets):
            k = self.key(p)
            if k in self.files:
                out.append(self._ls_line(k, p, long))
                continue
            if not self.is_dir(k):
                err.append(f"ls: {p}: No such file or directory")
                continue
            names = {}
            for f in self.b.under(self.files, k):
                rel = f[len(k) + 1:] if k else f
                head, _, rest = rel.partition("/")
                names[head] = bool(rest)
            if len(targets) > 1:
                out.append(("" if i == 0 else "\n") + f"{p}:")
            if long:
                out.append(f"total {len(names)}")
            for n in sorted(names, key=str.lower):
                full = posixpath.join(k, n) if k else n
                out.append(self._ls_line(full, n, long, is_dir=names[n]))
        return _shell_result(1 if err else 0, "\n".join(out), "\n".join(err))

    def _ls_line(self, k: str, shown: str, long: bool, is_dir: bool = False) -> str:
        if not long:
            return shown
        if is_dir:
            return f"drwxr-xr-x  1 user  staff  {0:>6} Jan  1 09:00 {shown}"
        size = len(self.files.get(k, "").encode("utf-8"))
        return f"-rw-r--r--  1 user  staff  {size:>6} Jan  1 09:00 {shown}"

    def c_cat(self, args: List[str]) -> str:
        _flags, paths = self._split_flags(args)
        paths = self.globbed(paths)
        if not paths:
            return _shell_result(1, "", "cat: reading stdin is not available in this offline sandbox")
        out, err = [], []
        for p in paths:
            text, e = self.read("cat", p)
            if e:
                err.append(e)
            else:
                out.append(text)
        return _shell_result(1 if err else 0, "\n".join(out), "\n".join(err))

    def _expand(self, cmd: str, paths: List[str], recursive: bool
                ) -> Tuple[List[Tuple[str, str]], List[str]]:
        """(显示名, vfs 键) 列表 + 错误行；-r 时目录展开为其下全部文件。"""
        got, err = [], []
        for p in paths:
            k = self.key(p)
            if k in self.files:
                got.append((p, k))
            elif self.is_dir(k):
                if not recursive:
                    err.append(f"{cmd}: {p}: Is a directory")
                    continue
                for f in self.b.under(self.files, k):
                    rel = f[len(k) + 1:] if k else f
                    shown = f if p in ("", ".", "./") else f"{p.rstrip('/')}/{rel}"
                    got.append((shown, f))
            else:
                err.append(f"{cmd}: {p}: No such file or directory")
        return got, err

    def globbed(self, paths: List[str]) -> List[str]:
        """未加引号的通配（* ? [）按 vfs 键展开（shlex 不展开）；无命中保留原词（随后报不存在）。"""
        out = []
        for p in paths:
            if any(c in p for c in "*?["):
                pat = self.key(p)
                hits = sorted(f for f in self.files if fnmatch.fnmatchcase(f, pat))
                out.extend(hits or [p])
            else:
                out.append(p)
        return out

    def c_grep(self, args: List[str]) -> str:
        opts, rest, pattern, ddash, i = set(), [], None, False, 0
        while i < len(args):
            t = args[i]
            if not ddash and t == "--":
                ddash = True
            elif not ddash and t == "-e" and i + 1 < len(args) and pattern is None:
                pattern = args[i + 1]
                i += 1
            elif not ddash and t.startswith("-") and len(t) > 1:
                bad = t if t.startswith("--") else "".join(c for c in t[1:] if c not in GREP_FLAGS)
                if bad:
                    return _shell_result(2, "", f"grep: unsupported option {t}: {SHELL_NOTE}")
                opts.update(t[1:])
            else:
                rest.append(t)
            i += 1
        if pattern is None:
            if not rest:
                return _shell_result(2, "", "usage: grep [-n] [-i] [-r] <pattern> <file>…")
            pattern, rest = rest[0], rest[1:]
        rest = self.globbed(rest)
        recursive = bool(opts & {"r", "R"})
        if not rest:
            if not recursive:
                return _shell_result(2, "", "grep: no input files (reading stdin is not "
                                            "available in this offline sandbox)")
            rest = ["."]
        flags = re.IGNORECASE if "i" in opts else 0
        try:
            body = (re.escape(pattern) if "F" in opts
                    else pattern if "E" in opts else bre_to_python(pattern))
            rx = re.compile(rf"\b(?:{body})\b" if "w" in opts else body, flags)
        except re.error as e:
            return _shell_result(2, "", f"grep: invalid pattern: {e}")
        targets, err = self._expand("grep", rest, recursive)
        multi = (len(targets) > 1 or recursive) and "h" not in opts
        out, matched = [], False
        for shown, k in targets:
            text, e = self.b.read(k)
            if e is not None:
                err.append(f"grep: {shown}: {e}")
                continue
            n_hit = 0
            for ln_no, line in enumerate(text.splitlines(), 1):
                hit = bool(rx.search(line))
                if "v" in opts:
                    hit = not hit
                if not hit:
                    continue
                n_hit += 1
                if "l" in opts or "c" in opts:
                    continue
                pre = (f"{shown}:" if multi else "") + (f"{ln_no}:" if "n" in opts else "")
                out.append(pre + line)
            matched = matched or n_hit > 0
            if "l" in opts and n_hit:
                out.append(shown)
            elif "c" in opts:
                out.append((f"{shown}:" if multi else "") + str(n_hit))
        if err:
            return _shell_result(2, "\n".join(out), "\n".join(err))
        return _shell_result(0 if matched else 1, "\n".join(out), benign=True)

    def c_find(self, args: List[str]) -> str:
        roots, i = [], 0
        while i < len(args) and not args[i].startswith("-"):
            roots.append(args[i])
            i += 1
        name_glob, iname, ftype, maxdepth = None, False, None, None
        while i < len(args):
            t = args[i]
            nxt = args[i + 1] if i + 1 < len(args) else None
            if t == "-print":
                i += 1
                continue
            if t in ("-name", "-iname") and nxt is not None:
                name_glob, iname = nxt, t == "-iname"
            elif t == "-type" and nxt in ("f", "d"):
                ftype = nxt
            elif t == "-maxdepth" and nxt is not None and nxt.isdigit():
                maxdepth = int(nxt)
            else:
                return _shell_result(1, "", f"find: unsupported primary {t!r}: {SHELL_NOTE}")
            i += 2
        out, err = [], []
        for r in roots or ["."]:
            k = self.key(r)
            if k in self.files:
                cands = [(k, "f")]
            elif self.is_dir(k):
                below = self.b.under(self.files, k)
                dirs = set()
                for f in below:
                    rel = f[len(k) + 1:] if k else f
                    parts = rel.split("/")
                    for j in range(1, len(parts)):
                        dirs.add("/".join(parts[:j]))
                cands = ([(k, "d")] if k else []) + sorted(
                    [((posixpath.join(k, d) if k else d), "d") for d in dirs]
                    + [(f, "f") for f in below])
            else:
                err.append(f"find: {r}: No such file or directory")
                continue
            for path, kind in cands:
                if ftype and kind != ftype:
                    continue
                depth = 0 if path == k else len((path[len(k) + 1:] if k else path).split("/"))
                if maxdepth is not None and depth > maxdepth:
                    continue
                base = posixpath.basename(path)
                if name_glob is not None and not (
                        fnmatch.fnmatchcase(base.lower(), name_glob.lower()) if iname
                        else fnmatch.fnmatchcase(base, name_glob)):
                    continue
                out.append(path)
        return _shell_result(1 if err else 0, "\n".join(out), "\n".join(err))

    def _headtail(self, cmd: str, args: List[str]) -> str:
        n, paths, i, from_line = 10, [], 0, False
        while i < len(args):
            t = args[i]
            if t == "-n" and i + 1 < len(args):
                from_line = cmd == "tail" and args[i + 1].startswith("+")
                try:
                    n = abs(int(args[i + 1]))
                except ValueError:
                    return _shell_result(1, "", f"{cmd}: illegal line count -- {args[i + 1]}")
                i += 2
                continue
            if re.fullmatch(r"-n?\d+", t):
                n = int(t.lstrip("-n"))
            elif t.startswith("-") and len(t) > 1:
                return _shell_result(1, "", f"{cmd}: unsupported option {t}: {SHELL_NOTE}")
            else:
                paths.append(t)
            i += 1
        paths = self.globbed(paths)
        if not paths:
            return _shell_result(1, "", f"{cmd}: reading stdin is not available in this offline sandbox")
        out, err = [], []
        for p in paths:
            text, e = self.read(cmd, p)
            if e:
                err.append(e)
                continue
            lines = text.splitlines()
            if cmd == "head":
                part = lines[:n]
            elif from_line:                 # tail -n +N = 第 N 行起
                part = lines[max(n, 1) - 1:]
            else:
                part = lines[-n:] if n else []
            if len(paths) > 1:
                out.append(("\n" if out else "") + f"==> {p} <==")
            out.extend(part)
        return _shell_result(1 if err else 0, "\n".join(out), "\n".join(err))

    def c_head(self, args: List[str]) -> str:
        return self._headtail("head", args)

    def c_tail(self, args: List[str]) -> str:
        return self._headtail("tail", args)

    def c_wc(self, args: List[str]) -> str:
        flags, paths = self._split_flags(args)
        which = "".join(f[1:] for f in flags) or "lwc"
        paths = self.globbed(paths)
        if not paths:
            return _shell_result(1, "", "wc: reading stdin is not available in this offline sandbox")
        out, err, tot = [], [], [0, 0, 0]
        for p in paths:
            text, e = self.read("wc", p)
            if e:
                err.append(e)
                continue
            nums = [text.count("\n"), len(text.split()), len(text.encode("utf-8"))]
            tot = [a + b for a, b in zip(tot, nums)]
            out.append(self._wc_line(nums, which, p))
        if len(paths) > 1:
            out.append(self._wc_line(tot, which, "total"))
        return _shell_result(1 if err else 0, "\n".join(out), "\n".join(err))

    @staticmethod
    def _wc_line(nums: List[int], which: str, name: str) -> str:
        cols = [nums[j] for j, c in enumerate("lwc") if c in which or (c == "c" and "m" in which)]
        return "".join(f"{v:>8}" for v in cols) + f" {name}"


def do_shell(bridge: VfsBridge, a: Dict[str, Any]) -> str:
    command = str(a.get("command") or "")
    if not command.strip():
        return f"{ERR}empty command."
    toks = shell_tokens(command)
    if toks is None:
        return _shell_result(127, "", f"zsh: not executed: {SHELL_NOTE}")
    head = posixpath.basename(toks[0]) if toks else ""
    if head not in SHELL_SUPPORTED:
        return _shell_result(127, "", f"zsh: command not found: {head} ({SHELL_NOTE})")
    try:
        return _Shell(bridge, a.get("cwd") or "").run(toks)
    except Exception as e:      # noqa: BLE001 —— 仿真自身的意外按命令失败回，不冒成 System Error
        return _shell_result(1, "", f"{head}: {type(e).__name__}: {e}")


# ---------------- 第 2 类：检索 ----------------

def do_rag_search(_b, a: Dict[str, Any]) -> str:
    return RAG_EMPTY.format(query=a.get("query", ""), category=a.get("category", ""))


def do_rag_get_context(_b, a: Dict[str, Any]) -> str:
    return RAG_CONTEXT_EMPTY


# ---------------- 第 3 类：计算 / 确定性状态 ----------------

#: run_math 的报错头（run_math.RunMathTool.execute 同句）
MATH_ERR = "Math sandbox error (fix the code and retry):\n"
MATH_EMPTY = ("(no output — end your code with a bare expression to echo its value, "
              "or print() what you need)")
_MATH_FUNCS: Dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max,
    "floor": math.floor, "ceil": math.ceil, "sum": sum, "int": int, "float": float,
}
_MATH_CONSTS = {"pi": math.pi, "e": math.e}
MATH_MAX_BITS = 10000          # 整数结果位数上限（乘方 / 乘法 / 加法同一道闸）
MATH_MAX_ITEMS = 100000        # 列表长度上限（[0] * n）
_SYMBOLS = {"x", "y", "z", "t", "n", "k"}
_BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
           ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
           ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b}


class _MathError(Exception):
    pass


class _Arith:
    """ast 白名单算术：数字、+ - * / // % **、括号、一元正负、列表 / 元组字面量、
    sqrt/abs/round/min/max/floor/ceil/sum/int/float（也认 math. 前缀）、pi / e、简单赋值、print()。"""

    def __init__(self):
        self.vars: Dict[str, Any] = {}
        self.printed: List[str] = []

    def run(self, code: str) -> str:
        code = code.replace("×", "*").replace("÷", "/").replace("−", "-")
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError as e:
            raise _MathError(f"SyntaxError: {e.msg} (line {e.lineno})")
        echo = None
        for i, st in enumerate(tree.body):
            last = i == len(tree.body) - 1
            if isinstance(st, (ast.Import, ast.ImportFrom)):
                continue            # import math 等：本桩预载同名函数，忽略
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                self.vars[st.targets[0].id] = self.ev(st.value)
                continue
            if isinstance(st, ast.Expr):
                v = st.value
                if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "print"):
                    self.printed.append(" ".join(str(self.ev(x)) for x in v.args))
                    continue
                val = self.ev(v)
                if last:
                    echo = val
                continue
            raise _MathError(f"NotImplementedError: '{type(st).__name__}' statements are not "
                             "supported by this offline arithmetic sandbox (numbers, + - * / // % **, "
                             "sqrt/abs/round/min/max/floor/ceil/sum, assignments and print() only)")
        out = list(self.printed)
        if echo is not None:
            out.append(repr(echo))
        return "\n".join(out)

    @staticmethod
    def cap(v: Any) -> Any:
        if isinstance(v, int) and not isinstance(v, bool) and v.bit_length() > MATH_MAX_BITS:
            raise _MathError(f"OverflowError: integer result exceeds {MATH_MAX_BITS} bits in this sandbox")
        if isinstance(v, list) and len(v) > MATH_MAX_ITEMS:
            raise _MathError("OverflowError: list too large for this sandbox")
        return v

    def ev(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = self.ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else +v
        if isinstance(node, ast.BinOp):
            a, b = self.ev(node.left), self.ev(node.right)
            if isinstance(node.op, ast.Pow):
                if abs(b) > 10000 or (abs(a) > 1 and abs(b) * math.log10(abs(a) + 1) > 4000):
                    raise _MathError("OverflowError: exponent too large for this sandbox")
                return self.cap(a ** b)
            fn = _BINOPS.get(type(node.op))
            if fn is None:
                raise _MathError(f"NotImplementedError: operator {type(node.op).__name__} "
                                 "is not supported here")
            if isinstance(node.op, ast.Mult) and (isinstance(a, list) or isinstance(b, list)):
                seq, times = (a, b) if isinstance(a, list) else (b, a)
                if not isinstance(times, int) or len(seq) * max(times, 0) > MATH_MAX_ITEMS:
                    raise _MathError("OverflowError: list repetition too large for this sandbox")
            try:
                return self.cap(fn(a, b))
            except ZeroDivisionError as e:
                raise _MathError(f"ZeroDivisionError: {e}")
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.ev(x) for x in node.elts]
        if isinstance(node, ast.Name):
            if node.id in self.vars:
                return self.vars[node.id]
            if node.id in _MATH_CONSTS:
                return _MATH_CONSTS[node.id]
            if node.id in _SYMBOLS:
                raise _MathError("NotImplementedError: symbolic math (SymPy) is not available in "
                                 "this offline sandbox; only numeric arithmetic is evaluated")
            raise _MathError(f"NameError: name '{node.id}' is not defined")
        if isinstance(node, ast.Call) and not node.keywords:
            fname = None
            if isinstance(node.func, ast.Name):
                fname = node.func.id
            elif (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                  and node.func.value.id in ("math", "np", "numpy")):
                fname = node.func.attr
            fn = _MATH_FUNCS.get(fname or "")
            if fn is None:
                raise _MathError(f"NotImplementedError: function {fname or '?'}() is not available "
                                 "in this offline arithmetic sandbox")
            args = [self.ev(x) for x in node.args]
            try:
                return fn(*args)
            except (TypeError, ValueError) as e:
                raise _MathError(f"{type(e).__name__}: {e}")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "math" and node.attr in _MATH_CONSTS:
            return _MATH_CONSTS[node.attr]
        raise _MathError(f"NotImplementedError: '{type(node).__name__}' is not supported by this "
                         "offline arithmetic sandbox")


def do_run_math(_b, a: Dict[str, Any]) -> str:
    code = str(a.get("code") or "")
    if not code.strip():
        return f"{ERR}empty code."
    try:
        out = _Arith().run(code)
    except _MathError as e:
        return MATH_ERR + str(e)
    except (OverflowError, RecursionError, MemoryError, TypeError, ValueError) as e:
        return MATH_ERR + f"{type(e).__name__}: {e}"
    return out or MATH_EMPTY


def do_get_current_time(_b, a: Dict[str, Any]) -> str:
    return FIXED_TIME


def do_system_status(_b, a: Dict[str, Any]) -> str:
    return ("Battery: n/a\n"
            "Disk (/): 412.3 GB free of 994.7 GB (41% free)\n"
            "Memory: 64 GB installed\n"
            "Uptime: 9:00  up 3 days,  2:14, 1 user, load averages: 1.52 1.61 1.70")


def do_calendar_list(_b, a: Dict[str, Any]) -> str:
    date = a.get("date")
    start = "2026-01-01"
    if date:
        start = str(date).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
            return f"{ERR}'date' must be 'YYYY-MM-DD', got {date!r}."
    span = min(max(1, _int_or_none(a.get("days")) or 1), 31)
    return f"No calendar events between {start} +{span}d."


def do_schedule_list(_b, a: Dict[str, Any]) -> str:
    return "No scheduled tasks exist yet."


def do_deck_status(_b, a: Dict[str, Any]) -> str:
    return "No deck in this session yet — call deck_init to start one."


def do_list_applications(_b, a: Dict[str, Any]) -> str:
    return "No GUI applications are currently running."


def do_read_ui_tree(_b, a: Dict[str, Any]) -> str:
    app = str(a.get("app") or "").strip()
    if app:
        return f"No running GUI application matches '{app}'. Use list_applications to see options."
    return "Could not determine the frontmost application."


def do_clipboard_read(_b, a: Dict[str, Any]) -> str:
    return "The clipboard is empty or holds non-text content (e.g. an image/file)."


# ---------------- 第 4 类：离线不可用 ----------------

def offline_text(name: str, env_tools: Sequence[str] = ()) -> str:
    """离线不可用回执：正常文本（不带失败前缀），明说什么都没做；有文件工具时指过去。"""
    files = [n for n in ("list_files", "read_file", "write_file", "append_file", "delete_file")
             if n in env_tools]
    hint = (f" For anything that lives in files, use the file tools ({' / '.join(files)})."
            if files else " Use the task's own tools instead.")
    return (f"{name} is not available in this offline sandbox (no network, desktop, cloud, "
            "calendar or app access); nothing was done and no side effects happened." + hint)


_BEHAVIOURS: Dict[str, Callable[[Any, Dict[str, Any]], str]] = {
    "list_dir": do_list_dir, "find_files": do_find_files, "edit_file": do_edit_file,
    "copy_file": do_copy_file, "move_file": do_move_file, "trash_file": do_trash_file,
    "run_shell_command": do_shell,
    "rag_search": do_rag_search, "rag_get_context": do_rag_get_context,
    "run_math": do_run_math, "get_current_time": do_get_current_time,
    "system_status": do_system_status, "calendar_list": do_calendar_list,
    "schedule_list": do_schedule_list, "deck_status": do_deck_status,
    "list_applications": do_list_applications, "read_ui_tree": do_read_ui_tree,
    "clipboard_read": do_clipboard_read,
}


def behaviour_kind(name: str, bridged: bool) -> str:
    """vfs | retrieval | compute | offline（file_extract 归 SpillReadTool，不在此表）。"""
    if name in VFS_BRIDGED and name != "file_extract":
        return "vfs" if bridged else "offline"
    if name in RETRIEVAL:
        return "retrieval"
    if name in COMPUTE:
        return "compute"
    return "offline"


def missing_required(parameters: Dict[str, Any], arguments: Dict[str, Any]) -> List[str]:
    return [k for k in (parameters or {}).get("required") or []
            if (arguments or {}).get(k) is None]


def missing_text(name: str, missing: Sequence[str]) -> str:
    """缺必填参数（桩自己的回执）。D44 起编排器模式下生产注册表的通用声明核对（validate_call）先拦、
    桩的 execute 根本不会被调，这句只剩绕过预检直接 execute_tool 的路径（如单测直调注册表）会出现；措辞保留 D44 之前
    `System Error: Exception occurred while executing '<名>': …` 的形状。判空口径（null 也算缺）与
    MCPRegistry.argument_problems 一致。"""
    return (f"{WERR}Exception occurred while executing '{name}': execute() missing required "
            f"argument(s): {', '.join(repr(m) for m in missing)}")


class UsableStub(BaseTool):
    """一个生产工具名的可用桩。声明照生产；效果分类照生产类属性；执行 = 本模块的确定性行为。
    served = 本 run 执行次数（每 run 新建注册表，计数即 run 级）。"""

    requires_approval = False
    approval_pinned = False

    def __init__(self, schema: Dict[str, Any], summary: str = "",
                 bridge: Optional[VfsBridge] = None, env_tools: Sequence[str] = ()):
        self.name = schema["name"]
        self.description = schema.get("description") or schema["name"]
        self.parameters = copy.deepcopy(schema.get("parameters") or {})
        self.summary = summary or ""
        tr = traits_for(self.name)
        self.effect = tr["effect"]
        self.is_concurrency_safe = tr["is_concurrency_safe"]
        self.is_read_only = tr["is_read_only"]
        self.kind = behaviour_kind(self.name, bridge is not None)
        if self.kind == "offline":
            # 「什么都没做」不是成功变更：生产写类特征会让编排层把它记成 mutated_ok（关掉 D2 零变更
            # 守门、解禁写屏障）。effect 仍照生产（审批分档口径）
            self.is_read_only = True
        self._bridge = bridge
        self._env_tools = tuple(env_tools)
        self.served = 0
        self.failed = 0
        self.failures: List[str] = []       # 本 run 失败回执原文（traj_from_history 据此剔出 system_errors）
        self._count_lock = threading.Lock()

    def effect_for(self, arguments: Dict[str, Any] = None) -> str:
        # 生产 shell 按命令判（只读 = read）；仿真只执行只读命令 = 支持的命令记 read，其余 write
        if self.name == "run_shell_command":
            return "read" if shell_supported((arguments or {}).get("command")) else "write"
        return self.effect

    def execute(self, **arguments) -> str:
        with self._count_lock:
            self.served += 1
        text = self._dispatch(arguments)
        if text.startswith(WERR):
            with self._count_lock:
                self.failed += 1
                self.failures.append(text)
        return text

    def _dispatch(self, arguments: Dict[str, Any]) -> str:
        missing = missing_required(self.parameters, arguments)
        if missing:
            return missing_text(self.name, missing)
        if self.kind == "offline":
            return offline_text(self.name, self._env_tools)
        fn = _BEHAVIOURS[self.name]
        try:
            return fn(self._bridge, arguments)
        except Exception as e:      # noqa: BLE001 —— 桩自身的意外不得冒成 registry 的 System Error
            return f"{ERR}{self.name} failed in the offline sandbox: {type(e).__name__}: {e}"
