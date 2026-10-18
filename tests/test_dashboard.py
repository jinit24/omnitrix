import json
from pathlib import Path
import sqlite3
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from omnitrix.dashboard import make_server, snapshot
from omnitrix.store import Store
from omnitrix.control import Control


def test_missing_database_stays_missing(tmp_path):
    path = tmp_path / 'absent' / 'state.db'
    assert snapshot(path)['available'] is False
    assert not path.parent.exists()


def test_snapshot_reads_live_wal_without_mutating_state(tmp_path):
    path = tmp_path / 'state.db'
    store = Store(path)
    goal = store.create_goal('<script>alert(1)</script>', ['Keep requirement'], tmp_path)
    Control(store).initialize(goal)
    before = store.db.total_changes
    first = snapshot(path)
    assert first['goals'][0]['requirements'] == ['Keep requirement']
    assert first['goals'][0]['data']['policy_versions'][0]['version'] == 1
    store.execute("UPDATE goals SET status='paused' WHERE id=?", (goal,))
    assert snapshot(path)['goals'][0]['status'] == 'paused'
    assert store.db.total_changes == before + 1
    store.db.close()


def test_readonly_connection_is_enforced(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    store = Store(path)
    store.create_goal('Observe', ['No writes'], tmp_path)
    store.db.close()
    original = sqlite3.connect
    def inspect(*args, **kwargs):
        assert args[0].endswith('?mode=ro')
        assert kwargs['uri'] is True
        db = original(*args, **kwargs)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("UPDATE goals SET status='bad'")
        db.rollback()
        return db
    monkeypatch.setattr(sqlite3, 'connect', inspect)
    assert snapshot(path)['available'] is True


def test_http_is_readonly_and_allowlisted(tmp_path):
    server = make_server(tmp_path / 'absent.db', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base + '/api/state') as response:
            assert not json.load(response)['available']
            assert "default-src 'self'" in response.headers['Content-Security-Policy']
        for route in ('/', '/app.js', '/styles.css', '/docs/', '/docs/architecture.html', '/docs/live-api-v2-gpt54.json'):
            with urlopen(base + route) as response:
                assert response.status == 200
                assert len(response.read()) > 100
        for route in ('/../pyproject.toml', '/docs/../dashboard.py', '/api/state/anything', '/docs/%2e%2e/store.py'):
            with pytest.raises(HTTPError) as error:
                urlopen(base + route)
            assert error.value.code == 404
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(base + '/api/state', method=method, data=b'{}'))
            assert error.value.code == 501
        assert not (tmp_path / 'absent.db').exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_docs_are_current():
    # Optional build dependency; runtime serves committed HTML without it.
    pytest.importorskip('markdown_it')
    import importlib.util
    script = Path(__file__).resolve().parents[1] / 'scripts/build_docs.py'
    spec = importlib.util.spec_from_file_location('build_docs', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, expected in module.build().items():
        assert (module.OUT / name).read_text() == expected, name


def test_pages_build_uses_real_recorded_state_and_project_paths(tmp_path):
    import importlib.util
    script = Path(__file__).resolve().parents[1] / 'scripts/build_pages.py'
    spec = importlib.util.spec_from_file_location('build_pages', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.build(tmp_path / 'omnitrix')
    html = (output / 'index.html').read_text()
    assert 'data-static="true"' in html
    assert 'href="./styles.css"' in html
    assert 'src="./app.js"' in html
    assert 'href="./docs/"' in html
    state = json.loads((output / 'experiment-state.json').read_text())
    assert state['snapshot'] is True
    assert not state.get('demo')
    assert 'Recorded real experiment' in state['reason']
    assert len(state['goals']) == 3
    assert all(not g['id'].startswith('sample-') for g in state['goals'])
    assert {g['status'] for g in state['goals']} == {'completed', 'paused'}
    assert (output / 'docs/experiments/research-quality/buried-bottleneck.html').is_file()
    assert not list(output.rglob('*.db'))
    assert 'href="../"' in (output / 'docs/architecture.html').read_text()
    assert (output / 'docs/live-api-v2-gpt54.json').is_file()


def test_public_snapshot_matches_recorded_checks_and_omits_private_fields():
    root = Path(__file__).resolve().parents[1]
    state = json.loads((root / 'docs/experiments/manager-state.json').read_text())
    report = json.loads((root / 'docs/experiments/live-api-v2-gpt54.json').read_text())
    import hashlib
    assert state['provenance']['report_sha256'] == hashlib.sha256(
        (root / 'docs/experiments/live-api-v2-gpt54.json').read_bytes()).hexdigest()
    results = {t['goal_id']: t for t in report['tasks'] if t['arm'] == 'manager'}
    for goal in state['goals']:
        result = results[goal['id']]
        assert (goal['status'] == 'completed') == result['verified_outcome']
        for check in goal['data']['verifications']:
            expected = next(c for c in result['checks'] if c['requirement'] == check['requirement'])
            assert check['candidate'] == expected['candidate']
            assert bool(check['passed']) == expected['passed']
        assert goal['data']['messages'] == []
        assert all('owner' not in r and 'session_id' not in r for r in goal['data']['runtime'])
    payload = json.dumps(state)
    assert '/workspace/' not in payload and '/tmp/' not in payload
