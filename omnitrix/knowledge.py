"""Shared, revision-aware knowledge and the bounded source evidence ledger."""
import hashlib
import csv
import json
import math
import time
import uuid
from pathlib import Path
from .resources import EXCLUDED, resource_kind

MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 16384

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
    if EXCLUDED.intersection(path.relative_to(root).parts):
        raise ValueError('Runtime state and dependency directories are outside research scope')
    return path


def source_revision(workspace, locator):
    """Hash in chunks: large exports need not be copied into agent context or memory."""
    path = source_path(workspace, locator)
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise ValueError('Source exceeds 32 MiB; provide a partitioned export instead')
            digest.update(chunk)
    return path, digest.hexdigest()


def source_lines(path):
    """Stream UTF-8 lines; refuse pathological rows rather than truncate them silently."""
    with path.open('rb') as stream:
        while line := stream.readline(MAX_LINE_BYTES + 1):
            if len(line) > MAX_LINE_BYTES:
                raise ValueError('Source line exceeds 16 KiB; provide a bounded/normalized export')
            yield line.decode('utf-8')


class Knowledge:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def source(self, source_id):
        row = self.db.execute('SELECT * FROM research_sources WHERE id=?', (source_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown evidence ID')
        result = dict(row)
        try:
            _, revision = source_revision(result['workspace'], result['locator'])
            result['stale'] = revision != result['revision']
        except (OSError, ValueError):
            result['stale'] = True
        # Captured observation age is explicit; not a claim about metric measurement age.
        result['age_seconds'] = max(0, time.time() - result['observed'])
        result['stale'] = result['stale'] or (resource_kind(result['locator']) in ('metrics','logs') and result['age_seconds'] > 86400)
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

    def backlog(self, goal_id, limit=None):
        return [dict(row) | {'spec': json.loads(row['spec'])} for row in self.db.execute(
            'SELECT * FROM opportunities WHERE goal_id=? ORDER BY score DESC,created,id LIMIT ?', (goal_id, -1 if limit is None else limit))]

    def progress(self, goal_id, limit=None):
        """Coverage of planned resources, not a claim that the whole system was scanned."""
        jobs = self.db.execute('SELECT stage,status,spec FROM research_jobs WHERE goal_id=? ORDER BY created DESC LIMIT ?', (goal_id, -1 if limit is None else limit)).fetchall()
        planned = {s for j in jobs for s in json.loads(j['spec'])['scope']}
        rows = self.db.execute('''SELECT DISTINCT s.locator FROM research_sources s
            JOIN research_access a ON a.source_id=s.id JOIN research_runs r ON r.id=a.run_id
            JOIN research_jobs j ON j.id=r.job_id WHERE j.goal_id=? ORDER BY s.locator LIMIT ?''', (goal_id, -1 if limit is None else limit)).fetchall()
        observed = {r['locator'] for r in rows}
        by_kind = {kind: sorted(p for p in observed if resource_kind(p) == kind)
                   for kind in ('code', 'metrics', 'logs', 'document')}
        backlog = self.backlog(goal_id, limit=limit)
        self.db.create_function('source_kind', 1, resource_kind, deterministic=True)
        kind_counts = {r['kind']: r['n'] for r in self.db.execute('''SELECT source_kind(locator) kind,count(*) n FROM (
            SELECT DISTINCT s.locator FROM research_sources s JOIN research_access a ON a.source_id=s.id
            JOIN research_runs r ON r.id=a.run_id JOIN research_jobs j ON j.id=r.job_id WHERE j.goal_id=?) GROUP BY kind''', (goal_id,))}
        candidate_count = self.db.execute('SELECT count(*) FROM opportunities WHERE goal_id=?', (goal_id,)).fetchone()[0]
        advice = []
        if not self.db.execute("SELECT 1 FROM research_jobs WHERE goal_id=? AND stage='discovery' AND status='discovered' LIMIT 1",(goal_id,)).fetchone():
            advice.append('Explore: no completed system discovery yet.')
        if planned - observed:
            advice.append('Explore: some explicitly planned resources remain unobserved.')
        if not (kind_counts.get('metrics') or kind_counts.get('logs')):
            advice.append('Seek runtime measurements or record their absence; code alone does not establish production impact.')
        if candidate_count < 2:
            advice.append('For a broad goal, compare another independent opportunity before committing to the first clue.')
        if any(o['status'] == 'open' for o in backlog):
            advice.append('Exploit: test a high-value open hypothesis against its falsification condition.')
        if any(j['stage'] == 'research' and j['status'] == 'falsified' for j in jobs):
            advice.append('Reprioritize after falsification; do not relaunch the same explanation under a new title.')
        return {'coverage_basis': 'Recent planned sources and a bounded observed-path sample; excerpts are partial and freshness must be checked.' if limit else 'Explicitly planned sources only; excerpts are partial and freshness must be checked.',
                'sample_limit': limit,
                'observed_sources': sorted(observed), 'unobserved_planned_sources': sorted(planned - observed),
                'observed_by_kind': by_kind, 'observed_kind_counts': kind_counts, 'candidate_count': candidate_count,
                'unresolved_candidates': self.db.execute("SELECT count(*) FROM opportunities WHERE goal_id=? AND status IN ('open','insufficient','blocked')",(goal_id,)).fetchone()[0],
                'advice': advice}

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
        if start_line < 1 or not 1 <= line_count <= 100:
            raise ValueError('Read between 1 and 100 lines starting at line 1 or later')
        canonical, path, revision = self._source(locator)
        selected = []
        size = 0
        next_line = None
        truncated = False
        for number, line in enumerate(source_lines(path), 1):
            if number < start_line:
                continue
            if number >= start_line + line_count:
                next_line = number
                break
            text = f'{number}: {line.rstrip(chr(10) + chr(13))}'
            if len(text) > 8000 or size + len(text) + 1 > 12000:
                truncated = True
                next_line = number
                if not selected:
                    selected.append(text[:8000] + ' [line clipped; normalize source for full content]')
                break
            selected.append(text)
            size += len(text) + 1
        return self._capture(canonical, path, revision, '\n'.join(selected)) | {
            'next_line': next_line, 'truncated': truncated, 'start_line': start_line}

    def _source(self, locator):
        canonical = str(source_path(self.workspace, locator).relative_to(Path(self.workspace).resolve()))
        if canonical not in self.scope:
            raise ValueError('Source is outside the explicit research scope')
        path, revision = source_revision(self.workspace, canonical)
        return canonical, path, revision

    def _capture(self, canonical, path, revision, excerpt):
        if source_revision(self.workspace, canonical)[1] != revision:
            raise ValueError('Source changed during observation; retry against a stable export')
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
                (source_id, self.workspace, canonical, revision,
                 excerpt, time.time(), path.stat().st_mtime))
            self.store.db.execute('INSERT INTO research_access VALUES(?,?)', (self.run_id, source_id))
        return self.knowledge.source(source_id)

    def search_source(self, locator, query, start_line=1):
        """Literal, case-insensitive search of at most 5000 lines/512 KiB per page."""
        self.charge('search_source', json.dumps([locator, query, start_line]))
        if not query.strip() or len(query) > 500 or start_line < 1:
            raise ValueError('Use a nonempty query <= 500 characters and start_line >= 1')
        canonical, path, revision = self._source(locator)
        matches = []
        scanned = size = output_size = 0
        next_line = None
        for number, line in enumerate(source_lines(path), 1):
            if number < start_line:
                continue
            if scanned >= 5000 or size + len(line.encode('utf-8')) > 524288:
                next_line = number
                break
            if query.casefold() in line.casefold():
                matched = line.rstrip('\r\n')
                if len(matched) > 600:
                    position = matched.casefold().find(query.casefold())
                    begin = max(0, position - 40)
                    matched = '[match context clipped] ' + matched[begin:begin + 600]
                text = f'{number}: {matched}'
                if output_size + len(text) > 10000 or len(matches) >= 20:
                    next_line = number
                    break
                matches.append(text)
                output_size += len(text)
            scanned += 1
            size += len(line.encode('utf-8'))
        summary = {'operation': 'literal_search', 'query': query, 'start_line': start_line,
                   'scanned_lines': scanned, 'next_line': next_line, 'partial': next_line is not None,
                   'matches': matches}
        return self._capture(canonical, path, revision, json.dumps(summary)) | summary

    def summarize_csv(self, locator, value_column, group_by=None, start_row=1, complete=False):
        """Aggregate up to 5000 CSV data rows/512 KiB, keeping totals separate by group."""
        if complete:
            pages = []
            cursor = start_row
            combined = {}
            scanned = invalid = 0
            baseline_revision = None
            while cursor is not None and self.remaining:
                observation = self.summarize_csv(locator, value_column, group_by, cursor)
                if baseline_revision is not None and observation['revision'] != baseline_revision:
                    raise ValueError('Source changed between CSV pages; retry against a stable export')
                baseline_revision = observation['revision']
                page = observation['summary']
                if len(set(combined) | set(page['groups'])) > 20:
                    break  # Explicitly partial; caller can resume at this page's first row.
                pages.append(page)
                scanned += page['scanned_rows']
                invalid += page['invalid_values']
                for name, values in page['groups'].items():
                    aggregate = combined.setdefault(name, {'count': 0, 'sum': 0.0, 'min': values['min'], 'max': values['max']})
                    aggregate['count'] += values['count']
                    aggregate['sum'] += values['sum']
                    if not math.isfinite(aggregate['sum']):
                        raise ValueError('CSV sum overflowed; normalize measurement units')
                    aggregate['min'] = min(aggregate['min'], values['min'])
                    aggregate['max'] = max(aggregate['max'], values['max'])
                cursor = page['next_row']
            if not pages:
                raise RuntimeError('Research tool budget exhausted')
            for values in combined.values():
                values['mean'] = values['sum'] / values['count']
            canonical, path, revision = self._source(locator)
            if revision != baseline_revision:
                raise ValueError('Source changed during CSV aggregation; retry against a stable export')
            summary = pages[0] | {'scanned_rows': scanned, 'invalid_values': invalid, 'groups': combined,
                                  'next_row': cursor, 'partial': cursor is not None, 'pages_aggregated': len(pages)}
            return self._capture(canonical, path, revision, json.dumps(summary)) | {'summary': summary}
        self.charge('summarize_csv', json.dumps([locator, value_column, group_by, start_row]))
        if start_row < 1:
            raise ValueError('start_row must be >= 1; row 1 is the first data row after the header')
        canonical, path, revision = self._source(locator)
        reader = csv.DictReader(source_lines(path), strict=True)
        if value_column not in (reader.fieldnames or []) or (group_by and group_by not in reader.fieldnames):
            raise ValueError('Requested columns absent; read the CSV header first')
        groups = {}
        scanned = invalid = size = 0
        next_row = None
        for number, row in enumerate(reader, 1):
            if number < start_row:
                continue
            key = row.get(group_by) if group_by else 'all'
            key = key or '(missing)'
            if len(key) > 200:
                raise ValueError('CSV group identifiers exceed 200 characters; normalize the export')
            row_size = len(json.dumps(row).encode('utf-8'))
            if scanned >= 5000 or size + row_size > 524288 or (key not in groups and len(groups) >= 20):
                next_row = number
                break
            scanned += 1
            size += row_size
            try:
                value = float(row.get(value_column))
                if not math.isfinite(value):
                    raise ValueError('Nonfinite measurement')
            except (ValueError, TypeError):
                invalid += 1
                continue
            group = groups.setdefault(key, {'count': 0, 'sum': 0.0, 'min': value, 'max': value})
            group['count'] += 1
            group['sum'] += value
            if not math.isfinite(group['sum']):
                raise ValueError('CSV sum overflowed; normalize measurement units before aggregating')
            group['min'] = min(group['min'], value)
            group['max'] = max(group['max'], value)
        for group in groups.values():
            group['mean'] = group['sum'] / group['count']
        summary = {'operation': 'csv_summary', 'value_column': value_column, 'group_by': group_by,
                   'start_row': start_row, 'scanned_rows': scanned, 'invalid_values': invalid,
                   'groups': groups, 'next_row': next_row, 'partial': next_row is not None,
                   'measurement_window': 'Not inferred; inspect timestamp columns and source context'}
        return self._capture(canonical, path, revision, json.dumps(summary)) | {'summary': summary}

    def prefetch(self):
        # Required context preparation is not one of the agent's discretionary tool calls.
        self.store.execute('INSERT INTO research_queries(run_id,operation,query,created) VALUES(?,?,?,?)',
            (self.run_id,'prefetch_knowledge','',time.time()))
        # Scope relevance outranks recency: a fresh unrelated report must not
        # evict the earlier falsification of this exact component's hypothesis.
        placeholders = ','.join('?' for _ in self.scope)
        rows = self.store.db.execute(f'''SELECT k.* FROM knowledge k JOIN research_jobs j ON j.id=k.job_id
            WHERE k.workspace=? ORDER BY EXISTS(SELECT 1 FROM json_each(j.spec,'$.scope')
            WHERE value IN ({placeholders})) DESC,k.created DESC LIMIT 5''',
            (str(Path(self.workspace).resolve()), *sorted(self.scope))).fetchall()
        records = []
        for row in rows:
            item = dict(row)
            item['body'] = json.loads(item['body'])
            item['citations'] = json.loads(item['citations'])
            item['sources'] = [self.knowledge.source(s) for s in item['citations']]
            item['stale'] = any(s['stale'] for s in item['sources'])
            records.append(item)
        return self._remember(records)

    def search(self, query='', offset=0):
        self.charge('search_knowledge', query)
        return self._search(query,offset)

    def _search(self,query,offset):
        records = self.knowledge.search(self.workspace, query, limit=5, offset=offset)
        return self._remember(records)

    def _remember(self, records):
        for record in records:
            for source in record['sources']:
                self.store.execute('INSERT OR IGNORE INTO research_access VALUES(?,?)',
                                   (self.run_id, source['id']))
        return records

    def evidence(self):
        return [self.knowledge.source(r[0]) for r in self.store.db.execute(
            'SELECT source_id FROM research_access WHERE run_id=?', (self.run_id,))]
