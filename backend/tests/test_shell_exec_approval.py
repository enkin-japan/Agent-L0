"""终端审批按操作和参数判定；这里只分类或驱动假执行器，绝不运行命令文本。"""
import asyncio

import pytest

from framework.mcp.registry import MCPRegistry
from framework.mcp.tools import shell_exec as _se
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
    "wc -l input", "stat -f %z input", "file input", "find . -name '*.py' -type f -print",
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
    # rg 默认递归：搜索根须在仓库内，故在仓库根下判（`input` = ROOT/input）。
    root = _se._REPO_ROOT
    assert _is_readonly(command, cwd=root) is readonly, command
    registry = object.__new__(MCPRegistry)
    registry._tools = {ShellExecTool.name: ShellExecTool()}
    assert registry.requires_approval(
        ShellExecTool.name, {"command": command, "cwd": root}) is not readonly


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


# ── 密钥隔离（阶段复审确认的暴露路径）────────────────────────────────────────
# 全部是合成值，经 monkeypatch 注入；读文件的用例只读 tmp_path 下自己造的文件。
_SYNTH = "synthetic-key-1234567890"


def _needs_approval(command, cwd=None):
    args = {"command": command} if cwd is None else {"command": command, "cwd": cwd}
    registry = object.__new__(MCPRegistry)
    registry._tools = {ShellExecTool.name: ShellExecTool()}
    return registry.requires_approval(ShellExecTool.name, args)


def test_child_env_drops_secret_variables(monkeypatch, tmp_path):
    """★ 子进程拿不到密钥变量：名单内的、按命名形态判定的都剔掉，普通变量照传。"""
    monkeypatch.setattr(_se, "_USER_ENV_CACHE", {})
    for name in ("AGENT_API_KEY", "INFER_API_KEY", "RAG_API_KEY", "RAG_COMPUTE_API_KEY",
                 "GIT_TOKEN", "HF_TOKEN", "APNS_KEY_ID", "SOME_SERVICE_TOKEN",
                 "MY_DB_PASSWORD", "X_CLIENT_SECRET", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, _SYNTH)
    monkeypatch.setenv("AGENT_DATA_DIR_SYNTH_CONTROL", "plain-value-ok")
    env = _se._user_env()
    leaked = [k for k in env if _se._is_secret_env_name(k)]
    assert not leaked, leaked
    assert "AGENT_API_KEY" not in env and "MY_DB_PASSWORD" not in env
    assert env.get("AGENT_DATA_DIR_SYNTH_CONTROL") == "plain-value-ok"
    assert env.get("PATH")

    # 端到端：真起子进程跑 `env`，变量名本身都不该出现（只断言值会被脱敏层掩盖）。
    # 只读表里没有能打印环境的命令（这正是要的），故把 `env` 当作免审批命令来跑：
    # execute() 按 `_is_readonly` 选环境，这里桩掉它，验的是「免审批 → 全剔版」这条路。
    monkeypatch.setattr(_se, "_is_readonly", lambda command, cwd="": True)
    out = ShellExecTool().execute("env", cwd=str(tmp_path))
    assert out.startswith("exit_code: 0"), out[:200]
    names = {line.split("=", 1)[0] for line in out.splitlines() if "=" in line}
    assert "AGENT_API_KEY" not in names
    assert "SOME_SERVICE_TOKEN" not in names and "HF_TOKEN" not in names
    assert "AGENT_DATA_DIR_SYNTH_CONTROL" in names
    assert _SYNTH not in out


