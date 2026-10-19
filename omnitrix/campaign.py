"""Durable scheduling of manager-approved research plans, without invented work.

A queue claim is committed before a worker starts. Interrupted claims are never
returned to ready automatically; normal supervisor reconciliation owns recovery.
"""
from contextlib import contextmanager
import hashlib
import json
import time
import uuid

from .knowledge import read_source_bytes


AGE_SECONDS = 300
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS campaign_queue (
      id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id),
      job_id TEXT NOT NULL UNIQUE REFERENCES research_jobs(id), primary_area TEXT NOT NULL,
      priority INTEGER NOT NULL, instructions TEXT NOT NULL, stage TEXT NOT NULL,
      spec TEXT NOT NULL, revisions TEXT NOT NULL, source_hashes TEXT NOT NULL,
      state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
      claimed REAL, claim_token TEXT, outcome TEXT, reason TEXT)''',
    '''CREATE INDEX IF NOT EXISTS campaign_ready ON campaign_queue(goal_id,state,priority,created)''',
    '''CREATE TABLE IF NOT EXISTS campaign_events (
      id INTEGER PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id),
      queue_id TEXT NOT NULL REFERENCES campaign_queue(id), kind TEXT NOT NULL,
      detail TEXT NOT NULL, created REAL NOT NULL)''',
)
TERMINAL_RESULTS = {'supported', 'falsified', 'discovered', 'insufficient', 'blocked',
                    'escalated', 'failed', 'interrupted', 'uncertain', 'stale', 'invalidated'}


def revision_fence(store, goal_id):
    """Read version identifiers directly; safe inside a Store.snapshot call."""
    goal = store.db.execute('SELECT workspace FROM goals WHERE id=?', (goal_id,)).fetchone()
    if not goal:
        raise ValueError('Unknown campaign goal')
    def latest(table, column):
        row = store.db.execute(f'SELECT max({column}) FROM {table} WHERE goal_id=?', (goal_id,)).fetchone()
        return row[0] or 0
    return {'goal_revision': latest('goal_versions', 'revision'),
            'policy_revision': latest('policy_versions', 'version'),
            'research_context_revision': latest('research_context_versions', 'version'),
            'workspace': goal['workspace']}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


@contextmanager
def transaction(db):
    """Do not commit a caller's transaction while enqueueing a research batch."""
    nested = db.in_transaction
    marker = 'campaign_' + uuid.uuid4().hex
    db.execute('SAVEPOINT ' + marker if nested else 'BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        if nested:
            db.execute('ROLLBACK TO ' + marker)
            db.execute('RELEASE ' + marker)
        else:
            db.rollback()
        raise
    else:
        if nested:
            db.execute('RELEASE ' + marker)
        else:
            db.commit()


class Campaign:
    def __init__(self, store, goal_id):
        self.store, self.db, self.goal_id = store, store.db, goal_id
        if not self.db.execute('SELECT 1 FROM goals WHERE id=?', (goal_id,)).fetchone():
            raise ValueError('Unknown campaign goal')
        for statement in SCHEMA:
            store.execute(statement)

    def _event(self, queue_id, kind, detail):
        self.db.execute('INSERT INTO campaign_events(goal_id,queue_id,kind,detail,created) VALUES(?,?,?,?,?)',
            (self.goal_id, queue_id, kind, canonical(detail), time.time()))

    def _row(self, row):
        result = dict(row)
        for field in ('spec', 'revisions', 'source_hashes', 'outcome'):
            result[field] = json.loads(result[field]) if result[field] else None
        result['queue_id'] = result['id']
        return result

    def _hashes(self, workspace, scope):
        return {locator: hashlib.sha256(read_source_bytes(workspace, locator)[1]).hexdigest()
                for locator in sorted(scope)}

    def enqueue(self, job_id, priority=3, instructions=''):
        if type(priority) is not int or not 1 <= priority <= 5:
            raise ValueError('Campaign priority must be 1..5; 1 is highest')
        if not isinstance(instructions, str) or len(instructions) > 6000:
            raise ValueError('Campaign instructions must be at most 6000 characters')
        with transaction(self.db):
            job = self.db.execute('SELECT * FROM research_jobs WHERE id=? AND goal_id=?', (job_id, self.goal_id)).fetchone()
            if not job:
                raise ValueError('Queued research must already be planned for this goal')
            existing = self.db.execute('SELECT * FROM campaign_queue WHERE job_id=?', (job_id,)).fetchone()
            if existing:
                if existing['priority'] != priority or existing['instructions'] != instructions:
                    raise ValueError('An existing queue assignment is immutable; do not silently replace approved work')
                return self._row(existing)
            if job['stage'] not in ('discovery', 'research') or job['status'] != 'pending' or job['attempts']:
                raise ValueError('Only fresh pending research plans can be queued; attempted work requires explicit reconciliation or targeted retry')
            spec = json.loads(job['spec'])
            area = spec.get('primary_area') or ''
            has_agenda = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_areas'").fetchone()
            if has_agenda:
                registered = self.db.execute('SELECT path FROM audit_areas WHERE goal_id=?', (self.goal_id,)).fetchall()
                if registered and area not in {row[0] for row in registered}:
                    raise ValueError('Queued audit plans require a registered primary_area')
            fence = revision_fence(self.store, self.goal_id)
            hashes = self._hashes(fence['workspace'], spec['scope'])
            now, queue_id = time.time(), uuid.uuid4().hex
            self.db.execute('''INSERT INTO campaign_queue
                (id,goal_id,job_id,primary_area,priority,instructions,stage,spec,revisions,source_hashes,state,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', (queue_id, self.goal_id, job_id, area, priority, instructions,
                job['stage'], canonical(spec), canonical(fence), canonical(hashes), 'ready', now, now))
            self._event(queue_id, 'enqueued', {'job_id': job_id, 'primary_area': area, 'priority': priority})
            return self._row(self.db.execute('SELECT * FROM campaign_queue WHERE id=?', (queue_id,)).fetchone())

    def _invalid_reason(self, row):
        if json.loads(row['revisions']) != revision_fence(self.store, self.goal_id):
            return 'Goal, policy, research context or workspace changed after this assignment was approved'
        job = self.db.execute('SELECT * FROM research_jobs WHERE id=? AND goal_id=?', (row['job_id'], self.goal_id)).fetchone()
        if not job or job['status'] != 'pending' or job['attempts']:
            return 'Research is no longer fresh pending work; inspect prior outcomes rather than replay it'
        if canonical(json.loads(job['spec'])) != row['spec'] or job['stage'] != row['stage']:
            return 'The approved research contract changed after enqueue'
        try:
            actual = self._hashes(json.loads(row['revisions'])['workspace'], json.loads(row['source_hashes']))
        except (ValueError, OSError) as exc:
            return 'Scoped source is no longer readable: ' + str(exc)
        if actual != json.loads(row['source_hashes']):
            return 'Scoped source revision changed after enqueue'
        return None

    def claim_next(self):
        if self.db.in_transaction:
            raise RuntimeError('Claim work only after the manager batch transaction is committed')
        with transaction(self.db):
            ready = self.db.execute("SELECT * FROM campaign_queue WHERE goal_id=? AND state='ready' ORDER BY created,id", (self.goal_id,)).fetchall()
            valid = []
            for row in ready:
                reason = self._invalid_reason(row)
                if reason:
                    self.db.execute("UPDATE campaign_queue SET state='invalidated',reason=?,updated=? WHERE id=? AND state='ready'", (reason, time.time(), row['id']))
                    self._event(row['id'], 'invalidated', {'reason': reason})
                else:
                    valid.append(row)
            if not valid:
                return None
            prior = {row['primary_area']: (row['count'], row['last_claim']) for row in self.db.execute('''
                SELECT primary_area,count(*) AS count,max(claimed) AS last_claim FROM campaign_queue
                WHERE goal_id=? AND claimed IS NOT NULL GROUP BY primary_area''', (self.goal_id,))}
            area_priority = {}
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_area_updates'").fetchone():
                area_priority = {row['area']: row['priority'] for row in self.db.execute('''SELECT area,priority FROM audit_area_updates a
                    WHERE goal_id=? AND revision=(SELECT max(revision) FROM audit_area_updates b WHERE b.goal_id=a.goal_id AND b.area=a.area)''', (self.goal_id,))}
            now = time.time()
            def rank(row):
                base = min(row['priority'], area_priority.get(row['primary_area'], row['priority']))
                aged = max(1, base - int(max(0, now-row['created']) // AGE_SECONDS))
                count, last = prior.get(row['primary_area'], (0, 0))
                return aged, count, last, row['created'], row['id']
            row = min(valid, key=rank)
            token = uuid.uuid4().hex
            changed = self.db.execute("UPDATE campaign_queue SET state='claimed',claimed=?,claim_token=?,updated=? WHERE id=? AND state='ready'", (now, token, now, row['id']))
            if changed.rowcount != 1:
                raise RuntimeError('Campaign assignment was claimed concurrently')
            self._event(row['id'], 'claimed', {'job_id': row['job_id'], 'primary_area': row['primary_area'], 'claim_token': token})
            return self._row(self.db.execute('SELECT * FROM campaign_queue WHERE id=?', (row['id'],)).fetchone())

    def complete_job(self, job_id, outcome):
        if not isinstance(outcome, dict) or outcome.get('status') not in TERMINAL_RESULTS:
            raise ValueError('A campaign completion requires an explicit terminal research outcome')
        if outcome.get('job_id', job_id) != job_id:
            raise ValueError('Campaign outcome belongs to a different research job')
        body = canonical(outcome)
        with transaction(self.db):
            row = self.db.execute('SELECT * FROM campaign_queue WHERE goal_id=? AND job_id=?', (self.goal_id, job_id)).fetchone()
            if not row:
                raise ValueError('Research job is not queued in this campaign')
            if row['state'] != 'claimed':
                if row['outcome'] == body:
                    return self._row(row)
                raise ValueError('Only the original claimed assignment can receive an outcome; queue history is immutable')
            status = outcome['status']
            state = status if status in ('interrupted','uncertain','invalidated','stale','failed') else 'completed'
            self.db.execute('UPDATE campaign_queue SET state=?,outcome=?,updated=? WHERE id=? AND state=?',
                (state, body, time.time(), row['id'], 'claimed'))
            self._event(row['id'], 'finished', {'job_id': job_id, 'state': state, 'research_status': status})
            return self._row(self.db.execute('SELECT * FROM campaign_queue WHERE id=?', (row['id'],)).fetchone())

    def completion_status(self):
        """Explicit potentially expensive revalidation; not a snapshot side effect."""
        from .audit import Audit
        audit = Audit(self.store, self.goal_id)
        coverage = audit.summary()
        gaps = audit.completion_gaps()
        return {'top_level_areas': coverage['top_level_areas'],
                'currently_eligible_top_level': coverage['top_level_areas'] - len(gaps),
                'gaps': gaps, 'complete_coverage_floor_met': bool(coverage['top_level_areas']) and not gaps,
                'whole_audit_complete': False,
                'note': 'A current reviewed sample is a minimum coverage floor, not exhaustive semantic audit completion.'}

    def summary(self):
        counts = {row['state']: row['count'] for row in self.db.execute('SELECT state,count(*) AS count FROM campaign_queue WHERE goal_id=? GROUP BY state', (self.goal_id,))}
        for state in ('ready', 'claimed', 'completed', 'failed', 'invalidated', 'interrupted', 'uncertain', 'stale'):
            counts.setdefault(state, 0)
        goal_status = self.db.execute('SELECT status FROM goals WHERE id=?', (self.goal_id,)).fetchone()[0]
        coverage = None
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_areas'").fetchone():
            from .audit import Audit
            coverage = Audit(self.store, self.goal_id).summary()
        run_status = ('paused' if goal_status == 'paused' else 'finished' if goal_status == 'completed' else
                      'working' if counts['ready'] or counts['claimed'] else
                      'blocked' if counts['uncertain'] or counts['interrupted'] else 'idle')
        return {'queue': counts, 'queued_jobs': sum(counts.values()), 'run_status': run_status,
                'budgeted_deliverable_status': goal_status, 'program_status': 'incomplete', 'coverage': coverage,
                'completion_validation': 'not_performed', 'whole_audit_complete': False,
                'note': 'Recorded coverage and a finished budgeted deliverable do not establish a complete repository audit. Explicit completion_status() revalidates current sources and revisions.'}
