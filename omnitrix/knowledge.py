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
CREATE TABLE IF NOT EXISTS research_preflights (
 job_id TEXT PRIMARY KEY REFERENCES research_jobs(id), proof TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS research_scope_grants (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES research_runs(id),
 sequence INTEGER NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(run_id,sequence));
CREATE TABLE IF NOT EXISTS research_progress (
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES research_runs(id),
 kind TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL);
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
    def __init__(self, store, run_id, workspace, scope, max_calls, *, require_checkpoint=False,
                 obligations=None, allow_scope_expansion=False, dependency_roots=None,
                 max_sources=8, deadline=None):
        self.store, self.run_id, self.workspace = store, run_id, workspace
        self.scope = {str(source_path(workspace, s).relative_to(Path(workspace).resolve())) for s in scope}
        self.remaining = max_calls
        self.knowledge = Knowledge(store)
        self.require_checkpoint, self.checkpoint_recorded = require_checkpoint, False
        self.obligations = [dict(item) for item in (obligations or [])]
        self.allow_scope_expansion = allow_scope_expansion
        self.dependency_roots, self.max_sources, self.deadline = dependency_roots or [], max_sources, deadline
        for grant in self.grants():
            self.scope.update(item['locator'] for item in grant['requests'])
            self.obligations.extend(grant['obligations'])

    def charge(self, operation, query):
        if self.deadline is not None and time.time() >= self.deadline:
            raise RuntimeError('Research time budget exhausted')
        if self.remaining <= 0:
            raise RuntimeError('Research tool budget exhausted')
        if self.require_checkpoint and not self.checkpoint_recorded and self.remaining == 1 and operation != 'checkpoint':
            raise RuntimeError('Last tool call is reserved for required checkpoint; checkpoint captured evidence, then report')
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
        revision = hashlib.sha256(data).hexdigest()
        if any(request['revision'] != revision for grant in self.grants()
               for request in grant['requests'] if request['locator'] == canonical):
            raise ValueError('Granted dependency source revision changed; replan from current source')
        lines = data.decode('utf-8').splitlines()
        excerpt = '\n'.join(f'{i+1}: {line}' for i, line in enumerate(lines)
                            if start_line <= i+1 < start_line+line_count)[:12000]
        prior = self.store.db.execute('''SELECT s.id FROM research_sources s JOIN research_access a ON s.id=a.source_id
            WHERE a.run_id=? AND s.locator=? AND s.revision=? AND s.excerpt=?''',
            (self.run_id, canonical, revision, excerpt)).fetchone()
        if prior:
            return self.knowledge.source(prior[0]) | {'next_action': self.progress_hint()}
        source_id = uuid.uuid4().hex
        job = self.store.db.execute('SELECT job_id FROM research_runs WHERE id=?',(self.run_id,)).fetchone()
        if job:
            self.store.execute("UPDATE task_runtime SET last_progress=?,progress_count=progress_count+1 WHERE state='running' AND json_extract(spec,'$.job_id')=?",(time.time(),job[0]))
        with self.store.db:
            self.store.db.execute('INSERT INTO research_sources VALUES(?,?,?,?,?,?,?)',
                (source_id, self.workspace, canonical, hashlib.sha256(data).hexdigest(),
                 excerpt, time.time(), path.stat().st_mtime))
            self.store.db.execute('INSERT INTO research_access VALUES(?,?)', (self.run_id, source_id))
        return self.knowledge.source(source_id) | {'next_action': self.progress_hint()}

    def progress_hint(self):
        if self.require_checkpoint and not self.checkpoint_recorded and self.remaining <= 1:
            return 'record_checkpoint_now; final tool call is reserved, then report remaining gaps'
        if self.obligations:
            return 'checkpoint newly completed obligations; unrelated reads do not renew semantic progress'
        return 'capture contrary evidence and record uncertainty before reporting'

    def locate(self, locator, query, offset=0):
        """Locate literal text within one scoped file; capture evidence using read next."""
        self.charge('locate_source', json.dumps([locator, query, offset]))
        if not query.strip() or len(query) > 200 or offset < 0:
            raise ValueError('Use a nonempty query up to 200 characters and nonnegative offset')
        canonical = str(source_path(self.workspace, locator).relative_to(Path(self.workspace).resolve()))
        if canonical not in self.scope:
            raise ValueError('Source is outside the explicit research scope')
        _, data = read_source_bytes(self.workspace, canonical)
        matches = [{'line': i, 'text': line[:400]} for i, line in enumerate(data.decode('utf-8').splitlines(), 1)
                   if query.casefold() in line.casefold()]
        page = matches[offset:offset+20]
        return {'locator': canonical, 'matches': page, 'total': len(matches),
                'next_offset': offset+len(page) if offset+len(page) < len(matches) else None,
                'evidence': 'Locations only; read_source captures citable excerpts'}

    def checkpoint(self, evidence_ids, uncertainty, next_step):
        self.charge('checkpoint', json.dumps([evidence_ids, uncertainty, next_step]))
        available = {e['id']: e for e in self.evidence()}
        if not evidence_ids or any(e not in available or available[e]['stale'] for e in evidence_ids):
            raise ValueError('Checkpoint requires current captured evidence')
        if not uncertainty.strip() or not next_step.strip() or len(uncertainty) > 2000 or len(next_step) > 2000:
            raise ValueError('Checkpoint uncertainty and next step require 1..2000 characters')
        job = self.store.db.execute('SELECT j.goal_id,j.spec FROM research_jobs j JOIN research_runs r ON r.job_id=j.id WHERE r.id=?', (self.run_id,)).fetchone()
        from .control import Control
        detail = {'run_id': self.run_id, 'evidence_ids': evidence_ids, 'uncertainty': uncertainty,
                  'next_step': next_step, 'decision': json.loads(job['spec']).get('decision','')}
        progress = self.obligation_progress(evidence_ids)
        previous = set()
        for row in self.store.db.execute("SELECT detail FROM research_progress WHERE run_id=? AND kind='checkpoint'", (self.run_id,)):
            previous.update(json.loads(row[0]).get('captured_keys', []))
        progress['newly_captured_keys'] = sorted(set(progress['captured_keys']) - previous)
        progress['semantic_progress'] = bool(progress['newly_captured_keys'])
        detail['obligation_progress'] = progress
        self.store.execute('INSERT INTO research_progress(run_id,kind,detail,created) VALUES(?,?,?,?)',
                           (self.run_id, 'checkpoint', json.dumps(progress), time.time()))
        self.checkpoint_recorded = True
        Control(self.store).event(job['goal_id'], 'research_checkpoint', detail)
        return detail

    def grants(self):
        return [json.loads(row[0]) for row in self.store.db.execute(
            'SELECT detail FROM research_scope_grants WHERE run_id=? ORDER BY sequence', (self.run_id,))]

    def obligation_progress(self, evidence_ids=None):
        from .planning import evidence_lines, source_info
        evidence = [item for item in self.evidence() if not item['stale'] and
                    (evidence_ids is None or item['id'] in evidence_ids)]
        captured, unresolved = [], []
        for index, obligation in enumerate(self.obligations):
            if not obligation.get('required', True):
                continue
            key = f"{index}:{obligation['kind']}:{obligation['locator']}:{obligation['start_line']}:{obligation.get('end_line')}"
            observed = set()
            try:
                _, lines, _ = source_info(self.workspace, obligation['locator'])
                end = obligation.get('end_line') or len(lines)
                for item in evidence:
                    if item['locator'] == obligation['locator']:
                        observed.update(evidence_lines(item, lines))
                complete = set(range(obligation['start_line'], end + 1)).issubset(observed)
            except (OSError, ValueError):
                complete = False
            if complete:
                captured.append(key)
            else:
                unresolved.append({'key': key, **obligation})
        return {'captured_required': len(captured), 'total_required': len(captured) + len(unresolved),
                'captured_keys': captured, 'unresolved_obligations': unresolved,
                'required_obligations_complete': not unresolved}

    def request_dependency(self, callsite_evidence_ids, reason, requests):
        """Grant a bounded prerequisite without resetting any attempt or resource limit."""
        from .planning import safe_locator, source_info, read_windows, evidence_lines
        from .research_models import DependencySource
        self.charge('request_dependency', json.dumps([callsite_evidence_ids, reason, requests]))
        if not self.allow_scope_expansion:
            raise ValueError('Scope expansion was not authorized by this plan')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1500:
            raise ValueError('Dependency reason requires 1..1500 characters')
        available = {item['id']: item for item in self.evidence()}
        if not 1 <= len(callsite_evidence_ids) <= 10 or any(source not in available or available[source]['stale'] or available[source]['locator'] not in self.scope for source in callsite_evidence_ids):
            raise ValueError('Dependency request requires current captured callsite evidence')
        if not 1 <= len(requests) <= 2 or len(self.grants()) >= 2:
            raise ValueError('At most two dependency grants of at most two files are allowed per run')
        normalized, obligations, minimum = [], [], 0
        roots = [safe_locator(self.workspace, root, directory=True) for root in self.dependency_roots]
        for request in requests:
            request = DependencySource.model_validate(request).model_dump()
            locator, lines, revision = source_info(self.workspace, request['locator'])
            if not any(root == '.' or locator.startswith(root + '/') for root in roots):
                raise ValueError('Dependency must be inside an approved dependency root')
            if not 1 <= request['start_line'] <= request['end_line'] <= len(lines):
                raise ValueError('Dependency line range exceeds current source')
            if locator in self.scope or any(item['locator'] == locator for item in normalized):
                raise ValueError('Dependency grant must add distinct new files')
            needed = set(range(request['start_line'], request['end_line'] + 1))
            for item in available.values():
                if not item['stale'] and item['locator'] == locator:
                    needed -= evidence_lines(item, lines)
            minimum += read_windows(needed, lines)
            normalized.append({**request, 'locator': locator, 'revision': revision})
            obligations.append({'kind': 'callee', 'locator': locator, 'start_line': request['start_line'],
                'end_line': request['end_line'], 'status': 'required', 'required': True, 'evidence_ids': []})
        if len(self.scope | {item['locator'] for item in normalized}) > self.max_sources:
            raise ValueError('Dependency grant exceeds the total explicit source limit')
        reserve = int(self.require_checkpoint and not self.checkpoint_recorded)
        pending = {}
        for obligation in self.obligation_progress()['unresolved_obligations']:
            locator, lines, _ = source_info(self.workspace, obligation['locator'])
            needed = pending.setdefault(locator, set())
            needed.update(range(obligation['start_line'], (obligation.get('end_line') or len(lines)) + 1))
            for item in available.values():
                if not item['stale'] and item['locator'] == locator:
                    needed.difference_update(evidence_lines(item, lines))
        minimum += sum(read_windows(positions, source_info(self.workspace, locator)[1]) for locator, positions in pending.items())
        if minimum + reserve > self.remaining:
            raise ValueError('Dependency cannot be captured within remaining reads and reserved checkpoint budget')
        grant = {'id': uuid.uuid4().hex, 'sequence': len(self.grants()) + 1,
                 'callsite_evidence_ids': callsite_evidence_ids, 'reason': reason,
                 'requests': normalized, 'obligations': obligations, 'remaining_tool_calls': self.remaining}
        self.store.execute('INSERT INTO research_scope_grants VALUES(?,?,?,?,?)',
            (grant['id'], self.run_id, grant['sequence'], json.dumps(grant), time.time()))
        self.scope.update(item['locator'] for item in normalized)
        self.obligations.extend(obligations)
        return {**grant, 'effective_scope': sorted(self.scope), 'deadline_unchanged': self.deadline}

    def prefetch(self):
        # Required context preparation is not one of the agent's discretionary tool calls.
        self.store.execute('INSERT INTO research_queries(run_id,operation,query,created) VALUES(?,?,?,?)',
            (self.run_id,'prefetch_knowledge','',time.time()))
        return self._search('',0)

    def search(self, query='', offset=0):
        self.charge('search_knowledge', query)
        return self._search(query,offset)

    def _search(self,query,offset):
        records = self.knowledge.search(self.workspace, query, limit=5, offset=offset)
        for record in records:
            for source in record['sources']:
                self.store.execute('INSERT OR IGNORE INTO research_access VALUES(?,?)',
                                   (self.run_id, source['id']))
        return records

    def evidence(self):
        return [self.knowledge.source(r[0]) for r in self.store.db.execute(
            'SELECT source_id FROM research_access WHERE run_id=?', (self.run_id,))]
