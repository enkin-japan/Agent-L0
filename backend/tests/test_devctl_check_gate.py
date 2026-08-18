"""`devctl check` 金测试（shell 侧，实跑）——波 A0 ≡ 台账 F3「给纪律装执行器」。

守的是**三值退出码纪律**本身，不是三条门的内容：
    0 = 全过 ／ 3 = 有门判否 ／ 2 = 用法错 ／ 1 = 有门跑不起来

核心的三条（其余都是它们的边角）：
  ★ 「门跑不起来」永远不能塌缩成「门说是」——缺 venv / 缺 node_modules / 缺夹具脚本，
    以及 pytest 一条都没收集到（rc=5），一律 rc=1，绝不是 0。
  ★ 合并优先级 1 > 3：一门红 + 一门跑不起来 = 这套门整体不可信，报 1。
  ★ 反空守卫：`--only` 门名打错、或 `--skip` 把门全跳掉，一律 rc=2 而非 0——
    「一门都没跑」被记成「过」正是本仓反复得的那个病。

脚手架隔离（绝不跑真的 1062 项）：每个用例把 devctl.sh + scripts/lib_edge.sh 拷进
tmp_path 临时 ROOT，三条门各换成一个按环境变量退出的桩：
  backend → backend/.venv/bin/python 桩（认出 `-m pytest` 才按 FAKE_PYTEST_RC 退出；
            其余调用形态回 0 空输出，好让 lib_edge 载入期的 _read_cfg_hosts 正常落空）
  web     → 真 npm 跑临时 frontend/package.json 里的 `"test": "exit N"`
            （devctl.sh 会把 /opt/homebrew/bin 前置进 PATH，PATH 挂桩挂不住 npm，
             所以从 package.json 这一侧控制退出码）
  apple   → apple-app/Tests/run_reducer_fixtures.sh 桩
bash / npm / 真 backend venv 任一缺席即跳过（最小镜像兜底）。
"""
import os
import shutil
import stat
import subprocess

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEVCTL_SRC = os.path.join(_REPO, "devctl.sh")
_LIB_EDGE_SRC = os.path.join(_REPO, "scripts", "lib_edge.sh")
_REAL_BACKEND_PY = os.path.join(_REPO, "backend", ".venv", "bin", "python")

_CONFIG_YAML = """\
inference:
  host: "127.0.0.1"
  mode: "inprocess"
  remote:
    host: ""
    port: 14161
rag:
  host: "127.0.0.1"
  compute:
    host: ""
    port: 14162
"""

# `-m pytest` 才按桩码退出；别的调用形态（lib_edge 载入期读 config 的 heredoc）
# 吞掉 stdin 后回 0，避免脚手架在载入期就炸。
_PY_STUB = """\
#!/bin/bash
for a in "$@"; do
    if [ "$a" = "pytest" ]; then
        printf '%s' "${FAKE_SECRET_KEY:-}" > "$LEAK_DIR/backend"
        exit "${FAKE_PYTEST_RC:-0}"
    fi
done
case "${1:-}" in
    # eval-rag 的透传取证：把收到的每个参数各打一行，好数清楚有没有被词分割拆开
    *run_retrieval_eval.py) shift; for a in "$@"; do echo "ARG:<$a>"; done; exit "${FAKE_RAG_RC:-0}" ;;
esac
cat >/dev/null 2>&1
exit 0
"""

_APPLE_STUB = """\
#!/bin/bash
printf '%s' "${FAKE_SECRET_KEY:-}" > "$LEAK_DIR/apple"
exit "${FAKE_APPLE_RC:-0}"
"""

# 前端门经真 npm 起脚本；探针单独成文件，免去 package.json 里的多层引号转义
_WEB_PROBE = """\
#!/bin/sh
printf '%s' "${FAKE_SECRET_KEY:-}" > "$LEAK_DIR/web"
exit "${FAKE_WEB_RC:-0}"
"""

_bash_present = shutil.which("bash") is not None
_npm_present = shutil.which("npm") is not None
_venv_present = os.path.exists(_REAL_BACKEND_PY)

pytestmark = pytest.mark.skipif(
    not _bash_present or not _npm_present or not _venv_present,
    reason="需要 bash、npm 与仓库真实 backend/.venv（脚手架桩与 lib_edge 载入期都用得到）",
)


