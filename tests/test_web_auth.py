import json
import threading
from http.cookies import SimpleCookie
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from omnitrix.dashboard import make_server
from omnitrix.store import Store
from omnitrix.control import Control
from omnitrix.web_auth import COOKIE, LoginError, Sessions, create_user, verify_password

PASSWORD = 'correct-test-password-123'


@pytest.fixture
def account(tmp_path):
    users = tmp_path / 'auth/users.json'
    create_user(users, 'operator', PASSWORD)
    return users, tmp_path / 'auth/sessions.db'


def test_password_hash_session_restore_reset_and_expiry(account):
    users, db = account
    assert PASSWORD not in users.read_text()
    assert users.stat().st_mode & 0o777 == 0o600
    encoded = json.loads(users.read_text())['users']['operator']
    assert verify_password(PASSWORD, encoded)
    assert not verify_password('wrong', encoded)
    auth = Sessions(users, db, secure=True)
    token, identity = auth.login('operator', PASSWORD, 'local')
    assert token not in db.read_bytes().decode(errors='ignore')
    cookie = auth.cookie(token)
    assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie and 'Secure' in cookie
    assert Sessions(users, db).authenticate(cookie)['username'] == 'operator'
    create_user(users, 'operator', 'replacement-test-password')
    assert auth.authenticate(cookie) is None
    expired = Sessions(users, db, lifetime=-1)
    token, _ = expired.login('operator', 'replacement-test-password', 'local')
    assert expired.authenticate(expired.cookie(token)) is None


def test_bad_login_and_rate_limit_are_durable(account):
    users, db = account
    auth = Sessions(users, db)
    for i in range(10):
        with pytest.raises(LoginError) as error:
            auth.login('operator' if i % 2 else 'unknown', 'wrong', 'remote')
        assert error.value.status == 401
    with pytest.raises(LoginError) as error:
        Sessions(users, db).login('operator', PASSWORD, 'remote')
    assert error.value.status == 429


def test_http_login_csrf_logout_and_no_public_signup(account, tmp_path):
    users, db = account
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Test browser account', ['Preserve criteria'], tmp_path)
    Control(store).initialize(goal)
    store.db.close()
    server = make_server(tmp_path / 'state.db', port=0, auth_users=users, auth_sessions=db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    def request(path, payload=None, cookie=None, csrf=None, origin=None):
        headers = {'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        if csrf:
            headers['X-CSRF-Token'] = csrf
        if origin:
            headers['Origin'] = origin
        with urlopen(Request(base + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)) as response:
            return json.load(response), response.headers
    try:
        assert request('/api/session')[0] == {'login_available': True, 'authenticated': False}
        with pytest.raises(HTTPError) as error:
            request('/api/state')
        assert error.value.code == 401
        with pytest.raises(HTTPError) as error:
            request('/api/login', {'username': 'operator', 'password': PASSWORD}, origin='https://untrusted.example')
        assert error.value.code == 403
        identity, headers = request('/api/login', {'username': 'operator', 'password': PASSWORD})
        jar = SimpleCookie(headers['Set-Cookie'])
        cookie = f'{COOKIE}={jar[COOKIE].value}'
        assert request('/api/session', cookie=cookie)[0]['authenticated']
        state, _ = request('/api/state', cookie=cookie)
        assert state['auth']['mode'] == 'session' and state['control_enabled']
        command = {'goal_id': goal, 'command': 'chat', 'text': 'Status please', 'request_id': 'one', 'goal_revision': 1, 'policy_revision': 1}
        for csrf in (None, 'forged'):
            with pytest.raises(HTTPError) as error:
                request('/api/commands', command, cookie, csrf)
            assert error.value.code == 403
        receipt, _ = request('/api/commands', command, cookie, identity['csrf_token'])
        assert receipt['state'] == 'pending'
        assert request('/api/commands', command, cookie, identity['csrf_token'])[0]['id'] == receipt['id']
        request('/api/logout', {}, cookie, identity['csrf_token'])
        with pytest.raises(HTTPError) as error:
            request('/api/state', cookie=cookie)
        assert error.value.code == 401
        with pytest.raises(HTTPError) as error:
            request('/api/users', {}, cookie)
        assert error.value.code == 401
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_login_does_not_imply_write_permission(account, tmp_path):
    users, db = account
    server = make_server(tmp_path / 'absent.db', port=0, auth_users=users, auth_sessions=db, allow_control=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + '/api/commands', data=b'{}', headers={'Content-Type':'application/json'}))
        assert error.value.code == 501
        assert not (tmp_path / 'absent.db').exists()
    finally:
        server.shutdown(); server.server_close(); thread.join()
