#!/usr/bin/env python3
"""Browser smoke test; requires Playwright and Chromium, never calls a model."""
import argparse
from contextlib import contextmanager
import fcntl
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omnitrix.control import Control
from omnitrix.dashboard import make_server
from omnitrix.store import Store
from omnitrix.web_auth import create_user


class QuietFiles(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def seed(directory):
    store = Store(directory / 'state.db')
    goal = store.create_goal('Improve request throughput safely',
                             ['Preserve result ordering', 'Reduce repeated scans'], directory)
    control = Control(store)
    control.initialize(goal)
    store.execute('UPDATE policy_versions SET instructions=? WHERE goal_id=?',
                  ('Keep changes bounded and measure before editing.', goal))
    now = time.time()
    spec = {'question': 'Measure repeated scans in the request path.',
            'hypothesis': 'Repeated scans dominate request time.',
            'falsification': 'A profile attributes less than 10% to repeated scans.',
            'scope': ['service.py'], 'budget': {'tool_calls': 6, 'seconds': 60, 'attempts': 2}}
    store.execute('INSERT INTO research_jobs VALUES(?,?,?,?,?,?,?,?,?)',
                  ('browser-research', goal, 'research', 'browser-signature', json.dumps(spec), 'running', 1, now, now))
    report = {'findings': [{'statement': 'The request path scans the same records twice.',
                           'classification': 'verified', 'evidence_ids': ['browser-source']}],
              'contradictory_evidence': ['The cache-hit path avoids scanning.'],
              'uncertainties': ['Production hit ratio is not recorded.']}
    store.execute('INSERT INTO research_runs VALUES(?,?,?,?,?,?,?,?,?)',
                  ('browser-run', 'browser-research', 'running', json.dumps(report), None, None, 2, now, None))
    store.execute('INSERT INTO research_sources VALUES(?,?,?,?,?,?,?)',
                  ('browser-source', str(directory), 'service.py', 'a' * 64,
                   'def request(records): return [r for r in records if r.ready]', now, now))
    store.execute('INSERT INTO research_access VALUES(?,?)', ('browser-run', 'browser-source'))
    store.execute('INSERT INTO tasks(id,goal_id,instructions,priority,adapter,status) VALUES(?,?,?,?,?,?)',
                  ('browser-task', goal, spec['question'], 1, 'codex', 'running'))
    store.execute('INSERT INTO task_runtime VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  ('browser-task', 1, 1, 1, 'running', json.dumps({'job_id': 'browser-research'}),
                   'browser-owner', None, None, now, now, now + 600, 2))
    proposal = control.propose(goal, 'policy', {'instructions': 'Run correctness checks before accepting changes.'})
    control.heartbeat(goal, 'browser-owner', start=True)
    return store, control, goal, proposal


