import json
from pathlib import Path
import subprocess

import pytest

from omnitrix.campaign import Campaign, CampaignSupervisor, ReadOnlyRuntime
from omnitrix.frontier import Frontier
from omnitrix.knowledge import Knowledge, ResearchTools
from omnitrix.models import Decision
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.store import Store
from tests_helpers import FixtureAgents


def setup(tmp_path):
    workspace = tmp_path/'repo';workspace.mkdir()
    (workspace/'system.md').write_text('Queue polling interval=30 seconds\n')
    subprocess.run(['git','init','-q',str(workspace)],check=True)
    subprocess.run(['git','-C',str(workspace),'add','.'],check=True)
    subprocess.run(['git','-C',str(workspace),'-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-qm','Source'],check=True)
    store = Store(tmp_path/'state.db')
    goal = store.create_goal('Investigate a broad system', ['Use evidence'], workspace)
    service = Campaign(store,goal)
    service.configure(days=3,decisions=1000,research_runs=2000,dollars=25,
                      quality={'sources':1,'branches':1,'hypotheses':1,'mechanisms':0})
    return store,goal,service,workspace


def test_three_day_deadline_and_all_consumed_budgets_survive_restart(tmp_path):
    store,goal,service,_ = setup(tmp_path)
    before = service.status()
    store.execute("UPDATE campaigns SET state='running' WHERE goal_id=?",(goal,))
    assert service.claim_decision(now=before['created']+2*86400)
    Research(store).charge(goal)
    store.db.close()
    restored = Store(tmp_path/'state.db');service=Campaign(restored,goal)
    assert service.configure()['decisions_spent']==1
    assert service.status()['deadline']==before['deadline']
    assert restored.db.execute('SELECT spent FROM research_limits').fetchone()[0]==1
    with pytest.raises(ValueError,match='reset'):
        service.configure(days=3,decisions=1000,research_runs=2000,dollars=25)
    assert not service.claim_decision(now=before['created']+3*86400+1)
    assert service.status()['state']=='deadline'


def test_campaign_cannot_exceed_users_25_dollar_limit(tmp_path):
    store,goal,_,_=setup(tmp_path)
    other=store.create_goal('Another investigation',['Evidence'],tmp_path)
    with pytest.raises(ValueError,match='25'):
        Campaign(store,other).configure(3,1000,2000,26)


def test_frontier_is_durable_paginated_and_classifies_metric_code_correctly(tmp_path):
    store,goal,_,workspace=setup(tmp_path)
    for bucket in ('clients','storage','streams'):
        path=workspace/bucket;path.mkdir()
        for i in range(50):(path/f'Metrics{i:03}.java').write_text('class Metrics {}')
    subprocess.run(['git','-C',str(workspace),'add','.'],check=True)
    subprocess.run(['git','-C',str(workspace),'-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-qm','Large tree'],check=True)
    frontier=Frontier(store,goal);summary=frontier.build()
    assert summary['files']==151 and summary['by_kind']['code']==150
    first=frontier.page('Metrics',limit=40)
    next_page=frontier.page('Metrics',after=first['next_after'],limit=40)
    assert first['total_matches']==150 and next_page['total_matches']==150
    assert len(first['matched_components'])==3
    assert first['entries'][-1]['locator']<next_page['entries'][0]['locator']
    assert len({f['bucket'] for f in frontier.suggestions(4)})==4
    fresh=Frontier(Store(store.path),goal)
    assert fresh.summary()['files']==151


async def test_research_campaign_completes_real_read_evaluate_loop(tmp_path):
    store,goal,service,workspace=setup(tmp_path)
    # The eighth decision can launch a useful read: assess its observations
    # after the worker runs, rather than cancelling it before its first read.
    store.execute('UPDATE campaigns SET stagnant_decisions=7 WHERE goal_id=?',(goal,))
    class Brain:
        def __init__(self):self.calls=0
        async def decide(self,state):
            self.calls+=1
            action=['discover','research','complete'][self.calls-1]
            spec=None if action=='complete' else ResearchPlan(scope=['system.md'],question='Which latency uncertainty should be resolved?',
                hypothesis='Queue polling interval is 30 seconds' if action=='research' else None,
                falsification='The registry gives another interval' if action=='research' else None)
            return Decision(action=action,rationale='Test orchestration, not live cognition',instructions='Read measured registry',task_id=None,priority=1,research_plan=spec)
    supervisor=CampaignSupervisor(store,goal,brain=Brain(),runtime=ReadOnlyRuntime(),research=Research(store,FixtureAgents()))
    result=await service.drive(supervisor=supervisor,poll_seconds=.001)
    assert result['state']=='completed' and result['decisions_spent']==3
    assert store.db.execute('SELECT spent FROM research_limits').fetchone()[0]==3
    assert 'supported' in Path(result['report_path']).read_text()