def _chmod_x(p):
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _scaffold(tmp_path, *, backend=True, node_modules=True, apple=True, patch=None):
    """搭临时 ROOT。三个开关分别控制「该门的前置在不在」——用来造「跑不起来」。

    `patch=(old, new)` 对拷进来的 devctl.sh 做一次定点替换，用来造真实门造不出的
    异常返回（例如门函数根本不存在时的 127）。替换命中数必须为 1，否则当场失败——
    否则脚手架会静默地测了个没被改动的副本。
    """
    devctl = tmp_path / "devctl.sh"
    shutil.copy(_DEVCTL_SRC, devctl)
    if patch is not None:
        old, new = patch
        src = devctl.read_text(encoding="utf-8")
        assert src.count(old) == 1, f"变异点命中 {src.count(old)} 次，脚手架失效"
        devctl.write_text(src.replace(old, new, 1), encoding="utf-8")
    _chmod_x(devctl)

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(_LIB_EDGE_SRC, scripts_dir / "lib_edge.sh")
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML, encoding="utf-8")

    # rag venv 的解释器也给一份桩：lib_edge 的 _read_cfg_hosts 在 backend 桩缺席时会借它
    for rel in ([("backend",)] if backend else []) + [("rag",)]:
        vbin = tmp_path.joinpath(*rel, ".venv", "bin")
        vbin.mkdir(parents=True)
        py = vbin / "python"
        py.write_text(_PY_STUB, encoding="utf-8")
        _chmod_x(py)

    front = tmp_path / "frontend"
    front.mkdir()
    (front / "package.json").write_text(
        '{"name":"stub","version":"0.0.0","private":true,'
        '"scripts":{"test":"sh ./probe.sh"}}',
        encoding="utf-8",
    )
    probe = front / "probe.sh"
    probe.write_text(_WEB_PROBE, encoding="utf-8")
    _chmod_x(probe)
    if node_modules:
        (front / "node_modules").mkdir()

    if apple:
        tests_dir = tmp_path / "apple-app" / "Tests"
        tests_dir.mkdir(parents=True)
        sh = tests_dir / "run_reducer_fixtures.sh"
        sh.write_text(_APPLE_STUB, encoding="utf-8")
        _chmod_x(sh)

    return devctl


_LEAK_SUBDIR = ".leakprobe"


def _run(devctl, args, cwd, **fake_rcs):
    leak = cwd / _LEAK_SUBDIR
    leak.mkdir(exist_ok=True)
    env = dict(
        PATH="/usr/bin:/bin:/usr/sbin:/opt/homebrew/bin:/usr/local/bin",
        HOME=os.environ.get("HOME", "/tmp"),
        # 三个门桩把「自己看到的密钥」写到这里取证。必须是专用子目录：
        # 直接用 cwd 的话 `$LEAK_DIR/backend` 会撞上脚手架里同名的 backend/ 目录。
        LEAK_DIR=str(leak),
    )
    for k, v in fake_rcs.items():
        env[k] = str(v)
    return subprocess.run(
        ["bash", str(devctl)] + args,
        cwd=str(cwd), capture_output=True, text=True, env=env, timeout=180,
    )


def _leak_seen_by(tmp_path, gate):
    """某条门的子进程实际看到的 FAKE_SECRET_KEY 值（空串 = 没看到）。"""
    return (tmp_path / _LEAK_SUBDIR / gate).read_text(encoding="utf-8")


def _out(r):
    return r.stdout + r.stderr


# ---- 全过 ----

def test_all_three_gates_pass_rc0(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check"], tmp_path, FAKE_PYTEST_RC=0, FAKE_WEB_RC=0, FAKE_APPLE_RC=0)
    assert r.returncode == 0, _out(r)
    combined = _out(r)
    assert "全过" in combined
    for gate in ("backend", "web", "apple"):
        assert gate in combined


# ---- 判否 = 3 ----

def test_backend_test_failure_is_rc3(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "--only", "backend"], tmp_path, FAKE_PYTEST_RC=1)
    assert r.returncode == 3, _out(r)
    assert "不过" in _out(r)


def test_web_failure_is_rc3(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "--only", "web"], tmp_path, FAKE_WEB_RC=1)
    assert r.returncode == 3, _out(r)


def test_apple_failure_is_rc3(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "--only", "apple"], tmp_path, FAKE_APPLE_RC=1)
    assert r.returncode == 3, _out(r)


