"""code_explore：代码仓语义探查（codegraph CLI 包壳，evaluations/codegraph.md 方案落地）。

codegraph（tree-sitter 符号图 + SQLite/FTS5，100% 本地）把「X 怎么实现 / 谁调用了 Y /
改 Z 波及哪里」这类跨文件拼图压成一次调用一份紧凑上下文——图遍历这种确定性逻辑
下沉到工具侧，与「架构补模型智力」同频。包壳形态照 media_download（yt-dlp）先例：
探活门控 + 子进程 + 模式分派。

守护：
- **遥测强关**：每次子进程注入 CODEGRAPH_TELEMETRY=0 + DO_NOT_TRACK=1（不赌用户
  装机时的选择；隐私北极星）。
- **init 单独过审批**（needs_approval 动态钩子）：它往用户代码仓写 .codegraph/
  目录且大仓可跑数分钟；查询类模式只读、免审批。
- **查询前置增量 sync**（秒级，fail-open）：不引常驻 daemon/文件监听，查询前同步
  一次的时效性对话场景足够，答案不基于陈旧图。
- 路径守卫：只收绝对路径 + 系统目录黑名单（给用户自己的仓库用，不是扫盘器）。

依赖：系统 PATH 上的 codegraph 二进制（用户经其 installer 安装/自更新），缺则不注册
（与 ffmpeg 同地位，不进 app bundle）。
"""
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from ..base import BaseTool

_MODES = ("explore", "symbol", "impact", "status", "init")
_QUERY_MODES = ("explore", "symbol", "impact")
# 模式 → CLI 子命令（symbol 对应 node：单符号源码+调用者，与 Read 同形）
_SUBCMD = {"explore": "explore", "symbol": "node", "impact": "impact"}
_INIT_TIMEOUT = 840          # timeout_seconds=900 的内层余量
_CMD_TIMEOUT = 120
_SYNC_TIMEOUT = 60

# 系统目录黑名单：本工具服务「用户自己的代码仓」，不是扫盘器
_FORBIDDEN_PREFIXES = ("/System", "/Library", "/usr", "/bin", "/sbin", "/etc",
                       "/Applications", "/private/etc")


def _guard_project_path(raw: str) -> "Path | str":
    """校验仓库路径；合法返回 Path，非法返回给模型的 System Error 字符串。"""
    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        return ("System Error: project_path must be an absolute path "
                "(e.g. /Users/you/projects/myrepo).")
    try:
        rp = p.resolve()
    except OSError as e:
        return f"System Error: cannot resolve project_path ({e})."
    if str(rp) == "/" or any(str(rp).startswith(pre) for pre in _FORBIDDEN_PREFIXES):
        return f"System Error: {rp} is a system location; point project_path at a code repository."
    if not rp.is_dir():
        return f"System Error: project_path {rp} is not an existing directory."
    return rp


def _cg_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["CODEGRAPH_TELEMETRY"] = "0"
    env["DO_NOT_TRACK"] = "1"
    return env


def _run(args, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(["codegraph", *args], cwd=str(cwd), env=_cg_env(),
                          capture_output=True, text=True, timeout=timeout)


class CodeExploreTool(BaseTool):
    name = "code_explore"
    description = (
        "Semantic code intelligence for a LOCAL code repository (codegraph symbol graph): "
        "answers 'how does X work', 'who calls Y', 'what breaks if I change Z' in one call, "
        "returning relevant source grouped by file plus call paths — follows dynamic-dispatch "
        "hops that text search cannot. mode='explore' (default): free-form question or area, "
        "needs query. mode='symbol': one symbol's source + callers (query = symbol name). "
        "mode='impact': blast radius of changing a symbol. mode='status': index statistics. "
        "mode='init': build the index for a repo that has none (writes .codegraph/ into the "
        "repo, may take minutes — asks approval). Read modes auto-sync the index first. "
        "project_path must be the absolute path of the repository."
    )
    requires_approval = False          # 查询类只读；init 经 needs_approval 动态升审批
    is_concurrency_safe = False        # 查询前置 sync 会写索引文件
    timeout_seconds = 900              # init 大仓建图以分钟计

    parameters = {
        "properties": {
            "project_path": {"type": "string",
                             "description": "Absolute path of the code repository to inspect."},
            "mode": {"type": "string", "enum": list(_MODES),
                     "description": "explore (default) | symbol | impact | status | init"},
            "query": {"type": "string",
                      "description": "explore: the question/area; symbol/impact: the exact "
                                     "symbol name. Not needed for status/init."},
        },
        "required": ["project_path"],
    }

    def is_available(self) -> bool:
        if shutil.which("codegraph") is None:
            print("[MCP-code_explore] ⚠️ PATH 上无 codegraph 二进制，工具不注册。")
            return False
        return True

    def needs_approval(self, arguments: Dict[str, Any]) -> bool:
        # init 往用户仓写 .codegraph/ 且可能跑数分钟——单独过审批；查询类免审
        return str((arguments or {}).get("mode", "explore")) == "init"

    def validate_arguments(self, arguments: Dict[str, Any]) -> Optional[str]:
        """审批前确定性预检：query 缺失这类必败调用不该骗一次审批/执行。"""
        mode = str((arguments or {}).get("mode", "explore"))
        if mode not in _MODES:
            return f"unknown mode {mode!r} (use {'/'.join(_MODES)})"
        if mode in _QUERY_MODES and not str((arguments or {}).get("query", "")).strip():
            return f"mode='{mode}' needs a non-empty query"
        return None

    def execute(self, project_path: str, mode: str = "explore",
                query: str = "") -> str:
        guarded = _guard_project_path(project_path)
        if isinstance(guarded, str):
            return guarded
        repo = guarded
        if mode not in _MODES:
            return f"System Error: unknown mode {mode!r} (use {'/'.join(_MODES)})."
        if mode in _QUERY_MODES and not (query or "").strip():
            return f"System Error: mode='{mode}' needs a non-empty query."

        has_index = (repo / ".codegraph").is_dir()
        try:
            if mode == "init":
                proc = _run(["init", str(repo)], cwd=repo, timeout=_INIT_TIMEOUT)
                if proc.returncode != 0:
                    return f"System Error: codegraph init failed:\n{(proc.stderr or proc.stdout)[-800:]}"
                return f"Index built for {repo}.\n{proc.stdout.strip()[-1200:]}"

            if not has_index and mode != "status":
                return (f"System Error: no .codegraph index at {repo}. "
                        f"Run mode='init' first (asks approval; builds the graph, "
                        f"may take minutes on large repos).")

            if has_index:
                # 查询前置增量同步（fail-open：同步失败仍查旧图，总比不答好）
                try:
                    _run(["sync", str(repo)], cwd=repo, timeout=_SYNC_TIMEOUT)
                except Exception:
                    pass

            if mode == "status":
                proc = _run(["status", str(repo)], cwd=repo, timeout=_CMD_TIMEOUT)
            else:
                proc = _run([_SUBCMD[mode], query], cwd=repo, timeout=_CMD_TIMEOUT)
        except subprocess.TimeoutExpired:
            return ("System Error: codegraph timed out. For huge repos try mode='status' "
                    "first, or a narrower query.")
        except FileNotFoundError:
            return "System Error: codegraph binary disappeared from PATH."

        if proc.returncode != 0:
            return f"System Error: codegraph {mode} failed:\n{(proc.stderr or proc.stdout)[-800:]}"
        out = (proc.stdout or "").strip()
        return out if out else f"(codegraph {mode} returned no output — symbol/area not found?)"
