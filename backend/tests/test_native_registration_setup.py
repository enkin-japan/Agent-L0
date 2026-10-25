"""Complete registration view model and real AgentClient under scripted URLProtocol."""
from pathlib import Path
import subprocess
import pytest
from .test_native_api_tool_operation import pytestmark
ROOT=Path(__file__).resolve().parents[2]
def compile_listing(temp,mutation=None):
    source=ROOT/'apple-app/Sources';model=source/'Store/RegistrationSetupState.swift'
    text=model.read_text()
    if mutation=='generation':
        target='self.active && self.requestID == id && currentClient() === client'
        assert text.count(target)==1;text=text.replace(target,'self.active && currentClient() === client')
    elif mutation=='coupled':
        target='''        async let queue: Void = loadQueue(operation)
        async let tools: Void = loadTools(operation)
        _ = await (queue, tools)'''
        assert text.count(target)==1
        text=text.replace(target,'''        do {
            let result = try await operation.perform { client in
                async let queue = client.listRegistrations()
                async let tools = client.listApiTools()
                return try await (queue, tools.tools)
            }
            items = result.0; tools = result.1
        } catch { queueError = error.localizedDescription }
        loadingQueue = false; loadingTools = false''')
    if mutation:
        model=temp/'RegistrationSetupState.swift';model.write_text(text)
    header=(ROOT/'apple-app/Tests/api_tool_operation_test.swift').read_text().split('@main struct SetupTests')[0]
    support=temp/'support.swift';support.write_text(header)
    binary=temp/'listing'
    r=subprocess.run(['xcrun','swiftc','-parse-as-library','-module-cache-path',str(temp/'modules'),*map(str,sorted((source/'API').glob('*.swift'))),*map(str,sorted((source / "Store").glob("Offline*.swift"))), str(source/'Store/TurnReducer.swift'),str(model),str(support),str(ROOT/'apple-app/Tests/registration_setup_test.swift'),'-o',str(binary)],capture_output=True,text=True,timeout=90)
    assert r.returncode==0,r.stdout+r.stderr
    return binary
@pytest.fixture(scope='module')
def listing(tmp_path_factory):return compile_listing(tmp_path_factory.mktemp('registration-setup'))
@pytest.mark.parametrize('mode',['queue-error','tool-error','slow-tools','overlap','client-change','closed','retain','nil'])
def test_registration_projections(listing,mode):
    r=subprocess.run([str(listing),mode],capture_output=True,text=True,timeout=15)
    assert r.returncode==0,r.stdout+r.stderr
@pytest.mark.parametrize('mutation,mode,expected', [('generation','overlap','old listing cannot overwrite newer projection or error'),('coupled','tool-error','tool failure must not hide pending queue')])
def test_registration_guard_mutation(tmp_path,mutation,mode,expected):
    binary=compile_listing(tmp_path,mutation)
    r=subprocess.run([str(binary),mode],capture_output=True,text=True,timeout=15)
    (tmp_path/'mutation-output.txt').write_text(r.stdout+r.stderr)
    assert r.returncode==1 and expected in r.stdout,r.stdout+r.stderr
