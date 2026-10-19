"""Plan admission and bounded prerequisite repair must work without another model run."""
import json
import time
import uuid

import pytest

from omnitrix.audit import Audit
from omnitrix.control import Control
from omnitrix.knowledge import ResearchTools
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.store import Store
from tests_helpers import FixtureAgents


@pytest.fixture
def state(tmp_path):
    (tmp_path / 'sources/pkg').mkdir(parents=True)
    for name in ('main', 'callee', 'test', 'other', 'last'):
        (tmp_path / f'sources/pkg/{name}.txt').write_text('Polling interval=30 seconds\ncall dependency()\n')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Audit contracts', ['Preserve uncertainty'], tmp_path)
    return store, goal, tmp_path


def spec(**updates):
    values = dict(scope=['sources/pkg/main.txt'], question='What is the polling contract?',
        hypothesis='Polling interval is 30 seconds', falsification='The interval differs',
        evidence_obligations=[dict(kind='implementation', locator='sources/pkg/main.txt', start_line=1, end_line=2)])
    values.update(updates)
    return ResearchPlan.model_validate(values)


def tools_for(state, plan=None, **options):
    store, goal, path = state
    plan = plan or spec()
    service = Research(store)
    job = service.plan(goal, 'research', plan)['job_id']
    run = uuid.uuid4().hex
    store.execute('INSERT INTO research_runs(id,job_id,status,created) VALUES(?,?,?,?)', (run, job, 'running', time.time()))
    tools = ResearchTools(store, run, str(path), plan.scope, plan.budget.tool_calls,
        obligations=[item.model_dump() for item in plan.evidence_obligations], **options)
    return service, job, run, tools


def test_preflight_rejects_missing_dependency_before_jobs_or_role_charge(state):
    store, goal, _ = state
    plan = spec(evidence_obligations=[dict(kind='callee', locator='sources/pkg/callee.txt', start_line=1, end_line=2)])
    with pytest.raises(ValueError, match='outside scope'):
        Research(store).plan(goal, 'research', plan)
    assert store.db.execute('SELECT count(*) FROM research_jobs').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM research_limits').fetchone()[0] == 0
    with pytest.raises(ValueError, match='Unresolved required dependencies'):
        Research(store).preflight(goal, 'research', spec(unresolved_dependencies=[dict(symbol='dependency', reason='Return contract unknown')]))


def test_preflight_proves_actual_minimum_reads_and_checkpoint_reservation(state):
    store, goal, path = state
    (path / 'sources/pkg/main.txt').write_text('\n'.join('x' for _ in range(250)))
    plan = spec(evidence_obligations=[dict(kind='implementation', locator='sources/pkg/main.txt', start_line=1, end_line=250)],
        budget=dict(tool_calls=3))
    with pytest.raises(ValueError, match='3 source reads.*1 reserved checkpoint'):
        Research(store, require_checkpoint=True).plan(goal, 'research', plan)
    plan.budget.tool_calls = 4
    proof = Research(store, require_checkpoint=True).preflight(goal, 'research', plan)['proof']
    assert proof['minimum_tool_calls'] == 4
    assert proof['reserved_checkpoint_calls'] == 1
    assert not store.db.execute('SELECT 1 FROM research_jobs').fetchone()
    # Very long lines, rather than the nominal 100-line limit, can dominate calls.
    (path / 'sources/pkg/main.txt').write_text('\n'.join('x' * 7000 for _ in range(3)))
    plan.evidence_obligations[0].end_line = 3
    assert Research(store).preflight(goal, 'research', plan)['proof']['minimum_read_calls'] == 3


