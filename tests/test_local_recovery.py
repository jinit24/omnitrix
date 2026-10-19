"""Same-owner failure injection; only local observations are auto-reconciled."""
import asyncio
import json

import pytest

from omnitrix.budget import Budget
from omnitrix.control import Control
from omnitrix.models import Decision
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor


class NoExternalRuntime:
    async def close(self):
        pass

    async def reconcile(self, thread):
        pytest.fail('Local automatic reconciliation must not contact a runtime')


class NoResearchCalls:
    async def run(self, role, context, tools):
        pytest.fail('Invalidated local preflight must not start or replay a role')


def research_decision(job_id):
    return Decision(action='retry_research', research_id=job_id, rationale='Inspect the planned source',
                    instructions='Read the declared source', task_id=None, priority=1)


def pause_decision():
    return Decision(action='pause', rationale='Replan the invalidated source', instructions='Preserve the failure evidence', task_id=None, priority=1)


@pytest.fixture
def system(tmp_path):
    (tmp_path / 'source.md').write_text('Queue interval is 30 seconds.\n')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Audit polling behavior', ['Use current evidence'], tmp_path)
    Control(store).initialize(goal)
    service = Research(store, NoResearchCalls())
    plan = ResearchPlan(scope=['source.md'], question='What is the polling interval?',
                        hypothesis='The polling interval is 30 seconds', falsification='The source specifies another interval')
    job = service.plan(goal, 'research', plan)['job_id']
    return store, goal, service, job, tmp_path


async def test_invalidated_local_preflight_reconciles_before_next_decision(system):
    store, goal, research, job, path = system
    budget = Budget.configure(store, goal, 25, {'fixture': {'input_per_million': 5, 'output_per_million': 15}})
    budget.reserve(json.dumps({'model': 'fixture', 'input': 'previous uncertain request', 'service_tier': 'default',
                               'max_output_tokens': 10}).encode(), 'research')
    before = budget.summary()

    class Brain:
        calls = 0

        async def decide(self, snapshot):
            self.calls += 1
            messages = snapshot['worker_messages']
            blocker = next(json.loads(message['body']) for message in messages if message['kind'] == 'blocker')
            outcome = next(json.loads(message['body']) for message in messages if message['kind'] == 'reconciled_outcome')
            assert blocker['error']
            assert outcome['job_id'] == job and outcome['runs'] == 0
            assert 'without replay' in outcome['feedback']
            return pause_decision()

    brain = Brain()
    supervisor = await Supervisor(store, goal, brain=brain, runtime=NoExternalRuntime(), research=research).open()
    try:
        dispatched = await supervisor.dispatch(research_decision(job))
        task = dispatched['task_id']
        # Invalidate the source between task creation and the attempt's final
        # read-only preflight, before any role/attempt dollar expenditure.
        (path / 'source.md').unlink()
        await asyncio.gather(supervisor.work[task])
        assert task not in supervisor.work
        assert store.db.execute('SELECT state FROM task_runtime WHERE task_id=?', (task,)).fetchone()[0] == 'uncertain'
        assert research.job(job)['attempts'] == 0

        result = await supervisor.advance(steps=1)
        assert result['action'] == 'pause' and brain.calls == 1
        assert store.db.execute('SELECT state FROM task_runtime WHERE task_id=?', (task,)).fetchone()[0] == 'reconciled'
        assert research.job(job)['attempts'] == 0
        after = budget.summary()
        assert after['committed_usd'] == before['committed_usd']
        assert after['requests'] == before['requests']
        assert after['unresolved_requests'] == before['unresolved_requests']
    finally:
        await supervisor.close()


async def test_active_local_task_cannot_be_reconciled_by_advance(system):
    store, goal, research, job, _ = system
    started = asyncio.Event()

    class Brain:
        async def decide(self, snapshot):
            pytest.fail('An active local task must not wake the manager')

    supervisor = await Supervisor(store, goal, brain=Brain(), runtime=NoExternalRuntime(), research=research).open()
    async def operation(task_id):
        started.set()
        await asyncio.Event().wait()
    try:
        task = await supervisor.submit('research', {'job_id': job}, operation)
        await started.wait()
        store.execute("UPDATE task_runtime SET state='uncertain' WHERE task_id=?", (task,))
        assert (await supervisor.advance(steps=1))['waiting']
        assert not store.db.execute("SELECT 1 FROM messages WHERE kind='reconciled_outcome'").fetchone()
    finally:
        await supervisor.close()
