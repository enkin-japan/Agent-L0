"""D2 第二批金测试：organizer（日历/提醒）+ file_manage（move/copy/trash）。

AppleScript 侧全 mock subprocess 断言**脚本与 argv 构造**——日期必须以数字分量经
argv 交付（本地化日期字符串是跨机雷区），文本绝不进脚本字面量（注入面）。
文件操作真 tmp 跑；「绝不静默覆盖」是本文件的重心纪律。
"""
import subprocess

from framework.mcp.tools.file_manage import CopyFileTool, MoveFileTool, TrashFileTool
from framework.mcp.tools.organizer import (
    CalendarAddTool, CalendarListTool, ReminderAddTool, _parse_when)


class _Osa:
    def __init__(self, stdout="", returncode=0):
        self.calls = []
        self.stdout, self.returncode = stdout, returncode

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        r = subprocess.CompletedProcess(cmd, self.returncode)
        r.stdout, r.stderr = self.stdout, "boom" if self.returncode else ""
        return r


def _argv(call):  # osascript -e <script> <argv...>
    return call[3:]


# ---------- 日期纪律 ----------

def test_parse_when_accepts_space_and_T():
    assert _parse_when("2026-07-18 15:30").hour == 15
    assert _parse_when("2026-07-18T15:30").minute == 30
    assert _parse_when("明天下午") is None
    assert _parse_when("2026-7-18 15:30")  # 单数字月份也认


# ---------- calendar_add ----------

def test_calendar_add_passes_date_as_numeric_argv(monkeypatch):
    osa = _Osa(stdout="家庭\n")
    monkeypatch.setattr("framework.mcp.tools.organizer.subprocess.run", osa)
    out = CalendarAddTool().execute("牙医", "2026-07-18 15:00", duration_minutes=30)
    argv = _argv(osa.calls[0])
    assert argv == ["2026", "7", "18", "15", "0", "30", "-", "牙医"]
    script = osa.calls[0][2]
    assert "first calendar" in script and "牙医" not in script  # 文本不进脚本字面量
    assert "day of d to 1" in script  # 先归 1 再设年月日（防月末翻转）
    assert "家庭" in out and "30 min" in out


def test_calendar_add_named_calendar_and_validation(monkeypatch):
    osa = _Osa()
    monkeypatch.setattr("framework.mcp.tools.organizer.subprocess.run", osa)
    CalendarAddTool().execute("会议", "2026-07-18 09:00", calendar="工作")
    assert "calendar named" in osa.calls[0][2]
    assert _argv(osa.calls[0])[6] == "工作"

    t = CalendarAddTool()
    assert "when" in t.validate_arguments({"title": "x", "when": "明天三点"})
    assert t.validate_arguments({"title": " ", "when": "2026-07-18 09:00"})
    assert t.validate_arguments({"title": "x", "when": "2026-07-18 09:00"}) is None


def test_calendar_add_tcc_denied_gets_hint(monkeypatch):
    monkeypatch.setattr("framework.mcp.tools.organizer.subprocess.run",
                        _Osa(returncode=1))
    out = CalendarAddTool().execute("x", "2026-07-18 09:00")
    assert "System Error" in out and "Automation" in out


# ---------- calendar_list ----------

def test_calendar_list_defaults_today_and_caps_days(monkeypatch):
    osa = _Osa(stdout="")
    monkeypatch.setattr("framework.mcp.tools.organizer.subprocess.run", osa)
    out = CalendarListTool().execute(days=99)
    assert _argv(osa.calls[0])[5] == "31"  # days 封顶
    assert "No calendar events" in out
    assert "date" in CalendarListTool().execute(date="下周")  # 坏日期报错
    assert len(osa.calls) == 1


# ---------- reminder_add ----------

def test_reminder_add_without_due_is_minimal(monkeypatch):
    osa = _Osa(stdout="提醒事项\n")
    monkeypatch.setattr("framework.mcp.tools.organizer.subprocess.run", osa)
    out = ReminderAddTool().execute("买牛奶")
    assert _argv(osa.calls[0]) == ["买牛奶"]
    assert "default list" in osa.calls[0][2]
    assert "买牛奶" in out


