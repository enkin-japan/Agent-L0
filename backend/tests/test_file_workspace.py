"""write_file/edit_file 的会话工作区语义（D1 实测缺口）。

守的事实：剧本让模型「把报告写进 workspace」，而工具侧此前只收绝对路径——模型
幻觉出 `/workspace/` 撞上 macOS 只读根（Errno 30），交付物无处安放。修复后：
相对文件名落 sessions/{sid}/assets/（与 render_chart 同范式），返回出货链接。
"""
import pytest

from framework.agents import runtime
from framework.mcp.tools.file_edit import EditFileTool, WriteFileTool


class _FakeSession:
    def __init__(self, tmp_path):
        self.session_dir = tmp_path
        self.session_id = "ws-test"


@pytest.fixture
def workspace(tmp_path):
    runtime.set_current_session(_FakeSession(tmp_path))
    try:
        yield tmp_path
    finally:
        runtime.set_current_session(None)


def test_relative_filename_lands_in_session_assets(workspace):
    out = WriteFileTool().execute("report-topic.md", "正文")
    target = workspace / "assets" / "report-topic.md"
    assert target.read_text(encoding="utf-8") == "正文"
    # 出货链接必须在响应里逐字可拷（与 /api/v1/files 路由对齐）
    assert "[report-topic.md](/api/v1/files/ws-test/assets/report-topic.md)" in out


def test_relative_edit_resolves_same_workspace_file(workspace):
    WriteFileTool().execute("note.md", "hello world")
    out = EditFileTool().execute("note.md", "world", "workspace")
    assert "OK" in out
    assert (workspace / "assets" / "note.md").read_text(encoding="utf-8") == "hello workspace"


@pytest.mark.parametrize("bad", ["sub/dir.md", "../escape.md", ".hidden"])
def test_relative_name_charset_guard(workspace, bad):
    """子目录/上跳/隐藏名一律打回——文件名字符集与出货路由 _ASSET_NAME_RE 对齐，
    否则写得进、取不出（或逃出会话目录）。"""
    out = WriteFileTool().execute(bad, "x")
    assert out.startswith("System Error")
    assert not (workspace / "assets" / bad).exists()


def test_relative_without_session_is_guided():
    runtime.set_current_session(None)
    out = WriteFileTool().execute("orphan.md", "x")
    assert out.startswith("System Error") and "absolute" in out


def test_absolute_path_semantics_unchanged(workspace, tmp_path):
    p = tmp_path / "abs.md"
    out = WriteFileTool().execute(str(p), "abs 内容")
    assert "OK" in out and "api/v1/files" not in out
    assert p.read_text(encoding="utf-8") == "abs 内容"
