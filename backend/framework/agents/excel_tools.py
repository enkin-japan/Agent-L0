"""Excel 沙箱执行核心：seatbelt 持久 Python REPL + 会话工作区管理。

与 math_tools 同格局（能力本体放 agents，MCP 层只是包壳），两点关键差异：

 1. **持久会话**。Excel 任务天然多轮（load → 改 → 存 → 修），每轮全新进程会逼着
    模型反复重写 load_workbook、并且记不清上一轮存没存。这里把一个 python 进程
    常驻，用 exec 在同一 globals 里跑代码（变量跨轮保留）。上游 excel_skill 的
    REPL 协议沿用，外加两处硬化：
      · 协议行打 \\x1e 记录分隔符前缀——模型代码里的原生 fd 写入（绕过
        redirect_stdout 的 C 层输出）不会污染 JSON 协议流；
      · 读取带墙钟超时——死循环代码会挂死 readline（上游无超时），超时即杀进程、
        下次调用自动重启全新 REPL，并明确告知模型变量已丢失。

 2. **seatbelt 写放行工作区**。math 沙箱全禁写；Excel 必须落盘，故 profile 只放行
    会话工作区 work/ 子树（SBPL 后写规则优先级更高）。网络仍全禁（断网沙箱是
    单元格 prompt-injection 的最后防线之一），读放行（python 要读 venv）。

工作区布局 sessions/{id}/excel/：
  uploads/   用户原件副本（work 之外 → 模型进程写不了；"永不原地改原件"物理成立）
  work/      REPL cwd，唯一可写子树（TMPDIR 指到 work/.tmp）
  preview/   verify 渲染的 PNG（harness 写，模型进程不可写 → 回读图不可伪造）
  report.json 最近一次 verify 报告（harness 写）
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Dict, Optional

DEFAULT_TIMEOUT = 60     # 单次 run_python 墙钟（秒）；配置可调，硬上限如下
MAX_TIMEOUT = 120
MAX_OUTPUT_CHARS = 4000  # 观察注入上限：整表 dump 由提示词禁止，这里兜底

# 协议行前缀：REPL driver 的每条响应都以此开头，非此前缀的行是模型代码的
# 原生输出（如 C 扩展直写 fd1），收集后并入 stdout 而不是撑破协议。
_REC = "\x1e"

# REPL driver：从 stdin 逐行读 {"code": ...}，exec 于同一 globals，回吐打标 JSON。
# 预载 openpyxl/pandas/Path（模型免写样板 import；REPL 启动时一次性付进口成本）。
_REPL_DRIVER = r'''
import sys, json, io, traceback, contextlib
G = {"__name__": "__main__"}
exec(compile(
    "from pathlib import Path\n"
    "import openpyxl\n"
    "from openpyxl import load_workbook, Workbook\n"
    "import pandas as pd\n",
    "<prelude>", "exec"), G)
print("\x1e" + json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        code = json.loads(line)["code"]
    except Exception as e:
        print("\x1e" + json.dumps({"ok": False, "stdout": "", "stderr": f"bad request: {e}"}),
              flush=True)
        continue
    out, err = io.StringIO(), io.StringIO()
    ok = True
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exec(compile(code, "<cell>", "exec"), G)
    except BaseException:
        ok = False
        err.write(traceback.format_exc())
    print("\x1e" + json.dumps({"ok": ok, "stdout": out.getvalue(), "stderr": err.getvalue()}),
          flush=True)
'''


def _seatbelt_profile(workdir: Path) -> str:
    """禁网 + 只放行 workdir 子树与 /dev/null 写。SBPL 后写的规则优先级更高。"""
    wd = str(workdir.resolve())
    if '"' in wd or "\\" in wd:
        raise ValueError(f"workdir path not SBPL-safe: {wd!r}")
    return (
        "(version 1)"
        "(allow default)"
        "(deny network*)"
        "(deny file-write*)"
        f'(allow file-write* (subpath "{wd}"))'
        '(allow file-write-data (literal "/dev/null"))'
    )


def seatbelt_available() -> bool:
    """探活：带 subpath 写放行的 profile 跑不起来就绝不执行模型代码。"""
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            probe = subprocess.run(
                ["/usr/bin/sandbox-exec", "-p", _seatbelt_profile(Path(tmp)),
                 "/usr/bin/true"],
                capture_output=True, timeout=10,
            )
            return probe.returncode == 0
    except Exception:
        return False


def file_sha256(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ExcelWorkspace:
    """会话级 Excel 工作区。root = sessions/{id}/excel。"""

    def __init__(self, session_dir: Path):
        self.root = Path(session_dir) / "excel"
        self.uploads = self.root / "uploads"
        self.work = self.root / "work"
        self.preview = self.root / "preview"
        self.report_path = self.root / "report.json"
        for d in (self.uploads, self.work, self.preview, self.work / ".tmp"):
            d.mkdir(parents=True, exist_ok=True)

    def stage_input(self, input_file: Path) -> Dict[str, str]:
        """把用户文件复制成 uploads 原件 + work 工作副本。返回两个相对模型视角的路径。
        同名文件重复暂存时直接覆盖——以本次调用的输入为准。"""
        src = Path(input_file).resolve()
        name = src.name
        orig = self.uploads / name
        work_copy = self.work / name
        for dst in (orig, work_copy):
            if dst.resolve() == src:  # 输入本就在工作区：别把源当目标删了
                continue
            if dst.exists():
                dst.chmod(0o644)
                dst.unlink()
            shutil.copy2(src, dst)
        if orig.exists():
            orig.chmod(0o444)  # 纵深防御：seatbelt 已挡模型进程，这里再挡 harness 误写
        return {"original": f"../uploads/{name}", "working": name}


class SandboxRepl:
    """seatbelt 内的持久 Python 会话。线程安全（一次一个调用）。"""

    def __init__(self, workdir: Path, timeout_default: int = DEFAULT_TIMEOUT):
        self.workdir = Path(workdir)
        self.timeout_default = int(timeout_default)
        self._proc: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.Lock()
        self._stderr_log = self.workdir / ".repl-stderr.log"
        self._stderr_fh = None
        self._dirty = False  # 上一个 REPL 非正常收场（超时杀/崩溃）→ 下次启动要告知模型

    # ---------------- 生命周期

    def _start(self) -> Optional[str]:
        """启动 REPL 并等 ready 信标。返回错误消息（None = 成功）。"""
        import os
        env = {**os.environ,
               "TMPDIR": str(self.workdir / ".tmp"),
               "HOME": str(self.workdir)}
        try:
            self._lines = queue.Queue()
            self._stderr_fh = open(self._stderr_log, "w")
            self._proc = subprocess.Popen(
                ["/usr/bin/sandbox-exec", "-p", _seatbelt_profile(self.workdir),
                 sys.executable, "-I", "-B", "-u", "-c", _REPL_DRIVER],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._stderr_fh,
                text=True, cwd=self.workdir, env=env,
            )
        except Exception as e:
            self._proc = None
            return f"failed to launch excel sandbox: {e}"
        threading.Thread(target=self._reader, args=(self._proc,), daemon=True).start()
        # 等 prelude 完成（pandas 导入约 1-2s；给足冷启动余量）
        r = self._read_record(timeout=60)
        if r is None or not r.get("ready"):
            self._kill()
            tail = self._stderr_tail()
            return f"excel sandbox failed to initialize: {tail or 'no ready beacon'}"
        return None

    def _reader(self, proc: subprocess.Popen) -> None:
        """读线程：行入队。进程退出（EOF）后放哨兵。"""
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                self._lines.put(line)
        except Exception:
            pass
        self._lines.put("")  # EOF 哨兵

    def _read_record(self, timeout: float) -> Optional[dict]:
        """取下一条打标协议行；杂散输出攒进 _stray。超时/EOF 返回 None。"""
        import time
        self._stray: list = []
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            try:
                line = self._lines.get(timeout=remain)
            except queue.Empty:
                return None
            if line == "":  # EOF 哨兵：进程已死
                return None
            if line.startswith(_REC):
                try:
                    return json.loads(line[len(_REC):])
                except json.JSONDecodeError:
                    return None
            self._stray.append(line.rstrip("\n"))  # 模型代码的原生 fd 输出

    def _stderr_tail(self, n: int = 500) -> str:
        try:
            return self._stderr_log.read_text(errors="replace")[-n:].strip()
        except Exception:
            return ""

    def _kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None
        if self._stderr_fh is not None:
            try:
                self._stderr_fh.close()
            except Exception:
                pass
            self._stderr_fh = None

    def close(self) -> None:
        with self._lock:
            self._kill()

    # ---------------- 执行

    def run(self, code: str, timeout: Optional[int] = None) -> Dict:
        """执行一段代码。返回 {"ok": bool, "output": str, "restarted": bool}：
        ok=False 时 output 是面向模型的报错文本；restarted=True 表示本次调用前
        REPL 刚重启过（上次超时/崩溃），变量已丢失。"""
        if not code or not code.strip():
            return {"ok": False, "output": "System Error: empty code.", "restarted": False}
        try:
            timeout = max(1, min(int(timeout or self.timeout_default), MAX_TIMEOUT))
        except (TypeError, ValueError):
            timeout = self.timeout_default

        with self._lock:
            restarted = False
            if self._proc is None or self._proc.poll() is not None:
                restarted = self._dirty or self._proc is not None  # 非正常收场后的重启
                err = self._start()
                if err:
                    return {"ok": False, "output": f"System Error: {err}",
                            "restarted": restarted}
                self._dirty = False

            try:
                assert self._proc and self._proc.stdin
                self._proc.stdin.write(
                    json.dumps({"code": textwrap.dedent(code)}) + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                self._kill()
                return {"ok": False, "restarted": restarted,
                        "output": f"System Error: sandbox pipe broken ({e}); "
                                  "it will restart on your next call (variables lost)."}

            r = self._read_record(timeout=timeout)
            if r is None:
                alive = self._proc is not None and self._proc.poll() is None
                self._kill()
                self._dirty = True
                if alive:
                    msg = (f"System Error: execution exceeded {timeout}s and the sandbox "
                           "was killed (infinite loop? oversized workbook scan?). "
                           "A FRESH session starts on your next call — all variables "
                           "are lost; reload the workbook first.")
                else:
                    msg = ("System Error: the sandbox process died"
                           + (f" — {self._stderr_tail()}" if self._stderr_tail() else "")
                           + ". A fresh session starts on your next call (variables lost).")
                return {"ok": False, "output": msg, "restarted": restarted}

            stdout = (r.get("stdout") or "")
            stray = "\n".join(getattr(self, "_stray", []) or [])
            if stray:
                stdout = (stdout + "\n" + stray).strip()
            stderr = (r.get("stderr") or "").strip()
            if len(stdout) > MAX_OUTPUT_CHARS:
                stdout = stdout[:MAX_OUTPUT_CHARS] + "\n…[output truncated]"
            if len(stderr) > MAX_OUTPUT_CHARS:
                stderr = stderr[:MAX_OUTPUT_CHARS] + "\n…[stderr truncated]"

            if not r.get("ok"):
                err_text = stderr or stdout or "unknown error"
                # 高频坑定向提示：沙箱拒绝类 traceback 对小模型不够直白，直接给改法
                if "Read-only file system" in err_text or "Operation not permitted" in err_text:
                    if any(k in code for k in ("socket", "urllib", "requests", "http")):
                        err_text += ("\nHint: this sandbox has NO network access; work "
                                     "only with the local files.")
                    else:
                        err_text += ("\nHint: file writes are only allowed inside the "
                                     "current working directory. Write your output file "
                                     "with a bare filename, e.g. wb.save('out.xlsx').")
                return {"ok": False, "output": err_text, "restarted": restarted}

            if stderr and stdout:
                stdout = stdout + "\n[stderr]\n" + stderr
            elif stderr:
                stdout = "[stderr]\n" + stderr
            return {"ok": True, "output": stdout, "restarted": restarted}
