import json
import sqlite3
import uuid
from .knowledge import SCHEMA
from .control import SCHEMA as CONTROL_SCHEMA
from .implementation import SCHEMA as IMPLEMENTATION_SCHEMA
from .spending import SCHEMA as SPENDING_SCHEMA, Spending
from .frontier import SCHEMA as FRONTIER_SCHEMA
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS goals (
          id TEXT PRIMARY KEY, objective TEXT NOT NULL, requirements TEXT NOT NULL,
          workspace TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id),
          instructions TEXT NOT NULL, priority INTEGER NOT NULL,
          adapter TEXT NOT NULL, status TEXT NOT NULL, session_id TEXT,
          result TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS decisions (
          id INTEGER PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id),
          body TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS attempts (
          id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
          instructions TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        ''')
        self.db.executescript(SCHEMA)
        self.db.executescript(CONTROL_SCHEMA)
        self.db.executescript(IMPLEMENTATION_SCHEMA)
        self.db.executescript(SPENDING_SCHEMA)
        if 'model' not in {r[1] for r in self.db.execute('PRAGMA table_info(spending_limits)')}:
            self.db.execute("ALTER TABLE spending_limits ADD COLUMN model TEXT NOT NULL DEFAULT 'gpt-4.1-2025-04-14'")
        self.db.executescript(FRONTIER_SCHEMA)
        if 'origin' not in {r[1] for r in self.db.execute('PRAGMA table_info(frontier_files)')}:
            self.db.execute("ALTER TABLE frontier_files ADD COLUMN origin TEXT NOT NULL DEFAULT 'git'")
        from .campaign import SCHEMA as CAMPAIGN_SCHEMA
        self.db.executescript(CAMPAIGN_SCHEMA)
        from .cadence import SCHEMA as CADENCE_SCHEMA
        self.db.executescript(CADENCE_SCHEMA)
        if 'last_review_decision' not in {r[1] for r in self.db.execute('PRAGMA table_info(campaign_cadence)')}:
            self.db.execute('ALTER TABLE campaign_cadence ADD COLUMN last_review_decision INTEGER NOT NULL DEFAULT 0')
        if 'quality_spec' not in {r[1] for r in self.db.execute('PRAGMA table_info(campaigns)')}:
            self.db.execute('''ALTER TABLE campaigns ADD COLUMN quality_spec TEXT NOT NULL
                DEFAULT '{"sources":20,"branches":4,"hypotheses":6,"mechanisms":2}' ''')
        self.db.executescript('''CREATE INDEX IF NOT EXISTS task_goal ON tasks(goal_id);
            CREATE INDEX IF NOT EXISTS decision_goal ON decisions(goal_id,id);
            CREATE INDEX IF NOT EXISTS research_goal ON research_jobs(goal_id,created);
            CREATE INDEX IF NOT EXISTS run_job ON research_runs(job_id);
            CREATE INDEX IF NOT EXISTS source_locator ON research_sources(workspace,locator);''')

    def execute(self, sql, args=()):
        if self.db.in_transaction:
            return self.db.execute(sql, args)
        with self.db:
            return self.db.execute(sql, args)

    def create_goal(self, objective, requirements, workspace):
        goal_id = uuid.uuid4().hex
        self.execute('INSERT INTO goals(id,objective,requirements,workspace) VALUES(?,?,?,?)',
                     (goal_id, objective, json.dumps(requirements), str(Path(workspace).resolve())))
        return goal_id

    def research_history(self, goal_id, offset=0, limit=10, include_report=True, stage=None):
        rows = self.db.execute('''SELECT j.*, r.id AS run_id, r.status AS run_status,
            r.report, r.evaluation, r.feedback FROM research_jobs j
            LEFT JOIN research_runs r ON r.id=(SELECT id FROM research_runs
                WHERE job_id=j.id ORDER BY created DESC, rowid DESC LIMIT 1)
            WHERE j.goal_id=? AND (? IS NULL OR j.stage=?)
            ORDER BY j.updated DESC, j.rowid DESC LIMIT ? OFFSET ?''',
            (goal_id, stage, stage, min(max(limit, 1), 20), max(offset, 0))).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            for key in ('report', 'evaluation'):
                item[key] = json.loads(item[key]) if item[key] else None
            if not include_report:
                item.pop('report')
            history.append(item)
        return history

    def research_state(self, goal_id):
        """Reconstruct the current research prerequisite without relying on chat history.

        Recorded acceptance is advisory: implementation authorization still checks
        evidence freshness, revisions, explicit permission and executable coverage.
        """
        budget = self.db.execute('SELECT max_runs-spent FROM research_limits WHERE goal_id=?',
                                 (goal_id,)).fetchone()
        remaining = budget[0] if budget else 12
        latest = self.research_history(goal_id, limit=1, include_report=False, stage='research')
        job = latest[0] if latest else None
        if job:
            job['scope_gaps'] = [r[0] for r in self.db.execute('''SELECT DISTINCT query
                FROM research_queries WHERE run_id=? AND operation='scope_gap' ORDER BY query''', (job['run_id'],))]
            reported_gaps = [gap['reference'] for gap in (job['evaluation'] or {}).get('blocking_gaps', [])
                if gap['basis'] == 'source' and gap['reference'] not in json.loads(job['spec'])['scope']]
            job['scope_gaps'] = sorted(set(job['scope_gaps'] + reported_gaps))
            job['retryable'] = (job['status'] in ('insufficient', 'blocked', 'interrupted')
                and job['attempts'] < json.loads(job['spec'])['budget']['attempts']
                and remaining >= 2 and not job['scope_gaps'])
            job['needs_scope_expansion'] = bool(job['scope_gaps'] and job['status'] in ('insufficient', 'blocked', 'escalated', 'interrupted'))
        accepted = [r[0] for r in self.db.execute('''SELECT id FROM research_jobs
            WHERE goal_id=? AND stage='research' AND status='supported'
            ORDER BY updated DESC LIMIT 20''', (goal_id,))]
        return {'accepted_job_ids': accepted, 'latest_job': job, 'remaining_role_runs': remaining}

    def snapshot(self, goal_id, history_limit=20):
        row = self.db.execute('SELECT * FROM goals WHERE id=?', (goal_id,)).fetchone()
        if row is None:
            raise ValueError(f'Unknown goal: {goal_id}')
        goal = dict(row)
        goal['requirements'] = json.loads(goal['requirements'])
        limit = -1 if history_limit is None else max(1, history_limit)
        tasks = [dict(r) for r in self.db.execute(
            'SELECT * FROM tasks WHERE goal_id=? ORDER BY rowid DESC LIMIT ?', (goal_id, limit))][::-1]
        decisions = [json.loads(r[0]) for r in self.db.execute(
            'SELECT body FROM decisions WHERE goal_id=? ORDER BY id DESC LIMIT ?', (goal_id, limit))][::-1]
        original_requirements = goal['requirements']
        revision = self.db.execute('SELECT * FROM goal_versions WHERE goal_id=? ORDER BY revision DESC LIMIT 1', (goal_id,)).fetchone()
        if revision:
            goal.update(objective=revision['objective'], requirements=json.loads(revision['requirements']),
                        revision=revision['revision'], constraints=json.loads(revision['constraints']))
        goal['original_requirements'] = original_requirements
        gate=self.db.execute('SELECT require_implementation FROM goal_completion_gates WHERE goal_id=?',(goal_id,)).fetchone()
        goal['requires_verified_implementation']=bool(gate and gate[0])
        research = [dict(r) for r in self.db.execute('SELECT * FROM research_jobs WHERE goal_id=? ORDER BY updated DESC, rowid DESC LIMIT 20', (goal_id,))]
        from .knowledge import Knowledge
        from .cadence import Cadence
        return {'goal': goal, 'tasks': tasks, 'decisions': decisions,
                'research': research, 'opportunities': Knowledge(self).backlog(goal_id, limit=history_limit),
                'research_progress': Knowledge(self).progress(goal_id, limit=40 if history_limit is not None else None),
                'history_counts': {'tasks': self.db.execute('SELECT count(*) FROM tasks WHERE goal_id=?',(goal_id,)).fetchone()[0],
                                   'decisions': self.db.execute('SELECT count(*) FROM decisions WHERE goal_id=?',(goal_id,)).fetchone()[0],
                                   'returned_limit': history_limit},
                'campaign': dict(r) if (r := self.db.execute('SELECT * FROM campaigns WHERE goal_id=?',(goal_id,)).fetchone()) else None,
                'campaign_cadence': Cadence(self, goal_id).status(),
                'spending': Spending(self,goal_id).status(),
                'remaining_decisions': r[0] if (r := self.db.execute('SELECT remaining FROM supervisor_budgets WHERE goal_id=?', (goal_id,)).fetchone()) else 0,
                'research_state': self.research_state(goal_id),
                'verification_results': [dict(r) for r in self.db.execute('''SELECT v.implementation_id,
                    v.candidate, v.goal_revision, v.requirement, v.passed, v.exit_code, v.artifact
                    FROM verifications v JOIN implementations i ON i.id=v.implementation_id
                    WHERE i.goal_id=? AND v.candidate=i.candidate AND v.goal_revision=?''',
                    (goal_id, goal.get('revision', 1)))],
                'latest_audit': dict(a) if (a := self.db.execute('''SELECT * FROM completion_audits
                    WHERE goal_id=? AND revision=? ORDER BY created DESC, rowid DESC LIMIT 1''',
                    (goal_id, goal.get('revision', 1))).fetchone()) else None,
                'research_budget': dict(r) if (r := self.db.execute('SELECT * FROM research_limits WHERE goal_id=?', (goal_id,)).fetchone()) else None,
                'active': [dict(r) for r in self.db.execute("SELECT * FROM task_runtime WHERE state IN ('running','waiting','uncertain','paused') AND task_id IN (SELECT id FROM tasks WHERE goal_id=?) ORDER BY rowid DESC LIMIT 50", (goal_id,))],
                'implementations': [dict(r) | {'spec':json.loads(r['spec'])} for r in self.db.execute('SELECT i.*,a.spec,a.revoked AS authorization_revoked FROM implementations i JOIN implementation_authorizations a ON i.authorization=a.id WHERE i.goal_id=?', (goal_id,))],
                'worker_messages': [dict(r) for r in self.db.execute("SELECT kind,body,task_id,created FROM messages WHERE goal_id=? AND direction='worker_to_manager' AND kind IN ('implementation_handoff','question','blocker','findings') ORDER BY created DESC LIMIT 20",(goal_id,))],
                'manager_feedback': [dict(r) | {'body':json.loads(r['body'])} for r in self.db.execute("SELECT kind,body,created FROM messages WHERE goal_id=? AND direction='worker_to_manager' AND kind IN ('decision_rejected','completion_rejected','manager_budget_exhausted') ORDER BY created DESC LIMIT 10",(goal_id,))],
                'pending_messages': [dict(r) for r in self.db.execute("SELECT * FROM messages WHERE goal_id=? AND state IN ('pending','uncertain','sending','sent_no_ack') ORDER BY created DESC LIMIT 20", (goal_id,))][::-1]}


    def task(self, task_id):
        row = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError(f'Unknown task: {task_id}')
        return dict(row)
