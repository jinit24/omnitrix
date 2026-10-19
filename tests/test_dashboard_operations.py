"""Operational dashboard contracts use durable state, never model completion claims."""
import asyncio
import json
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from omnitrix.control import Control
from omnitrix.dashboard import create_goal, make_server, snapshot
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor


class Runtime:
    async def close(self):
        pass


async def test_heartbeat_requires_owner_lock_and_distinguishes_stale_and_stopped(tmp_path):
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Observe work', ['Check behavior'], tmp_path)
    control = Control(store)
    control.heartbeat(goal, 'vanished', start=True)
    assert snapshot(store.path)['manager']['state'] == 'offline'
    supervisor = await Supervisor(store, goal, runtime=Runtime()).open()
    try:
        presence = snapshot(store.path)['manager']
        assert presence['state'] == 'online' and presence['owner'] == supervisor.owner
        store.execute('UPDATE manager_presence SET heartbeat_at=?', (time.time() - 30,))
        assert snapshot(store.path)['manager']['state'] == 'stale'
        control.heartbeat(goal, supervisor.owner)
        assert snapshot(store.path)['manager']['state'] == 'online'
    finally:
        await supervisor.close()
    presence = snapshot(store.path)['manager']
    assert presence['state'] == 'offline' and presence['stopped_at']
    store.db.close()


async def test_heartbeat_continues_while_manager_reply_waits(tmp_path):
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Observe slow replies', ['Keep responsive'], tmp_path)
    supervisor = await Supervisor(store, goal, runtime=Runtime()).open()
    started, release = asyncio.Event(), asyncio.Event()
    async def slow_reply(text, expected=None):
        started.set()
        await release.wait()
        return {'response': 'Still working'}
    supervisor.chat = slow_reply
    supervisor.control.message(goal, 'control', {'command': 'chat', 'text': 'Progress?'})
    try:
        await asyncio.wait_for(started.wait(), 1)
        store.execute('UPDATE manager_presence SET heartbeat_at=0')
        await asyncio.sleep(2.1)
        assert snapshot(store.path)['manager']['state'] == 'online'
        assert not release.is_set()
    finally:
        release.set()
        await supervisor.close()
        store.db.close()


def checks(store, goal):
    Control(store).initialize(goal)
    store.execute('INSERT INTO implementation_authorizations VALUES(?,?,?,?,?,?,?)',
                  ('authorization', goal, 1, 1, '{}', 0, time.time()))
    store.execute('INSERT INTO implementations VALUES(?,?,?,?,?,?,?,?,?,?)',
                  ('implementation', goal, 'authorization', 'base', 'candidate-a', '/unused', 'completed', 1, '', time.time()))
    store.execute('INSERT INTO verifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  ('check', 'implementation', 'candidate-a', 1, 'Original criterion', '[]', 1, 0, '/unused', 'digest', 'local', time.time()))


def test_checklist_requires_exact_candidate_and_current_goal_and_policy_revisions(tmp_path):
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Check coverage', ['Original criterion'], tmp_path)
    checks(store, goal)
    assert snapshot(store.path)['goals'][0]['checklist'][0]['state'] == 'verified'
    store.execute("UPDATE implementations SET candidate='candidate-b'")
    assert snapshot(store.path)['goals'][0]['checklist'][0]['state'] == 'stale'
    store.execute("UPDATE implementations SET candidate='candidate-a'")
    control = Control(store)
    proposal = control.propose(goal, 'requirements', {'constraint': 'Retain behavior', 'add_requirements': ['New criterion']})
    control.accept(goal, proposal)
    state = snapshot(store.path)['goals'][0]
    assert state['original_requirements'] == ['Original criterion']
    assert state['requirements'] == ['Original criterion', 'New criterion']
    assert [(c['state'], c['original']) for c in state['checklist']] == [('stale', True), ('pending', False)]
    store.execute('UPDATE implementation_authorizations SET goal_revision=2,revoked=0')
    store.execute('UPDATE verifications SET goal_revision=2')
    assert snapshot(store.path)['goals'][0]['checklist'][0]['state'] == 'verified'
    proposal = control.propose(goal, 'policy', {'instructions': 'Be more thorough'})
    control.accept(goal, proposal)
    store.execute('UPDATE implementation_authorizations SET revoked=0')
    assert snapshot(store.path)['goals'][0]['checklist'][0]['state'] == 'stale'
    store.db.close()


def test_proposal_review_exposes_changes_and_stale_proposals_cannot_apply(tmp_path):
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Review instructions', ['Original criterion'], tmp_path)
    checks(store, goal)
    control = Control(store)
    proposal = control.propose(goal, 'policy', {'instructions': 'Inspect edge cases'})
    review = snapshot(store.path)['goals'][0]['data']['proposals'][0]['review']
    assert review['old_instructions'] == '' and review['new_instructions'] == 'Inspect edge cases'
    assert review['invalidated_approvals'] == ['authorization']
    assert review['stale_verification_ids'] == ['check']
    assert review['base_goal_revision'] == review['base_policy_revision'] == 1
    newer = control.propose(goal, 'policy', {'instructions': 'Inspect regressions'})
    control.accept(goal, newer)
    review = next(p for p in snapshot(store.path)['goals'][0]['data']['proposals'] if p['id'] == proposal)['review']
    assert review['stale']
    with pytest.raises(ValueError, match='stale'):
        control.accept(goal, proposal)
    store.db.close()


def test_checklist_does_not_hide_another_failing_check_for_same_criterion(tmp_path):
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Check every assertion', ['Original criterion'], tmp_path)
    checks(store, goal)
    store.execute('INSERT INTO verifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  ('edge-check', 'implementation', 'candidate-a', 1, 'Original criterion', '["edge-case"]',
                   0, 1, '/unused', 'digest', 'local', time.time()))
    assert snapshot(store.path)['goals'][0]['checklist'][0]['state'] == 'failed'
    store.db.close()


