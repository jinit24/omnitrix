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
        if 'model' not in {r[1] for r in self.db.execute('PRAGMA table_info(spending_requests)')}:
            self.db.execute("ALTER TABLE spending_requests ADD COLUMN model TEXT NOT NULL DEFAULT 'gpt-4.1-2025-04-14'")
        self.db.executescript(FRONTIER_SCHEMA)
        if 'origin' not in {r[1] for r in self.db.execute('PRAGMA table_info(frontier_files)')}:
            self.db.execute("ALTER TABLE frontier_files ADD COLUMN origin TEXT NOT NULL DEFAULT 'git'")
        from .campaign import SCHEMA as CAMPAIGN_SCHEMA
        self.db.executescript(CAMPAIGN_SCHEMA)
        if 'strategy' not in {r[1] for r in self.db.execute('PRAGMA table_info(campaigns)')}:
            self.db.execute("ALTER TABLE campaigns ADD COLUMN strategy TEXT NOT NULL DEFAULT 'manager'")
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
        research = [dict(r) for r in self.db.execute('SELECT * FROM research_jobs WHERE goal_id=? ORDER BY created DESC LIMIT 20', (goal_id,))]
        from .knowledge import Knowledge
        return {'goal': goal, 'tasks': tasks, 'decisions': decisions,
                'research': research, 'opportunities': Knowledge(self).backlog(goal_id, limit=history_limit),
                'research_progress': Knowledge(self).progress(goal_id, limit=40 if history_limit is not None else None),
                'history_counts': {'tasks': self.db.execute('SELECT count(*) FROM tasks WHERE goal_id=?',(goal_id,)).fetchone()[0],
                                   'decisions': self.db.execute('SELECT count(*) FROM decisions WHERE goal_id=?',(goal_id,)).fetchone()[0],
                                   'returned_limit': history_limit},
                'campaign': dict(r) if (r := self.db.execute('SELECT * FROM campaigns WHERE goal_id=?',(goal_id,)).fetchone()) else None,
                'spending': Spending(self,goal_id).status(),
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