# ---- ★ 跑不起来 = 1，绝不是 0 ----

def test_pytest_collected_nothing_is_internal_not_pass(tmp_path):
    """pytest rc=5（一条都没收集到）。这是最阴的一格：测试文件被挪走后
    「一条都没跑」若记成过，整条 backend 门就静默失效了。"""
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "--only", "backend"], tmp_path, FAKE_PYTEST_RC=5)
    assert r.returncode == 1, _out(r)          # 1 = 跑不起来，**不是** 0 也不是 3
    assert "跑不起来" in _out(r)


def test_pytest_usage_error_is_internal(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "--only", "backend"], tmp_path, FAKE_PYTEST_RC=4)
    assert r.returncode == 1, _out(r)


def test_missing_backend_venv_is_internal_not_silent_skip(tmp_path):
    devctl = _scaffold(tmp_path, backend=False)
    r = _run(devctl, ["check", "--only", "backend"], tmp_path)
    assert r.returncode == 1, _out(r)
    assert "跑不起来" in _out(r)


def test_missing_node_modules_is_internal(tmp_path):
    devctl = _scaffold(tmp_path, node_modules=False)
    r = _run(devctl, ["check", "--only", "web"], tmp_path)
    assert r.returncode == 1, _out(r)
    assert "跑不起来" in _out(r)


def test_missing_apple_script_is_internal(tmp_path):
    devctl = _scaffold(tmp_path, apple=False)
    r = _run(devctl, ["check", "--only", "apple"], tmp_path)
    assert r.returncode == 1, _out(r)
    assert "跑不起来" in _out(r)


def test_gate_returning_an_unexpected_code_is_internal_not_pass(tmp_path):
    """★ 门返回 {0,3} 以外的码（典型：函数名拼错 → 127）必须归「跑不起来」。

    这条抓的是一个真出现过的写法：合并函数写成「三个已知码都不匹配就回 0」，
    于是 127 会让汇总打「跑不起来」、退出码却是 0——汇总说不可信、退出码说过。
    """
    devctl = _scaffold(
        tmp_path,
        patch=('        "check_gate_${g}"\n',
               '        check_gate_typo_does_not_exist_${g} 2>/dev/null\n'),
    )
    r = _run(devctl, ["check", "--only", "web"], tmp_path)
    assert r.returncode == 1, _out(r)          # 归「跑不起来」，绝不是 0


# ---- ★ 合并优先级 1 > 3 ----

def test_internal_error_outranks_plain_failure(tmp_path):
    """backend 判否(3) + apple 跑不起来(1) → 合并必须是 1：
    这套门整体不可信，比「某条红了」更需要人介入。"""
    devctl = _scaffold(tmp_path, apple=False)
    r = _run(devctl, ["check"], tmp_path, FAKE_PYTEST_RC=1, FAKE_WEB_RC=0)
    assert r.returncode == 1, _out(r)


def test_failure_outranks_pass(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check"], tmp_path, FAKE_PYTEST_RC=0, FAKE_WEB_RC=1, FAKE_APPLE_RC=0)
    assert r.returncode == 3, _out(r)


# ---- ★ 反空守卫 ----

def test_unknown_gate_name_is_usage_error(tmp_path):
    """`--only bakend` 若被当成「没有匹配的门」而回 0，就是一次彻底的假绿。"""
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "--only", "bakend"], tmp_path)
    assert r.returncode == 2, _out(r)
    assert "未知的门名" in _out(r)


def test_skipping_every_gate_is_usage_error_not_pass(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "--skip", "backend,web,apple"], tmp_path)
    assert r.returncode == 2, _out(r)
    assert "一门都没有真跑" in _out(r)


def test_missing_flag_value_is_usage_error(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "--skip"], tmp_path)
    assert r.returncode == 2, _out(r)


# ---- --skip / --only 的正常语义 ----

def test_explicit_skip_is_loudly_reported_and_does_not_run_the_gate(tmp_path):
    """显式跳过是人的决定，允许；但必须在汇总里留痕，且被跳的门不许影响判决。"""
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "--skip", "backend"], tmp_path,
             FAKE_PYTEST_RC=1, FAKE_WEB_RC=0, FAKE_APPLE_RC=0)
    assert r.returncode == 0, _out(r)
    assert "跳过" in _out(r)


