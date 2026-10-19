import asyncio
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from omnitrix.control import Control
from omnitrix.dashboard import enqueue, make_server
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor

TOKEN = 'test-token-' + 'x' * 32
ORIGIN = 'https://jinit24.github.io'


@pytest.fixture
def control(tmp_path):
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Improve correctness', ['Keep original criterion'], tmp_path)
    other = store.create_goal('Separate objective', ['Separate criterion'], tmp_path)
    Control(store).initialize(goal)
    Control(store).initialize(other)
    server = make_server(store.path, port=0, control_token=TOKEN, allowed_origins=[ORIGIN])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield store, goal, other, f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    server.server_close()
    thread.join()
    store.db.close()


def payload(goal, **changes):
    return {'goal_id': goal, 'command': 'chat', 'text': 'What are you working on?',
            'request_id': 'request-1', 'goal_revision': 1, 'policy_revision': 1} | changes


def request(base, path='/api/state', body=None, token=TOKEN, origin=ORIGIN, method=None):
    headers = {'Origin': origin}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if body is not None:
        headers['Content-Type'] = 'application/json'
    with urlopen(Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                         headers=headers, method=method), timeout=5) as response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else None, response.headers


def test_token_and_explicit_origin_required(control):
    store, goal, other, base = control
    for token, origin, expected in [(None, ORIGIN, 401), ('wrong', ORIGIN, 401),
                                     (TOKEN, 'https://untrusted.example', 403)]:
        with pytest.raises(HTTPError) as error:
            request(base, token=token, origin=origin)
        assert error.value.code == expected
    status, state, headers = request(base)
    assert status == 200 and state['control_enabled']
    assert TOKEN not in json.dumps(state)
    assert headers['Access-Control-Allow-Origin'] == ORIGIN
    status, _, headers = request(base, token=None, method='OPTIONS')
    assert status == 204
    assert 'Authorization' in headers['Access-Control-Allow-Headers']


def test_idempotent_authenticated_durable_queue(control):
    store, goal, other, base = control
    body = payload(goal)
    _, first, _ = request(base, '/api/commands', body)
    _, second, _ = request(base, '/api/commands', body)
    assert first == second and first['state'] == 'pending'
    assert store.db.execute("SELECT count(*) FROM messages WHERE kind='control'").fetchone()[0] == 1
    Control(store).ack(first['id'])
    assert request(base, '/api/commands', body)[1]['state'] == 'acknowledged'
    with pytest.raises(HTTPError) as error:
        request(base, '/api/commands', body | {'text': 'Different instruction'})
    assert error.value.code == 409
    fresh = Store(store.path)
    assert fresh.db.execute('SELECT state FROM messages WHERE id=?', (first['id'],)).fetchone()[0] == 'acknowledged'
    fresh.db.close()


def test_stale_and_cross_goal_commands_rejected(control):
    store, goal, other, base = control
    store.execute("INSERT INTO tasks(id,goal_id,instructions,priority,adapter,status) VALUES(?,?,?,?,?,?)",
                  ('other-task', other, 'Separate work', 1, 'mock', 'running'))
    for changes in ({'policy_revision': 0}, {'command': 'cancel', 'text': 'other-task'},
                    {'command': 'authorize'}, {'command': 'continue', 'text': '999'},
                    {'command': 'requirements', 'text': json.dumps({'constraint': 'Stop', 'affected_tasks': ['other-task']})}):
        with pytest.raises(HTTPError) as error:
            request(base, '/api/commands', payload(goal, **changes))
        assert error.value.code == 409
    assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0


async def test_policy_proposal_acceptance_and_stale_queued_command(control):
    store, goal, other, base = control
    class Runtime:
        async def close(self):
            pass
    supervisor = await Supervisor(store, goal, runtime=Runtime()).open()
    supervisor.housekeeper.cancel()
    await asyncio.gather(supervisor.housekeeper, return_exceptions=True)
    try:
        enqueue(store.path, payload(goal, command='policy', text='Be thorough about correctness'))
        await supervisor.process_inbox()
        proposal = store.db.execute('SELECT id FROM proposals WHERE goal_id=?', (goal,)).fetchone()[0]
        assert Control(store).current(goal)['policy']['version'] == 1
        enqueue(store.path, payload(goal, command='accept', text=proposal, request_id='accept'))
        enqueue(store.path, payload(goal, command='policy', text='Stale replacement', request_id='old'))
        await supervisor.process_inbox()
        assert Control(store).current(goal)['policy']['version'] == 2
        assert store.db.execute('SELECT count(*) FROM policy_versions WHERE goal_id=?', (goal,)).fetchone()[0] == 2
        reply = store.db.execute('SELECT body FROM messages WHERE id=?', (f'reply:web:{goal}:old',)).fetchone()[0]
        assert 'stale' in reply
        assert store.snapshot(goal)['goal']['original_requirements'] == ['Keep original criterion']
    finally:
        await supervisor.close()


def test_control_never_creates_missing_database(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises(Exception):
        enqueue(path, payload('absent'))
    assert not path.exists()
    with pytest.raises(ValueError):
        make_server(path, port=0, control_token='short')


async def test_accept_rechecks_revision_after_waiting_for_manager_lock(control):
    store, goal, other, base = control
    class Runtime:
        async def close(self):
            pass
    supervisor = await Supervisor(store, goal, runtime=Runtime()).open()
    supervisor.housekeeper.cancel()
    await asyncio.gather(supervisor.housekeeper, return_exceptions=True)
    proposed = supervisor.control.propose(goal, 'policy', {'instructions': 'First proposed policy'})
    newer = supervisor.control.propose(goal, 'policy', {'instructions': 'Newer accepted policy'})
    await supervisor.actor.acquire()
    waiting = asyncio.create_task(supervisor.accept(proposed, expected=payload(goal)))
    try:
        await asyncio.sleep(0)
        supervisor.control.accept(goal, newer)
        supervisor.actor.release()
        with pytest.raises(ValueError, match='stale'):
            await waiting
        assert supervisor.control.current(goal)['policy']['instructions'] == 'Newer accepted policy'
        assert store.db.execute('SELECT state FROM proposals WHERE id=?', (proposed,)).fetchone()[0] == 'pending'
    finally:
        if supervisor.actor.locked():
            supervisor.actor.release()
        await supervisor.close()


def test_malformed_requirement_proposal_does_not_break_state(control):
    store, goal, other, base = control
    bad = {'constraint': 'Preserve interfaces', 'add_requirements': 'not a list'}
    with pytest.raises(ValueError, match='list'):
        Control(store).propose(goal, 'requirements', bad)
    with pytest.raises(HTTPError) as error:
        request(base, '/api/commands', payload(goal, command='requirements', text=json.dumps(bad)))
    assert error.value.code == 409
    store.execute('INSERT INTO proposals VALUES(?,?,?,?,?,?)', ('legacy', goal, 'requirements', json.dumps(bad), 'pending', 1))
    assert request(base)[0] == 200
    proposal = next(p for g in request(base)[1]['goals'] for p in g['data']['proposals'] if p['id'] == 'legacy')
    assert proposal['review']['invalid'] and proposal['review']['stale']
