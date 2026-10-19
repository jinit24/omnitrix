import asyncio
import fcntl
import json
import os
import time
from agents import Agent, Runner, RunConfig, ModelSettings, function_tool
from pydantic import create_model
from typing import Literal
from .control import Control
from .knowledge import Knowledge
from .research import Research
from .implementation import Implementations
from .execution import Execution
from .models import Decision
from .prompts import load_instructions


def brief_result(value):
    """A recallable worker handoff, without replaying every report twice."""
    if isinstance(value,str):
        try:
            value=json.loads(value)
        except ValueError:
            return value[:1500]
    if not isinstance(value,dict):
        return value
    output={key:value[key] for key in ('job_id','run_id','status','feedback','error') if key in value}
    if isinstance(value.get('evaluation'),dict):
        output['evaluation']={key:val[:1200] if isinstance(val,str) else val for key,val in value['evaluation'].items()}
    report=value.get('report')
    if isinstance(report,dict):
        output['report_summary']={key:report[key] for key in ('blocked','uncertainties','gaps') if key in report}
        output['report_summary']['finding_count']=len(report.get('findings',[]))
        output['report_summary']['findings']=[{key:val[:700] if isinstance(val,str) else val for key,val in finding.items()}
            for finding in report.get('findings',[])[:6]]
        output['report_summary']['system_map_names']=[entry['name'] for entry in report.get('system_map',[])[:12]]
        output['report_summary']['opportunity_titles']=[entry['title'] for entry in report.get('opportunities',[])[:12]]
    return output or {key:value[key] for key in ('summary','status','candidate','id','unresolved_concerns') if key in value}


def compact_snapshot(snapshot, store=None):
    """One current state, with complete criteria and recallable bounded history."""
    context = {**snapshot}
    context['tasks'] = [{k: t[k] for k in ('id','adapter','status','error','session_id') if k in t}
                        | {'result': brief_result(t.get('result'))} for t in snapshot['tasks'][-6:]]
    context['decisions'] = snapshot['decisions'][-8:]
    context['research'] = snapshot['research'][:8]
    context['opportunities'] = snapshot['opportunities'][:8]
    context['manager_feedback'] = snapshot.get('manager_feedback', [])[:5]
    # Findings already occur in task results. Keep questions/blockers, and exactly
    # one full implementation handoff for the final review (not every past attempt).
    messages = snapshot.get('worker_messages', [])
    latest = next((m for m in messages if m['kind'] == 'implementation_handoff'), None)
    context['worker_messages'] = ([latest] if latest else []) + [
        m for m in messages if m['kind'] in ('question','blocker')][:5]
    context['pending_messages'] = [m for m in snapshot.get('pending_messages', [])
                                    if m['kind'] != 'findings'][-10:]
    context['active'] = [{k: r[k] for k in ('task_id','state','deadline','goal_revision','policy_revision')}
                         for r in snapshot.get('active', [])]
    context['implementations'] = snapshot.get('implementations', [])[-8:]
    context['context_window'] = ('Recent 6 tasks, 8 decisions/jobs/opportunities/implementations; '
        'one latest implementation report. History is incomplete: recall_history and recall_artifact '
        'retrieve original records. Mandatory goal criteria and policy are never summarized away.')
    if store:
        goal_id = snapshot['goal']['id']
        current = Control(store).current(goal_id)
        context['policy_revision'] = current['policy']['version']
        context['dialogue'] = [dict(r) for r in store.db.execute(
            "SELECT kind,body FROM messages WHERE goal_id=? AND kind IN ('chat','chat_reply') ORDER BY created DESC LIMIT 12",
            (goal_id,))][::-1]
        # Explain readiness from actual authorization checks rather than making
        # the model infer missing permission from an unrelated research verdict.
        service = Implementations(store)
        context['implementation_readiness'] = []
        for row in context['implementations']:
            try:
                _, spec = service.auth(row['authorization'])
                readiness = {'authorized': True, 'basis': spec.basis}
            except ValueError as exc:
                readiness = {'authorized': False, 'blocker': str(exc)}
            context['implementation_readiness'].append({'id': row['id'], 'status': row['status'], **readiness})
        ids = [row['id'] for row in context['implementations']]
        latest_handoff = store.db.execute("SELECT kind,body,task_id,created FROM messages WHERE goal_id=? AND kind='implementation_handoff' ORDER BY created DESC LIMIT 1", (goal_id,)).fetchone()
        context['worker_messages'] = ([dict(latest_handoff)] if latest_handoff else []) + [
            m for m in messages if m['kind'] in ('question','blocker')][:5]
        context['verification'] = [dict(r) for r in store.db.execute(
            '''SELECT v.implementation_id,v.candidate,v.requirement,v.passed,v.exit_code,v.artifact,v.failure_kind
               FROM verifications v JOIN implementations i ON i.id=v.implementation_id
               WHERE i.goal_id=? AND v.candidate=i.candidate AND v.goal_revision=? ORDER BY v.created DESC LIMIT 60''',
            (goal_id, current['goal']['revision'])) if r['implementation_id'] in ids]
    return context


