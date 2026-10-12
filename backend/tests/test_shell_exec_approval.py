"""终端审批按操作和参数判定；这里只分类或驱动假执行器，绝不运行命令文本。"""
import asyncio

import pytest

from framework.mcp.registry import MCPRegistry
from framework.mcp.tools.shell_exec import ShellExecTool, _is_readonly


@pytest.mark.parametrize("command", [
    "sort -o result input", "sort -oresult input", "sort --output=result input",
    "sort --compress-program=evil input", "uniq input output", "xxd input output",
    "git branch -D main", "git branch new-name", "git tag new-tag",
    "git remote add origin remote", "git config core.pager evil",
    "git reflog expire --all", "git ls-remote --upload-pack=evil origin",
    "git diff --ext-diff", "git show --textconv", "git -c alias.status=evil status",
    "python -cpass", "python3 -mhttp.server", "python -", "python --version script.py",
    "node --eval=evil", "node --require=evil", "node --import=evil", "ruby -revil",
    "find . -delete", "find . -exec evil", "find . -fprint output",
    "fd -x evil", "fd --exec=evil", "rg --pre=evil pattern", "rg --hostname-bin=evil",
    "awk 'system evil'", "sed 'w output' input", "sed 'e evil' input",
    "yq -i . input", "jq --run-tests input", "diff --output=result a b",
    "file -C", "date 101112002026", "hostname changed", "ifconfig en0 down",
    "top -w output", "man -P evil ls", "history -w output", "./cat input",
    "/tmp/ls input", "ls --unknown-option", "sort --unknown-option input",
    "cat input > output", "echo hi\npython script.py", "cat <(evil)",
    "find -delete", "find . -name *", "grep -f * input", "ls *",
    "rg pattern input", "git status", "git log", "git diff", "git show",
    "file -z input", "git --paginate branch --list", "git branch -l new-name",
    "arch evil", "arch --help", "sort --output result input", "sort -roresult input",
    "git branch --list --help", "git tag --list --help", "git --help",
])
def test_effectful_or_unreviewed_forms_require_existing_approval(command):
    assert not _is_readonly(command), command
    registry = object.__new__(MCPRegistry)
    registry._tools = {ShellExecTool.name: ShellExecTool()}
    assert registry.requires_approval(ShellExecTool.name, {"command": command})


@pytest.mark.parametrize("command", [
    "ls -la /tmp", "pwd", "cat -- input", "head -n 10 input", "tail -n10 input",
    "wc -l input", "stat -f %z input", "file input", "grep -rn TODO src",
    "rg --no-config -n --glob '*.py' TODO src", "find . -name '*.py' -type f -print",
    "fd --type f py src", "sort -nr input", "uniq -c input", "cut -d, -f1 input",
    "diff -u a b", "cmp a b", "xxd -l 32 input", "date +%Y-%m-%d",
    "hostname", "ifconfig", "man ls", "python --version", "python3 -V",
    "node --version", "ruby --version", "git branch --list", "git tag --list",
    "git config --get core.editor", "git rev-parse --show-toplevel",
    "ls --help", "grep --help", "arch",
])
def test_reviewed_readonly_queries_remain_automatic(command):
    assert _is_readonly(command), command
    assert not ShellExecTool().needs_approval({"command": command})


@pytest.mark.parametrize("command, readonly", [
    ("rg -e --no-config pattern input", False),
    ("rg -- --no-config input", False),
    ("rg --regexp --no-config pattern input", False),
    ("rg -ne --no-config pattern input", False),
    ("rg --no-config -e pattern input", True),
    ("rg -e --no-config --no-config input", True),
    ("rg --no-config -- --no-config input", True),
    ("rg pattern --no-config input", True),
])
def test_rg_no_config_must_be_consumed_as_a_flag(command, readonly):
    assert _is_readonly(command) is readonly, command
    registry = object.__new__(MCPRegistry)
    registry._tools = {ShellExecTool.name: ShellExecTool()}
    assert registry.requires_approval(ShellExecTool.name, {"command": command}) is not readonly


def test_shell_call_pauses_before_executor_and_denial_never_executes(make_session, tmp_path):
    from framework.core.orchestrator import Orchestrator
    from .fake_engine import FakeEngine, call
    from .test_orchestrator_lifecycles import GEN, begin_turn

    class NeverExecute(ShellExecTool):
        def execute(self, **kwargs):
            pytest.fail("approval must precede shell execution")

    registry = MCPRegistry(tmp_path / "empty-tools")
    registry.register(NeverExecute())
    command = "sort -o output input"
    engine = FakeEngine([{"tool_calls": [call(ShellExecTool.name, command=command)]},
                         {"text": "已取消。"}])
    orch = Orchestrator(engine, registry, {"react_max_steps": 4, "max_total_iterations": 12})
    session = make_session()
    session.add_user_message("排序")
    begin_turn(session)
    pending = asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))
    assert pending["status"] == "pending_approval"
    assert pending["pending_tools"][0]["arguments"] == {"command": command}
    resumed = asyncio.run(orch.resume_after_approval(
        session, approved=False, journal=session.journal, **GEN))
    assert resumed["status"] == "success"
    assert not any(e["type"] == "tool_started" for e in session.journal.ring)