def load_pages(directory):
    spec = importlib.util.spec_from_file_location('pages', ROOT / 'scripts/build_pages.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(directory)


def nav(page, view):
    page.locator(f'nav [data-view="{view}"]').click()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium', default='/usr/bin/chromium')
    parser.add_argument('--output', type=Path, default=ROOT / '.omnitrix/ui-check')
    args = parser.parse_args()
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        raise SystemExit('Install browser tooling: pip install playwright; playwright install chromium')
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='omnitrix-ui-') as temporary:
        directory = Path(temporary)
        store, control, goal, proposal = seed(directory)
        users = directory / 'users.json'
        password = secrets.token_urlsafe(24)
        create_user(users, 'operator', password)
        lock = open(str(store.path) + '.lock', 'a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        live = make_server(store.path, port=0, workspace_root=directory,
                           auth_users=users, auth_sessions=directory / 'sessions.db')
        pages = load_pages(directory / 'pages')
        archived = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietFiles, directory=pages))
        try:
            with serving(live) as base, serving(archived) as archive, sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chromium, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base)
                page.get_by_role('button', name='Sign in', exact=True).click()
                page.locator('#login-username').fill('operator')
                page.locator('#login-password').fill(password)
                page.locator('#login-submit').click()
                expect(page.locator('#login-dialog')).not_to_be_visible()
                expect(page.locator('#system-status')).to_contain_text('Live state')
                expect(page.locator('#system-status')).to_contain_text('Manager online')
                expect(page.locator('.requirement')).to_have_count(2)
                assert any(c['httpOnly'] and c['sameSite'] == 'Strict' for c in page.context.cookies())
                page.reload()
                expect(page.locator('#connect-open')).to_have_text('Account')
                results.append('Password login, protected session restoration, live heartbeat and criteria')
                store.execute('UPDATE manager_presence SET heartbeat_at=?', (time.time() - 60,))
                page.locator('#refresh').click()
                expect(page.locator('#system-status')).to_contain_text('Manager stale')
                control.heartbeat(goal, 'browser-owner', stopped=True)
                page.locator('#refresh').click()
                expect(page.locator('#system-status')).to_contain_text('Manager offline')
                control.heartbeat(goal, 'browser-owner', start=True)
                page.locator('#refresh').click()
                expect(page.locator('#system-status')).to_contain_text('Manager online')
                page.route('**/api/state', lambda route: route.abort('failed'))
                page.locator('#refresh').click()
                expect(page.locator('#system-status')).to_contain_text('Connection lost')
                expect(page.locator('#goal-create-open')).to_be_disabled()
                expect(page.locator('#goal-select option:checked')).to_contain_text('Improve request throughput')
                page.unroute('**/api/state')
                page.locator('#refresh').click()
                expect(page.locator('#goal-create-open')).to_be_enabled()
                results.append('Stale/offline manager detection and connection loss preserve state but disable writes')

                page.locator('.skip-link').focus()
                page.keyboard.press('Enter')
                expect(page.locator('#content')).to_be_focused()
                results.append('Keyboard skip navigation reaches the workspace')

                nav(page, 'tasks')
                page.locator('.task-row').first.click()
                expect(page.locator('#drawer')).to_be_visible()
                for title in ('Hypothesis', 'Budget & progress', 'Findings', 'Evidence & contradictions', 'Blockers & remaining uncertainty'):
                    expect(page.locator('#drawer-body')).to_contain_text(title)
                expect(page.locator('#drawer-body')).to_contain_text('service.py')
                page.keyboard.press('Tab')
                assert page.locator('#drawer').evaluate('(e) => e.contains(document.activeElement)')
                page.keyboard.press('Escape')
                expect(page.locator('#drawer')).not_to_be_visible()
                results.append('Structured task detail, evidence and keyboard focus containment')

                nav(page, 'chat')
                page.locator('#manager-text').fill('Keep this draft while the dashboard refreshes.')
                page.locator('#refresh').click()
                expect(page.locator('#manager-text')).to_have_value('Keep this draft while the dashboard refreshes.')
                nav(page, 'overview')
                nav(page, 'chat')
                expect(page.locator('#manager-text')).to_have_value('Keep this draft while the dashboard refreshes.')
                page.locator('#manager-text').fill('What is the current blocker?')
                page.locator('#manager-form button[type=submit]').click()
                expect(page.locator('[data-delivery="queued"]')).to_have_count(1)
                message = store.db.execute("SELECT id FROM messages WHERE kind='control'").fetchone()[0]
                store.execute("UPDATE messages SET state='sending' WHERE id=?", (message,))
                page.locator('#refresh').click()
                expect(page.locator('[data-delivery="processing"]')).to_have_count(1)
                control.ack(message)
                page.locator('#refresh').click()
                expect(page.locator('[data-delivery="acknowledged"]')).to_have_count(1)
                control.message(goal, 'reply', {'response': 'Waiting for production cache-hit measurements.'},
                                direction='manager_to_user', message_id='reply:' + message, state='acknowledged')
                page.locator('#refresh').click()
                expect(page.locator('[data-delivery="answered"]')).to_have_count(1)
                results.append('Draft retention and distinct queued, processing, acknowledged and answered receipts')

                page.locator(f'[data-review="{proposal}"]').click()
                expect(page.locator('.diff-before')).to_contain_text('Keep changes bounded')
                expect(page.locator('.diff-after')).to_contain_text('Run correctness checks')
                expect(page.locator('#proposal-review')).to_contain_text('Affected active tasks · 1')
                expect(page.locator('#proposal-apply')).to_be_disabled()
                page.locator('#proposal-confirm').check()
                expect(page.locator('#proposal-apply')).to_be_enabled()
                page.keyboard.press('Escape')
                assert not store.db.execute("SELECT 1 FROM messages WHERE body LIKE '%accept%'").fetchone()
                results.append('Approval diff, affected task review and explicit confirmation before writes')

                # Lose one successful response, then retry the unchanged durable request.
                dropped, requests = [False], []
                def lose_receipt(route):
                    requests.append(route.request.post_data_json['request_id'])
                    if not dropped[0]:
                        route.fetch()
                        dropped[0] = True
                        route.abort('failed')
                    else:
                        route.continue_()
                page.route('**/api/commands', lose_receipt)
                page.locator('#manager-text').fill('Do not duplicate this request.')
                page.locator('#manager-form button[type=submit]').click()
                expect(page.locator('#send-note')).to_contain_text('Delivery unconfirmed')
                page.locator('#manager-form button[type=submit]').click()
                expect(page.locator('#send-note')).to_contain_text('Queued')
                assert len(requests) == 2 and requests[0] == requests[1]
                assert store.db.execute("SELECT count(*) FROM messages WHERE kind='control'").fetchone()[0] == 2
                page.unroute('**/api/commands', lose_receipt)
                second = store.db.execute("SELECT id FROM messages WHERE kind='control' AND id<>?", (message,)).fetchone()[0]
                store.execute("UPDATE messages SET state='uncertain' WHERE id=?", (second,))
                page.locator('#refresh').click()
                expect(page.locator('[data-delivery="uncertain"]')).to_have_count(1)
                store.execute("UPDATE messages SET state='failed' WHERE id=?", (second,))
                page.locator('#refresh').click()
                expect(page.locator('[data-delivery="error"]')).to_have_count(1)
                results.append('Lost acknowledgment retries preserve request identity and avoid duplicates')

                page.locator('#goal-create-open').click()
                page.locator('#goal-objective').fill('Investigate memory overhead')
                page.locator('#goal-requirements').fill('Measure peak heap\nPreserve request correctness')
                page.locator('#goal-implementation').uncheck()
                page.locator('#goal-submit').click()
                expect(page.locator('#goal-dialog')).not_to_be_visible()
                expect(page.locator('#goal-select option:checked')).to_have_text('Investigate memory overhead')
                assert store.db.execute('SELECT count(*) FROM goals').fetchone()[0] == 2
                assert store.db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
                results.append('Goal creation persists criteria without starting a worker or granting code permission')

                page.select_option('#goal-select', goal)
                nav(page, 'overview')
                control.heartbeat(goal, 'browser-owner')
                page.locator('#refresh').click()
                page.screenshot(path=str(args.output / 'desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.screenshot(path=str(args.output / 'mobile.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile horizontal overflow'
                nav(page, 'chat')
                expect(page.locator('#manager-text')).to_be_visible()
                page.locator('#goal-create-open').click()
                expect(page.locator('#goal-objective')).to_be_visible()
                assert page.locator('#goal-dialog').evaluate('(e) => e.scrollWidth <= e.clientWidth')
                page.keyboard.press('Escape')
                results.append('Desktop and 390px mobile layouts, usable navigation and responsive dialogs')

                page.goto(archive)
                expect(page.locator('#system-status')).to_contain_text('Recorded experiment')
                expect(page.locator('#goal-create-open')).to_be_disabled()
                nav(page, 'chat')
                expect(page.locator('#manager-text')).to_be_disabled()
                expect(page.locator('#manager-form button[type=submit]')).to_be_disabled()
                page.screenshot(path=str(args.output / 'archived.png'), full_page=True)
                results.append('Archived experiment is visibly labeled and all write controls are disabled')
                assert not errors, errors
                browser.close()
        finally:
            lock.close()
            store.db.close()
    report = {'passed': len(results), 'checks': results, 'models_called': False}
    (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