def test_reminder_add_with_due_and_list(monkeypatch):
    osa = _Osa(stdout="工作\n")
    monkeypatch.setattr("framework.mcp.tools.organizer.subprocess.run", osa)
    ReminderAddTool().execute("交报告", due="2026-07-20 18:00", list="工作")
    argv = _argv(osa.calls[0])
    assert argv == ["2026", "7", "20", "18", "0", "交报告", "工作"]
    assert "list named" in osa.calls[0][2]
    assert "due" in ReminderAddTool().validate_arguments({"title": "x", "due": "傍晚"})


# ---------- move / copy：绝不静默覆盖 ----------

def test_move_renames_and_moves_into_dir(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    out = MoveFileTool().execute(str(f), str(tmp_path / "b.txt"))
    assert "Moved" in out and (tmp_path / "b.txt").exists() and not f.exists()

    sub = tmp_path / "sub"
    sub.mkdir()
    MoveFileTool().execute(str(tmp_path / "b.txt"), str(sub))
    assert (sub / "b.txt").exists()


def test_move_copy_refuse_overwrite_and_missing(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("1")
    b.write_text("2")
    for tool in (MoveFileTool(), CopyFileTool()):
        assert "refusing to overwrite" in tool.execute(str(a), str(b))
        assert "not found" in tool.validate_arguments(
            {"source": str(tmp_path / "no"), "destination": str(b)})
        assert "already exists" in tool.validate_arguments(
            {"source": str(a), "destination": str(b)})
        assert "does not exist" in tool.execute(str(a), str(tmp_path / "no" / "c.txt"))
    assert b.read_text() == "2"  # 谁都没动它


def test_copy_file_and_folder(tmp_path):
    src = tmp_path / "dir"
    src.mkdir()
    (src / "f.txt").write_text("x")
    out = CopyFileTool().execute(str(src), str(tmp_path / "dir2"))
    assert "Copied" in out and (tmp_path / "dir2" / "f.txt").exists()
    assert (src / "f.txt").exists()  # 原件还在


# ---------- trash ----------

def test_trash_prefers_native_nsfilemanager(monkeypatch, tmp_path):
    # 主路 = NSFileManager（进程内、零 TCC）：native 可用时绝不落 Finder/subprocess
    #（bundle 守护进程弹不出自动化授权窗，AE 路挂死 30s——2026-08-10 真机实案）
    f = tmp_path / "old.log"
    f.write_text("x")
    monkeypatch.setattr("framework.mcp.tools.file_manage._trash_native",
                        lambda p: f"Moved to Trash (recoverable): {p}")
    osa = _Osa()
    monkeypatch.setattr("framework.mcp.tools.file_manage.subprocess.run", osa)
    out = TrashFileTool().execute(str(f))
    assert "recoverable" in out
    assert osa.calls == []


def test_trash_falls_back_to_finder_via_argv(monkeypatch, tmp_path):
    # PyObjC 缺席（_trash_native 返回 None）才走 Finder，路径仍经 argv 交付防注入
    f = tmp_path / "old.log"
    f.write_text("x")
    monkeypatch.setattr("framework.mcp.tools.file_manage._trash_native", lambda p: None)
    osa = _Osa()
    monkeypatch.setattr("framework.mcp.tools.file_manage.subprocess.run", osa)
    out = TrashFileTool().execute(str(f))
    cmd = osa.calls[0]
    assert cmd[:2] == ["osascript", "-e"] and cmd[3] == str(f)
    assert "Finder" in cmd[2] and str(f) not in cmd[2]
    assert "recoverable" in out
    assert "not found" in TrashFileTool().validate_arguments({"path": str(tmp_path / "no")})


# ---------- 元数据纪律 ----------

def test_batch2_metadata_discipline():
    writers = [CalendarAddTool, ReminderAddTool, MoveFileTool, CopyFileTool, TrashFileTool]
    for cls in writers:
        assert cls.requires_approval is True, cls.name
        assert cls.is_concurrency_safe is False and cls.is_read_only is False, cls.name
    assert CalendarListTool.requires_approval is False
    assert CalendarListTool.is_concurrency_safe is True
    for cls in writers + [CalendarListTool]:
        assert cls.name and cls.description and "properties" in cls.parameters, cls.name
        assert callable(getattr(cls(), "is_available")), cls.name