def available_actions(snapshot):
    """Constrain the model's action enum before it can invent an executable ID."""
    actions = ['discover','research','research_batch','pause']
    if any(r['status'] in ('pending','insufficient','blocked','interrupted') for r in snapshot['research']):
        actions.append('retry_research')
    campaign = snapshot.get('campaign') or snapshot.get('spending')
    code_goal = snapshot['goal'].get('requires_verified_implementation') or snapshot.get('implementations')
    if not campaign:
        ready = snapshot.get('implementation_readiness', [])
        if any(r['authorized'] and r['status'] in ('authorized','repair') for r in ready):
            actions.append('implement')
        if any(r['authorized'] and r['status']=='ready_for_audit' for r in ready):
            actions.append('audit')
        if not code_goal:
            actions.append('delegate')
            if any(t.get('session_id') for t in snapshot['tasks']):
                actions.append('follow_up')
        if not code_goal or any(r['authorized'] and r['status']=='completed' for r in ready):
            actions.append('complete')
    else:
        # Campaign completion is still checked against its durable breadth gates.
        actions.append('complete')
    return actions


class AgentBrain:
    """A conversational Agents SDK agent with durable context and recall tools."""
    def __init__(self, model=None, store=None):
        self.store = store
        self.model = model or os.getenv('OMNITRIX_MODEL', 'gpt-4.1')

    async def decide(self, snapshot):
        @function_tool
        async def recall_history(section: str, offset: int = 0) -> str:
            """Read earlier tasks or decisions from durable history, 10 entries at a time."""
            if offset<0:
                return 'Use a nonnegative offset.'
            if section=='messages' and self.store:
                return json.dumps([dict(r) for r in self.store.db.execute('SELECT kind,body,direction,created FROM messages WHERE goal_id=? ORDER BY created LIMIT 10 OFFSET ?',(snapshot['goal']['id'],offset))])
            if section not in ('tasks', 'decisions'):
                return 'Use tasks, decisions, or messages.'
            if self.store:
                if section == 'tasks':
                    return json.dumps([dict(r) for r in self.store.db.execute('SELECT * FROM tasks WHERE goal_id=? ORDER BY rowid LIMIT 10 OFFSET ?', (snapshot['goal']['id'], offset))])
                return json.dumps([json.loads(r[0]) for r in self.store.db.execute('SELECT body FROM decisions WHERE goal_id=? ORDER BY id LIMIT 10 OFFSET ?', (snapshot['goal']['id'],offset))])
            return json.dumps(snapshot[section][offset:offset + 10])

        tools = [recall_history]
        policy = ''
        session = None
        if self.store:
            control = Control(self.store)
            goal_id = snapshot['goal']['id']
            current = control.current(goal_id)
            policy = current['policy']['instructions']
            # Campaign prompts already contain a durable bounded state snapshot.
            # Replaying whole prior snapshots through an SDK session duplicates
            # tens of thousands of input tokens without adding new evidence.
            session = None  # State is supplied once; durable dialogue is retrieved separately.
            @function_tool
            async def search_knowledge(query: str = '', offset: int = 0) -> str:
                """Retrieve shared verified facts, system maps, assumptions and rejected hypotheses."""
                return json.dumps(Knowledge(self.store).search(snapshot['goal']['workspace'], query, offset=offset))
            @function_tool
            async def list_resources(directory: str = '.', offset: int = 0) -> str:
                """List 40 sorted direct children with file kinds/sizes. Follow next_offset to inspect later pages. No recursive scan; choose explicit research sources."""
                from .resources import list_resources as inventory
                result = inventory(snapshot['goal']['workspace'], directory, offset)
                if snapshot.get('campaign'):
                    from .frontier import Frontier
                    result['indexed_files_in_subtree'] = Frontier(self.store,goal_id).page(prefix=directory,limit=20)
                control.event(goal_id,'manager_inventory',{'directory':directory,'offset':offset,'result':result})
                return json.dumps(result)
            tools += [search_knowledge, list_resources]
            @function_tool
            async def recall_artifact(kind: str, artifact_id: str) -> str:
                """Read a full task, research report/evaluation, or implementation with verification by ID. Goal scoped."""
                if kind == 'task':
                    row = self.store.db.execute('SELECT * FROM tasks WHERE id=? AND goal_id=?', (artifact_id,goal_id)).fetchone()
                    return json.dumps(dict(row) if row else {'error':'Unknown task for this goal'})
                if kind == 'research':
                    rows = self.store.db.execute('SELECT r.* FROM research_runs r JOIN research_jobs j ON j.id=r.job_id WHERE j.id=? AND j.goal_id=? ORDER BY r.created DESC LIMIT 3', (artifact_id,goal_id)).fetchall()
                    return json.dumps([dict(r) for r in rows])
                if kind == 'implementation':
                    row = self.store.db.execute('SELECT * FROM implementations WHERE id=? AND goal_id=?', (artifact_id,goal_id)).fetchone()
                    if not row:
                        return json.dumps({'error':'Unknown implementation for this goal'})
                    reports = [json.loads(r[0]) for r in self.store.db.execute("SELECT body FROM messages WHERE goal_id=? AND kind='implementation_handoff' AND json_extract(body,'$.implementation_id')=? ORDER BY created DESC LIMIT 3", (goal_id,artifact_id))]
                    return json.dumps({'implementation':dict(row), 'reports':reports,
                        'verification':[dict(r) for r in self.store.db.execute('SELECT * FROM verifications WHERE implementation_id=?', (artifact_id,))]})
                return 'Use task, research, or implementation.'
            tools.append(recall_artifact)
            if snapshot.get('campaign'):
                from .frontier import Frontier
                @function_tool
                async def search_frontier(query: str = '', kind: str | None = None, after: str = '', directory: str = '') -> str:
                    """Search file PATH NAMES only, never contents. directory filters a directory subtree, not a filename. Query a full path with directory=''. Use next_after for pages. To read/search contents, return a research plan with real file paths."""
                    result=Frontier(self.store, goal_id).page(query, kind, after,prefix=directory)
                    control.event(goal_id,'manager_frontier_query',{'query':query,'kind':kind,'after':after,'directory':directory,'result':result})
                    return json.dumps(result)
                tools.append(search_frontier)
        context = compact_snapshot(snapshot, self.store)
        context['available_actions'] = available_actions(context)
        output_type = create_model('AvailableDecision', __base__=Decision,
            action=(Literal[tuple(context['available_actions'])], ...)) if self.store else Decision
        from .spending import metered_model
        agent = Agent(
            name='Engineering manager', model=metered_model(self.store, snapshot['goal']['id'], 'manager', self.model),
            instructions=load_instructions('campaign' if snapshot.get('campaign') else 'manager', policy),
            tools=tools, output_type=output_type,
            model_settings=ModelSettings(tool_choice='search_frontier' if self.store and snapshot.get('campaign')
                else 'auto'))
        # Stable objective + concise outcomes, never worker event streams.
        if self.store:
            from .resources import list_resources as inventory
            context['resource_inventory'] = inventory(snapshot['goal']['workspace'])
            if not snapshot.get('implementations') and not snapshot.get('research_progress', {}).get('observed_sources'):
                # Bootstrap only metadata, one level deep. This prevents a worker
                # task merely to discover valid source paths and remains bounded.
                directories = context['resource_inventory']['subdirectories']
                context['initial_directory_pages'] = [inventory(snapshot['goal']['workspace'], directory)
                                                       for directory in directories[:8]]
                context['unlisted_initial_directories'] = directories[8:]
            if snapshot.get('campaign'):
                from .frontier import Frontier
                context['frontier'] = Frontier(self.store, goal_id).summary()
                context['unexamined_branches'] = Frontier(self.store, goal_id).suggestions()
                from .campaign import Campaign
                context['campaign_evidence_requirements'] = Campaign(self.store,goal_id).quality()
        try:
            started = time.monotonic()
            result = await Runner.run(agent, json.dumps(context), max_turns=24 if snapshot.get('campaign') else 8, session=session,
                                      run_config=RunConfig(tracing_disabled=True))
            if self.store:
                from .telemetry import record_usage
                record_usage(self.store,snapshot['goal']['id'],result,'manager')
                control.event(goal_id,'manager_turn',{'seconds':time.monotonic()-started,
                    'context_bytes':len(json.dumps(context).encode()),'action':result.final_output.action})
            return result.final_output
        finally:
            if session:
                session.close()


