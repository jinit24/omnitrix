"""Dashboard snapshots and opt-in authenticated control through the durable inbox."""
import json
import secrets
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ASSETS = Path(__file__).with_name('web')
JSON_FIELDS = {'requirements', 'constraints', 'spec', 'body', 'detail', 'result', 'report', 'evaluation', 'citations', 'feedback'}
TABLES = ('tasks', 'research_jobs', 'opportunities', 'activity', 'decisions', 'messages',
          'knowledge', 'implementations', 'goal_versions', 'policy_versions', 'research_limits',
          'completion_audits', 'proposals', 'campaigns', 'spending_limits', 'spending_requests', 'frontier_meta')


def snapshot(path):
    """One consistent, bounded SQLite read transaction, including WAL commits."""
    path = Path(path).resolve()
    if not path.is_file():
        return {'available': False, 'updated': time.time(), 'goals': [], 'reason': 'No state database yet. Create a goal with the CLI to get started.'}
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
        for goal in goals:
            goal['data'] = {t: rows(t, 'WHERE goal_id=?', (goal['id'],)) for t in TABLES}
            versions = goal['data']['goal_versions']
            if versions:
                goal.update({k: versions[0][k] for k in ('objective', 'requirements', 'constraints', 'revision')})
            goal['data']['runtime'] = rows('task_runtime', 'WHERE task_id IN (SELECT id FROM tasks WHERE goal_id=?)', (goal['id'],))
            goal['data']['research_runs'] = rows('research_runs', 'WHERE job_id IN (SELECT id FROM research_jobs WHERE goal_id=?)', (goal['id'],))
            goal['data']['verifications'] = rows('verifications', 'WHERE implementation_id IN (SELECT id FROM implementations WHERE goal_id=?)', (goal['id'],))
        return {'available': True, 'updated': time.time(), 'goals': goals, 'limit': 200}
    finally:
        db.close()


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
                affected = update.get('affected_tasks', [])
                if not isinstance(affected, list) or any(not isinstance(t, str) or not db.execute('SELECT 1 FROM tasks WHERE id=? AND goal_id=?', (t, goal_id)).fetchone() for t in affected):
                    raise ValueError('Affected tasks must belong to this goal')
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                (message_id, goal_id, None, 'user_to_manager', 'control', encoded, 'pending', None, time.time(), None))
        return {'id': message_id, 'state': 'pending'}
    finally:
        db.close()


def make_server(path, host='127.0.0.1', port=8765, *, control_token=None, allowed_origins=()):
    if control_token is not None and len(control_token) < 32:
        raise ValueError('OMNITRIX_DASHBOARD_TOKEN must contain at least 32 characters')
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def origin_allowed(self):
            origin = self.headers.get('Origin')
            return not origin or origin in allowed_origins or origin in (f"http://{self.headers.get('Host')}", f"https://{self.headers.get('Host')}")

        def authorized(self):
            if not self.origin_allowed():
                self.respond(403, b'{"error":"Origin not allowed"}', 'application/json')
                return False
            if control_token and not secrets.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + control_token).encode()):
                self.respond(401, b'{"error":"Connect with the dashboard token"}', 'application/json')
                return False
            return True

        def do_OPTIONS(self):
            if self.origin_allowed():
                self.respond(204, b'', 'text/plain')
            else:
                self.respond(403, b'', 'text/plain')

        def do_POST(self):
            if not control_token:
                self.send_error(501, 'Control is disabled')
                return
            if not self.authorized():
                return
            if urlsplit(self.path).path != '/api/commands':
                self.respond(404, b'{"error":"Unknown route"}', 'application/json')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('Expected application/json, at most 16 KiB')
                payload = json.loads(self.rfile.read(length))
                receipt = enqueue(path, payload)
                self.respond(202, json.dumps(receipt).encode(), 'application/json')
            except (ValueError, TypeError) as exc:
                self.respond(409, json.dumps({'error': str(exc)}).encode(), 'application/json')
            except sqlite3.Error:
                self.respond(503, b'{"error":"Inbox unavailable; retry with the same request ID"}', 'application/json')

        def do_GET(self):
            route = urlsplit(self.path).path
            if route == '/api/state':
                if not self.authorized():
                    return
                try:
                    payload = json.dumps(snapshot(path) | {'control_enabled': bool(control_token)}).encode()
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

        def respond(self, status, body, kind):
            self.send_response(status)
            origin = self.headers.get('Origin')
            if origin and origin in allowed_origins:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Vary', 'Origin')
                self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
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


def serve(path, host='127.0.0.1', port=8765, *, control_token=None, allowed_origins=()):
    server = make_server(path, host, port, control_token=control_token, allowed_origins=allowed_origins)
    mode = 'authenticated control' if control_token else 'read-only'
    print(f'Omnitrix dashboard listening on {host}:{server.server_port} ({mode})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
