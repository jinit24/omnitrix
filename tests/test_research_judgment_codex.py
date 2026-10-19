"""Regression coverage for research judgment revision races."""
import asyncio

from omnitrix.control import Control
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.store import Store
from tests_helpers import FixtureAgents


async def test_requirements_change_during_evaluation_returns_stale(tmp_path):
    (tmp_path / 'system.md').write_text('Queue polling interval=30 seconds.\n')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Investigate queue latency', ['Preserve delivery'], tmp_path)
    evaluation_started = asyncio.Event()
    release_evaluation = asyncio.Event()

    class PausedEvaluationAgents(FixtureAgents):
        async def run(self, role, context, tools):
            if role == 'evaluation':
                assert context['goal']['original_requirements'] == ['Preserve delivery']
                assert context['goal']['requirements'] == ['Preserve delivery']
                assert context['goal']['revision'] == 1
                evaluation_started.set()
                await release_evaluation.wait()
            return await super().run(role, context, tools)

    service = Research(store, PausedEvaluationAgents())
    plan = ResearchPlan(scope=['system.md'], question='Does polling cause the stated delay?',
        hypothesis='Queue polling interval is 30 seconds', falsification='Interval is not 30 seconds')
    job = service.plan(goal, 'research', plan)['job_id']

    attempt_task = asyncio.create_task(service.attempt(job))
    await asyncio.wait_for(evaluation_started.wait(), timeout=2)
    control = Control(store)
    proposal = control.propose(goal, 'requirements', {
        'constraint': 'No production changes', 'add_requirements': ['Bound investigation time']})
    control.accept(goal, proposal)
    release_evaluation.set()
    result = await asyncio.wait_for(attempt_task, timeout=2)

    assert result['status'] == 'stale'
    assert result['evaluation'] is None
    assert result['feedback'] == 'Revision changed during evaluation'
    assert not store.db.execute(
        "SELECT 1 FROM knowledge WHERE job_id=? AND classification='verified'", (job,)
    ).fetchone()
