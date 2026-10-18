"""Shared, revision-aware knowledge and the bounded source evidence ledger."""
import hashlib
import json
import time
import uuid
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS research_limits (
 goal_id TEXT PRIMARY KEY REFERENCES goals(id), max_runs INTEGER NOT NULL,
 spent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS research_jobs (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), stage TEXT NOT NULL,
 signature TEXT NOT NULL, spec TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS research_signature ON research_jobs(signature);
CREATE TABLE IF NOT EXISTS research_runs (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES research_jobs(id),
 status TEXT NOT NULL, report TEXT, evaluation TEXT, feedback TEXT,
 tool_calls INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, finished REAL);
CREATE TABLE IF NOT EXISTS research_sources (
 id TEXT PRIMARY KEY, workspace TEXT NOT NULL, locator TEXT NOT NULL,
 revision TEXT NOT NULL, excerpt TEXT NOT NULL, observed REAL NOT NULL,
 source_modified REAL NOT NULL);
CREATE TABLE IF NOT EXISTS research_access (
 run_id TEXT NOT NULL REFERENCES research_runs(id), source_id TEXT NOT NULL REFERENCES research_sources(id),
 PRIMARY KEY(run_id,source_id));
CREATE TABLE IF NOT EXISTS research_queries (
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES research_runs(id),
 operation TEXT NOT NULL, query TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge (
 id TEXT PRIMARY KEY, workspace TEXT NOT NULL, goal_id TEXT NOT NULL REFERENCES goals(id),
 job_id TEXT NOT NULL REFERENCES research_jobs(id), kind TEXT NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, classification TEXT NOT NULL,
 citations TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS opportunities (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id),
 job_id TEXT NOT NULL REFERENCES research_jobs(id), title TEXT NOT NULL,
 spec TEXT NOT NULL, score REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open',
 created REAL NOT NULL, updated REAL NOT NULL);
'''


def source_path(workspace, locator):
    root = Path(workspace).resolve()
    path = (root / locator).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Source must be an existing file inside the workspace')
    if any(part in {'.git', '.omnitrix', '.venv', 'node_modules'} for part in path.relative_to(root).parts):
        raise ValueError('Runtime state and dependency directories are outside research scope')
    return path


def read_source_bytes(workspace, locator):
    path = source_path(workspace, locator)
    with path.open('rb') as stream:
        data = stream.read(131073)
    if len(data) > 131072:
        raise ValueError('Source exceeds 128 KiB; provide a bounded export instead')
    return path, data


class Knowledge:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def source(self, source_id):
        row = self.db.execute('SELECT * FROM research_sources WHERE id=?', (source_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown evidence ID')
        result = dict(row)
        try:
            _, data = read_source_bytes(result['workspace'], result['locator'])
            result['stale'] = hashlib.sha256(data).hexdigest() != result['revision']
        except (OSError, ValueError):
            result['stale'] = True
        # Captured observation age is explicit; not a claim about metric measurement age.
        result['age_seconds'] = max(0, time.time() - result['observed'])
        result['stale'] = result['stale'] or result['age_seconds'] > 86400
        return result

    def put(self, goal_id, job_id, kind, title, body, classification, citations):
        workspace = self.store.snapshot(goal_id)['goal']['workspace']
        record_id = uuid.uuid4().hex
        self.store.execute('INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,?)',
            (record_id, workspace, goal_id, job_id, kind, title, json.dumps(body),
             classification, json.dumps(citations), time.time()))
        return record_id

    def search(self, workspace, query='', limit=10, offset=0):
        """Literal substring search, scoped to a workspace, with bounded pagination."""
        escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        rows = self.db.execute('''SELECT * FROM knowledge WHERE workspace=? AND
            (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')
            ORDER BY created DESC LIMIT ? OFFSET ?''',
            (str(Path(workspace).resolve()), f'%{escaped}%', f'%{escaped}%',
             min(max(limit, 1), 20), max(offset, 0))).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item['body'] = json.loads(item['body'])
            item['citations'] = json.loads(item['citations'])
            item['sources'] = [self.source(s) for s in item['citations']]
            item['stale'] = any(s['stale'] for s in item['sources'])
            results.append(item)
        return results

    def backlog(self, goal_id):
        return [dict(row) | {'spec': json.loads(row['spec'])} for row in self.db.execute(
            'SELECT * FROM opportunities WHERE goal_id=? ORDER BY score DESC,created,id', (goal_id,))]

    def jobs(self, goal_id):
        result = []
        for row in self.db.execute('SELECT * FROM research_jobs WHERE goal_id=? ORDER BY created', (goal_id,)):
            job = dict(row)
            job['spec'] = json.loads(job['spec'])
            job['runs'] = [dict(r) for r in self.db.execute(
                'SELECT * FROM research_runs WHERE job_id=? ORDER BY created', (job['id'],))]
            result.append(job)
        return result

    def export(self, goal_id):
        workspace = self.store.snapshot(goal_id)['goal']['workspace']
        # Export all records, rather than silently truncating to a search page.
        ids = [r[0] for r in self.db.execute('SELECT id FROM knowledge WHERE goal_id=?', (goal_id,))]
        records = []
        for record_id in ids:
            row = dict(self.db.execute('SELECT * FROM knowledge WHERE id=?', (record_id,)).fetchone())
            row['body'] = json.loads(row['body'])
            row['citations'] = json.loads(row['citations'])
            row['sources'] = [self.source(s) for s in row['citations']]
            row['stale'] = any(s['stale'] for s in row['sources'])
            records.append(row)
        return {'workspace': workspace, 'records': records, 'opportunities': self.backlog(goal_id),
                'jobs': self.jobs(goal_id), 'queries': [dict(r) for r in self.db.execute('SELECT q.* FROM research_queries q JOIN research_runs r ON q.run_id=r.id JOIN research_jobs j ON r.job_id=j.id WHERE j.goal_id=? ORDER BY q.id',(goal_id,))]}


class ResearchTools:
    """No shell or unbounded repository scan. Every observation has provenance."""
    def __init__(self, store, run_id, workspace, scope, max_calls):
        self.store, self.run_id, self.workspace = store, run_id, workspace
        self.scope = {str(source_path(workspace, s).relative_to(Path(workspace).resolve())) for s in scope}
        self.remaining = max_calls
        self.knowledge = Knowledge(store)

    def charge(self, operation, query):
        if self.remaining <= 0:
            raise RuntimeError('Research tool budget exhausted')
        self.remaining -= 1
        with self.store.db:
            self.store.db.execute('UPDATE research_runs SET tool_calls=tool_calls+1 WHERE id=?', (self.run_id,))
            self.store.db.execute('INSERT INTO research_queries(run_id,operation,query,created) VALUES(?,?,?,?)',
                                 (self.run_id, operation, query, time.time()))

    def read(self, locator, start_line=1, line_count=80):
        self.charge('read_source', json.dumps([locator, start_line, line_count]))
        canonical = str(source_path(self.workspace, locator).relative_to(Path(self.workspace).resolve()))
        if canonical not in self.scope:
            raise ValueError('Source is outside the explicit research scope')
        if start_line < 1 or not 1 <= line_count <= 100:
            raise ValueError('Read between 1 and 100 lines starting at line 1 or later')
        path, data = read_source_bytes(self.workspace, canonical)
        lines = data.decode('utf-8').splitlines()
        excerpt = '\n'.join(f'{i+1}: {line}' for i, line in enumerate(lines)
                            if start_line <= i+1 < start_line+line_count)[:12000]
        revision = hashlib.sha256(data).hexdigest()
        prior = self.store.db.execute('''SELECT s.id FROM research_sources s JOIN research_access a ON s.id=a.source_id
            WHERE a.run_id=? AND s.locator=? AND s.revision=? AND s.excerpt=?''',
            (self.run_id, canonical, revision, excerpt)).fetchone()
        if prior:
            return self.knowledge.source(prior[0])
        source_id = uuid.uuid4().hex
        job = self.store.db.execute('SELECT job_id FROM research_runs WHERE id=?',(self.run_id,)).fetchone()
        if job:
            self.store.execute("UPDATE task_runtime SET last_progress=?,progress_count=progress_count+1 WHERE state='running' AND json_extract(spec,'$.job_id')=?",(time.time(),job[0]))
        with self.store.db:
            self.store.db.execute('INSERT INTO research_sources VALUES(?,?,?,?,?,?,?)',
                (source_id, self.workspace, canonical, hashlib.sha256(data).hexdigest(),
                 excerpt, time.time(), path.stat().st_mtime))
            self.store.db.execute('INSERT INTO research_access VALUES(?,?)', (self.run_id, source_id))
        return self.knowledge.source(source_id)

    def search(self, query='', offset=0):
        self.charge('search_knowledge', query)
        records = self.knowledge.search(self.workspace, query, limit=5, offset=offset)
        for record in records:
            for source in record['sources']:
                self.store.execute('INSERT OR IGNORE INTO research_access VALUES(?,?)',
                                   (self.run_id, source['id']))
        return records

    def evidence(self):
        return [self.knowledge.source(r[0]) for r in self.store.db.execute(
            'SELECT source_id FROM research_access WHERE run_id=?', (self.run_id,))]