def test_new_context_cannot_bypass_typed_evidence_plan(state):
    store, goal, _ = state
    audit = Audit(store, goal)
    audit.initialize([dict(path='pkg', files=5, bytes=200, kind='directory')])
    control = Control(store)
    proposal = control.propose(goal, 'research_context', dict(brief='Trace implementation and contrary behavior',
        rubric=['Cite current code'], require_evidence_plan=True))
    control.accept(goal, proposal)
    plan = spec(primary_area='pkg', decision='Choose next fix', expected_value='Avoid false positives', stop_condition='Capture contract')
    with pytest.raises(ValueError, match='implementation and test/contract/final_outcome'):
        Research(store).preflight(goal, 'research', plan)
    data = plan.model_dump()
    data['evidence_obligations'].append(dict(kind='test', locator='sources/pkg/test.txt', start_line=1, end_line=2))
    data['scope'].append('sources/pkg/test.txt')
    assert Research(store).preflight(goal, 'research', data)['spec'].primary_area == 'pkg'


def test_reserved_checkpoint_and_semantic_progress_survive_reopen(state):
    service, job, run, tools = tools_for(state, spec(budget=dict(tool_calls=3)), require_checkpoint=True)
    source = tools.read('sources/pkg/main.txt')
    # Unique observations alone do not close a semantic checkpoint.
    assert service.progress_assessment(job)['semantic_progress_count'] == 0
    tools.read('sources/pkg/main.txt', 2, 1)
    with pytest.raises(RuntimeError, match='reserved for required checkpoint'):
        tools.read('sources/pkg/main.txt')
    assert tools.remaining == 1
    result = tools.checkpoint([source['id']], 'Caller behavior remains unknown', 'Report bounded contract')
    assert result['obligation_progress']['semantic_progress']
    assert tools.remaining == 0
    reopened = Research(Store(state[0].path))
    assessment = reopened.progress_assessment(job)
    assert assessment['semantic_progress_count'] == 1
    assert assessment['captured_required'] == 1
    assert assessment['suggested_action'] == 'report'


def test_repeated_checkpoints_do_not_invent_semantic_progress(state):
    service, job, _, tools = tools_for(state)
    source = tools.read('sources/pkg/main.txt')
    tools.checkpoint([source['id']], 'Unknown runtime behavior', 'Inspect test')
    before = service.progress_assessment(job)
    tools.read('sources/pkg/main.txt', 2, 1)
    tools.checkpoint([source['id']], 'Still unknown runtime behavior', 'Inspect test')
    after = service.progress_assessment(job)
    assert after['semantic_progress_at'] == before['semantic_progress_at']
    assert after['semantic_progress_count'] == before['semantic_progress_count'] == 1


def test_dependency_grants_are_bounded_durable_and_keep_existing_budgets(state):
    plan = spec(budget=dict(tool_calls=8), allow_scope_expansion=True, dependency_roots=['sources/pkg'])
    _, _, run, tools = tools_for(state, plan, allow_scope_expansion=True, dependency_roots=plan.dependency_roots, deadline=time.time() + 60)
    source = tools.read('sources/pkg/main.txt')
    original_deadline = tools.deadline
    for name in ('callee', 'test'):
        grant = tools.request_dependency([source['id']], 'Callsite needs its return contract', [dict(locator=f'sources/pkg/{name}.txt', start_line=1, end_line=2)])
        assert len(grant['requests']) == 1
        tools.read(f'sources/pkg/{name}.txt')
    with pytest.raises(ValueError, match='At most two dependency grants'):
        tools.request_dependency([source['id']], 'One more dependency', [dict(locator='sources/pkg/other.txt', start_line=1, end_line=2)])
    assert tools.deadline == original_deadline and tools.remaining == 2
    reopened = ResearchTools(Store(state[0].path), run, str(state[2]), plan.scope, 8)
    assert reopened.scope == set(plan.scope + ['sources/pkg/callee.txt', 'sources/pkg/test.txt'])
    assert len(reopened.grants()) == 2
    assert len(reopened.obligations) == 2