async def test_unobserved_frontier_blocks_premature_pause_and_unmetered_workers(tmp_path):
    store,goal,service,workspace=setup(tmp_path);Frontier(store,goal).build()
    supervisor=CampaignSupervisor(store,goal,runtime=ReadOnlyRuntime())
    for action in ('pause','complete','delegate','implement'):
        decision=Decision(action=action,rationale='Stop without evidence',instructions='Stop',task_id=None,priority=1)
        with pytest.raises(ValueError):
            await supervisor.dispatch(decision)


def test_manager_snapshot_stays_bounded_but_full_history_is_retained(tmp_path):
    store,goal,_,_=setup(tmp_path)
    for i in range(1000):
        store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',(goal,json.dumps({'event':i})))
    state=store.snapshot(goal)
    assert len(state['decisions'])==20
    assert state['decisions'][0]['event']==980
    assert state['history_counts']['decisions']==1000
    assert len(store.snapshot(goal,history_limit=None)['decisions'])==1000


def test_recover_read_only_attempt_retains_spend_without_replaying(tmp_path):
    store,goal,service,_=setup(tmp_path)
    from omnitrix.execution import Execution
    from omnitrix.control import Control
    job=Research(store).plan(goal,'discovery',ResearchPlan(scope=['system.md'],question='Which observation should we inspect?'))['job_id']
    Research(store).charge(goal)
    store.execute("UPDATE research_jobs SET attempts=1,status='interrupted' WHERE id=?",(job,))
    task=Execution(store,type('Adapter',(),{'name':'research'})()).assign(goal,'Read source',1)
    store.execute('''INSERT INTO task_runtime(task_id,goal_revision,policy_revision,state,spec,started,last_progress,deadline)
        VALUES(?,1,1,'uncertain',?,0,0,1)''',(task,json.dumps({'job_id':job})))
    service.recover_reads()
    assert store.db.execute('SELECT state FROM task_runtime').fetchone()[0]=='reconciled'
    assert store.db.execute('SELECT attempts FROM research_jobs').fetchone()[0]==1
    assert store.db.execute('SELECT spent FROM research_limits').fetchone()[0]==1
    assert store.db.execute('SELECT count(*) FROM research_runs').fetchone()[0]==0


async def test_bounded_context_can_recall_old_decisions_through_real_sdk_tool(tmp_path,monkeypatch):
    from agents.tool_context import ToolContext
    from omnitrix.manager import AgentBrain
    store,goal,_,_=setup(tmp_path)
    for i in range(50):store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',(goal,json.dumps({'event':i})))
    async def run(agent,prompt,**kwargs):
        assert len(json.loads(prompt)['decisions'])==8
        assert kwargs['session'] is None
        tool=agent.tools[0];payload=json.dumps({'section':'decisions','offset':0})
        context=ToolContext(context=None,tool_name=tool.name,tool_call_id='recall',tool_arguments=payload)
        recalled=json.loads(await tool.on_invoke_tool(context,payload))
        assert recalled[0]['event']==0
        frontier = next(tool for tool in agent.tools if tool.name == 'search_frontier')
        for index in range(3):
            assert frontier.is_enabled(None, agent)
            arguments = json.dumps({'query': 'system'})
            context = ToolContext(context=None, tool_name=frontier.name,
                tool_call_id=str(index), tool_arguments=arguments)
            await frontier.on_invoke_tool(context, arguments)
        assert all(not tool.is_enabled(None, agent) for tool in agent.tools)
        return type('Result',(),{'final_output':Decision(action='pause',rationale='Test recall',instructions='Inspect history',task_id=None,priority=1)})()
    monkeypatch.setattr('omnitrix.manager.Runner.run',run)
    await AgentBrain(store=store).decide(store.snapshot(goal))
    await store._campaign_client.close()


def test_unchanged_code_does_not_expire_only_because_campaign_crosses_midnight(tmp_path):
    store,goal,_,workspace=setup(tmp_path)
    path=workspace/'Metrics.java';path.write_text('class Metrics {}')
    job=Research(store).plan(goal,'discovery',ResearchPlan(scope=['Metrics.java'],question='What behavior does this code implement?'))['job_id']
    store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('r',?,'running',0)",(job,))
    source=ResearchTools(store,'r',str(workspace),['Metrics.java'],1).read('Metrics.java')
    store.execute('UPDATE research_sources SET observed=0 WHERE id=?',(source['id'],))
    assert not Knowledge(store).source(source['id'])['stale']