def test_only_runs_just_that_gate(tmp_path):
    """`--only web` 时 backend 桩设为红——若它仍被跑到，rc 就不会是 0。"""
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "--only", "web"], tmp_path, FAKE_PYTEST_RC=1, FAKE_WEB_RC=0)
    assert r.returncode == 0, _out(r)
    assert "backend" not in _out(r).split("check 汇总")[-1]


# ---- eval-rag：只验参数与退出码的透传，绝不真跑棘轮（它要加载模型、占 GPU）----

def _rag_scaffold(tmp_path):
    devctl = _scaffold(tmp_path)
    evals = tmp_path / "rag" / "evals"
    evals.mkdir(parents=True)
    (evals / "run_retrieval_eval.py").write_text("# stub\n", encoding="utf-8")
    return devctl


def test_eval_rag_passes_quoted_arguments_through_intact(tmp_path):
    """★ devctl 的全局参数期会 `set -- $_args` 重排位置参数，那一步做词分割——
    `--reason "两个 词"` 会被打散成两个参数，「原样透传」的承诺在这条路径上就是假的。
    修法是在参数期之前留一份原始 argv。"""
    devctl = _rag_scaffold(tmp_path)
    r = _run(devctl, ["eval-rag", "--allow-floor-drop", "--reason", "item set grew"], tmp_path)
    args = [ln for ln in r.stdout.splitlines() if ln.startswith("ARG:<")]
    assert args == [
        "ARG:<--allow-floor-drop>",
        "ARG:<--reason>",
        "ARG:<item set grew>",       # ← 关键：三个词仍是一个参数，没被拆成三个
    ], r.stdout + r.stderr


def test_eval_rag_passes_exit_code_through_verbatim(tmp_path):
    devctl = _rag_scaffold(tmp_path)
    r = _run(devctl, ["eval-rag"], tmp_path, FAKE_RAG_RC=1)
    assert r.returncode == 1, _out(r)
    assert "不过" in _out(r)


def test_eval_rag_missing_venv_is_internal_error(tmp_path):
    devctl = _scaffold(tmp_path)
    shutil.rmtree(tmp_path / "rag" / ".venv")
    r = _run(devctl, ["eval-rag"], tmp_path)
    assert r.returncode == 1, _out(r)
    assert "跑不起来" in _out(r)


# ---- 钩子安装 ----

# git 的环境变量必须摘干净：本组从这次改动起就在提交路径上跑，而 git 在 pre-commit
# 里会导出 GIT_INDEX_FILE/GIT_PREFIX，`git --git-dir=… commit` 等形态还会导出 GIT_DIR
# ——继承它们会让 `git init` 作用到**别人的真仓库**上，并让这些用例当场变红、把所有
# 提交挡死（首审 Critical 3）。
_GIT_ENV_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR", "GIT_PREFIX", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES", "GIT_NAMESPACE",
)


def _git_init(tmp_path):
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", "."],
                   cwd=str(tmp_path), check=True, capture_output=True,
                   env={k: v for k, v in os.environ.items() if k not in _GIT_ENV_VARS})


def _init_repo_with_tracked_hook(tmp_path, body="#!/bin/bash\nexit 0\n"):
    hooks_src = tmp_path / "scripts" / "git-hooks"
    hooks_src.mkdir()
    p = hooks_src / "pre-commit"
    p.write_text(body, encoding="utf-8")
    _chmod_x(p)
    _git_init(tmp_path)
    return p


def test_install_hooks_writes_a_relocatable_shim_not_a_symlink(tmp_path):
    """★ 装的必须是每次现算仓库根的 shim。软链会在仓库搬家后变悬空，而 git 对
    执行不了的钩子是**静默跳过**——守卫又一次自己把自己关掉。"""
    devctl = _scaffold(tmp_path)
    _init_repo_with_tracked_hook(tmp_path)

    r = _run(devctl, ["install-hooks"], tmp_path)
    assert r.returncode == 0, _out(r)
    dest = tmp_path / ".git" / "hooks" / "pre-commit"
    assert not dest.is_symlink()
    assert os.access(dest, os.X_OK)
    body = dest.read_text(encoding="utf-8")
    # 判据落在「shim 每次现算仓库根」这一条上；不再断言 `tmp_path not in body`
    # ——shim 正文是固定 heredoc，构造上就不含任何路径变量，那条恒真。
    assert "git rev-parse --show-toplevel" in body


