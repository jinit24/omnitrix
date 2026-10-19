import json
import sqlite3
import uuid
from .knowledge import SCHEMA
from .control import SCHEMA as CONTROL_SCHEMA
from .implementation import SCHEMA as IMPLEMENTATION_SCHEMA
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

    def snapshot(self, goal_id):
        row = self.db.execute('SELECT * FROM goals WHERE id=?', (goal_id,)).fetchone()
        if row is None:
            raise ValueError(f'Unknown goal: {goal_id}')
        goal = dict(row)
        goal['requirements'] = json.loads(goal['requirements'])
        tasks = [dict(r) for r in self.db.execute(
            'SELECT * FROM tasks WHERE goal_id=? ORDER BY rowid', (goal_id,))]
        decisions = [json.loads(r[0]) for r in self.db.execute(
            'SELECT body FROM decisions WHERE goal_id=? ORDER BY id', (goal_id,))]
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
        from .control import Control
        from .budget import budget_enabled, Budget
        dollar_budget = Budget(self,goal_id).summary() if budget_enabled(self,goal_id) else None
        audit = None
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_areas'").fetchone():
            from .audit import Audit
            audit = Audit(self,goal_id).summary()
        campaign = None
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_queue'").fetchone():
            from .campaign import Campaign
            campaign = Campaign(self, goal_id).summary()
        repairs = []
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manager_repair_tickets'").fetchone():
            repairs = [dict(r) for r in self.db.execute(
                "SELECT * FROM manager_repair_tickets WHERE goal_id=? AND state='pending' ORDER BY created DESC LIMIT 1", (goal_id,))]
        return {'dollar_budget': dollar_budget, 'audit_coverage': audit, 'goal': goal, 'tasks': tasks, 'decisions': decisions,
                'campaign': campaign, 'manager_repairs': repairs,
                'research_context': Control(self).research_context(goal_id),
                'research_reviews': [dict(r) for r in self.db.execute('SELECT id,job_id,run_id,status,error,created,finished FROM research_reviews WHERE job_id IN (SELECT id FROM research_jobs WHERE goal_id=?) ORDER BY created DESC LIMIT 20',(goal_id,))],
                'research_agenda': [dict(r) for r in self.db.execute('SELECT stage,status,count(*) AS jobs FROM research_jobs WHERE goal_id=? GROUP BY stage,status', (goal_id,))],
                'research_checkpoints': [dict(r) for r in self.db.execute("SELECT detail,created FROM activity WHERE goal_id=? AND kind='research_checkpoint' ORDER BY id DESC LIMIT 10", (goal_id,))],
                'research': research, 'opportunities': Knowledge(self).backlog(goal_id),
                'research_budget': dict(r) if (r := self.db.execute('SELECT * FROM research_limits WHERE goal_id=?', (goal_id,)).fetchone()) else None,
                'active': [dict(r) for r in self.db.execute('SELECT * FROM task_runtime WHERE task_id IN (SELECT id FROM tasks WHERE goal_id=?)', (goal_id,))],
                'implementations': [dict(r) | {'spec':json.loads(r['spec'])} for r in self.db.execute('SELECT i.*,a.spec,a.revoked AS authorization_revoked FROM implementations i JOIN implementation_authorizations a ON i.authorization=a.id WHERE i.goal_id=?', (goal_id,))],
                'worker_messages': [dict(r) for r in self.db.execute("SELECT id,kind,body,task_id,direction,state,created FROM messages WHERE goal_id=? AND direction='worker_to_manager' AND kind IN ('implementation_handoff','question','blocker','findings','reconciled_outcome','stalled','manager_output_invalid','budget_guard_blocked','decision_rejected','completion_rejected') ORDER BY created DESC LIMIT 20",(goal_id,))],
                'pending_messages': [dict(r) for r in self.db.execute("SELECT * FROM messages WHERE goal_id=? AND state IN ('pending','uncertain','sending','sent_no_ack') ORDER BY created LIMIT 20", (goal_id,))]}


    def task(self, task_id):
        row = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError(f'Unknown task: {task_id}')
        return dict(row)