def test_dependency_grant_accepts_explicit_workspace_root_without_bypassing_boundaries(state):
    _, _, path = state
    (path / 'helper.txt').write_text('dependency return contract\n')
    (path / '.git').mkdir()
    (path / '.git/private.txt').write_text('excluded state\n')
    (path / 'linked.txt').symlink_to(path / 'helper.txt')
    plan = spec(allow_scope_expansion=True, dependency_roots=['.'])
    _, _, _, tools = tools_for(state, plan, allow_scope_expansion=True, dependency_roots=['.'])
    source = tools.read('sources/pkg/main.txt')
    for locator in ('.git/private.txt', 'linked.txt', '../outside.txt'):
        with pytest.raises(ValueError):
            tools.request_dependency([source['id']], 'Resolve the captured caller',
                [dict(locator=locator, start_line=1, end_line=1)])
    assert not tools.grants()
    grant = tools.request_dependency([source['id']], 'Resolve the captured caller',
        [dict(locator='helper.txt', start_line=1, end_line=1)])
    assert grant['requests'][0]['locator'] == 'helper.txt'
    assert 'helper.txt' in tools.scope


def test_dependency_grant_pins_source_revision_across_tool_reopen(state):
    store, _, path = state
    plan = spec(allow_scope_expansion=True, dependency_roots=['sources/pkg'])
    _, _, run, tools = tools_for(state, plan, allow_scope_expansion=True,
        dependency_roots=plan.dependency_roots)
    source = tools.read('sources/pkg/main.txt')
    tools.request_dependency([source['id']], 'Capture the return contract',
        [dict(locator='sources/pkg/callee.txt', start_line=1, end_line=2)])
    (path / 'sources/pkg/callee.txt').write_text('changed contract\nstill two lines\n')
    reopened = ResearchTools(Store(store.path), run, str(path), plan.scope, 8)
    for reader in (tools, reopened):
        with pytest.raises(ValueError, match='Granted dependency source revision changed'):
            reader.read('sources/pkg/callee.txt')
        assert not reader.obligation_progress()['required_obligations_complete']
    assert not store.db.execute("SELECT 1 FROM research_sources WHERE locator='sources/pkg/callee.txt'").fetchone()


@pytest.mark.parametrize('case', ['outside', 'symlink', 'invented', 'too_many', 'infeasible'])
def test_dependency_request_rejects_invalid_boundary_and_feasibility(state, case):
    _, _, path = state
    (path / 'outside.txt').write_text('outside')
    (path / 'sources/pkg/link.txt').symlink_to(path / 'outside.txt')
    plan = spec(budget=dict(tool_calls=3 if case == 'infeasible' else 8))
    _, _, _, tools = tools_for(state, plan, allow_scope_expansion=True, dependency_roots=['sources/pkg'], require_checkpoint=case == 'infeasible')
    source = tools.read('sources/pkg/main.txt')
    request = dict(locator='sources/pkg/callee.txt', start_line=1, end_line=2)
    if case == 'outside': request['locator'] = 'outside.txt'
    if case == 'symlink': request['locator'] = 'sources/pkg/link.txt'
    with pytest.raises(ValueError):
        tools.request_dependency(['invented'] if case == 'invented' else [source['id']], 'Resolve declared callsite',
            [request] * 3 if case == 'too_many' else [request])
    assert not tools.grants()


async def test_evaluator_receives_final_scope_and_report_obligation_snapshot(state):
    store, goal, _ = state
    plan = spec(allow_scope_expansion=True, dependency_roots=['sources/pkg'])
    seen = []
    class Agents(FixtureAgents):
        async def run(self, role, context, tools):
            if role == 'research':
                source = tools.read('sources/pkg/main.txt')
                tools.request_dependency([source['id']], 'Callsite return contract', [dict(locator='sources/pkg/callee.txt', start_line=1, end_line=2)])
                tools.read('sources/pkg/callee.txt')
                tools.checkpoint([item['id'] for item in tools.evidence()], 'Runtime outcomes unmeasured', 'Report bounded static contract')
            else:
                assert 'sources/pkg/callee.txt' in context['plan']['scope']
                assert len(context['scope_grants']) == 1
                assert context['evidence_obligation_progress']['captured_required'] == 2
                tools.read('sources/pkg/callee.txt')
                seen.append(role)
            return await super().run(role, context, tools)
    service = Research(store, Agents(), require_checkpoint=True)
    job = service.plan(goal, 'research', plan)['job_id']
    result = await service.attempt(job)
    assert result['status'] == 'supported', result
    assert seen == ['evaluation']
    assert service.job(job)['attempts'] == 1
    assert store.db.execute("SELECT count(*) FROM research_progress WHERE kind='report_snapshot'").fetchone()[0] == 1