def test_installed_shim_actually_dispatches_to_the_tracked_hook(tmp_path):
    """装完要真能转调到入库正身——只检查文件内容会漏掉「装了但调不动」。"""
    devctl = _scaffold(tmp_path)
    _init_repo_with_tracked_hook(tmp_path, "#!/bin/bash\necho TRACKED-HOOK-RAN\nexit 7\n")
    assert _run(devctl, ["install-hooks"], tmp_path).returncode == 0

    # env 同样要摘掉 GIT_*：shim 靠 `git rev-parse --show-toplevel` 现算仓库根，
    # 继承外部的 GIT_DIR/GIT_WORK_TREE 会让它算到别人的仓库去。
    r = subprocess.run(["bash", str(tmp_path / ".git" / "hooks" / "pre-commit")],
                       cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
                       env={k: v for k, v in os.environ.items() if k not in _GIT_ENV_VARS})
    assert r.returncode == 7, r.stdout + r.stderr
    assert "TRACKED-HOOK-RAN" in r.stdout


def test_install_hooks_is_idempotent(tmp_path):
    devctl = _scaffold(tmp_path)
    _init_repo_with_tracked_hook(tmp_path)
    assert _run(devctl, ["install-hooks"], tmp_path).returncode == 0
    r = _run(devctl, ["install-hooks"], tmp_path)
    assert r.returncode == 0, _out(r)


def test_install_hooks_refuses_to_clobber_someone_elses_hook(tmp_path):
    devctl = _scaffold(tmp_path)
    _init_repo_with_tracked_hook(tmp_path)
    dest = tmp_path / ".git" / "hooks" / "pre-commit"
    dest.write_text("#!/bin/bash\necho mine\n", encoding="utf-8")

    r = _run(devctl, ["install-hooks"], tmp_path)
    assert r.returncode != 0, _out(r)
    assert "echo mine" in dest.read_text(encoding="utf-8")


def test_hook_install_outside_a_git_repo_reports_failure_not_success(tmp_path):
    """★「装不成」不能报成「装好了」——与三值纪律同一条道理。"""
    devctl = _scaffold(tmp_path)
    (tmp_path / "scripts" / "git-hooks").mkdir()
    (tmp_path / "scripts" / "git-hooks" / "pre-commit").write_text(
        "#!/bin/bash\nexit 0\n", encoding="utf-8")
    # 刻意不 git init；并用 GIT_CEILING_DIRECTORIES 挡住向上找到真仓库
    r = _run(devctl, ["install-hooks"], tmp_path, GIT_CEILING_DIRECTORIES=str(tmp_path.parent))
    assert r.returncode != 0, _out(r)
    assert not (tmp_path / ".git").exists()


def test_install_hooks_upgrades_a_dangling_symlink_from_the_old_design(tmp_path):
    """早期实现装的是绝对路径软链，仓库一搬家即悬空。
    `-e` 跟随软链、判不出悬空，不先摘掉就会被 `cat >` 写穿到软链目标那个路径上去。"""
    devctl = _scaffold(tmp_path)
    src = _init_repo_with_tracked_hook(tmp_path)
    dest = tmp_path / ".git" / "hooks" / "pre-commit"
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(src), dest)
    src.unlink()                      # 造成悬空：软链在，目标没了
    src.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    _chmod_x(src)

    r = _run(devctl, ["install-hooks"], tmp_path)
    assert r.returncode == 0, _out(r)
    assert not dest.is_symlink()
    assert "git rev-parse --show-toplevel" in dest.read_text(encoding="utf-8")


def test_install_hooks_refuses_a_foreign_symlink(tmp_path):
    devctl = _scaffold(tmp_path)
    _init_repo_with_tracked_hook(tmp_path)
    other = tmp_path / "someone-elses-hook.sh"
    other.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    dest = tmp_path / ".git" / "hooks" / "pre-commit"
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(other), dest)

    r = _run(devctl, ["install-hooks"], tmp_path)
    assert r.returncode != 0, _out(r)
    assert dest.is_symlink()          # 原样保留，不许静默摘掉别人的东西


def test_only_and_skip_naming_the_same_gate_leaves_nothing_to_run(tmp_path):
    """`--only backend --skip backend` 走的是与「跳光三门」不同的分支，同样不许回 0。"""
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "--only", "backend", "--skip", "backend"], tmp_path)
    assert r.returncode == 2, _out(r)
    assert "一门都没有真跑" in _out(r)


