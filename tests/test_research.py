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
