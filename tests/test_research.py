import json
import pytest
from omnitrix.store import Store
from omnitrix.research import Research
from omnitrix.research_models import (ResearchPlan, ResearchBudget, DiscoveryReport, MapEntry,
    Opportunity, ResearchReport, Finding, Evaluation)
from omnitrix.knowledge import Knowledge, ResearchTools
from omnitrix.models import Decision
from omnitrix.manager import Manager
from tests_helpers import FixtureAgents


@pytest.fixture
def system(tmp_path):
    (tmp_path/'system.md').write_text('Service queue uses polling interval=30 seconds.\nDependency: database.\nMetrics: no retry errors.\n')
    store = Store(tmp_path/'state.db')
    goal = store.create_goal('Improve performance, reliability, and cost', ['Prioritize supported opportunity'], tmp_path)
    return store, goal, tmp_path


def plan(hypothesis=None, attempts=2):
    return ResearchPlan(scope=['system.md'], question='What is the highest-value uncertainty?',
        hypothesis=hypothesis, falsification='Interval is not 30 seconds' if hypothesis else None,
        budget=ResearchBudget(tool_calls=8,seconds=2,attempts=attempts))


async def accepted(system):
    store, goal, _ = system
    service = Research(store,FixtureAgents())
    job = service.plan(goal,'research',plan('Queue polling interval is 30 seconds'))['job_id']
    result = await service.attempt(job)
    assert result['status']=='supported'
    return job


async def test_complete_manager_discovery_research_evaluation_loop(system):
    store, goal, _ = system
    class Brain:
        def __init__(self): self.seen=[]
        async def decide(self,state):
            self.seen.append(state)
            if not state['research']:
                action='discover'; spec=plan()
            elif not any(r['stage']=='research' for r in state['research']):
                action='research'; spec=plan('Queue polling interval is 30 seconds')
                spec.opportunity_id=state['opportunities'][0]['id']
            else:
                assert state['research'][0]['status']=='supported'
                action='pause';spec=None
            return Decision(action=action,rationale='Resolve the highest-value uncertainty',instructions='Examine polling evidence',task_id=None,priority=1,research_plan=spec)
    from unittest.mock import patch
    brain=Brain()
    # Keep manager orchestration real while replacing only model-backed specialist reasoning.
    original=Research
    with patch('omnitrix.manager.Research',lambda store: original(store,FixtureAgents())):
        result=await Manager(store,brain,None).resume(goal,steps=3)
    assert result['goal']['status']=='paused'
    assert result['opportunities'][0]['score']>result['opportunities'][1]['score']
    assert Knowledge(store).search(str(system[2]),'interval')[0]['classification']=='verified'
    assert len(brain.seen)==3


async def test_unsupported_claims_fail_even_if_evaluator_trusts_them(system):
    store, goal, _=system
    service=Research(store,FixtureAgents(misleading=True))
    job=service.plan(goal,'research',plan('Queue polling interval is 30 seconds'))['job_id']
    result=await service.attempt(job)
    assert result['status']=='insufficient'
    assert 'Unknown' in result['feedback']
    assert not store.db.execute("SELECT 1 FROM knowledge WHERE classification='verified'").fetchone()
    with pytest.raises(ValueError,match='targeted'):
        await service.attempt(job)
    second=await service.attempt(job,'Read actual source IDs, not the invented benchmark')
    assert second['status']=='escalated'
    assert service.job(job)['attempts']==2


async def test_new_session_reuses_research_and_detects_revision_change(system):
    store, goal, path=system
    await accepted(system)
    new=Store(store.path)
    prior=Knowledge(new).search(str(path),'interval')
    assert prior and not prior[0]['stale']
    duplicate=Research(new).plan(goal,'research',plan('Queue polling interval is 30 seconds'))
    assert duplicate['duplicate']
    (path/'system.md').write_text('Service queue polling interval=10 seconds\n')
    assert Knowledge(new).search(str(path),'interval')[0]['stale']
    assert not Research(new).plan(goal,'research',plan('Queue polling interval is 30 seconds'))['duplicate']


