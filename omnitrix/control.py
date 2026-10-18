"""Durable conversation, revisions, ownership, delivery, and revision fences."""
import json
import time
import uuid
from agents import SQLiteSession

SCHEMA = '''
CREATE TABLE IF NOT EXISTS goal_completion_gates (
 goal_id TEXT PRIMARY KEY REFERENCES goals(id), require_implementation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS goal_versions (
 goal_id TEXT NOT NULL REFERENCES goals(id), revision INTEGER NOT NULL,
 objective TEXT NOT NULL, requirements TEXT NOT NULL, constraints TEXT NOT NULL,
 reason TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(goal_id,revision));
CREATE TABLE IF NOT EXISTS policy_versions (
 goal_id TEXT NOT NULL REFERENCES goals(id), version INTEGER NOT NULL,
 instructions TEXT NOT NULL, origin TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(goal_id,version));
CREATE TABLE IF NOT EXISTS proposals (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), kind TEXT NOT NULL,
 body TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), task_id TEXT,
 direction TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL,
 runtime_id TEXT, created REAL NOT NULL, acknowledged REAL);
CREATE TABLE IF NOT EXISTS task_runtime (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), goal_revision INTEGER NOT NULL,
 policy_revision INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL, spec TEXT NOT NULL, owner TEXT, session_id TEXT, turn_id TEXT,
 started REAL NOT NULL, last_progress REAL NOT NULL, deadline REAL NOT NULL,
 progress_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS activity (
 id INTEGER PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), task_id TEXT,
 kind TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS completion_audits (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), revision INTEGER NOT NULL,
 candidate TEXT NOT NULL, requirements TEXT NOT NULL, passed INTEGER NOT NULL, created REAL NOT NULL);
'''


