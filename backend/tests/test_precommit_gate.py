"""提交路径上的两道执行器金测试（波 A0）。

一、`scripts/git-hooks/pre-commit`——把 `devctl check` 接到提交上。
    守：白名单**只**认 `.md` 与 `benchmarks/data/`，且必须是**全部**暂存路径都命中才跳过；
    掺一个代码文件就得跑门（白名单放宽一格 = 守卫在某类改动上静默关掉自己）；
    退出码原样透传，不许把「不过」压成「过」。

二、`.claude/hooks/block-no-verify.sh`——PreToolUse(Bash) 拦 `git commit --no-verify|-n`。
    钩子挡得住「忘了跑门」，挡不住「把门一键关掉」，这条补的是后者。
    契约的反直觉处：PreToolUse **只有 exit 2 阻断**，其它非零码是「非阻断错误」——
    钩子自己崩了会放行。故本组同时守两个方向：
      · 该拦的要拦（含 python3 缺席时对长旗标的原文兜底）；
      · **畸形输入绝不许 exit 2**——恒阻断会把整个会话锁死，比漏判严重得多。

真跑 git 与 bash；缺任一即跳过。
"""
import json
import os
import shutil
import stat
import subprocess

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HOOK_SRC = os.path.join(_REPO, "scripts", "git-hooks", "pre-commit")
_GUARD_SRC = os.path.join(_REPO, ".claude", "hooks", "block-no-verify.sh")

_DEVCTL_STUB = """\
#!/bin/bash
echo "STUB-DEVCTL-CALLED $*" >> "$(dirname "$0")/.devctl-calls"
exit "${FAKE_CHECK_RC:-0}"
"""

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="需要 bash 与 git",
)


# ============ 一、pre-commit 钩子 ============

# ★ 必须把 git 的环境变量摘干净再跑。git 在 pre-commit 里会导出 GIT_INDEX_FILE /
# GIT_PREFIX（实测），而 `git --git-dir=… commit`、GUI 客户端、rebase 派生的钩子链
# 还会导出 GIT_DIR/GIT_WORK_TREE。本组测试从这次改动起**就在提交路径上**，一旦继承
# 这些变量：`git init`/`git add` 会作用到**别人的真仓库**上，而且这几条会当场变红 →
# check 回 3 → 所有提交被自己挡死。所以既清 env，也给每个 git 调用显式 cwd。
_GIT_ENV_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR", "GIT_PREFIX", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES", "GIT_NAMESPACE",
)


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_VARS}
    env.update(extra)
    return env


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=str(cwd), check=True,
                          capture_output=True, env=_clean_env())


def _git_repo(tmp_path):
    """临时 git 仓：真钩子 + devctl 桩（记录是否被调用、按 FAKE_CHECK_RC 退出）。"""
    _git(["-c", "init.defaultBranch=main", "init", "-q", "."], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)

    hook = tmp_path / "pre-commit"
    shutil.copy(_HOOK_SRC, hook)
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

    devctl = tmp_path / "devctl.sh"
    devctl.write_text(_DEVCTL_STUB, encoding="utf-8")
    devctl.chmod(devctl.stat().st_mode | stat.S_IEXEC)
    return hook


def _stage(tmp_path, *rel_paths):
    for rel in rel_paths:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
        _git(["add", "--", rel], tmp_path)


def _run_hook(tmp_path, hook, check_rc=0):
    return subprocess.run(["bash", str(hook)], cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=60,
                          env=_clean_env(FAKE_CHECK_RC=str(check_rc)))


def _check_was_called(tmp_path):
    return (tmp_path / ".devctl-calls").exists()


def test_docs_only_diff_is_waved_through_without_running_any_gate(tmp_path):
    """文档-only 直接放行、一门都不跑。曾经的语义是「只跑 docs 门」——那道门守的
    规模表住在 `EXECUTION_LEDGER.md` 里（首审 Critical 3）；2026-08-29 台账冻结
    归档、docs 门退役后，文档改动不再有守卫对象，快路径由「只跑 docs 门」变为放行。"""
    hook = _git_repo(tmp_path)
    _stage(tmp_path, "docs/notes.md", "README.md")
    r = _run_hook(tmp_path, hook)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not _check_was_called(tmp_path), "文档-only 不该再起任何门"


