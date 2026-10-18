import fcntl
import json
import os
from agents import Agent, Runner, RunConfig, function_tool
from .control import Control
from .knowledge import Knowledge
from .research import Research
from .implementation import Implementations
from .execution import Execution
from .models import Decision
from .workers import ROOT


class AgentBrain:
    """A conversational Agents SDK agent with durable context and recall tools."""
    def __init__(self, model=None, store=None):
        self.store = store
        self.model = model or os.getenv('OMNITRIX_MODEL', 'gpt-4.1')

    async def decide(self, snapshot):
        @function_tool
        def recall_history(section: str, offset: int = 0) -> str:
            """Read earlier tasks or decisions from durable history, 10 entries at a time."""
            if offset<0:
                return 'Use a nonnegative offset.'
            if section=='messages' and self.store:
                return json.dumps([dict(r) for r in self.store.db.execute('SELECT kind,body,direction,created FROM messages WHERE goal_id=? ORDER BY created LIMIT 10 OFFSET ?',(snapshot['goal']['id'],offset))])
            if section not in ('tasks', 'decisions'):
                return 'Use tasks, decisions, or messages.'
            return json.dumps(snapshot[section][offset:offset + 10])

        tools = [recall_history]
        policy = ''
        session = None
        if self.store:
            control = Control(self.store)
            goal_id = snapshot['goal']['id']
            current = control.current(goal_id)
            policy = '\nDurable manager policy:\n' + current['policy']['instructions']
            session = control.session(goal_id)
            @function_tool
            def search_knowledge(query: str = '', offset: int = 0) -> str:
                """Retrieve shared verified facts, system maps, assumptions and rejected hypotheses."""
                return json.dumps(Knowledge(self.store).search(snapshot['goal']['workspace'], query, offset=offset))
            @function_tool
            def list_resources(directory: str = '.') -> str:
                """List at most 40 direct children; no recursive scan. Select explicit bounded research scopes."""
                from pathlib import Path
                root = Path(snapshot['goal']['workspace']).resolve()
                path = (root / directory).resolve()
                if not path.is_relative_to(root) or not path.is_dir():
                    return 'Not a directory inside the workspace'
                import itertools
                return json.dumps([{'name': p.name, 'directory': p.is_dir()} for p in itertools.islice(path.iterdir(),40)
                                   if p.name not in ('.git','.omnitrix','.venv','node_modules')])
            tools += [search_knowledge, list_resources]
        agent = Agent(
            name='Engineering manager', model=self.model,
            instructions=(ROOT / 'instructions/MANAGER.md').read_text() + '\n' +
                         (ROOT / 'instructions/KNOWLEDGE.md').read_text() + policy,
            tools=tools, output_type=Decision)
        # Stable objective + concise outcomes, never worker event streams.
        context = {**snapshot, 'tasks': snapshot['tasks'][-20:],
                   'decisions': snapshot['decisions'][-20:]}
        try:
            result = await Runner.run(agent, json.dumps(context), max_turns=8, session=session,
                                      run_config=RunConfig(tracing_disabled=True))
            if self.store:
                from .telemetry import record_usage
                record_usage(self.store,snapshot['goal']['id'],result,'manager')
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
                    if decision.action=='complete' and self.store.snapshot(goal_id)['implementations']:
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
