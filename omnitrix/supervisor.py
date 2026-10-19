"""One manager actor, independent worker tasks, durable messages and result fences."""
import asyncio
import fcntl
import hashlib
import json
import time
import uuid
from agents import Agent, Runner, RunConfig
from agents.exceptions import MaxTurnsExceeded
from .control import Control
from .execution import Execution
from .implementation import Implementations
from .knowledge import Knowledge
from .manager import AgentBrain
from .models import ChatReply,Handoff
from .research import Research
from .runtime import CodexRuntime
from .workers import ROOT


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
        self.heartbeat_task = None

    async def open(self):
        self._lock = open(str(self.store.path)+'.lock', 'a')
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise RuntimeError('Another manager owns this database') from None
        try:
            self.control.initialize(self.goal_id)
            self.control.recover(self.goal_id)
            # Uncertain implementations must be inspected, never automatically reexecuted.
            self.store.execute("UPDATE implementations SET status='uncertain' WHERE goal_id=? AND status='running'", (self.goal_id,))
            self.control.heartbeat(self.goal_id, self.owner, start=True)
            self.heartbeat_task = asyncio.create_task(self.keep_presence())
            self.housekeeper = asyncio.create_task(self.housekeeping())
        except BaseException:
            self._lock.close()
            self._lock = None
            raise
        return self

    async def keep_presence(self):
        # Separate from inbox processing: a slow model reply must not hide a live owner.
        while not self.closed:
            self.control.heartbeat(self.goal_id, self.owner)
            await asyncio.sleep(2)

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
                self._check_control_revision(payload)
                if command=='status':
                    result=self.status()
                elif command=='chat':
                    result=await self.chat(text, expected=payload)
                elif command=='policy':
                    result={'proposal':self.control.propose(self.goal_id,'policy',{'instructions':text})}
                elif command=='requirements':
                    result={'proposal':self.control.propose(self.goal_id,'requirements',json.loads(text))}
                elif command=='accept':
                    result=await self.accept(text, expected=payload)
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

    def _check_control_revision(self, payload):
        if 'goal_revision' not in payload:
            return
        current = self.control.current(self.goal_id)
        if payload['goal_revision'] != current['goal']['revision'] or payload.get('policy_revision') != current['policy']['version']:
            raise ValueError('Queued command is stale after an instruction change; refresh and send again')

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
                if self.control.valid_result(task_id, generation):
                    with self.db:
                        terminal = 'uncertain' if isinstance(result,dict) and result.get('status')=='uncertain' else 'completed'
                        self.db.execute('UPDATE task_runtime SET state=?,last_progress=? WHERE task_id=?', (terminal,time.time(),task_id))
                        self.db.execute('UPDATE tasks SET status=?,result=? WHERE id=?', (terminal,json.dumps(result),task_id))
                    self.control.message(self.goal_id, 'findings', result, task_id, 'worker_to_manager')
                else:
                    self.control.message(self.goal_id, 'stale_result', result, task_id, 'worker_to_manager')
                self.control.event(self.goal_id, 'worker_finished', {'result': result}, task_id)
            except asyncio.CancelledError:
                row = self.db.execute('SELECT state FROM task_runtime WHERE task_id=?', (task_id,)).fetchone()
                state = row[0] if row and row[0] in ('paused','cancelled','invalidated') else 'uncertain'
                self.store.execute('UPDATE task_runtime SET state=? WHERE task_id=?', (state, task_id))
                self.store.execute('UPDATE tasks SET status=? WHERE id=?', (state, task_id))
                self.control.event(self.goal_id, 'worker_stopped', {'state': state}, task_id)
                raise
            except Exception as exc:
                self.store.execute("UPDATE task_runtime SET state='uncertain' WHERE task_id=?", (task_id,))
                self.store.execute("UPDATE tasks SET status='failed',error=? WHERE id=?", (str(exc), task_id))
                self.control.message(self.goal_id, 'blocker', {'error': str(exc)}, task_id, 'worker_to_manager')
            finally:
                self.work.pop(task_id, None)
        self.work[task_id] = asyncio.create_task(execute())
        self.control.event(self.goal_id, 'worker_started', {'kind': kind, 'spec': spec}, task_id)
        return task_id

    async def advance(self, steps=None):
        async with self.actor:
            if steps is not None:
                if not 1 <= steps <= 1000:
                    raise ValueError('Autonomy budget must be 1..1000 decisions')
                self.autonomy = steps
            if self.status()['goal']['status']=='completed':
                self.autonomy=0
                return {'completed':True}
            if self.work or self.autonomy < 1 or self.closed:
                return {'waiting': bool(self.work)}
            # Unknown external outcomes are a reconciliation requirement, not a retry signal.
            unresolved = self.db.execute("SELECT 1 FROM task_runtime WHERE state='uncertain' AND task_id IN (SELECT id FROM tasks WHERE goal_id=?)", (self.goal_id,)).fetchone()
            uncertain_delivery = self.db.execute("SELECT 1 FROM messages WHERE goal_id=? AND direction='manager_to_worker' AND state IN ('uncertain','sending')",(self.goal_id,)).fetchone()
            if unresolved or uncertain_delivery:
                self.autonomy = 0
                return {'blocked': 'Reconcile uncertain worker threads before further autonomous actions'}
            if self.db.execute('SELECT 1 FROM campaigns WHERE goal_id=?',(self.goal_id,)).fetchone():
                from .campaign import Campaign
                if not Campaign(self.store,self.goal_id).claim_decision():
                    self.autonomy=0
                    return {'blocked':'Saved campaign decision/state/deadline limit reached'}
            self.autonomy -= 1
            try:
                decision = await asyncio.wait_for(self.brain.decide(self.status()),120)
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
        if self.db.execute('SELECT 1 FROM campaigns WHERE goal_id=?',(self.goal_id,)).fetchone():
            from .campaign import Campaign
            Campaign(self.store,self.goal_id).validate_decision(decision)
        if self.db.execute('SELECT 1 FROM spending_limits WHERE goal_id=?',(self.goal_id,)).fetchone() and action in ('delegate','follow_up','implement','audit'):
            raise ValueError('Budgeted campaigns are read-only; unmetered native workers and code actions are disabled')
        if self.status()['goal']['requires_verified_implementation'] and action in ('delegate','follow_up'):
            raise ValueError('This code goal requires typed discover/research agents before authorized implementation; use list_resources directly for inventory, not legacy delegation')
        if action in ('pause','complete'):
            if action == 'complete':
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
                return await self.research.attempt(job_id, decision.instructions if action=='retry_research' else '')
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
            if now > row['deadline'] or now-row['last_progress'] > stall_seconds:
                await self.stop(row['task_id'], pause=True)
                self.control.message(self.goal_id,'stalled',{'reason':'Deadline or meaningful-progress budget exhausted'},row['task_id'],'worker_to_manager')
        # waiting workers are not polled with model calls. External events/user answers wake them.

    async def reconcile(self, task_id):
        row = self.db.execute('SELECT * FROM task_runtime WHERE task_id=? AND task_id IN (SELECT id FROM tasks WHERE goal_id=?)', (task_id,self.goal_id)).fetchone()
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

    async def chat(self, text, expected=None):
        async with self.actor:
            self._check_control_revision(expected or {})
            message_id = self.control.message(self.goal_id, 'chat', {'text': text})
            session = self.control.session(self.goal_id)
            current = self.control.current(self.goal_id)
            from .spending import metered_model
            agent = Agent(name='Engineering manager', model=metered_model(self.store,self.goal_id,'manager_chat',self.brain.model if hasattr(self.brain,'model') else 'gpt-4.1'),
                instructions=(ROOT / 'instructions/CHAT.md').read_text()+'\n'+current['policy']['instructions'], output_type=ChatReply)
            try:
                result = await asyncio.wait_for(Runner.run(agent, json.dumps({'user_message':text,
                    'state':self.status(),'policy':current['policy']}), session=session,
                    max_turns=4, run_config=RunConfig(tracing_disabled=True)),120)
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
                session.close()

    async def accept(self, proposal_id, expected=None):
        async with self.actor:
            self._check_control_revision(expected or {})
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
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            await asyncio.gather(self.heartbeat_task, return_exceptions=True)
        for task_id in list(self.work):
            await self.stop(task_id,pause=True)
        try:
            await self.runtime.close()
        finally:
            try:
                self.control.heartbeat(self.goal_id, self.owner, stopped=True)
            finally:
                if self._lock:
                    self._lock.close()