def test_approved_commands_keep_user_tokens_but_never_backend_keys(monkeypatch, tmp_path):
    """★ 用户点过头的命令要能用 gh / huggingface-cli：保留 HF_TOKEN/GH_TOKEN，只剔后端四把钥匙。"""
    monkeypatch.setattr(_se, "_USER_ENV_CACHE", {})
    for name in ("AGENT_API_KEY", "INFER_API_KEY", "RAG_API_KEY", "RAG_COMPUTE_API_KEY",
                 "HF_TOKEN", "GH_TOKEN"):
        monkeypatch.setenv(name, _SYNTH)
    env = _se._user_env(strict=False)
    assert env.get("HF_TOKEN") == _SYNTH and env.get("GH_TOKEN") == _SYNTH
    for name in ("AGENT_API_KEY", "INFER_API_KEY", "RAG_API_KEY", "RAG_COMPUTE_API_KEY"):
        assert name not in env
    # execute() 真按审批判定选环境：`env` 不在只读表里 → 需审批 → 宽松版。
    assert _needs_approval("env")
    out = ShellExecTool().execute("env", cwd=str(tmp_path))
    names = {line.split("=", 1)[0] for line in out.splitlines() if "=" in line}
    assert "HF_TOKEN" in names and "GH_TOKEN" in names
    assert "AGENT_API_KEY" not in names and "RAG_COMPUTE_API_KEY" not in names
    assert _SYNTH not in out                     # 值仍被脱敏


@pytest.mark.parametrize("name, secret", [
    ("AGENT_API_KEY", True), ("OPENAI_API_KEY", True), ("GH_TOKEN", True),
    ("APNS_TEAM_ID", True), ("DB_PASSWORD", True), ("SECRET_SAUCE", True),
    ("bench_key", True), ("PATH", False), ("HOME", False), ("AGENT_DATA_DIR", False),
    ("INFER_PORT", False), ("LANG", False), ("SSH_AUTH_SOCK", False),
])
def test_secret_env_name_classification(name, secret):
    assert _se._is_secret_env_name(name) is secret


@pytest.fixture
def synthetic_env_file(tmp_path):
    f = tmp_path / ".env.local"
    f.write_text(f"AGENT_API_KEY={_SYNTH}\n", encoding="utf-8")
    return f


@pytest.mark.parametrize("template", [
    "cat {d}/.env.local", "cat {d}/.env", "head -n 5 {d}/.env.local", "tail {d}/.env.production",
    "grep -rn KEY {d}/.env.local", "grep -r X {d}/.env*", "xxd {d}/.env.local",
    "strings {d}/.secrets.env", "cat colab.env", "cat reminder/bark.env", "cat credentials.json",
    "cat x.p8", "cat {d}/AuthKey_ABC.P8", "cat server.pem", "cat id.key", "cat cert.p12",
    "ls {d}/.keys", "ls .colab-compute", "cat backend/auth/devices.json",
    "cat backend/x/../auth/devices.json", "ls reminder",
    "grep --file={d}/.env.local x", "grep -f.env x", "cat -- .ENV.LOCAL",
    "find . -name '.env*'", "find . -name '*.p8'", "find . -path '*/auth/*'",
    "rg --no-config --glob '.env*' KEY .", "rg --no-config -g '*.pem' KEY .",
    "grep -rn KEY --include='.env*' .",
    # 搜索根点名了具体目录：只能靠通配模式本身命中，不让整片目录递归规则顶替。
    "rg --no-config -g '*.pem' KEY src", "grep -rn KEY --include='*.p12' src",
    "find src -name '*.pem'",
    # 新增的精确名与目录段
    "cat .hf_token_tmp", "cat .netrc", "cat .envrc", "cat id_rsa", "cat .ssh/id_ed25519",
    "cat id_ecdsa", "ls secrets", "ls .ssh", "ls .aws", "ls .gnupg", "cat .docker/config.json",
    # 粘连短选项：路径不从第 2 个字符起
    "grep -nf.env x", "head -n5.env",
])
def test_secret_paths_require_approval(template, synthetic_env_file):
    # cwd=仓库根：让递归读取的根落在允许范围内，命中只能来自词元层规则本身。
    command = template.format(d=synthetic_env_file.parent)
    assert not _is_readonly(command, cwd=_se._REPO_ROOT), command
    assert _needs_approval(command, cwd=_se._REPO_ROOT), command