def test_help_is_the_only_legitimate_zero_without_running_a_gate(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "--help"], tmp_path)
    assert r.returncode == 0, _out(r)
    assert "用法" in _out(r)
    assert "▷" not in _out(r)         # 一门都没起跑


def test_dash_h_is_not_help_and_does_not_hang(tmp_path):
    """★ `-h` 在本脚本表 host。外层参数期的 `shift 2 || true` 曾在只剩 1 个参数时
    原地不动 → while 死循环，`./devctl.sh check -h` 挂死不返回（提交路径上等价于
    永久阻塞）。这条同时钉住「不挂死」与「给出指路而非沉默」。"""
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["check", "-h"], tmp_path)   # _run 的 timeout=180 兜住挂死
    assert r.returncode == 2, _out(r)
    assert "--help" in _out(r)


def test_gates_do_not_inherit_secrets_from_env_local(tmp_path):
    """★ 把门接到提交路径上 = 每次 `git commit` 都把 .env.local 里的密钥（五把 Bearer
    钥匙 + 两个 App Store Connect 标识）经 devctl 顶部的 `set -a` 交给 pytest / npm /
    xcrun 的子进程；npm 还会连带跑 node_modules 的 lifecycle 脚本。三条门一把都不需要，
    故必须在门的子 shell 里摘掉。这是本次改动新引入的暴露面（首审 #14）。"""
    devctl = _scaffold(tmp_path)
    (tmp_path / ".env.local").write_text(
        'FAKE_SECRET_KEY="must-not-reach-the-gates"\n', encoding="utf-8")

    r = _run(devctl, ["check"], tmp_path)
    assert r.returncode == 0, _out(r)
    for gate in ("backend", "web", "apple"):
        leaked = _leak_seen_by(tmp_path, gate)
        assert leaked == "", f"{gate} 门的子进程看到了密钥：{leaked!r}"


def test_dropping_secrets_does_not_strip_what_the_gates_need(tmp_path):
    """反向守卫：摘密钥不能把 PATH/HOME 这类运行必需项一起摘了——否则门会以
    「跑不起来」的形态假红，比漏摘更难查。有 .env.local 在场时三门仍须实跑通过。"""
    devctl = _scaffold(tmp_path)
    (tmp_path / ".env.local").write_text(
        'FAKE_SECRET_KEY="x"\nAGENT_API_KEY="y"\nASC_KEY_ID="z"\n', encoding="utf-8")
    r = _run(devctl, ["check"], tmp_path)
    assert r.returncode == 0, _out(r)


# ---- ★ 横切：所有「透传下游退出码」的子命令都不许把帮助当成绿灯 ----

@pytest.mark.parametrize("cmd", ["check", "eval-rag", "doc-numbers", "preflight"])
def test_help_never_reads_as_a_successful_run(tmp_path, cmd):
    """★ `--help` 打完就 exit 0，而封装层若照着这个 0 打结论，就会宣布一次**从未跑过**
    的检查「过了」。

    这个坑在本波里出现了两次：A3 的 `doc-numbers --help` 被首审抓到；`eval-rag --help`
    则是**在 A0 就写下的**，过了首审也过了主会话签核，直到把全部子命令的 `--help`
    拉齐排查才现形（当时它真的打印「eval-rag 过（rc=0）」）。
    所以这条按**子命令清单**参数化，而不是只钉出事的那一个——新加子命令会自动进来。
    """
    devctl = _scaffold(tmp_path)
    r = _run(devctl, [cmd, "--help"], tmp_path)
    assert r.returncode == 0, _out(r)
    out = _out(r)
    for green in ("过（rc=0）", "全过 ✅", "一致（rc=0）", '"status"'):
        assert green not in out, f"{cmd} --help 打出了绿灯结论 {green!r}：\n{out}"
    assert "用法" in out


@pytest.mark.parametrize("cmd", ["check", "eval-rag", "doc-numbers", "preflight"])
def test_dash_h_is_a_usage_error_everywhere(tmp_path, cmd):
    """本脚本全局约定「-h 表 host 不表 help」，各子命令要给一致的指路而非各自为政。"""
    devctl = _scaffold(tmp_path)
    r = _run(devctl, [cmd, "-h"], tmp_path)
    assert r.returncode == 2, _out(r)
    assert "--help" in _out(r)
