"""rotate_log 日志代际隔离 + managed_or_report 占用者分类金测试（shell 侧，实跑真函数）。

守 lib_edge.sh 的 rotate_log：本次启动尝试的起点把上一代进程日志轮转为 $1.prev，
免得 devctl 失败时 tail 把上一代的旧拒启信息混进来误导诊断（实机事故 2026-07-19）。

另守 managed_or_report 对端口占用者的措辞分叉：占用者若是 app 内嵌解释器
（路径含 `.app/Contents/Resources/`，R5-3 PBS 运行时）= 本 app 上一代执行层残留
（强杀/崩溃未走清理留下的），标"可安全回收"；否则维持"该端口的监听者不是本次
启动器拉起的服务"的原措辞——别把自家旧代当成外来程序吓唬用户，也别把真正的
外来占用者说成"自家残留"误导用户直接回收。

按 test_keygen_shell.py 的既有模式（source + 子进程实跑），不 grep 文本当测试。
bash 缺席时跳过（CI 最小镜像兜底）。
"""
import os
import shutil
import subprocess

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LIB_EDGE = os.path.join(_REPO, "scripts", "lib_edge.sh")

# lib_edge.sh 顶层 source 时跑 edge_require_env + edge_resolve_urls：布局参数缺失会往
# stderr 打提示。与 keygen 测试同级的最小 env 打桩——喂 7 个 EDGE_* 占位（解释器路径故意
# 不可执行，_read_cfg_hosts 静默回落，无 python 亦无副作用），只为让 source 干净通过。
_STUB_ENV = dict(
    os.environ,
    EDGE_BACKEND_DIR="/nonexistent/backend", EDGE_BACKEND_PY="/nonexistent/py",
    EDGE_SEARX_DIR="/nonexistent/searx", EDGE_SEARX_PY="/nonexistent/searxpy",
    EDGE_SEARX_SETTINGS="/nonexistent/settings.yml",
    EDGE_LOG_DIR="/nonexistent/logs", EDGE_CONFIG="/nonexistent/config.yaml",
)


def _call_rotate(log_path: str) -> subprocess.CompletedProcess:
    """source lib_edge.sh 后调真 rotate_log。传路径为位置参数，避免注入。"""
    script = f'source "{_LIB_EDGE}"; rotate_log "$1"'
    return subprocess.run(["bash", "-c", script, "_", log_path],
                          capture_output=True, text=True, env=_STUB_ENV)


@pytest.mark.skipif(not shutil.which("bash"), reason="需要 bash")
def test_empty_or_missing_log_not_rotated(tmp_path):
    # 不存在的日志：不轮转、不报错、不生成 .prev
    missing = tmp_path / "missing.log"
    r = _call_rotate(str(missing))
    assert r.returncode == 0, r.stderr
    assert not missing.exists()
    assert not (tmp_path / "missing.log.prev").exists()

    # 空日志（0 字节）：不轮转（-s 判据），原文件仍在、无 .prev
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    r = _call_rotate(str(empty))
    assert r.returncode == 0, r.stderr
    assert empty.exists()
    assert not (tmp_path / "empty.log.prev").exists()


@pytest.mark.skipif(not shutil.which("bash"), reason="需要 bash")
def test_nonempty_log_rotated_to_prev(tmp_path):
    log = tmp_path / "backend.log"
    log.write_text("第一代日志内容\n", encoding="utf-8")
    r = _call_rotate(str(log))
    assert r.returncode == 0, r.stderr
    prev = tmp_path / "backend.log.prev"
    # 原文件消失、内容搬到 .prev
    assert not log.exists()
    assert prev.exists()
    assert prev.read_text(encoding="utf-8") == "第一代日志内容\n"


def _call_require_host(value: str) -> subprocess.CompletedProcess:
    """source lib_edge.sh 后调真 require_host（$3 指引文本用作 stderr 断言锚点）。"""
    script = f'source "{_LIB_EDGE}"; require_host "$1" "算力端" "填 INFER_REMOTE_HOST"'
    return subprocess.run(["bash", "-c", script, "_", value],
                          capture_output=True, text=True, env=_STUB_ENV)


@pytest.mark.skipif(not shutil.which("bash"), reason="需要 bash")
def test_require_host_nonempty_returns_zero_silent():
    r = _call_require_host("100.64.0.9")
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == ""


@pytest.mark.skipif(not shutil.which("bash"), reason="需要 bash")
def test_require_host_empty_returns_one_with_guidance():
    r = _call_require_host("")
    assert r.returncode == 1
    assert "填 INFER_REMOTE_HOST" in r.stderr


@pytest.mark.skipif(not shutil.which("bash"), reason="需要 bash")
def test_require_host_whitespace_only_rejected():
    # 纯空白按空处理（tr 去空白后为空）
    r = _call_require_host("   ")
    assert r.returncode == 1
    assert "填 INFER_REMOTE_HOST" in r.stderr


@pytest.mark.skipif(not shutil.which("bash"), reason="需要 bash")
def test_second_rotate_overwrites_old_prev(tmp_path):
    log = tmp_path / "backend.log"
    prev = tmp_path / "backend.log.prev"

    # 第一次轮转：G1 → .prev
    log.write_text("G1\n", encoding="utf-8")
    assert _call_rotate(str(log)).returncode == 0
    assert prev.read_text(encoding="utf-8") == "G1\n"

    # 第二代写入后再轮转：G2 覆盖旧 .prev（G1 被丢弃，只留最近一代）
    log.write_text("G2\n", encoding="utf-8")
    assert _call_rotate(str(log)).returncode == 0
    assert not log.exists()
    assert prev.read_text(encoding="utf-8") == "G2\n"


def _call_managed_or_report(ps_output: str) -> subprocess.CompletedProcess:
    """source lib_edge.sh 后用假 listen_pids/ps 顶替真实系统调用，实跑
    managed_or_report。listen_pids 固定吐一个假 pid（占用者存在）；pid 文件给
    不存在的路径（无记录，走硬失败报告分支）；ps 按 $FAKE_PS_OUTPUT 回放调用方
    指定的 comm 路径，驱动 own_stray 判据的两条岔路。"""
    script = (
        'source "$1"; '
        'listen_pids() { echo 99999; }; '
        'ps() { echo "$FAKE_PS_OUTPUT"; }; '
        'managed_or_report backend 14159 /nonexistent/pid stop-backend'
    )
    env = dict(_STUB_ENV, FAKE_PS_OUTPUT=ps_output)
    return subprocess.run(["bash", "-c", script, "_", _LIB_EDGE],
                          capture_output=True, text=True, env=env)


@pytest.mark.skipif(not shutil.which("bash"), reason="需要 bash")
def test_managed_or_report_app_bundle_stray_labeled_recyclable():
    # 占用者是 app 内嵌解释器路径 → 识别为本 app 上一代执行层残留，标"可安全回收"
    r = _call_managed_or_report(
        "/Applications/MacAgent.app/Contents/Resources/python/bin/python3.13")
    assert r.returncode == 1
    assert "上一代执行层残留" in r.stderr
    assert "可安全回收" in r.stderr
    assert "不是本次启动器拉起的服务" not in r.stderr


@pytest.mark.skipif(not shutil.which("bash"), reason="需要 bash")
def test_managed_or_report_foreign_process_keeps_original_wording():
    # 占用者是无关程序 → 维持原措辞，不得被误标为"残留"
    r = _call_managed_or_report("/usr/local/bin/some-other-daemon")
    assert r.returncode == 1
    assert "不是本次启动器拉起的服务" in r.stderr
    assert "残留" not in r.stderr
