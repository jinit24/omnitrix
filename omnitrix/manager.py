import fcntl
import json
import os
from agents import Agent, Runner, RunConfig, ModelSettings, function_tool
from .control import Control
from .knowledge import Knowledge
from .research import Research
from .implementation import Implementations
from .execution import Execution
from .models import Decision
from .workers import ROOT


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
    return output or value


class AgentBrain:
    """A conversational Agents SDK agent with durable context and recall tools."""
    def __init__(self, model=None, store=None):
        self.store = store
        self.model_override = model or os.getenv('OMNITRIX_MODEL')
        self.model = self.model_override or 'gpt-5.4'

    async def decide(self, snapshot):
        remaining_calls = 4

        def tools_available(context, agent):
            return remaining_calls > 0

        def spend_call():
            nonlocal remaining_calls
            if remaining_calls < 1:
                return False
            remaining_calls -= 1
            return True

        @function_tool(is_enabled=tools_available)
        async def recall_history(section: str, offset: int = 0) -> str:
            """Read tasks, decisions, messages or full research reports from durable history, 10 entries at a time."""
            if not spend_call():
                return 'Manager tool budget exhausted. Choose the next bounded action using observed evidence.'
            if offset<0:
                return 'Use a nonnegative offset.'
            if section=='messages' and self.store:
                return json.dumps([dict(r) for r in self.store.db.execute('SELECT kind,body,direction,created FROM messages WHERE goal_id=? ORDER BY created LIMIT 10 OFFSET ?',(snapshot['goal']['id'],offset))])
            if section=='research' and self.store:
                return json.dumps(self.store.research_history(snapshot['goal']['id'], offset=offset))
            if section not in ('tasks', 'decisions'):
                return 'Use tasks, decisions, messages, or research.'
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
            policy = '\nDurable manager policy:\n' + current['policy']['instructions']
            # Campaign prompts already contain a durable bounded state snapshot.
            # Replaying whole prior snapshots through an SDK session duplicates
            # tens of thousands of input tokens without adding new evidence.
            session = None if snapshot.get('campaign') else control.session(goal_id)
            @function_tool(is_enabled=tools_available)
            async def search_knowledge(query: str = '', offset: int = 0) -> str:
                """Retrieve shared verified facts, system maps, assumptions and rejected hypotheses."""
                if not spend_call():
                    return 'Manager tool budget exhausted. Choose the next bounded action using observed evidence.'
                return json.dumps(Knowledge(self.store).search(snapshot['goal']['workspace'], query, offset=offset))
            @function_tool(is_enabled=tools_available)
            async def list_resources(directory: str = '.', offset: int = 0) -> str:
                """List 40 sorted direct children with file kinds/sizes. Follow next_offset to inspect later pages. No recursive scan; choose explicit research sources."""
                if not spend_call():
                    return 'Manager tool budget exhausted. Choose the next bounded action using observed evidence.'
                from .resources import list_resources as inventory
                result = inventory(snapshot['goal']['workspace'], directory, offset)
                if snapshot.get('campaign'):
                    from .frontier import Frontier
                    result['indexed_files_in_subtree'] = Frontier(self.store,goal_id).page(prefix=directory,limit=20)
                control.event(goal_id,'manager_inventory',{'directory':directory,'offset':offset,'result':result})
                return json.dumps(result | {'remaining_manager_tool_calls': remaining_calls})
            tools += [search_knowledge, list_resources]
            if snapshot.get('campaign'):
                from .frontier import Frontier
                @function_tool(is_enabled=tools_available)
                async def search_frontier(query: str = '', kind: str | None = None, after: str = '', directory: str = '') -> str:
                    """Search file PATH NAMES only, never contents. directory filters a directory subtree, not a filename. Query a full path with directory=''. Use next_after for pages. To read/search contents, return a research plan with real file paths."""
                    if not spend_call():
                        return 'Manager tool budget exhausted. Choose the next bounded action using observed evidence.'
                    result=Frontier(self.store, goal_id).page(query, kind, after,prefix=directory)
                    control.event(goal_id,'manager_frontier_query',{'query':query,'kind':kind,'after':after,'directory':directory,'result':result})
                    return json.dumps(result)
                tools.append(search_frontier)
        from .spending import metered_model
        agent = Agent(
            name='Engineering manager', model=metered_model(self.store, snapshot['goal']['id'], 'manager', self.model_override),
            instructions=(ROOT / ('instructions/CAMPAIGN.md' if snapshot.get('campaign') else 'instructions/MANAGER.md')).read_text() + '\n' +
                         (ROOT / 'instructions/KNOWLEDGE.md').read_text() + policy,
            tools=tools, output_type=Decision,
            model_settings=ModelSettings(tool_choice='search_frontier' if self.store and snapshot.get('campaign')
                else 'list_resources' if self.store and not snapshot['research'] else 'auto'))
        # Stable objective + concise outcomes, never worker event streams.
        context = {**snapshot, 'tasks': snapshot['tasks'][-20:],
                   'decisions': snapshot['decisions'][-20:], 'manager_tool_budget': remaining_calls}
        if snapshot.get('campaign'):
            context['tasks']=[task | {'result':brief_result(task['result'])} for task in snapshot['tasks'][-6:]]
            context['decisions']=snapshot['decisions'][-8:]
            context['research']=snapshot['research'][:8]
            context['opportunities']=snapshot['opportunities'][:8]
            context['worker_messages']=[message | {'body':brief_result(message['body'])}
                for message in snapshot['worker_messages'][:8]]
            context['pending_messages']=[message | {'body':brief_result(message['body'])}
                for message in snapshot['pending_messages'][-10:]]
            context['manager_feedback']=snapshot['manager_feedback'][:5]
            context['context_window']='Recent 6 tasks, 8 decisions/jobs/opportunities, compact worker handoffs. Full original reports and older decisions remain in recall_history and search_knowledge; these excerpts are not complete coverage.'
        if self.store:
            from .resources import list_resources as inventory
            context['resource_inventory'] = inventory(snapshot['goal']['workspace'])
            if not snapshot.get('research_progress', {}).get('observed_sources'):
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
            from .telemetry import UsageRecorder
            result = await Runner.run(agent, json.dumps(context), max_turns=24 if snapshot.get('campaign') else 8, session=session,
                                      run_config=RunConfig(tracing_disabled=True),
                                      hooks=UsageRecorder(self.store, snapshot['goal']['id'], 'manager'))
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
                    if service.audit(row['id'])['passed']:
                        break
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
