"""Local, read-only observability. Never instantiate Store or take manager ownership."""
import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ASSETS = Path(__file__).with_name('web')
JSON_FIELDS = {'requirements', 'constraints', 'spec', 'body', 'detail', 'result', 'report', 'evaluation', 'citations', 'feedback'}
TABLES = ('tasks', 'research_jobs', 'opportunities', 'activity', 'decisions', 'messages',
          'knowledge', 'implementations', 'goal_versions', 'policy_versions', 'research_limits',
          'completion_audits', 'proposals')


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


def make_server(path, host='127.0.0.1', port=8765):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = urlsplit(self.path).path
            if route == '/api/state':
                try:
                    payload = json.dumps(snapshot(path)).encode()
                    self.respond(200, payload, 'application/json')
                except (sqlite3.Error, ValueError):
                    self.respond(503, b'{"error":"State temporarily unavailable. Retrying automatically."}', 'application/json')
                return
            files = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/styles.css': ('styles.css', 'text/css')}
            if route.startswith('/docs/'):
                name = route.removeprefix('/docs/') or 'index.html'
                types = {'.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.json': 'application/json'}
                docs = {p.name: p for p in (ASSETS / 'docs').rglob('*') if p.is_file() and p.suffix in types}
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


def serve(path, host='127.0.0.1', port=8765):
    server = make_server(path, host, port)
    print(f'Omnitrix dashboard listening on {host}:{server.server_port} (read-only)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
