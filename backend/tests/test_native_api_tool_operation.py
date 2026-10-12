"""Real API transport proves stale settings forms neither write nor consume success."""
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
import json
ROOT=Path(__file__).resolve().parents[2]
pytestmark=pytest.mark.skipif(sys.platform!='darwin' or not shutil.which('xcrun'),reason='requires Swift toolchain')
def compile_operation(temp, mutation=None):
    src=ROOT/'apple-app/Sources'
    files=sorted((src/'API').glob('*.swift'))
    if mutation:
        original=src/'API/ApiToolOperation.swift'
        text=original.read_text()
        old='guard let client, isCurrent() else { throw CancellationError() }' if mutation=='before' else ('        } catch {\n            guard isCurrent(), !Task.isCancelled else { throw CancellationError() }' if mutation=='late-error' else '            guard isCurrent(), !Task.isCancelled else { throw CancellationError() }\n            return result')
        assert text.count(old)==1
        text=text.replace(old,'guard let client else { throw CancellationError() }' if mutation=='before' else ('        } catch {' if mutation=='late-error' else '            return result'))
        copy=temp/'ApiToolOperation.swift';copy.write_text(text)
        files=[copy if f==original else f for f in files]
    binary=temp/'setup'
    result=subprocess.run(['xcrun','swiftc','-parse-as-library','-module-cache-path',str(temp/'modules'),*map(str,files),str(src/'Store/TurnReducer.swift'),str(ROOT/'apple-app/Tests/api_tool_operation_test.swift'),'-o',str(binary)],capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stdout+result.stderr
    return binary
@pytest.fixture(scope='module')
def operation(tmp_path_factory):return compile_operation(tmp_path_factory.mktemp('api-tool-operation'))
@pytest.mark.parametrize('mode',['current','nil','before','late','late-error','current-error','closed','reopened'])
def test_form_connection_ownership(operation,mode):
    r=subprocess.run([str(operation),mode],capture_output=True,text=True,timeout=15)
    assert r.returncode==0,r.stdout+r.stderr
@pytest.mark.parametrize('mutation',['before','late','late-error'])
def test_connection_guard_mutation(tmp_path,mutation):
    binary=compile_operation(tmp_path,mutation)
    r=subprocess.run([str(binary),mutation],capture_output=True,text=True,timeout=15)
    (tmp_path/'mutation-output.txt').write_text(r.stdout+r.stderr)
    expected='stale or missing form cannot write' if mutation=='before' else 'late result cannot advance'
    assert r.returncode==1 and expected in r.stdout,r.stdout+r.stderr

def test_editor_presentation_guard_mutation(tmp_path):
    src=ROOT/'apple-app/Sources'
    original=src/'API/ApiToolOperation.swift'
    text=original.read_text()
    old='connectionIsCurrent() && presentationIsCurrent()'
    assert text.count(old)==1
    copy=tmp_path/'ApiToolOperation.swift';copy.write_text(text.replace(old,'connectionIsCurrent()'))
    files=[copy if p==original else p for p in sorted((src/'API').glob('*.swift'))]
    binary=tmp_path/'setup'
    r=subprocess.run(['xcrun','swiftc','-parse-as-library','-module-cache-path',str(tmp_path/'modules'),*map(str,files),str(src/'Store/TurnReducer.swift'),str(ROOT/'apple-app/Tests/api_tool_operation_test.swift'),'-o',str(binary)],capture_output=True,text=True,timeout=90)
    assert r.returncode==0,r.stdout+r.stderr
    for mode in ('closed','reopened'):
        r=subprocess.run([str(binary),mode],capture_output=True,text=True,timeout=15)
        (tmp_path/(mode+'-mutation-output.txt')).write_text(r.stdout+r.stderr)
        assert r.returncode==1 and 'late result cannot advance' in r.stdout,r.stdout+r.stderr

@pytest.mark.parametrize('overrides,expected', [
    ({}, 'pending'),
    ({'enabled': False}, 'complete'),
    ({'registered': True, 'runtime_declaration_current': True}, 'complete'),
    ({'registered': True}, 'complete'),
    ({'registered': True, 'runtime_declaration_current': False}, 'pending'),
    ({'registered': True, 'enabled': False, 'runtime_declaration_current': False}, 'pending'),
    ({'registered': True, 'auth_env': 'SYNTHETIC_KEY', 'has_secret': False}, 'pending'),
    ({'source': 'repo'}, 'complete'),
    ({'problem': 'synthetic bad declaration'}, 'complete'),
])
def test_same_saved_object_continuation_projection(operation,tmp_path,overrides,expected):
    item={'file':'fixture.yaml','source':'user','name':'fixture','description':'fixture',
          'endpoint':'https://fixture.invalid','method':'POST','enabled':True,
          'has_secret':False,'requires_approval':True,'has_canary':False,'registered':False,
          **overrides}
    path=tmp_path/'item.json';path.write_text(json.dumps(item))
    r=subprocess.run([str(operation),'mapping',str(path)],capture_output=True,text=True,timeout=10)
    assert r.returncode==0,r.stdout+r.stderr
    assert r.stdout.strip()==expected