class Control:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def initialize(self, goal_id):
        goal = self.store.snapshot(goal_id)['goal']
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO goal_versions VALUES(?,?,?,?,?,?,?)',
                (goal_id, 1, goal['objective'], json.dumps(goal['requirements']), '[]', 'Original goal', time.time()))
            self.db.execute('INSERT OR IGNORE INTO policy_versions VALUES(?,?,?,?,?)',
                (goal_id, 1, '', 'initial', time.time()))

    def current(self, goal_id):
        self.initialize(goal_id)
        goal = dict(self.db.execute('SELECT * FROM goal_versions WHERE goal_id=? ORDER BY revision DESC LIMIT 1',
                                   (goal_id,)).fetchone())
        goal['requirements'] = json.loads(goal['requirements'])
        goal['constraints'] = json.loads(goal['constraints'])
        policy = dict(self.db.execute('SELECT * FROM policy_versions WHERE goal_id=? ORDER BY version DESC LIMIT 1',
                                     (goal_id,)).fetchone())
        return {'goal': goal, 'policy': policy, 'session_id': f'manager:{goal_id}'}

    def require_implementation(self, goal_id):
        self.initialize(goal_id)
        self.store.execute('INSERT OR REPLACE INTO goal_completion_gates VALUES(?,1)',(goal_id,))

    def session(self, goal_id):
        # SDK sessions keep manager-only conversation separate from worker transcripts.
        return SQLiteSession(f'manager:{goal_id}', str(self.store.path) + '.sessions.db',
                             session_settings={'limit': 12})

    def message(self, goal_id, kind, body, task_id=None, direction='user_to_manager', message_id=None,
                runtime_id=None, state='pending'):
        message_id = message_id or uuid.uuid4().hex
        self.store.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',
            (message_id, goal_id, task_id, direction, kind, json.dumps(body), state,
             runtime_id, time.time(), None))
        return message_id

    def ack(self, message_id):
        self.store.execute("UPDATE messages SET state='acknowledged',acknowledged=? WHERE id=?", (time.time(), message_id))

    def event(self, goal_id, kind, detail, task_id=None):
        self.store.execute('INSERT INTO activity(goal_id,task_id,kind,detail,created) VALUES(?,?,?,?,?)',
                           (goal_id, task_id, kind, json.dumps(detail), time.time()))

    def propose(self, goal_id, kind, body):
        if kind not in ('policy', 'requirements'):
            raise ValueError('Only explicit policy or requirements proposals are supported')
        proposal_id = uuid.uuid4().hex
        self.store.execute('INSERT INTO proposals VALUES(?,?,?,?,?,?)',
                           (proposal_id, goal_id, kind, json.dumps(body), 'pending', time.time()))
        return proposal_id

    def accept(self, goal_id, proposal_id):
        proposal = self.db.execute('SELECT * FROM proposals WHERE id=? AND goal_id=?',
                                  (proposal_id, goal_id)).fetchone()
        if proposal is None or proposal['state'] != 'pending':
            raise ValueError('Unknown or already applied proposal')
        body = json.loads(proposal['body'])
        current = self.current(goal_id)
        if not isinstance(body,dict):
            raise ValueError('Proposal must be an object')
        if proposal['kind']=='requirements' and (not isinstance(body.get('constraint'),str) or not body['constraint'].strip() or not isinstance(body.get('add_requirements',[]),list) or any(not isinstance(c,str) or not c.strip() for c in body.get('add_requirements',[]))):
            raise ValueError('Requirements need a constraint and a list of nonempty criteria')
        with self.db:
            if proposal['kind'] == 'policy':
                text = body['instructions'].strip()
                if not text:
                    raise ValueError('Policy text cannot be empty')
                self.db.execute('INSERT INTO policy_versions VALUES(?,?,?,?,?)',
                    (goal_id, current['policy']['version']+1, text, 'explicit user approval', time.time()))
            else:
                # Original mandatory criteria are immutable; user additions cannot silently waive them.
                criteria = list(dict.fromkeys(current['goal']['requirements'] + body.get('add_requirements', [])))
                constraints = current['goal']['constraints'] + [body['constraint']]
                self.db.execute('INSERT INTO goal_versions VALUES(?,?,?,?,?,?,?)',
                    (goal_id, current['goal']['revision']+1, current['goal']['objective'],
                     json.dumps(criteria), json.dumps(constraints), body['constraint'], time.time()))
            self.db.execute("UPDATE proposals SET state='applied' WHERE id=?", (proposal_id,))
            self.db.execute("UPDATE goals SET status='active' WHERE id=?",(goal_id,))
            self.db.execute("UPDATE implementations SET status='invalidated' WHERE goal_id=? AND status IN ('running','repair','ready_for_audit','completed')",(goal_id,))
            active = list(self.db.execute("SELECT * FROM task_runtime WHERE state IN ('running','waiting','paused') AND task_id IN (SELECT id FROM tasks WHERE goal_id=?)", (goal_id,)))
            # An explicit target list preserves unaffected work; omitted targets means all active work.
            affected = body.get('affected_tasks')
            for task in active:
                if affected is not None and task['task_id'] not in affected:
                    continue
                self.db.execute("UPDATE task_runtime SET state='invalidated',generation=generation+1 WHERE task_id=?", (task['task_id'],))
                self.message(goal_id, 'constraints_changed', body, task['task_id'], 'manager_to_worker')
                job_id = json.loads(task['spec']).get('job_id')
                if job_id:
                    self.db.execute("UPDATE knowledge SET classification='invalidated' WHERE job_id=?", (job_id,))
                    self.db.execute("UPDATE research_jobs SET status='invalidated' WHERE id=?", (job_id,))
            # Candidate authorization and verification are bound to revision, not blanket permission.
            self.db.execute('UPDATE implementation_authorizations SET revoked=1 WHERE goal_id=?', (goal_id,))
        self.event(goal_id, 'revision_changed', {'proposal': proposal_id, 'affected': [t['task_id'] for t in active if affected is None or t['task_id'] in affected]})
        return self.current(goal_id)

    def pending(self, goal_id):
        return [dict(r) for r in self.db.execute("SELECT * FROM messages WHERE goal_id=? AND state IN ('pending','uncertain','sending','sent_no_ack') ORDER BY created", (goal_id,))]

    def valid_result(self, task_id, generation):
        row = self.db.execute('SELECT * FROM task_runtime WHERE task_id=?', (task_id,)).fetchone()
        return row is not None and row['generation'] == generation and row['state'] == 'running'

    def recover(self, goal_id):
        # Never replay a start/send with an unknown outcome. Reconcile the external thread first.
        with self.db:
            self.db.execute("UPDATE task_runtime SET state='uncertain' WHERE state IN ('running','waiting') AND task_id IN (SELECT id FROM tasks WHERE goal_id=?)", (goal_id,))
            self.db.execute("UPDATE tasks SET status='uncertain' WHERE id IN (SELECT task_id FROM task_runtime WHERE state='uncertain')")
            self.db.execute("UPDATE messages SET state='uncertain' WHERE goal_id=? AND state='sending'", (goal_id,))
            self.db.execute("UPDATE research_runs SET status='interrupted',feedback='Process restart; inspect prior evidence before retry' WHERE status='running' AND job_id IN (SELECT id FROM research_jobs WHERE goal_id=?)", (goal_id,))
            self.db.execute("UPDATE research_jobs SET status='interrupted' WHERE status='running' AND goal_id=?", (goal_id,))
        self.event(goal_id, 'recovery', {'pending_messages': len(self.pending(goal_id))})
