"""Dashboard snapshots and opt-in authenticated control through the durable inbox."""
import fcntl
import json
import secrets
import sqlite3
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .web_auth import Sessions, LoginError

ASSETS = Path(__file__).with_name('web')
JSON_FIELDS = {'requirements', 'constraints', 'spec', 'body', 'detail', 'result', 'report', 'evaluation', 'citations', 'feedback', 'argv'}
TABLES = ('tasks', 'research_jobs', 'opportunities', 'activity', 'decisions', 'messages',
          'knowledge', 'implementations', 'goal_versions', 'policy_versions', 'research_limits',
          'completion_audits', 'proposals', 'campaigns', 'spending_limits', 'spending_requests', 'frontier_meta',
          'implementation_authorizations')


def manager_presence(path, records):
    """A recent heartbeat only means online while the exclusive owner lock is held."""
    presence = records[0] if records else {}
    state = 'offline'
    if presence and not presence.get('stopped_at'):
        try:
            with open(str(path) + '.lock', 'r') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    state = 'online' if time.time() - presence['heartbeat_at'] <= 10 else 'stale'
        except FileNotFoundError:
            pass
    return presence | {'state': state}


def enrich_goal(goal, bases):
    """Expose review and coverage using current requirements and exact candidate checks."""
    data = goal['data']
    versions, policies = data['goal_versions'], data['policy_versions']
    original = goal['original_requirements']
    goal['revision'] = versions[0]['revision'] if versions else 1
    goal['policy_revision'] = policies[0]['version'] if policies else 1
    goal['current_policy'] = policies[0]['instructions'] if policies else ''
    goal.setdefault('constraints', [])
    goal['requirements'] = list(dict.fromkeys(original + goal['requirements']))
    auths = {a['id']: a for a in data['implementation_authorizations']}
    implementations = {i['id']: i for i in data['implementations']}
    checklist = []
    verified_implementations = set(implementations)
    for criterion in goal['requirements']:
        evidence = [v for v in data['verifications'] if v['requirement'] == criterion]
        current = []
        seen = set()
        for check in evidence:
            implementation = implementations.get(check['implementation_id'], {})
            authorization = auths.get(implementation.get('authorization'), {})
            identity = check['implementation_id'], check['candidate'], json.dumps(check['argv'], sort_keys=True)
            if identity in seen:
                continue
            seen.add(identity)
            if (check['candidate'] == implementation.get('candidate') and not authorization.get('revoked', True)
                and check['goal_revision'] == goal['revision'] == authorization.get('goal_revision')
                and authorization.get('policy_revision') == goal['policy_revision']):
                current.append(check)
        passing = {c['implementation_id'] for c in current if all(v['passed'] for v in current if v['implementation_id'] == c['implementation_id'])}
        verified_implementations &= passing
        state = ('verified' if passing else 'failed') if current else ('stale' if evidence else 'pending')
        checklist.append({'criterion': criterion, 'mandatory': True, 'original': criterion in original,
                          'goal_revision': goal['revision'], 'policy_revision': goal['policy_revision'],
                          'state': state, 'evidence': current or evidence})
    goal['checklist'] = checklist
    goal['completion'] = {'verified': sum(c['state'] == 'verified' for c in checklist), 'total': len(checklist),
                          'all_verified': bool(checklist) and bool(verified_implementations),
                          'audited': any(a['passed'] and a['revision'] == goal['revision'] and
                              any(i['id'] in verified_implementations and i['candidate'] == a['candidate'] and i['status'] == 'completed' and
                                  not auths.get(i['authorization'], {}).get('revoked', True) and
                                  auths.get(i['authorization'], {}).get('policy_revision') == goal['policy_revision'] and
                                  auths.get(i['authorization'], {}).get('goal_revision') == goal['revision']
                                  for i in implementations.values()) for a in data['completion_audits'])}
    active = [r for r in data['runtime'] if r['state'] in ('running', 'waiting', 'paused')]
    for proposal in data['proposals']:
        body = proposal['body'] if isinstance(proposal['body'], dict) else {}
        base = bases.get(proposal['id'], {'goal_revision': goal['revision'], 'policy_revision': goal['policy_revision']})
        before = next((p['instructions'] for p in policies if p['version'] == base['policy_revision']), goal['current_policy'])
        additions = body.get('add_requirements', [])
        invalid = not isinstance(additions, list) or any(not isinstance(c, str) or not c.strip() for c in additions)
        additions = [] if invalid else additions
        criteria_before = next((v['requirements'] for v in versions if v['revision'] == base['goal_revision']), goal['requirements'])
        targeted = body.get('affected_tasks')
        affected = [r['task_id'] for r in active if targeted is None or r['task_id'] in targeted]
        jobs = {r['spec'].get('job_id') for r in active if r['task_id'] in affected and isinstance(r['spec'], dict)}
        proposal['review'] = {'old_instructions': before, 'new_instructions': body.get('instructions', before),
            'constraint': body.get('constraint'), 'added_requirements': additions, 'invalid': invalid,
            'old_requirements': criteria_before,
            'new_requirements': list(dict.fromkeys(criteria_before + additions)),
            'affected_task_ids': affected, 'invalidated_approvals': [a['id'] for a in auths.values() if not a['revoked']],
            'affected_evidence_ids': [k['id'] for k in data['knowledge'] if k['job_id'] in jobs],
            'stale_verification_ids': [v['id'] for v in data['verifications']],
            'base_goal_revision': base['goal_revision'], 'base_policy_revision': base['policy_revision'],
            'stale': invalid or base['goal_revision'] != goal['revision'] or base['policy_revision'] != goal['policy_revision']}


