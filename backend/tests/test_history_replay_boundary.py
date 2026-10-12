"""Real session/journal recovery lower bound, CPU only; no model/network/services."""
import asyncio
import json
from types import SimpleNamespace
from framework.api import routes
from framework.api.schemas import HistoryResponse
from framework.core.journal import BOOT_ID, TurnJournal
from framework.memory.session import SessionManager, ChatSession


def setup(tmp_path):
    mgr = SessionManager(tmp_path / 'sessions')
    session = ChatSession('A', mgr.sessions_dir, 'synthetic')
    mgr._all_sessions['A'] = session
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_manager=mgr, scheduler=None)))
    return mgr, session, req


def test_history_waterline_precedes_read_and_does_not_wait_for_generation(tmp_path):
    mgr, session, req = setup(tmp_path)
    session.journal.emit('turn_queued', {'prompt': 'p'}, turn_id='t_A')
    bound = session.journal.seq
    original = mgr.get_history
    def finish_during_read(sid):
        result = original(sid)
        turn = {'status': 'success', 'prompt': 'p', 'response': [{'role': 'assistant', 'content': 'answer'}]}
        session.append_log(turn)
        session.journal.emit('turn_ended', {**turn, 'turn_index': 0})
        return result
    mgr.get_history = finish_during_read
    async def scenario():
        async with session.lock:
            history = await asyncio.wait_for(routes.get_history('A', req), 1)
        ts = await routes.get_turn_state('A', req)
        assert history['replay_after'] == bound
        assert ts['turn'] is None and ts['last_seq'] > history['replay_after']
        replay = session.journal.replay(history['replay_after'])
        assert replay[-1]['data']['response'][0]['content'] == 'answer'
        assert HistoryResponse(**history).model_dump()['replay_after'] == bound
        assert history['generation'] == BOOT_ID
    asyncio.run(scenario())


def test_cold_missing_and_loaded_waterlines_agree(tmp_path):
    mgr, session, req = setup(tmp_path)
    session.journal.emit('turn_queued', {'prompt': 'p'}, turn_id='t_A')
    session.journal.emit('turn_ended', {'status': 'success', 'turn_index': 0})
    mgr._all_sessions.clear()
    async def scenario():
        cold = await routes.get_history('A', req)
        ts = await routes.get_turn_state('A', req)
        assert cold['replay_after'] == ts['last_seq'] == TurnJournal(session.session_dir).seq
        missing = await routes.get_history('missing', req)
        assert missing['replay_after'] == 0 and missing['turns'] == []
        assert missing['generation'] == cold['generation'] == BOOT_ID
        assert not (mgr.sessions_dir / 'missing').exists()
    asyncio.run(scenario())
