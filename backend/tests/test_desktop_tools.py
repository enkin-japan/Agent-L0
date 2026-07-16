"""D2 第一批桌面工具金测试：file_nav（list_dir/find_files）+ desktop 五件套。

测试策略：纯本地只读的（list_dir/system_status）真跑；碰系统状态的
（pbcopy/osascript/open/mdfind）monkeypatch subprocess 断言**命令构造**——
真弹通知/真覆盖用户剪贴板/真开应用是对开发机的骚扰，且 CI 无 GUI 也要能跑。
open_path 的审批矩阵是本文件的重心：白名单放行/审批的分界一旦漂移就是提权面。
"""
import stat
import subprocess

import pytest

from framework.mcp.tools.desktop import (
    ClipboardReadTool, ClipboardWriteTool, NotifyUserTool, OpenPathTool,
    SystemStatusTool, _is_view_safe)
from framework.mcp.tools.file_nav import FindFilesTool, ListDirTool


class _Ran:
    """subprocess.run 替身：记录 argv/kwargs，按脚本返回。"""

    def __init__(self, stdout="", returncode=0):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        r = subprocess.CompletedProcess(cmd, self.returncode)
        r.stdout = self.stdout
        r.stderr = ""
        return r


# ---------- list_dir ----------

def test_list_dir_orders_dirs_first_and_hides_dotfiles(tmp_path):
    (tmp_path / "zeta.txt").write_text("x")
    (tmp_path / "alpha").mkdir()
    (tmp_path / ".hidden").write_text("x")
    out = ListDirTool().execute(str(tmp_path))
    assert out.index("alpha/") < out.index("zeta.txt")
    assert ".hidden" not in out
    assert "(2 entries)" in out
    # show_hidden 放开后可见
    assert ".hidden" in ListDirTool().execute(str(tmp_path), show_hidden=True)


def test_list_dir_rejects_missing_and_file_paths(tmp_path):
    assert "not found" in ListDirTool().execute(str(tmp_path / "nope"))
    f = tmp_path / "a.txt"
    f.write_text("x")
    out = ListDirTool().execute(str(f))
    assert "not a folder" in out and "file_extract" in out


def test_list_dir_expands_home():
    out = ListDirTool().execute("~")
    assert "System Error" not in out


# ---------- find_files ----------

def test_find_files_builds_name_mode_by_default(monkeypatch, tmp_path):
    ran = _Ran(stdout=f"{tmp_path}/a.pdf\n")
    (tmp_path / "a.pdf").write_text("x")
    monkeypatch.setattr("framework.mcp.tools.file_nav.subprocess.run", ran)
    out = FindFilesTool().execute("report")
    assert ran.calls[0][0] == ["mdfind", "-name", "report"]
    assert "a.pdf" in out


def test_find_files_content_mode_and_folder_scope(monkeypatch, tmp_path):
    ran = _Ran(stdout="")
    monkeypatch.setattr("framework.mcp.tools.file_nav.subprocess.run", ran)
    out = FindFilesTool().execute("发票", search_content=True, folder=str(tmp_path))
    assert ran.calls[0][0] == ["mdfind", "-onlyin", str(tmp_path), "发票"]
    assert "No files matched" in out
    # 不存在的 folder 直接报错，不去跑 mdfind
    assert "folder not found" in FindFilesTool().execute("x", folder=str(tmp_path / "nope"))
    assert len(ran.calls) == 1


def test_find_files_rejects_empty_query():
    assert FindFilesTool().validate_arguments({"query": "  "})
    assert FindFilesTool().validate_arguments({"query": "ok"}) is None


# ---------- open_path 审批矩阵（本文件重心） ----------