def test_skill_md_is_functional_content_not_documentation(tmp_path):
    """★ `skills/*.md` 是运行时剧本（skill_registry 启动即解析 frontmatter，坏了会炸），
    不是文档。docs 门退役后文档-only 快路径变成零门放行，若剧本也被当文档放行，
    改坏一份剧本会绿灯入库、启动时才炸。故 skills/ 下的 .md 必须走全门。"""
    hook = _git_repo(tmp_path)
    _stage(tmp_path, "skills/broken-playbook.md")
    r = _run_hook(tmp_path, hook)
    assert _check_was_called(tmp_path), r.stdout + r.stderr


def test_backend_skills_md_is_also_not_whitelisted(tmp_path):
    """用户自建剧本目录 backend/skills/ 同理。"""
    hook = _git_repo(tmp_path)
    _stage(tmp_path, "backend/skills/user-playbook.md")
    r = _run_hook(tmp_path, hook)
    assert _check_was_called(tmp_path), r.stdout + r.stderr


def test_benchmarks_data_files_take_the_light_path(tmp_path):
    hook = _git_repo(tmp_path)
    _stage(tmp_path, "benchmarks/data/soak/req.jsonl", "benchmarks/data/soak/summary.json")
    r = _run_hook(tmp_path, hook)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not _check_was_called(tmp_path)


def test_a_script_inside_benchmarks_data_is_not_whitelisted(tmp_path):
    """★ 白名单曾写成整个 `benchmarks/data/*` 并注明「无可执行内容」——而该目录下
    真躺着 t4_divergence_probe.py / t4_verdict.py 两个脚本。整目录放行 = 改这两个
    文件时三门被静默跳过。"""
    hook = _git_repo(tmp_path)
    _stage(tmp_path, "benchmarks/data/mtp-t4-divergence/t4_verdict.py")
    r = _run_hook(tmp_path, hook)
    assert _check_was_called(tmp_path), r.stdout + r.stderr


def test_one_code_file_among_docs_still_runs_the_gates(tmp_path):
    """★ 白名单必须是「全部命中才跳」，不是「有命中就跳」。"""
    hook = _git_repo(tmp_path)
    _stage(tmp_path, "README.md", "backend/framework/thing.py")
    r = _run_hook(tmp_path, hook)
    assert _check_was_called(tmp_path), r.stdout + r.stderr
    assert r.returncode == 0


def test_benchmarks_code_is_not_whitelisted(tmp_path):
    """benchmarks/ 下的**脚本**不在白名单里——只有 benchmarks/data/ 的档案才是。"""
    hook = _git_repo(tmp_path)
    _stage(tmp_path, "benchmarks/bench_baseline.py")
    r = _run_hook(tmp_path, hook)
    assert _check_was_called(tmp_path), r.stdout + r.stderr


def test_gate_failure_blocks_the_commit_and_passes_rc_through(tmp_path):
    hook = _git_repo(tmp_path)
    _stage(tmp_path, "backend/framework/thing.py")
    r = _run_hook(tmp_path, hook, check_rc=3)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "提交已中止" in r.stderr


def test_gate_unrunnable_also_blocks_the_commit(tmp_path):
    """rc=1「跑不起来」同样必须挡住提交——门不可信时提交就是在盲提。"""
    hook = _git_repo(tmp_path)
    _stage(tmp_path, "backend/framework/thing.py")
    r = _run_hook(tmp_path, hook, check_rc=1)
    assert r.returncode == 1, r.stdout + r.stderr


def test_empty_stage_is_explicitly_allowed(tmp_path):
    hook = _git_repo(tmp_path)
    r = _run_hook(tmp_path, hook)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not _check_was_called(tmp_path)
    assert "无暂存文件" in r.stdout