async def test_single_supported_evidence_gap_does_not_complete_broad_campaign(tmp_path):
    store,goal,service,workspace=setup(tmp_path)
    Frontier(store,goal).build()
    job=Research(store,FixtureAgents()).plan(goal,'research',ResearchPlan(scope=['system.md'],
        question='Which latency uncertainty should be resolved?',hypothesis='Queue polling interval is 30 seconds',
        falsification='The registry gives another interval'))['job_id']
    assert (await Research(store,FixtureAgents()).attempt(job))['status']=='supported'
    # Enough sources/branches/hypotheses, but no actual inefficiency mechanism.
    store.execute('UPDATE campaigns SET quality_spec=? WHERE goal_id=?',
        (json.dumps({'sources':1,'branches':1,'hypotheses':1,'mechanisms':1}),goal))
    assert service.quality()['actual']=={'sources':1,'branches':1,'hypotheses':1,'mechanisms':0}
    with pytest.raises(ValueError,match='requirements remain unmet'):
        await CampaignSupervisor(store,goal,runtime=ReadOnlyRuntime()).dispatch(
            Decision(action='complete',rationale='One supported audit',instructions='Complete',task_id=None,priority=1))


def test_mechanism_finding_requires_workload_and_falsification():
    from omnitrix.research_models import Finding
    from pydantic import ValidationError
    with pytest.raises(ValidationError,match='workload'):
        Finding(statement='Missing performance data',classification='claim',evidence_ids=[],category='mechanism')
    finding=Finding(statement='Repeated scan is quadratic in the number of inputs',classification='claim',
        evidence_ids=['actual-code'],category='mechanism',workload='N unique keys',
        falsification='An early index avoids the inner scan')
    assert finding.category=='mechanism'


def test_local_export_is_searchable_without_becoming_git_source_or_production_telemetry(tmp_path):
    store,goal,service,workspace=setup(tmp_path)
    frontier=Frontier(store,goal);frontier.build()
    exported=workspace/'build'/'observations';exported.mkdir(parents=True)
    (exported/'local-test.log').write_text('Local fixture test, not a broker workload.\nBUILD SUCCESSFUL\n')
    result=frontier.register(['build/observations/local-test.log'])
    assert result['by_origin']=={'git':1,'export':1}
    assert frontier.page(kind='logs')['entries'][0]['origin']=='export'
    assert frontier.suggestions(1)[0]['kind']=='logs'
    outside=tmp_path/'outside.log';outside.write_text('External file')
    with pytest.raises(ValueError):frontier.register([str(outside)])


def test_invalid_directory_scope_explains_repair_with_real_leaf_paths(tmp_path):
    store,goal,_,workspace=setup(tmp_path)
    leaf=workspace/'clients'/'src'/'Queue.java';leaf.parent.mkdir(parents=True);leaf.write_text('class Queue {}')
    subprocess.run(['git','-C',str(workspace),'add','.'],check=True)
    subprocess.run(['git','-C',str(workspace),'-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-qm','Component'],check=True)
    Frontier(store,goal).build()
    with pytest.raises(ValueError,match='clients/src/Queue.java'):
        Research(store).plan(goal,'discovery',ResearchPlan(scope=['clients'],question='Which code should be explored?'))


def test_recent_repair_feedback_is_not_hidden_by_old_pending_messages(tmp_path):
    from omnitrix.control import Control
    store,goal,_,_=setup(tmp_path);control=Control(store)
    for n in range(30):control.message(goal,'findings',{'old':n},direction='worker_to_manager')
    control.message(goal,'decision_rejected',{'error':'Use real leaf files'},direction='worker_to_manager')
    state=store.snapshot(goal)
    assert state['manager_feedback'][0]['body']['error']=='Use real leaf files'
    assert state['pending_messages'][-1]['kind']=='decision_rejected'


async def test_ordinary_supervisor_entry_point_cannot_reset_campaign_decisions(tmp_path):
    from omnitrix.supervisor import Supervisor
    store,goal,service,_=setup(tmp_path)
    store.execute("UPDATE campaigns SET state='running',decisions_spent=max_decisions WHERE goal_id=?",(goal,))
    class Brain:
        async def decide(self,state):pytest.fail('Budget must stop before inference')
    supervisor=Supervisor(store,goal,brain=Brain(),runtime=ReadOnlyRuntime())
    assert (await supervisor.advance(steps=1000))['blocked']
    assert service.status()['state']=='decision_budget'
    with pytest.raises(ValueError,match='requirements'):
        await supervisor.dispatch(Decision(action='complete',rationale='Alternate route',instructions='Complete',task_id=None,priority=1))


async def test_legacy_manager_cannot_bypass_campaign_read_only_contract(tmp_path):
    from omnitrix.manager import Manager
    store,goal,_,_=setup(tmp_path)
    with pytest.raises(ValueError,match='campaign owner'):
        await Manager(store,None,None).resume(goal)