def snapshot(path):
    """One consistent, bounded SQLite read transaction, including WAL commits."""
    path = Path(path).resolve()
    if not path.is_file():
        return {'available': False, 'updated': time.time(), 'manager': {'state': 'offline'}, 'goals': [], 'reason': 'No state database yet. Create a goal to get started.'}
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'goals' not in tables:
            raise ValueError('Not an Omnitrix state database')
        def rows(table, where='', args=()):
            if table not in tables:
                return []
            output = []
            for row in db.execute(f'SELECT * FROM {table} {where} ORDER BY rowid DESC LIMIT 200', args):
                item = dict(row)
                for key in item.keys() & JSON_FIELDS:
                    try:
                        item[key] = json.loads(item[key])
                    except (ValueError, TypeError):
                        pass
                output.append(item)
            return output
        goals = rows('goals')
        bases = {b['proposal_id']: {'goal_revision': b['goal_revision'], 'policy_revision': b['policy_revision']} for b in rows('proposal_revisions')}
        for goal in goals:
            goal['original_requirements'] = list(goal['requirements'])
            goal['data'] = {t: rows(t, 'WHERE goal_id=?', (goal['id'],)) for t in TABLES}
            versions = goal['data']['goal_versions']
            if versions:
                goal.update({k: versions[0][k] for k in ('objective', 'requirements', 'constraints', 'revision')})
            goal['data']['runtime'] = rows('task_runtime', 'WHERE task_id IN (SELECT id FROM tasks WHERE goal_id=?)', (goal['id'],))
            goal['data']['research_runs'] = rows('research_runs', 'WHERE job_id IN (SELECT id FROM research_jobs WHERE goal_id=?)', (goal['id'],))
            runs = 'SELECT id FROM research_runs WHERE job_id IN (SELECT id FROM research_jobs WHERE goal_id=?)'
            goal['data']['research_access'] = rows('research_access', f'WHERE run_id IN ({runs})', (goal['id'],))
            goal['data']['research_sources'] = rows('research_sources', f'WHERE id IN (SELECT source_id FROM research_access WHERE run_id IN ({runs}))', (goal['id'],))
            goal['data']['research_queries'] = rows('research_queries', f'WHERE run_id IN ({runs})', (goal['id'],))
            goal['data']['verifications'] = rows('verifications', 'WHERE implementation_id IN (SELECT id FROM implementations WHERE goal_id=?)', (goal['id'],))
            enrich_goal(goal, bases)
        return {'available': True, 'updated': time.time(), 'goals': goals, 'manager': manager_presence(path, rows('manager_presence')), 'limit': 200}
    finally:
        db.close()