def test_hook_survives_the_very_first_commit(tmp_path):
    """初始提交没有 HEAD：钩子不许在这里崩（崩了等于新克隆第一次提交就绕过门）。
    必须连 returncode 一起断言——只查「输出里没有 fatal」的话，钩子在初始提交上
    回 1 但不打 fatal 时这条照样绿。"""
    hook = _git_repo(tmp_path)          # 刻意不建任何提交，HEAD 不存在
    _stage(tmp_path, "backend/framework/thing.py")
    r = _run_hook(tmp_path, hook)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fatal" not in (r.stdout + r.stderr).lower()
    assert _check_was_called(tmp_path)


# ============ 二、--no-verify 守卫 ============

def _guard(command: str, *, path_override=None):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = dict(os.environ)
    if path_override:
        env["PATH"] = path_override
    return subprocess.run(["bash", _GUARD_SRC], input=payload, capture_output=True,
                          text=True, env=env, timeout=60)


# (命令, 阻断理由里必须出现的串)。第二项不能省：只断言 rc==2 的话，
# 「拦到了但拦的是别的东西」与「拦对了」分不开——守卫的固定文案本身就含 --no-verify，
# 于是 `"no-verify" in stderr` 是一条恒真断言，杀不死任何变异体。
BLOCKED = [
    ("git commit --no-verify -m x", "--no-verify"),
    ("git commit -n -m x", "-n"),
    ("git commit -nm x", "-nm"),
    ("git commit --amend -n --no-edit", "-n"),
    ("cd /tmp/foo && git commit -n -m x", "-n"),
    ("git -c user.name=x commit -n -m x", "-n"),
    ("/usr/bin/git commit --no-verify", "--no-verify"),
    # ★ 分隔符出现在**引号内部**。先按分隔符预切、再切词的实现会在这里把命令切碎，
    #   随后因引号不闭合判为「不违规」并静默放行。
    ('git commit -n -m "fix: a|b"', "-n"),
    # ★ 本仓 CLAUDE.md 强制的多行 commit message（Co-Authored-By / Claude-Session
    #   各占一行）。这是本仓最主流的提交形态，曾整个漏过。
    ('git commit -n -m "标题\n\nCo-Authored-By: X\nClaude-Session: Y"', "-n"),
    ("env git commit -n -m x", "-n"),                    # 透明包裹层
    ("GIT_AUTHOR_NAME=x git commit -n -m x", "-n"),      # VAR=VAL 前缀
    ('bash -c "git commit -n -m x"', "-n"),              # 内层 shell
    ("(git commit -n -m x)", "-n"),                      # 括号子 shell
    ("git commit --no-verif -m x", "--no-verif"),        # git 允许的前缀缩写
    # core.hooksPath 与 --no-verify 等效（整套钩子消失），git config 那条更是永久性的
    ("git -c core.hooksPath=/dev/null commit -m x", "core.hooksPath"),
    ("git config core.hooksPath /dev/null", "core.hooksPath"),
    # ★ 多行**命令序列**（阶段复审 H1）:shlex 缺省把换行当空白吃掉,SEPS 里的 "\n"
    #   永远等不到 token——add/cd 起头的多行命令被拼成一段,首段无辜即整条放行。
    #   这是本仓 agent 最常见的提交形态,曾整个漏过(引号内换行那条只护了另一半)。
    ("git add -A\ngit commit -n -m wave", "-n"),
    ("cd /Volumes/enkin/project/Agent\ngit commit --no-verify -m x", "--no-verify"),
    ("git status\ngit add -A\ngit commit -nm x", "-nm"),
]

ALLOWED = [
    'git commit -m "fix"',
    'git commit -m "标题\n\nCo-Authored-By: X"',                  # 多行但正常
    'git commit -m "add -n flag and --no-verify to the docs"',   # 引号内的假阳性
    'git commit -m"none"',                                       # -m 贴写值，含 n
    'git commit -am"cleanup"',                                   # 短旗标簇 + 贴写值
    'git add -A\ngit commit -m "clean"',                         # 干净的多行序列:不许误拦
    "git log -n 5 --oneline",
    "git add . && git commit -m ok",
    "grep -rn commit devctl.sh",
    "git commit --amend --no-edit",
    "ls -la",
]


