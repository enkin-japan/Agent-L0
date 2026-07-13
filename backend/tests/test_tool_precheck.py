"""工具参数预检单元测试:shell 引号硬伤在审批前被确定性拦截。

守护的失败形态(实测,复测会话 d618f0d6):模型把函数调用语法带进命令
(`mkdir -p ~/Desktop/W12_Exercises')`),用户批准了一条注定 `zsh: unmatched '`
的命令,修正后又要一次审批——一次审批点击被浪费在必败调用上。
"""
from framework.mcp.tools.shell_exec import ShellExecTool


def check(cmd):
    return ShellExecTool().validate_arguments({"command": cmd})


def test_unmatched_quote_rejected():
    err = check("mkdir -p ~/Desktop/W12_Exercises')")
    assert err is not None and err.startswith("System Error:")
    assert "NOT sent for user approval" in err


def test_normal_commands_pass():
    assert check("mkdir -p ~/Desktop/W12_Exercises") is None
    assert check("ls -la /tmp") is None
    assert check('grep -r "TODO" src/') is None
    assert check("echo \"it's fine\"") is None      # 双引号内的撇号合法


def test_heredoc_skipped_not_false_positive():
    # heredoc 正文可含奇数引号(don't),shlex 解析不了正文 → 预检跳过,交执行路径
    assert check("cat <<EOF\ndon't panic\nEOF") is None


def test_empty_command_rejected():
    assert check("") is not None
    assert check("   ") is not None


def test_registry_hook_shape():
    """registry.validate_call 的委托契约:无 validate_arguments 的工具返回 None。"""
    from framework.mcp.registry import MCPRegistry
    assert callable(getattr(MCPRegistry, "validate_call"))