async def test_falsification_is_durable(system):
    store, goal, path=system
    service=Research(store,FixtureAgents(verdict='falsified'))
    job=service.plan(goal,'research',plan('Queue polling interval is 10 seconds'))['job_id']
    assert (await service.attempt(job))['status']=='falsified'
    assert any(k['kind']=='rejected_hypothesis' for k in Knowledge(store).search(str(path)))


async def test_goal_budget_is_durable_and_stops_retries(system):
    store,goal,_=system
    service=Research(store,FixtureAgents())
    service.configure(goal,1)
    job=service.plan(goal,'research',plan('Queue polling interval is 30 seconds'))['job_id']
    assert (await service.attempt(job))['status']=='escalated'
    assert store.db.execute('SELECT spent FROM research_limits').fetchone()[0]==1


async def test_scope_tool_budget_and_evidence_queries(system):
    store,goal,path=system
    service=Research(store,FixtureAgents())
    job=service.plan(goal,'discovery',plan())['job_id']
    await service.attempt(job)
    run=store.db.execute('SELECT id FROM research_runs').fetchone()[0]
    tools=ResearchTools(store,run,str(path),['system.md'],2)
    (path/'outside.md').write_text('outside')
    with pytest.raises(ValueError,match='scope'):
        tools.read('outside.md')
    tools.read('system.md')
    with pytest.raises(RuntimeError,match='budget'):
        tools.read('system.md')
    assert store.db.execute('SELECT count(*) FROM research_queries').fetchone()[0]>=3
    with pytest.raises(ValueError):
        plan('')


async def test_independent_evaluator_receives_captured_content(system):
    store,goal,_=system
    agents=FixtureAgents()
    job=Research(store,agents).plan(goal,'research',plan('Queue polling interval is 30 seconds'))['job_id']
    await Research(store,agents).attempt(job)
    assert 'interval=30' in agents.evaluated_content


async def test_evaluation_receives_current_contract_and_keeps_optional_gaps_nonblocking(system):
    from omnitrix.control import Control
    store, goal, _ = system
    control = Control(store)
    proposal = control.propose(goal, 'requirements', {
        'constraint': 'Only change polling behavior', 'add_requirements': ['Preserve database access']})
    control.accept(goal, proposal)
    proposal = control.propose(goal, 'policy', {'instructions': 'Use the supplied acceptance contract.'})
    control.accept(goal, proposal)
    class ScopedAgents(FixtureAgents):
        async def run(self, role, context, tools):
            result = await super().run(role, context, tools)
            if role == 'evaluation':
                assert context['goal']['requirements'] == ['Prioritize supported opportunity', 'Preserve database access']
                assert context['goal']['constraints'] == ['Only change polling behavior']
                assert context['policy']['instructions'] == 'Use the supplied acceptance contract.'
                result.non_blocking_uncertainties = ['Production throughput has not been measured']
            return result
    service = Research(store, ScopedAgents())
    job = service.plan(goal, 'research', plan('Queue polling interval is 30 seconds'))['job_id']
    outcome = await service.attempt(job)
    assert outcome['status'] == 'supported'
    assert outcome['evaluation']['non_blocking_uncertainties']
    assert Knowledge(store).search(str(system[2]), 'interval')[0]['classification'] == 'verified'


@pytest.mark.parametrize('reference,valid', [
    ('Prioritize supported opportunity', True), ('Prove every possible edge case', False)])
async def test_blocking_gap_must_reference_the_current_contract(system, reference, valid):
    from omnitrix.research_models import EvaluationGap
    store, goal, _ = system
    class GapAgents(FixtureAgents):
        async def run(self, role, context, tools):
            result = await super().run(role, context, tools)
            if role == 'evaluation':
                result.verdict = 'insufficient'
                result.blocking_gaps = [EvaluationGap(basis='requirement', reference=reference,
                    missing_evidence='No measurement supports the performance claim',
                    next_action='Read the scoped measurements before making a performance claim')]
            return result
    service = Research(store, GapAgents())
    job = service.plan(goal, 'research', plan('Queue polling interval is 30 seconds'))['job_id']
    outcome = await service.attempt(job)
    assert outcome['status'] == 'insufficient'
    assert not store.db.execute("SELECT 1 FROM knowledge WHERE classification='verified'").fetchone()
    if valid:
        assert outcome['evaluation']['blocking_gaps'][0]['reference'] == reference
    else:
        assert 'exact current requirement' in outcome['feedback']


