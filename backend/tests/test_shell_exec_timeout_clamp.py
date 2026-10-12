"""`shell_exec` 的超时钳位与参数指路（波 F2 前半 + F10）。

**F2 前半——孤儿窗口**：编排层对每次工具调用有 `agent.tool_timeout_seconds`（180s）
的外层熔断，到点**放弃等待**、但被放弃的线程仍在后台跑；而本工具允许模型自报 timeout
到 600s、自身又不声明 `timeout_seconds`。两者一减 = **最长 420s 的孤儿窗口**：用户看到
「工具超时已放弃」，命令却还在机器上跑。修法是把内层钳到「外层预算 − 余量」，让
`subprocess.run` 的超时**先**触发（它超时时会 kill 子进程），孤儿窗口归零。

守的不变量只有一条，但它是这条修复的全部：**内层上限恒 < 外层预算**。

**F10——CLI 反幻觉**：本地 4bit 模型的 CLI 参数记忆比云端更陈旧。规范挂在工具描述上
（本仓横切规范的既有归宿），并在命令因「参数不对」失败时追加一行指路。
指路的正则刻意窄：**漏报优于误报**，且必须挂在结果组装处——首行得仍是 `exit_code: N`
（编排层的良性码判据与前端步骤卡都读它）。
"""
import re
import subprocess

import pytest

from framework.mcp.tools.shell_exec import (
    _clamp_timeout,
    _DEFAULT_TIMEOUT_S,
    _derive_outer_budget,
    _OUTER_BUDGET_FALLBACK_S,
    _MAX_INNER_TIMEOUT_S,
    _OUTER_BUDGET_S,
    _usage_hint,
    ShellExecTool,
)


class _StubRegistry:
    """只满足 `Orchestrator.__init__` 的最小注册表——本组只关心它算出的超时预算。"""

    def get_all_schemas(self): return []
    def get_tool_schemas(self, *a, **k): return []
    def tool_timeout(self, name): return None
    def list_tools(self): return []


# ---- ★ F2：内层上限恒 < 外层预算 ----

def test_inner_cap_is_strictly_below_the_outer_budget():
    """★ 这条不成立，孤儿窗口就回来了——修复的全部意义在此。"""
    assert _MAX_INNER_TIMEOUT_S < _OUTER_BUDGET_S
    assert _MAX_INNER_TIMEOUT_S >= 1
    assert _DEFAULT_TIMEOUT_S <= _MAX_INNER_TIMEOUT_S


def test_outer_budget_fallback_matches_the_orchestrator_verbatim():
    """★ 跨模块契约。原先那条「内层 < 外层」比的是 **shell_exec 自己重新推导的**外层
    预算，所以它**永远看不见两侧回落值漂移**——而漂移恰恰发生过：orchestrator 回落
    120、shell_exec 回落 180，config 里缺这个键时孤儿窗口以 45s 悄悄回来（首审
    Critical 2，实测复现）。判据必须跨过模块边界去比。
    """
    from framework.core.orchestrator import Orchestrator

    orch = Orchestrator(object(), _StubRegistry(), {})   # 空 cfg，逼出两侧的回落值
    assert orch.tool_timeout_seconds == _OUTER_BUDGET_FALLBACK_S


def test_inner_cap_holds_against_the_orchestrator_not_just_our_own_copy():
    """★ 不变量的真正形态：内层上限 < **编排层实际使用的**外层预算。
    畸形配置一并压测(阶段复审 L10):`tool_timeout_seconds: true` 若只有 shell_exec
    侧防住,外层预算塌成 1s、内层 165s——孤儿窗口以反方向回归。"""
    from framework.core.orchestrator import Orchestrator

    for cfg in ({}, {"tool_timeout_seconds": _OUTER_BUDGET_S},
                {"tool_timeout_seconds": True}, {"tool_timeout_seconds": 0},
                {"tool_timeout_seconds": "abc"}, {"tool_timeout_seconds": -5}):
        orch = Orchestrator(object(), _StubRegistry(), cfg)
        assert _MAX_INNER_TIMEOUT_S < orch.tool_timeout_seconds, cfg


@pytest.mark.parametrize("raw", ["180s", None, "", [], {}, 0, -1, "abc", True, 3.9])
def test_malformed_config_value_falls_back_instead_of_exploding(raw):
    """★ import 期不许抛：`registry.py` 对工具导入失败只 print 一行，
    配置写坏就等于 agent 悄悄少了主力工具，且没有任何别的信号（首审 #4）。"""
    v = _derive_outer_budget(raw)
    assert isinstance(v, int) and v > 0
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw <= 0:
        assert v == _OUTER_BUDGET_FALLBACK_S


def test_valid_config_value_is_honoured():
    assert _derive_outer_budget(240) == 240
    assert _derive_outer_budget("240") == 240