def test_create_goal_is_atomic_idempotent_and_does_not_start_owner(tmp_path):
    path = tmp_path / 'state.db'
    body = {'request_id': 'creation-1', 'objective': 'Improve reliability',
            'requirements': ['Keep compatibility', 'Keep compatibility'], 'workspace': '.'}
    first = create_goal(path, body, tmp_path)
    assert first == create_goal(path, body, tmp_path)
    assert first['started'] is False
    store = Store(path)
    assert store.db.execute('SELECT count(*) FROM goals').fetchone()[0] == 1
    assert store.snapshot(first['goal_id'])['goal']['original_requirements'] == ['Keep compatibility']
    assert store.db.execute('SELECT count(*) FROM manager_presence').fetchone()[0] == 0
    assert not (tmp_path / 'state.db.lock').exists()
    with pytest.raises(ValueError, match='different content'):
        create_goal(path, body | {'objective': 'Another objective'}, tmp_path)
    store.db.close()


def test_code_goal_requires_scoped_repo_root_and_only_sets_completion_gate(tmp_path):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                    'commit', '--allow-empty', '-m', 'Fixture'], check=True, capture_output=True)
    nested = repo / 'nested'
    nested.mkdir()
    body = {'request_id': 'code-goal', 'objective': 'Correct behavior', 'requirements': ['Keep compatibility'],
            'workspace': 'repo', 'require_implementation': True}
    receipt = create_goal(tmp_path / 'state.db', body, tmp_path)
    store = Store(tmp_path / 'state.db')
    assert store.snapshot(receipt['goal_id'])['goal']['requires_verified_implementation']
    assert store.db.execute('SELECT count(*) FROM implementation_authorizations').fetchone()[0] == 0
    with pytest.raises(ValueError, match='repository root'):
        create_goal(tmp_path / 'state.db', body | {'request_id': 'nested', 'workspace': 'repo/nested'}, tmp_path)
    link = tmp_path / 'escape'
    link.symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match='workspace root'):
        create_goal(tmp_path / 'state.db', body | {'request_id': 'escape', 'workspace': 'escape'}, tmp_path)
    store.db.close()


@pytest.mark.parametrize('changes', [
    {'objective': ' '}, {'requirements': []}, {'requirements': [' ']},
    {'workspace': '..'}, {'require_implementation': True}, {'require_implementation': 'yes'},
])
def test_goal_creation_rejects_invalid_or_unscoped_work_before_writing(tmp_path, changes):
    path = tmp_path / 'absent.db'
    body = {'request_id': 'creation', 'objective': 'Improve reliability', 'requirements': ['Keep behavior']}
    with pytest.raises(ValueError):
        create_goal(path, body | changes, tmp_path)
    assert not path.exists()


def test_goal_creation_http_uses_auth_origin_and_existing_request_size_limit(tmp_path):
    token = 'a' * 40
    origin = 'https://example.test'
    server = make_server(tmp_path / 'state.db', port=0, control_token=token,
                         allowed_origins=[origin], workspace_root=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    body = {'request_id': 'http-goal', 'objective': 'Investigate a bounded issue', 'requirements': ['Preserve correctness']}
    def post(payload, auth=token, source=origin):
        return urlopen(Request(base + '/api/goals', data=json.dumps(payload).encode(),
            headers={'Authorization': 'Bearer ' + auth, 'Origin': source, 'Content-Type': 'application/json'}), timeout=3)
    try:
        for auth, source, status in [('wrong', origin, 401), (token, 'https://unknown.test', 403)]:
            with pytest.raises(HTTPError) as error:
                post(body, auth, source)
            assert error.value.code == status
        with post(body) as result:
            assert result.status == 201
            receipt = json.load(result)
        with post(body) as result:
            assert json.load(result) == receipt
        with pytest.raises(HTTPError) as error:
            post(body | {'objective': 'x' * 17000})
        assert error.value.code == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