async def test_research_feedback_survives_restart_and_recent_history_eviction(system):
    from omnitrix.research_models import EvaluationGap
    store, goal, path = system
    class GapAgents(FixtureAgents):
        async def run(self, role, context, tools):
            result = await super().run(role, context, tools)
            if role == 'evaluation':
                result.verdict = 'insufficient'
                result.feedback = 'Read the interval source before claiming a latency improvement'
                result.blocking_gaps = [EvaluationGap(basis='claim', reference=context['report']['findings'][0]['statement'],
                    missing_evidence='The claimed value needs independent confirmation',
                    next_action='Read system.md')]
            return result
    service = Research(store, GapAgents())
    job = service.plan(goal, 'research', plan('Queue polling interval is 30 seconds'))['job_id']
    await service.attempt(job)
    for index in range(25):
        discovery = plan().model_copy(update={'question': f'Inspect discovery scope number {index}'})
        service.plan(goal, 'discovery', discovery)
    store.db.close()
    fresh = Store(path/'state.db')
    state = fresh.snapshot(goal)
    assert job not in {row['id'] for row in state['research']}
    assert state['research_state']['latest_job']['id'] == job
    assert state['research_state']['latest_job']['retryable']
    assert 'interval source' in state['research_state']['latest_job']['feedback']
    assert state['research_state']['remaining_role_runs'] == 10
    older = fresh.research_history(goal, offset=20)
    assert any(row['id'] == job and row['report']['findings'] for row in older)
    fresh.execute('UPDATE research_limits SET spent=max_runs WHERE goal_id=?', (goal,))
    assert not fresh.research_state(goal)['latest_job']['retryable']


async def test_missing_scope_is_replanned_without_replaying_or_resetting_goal_budget(system):
    from omnitrix.control import Control
    from omnitrix.supervisor import Supervisor
    class Runtime:
        async def close(self):
            pass
    store, goal, path = system
    (path/'contract.py').write_text('assert polling_interval == 30\n')
    Control(store).require_implementation(goal)
    class NeedsContract(FixtureAgents):
        async def run(self, role, context, tools):
            if role == 'research':
                tools.read('contract.py')
            return await super().run(role, context, tools)
    service = Research(store, NeedsContract())
    spec = plan('Queue polling interval is 30 seconds')
    job = service.plan(goal, 'research', spec)['job_id']
    outcome = await service.attempt(job)
    assert outcome['status'] == 'blocked'
    assert outcome['scope_gaps'] == ['contract.py']
    state = store.research_state(goal)['latest_job']
    assert state['needs_scope_expansion'] and not state['retryable']
    supervisor = await Supervisor(store, goal, runtime=Runtime(), research=service).open()
    try:
        retry = Decision(action='retry_research', rationale='Add contract.py', instructions='Read contract.py too',
            task_id=None, priority=1, research_id=job)
        with pytest.raises(ValueError, match='do not change scope'):
            await supervisor.dispatch(retry)
        pause = Decision(action='pause', rationale='Tool repair required', instructions='Fix access',
            task_id=None, priority=1, pause_reason='reconciliation')
        with pytest.raises(ValueError, match='local files exist'):
            await supervisor.dispatch(pause)
        expanded = spec.model_copy(update={'scope': ['system.md', 'contract.py']})
        new_job = service.plan(goal, 'research', expanded)['job_id']
        assert new_job != job
        assert (await service.attempt(new_job))['status'] == 'supported'
        assert service.job(job)['attempts'] == 1
        assert store.research_state(goal)['remaining_role_runs'] == 9
    finally:
        await supervisor.close()