class Manager:
    def __init__(self, store, brain, worker):
        self.store, self.brain, self.worker = store, brain, worker

    async def resume(self, goal_id, steps=5, message=None):
        if steps < 1:
            raise ValueError('steps must be positive')
        # One owner per database, including separate CLI processes. A crash releases it.
        with open(str(self.store.path) + '.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Another manager owns this database') from None
            return await self._resume(goal_id, steps, message)

    async def _resume(self, goal_id, steps, message):
        if self.store.db.execute('SELECT 1 FROM campaigns WHERE goal_id=?',(goal_id,)).fetchone():
            raise ValueError('Use the campaign owner for this goal; legacy managers cannot bypass saved campaign controls')
        snapshot = self.store.snapshot(goal_id)
        if snapshot['goal']['status'] == 'completed':
            return snapshot
        # No other manager can own the database while this lock is held.
        self.store.execute("UPDATE attempts SET status='interrupted' WHERE status='running'")
        self.store.execute("UPDATE tasks SET status='interrupted' WHERE status='running'")
        if message:
            self.store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',
                               (goal_id, json.dumps({'user_message': message})))
        self.store.execute("UPDATE goals SET status='active' WHERE id=?", (goal_id,))
        for _ in range(steps):
            decision = await self.brain.decide(self.store.snapshot(goal_id))
            if decision.action == 'research_batch':
                research = Research(self.store)
                plans = research.plan_batch(goal_id, decision.research_plans, decision.independence_reason)
                outcomes = await asyncio.gather(*(research.attempt(p['job_id']) for p in plans if not p['duplicate']))
                self.store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',
                    (goal_id,json.dumps({'decision':decision.model_dump(),'research_outcomes':outcomes})))
                continue
            if decision.action in ('discover', 'research', 'retry_research'):
                research = Research(self.store)
                if decision.action == 'retry_research':
                    job = research.job(decision.research_id)
                    if job['goal_id'] != goal_id:
                        raise ValueError('Research job belongs to another goal')
                    outcome = await research.attempt(job['id'], decision.instructions)
                else:
                    if decision.research_plan is None:
                        raise ValueError('Research action requires a bounded plan')
                    planned = research.plan(goal_id, 'discovery' if decision.action == 'discover' else 'research', decision.research_plan)
                    outcome = planned if planned['duplicate'] else await research.attempt(planned['job_id'])
                self.store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',
                    (goal_id, json.dumps({'decision': decision.model_dump(), 'research_outcome': outcome})))
                continue
            if decision.action in ('implement', 'audit'):
                service = Implementations(self.store)
                row = service.get(decision.implementation_id)
                if row['goal_id'] != goal_id:
                    raise ValueError('Implementation belongs to another goal')
                if decision.action == 'implement':
                    await service.run(row['id'])
                else:
                    service.audit(row['id'])
                continue
            # Validate references before recording any executable assignment.
            task = None
            if decision.action == 'follow_up':
                task = self.store.task(decision.task_id)
                if task['goal_id'] != goal_id or task['adapter'] != self.worker.name:
                    raise ValueError('Follow-up must belong to this goal and adapter')
            if decision.action in ('delegate', 'follow_up') and not decision.instructions.strip():
                raise ValueError('Delegation requires bounded instructions')
            with self.store.db:
                self.store.db.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',
                                      (goal_id, decision.model_dump_json()))
                if decision.action in ('complete', 'pause'):
                    if decision.action=='complete' and (self.store.snapshot(goal_id)['implementations'] or self.store.snapshot(goal_id)['goal']['requires_verified_implementation']):
                        current = Control(self.store).current(goal_id)
                        if not self.store.db.execute('SELECT 1 FROM completion_audits WHERE goal_id=? AND revision=? AND passed=1',(goal_id,current['goal']['revision'])).fetchone():
                            raise ValueError('Completion requires a current exact-revision goal audit')
                    status = 'completed' if decision.action == 'complete' else 'paused'
                    self.store.db.execute('UPDATE goals SET status=? WHERE id=?', (status, goal_id))
                    break
                execution = Execution(self.store, self.worker)
                if task is None:
                    task_id = execution.assign(goal_id, decision.instructions, decision.priority)
                else:
                    task_id = task['id']
            try:
                await execution.send(task_id, decision.instructions, snapshot['goal']['workspace'])
            except Exception:
                # The next agent turn sees the persisted failure and decides what to do.
                pass
        return self.store.snapshot(goal_id)