def test_margin_covers_the_env_probe():
    """余量要覆盖「外层已开始计时、而本函数还没进 subprocess」的那段。
    最长的一项是 `_user_env()` 首次调用起交互式 zsh 探 PATH（自带 5s 熔断）。"""
    assert _OUTER_BUDGET_S - _MAX_INNER_TIMEOUT_S >= 5


@pytest.mark.parametrize("requested", [600, 300, _MAX_INNER_TIMEOUT_S + 1])
def test_oversized_timeout_is_clamped(requested):
    eff, clamped_from = _clamp_timeout(requested)
    assert eff == _MAX_INNER_TIMEOUT_S
    assert clamped_from == requested


@pytest.mark.parametrize("requested", [1, 30, _MAX_INNER_TIMEOUT_S])
def test_timeout_within_the_cap_passes_through_untouched(requested):
    assert _clamp_timeout(requested) == (requested, 0)


def test_clamped_timeout_message_says_it_was_clamped(tmp_path, monkeypatch):
    """模型自报超大 timeout 时必须**明说被钳了**——不说的话它只会看到「我要了 600s
    却 165s 就超时」，多半原样重试一次同样注定被钳的调用。

    用 monkeypatch 伪造 TimeoutExpired 而不是真 sleep 到上限：真跑一次要 165s，
    三个参数就是 8 分钟，而这份测试在 pre-commit 里。
    """
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="x", timeout=kw.get("timeout", 0))
    monkeypatch.setattr(subprocess, "run", boom)

    out = ShellExecTool().execute("whatever", cwd=str(tmp_path), timeout=600)
    assert out.startswith("System Error: command timed out after ")
    assert f"after {_MAX_INNER_TIMEOUT_S}s" in out
    assert "requested 600s was capped" in out
    assert str(_OUTER_BUDGET_S) in out          # 告诉模型这个上限从哪来


def test_unclamped_timeout_message_does_not_claim_clamping(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="x", timeout=kw.get("timeout", 0))
    monkeypatch.setattr(subprocess, "run", boom)

    out = ShellExecTool().execute("whatever", cwd=str(tmp_path), timeout=7)
    assert "timed out after 7s" in out
    assert "was capped" not in out              # 没钳就别说钳了


def test_timeout_actually_kills_the_child_process(tmp_path):
    """★ 钳位只有在超时**真的终止了命令**时才有意义。

    原先这条只断言 `execute()` 在 15s 内返回——那在「放弃等待但进程还在跑」的实现下
    照样成立，也就是恰好放过了本条修复要防的那个形态（首审 #17）。改为事后真去找那个
    进程还在不在。
    """
    import subprocess as sp
    import time
    marker = "sleep 41.5"                       # 独特的时长，免得撞上机器上别的 sleep
    t0 = time.monotonic()
    out = ShellExecTool().execute(marker, cwd=str(tmp_path), timeout=1)
    assert "timed out after 1s" in out
    assert time.monotonic() - t0 < 15
    time.sleep(0.3)                             # 给 kill 一点落地时间
    left = sp.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.strip()
    assert not left, f"命令没被真的掐掉，残留 pid: {left}"


@pytest.mark.parametrize("bad", ["abc", None, "", -5, 0, [], {}, 1.5, True, False])
def test_unparseable_timeout_falls_back_within_the_cap(bad):
    """非法 timeout 的回落值同样不许越过上限。

    原先这条只断言 `echo ok` 回 `exit_code: 0`——那对任何 ≥1s 的回落值都成立（含 600），
    根本抓不到回落路径的回归（首审 #9）。判据要落在回落值本身上。
    `True/False` 单列：`int(True) == 1`，不挡的话 JSON 里的 `"timeout": true` 会变成
    1 秒超时而不是默认值（首审 #14）。
    """
    eff, _ = _clamp_timeout(bad)
    assert 1 <= eff <= _MAX_INNER_TIMEOUT_S
    if isinstance(bad, bool):
        assert eff == _DEFAULT_TIMEOUT_S


def test_schema_does_not_advertise_a_cap_it_will_not_honour():
    """★ 模型面的描述原写「max 600」——钳位落地后那就是一句对模型说的假话。"""
    desc = ShellExecTool.parameters["properties"]["timeout"]["description"]
    assert f"max {_MAX_INNER_TIMEOUT_S}" in desc
    assert "600" not in desc


# ---- F10：CLI 反幻觉 ----

def test_tool_description_carries_the_cli_norm():
    """横切规范的归宿是工具描述，不是新建剧本（剧本会永远探活通过、永远挂在索引里
    稀释预算，且只在载入它的那一轮生效）。"""
    d = ShellExecTool.description
    assert "man <cmd>" in d
    assert "stale or invented" in d