def create_goal(path, payload, workspace_root):
    """Persist a scoped goal once; creation never grants code permission or starts an owner."""
    from .control import Control
    from .store import Store
    if not isinstance(payload, dict):
        raise ValueError('Expected a JSON object')
    request_id, objective = payload.get('request_id'), payload.get('objective')
    criteria, workspace = payload.get('requirements'), payload.get('workspace', '.')
    require_code = payload.get('require_implementation', False)
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError('Stable request ID required')
    if not isinstance(objective, str) or not 1 <= len(objective.strip()) <= 12000:
        raise ValueError('Provide a nonempty objective, at most 12000 characters')
    if (not isinstance(criteria, list) or not 1 <= len(criteria) <= 30 or
        any(not isinstance(c, str) or not 1 <= len(c.strip()) <= 2000 for c in criteria)):
        raise ValueError('Provide 1–30 nonempty acceptance criteria, at most 2000 characters each')
    if not isinstance(workspace, str) or not workspace.strip() or len(workspace) > 4096 or type(require_code) is not bool:
        raise ValueError('Provide a workspace path and boolean require_implementation')
    root = Path(workspace_root).resolve()
    target = (root / workspace).resolve()
    if not target.is_relative_to(root) or not target.is_dir():
        raise ValueError('Workspace must be an existing directory inside the configured workspace root')
    if require_code:
        try:
            check = subprocess.run(['git', '-C', str(target), 'rev-parse', '--verify', 'HEAD^{commit}'],
                                   capture_output=True, timeout=3)
            toplevel = subprocess.run(['git', '-C', str(target), 'rev-parse', '--show-toplevel'],
                                      capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Git repository validation could not complete') from None
        if check.returncode or toplevel.returncode or Path(toplevel.stdout.strip()).resolve() != target:
            raise ValueError('Implementation goals require a Git repository root with a committed revision')
    body = json.dumps({'objective': objective.strip(), 'requirements': list(dict.fromkeys(c.strip() for c in criteria)),
                       'workspace': str(target), 'require_implementation': require_code}, sort_keys=True)
    store = Store(path)
    try:
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            prior = store.db.execute('SELECT body,goal_id FROM goal_requests WHERE request_id=?', (request_id,)).fetchone()
            if prior:
                if prior['body'] != body:
                    raise ValueError('Request ID already used for different content')
                goal_id = prior['goal_id']
            else:
                goal_id = store.create_goal(objective.strip(), json.loads(body)['requirements'], target)
                control = Control(store)
                control.initialize(goal_id)
                if require_code:
                    control.require_implementation(goal_id)
                store.db.execute('INSERT INTO goal_requests VALUES(?,?,?)', (request_id, body, goal_id))
                control.event(goal_id, 'goal_created', {'source': 'dashboard', 'request_id': request_id})
        return {'goal_id': goal_id, 'state': 'created', 'started': False}
    finally:
        store.db.close()


def enqueue(path, payload):
    """Append to the existing owner's inbox; never run a second manager."""
    if not isinstance(payload, dict):
        raise ValueError('Expected a JSON object')
    command, text = payload.get('command'), payload.get('text')
    allowed = {'chat', 'policy', 'requirements', 'accept', 'pause', 'cancel', 'steer', 'continue', 'reconcile'}
    if command not in allowed or not isinstance(text, str) or not text.strip() or len(text) > 12000:
        raise ValueError('Choose a supported command and nonempty text')
    goal_id, request_id = payload.get('goal_id'), payload.get('request_id')
    if not isinstance(goal_id, str) or not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError('Goal and stable request ID required')
    if any(type(payload.get(k)) is not int for k in ('goal_revision', 'policy_revision')):
        raise ValueError('Current integer goal and policy revisions required')
    body = {k: payload.get(k) for k in ('command', 'text', 'goal_revision', 'policy_revision')}
    encoded = json.dumps(body, sort_keys=True)
    message_id = f'web:{goal_id}:{request_id}'
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True, timeout=2)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT body,state FROM messages WHERE id=?', (message_id,)).fetchone()
            if prior:
                if prior[0] != encoded:
                    raise ValueError('Request ID already used for different content')
                return {'id': message_id, 'state': prior[1]}
            if not db.execute('SELECT 1 FROM goals WHERE id=?', (goal_id,)).fetchone():
                raise ValueError('Unknown goal')
            revision = db.execute('SELECT MAX(revision) FROM goal_versions WHERE goal_id=?', (goal_id,)).fetchone()[0] or 1
            policy = db.execute('SELECT MAX(version) FROM policy_versions WHERE goal_id=?', (goal_id,)).fetchone()[0] or 1
            if body['goal_revision'] != revision or body['policy_revision'] != policy:
                raise ValueError('Instructions changed. Refresh before sending.')
            if command in ('pause', 'cancel', 'steer', 'reconcile'):
                task = text.split(' ', 1)[0]
                if not db.execute('SELECT 1 FROM tasks WHERE id=? AND goal_id=?', (task, goal_id)).fetchone():
                    raise ValueError('Task does not belong to this goal')
                if command == 'steer' and (' ' not in text or not text.split(' ', 1)[1].strip()):
                    raise ValueError('Steering requires a task ID and instruction')
            if command == 'accept' and not db.execute("SELECT 1 FROM proposals WHERE id=? AND goal_id=? AND state='pending'", (text, goal_id)).fetchone():
                raise ValueError('Unknown pending proposal')
            if command == 'continue' and (not text.isdecimal() or not 1 <= int(text) <= 50):
                raise ValueError('Continue requires 1–50 decisions')
            if command == 'requirements':
                update = json.loads(text)
                if not isinstance(update, dict) or not isinstance(update.get('constraint'), str) or not update['constraint'].strip():
                    raise ValueError('Requirements need a constraint')
                additions = update.get('add_requirements', [])
                if not isinstance(additions, list) or any(not isinstance(c, str) or not c.strip() for c in additions):
                    raise ValueError('Added requirements must be a list of nonempty criteria')
                affected = update.get('affected_tasks', [])
                if not isinstance(affected, list) or any(not isinstance(t, str) or not db.execute('SELECT 1 FROM tasks WHERE id=? AND goal_id=?', (t, goal_id)).fetchone() for t in affected):
                    raise ValueError('Affected tasks must belong to this goal')
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                (message_id, goal_id, None, 'user_to_manager', 'control', encoded, 'pending', None, time.time(), None))
        return {'id': message_id, 'state': 'pending'}
    finally:
        db.close()


