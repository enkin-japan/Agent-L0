"""devctl.sh `-p/--profile` + `-h/--host` 金测试（shell 侧，实跑一次）。

守两件事：
  1. `-h/--host` 落盘 .env.local 两键 INFER_REMOTE_HOST/RAG_COMPUTE_HOST（keygen 同形态：
     无则创建+chmod 600，已有键就地替换、无则追加），本次启动立即生效（export + 重算
     edge_resolve_urls），后续启动免传。
  2. 参数期拦截：`-h` 只对 start|restart 有意义；必须带 -p/--profile（单机形态不需要
     跨机地址）；值以 `-` 开头视为缺失；值含白名单 [A-Za-z0-9_.:-] 外字符视为非法。
  gpu 分支顶部的 require_host 拦截三层皆空（-h/.env.local/config）的情形，
  fail 文案须含 "地址为空" 与 "-h <本机 tailnet IP>"（指引新参数）。

脚手架隔离（绝不在真仓库根跑 start）：每个测试把 devctl.sh + scripts/lib_edge.sh 拷进
tmp_path 临时 ROOT，配最小 config.yaml（两处 compute/remote host 留空，与仓库真实
通用模板一致），backend/.venv/bin/python 软链到真仓库 venv（_read_cfg_hosts 需要它
解析 config；真 venv 有 pyyaml）。不建 rag/searxng venv——任何越过地址拦截的路径都会
立刻撞“缺少 venv”类失败而不会真拉起服务，这是刻意的安全网，用来把“地址关卡”与
“后续服务缺失”两类失败区分开。

用例 2/3 会对 203.0.113.9:14162 发 curl 探活（TEST-NET-3 不可路由，3 秒超时），
单测可能耗几秒——可接受，不 mock。

bash 缺席时跳过（CI 最小镜像兜底）。
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
  mode: "remote"
  remote:
    host: ""
    port: 14161
rag:
  host: "127.0.0.1"
  compute:
    host: ""
    port: 14162
"""

_CLEAN_ENV_BASE = dict(
    PATH="/usr/bin:/bin:/usr/sbin:/opt/homebrew/bin",
    HOME=os.environ.get("HOME", "/tmp"),
)

_bash_present = shutil.which("bash") is not None
_venv_present = os.path.exists(_REAL_BACKEND_PY)


def _scaffold(tmp_path):
    """把真脚本拷进临时 ROOT，配最小 config，backend 解释器软链真 venv。返回 devctl 路径。"""
    devctl = tmp_path / "devctl.sh"
    shutil.copy(_DEVCTL_SRC, devctl)
    devctl.chmod(devctl.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(_LIB_EDGE_SRC, scripts_dir / "lib_edge.sh")

    (tmp_path / "config.yaml").write_text(_CONFIG_YAML, encoding="utf-8")

    venv_bin = tmp_path / "backend" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    os.symlink(_REAL_BACKEND_PY, venv_bin / "python")

    return devctl


def _run(devctl, args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(devctl)] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=dict(_CLEAN_ENV_BASE),
        timeout=60,
    )


def _env_local_path(tmp_path):
    return tmp_path / ".env.local"


def _read_key_lines(text: str, key: str):
    return [ln for ln in text.splitlines() if ln.startswith(key + "=")]


pytestmark = pytest.mark.skipif(
    not _bash_present or not _venv_present,
    reason="需要 bash 与仓库真实 backend/.venv/bin/python（_read_cfg_hosts 解析 config 用）",
)


def test_start_gpu_no_host_no_config_intercepted(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["start", "-p", "gpu"], tmp_path)
    assert r.returncode != 0
    combined = r.stdout + r.stderr
    assert "地址为空" in combined
    assert "-h" in combined
    assert "已落盘" not in combined
    assert not _env_local_path(tmp_path).exists()


def test_start_gpu_with_host_persists_and_is_picked_up_next_run(tmp_path):
    devctl = _scaffold(tmp_path)

    # 步骤一：带 -h 落盘
    r1 = _run(devctl, ["start", "-p", "gpu", "-h", "203.0.113.9"], tmp_path)
    envfile = _env_local_path(tmp_path)
    assert envfile.exists(), r1.stdout + r1.stderr
    text = envfile.read_text(encoding="utf-8")
    infer_lines = _read_key_lines(text, "INFER_REMOTE_HOST")
    ragc_lines = _read_key_lines(text, "RAG_COMPUTE_HOST")
    assert len(infer_lines) == 1
    assert len(ragc_lines) == 1
    assert infer_lines[0] == 'INFER_REMOTE_HOST="203.0.113.9"'
    assert ragc_lines[0] == 'RAG_COMPUTE_HOST="203.0.113.9"'
    assert oct(envfile.stat().st_mode & 0o777) == "0o600"

    combined1 = r1.stdout + r1.stderr
    assert "已落盘" in combined1
    assert r1.returncode != 0
    assert "地址为空" not in combined1

    # 步骤二：再跑一次不带 -h，落盘值应被拾取（不再报地址为空，也不再重复落盘提示）
    r2 = _run(devctl, ["start", "-p", "gpu"], tmp_path)
    combined2 = r2.stdout + r2.stderr
    assert "地址为空" not in combined2
    assert "已落盘" not in combined2


def test_persist_host_upserts_existing_key_and_appends_missing(tmp_path):
    devctl = _scaffold(tmp_path)
    envfile = _env_local_path(tmp_path)
    envfile.write_text(
        'INFER_REMOTE_HOST="1.1.1.1"\n'
        'OTHER_KEY="keep"\n',
        encoding="utf-8",
    )

    r = _run(devctl, ["start", "-p", "gpu", "-h", "203.0.113.9"], tmp_path)
    text = envfile.read_text(encoding="utf-8")

    infer_lines = _read_key_lines(text, "INFER_REMOTE_HOST")
    assert len(infer_lines) == 1
    assert infer_lines[0] == 'INFER_REMOTE_HOST="203.0.113.9"'
    assert 'OTHER_KEY="keep"' in text
    ragc_lines = _read_key_lines(text, "RAG_COMPUTE_HOST")
    assert len(ragc_lines) == 1
    assert ragc_lines[0] == 'RAG_COMPUTE_HOST="203.0.113.9"'
    assert "已落盘" in (r.stdout + r.stderr)


def test_host_without_profile_rejected(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["start", "-h", "203.0.113.9"], tmp_path)
    assert r.returncode != 0
    assert "单机形态不需要" in (r.stdout + r.stderr)
    assert not _env_local_path(tmp_path).exists()


def test_host_on_stop_rejected(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["stop", "-p", "gpu", "-h", "203.0.113.9"], tmp_path)
    assert r.returncode != 0
    assert "只对 start|restart 有意义" in (r.stdout + r.stderr)


def test_host_missing_value_dash_treated_as_missing(tmp_path):
    devctl = _scaffold(tmp_path)
    # -h 后紧跟另一个形如旗标的 token：按“缺少地址值”处理
    r = _run(devctl, ["start", "-p", "gpu", "-h", "-p"], tmp_path)
    assert r.returncode != 0
    assert "缺少地址值" in (r.stdout + r.stderr)


def test_host_illegal_value_rejected(tmp_path):
    devctl = _scaffold(tmp_path)
    r = _run(devctl, ["start", "-p", "gpu", "-h", "bad|value"], tmp_path)
    assert r.returncode != 0
    assert "非法地址值" in (r.stdout + r.stderr)
    envfile = _env_local_path(tmp_path)
    if envfile.exists():
        assert "bad" not in envfile.read_text(encoding="utf-8")
