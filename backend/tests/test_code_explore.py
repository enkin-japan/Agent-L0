"""code_explore 金测试：全部离线（不需要 codegraph 二进制、不跑真子进程）。

钉住的行为：
1. 探活：PATH 无 codegraph 不注册（S.3：宣告跟着能力走）；
2. init 单独过审批（needs_approval 动态钩子）、查询类免审；
3. validate_arguments 预检：坏 mode / 缺 query 在审批与执行前拦下；
4. 路径守卫：相对路径 / 系统目录 / 不存在目录都给可行动 System Error；
5. 命令装配：遥测强关两枚环境变量、查询前置 sync、模式 → 子命令映射；
6. 无索引查询 → 指路 init 而非裸失败。
"""
import subprocess
from unittest.mock import patch

from framework.mcp.tools import code_explore as ce
from framework.mcp.tools.code_explore import CodeExploreTool


def _ok(stdout="OUT", rc=0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


# ---------- 探活 / 审批 / 预检 ----------

def test_not_registered_without_binary():
    with patch.object(ce.shutil, "which", return_value=None):
        assert CodeExploreTool().is_available() is False
    with patch.object(ce.shutil, "which", return_value="/usr/local/bin/codegraph"):
        assert CodeExploreTool().is_available() is True


def test_init_needs_approval_queries_do_not():
    tool = CodeExploreTool()
    assert tool.needs_approval({"mode": "init"}) is True
    for m in ("explore", "symbol", "impact", "status"):
        assert tool.needs_approval({"mode": m}) is False
    assert tool.needs_approval({}) is False          # 缺省 explore 免审


def test_precheck_blocks_doomed_calls():
    tool = CodeExploreTool()
    assert "unknown mode" in tool.validate_arguments({"mode": "nuke"})
    assert "needs a non-empty query" in tool.validate_arguments({"mode": "explore"})
    assert tool.validate_arguments({"mode": "status"}) is None
    assert tool.validate_arguments({"mode": "explore", "query": "auth flow"}) is None


# ---------- 路径守卫 ----------

def test_path_guards(tmp_path):
    tool = CodeExploreTool()
    assert "must be an absolute path" in tool.execute("relative/repo")
    assert "system location" in tool.execute("/usr/lib")
    assert "not an existing directory" in tool.execute(str(tmp_path / "ghost"))


# ---------- 命令装配（mock 子进程） ----------

def test_query_syncs_first_and_maps_subcommand(tmp_path):
    (tmp_path / ".codegraph").mkdir()
    calls = []

    def fake_run(args, cwd, env, capture_output, text, timeout):
        calls.append((args, env))
        return _ok("explore result")

    with patch.object(ce.subprocess, "run", side_effect=fake_run):
        out = CodeExploreTool().execute(str(tmp_path), mode="explore", query="auth flow")
    assert out == "explore result"
    assert [c[0][1] for c in calls] == ["sync", "explore"]     # 先 sync 后查
    assert calls[1][0][2] == "auth flow"
    for _, env in calls:                                        # 遥测强关（每次子进程）
        assert env["CODEGRAPH_TELEMETRY"] == "0"
        assert env["DO_NOT_TRACK"] == "1"


def test_symbol_maps_to_node(tmp_path):
    (tmp_path / ".codegraph").mkdir()
    calls = []
    with patch.object(ce.subprocess, "run",
                      side_effect=lambda args, **kw: (calls.append(args), _ok())[1]):
        CodeExploreTool().execute(str(tmp_path), mode="symbol", query="UserService")
    assert calls[-1][1] == "node"


def test_no_index_points_to_init(tmp_path):
    tool = CodeExploreTool()
    out = tool.execute(str(tmp_path), mode="explore", query="x")
    assert out.startswith("System Error") and "mode='init'" in out


def test_init_runs_and_reports(tmp_path):
    calls = []
    with patch.object(ce.subprocess, "run",
                      side_effect=lambda args, **kw: (calls.append(args), _ok("built"))[1]):
        out = CodeExploreTool().execute(str(tmp_path), mode="init")
    assert calls[0][1] == "init"
    assert "Index built" in out


def test_nonzero_exit_surfaces_stderr(tmp_path):
    (tmp_path / ".codegraph").mkdir()
    proc = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
    with patch.object(ce.subprocess, "run", return_value=proc):
        out = CodeExploreTool().execute(str(tmp_path), mode="explore", query="x")
    assert out.startswith("System Error") and "boom" in out
