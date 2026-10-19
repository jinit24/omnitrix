"""Behavioral tests for adaptive routing, compact recall, and concurrent research."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext

from omnitrix.control import Control
from omnitrix.implementation import AcceptanceCheck, Implementations, ImplementationSpec, git
from omnitrix.manager import AgentBrain, compact_snapshot
from omnitrix.models import ChatReply, Decision
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.spending import Spending
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor
from tests_helpers import FixtureAgents


def decision(action, **kwargs):
    return Decision(action=action, rationale='Exercise bounded orchestration', instructions='Review current evidence',
                    task_id=None, priority=1, **kwargs)


@pytest.fixture
def project(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    subprocess.run(['git','init','-qb','main',str(repo)],check=True)
    (repo/'service.py').write_text('def double(x): return x\n')
    (repo/'system.md').write_text('Queue polling interval=30 seconds\n')
    git(repo,'add','.')
    git(repo,'-c','user.name=Test','-c','user.email=test@localhost','commit','-qm','Fixture')
    store=Store(tmp_path/'state.db')
    goal=store.create_goal('Correct double values',['Double a positive and preserve zero'],repo)
    Control(store).require_implementation(goal)
    spec=ImplementationSpec(objective='Correct the double function',basis='direct',
        contract='double(x) returns x * 2 for the supplied numeric input.',allowed_paths=['service.py'],
        checks=[AcceptanceCheck(requirement='Double a positive and preserve zero',
            argv=[sys.executable,'-c','from service import double; assert double(3)==6; assert double(0)==0'])])
    return store,goal,repo,spec


class Runtime:
    def __init__(self, fail_first=False): self.sessions=[];self.prompts=[];self.fail_first=fail_first
    async def start(self,workspace,instructions,**kwargs):
        self.sessions.append(kwargs.get('session_id'));self.prompts.append(json.loads(instructions))
        multiplier=3 if self.fail_first and len(self.sessions)==1 else 2
        (Path(workspace)/'service.py').write_text(f'def double(x): return x * {multiplier}\n')
        return 'native-thread',f'turn-{len(self.sessions)}'
    async def wait(self,*args):
        return {'status':'completed','items':[{'type':'agentMessage','text':json.dumps({
            'summary':'Multiply by two','artifacts':['service.py'],'tests_run':[],
            'unresolved_concerns':[],'questions':[],
            'quality_report':'Before x; after 2*x. Independent checks pending; no deployment. Local monitoring: rerun the fixed acceptance checks.'})}]}
    async def close(self): pass


async def test_direct_route_has_one_final_review_no_research_and_reuses_repair_session(project):
    store,goal,repo,spec=project
    runtime=Runtime(fail_first=True)
    service=Implementations(store,runtime)
    iid=service.create(service.authorize(goal,spec,explicit=True))
    class Reviewer:
        calls=0
        async def decide(self,state):
            self.calls+=1
            assert state['implementations'][0]['status']=='completed'
            assert not state['research']
            assert any(m['kind']=='implementation_handoff' for m in state['worker_messages'])
            return decision('complete')
    brain=Reviewer()
    owner=await Supervisor(store,goal,brain=brain,runtime=runtime).open()
    try:
        result=await owner.advance(3)
        assert brain.calls==0 and 'task_id' in result
        await asyncio.gather(*list(owner.work.values()))
        assert service.get(iid)['status']=='completed'
        await owner.advance()
        assert store.snapshot(goal)['goal']['status']=='completed'
        assert brain.calls==1
        assert runtime.sessions==[None,'native-thread']
        assert runtime.prompts[1]['resumed_repair'] and runtime.prompts[1]['feedback']
        assert (repo/'service.py').read_text()=='def double(x): return x\n'
    finally:
        await owner.close()


def test_direct_requires_explicit_contract_all_checks_and_current_permission(project):
    store,goal,repo,spec=project
    service=Implementations(store)
    with pytest.raises(ValueError,match='explicit'):
        service.authorize(goal,spec)
    with pytest.raises(ValueError,match='bounded contract'):
        ImplementationSpec.model_validate(spec.model_dump() | {'contract':''})
    incomplete=spec.model_copy(deep=True);incomplete.checks[0].requirement='Something else'
    with pytest.raises(ValueError,match='mandatory'):
        service.authorize(goal,incomplete,explicit=True)
    approval=service.authorize(goal,spec,explicit=True)
    proposal=Control(store).propose(goal,'requirements',{'constraint':'Also negatives','add_requirements':['Double negatives']})
    Control(store).accept(goal,proposal)
    with pytest.raises(ValueError,match='revoked'):
        service.create(approval)


def test_budgeted_campaign_cannot_use_earlier_direct_authorization(project):
    store,goal,repo,spec=project
    service=Implementations(store)
    approval=service.authorize(goal,spec,explicit=True)
    Spending(store,goal).configure(1)
    with pytest.raises(ValueError,match='read-only'):
        service.create(approval)
    with pytest.raises(ValueError,match='campaign'):
        service.authorize(goal,spec,explicit=True)


async def test_direct_still_rejects_changed_oracle_and_out_of_scope_edits(project,tmp_path):
    store,goal,repo,spec=project
    checker=tmp_path/'check.py';checker.write_text('assert True\n')
    spec.checks[0].argv=[sys.executable,str(checker)]
    class OutOfScope(Runtime):
        async def start(self,workspace,instructions,**kwargs):
            (Path(workspace)/'system.md').write_text('Unauthorized edit')
            return await super().start(workspace,instructions,**kwargs)
    service=Implementations(store,OutOfScope())
    iid=service.create(service.authorize(goal,spec,explicit=True))
    assert (await service.run(iid))['status']=='uncertain'
    assert 'outside explicit' in service.get(iid)['feedback']
    checker.write_text('assert False\n')
    with pytest.raises(ValueError,match='oracle changed'):
        service.auth(service.get(iid)['authorization'])


def plans():
    return [ResearchPlan(scope=['system.md'],question=f'Which bounded question {i} is supported?',
        hypothesis=f'Queue polling interval is 30 seconds in scenario {i}',falsification='Registry specifies a different interval') for i in range(2)]


async def test_research_wave_overlaps_but_charges_each_role_and_preserves_evaluator_contract(project):
    store,goal,repo,spec=project
    entered=asyncio.Event();release=asyncio.Event()
    class Agents(FixtureAgents):
        running=0
        async def run(self,role,context,tools):
            if role=='research':
                self.running+=1
                if self.running==2: entered.set()
                await release.wait()
            else:
                assert context['acceptance_criteria']==store.snapshot(goal)['goal']['requirements']
                assert context['review_stage']=='research_evidence_before_candidate_verification'
            return await super().run(role,context,tools)
    research=Research(store,Agents());research.configure(goal,max_runs=4)
    owner=await Supervisor(store,goal,runtime=Runtime(),research=research).open()
    try:
        result=await owner.dispatch(decision('research_batch',research_plans=plans(),
            independence_reason='Each scenario reads the same immutable registry without peer outputs'))
        assert len(result['task_ids'])==2
        await asyncio.wait_for(entered.wait(),2)
        assert len(owner.work)==2
        release.set()
        await asyncio.gather(*list(owner.work.values()))
        assert all(r['status']=='supported' for r in store.snapshot(goal)['research'])
        assert store.db.execute('SELECT spent FROM research_limits').fetchone()[0]==4
    finally:
        release.set();await owner.close()


async def test_research_wave_preflight_starts_nothing_on_invalid_scope_or_insufficient_budget(project):
    store,goal,repo,spec=project
    research=Research(store,FixtureAgents());research.configure(goal,max_runs=3)
    owner=Supervisor(store,goal,runtime=Runtime(),research=research)
    with pytest.raises(ValueError,match='budget'):
        await owner.dispatch(decision('research_batch',research_plans=plans(),independence_reason='No cross-plan dependencies'))
    assert not owner.work and not store.snapshot(goal)['research']
    invalid=plans();invalid[1].scope=['not-a-real-file']
    with pytest.raises(ValueError,match='existing file'):
        research.plan_batch(goal,invalid,'No cross-plan dependencies')
    assert not store.snapshot(goal)['research']
    with pytest.raises(ValueError,match='repeats'):
        research.plan_batch(goal,[plans()[0],plans()[0]],'No cross-plan dependencies')


async def test_revision_change_invalidates_all_results_in_research_wave(project):
    store,goal,repo,spec=project
    entered=asyncio.Event();release=asyncio.Event()
    class Waiting(FixtureAgents):
        count=0
        async def run(self,role,context,tools):
            self.count+=1
            if self.count==2: entered.set()
            await release.wait()
            return await super().run(role,context,tools)
    owner=await Supervisor(store,goal,runtime=Runtime(),research=Research(store,Waiting())).open()
    try:
        await owner.dispatch(decision('research_batch',research_plans=plans(),independence_reason='Independent immutable reads'))
        await asyncio.wait_for(entered.wait(),2)
        proposal=owner.control.propose(goal,'requirements',{'constraint':'Changed contract','add_requirements':['Handle negative values']})
        owner.control.accept(goal,proposal)
        release.set()
        await asyncio.gather(*list(owner.work.values()))
        assert not store.db.execute("SELECT 1 FROM knowledge WHERE classification='verified'").fetchone()
        assert all(r['status'] in ('stale','invalidated','interrupted') for r in store.snapshot(goal)['research'])
    finally:
        release.set();await owner.close()


async def test_manager_context_does_not_replay_snapshots_and_recall_is_goal_scoped(project,monkeypatch):
    store,goal,repo,spec=project
    service=Implementations(store,Runtime());iid=service.create(service.authorize(goal,spec,explicit=True))
    await service.run(iid)
    control=Control(store)
    for i in range(30):
        control.message(goal,'findings',{'report':{'quality_report':'duplicate-'*1000}},direction='worker_to_manager')
        store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',(goal,json.dumps({'n':i})))
    async def fake_run(agent,prompt,**kwargs):
        state=json.loads(prompt)
        assert kwargs['session'] is None
        assert state['goal']['requirements']==store.snapshot(goal)['goal']['requirements']
        assert len(state['decisions'])==8
        assert 'duplicate-' not in prompt
        tool=next(t for t in agent.tools if t.name=='recall_artifact')
        for identity,found in ((iid,True),('another-goal-id',False)):
            payload=json.dumps({'kind':'implementation','artifact_id':identity})
            ctx=ToolContext(context=None,tool_name=tool.name,tool_call_id='recall',tool_arguments=payload)
            result=json.loads(await tool.on_invoke_tool(ctx,payload))
            assert ('implementation' in result)==found
        return SimpleNamespace(final_output=decision('pause'))
    monkeypatch.setattr('omnitrix.manager.Runner.run',fake_run)
    await AgentBrain(store=store).decide(store.snapshot(goal))
    assert store.db.execute('SELECT count(*) FROM decisions').fetchone()[0]==30


async def test_chat_remembers_words_across_restart_without_state_replay(project,monkeypatch):
    store,goal,repo,spec=project
    calls=[]
    async def fake_run(agent,prompt,**kwargs):
        assert kwargs['session'] is None
        calls.append(json.loads(prompt))
        return SimpleNamespace(final_output=ChatReply(response='Saved conversation',proposal_kind='none',
            proposal_text='',affected_tasks=[],add_requirements=[]))
    monkeypatch.setattr('omnitrix.supervisor.Runner.run',fake_run)
    await Supervisor(store,goal,runtime=Runtime()).chat('Remember the latency constraint')
    restored=Store(store.path)
    await Supervisor(restored,goal,runtime=Runtime()).chat('What did we discuss?')
    dialogue=calls[1]['state']['dialogue']
    assert 'latency constraint' in json.dumps(dialogue) and 'Saved conversation' in json.dumps(dialogue)
    assert 'worker_messages' not in json.dumps(dialogue)


async def test_adaptive_benchmark_scores_exact_candidate_with_no_research(tmp_path):
    from scripts.benchmark import ROOT,run_arm
    task=json.loads((ROOT/'benchmarks/tasks.json').read_text())[1]
    report=await run_arm(task,'adaptive',tmp_path/'adaptive',False,True,6)
    assert report['verified_outcome'] and not report['errors']
    assert report['repeated_research_queries']==0
    assert report['manager_decisions']==2
    assert not report['incorrect_completion_claim']


async def test_action_schema_excludes_impossible_implementation_and_legacy_delegation(project,monkeypatch):
    store,goal,repo,spec=project
    async def fake_run(agent,prompt,**kwargs):
        schema=agent.output_type.model_json_schema()['properties']['action']['enum']
        assert 'implement' not in schema and 'follow_up' not in schema
        assert 'delegate' not in schema and 'complete' not in schema
        assert {'research','discover','research_batch','pause'} <= set(schema)
        assert json.loads(prompt)['available_actions']==schema
        return SimpleNamespace(final_output=decision('pause'))
    monkeypatch.setattr('omnitrix.manager.Runner.run',fake_run)
    await AgentBrain(store=store).decide(store.snapshot(goal))


async def test_prefetch_prioritizes_older_relevant_evidence_over_unrelated_recent_records(project):
    from omnitrix.knowledge import ResearchTools
    store,goal,repo,spec=project
    research=Research(store,FixtureAgents())
    first=research.plan(goal,'research',plans()[0])['job_id']
    assert (await research.attempt(first))['status']=='supported'
    for i in range(7):
        filename=f'unrelated{i}.md';(repo/filename).write_text('Queue polling interval=30 seconds\n')
        p=plans()[0].model_copy(update={'scope':[filename],'question':f'Unrelated question number {i}'})
        job=research.plan(goal,'discovery',p.model_copy(update={'hypothesis':None,'falsification':None}))['job_id']
        await research.attempt(job)
    latest=research.plan(goal,'research',plans()[1])['job_id']
    store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('prefetch-run',?,'running',0)",(latest,))
    tools=ResearchTools(store,'prefetch-run',str(repo),['system.md'],1)
    records=tools.prefetch()
    assert records[0]['job_id']==first
    assert tools.remaining==1
    (repo/'system.md').write_text('Queue polling interval=60 seconds\n')
    assert tools.prefetch()[0]['stale']


@pytest.mark.parametrize('blocking,expected',[(False,'supported'),(True,'insufficient')])
async def test_nonblocking_unknowns_do_not_override_supported_evidence(project,blocking,expected):
    store,goal,repo,spec=project
    class Reviewer(FixtureAgents):
        async def run(self,role,context,tools):
            result=await super().run(role,context,tools)
            if role=='evaluation':
                result.nonblocking_uncertainties=['Production latency has not been measured; claim is code-only']
                result.blocking_questions=['Caller contradicts the stated contract; inspect the scoped call site'] if blocking else []
            return result
    service=Research(store,Reviewer())
    job=service.plan(goal,'research',plans()[0])['job_id']
    assert (await service.attempt(job))['status']==expected


async def test_uncertain_standalone_implementation_blocks_another_direct_action(project):
    store,goal,repo,spec=project
    class NoDecisions:
        async def decide(self,state): raise AssertionError('Uncertain outcome must block new model actions')
    service=Implementations(store,Runtime())
    first=service.create(service.authorize(goal,spec,explicit=True))
    store.execute("UPDATE implementations SET status='uncertain' WHERE id=?",(first,))
    service.create(service.authorize(goal,spec,explicit=True))
    runtime=Runtime()
    owner=await Supervisor(store,goal,brain=NoDecisions(),runtime=runtime).open()
    try:
        assert 'Reconcile uncertain' in (await owner.advance(3))['blocked']
        assert not runtime.sessions
    finally:
        await owner.close()