@pytest.mark.parametrize("cmd,reason", BLOCKED)
def test_guard_blocks_bypass(cmd, reason):
    r = _guard(cmd)
    assert r.returncode == 2, f"{cmd!r} 未被拦下（rc={r.returncode}）"
    assert reason in r.stderr, f"拦是拦了，但理由不是 {reason!r}：{r.stderr}"


@pytest.mark.parametrize("cmd", ALLOWED)
def test_guard_allows_normal_commands(cmd):
    r = _guard(cmd)
    assert r.returncode == 0, f"{cmd!r} 被误拦（rc={r.returncode}, {r.stderr}）"


def test_guard_falls_back_to_raw_grep_when_python3_is_broken(tmp_path):
    """★ 解析失败绝不等于放行：python3 崩了，长旗标仍要拦住。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    py = fake_bin / "python3"
    py.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    path = f"{fake_bin}:/usr/bin:/bin"

    blocked = _guard("git commit --no-verify -m x", path_override=path)
    assert blocked.returncode == 2, blocked.stderr

    allowed = _guard('git commit -m "fix"', path_override=path)
    assert allowed.returncode == 0, allowed.stderr


# ★ 每一条都**必须含 `commit`**，否则会在守卫开头的零成本短路处就 exit 0，
#   python 根本不会被调用——那样这些用例验的是短路，不是「解析失败时的行为」，
#   杀不死「解析失败就 exit 2」这个变异体。
MALFORMED = [
    "not json at all but mentions commit",
    '{"tool_input": {"command": ',                 # 截断
    '{"tool_input": null, "hint": "commit"}',      # tool_input 不是对象
    '{"tool_input": {"command": 42}, "h": "commit"}',   # command 不是字符串
    '{"tool_input": {"command": "git commit -m \\"unclosed}',  # 引号不闭合
    '[{"commit": 1}]',                             # 顶层不是对象
]


@pytest.mark.parametrize("payload", MALFORMED)
def test_guard_never_hard_blocks_on_malformed_input(payload):
    """★ 反向守卫：恒 exit 2 会把整个会话彻底锁死，比漏判严重得多。
    畸形输入既拿不到命令、原文里也没有危险串时，唯一正确的行为是放行。"""
    r = subprocess.run(["bash", _GUARD_SRC], input=payload, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, f"畸形输入 {payload!r} 触发了阻断（rc={r.returncode}）"


def test_guard_still_blocks_when_json_is_malformed_but_text_is_dangerous():
    """解析不出来 ≠ 放行：原文兜底要接住长旗标。"""
    r = subprocess.run(["bash", _GUARD_SRC], capture_output=True, text=True, timeout=60,
                       input='{"tool_input": {"command": "git commit --no-verify -m unclosed')
    assert r.returncode == 2, r.stdout + r.stderr


def test_guard_is_wired_into_settings_json():
    """守卫脚本再对，没接进 settings.json 也等于不存在。

    注：不断言脚本的执行位——settings 里配的是 `bash "<path>"`，不经执行位；
    断言它既不是生效的必要条件、也不是充分条件（原写法两头不沾）。改为断言那条
    命令确实以 bash 起、且路径可读。
    """
    settings = json.loads(
        open(os.path.join(_REPO, ".claude", "settings.json"), encoding="utf-8").read()
    )
    entries = settings["hooks"]["PreToolUse"]
    cmds = [h["command"] for e in entries if e.get("matcher") == "Bash" for h in e["hooks"]]
    wired = [c for c in cmds if "block-no-verify.sh" in c]
    assert wired, cmds
    assert all(c.strip().startswith("bash ") for c in wired), wired
    assert os.access(_GUARD_SRC, os.R_OK)