def test_secret_path_via_cwd_requires_approval(tmp_path):
    auth = tmp_path / "backend" / "auth"
    auth.mkdir(parents=True)
    assert _needs_approval("cat devices.json", cwd=str(auth))
    assert _needs_approval("cat auth/devices.json", cwd=str(tmp_path / "backend"))
    assert _needs_approval("ls", cwd=str(tmp_path / ".keys"))
    assert not _needs_approval("cat README.md", cwd=str(tmp_path))
    assert not _needs_approval("ls", cwd=str(tmp_path / "backend"))


@pytest.mark.parametrize("command", [
    "cat README.md", "ls", "ls -la", "git branch --list", "git rev-parse --show-toplevel",
    "grep -rn TODO src", "cat environment.md", "cat docs/.envrc.md.txt", "cat reminders.txt",
    "find . -name '*.py' -type f -print", "cat keys.txt", "cat monkey.keynote.txt",
    "ps aux", "ps -ax", "ps -o pid,command", "ps -ef", "ps -axo pid,command",
    "find . -name '*.json'", "rg --no-config -n --glob '*.py' TODO src",
])
def test_ordinary_reads_stay_automatic(command):
    assert _is_readonly(command, cwd=_se._REPO_ROOT), command
    assert not _needs_approval(command, cwd=_se._REPO_ROOT), command


@pytest.mark.parametrize("command", ["ps -E", "ps e", "ps auxe", "ps -axE", "ps -Aww -E", "ps -eE"])
def test_ps_environment_display_requires_approval(command):
    assert not _is_readonly(command), command
    assert _needs_approval(command), command


