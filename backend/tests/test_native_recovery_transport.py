"""Complete production AgentStore/API/SSE with synthetic URLProtocol, no real network."""
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != 'darwin' or not shutil.which('xcrun'), reason='requires Swift toolchain')
def compile_native(tmp_path_factory, delay_cleanup=False):
    temp = tmp_path_factory.mktemp('native-recovery')
    source = ROOT / 'apple-app/Sources'
    binary = temp / 'recovery'
    store = source / 'Store/AgentStore.swift'
    if delay_cleanup:
        # Preserve the COMPLETE production implementation. The only instrumentation
        # is a scheduler gate after attachLoop returns and before its deferred cleanup.
        # URLSession normally completes cancellation too early to force this race.
        actual = store.read_text()
        hook = '            self?.attachEnded(id: id)'
        assert actual.count(hook) == 1
        store = temp / 'AgentStore.swift'
        store.write_text(actual.replace(hook,
            '            await RecoveryCleanupGate.wait()\n' + hook))
    result = subprocess.run(['xcrun','swiftc','-parse-as-library','-module-cache-path',str(temp/'modules'),
        *map(str,sorted((source/'API').glob('*.swift'))),*map(str,sorted((source / "Store").glob("Offline*.swift"))), str(source/'Store/TurnReducer.swift'),
        str(source/'Store/ChatMessage.swift'),str(store),
        str(ROOT/'apple-app/Tests/recovery_support.swift'),str(ROOT/'apple-app/Tests/recovery_transport_test.swift'),'-o',str(binary)],
        capture_output=True,text=True,timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary
@pytest.fixture(scope='module')
def native_recovery(tmp_path_factory):
    return compile_native(tmp_path_factory)

@pytest.fixture(scope='module')
def native_deferred_cleanup(tmp_path_factory):
    return compile_native(tmp_path_factory, delay_cleanup=True)


def test_native_deferred_cleanup(native_deferred_cleanup):
    result = subprocess.run([str(native_deferred_cleanup), str(ROOT/'protocol/fixtures'), 'deferred-cleanup'],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr

@pytest.mark.parametrize('mode',['gap','included','no-queued','thin','old-count','fresh-completed','old-cleanup','overlap','overlap-same','overlap-early','overlap-same-early','overlap-error','404','late-accepted','late-duplicate','late-error','late-conflict','late-accepted-aba','late-duplicate-aba','late-error-aba','late-conflict-aba','late-resume-conflict-aba','client-change','delete-failed','auth-failed','auth-unknown','auth-ok','auth-engine-starting','auth-engine-ready','auth-offline','auth-late'])
def test_native_recovery_transport(native_recovery,mode):
    result=subprocess.run([str(native_recovery),str(ROOT/'protocol/fixtures'),mode],capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stdout+result.stderr
