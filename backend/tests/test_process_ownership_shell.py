"""Service ownership regressions. Every process query, signal and wait is synthetic."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / 'scripts/lib_edge.sh'
ENV = dict(os.environ, EDGE_BACKEND_DIR='/nonexistent/backend',
           EDGE_BACKEND_PY='/nonexistent/python', EDGE_SEARX_DIR='/nonexistent/searx',
           EDGE_SEARX_PY='/nonexistent/python', EDGE_SEARX_SETTINGS='/nonexistent/settings',
           EDGE_CONFIG='/nonexistent/config')


def run_stop(tmp_path, scenario, *, pid=True, identity=True, operation='stop'):
    pidfile = tmp_path / 'backend.pid'
    if pid:
        pidfile.write_text('100\n')
    if identity:
        Path(str(pidfile) + '.identity').write_text('v1:100:501:darwin:1000:123456\n')
    signals = tmp_path / 'signals'
    script = r'''
source "$1"
phase=0
dead=""
listen_pids() { echo 999; }
# An executable outside the fixture cannot be invoked via xargs kill either.
xargs() { echo "unexpected xargs" >> "$SIGNALS"; return 1; }
ps() { echo '/Applications/Unrelated.app/Contents/Resources/python'; }
kill() {
    if [ "$1" = -0 ]; then return 0; fi
    echo "$*" >> "$SIGNALS"
    if [ "$SCENARIO" = exit ]; then phase=1; fi
    [ "$1" = -KILL ] && dead="$dead $2"
    return 0
}
sleep() { phase=1; }
edge_process_identity() {
    case " $dead " in *" $1 "*) return 3 ;; esac
    case "$SCENARIO:$1:$phase" in
        reuse:100:*|replacement:100:1) echo v1:100:501:darwin:2000:123456 ;;
        exit:*:1) return 3 ;;
        *:100:*) echo v1:100:501:darwin:1000:123456 ;;
        child_reuse:101:1) echo v1:101:501:darwin:2000:123456 ;;
        *:101:*) echo v1:101:501:darwin:1001:123456 ;;
        *) return 1 ;;
    esac
}
edge_process_snapshot() {
    printf '100 v1:100:501:darwin:1000:123456\n'
    if [ "$SCENARIO" = children ] || [ "$SCENARIO" = child_reuse ]; then
        printf '101 v1:101:501:darwin:1001:123456\n'
    fi
}
if [ "$OPERATION" = reap ]; then edge_reap; else stop_one backend 14159 "$2" 1; fi
'''
    env = dict(ENV, EDGE_LOG_DIR=str(tmp_path), SIGNALS=str(signals), SCENARIO=scenario,
               OPERATION=operation)
    if operation == 'reap':
        env['EDGE_BACKEND_PY'] = '/Applications/Unrelated.app/Contents/Resources/python'
    r = subprocess.run(['bash', '-c', script, '_', str(LIB), str(pidfile)],
                       env=env, capture_output=True, text=True, timeout=10)
    return r, signals.read_text().splitlines() if signals.exists() else [], pidfile


def test_foreign_listener_without_pid_receives_no_signal(tmp_path):
    r, signals, _ = run_stop(tmp_path, 'foreign', pid=False, identity=False)
    assert signals == [], r.stderr
    assert r.returncode != 0
    assert '已停止' not in r.stdout


def test_legacy_pid_without_identity_is_refused(tmp_path):
    r, signals, pidfile = run_stop(tmp_path, 'legacy', identity=False)
    assert signals == [], r.stderr
    assert r.returncode != 0 and pidfile.exists()


def test_reused_pid_receives_no_signal(tmp_path):
    r, signals, pidfile = run_stop(tmp_path, 'reuse')
    assert signals == [], r.stderr
    assert r.returncode != 0 and pidfile.exists()


def test_pid_replacement_during_grace_is_not_killed(tmp_path):
    r, signals, _ = run_stop(tmp_path, 'replacement')
    assert signals == ['-TERM 100'], r.stderr
    assert '999' not in ' '.join(signals)


def test_verified_root_and_children_are_stopped(tmp_path):
    r, signals, pidfile = run_stop(tmp_path, 'children')
    assert r.returncode == 0, r.stderr
    assert set(signals) == {'-TERM 100', '-TERM 101', '-KILL 100', '-KILL 101'}
    assert not pidfile.exists() and not Path(str(pidfile) + '.identity').exists()


def test_child_pid_reuse_during_grace_is_not_killed(tmp_path):
    r, signals, _ = run_stop(tmp_path, 'child_reuse')
    assert '-TERM 101' in signals and '-KILL 101' not in signals
    assert '-KILL 100' in signals


def test_reap_does_not_adopt_unrecorded_app_interpreter(tmp_path):
    r, signals, _ = run_stop(tmp_path, 'foreign', pid=False, identity=False, operation='reap')
    assert signals == [], r.stderr
    assert '解释器属本布局' not in r.stdout


def test_verified_process_exiting_after_term_needs_no_kill(tmp_path):
    r, signals, _ = run_stop(tmp_path, 'exit')
    assert r.returncode == 0, r.stderr
    assert signals == ['-TERM 100']


def test_failed_kill_does_not_report_stopped(tmp_path):
    pidfile = tmp_path / 'backend.pid'
    pidfile.write_text('100\n')
    Path(str(pidfile) + '.identity').write_text('v1:100:501:darwin:1000:1\n')
    script = r'''
source "$1"
edge_process_identity() { echo v1:100:501:darwin:1000:1; }
edge_process_snapshot() { echo '100 v1:100:501:darwin:1000:1'; }
kill() { [ "$1" != -KILL ]; }
sleep() { :; }
listen_pids() { echo 999; }
stop_one backend 14159 "$2" 0
'''
    r = subprocess.run(['bash', '-c', script, '_', str(LIB), str(pidfile)],
                       env=dict(ENV, EDGE_LOG_DIR=str(tmp_path)), capture_output=True,
                       text=True, timeout=10)
    assert r.returncode != 0 and pidfile.exists()
    assert '已停止' not in r.stdout


def test_snapshot_invalidated_before_term_receives_no_signal(tmp_path):
    pidfile = tmp_path / 'backend.pid'
    pidfile.write_text('100\n')
    Path(str(pidfile) + '.identity').write_text('v1:100:501:darwin:1000:1\n')
    changed = tmp_path / 'changed'
    signals = tmp_path / 'signals'
    script = r'''
source "$1"
edge_process_identity() {
    if [ -f "$CHANGED" ]; then echo v1:100:501:darwin:2000:1;
    else echo v1:100:501:darwin:1000:1; fi
}
edge_process_snapshot() { touch "$CHANGED"; echo '100 v1:100:501:darwin:1000:1'; }
kill() { echo "$*" >> "$SIGNALS"; }
sleep() { :; }
listen_pids() { echo 999; }
stop_one backend 14159 "$2" 0
'''
    r = subprocess.run(['bash', '-c', script, '_', str(LIB), str(pidfile)],
                       env=dict(ENV, EDGE_LOG_DIR=str(tmp_path), CHANGED=str(changed),
                                SIGNALS=str(signals)), capture_output=True, text=True,
                       timeout=10)
    assert r.returncode == 0, r.stderr
    assert not signals.exists()


def test_new_instance_record_during_grace_is_preserved(tmp_path):
    pidfile = tmp_path / 'backend.pid'
    pidfile.write_text('100\n')
    identity = Path(str(pidfile) + '.identity')
    identity.write_text('v1:100:501:darwin:1000:1\n')
    script = r'''
source "$1"
phase=0
edge_process_identity() {
    [ "$phase" = 1 ] && return 3
    echo v1:100:501:darwin:1000:1
}
edge_process_snapshot() { echo '100 v1:100:501:darwin:1000:1'; }
kill() { :; }
PIDFILE="$2"
sleep() {
    phase=1
    echo 200 > "$PIDFILE"
    echo v1:200:501:darwin:2000:1 > "$PIDFILE.identity"
}
listen_pids() { echo 200; }
stop_one backend 14159 "$2" 1
'''
    r = subprocess.run(['bash', '-c', script, '_', str(LIB), str(pidfile)],
                       env=dict(ENV, EDGE_LOG_DIR=str(tmp_path)), capture_output=True,
                       text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    assert pidfile.read_text() == '200\n'
    assert identity.read_text() == 'v1:200:501:darwin:2000:1\n'


def test_spawn_records_identity_without_commands_or_arguments(tmp_path):
    script = r'''
source "$1"
nohup() { printf invoked > "$INVOKED"; }
kill() { echo unexpected-signal >&2; return 1; }
edge_process_identity() { echo "v1:$1:501:darwin:1000:987654"; }
spawn_daemon "$2" "$2/service.log" "$2/service.pid" synthetic-daemon secret-argument
'''
    r = subprocess.run(['bash', '-c', script, '_', str(LIB), str(tmp_path)],
                       env=dict(ENV, EDGE_LOG_DIR=str(tmp_path),
                                INVOKED=str(tmp_path / 'invoked')),
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    pid = (tmp_path / 'service.pid').read_text().strip()
    identity = (tmp_path / 'service.pid.identity').read_text()
    assert pid.isdecimal()
    assert identity == f'v1:{pid}:501:darwin:1000:987654\n'
    assert (tmp_path / 'service.pid.identity').stat().st_mode & 0o077 == 0
    assert 'secret-argument' not in identity + r.stdout + r.stderr


# Execute the exact inline adapter against a synthetic kernel API, never real processes.
def query_source():
    source = LIB.read_text()
    return 'import ctypes, errno' + source.split("<<'PY'\nimport ctypes, errno", 1)[1].split('\nPY\n}', 1)[0]


def darwin_query(monkeypatch, records, *, mode='identity', pid='100', expected=None):
    import ctypes
    import errno
    import sys
    calls = []

    def proc_pidinfo(pid, flavor, arg, ptr, size):
        calls.append(pid)
        values = records(pid, calls) if callable(records) else records.get(pid)
        if values is None:
            ctypes.set_errno(errno.ESRCH)
            return 0
        b = ptr._obj
        b.pid = pid
        b.ppid, b.uid, b.sec, b.usec = values
        return size

    class FakeLib:
        pass
    lib = FakeLib()
    lib.proc_pidinfo = proc_pidinfo
    monkeypatch.setattr(ctypes, 'CDLL', lambda *a, **k: lib)
    monkeypatch.setattr(os, 'geteuid', lambda: 501)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(sys, 'argv', ['_', mode, pid] + ([expected] if expected else []))
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: '100\n101\n999\n')
    exec(compile(query_source(), str(LIB), 'exec'), {})
    return calls


def test_darwin_identity_keeps_microseconds(monkeypatch, capsys):
    darwin_query(monkeypatch, {100: (1, 501, 123456, 999999)})
    assert capsys.readouterr().out == 'v1:100:501:darwin:123456:999999\n'


def test_darwin_wrong_uid_refused(monkeypatch):
    with pytest.raises(SystemExit) as e:
        darwin_query(monkeypatch, {100: (1, 502, 123456, 1)})
    assert e.value.code == 1


def test_darwin_missing_process_is_distinct_from_uncertainty(monkeypatch):
    with pytest.raises(SystemExit) as e:
        darwin_query(monkeypatch, {})
    assert e.value.code == 3


def test_darwin_tree_includes_only_verified_descendants(monkeypatch, capsys):
    darwin_query(monkeypatch, {100: (1, 501, 1000, 1), 101: (100, 501, 1001, 2),
                              999: (1, 501, 1002, 3)}, mode='tree',
                 expected='v1:100:501:darwin:1000:1')
    assert capsys.readouterr().out.splitlines() == [
        '100 v1:100:501:darwin:1000:1', '101 v1:101:501:darwin:1001:2']


def test_darwin_tree_rechecks_root_creation_before_returning(monkeypatch, capsys):
    def records(pid, calls):
        if pid == 100:
            return (1, 501, 1000, 1 if calls.count(100) < 3 else 2)
        if pid == 101:
            return (100, 501, 1001, 1)
    with pytest.raises(SystemExit) as e:
        darwin_query(monkeypatch, records, mode='tree', expected='v1:100:501:darwin:1000:1')
    assert e.value.code == 1
    assert capsys.readouterr().out == ''


def test_darwin_spawn_identity_requires_creator_parent(monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        darwin_query(monkeypatch, {100: (200, 501, 1000, 1)}, expected='300')
    assert e.value.code == 1
    assert capsys.readouterr().out == ''


def test_linux_identity_keeps_boot_and_start_tick(monkeypatch, capsys):
    import sys
    from types import SimpleNamespace
    import pathlib

    class FakePath:
        def __init__(self, path):
            self.path = str(path)
        def __truediv__(self, part):
            return FakePath(self.path + '/' + str(part))
        def stat(self):
            return SimpleNamespace(st_uid=501)
        def read_text(self):
            if self.path.endswith('boot_id'):
                return 'synthetic-boot\n'
            # /proc/stat fields 3..22; command may contain a closing parenthesis.
            return '100 (synthetic ) command) S 1 ' + '0 ' * 17 + '987654321\n'
    monkeypatch.setattr(pathlib, 'Path', FakePath)
    monkeypatch.setattr(os, 'geteuid', lambda: 501)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(sys, 'argv', ['_', 'identity', '100'])
    exec(compile(query_source(), str(LIB), 'exec'), {})
    assert capsys.readouterr().out == 'v1:100:501:linux:synthetic-boot:987654321\n'


@pytest.mark.parametrize('operation', ['start_searx', 'stop_searx'])
@pytest.mark.parametrize('docker_state', ['found', 'unknown'])
@pytest.mark.parametrize('native_state', ['healthy', 'free', 'foreign'])
def test_legacy_docker_never_controls_native_service(tmp_path, operation, docker_state, native_state):
    calls = tmp_path / 'calls'
    executable = tmp_path / 'synthetic-python'
    executable.write_text('#!/bin/sh\nexit 99\n')
    executable.chmod(0o700)  # Only its presence is tested; spawn is intercepted below.
    script = r'''
source "$1"
docker() {
    echo "$*" >> "$CALLS"
    if [ "$1" != ps ]; then return 99; fi
    [ "$DOCKER_STATE" = unknown ] && return 1
    echo synthetic-container-id
}
# Docker's state says nothing about the independently verified native service.
is_ready() { [ "$NATIVE_STATE" != free ]; }
managed_or_report() { echo native-check >> "$CALLS"; [ "$NATIVE_STATE" != foreign ]; }
guard_port_before_spawn() { [ "$NATIVE_STATE" != foreign ]; }
stop_one() { echo native-stop >> "$CALLS"; [ "$NATIVE_STATE" != foreign ]; }
ensure_searx_settings() { :; }
rotate_log() { :; }
wait_ready() { return 0; }
spawn_daemon() { echo native-spawn >> "$CALLS"; echo 100 > "$3"; }
"$2"
'''
    r = subprocess.run(['bash', '-c', script, '_', str(LIB), operation],
                       env=dict(ENV, EDGE_LOG_DIR=str(tmp_path), CALLS=str(calls),
                                DOCKER_STATE=docker_state, NATIVE_STATE=native_state,
                                EDGE_SEARX_PY=str(executable)), capture_output=True,
                       text=True, timeout=10)
    assert (r.returncode == 0) == (native_state != 'foreign'), r.stderr
    actual = calls.read_text()
    assert 'ps -aq -f name=^searxng$' in actual
    assert 'rm' not in actual
    assert ('native-stop' in actual) == (operation == 'stop_searx')
    assert ('native-spawn' in actual) == (operation == 'start_searx' and native_state == 'free')


def test_identity_uncertainty_after_term_is_not_reported_as_stopped(tmp_path):
    pidfile = tmp_path / 'backend.pid'
    pidfile.write_text('100\n')
    Path(str(pidfile) + '.identity').write_text('v1:100:501:darwin:1000:1\n')
    script = r'''
source "$1"
phase=0
edge_process_identity() {
    [ "$phase" = 1 ] && return 1
    echo v1:100:501:darwin:1000:1
}
edge_process_snapshot() { echo '100 v1:100:501:darwin:1000:1'; }
kill() { phase=1; }
sleep() { :; }
listen_pids() { echo 999; }
stop_one backend 14159 "$2" 0
'''
    r = subprocess.run(['bash', '-c', script, '_', str(LIB), str(pidfile)],
                       env=dict(ENV, EDGE_LOG_DIR=str(tmp_path)), capture_output=True,
                       text=True, timeout=10)
    assert r.returncode != 0 and pidfile.exists()
    assert '已停止' not in r.stdout


@pytest.mark.parametrize('listener, expected_rc', [('101', 0), ('999', 1)])
def test_managed_listener_must_belong_to_verified_tree(tmp_path, listener, expected_rc):
    pidfile = tmp_path / 'backend.pid'
    pidfile.write_text('100\n')
    Path(str(pidfile) + '.identity').write_text('v1:100:501:darwin:1000:1\n')
    script = r'''
source "$1"
edge_process_identity() { echo v1:100:501:darwin:1000:1; }
edge_process_snapshot() {
    echo '100 v1:100:501:darwin:1000:1'
    echo '101 v1:101:501:darwin:1001:1'
}
listen_pids() { echo "$LISTENER"; }
ps() { echo '/synthetic/executable'; }
kill() { echo unexpected-signal >&2; return 99; }
managed_or_report backend 14159 "$2" backend
'''
    r = subprocess.run(['bash', '-c', script, '_', str(LIB), str(pidfile)],
                       env=dict(ENV, EDGE_LOG_DIR=str(tmp_path), LISTENER=listener),
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == expected_rc, r.stderr
    assert 'unexpected-signal' not in r.stderr


@pytest.mark.parametrize('failed_service', ['backend', 'searx', 'rag'])
def test_edge_down_attempts_all_owned_services_and_returns_failure(tmp_path, failed_service):
    calls = tmp_path / 'calls'
    script = r'''
source "$1"
stop_backend() { echo backend >> "$CALLS"; [ "$FAILED_SERVICE" != backend ]; }
stop_searx() { echo searx >> "$CALLS"; [ "$FAILED_SERVICE" != searx ]; }
stop_rag_local() { echo rag >> "$CALLS"; [ "$FAILED_SERVICE" != rag ]; }
rag_is_local() { return 0; }
EDGE_RAG_PY=/synthetic/python
edge_down
'''
    r = subprocess.run(['bash', '-c', script, '_', str(LIB)],
                       env=dict(ENV, EDGE_LOG_DIR=str(tmp_path), CALLS=str(calls),
                                FAILED_SERVICE=failed_service), capture_output=True,
                       text=True, timeout=10)
    assert r.returncode != 0, r.stderr
    assert calls.read_text().splitlines() == ['backend', 'searx', 'rag']


@pytest.mark.parametrize('root_state, listener, identity, expected_rc', [
    ('gone', '', True, 0), ('gone', '999', True, 1),
    ('unknown', '', True, 1), ('reused', '', True, 1), ('gone', '', False, 1),
])
def test_natural_root_exit_is_distinct_from_unowned_or_unknown(tmp_path, root_state, listener,
                                                             identity, expected_rc):
    pidfile = tmp_path / 'backend.pid'
    pidfile.write_text('100\n')
    sidecar = Path(str(pidfile) + '.identity')
    if identity:
        sidecar.write_text('v1:100:501:darwin:1000:1\n')
    signals = tmp_path / 'signals'
    script = r'''
source "$1"
edge_process_identity() {
    case "$ROOT_STATE" in
        gone) return 3 ;;
        unknown) return 1 ;;
        reused) echo v1:100:501:darwin:2000:1 ;;
    esac
}
edge_process_snapshot() { echo unexpected-snapshot >&2; return 99; }
listen_pids() { printf '%s' "$LISTENER"; }
kill() { echo "$*" >> "$SIGNALS"; return 99; }
stop_one backend 14159 "$2" 0
'''
    r = subprocess.run(['bash', '-c', script, '_', str(LIB), str(pidfile)],
                       env=dict(ENV, EDGE_LOG_DIR=str(tmp_path), ROOT_STATE=root_state,
                                LISTENER=listener, SIGNALS=str(signals)),
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == expected_rc, r.stderr
    assert not signals.exists()
    assert pidfile.exists() == (expected_rc != 0)
    if expected_rc == 0:
        assert not sidecar.exists()
        assert '根进程已退出' in r.stdout
        assert '未认领其他进程' in r.stdout
        assert '本实例进程已停止' not in r.stdout


def test_record_switch_between_compare_and_remove_cannot_delete_new_record(tmp_path):
    pidfile = tmp_path / 'backend.pid'
    pidfile.write_text('100\n')
    sidecar = Path(str(pidfile) + '.identity')
    sidecar.write_text('v1:100:501:darwin:1000:1\n')
    script = r'''
source "$1"
phase=0
edge_process_identity() {
    [ "$phase" = 1 ] && return 3
    echo v1:100:501:darwin:1000:1
}
edge_process_snapshot() { echo '100 v1:100:501:darwin:1000:1'; }
listen_pids() { :; }
kill() { phase=1; }
sleep() { :; }
# Deterministically switch generation after old stop has compared both files.
rm() {
    echo 200 > "$PIDFILE"
    echo v1:200:501:darwin:2000:1 > "$PIDFILE.identity"
    command rm "$@"
}
PIDFILE="$2"
# With the repaired helper, publish through the shared record writer immediately
# before deletion. The old rm barrier above proves the previous compare/rm gap.
if declare -f _edge_process_record >/dev/null; then
    eval "$(declare -f _edge_process_record | sed '1s/_edge_process_record/_edge_real_record/')"
    _edge_process_record() {
        if [ "$1" = delete ]; then
            _edge_real_record write "$PIDFILE" 200 v1:200:501:darwin:2000:1 || return 99
        fi
        _edge_real_record "$@"
    }
fi
stop_one backend 14159 "$2" 0
'''
    r = subprocess.run(['bash', '-c', script, '_', str(LIB), str(pidfile)],
                       env=dict(ENV, EDGE_LOG_DIR=str(tmp_path)), capture_output=True,
                       text=True, timeout=10)
    # The legacy rm barrier is never entered by locked cleanup. Prove actual locking
    # and blocked concurrent replacement independently below using a filesystem barrier.
    assert r.returncode == 0, r.stderr
    assert pidfile.exists(), 'unlocked rm deleted the concurrently published generation'
    assert pidfile.read_text() == '200\n'
    assert sidecar.read_text() == 'v1:200:501:darwin:2000:1\n'


def record_source():
    return LIB.read_text().split("<<'PY_RECORD'\n", 1)[1].split('\nPY_RECORD\n}', 1)[0]


def test_record_publication_waits_for_locked_compare_and_delete(tmp_path):
    """Filesystem-only subprocesses exercise the actual fcntl helper with a barrier."""
    import sys
    import time

    pidfile = tmp_path / 'backend.pid'
    sidecar = Path(str(pidfile) + '.identity')
    old = 'v1:100:501:darwin:1000:1'
    new = 'v1:200:501:darwin:2000:1'
    pidfile.write_text('100\n')
    sidecar.write_text(old + '\n')
    entered, release, done, attempt = [tmp_path / name for name in
                                     ('entered', 'release', 'done', 'attempt')]
    # Hold deletion between its compare and first unlink, while still under LOCK_EX.
    delete_prefix = f'''
import os, pathlib, time
_real_unlink = os.unlink
def barrier_unlink(path, *args, **kwargs):
    if str(path) == {str(pidfile)!r}:
        pathlib.Path({str(entered)!r}).write_text('entered')
        deadline = time.monotonic() + 5
        while not pathlib.Path({str(release)!r}).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('test barrier timed out')
            time.sleep(.01)
    return _real_unlink(path, *args, **kwargs)
os.unlink = barrier_unlink
'''
    writer_prefix = f'import pathlib; pathlib.Path({str(attempt)!r}).write_text("attempt")\n'
    writer_suffix = f'\npathlib.Path({str(done)!r}).write_text("done")\n'
    deleter = subprocess.Popen([sys.executable, '-c', delete_prefix + record_source(),
                               'delete', str(pidfile), '100', old],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    writer = None
    try:
        deadline = time.monotonic() + 5
        while not entered.exists():
            assert deleter.poll() is None, deleter.communicate()
            assert time.monotonic() < deadline
            time.sleep(.01)
        writer = subprocess.Popen([sys.executable, '-c', writer_prefix + record_source() + writer_suffix,
                                   'write', str(pidfile), '200', new],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while not attempt.exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        # The writer gets a bounded opportunity to finish if it bypasses the lock.
        deadline = time.monotonic() + .25
        while time.monotonic() < deadline and not done.exists():
            time.sleep(.01)
        assert not done.exists(), 'record publication bypassed the deletion lock'
        assert pidfile.read_text() == '100\n'
        assert sidecar.read_text() == old + '\n'
    finally:
        release.write_text('release')
        d_out = deleter.communicate(timeout=10)
        w_out = writer.communicate(timeout=10) if writer is not None else None
    assert deleter.returncode == 0, d_out
    assert writer is not None and writer.returncode == 0, w_out
    assert pidfile.read_text() == '200\n'
    assert sidecar.read_text() == new + '\n'
    assert Path(str(pidfile) + '.lock').exists(), 'lock inode must remain stable'


def test_disappearing_descendant_is_not_mistaken_for_dead_root(tmp_path):
    pidfile = tmp_path / 'backend.pid'
    pidfile.write_text('100\n')
    Path(str(pidfile) + '.identity').write_text('v1:100:501:darwin:1000:1\n')
    script = r'''
source "$1"
edge_process_identity() { echo v1:100:501:darwin:1000:1; }
edge_process_snapshot() { return 3; }
listen_pids() { :; }
kill() { echo unexpected-signal >&2; return 99; }
stop_one backend 14159 "$2" 0
'''
    r = subprocess.run(['bash', '-c', script, '_', str(LIB), str(pidfile)],
                       env=dict(ENV, EDGE_LOG_DIR=str(tmp_path)), capture_output=True,
                       text=True, timeout=10)
    assert r.returncode != 0 and pidfile.exists()
    assert '根进程已退出' not in r.stdout
    assert 'unexpected-signal' not in r.stderr


def test_immortal_process_after_kill_is_reported_alive(tmp_path):
    """KILL 发出后仍存活：按 $SECONDS 墙钟观察满 5 s 才判「仍存活」，保留身份记录（2026-10-29 改墙钟后的行为用例）。"""
    import time
    pidfile = tmp_path / 'backend.pid'
    pidfile.write_text('100\n')
    Path(str(pidfile) + '.identity').write_text('v1:100:501:darwin:1000:1\n')
    signals = tmp_path / 'signals'
    script = r'''
source "$1"
edge_process_identity() { echo v1:100:501:darwin:1000:1; }
edge_process_snapshot() { echo '100 v1:100:501:darwin:1000:1'; }
kill() { echo "$*" >> "$SIGNALS"; return 0; }
sleep() { command sleep 0.05; }
listen_pids() { echo 999; }
stop_one backend 14159 "$2" 0
'''
    t0 = time.monotonic()
    r = subprocess.run(['bash', '-c', script, '_', str(LIB), str(pidfile)],
                       env=dict(ENV, EDGE_LOG_DIR=str(tmp_path), SIGNALS=str(signals)),
                       capture_output=True, text=True, timeout=30)
    elapsed = time.monotonic() - t0
    assert r.returncode != 0 and pidfile.exists()
    assert signals.read_text().splitlines() == ['-TERM 100', '-KILL 100'], r.stderr
    assert '仍存活' in r.stdout + r.stderr and '已停止' not in r.stdout
    assert 4.0 <= elapsed < 12, elapsed   # $SECONDS 整秒粒度：实际窗口落在 (4, 5] s