def test_redaction_replaces_secret_values(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", _SYNTH)
    monkeypatch.setenv("RAG_API_KEY", "abc1234")              # < 8 字符：不替换
    monkeypatch.setenv("PLAIN_SETTING", "plain-value-12345")   # 非密钥名：不替换
    text = f"a {_SYNTH} b abc1234 c plain-value-12345"
    out = _se._redact_secrets(text)
    assert _SYNTH not in out
    assert "[REDACTED:AGENT_API_KEY]" in out
    assert "abc1234" in out and "plain-value-12345" in out


def test_tool_output_is_redacted_end_to_end(monkeypatch, tmp_path, synthetic_env_file):
    """审批后真去读合成 `.env.local`（以及 stderr 路径），值都不能原样回到模型上下文。"""
    monkeypatch.setenv("AGENT_API_KEY", _SYNTH)
    monkeypatch.setattr(_se, "_USER_ENV_CACHE", {})
    out = ShellExecTool().execute(f"cat {synthetic_env_file}", cwd=str(tmp_path))
    assert out.startswith("exit_code: 0"), out[:200]
    assert _SYNTH not in out
    assert "AGENT_API_KEY=[REDACTED:AGENT_API_KEY]" in out
    err = ShellExecTool().execute(f"cat {synthetic_env_file} >&2", cwd=str(tmp_path))
    assert "stderr:" in err and _SYNTH not in err and "[REDACTED:AGENT_API_KEY]" in err


@pytest.mark.parametrize("command", [
    "grep -r KEY", "grep -r KEY .", "grep -rn KEY ./", "grep -R KEY ..", "grep -r KEY ../..",
    "grep -r KEY ~", "grep -r KEY /", "grep --recursive KEY .", "grep -rn -e KEY",
    "grep -rn KEY src .", "grep -ri -e KEY .", "grep -nr KEY ~/",
    "rg --no-config KEY", "rg --no-config KEY .", "rg --no-config -n KEY ~",
    "rg --no-config -e KEY", "rg --no-config --files", "rg --no-config KEY src /",
])
def test_broad_recursive_search_requires_approval(command):
    """★ 不点名路径的递归搜索会把当前目录下的 `.env.local` 一并读出，必须审批。"""
    assert not _is_readonly(command), command
    assert _needs_approval(command), command


@pytest.mark.parametrize("command", [
    "grep -rn TODO src", "grep -r -e KEY src", "grep -rn KEY src/framework tests",
    "grep KEY .", "grep -n KEY README.md", "rg --no-config foo backend/framework",
    "rg --no-config -e foo src", "rg --no-config --files src", "grep -r .. src",
    "grep -rn TODO backend/framework", "grep -d recurse KEY backend/framework",
    "grep --directories=recurse KEY backend/framework", "grep -d skip KEY backend",
    "diff -r backend/framework backend/tests", "diff -u a b",
])
def test_scoped_or_non_recursive_search_stays_automatic(command):
    assert _is_readonly(command, cwd=_se._REPO_ROOT), command
    assert not _needs_approval(command, cwd=_se._REPO_ROOT), command


# ── 递归读取：根路径解析 + 允许范围（cwd = 仓库根）────────────────────────────
# 这一组刻意只放**字面量规则看不出**的根（没有 `.`/`~`/`/` 字面、词元层也不命中），
# 它们变红才是「根路径解析」生效的证据。

@pytest.mark.parametrize("template", [
    "grep -r KEY backend", "grep -r . {root}", "grep -r KEY /Users/x",
    "rg --no-config -uuu KEY backend",
    "grep -r KEY backend/push", "grep -r KEY backend/registrations/x", "grep -r KEY {root}/..",
    "rg --no-config KEY backend/framework /opt",
])
def test_recursive_root_resolution_requires_approval(template):
    root = _se._REPO_ROOT
    command = template.format(root=root)
    assert not _is_readonly(command, cwd=root), command
    assert _needs_approval(command, cwd=root), command


def test_recursive_root_defaults_to_cwd(tmp_path):
    root = _se._REPO_ROOT
    assert _needs_approval("grep -r KEY", cwd=f"{root}/backend")          # cwd 是 auth 的祖先
    assert not _needs_approval("grep -r KEY", cwd=f"{root}/backend/framework")
    assert _needs_approval("grep -r KEY", cwd=str(tmp_path))              # 仓库外
    assert _needs_approval("grep -rn TODO src")                          # 无 cwd = ~ 下


def test_recursive_root_follows_symlinks_and_home_locations(monkeypatch, tmp_path):
    repo, outside, home = tmp_path / "repo", tmp_path / "outside", tmp_path / "repo" / "home"
    (repo / "src").mkdir(parents=True)
    outside.mkdir()
    (home / "Library").mkdir(parents=True)
    (repo / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(_se, "_REPO_ROOT", str(repo))
    monkeypatch.setenv("HOME", str(home))
    assert _needs_approval("grep -r KEY link", cwd=str(repo))            # 符号链接出仓库
    assert _needs_approval("grep -r KEY home/Library", cwd=str(repo))    # ~/Library/Keychains 的祖先
    assert _needs_approval("grep -r KEY home/.config", cwd=str(repo))    # ~/.config/gh 的祖先
    assert not _needs_approval("grep -r KEY src", cwd=str(repo))


@pytest.mark.parametrize("command", [
    "grep -d recurse KEY backend", "grep --directories=recurse KEY backend",
    "grep -drecurse KEY backend",
])
def test_grep_directories_recurse_is_recursive(command):
    assert _needs_approval(command, cwd=_se._REPO_ROOT), command


@pytest.mark.parametrize("command", [
    "diff -rN . /var/empty", "diff -r backend/framework /var/empty",
    "diff --recursive backend/framework backend",
])
def test_recursive_diff_requires_approval(command):
    assert _needs_approval(command, cwd=_se._REPO_ROOT), command


@pytest.mark.parametrize("command", [
    "cat .\u017fecrets.env", "cat .\uff25NV", "cat \uff0eenv", "cat \uff21\uff22.txt",
    "cat \u212a.txt", "cat \ufb01le.txt",
])
def test_non_ascii_tokens_require_approval(command):
    assert not _is_readonly(command, cwd=_se._REPO_ROOT), command
    assert _needs_approval(command, cwd=_se._REPO_ROOT), command


@pytest.mark.parametrize("command", [
    "grep -n reminder notes.txt", "grep -e .env notes.txt", "grep -n credentials.json notes.txt",
    "rg --no-config -n secrets backend/framework", "grep -rn .ssh backend/framework",
])
def test_search_pattern_is_not_treated_as_a_path(command):
    """误伤回归：模式词元不是路径。"""
    assert _is_readonly(command, cwd=_se._REPO_ROOT), command
    assert not _needs_approval(command, cwd=_se._REPO_ROOT), command


@pytest.mark.parametrize("command", [
    "grep -n KEY reminder/notes.txt",          # 路径操作数照判
    "grep -m 1 reminder notes.txt",            # 模式前有独立选项值：解析可能错位，宁可审批
    "grep -f .env notes.txt",                  # -f 的值是文件
])
def test_search_paths_and_ambiguous_patterns_still_checked(command):
    assert _needs_approval(command, cwd=_se._REPO_ROOT), command


@pytest.mark.parametrize("command", [
    "cat \u7b14\u8bb0.md", "cat notes-\u7b14\u8bb0.md", "grep -rn \u4e2d\u6587 backend/framework",
    "cat \u30ce\u30fc\u30c8.txt",
])
def test_plain_non_ascii_text_stays_automatic(command):
    """误伤回归：中文/日文不会被折叠成 ASCII，照常免审批。"""
    assert _is_readonly(command, cwd=_se._REPO_ROOT), command
    assert not _needs_approval(command, cwd=_se._REPO_ROOT), command


@pytest.mark.parametrize("command", ["grep -rn foo apple-app/Sources", "grep -rn foo apple-app"])
def test_apple_app_tree_is_not_a_secret_location(command):
    assert _is_readonly(command, cwd=_se._REPO_ROOT), command
    assert not _needs_approval(command, cwd=_se._REPO_ROOT), command



# ── 数据目录里的密钥位置（api_tools/.secrets.env、auth、push、registrations）────────

def test_backend_api_tools_is_a_secret_location(monkeypatch, tmp_path):
    # 数据根挪到别处：让 dev 形态的 data_dir（= backend/）不替仓库根清单兜底。
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path / "elsewhere"))
    root = _se._REPO_ROOT
    assert _needs_approval("grep -r KEY backend/api_tools", cwd=root)
    assert _needs_approval("cat backend/api_tools/tools.yaml", cwd=root)
    assert _needs_approval(f"cat {root}/backend/api_tools/tools.yaml")
    assert _needs_approval("cat backend/api_tools/.secrets.env", cwd=root)   # 精确名规则（回归）
    assert not _needs_approval("grep -rn foo backend/framework", cwd=root)