async def test_evaluator_reads_cannot_repair_missing_research_obligation(state):
    store, goal, _ = state
    plan = spec(scope=['sources/pkg/main.txt', 'sources/pkg/test.txt'], evidence_obligations=[
        dict(kind='implementation', locator='sources/pkg/main.txt', start_line=1, end_line=2),
        dict(kind='test', locator='sources/pkg/test.txt', start_line=1, end_line=2)])
    class Agents(FixtureAgents):
        async def run(self, role, context, tools):
            if role == 'evaluation': tools.read('sources/pkg/test.txt')
            return await super().run(role, context, tools)
    service = Research(store, Agents())
    job = service.plan(goal, 'research', plan)['job_id']
    result = await service.attempt(job)
    assert result['status'] == 'insufficient'
    assert 'obligations' in result['feedback']
    result = await service.reevaluate(job, 'Check whether captured code suffices')
    assert result['status'] == 'insufficient'
    assert not store.db.execute("SELECT 1 FROM knowledge WHERE classification='verified'").fetchone()


async def test_legacy_plan_acquires_required_obligations_when_dependency_granted(state):
    store, goal, _ = state
    plan = spec(evidence_obligations=[], allow_scope_expansion=True, dependency_roots=['sources/pkg'])
    class Agents(FixtureAgents):
        async def run(self, role, context, tools):
            if role == 'research':
                source = tools.read('sources/pkg/main.txt')
                tools.request_dependency([source['id']], 'Need callee before concluding', [dict(locator='sources/pkg/callee.txt', start_line=1, end_line=2)])
            else:
                assert context['process_requirements']['required_evidence_obligations']
                assert context['plan']['evidence_obligations'][0]['kind'] == 'callee'
            return await super().run(role, context, tools)
    service = Research(store, Agents())
    result = await service.attempt(service.plan(goal, 'research', plan)['job_id'])
    assert result['status'] == 'insufficient'
    assert 'obligations' in result['feedback']


async def test_validated_captured_obligations_are_attached_without_prefetch_luck(state):
    from omnitrix.research_models import Evaluation, Finding, ResearchReport
    store, goal, _ = state
    _, _, _, prior = tools_for(state)
    source = prior.read('sources/pkg/main.txt')
    plan = spec(budget=dict(tool_calls=1), evidence_obligations=[dict(kind='implementation',
        locator='sources/pkg/main.txt', start_line=1, end_line=2, status='captured', evidence_ids=[source['id']])])
    class Agents:
        async def run(self, role, context, tools):
            assert source['id'] in {item['id'] for item in tools.evidence()}
            if role == 'research':
                tools.checkpoint([source['id']], 'Runtime behavior remains unmeasured', 'Report static source contract')
                return ResearchReport(stop_reason='resolved', findings=[Finding(statement='Polling interval is 30 seconds', classification='claim', evidence_ids=[source['id']])],
                    supporting_evidence=[source['id']], contradictory_evidence=[], uncertainties=['Static observation'], recommendation='Inspect runtime next', blocked=False)
            return Evaluation(verdict='supported', evidence_ids=[source['id']], reason='Captured contract supports the bounded observation', feedback='Retain static scope')
    service = Research(store, Agents(), require_checkpoint=True)
    result = await service.attempt(service.plan(goal, 'research', plan)['job_id'])
    assert result['status'] == 'supported', result
    assert store.db.execute('SELECT count(*) FROM knowledge WHERE classification=?', ('verified',)).fetchone()[0] == 1


def test_checkpoint_reservation_error_is_actionable_to_actual_tool_dispatch():
    from omnitrix.research import research_tool_error
    message = research_tool_error(None, RuntimeError('Last tool call is reserved for required checkpoint; checkpoint captured evidence, then report'))
    assert 'reserved for required checkpoint' in message
    assert 'typed stop reason' in message
