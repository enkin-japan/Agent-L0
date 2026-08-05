"""officecli 二进制包装：查找、单发命令、批处理、校验。

调用形态刻意保持"一次调用一进程"（禁自动常驻，见 __init__ 说明）；文档构建
全走 batch——officecli 的 batch 默认原子（任一条失败整批回滚、磁盘文件保持
原样），天然就是交付门禁的一半：绝不会产出半成品文件。

版本纪律：装机经 brew 钉版（首测 1.0.143），自动更新已关。本层不做版本断言
（brew upgrade 是用户显式动作），但 probe() 会把版本号写进日志行便于排查。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# 与 excel/recalc.py 找 soffice 同教训：app 进程的 PATH 未必含 brew 前缀
_FALLBACKS = ("/opt/homebrew/bin/officecli", "/usr/local/bin/officecli")

_cached_bin: Optional[str] = None
_probed = False


class OfficeCliError(RuntimeError):
    """officecli 调用失败：携带结构化明细（stderr / 失败条目），供上层如实上报。"""

    def __init__(self, message: str, detail: Any = None):
        super().__init__(message)
        self.detail = detail


def find_officecli() -> Optional[str]:
    """查找二进制：PATH → brew 常见前缀。结果缓存（含"没找到"，经 rescan 重探）。"""
    global _cached_bin, _probed
    if _probed and _cached_bin:
        return _cached_bin
    path = shutil.which("officecli")
    if not path:
        for cand in _FALLBACKS:
            if Path(cand).is_file() and os.access(cand, os.X_OK):
                path = cand
                break
    _cached_bin = path
    _probed = True
    return path


def _env() -> Dict[str, str]:
    env = dict(os.environ)
    env["OFFICECLI_NO_AUTO_RESIDENT"] = "1"   # 一次一进程，退出即落盘
    env["OFFICECLI_SKIP_UPDATE"] = "1"        # 出网纪律纵深防御
    return env


def run_cli(args: List[str], timeout: int = 60) -> Dict[str, Any]:
    """单发命令（自动追加 --json）。成功返回解析后的 JSON；失败抛 OfficeCliError。"""
    binary = find_officecli()
    if binary is None:
        raise OfficeCliError("officecli binary not found (brew install officecli)")
    cmd = [binary, *args]
    if "--json" not in cmd:
        cmd.append("--json")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=_env())
    except subprocess.TimeoutExpired:
        raise OfficeCliError(f"officecli timed out after {timeout}s: {' '.join(args[:3])}")
    payload: Any = None
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = proc.stdout.strip()
    if proc.returncode != 0:
        # 结构化错误对象优先（code/suggestion），退化到 stderr 文本
        detail = payload if isinstance(payload, dict) else (proc.stderr.strip() or payload)
        raise OfficeCliError(f"officecli failed: {' '.join(args[:3])}", detail)
    if isinstance(payload, dict):
        return payload
    return {"success": True, "data": payload}


def batch(file: Path, commands: List[Dict[str, Any]], timeout: int = 180) -> Dict[str, Any]:
    """批处理（原子）：commands 走临时文件（体量大 + 免 argv 上限/转义面）。"""
    if not commands:
        return {"success": True, "data": "empty batch"}
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(commands, f, ensure_ascii=False)
            tmp = f.name
        return run_cli(["batch", str(file), "--input", tmp], timeout=timeout)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def validate(file: Path, timeout: int = 60) -> Dict[str, Any]:
    return run_cli(["validate", str(file)], timeout=timeout)


def probe_version() -> Optional[str]:
    """探活并取版本号（is_available 日志用）；异常一律吞成 None——探测不许炸注册。"""
    binary = find_officecli()
    if binary is None:
        return None
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True,
                              timeout=15, env=_env())
        return proc.stdout.strip() or None
    except Exception:
        return None
