"""Persistent wall-clock, decision and dollar limits for read-only investigations."""
import asyncio
import json
from pathlib import Path
import time

from .control import Control
from .frontier import Frontier
from .research import Research
from .spending import Spending, SpendingLimitReached
from .supervisor import Supervisor

SCHEMA = '''
CREATE TABLE IF NOT EXISTS campaigns (
 goal_id TEXT PRIMARY KEY REFERENCES goals(id), created REAL NOT NULL, deadline REAL NOT NULL,
 max_decisions INTEGER NOT NULL, decisions_spent INTEGER NOT NULL DEFAULT 0,
 max_research_runs INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT,
 report_path TEXT NOT NULL, last_progress INTEGER NOT NULL DEFAULT 0,
 stagnant_decisions INTEGER NOT NULL DEFAULT 0,
 strategy TEXT NOT NULL DEFAULT 'manager',
 quality_spec TEXT NOT NULL DEFAULT '{"sources":20,"branches":4,"hypotheses":6,"mechanisms":2}');
'''


class ReadOnlyRuntime:
    async def close(self):
        pass


class CampaignSupervisor(Supervisor):
    """Supervisor whose campaign controls also apply to CLI/inbox entry points."""


class Campaign:
    def __init__(self, store, goal_id):
        self.store, self.goal_id = store, goal_id

    def validate_decision(self, decision):
        if decision.action not in ('discover', 'research', 'retry_research', 'pause', 'complete'):
            raise ValueError('This campaign is read-only. Use discovery/research; external edits, PRs and unmetered native workers are disabled.')
        if decision.action == 'complete':
            quality = Campaign(self.store,self.goal_id).quality()
            if not quality['met']:
                raise ValueError('Campaign evidence requirements remain unmet: '+json.dumps(quality))
            supported = self.store.db.execute("SELECT 1 FROM research_jobs WHERE goal_id=? AND status='supported' LIMIT 1", (self.goal_id,)).fetchone()
            if not supported:
                raise ValueError('Completion needs an independently supported finding. Falsification alone does not establish the replacement explanation.')
            if self.status()['strategy']=='investigator':
                from .investigator import completion_context
                _,digest=completion_context(self.store,self.store.snapshot(self.goal_id))
                row=self.store.db.execute("SELECT detail FROM activity WHERE goal_id=? AND kind='research_goal_audit' ORDER BY id DESC LIMIT 1",(self.goal_id,)).fetchone()
                audit=json.loads(row[0]) if row else {}
                if not audit.get('passed') or audit.get('digest')!=digest:
                    raise ValueError('Investigator completion needs a fresh independent objective/requirements audit; counts alone are insufficient')
        if decision.action == 'pause':
            observed = self.store.db.execute('''SELECT 1 FROM research_access a JOIN research_runs r ON r.id=a.run_id
                JOIN research_jobs j ON j.id=r.job_id WHERE j.goal_id=? LIMIT 1''', (self.goal_id,)).fetchone()
            if not observed and Frontier(self.store, self.goal_id).suggestions(1):
                raise ValueError('Accessible indexed files remain unexamined. Inspect the frontier before declaring sources unavailable.')
            if not Campaign(self.store,self.goal_id).quality()['met'] and Frontier(self.store,self.goal_id).suggestions(1):
                external = self.store.db.execute("""SELECT 1 FROM research_runs r JOIN research_jobs j ON j.id=r.job_id
                    WHERE j.goal_id=? AND (r.feedback LIKE 'PermissionError:%' OR r.feedback LIKE 'APIConnectionError:%'
                    OR r.feedback LIKE 'AuthenticationError:%' OR r.feedback LIKE 'RateLimitError:%') LIMIT 1""",(self.goal_id,)).fetchone()
                if not external:
                    raise ValueError('Unexamined branches and research requirements remain. Missing production telemetry is a gap, not a reason to stop all code/test investigation. Do not invent issues; test another plausible explanation.')

    def status(self):
        row = self.store.db.execute('SELECT * FROM campaigns WHERE goal_id=?', (self.goal_id,)).fetchone()
        return dict(row) | {'spending':Spending(self.store,self.goal_id).status()} if row else None

    def configure(self, days=None, decisions=None, research_runs=None, dollars=None, report=None, quality=None,strategy=None):
        if strategy is not None and strategy not in ('manager','investigator'):raise ValueError('Unknown campaign strategy')
        existing = self.status()
        if existing:
            if any(v is not None for v in (days, decisions, research_runs, dollars, report, quality)):
                raise ValueError('Resume the saved campaign without new limits; limits cannot be reset by rerunning it')
            if strategy and strategy!=existing['strategy']:
                self.store.execute('UPDATE campaigns SET strategy=? WHERE goal_id=?',(strategy,self.goal_id))
                Control(self.store).event(self.goal_id,'campaign_strategy_changed',{'prior':existing['strategy'],'strategy':strategy,'limits_reset':False})
            return self.status()
        if days is None or decisions is None or research_runs is None or dollars is None:
            raise ValueError('First run requires --days, --decisions, --research-runs and --dollars')
        if not 0 < days <= 30 or not 1 <= decisions <= 100000 or not 1 <= research_runs <= 100000:
            raise ValueError('Use days 0..30, decisions and role runs 1..100000')
        if not 0 < dollars <= 25:
            raise ValueError('Each campaign must have a positive dollar limit no greater than $25')
        goal = self.store.db.execute('SELECT 1 FROM goals WHERE id=?', (self.goal_id,)).fetchone()
        if not goal:
            raise ValueError('Unknown goal')
        if self.store.db.execute('SELECT 1 FROM research_limits WHERE goal_id=?', (self.goal_id,)).fetchone():
            raise ValueError('Use a fresh goal: previously unmetered work cannot be retroactively budgeted')
        now = time.time()
        with self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            Spending(self.store, self.goal_id).configure(dollars)
            Research(self.store).configure(self.goal_id, research_runs)
            self.store.db.execute('''INSERT INTO campaigns(goal_id,created,deadline,max_decisions,decisions_spent,max_research_runs,state,reason,report_path,last_progress,stagnant_decisions)
                VALUES(?,?,?,?,0,?,?,NULL,?,0,0)''',
                (self.goal_id, now, now+days*86400, decisions, research_runs, 'ready',
                 str(Path(report or self.store.path.parent / 'campaign.html').absolute())))
            if quality is not None:
                if set(quality) != {'sources','branches','hypotheses','mechanisms'} or any(not isinstance(v,int) or v<0 for v in quality.values()):
                    raise ValueError('Invalid campaign quality requirements')
                self.store.db.execute('UPDATE campaigns SET quality_spec=? WHERE goal_id=?',(json.dumps(quality),self.goal_id))
            if strategy:self.store.db.execute('UPDATE campaigns SET strategy=? WHERE goal_id=?',(strategy,self.goal_id))
        return self.status()

    def quality(self):
        required = json.loads(self.status()['quality_spec'])
        db=self.store.db
        observed=db.execute('''SELECT DISTINCT s.locator FROM research_sources s JOIN research_access a ON a.source_id=s.id
            JOIN research_runs r ON r.id=a.run_id JOIN research_jobs j ON j.id=r.job_id WHERE j.goal_id=?''',(self.goal_id,)).fetchall()
        branches=db.execute('''SELECT count(DISTINCT f.bucket) FROM frontier_files f WHERE goal_id=? AND EXISTS(
            SELECT 1 FROM research_sources s JOIN research_access a ON a.source_id=s.id JOIN research_runs r ON r.id=a.run_id
            JOIN research_jobs j ON j.id=r.job_id WHERE j.goal_id=f.goal_id AND s.locator=f.locator)''',(self.goal_id,)).fetchone()[0]
        mechanisms=set()
        from .knowledge import Knowledge
        from .resources import resource_kind
        knowledge=Knowledge(self.store)
        for row in db.execute("SELECT body,citations FROM knowledge WHERE goal_id=? AND kind='finding' AND classification='verified'",(self.goal_id,)):
            body=json.loads(row['body']);finding=body['finding']
            if finding.get('category')!='mechanism' or body.get('mechanism_proof_version')!=2:continue
            sources=[knowledge.source(s) for s in json.loads(row['citations'])]
            identity=body.get('mechanism_identity')
            if identity and sources and not any(s['stale'] for s in sources) and any(resource_kind(s['locator'])=='code' for s in sources):mechanisms.add(tuple(identity))
        actual={'sources':len(observed),'branches':branches,'hypotheses':db.execute("SELECT count(*) FROM research_jobs WHERE goal_id=? AND stage='research' AND status IN ('supported','falsified')",(self.goal_id,)).fetchone()[0],'mechanisms':len(mechanisms)}
        return {'required':required,'actual':actual,'met':all(actual[k]>=v for k,v in required.items()),
                'note':'Minimum breadth and evidence gates, not proof of exhaustive coverage or production impact.'}

    def stop(self, state, reason):
        self.store.execute('UPDATE campaigns SET state=?,reason=? WHERE goal_id=?', (state, reason, self.goal_id))
        Control(self.store).event(self.goal_id, 'campaign_stopped', {'state': state, 'reason': reason})

    def claim_decision(self, now=None):
        now = time.time() if now is None else now
        with self.store.db:
            row = self.status()
            if row['deadline'] <= now:
                self.stop('deadline', 'Saved wall-clock deadline reached')
                return False
            changed = self.store.db.execute("UPDATE campaigns SET decisions_spent=decisions_spent+1 WHERE goal_id=? AND state='running' AND decisions_spent<max_decisions", (self.goal_id,))
            if changed.rowcount != 1:
                if row['state'] == 'running':
                    self.stop('decision_budget', 'Saved decision budget exhausted')
                return False
        return True

    def recover_reads(self):
        # The owner lock proves the previous local writer is gone. Read-only SDK
        # roles have no external code action to replay; retain their spent attempts.
        rows = self.store.db.execute('''SELECT t.id,r.spec FROM tasks t JOIN task_runtime r ON r.task_id=t.id
            WHERE t.goal_id=? AND t.adapter='research' AND r.state='uncertain' ''', (self.goal_id,)).fetchall()
        for row in rows:
            job_id = json.loads(row['spec']).get('job_id')
            job = self.store.db.execute('SELECT status FROM research_jobs WHERE id=? AND goal_id=?', (job_id,self.goal_id)).fetchone()
            if not job:
                continue
            self.store.execute("UPDATE task_runtime SET state='reconciled' WHERE task_id=?", (row['id'],))
            self.store.execute("UPDATE tasks SET status='interrupted' WHERE id=?", (row['id'],))
            Control(self.store).event(self.goal_id, 'read_only_recovery', {'job_id': job_id, 'task_id': row['id'],
                'reason': 'Previous local read stopped; evidence and spent attempts retained. No role request replayed.'})

    def checkpoint(self):
        from .reporting import goal_report, write_report
        state=self.status()
        frontier=Frontier(self.store,self.goal_id).summary()
        summary={'state':state['state'],'reason':state['reason'],'decisions_spent':state['decisions_spent'],
            'spending':state['spending'],'quality':self.quality(),
            'observed_files':frontier['observed_files'] if frontier else 0,
            'role_runs_spent':self.store.db.execute('SELECT spent FROM research_limits WHERE goal_id=?',(self.goal_id,)).fetchone()[0]}
        previous=self.store.db.execute("SELECT detail FROM activity WHERE goal_id=? AND kind='campaign_checkpoint' ORDER BY id DESC LIMIT 1",(self.goal_id,)).fetchone()
        if not previous or json.loads(previous[0])!=summary:
            Control(self.store).event(self.goal_id,'campaign_checkpoint',summary)
        write_report(goal_report(self.store, self.goal_id), self.status()['report_path'])

    def source_progress(self):
        # A new UUID, reread, or recall of the same excerpt is not exploration.
        # New pages and changed revisions still count even within one file.
        return self.store.db.execute('''SELECT count(*) FROM (
            SELECT s.locator,s.revision,s.excerpt FROM research_sources s
            JOIN research_access a ON a.source_id=s.id JOIN research_runs r ON r.id=a.run_id
            JOIN research_jobs j ON j.id=r.job_id WHERE j.goal_id=?
            GROUP BY s.locator,s.revision,s.excerpt)''',(self.goal_id,)).fetchone()[0]

    async def drive(self, supervisor=None, continue_paused=False, poll_seconds=.2):
        saved = self.status()
        if not saved:
            raise ValueError('Configure the campaign first')
        if saved['state'] not in ('ready', 'running', 'interrupted') and not continue_paused:
            return saved
        if supervisor is None:
            from .manager import AgentBrain
            from .research import ResearchAgents
            supervisor = CampaignSupervisor(self.store, self.goal_id, runtime=ReadOnlyRuntime(),
                brain=AgentBrain('gpt-4.1', store=self.store), research=Research(self.store, ResearchAgents('gpt-4.1')))
            if saved['strategy']=='investigator':
                from .investigator import InvestigatorBrain,PreparedResearchAgents
                supervisor.brain=InvestigatorBrain(self.store)
                supervisor.research=Research(self.store,PreparedResearchAgents(supervisor.brain))
        await supervisor.open()
        try:
            if continue_paused:
                self.store.execute("UPDATE goals SET status='active' WHERE id=? AND status IN ('paused','completed')", (self.goal_id,))
                self.store.execute('UPDATE campaigns SET stagnant_decisions=0 WHERE goal_id=?',(self.goal_id,))
                Control(self.store).event(self.goal_id,'campaign_resumed',{'prior_state':saved['state'],
                    'prior_reason':saved['reason'],'prior_stagnant_decisions':saved['stagnant_decisions'],
                    'note':'Explicit continuation resets the stagnation window only; saved deadline and all consumed budgets are unchanged.'})
            self.recover_reads()
            Frontier(self.store,self.goal_id).build()
            self.store.execute("UPDATE campaigns SET state='running',reason=NULL WHERE goal_id=?", (self.goal_id,))
            last_report = 0
            while not supervisor.closed and self.status()['state'] == 'running':
                if time.time() >= saved['deadline']:
                    self.stop('deadline', 'Saved wall-clock deadline reached')
                    break
                goal = self.store.db.execute('SELECT status FROM goals WHERE id=?', (self.goal_id,)).fetchone()[0]
                if goal in ('completed', 'paused'):
                    self.stop(goal, 'Manager completed or paused the investigation; no automatic restart')
                    break
                await supervisor.monitor()
                if not supervisor.work:
                    count = self.source_progress()
                    current = self.status()
                    stagnant = 0 if count != current['last_progress'] else current['stagnant_decisions']
                    self.store.execute('UPDATE campaigns SET last_progress=?,stagnant_decisions=? WHERE goal_id=?', (count,stagnant,self.goal_id))
                    if stagnant >= 8:
                        self.stop('paused', 'Eight decisions without new source access; revise the approach instead of burning the budget')
                        break
                    result = await supervisor.advance(steps=1)
                    if result.get('blocked') and not supervisor.work:
                        if self.status()['state']=='running':
                            self.stop('paused', str(result['blocked']))
                        break
                    self.store.execute('UPDATE campaigns SET stagnant_decisions=stagnant_decisions+1 WHERE goal_id=?',(self.goal_id,))
                if time.time() - last_report >= 60:
                    self.checkpoint()
                    last_report = time.time()
                await asyncio.sleep(poll_seconds)
        except SpendingLimitReached as exc:
            # A pause can arrive while a model turn is between paid requests.
            # Its next reservation must fail without relabeling the operator's
            # explicit stop as dollar exhaustion.
            if self.status()['state']=='running':
                self.stop('deadline' if time.time()>=saved['deadline'] else 'dollar_budget',str(exc))
        except asyncio.CancelledError:
            self.stop('interrupted', 'Owner stopped; resume retains deadline, spending reservations and research attempts')
            raise
        except Exception as exc:
            self.stop('paused', 'Campaign error: '+type(exc).__name__)
            raise
        finally:
            await supervisor.close()
            self.checkpoint()
            if hasattr(self.store, '_campaign_client'):
                await self.store._campaign_client.close()
                del self.store._campaign_client
        return self.status()