def make_server(path, host='127.0.0.1', port=8765, *, control_token=None, allowed_origins=(), workspace_root=None,
                auth_users=None, auth_sessions=None, secure_cookies=False, allow_control=None):
    workspace_root = Path(workspace_root or Path.cwd()).resolve()
    sessions = Sessions(auth_users, auth_sessions or str(path) + '.web-sessions.db', secure=secure_cookies) if auth_users else None
    control_enabled = bool(control_token or sessions) if allow_control is None else bool(allow_control)
    if control_enabled and not (control_token or sessions):
        raise ValueError('Configure a dashboard user or token before enabling control')
    if control_token is not None and len(control_token) < 32:
        raise ValueError('OMNITRIX_DASHBOARD_TOKEN must contain at least 32 characters')
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def origin_allowed(self):
            origin = self.headers.get('Origin')
            return not origin or origin in allowed_origins or origin in (f"http://{self.headers.get('Host')}", f"https://{self.headers.get('Host')}")

        def authorized(self, write=False):
            if not self.origin_allowed():
                self.respond(403, b'{"error":"Origin not allowed"}', 'application/json')
                return False
            try:
                identity = sessions.authenticate(self.headers.get('Cookie')) if sessions else None
            except (OSError, ValueError, sqlite3.Error):
                self.respond(503, b'{"error":"Authentication unavailable"}', 'application/json')
                return False
            bearer = control_token and secrets.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + control_token).encode())
            if not identity and bearer:
                identity = {'mode': 'token'}
            if (control_token or sessions) and not identity:
                self.respond(401, b'{"error":"Sign in or connect with a dashboard token"}', 'application/json')
                return False
            self.identity = identity or {'mode': 'readonly'}
            if write and identity and identity['mode'] == 'session' and not secrets.compare_digest(
                    self.headers.get('X-CSRF-Token', '').encode(), identity['csrf_token'].encode()):
                self.respond(403, b'{"error":"Session changed; refresh before sending"}', 'application/json')
                return False
            return True

        def json_body(self):
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384 or self.headers.get_content_type() != 'application/json':
                raise ValueError('Expected application/json, at most 16 KiB')
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError('Expected a JSON object')
            return value

        def login(self):
            origin = self.headers.get('Origin')
            if not sessions:
                self.respond(404, b'{"error":"Password login is not configured"}', 'application/json')
                return
            if origin and origin not in (f"http://{self.headers.get('Host')}", f"https://{self.headers.get('Host')}"):
                self.respond(403, b'{"error":"Sign in from the manager site"}', 'application/json')
                return
            try:
                payload = self.json_body()
                token, identity = sessions.login(payload.get('username'), payload.get('password'), self.client_address[0])
                self.respond(200, json.dumps(identity | {'authenticated': True, 'login_available': True}).encode(),
                             'application/json', {'Set-Cookie': sessions.cookie(token)})
            except LoginError as exc:
                self.respond(exc.status, json.dumps({'error': str(exc)}).encode(), 'application/json')
            except (ValueError, TypeError):
                self.respond(400, b'{"error":"Invalid login request"}', 'application/json')
            except (OSError, sqlite3.Error):
                self.respond(503, b'{"error":"Login unavailable"}', 'application/json')

        def do_OPTIONS(self):
            if self.origin_allowed():
                self.respond(204, b'', 'text/plain')
            else:
                self.respond(403, b'', 'text/plain')

        def do_POST(self):
            route = urlsplit(self.path).path
            if route == '/api/login':
                self.login()
                return
            if route == '/api/logout' and sessions:
                if not self.authorized(write=True):
                    return
                sessions.logout(self.headers.get('Cookie'))
                self.respond(200, b'{"authenticated":false}', 'application/json', {'Set-Cookie': sessions.cookie(clear=True)})
                return
            if not control_enabled:
                self.send_error(501, 'Control is disabled')
                return
            if not self.authorized(write=True):
                return
            if route not in ('/api/commands', '/api/goals'):
                self.respond(404, b'{"error":"Unknown route"}', 'application/json')
                return
            try:
                payload = self.json_body()
                receipt = create_goal(path, payload, workspace_root) if route == '/api/goals' else enqueue(path, payload)
                self.respond(201 if route == '/api/goals' else 202, json.dumps(receipt).encode(), 'application/json')
            except (ValueError, TypeError) as exc:
                self.respond(409, json.dumps({'error': str(exc)}).encode(), 'application/json')
            except sqlite3.Error:
                self.respond(503, b'{"error":"Inbox unavailable; retry with the same request ID"}', 'application/json')

        def do_GET(self):
            route = urlsplit(self.path).path
            if route == '/api/session':
                try:
                    identity = sessions.authenticate(self.headers.get('Cookie')) if sessions else None
                    data = {'login_available': bool(sessions), 'authenticated': bool(identity)} | (identity or {})
                    self.respond(200, json.dumps(data).encode(), 'application/json')
                except (OSError, ValueError, sqlite3.Error):
                    self.respond(503, b'{"error":"Authentication unavailable"}', 'application/json')
                return
            if route == '/api/state':
                if not self.authorized():
                    return
                try:
                    payload = json.dumps(snapshot(path) | {'control_enabled': control_enabled, 'workspace_root': str(workspace_root), 'auth': self.identity}).encode()
                    self.respond(200, payload, 'application/json')
                except (sqlite3.Error, ValueError):
                    self.respond(503, b'{"error":"State temporarily unavailable. Retrying automatically."}', 'application/json')
                return
            files = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/styles.css': ('styles.css', 'text/css')}
            if route.startswith('/docs/'):
                name = route.removeprefix('/docs/') or 'index.html'
                types = {'.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.json': 'application/json'}
                root = ASSETS / 'docs'
                docs = {p.relative_to(root).as_posix(): p for p in root.rglob('*') if p.is_file() and p.suffix in types}
                for file in list(docs.values()):
                    docs.setdefault(file.name, file)
                if name in docs:
                    file = docs[name]
                    self.respond(200, file.read_bytes(), types[file.suffix])
                    return
            if route not in files:
                self.respond(404, b'Not found', 'text/plain')
                return
            name, kind = files[route]
            self.respond(200, (ASSETS / name).read_bytes(), kind)

        def respond(self, status, body, kind, extra_headers=None):
            self.send_response(status)
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            origin = self.headers.get('Origin')
            if origin and origin in allowed_origins:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Vary', 'Origin')
                self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, X-CSRF-Token')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Content-Type', kind + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return ThreadingHTTPServer((host, port), Handler)


def serve(path, host='127.0.0.1', port=8765, *, control_token=None, allowed_origins=(), workspace_root=None,
          auth_users=None, auth_sessions=None, secure_cookies=False, allow_control=None):
    server = make_server(path, host, port, control_token=control_token, allowed_origins=allowed_origins, workspace_root=workspace_root, auth_users=auth_users,
                         auth_sessions=auth_sessions, secure_cookies=secure_cookies, allow_control=allow_control)
    mode = 'authenticated dashboard' if control_token or auth_users else 'read-only'
    print(f'Omnitrix dashboard listening on {host}:{server.server_port} ({mode})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
