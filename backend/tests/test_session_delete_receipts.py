"""The HTTP adapter preserves real absence versus a failed physical deletion."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from framework.api.routes import router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from framework.memory.session import SessionManager
FIXTURE=json.loads((Path(__file__).resolve().parents[2]/'protocol/fixtures/017-delete-absent.responses.json').read_text())
async def http_delete(manager, session_id):
    app = FastAPI()  # No lifespan, auth store, sockets, models or services.
    app.include_router(router)
    app.state.session_manager = manager
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.invalid") as client:
        response = await client.delete("/api/v1/chat/" + session_id)
    assert response.status_code == 200
    return response.json()
def check(result,kind):
    expected=FIXTURE[kind]
    assert result['status']==expected['status']
    assert result.get('reason')==expected.get('reason')
def test_absent_local_draft_has_explicit_receipt(tmp_path):
    manager=SessionManager(tmp_path)
    check(asyncio.run(http_delete(manager,'local-draft')),'absent')
    assert not (tmp_path/'local-draft').exists() and not manager._all_sessions

def test_existing_session_is_deleted(tmp_path):
    manager=SessionManager(tmp_path);(tmp_path/'existing').mkdir()
    (tmp_path/'existing'/'synthetic.txt').write_text('fixture')
    check(asyncio.run(http_delete(manager,'existing')),'deleted')
    assert not (tmp_path/'existing').exists()

def test_disk_failure_is_not_absence(tmp_path,monkeypatch):
    import framework.memory.session as module
    manager=SessionManager(tmp_path);(tmp_path/'existing').mkdir()
    def fail(path):raise PermissionError('synthetic refusal')
    monkeypatch.setattr(module.shutil,'rmtree',fail)
    check(asyncio.run(http_delete(manager,'existing')),'failed')
    assert (tmp_path/'existing').exists()

def test_hot_session_without_directory_still_counts_as_deleted(tmp_path):
    manager=SessionManager(tmp_path);closed=[]
    manager._all_sessions['hot']=SimpleNamespace(cancel_requested=False,lock=asyncio.Lock(),freeze=lambda:None,journal=SimpleNamespace(close=lambda:closed.append(True)))
    result=asyncio.run(http_delete(manager,'hot'))
    check(result,'deleted')
    assert result['frozen'] is True and closed==[True]


def test_missing_response_field_mutation(tmp_path):
    import inspect
    from framework.api import schemas, routes
    source = inspect.getsource(schemas.DeleteSessionResponse)
    line = '    reason: Optional[str] = None\n'
    assert source.count(line) == 1
    mutant = tmp_path/'schema.py'
    mutant.write_text(source.replace(line, ''))
    namespace = dict(vars(schemas))
    exec(compile(mutant.read_text(), str(mutant), 'exec'), namespace)

    async def mutated_request():
        app = FastAPI()
        app.state.session_manager = SessionManager(tmp_path/'sessions')
        app.delete('/api/v1/chat/{session_id}', response_model=namespace['DeleteSessionResponse'])(routes.delete_session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://fixture.invalid') as client:
            return (await client.delete('/api/v1/chat/local-draft')).json()

    result = asyncio.run(mutated_request())
    with pytest.raises(AssertionError) as failure:
        check(result, 'absent')
    (tmp_path/'mutation-output.txt').write_text(json.dumps(result)+'\n'+str(failure.value))
    print('Mutation omitted HTTP reason: intended absence assertion failure')
