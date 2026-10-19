import json

import pytest

from omnitrix.knowledge import Knowledge
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.store import Store
from tests_helpers import FixtureAgents


@pytest.fixture
def state(tmp_path):
    (tmp_path / 'source.md').write_text('Polling interval=30 seconds')
    (tmp_path / 'directory').mkdir()
    (tmp_path / 'large.txt').write_bytes(b'a' * 131073)
    (tmp_path / 'binary.txt').write_bytes(b'\xff\xfe')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Audit repository', ['Resolve bounded contracts'], tmp_path)
    return store, goal, tmp_path


@pytest.mark.parametrize('locator', ['directory', 'missing.md', 'large.txt', 'binary.txt', '../outside'])
async def test_unreadable_opportunity_scope_is_retained_as_nonactionable_lead(state, locator):
    store, goal, path = state

    class Leads(FixtureAgents):
        async def run(self, role, context, tools):
            report = await super().run(role, context, tools)
            if role == 'discovery':
                report.opportunities = [report.opportunities[0].model_copy(update={'scope': [locator]})]
            return report

    service = Research(store, Leads())
    job = service.plan(goal, 'discovery', ResearchPlan(scope=['source.md'], question='Which uncertainty merits focused research?'))['job_id']
    outcome = await service.attempt(job)
    assert outcome['status'] == 'discovered' and outcome['report']['opportunities']
    lead = Knowledge(store).backlog(goal)[0]
    assert lead['status'] == 'needs_scope' and lead['score'] == 0
    assert not lead['spec']['actionable'] and lead['spec']['confidence'] == 0
    assert locator in lead['spec']['scope_gaps'][0]
    assert 'navigation' in lead['spec']['scope_gaps'][0]
    saved = store.db.execute('SELECT report FROM research_runs WHERE job_id=?', (job,)).fetchone()[0]
    assert json.loads(saved)['opportunities'][0]['scope'] == [locator]


async def test_discovery_declared_missing_test_gap_is_not_silently_waived(state):
    store, goal, _ = state

    class Leads(FixtureAgents):
        async def run(self, role, context, tools):
            report = await super().run(role, context, tools)
            if role == 'discovery':
                report.opportunities = [report.opportunities[0].model_copy(update={
                    'actionable': False, 'scope_gaps': ['Locate tests that establish the cancellation contract']})]
            return report

    service = Research(store, Leads())
    job = service.plan(goal, 'discovery', ResearchPlan(scope=['source.md'], question='Which uncertainty merits focused research?'))['job_id']
    await service.attempt(job)
    lead = Knowledge(store).backlog(goal)[0]
    assert lead['status'] == 'needs_scope'
    assert lead['spec']['scope_gaps'] == ['Locate tests that establish the cancellation contract']


async def test_resolved_scope_reopens_same_lead_without_duplication(state):
    store, goal, _ = state

    class Leads(FixtureAgents):
        locator = 'directory'

        async def run(self, role, context, tools):
            report = await super().run(role, context, tools)
            if role == 'discovery':
                report.opportunities = [report.opportunities[0].model_copy(update={'scope': [self.locator]})]
            return report

    agents = Leads()
    service = Research(store, agents)
    spec = ResearchPlan(scope=['source.md'], question='Which uncertainty merits focused research?')
    job = service.plan(goal, 'discovery', spec)['job_id']
    await service.attempt(job)
    original = Knowledge(store).backlog(goal)[0]
    agents.locator = 'source.md'
    next_job = service.plan(goal, 'discovery', spec.model_copy(update={'question': 'Which uncertainty merits a different focused investigation?'}))['job_id']
    await service.attempt(next_job)
    backlog = Knowledge(store).backlog(goal)
    assert len(backlog) == 1 and backlog[0]['id'] == original['id']
    assert backlog[0]['status'] == 'open' and backlog[0]['score'] > 0
    assert backlog[0]['spec']['actionable'] and not backlog[0]['spec']['scope_gaps']