async def test_timeout_preserves_attempt_and_escalates(system):
    import asyncio
    store,goal,_=system
    class Slow(FixtureAgents):
        async def run(self,*args): await asyncio.sleep(2)
    service=Research(store,Slow())
    spec=plan('Queue polling interval is 30 seconds',attempts=1)
    spec.budget.seconds=1
    job=service.plan(goal,'research',spec)['job_id']
    assert (await service.attempt(job))['status']=='escalated'
    assert store.db.execute('SELECT finished FROM research_runs').fetchone()[0]


async def test_already_exhausted_goal_cannot_launch_another_agent(system):
    store,goal,_=system
    service=Research(store,FixtureAgents())
    service.configure(goal,1)
    service.charge(goal)
    job=service.plan(goal,'discovery',plan())['job_id']
    outcome=await service.attempt(job)
    assert outcome['status']=='escalated'
    assert store.db.execute('SELECT count(*) FROM research_runs').fetchone()[0]==0


async def test_mandatory_knowledge_lookup_does_not_starve_one_read_budget(system):
    store,goal,_=system
    service=Research(store,FixtureAgents())
    spec=plan('Queue polling interval is 30 seconds')
    spec.budget.tool_calls=1
    job=service.plan(goal,'research',spec)['job_id']
    result=await service.attempt(job)
    assert result['status']=='supported'
    assert store.db.execute("SELECT count(*) FROM research_queries WHERE operation='prefetch_knowledge'").fetchone()[0]==1
    assert store.db.execute('SELECT tool_calls FROM research_runs').fetchone()[0]==1


async def test_actual_sdk_tool_dispatch_keeps_sqlite_on_its_owner_thread(system,monkeypatch):
    from agents.tool_context import ToolContext
    from omnitrix.research import ResearchAgents
    store,goal,path=system
    service=Research(store,FixtureAgents())
    job=service.plan(goal,'discovery',plan())['job_id']
    await service.attempt(job)
    run=store.db.execute('SELECT id FROM research_runs').fetchone()[0]
    tools=ResearchTools(store,run,str(path),['system.md'],4)
    async def fake_run(agent,prompt,**kwargs):
        arguments=json.dumps({'locator':'system.md','start_line':1,'line_count':80})
        ctx=ToolContext(context=None,tool_name='read_source',tool_call_id='actual-sdk-dispatch',tool_arguments=arguments)
        result=await agent.tools[0].on_invoke_tool(ctx,arguments)
        assert 'interval=30' in json.loads(result)['excerpt']
        class Result:
            final_output=DiscoveryReport(system_map=[],opportunities=[],gaps=[])
        return Result()
    monkeypatch.setattr('omnitrix.research.Runner.run',fake_run)
    await ResearchAgents().run('discovery',{},tools)


async def test_completed_discovery_report_is_delivered_to_manager(system):
    store,goal,_=system
    service=Research(store,FixtureAgents())
    job=service.plan(goal,'discovery',plan())['job_id']
    outcome=await service.attempt(job)
    assert outcome['report']['system_map'][0]['name']=='queue'
    assert outcome['report']['opportunities']


async def test_sdk_read_bounds_are_advertised_and_errors_are_actionable(system,monkeypatch):
    from agents.tool_context import ToolContext
    from omnitrix.research import ResearchAgents
    store,goal,path=system
    service=Research(store,FixtureAgents())
    job=service.plan(goal,'discovery',plan())['job_id']
    await service.attempt(job)
    run=store.db.execute('SELECT id FROM research_runs').fetchone()[0]
    tools=ResearchTools(store,run,str(path),['system.md'],2)
    async def fake_run(agent,prompt,**kwargs):
        tool=agent.tools[0]
        assert tool.params_json_schema['properties']['line_count']['maximum']==100
        args=json.dumps({'locator':'system.md','start_line':1,'line_count':220})
        ctx=ToolContext(context=None,tool_name='read_source',tool_call_id='invalid-range',tool_arguments=args)
        result=await tool.on_invoke_tool(ctx,args)
        assert '100' in result and 'source access blocked' in result
        class Result:
            final_output=DiscoveryReport(system_map=[],opportunities=[],gaps=[])
        return Result()
    monkeypatch.setattr('omnitrix.research.Runner.run',fake_run)
    await ResearchAgents().run('discovery',{},tools)
