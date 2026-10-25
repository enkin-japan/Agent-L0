"""Bounded native thin-terminal recovery through complete production Store/API/SSE."""
from pathlib import Path
import pytest
import signal
import subprocess
from .test_native_retry_transport import compile_retry, pytestmark
from .native_test_runtime import run_native_case
ROOT=Path(__file__).resolve().parents[2]

def compile_thin(tmp,store_text=None,actual_l10n=False):
    header=(ROOT/'apple-app/Tests/recovery_transport_test.swift').read_text().split('@main struct RecoveryTests')[0]
    harness=tmp/'thin.swift'
    harness.write_text(header+(ROOT/'apple-app/Tests/thin_recovery_test.swift').read_text())
    if not actual_l10n:
        return compile_retry(tmp,store_text=store_text,harness=harness)
    source=ROOT/'apple-app/Sources'
    store=source/'Store/AgentStore.swift'
    if store_text is not None:
        store=tmp/'AgentStore.swift';store.write_text(store_text)
    support=tmp/'support.swift'
    actual_support=(ROOT/'apple-app/Tests/recovery_support.swift').read_text()
    stub='func L(_ value: String) -> String { value }\n'
    assert actual_support.count(stub)==1
    support.write_text(actual_support.replace(stub,''))
    binary=tmp/'thin-locale'
    result=subprocess.run(['xcrun','swiftc','-parse-as-library','-D','THIN_ACTUAL_L10N',
        '-module-cache-path',str(tmp/'modules'),*map(str,sorted((source/'API').glob('*.swift'))),
        *map(str,sorted((source / "Store").glob("Offline*.swift"))), str(source/'Store/TurnReducer.swift'),str(source/'Store/ChatMessage.swift'),str(store),
        str(source/'App/L10n.swift'),str(support),str(harness),'-o',str(binary)],
        capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stdout+result.stderr
    return binary

@pytest.fixture(scope='module')
def native_thin(tmp_path_factory):return compile_thin(tmp_path_factory.mktemp('native-thin'))

@pytest.mark.parametrize('mode',['absent-null','zero-null','absent-ended','zero-ended','nil-index','negative-index','fraction-index','huge-index','string-index','normal','stable-error','manual','preserve-t2','late-session','late-aba','late-client'])
def test_native_thin_recovery(native_thin,mode,tmp_path):
    result=run_native_case(native_thin,[mode,ROOT/'protocol/fixtures'],tmp_path)
    (tmp_path/'case-output.txt').write_text(result.stdout+result.stderr)
    assert result.returncode==0,result.stdout+result.stderr
    print(result.stdout.strip())

@pytest.mark.parametrize('mutation,mode,expected',[
    ('required_coverage','absent-null','timeout missing thin record stops bounded recovery'),
    ('projection_order','preserve-t2','incomplete reload preserves original winner and canonical metadata'),
    ('integer_index','fraction-index','timeout same recovery entrance'),
    ('range_index','huge-index','Double value cannot be converted to Int'),
    ('manual_requirement','manual','manual incomplete reload preserves winner without false success or reattachment'),
    ('stable_error','stable-error','incomplete synchronization error stays visible until explicit recovery'),
])
def test_native_thin_recovery_mutation(tmp_path,mutation,mode,expected):
    store=(ROOT/'apple-app/Sources/Store/AgentStore.swift').read_text()
    if mutation=='required_coverage':
        old='if let required = missingThinRecord,\n               required.index.map({ history.turns.indices.contains($0) }) != true {'
        assert store.count(old)==1
        store=store.replace(old,'if let required = missingThinRecord, false {')
    elif mutation=='projection_order':
        start=store.index('            if let required = missingThinRecord,')
        end=store.index('            // Preserve only',start)
        guard=store[start:end]
        store=store[:start]+store[end:]
        anchor='            replayAfter = min(history.replayAfter ?? 0,'
        assert store.count(anchor)==1
        store=store.replace(anchor,guard+anchor)
    elif mutation=='manual_requirement':
        old='        // Manual sync retries the same coverage requirement; it cannot waive it.'
        assert store.count(old)==1
        store=store.replace(old,'        missingThinRecord = nil')
    elif mutation=='integer_index':
        old=', n.rounded(.towardZero) == n'
        assert store.count(old)==1
        store=store.replace(old,'')
    elif mutation=='range_index':
        old='n < Double(Int.max)'
        assert store.count(old)==1
        store=store.replace(old,'true')
    else:
        old='guard errorMessage != nil, errorMessage != incompleteHistoryError else { return }'
        assert store.count(old)==1
        store=store.replace(old,'guard errorMessage != nil else { return }')
    # Complete production sources compile; fault exists only in isolated Store copy.
    binary=compile_thin(tmp_path,store_text=store)
    result=run_native_case(binary,[mode,ROOT/'protocol/fixtures'],tmp_path/'case')
    (tmp_path/'mutation-output.txt').write_text(result.stdout+result.stderr)
    if mutation=='range_index':
        # The harness turns the runtime trap into exit 128+signal (no crash report / dialog).
        trapped={128+signal.SIGTRAP,128+signal.SIGILL}
        assert result.returncode in trapped and expected in result.stderr,(
            'returncode -5 means exitOnFatalSignals() is not installed (crash dialog is back)\n'
            +result.stdout+result.stderr)
    else:
        assert result.returncode==1 and 'FAIL: '+expected in result.stdout,result.stdout+result.stderr
    print(f'Mutation {mutation}: intended failure ({expected}), rc={result.returncode}')


@pytest.fixture(scope='module')
def native_thin_locale(tmp_path_factory):
    return compile_thin(tmp_path_factory.mktemp('native-thin-locale'),actual_l10n=True)

def test_native_thin_locale_recovery(native_thin_locale,tmp_path):
    result=run_native_case(native_thin_locale,['locale',ROOT/'protocol/fixtures'],tmp_path)
    (tmp_path/'case-output.txt').write_text(result.stdout+result.stderr)
    assert result.returncode==0,result.stdout+result.stderr
    print(result.stdout.strip())

def test_native_thin_locale_identity_mutation(tmp_path):
    store=(ROOT/'apple-app/Sources/Store/AgentStore.swift').read_text()
    old='errorMessage == recorded'
    assert store.count(old)==1
    store=store.replace(old,'errorMessage == L("历史记录尚不完整；原展示已保留，请手动同步后再继续。")')
    binary=compile_thin(tmp_path,store_text=store,actual_l10n=True)
    result=run_native_case(binary,['locale',ROOT/'protocol/fixtures'],tmp_path/'case')
    (tmp_path/'mutation-output.txt').write_text(result.stdout+result.stderr)
    expected='completed sync clears prior incomplete error across language changes'
    assert result.returncode==1 and 'FAIL: '+expected in result.stdout,result.stdout+result.stderr
    print('Mutation localized-identity: intended failure: '+result.stdout.strip())