def test_open_path_approval_matrix(tmp_path):
    doc = tmp_path / "read.pdf"
    doc.write_text("x")
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n")
    execbit = tmp_path / "tool.txt"
    execbit.write_text("x")
    execbit.chmod(execbit.stat().st_mode | stat.S_IXUSR)
    app = tmp_path / "Fake.app"
    app.mkdir()

    free = [f"https://example.com", str(tmp_path), str(doc)]
    gated = [str(script),            # 可执行扩展
             str(execbit),           # 白名单扩展但带执行位
             str(app),               # .app 目录
             str(tmp_path / "no"),   # 不存在
             "javascript:alert(1)",  # 非 http(s) scheme
             ""]
    t = OpenPathTool()
    for target in free:
        assert not t.needs_approval({"target": target}), target
    for target in gated:
        assert t.needs_approval({"target": target}), target
    assert _is_view_safe(str(doc)) and not _is_view_safe(str(script))


def test_open_path_validate_and_execute(monkeypatch, tmp_path):
    t = OpenPathTool()
    assert "not found" in t.validate_arguments({"target": str(tmp_path / "no.pdf")})
    assert "scheme" in t.validate_arguments({"target": "ftp://x"})
    assert t.validate_arguments({"target": "https://example.com"}) is None

    ran = _Ran()
    monkeypatch.setattr("framework.mcp.tools.desktop.subprocess.run", ran)
    doc = tmp_path / "a.pdf"
    doc.write_text("x")
    out = t.execute(str(doc))
    assert ran.calls[0][0] == ["open", str(doc)] and "Opened" in out
    # 必败调用不外逸到 open
    assert "System Error" in t.execute(str(tmp_path / "nope.pdf"))
    assert len(ran.calls) == 1


# ---------- 剪贴板 ----------

def test_clipboard_write_pipes_exact_bytes(monkeypatch):
    ran = _Ran()
    monkeypatch.setattr("framework.mcp.tools.desktop.subprocess.run", ran)
    out = ClipboardWriteTool().execute("你好 world")
    cmd, kw = ran.calls[0]
    assert cmd == ["pbcopy"] and kw["input"] == "你好 world".encode("utf-8")
    assert "8 characters" in out


def test_clipboard_read_caps_and_handles_empty(monkeypatch):
    class _Big:
        def __call__(self, cmd, **kw):
            r = subprocess.CompletedProcess(cmd, 0)
            r.stdout = ("x" * 9000).encode()
            return r
    monkeypatch.setattr("framework.mcp.tools.desktop.subprocess.run", _Big())
    out = ClipboardReadTool().execute()
    assert "first 8000 shown" in out and len(out) < 8200

    monkeypatch.setattr("framework.mcp.tools.desktop.subprocess.run",
                        _Ran(stdout=b""))
    assert "empty" in ClipboardReadTool().execute()


# ---------- 通知 ----------

def test_notify_passes_message_via_argv_not_script(monkeypatch):
    ran = _Ran()
    monkeypatch.setattr("framework.mcp.tools.desktop.subprocess.run", ran)
    evil = 'done" with title "pwn'
    out = NotifyUserTool().execute(evil, title="任务")
    cmd, _ = ran.calls[0]
    # message/title 是独立 argv 项，绝不进 -e 脚本字面量（无注入面）
    assert cmd[:2] == ["osascript", "-e"] and cmd[3] == evil and cmd[4] == "任务"
    assert evil not in cmd[2]
    assert "Notification shown" in out


# ---------- 系统状态（真跑，本机只读） ----------

@pytest.mark.skipif(not SystemStatusTool().is_available(), reason="非 macOS")
def test_system_status_reports_all_sections():
    out = SystemStatusTool().execute()
    assert "Battery:" in out and "Disk (/):" in out and "Memory:" in out
    assert "GB" in out


# ---------- 探活与元数据纪律 ----------

def test_batch_metadata_discipline():
    """扩充纪律：每工具必须探活可判、只读/审批标记与实现一致。"""
    readonly = [ListDirTool, FindFilesTool, ClipboardReadTool, SystemStatusTool]
    effectful = [OpenPathTool, ClipboardWriteTool, NotifyUserTool]
    for cls in readonly:
        assert cls.is_concurrency_safe is True and cls.requires_approval is False, cls.name
    for cls in effectful:
        assert cls.is_concurrency_safe is False and cls.is_read_only is False, cls.name
    for cls in readonly + effectful:
        assert callable(getattr(cls(), "is_available")), cls.name
        assert cls.name and cls.description and "properties" in cls.parameters, cls.name