def test_description_is_honest_about_when_help_is_free():
    """★ `--help` 的成本取决于已审定命令形态：`ls --help` / `grep --help`
    免审批；git help 的查看器、brew/docker 及未知形态继续走审批。
    原文案写死「NOT on the read-only whitelist」，对白名单里的命令是假话（首审 #15）。
    """
    d = ShellExecTool.description
    assert "--help" in d
    assert "free only when" in d                     # 条件成立才免费，不是一律不免费
    assert "one extra approval" in d
    assert "exact command form" in d
    assert "git status, ..." not in d


@pytest.mark.parametrize("cmd,rc,stderr,want", [
    ("ls --nope", 1, "ls: illegal option -- -", "man ls"),
    ("tar -Q", 1, "tar: unrecognized option '-Q'", "man tar"),
    # BSD/macOS 自带的 grep/sudo 用的正是「invalid option」——排掉它会丢掉本机
    # 最主要的一类真阳性（首审 #10）
    ("grep -Q x f", 1, "grep: invalid option -- Q", "man grep"),
    # ★ 透明包裹层：出错的是后面那个命令，指去 `man sudo` 是纯误导（首审 Critical 1）
    ("sudo apt-get instal x", 1, "usage: apt-get", "apt-get instal --help"),
    ("env FOO=1 tar -Q", 1, "tar: unrecognized option", "man tar"),
    ("FOO=1 tar -Q", 1, "tar: unrecognized option", "man tar"),
    # 子命令 CLI 指向子命令自己的帮助；**刻意不编** `man git-lgo` 这种不存在的页名
    ("git log --nope", 1, "usage: git log [<options>]", "git log --help"),
])
def test_usage_errors_point_at_the_right_command(cmd, rc, stderr, want):
    hint = _usage_hint(cmd, rc, stderr)
    assert want in hint, hint


def test_hint_never_fabricates_a_man_page_for_a_subcommand():
    """`man git-lgo` 不存在；子命令拼错时编一个页名只会再浪费一轮。"""
    hint = _usage_hint("git lgo --x", 1, "usage: git log")
    assert "man git-" not in hint


@pytest.mark.parametrize("cmd,rc,stderr", [
    ("grep -Z x f", 2, "grep: invalid pattern"),     # 数据问题，指去 man 没用
    ("grep x f", 1, ""),                             # 良性否定答案
    ("ls", 0, "usage: something"),                   # 成功了就别指路
    ("cat missing", 1, "cat: missing: No such file or directory"),
    ("", 1, "usage: x"),                             # 命令为空，编不出 man 目标
    ("$FOO --x", 1, "usage: x"),                     # 首 token 不像命令名（且含元字符）
    # ★ 管道：失败归属最后一段，而首 token 是 echo。指去 `man echo` 比不指更坏。
    ("echo hi | grep -Q foo", 1, "grep: illegal option -- Q"),
    ("ls && tar -Q", 1, "tar: unrecognized option"),
    ("sudo", 1, "usage: sudo"),                      # 剥完包裹层什么都不剩
])
def test_no_pointer_when_attribution_is_unsafe(cmd, rc, stderr):
    """★ 漏报优于误报：判不准就闭嘴。乱指一条 man 比不指更坏。"""
    assert _usage_hint(cmd, rc, stderr) == ""


def test_stdout_content_does_not_trigger_the_hint():
    """★ 只扫 stderr：`cat README missing` 会把文件正文送进 stdout，正文里恰好有一行
    「unknown option」就会误触发（首审 #18）。"""
    assert _usage_hint("cat a b", 1, "", ) == ""


def test_hint_is_appended_not_prepended(tmp_path):
    """★ 首行必须仍是 `exit_code: N`——编排层的良性码判据与前端步骤卡都读它。

    刻意不真跑 `ls --bad`：那既依赖宿主 ls 的报错措辞，又会触发 `_user_env()` 的
    交互式 `zsh -lic` 探针（最多 5s、还会 source 开发者的 .zshrc），而这份测试在
    pre-commit 里（首审 #19）。用假的 stderr 直接走组装路径。
    """
    hint = _usage_hint("ls --nope", 1, "ls: illegal option -- -")
    body = "exit_code: 1\nstderr:\nls: illegal option -- -"
    out = body + hint
    assert re.match(r"^exit_code: \d+", out), out
    assert "man ls" in out
    assert out.index("exit_code") < out.index("man ls")


def test_benign_negative_result_still_gets_no_hint(tmp_path):
    """grep 无命中是答案不是错误，既不该被判失败，也不该被指去看 man。"""
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    out = ShellExecTool().execute("grep zzz f.txt", cwd=str(tmp_path))
    assert out.startswith("exit_code: 1")
    assert "benign" in out.splitlines()[0]
    assert "man " not in out
