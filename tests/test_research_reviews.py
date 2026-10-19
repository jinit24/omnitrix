import json

import pytest

from omnitrix.control import Control
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.store import Store
from tests_helpers import FixtureAgents


@pytest.fixture
def state(tmp_path):
    (tmp_path / 'source.md').write_text('Polling interval=30 seconds')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Research reliability', ['Preserve contracts'], tmp_path)
    spec = ResearchPlan(scope=['source.md'], question='What is the polling contract?',
        hypothesis='Polling interval is 30 seconds', falsification='Source specifies another interval')
    return store, goal, spec, tmp_path


class ReviewedAgents(FixtureAgents):
    def __init__(self, checkpoint=True):
        super().__init__()
        self.roles = []
        self.checkpoint = checkpoint

    async def run(self, role, context, tools):
        self.roles.append(role)
        if role == 'research' and self.checkpoint:
            evidence = tools.read('source.md')
            tools.checkpoint([evidence['id']], 'Impact remains unknown', 'Report static contract')
        result = await super().run(role, context, tools)
        if role == 'evaluation':
            result.evidential_verdict = 'supported'
            if 'review_kind' not in context:
                result.verdict = 'insufficient'
                result.workflow_issues = ['Original review lacked process context']
            else:
                assert context['review_feedback']
                assert context['process_attestation']['checkpoint_before_report'] == self.checkpoint
        return result


async def initial(state, agents=None, required=True):
    store, goal, spec, _ = state
    agents = agents or ReviewedAgents()
    service = Research(store, agents, require_checkpoint=required)
    job = service.plan(goal, 'research', spec)['job_id']
    assert (await service.attempt(job))['status'] == 'insufficient'
    return service, agents, job


async def test_additional_review_reuses_report_without_changing_original_outputs_or_attempts(state):
    store, goal, _, _ = state
    service, agents, job = await initial(state)
    original = dict(store.db.execute('SELECT * FROM research_runs').fetchone())
    result = await service.reevaluate(job, 'Use the actual recorded researcher checkpoint and portfolio')
    assert result['status'] == 'supported' and service.job(job)['status'] == 'supported'
    retained = dict(store.db.execute('SELECT * FROM research_runs').fetchone())
    assert retained['report'] == original['report']
    assert retained['evaluation'] == original['evaluation']
    assert retained['status'] == original['status']
    assert service.job(job)['attempts'] == 1
    assert agents.roles == ['research', 'evaluation', 'evaluation']
    assert store.db.execute('SELECT spent FROM research_limits WHERE goal_id=?', (goal,)).fetchone()[0] == 3
    review = store.db.execute('SELECT * FROM research_reviews').fetchone()
    assert json.loads(review['context'])['process_attestation']['checkpoint_before_report']
    assert review['status'] == 'supported' and review['finished']
    assert store.db.execute("SELECT 1 FROM knowledge WHERE classification='verified'").fetchone()
    with pytest.raises(ValueError):
        await service.reevaluate(job, 'Repeat the review')
    with pytest.raises(ValueError, match='cannot be reset'):
        await service.reevaluate(job, 'Increase the review limit', max_reviews=2)


async def test_reviewer_checkpoint_cannot_repair_missing_original_checkpoint(state):
    store, _, _, _ = state

    class Agents(ReviewedAgents):
        async def run(self, role, context, tools):
            result = await super().run(role, context, tools)
            if role == 'evaluation':
                tools.checkpoint(result.evidence_ids, 'Intent remains unknown', 'Finish evaluation')
            return result

    service, agents, job = await initial(state, Agents(checkpoint=False))
    # Reopening through the legacy default cannot waive the original requirement.
    reopened = Research(Store(store.path), agents)
    result = await reopened.reevaluate(job, 'Assess the retained report without rerunning research')
    assert result['status'] == 'insufficient'
    assert 'before the report' in result['evaluation']['feedback']
    assert service.job(job)['status'] == 'insufficient'
    assert not store.db.execute("SELECT 1 FROM knowledge WHERE classification='verified'").fetchone()
    with pytest.raises(ValueError, match='review budget exhausted'):
        await Research(Store(store.path), agents).reevaluate(job, 'Try another review after restart')


async def test_changed_policy_or_sources_require_fresh_research(state):
    store, goal, _, path = state
    service, agents, job = await initial(state)
    (path / 'source.md').write_text('Polling interval=10 seconds')
    with pytest.raises(ValueError, match='stale evidence'):
        await service.reevaluate(job, 'Review changed sources')
    (path / 'source.md').write_text('Polling interval=30 seconds')
    control = Control(store)
    proposal = control.propose(goal, 'policy', {'instructions': 'Require production measurements'})
    control.accept(goal, proposal)
    with pytest.raises(ValueError, match='revisions differ'):
        await service.reevaluate(job, 'Review under a different policy')
    assert agents.roles == ['research', 'evaluation']
    assert store.db.execute('SELECT count(*) FROM research_reviews').fetchone()[0] == 0


async def test_evaluation_budget_is_not_reset_by_additional_review(state):
    store, goal, _, _ = state
    agents = ReviewedAgents()
    Research(store).configure(goal, 2)
    service, _, job = await initial(state, agents)
    with pytest.raises(RuntimeError, match='budget exhausted'):
        await service.reevaluate(job, 'Use a review without a fresh role budget')
    assert agents.roles == ['research', 'evaluation']
    assert store.db.execute('SELECT count(*) FROM research_reviews').fetchone()[0] == 0
    assert store.db.execute('SELECT spent FROM research_limits').fetchone()[0] == 2


async def test_review_failure_is_durable_and_cannot_replay(state):
    store, _, _, _ = state

    class Agents(ReviewedAgents):
        async def run(self, role, context, tools):
            if 'review_kind' in context:
                raise RuntimeError('Evaluator unavailable')
            return await super().run(role, context, tools)

    service, agents, job = await initial(state, Agents())
    with pytest.raises(ValueError, match='targeted feedback'):
        await service.reevaluate(job, '')
    result = await service.reevaluate(job, 'Review the retained source findings with fixed process context')
    assert result['status'] == 'blocked'
    assert Store(store.path).db.execute('SELECT status FROM research_reviews').fetchone()[0] == 'blocked'
    with pytest.raises(ValueError, match='review budget exhausted'):
        await service.reevaluate(job, 'Replay failed evaluation')
    assert service.job(job)['attempts'] == 1
