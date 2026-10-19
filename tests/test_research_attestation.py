import json

import pytest

from omnitrix.audit import Audit
from omnitrix.knowledge import Knowledge
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.store import Store
from tests_helpers import FixtureAgents


@pytest.fixture
def state(tmp_path):
    (tmp_path / 'source.md').write_text('Polling interval=30 seconds')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Audit reliability', ['Preserve contracts'], tmp_path)
    spec = ResearchPlan(scope=['source.md'], question='What is the polling contract?',
        hypothesis='Polling interval is 30 seconds', falsification='Source specifies another interval')
    return store, goal, spec, tmp_path


async def test_researcher_checkpoint_and_portfolio_are_durable_reviewer_inputs(state):
    store, goal, spec, path = state
    Audit(store, goal).initialize([{'path': 'pkg', 'files': 10, 'bytes': 1000, 'kind': 'directory'}])
    (path / 'MANIFEST.json').write_text(json.dumps({'repository': 'example', 'commit': 'pinned',
        'inventory_files': 10, 'exported_text_files': 8,
        'exclusions': [{'path': 'binary', 'reason': 'binary'}, {'path': 'link', 'reason': 'symlink'}]}))

    class Agents(FixtureAgents):
        async def run(self, role, context, tools):
            assert context['portfolio']['manifest']['excluded_files'] == 2
            assert context['portfolio']['manifest']['exclusion_reasons'] == {'binary': 1, 'symlink': 1}
            assert context['portfolio']['audit_agenda'][0]['path'] == 'pkg'
            assert context['process_requirements']['researcher_checkpoint_before_report']
            if role == 'research':
                source = tools.read('source.md')
                tools.checkpoint([source['id']], 'Production consequences unknown', 'Report bounded contract')
            if role == 'evaluation':
                assert context['process_attestation']['checkpoint_before_report']
                saved = context['researcher_checkpoints'][0]
                assert saved['current_citations']
                assert saved['created'] <= context['process_attestation']['report_saved_at']
            return await super().run(role, context, tools)

    service = Research(store, Agents(), require_checkpoint=True)
    job = service.plan(goal, 'research', spec)['job_id']
    result = await service.attempt(job)
    assert result['status'] == 'supported'
    reopened = Store(store.path)
    review = json.loads(reopened.db.execute("SELECT context FROM research_run_contexts WHERE role='evaluation'").fetchone()[0])
    assert review['process_attestation']['checkpoint_before_report']
    assert len(review['researcher_checkpoints']) == 1


async def test_evaluator_checkpoint_cannot_satisfy_researcher_requirement(state):
    store, goal, spec, _ = state

    class Agents(FixtureAgents):
        async def run(self, role, context, tools):
            result = await super().run(role, context, tools)
            if role == 'evaluation':
                assert not context['process_attestation']['checkpoint_before_report']
                assert context['researcher_checkpoints'] == []
                tools.checkpoint(result.evidence_ids, 'Intent unresolved', 'Finish evaluation')
                result.evidential_verdict = 'supported'
            return result

    service = Research(store, Agents(), require_checkpoint=True)
    job = service.plan(goal, 'research', spec)['job_id']
    result = await service.attempt(job)
    assert result['status'] == 'insufficient'
    assert result['evaluation']['evidential_verdict'] == 'supported'
    assert 'before the report' in result['feedback']
    findings = Knowledge(store).search(str(path := store.path.parent))
    assert {item['classification'] for item in findings} == {'supported_observation'}
    assert not store.db.execute("SELECT 1 FROM knowledge WHERE classification='verified'").fetchone()


async def test_supported_observation_survives_workflow_failure_without_verification(state):
    store, goal, spec, _ = state

    class Agents(FixtureAgents):
        async def run(self, role, context, tools):
            result = await super().run(role, context, tools)
            if role == 'evaluation':
                result.verdict = 'insufficient'
                result.evidential_verdict = 'supported'
                result.workflow_issues = ['Portfolio explanation incomplete']
            return result

    service = Research(store, Agents())
    job = service.plan(goal, 'research', spec)['job_id']
    result = await service.attempt(job)
    assert result['status'] == 'insufficient'
    assert store.db.execute('SELECT classification FROM knowledge').fetchone()[0] == 'supported_observation'
    assert not store.db.execute('SELECT 1 FROM implementation_authorizations').fetchone()


async def test_invalid_citations_cannot_become_supported_observations(state):
    store, goal, spec, _ = state

    class Agents(FixtureAgents):
        async def run(self, role, context, tools):
            result = await super().run(role, context, tools)
            if role == 'evaluation':
                result.evidential_verdict = 'supported'
            return result

    service = Research(store, Agents(misleading=True))
    job = service.plan(goal, 'research', spec)['job_id']
    result = await service.attempt(job)
    assert result['evaluation']['evidential_verdict'] == 'insufficient'
    assert store.db.execute('SELECT classification FROM knowledge').fetchone()[0] == 'assumption'


async def test_missing_portfolio_metadata_is_explicit_and_legacy_needs_no_checkpoint(state):
    store, goal, spec, _ = state

    class Agents(FixtureAgents):
        async def run(self, role, context, tools):
            assert 'unknown' in context['portfolio']['manifest']['status']
            assert 'unknown' in context['portfolio']['audit_summary']['status']
            assert not context['process_requirements']['researcher_checkpoint_before_report']
            return await super().run(role, context, tools)

    service = Research(store, Agents())
    job = service.plan(goal, 'research', spec)['job_id']
    assert (await service.attempt(job))['status'] == 'supported'