def test_data_dir_secret_subdirs_follow_agent_data_dir(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / "var" / "data" / "auth").mkdir(parents=True)
    (repo / "var" / "other").mkdir(parents=True)
    monkeypatch.setattr(_se, "_REPO_ROOT", str(repo))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_DATA_DIR", str(repo / "var" / "data"))
    # 递归根：data_dir/auth 本身、data_dir（auth 的祖先）→ 审批；同级无关目录照常
    assert _needs_approval("grep -r KEY var/data/auth", cwd=str(repo))
    assert _needs_approval("grep -r KEY var/data", cwd=str(repo))
    assert not _needs_approval("grep -r KEY var/other", cwd=str(repo))
    # 词元层点名：数据根在仓库外（app 形态）时，绝对路径点名其中的密钥目录
    app_data = tmp_path / "appdata"
    monkeypatch.setenv("AGENT_DATA_DIR", str(app_data))
    for sub in ("auth", "push", "registrations", "api_tools"):
        assert _needs_approval(f"cat {app_data}/{sub}/x.json"), sub
    assert not _needs_approval(f"cat {app_data}/sessions/x.json")


def test_app_support_data_root_is_a_fixed_home_location(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path / "elsewhere"))
    target = home / "Library" / "Application Support" / "dev.enkin.macagent" / "push" / "t.json"
    assert _needs_approval(f"cat '{target}'")
