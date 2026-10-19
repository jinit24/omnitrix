"""One manager actor, independent worker tasks, durable messages and result fences."""
import asyncio
import fcntl
import hashlib
import json
import time
import uuid
from agents import Agent, Runner
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from .control import Control
from .execution import Execution
from .implementation import Implementations
from .knowledge import Knowledge
from .manager import AgentBrain
from .models import ChatReply,Handoff,Decision
from .research import Research
from .runtime import CodexRuntime
from .workers import ROOT
from .budget import budget_run_config, close_budget_run_config, assert_no_unmetered_worker, BudgetError


REPAIR_SCHEMA = '''CREATE TABLE IF NOT EXISTS manager_repair_tickets (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id),
 fingerprint TEXT NOT NULL, category TEXT NOT NULL, diagnostic TEXT NOT NULL,
 state TEXT NOT NULL, created REAL NOT NULL, finished REAL,
 UNIQUE(goal_id,fingerprint));'''


class Supervisor:
    def __init__(self, store, goal_id, brain=None, runtime=None, research=None):
        self.store, self.db, self.goal_id = store, store.db, goal_id
        self.control = Control(store)
        self.brain = brain or AgentBrain(store=store)
        self.runtime = runtime or CodexRuntime(self.on_runtime_event)
        if runtime is not None and hasattr(runtime,'on_event'):
            runtime.on_event = self.on_runtime_event
        self.research = research or Research(store)
        self.actor = asyncio.Lock()
        self.work = {}
        self.owner = uuid.uuid4().hex
        self.autonomy = 0
        self._lock = None
        self.closed = False
        self.housekeeper = None
        self.store.execute(REPAIR_SCHEMA)

    def campaign(self, create=False):
        """Keep legacy goals untouched until a campaign queue is explicitly used."""
        if not create and not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_queue'").fetchone():
            return None
        from .campaign import Campaign
        return Campaign(self.store, self.goal_id)

    def finish_queued(self, spec, outcome):
        campaign = self.campaign()
        if campaign is not None and spec.get('job_id'):
            row = self.db.execute("SELECT state FROM campaign_queue WHERE goal_id=? AND job_id=?", (self.goal_id, spec['job_id'])).fetchone()
            if row and row['state'] == 'claimed':
                outcome = dict(outcome)
                if outcome.get('status') in ('paused', 'cancelled'):
                    outcome['status'] = 'interrupted'
                campaign.complete_job(spec['job_id'], outcome)

    async def open(self):
        self._lock = open(str(self.store.path)+'.lock', 'a')
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise RuntimeError('Another manager owns this database') from None
        self.control.initialize(self.goal_id)
        self.control.recover(self.goal_id)
        # A corrective request interrupted by process death is not replayed.
        pending_repairs = self.db.execute("SELECT id FROM manager_repair_tickets WHERE goal_id=? AND state='pending'", (self.goal_id,)).fetchall()
        if pending_repairs:
            with self.db:
                self.db.execute("UPDATE manager_repair_tickets SET state='interrupted',finished=? WHERE goal_id=? AND state='pending'", (time.time(), self.goal_id))
                self.db.execute("UPDATE goals SET status='paused' WHERE id=?", (self.goal_id,))
            self.control.event(self.goal_id, 'manager_repair_interrupted', {'tickets': [r['id'] for r in pending_repairs]})
        # Uncertain implementations must be inspected, never automatically reexecuted.
        self.store.execute("UPDATE implementations SET status='uncertain' WHERE goal_id=? AND status='running'", (self.goal_id,))
        # Local SDK observations have no external action to replay. Reconcile
        # their retained results automatically; attempts and dollar reservations
        # remain consumed. Unknown external threads/deliveries stay fenced.
        local = self.db.execute('''SELECT r.task_id FROM task_runtime r JOIN tasks t ON t.id=r.task_id
            WHERE t.goal_id=? AND t.adapter IN ('research','evaluation')
            AND r.state='uncertain' AND r.session_id IS NULL''', (self.goal_id,)).fetchall()
        for row in local:
            spec = json.loads(self.db.execute('SELECT spec FROM task_runtime WHERE task_id=?', (row['task_id'],)).fetchone()[0])
            if spec.get('job_id'):
                await self.reconcile(row['task_id'])
        campaign = self.campaign()
        if campaign is not None:
            # A crash can land between the committed claim and local task
            # creation. Preserve that consumed claim instead of inventing a
            # replacement execution on restart.
            for queued in self.db.execute("SELECT job_id,claimed FROM campaign_queue WHERE goal_id=? AND state='claimed'", (self.goal_id,)).fetchall():
                dispatched = self.db.execute("SELECT 1 FROM task_runtime r JOIN tasks t ON t.id=r.task_id WHERE t.goal_id=? AND json_extract(r.spec,'$.job_id')=? AND r.started>=?", (self.goal_id, queued['job_id'], queued['claimed'])).fetchone()
                if not dispatched:
                    campaign.complete_job(queued['job_id'], {'status': 'interrupted', 'feedback': 'Process stopped after the durable queue claim; no automatic replay of the assignment'})
        self.housekeeper = asyncio.create_task(self.housekeeping())
        return self

    async def housekeeping(self):
        while not self.closed:
            await self.monitor()
            await self.process_inbox()
            await asyncio.sleep(.2)

    async def process_inbox(self):
        inbox = self.db.execute("SELECT * FROM messages WHERE goal_id=? AND kind='control' AND state='pending' ORDER BY created LIMIT 10",(self.goal_id,)).fetchall()
        for message in inbox:
            self.store.execute("UPDATE messages SET state='sending' WHERE id=? AND state='pending'",(message['id'],))
            payload = json.loads(message['body'])
            command, text = payload.get('command','chat'),payload.get('text','')
            try:
                if command=='status':
                    result=self.status()
                elif command=='chat':
                    result=await self.chat(text)
                elif command=='policy':
                    result={'proposal':self.control.propose(self.goal_id,'policy',{'instructions':text})}
                elif command=='requirements':
                    result={'proposal':self.control.propose(self.goal_id,'requirements',json.loads(text))}
                elif command=='accept':
                    result=await self.accept(text)
                elif command in ('pause','cancel'):
                    result=await self.stop(text,command=='pause')
                elif command=='steer':
                    task_id,_,instruction=text.partition(' ')
                    mid=self.control.message(self.goal_id,'steering',{'instruction':instruction},task_id,'manager_to_worker')
                    result=await self.deliver(mid)
                elif command=='continue':
                    result=await self.advance(int(text or 5))
                elif command=='reconcile':
                    result=await self.reconcile(text)
                else:
                    raise ValueError('Unsupported control command')
                self.control.message(self.goal_id,'reply',result,direction='manager_to_user',message_id='reply:'+message['id'],state='acknowledged')
                self.control.ack(message['id'])
            except Exception as exc:
                self.control.message(self.goal_id,'reply',{'error':str(exc)},direction='manager_to_user',message_id='reply:'+message['id'],state='acknowledged')
                self.control.ack(message['id'])

    def status(self):
        return self.store.snapshot(self.goal_id)

    async def submit(self, kind, spec, operation, seconds=300):
        current = self.control.current(self.goal_id)
        task_id = Execution(self.store, type('Adapter', (), {'name': kind})()).assign(
            self.goal_id, json.dumps(spec), 1)
        now = time.time()
        self.store.execute('''INSERT INTO task_runtime
            (task_id,goal_revision,policy_revision,state,spec,owner,started,last_progress,deadline)
            VALUES(?,?,?,?,?,?,?,?,?)''', (task_id, current['goal']['revision'], current['policy']['version'],
            'running', json.dumps(spec), self.owner, now, now, now+seconds))
        self.store.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        generation = 1
        async def execute():
            try:
                result = await operation(task_id)
                accepted = self.control.valid_result(task_id, generation)
                if accepted:
                    with self.db:
                        terminal = 'uncertain' if isinstance(result,dict) and result.get('status')=='uncertain' else 'completed'
                        self.db.execute('UPDATE task_runtime SET state=?,last_progress=? WHERE task_id=?', (terminal,time.time(),task_id))
                        self.db.execute('UPDATE tasks SET status=?,result=? WHERE id=?', (terminal,json.dumps(result),task_id))
                    self.control.message(self.goal_id, 'findings', result, task_id, 'worker_to_manager')
                else:
                    self.control.message(self.goal_id, 'stale_result', result, task_id, 'worker_to_manager')
                self.control.event(self.goal_id, 'worker_finished', {'result': result}, task_id)
                self.finish_queued(spec, result if accepted else {'status': 'invalidated'})
            except asyncio.CancelledError:
                row = self.db.execute('SELECT state FROM task_runtime WHERE task_id=?', (task_id,)).fetchone()
                state = row[0] if row and row[0] in ('paused','cancelled','invalidated') else 'uncertain'
                self.store.execute('UPDATE task_runtime SET state=? WHERE task_id=?', (state, task_id))
                self.store.execute('UPDATE tasks SET status=? WHERE id=?', (state, task_id))
                self.control.event(self.goal_id, 'worker_stopped', {'state': state}, task_id)
                self.finish_queued(spec, {'status': state})
                raise
            except Exception as exc:
                self.store.execute("UPDATE task_runtime SET state='uncertain' WHERE task_id=?", (task_id,))
                self.store.execute("UPDATE tasks SET status='failed',error=? WHERE id=?", (str(exc), task_id))
                self.control.message(self.goal_id, 'blocker', {'error': str(exc)}, task_id, 'worker_to_manager')
                self.finish_queued(spec, {'status': 'uncertain', 'error': str(exc)})
            finally:
                self.work.pop(task_id, None)
        self.work[task_id] = asyncio.create_task(execute())
        self.control.event(self.goal_id, 'worker_started', {'kind': kind, 'spec': spec}, task_id)
        return task_id

    @staticmethod
    def repair_category(exc):
        diagnostic = str(exc).casefold()
        if type(exc).__name__ == 'ContextLimitError':
            return 'context_limit'
        if isinstance(exc, ModelBehaviorError) and any(word in diagnostic for word in ('json', 'schema', 'structured', 'validation', 'parse', 'output type')):
            return 'structured_output'
        if isinstance(exc, BudgetError) and 'http 400' in diagnostic and any(word in diagnostic for word in ('no tool call found', 'no tool output found', 'orphan')):
            return 'history_pairing'
        return None

    async def manager_decision(self):
        """At most one changed local repair, separately charged to autonomy.

        Provider failures, timeouts and dollar exhaustion are never replayed.
        Corrective requests use fresh current state and an explicit repair
        instruction; the failed request and any reservation remain untouched.
        """
        ticket = None
        snapshot = self.status()
        while True:
            try:
                decision = await asyncio.wait_for(self.brain.decide(snapshot), 300 if snapshot.get('audit_coverage') else 120)
            except (ModelBehaviorError, BudgetError) as exc:
                if ticket:
                    self.store.execute("UPDATE manager_repair_tickets SET state='failed',finished=? WHERE id=?", (time.time(), ticket))
                    self.control.event(self.goal_id, 'manager_repair_failed', {'ticket_id': ticket, 'error': type(exc).__name__, 'diagnostic': str(exc)[:2000]})
                    raise
                category = self.repair_category(exc)
                if not category or self.autonomy < 1:
                    raise
                current = self.control.current(self.goal_id)
                fingerprint = hashlib.sha256(json.dumps([category, current['goal']['revision'], current['policy']['version'], current['research_context']['version']]).encode()).hexdigest()
                ticket = uuid.uuid4().hex
                inserted = self.store.execute('''INSERT OR IGNORE INTO manager_repair_tickets
                    VALUES(?,?,?,?,?,'pending',?,NULL)''',
                    (ticket, self.goal_id, fingerprint, category, str(exc)[:2000], time.time()))
                if inserted.rowcount != 1:
                    raise
                self.autonomy -= 1
                repair = {'ticket_id': ticket, 'category': category, 'diagnostic': str(exc)[:2000],
                    'required_change': 'Build a smaller fresh decision from current durable state. Return one valid structured decision. Do not repeat prior tool calls or replay the failed request.',
                    'fresh_context': True}
                self.control.event(self.goal_id, 'manager_repair_started', repair)
                snapshot = self.status()
                snapshot['manager_repair'] = repair
            except BaseException:
                if ticket:
                    self.store.execute("UPDATE manager_repair_tickets SET state='interrupted',finished=? WHERE id=? AND state='pending'", (time.time(), ticket))
                raise
            else:
                if ticket:
                    self.store.execute("UPDATE manager_repair_tickets SET state='completed',finished=? WHERE id=?", (time.time(), ticket))
                    self.control.event(self.goal_id, 'manager_repair_completed', {'ticket_id': ticket})
                return decision

    async def advance(self, steps=None):
        async with self.actor:
            if steps is not None:
                if not 1 <= steps <= 1000:
                    raise ValueError('Autonomy budget must be 1..1000 decisions')
                self.autonomy = steps
            status = self.status()
            if status['goal']['status']=='completed':
                self.autonomy=0
                return {'completed':True}
            terminal_kinds = ('budget_guard_blocked', 'manager_budget_exhausted', 'manager_repair_interrupted')
            terminal = self.db.execute("SELECT kind,detail FROM activity WHERE goal_id=? AND kind IN ('budget_guard_blocked','manager_budget_exhausted','manager_repair_interrupted','decision','scheduled_decision','revision_changed') ORDER BY id DESC LIMIT 1", (self.goal_id,)).fetchone()
            if status['goal']['status']=='paused' and terminal and terminal['kind'] in terminal_kinds:
                self.autonomy=0
                return {'blocked': {'error': terminal['kind'], 'reason': 'A terminal budget or interrupted corrective request remains paused; apply a changed policy or requirements before continuing'}}
            if self.work or self.autonomy < 1 or self.closed:
                return {'waiting': bool(self.work)}
            # Finished local observation tasks can be inspected immediately.
            # This never restarts a role or retries its provider request, and
            # the in-memory-work fence above ensures its finalizer has run.
            local = self.db.execute('''SELECT r.task_id,r.spec FROM task_runtime r
                JOIN tasks t ON t.id=r.task_id WHERE t.goal_id=?
                AND t.adapter IN ('research','evaluation')
                AND r.state='uncertain' AND r.session_id IS NULL''', (self.goal_id,)).fetchall()
            for row in local:
                if row['task_id'] not in self.work and json.loads(row['spec']).get('job_id'):
                    await self.reconcile(row['task_id'])
            # Unknown external outcomes are a reconciliation requirement, not a retry signal.
            unresolved = self.db.execute("SELECT 1 FROM task_runtime WHERE state='uncertain' AND task_id IN (SELECT id FROM tasks WHERE goal_id=?)", (self.goal_id,)).fetchone()
            uncertain_delivery = self.db.execute("SELECT 1 FROM messages WHERE goal_id=? AND direction='manager_to_worker' AND state IN ('uncertain','sending')",(self.goal_id,)).fetchone()
            if unresolved or uncertain_delivery:
                self.autonomy = 0
                return {'blocked': 'Reconcile uncertain worker threads before further autonomous actions'}
            campaign = self.campaign()
            queued = campaign.claim_next() if campaign is not None else None
            if queued:
                self.autonomy -= 1
                decision = Decision(action='retry_research', rationale='Dispatch the next current preflighted campaign task without a manager call',
                    instructions=queued['instructions'], task_id=None, priority=queued['priority'], research_id=queued['job_id'])
                self.store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)', (self.goal_id, decision.model_dump_json()))
                self.control.event(self.goal_id, 'scheduled_decision', {'decision': decision.model_dump(), 'queue_id': queued['queue_id']})
                try:
                    return await self.dispatch(decision)
                except Exception as exc:
                    campaign.complete_job(queued['job_id'], {'status': 'failed', 'error': str(exc)})
                    raise
            self.autonomy -= 1
            try:
                decision = await self.manager_decision()
            except ModelBehaviorError as exc:
                self.autonomy=0
                self.store.execute("UPDATE goals SET status='paused' WHERE id=?",(self.goal_id,))
                reason={'error':type(exc).__name__,'reason':str(exc),
                        'next':'Inspect retained provider response and correct the structured decision; no action was dispatched'}
                self.control.message(self.goal_id,'manager_output_invalid',reason,direction='worker_to_manager')
                self.control.event(self.goal_id,'manager_output_invalid',reason)
                return {'blocked':reason}
            except BudgetError as exc:
                self.autonomy=0
                self.store.execute("UPDATE goals SET status='paused' WHERE id=?",(self.goal_id,))
                reason={'error':type(exc).__name__,'reason':str(exc),
                        'next':'Inspect durable dollar reservations and provider diagnostics before continuing'}
                self.control.message(self.goal_id,'budget_guard_blocked',reason,direction='worker_to_manager')
                self.control.event(self.goal_id,'budget_guard_blocked',reason)
                return {'blocked':reason}
            except (MaxTurnsExceeded,asyncio.TimeoutError) as exc:
                self.autonomy=0
                self.store.execute("UPDATE goals SET status='paused' WHERE id=?",(self.goal_id,))
                reason={'error':type(exc).__name__,'reason':'Manager reasoning budget exhausted; retained conversation/state require a changed approach, not automatic replay'}
                self.control.message(self.goal_id,'manager_budget_exhausted',reason,direction='worker_to_manager')
                self.control.event(self.goal_id,'manager_budget_exhausted',reason)
                return {'blocked':reason}
            self.store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)', (self.goal_id, decision.model_dump_json()))
            self.control.event(self.goal_id, 'decision', decision.model_dump())
            try:
                return await self.dispatch(decision)
            except (ValueError,FileNotFoundError) as exc:
                feedback={'action':decision.action,'error':str(exc),
                    'next':'Inspect available resources and correct the bounded plan; do not repeat the rejected arguments'}
                self.control.message(self.goal_id,'decision_rejected',feedback,direction='worker_to_manager')
                self.control.event(self.goal_id,'decision_rejected',feedback)
                repetitions=self.db.execute("SELECT count(*) FROM activity WHERE goal_id=? AND kind='decision_rejected' AND detail=?",(self.goal_id,json.dumps(feedback))).fetchone()[0]
                if repetitions>=3:
                    self.autonomy=0
                    self.store.execute("UPDATE goals SET status='paused' WHERE id=?",(self.goal_id,))
                return {'rejected':feedback,'escalated':repetitions>=3}

    async def dispatch(self, decision):
        action = decision.action
        if action in ('delegate','follow_up','implement'):
            assert_no_unmetered_worker(self.store, self.goal_id)
        if self.status()['goal']['requires_verified_implementation'] and action in ('delegate','follow_up'):
            raise ValueError('This code goal requires typed discover/research agents before authorized implementation; use list_resources directly for inventory, not legacy delegation')
        if action in ('pause','complete'):
            if action == 'complete':
                campaign = self.campaign()
                if campaign is not None:
                    queue = campaign.summary()['queue']
                    if queue['ready'] or queue['claimed']:
                        raise ValueError('Approved campaign research remains queued or active; dispatch it before completing the budgeted deliverable')
                if self.status().get('audit_coverage',{}):
                    from .audit import Audit
                    gaps = Audit(self.store,self.goal_id).completion_gaps()
                    if gaps:
                        raise ValueError(f'Audit remains incomplete: {len(gaps)} top-level areas lack independent source sampling; retain gaps and continue or pause within budget')
                # Implementation goals cannot complete on an agent's assertion alone.
                implementations = self.status()['implementations']
                revision = self.control.current(self.goal_id)['goal']['revision']
                audit = self.db.execute('SELECT 1 FROM completion_audits WHERE goal_id=? AND revision=? AND passed=1',(self.goal_id,revision)).fetchone()
                required=self.status()['goal']['requires_verified_implementation']
                if (implementations or required) and (not audit or not any(r['status'] == 'completed' for r in implementations)):
                    reason='An exact-revision verified implementation and goal audit are required; research or recommendations alone do not satisfy this goal'
                    self.control.message(self.goal_id,'completion_rejected',{'reason':reason},direction='worker_to_manager')
                    return {'blocked':reason}
            self.autonomy=0
            self.store.execute('UPDATE goals SET status=? WHERE id=?', ('completed' if action=='complete' else 'paused', self.goal_id))
            return decision.model_dump()
        if action == 'schedule_research':
            if not decision.research_batch:
                raise ValueError('Scheduling research requires at least one explicit bounded plan')
            from .scheduling import enqueue_batch
            result = enqueue_batch(self.store, self.goal_id, decision.research_batch,
                                   research=self.research, instructions=decision.instructions)
            self.control.event(self.goal_id, 'research_batch_scheduled', {'jobs': result['scheduled']})
            return result
        if action == 'reevaluate':
            job = self.research.job(decision.research_id)
            if job['goal_id'] != self.goal_id:
                raise ValueError('Research job belongs to another goal')
            async def review(task_id):
                return await self.research.reevaluate(job['id'], decision.instructions)
            return {'task_id': await self.submit('evaluation', {'job_id':job['id'], 'review_only':True}, review,
                json.loads(job['spec'])['budget']['seconds'] + 10)}
        if action in ('discover','research','retry_research'):
            if action == 'retry_research':
                job = self.research.job(decision.research_id)
                if job['goal_id'] != self.goal_id:
                    raise ValueError('Research job belongs to another goal')
                job_id = job['id']
            else:
                if decision.research_plan is None:
                    raise ValueError('Research needs an explicit bounded plan')
                plan = self.research.plan(self.goal_id, 'discovery' if action=='discover' else 'research', decision.research_plan)
                if plan['duplicate']:
                    self.control.message(self.goal_id, 'duplicate_research', plan, direction='worker_to_manager')
                    return plan
                job_id = plan['job_id']
            async def investigate(task_id):
                return await self.research.attempt(job_id, decision.instructions)
            return {'task_id': await self.submit('research', {'job_id': job_id}, investigate,
                json.loads(self.research.job(job_id)['spec'])['budget']['seconds'] * 2 + 10)}
        if action in ('implement','audit'):
            service = Implementations(self.store, self.runtime)
            row = service.get(decision.implementation_id)
            if row['goal_id'] != self.goal_id:
                raise ValueError('Implementation belongs to another goal')
            if action == 'audit':
                return service.audit(row['id'])
            async def implement(task_id):
                return await service.run(row['id'], task_id)
            return {'task_id': await self.submit('implementation', {'implementation_id': row['id']}, implement, 1800)}
        if action in ('delegate', 'follow_up'):
            session_id = None
            if action == 'follow_up':
                previous = self.store.task(decision.task_id)
                if previous['goal_id'] != self.goal_id:
                    raise ValueError('Task belongs to another goal')
                session_id = previous['session_id']
            async def run(task_id):
                thread_id, turn_id = await self.runtime.start(self.status()['goal']['workspace'], decision.instructions, session_id,
                    on_thread=lambda sid: self.store.execute('UPDATE task_runtime SET session_id=? WHERE task_id=?',(sid,task_id)),schema=Handoff.model_json_schema())
                self.store.execute('UPDATE task_runtime SET session_id=?,turn_id=? WHERE task_id=?', (thread_id,turn_id,task_id))
                self.store.execute('UPDATE tasks SET session_id=? WHERE id=?', (thread_id, task_id))
                result = await self.runtime.wait(thread_id, turn_id, 300)
                if result['status'] != 'completed':
                    raise RuntimeError('Worker turn failed or was interrupted; reconcile before retry')
                for item in reversed(result.get('items',[])):
                    if item.get('type')=='agentMessage':
                        try:
                            return Handoff.model_validate_json(item['text']).model_dump()
                        except (ValueError,KeyError):
                            continue
                raise RuntimeError('Worker returned no structured handoff; inspect its preserved session')
            repeats = self.db.execute("SELECT count(*) FROM tasks WHERE goal_id=? AND adapter='codex' AND instructions=? AND status IN ('failed','paused','cancelled')",(self.goal_id,json.dumps({'instructions':decision.instructions}))).fetchone()[0]
            if repeats>=2:
                self.autonomy=0
                self.control.event(self.goal_id,'approach_escalated',{'instructions':decision.instructions,'failures':repeats})
                return {'blocked':'Repeated unsuccessful approach; manager must change the hypothesis or plan'}
            return {'task_id': await self.submit('codex', {'instructions': decision.instructions}, run)}
        raise ValueError('Unsupported manager action')

    async def on_runtime_event(self, data):
        params = data.get('params', {})
        method = data.get('method','')
        if method == 'turn/started':
            self.store.execute('UPDATE task_runtime SET turn_id=? WHERE session_id=?',
                (params.get('turn',{}).get('id'),params.get('threadId')))
        row = self.db.execute('SELECT * FROM task_runtime WHERE session_id=? AND turn_id=?',
                              (params.get('threadId'),params.get('turnId') or params.get('turn',{}).get('id'))).fetchone()
        if row is None and params.get('threadId') and method=='thread/tokenUsage/updated':
            row=self.db.execute('SELECT * FROM task_runtime WHERE session_id=? ORDER BY started DESC LIMIT 1',(params['threadId'],)).fetchone()
        if row is None:
            return
        task_id = row['task_id']
        if method == 'item/tool/requestUserInput':
            params=dict(params,runtime_connection=getattr(self.runtime,'connection_id',self.owner))
            self.control.message(self.goal_id, 'question', params, task_id, 'worker_to_manager',
                message_id=f"rpc:{params['runtime_connection']}:{row['session_id']}:{data['id']}", runtime_id=str(data['id']))
            self.store.execute("UPDATE task_runtime SET state='waiting' WHERE task_id=?", (task_id,))
        elif 'requestApproval' in method:
            self.control.message(self.goal_id,'approval_denied',params,task_id,'worker_to_manager')
        elif method == 'item/completed':
            item = params.get('item', {})
            if item.get('type') in ('fileChange','commandExecution'):
                # Count new concrete results, not streaming text, tokens or process heartbeats.
                meaningful = {k:v for k,v in item.items() if k not in ('id','durationMs','processId')}
                digest = hashlib.sha256(json.dumps(meaningful, sort_keys=True).encode()).hexdigest()
                prior = self.db.execute('SELECT 1 FROM activity WHERE task_id=? AND kind=? AND detail=?',
                                        (task_id, 'evidence_progress', json.dumps({'digest': digest}))).fetchone()
                if not prior:
                    self.control.event(self.goal_id, 'evidence_progress', {'digest': digest}, task_id)
                    self.store.execute('UPDATE task_runtime SET last_progress=?,progress_count=progress_count+1 WHERE task_id=?',
                                       (time.time(),task_id))
        elif method == 'thread/tokenUsage/updated':
            # Notifications contain cumulative usage; persist them without summing duplicate totals.
            self.control.event(self.goal_id, 'runtime_usage_snapshot', params, task_id)

    async def deliver(self, message_id):
        message = self.db.execute('SELECT * FROM messages WHERE id=? AND goal_id=?', (message_id,self.goal_id)).fetchone()
        if message is None or message['state'] == 'acknowledged':
            return
        if message['state'] in ('uncertain','sending'):
            return {'blocked': 'Delivery outcome unknown; reconcile before resend'}
        row = self.db.execute('SELECT * FROM task_runtime WHERE task_id=?', (message['task_id'],)).fetchone()
        if row is None:
            raise ValueError('Message has no worker assignment')
        if message['kind'] == 'constraints_changed':
            # Restart the bounded role with new constraints after cancellation; never let old evidence pass a revision fence.
            self.store.execute("UPDATE messages SET state='sending' WHERE id=?", (message_id,))
            if row['session_id'] and row['turn_id']:
                try:
                    await self.runtime.steer(row['session_id'],row['turn_id'],json.dumps(json.loads(message['body'])),message_id)
                    await self.runtime.interrupt(row['session_id'],row['turn_id'])
                except Exception:
                    self.store.execute("UPDATE messages SET state='uncertain' WHERE id=?",(message_id,))
                    self.store.execute("UPDATE task_runtime SET state='uncertain' WHERE task_id=?",(row['task_id'],))
                    raise
            task = self.work.get(row['task_id'])
            if task:
                task.cancel()
            self.control.ack(message_id)
            return {'delivered': 'Constraint received; affected execution invalidated and stopped'}
        if not row['session_id']:
            return {'pending': 'Worker has no live runtime session; message retained for next bounded attempt'}
        self.store.execute("UPDATE messages SET state='sending' WHERE id=?", (message_id,))
        try:
            await self.runtime.steer(row['session_id'], row['turn_id'], message['body'], message_id)
        except Exception:
            self.store.execute("UPDATE messages SET state='uncertain' WHERE id=?", (message_id,))
            raise
        self.control.ack(message_id)
        return {'acknowledged': message_id}

    async def stop(self, task_id, pause=False):
        row = self.db.execute('SELECT * FROM task_runtime WHERE task_id=? AND task_id IN (SELECT id FROM tasks WHERE goal_id=?)',
                              (task_id,self.goal_id)).fetchone()
        if row is None:
            raise ValueError('Unknown task in this goal')
        state = 'paused' if pause else 'cancelled'
        self.store.execute('UPDATE task_runtime SET state=?,generation=generation+1 WHERE task_id=?', (state,task_id))
        if row['session_id'] and row['turn_id']:
            try:
                await self.runtime.interrupt(row['session_id'],row['turn_id'])
            except Exception as exc:
                self.store.execute("UPDATE task_runtime SET state='uncertain' WHERE task_id=?",(task_id,))
                self.control.message(self.goal_id,'interrupt_uncertain',{'error':str(exc)},task_id,'worker_to_manager')
                state='uncertain'
        task = self.work.get(task_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.control.event(self.goal_id, state, {}, task_id)
        return {'task_id': task_id, 'state': state, 'resume': 'Resume the saved session with follow_up; no OS-level process pause is promised'}

    async def monitor(self, now=None, stall_seconds=300):
        now = now or time.time()
        for row in self.db.execute("SELECT * FROM task_runtime WHERE state='running' AND task_id IN (SELECT id FROM tasks WHERE goal_id=?)", (self.goal_id,)).fetchall():
            spec = json.loads(row['spec'])
            progress = None
            last_meaningful = row['last_progress']
            adapter = self.db.execute('SELECT adapter FROM tasks WHERE id=?', (row['task_id'],)).fetchone()[0]
            if adapter == 'research' and spec.get('job_id') and not row['session_id']:
                progress = self.research.progress_assessment(spec['job_id'])
                if not progress.get('legacy_scope_only', True) and not progress.get('report_saved', False):
                    # Capturing more files and token heartbeats cannot keep a
                    # dependency-aware task alive indefinitely. Its checkpoint
                    # must close a new evidence obligation.
                    last_meaningful = progress['semantic_progress_at']
                    if now-last_meaningful > stall_seconds / 2:
                        detail = {'job_id': spec['job_id'], 'suggested_action': progress['suggested_action'],
                            'remaining_tool_calls': progress.get('remaining_tool_calls'),
                            'unresolved_obligations': progress.get('unresolved_obligations', []),
                            'semantic_progress_count': progress['semantic_progress_count']}
                        prior = self.db.execute("SELECT 1 FROM activity WHERE task_id=? AND kind='research_progress_guidance' AND detail=?", (row['task_id'], json.dumps(detail))).fetchone()
                        if not prior:
                            self.control.event(self.goal_id, 'research_progress_guidance', detail, row['task_id'])
                            self.control.message(self.goal_id, 'research_progress_guidance', detail, row['task_id'], 'worker_to_manager')
            if now > row['deadline'] or now-last_meaningful > stall_seconds:
                await self.stop(row['task_id'], pause=True)
                self.control.message(self.goal_id,'stalled',{'reason':'Deadline or meaningful-progress budget exhausted',
                    'progress': progress, 'next': 'Preserve evidence and revise the missing obligation or dependency before a targeted retry'},row['task_id'],'worker_to_manager')
        # waiting workers are not polled with model calls. External events/user answers wake them.

    async def reconcile(self, task_id):
        row = self.db.execute('SELECT * FROM task_runtime WHERE task_id=? AND task_id IN (SELECT id FROM tasks WHERE goal_id=?)', (task_id,self.goal_id)).fetchone()
        adapter = self.db.execute('SELECT adapter FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is not None and adapter and adapter[0] in ('research', 'evaluation') and not row['session_id'] and json.loads(row['spec']).get('job_id'):
            # SDK research is read-only local work, with no external thread to reconcile.
            # Its durable observations can be inspected without replaying a model call.
            if task_id in self.work:
                return {'blocked': 'Research is still active; stop it before reconciliation'}
            job_id = json.loads(row['spec'])['job_id']
            job = self.research.job(job_id)
            if job['goal_id'] != self.goal_id:
                raise ValueError('Research job belongs to another goal')
            runs = [dict(r) for r in self.db.execute('SELECT * FROM research_runs WHERE job_id=? ORDER BY created', (job_id,))]
            sources = [Knowledge(self.store).source(r[0]) for r in self.db.execute(
                'SELECT DISTINCT a.source_id FROM research_access a JOIN research_runs r ON r.id=a.run_id WHERE r.job_id=?', (job_id,))]
            latest = runs[-1] if runs else {}
            result = {'job_id': job_id, 'status': job['status'], 'runs': len(runs),
                'evidence': [{'id': s['id'], 'locator': s['locator'], 'stale': s['stale']} for s in sources],
                'report': json.loads(latest['report']) if latest.get('report') else None,
                'evaluation': json.loads(latest['evaluation']) if latest.get('evaluation') else None,
                'feedback': 'Local research reconciled without replay; retries require targeted feedback and remaining budget'}
            review = None
            if json.loads(row['spec']).get('review_only'):
                review = self.db.execute('SELECT id,status,evaluation,error FROM research_reviews WHERE job_id=? ORDER BY created DESC LIMIT 1',(job_id,)).fetchone()
                if review:
                    result['additional_review'] = dict(review)
                    result['status'] = review['status']
                    result['evaluation'] = json.loads(review['evaluation']) if review['evaluation'] else None
                    result['feedback'] = 'Additional review reconciled without replay; its one-review budget remains consumed'
            with self.db:
                self.db.execute("UPDATE task_runtime SET state='reconciled' WHERE task_id=?", (task_id,))
                self.db.execute("UPDATE tasks SET status=?,result=? WHERE id=?",
                    ('interrupted' if result['status'] == 'interrupted' else 'completed', json.dumps(result), task_id))
            self.control.message(self.goal_id, 'reconciled_outcome', result, task_id, 'worker_to_manager')
            self.control.event(self.goal_id, 'reconciliation', result, task_id)
            from .budget import Budget, budget_enabled
            if budget_enabled(self.store, self.goal_id):
                budget = Budget(self.store, self.goal_id)
                if budget.economical_policy():
                    for run in runs:
                        if run['status'] != 'running':
                            # This releases unused review protection, never a
                            # charged or uncertain provider request reservation.
                            budget.release_review(run['id'])
                    if review is not None and review['status'] != 'running':
                        budget.release_review(review['id'])
            self.finish_queued(json.loads(row['spec']), result)
            return result
        if row is None or not row['session_id']:
            return {'blocked':'No external session ID; inspect recorded artifacts manually before authorizing replacement'}
        result = await self.runtime.reconcile(row['session_id'])
        turns = result['thread'].get('turns', [])
        turn = next((t for t in turns if t['id'] == row['turn_id']), None)
        if turn and turn['status'] in ('completed','failed','interrupted'):
            self.store.execute("UPDATE task_runtime SET state='reconciled' WHERE task_id=?", (task_id,))
            self.control.message(self.goal_id,'reconciled_outcome',turn,task_id,'worker_to_manager')
            implementation_id=json.loads(row['spec']).get('implementation_id')
            if implementation_id:
                result['implementation']=await Implementations(self.store,self.runtime).reconcile(implementation_id,result)
        # Pending delivery is not replayed solely because the thread is reachable.
        self.control.event(self.goal_id,'reconciliation',result,task_id)
        return result

    async def chat(self, text):
        async with self.actor:
            message_id = self.control.message(self.goal_id, 'chat', {'text': text})
            session = self.control.session(self.goal_id)
            current = self.control.current(self.goal_id)
            agent = Agent(name='Engineering manager', model=self.brain.model if hasattr(self.brain,'model') else 'gpt-4.1',
                instructions=(ROOT / 'instructions/CHAT.md').read_text()+'\n'+current['policy']['instructions'], output_type=ChatReply)
            run_config = budget_run_config(self.store, self.goal_id, 'manager_chat', model=agent.model)
            from .manager_context import manager_chat_context, manager_history_filter
            from .manager import merge_manager_history
            run_config.call_model_input_filter = manager_history_filter
            run_config.session_input_callback = merge_manager_history
            try:
                result = await asyncio.wait_for(Runner.run(agent, json.dumps(manager_chat_context(self.status(), text, current['policy'])), session=session,
                    max_turns=4, run_config=run_config),120)
                from .telemetry import record_usage
                record_usage(self.store,self.goal_id,result,'manager_chat')
                reply = result.final_output
                self.control.ack(message_id)
                if reply.proposal_kind in ('policy','requirements'):
                    body = {'instructions':reply.proposal_text} if reply.proposal_kind=='policy' else {
                        'constraint':reply.proposal_text, 'add_requirements':reply.add_requirements}
                    body['affected_tasks'] = reply.affected_tasks or None
                    proposal_id = self.control.propose(self.goal_id,reply.proposal_kind,body)
                    return {'response':reply.response,'proposal':proposal_id,'proposed_change':body,
                            'next':f'/accept {proposal_id} to apply this durable instruction update'}
                if reply.proposal_kind in ('steer','cancel','pause'):
                    # Conversational control is targeted, recorded, and uses native runtime methods.
                    results = []
                    for task_id in reply.affected_tasks:
                        if reply.proposal_kind in ('cancel','pause'):
                            results.append(await self.stop(task_id,reply.proposal_kind=='pause'))
                        else:
                            mid = self.control.message(self.goal_id,'steering',{'instruction':reply.proposal_text},task_id,'manager_to_worker')
                            results.append(await self.deliver(mid))
                    return {'response':reply.response,'delivery':results}
                return {'response':reply.response}
            finally:
                await close_budget_run_config(run_config)
                session.close()

    async def accept(self, proposal_id):
        async with self.actor:
            result = self.control.accept(self.goal_id,proposal_id)
            for message in self.control.pending(self.goal_id):
                if message['kind'] == 'constraints_changed':
                    try:
                        await self.deliver(message['id'])
                    except Exception as exc:
                        self.control.event(self.goal_id,'delivery_uncertain',{'message':message['id'],'error':str(exc)})
            return result

    def resolve_delivery(self, message_id, delivered, evidence):
        if len(evidence.strip()) < 10:
            raise ValueError('Describe the reconciliation evidence before resolving delivery')
        row = self.db.execute("SELECT * FROM messages WHERE id=? AND goal_id=? AND state IN ('uncertain','sending','sent_no_ack')", (message_id,self.goal_id)).fetchone()
        if row is None:
            raise ValueError('Message is not awaiting reconciliation')
        if delivered:
            self.control.ack(message_id)
        else:
            self.store.execute("UPDATE messages SET state='pending' WHERE id=?", (message_id,))
        self.control.event(self.goal_id,'delivery_reconciled',{'message':message_id,'delivered':delivered,'evidence':evidence,'source':'explicit user reconciliation'})
        return {'message':message_id,'resolved':True}

    async def answer(self, question_id, answers):
        message = self.db.execute("SELECT * FROM messages WHERE id=? AND goal_id=? AND kind='question'", (question_id,self.goal_id)).fetchone()
        if message is None:
            raise ValueError('Unknown worker question')
        if message['state'] != 'pending':
            return {'blocked':'Answer already sent or outcome uncertain; do not replay'}
        if json.loads(message['body']).get('runtime_connection') != getattr(self.runtime,'connection_id',self.owner):
            raise RuntimeError('Question belongs to an earlier runtime; reconcile and resume instead of replaying its RPC response')
        row = self.db.execute('SELECT * FROM task_runtime WHERE task_id=?', (message['task_id'],)).fetchone()
        reply_id = self.control.message(self.goal_id,'answer',answers,message['task_id'],'manager_to_worker',runtime_id=message['runtime_id'])
        self.store.execute("UPDATE messages SET state='sending' WHERE id=?", (reply_id,))
        await self.runtime.answer(int(message['runtime_id']),answers)
        self.store.execute("UPDATE messages SET state='sent_no_ack' WHERE id=?", (reply_id,))
        self.control.ack(question_id)
        self.store.execute("UPDATE task_runtime SET state='running',last_progress=? WHERE task_id=?", (time.time(),message['task_id']))
        return {'sent':reply_id,'acknowledged':False}

    async def drive(self, steps=5, seconds=3600, linger=False):
        self.autonomy = steps
        deadline = time.monotonic()+seconds
        while not self.closed and time.monotonic()<deadline:
            await self.monitor()
            if not self.work:
                if self.autonomy < 1:
                    if not linger:
                        break
                else:
                    await self.advance()
            await asyncio.sleep(.2)
        return self.status()

    async def close(self):
        self.closed = True
        self.autonomy = 0
        if self.housekeeper:
            self.housekeeper.cancel()
            await asyncio.gather(self.housekeeper,return_exceptions=True)
        for task_id in list(self.work):
            await self.stop(task_id,pause=True)
        await self.runtime.close()
        if self._lock:
            self._lock.close()
